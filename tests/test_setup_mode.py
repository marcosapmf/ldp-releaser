"""Settings missing or rejected while nobody can answer the terminal setup:
LDP Releaser serves a setup page and API instead, checks the answers as the
terminal setup does, saves them and restarts."""

import json
import stat
import time
import tomllib
import unittest
import urllib.error
import urllib.request

from tests.harness import PERSONAL, TEAM, AppTestCase


class SetupPageTestCase(AppTestCase):

    def api(self, method, path, payload=None, token=True):
        headers = {"Content-Type": "application/json"}

        if token:
            headers["Authorization"] = f"Bearer {(self.config_dir() / 'api-token').read_text().strip()}"

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}",
            data=None if payload is None else json.dumps(payload).encode(),
            headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exception:
            return exception.code, json.loads(exception.read() or b"null")

    def config_dir(self):
        return self.temp / "config" / "ldp-releaser"

    def field(self, key):
        return {field["key"]: field for field in self.call("/api/setup")[1]["fields"]}[key]

    def page(self, path):
        with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30) as response:
            return response.read().decode()

    def personal(self):
        with (self.config_dir() / "config.toml").open("rb") as file:
            return tomllib.load(file)

    def personal_text(self):
        return PERSONAL.format(
            asah_checkout=self.asah_checkout, checkout=self.checkout, port=self.port)

    def start_app(self):

        # The setup page has no /api/config, so wait for its own state.

        super_call = self.call
        self.call = lambda path, payload=None: super_call(
            "/api/setup" if path == "/api/config" else path, payload)

        try:
            super().start_app()
        finally:
            self.call = super_call

    def wait_for_releaser(self):
        deadline = time.time() + 30

        while time.time() < deadline:
            try:
                if self.call("/api/config")[0] == 200:
                    return
            except OSError:
                pass

            time.sleep(0.1)

        self.fail("LDP Releaser did not restart")


class MissingTokenTest(SetupPageTestCase):

    def write_settings(self):
        super().write_settings()

        # The Jira token was removed, for example after it expired.

        text = self.personal_text().replace(
            'email = "tester@example.com"\ntoken = "secret-token"', 'email = "tester@example.com"')

        (self.config_dir() / "config.toml").write_text(text)

    def test_page_lists_what_is_missing(self):
        status, body = self.call("/api/setup")

        self.assertEqual(200, status)
        self.assertEqual(["jira.token is missing"], body["problems"])
        self.assertEqual([], body["team"]["problems"])
        self.assertEqual(
            ["jenkins.user", "jenkins.token", "jira.email", "jira.token", "charts.checkout",
             "asah.checkout"],
            [field["key"] for field in body["fields"]])

        fields = {field["key"]: field for field in body["fields"]}

        self.assertEqual("tester", fields["jenkins.user"]["value"])
        self.assertIsNone(fields["jenkins.token"]["value"])
        self.assertTrue(fields["jenkins.token"]["set"])
        self.assertEqual("jira.token is missing", fields["jira.token"]["problem"])
        self.assertFalse(fields["jira.token"]["set"])
        self.assertFalse(fields["asah.checkout"]["required"])
        self.assertIn("Save and start", self.page("/"))

        # A dashboard learns why the API is not there.

        status, body = self.call("/api/v1/status")

        self.assertEqual(503, status)
        self.assertEqual(["jira.token is missing"], body["problems"])

    def test_a_required_value_is_asked_for(self):
        status, body = self.call("/api/setup", {"values": {}})

        self.assertEqual(400, status)
        self.assertEqual({"jira.token": "Atlassian API token is required"}, body["errors"])

    def test_a_rejected_value_saves_nothing(self):
        status, body = self.call(
            "/api/setup",
            {"values": {"charts.checkout": str(self.temp / "nowhere"), "jira.token": "wrong-token"}})

        self.assertEqual(400, status)
        self.assertEqual("Jira rejected that email and token", body["errors"]["jira.token"])
        self.assertIn("is not a git checkout", body["errors"]["charts.checkout"])
        self.assertNotIn("token", self.personal()["jira"])

    def test_a_dashboard_sets_it_up_through_the_api(self):
        status, body = self.api("GET", "/api/v1/openapi.json", token=False)

        self.assertEqual(200, status)
        self.assertIn("/api/v1/setup", body["paths"])
        self.assertEqual(401, self.api("GET", "/api/v1/setup", token=False)[0])

        status, body = self.api("GET", "/api/v1/setup")

        self.assertEqual(200, status)
        self.assertEqual(["jira.token is missing"], body["problems"])

        self.assertEqual(
            202, self.api("POST", "/api/v1/setup", {"values": {"jira.token": "secret-token"}})[0])

        self.wait_for_releaser()

        # Running, it answers the same question with nothing left to do.

        status, body = self.api("GET", "/api/v1/setup")

        self.assertEqual(200, status)
        self.assertEqual([], body["problems"])
        self.assertEqual(409, self.api("POST", "/api/v1/setup", {"values": {}})[0])

        personal = self.personal()

        self.assertEqual("secret-token", personal["jira"]["token"])
        self.assertEqual("tester", personal["jenkins"]["user"])
        self.assertEqual(0o600, stat.S_IMODE((self.config_dir() / "config.toml").stat().st_mode))

    def test_other_sites_cannot_save(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/setup",
            data=json.dumps({"values": {"jira.token": "secret-token"}}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://elsewhere.example"},
            method="POST")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=30)

        self.assertEqual(403, raised.exception.code)
        self.assertNotIn("token", self.personal()["jira"])


class RejectedTokenTest(SetupPageTestCase):

    def write_settings(self):
        super().write_settings()

        # Jenkins no longer accepts the saved token, for example after a
        # rotation.

        text = self.personal_text().replace(
            'user = "tester"\ntoken = "secret-token"', 'user = "tester"\ntoken = "expired-token"')

        (self.config_dir() / "config.toml").write_text(text)

    def test_the_rejected_token_is_asked_for_again(self):
        field = self.field("jenkins.token")

        self.assertEqual("Jenkins rejected your user and token", field["problem"])
        self.assertTrue(field["set"])

        # Keeping the saved token checks it again.

        status, body = self.call("/api/setup", {"values": {}})

        self.assertEqual(400, status)
        self.assertEqual(
            {"jenkins.token": "Jenkins rejected that user and token (401)"}, body["errors"])

        self.assertEqual(
            202, self.call("/api/setup", {"values": {"jenkins.token": "secret-token"}})[0])

        self.wait_for_releaser()

        self.assertEqual("secret-token", self.personal()["jenkins"]["token"])


class MissingTeamFileTest(SetupPageTestCase):

    def write_settings(self):
        super().write_settings()

        (self.config_dir() / "ldp-releaser.team.toml").unlink()

    def test_the_team_file_comes_first(self):
        status, body = self.call("/api/setup")

        self.assertEqual(200, status)
        self.assertEqual([], body["fields"])
        self.assertIn("team settings not found", body["team"]["problems"][0])

        status, body = self.call("/api/setup", {"values": {"jira.token": "secret-token"}})

        self.assertEqual(409, status)
        self.assertEqual("Set up the team settings first", body["error"])

        status, body = self.call("/api/setup/team", {"path": str(self.temp / "missing.toml")})

        self.assertEqual(409, status)
        self.assertIn("does not exist", body["error"])

        shared = self.temp / "shared.team.toml"
        shared.write_text(TEAM.format(url=self.fakes.url))

        self.assertEqual(202, self.call("/api/setup/team", {"path": str(shared)})[0])

        self.wait_for_releaser()

        team_file = self.config_dir() / "ldp-releaser.team.toml"

        self.assertEqual(shared.read_text(), team_file.read_text())
        self.assertEqual(0o600, stat.S_IMODE(team_file.stat().st_mode))
        self.assertNotIn("team", self.personal())


if __name__ == "__main__":
    unittest.main()
