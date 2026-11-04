"""The API for other tools: the page's information and actions under /api/v1,
behind a token."""

import json
import re
import stat
import tempfile
import time
import tomllib
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

from ldp_releaser import config
from tests.harness import ROOT, AppTestCase

DASHBOARD = "http://dashboard.example:3000"


class ApiTest(AppTestCase):

    def setUp(self):
        super().setUp()

        self.auto_confirm = False

    def write_settings(self):
        super().write_settings()

        with (self.temp / "config" / "ldp-releaser" / "config.toml").open("a") as file:
            file.write(f'\n[api]\nallowed_origins = ["{DASHBOARD}"]\n')

    def request(self, method, path, payload=None, headers=None, token=True):
        data = None

        if payload is not None:
            data = json.dumps(payload).encode()

        headers = dict(headers or {})

        if token:
            headers["Authorization"] = f"Bearer {self.token()}"

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null"), response.headers
        except urllib.error.HTTPError as exception:
            return exception.code, json.loads(exception.read() or b"null"), exception.headers

    def token(self):
        return (self.temp / "config" / "ldp-releaser" / "api-token").read_text().strip()

    def wait_for_attention(self, kind, timeout=60):
        deadline = time.time() + timeout

        while time.time() < deadline:
            items = self.request("GET", "/api/v1/attention")[1]

            for item in items:
                if item["kind"] == kind:
                    return item

            time.sleep(0.1)

        self.fail(f"nothing waiting for {kind}")

    def test_calls_need_the_token(self):
        token_file = self.temp / "config" / "ldp-releaser" / "api-token"

        self.assertEqual(0o600, stat.S_IMODE(token_file.stat().st_mode))
        self.assertEqual(401, self.request("GET", "/api/v1/runs", token=False)[0])
        self.assertEqual(
            401,
            self.request("GET", "/api/v1/runs", headers={"Authorization": "Bearer wrong"},
                         token=False)[0])
        self.assertEqual(
            401, self.request("POST", "/api/v1/runs/abc/cancel", {}, token=False)[0])
        self.assertEqual(200, self.request("GET", "/api/v1/openapi.json", token=False)[0])

        status, body, _ = self.request("GET", "/api/v1")

        self.assertEqual(200, status)
        self.assertEqual(1, body["version"])

    def test_every_documented_path_is_served(self):
        spec = json.loads((ROOT / "ldp_releaser" / "static" / "openapi.json").read_text())

        # Calls that would start something get an empty body or an unknown
        # run, so they fail validation instead.

        for path, methods in spec["paths"].items():
            concrete = path.replace("{runId}", "missing").replace("{stage}", "asahBuild")

            for method in methods:
                if method == "get":
                    status, body, _ = self.request("GET", concrete)

                    if "{runId}" in path:
                        self.assertEqual((404, "Run not found"), (status, body["error"]), path)
                    else:
                        self.assertEqual(200, status, (path, body))
                elif path == "/api/v1/gcloud/login":
                    continue
                else:
                    status, body, _ = self.request("POST", concrete, {})

                    self.assertIn(status, (400, 404, 409), (path, body))
                    self.assertNotEqual("Not found", body["error"], path)

        # And every route of the server is documented.

        source = (ROOT / "ldp_releaser" / "server.py").read_text()
        routes = set(re.findall(r'path == "(/api/[\w/-]+)"', source))
        documented = {path.replace("/api/v1", "/api", 1) for path in spec["paths"]}

        self.assertEqual(set(), routes - documented - {"/api/v1", "/api/v1/openapi.json"})

    def test_release_is_answered_through_attention(self):
        status, body, _ = self.request(
            "POST", "/api/v1/runs", {"releaseEnvironments": ["int"], "tagSuffix": "20260101-1234567"})

        self.assertEqual(201, status, body)

        run_id = body["id"]
        item = self.wait_for_attention("confirm-rollout")

        self.assertEqual(run_id, item["runId"])
        self.assertEqual("rollout", item["stage"])

        status_body = self.request("GET", "/api/v1/status")[1]

        self.assertEqual(1, status_body["attention"])
        self.assertEqual(run_id, status_body["latest"]["release"]["id"])
        self.assertEqual(1, status_body["running"])

        # The page's endpoints still refuse other sites, even an origin the
        # API allows.

        self.assertEqual(
            403,
            self.request("POST", f"/api/runs/{run_id}/confirm-rollout", {},
                         headers={"Content-Type": "application/json", "Origin": DASHBOARD},
                         token=False)[0])

        [confirm] = item["actions"]

        self.assertEqual(f"/api/v1/runs/{run_id}/confirm-rollout", confirm["path"])
        self.assertEqual(202, self.request(confirm["method"], confirm["path"], {})[0])

        item = self.wait_for_attention("close-tickets")

        self.assertEqual(
            ["close-tickets", "keep-tickets"], [action["kind"] for action in item["actions"]])

        keep = item["actions"][1]

        self.assertEqual(202, self.request(keep["method"], keep["path"], {})[0])

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual([], self.request("GET", "/api/v1/attention")[1])

        [listed] = self.request("GET", "/api/v1/runs?kind=release&status=success&limit=1")[1]

        self.assertEqual(run_id, listed["id"])
        self.assertNotIn("log", listed["stages"]["jira"])
        self.assertEqual("Release INT Environment", listed["title"])
        self.assertEqual([], self.request("GET", "/api/v1/runs?kind=composer")[1])
        self.assertEqual(400, self.request("GET", "/api/v1/runs?limit=0")[0])

        detail = self.request("GET", f"/api/v1/runs/{run_id}")[1]

        self.assertIn("log", detail["stages"]["jira"])
        self.assertEqual("release", detail["kind"])

    def test_browser_pages_on_allowed_origins_only(self):
        preflight = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/runs", method="OPTIONS",
            headers={"Access-Control-Request-Method": "POST", "Origin": DASHBOARD})

        with urllib.request.urlopen(preflight, timeout=30) as response:
            self.assertEqual(204, response.status)
            self.assertEqual(DASHBOARD, response.headers["Access-Control-Allow-Origin"])
            self.assertIn("Authorization", response.headers["Access-Control-Allow-Headers"])

        status, _, headers = self.request(
            "OPTIONS", "/api/v1/runs", headers={"Origin": "https://elsewhere.example"},
            token=False)

        self.assertEqual(403, status)
        self.assertIsNone(headers["Access-Control-Allow-Origin"])

        status, _, headers = self.request("GET", "/api/v1/status", headers={"Origin": DASHBOARD})

        self.assertEqual(200, status)
        self.assertEqual(DASHBOARD, headers["Access-Control-Allow-Origin"])

        _, _, headers = self.request(
            "GET", "/api/v1/status", headers={"Origin": "https://elsewhere.example"})

        self.assertIsNone(headers["Access-Control-Allow-Origin"])

    def test_other_host_names_are_refused(self):

        # A page can point its own host name at 127.0.0.1 (DNS rebinding).

        status, _, _ = self.request(
            "GET", "/api/v1/status", headers={"Host": f"attacker.example:{self.port}"})

        self.assertEqual(403, status)

    def test_invalid_body_is_refused(self):
        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}/api/v1/composer-runs", data=b"{not json",
            headers={"Authorization": f"Bearer {self.token()}"}, method="POST")

        with self.assertRaises(urllib.error.HTTPError) as raised:
            urllib.request.urlopen(request, timeout=30)

        self.assertEqual(400, raised.exception.code)


class AllowedOriginsSettingTest(unittest.TestCase):

    def test_setup_keeps_the_allowed_origins(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.toml"

            with mock.patch.object(config, "PERSONAL_FILE", path):
                config.write_personal(
                    {"api": {"allowed_origins": [DASHBOARD, "http://localhost:5173"]},
                     "jenkins": {"user": "tester"}})

            with path.open("rb") as file:
                personal = tomllib.load(file)

        self.assertEqual(
            [DASHBOARD, "http://localhost:5173"], personal["api"]["allowed_origins"])
        self.assertEqual([], config.validate({"api": personal["api"]}, {"api.allowed_origins": "list"}))


if __name__ == "__main__":
    unittest.main()
