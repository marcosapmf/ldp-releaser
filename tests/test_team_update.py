"""A newer team file in 1Password: noticed by the page, and applied with a
restart when the person asks."""

import os
import stat
import time
import unittest

from ldp_releaser import release, store
from tests.harness import TEAM, AppTestCase


class TeamUpdateTestCase(AppTestCase):

    def write_settings(self):
        super().write_settings()

        with (self.config_dir() / "config.toml").open("a") as file:
            file.write('\n[team]\nonepassword_item = "team file"\nonepassword_vault = "Shared"\n')

        self.publish(self.team_text())

    def config_dir(self):
        return self.temp / "config" / "ldp-releaser"

    def op_calls(self):
        path = self.temp / "op" / "calls"

        if not path.exists():
            return []

        return path.read_text().splitlines()

    def publish(self, text):
        directory = self.temp / "op" / "Shared"
        directory.mkdir(parents=True, exist_ok=True)

        (directory / "team file").write_text(text)

    def team_text(self):
        return TEAM.format(url=self.fakes.url)

    def team_update(self):

        # Asks for one check, then waits for its answer.

        self.call("/api/team-update?check=1")

        deadline = time.time() + 30

        while time.time() < deadline:
            status, body = self.call("/api/team-update")

            self.assertEqual(200, status, body)

            if body["status"] not in ("checking", "unknown"):
                return body

            time.sleep(0.1)

        self.fail("the check did not finish")


class TeamUpdateTest(TeamUpdateTestCase):

    def test_same_file_is_current(self):
        self.assertEqual("current", self.team_update()["status"])
        self.assertIn("document get team file --vault Shared", self.op_calls())

    def test_newer_file_is_applied_and_the_run_resumes(self):
        newer = self.team_text().replace(
            'templates = ["streaming-pipeline", "batch-pipeline"]',
            'templates = ["streaming-pipeline", "batch-pipeline", "other-pipeline"]')

        self.publish(newer)

        update = self.team_update()

        self.assertEqual(["dataflow.templates"], update["changes"])
        self.assertEqual("available", update["status"])

        # A build waiting in Jenkins is re-attached after the restart.

        status, body = self.call("/api/runs", {"asahBranch": "hold"})
        run_id = body["id"]

        self.assertEqual(202, self.call("/api/team-update", {})[0])

        deadline = time.time() + 30

        while time.time() < deadline:
            try:
                status, config = self.call("/api/config")

                if status == 200 and "other-pipeline" in config["dataflow"]["templates"]:
                    break
            except OSError:
                pass

            time.sleep(0.1)
        else:
            self.fail("LDP Releaser did not restart with the new settings")

        team_file = self.config_dir() / "ldp-releaser.team.toml"

        self.assertEqual(newer, team_file.read_text())
        self.assertEqual(0o600, stat.S_IMODE(team_file.stat().st_mode))
        self.assertEqual("current", self.team_update()["status"])

        self.fakes.release_builds()

        run = self.wait(run_id)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn("Resumed after a restart", run["stages"]["asahBuild"]["log"])

    def test_file_this_version_cannot_use_is_not_applied(self):
        self.publish(self.team_text().replace('prd_environment = "prd"', ""))

        update = self.team_update()

        self.assertEqual(["release.prd_environment"], update["changes"])
        self.assertIn("release.prd_environment is missing", update["problems"])
        self.assertEqual("invalid", update["status"])

        status, body = self.call("/api/team-update", {})

        self.assertEqual(409, status)
        self.assertIn("cannot be used", body["error"])
        self.assertEqual(
            self.team_text(), (self.config_dir() / "ldp-releaser.team.toml").read_text())

    def test_local_edits_not_uploaded_yet_are_not_undone(self):
        document = self.temp / "op" / "Shared" / "team file"

        self.publish(self.team_text().replace('job_prefix = "streaming-"', ""))

        an_hour_ago = time.time() - 3600

        os.utime(document, (an_hour_ago, an_hour_ago))

        update = self.team_update()

        self.assertEqual(["dataflow.job_prefix"], update["changes"])
        self.assertEqual("local-newer", update["status"])

        status, body = self.call("/api/team-update", {})

        self.assertEqual(409, status)
        self.assertIn("Upload them instead", body["error"])
        self.assertEqual(
            self.team_text(), (self.config_dir() / "ldp-releaser.team.toml").read_text())

    def test_unreachable_1password_shows_nothing(self):
        (self.temp / "op" / "Shared" / "team file").unlink()

        self.assertEqual("unavailable", self.team_update()["status"])


class WithoutItemTest(AppTestCase):

    def test_nothing_is_checked(self):
        self.assertEqual("off", self.call("/api/team-update?check=1")[1]["status"])
        self.assertEqual(409, self.call("/api/team-update", {})[0])
        self.assertFalse((self.temp / "op" / "calls").exists())


class UnresumableStageTest(unittest.TestCase):

    def run_with(self, kind=None, **statuses):
        stages = {name: release.new_stage(None, True) for name in release.STAGE_NAMES}

        for name, status in statuses.items():
            stages[name]["status"] = status

        run = {"finishedAt": None, "stages": stages}

        if kind:
            run["kind"] = kind

        return run

    def test_steps_a_restart_would_stop_halfway(self):
        self.assertIsNone(store.unresumable_stage(self.run_with(asahBuild="running")))
        self.assertIsNone(store.unresumable_stage(self.run_with(developMerge="running")))
        self.assertEqual(
            "developPullRequest",
            store.unresumable_stage(self.run_with(developPullRequest="running")))
        self.assertEqual("jira", store.unresumable_stage(self.run_with(jira="queued")))

        fixing = self.run_with()
        fixing["stages"]["asahBuild"]["fix"] = {"status": "running"}

        self.assertEqual("asahBuild", store.unresumable_stage(fixing))


if __name__ == "__main__":
    unittest.main()
