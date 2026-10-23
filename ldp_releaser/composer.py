"""The Composer flow: uploads the DAGs of an Asah version to the Composer
environments of the chosen release environments, one gcloud configuration
at a time. It is separate from releases and only runs when started.
"""

import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import uuid
from pathlib import Path

from . import gcloud, health, release

LOGIN_POLL_SECONDS = float(os.environ.get("LDP_LOGIN_POLL_SECONDS", "15"))


def checkout(run, worktree):

    # Fetches the requested version from the team's repository into a
    # temporary worktree, so the person's own checkout is left alone.

    settings = release.settings
    clone = str(Path(settings["asah.checkout"]).expanduser())

    source = matching_remote(clone, settings["asah.repository"])

    if source is None:
        source = f"git@github.com:{settings['asah.repository']}.git"

    fetch = subprocess.run(
        ["git", "-C", clone, "fetch", "--no-tags", source, run["ref"]],
        capture_output=True, text=True, timeout=600)

    release.append_log(run, "checkout", f"$ git fetch {source} {run['ref']}\n{fetch.stderr}")

    target = "FETCH_HEAD"

    if fetch.returncode:

        # A commit that only exists locally (or a short hash) cannot be
        # fetched by name; use it if the clone has it.

        target = run["ref"]

    commit = release.run_command(
        run, "checkout", ["git", "-C", clone, "rev-parse", "--verify", f"{target}^{{commit}}"]
    ).strip()

    release.run_command(
        run, "checkout",
        ["git", "-C", clone, "worktree", "add", "--detach", str(worktree), commit])

    release.update_run(run, commit=commit)

    release.append_log(run, "checkout", f"Using {run['ref']} at {commit[:10]}\n")


def composer_enabled(settings):
    return bool(
        settings["asah.composer_directory"] and settings["asah.composer_script"] and
        settings["gcloud.environments"])


def create_run(environments, ref):
    settings = release.settings

    configurations = [
        name for environment in settings.environments() if environment in environments
        for name in settings["gcloud.environments"].get(environment, [])
    ]

    labels = {"checkout": f"Fetch {ref}", "gcloudLogin": "gcloud login"}
    order = ["gcloudLogin", "checkout"]

    for name in configurations:
        labels[f"dags-{name}"] = f"Upload DAGs to {name}"
        order.append(f"dags-{name}")

    run = {
        "cancelRequested": False,
        "commit": None,
        "configurations": configurations,
        "createdAt": time.time(),
        "environments": environments,
        "error": None,
        "finishedAt": None,
        "id": uuid.uuid4().hex[:8],
        "kind": "composer",
        "ref": ref,
        "stageLabels": labels,
        "stageOrder": order,
        "stages": {name: release.new_stage("gcloud", True) for name in order},
        "status": "running",
    }

    with release.runs_lock:
        release.runs[run["id"]] = run

    release.on_change(run)

    return run


def execute(run):
    worktree = Path(tempfile.mkdtemp(prefix="ldp-releaser-composer-")) / "asah"

    try:
        result = release.run_local_stage(
            run, "gcloudLogin", lambda: wait_for_login(run, "gcloudLogin"))

        # The worktree is temporary, so after a restart the version is
        # fetched again even if it was fetched before.

        if result == "SUCCESS":
            result = release.run_local_stage(run, "checkout", lambda: checkout(run, worktree))

        for name in run["configurations"]:
            stage_name = f"dags-{name}"

            if result != "SUCCESS":
                break

            if release.stage_done(run, stage_name):
                continue

            result = release.run_local_stage(
                run, stage_name, lambda: upload(run, stage_name, name, worktree))

        if result == "SUCCESS":
            release.update_run(run, status="success")
        elif result == "ABORTED":
            release.update_run(run, status="aborted")
        else:
            release.update_run(run, status="failed")
    except Exception as exception:
        release.update_run(run, error=str(exception), status="failed")
    finally:
        remove_worktree(run, worktree)
        release.finish(run)


def matching_remote(clone, repository):
    completed = subprocess.run(
        ["git", "-C", clone, "remote", "-v"], capture_output=True, text=True, timeout=30)

    for line in completed.stdout.splitlines():
        parts = line.split()

        if len(parts) >= 2 and health.origin_matches(parts[1], repository):
            return parts[0]

    return None


def problems(settings):
    if not composer_enabled(settings):
        return []

    checkout_path = settings["asah.checkout"]

    if not checkout_path:
        return ["no Asah clone configured; run ./ldp-releaser --setup"]

    clone = Path(checkout_path).expanduser()

    if not (clone / ".git").exists():
        return [f"{clone} is not a git clone of {settings['asah.repository']}"]

    return []


def remove_worktree(run, worktree):
    checkout_path = release.settings["asah.checkout"]

    if checkout_path and worktree.exists():
        subprocess.run(
            ["git", "-C", str(Path(checkout_path).expanduser()), "worktree", "remove",
             "--force", str(worktree)],
            capture_output=True, text=True, timeout=120)

    shutil.rmtree(worktree.parent, ignore_errors=True)


def stream(run, stage_name, command, cwd, environment):

    # Runs the upload script, showing its output live; Cancel stops it.

    process = subprocess.Popen(
        command, cwd=cwd, env=environment, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, text=True, bufsize=1)

    def stop_on_cancel():
        while process.poll() is None:
            if run["cancelRequested"]:
                process.terminate()

                return

            time.sleep(0.5)

    threading.Thread(daemon=True, target=stop_on_cancel).start()

    for line in process.stdout:
        release.append_log(run, stage_name, line)

    return process.wait()


def upload(run, stage_name, name, worktree):
    settings = release.settings
    configuration = settings["gcloud.configurations"][name]

    directory = worktree / settings["asah.composer_directory"]
    script = directory / settings["asah.composer_script"]

    if not script.exists():
        raise RuntimeError(
            f"{run['ref']} has no {settings['asah.composer_directory']}/"
            f"{settings['asah.composer_script']}")

    environment = {
        **os.environ,
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "LCP_PROJECT_ID": configuration.get("lcp_project_id", name),
        "PROJECT_ID": configuration["project"],
        "REGION": configuration["region"],
    }

    for attempt in (1, 2):
        if wait_for_login(run, stage_name) == "ABORTED":
            return "ABORTED"

        release.append_log(
            run, stage_name,
            f"$ LCP_PROJECT_ID={environment['LCP_PROJECT_ID']} "
            f"PROJECT_ID={environment['PROJECT_ID']} REGION={environment['REGION']} "
            f"{settings['asah.composer_script']}\n")

        code = stream(run, stage_name, ["bash", str(script)], directory, environment)

        if code == 0:
            return None

        if run["cancelRequested"]:
            return "ABORTED"

        # A login that expired during the upload: wait for a new login and
        # upload again; importing the same files twice is harmless.

        if attempt == 1 and gcloud.login_problem():
            release.append_log(
                run, stage_name,
                "[LDP Releaser] The gcloud login expired during the upload; it "
                "starts again once you log in\n")

            continue

        break

    raise RuntimeError(f"Uploading the DAGs to {name} failed (exit code {code})")


def validate(payload):
    settings = release.settings

    if not composer_enabled(settings):
        raise ValueError("The team settings have no Composer setup")

    problem = next(iter(problems(settings)), None)

    if problem:
        raise ValueError(problem)

    available = [
        environment for environment in settings.environments()
        if environment in settings["gcloud.environments"]
    ]

    environments = payload.get("environments") or []

    if not isinstance(environments, list) or not environments or not set(
            environments) <= set(available):

        raise ValueError(f"Pick environments among {', '.join(available)}")

    ref = str(payload.get("ref", "")).strip()

    if not re.fullmatch(r"[\w./-]+", ref):
        raise ValueError("Enter the Asah branch, tag or commit to take the DAGs from")

    for environment in environments:
        for name in settings["gcloud.environments"][environment]:
            if not settings["gcloud.configurations"][name].get("region"):
                raise ValueError(f"gcloud configuration {name} has no region in the team settings")

    return [environment for environment in available if environment in environments], ref


def wait_for_login(run, stage_name):
    problem = gcloud.login_problem()

    if not problem:
        return None

    release.update_stage(
        run, stage_name, needsGcloudLogin=True,
        why=f"{problem[0].upper()}{problem[1:]}. Use the Log in to gcloud button.")

    while gcloud.login_problem():
        if release.sleep_unless_cancelled(run, LOGIN_POLL_SECONDS):
            release.update_stage(run, stage_name, needsGcloudLogin=False)

            return "ABORTED"

    release.update_stage(run, stage_name, needsGcloudLogin=False, why=None)
    release.append_log(run, stage_name, "gcloud login is valid again\n")

    return None
