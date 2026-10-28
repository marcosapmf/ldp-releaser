"""Explaining a failed Jenkins build from its log, and fixing it."""

import time
import unittest
import zipfile

from tests.fakes import git_output
from tests.harness import AppTestCase, git

BUILD_GRADLE = """dependencies {{
\tapi group: "com.example", name: "new-lib", version: "{declared}"
}}

configurations.all {{
\texclude group: "org.example", module: "zzz-lib"

\tresolutionStrategy {{
\t\tforce group: "com.example", name: "new-lib", version: "{forced}"
\t}}
}}
"""


class FailureTest(AppTestCase):

    def explain(self, run, stage_name="asahBuild"):
        return self.call(f"/api/runs/{run['id']}/stages/{stage_name}/failure", {})

    def test_blocking_scan_is_compared_with_the_last_successful_build(self):
        self.run_release({"asahBranch": "main", "mode": "asah"})

        run = self.run_release({"asahBranch": "vulnerable", "mode": "asah"})

        self.assertEqual("failed", run["stages"]["asahBuild"]["status"])

        status, explanation = self.explain(run)

        self.assertEqual(200, status, explanation)
        self.assertEqual(1, explanation["comparedWith"]["number"])
        self.assertEqual(["ERROR: The build failed."], explanation["errors"])
        self.assertEqual(
            [
                {
                    "fix": "fixed in 2.1, 1.9.5, 1.2.9",
                    "id": "CVE-2026-0002",
                    "images": ["backend", "publisher"],
                    "jars": ["new-lib-2.0.jar"],
                    "package": "com.example_new-lib",
                    "published": "1 days",
                    "severity": "critical",
                    "version": "2.0",
                },
            ],
            [item for item in explanation["newVulnerabilities"] if item["version"] == "2.0"])
        self.assertEqual(
            ["backend", "publisher"], [scan["image"] for scan in explanation["scans"]])
        self.assertEqual("Image Scan", explanation["step"])
        self.assertEqual(
            explanation, self.run_by_id(run["id"])["stages"]["asahBuild"]["failure"])

    def test_compile_failure_shows_the_gradle_reason(self):
        run = self.run_release({"asahBranch": "broken", "mode": "asah"})

        status, explanation = self.explain(run)

        self.assertEqual(200, status, explanation)
        self.assertIsNone(explanation["comparedWith"])
        self.assertEqual(
            ["Execution failed for task ':compileJava'.", "ERROR: The build failed."],
            explanation["errors"])
        self.assertEqual([], explanation["scans"])
        self.assertEqual("Compile", explanation["step"])

    def test_only_failed_jenkins_steps_are_explained(self):
        run = self.run_release({"asahBranch": "main", "mode": "asah"})

        self.assertEqual(409, self.explain(run)[0])
        self.assertEqual(404, self.explain(run, "unknown")[0])


class FixTest(AppTestCase):

    def setUp(self):
        super().setUp()

        # The team's main branch forces the vulnerable version; the released
        # branch already has the fixed one. The person's fork is a remote of
        # their clone, and Gradle cached the two libraries that bundle a copy.

        clone = self.temp / "asah-setup"

        git("clone", "-q", str(self.asah_origin), str(clone))

        (clone / "common").mkdir()

        for branch, declared, forced in (("main", "1.0", "2.0"), ("vulnerable", "2.1", "2.1")):
            git("checkout", "-q", "-B", branch, "origin/main", cwd=clone)

            (clone / "common" / "build.gradle").write_text(
                BUILD_GRADLE.format(declared=declared, forced=forced))

            git("add", "-A", cwd=clone)
            git("-c", "user.name=Test", "-c", "user.email=test@example.com",
                "commit", "-qm", "build", cwd=clone)
            git("push", "-q", "origin", branch, cwd=clone)

        self.fork = self.temp / "tester" / "asah.git"

        git("init", "-q", "--bare", str(self.fork))
        git("remote", "add", "fork", str(self.fork), cwd=self.asah_checkout)

        cache = self.asah_checkout / ".gradle" / "caches" / "modules-2" / "files-2.1"

        for group, name, processor in (
                ("com.example.tools", "processor-lib", True),
                ("com.example", "runtime-lib", False)):

            version = "1.0" if processor else "3.0"
            jar = cache / group / name / version / "0abc" / f"{name}-{version}.jar"

            jar.parent.mkdir(parents=True)

            with zipfile.ZipFile(jar, "w") as archive:
                archive.writestr("com/example/Bundled.class", "")

                if processor:
                    archive.writestr(
                        "META-INF/services/javax.annotation.processing.Processor",
                        "com.example.Processor\n")

    def fork_file(self, branch):
        return git_output(self.fork, "show", f"{branch}:common/build.gradle") + "\n"

    def fork_log(self, branch, subtask):

        # The fix commits on the pushed branch, oldest first.

        subjects = git_output(self.fork, "log", "--format=%s", branch).splitlines()

        return [subject for subject in reversed(subjects) if subject.startswith(subtask)]

    def wait_for_fix(self, run_id):
        deadline = time.time() + 60

        while time.time() < deadline:
            fix = self.run_by_id(run_id)["stages"]["asahBuild"].get("fix")

            if fix and fix["status"] != "running":
                return fix

            time.sleep(0.1)

        self.fail("the fix did not finish")

    def test_fixes_are_committed_one_by_one_and_sent_to_both_branches(self):
        self.run_release({"asahBranch": "main", "mode": "asah"})

        run = self.run_release(
            {"asahBranch": "vulnerable", "mode": "asah", "releaseEnvironments": ["stg"]})

        status, explanation = self.call(f"/api/runs/{run['id']}/stages/asahBuild/failure", {})

        self.assertEqual(200, status, explanation)
        self.assertEqual(
            [
                {"cves": ["CVE-2026-0002"], "group": "com.example", "kind": "update",
                 "name": "new-lib", "version": "2.1"},
                {"bundles": "new-lib 1.5", "cves": ["CVE-2026-0002"],
                 "group": "com.example.tools", "kind": "exclude", "name": "processor-lib"},
            ],
            explanation["fixes"])
        self.assertEqual(
            ["runtime-lib-3.0.jar bundles new-lib 1.4 (CVE-2026-0002) and is needed at "
             "runtime: it needs a release of runtime-lib with a fixed new-lib, or a scan "
             "exception"],
            explanation["problems"])

        self.assertEqual(202, self.call(f"/api/runs/{run['id']}/stages/asahBuild/fix", {})[0])

        fix = self.wait_for_fix(run["id"])

        self.assertEqual("success", fix["status"], fix["log"])

        # The release Task was created for the fix, and the subtask is not
        # mistaken for the release's Technical Task when the Task is reused.

        tasks = [key for key, issue in self.fakes.issues.items() if issue["type"] == "Task"]
        subtask = fix["subtask"]

        self.assertEqual(["Release STG Environment"], [
            self.fakes.issues[key]["summary"] for key in tasks])
        self.assertEqual(tasks[0], self.fakes.issues[subtask]["parent"])
        self.assertEqual("Private", self.fakes.issues[subtask]["security"])
        self.assertEqual("In Progress", self.fakes.issues[subtask]["status"])
        self.assertEqual(
            "Fix CVE-2026-0002 found by the image scan", self.fakes.issues[subtask]["summary"])
        self.assertEqual(
            [[]], [ticket["technicalTasks"] for ticket in self.call("/api/release-tickets")[1]])

        # The main branch gets both fixes, one commit each; the released
        # branch only needs the exclusion.

        self.assertEqual(
            [f"{subtask} Update new-lib to 2.1",
             f"{subtask} Exclude the processor-lib annotation processor from the runtime"],
            self.fork_log(f"{subtask}-main", subtask))
        self.assertEqual(
            BUILD_GRADLE.format(declared="2.1", forced="2.1").replace(
                '\texclude group: "org.example"',
                '\texclude group: "com.example.tools", module: "processor-lib"\n'
                '\texclude group: "org.example"'),
            self.fork_file(f"{subtask}-main"))
        self.assertEqual(
            [f"{subtask} Exclude the processor-lib annotation processor from the runtime"],
            self.fork_log(f"{subtask}-vulnerable", subtask))

        pulls = self.gh_state_data()["pulls"]

        self.assertEqual(
            [("main", f"tester:{subtask}-main", f"{subtask} | main"),
             ("vulnerable", f"tester:{subtask}-vulnerable", f"{subtask} | vulnerable")],
            [(pull["base"], pull["head"], pull["title"]) for pull in pulls.values()])
        self.assertIn(
            "- runtime-lib-3.0.jar bundles new-lib 1.4 and is needed at runtime",
            pulls["1"]["body"])
        self.assertNotIn("CVE", pulls["1"]["body"] + pulls["2"]["body"])
        self.assertIn(f"/browse/{subtask}", pulls["1"]["body"])
        self.assertEqual(
            ["Pull request to main", "Pull request to vulnerable"],
            [link["label"] for link in fix["links"] if link["label"].startswith("Pull")])

        # The person's clone stays on its branch, with its uncommitted work.

        self.assertEqual(
            "my-feature", git_output(self.asah_checkout / ".git", "branch", "--show-current"))
        self.assertTrue((self.asah_checkout / "work-in-progress.txt").exists())
        self.assertEqual(
            409, self.call(f"/api/runs/{run['id']}/stages/asahBuild/fix", {})[0])

    def test_cves_stay_out_of_jira_when_the_ticket_cannot_be_private(self):

        # Jira keeps the subtask at its parent's level, which is public.

        self.fakes.inherit_security = True

        run = self.run_release(
            {"asahBranch": "vulnerable", "mode": "asah", "releaseEnvironments": ["stg"]})

        self.call(f"/api/runs/{run['id']}/stages/asahBuild/failure", {})
        self.call(f"/api/runs/{run['id']}/stages/asahBuild/fix", {})

        fix = self.wait_for_fix(run["id"])
        subtask = fix["subtask"]

        self.assertEqual("failed", fix["status"])
        self.assertEqual(
            f"Jira did not make {subtask} Private (it is public), so its title does not "
            f"name the vulnerabilities. Make {subtask} Private by hand, then try again",
            fix["error"])
        self.assertNotIn(
            "CVE", " ".join(issue["summary"] for issue in self.fakes.issues.values()))
        self.assertEqual({}, self.gh_state_data()["pulls"])


if __name__ == "__main__":
    unittest.main()
