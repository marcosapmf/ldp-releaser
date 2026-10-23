"""The first-run setup in the terminal."""

import os
import shutil
import stat
import subprocess
import sys
import time
import tomllib
import unittest

from tests.harness import ROOT, TEAM, AppTestCase


class SetupTest(AppTestCase):

    def setUp(self):

        # Start without any settings, like a teammate on their first run.

        super().setUp()

        self.stop_app()

        for path in (self.temp / "config" / "ldp-releaser").iterdir():
            path.unlink()

        (self.temp / "gcloud.json").unlink(missing_ok=True)

    def test_first_run_asks_for_everything(self):
        team_file = self.temp / "shared.team.toml"
        team_file.write_text(TEAM.format(url=self.fakes.url))

        incomplete = self.temp / "incomplete.team.toml"
        incomplete.write_text("[jenkins]\nurl = 'https://ci.example.com'\n")

        answers = "\n".join([
            str(self.temp / "missing.toml"),   # does not exist
            str(incomplete),                   # incomplete
            str(team_file),
            "tester", "wrong-token",           # rejected by Jenkins
            "tester", "secret-token",
            "tester@example.com", "secret-token",
            str(self.temp / "nowhere"),        # not a checkout
            str(self.checkout),
            str(self.temp / "nowhere"),        # not a clone
            str(self.asah_checkout),
            str(self.port),
        ]) + "\n"

        # A PATH with the fakes, git and Python but without the 1Password
        # CLI, so the setup asks for a file path.

        tools = self.temp / "tools"
        tools.mkdir()

        for name in ("git", "python3"):
            (tools / name).symlink_to(shutil.which(name))

        process = subprocess.Popen(
            [sys.executable, "-m", "ldp_releaser"], cwd=ROOT,
            env={**self.environment(),
                 "PATH": f"{ROOT / 'tests' / 'bin'}{os.pathsep}{tools}"},
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True)

        process.stdin.write(answers)
        process.stdin.close()

        output = ""
        deadline = time.time() + 30

        while "running on" not in output and time.time() < deadline:
            line = process.stdout.readline()

            if not line:
                break

            output += line

        process.terminate()
        process.wait(timeout=10)
        process.stdout.close()

        self.assertIn("does not exist", output)
        self.assertIn("jenkins.folder is missing", output)
        self.assertIn("Jenkins rejected that user and token", output)
        self.assertIn("Jira: logged in as Test User", output)
        self.assertIn("is not a git checkout", output)
        self.assertIn("is not a git clone", output)
        self.assertIn("running on", output)
        self.assertIn("team-stg: created for example-staging", output)

        directory = self.temp / "config" / "ldp-releaser"

        for name in ("config.toml", "ldp-releaser.team.toml"):
            mode = stat.S_IMODE((directory / name).stat().st_mode)

            self.assertEqual(0o600, mode, name)

        with (directory / "config.toml").open("rb") as file:
            personal = tomllib.load(file)

        self.assertEqual("tester", personal["jenkins"]["user"])
        self.assertEqual("secret-token", personal["jenkins"]["token"])
        self.assertEqual(str(self.checkout), personal["charts"]["checkout"])
        self.assertEqual(str(self.asah_checkout), personal["asah"]["checkout"])


if __name__ == "__main__":
    unittest.main()
