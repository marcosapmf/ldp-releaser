"""Starts LDP Releaser against the fakes, with its own settings and data."""

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

from tests.fakes import FakeServices

ROOT = Path(__file__).resolve().parent.parent

TEAM = """
[jenkins]
url = "{url}"
folder = "team"
commit_pattern = 'REVISION=([0-9a-f]{{40}})'

[asah]
build_job = "build-asah"
composer_directory = "composer"
composer_script = "scripts/upload_dag.sh"
branch_parameter = "BRANCH"
repository = "example-org/asah"
image_pattern = 'docker push (registry.example.com/asah:\\S+)'
dependency_file = "common/build.gradle"
main_branch = "main"
tag_suffix_pattern = '\\d{{8}}-[0-9a-f]{{7,40}}'
tag_format = "{{version}}"

[faro]
build_job = "build-faro"
deploy_job = "deploy-faro"
branch_parameter = "BRANCH"
environment_parameter = "ENVIRONMENT"
image_parameter = "IMAGE"
repository = "example-org/faro"
image_pattern = 'docker push (registry.example.com/faro:\\S+)'
tag_format = "faro-v{{version}}"

[faro.build_environments]
stg = "stg"
int = "prd"
prd = "prd"

[charts]
repository = "example-org/charts"
develop_branch = "develop"
production_branch = "production"
tag_suffix_key = "tagSuffix"
contributing_url = "https://github.example/contributing"

[charts.files]
stg = "deploy/stg.yaml"
int = "deploy/int.yaml"
prd = "deploy/prd.yaml"

[charts.production]
environments = ["int", "prd"]

[charts.merge]
checks = ["Prisma Cloud / Code analysis"]
develop_method = "squash"
production_method = "merge"

[jira]
url = "{url}"
project = "PROJ"
board_id = 1
component = "Platform"
task_type = "Task"
technical_task_type = "Technical Task"
fix_security_level = "Private"
title = "Release {{environments}} Environment{{plural}}"

[jira.workflow]
task_to_in_progress = ["Selected for Development", "In Progress"]
technical_task_to_in_progress = ["In Progress"]
closed_status = "Closed"
completed_resolution = "Completed"
discarded_resolution = "Discarded"

[argocd]
search = "asah"
image_name = "registry.example.com/asah"

[argocd.urls]
stg = "{url}"
int = "https://argocd.example"
prd = "{url}/prd-instance"

[argocd.applications]
stg = ["team-stg-asah"]
prd = ["team-prd-asah-1", "team-prd-asah-2"]

[argocd.tokens]
stg = "fake-argocd-token"
prd = "fake-argocd-token"

[release]
prd_environment = "prd"

[slack]
webhook_url = "{url}/slack"

[gcloud.configurations]
team-stg = {{ project = "example-staging", region = "us-west1", zone = "us-west1-a" }}
team-prd-1 = {{ project = "example-prd-1", region = "europe-west1" }}
team-prd-2 = {{ project = "example-prd-2", region = "us-east1", lcp_project_id = "second" }}

[gcloud.environments]
stg = ["team-stg"]
prd = ["team-prd-1", "team-prd-2"]
"""

PERSONAL = """
[asah]
checkout = "{asah_checkout}"

[jenkins]
user = "tester"
token = "secret-token"

[jira]
email = "tester@example.com"
token = "secret-token"

[charts]
checkout = "{checkout}"

[server]
port = {port}
"""


UPLOAD_SCRIPT = """#!/bin/bash
if [ -z "$LCP_PROJECT_ID" ] || [ -z "$PROJECT_ID" ] || [ -z "$REGION" ]
then
    echo "LCP_PROJECT_ID, PROJECT_ID and REGION are required"
    exit 1
fi

cd src/main/python

for file in *
do
    gcloud composer environments storage dags import --environment "ac-$LCP_PROJECT_ID" \\
        --location "$REGION" --project "$PROJECT_ID" --source "$file" || exit 1
done

cd - > /dev/null

gcloud composer environments storage dags import --environment "ac-$LCP_PROJECT_ID" \\
    --location "$REGION" --project "$PROJECT_ID" --source src/main/resources/ || exit 1
"""


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))

        return sock.getsockname()[1]


# Tests use none of the person's git settings (such as commit signing
# through a password manager), only what they set themselves.

GIT_ISOLATION = {"GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}


def git(*args, cwd=None):
    subprocess.run(
        ["git", *args], capture_output=True, check=True, cwd=cwd,
        env={**os.environ, **GIT_ISOLATION}, text=True)


class AppTestCase(unittest.TestCase):

    def setUp(self):
        self.temp = Path(tempfile.mkdtemp(prefix="ldp-releaser-test-"))
        self.fakes = FakeServices()
        self.fakes.start()
        self.origin = self.temp / "example-org" / "charts.git"
        self.checkout = self.temp / "charts"
        self.gh_state = self.temp / "gh"
        self.gh_state.mkdir()
        self.port = free_port()
        self.process = None

        self.create_charts()
        self.create_asah()
        self.write_settings()

        self.auto_confirm = True
        self.fakes.argocd_origin = self.origin
        self.fakes.argocd_apps = {
            "team-prd-asah-1": {"branch": "production", "file": "deploy/prd.yaml"},
            "team-prd-asah-2": {"branch": "production", "file": "deploy/prd.yaml"},
            "team-stg-asah": {"branch": "develop", "file": "deploy/stg.yaml"},
        }
        self.before_start()
        self.start_app()

    def tearDown(self):
        self.stop_app()
        self.fakes.stop()

        shutil.rmtree(self.temp, ignore_errors=True)

    def before_start(self):
        pass

    def call(self, path, payload=None):
        data = None
        method = "GET"

        if payload is not None:
            data = json.dumps(payload).encode()
            method = "POST"

        request = urllib.request.Request(
            f"http://127.0.0.1:{self.port}{path}", data=data,
            headers={"Content-Type": "application/json"}, method=method)

        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.status, json.loads(response.read() or b"null")
        except urllib.error.HTTPError as exception:
            return exception.code, json.loads(exception.read() or b"null")

    def create_asah(self):

        # The team's Asah repository with a Composer folder, and a personal
        # clone on another branch with uncommitted work, which must stay as
        # it is.

        self.asah_origin = self.temp / "example-org" / "asah.git"
        self.asah_checkout = self.temp / "asah"

        self.asah_origin.parent.mkdir(parents=True, exist_ok=True)

        git("init", "--bare", "-b", "main", str(self.asah_origin))
        git("clone", str(self.asah_origin), str(self.asah_checkout))
        git("checkout", "-b", "main", cwd=self.asah_checkout)

        composer = self.asah_checkout / "composer"

        for path, content in {
                "scripts/upload_dag.sh": UPLOAD_SCRIPT,
                "src/main/python/a_dag.py": "# DAG a\n",
                "src/main/python/b_dag.py": "# DAG b\n",
                "src/main/resources/query.sql": "select 1;\n"}.items():

            (composer / path).parent.mkdir(parents=True, exist_ok=True)
            (composer / path).write_text(content)

        git("add", "-A", cwd=self.asah_checkout)
        git("-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-m", "DAGs", cwd=self.asah_checkout)
        git("push", "origin", "main", cwd=self.asah_checkout)
        git("remote", "rename", "origin", "upstream", cwd=self.asah_checkout)
        git("remote", "add", "origin", "git@github.example:someone/asah-fork.git",
            cwd=self.asah_checkout)
        git("checkout", "-b", "my-feature", cwd=self.asah_checkout)

        (self.asah_checkout / "work-in-progress.txt").write_text("not committed\n")

    def create_charts(self):
        self.origin.parent.mkdir(parents=True)

        git("init", "--bare", "-b", "develop", str(self.origin))
        git("clone", str(self.origin), str(self.checkout))
        git("checkout", "-b", "develop", cwd=self.checkout)

        (self.checkout / "deploy").mkdir()

        for environment in ("stg", "int", "prd"):
            (self.checkout / "deploy" / f"{environment}.yaml").write_text(
                'global:\n  image:\n    tagSuffix: "20250101-0000000"\n')

        git("add", "-A", cwd=self.checkout)
        git("-c", "user.name=Test", "-c", "user.email=test@example.com",
            "commit", "-m", "init", cwd=self.checkout)
        git("push", "origin", "develop", cwd=self.checkout)
        git("push", "origin", "develop:production", cwd=self.checkout)
        git("checkout", "-b", "my-work", cwd=self.checkout)

    def environment(self):
        environment = {
            key: value for key, value in os.environ.items()
            if not key.startswith(("JIRA_API_", "LDP_"))
        }

        environment.update(GIT_ISOLATION)
        environment.update(
            CLOUDSDK_CONFIG=str(self.temp / "gcloud-sdk"),
            FAKE_GCLOUD_ACCOUNT="tester@example.com",
            FAKE_GCLOUD_STATE=str(self.temp / "gcloud.json"),
            FAKE_GH_STATE=str(self.gh_state),
            FAKE_ORIGIN=str(self.origin),
            GIT_AUTHOR_EMAIL="test@example.com",
            GIT_AUTHOR_NAME="Test",
            GIT_COMMITTER_EMAIL="test@example.com",
            GIT_COMMITTER_NAME="Test",
            LDP_ARGOCD_POLL_SECONDS="0.2",
            LDP_LOGIN_POLL_SECONDS="0.2",
            LDP_MERGE_POLL_SECONDS="0.2",
            LDP_POLL_SECONDS="0.05",
            PATH=f"{ROOT / 'tests' / 'bin'}{os.pathsep}{os.environ['PATH']}",
            XDG_CONFIG_HOME=str(self.temp / "config"),
            XDG_DATA_HOME=str(self.temp / "data"),
        )

        return environment

    def gh_state_data(self):
        return json.loads((self.gh_state / "state.json").read_text())

    def run_release(self, payload, on_tick=None, timeout=120):

        # Starts a run and drives it to the end: confirms the rollout and,
        # unless on_tick decides otherwise, keeps the tickets open.

        status, body = self.call("/api/runs", payload)

        self.assertEqual(201, status, body)

        return self.wait(body["id"], on_tick, timeout)

    def run_by_id(self, run_id):
        return {run["id"]: run for run in self.call("/api/runs")[1]}[run_id]

    def start_app(self):
        self.process = subprocess.Popen(
            [sys.executable, "-m", "ldp_releaser"], cwd=ROOT,
            env=self.environment(), stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)

        deadline = time.time() + 20

        while time.time() < deadline:
            if self.process.poll() is not None:
                self.fail(f"LDP Releaser exited: {self.process.stdout.read()}")

            try:
                if self.call("/api/config")[0] == 200:
                    return
            except OSError:
                pass

            time.sleep(0.1)

        self.fail("LDP Releaser did not start")

    def stop_app(self):
        if self.process and self.process.poll() is None:
            self.process.send_signal(signal.SIGTERM)

            try:
                self.process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self.process.kill()

        if self.process:
            self.process.stdout.close()

    def wait(self, run_id, on_tick=None, timeout=120):
        deadline = time.time() + timeout

        while time.time() < deadline:
            run = self.run_by_id(run_id)

            if run["finishedAt"]:
                return run

            if on_tick:
                on_tick(run)

            rollout = run["stages"].get("rollout")

            if (self.auto_confirm and rollout and rollout["status"] == "running" and
                    not run["rolloutConfirmed"]):

                self.call(f"/api/runs/{run_id}/confirm-rollout", {})

            time.sleep(0.1)

        self.fail(f"run {run_id} did not finish: {json.dumps(self.run_by_id(run_id))[:2000]}")

    def write_settings(self):
        directory = self.temp / "config" / "ldp-releaser"
        directory.mkdir(parents=True)

        (directory / "ldp-releaser.team.toml").write_text(TEAM.format(url=self.fakes.url))
        (directory / "config.toml").write_text(
            PERSONAL.format(
                asah_checkout=self.asah_checkout, checkout=self.checkout, port=self.port))

        for path in directory.iterdir():
            path.chmod(0o600)
