"""Checks that everything a release needs is set up on this machine."""

import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error

from . import composer, gcloud

CACHE_SECONDS = 60

cache = {"checkedAt": 0, "checks": []}
cache_lock = threading.Lock()


def check_charts(settings):
    checkout = settings["charts.checkout"]

    completed = subprocess.run(
        ["git", "-C", checkout, "remote", "get-url", "origin"],
        capture_output=True, text=True, timeout=30)

    if completed.returncode:
        return f"{checkout} is not a git checkout with an origin remote"

    if not origin_matches(completed.stdout.strip(), settings["charts.repository"]):
        return (
            f"origin of {checkout} is {completed.stdout.strip()}, not "
            f"{settings['charts.repository']}")

    return None


def check_github(settings):
    if not shutil.which("gh"):
        return "the gh CLI is not installed"

    completed = subprocess.run(
        ["gh", "api", f"repos/{settings['charts.repository']}",
         "--jq", ".permissions.push"],
        capture_output=True, text=True, timeout=60)

    if completed.returncode:
        return (
            f"gh cannot read {settings['charts.repository']}; run `gh auth "
            f"login` ({completed.stderr.strip()[:200]})")

    if completed.stdout.strip() != "true":
        return f"your GitHub account cannot push to {settings['charts.repository']}"

    return None


def check_git():
    if not shutil.which("git"):
        return "git is not installed"

    completed = subprocess.run(
        ["git", "--version"], capture_output=True, text=True, timeout=30)

    match = re.search(r"(\d+)\.(\d+)", completed.stdout)

    if not match or (int(match.group(1)), int(match.group(2))) < (2, 25):
        return f"git 2.25 or newer is needed ({completed.stdout.strip()})"

    return None


def check_jenkins(jenkins):
    try:
        jenkins.check()
    except urllib.error.HTTPError as exception:
        return f"Jenkins rejected the login ({exception.code})"
    except (OSError, urllib.error.URLError) as exception:
        return f"Jenkins is not reachable ({exception})"

    return None


def check_jira(jira):
    try:
        jira.check()
    except (OSError, RuntimeError, urllib.error.URLError) as exception:
        return f"Jira rejected the login or is not reachable ({str(exception)[:200]})"

    return None


def check_slack(settings):
    if not settings["slack.webhook_url"]:
        return "no Slack webhook in the team settings, so nobody is notified"

    return None


def checks(jenkins, jira, settings, refresh=False):
    with cache_lock:
        if not refresh and time.time() - cache["checkedAt"] < CACHE_SECONDS:
            return cache["checks"]

    results = [
        result("Jenkins", check_jenkins(jenkins), required=True),
        result("Jira", check_jira(jira), required=True),
        result("GitHub", check_github(settings), required=True),
        result("Charts checkout", check_charts(settings), required=True),
        result("git", check_git(), required=True),
        result("Python", check_python(), required=True),
        result("Slack", check_slack(settings), required=False),
    ]

    if composer.composer_enabled(settings):
        results.append(
            result("Asah clone", "; ".join(composer.problems(settings)) or None,
                   required=False))

    if gcloud.configured(settings):
        results.append(
            result("gcloud", "; ".join(gcloud.problems(settings)) or None,
                   required=False))

    with cache_lock:
        cache["checkedAt"] = time.time()
        cache["checks"] = results

    return results


def check_python():
    if sys.version_info < (3, 11):
        return f"Python 3.11 or newer is needed ({sys.version.split()[0]})"

    return None


def origin_matches(url, repository):

    # Matches git@github.com:owner/name(.git) and https://github.com/owner/name(.git).

    match = re.search(r"[:/]([\w.-]+/[\w.-]+?)(?:\.git)?/?$", url)

    return bool(match) and match.group(1).lower() == repository.lower()


def result(name, problem, required):
    return {
        "name": name,
        "ok": problem is None,
        "problem": problem,
        "required": required,
    }
