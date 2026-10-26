"""End-to-end release scenarios against the fakes."""

import json
import subprocess
import time
import unittest

from tests.harness import AppTestCase, git


def decide(decision):

    # Answers the "close the tickets?" question of a non-PRD release.

    def on_tick(run):
        stage = run["stages"]["closeTickets"]

        if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
            on_tick.case.call(f"/api/runs/{run['id']}/{decision}", {})

    return on_tick


class ReleaseTest(AppTestCase):

    def decide(self, decision):
        on_tick = decide(decision)
        on_tick.case = self

        return on_tick

    def file_on(self, branch, environment):
        return subprocess.run(
            ["git", "--git-dir", str(self.origin), "show",
             f"{branch}:deploy/{environment}.yaml"],
            capture_output=True, check=True, text=True).stdout

    def test_asah_build_failure_skips_the_rest(self):
        run = self.run_release(
            {"asahBranch": "broken", "environment": "stg", "faroBranch": "main",
             "releaseEnvironments": ["stg"]})

        self.assertEqual("failed", run["status"])
        self.assertEqual("failed", run["stages"]["asahBuild"]["status"])
        self.assertEqual("skipped", run["stages"]["jira"]["status"])
        self.assertEqual("skipped", run["stages"]["faroBuild-stg"]["status"])
        self.assertEqual({}, self.fakes.issues)

    def test_cancel_while_waiting_for_merge(self):
        def on_tick(run):
            if (run["stages"]["developMerge"]["status"] == "running" and
                    not run["cancelRequested"]):

                self.call(f"/api/runs/{run['id']}/cancel", {})

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-fffffff"},
            on_tick)

        self.assertEqual("aborted", run["status"])
        self.assertEqual("aborted", run["stages"]["developMerge"]["status"])
        self.assertEqual("skipped", run["stages"]["closeTickets"]["status"])

    def test_closed_pull_request_fails_the_run(self):
        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-ccccccc"})

        self.assertEqual("failed", run["status"])
        self.assertIn("closed without being merged", run["error"])

    def test_flaky_jenkins_is_retried(self):
        run = self.run_release({"asahBranch": "flaky"})

        log = run["stages"]["asahBuild"]["log"]

        self.assertEqual("success", run["status"])
        self.assertIn("Access Denied", log)
        self.assertIn("retrying", log)

    def test_non_prd_release_asks_about_the_tickets(self):
        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1111111"},
            self.decide("keep-tickets"))

        self.assertEqual("success", run["status"])
        self.assertEqual("skipped", run["stages"]["closeTickets"]["status"])
        self.assertEqual("skipped", run["stages"]["productionPullRequest"]["status"])
        self.assertEqual("In Progress", self.fakes.issues[run["jiraTask"]]["status"])
        self.assertIn('tagSuffix: "20260101-1111111"', self.file_on("develop", "stg"))

        tickets = self.call("/api/release-tickets")[1]

        self.assertEqual([run["jiraTask"]], [ticket["key"] for ticket in tickets])

        second = self.run_release(
            {"releaseEnvironments": ["stg", "int"], "tagSuffix": "20260101-2222222",
             "tickets": {"action": "reuse", "key": run["jiraTask"]}},
            self.decide("close-tickets"))

        task = self.fakes.issues[run["jiraTask"]]

        self.assertEqual("success", second["status"])
        self.assertEqual(run["jiraTask"], second["jiraTask"])
        self.assertEqual("Closed", task["status"])
        self.assertEqual("Completed", task["resolution"])
        self.assertEqual("Release STG and INT Environments", task["summary"])

    def test_prd_release_end_to_end(self):
        run = self.run_release(
            {"asahBranch": "main", "environment": "prd", "faroBranch": "main",
             "releaseEnvironments": ["stg", "int", "prd"], "version": "1.2.3"})

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual("20260101-abcd001", run["tagSuffix"])

        task = self.fakes.issues[run["jiraTask"]]
        technical_task = self.fakes.issues[run["jiraTechnicalTask"]]

        self.assertEqual("Closed", task["status"])
        self.assertEqual("Release STG, INT and PRD Environments", task["summary"])
        self.assertEqual("Closed", technical_task["status"])
        self.assertEqual(run["jiraTask"], technical_task["parent"])

        for environment in ("stg", "int", "prd"):
            self.assertIn('tagSuffix: "20260101-abcd001"', self.file_on("develop", environment))

        for environment in ("int", "prd"):
            self.assertIn(
                'tagSuffix: "20260101-abcd001"', self.file_on("production", environment))

        pulls = self.gh_state_data()["pulls"]

        self.assertEqual(["develop", "production"], [pull["base"] for pull in pulls.values()])
        self.assertIn("Cherry-pick of #1", pulls["2"]["body"])
        self.assertEqual(
            [{"chartsUrl": "https://github.example/pull/1", "environments": "STG, INT, PRD"},
             {"chartsUrl": "https://github.example/pull/2", "environments": "STG, INT, PRD"}],
            self.fakes.slack)

        tags = self.gh_state_data()["tags"]

        self.assertEqual(f"{1:040x}", tags["example-org/asah:1.2.3"])
        self.assertEqual(f"{1:040x}", tags["example-org/faro:faro-v1.2.3"])

        self.assertEqual(
            ["Argo CD (STG)", "Argo CD (INT, PRD)"],
            [link["label"] for link in run["stages"]["rollout"]["links"]])

        deploy = next(call for call in self.fakes.calls if call.get("job") == "deploy-faro")

        self.assertEqual(
            {"ENVIRONMENT": "prd", "IMAGE": "registry.example.com/faro:prd-20260101-f000001"},
            deploy["params"])

    def test_production_conflict_after_a_develop_only_release(self):
        first = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1111111"},
            self.decide("close-tickets"))

        self.assertEqual("success", first["status"])

        second = self.run_release(
            {"releaseEnvironments": ["stg", "int"], "tagSuffix": "20260101-2222222"},
            self.decide("close-tickets"))

        self.assertEqual("success", second["status"], second["error"])
        self.assertIn(
            "Resolved the conflict in deploy/stg.yaml",
            second["stages"]["productionPullRequest"]["log"])
        self.assertIn('tagSuffix: "20260101-2222222"', self.file_on("production", "int"))
        self.assertIn('tagSuffix: "20260101-2222222"', self.file_on("production", "stg"))

    def test_teammates_ticket_is_offered_and_discarded(self):
        teammate = {"accountId": "someone-else", "displayName": "Teammate"}

        task = self.fakes.add_issue(
            "Task", "Release PRD Environment", assignee=teammate, status="In Progress")
        technical_task = self.fakes.add_issue(
            "Technical Task", "Release PRD Environment", assignee=teammate,
            parent=task, status="In Progress")

        tickets = self.call("/api/release-tickets")[1]

        self.assertEqual("Teammate", tickets[0]["assignee"]["displayName"])
        self.assertEqual([technical_task], tickets[0]["technicalTasks"])

        status, body = self.call(
            "/api/runs", {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-3333333"})

        self.assertEqual(400, status)
        self.assertIn("choose whether to reuse", body["error"])

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-3333333",
             "tickets": {"action": "new"}},
            self.decide("keep-tickets"))

        self.assertEqual("success", run["status"], run["error"])
        self.assertNotEqual(task, run["jiraTask"])
        self.assertEqual("Discarded", self.fakes.issues[task]["resolution"])
        self.assertEqual("Discarded", self.fakes.issues[technical_task]["resolution"])

    def test_validation(self):
        state = self.gh_state / "state.json"
        state.write_text(json.dumps(
            {"next": 1, "pulls": {}, "tags": {"example-org/asah:1.0.0": "x"}}))

        cases = [
            ({}, "Nothing to do"),
            ({"releaseEnvironments": ["dev"], "tagSuffix": "20260101-1234567"}, "must be among"),
            ({"releaseEnvironments": ["stg"], "tagSuffix": "nope"}, "tag suffix"),
            ({"environment": "qa", "faroBranch": "main"}, "must be among"),
            ({"faroBranch": "main", "faroEnvironments": []}, "Pick at least one Faro environment"),
            ({"asahBranch": "main", "releaseEnvironments": ["stg"], "version": "1.0.1"},
             "only created for PRD"),
            ({"asahBranch": "main", "releaseEnvironments": ["prd"], "version": "1.0.0"},
             "already exists"),
            ({"releaseEnvironments": ["prd"], "tagSuffix": "20260101-1234567",
              "version": "1.0.1"}, "Asah or Faro branch"),
        ]

        for payload, message in cases:
            status, body = self.call("/api/runs", payload)

            self.assertEqual(400, status, payload)
            self.assertIn(message, body["error"], payload)


class ResumeTest(AppTestCase):

    def wait_until(self, run_id, condition, timeout=60):
        deadline = time.time() + timeout

        while time.time() < deadline:
            run = self.run_by_id(run_id)

            if condition(run):
                return run

            time.sleep(0.1)

        self.fail("condition not reached")

    def test_build_reattaches_after_a_restart(self):
        status, body = self.call("/api/runs", {"asahBranch": "hold"})
        run_id = body["id"]

        before = self.wait_until(
            run_id, lambda run: run["stages"]["asahBuild"]["number"] is not None)

        self.stop_app()
        self.start_app()

        self.fakes.release_builds()

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(before["stages"]["asahBuild"]["number"], run["stages"]["asahBuild"]["number"])
        self.assertIn("Resumed after a restart", run["stages"]["asahBuild"]["log"])
        self.assertEqual(1, sum(1 for call in self.fakes.calls if call.get("job") == "build-asah"))

    def test_merge_wait_resumes_after_a_restart(self):
        status, body = self.call(
            "/api/runs", {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-fffffff"})
        run_id = body["id"]

        self.wait_until(run_id, lambda run: run["stages"]["developMerge"]["status"] == "running")

        self.stop_app()
        self.start_app()

        (self.gh_state / "merge-now").touch()

        run = self.wait(run_id, self.keep_tickets(run_id))

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(1, len(self.gh_state_data()["pulls"]))
        self.assertEqual(1, len([key for key, issue in self.fakes.issues.items() if issue["type"] == "Task"]))

    def test_restart_during_a_jira_change_fails_the_run(self):
        status, body = self.call(
            "/api/runs", {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-fffffff"})
        run_id = body["id"]

        self.wait_until(run_id, lambda run: run["stages"]["developMerge"]["status"] == "running")

        self.stop_app()

        # Pretend the server stopped in the middle of the pull request step.

        path = self.temp / "data" / "ldp-releaser" / "runs" / f"{run_id}.json"
        run = json.loads(path.read_text())
        run["stages"]["developMerge"]["status"] = "pending"
        run["stages"]["developPullRequest"]["status"] = "running"
        path.write_text(json.dumps(run))

        self.start_app()

        run = self.run_by_id(run_id)

        self.assertEqual("failed", run["status"])
        self.assertIn("developPullRequest step was running", run["error"])

    def keep_tickets(self, run_id):
        def on_tick(run):
            stage = run["stages"]["closeTickets"]

            if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
                self.call(f"/api/runs/{run_id}/keep-tickets", {})

        return on_tick


class SecurityTest(AppTestCase):

    def test_requests_from_other_sites_are_rejected(self):
        request_with_origin = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "-X", "POST",
             "-H", "Content-Type: application/json", "-H", "Origin: https://evil.example",
             "-d", "{}", f"http://127.0.0.1:{self.port}/api/runs"],
            capture_output=True, text=True)
        request_with_host = subprocess.run(
            ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "-H", "Host: evil.example", f"http://127.0.0.1:{self.port}/api/runs"],
            capture_output=True, text=True)

        self.assertEqual("403", request_with_origin.stdout)
        self.assertEqual("403", request_with_host.stdout)


if __name__ == "__main__":
    unittest.main()


class FaroEnvironmentsTest(AppTestCase):

    def faro_calls(self):
        return [
            (call["job"], call["params"].get("ENVIRONMENT"))
            for call in self.fakes.calls if call.get("job") in ("build-faro", "deploy-faro")
        ]

    def test_environments_are_built_and_deployed_one_after_the_other(self):
        run = self.run_release(
            {"faroBranch": "main", "faroEnvironments": ["int", "stg"], "mode": "faro"})

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(["stg", "int"], run["faroEnvironments"])
        self.assertEqual(
            [("build-faro", "stg"), ("deploy-faro", "stg"), ("build-faro", "prd"),
             ("deploy-faro", "int")],
            self.faro_calls())
        self.assertLessEqual(
            run["stages"]["faroDeploy-stg"]["finishedAt"],
            run["stages"]["faroBuild-int"]["startedAt"])
        self.assertEqual(
            {"int": "registry.example.com/faro:prd-20260101-f000002",
             "stg": "registry.example.com/faro:stg-20260101-f000001"},
            run["faroImages"])

        deploy_int = [
            call for call in self.fakes.calls if call.get("job") == "deploy-faro"][1]

        self.assertEqual(
            {"ENVIRONMENT": "int", "IMAGE": "registry.example.com/faro:prd-20260101-f000002"},
            deploy_int["params"])

    def test_a_failure_stops_the_next_environments(self):
        run = self.run_release(
            {"faroBranch": "broken-prd", "faroEnvironments": ["stg", "int", "prd"],
             "mode": "faro"})

        self.assertEqual("failed", run["status"])
        self.assertEqual("success", run["stages"]["faroDeploy-stg"]["status"])
        self.assertEqual("failed", run["stages"]["faroBuild-int"]["status"])
        self.assertEqual("skipped", run["stages"]["faroDeploy-int"]["status"])
        self.assertEqual("skipped", run["stages"]["faroBuild-prd"]["status"])
        self.assertEqual(
            [("build-faro", "stg"), ("deploy-faro", "stg"), ("build-faro", "prd")],
            self.faro_calls())

    def test_prd_tag_points_at_the_prd_build(self):
        run = self.run_release(
            {"faroBranch": "main", "faroEnvironments": ["stg", "prd"], "mode": "faro",
             "version": "2.0.0"})

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(
            f"{2:040x}", self.gh_state_data()["tags"]["example-org/faro:faro-v2.0.0"])


class AutoMergeTest(AppTestCase):

    def keep_tickets(self, run):
        stage = run["stages"]["closeTickets"]

        if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
            self.call(f"/api/runs/{run['id']}/keep-tickets", {})

    def test_approved_pull_requests_are_merged_once_prisma_passes(self):
        run = self.run_release(
            {"releaseEnvironments": ["stg", "int"], "tagSuffix": "20260101-1234567"},
            self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(
            [{"method": "squash", "number": "1"}, {"method": "merge", "number": "2"}],
            self.gh_state_data()["merges"])

        develop_log = run["stages"]["developMerge"]["log"]

        self.assertIn("waiting for Prisma Cloud / Code analysis", develop_log)
        self.assertIn("approved and checks passed, merged (squash)", develop_log)
        self.assertIn(
            "merged (merge)", run["stages"]["productionMerge"]["log"])

    def test_a_failed_prisma_check_blocks_the_merge(self):
        status, body = self.call(
            "/api/runs", {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-9999999"})
        run_id = body["id"]

        deadline = time.time() + 30

        while time.time() < deadline:
            log = self.run_by_id(run_id)["stages"]["developMerge"]["log"]

            if "failed" in log:
                break

            time.sleep(0.1)

        self.assertIn(
            "not merging: Prisma Cloud / Code analysis failed; re-run it or merge by hand", log)

        time.sleep(1)

        self.assertNotIn("merges", self.gh_state_data())
        self.assertEqual("running", self.run_by_id(run_id)["stages"]["developMerge"]["status"])

        self.call(f"/api/runs/{run_id}/cancel", {})

        self.assertEqual("aborted", self.wait(run_id)["status"])


class HumanMergeTest(AppTestCase):

    def environment(self):
        return {**super().environment(), "FAKE_GH_HUMAN_MERGES": "1"}

    def write_settings(self):

        # A team without merge settings: people merge the pull requests.

        super().write_settings()

        team = self.temp / "config" / "ldp-releaser" / "ldp-releaser.team.toml"

        text = team.read_text()
        start = text.index("[charts.merge]")
        end = text.index("[jira]")

        team.write_text(text[:start] + text[end:])

    def test_without_merge_settings_the_run_waits_for_a_person(self):
        def keep(run):
            stage = run["stages"]["closeTickets"]

            if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
                self.call(f"/api/runs/{run['id']}/keep-tickets", {})

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"}, keep)

        self.assertEqual("success", run["status"], run["error"])
        self.assertNotIn("merges", self.gh_state_data())
        self.assertNotIn("checks passed, merged", run["stages"]["developMerge"]["log"])
        self.assertIn("merged (approved)", run["stages"]["developMerge"]["log"])
