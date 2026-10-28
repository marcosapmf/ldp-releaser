"""Explains why a Jenkins build failed, from its console log.

The explanation names the pipeline step that failed and the error lines of
the log. When an image scan (twistcli) blocked the build, it also compares
the scan with the job's last successful build: vulnerabilities that were
not there before are usually the ones that started failing the build, even
when the code did not change. The scan reports archived by the build say
which jar each one is in, which is what the fixes are planned from.
"""

import json
import re

from . import fixes

ANNOTATION_PATTERN = re.compile(r"ha:////[A-Za-z0-9+/=]+")
ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ERROR_PATTERNS = [
    re.compile(pattern) for pattern in (
        r"^ERROR:",
        r"^\[ERROR\]",
        r"^FAILURE: ",
        r"BUILD FAILED",
        r"Error response from daemon",
        r"Scan failed due to",
        r"error Command failed",
        r"npm ERR!",
        r"script returned exit code [1-9]",
    )
]
MAX_ERRORS = 20
SEVERITIES = ("critical", "high", "medium", "low")
SKIPPED_PATTERN = re.compile(r'Stage "(.+)" skipped due to earlier failure')
STEP_PATTERN = re.compile(r"^\[Pipeline\] \{ \((.+)\)$")
VULNERABILITY_PATTERN = re.compile(r"^(CVE|GHSA|PRISMA)-")


def clean(text):

    # Jenkins hides console notes (ha:////...) behind escape codes; drop
    # both so only the readable text is left.

    return ANNOTATION_PATTERN.sub("", ANSI_PATTERN.sub("", text))


def error_lines(lines):
    errors = []
    reason = False

    for line in lines:
        stripped = line.strip()

        # Gradle explains the failure on the lines after "What went wrong".

        if reason:
            if stripped and not stripped.startswith("* Try:"):
                errors.append(stripped)

                continue

            reason = False

        if stripped == "* What went wrong:":
            reason = True

            continue

        if any(pattern.search(stripped) for pattern in ERROR_PATTERNS):
            errors.append(stripped)

    unique = list(dict.fromkeys(errors))

    return unique[:MAX_ERRORS]


def explain(jenkins, build_url):
    log = clean(jenkins.get_text(build_url + "consoleText"))
    lines = log.splitlines()
    scans = scan_results(lines)

    failed_scans = [scan for scan in scans if scan["result"] == "FAIL"]

    # Each failed scan already carries its reason.

    errors = [
        line for line in error_lines(lines)
        if not any(line == scan["reason"] for scan in failed_scans)
    ]

    explanation = {
        "comparedWith": None,
        "errors": errors,
        "fixes": [],
        "newVulnerabilities": [],
        "problems": [],
        "scans": failed_scans,
        "step": failed_step(lines),
    }

    if not failed_scans:
        return explanation

    job_url = build_url.rstrip("/").rsplit("/", 1)[0] + "/"
    last_success = jenkins.get_json(
        job_url + "api/json", "lastSuccessfulBuild[number,url]").get("lastSuccessfulBuild")

    found = vulnerabilities(lines)

    if last_success:
        explanation["comparedWith"] = last_success

        before = vulnerabilities(
            clean(jenkins.get_text(last_success["url"] + "consoleText")).splitlines())

        found = {key: value for key, value in found.items() if key not in before}

    reports = scan_reports(jenkins, build_url)

    for item in found.values():
        report = reports.get((item["id"], item["package"], item["version"]))

        item["jars"] = []

        if report:
            item["fix"] = report["fix"]
            item["jars"] = sorted(report["jars"])

    explanation["newVulnerabilities"] = sorted(
        found.values(),
        key=lambda item: (severity_rank(item["severity"]), item["id"], item["package"]))
    explanation["fixes"], explanation["problems"] = fixes.plan(
        explanation["newVulnerabilities"])

    return explanation


def failed_step(lines):

    # The pipeline step that was running when the build failed: the last
    # one started before Jenkins began skipping steps.

    steps = []

    for line in lines:
        skipped = SKIPPED_PATTERN.search(line)

        if skipped:
            if steps and steps[-1] == skipped[1]:
                steps.pop()

            break

        match = STEP_PATTERN.match(line.strip())

        if match and not match[1].startswith("Declarative: "):
            steps.append(match[1])

    if steps:
        return steps[-1]

    return None


def image_name(image):
    name = image.rsplit(":", 1)[-1]

    return re.sub(r"-\d{8}-[0-9a-f]+$", "", name)


def scan_results(lines):
    scans = []

    for line in lines:
        stripped = line.strip()

        match = re.match(r"^Vulnerability threshold check results: (\w+)", stripped)

        if match and scans:
            scans[-1]["result"] = match[1]

        if stripped.startswith("Scan failed due to") and scans:
            scans[-1]["reason"] = stripped

        if "twistcli images scan" in stripped:
            scans.append(
                {"image": image_name(stripped.split()[-1]), "reason": None, "result": None})

    return scans


def scan_reports(jenkins, build_url):

    # The full scan results the build archived, keyed like `vulnerabilities`:
    # the complete fix versions and the jars each vulnerability was found in.

    artifacts = jenkins.get_json(build_url + "api/json", "artifacts[relativePath]")
    reports = {}

    for artifact in artifacts.get("artifacts") or []:
        path = artifact["relativePath"]

        if not re.search(r"(^|/)vulnerabilities_[^/]*\.json$", path):
            continue

        try:
            report = json.loads(jenkins.get_text(build_url + "artifact/" + path))
        except ValueError:
            continue

        for result in report.get("results") or []:
            for vulnerability in result.get("vulnerabilities") or []:
                key = (
                    vulnerability.get("id"), vulnerability.get("packageName"),
                    vulnerability.get("packageVersion"))

                entry = reports.setdefault(
                    key, {"fix": vulnerability.get("status") or "", "jars": set()})

                if vulnerability.get("packagePath"):
                    entry["jars"].add(vulnerability["packagePath"].rsplit("/", 1)[-1])

    return reports


def severity_rank(severity):
    if severity in SEVERITIES:
        return SEVERITIES.index(severity)

    return len(SEVERITIES)


def vulnerabilities(lines):

    # Rows of the twistcli tables, keyed by (id, package, version), with
    # the images each one was found in.

    found = {}
    image = None

    for line in lines:
        stripped = line.strip()

        if "twistcli images scan" in stripped:
            image = image_name(stripped.split()[-1])

            continue

        if not stripped.startswith("|"):
            continue

        cells = [cell.strip() for cell in stripped.strip("|").split("|")]

        if len(cells) < 7 or not VULNERABILITY_PATTERN.match(cells[0]):
            continue

        key = (cells[0], cells[3], cells[4])

        item = found.setdefault(
            key,
            {
                "fix": cells[5],
                "id": cells[0],
                "images": [],
                "package": cells[3],
                "published": cells[6],
                "severity": cells[1],
                "version": cells[4],
            })

        if image and image not in item["images"]:
            item["images"].append(image)

    return found
