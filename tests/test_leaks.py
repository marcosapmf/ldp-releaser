"""The repository must not contain secrets or private settings values."""

import subprocess
import sys
import unittest

from tests.harness import ROOT

sys.path.insert(0, str(ROOT / "scripts"))

import check_leaks  # noqa: E402


class LeakTest(unittest.TestCase):

    def test_repository_has_no_private_data(self):
        completed = subprocess.run(
            [sys.executable, "scripts/check_leaks.py"], capture_output=True,
            cwd=ROOT, text=True)

        self.assertEqual(0, completed.returncode, completed.stdout)

    def test_regions_are_public_but_look_alike_project_ids_are_not(self):

        # Project IDs often look like region names (word-word3), so only
        # real region families are treated as public.

        for value in ("asia-south1-b", "europe-west3", "us-west1"):
            self.assertTrue(check_leaks.REGION_PATTERN.fullmatch(value), value)

        for value in ("example-prd1", "team-staging2", "acme-ac3"):
            self.assertFalse(check_leaks.REGION_PATTERN.fullmatch(value), value)

    def test_values_are_found_case_insensitively_as_whole_words(self):
        values = {"example-prd1", "SECRET_VALUE"}

        self.assertTrue(check_leaks.find_leaks('project = "Example-PRD1"', values))
        self.assertTrue(check_leaks.find_leaks("token=secret_value", values))
        self.assertFalse(check_leaks.find_leaks("def secret_values():", values))


if __name__ == "__main__":
    unittest.main()
