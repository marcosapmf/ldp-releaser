"""Creates the team's gcloud configurations on this machine.

The team file maps configuration names to projects, regions and zones; each
person's own gcloud login is used as the account. Existing configurations
are never changed, and the active configuration is never switched.
"""

import json
import re
import shutil
import subprocess
import threading
import time

URL_PATTERN = re.compile(r"https://\S+")


class Login:

    # One `gcloud auth login` at a time, started from the page. gcloud opens
    # the sign-in page in the browser and waits for Google to call it back;
    # when it cannot open a browser it prints the link and asks for the
    # verification code instead, which the page passes on.

    def __init__(self):
        self._lock = threading.Lock()
        self._process = None
        self._state = idle_state()

    def send_code(self, code):
        with self._lock:
            if not self._process or self._state["status"] != "running":
                return False

            self._process.stdin.write(code.strip() + "\n")
            self._process.stdin.flush()

            self._state["needsCode"] = False

            return True

    def start(self):
        with self._lock:
            if self._state["status"] == "running":
                return self.state()

            command = ["gcloud", "auth", "login", "--brief"]
            account = active_account()

            if account:
                command.append(account)

            self._process = subprocess.Popen(
                command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            self._state = {
                "needsCode": False,
                "output": "",
                "startedAt": time.time(),
                "status": "running",
                "url": None,
            }

        threading.Thread(daemon=True, target=self._watch).start()

        return self.state()

    def state(self):
        with self._lock:
            return dict(self._state)

    def _watch(self):
        process = self._process

        for line in process.stdout:
            with self._lock:
                self._state["output"] = (self._state["output"] + line)[-4000:]

                match = URL_PATTERN.search(line)

                if match and not self._state["url"]:
                    self._state["url"] = match.group(0)

                if re.search(r"(verification|authorization) code", line, re.IGNORECASE):
                    self._state["needsCode"] = True

        process.wait()

        status = "failed"

        if process.returncode == 0:
            status = "succeeded"

        with self._lock:
            self._state["needsCode"] = False
            self._state["status"] = status


def idle_state():
    return {"needsCode": False, "output": "", "startedAt": None, "status": "idle", "url": None}


def active_account():
    completed = run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])

    if completed.returncode:
        return None

    return completed.stdout.strip() or None


def login_problem():

    # `gcloud auth list` only reads local files and keeps showing an account
    # after its login expires; getting a token proves the login still works.
    # The token itself is discarded.

    if not active_account():
        return "no gcloud login; run `gcloud auth login`"

    completed = run(["gcloud", "auth", "print-access-token"])

    if completed.returncode:
        return "your gcloud login has expired; run `gcloud auth login`"

    return None


def configured(settings):
    return bool(settings["gcloud.configurations"])


def describe(name):
    completed = run(
        ["gcloud", "config", "configurations", "describe", name, "--format=json"])

    if completed.returncode:
        return None

    return json.loads(completed.stdout)


def problems(settings):

    # What is missing for the gcloud steps: the CLI, a login that still
    # works, and the team's configurations.

    if not configured(settings):
        return []

    if not shutil.which("gcloud"):
        return ["the gcloud CLI is not installed"]

    login = login_problem()

    if login:
        return [login]

    found = []

    for name, wanted in settings["gcloud.configurations"].items():
        problem = mismatch(name, wanted, describe(name))

        if problem:
            found.append(problem)

    return found


def mismatch(name, wanted, configuration):
    if configuration is None:
        return f"gcloud configuration {name} is missing; restart LDP Releaser to create it"

    project = configuration.get("properties", {}).get("core", {}).get("project")

    if project != wanted["project"]:
        return (
            f"gcloud configuration {name} points to {project}, not "
            f"{wanted['project']}; rename or fix it by hand")

    return None


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=60)


def sync(settings):

    # Creates the configurations that do not exist yet and returns one line
    # per configuration describing what happened.

    if not configured(settings):
        return []

    if not shutil.which("gcloud"):
        return ["gcloud is not installed, so no configurations were created"]

    login = login_problem()

    if login:
        return [f"{login}; configurations are created on the next start"]

    account = active_account()

    report = []

    for name, wanted in settings["gcloud.configurations"].items():
        configuration = describe(name)

        if configuration is not None:
            problem = mismatch(name, wanted, configuration)

            report.append(problem or f"{name}: already set up")

            continue

        create = run(["gcloud", "config", "configurations", "create", name, "--no-activate"])

        if create.returncode:
            report.append(f"{name}: could not be created ({create.stderr.strip()[:200]})")

            continue

        values = {
            "account": account,
            "compute/region": wanted.get("region"),
            "compute/zone": wanted.get("zone"),
            "project": wanted["project"],
        }

        failure = None

        for key, value in values.items():
            if not value:
                continue

            completed = run(
                ["gcloud", "config", "set", key, value, f"--configuration={name}"])

            if completed.returncode:
                failure = f"setting {key} failed ({completed.stderr.strip()[:200]})"

                break

        # A configuration without its project is worse than none, so a
        # half-made one is removed again.

        if failure:
            run(["gcloud", "config", "configurations", "delete", name, "--quiet"])

            report.append(f"{name}: not created, {failure}")

            continue

        report.append(f"{name}: created for {wanted['project']}")

    return report
