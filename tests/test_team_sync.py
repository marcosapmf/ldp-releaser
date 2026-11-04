"""A team file without settings this version needs: started without a
terminal, LDP Releaser offers to sync it from 1Password, and never checks
1Password while the file is complete."""

import json
import stat
import time
import tomllib
import unittest
import urllib.error
import urllib.request

from tests.harness import TEAM, AppTestCase

# A setting a newer version needs, removed from the local copy.

MISSING = 'prd_environment = "prd"'


class TeamSyncTestCase(AppTestCase):

    local_team = None

    def write_settings(self):
        super().write_settings()

        if self.local_team:
            self.team_file().write_text(self.local_team(self.team_text()))

        with (self.config_dir() / "config.toml").open("a") as file:
            file.write('\n[team]\nonepassword_item = "team file"\nonepassword_vault = "Shared"\n')

        self.publish(self.team_text())

    def config_dir(self):
        return self.temp / "config" / "ldp-releaser"

    def op_calls(self):
        path = self.temp / "op" / "calls"

        if not path.exists():
            return []

        return path.read_text().splitlines()

    def page(self, path):
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{self.port}{path}", timeout=30) as response:
                return response.status, response.read().decode()
        except urllib.error.HTTPError as exception:
            return exception.code, exception.read().decode()

    def publish(self, text):
        directory = self.temp / "op" / "Shared"
        directory.mkdir(parents=True, exist_ok=True)

        (directory / "team file").write_text(text)

    def start_app(self):

        # The sync page has no /api/config, so wait for its own state.

        if not self.local_team:
            return super().start_app()

        super_call = self.call
        self.call = lambda path, payload=None: super_call(
            "/api/team-sync" if path == "/api/config" else path, payload)

        try:
            super().start_app()
        finally:
            self.call = super_call

    def team_file(self):
        return self.config_dir() / "ldp-releaser.team.toml"

    def team_text(self):
        return TEAM.format(url=self.fakes.url)


class CompleteTeamFileTest(TeamSyncTestCase):

    def test_1password_is_not_checked(self):
        self.assertEqual(200, self.call("/api/config")[0])
        self.assertEqual(404, self.call("/api/team-sync")[0])
        self.assertEqual([], self.op_calls())


class MissingSettingTest(TeamSyncTestCase):

    local_team = staticmethod(lambda text: text.replace(MISSING, ""))

    def test_sync_page_saves_the_file_and_restarts(self):
        status, body = self.call("/api/team-sync")

        self.assertEqual(200, status)
        self.assertEqual("team file", body["item"])
        self.assertEqual(["release.prd_environment is missing"], body["problems"])
        self.assertEqual("Shared", body["vault"])

        status, html = self.page("/")

        self.assertEqual(200, status)
        self.assertIn("Sync from 1Password", html)

        # A dashboard learns why the API is not there.

        status, body = self.call("/api/v1/status")

        self.assertEqual(503, status)
        self.assertEqual(["release.prd_environment is missing"], body["problems"])
        self.assertEqual([], self.op_calls())

        self.assertEqual(
            202, self.call("/api/team-sync", {"item": "team file", "vault": "Shared"})[0])

        deadline = time.time() + 30

        while time.time() < deadline:
            try:
                if self.call("/api/config")[0] == 200:
                    break
            except OSError:
                pass

            time.sleep(0.1)
        else:
            self.fail("LDP Releaser did not restart")

        self.assertEqual(self.team_text(), self.team_file().read_text())
        self.assertEqual(0o600, stat.S_IMODE(self.team_file().stat().st_mode))
        self.assertEqual(["document get team file --vault Shared"], self.op_calls())

    def test_another_item_is_remembered(self):
        directory = self.temp / "op" / "Other"
        directory.mkdir()

        (directory / "moved file").write_text(self.team_text())

        self.assertEqual(
            202, self.call("/api/team-sync", {"item": "moved file", "vault": "Other"})[0])

        deadline = time.time() + 30

        while time.time() < deadline:
            try:
                if self.call("/api/config")[0] == 200:
                    break
            except OSError:
                pass

            time.sleep(0.1)

        with (self.config_dir() / "config.toml").open("rb") as file:
            personal = tomllib.load(file)

        self.assertEqual(
            {"onepassword_item": "moved file", "onepassword_vault": "Other"}, personal["team"])
        self.assertEqual("tester", personal["jenkins"]["user"])

    def test_file_in_1password_without_them_changes_nothing(self):
        self.publish(self.team_text().replace(MISSING, ""))

        status, body = self.call("/api/team-sync", {"item": "team file", "vault": "Shared"})

        self.assertEqual(409, status)
        self.assertIn("lacks them too: release.prd_environment is missing", body["error"])
        self.assertEqual(self.team_text().replace(MISSING, ""), self.team_file().read_text())

    def test_unknown_item_is_reported(self):
        status, body = self.call("/api/team-sync", {"item": "nothing", "vault": "Shared"})

        self.assertEqual(409, status)
        self.assertIn("isn't an item", body["error"])

    def test_other_sites_cannot_sync(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/team-sync",
            data=json.dumps({"item": "team file", "vault": "Shared"}).encode(),
            headers={"Content-Type": "application/json", "Origin": "https://elsewhere.example"},
            method="POST")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=30)

        self.assertEqual(403, raised.exception.code)
        self.assertEqual([], self.op_calls())


if __name__ == "__main__":
    unittest.main()
