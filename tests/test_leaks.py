"""The repository must not contain secrets or private settings values."""

import subprocess
import sys
import unittest

from tests.harness import ROOT


class LeakTest(unittest.TestCase):

    def test_repository_has_no_private_data(self):
        completed = subprocess.run(
            [sys.executable, "scripts/check_leaks.py"], capture_output=True,
            cwd=ROOT, text=True)

        self.assertEqual(0, completed.returncode, completed.stdout)


if __name__ == "__main__":
    unittest.main()
