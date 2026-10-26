"""The Composer flow: DAG uploads with a gcloud login check first."""

import subprocess
import time
import unittest

from tests.harness import AppTestCase


class ComposerTest(AppTestCase):

    def expire_login(self):
        (self.temp / "gcloud-expired").touch()

    def git_output(self, *args):
        return subprocess.run(
            ["git", "-C", str(self.asah_checkout), *args], capture_output=True,
            check=True, text=True).stdout.strip()

    def imports(self):
        return self.gcloud_state_data().get("imports", [])

    def gcloud_state_data(self):
        import json

        return json.loads((self.temp / "gcloud.json").read_text())

    def start_composer(self, payload):
        status, body = self.call("/api/composer-runs", payload)

        self.assertEqual(201, status, body)

        return body["id"]

    def wait_for(self, condition, timeout=60):
        deadline = time.time() + timeout

        while time.time() < deadline:
            value = condition()

            if value:
                return value

            time.sleep(0.1)

        self.fail("condition not reached")

    def test_dags_are_uploaded_to_each_configuration(self):
        run = self.wait(self.start_composer({"environments": ["prd", "stg"], "ref": "main"}))

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(["stg", "prd"], run["environments"])
        self.assertEqual(
            ["gcloudLogin", "checkout", "dags-team-stg", "dags-team-prd-1", "dags-team-prd-2"],
            run["stageOrder"])
        self.assertEqual(self.git_output("rev-parse", "upstream/main"), run["commit"])

        expected = []

        for environment, location, project in (
                ("ac-team-stg", "us-west1", "example-staging"),
                ("ac-team-prd-1", "europe-west1", "example-prd-1"),
                ("ac-second", "us-east1", "example-prd-2")):

            for source in ("a_dag.py", "b_dag.py", "src/main/resources/"):
                expected.append(
                    {"environment": environment, "location": location,
                     "project": project, "source": source})

        self.assertEqual(expected, self.imports())

        # The personal clone keeps its branch, its uncommitted work and has
        # no leftover worktree.

        self.assertEqual("my-feature", self.git_output("branch", "--show-current"))
        self.assertTrue((self.asah_checkout / "work-in-progress.txt").exists())
        self.assertEqual(1, len(self.git_output("worktree", "list").splitlines()))

    def test_expired_login_waits_for_the_login_button(self):
        self.expire_login()

        run_id = self.start_composer({"environments": ["stg"], "ref": "main"})

        run = self.wait_for(
            lambda: (lambda run: run["stages"]["gcloudLogin"].get("needsGcloudLogin") and run)(
                self.run_by_id(run_id)))

        self.assertIn("expired", run["stages"]["gcloudLogin"]["why"])
        self.assertEqual([], self.imports())
        self.assertIn("expired", self.call("/api/gcloud/login?check=1")[1]["problem"])

        self.assertEqual(202, self.call("/api/gcloud/login", {})[0])

        state = self.wait_for(
            lambda: (lambda state: state["needsCode"] and state)(self.call("/api/gcloud/login")[1]))

        self.assertEqual("https://accounts.example/o/oauth2/auth?client=fake", state["url"])

        self.assertEqual(202, self.call("/api/gcloud/login/code", {"code": "good-code"})[0])

        self.wait_for(lambda: self.call("/api/gcloud/login")[1]["status"] == "succeeded")

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn("gcloud login is valid again", run["stages"]["gcloudLogin"]["log"])
        self.assertEqual(3, len(self.imports()))

    def test_wrong_code_keeps_waiting_until_cancelled(self):
        self.expire_login()

        run_id = self.start_composer({"environments": ["stg"], "ref": "main"})

        self.wait_for(lambda: self.run_by_id(run_id)["stages"]["gcloudLogin"].get("needsGcloudLogin"))

        self.call("/api/gcloud/login", {})
        self.wait_for(lambda: self.call("/api/gcloud/login")[1]["needsCode"])
        self.call("/api/gcloud/login/code", {"code": "wrong"})

        self.wait_for(lambda: self.call("/api/gcloud/login")[1]["status"] == "failed")

        self.assertEqual("running", self.run_by_id(run_id)["status"])

        self.call(f"/api/runs/{run_id}/cancel", {})

        run = self.wait(run_id)

        self.assertEqual("aborted", run["status"])
        self.assertEqual([], self.imports())

    def test_login_expiring_during_the_upload_is_retried(self):
        run_id = self.start_composer({"environments": ["stg", "prd"], "ref": "main"})

        # Expire the login as soon as the first configuration is done.

        self.wait_for(lambda: len(self.imports()) >= 3)
        self.expire_login()

        self.wait_for(
            lambda: any(
                stage.get("needsGcloudLogin")
                for stage in self.run_by_id(run_id)["stages"].values()))

        (self.temp / "gcloud-expired").unlink()

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(
            {"ac-team-stg", "ac-team-prd-1", "ac-second"},
            {entry["environment"] for entry in self.imports()})

    def test_validation(self):
        cases = [
            ({"environments": [], "ref": "main"}, "Pick environments"),
            ({"environments": ["int"], "ref": "main"}, "Pick environments"),
            ({"environments": ["stg"], "ref": ""}, "Asah branch, tag or commit"),
            ({"environments": ["stg"], "ref": "main; rm -rf /"}, "Asah branch, tag or commit"),
        ]

        for payload, message in cases:
            status, body = self.call("/api/composer-runs", payload)

            self.assertEqual(400, status, payload)
            self.assertIn(message, body["error"], payload)

    def test_unknown_version_fails_at_the_fetch(self):
        run = self.wait(self.start_composer({"environments": ["stg"], "ref": "no-such-branch"}))

        self.assertEqual("failed", run["status"])
        self.assertEqual("failed", run["stages"]["checkout"]["status"])
        self.assertEqual([], self.imports())


if __name__ == "__main__":
    unittest.main()


class ReleaseWithComposerTest(AppTestCase):

    def asah_commit(self):
        return subprocess.run(
            ["git", "-C", str(self.asah_checkout), "rev-parse", "upstream/main"],
            capture_output=True, check=True, text=True).stdout.strip()

    def imports(self):
        import json

        return json.loads((self.temp / "gcloud.json").read_text()).get("imports", [])

    def keep_tickets(self, run):
        stage = run["stages"]["closeTickets"]

        if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
            self.call(f"/api/runs/{run['id']}/keep-tickets", {})

    def test_dags_of_the_built_commit_follow_the_rollout(self):
        commit = self.asah_commit()

        self.fakes.revisions["build-asah"] = commit

        run = self.run_release(
            {"asahBranch": "main", "composer": True, "mode": "asah",
             "releaseEnvironments": ["stg"]},
            self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual("asah", run["mode"])
        self.assertEqual(commit, run["composerCommit"])
        self.assertEqual(["team-stg"], run["composerDone"])
        self.assertLess(
            run["stages"]["rollout"]["finishedAt"], run["stages"]["composer"]["startedAt"])
        self.assertEqual(
            ["a_dag.py", "b_dag.py", "src/main/resources/"],
            [entry["source"] for entry in self.imports()])
        self.assertEqual({"ac-team-stg"}, {entry["environment"] for entry in self.imports()})

    def test_dags_of_a_typed_tag_suffix(self):
        commit = self.asah_commit()

        run = self.run_release(
            {"composer": True, "releaseEnvironments": ["stg"],
             "tagSuffix": f"20260101-{commit[:7]}"},
            self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(commit, run["composerCommit"])
        self.assertEqual(3, len(self.imports()))

    def test_faro_tab_runs_only_faro(self):
        run = self.run_release({"environment": "stg", "faroBranch": "main", "mode": "faro"})

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual("faro", run["mode"])
        self.assertEqual("skipped", run["stages"]["asahBuild"]["status"])
        self.assertEqual("success", run["stages"]["faroDeploy-stg"]["status"])

    def test_modes_and_composer_are_validated(self):
        cases = [
            ({"asahBranch": "main", "environment": "stg", "faroBranch": "main", "mode": "asah"},
             "does not build Faro"),
            ({"environment": "stg", "faroBranch": "main", "mode": "faro",
              "releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"},
             "does not build or release Asah"),
            ({"asahBranch": "main", "composer": True}, "pick at least one"),
            ({"composer": True, "releaseEnvironments": ["int"], "tagSuffix": "20260101-1234567"},
             "No gcloud configurations for int"),
            ({"asahBranch": "main", "mode": "dataflow"}, "Unknown mode"),
        ]

        for payload, message in cases:
            status, body = self.call("/api/runs", payload)

            self.assertEqual(400, status, payload)
            self.assertIn(message, body["error"], payload)
