"""The rollout confirmed automatically through Argo CD."""

import subprocess
import time
import unittest

from tests.harness import AppTestCase, git


class RolloutCheckTest(AppTestCase):

    def setUp(self):
        super().setUp()

        # Let Argo CD confirm; tests click the button only on purpose.

        self.auto_confirm = False

    def keep_tickets(self, run):
        stage = run["stages"]["closeTickets"]

        if stage["status"] == "running" and stage["why"] and not run["ticketsDecision"]:
            self.call(f"/api/runs/{run['id']}/keep-tickets", {})

    def push_stg_suffix(self, suffix):

        # Someone else merges a newer release for STG on top of ours.

        clone = self.temp / "other-clone"

        git("clone", "-q", "-b", "develop", str(self.origin), str(clone))

        (clone / "deploy" / "stg.yaml").write_text(
            f'global:\n  image:\n    tagSuffix: "{suffix}"\n')

        git("-c", "user.name=Other", "-c", "user.email=other@example.com",
            "commit", "-qam", "newer release", cwd=clone)
        git("push", "-q", "origin", "develop", cwd=clone)

    def rollout_log(self, run):
        return run["stages"]["rollout"]["log"]

    def test_stg_rollout_is_confirmed_by_argocd(self):
        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"},
            self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn(
            "team-stg-asah: done, 2 images on 20260101-1234567, Synced, Healthy "
            "(osbasahupgrade succeeded)",
            self.rollout_log(run))
        self.assertIn("rollout confirmed by Argo CD", self.rollout_log(run))
        self.assertFalse(run["rolloutConfirmed"])

    def test_waits_until_argocd_syncs_the_merge(self):
        self.fakes.argocd_apps["team-stg-asah"]["lag"] = 4

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"},
            self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn("Argo CD has not synced the charts merge yet", self.rollout_log(run))
        self.assertIn("rollout confirmed by Argo CD", self.rollout_log(run))

    def test_failed_upgrade_hook_fails_the_rollout(self):
        self.fakes.argocd_apps["team-stg-asah"]["hook"] = "Failed"

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"})

        self.assertEqual("failed", run["status"])
        self.assertEqual("failed", run["stages"]["rollout"]["status"])
        self.assertIn(
            "team-stg-asah: Job osbasahupgrade (PreSync hook) failed: Job has reached the "
            "specified backoff limit",
            run["error"])
        self.assertEqual("skipped", run["stages"]["closeTickets"]["status"])

    def test_unhealthy_pods_are_shown_and_waited_for(self):
        app = self.fakes.argocd_apps["team-stg-asah"]
        app["health"] = "Degraded"

        def recover(run):
            if "CrashLoopBackOff" in run["stages"]["rollout"]["log"]:
                app["health"] = "Healthy"

            self.keep_tickets(run)

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"}, recover)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn(
            "pod team-stg-asah-publisher-1 Degraded (CrashLoopBackOff)", self.rollout_log(run))

    def test_superseded_release_is_done_with_a_warning(self):
        pushed = []

        def supersede(run):
            if run["stages"]["rollout"]["status"] == "running" and not pushed:
                pushed.append(True)

                self.push_stg_suffix("20260202-7777777")

            self.keep_tickets(run)

        self.fakes.argocd_apps["team-stg-asah"]["syncing"] = 10

        run = self.run_release(
            {"releaseEnvironments": ["stg"], "tagSuffix": "20260101-1234567"}, supersede)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn("superseded", self.rollout_log(run))
        self.assertIn("sets 20260202-7777777", self.rollout_log(run))
        self.assertIn("Warning: team-stg-asah was superseded", self.rollout_log(run))

    def test_environments_without_a_token_still_need_the_button(self):
        status, body = self.call(
            "/api/runs", {"releaseEnvironments": ["stg", "int"], "tagSuffix": "20260101-1234567"})
        run_id = body["id"]

        deadline = time.time() + 60

        while time.time() < deadline:
            run = self.run_by_id(run_id)

            if "Checking STG in Argo CD: done" in (run["stages"]["rollout"]["why"] or ""):
                break

            time.sleep(0.1)

        self.assertIn("Check in Argo CD that the asah apps for INT", run["stages"]["rollout"]["why"])

        time.sleep(1)

        self.assertEqual("running", self.run_by_id(run_id)["stages"]["rollout"]["status"])

        self.call(f"/api/runs/{run_id}/confirm-rollout", {})

        run = self.wait(run_id, self.keep_tickets)

        self.assertEqual("success", run["status"], run["error"])
        self.assertIn("rollout confirmed by hand", self.rollout_log(run))

    def test_setup_panel_checks_the_tokens(self):
        check = next(
            check for check in self.call("/api/health?refresh=1")[1] if check["name"] == "Argo CD")

        self.assertTrue(check["ok"], check["problem"])


class RejectedTokenTest(AppTestCase):

    def write_settings(self):
        super().write_settings()

        team = self.temp / "config" / "ldp-releaser" / "ldp-releaser.team.toml"

        team.write_text(team.read_text().replace(
            'stg = "fake-argocd-token"', 'stg = "revoked-token"'))

    def test_a_rejected_token_is_reported(self):
        check = next(
            check for check in self.call("/api/health?refresh=1")[1] if check["name"] == "Argo CD")

        self.assertFalse(check["ok"])
        self.assertIn("the stg token was refused (401)", check["problem"])


if __name__ == "__main__":
    unittest.main()
