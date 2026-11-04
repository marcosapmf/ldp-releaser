"""Started with nobody to answer the terminal setup (for example by the
Claude Code skill or a dashboard) while settings are missing or rejected,
LDP Releaser serves a setup page and its API instead of the releaser. Once
the settings are saved it restarts; unfinished runs resume as after any
restart."""

import json
import shutil
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import api, composer, config, team_sync, wizard

OPENAPI_FILE = Path(__file__).parent / "static" / "openapi.json"
SETUP_FILE = Path(__file__).parent / "static" / "setup.html"


class Handler(BaseHTTPRequestHandler):

    personal = None
    port = None
    rejected = None

    def do_GET(self):
        if not self._is_local_host():
            return self._send_json(403, {"error": "Forbidden host"})

        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            return self._send(200, SETUP_FILE.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/setup":
            return self._send_json(200, describe(self.personal, self.rejected))

        if path == "/api/v1/openapi.json":
            return self._send(200, OPENAPI_FILE.read_bytes(), "application/json")

        if path == "/api/v1/setup":
            if not api.authorized(self.headers.get("Authorization")):
                return self._send_json(401, {"error": "Missing or wrong API token"})

            return self._send_json(200, describe(self.personal, self.rejected))

        if path.startswith("/api/"):
            return self._unavailable()

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):
        if not self._is_local_host():
            return self._send_json(403, {"error": "Forbidden host"})

        path = urllib.parse.urlparse(self.path).path

        if path.startswith("/api/v1/"):

            # Other tools send the API token, as for the releaser's API.

            if not api.authorized(self.headers.get("Authorization")):
                return self._send_json(401, {"error": "Missing or wrong API token"})

            path = "/api" + path[len("/api/v1"):]
        elif not self._is_same_site():
            return self._send_json(403, {"error": "Forbidden"})

        if path not in ("/api/setup", "/api/setup/team"):
            return self._unavailable()

        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "The body is not valid JSON"})

        if not isinstance(body, dict):
            return self._send_json(400, {"error": "The body must be a JSON object"})

        if path == "/api/setup/team":
            return self._save_team(body)

        return self._save_personal(body)

    def log_message(self, format, *args):
        pass

    def _is_local_host(self):
        return self.headers.get("Host") in (
            f"127.0.0.1:{self.port}", f"localhost:{self.port}")

    def _is_same_site(self):

        # The same checks as the releaser's page: only this page may save
        # settings, so another site in the browser cannot.

        content_type = self.headers.get("Content-Type", "")
        origin = self.headers.get("Origin")

        return content_type.startswith("application/json") and (
            not origin or urllib.parse.urlparse(origin).netloc == self.headers.get("Host"))

    def _restart(self):
        threading.Thread(daemon=True, target=team_sync.restart).start()

        return self._send_json(202, {"restarting": True})

    def _save_personal(self, body):
        values = body.get("values")

        if not isinstance(values, dict):
            return self._send_json(400, {"error": "Send the settings as an object in values"})

        if team_sync.problems():
            return self._send_json(409, {"error": "Set up the team settings first"})

        try:
            errors = save(self.personal, self.rejected, values)
        except wizard.SetupError as exception:
            return self._send_json(502, {"error": str(exception)})

        if errors:
            return self._send_json(
                400, {"error": "Some settings were not accepted", "errors": errors})

        return self._restart()

    def _save_team(self, body):
        item = str(body.get("item") or "").strip()
        path = str(body.get("path") or "").strip()

        if path:
            problem = team_sync.use_file(self.personal, path)
        elif item:
            problem = team_sync.sync(self.personal, item, str(body.get("vault") or "").strip() or None)
        else:
            return self._send_json(
                400, {"error": "Enter the 1Password item of the team file, or the path to it"})

        if problem:
            return self._send_json(409, {"error": problem})

        return self._restart()

    def _send(self, status, body, content_type):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, status, payload):
        self._send(status, json.dumps(payload).encode(), "application/json")

    def _unavailable(self):
        return self._send_json(
            503,
            {
                "error": "LDP Releaser needs its settings first; finish the setup on "
                         "the page, or through /api/v1/setup",
                "problems": problems(self.personal, self.rejected),
            })


def describe(personal, rejected):

    # What the setup page shows: the team file first, since the personal
    # settings are checked against it.

    team_problems = team_sync.problems() or []

    team = {
        "item": config.get_value(personal, "team.onepassword_item"),
        "onePassword": shutil.which("op") is not None,
        "problems": team_problems,
        "vault": config.get_value(personal, "team.onepassword_vault"),
    }

    if team_problems:
        return {"fields": [], "problems": team_problems, "team": team}

    current = config.with_environment(personal)
    personal_problems = config.validate_personal(current)

    entries = []

    for key, label in fields(config.Settings(config.read_toml(config.TEAM_FILE))):
        kind = config.PERSONAL_SCHEMA[key]
        value = config.get_value(current, key)

        problem = rejected.get(key) or next(
            (problem for problem in personal_problems if problem.startswith(key + " ")), None)

        entries.append({
            "environment": config.environment_value(key, fallbacks=False) is not None,
            "key": key,
            "kind": kind,
            "label": label,
            "problem": problem,
            "required": key not in config.OPTIONAL_KEYS,
            "set": value not in (None, ""),
            "value": None if kind == "secret" else value,
        })

    return {"fields": entries, "problems": problems(personal, rejected), "team": team}


def fields(settings):

    # The personal settings the page asks for, in the terminal setup's order.
    # The Asah clone is only asked for when the team has the Composer setup.

    labels = wizard.labels(settings)

    keys = ["jenkins.user", "jenkins.token", "jira.email", "jira.token", "charts.checkout"]

    if composer.composer_enabled(settings):
        keys.append("asah.checkout")

    return [(key, labels[key]) for key in keys]


def problems(personal, rejected=None):

    # Why LDP Releaser cannot start, or an empty list. The team file comes
    # first, since the personal settings are checked against it.

    team_problems = team_sync.problems()

    if team_problems:
        return team_problems

    return config.validate_personal(config.with_environment(personal)) + list(
        (rejected or {}).values())


def save(personal, rejected, values):

    # Checks the values as the terminal setup does and saves them. Returns
    # why each one cannot be used, or an empty dict once they are saved. An
    # empty value keeps the current one.

    settings = config.Settings(config.read_toml(config.TEAM_FILE))
    labels = dict(fields(settings))

    answers = {}
    errors = {}

    for key, value in values.items():
        if key not in labels:
            errors[key] = "This setting is not set on this page"

            continue

        value = str(value or "").strip()

        if not value:
            continue

        if config.environment_value(key, fallbacks=False):
            errors[key] = f"{labels[key]} comes from an environment variable; change it there"

            continue

        answers[key] = value

    current = config.with_environment(personal)

    for key, value in answers.items():
        config.set_value(current, key, value)

    for key, label in labels.items():
        if key not in config.OPTIONAL_KEYS and config.get_value(current, key) in (None, ""):
            errors.setdefault(key, f"{label} is required")

    if errors:
        return errors

    # Jenkins and Jira are checked again when one of their values changed, or
    # when they rejected the saved ones at the start.

    if {"jenkins.token", "jenkins.user"} & (answers.keys() | rejected.keys()):
        problem = wizard.check_jenkins(
            settings, config.get_value(current, "jenkins.user"),
            config.get_value(current, "jenkins.token"))

        if problem:
            errors["jenkins.token"] = problem

    if {"jira.email", "jira.token"} & (answers.keys() | rejected.keys()):
        problem, _ = wizard.check_jira(
            settings, config.get_value(current, "jira.email"),
            config.get_value(current, "jira.token"))

        if problem:
            errors["jira.token"] = problem

    if "charts.checkout" in answers:
        problem = wizard.check_charts(settings, answers["charts.checkout"])

        if problem:
            errors["charts.checkout"] = problem

    if "asah.checkout" in answers:
        problem, _ = wizard.check_asah(settings, answers["asah.checkout"])

        if problem:
            errors["asah.checkout"] = problem

    if errors:
        return errors

    for key, value in answers.items():
        wizard.keep(personal, key, value)

    config.write_personal(personal)

    return {}


def serve(personal, rejected=None):

    # personal is the file as saved, without the environment variables, so
    # saving writes none of them to it.

    Handler.personal = personal
    Handler.port = (
        config.get_value(config.with_environment(personal), "server.port") or
        config.DEFAULT_PORT)
    Handler.rejected = rejected or {}

    # A dashboard reads the token to call /api/v1/setup, before the first
    # complete start creates it.

    api.api_token()

    server = ThreadingHTTPServer(("127.0.0.1", Handler.port), Handler)

    print(
        f"LDP Releaser needs its settings first; finish the setup on "
        f"http://127.0.0.1:{Handler.port}", flush=True)

    server.serve_forever()
