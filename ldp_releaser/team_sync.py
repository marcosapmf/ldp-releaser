"""When the team file lacks settings this version needs, serves a page that
syncs it from 1Password instead of the releaser, then restarts."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import config

SYNC_FILE = Path(__file__).parent / "static" / "sync.html"


class Handler(BaseHTTPRequestHandler):

    personal = None
    port = None
    problems = None

    def do_GET(self):
        if not self._is_local_host():
            return self._send_json(403, {"error": "Forbidden host"})

        path = urllib.parse.urlparse(self.path).path

        if path == "/":
            return self._send(200, SYNC_FILE.read_bytes(), "text/html; charset=utf-8")

        if path == "/api/team-sync":
            return self._send_json(
                200,
                {
                    "item": config.get_value(self.personal, "team.onepassword_item"),
                    "problems": self.problems,
                    "vault": config.get_value(self.personal, "team.onepassword_vault"),
                })

        if path.startswith("/api/"):
            return self._unavailable()

        self._send_json(404, {"error": "Not found"})

    def do_POST(self):

        # The same checks as the releaser's page: only this page may sync.

        content_type = self.headers.get("Content-Type", "")
        origin = self.headers.get("Origin")

        if (not self._is_local_host() or
                not content_type.startswith("application/json") or
                (origin and urllib.parse.urlparse(origin).netloc !=
                 self.headers.get("Host"))):

            if self.path.startswith("/api/v1"):
                return self._unavailable()

            return self._send_json(403, {"error": "Forbidden"})

        if self.path != "/api/team-sync":
            return self._unavailable()

        try:
            payload = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))) or b"{}")
        except ValueError:
            return self._send_json(400, {"error": "The body is not valid JSON"})

        item = str(payload.get("item") or "").strip()
        vault = str(payload.get("vault") or "").strip() or None

        if not item:
            return self._send_json(400, {"error": "Enter the 1Password item of the team file"})

        problem = sync(self.personal, item, vault)

        if problem:
            return self._send_json(409, {"error": problem})

        threading.Thread(daemon=True, target=restart).start()

        return self._send_json(202, {"restarting": True})

    def log_message(self, format, *args):
        pass

    def _is_local_host(self):
        return self.headers.get("Host") in (
            f"127.0.0.1:{self.port}", f"localhost:{self.port}")

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
                "error": "The team settings lack settings this version needs; sync "
                         "them from 1Password on the page",
                "problems": self.problems,
            })


def available(personal):
    return (
        config.validate_personal(config.with_environment(personal)) == [] and
        shutil.which("op") is not None)


def fetch(item, vault):
    command = ["op", "document", "get", item]

    if vault:
        command += ["--vault", vault]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exception:
        raise RuntimeError(f"1Password could not fetch the team file ({exception})")

    if completed.returncode:
        raise RuntimeError(
            f"1Password could not fetch the team file ({completed.stderr.strip()[:200]})")

    return completed.stdout


def problems():

    # What the team file lacks, or None when it is complete.

    team = config.read_toml(config.TEAM_FILE)

    if team is None:
        return [f"team settings not found at {config.TEAM_FILE}"]

    return config.validate_team(team) or None


def restart():

    # Lets the response reach the page first.

    time.sleep(0.5)

    os.chdir(Path(__file__).resolve().parent.parent)
    os.execv(sys.executable, [sys.executable, "-m", "ldp_releaser"])


def serve(personal, team_problems):

    # personal is the file as saved, without the environment variables, so
    # saving the 1Password item writes none of them to it.

    Handler.personal = personal
    Handler.port = (
        config.get_value(config.with_environment(personal), "server.port") or
        config.DEFAULT_PORT)
    Handler.problems = team_problems

    server = ThreadingHTTPServer(("127.0.0.1", Handler.port), Handler)

    print(
        f"The team settings lack settings this version needs; sync them from "
        f"1Password on http://127.0.0.1:{Handler.port}", flush=True)

    server.serve_forever()


def sync(personal, item, vault):

    # Returns why the file in 1Password cannot be used, or None once it is
    # saved.

    try:
        text = fetch(item, vault)
    except RuntimeError as exception:
        return str(exception)

    try:
        team_problems = config.validate_team(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exception:
        return f"The file in 1Password is not valid TOML ({exception})"

    if team_problems:
        return (
            "The file in 1Password lacks them too: " + "; ".join(team_problems) +
            ". Ask the owner of the team file to add them.")

    config.write_private(config.TEAM_FILE, text)

    # The next sync, and --setup, offer the same item.

    config.set_value(personal, "team.onepassword_item", item)
    config.set_value(personal, "team.onepassword_vault", vault)
    config.write_personal(personal)

    return None
