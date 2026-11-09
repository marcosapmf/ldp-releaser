"""The Dataflow flow: one build, templates published per configuration, and
the streaming jobs updated, or drained and relaunched when a person says so."""

import json
import subprocess
import time
import unittest

from tests.harness import AppTestCase

# The streaming jobs running before each test, by gcloud configuration.

JOBS = [
    ("streaming-stg-a", "example-staging", "us-west1"),
    ("streaming-prd-a", "example-prd-1", "europe-west1"),
    ("streaming-prd-b", "example-prd-1", "europe-west1"),
    ("streaming-prd-c", "example-prd-2", "us-east1"),
]


class DataflowTestCase(AppTestCase):

    running_jobs = JOBS

    def before_start(self):
        jobs = [
            {"creationTime": "2025-01-01 00:00:00", "id": f"old-{name}", "name": name,
             "project": project, "region": region, "state": "Running"}
            for name, project, region in self.running_jobs
        ]

        (self.temp / "gcloud.json").write_text(
            json.dumps({"active": "default", "configurations": {}, "jobs": jobs}))

    def asah_commit(self):
        return subprocess.run(
            ["git", "-C", str(self.asah_checkout), "rev-parse", "upstream/main"],
            capture_output=True, check=True, text=True).stdout.strip()

    def gcloud_state_data(self):
        return json.loads((self.temp / "gcloud.json").read_text())

    def job_states(self):
        return {job["id"]: job["state"] for job in self.gcloud_state_data()["jobs"]}

    def start_dataflow(self, payload):
        status, body = self.call("/api/dataflow-runs", payload)

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


class DataflowTest(DataflowTestCase):

    def test_templates_are_published_and_jobs_updated_per_configuration(self):
        run = self.wait(self.start_dataflow(
            {"environments": ["prd", "stg"], "ref": "main",
             "templates": ["batch-pipeline", "streaming-pipeline"]}))

        self.assertEqual("success", run["status"], run["error"])

        commit = self.asah_commit()
        date = subprocess.run(
            ["git", "-C", str(self.asah_checkout), "show", "-s", "--format=%cd",
             "--date=format-local:%Y%m%d", commit],
            capture_output=True, check=True, env={"TZ": "UTC"}, text=True).stdout.strip()
        version = f"{date}-{commit[:7]}"

        self.assertEqual(commit, run["commit"])
        self.assertEqual(
            ["gcloudLogin", "checkout", "build", "dataflow-team-stg", "dataflow-team-prd-1",
             "dataflow-team-prd-2"],
            run["stageOrder"])
        self.assertEqual(["streaming-pipeline", "batch-pipeline"], run["dataflowTemplates"])
        self.assertEqual(version, run["dataflowVersion"])

        state = self.gcloud_state_data()

        self.assertEqual(
            [
                f"{region}-docker.pkg.dev/{project}/pipelines/{template}:{version}"
                for project, region in (
                    ("example-staging", "us-west1"), ("example-prd-1", "europe-west1"),
                    ("example-prd-2", "us-east1"))
                for template in ("streaming-pipeline", "batch-pipeline")
            ],
            [template["image"] for template in state["templates"]])
        self.assertEqual(
            [
                {"name": "streaming-stg-a", "network": "net-stg", "project": "example-staging",
                 "subnetwork": "subnet-stg", "update": True},
                {"name": "streaming-prd-a", "network": "net-prd-1", "project": "example-prd-1",
                 "subnetwork": "subnet-prd-1", "update": True},
                {"name": "streaming-prd-b", "network": "net-prd-1", "project": "example-prd-1",
                 "subnetwork": "subnet-prd-1", "update": True},
                {"name": "streaming-prd-c", "network": "net-prd-2", "project": "example-prd-2",
                 "subnetwork": "subnet-prd-2", "update": True},
            ],
            state["launches"])
        self.assertEqual(
            {"Running": 4, "Updated": 4},
            {value: list(self.job_states().values()).count(value)
             for value in set(self.job_states().values())})
        self.assertNotIn("drains", state)

        # The active configuration followed the deploy.

        self.assertEqual(["team-stg", "team-prd-1", "team-prd-2"], state["activations"])
        self.assertEqual("team-prd-2", state["active"])
        self.assertEqual("team-prd-2", run["gcloudConfiguration"])

        # The personal clone keeps its branch and its uncommitted work, and
        # has no leftover worktree.

        self.assertEqual(
            "my-feature",
            subprocess.run(
                ["git", "-C", str(self.asah_checkout), "branch", "--show-current"],
                capture_output=True, check=True, text=True).stdout.strip())
        self.assertTrue((self.asah_checkout / "work-in-progress.txt").exists())

    def test_batch_template_only_is_published_without_touching_jobs(self):
        run = self.wait(self.start_dataflow(
            {"environments": ["stg"], "ref": "main", "templates": ["batch-pipeline"]}))

        self.assertEqual("success", run["status"], run["error"])

        state = self.gcloud_state_data()

        self.assertEqual(
            ["gs://example-staging-dataflow/flex-templates/batch-pipeline.json"],
            [template["spec"] for template in state["templates"]])
        self.assertNotIn("launches", state)

    def test_refused_update_waits_to_drain_and_relaunch(self):
        (self.temp / "dataflow-incompatible").touch()

        run_id = self.start_dataflow(
            {"environments": ["stg"], "ref": "main", "templates": ["streaming-pipeline"]})

        run = self.wait_for(
            lambda: (lambda run: run["dataflowRelaunch"] and run)(self.run_by_id(run_id)))
        stage = run["stages"]["dataflow-team-stg"]

        self.assertEqual("running", stage["status"])
        self.assertIn("the updated job job-5 is Failed", stage["why"])
        self.assertEqual(
            "https://console.cloud.google.com/dataflow/jobs/us-west1/old-streaming-stg-a"
            "?project=example-staging",
            stage["links"][0]["url"])
        self.assertEqual("Running", self.job_states()["old-streaming-stg-a"])

        self.assertEqual(202, self.call(f"/api/runs/{run_id}/relaunch-dataflow", {})[0])

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIsNone(run["dataflowRelaunch"])

        state = self.gcloud_state_data()

        self.assertEqual(["old-streaming-stg-a"], state["drains"])
        self.assertEqual([True, False], [launch["update"] for launch in state["launches"]])
        self.assertEqual(
            {"job-5": "Failed", "job-6": "Running", "old-streaming-prd-a": "Running",
             "old-streaming-prd-b": "Running", "old-streaming-prd-c": "Running",
             "old-streaming-stg-a": "Drained"},
            self.job_states())
        self.assertEqual(409, self.call(f"/api/runs/{run_id}/relaunch-dataflow", {})[0])

    def test_refused_update_can_be_cancelled(self):
        (self.temp / "dataflow-incompatible").touch()

        run_id = self.start_dataflow(
            {"environments": ["stg"], "ref": "main", "templates": ["streaming-pipeline"]})

        self.wait_for(lambda: self.run_by_id(run_id)["dataflowRelaunch"])
        self.call(f"/api/runs/{run_id}/cancel", {})

        run = self.wait(run_id)

        self.assertEqual("aborted", run["status"])
        self.assertNotIn("drains", self.gcloud_state_data())
        self.assertEqual("Running", self.job_states()["old-streaming-stg-a"])

    def test_expired_login_waits_before_the_build(self):
        (self.temp / "gcloud-expired").touch()

        run_id = self.start_dataflow(
            {"environments": ["stg"], "ref": "main", "templates": ["batch-pipeline"]})

        self.wait_for(
            lambda: self.run_by_id(run_id)["stages"]["gcloudLogin"].get("needsGcloudLogin"))

        self.assertEqual("pending", self.run_by_id(run_id)["stages"]["build"]["status"])

        (self.temp / "gcloud-expired").unlink()

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])

    def test_validation(self):
        cases = [
            ({"environments": [], "ref": "main", "templates": ["batch-pipeline"]},
             "Pick environments"),
            ({"environments": ["int"], "ref": "main", "templates": ["batch-pipeline"]},
             "Pick environments"),
            ({"environments": ["stg"], "ref": "main; rm -rf /", "templates": ["batch-pipeline"]},
             "Asah branch, tag or commit"),
            ({"environments": ["stg"], "ref": "main", "templates": []}, "Pick templates"),
            ({"environments": ["stg"], "ref": "main", "templates": ["other"]}, "Pick templates"),
        ]

        for payload, message in cases:
            status, body = self.call("/api/dataflow-runs", payload)

            self.assertEqual(400, status, payload)
            self.assertIn(message, body["error"], payload)


class DataflowWithoutJobsTest(DataflowTestCase):

    running_jobs = []

    def test_streaming_job_that_is_not_running_is_not_started(self):
        run = self.wait(self.start_dataflow(
            {"environments": ["stg"], "ref": "main", "templates": ["streaming-pipeline"]}))

        self.assertEqual("failed", run["status"])
        self.assertIn("streaming-stg-a is not running in team-stg", run["error"])
        self.assertNotIn("launches", self.gcloud_state_data())


class ReleaseWithDataflowTest(DataflowTestCase):

    def keep_tickets(self, run):
        stage = run["stages"]["closeTickets"]

        if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
            self.call(f"/api/runs/{run['id']}/keep-tickets", {})

    def test_templates_of_the_released_commit_follow_the_composer_dags(self):
        commit = self.asah_commit()

        self.fakes.revisions["build-asah"] = commit

        run = self.run_release(
            {"asahBranch": "main", "composer": True, "dataflow": ["streaming-pipeline"],
             "mode": "asah", "releaseEnvironments": ["stg"]},
            self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(commit, run["dataflowCommit"])
        self.assertEqual(run["tagSuffix"], run["dataflowVersion"])
        self.assertLess(
            run["stages"]["composer"]["finishedAt"], run["stages"]["dataflow"]["startedAt"])
        self.assertEqual(
            [f"us-west1-docker.pkg.dev/example-staging/pipelines/streaming-pipeline:"
             f"{run['tagSuffix']}"],
            [template["image"] for template in self.gcloud_state_data()["templates"]])
        self.assertEqual(["streaming-stg-a"], [
            launch["name"] for launch in self.gcloud_state_data()["launches"]])

    def test_dataflow_needs_released_environments(self):
        cases = [
            ({"asahBranch": "main", "dataflow": ["batch-pipeline"]}, "pick at least one"),
            ({"dataflow": ["other"], "releaseEnvironments": ["stg"],
              "tagSuffix": "20260101-1234567"}, "Pick templates"),
            ({"dataflow": ["batch-pipeline"], "releaseEnvironments": ["int"],
              "tagSuffix": "20260101-1234567"}, "No gcloud configurations for int"),
        ]

        for payload, message in cases:
            status, body = self.call("/api/runs", payload)

            self.assertEqual(400, status, payload)
            self.assertIn(message, body["error"], payload)


if __name__ == "__main__":
    unittest.main()
