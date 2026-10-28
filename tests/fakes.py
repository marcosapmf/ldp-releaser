"""In-process fakes of Jenkins, Jira and a Slack webhook for the tests.

Jenkins builds behave according to the branch they are given:
    broken  -> the build fails
    flaky   -> every third request about the build gets a 403
    hold    -> the build runs until `release_builds()` is called
    slow    -> the build waits in the queue for a few polls
"""

import base64
import itertools
import json
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

AUTHORIZATIONS = {
    "Basic " + base64.b64encode(login).decode()
    for login in (b"tester:secret-token", b"tester@example.com:secret-token")
}
FOLDER = "team"
ME = {"accountId": "me", "displayName": "Test User"}


class FakeServices:

    def __init__(self):
        self.builds = {}
        self.calls = []
        self.held = True
        self.inherit_security = False
        self.issues = {}
        self.lock = threading.Lock()
        self.numbers = {}
        self.queue = {}
        self.queue_ids = itertools.count(1)
        self.revisions = {}
        self.slack = []
        self.ticket_numbers = itertools.count(101)

        # Argo CD: applications read their live state from the charts
        # origin (set by the harness). Per application, tests can delay the
        # sync (lag: looks before it catches up; syncing: looks with a sync
        # still running), fail the upgrade hook or report unhealthy pods.

        self.argocd_apps = {}
        self.argocd_origin = None

        services = self

        class Handler(BaseHTTPRequestHandler):

            def do_GET(self):
                services.handle(self, "GET")

            def do_POST(self):
                services.handle(self, "POST")

            def do_PUT(self):
                services.handle(self, "PUT")

            def log_message(self, *args):
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def add_issue(self, issue_type, summary, assignee=ME, parent=None, status="Open"):
        key = f"PROJ-{next(self.ticket_numbers)}"

        self.issues[key] = {
            "assignee": assignee,
            "parent": parent,
            "security": None,
            "status": status,
            "summary": summary,
            "type": issue_type,
        }

        return key

    def handle(self, request, method):
        url = urllib.parse.urlparse(request.path)
        body = request.rfile.read(int(request.headers.get("Content-Length", 0)))

        if url.path == "/slack":
            self.slack.append(json.loads(body))

            return send(request, 200, {"ok": True})

        argocd_path = url.path.removeprefix("/prd-instance")

        if argocd_path.startswith("/api/v1/"):
            if request.headers.get("Authorization") != "Bearer fake-argocd-token":
                return send(request, 401, {"error": "invalid token"})

            with self.lock:
                return self.argocd(request, argocd_path)

        if request.headers.get("Authorization") not in AUTHORIZATIONS:
            return send(request, 401, {"error": "unauthorized"})

        with self.lock:
            if url.path.startswith("/rest/"):
                return self.jira(request, method, url, body)

            return self.jenkins(request, method, url, body)

    def argocd(self, request, path):
        if path == "/api/v1/applications":
            return send(
                request, 200,
                {"items": [self.argocd_application(name) for name in self.argocd_apps]})

        name = path.removeprefix("/api/v1/applications/").removesuffix("/resource-tree")

        if path.endswith("/resource-tree") and name in self.argocd_apps:
            app = self.argocd_apps[name]
            pods = [
                {"health": {"status": "Healthy"}, "info": [{"name": "Status Reason", "value": "Running"}],
                 "kind": "Pod", "name": f"{name}-backend-1"},
            ]

            if app.get("health") == "Degraded":
                pods.append(
                    {"health": {"status": "Degraded"},
                     "info": [{"name": "Status Reason", "value": "CrashLoopBackOff"}],
                     "kind": "Pod", "name": f"{name}-publisher-1"})

            return send(request, 200, {"nodes": pods})

        send(request, 404, {"error": path})

    def argocd_application(self, name):
        app = self.argocd_apps[name]
        app["looks"] = app.get("looks", 0) + 1

        revision = git_output(self.argocd_origin, "rev-parse", f"refs/heads/{app['branch']}")

        # A lagging application keeps the revision it had at its first look.

        if app.get("lag", 0) >= app["looks"]:
            revision = app.setdefault("stuckAt", revision)

        values = git_output(self.argocd_origin, "show", f"{revision}:{app['file']}")
        suffix = values.split('tagSuffix: "', 1)[1].split('"', 1)[0]

        hook = app.get("hook", "Succeeded")
        phase = "Succeeded"

        if hook == "Failed":
            phase = "Failed"

        # A sync still running for the first looks (syncing: how many).

        if app.get("syncing", 0) >= app["looks"]:
            phase = "Running"

        return {
            "metadata": {"name": name},
            "status": {
                "health": {"status": app.get("health", "Healthy")},
                "operationState": {
                    "message": "one or more synchronization tasks completed unsuccessfully",
                    "phase": phase,
                    "syncResult": {
                        "resources": [{
                            "hookPhase": hook, "hookType": "PreSync", "kind": "Job",
                            "message": "Job has reached the specified backoff limit",
                            "name": "osbasahupgrade",
                        }],
                        "revision": revision,
                    },
                },
                "summary": {"images": [
                    f"registry.example.com/asah:backend-{suffix}",
                    f"registry.example.com/asah:publisher-{suffix}",
                    "registry.example.com/sidecar:1.0",
                ]},
                "sync": {"revision": revision, "status": "Synced"},
            },
        }

    def jenkins(self, request, method, url, body):
        path = url.path

        if path == f"/job/{FOLDER}/api/json":
            return send(request, 200, {"name": FOLDER})

        match = re.fullmatch(rf"/job/{FOLDER}/job/([\w-]+)/buildWithParameters", path)

        if match and method == "POST":
            params = {
                key: values[0]
                for key, values in urllib.parse.parse_qs(body.decode()).items()
            }

            self.calls.append({"job": match[1], "params": params})

            queue_id = next(self.queue_ids)

            self.queue[queue_id] = {"job": match[1], "params": params, "polls": 0}

            return send(
                request, 201, None,
                {"Location": f"{self.url}/queue/item/{queue_id}/"})

        if path == "/queue/cancelItem":
            queue_id = int(urllib.parse.parse_qs(body.decode())["id"][0])

            self.queue[queue_id]["cancelled"] = True

            return send(request, 204, None)

        match = re.fullmatch(r"/queue/item/(\d+)/api/json", path)

        if match:
            return self.jenkins_queue_item(request, int(match[1]))

        match = re.fullmatch(
            rf"/job/{FOLDER}/job/([\w-]+)/(\d+)/"
            rf"(api/json|artifact/.+|consoleText|logText/progressiveText|stop)",
            path)

        if match:
            return self.jenkins_build(
                request, self.builds[(match[1], int(match[2]))], match[3], url)

        match = re.fullmatch(rf"/job/{FOLDER}/job/([\w-]+)/api/json", path)

        if match:
            builds = [
                {"actions": [{"parameters": [
                    {"name": name, "value": value}
                    for name, value in build["params"].items()]}]}
                for (job, _), build in self.builds.items() if job == match[1]
            ]

            successes = [
                number for (job, number), build in self.builds.items()
                if job == match[1] and build["result"] == "SUCCESS"
            ]

            last_success = None

            if successes:
                last_success = {
                    "number": max(successes),
                    "url": f"{self.url}/job/{FOLDER}/job/{match[1]}/{max(successes)}/",
                }

            return send(request, 200, {"builds": builds, "lastSuccessfulBuild": last_success})

        send(request, 404, {"error": path})

    def jenkins_build(self, request, build, action, url):
        branch = build["params"].get("BRANCH")

        build["hits"] += 1

        if branch == "flaky" and build["hits"] % 3 == 0:
            return send_text(request, 403, "<h1>Access Denied</h1><p>try later</p>")

        if action == "stop":
            build["stopped"] = True

            return send(request, 200, None)

        if action == "api/json":
            build["polls"] += 1

            if build["stopped"]:
                build["result"] = "ABORTED"
            elif branch == "hold" and self.held:
                pass
            elif build["polls"] >= 4:
                build["result"] = "SUCCESS"

                if branch in ("broken", "vulnerable") or (
                        branch == "broken-prd" and build["params"].get("ENVIRONMENT") == "prd"):

                    build["result"] = "FAILURE"

            artifacts = []

            if build["job"] == "build-asah" and build["result"]:
                artifacts = [
                    {"relativePath": f"vulnerabilities_{service}.json"}
                    for service in ("backend", "publisher")
                ]

            return send(
                request, 200,
                {
                    "artifacts": artifacts,
                    "building": build["result"] is None,
                    "result": build["result"],
                })

        if action.startswith("artifact/"):
            return send(request, 200, self.scan_report(build))

        text = self.log(build)

        if action == "consoleText":
            return send_text(request, 200, text)

        start = int(urllib.parse.parse_qs(url.query).get("start", ["0"])[0])

        return send_text(
            request, 200, text[start:],
            {"X-More-Data": "true", "X-Text-Size": str(len(text))})

    def jenkins_queue_item(self, request, queue_id):
        item = self.queue[queue_id]

        item["polls"] += 1

        if item.get("cancelled"):
            return send(request, 200, {"cancelled": True})

        waits = 1

        if item["params"].get("BRANCH") == "slow":
            waits = 3

        if item["polls"] <= waits:
            return send(request, 200, {"why": "Waiting for next available executor"})

        if "build" not in item:
            job = item["job"]

            self.numbers[job] = self.numbers.get(job, 0) + 1

            item["build"] = self.numbers[job]

            self.builds[(job, item["build"])] = {
                "hits": 0,
                "job": job,
                "number": item["build"],
                "params": item["params"],
                "polls": 0,
                "result": None,
                "stopped": False,
            }

        number = item["build"]

        return send(
            request, 200,
            {
                "executable": {
                    "number": number,
                    "url": f"{self.url}/job/{FOLDER}/job/{item['job']}/{number}/",
                },
            })

    def jira(self, request, method, url, body):
        path = url.path
        payload = json.loads(body or b"null")

        if path == "/rest/api/3/myself":
            return send(request, 200, ME)

        if path == "/rest/api/3/issue" and method == "POST":
            fields = payload["fields"]

            key = self.add_issue(
                fields["issuetype"]["name"], fields["summary"],
                assignee={"accountId": fields["assignee"]["accountId"],
                          "displayName": ME["displayName"]},
                parent=(fields.get("parent") or {}).get("key"))

            self.set_security(key, fields)

            self.calls.append({"jira": "create", "key": key, "fields": fields})

            return send(request, 201, {"key": key})

        if path == "/rest/api/3/search/jql":
            return send(request, 200, {"issues": self.jira_search()})

        match = re.fullmatch(r"/rest/api/3/issue/([\w-]+)/transitions", path)

        if match:
            if method == "GET":
                return send(request, 200, {"transitions": self.transitions(match[1])})

            return self.jira_transition(request, match[1], payload)

        match = re.fullmatch(r"/rest/api/3/issue/([\w-]+)", path)

        if match and method == "PUT":
            fields = payload["fields"]

            if "summary" in fields:
                self.issues[match[1]]["summary"] = fields["summary"]

            self.set_security(match[1], fields)

            return send(request, 204, None)

        if match:
            issue = self.issues[match[1]]

            return send(
                request, 200,
                {
                    "fields": {
                        "security": issue["security"] and {"name": issue["security"]},
                        "status": {"name": issue["status"]},
                        "summary": issue["summary"],
                    },
                    "key": match[1],
                })

        if re.fullmatch(r"/rest/agile/1.0/board/\d+/sprint", path):
            return send(request, 200, {"values": [{"id": 7, "name": "Sprint 7"}]})

        if re.fullmatch(r"/rest/agile/1.0/sprint/\d+/issue", path):
            self.calls.append({"jira": "sprint", "issues": payload["issues"]})

            return send(request, 204, None)

        send(request, 404, {"error": path})

    def jira_search(self):
        issues = []

        for key, issue in sorted(
                self.issues.items(), key=lambda item: -int(item[0].split("-")[1])):

            if issue["type"] != "Task" or issue["status"] == "Closed":
                continue

            subtasks = [
                {
                    "fields": {
                        "issuetype": {"name": subtask["type"]},
                        "status": {"name": subtask["status"]},
                        "summary": subtask["summary"],
                    },
                    "key": subtask_key,
                }
                for subtask_key, subtask in self.issues.items()
                if subtask["parent"] == key
            ]

            issues.append(
                {
                    "fields": {
                        "assignee": issue["assignee"],
                        "status": {"name": issue["status"]},
                        "subtasks": subtasks,
                        "summary": issue["summary"],
                    },
                    "key": key,
                })

        return issues

    def jira_transition(self, request, key, payload):
        transitions = {
            transition["id"]: transition for transition in self.transitions(key)
        }

        transition = transitions.get(payload["transition"]["id"])

        if transition is None:
            return send(request, 400, {"errorMessages": ["transition not valid"]})

        status = transition["to"]["name"]

        if status == "Closed" and not (payload.get("fields") or {}).get("resolution"):
            return send(request, 400, {"errors": {"resolution": "required"}})

        self.issues[key]["status"] = status
        self.issues[key]["resolution"] = (
            (payload.get("fields") or {}).get("resolution", {}).get("name"))

        return send(request, 204, None)

    def set_security(self, key, fields):

        # With inherit_security set, Jira keeps subtasks at their parent's
        # level whatever is asked, as some projects are configured to.

        issue = self.issues[key]

        if self.inherit_security and issue["parent"]:
            issue["security"] = self.issues[issue["parent"]]["security"]
        elif "security" in fields:
            issue["security"] = fields["security"]["name"]

    def release_builds(self):
        with self.lock:
            self.held = False

    def start(self):
        threading.Thread(daemon=True, target=self.server.serve_forever).start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def transitions(self, key):

        # The same workflow as the real project: a Task goes through Selected
        # for Development and closes only once its subtasks are closed.

        issue = self.issues[key]

        def transition(transition_id, name):
            return {"id": transition_id, "to": {"name": name}}

        if issue["type"] == "Task":
            subtasks_closed = all(
                subtask["status"] == "Closed" for subtask in self.issues.values()
                if subtask["parent"] == key)

            closed = []

            if subtasks_closed:
                closed = [transition("71", "Closed")]

            return {
                "In Progress": closed + [transition("81", "Open")],
                "Open": [transition("71", "Closed"),
                         transition("11", "Selected for Development")],
                "Selected for Development": [transition("21", "In Progress")],
            }.get(issue["status"], [])

        return {
            "In Progress": [transition("11", "Closed"), transition("51", "Open")],
            "Open": [transition("11", "Closed"), transition("41", "In Progress")],
        }.get(issue["status"], [])

    def log(self, build):
        lines = [f"[{build['job']}] step {index}\n" for index in range(build["polls"])]

        if build["result"] == "SUCCESS":
            number = build["number"]
            revision = self.revisions.get(build["job"], f"{number:040x}")

            lines.append(f"+ docker build --build-arg REVISION={revision} .\n")

            if build["job"] == "build-asah":
                for service in ("backend", "publisher", "backend"):
                    lines.append(
                        f"+ docker push registry.example.com/asah:"
                        f"{service}-20260101-abcd{number:03d}\n")
            elif build["job"] == "build-faro":
                environment = build["params"]["ENVIRONMENT"]

                lines.append(
                    f"+ docker push registry.example.com/faro:"
                    f"{environment}-20260101-f00{number:04d}\n")

        if build["job"] == "build-asah" and build["result"]:
            lines.extend(self.scan_log(build))

        return "".join(lines) + "\x1b[1mdone\x1b[0m\n"

    def scan_report(self, build):

        # The report twistcli archives: the full fix versions and the jar
        # each vulnerability is in. The new one is in the library itself, in
        # an annotation processor and in a library needed at runtime.

        vulnerabilities = [
            {"id": "CVE-2020-0001", "packageName": "old-lib", "packagePath": "/app/lib/old-lib-1.0.jar",
             "packageVersion": "1.0", "status": "fixed in 1.1"},
        ]

        if build["params"].get("BRANCH") == "vulnerable":
            for version, jar in (
                    ("2.0", "new-lib-2.0.jar"), ("1.5", "processor-lib-1.0.jar"),
                    ("1.4", "runtime-lib-3.0.jar")):

                vulnerabilities.append(
                    {"id": "CVE-2026-0002", "packageName": "com.example_new-lib",
                     "packagePath": f"/app/lib/{jar}", "packageVersion": version,
                     "status": "fixed in 2.1, 1.9.5, 1.2.9"})

        return {"results": [{"vulnerabilities": vulnerabilities}]}

    def scan_log(self, build):

        # The image scan step: every build has a known vulnerability, and
        # the "vulnerable" branch also has a new one that blocks the build.
        # A failed build ends like a Jenkins pipeline: the later steps are
        # skipped and the error is reported.

        branch = build["params"].get("BRANCH")
        lines = ["[Pipeline] { (Compile)\n"]

        if branch == "broken":
            lines.append("* What went wrong:\nExecution failed for task ':compileJava'.\n\n")
        else:
            lines.append("[Pipeline] { (Image Scan)\n")

            for service in ("backend", "publisher"):
                lines.append(
                    f"+ ./twistcli images scan --details registry.example.com/asah:"
                    f"{service}-20260101-abcd{build['number']:03d}\n"
                    f"| CVE-2020-0001 | high | 7.50 | old-lib | 1.0 | fixed in 1.1 | 100 days "
                    f"| < 1 hour | old |\n")

                result = "PASS"

                if branch == "vulnerable":
                    for version in ("2.0", "1.5", "1.4"):
                        lines.append(
                            f"| CVE-2026-0002 | \x1b[31;1mcritical\x1b[0m | 9.80 | "
                            f"com.example_new-lib | {version} | fixed in 2.1, 1.9.5,... "
                            f"| 1 days | < 1 hour | new |\n")

                    lines.append(
                        "Scan failed due to vulnerability policy violations: 3 vulnerabilities\n")

                    result = "FAIL"

                lines.append(f"Vulnerability threshold check results: {result}\n")

        if build["result"] == "FAILURE":
            lines.append(
                "ha:////4AdN2NFD7avhjgOzjzcbivs04R5x=[Pipeline] { (Push Docker Images)\n"
                'Stage "Push Docker Images" skipped due to earlier failure(s)\n'
                "ERROR: The build failed.\n")

        return lines


def git_output(repository, *args):
    import subprocess

    return subprocess.run(
        ["git", "--git-dir", str(repository), *args], capture_output=True, check=True,
        text=True).stdout.strip()


def send(request, status, payload, headers=None):
    body = b""

    if payload is not None:
        body = json.dumps(payload).encode()

    request.send_response(status)

    for name, value in (headers or {}).items():
        request.send_header(name, value)

    request.send_header("Content-Type", "application/json")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)


def send_text(request, status, text, headers=None):
    body = text.encode()

    request.send_response(status)

    for name, value in (headers or {}).items():
        request.send_header(name, value)

    request.send_header("Content-Type", "text/plain")
    request.send_header("Content-Length", str(len(body)))
    request.end_headers()
    request.wfile.write(body)
