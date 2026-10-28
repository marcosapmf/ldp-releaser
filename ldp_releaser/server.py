"""The local web page and its JSON API. Only answers on 127.0.0.1."""

import json
import re
import threading
import urllib.error
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import composer, failure, fixes, gcloud, health, release

INDEX_FILE = Path(__file__).parent / "static" / "index.html"


class Handler(BaseHTTPRequestHandler):

    account = None
    gcloud_login = gcloud.Login()
    jenkins = None
    jira = None
    port = None
    settings = None

    def do_GET(self):
        if not self._is_local_host():
            return self._send_json(403, {"error": "Forbidden host"})

        url = urllib.parse.urlparse(self.path)

        if url.path == "/":
            return self._send(200, INDEX_FILE.read_bytes(), "text/html; charset=utf-8")

        if url.path == "/api/branches":
            return self._branches()

        if url.path == "/api/config":
            return self._send_json(200, self._config())

        if url.path == "/api/gcloud/login":
            state = self.gcloud_login.state()

            if "check" in urllib.parse.parse_qs(url.query):
                state["problem"] = gcloud.login_problem()

            return self._send_json(200, state)

        if url.path == "/api/health":
            refresh = "refresh" in urllib.parse.parse_qs(url.query)

            return self._send_json(
                200,
                health.checks(self.jenkins, self.jira, self.settings, refresh))

        if url.path == "/api/release-tickets":
            try:
                return self._send_json(200, release.open_release_tickets(self.jira))
            except (OSError, RuntimeError, urllib.error.URLError) as exception:
                return self._send_json(502, {"error": str(exception)})

        if url.path == "/api/runs":
            with release.runs_lock:
                body = json.dumps(
                    sorted(
                        release.runs.values(), key=lambda run: run["createdAt"],
                        reverse=True))

            return self._send(200, body.encode(), "application/json")

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):

        # Reject cross-site requests so another page in the browser cannot
        # start a release through this server.

        content_type = self.headers.get("Content-Type", "")
        origin = self.headers.get("Origin")

        if (not self._is_local_host() or
                not content_type.startswith("application/json") or
                (origin and urllib.parse.urlparse(origin).netloc !=
                 self.headers.get("Host"))):

            return self._send_json(403, {"error": "Forbidden"})

        if self.path == "/api/runs":
            return self._start_run()

        if self.path == "/api/composer-runs":
            return self._start_composer_run()

        if self.path == "/api/gcloud/login":
            return self._send_json(202, self.gcloud_login.start())

        if self.path == "/api/gcloud/login/code":
            payload = self._payload()

            if not self.gcloud_login.send_code(str(payload.get("code", ""))):
                return self._send_json(409, {"error": "No gcloud login is waiting for a code"})

            return self._send_json(202, self.gcloud_login.state())

        match = re.fullmatch(r"/api/runs/(\w+)/stages/([\w-]+)/failure", self.path)

        if match:
            return self._explain_failure(match.group(1), match.group(2))

        match = re.fullmatch(r"/api/runs/(\w+)/stages/asahBuild/fix", self.path)

        if match:
            return self._fix(match.group(1))

        match = re.fullmatch(
            r"/api/runs/(\w+)/(cancel|close-tickets|confirm-rollout|keep-tickets)",
            self.path)

        if match:
            return self._run_action(match.group(1), match.group(2))

        self._send_json(404, {"error": "Not found"})

    def log_message(self, format, *args):
        pass

    def _branches(self):
        try:
            return self._send_json(
                200,
                {
                    "asah": release.recent_branches(
                        self.jenkins, self.settings["asah.build_job"],
                        self.settings["asah.branch_parameter"]),
                    "faro": release.recent_branches(
                        self.jenkins, self.settings["faro.build_job"],
                        self.settings["faro.branch_parameter"]),
                })
        except (OSError, urllib.error.URLError) as exception:
            return self._send_json(502, {"error": str(exception)})

    def _config(self):
        settings = self.settings

        return {
            "account": self.account,
            "asahTagFormat": settings["asah.tag_format"],
            "composer": {
                "enabled": composer.composer_enabled(settings),
                "environments": {
                    environment: settings["gcloud.environments"][environment]
                    for environment in settings.environments()
                    if environment in (settings["gcloud.environments"] or {})
                },
            },
            "environments": settings.environments(),
            "faroBuildEnvironments": settings["faro.build_environments"],
            "faroEnvironments": settings.faro_environments(),
            "faroTagFormat": settings["faro.tag_format"],
            "fixesEnabled": fixes.enabled(settings),
            "mainBranch": settings["asah.main_branch"],
            "jiraUrl": settings["jira.url"].rstrip("/"),
            "prdEnvironment": settings["release.prd_environment"],
            "productionBranch": settings["charts.production_branch"],
            "productionEnvironments": settings["charts.production.environments"],
            "title": settings["jira.title"],
        }

    def _explain_failure(self, run_id, stage_name):
        with release.runs_lock:
            run = release.runs.get(run_id)
            stage = (run or {}).get("stages", {}).get(stage_name)

            if stage is None:
                return self._send_json(404, {"error": "Step not found"})

            if stage["status"] != "failed" or not stage["url"]:
                return self._send_json(
                    409, {"error": "The step is not a failed Jenkins build"})

            build_url = stage["url"]

        try:
            explanation = failure.explain(self.jenkins, build_url)
        except (OSError, urllib.error.URLError) as exception:
            return self._send_json(
                502, {"error": f"Could not read the build log: {exception}"})

        release.update_stage(run, stage_name, failure=explanation)

        return self._send_json(200, explanation)

    def _fix(self, run_id):
        if not fixes.enabled(self.settings):
            return self._send_json(
                409,
                {
                    "error": "Fixes need asah.dependency_file, asah.main_branch and "
                             "jira.fix_security_level in the team file and asah.checkout "
                             "in your settings",
                })

        with release.runs_lock:
            run = release.runs.get(run_id)

        if run is None:
            return self._send_json(404, {"error": "Run not found"})

        problem = fixes.start(self.jira, run, "asahBuild")

        if problem:
            return self._send_json(409, {"error": problem})

        return self._send_json(202, {"id": run_id})

    def _is_local_host(self):
        return self.headers.get("Host") in (
            f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _run_action(self, run_id, action):
        with release.runs_lock:
            run = release.runs.get(run_id)

            if run is None:
                return self._send_json(404, {"error": "Run not found"})

            if action == "cancel":
                run["cancelRequested"] = True
            elif action == "confirm-rollout":
                if run["stages"]["rollout"]["status"] != "running":
                    return self._send_json(
                        409, {"error": "The run is not waiting for a rollout"})

                run["rolloutConfirmed"] = True
            else:
                if (run["prdRelease"] or run["ticketsDecision"] is not None or
                        run["stages"]["closeTickets"]["status"] != "running"):

                    return self._send_json(
                        409,
                        {
                            "error": "The run is not waiting for a decision on "
                                     "the Jira tickets",
                        })

                if action == "close-tickets":
                    run["ticketsDecision"] = "close"
                else:
                    run["ticketsDecision"] = "keep"

        release.on_change(run)

        return self._send_json(202, {"id": run["id"]})

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _payload(self):
        length = int(self.headers.get("Content-Length", 0))

        return json.loads(self.rfile.read(length) or b"{}")

    def _start_composer_run(self):
        try:
            environments, ref = composer.validate(self._payload())
        except ValueError as exception:
            return self._send_json(400, {"error": str(exception)})

        run = composer.create_run(environments, ref)

        start_run(self.jenkins, self.jira, run)

        return self._send_json(201, {"id": run["id"]})

    def _start_run(self):
        payload = self._payload()

        problems = [
            check["problem"]
            for check in health.checks(self.jenkins, self.jira, self.settings)
            if check["required"] and not check["ok"]
        ]

        if problems:
            return self._send_json(
                400, {"error": "Setup problem: " + "; ".join(problems)})

        try:
            values = release.validate_run(payload, verify_tags=True)
        except ValueError as exception:
            return self._send_json(400, {"error": str(exception)})

        discard_tickets = []
        reuse_ticket = None

        if values[3]:
            try:
                discard_tickets, reuse_ticket = release.choose_release_tickets(
                    self.jira, payload.get("tickets") or {})
            except ValueError as exception:
                return self._send_json(400, {"error": str(exception)})
            except (OSError, RuntimeError, urllib.error.URLError) as exception:
                return self._send_json(
                    502,
                    {"error": f"Could not look up open release tickets: {exception}"})

        run = release.create_run(*values, discard_tickets, reuse_ticket)

        start_run(self.jenkins, self.jira, run)

        return self._send_json(201, {"id": run["id"]})


def serve(account, jenkins, jira, settings, unfinished_runs):
    Handler.account = account
    Handler.jenkins = jenkins
    Handler.jira = jira
    Handler.port = settings["server.port"]
    Handler.settings = settings

    for run in unfinished_runs:
        start_run(jenkins, jira, run)

    server = ThreadingHTTPServer(("127.0.0.1", Handler.port), Handler)

    print(f"LDP Releaser running on http://127.0.0.1:{Handler.port}", flush=True)

    server.serve_forever()


def start_run(jenkins, jira, run):
    if run.get("kind") == "composer":
        threading.Thread(args=(run,), daemon=True, target=composer.execute).start()
    else:
        threading.Thread(
            args=(jenkins, jira, run), daemon=True, target=release.execute).start()
