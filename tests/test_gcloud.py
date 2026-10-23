"""The team's gcloud configurations are created with each person's login."""

import json
import tomllib
import unittest

from ldp_releaser import config
from tests.harness import TEAM, AppTestCase


class GcloudTest(AppTestCase):

    def before_start(self):

        # Someone who already has a configuration with one of the team's
        # names, pointing to another project, and another one active.

        (self.temp / "gcloud.json").write_text(json.dumps({
            "active": "personal",
            "configurations": {
                "personal": {"core": {"project": "my-sandbox"}},
                "team-prd-2": {"core": {"project": "something-else"}},
            },
        }))

    def gcloud_state(self):
        return json.loads((self.temp / "gcloud.json").read_text())

    def test_missing_configurations_are_created_and_others_left_alone(self):
        state = self.gcloud_state()

        self.assertEqual("personal", state["active"])
        self.assertEqual(
            {
                "compute": {"region": "us-west1", "zone": "us-west1-a"},
                "core": {"account": "tester@example.com", "project": "example-staging"},
            },
            state["configurations"]["team-stg"])
        self.assertEqual(
            {
                "compute": {"region": "europe-west1"},
                "core": {"account": "tester@example.com", "project": "example-prd-1"},
            },
            state["configurations"]["team-prd-1"])
        self.assertEqual(
            {"core": {"project": "something-else"}}, state["configurations"]["team-prd-2"])
        self.assertEqual(
            {"core": {"project": "my-sandbox"}}, state["configurations"]["personal"])

        check = next(
            check for check in self.call("/api/health")[1] if check["name"] == "gcloud")

        self.assertFalse(check["ok"])
        self.assertFalse(check["required"])
        self.assertIn("team-prd-2 points to something-else", check["problem"])

    def test_half_made_configuration_is_removed(self):
        self.stop_app()

        state = self.gcloud_state()
        state["configurations"].pop("team-prd-1")
        (self.temp / "gcloud.json").write_text(json.dumps(state))

        self.forbidden = "example-prd-1"

        self.start_app()

        self.assertNotIn("team-prd-1", self.gcloud_state()["configurations"])

    def test_expired_login_is_reported(self):
        self.stop_app()

        self.expired = True

        self.start_app()

        check = next(
            check for check in self.call("/api/health?refresh=1")[1]
            if check["name"] == "gcloud")

        self.assertFalse(check["ok"])
        self.assertIn("gcloud login has expired", check["problem"])

    def environment(self):
        environment = super().environment()

        if getattr(self, "expired", False):
            environment["FAKE_GCLOUD_EXPIRED"] = "1"

        if getattr(self, "forbidden", None):
            environment["FAKE_GCLOUD_FORBIDDEN_PROJECTS"] = self.forbidden

        return environment

    def test_team_file_mapping_is_validated(self):
        team = tomllib.loads(TEAM.format(url="https://ci.example.com"))

        team["gcloud"]["environments"]["int"] = ["team-uat"]
        team["gcloud"]["configurations"]["Bad Name"] = {"project": "x"}

        problems = config.validate_team(team)

        self.assertTrue(any("invalid configuration name 'Bad Name'" in p for p in problems), problems)

        del team["gcloud"]["configurations"]["Bad Name"]

        problems = config.validate_team(team)

        self.assertEqual(
            ["gcloud.environments.int names unknown configurations: team-uat"], problems)


if __name__ == "__main__":
    unittest.main()
