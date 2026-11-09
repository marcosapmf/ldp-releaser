"""The Dataflow flow: builds the pipelines of an Asah version once, publishes
the chosen Flex Templates to the gcloud configurations of the chosen
environments, and updates the running streaming jobs to the new template.

It runs on its own from the Dataflow tab, or as an optional release step
after the Composer DAGs. Template versions use the Asah tag suffix format,
so the images match the release they come from.

Dataflow refuses an in-place update when the pipeline changed too much. The
old job keeps running then, and the step waits for a person to choose
"Drain and relaunch", because draining stops the ingestion for a while.
"""

import json
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path

from . import composer, gcloud, release

COMPATIBILITY_PATTERN = re.compile(r"compatib", re.IGNORECASE)
FINISHED_STATES = {"Cancelled", "Done", "Drained", "Failed", "Stopped", "Updated"}
JOB_POLL_SECONDS = float(os.environ.get("LDP_DATAFLOW_POLL_SECONDS", "30"))
JOB_TIMEOUT_SECONDS = float(os.environ.get("LDP_DATAFLOW_TIMEOUT_SECONDS", "1800"))


def active_job(run, stage_name, name, job):
    running = [
        item for item in jobs(run, stage_name, name, job)
        if item["state"] not in FINISHED_STATES
    ]

    if running:
        return running[0]

    return None


def build(run, stage_name, worktree):
    settings = release.settings
    directory = module_directory(run, worktree)
    command = settings["dataflow.build_command"]

    release.append_log(run, stage_name, f"$ {' '.join(command)}\n")

    code = composer.stream(run, stage_name, command, directory, dict(os.environ))

    if run["cancelRequested"]:
        return "ABORTED"

    if code:
        raise RuntimeError(f"The Dataflow build failed (exit code {code})")

    return None


def commit_version(commit):

    # The tag suffix format, from the commit: its date, then its short hash.

    settings = release.settings
    clone = str(Path(settings["asah.checkout"]).expanduser())

    date = subprocess.run(
        ["git", "-C", clone, "show", "-s", "--format=%cd", "--date=format-local:%Y%m%d",
         commit],
        capture_output=True, check=True, env={**os.environ, "TZ": "UTC"}, text=True,
        timeout=30).stdout.strip()

    return f"{date}-{commit[:7]}"


def configurations_for(environments):
    return composer.configurations_for(environments)


def create_run(environments, ref, templates):
    configurations = configurations_for(environments)

    labels = {
        "build": "Build the pipelines", "checkout": f"Fetch {ref}", "gcloudLogin": "gcloud login",
    }
    order = ["gcloudLogin", "checkout", "build"]

    for name in configurations:
        labels[f"dataflow-{name}"] = f"Deploy to {name}"
        order.append(f"dataflow-{name}")

    run = {
        "cancelRequested": False,
        "commit": None,
        "configurations": configurations,
        "createdAt": time.time(),
        "dataflowDone": [],
        "dataflowRelaunch": None,
        "dataflowTemplates": templates,
        "dataflowVersion": None,
        "environments": environments,
        "error": None,
        "finishedAt": None,
        "id": uuid.uuid4().hex[:8],
        "kind": "dataflow",
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


def deploy(run, stage_name, name, worktree):

    # Publishes each template to one configuration, then updates its
    # streaming jobs. Whatever was done before a restart is not repeated.

    settings = release.settings

    for template in run["dataflowTemplates"]:
        key = f"{name}:{template}"

        if key in run["dataflowDone"]:
            continue

        if publish(run, stage_name, name, template, worktree) == "ABORTED":
            return "ABORTED"

        mark_done(run, key)

    if settings["dataflow.streaming_template"] not in run["dataflowTemplates"]:
        return None

    for dxp_project in settings["dataflow.configurations"][name]["dxp_projects"]:
        key = f"{name}:job:{dxp_project}"

        if key in run["dataflowDone"]:
            continue

        if roll_out(run, stage_name, name, dxp_project, worktree) == "ABORTED":
            return "ABORTED"

        mark_done(run, key)

    return None


def drain(run, stage_name, name, job, job_id):
    for item in jobs(run, stage_name, name, job):
        if item["id"] == job_id and item["state"] in FINISHED_STATES:
            return None

    release.append_log(run, stage_name, f"Draining {job} ({job_id})\n")

    gcloud_json(run, stage_name, name, ["dataflow", "jobs", "drain", job_id], parse=False)

    deadline = time.time() + JOB_TIMEOUT_SECONDS

    while time.time() < deadline:
        state = next(
            (item["state"] for item in jobs(run, stage_name, name, job) if item["id"] == job_id),
            None)

        if state in FINISHED_STATES:
            release.append_log(run, stage_name, f"{job} ({job_id}) is {state}\n")

            return None

        if release.sleep_unless_cancelled(run, JOB_POLL_SECONDS):
            return "ABORTED"

    raise RuntimeError(f"{job} ({job_id}) did not finish draining in time; check the Dataflow console")


def enabled(settings):
    return bool(
        settings["dataflow.directory"] and settings["dataflow.build_command"] and
        settings["dataflow.publish_script"] and settings["dataflow.templates"] and
        settings["gcloud.environments"])


def execute(run):
    worktree = Path(tempfile.mkdtemp(prefix="ldp-releaser-dataflow-")) / "asah"

    try:
        result = release.run_local_stage(
            run, "gcloudLogin", lambda: composer.wait_for_login(run, "gcloudLogin"))

        # The worktree is temporary, so after a restart the version is
        # fetched and built again even if it was before.

        if result == "SUCCESS":
            result = release.run_local_stage(run, "checkout", lambda: fetch(run, worktree))

        if result == "SUCCESS":
            result = release.run_local_stage(run, "build", lambda: build(run, "build", worktree))

        for name in run["configurations"]:
            stage_name = f"dataflow-{name}"

            if result != "SUCCESS":
                break

            if release.stage_done(run, stage_name):
                continue

            composer.activate_configuration(run, stage_name, name)

            result = release.run_local_stage(
                run, stage_name, lambda: deploy(run, stage_name, name, worktree))

        if result == "SUCCESS":
            release.update_run(run, status="success")
        elif result == "ABORTED":
            release.update_run(run, status="aborted")
        else:
            release.update_run(run, status="failed")
    except Exception as exception:
        release.update_run(run, error=str(exception), status="failed")
    finally:
        composer.remove_worktree(run, worktree)
        release.finish(run)


def fetch(run, worktree):
    commit = composer.checkout(run, "checkout", run["ref"], worktree)

    version = commit_version(commit)

    release.update_run(run, commit=commit, dataflowVersion=version)
    release.append_log(run, "checkout", f"Template version {version}\n")


def gcloud_environment(name):
    configuration = release.settings["gcloud.configurations"][name]

    # The scripts read the project and region from gcloud's properties;
    # these variables set them for one command, so the active
    # configuration is neither used nor changed.

    return {
        **os.environ,
        "CLOUDSDK_COMPUTE_REGION": configuration["region"],
        "CLOUDSDK_CORE_DISABLE_PROMPTS": "1",
        "CLOUDSDK_CORE_PROJECT": configuration["project"],
    }


def gcloud_json(run, stage_name, name, args, parse=True):
    configuration = release.settings["gcloud.configurations"][name]
    command = [
        "gcloud", *args, f"--project={configuration['project']}",
        f"--region={configuration['region']}",
    ]

    if parse:
        command.append("--format=json")

    for attempt in (1, 2):
        completed = subprocess.run(
            command, capture_output=True, env=gcloud_environment(name), text=True,
            timeout=300)

        if not completed.returncode:
            if parse:
                return json.loads(completed.stdout or "[]")

            release.append_log(run, stage_name, completed.stdout + completed.stderr)

            return None

        if attempt == 1 and gcloud.login_problem():
            if composer.wait_for_login(run, stage_name) == "ABORTED":
                raise RuntimeError("Cancelled while waiting for the gcloud login")

            continue

        break

    release.append_log(run, stage_name, completed.stderr)

    raise RuntimeError(f"gcloud {' '.join(args[:3])} failed in {name} (exit code {completed.returncode})")


def jobs(run, stage_name, name, job):

    # Every job with that name, newest first: an update or a relaunch
    # creates a new job with the same name.

    found = gcloud_json(
        run, stage_name, name,
        ["dataflow", "jobs", "list", "--status=all", f"--filter=name={job}"])

    matching = [item for item in found if item.get("name") == job]

    return sorted(matching, key=lambda item: item.get("creationTime", ""), reverse=True)


def mark_done(run, key):
    release.update_run(run, dataflowDone=[*run["dataflowDone"], key])


def module_directory(run, worktree):
    settings = release.settings
    directory = worktree / settings["dataflow.directory"]

    if not directory.is_dir():
        raise RuntimeError(f"{run.get('ref') or 'The released commit'} has no {settings['dataflow.directory']}")

    return directory


def problems(settings):
    if not enabled(settings):
        return []

    return composer.problems(settings)


def publish(run, stage_name, name, template, worktree):
    settings = release.settings

    release.append_log(run, stage_name, f"== {name}: {template} {run['dataflowVersion']}\n")

    code, _ = run_script(
        run, stage_name, name, settings["dataflow.publish_script"],
        [template, run["dataflowVersion"]], worktree)

    if code == "ABORTED":
        return "ABORTED"

    if code:
        raise RuntimeError(f"Publishing {template} to {name} failed (exit code {code})")

    return None


def release_step(run):

    # Dataflow as part of a release, after the Composer DAGs: the login, the
    # commit that was released, the build, then every configuration of the
    # released environments, with the release's tag suffix as the version.

    if composer.wait_for_login(run, "dataflow") == "ABORTED":
        return "ABORTED"

    ref = run["asahCommit"]

    if not ref:
        ref = run["tagSuffix"].rsplit("-", 1)[-1]

    worktree = Path(tempfile.mkdtemp(prefix="ldp-releaser-dataflow-")) / "asah"

    try:
        commit = composer.checkout(run, "dataflow", ref, worktree)

        release.update_run(run, dataflowCommit=commit, dataflowVersion=run["tagSuffix"])

        if build(run, "dataflow", worktree) == "ABORTED":
            return "ABORTED"

        for name in configurations_for(run["releaseEnvironments"]):
            composer.activate_configuration(run, "dataflow", name)

            if deploy(run, "dataflow", name, worktree) == "ABORTED":
                return "ABORTED"
    finally:
        composer.remove_worktree(run, worktree)

    return None


def relaunch(run, stage_name, name, dxp_project, worktree):
    settings = release.settings
    job = settings["dataflow.job_prefix"] + dxp_project
    pending = run["dataflowRelaunch"]

    if drain(run, stage_name, name, job, pending["jobId"]) == "ABORTED":
        return "ABORTED"

    known = {item["id"] for item in jobs(run, stage_name, name, job)}

    release.append_log(run, stage_name, f"Relaunching {job}\n")

    code, _ = run_script(
        run, stage_name, name, settings["dataflow.streaming_script"],
        streaming_arguments(name, dxp_project), worktree)

    if code == "ABORTED":
        return "ABORTED"

    if code:
        raise RuntimeError(
            f"Relaunching {job} in {name} failed (exit code {code}); it is drained, so "
            f"no events are processed until it is started again")

    state, job_id = wait_for_new_job(run, stage_name, name, job, known)

    if state == "ABORTED":
        return "ABORTED"

    if state != "Running":
        raise RuntimeError(
            f"The relaunched {job} ({job_id}) in {name} is {state}; it is drained, so no "
            f"events are processed until it is started again")

    release.append_log(run, stage_name, f"{job} ({job_id}) is Running\n")
    release.update_run(run, dataflowRelaunch=None)

    return None


def roll_out(run, stage_name, name, dxp_project, worktree):
    settings = release.settings
    job = settings["dataflow.job_prefix"] + dxp_project
    key = f"{name}:job:{dxp_project}"
    pending = run.get("dataflowRelaunch")

    # A refused update waiting for a decision when the server stopped
    # waits again instead of updating again.

    if not pending or pending["key"] != key:
        current = active_job(run, stage_name, name, job)

        if current is None:
            raise RuntimeError(
                f"{job} is not running in {name}, so there is nothing to update; start it "
                f"by hand, then try again")

        known = {item["id"] for item in jobs(run, stage_name, name, job)}

        release.append_log(run, stage_name, f"Updating {job} ({current['id']})\n")

        code, output = run_script(
            run, stage_name, name, settings["dataflow.streaming_script"],
            ["--update", *streaming_arguments(name, dxp_project)], worktree)

        if code == "ABORTED":
            return "ABORTED"

        if code == 0:
            state, job_id = wait_for_new_job(run, stage_name, name, job, known)

            if state == "ABORTED":
                return "ABORTED"

            if state == "Running":
                release.append_log(run, stage_name, f"{job} ({job_id}) is Running\n")

                return None

            reason = f"the updated job {job_id} is {state}"
        elif COMPATIBILITY_PATTERN.search(output):
            reason = "Dataflow refused the update"
        else:
            raise RuntimeError(f"Updating {job} in {name} failed (exit code {code})")

        # Only offer to drain a job that still runs: otherwise nothing is
        # processing the events and a person has to look.

        if active_job(run, stage_name, name, job) is None:
            raise RuntimeError(
                f"Updating {job} in {name} failed ({reason}) and no {job} is running "
                f"anymore; check the Dataflow console")

        configuration = settings["gcloud.configurations"][name]

        pending = {
            "approved": False,
            "configuration": name,
            "job": job,
            "jobId": current["id"],
            "key": key,
            "reason": reason,
        }

        release.update_run(run, dataflowRelaunch=pending)
        release.add_stage_link(
            run, stage_name, f"{job} in the Dataflow console",
            f"https://console.cloud.google.com/dataflow/jobs/{configuration['region']}/"
            f"{current['id']}?project={configuration['project']}")

    if wait_for_relaunch(run, stage_name) == "ABORTED":
        return "ABORTED"

    return relaunch(run, stage_name, name, dxp_project, worktree)


def run_script(run, stage_name, name, script, arguments, worktree):

    # Runs one of the module's scripts against a configuration, showing its
    # output live. A login that expired meanwhile is waited for and the
    # script runs again: publishing or updating twice is harmless.

    directory = module_directory(run, worktree)
    path = directory / script

    if not path.exists():
        raise RuntimeError(f"The Asah version has no {release.settings['dataflow.directory']}/{script}")

    for attempt in (1, 2):
        if composer.wait_for_login(run, stage_name) == "ABORTED":
            return "ABORTED", ""

        release.append_log(run, stage_name, f"$ {script} {' '.join(arguments)}\n")

        start = len(run["stages"][stage_name]["log"])
        code = composer.stream(
            run, stage_name, ["bash", str(path), *arguments], directory,
            gcloud_environment(name))
        output = run["stages"][stage_name]["log"][start:]

        if code == 0:
            return 0, output

        if run["cancelRequested"]:
            return "ABORTED", output

        if attempt == 1 and gcloud.login_problem():
            release.append_log(
                run, stage_name,
                "[LDP Releaser] The gcloud login expired; the script runs again once you "
                "log in\n")

            continue

        return code, output


def streaming_arguments(name, dxp_project):
    configuration = release.settings["dataflow.configurations"][name]

    return [dxp_project, configuration["network"], configuration["subnetwork"]]


def validate(payload):
    settings = release.settings

    if not enabled(settings):
        raise ValueError("The team settings have no Dataflow setup")

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
        raise ValueError("Enter the Asah branch, tag or commit to build the pipelines from")

    environments = [environment for environment in available if environment in environments]

    return environments, ref, validate_templates(environments, payload.get("templates"))


def validate_templates(environments, templates):
    settings = release.settings

    if not isinstance(templates, list) or not templates or not set(templates) <= set(
            settings["dataflow.templates"]):

        raise ValueError(f"Pick templates among {', '.join(settings['dataflow.templates'])}")

    missing = [
        environment for environment in environments
        if environment not in (settings["gcloud.environments"] or {})
    ]

    if missing:
        raise ValueError(
            f"No gcloud configurations for {', '.join(missing)} in the team settings")

    if settings["dataflow.streaming_template"] in templates:
        unknown = [
            name for name in configurations_for(environments)
            if name not in (settings["dataflow.configurations"] or {})
        ]

        if unknown:
            raise ValueError(
                f"The team settings have no Dataflow jobs for {', '.join(unknown)} "
                f"(dataflow.configurations), so {settings['dataflow.streaming_template']} "
                f"cannot be rolled out there")

    return [template for template in settings["dataflow.templates"] if template in templates]


def wait_for_new_job(run, stage_name, name, job, known):

    # The job a launch or an update created, once it runs or ends.

    deadline = time.time() + JOB_TIMEOUT_SECONDS
    reported = None

    while time.time() < deadline:
        new = [item for item in jobs(run, stage_name, name, job) if item["id"] not in known]

        if new:
            state = new[0]["state"]

            if state == "Running" or state in FINISHED_STATES:
                return state, new[0]["id"]

            if state != reported:
                release.append_log(run, stage_name, f"{job} ({new[0]['id']}) is {state}\n")

                reported = state

        if release.sleep_unless_cancelled(run, JOB_POLL_SECONDS):
            return "ABORTED", None

    raise RuntimeError(f"The new {job} did not start in time; check the Dataflow console")


def wait_for_relaunch(run, stage_name):
    pending = run["dataflowRelaunch"]

    if pending["approved"]:
        return None

    release.update_stage(
        run, stage_name,
        why=f"Updating {pending['job']} in {pending['configuration']} failed: "
            f"{pending['reason']}. The old job keeps running. Drain and relaunch it to use "
            f"the new template, or cancel the run.")

    while not run["dataflowRelaunch"]["approved"]:
        if release.sleep_unless_cancelled(run, 1):
            return "ABORTED"

    release.update_stage(run, stage_name, why=None)

    return None
