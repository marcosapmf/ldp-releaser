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

    def test_jenkins_outage_does_not_fail_the_build(self):

        # Jenkins' gateway answers 502, then Jenkins denies Overall/Read while
        # it restarts, 16 requests in all: more than the retries for other
        # errors allow, while the build keeps running there.

        run = self.run_release({"asahBranch": "outage"})

        log = run["stages"]["asahBuild"]["log"]

        self.assertEqual("success", run["status"])
        self.assertEqual("success", run["stages"]["asahBuild"]["status"])
        self.assertIn("Jenkins answers again after", log)
        self.assertIn("Overall/Read", log)
        self.assertIn("the build keeps running in Jenkins", log)
        self.assertIsNone(run["stages"]["asahBuild"]["why"])

    def test_jenkins_down_for_good_fails_the_run(self):
        run = self.run_release({"asahBranch": "down"})

        self.assertEqual("failed", run["status"])
        self.assertIn("502", run["error"])

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

        # The first released environment's gcloud configuration is active.

        self.assertEqual("team-stg", run["gcloudConfiguration"])
        self.assertEqual(
            "team-stg", json.loads((self.temp / "gcloud.json").read_text())["active"])

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
            [["Argo CD (STG)"], ["Argo CD (INT)"], ["Argo CD (PRD)"]],
            [[link["label"] for link in run["stages"][f"rollout-{environment}"]["links"]]
             for environment in ("stg", "int", "prd")])

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

    def test_run_saved_before_a_new_step_resumes(self):
        status, body = self.call(
            "/api/runs", {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-fffffff"})
        run_id = body["id"]

        self.wait_until(run_id, lambda run: run["stages"]["developMerge"]["status"] == "running")

        self.stop_app()

        # Saved by an older version: steps one after the other, a single
        # rollout, no Dataflow step and no step order.

        path = self.temp / "data" / "ldp-releaser" / "runs" / f"{run_id}.json"
        run = json.loads(path.read_text())

        for key in ("dataflow", "join", "lanes", "layout", "stageOrder", "tracks", "trunk"):
            run.pop(key)

        run["stages"].pop("rollout-stg")
        path.write_text(json.dumps(run))

        self.start_app()

        (self.gh_state / "merge-now").touch()

        run = self.wait(run_id, self.keep_tickets(run_id))

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual("skipped", run["stages"]["dataflow"]["status"])

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

    def builds(self):
        return sorted(target for job, target in self.faro_calls() if job == "build-faro")

    def deploys(self):
        return sorted(target for job, target in self.faro_calls() if job == "deploy-faro")

    def test_each_target_is_built_once_and_deployed_after_its_build(self):
        run = self.run_release(
            {"faroBranch": "main", "faroEnvironments": ["int", "stg"], "mode": "faro"})

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(["stg", "int"], run["faroEnvironments"])
        self.assertEqual(["prd", "stg"], self.builds())
        self.assertEqual(["int", "stg"], self.deploys())

        # The two builds run side by side; each deploy waits for its build.

        self.assertLess(run["stages"]["faroBuild-prd"]["startedAt"], run["stages"]["faroBuild-stg"]["finishedAt"])
        self.assertLessEqual(run["stages"]["faroBuild-prd"]["finishedAt"], run["stages"]["faroDeploy-int"]["startedAt"])
        self.assertLessEqual(run["stages"]["faroBuild-stg"]["finishedAt"], run["stages"]["faroDeploy-stg"]["startedAt"])
        self.assertIn("faro:prd-", run["faroImages"]["int"])
        self.assertIn("faro:stg-", run["faroImages"]["stg"])

        deploy_int = next(
            call for call in self.fakes.calls
            if call.get("job") == "deploy-faro" and call["params"]["ENVIRONMENT"] == "int")

        self.assertEqual(
            {"ENVIRONMENT": "int", "IMAGE": run["faroImages"]["int"]}, deploy_int["params"])

    def test_a_failure_stops_the_next_environments(self):
        run = self.run_release(
            {"faroBranch": "broken-prd", "faroEnvironments": ["stg", "int", "prd"],
             "mode": "faro"})

        self.assertEqual("failed", run["status"])
        self.assertEqual("failed", run["stages"]["faroBuild-prd"]["status"])
        self.assertEqual("skipped", run["stages"]["faroDeploy-int"]["status"])
        self.assertEqual("skipped", run["stages"]["faroDeploy-prd"]["status"])
        self.assertEqual(["prd", "stg"], self.builds())
        self.assertNotIn("int", self.deploys())
        self.assertNotIn("prd", self.deploys())

    def test_environments_with_the_same_target_share_one_build(self):
        run = self.run_release(
            {"faroBranch": "main", "faroEnvironments": ["stg", "int", "prd"], "mode": "faro"})

        self.assertEqual("success", run["status"], run["error"])
        self.assertEqual(["prd", "stg"], self.builds())
        self.assertEqual(["int", "prd", "stg"], self.deploys())
        self.assertEqual(run["faroImages"]["int"], run["faroImages"]["prd"])
        self.assertNotIn("faroBuild-int", run["stages"])
        self.assertEqual("Faro build prd (for INT and PRD)", run["stageLabels"]["faroBuild-prd"])
        self.assertEqual(
            {"stg": ["faroDeploy-stg"], "int": ["faroDeploy-int"], "prd": ["faroDeploy-prd"]}, run["lanes"])

    def test_prd_tag_points_at_the_prd_build(self):
        run = self.run_release(
            {"faroBranch": "main", "faroEnvironments": ["stg", "prd"], "mode": "faro",
             "version": "2.0.0"})

        self.assertEqual("success", run["status"], run["error"])

        # The two builds run side by side, so either can be #1.

        prd_build = next(
            build for (job, _), build in self.fakes.builds.items()
            if job == "build-faro" and build["params"]["ENVIRONMENT"] == "prd")

        self.assertEqual(
            f"{prd_build['number']:040x}", self.gh_state_data()["tags"]["example-org/faro:faro-v2.0.0"])


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


class ReuseBuildsTest(AppTestCase):

    def set_head(self, repository, branch, sha):
        state_file = self.gh_state / "state.json"
        state = {"next": 1, "pulls": {}, "tags": {}}

        if state_file.exists():
            state = json.loads(state_file.read_text())

        state.setdefault("heads", {})[f"{repository}:{branch}"] = sha
        self.gh_state.mkdir(parents=True, exist_ok=True)
        state_file.write_text(json.dumps(state))

    def builds(self, job):
        return [call for call in self.fakes.calls if call.get("job") == job]

    def test_an_unchanged_branch_reuses_the_last_asah_build(self):
        first = self.run_release({"asahBranch": "main", "mode": "asah"})

        self.assertEqual("success", first["status"], first["error"])

        self.set_head("example-org/asah", "main", first["asahCommit"])

        second = self.run_release({"asahBranch": "main", "mode": "asah"})

        self.assertEqual("success", second["status"], second["error"])
        self.assertEqual(1, len(self.builds("build-asah")))
        self.assertTrue(second["stages"]["asahBuild"]["reused"])
        self.assertEqual(first["asahImages"], second["asahImages"])
        self.assertEqual(first["stages"]["asahBuild"]["url"], second["stages"]["asahBuild"]["url"])
        self.assertIn("Reused build #1", second["stages"]["asahBuild"]["log"])

    def test_a_new_commit_builds_again(self):
        self.run_release({"asahBranch": "main", "mode": "asah"})
        self.set_head("example-org/asah", "main", "f" * 40)

        second = self.run_release({"asahBranch": "main", "mode": "asah"})

        self.assertEqual("success", second["status"], second["error"])
        self.assertEqual(2, len(self.builds("build-asah")))
        self.assertFalse(second["stages"]["asahBuild"].get("reused"))

    def test_reuse_can_be_turned_off(self):
        first = self.run_release({"asahBranch": "main", "mode": "asah"})
        self.set_head("example-org/asah", "main", first["asahCommit"])

        second = self.run_release({"asahBranch": "main", "mode": "asah", "reuseBuilds": False})

        self.assertEqual("success", second["status"], second["error"])
        self.assertEqual(2, len(self.builds("build-asah")))

    def test_a_later_run_deploys_the_faro_build_of_the_same_target(self):
        first = self.run_release({"faroBranch": "main", "faroEnvironments": ["prd"], "mode": "faro"})

        self.assertEqual("success", first["status"], first["error"])

        self.set_head("example-org/faro", "main", first["faroCommits"]["prd"])

        second = self.run_release({"faroBranch": "main", "faroEnvironments": ["int"], "mode": "faro"})

        self.assertEqual("success", second["status"], second["error"])
        self.assertEqual(1, len(self.builds("build-faro")))
        self.assertEqual(first["faroImages"]["prd"], second["faroImages"]["int"])
        self.assertEqual(
            first["faroImages"]["prd"],
            self.builds("deploy-faro")[-1]["params"]["IMAGE"])

    def test_a_build_for_another_target_is_not_reused(self):
        first = self.run_release({"faroBranch": "main", "faroEnvironments": ["stg"], "mode": "faro"})
        self.set_head("example-org/faro", "main", first["faroCommits"]["stg"])

        second = self.run_release({"faroBranch": "main", "faroEnvironments": ["prd"], "mode": "faro"})

        self.assertEqual("success", second["status"], second["error"])
        self.assertEqual(
            [("build-faro", "stg"), ("build-faro", "prd")],
            [(call["job"], call["params"]["ENVIRONMENT"]) for call in self.builds("build-faro")])


class ResumeEndedRunTest(AppTestCase):

    def wait_until(self, run_id, condition, timeout=60):
        deadline = time.time() + timeout

        while time.time() < deadline:
            run = self.run_by_id(run_id)

            if condition(run):
                return run

            time.sleep(0.1)

        self.fail(f"run {run_id} never got there: {json.dumps(self.run_by_id(run_id))[:1500]}")

    def test_a_run_cancelled_during_the_deploy_resumes_without_building_again(self):
        self.fakes.hold_deploys = True

        status, body = self.call("/api/runs", {"faroBranch": "main", "faroEnvironments": ["stg"], "mode": "faro"})
        run_id = body["id"]

        self.wait_until(run_id, lambda run: run["stages"]["faroDeploy-stg"]["status"] == "running")
        self.call(f"/api/runs/{run_id}/cancel", {})

        run = self.wait_until(run_id, lambda run: run["finishedAt"])

        self.assertEqual("aborted", run["status"])
        self.assertEqual("success", run["stages"]["faroBuild-stg"]["status"])
        self.assertEqual("aborted", run["stages"]["faroDeploy-stg"]["status"])

        self.fakes.hold_deploys = False

        status, body = self.call(f"/api/runs/{run_id}/resume", {})

        self.assertEqual(202, status, body)
        self.assertEqual(["faroDeploy-stg"], body["retried"])
        self.assertIn("faroBuild-stg", body["kept"])

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])

        builds = [call for call in self.fakes.calls if call.get("job") == "build-faro"]
        deploys = [call for call in self.fakes.calls if call.get("job") == "deploy-faro"]

        self.assertEqual(1, len(builds))
        self.assertEqual(2, len(deploys))
        self.assertEqual(deploys[0]["params"]["IMAGE"], deploys[1]["params"]["IMAGE"])
        self.assertIn("resumed: this step runs again", run["stages"]["faroDeploy-stg"]["log"])
        self.assertEqual(1, len(run["resumedAt"]))

    def test_only_an_ended_release_can_be_resumed(self):
        self.fakes.hold_deploys = True

        status, body = self.call("/api/runs", {"faroBranch": "main", "faroEnvironments": ["stg"], "mode": "faro"})
        run_id = body["id"]

        self.wait_until(run_id, lambda run: run["stages"]["faroDeploy-stg"]["status"] == "running")

        status, body = self.call(f"/api/runs/{run_id}/resume", {})

        self.assertEqual(409, status)
        self.assertIn("still running", body["error"])

        self.fakes.hold_deploys = False

        run = self.wait(run_id)

        self.assertEqual("success", run["status"])
        self.assertEqual(409, self.call(f"/api/runs/{run_id}/resume", {})[0])
