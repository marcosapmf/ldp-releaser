"""Creates the team's gcloud configurations on this machine.

The team file maps configuration names to projects, regions and zones; each
person's own gcloud login is used as the account. Existing configurations
are never changed, and the active configuration is never switched.
"""

import json
import shutil
import subprocess


def active_account():
    completed = run(
        ["gcloud", "auth", "list", "--filter=status:ACTIVE", "--format=value(account)"])

    if completed.returncode:
        return None

    return completed.stdout.strip() or None


def configured(settings):
    return bool(settings["gcloud.configurations"])


def describe(name):
    completed = run(
        ["gcloud", "config", "configurations", "describe", name, "--format=json"])

    if completed.returncode:
        return None

    return json.loads(completed.stdout)


def problems(settings):

    # What is missing for the gcloud steps, without calling Google Cloud: only
    # the local CLI, login and configurations are checked.

    if not configured(settings):
        return []

    if not shutil.which("gcloud"):
        return ["the gcloud CLI is not installed"]

    if not active_account():
        return ["no gcloud login; run `gcloud auth login`"]

    found = []

    for name, wanted in settings["gcloud.configurations"].items():
        problem = mismatch(name, wanted, describe(name))

        if problem:
            found.append(problem)

    return found


def mismatch(name, wanted, configuration):
    if configuration is None:
        return f"gcloud configuration {name} is missing; restart LDP Releaser to create it"

    project = configuration.get("properties", {}).get("core", {}).get("project")

    if project != wanted["project"]:
        return (
            f"gcloud configuration {name} points to {project}, not "
            f"{wanted['project']}; rename or fix it by hand")

    return None


def run(command):
    return subprocess.run(command, capture_output=True, text=True, timeout=60)


def sync(settings):

    # Creates the configurations that do not exist yet and returns one line
    # per configuration describing what happened.

    if not configured(settings):
        return []

    if not shutil.which("gcloud"):
        return ["gcloud is not installed, so no configurations were created"]

    account = active_account()

    if not account:
        return ["no gcloud login, so no configurations were created; run `gcloud auth login`"]

    report = []

    for name, wanted in settings["gcloud.configurations"].items():
        configuration = describe(name)

        if configuration is not None:
            problem = mismatch(name, wanted, configuration)

            report.append(problem or f"{name}: already set up")

            continue

        create = run(["gcloud", "config", "configurations", "create", name, "--no-activate"])

        if create.returncode:
            report.append(f"{name}: could not be created ({create.stderr.strip()[:200]})")

            continue

        values = {
            "account": account,
            "compute/region": wanted.get("region"),
            "compute/zone": wanted.get("zone"),
            "project": wanted["project"],
        }

        failure = None

        for key, value in values.items():
            if not value:
                continue

            completed = run(
                ["gcloud", "config", "set", key, value, f"--configuration={name}"])

            if completed.returncode:
                failure = f"setting {key} failed ({completed.stderr.strip()[:200]})"

                break

        # A configuration without its project is worse than none, so a
        # half-made one is removed again.

        if failure:
            run(["gcloud", "config", "configurations", "delete", name, "--quiet"])

            report.append(f"{name}: not created, {failure}")

            continue

        report.append(f"{name}: created for {wanted['project']}")

    return report
