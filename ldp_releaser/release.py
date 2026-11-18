"""The release flow: Jenkins builds, Jira tickets, charts pull requests, the
rollout confirmation, the Faro deploy, GitHub tags and closing the tickets.

Every step reads its settings from the team and personal files, and every
change to a run is reported through `on_change` so it can be saved.
"""

import concurrent.futures
import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from . import gcloud

LOG_TAIL_CHARS = 60_000
ARGOCD_POLL_SECONDS = float(os.environ.get("LDP_ARGOCD_POLL_SECONDS", "30"))
MERGE_POLL_SECONDS = float(os.environ.get("LDP_MERGE_POLL_SECONDS", "60"))
JENKINS_OUTAGE_SECONDS = float(os.environ.get("LDP_JENKINS_OUTAGE_SECONDS", "1800"))
POLL_RETRIES = 10
POLL_SECONDS = float(os.environ.get("LDP_POLL_SECONDS", "5"))
TRANSIENT_HTTP_CODES = {429, 500, 502, 503, 504}

ANSI_PATTERN = re.compile(r"\x1b\[[0-9;]*m")
BRANCH_PATTERN = re.compile(r"[\w./-]+")
VERSION_PATTERN = re.compile(r"\d+(\.\d+)+")

# Steps, in the order a run goes through them.

STAGE_NAMES = (
    "asahBuild", "jira", "developPullRequest", "developMerge",
    "productionPullRequest", "productionMerge", "rollout", "composer",
    "dataflow", "faroBuild", "faroDeploy", "tags", "closeTickets",
)

MODES = ("asah", "faro", "release")

# Steps that can safely pick up again after a restart: Jenkins builds are
# re-attached and waits start waiting again. The others change Jira, git or
# GitHub halfway, so a restart during them fails the run instead.

RESUMABLE_STAGES = {
    "asahBuild", "closeTickets", "composer", "dataflow", "developMerge", "faroBuild",
    "faroDeploy", "productionMerge", "rollout",
}

runs = {}
runs_lock = threading.Lock()
settings = None


def on_change(run):
    pass


def configure(new_settings, change_listener=None):
    global on_change, settings

    settings = new_settings

    if change_listener:
        on_change = change_listener


def add_stage_link(run, stage_name, label, url):
    with runs_lock:
        links = run["stages"][stage_name]["links"]

        if not any(link["url"] == url for link in links):
            links.append({"label": label, "url": url})

    on_change(run)


def adf_paragraph(text):
    return {
        "content": [
            {
                "content": [{"text": text, "type": "text"}],
                "type": "paragraph",
            },
        ],
        "type": "doc",
        "version": 1,
    }


def append_log(run, stage_name, text):
    if not text:
        return

    with runs_lock:
        stage = run["stages"][stage_name]
        log = stage["log"] + ANSI_PATTERN.sub("", text)
        stage["log"] = log[-LOG_TAIL_CHARS:]

    on_change(run)


@contextlib.contextmanager
def charts_worktree(run, stage_name, base_branch, branch):
    checkout = settings["charts.checkout"]

    run_command(
        run, stage_name,
        [
            "git", "-C", checkout, "fetch", "origin",
            settings["charts.develop_branch"],
            settings["charts.production_branch"],
        ])

    branch_created = False
    worktree = Path(tempfile.mkdtemp(prefix="ldp-releaser-")) / "charts"

    try:
        run_command(
            run, stage_name,
            [
                "git", "-C", checkout, "worktree", "add", "-b", branch,
                str(worktree), f"origin/{base_branch}",
            ])

        branch_created = True

        yield worktree
    finally:
        run_command(
            run, stage_name,
            ["git", "-C", checkout, "worktree", "remove", "--force",
             str(worktree)],
            check=False)

        # The branch lives on origin now, so drop the local copy to keep the
        # charts checkout clean.

        if branch_created:
            run_command(
                run, stage_name, ["git", "-C", checkout, "branch", "-D", branch],
                check=False)

        shutil.rmtree(worktree.parent, ignore_errors=True)


def cherry_pick(commits, run, worktree):
    git = ["git", "-C", str(worktree)]
    production_branch = settings["charts.production_branch"]

    head = run_command(
        run, "productionPullRequest", [*git, "rev-parse", "HEAD"]).strip()

    run_command(
        run, "productionPullRequest", [*git, "cherry-pick", *commits],
        check=False)

    release_paths = {relative_path for _, relative_path in release_files(run)}

    # The production branch only catches up with the develop branch on
    # release trains, so a tagSuffix line changed by an earlier release of
    # a develop-only environment conflicts. Keep the production branch's
    # file and set the new suffix in it. Any other conflict needs a person.

    for _ in range(len(commits)):
        conflicts = run_command(
            run, "productionPullRequest",
            [*git, "diff", "--name-only", "--diff-filter=U"]).split()

        if not conflicts:
            break

        unexpected = [path for path in conflicts if path not in release_paths]

        if unexpected:
            run_command(
                run, "productionPullRequest", [*git, "cherry-pick", "--abort"],
                check=False)

            raise RuntimeError(
                f"Cherry-picking onto {production_branch} conflicts in "
                f"{', '.join(unexpected)}, so open that PR by hand")

        for relative_path in conflicts:
            run_command(
                run, "productionPullRequest",
                [*git, "checkout", "--ours", "--", relative_path])

            set_tag_suffix(relative_path, run["tagSuffix"], worktree)

            run_command(
                run, "productionPullRequest", [*git, "add", relative_path])

            append_log(
                run, "productionPullRequest",
                f"Resolved the conflict in {relative_path} by keeping "
                f"{production_branch}'s file with the new tag suffix "
                f"{run['tagSuffix']}\n")

        run_command(
            run, "productionPullRequest",
            [*git, "-c", "core.editor=true", "cherry-pick", "--continue"],
            check=False)

    if run_command(
            run, "productionPullRequest",
            [*git, "diff", "--name-only", "--diff-filter=U"]).split():

        raise RuntimeError(
            f"The cherry-pick onto {production_branch} still has conflicts, "
            f"so open that PR by hand")

    if run_command(
            run, "productionPullRequest",
            [*git, "rev-parse", "HEAD"]).strip() == head:

        raise RuntimeError(
            f"The cherry-pick onto {production_branch} added no commit, see "
            f"the output above")


def choose_release_tickets(jira, choice):

    # Open release tickets are looked up again at submit, so the choice is
    # checked against Jira as it is now, not as the form last saw it.

    tickets = open_release_tickets(jira)

    if not tickets:
        return [], None

    action = choice.get("action")

    if action == "new":
        return tickets, None

    if action == "reuse":
        for ticket in tickets:
            if ticket["key"] == choice.get("key"):
                return [], ticket

        raise ValueError(
            f"{choice.get('key')} is no longer an open release ticket, so "
            f"reload the page and choose again")

    keys = ", ".join(ticket["key"] for ticket in tickets)

    raise ValueError(
        f"Open release tickets found ({keys}): choose whether to reuse one "
        f"or create new tickets")


def close_release_tickets(jira, run):

    # A PRD release ends the release, so its tickets close on their own.
    # Anything short of PRD may have more work, so the person decides.

    if not run["prdRelease"]:
        update_stage(
            run, "closeTickets",
            why="Not a PRD release: close the Jira tickets or keep them open")

        while run["ticketsDecision"] is None:
            if sleep_unless_cancelled(run, 1):
                return "ABORTED"

        if run["ticketsDecision"] == "keep":
            append_log(
                run, "closeTickets",
                f"Kept {run['jiraTask']} and {run['jiraTechnicalTask']} open\n")

            return "SKIPPED"

    closed_status = settings["jira.workflow.closed_status"]

    # The Task can only be closed once its Technical Task is closed.

    for key in (run["jiraTechnicalTask"], run["jiraTask"]):
        if jira.status(key) != closed_status:
            jira.transition_to(
                key, closed_status,
                {
                    "resolution": {
                        "name": settings["jira.workflow.completed_resolution"],
                    },
                })

            append_log(
                run, "closeTickets",
                f"Closed {key} as "
                f"{settings['jira.workflow.completed_resolution']}\n")

        add_stage_link(run, "closeTickets", key, jira.issue_url(key))


def create_pull_request(run, stage_name, base_branch, body, branch, worktree):
    run_command(
        run, stage_name,
        ["git", "-C", str(worktree), "push", "-u", "origin", branch])

    output = run_command(
        run, stage_name,
        [
            "gh", "pr", "create", "--base", base_branch, "--body", body,
            "--head", branch, "--repo", settings["charts.repository"],
            "--title", f"{run['jiraTechnicalTask']} {run['releaseSummary']}",
        ],
        cwd=worktree)

    pull_request_url = output.strip().splitlines()[-1]

    add_stage_link(run, stage_name, "Pull request", pull_request_url)

    notify_slack(run, stage_name, pull_request_url)

    return pull_request_url


def create_release_tags(run):
    prd_environment = settings["release.prd_environment"]

    for project, repository, tag in release_tags(
            run["asahBranch"], run["faroBranch"], run["version"]):

        if project == "asah":
            commit = run["asahCommit"]
        else:
            commit = run.get("faroCommits", {}).get(prd_environment) or run.get(
                "faroCommit")

        if commit is None:
            raise RuntimeError(
                f"The build log has no commit matching "
                f"jenkins.commit_pattern, so there is no commit to tag as "
                f"{tag}")

        run_command(
            run, "tags",
            [
                "gh", "api", "--method", "POST",
                f"repos/{repository}/git/refs", "-f", f"ref=refs/tags/{tag}",
                "-f", f"sha={commit}", "--jq", ".ref",
            ])

        add_stage_link(
            run, "tags", f"{tag} ({commit[:7]})",
            f"https://github.com/{repository}/tree/{tag}")


def create_release_tickets(jira, run):
    verify_charts_change(run)

    for task in run["discardTickets"]:
        discard_release_ticket(jira, run, task)

    if run["reuseTicket"]:
        return reuse_release_ticket(jira, run, run["reuseTicket"])

    summary = run["releaseSummary"]

    fields = {
        "assignee": {"accountId": jira.check()["accountId"]},
        "components": [{"name": settings["jira.component"]}],
        "project": {"key": settings["jira.project"]},
        "summary": summary,
    }

    task_key, sprint = new_release_task(
        jira, summary,
        f"Release the Asah images with tag suffix {run['tagSuffix']} to "
        f"{list_environments(run['releaseEnvironments'])}.")

    append_log(run, "jira", f"Created {task_key}: {summary}\n")
    add_stage_link(run, "jira", f"{task_key} (Task)", jira.issue_url(task_key))
    update_run(run, jiraTask=task_key)

    if sprint:
        append_log(run, "jira", f"Added {task_key} to sprint {sprint}\n")
    else:
        append_log(run, "jira", "No active sprint, so no sprint was set\n")

    technical_task_key = jira.request(
        "POST", "/rest/api/3/issue",
        {
            "fields": {
                **fields,
                "issuetype": {"name": settings["jira.technical_task_type"]},
                "parent": {"key": task_key},
            },
        })["key"]

    append_log(run, "jira", f"Created {technical_task_key}\n")
    add_stage_link(
        run, "jira", f"{technical_task_key} (Technical Task)",
        jira.issue_url(technical_task_key))
    update_run(run, jiraTechnicalTask=technical_task_key)

    move_to_in_progress(
        jira, run, task_key, "jira.workflow.task_to_in_progress")
    move_to_in_progress(
        jira, run, technical_task_key,
        "jira.workflow.technical_task_to_in_progress")


def create_run(
        asah_branch, faro_environments, faro_branch, release_environments,
        tag_suffix, version, composer=False, mode="release", dataflow=(),
        discard_tickets=(), reuse_ticket=None, reuse_builds=True):

    production = bool(
        set(settings["charts.production.environments"]) &
        set(release_environments))

    run = {
        "asahBranch": asah_branch,
        "asahCommit": None,
        "asahImages": [],
        "cancelRequested": False,
        "composer": composer,
        "composerCommit": None,
        "composerDone": [],
        "createdAt": time.time(),
        "dataflow": list(dataflow),
        "dataflowCommit": None,
        "dataflowDone": [],
        "dataflowRelaunch": None,
        "dataflowTemplates": list(dataflow),
        "dataflowVersion": None,
        "developMerge": None,
        "developPullRequest": None,
        "discardTickets": list(discard_tickets),
        "error": None,
        "faroBranch": faro_branch,
        "faroCommits": {},
        "faroBuilds": {},
        "faroEnvironments": faro_environments,
        "faroImages": {},
        "faroSharedBuilds": True,
        "finishedAt": None,
        "id": uuid.uuid4().hex[:8],
        "jiraTask": None,
        "jiraTechnicalTask": None,
        "mode": mode,
        "prdRelease": is_prd_release(
            faro_branch, faro_environments, release_environments),
        "productionMerge": None,
        "productionPullRequest": None,
        "releaseEnvironments": release_environments,
        "releaseSummary": None,
        "reuseBuilds": reuse_builds,
        "reuseTicket": reuse_ticket,
        "rolloutConfirmed": False,
        "rolloutsConfirmed": {environment: False for environment in release_environments},
        "stages": {
            "asahBuild": new_stage(settings["asah.build_job"], asah_branch),
            "closeTickets": new_stage("jira", release_environments),
            "composer": new_stage("gcloud", composer),
            "dataflow": new_stage("gcloud", bool(dataflow)),
            "developMerge": new_stage("charts", release_environments),
            "developPullRequest": new_stage("charts", release_environments),
            "jira": new_stage("jira", release_environments),
            "productionMerge": new_stage("charts", production),
            "productionPullRequest": new_stage("charts", production),
            "tags": new_stage("github", version),
        },
        "status": "running",
        "tagSuffix": tag_suffix,
        "ticketsDecision": None,
        "version": version,
    }

    tree_layout(run, composer, dataflow, faro_environments, release_environments)

    if release_environments:
        run["releaseSummary"] = release_summary(release_environments)

    with runs_lock:
        runs[run["id"]] = run

    on_change(run)

    return run


def tree_layout(run, composer_on, dataflow_templates, faro_environments, release_environments):

    # A release as a tree: the Asah build; then, side by side, the charts
    # (tickets, pull requests, merges) and the Faro builds (one per build
    # target); then a branch per environment that goes on as soon as its own
    # rollout is confirmed (rollout, Composer DAGs, Dataflow, Faro deploy);
    # then the steps that need every branch (tags, closing the tickets).

    from . import composer

    stages = run["stages"]
    labels = {}

    for name in ("composer", "dataflow"):
        stages.pop(name, None)

    def add(name, job, label):
        stages[name] = new_stage(job, True)
        labels[name] = label

        return name

    faro_builds = []

    for target in dict.fromkeys(faro_target(environment) for environment in faro_environments):
        sharing = [environment for environment in faro_environments if faro_target(environment) == target]

        faro_builds.append(add(
            f"faroBuild-{target}", settings["faro.build_job"],
            f"Faro build {target} (for {list_environments(sharing)})"))

    lanes = {}

    for environment in settings.environments():
        if environment not in release_environments and environment not in faro_environments:
            continue

        names = []

        if environment in release_environments:
            names.append(add(f"rollout-{environment}", "argocd", "Rollout confirmed"))

            configurations = composer.configurations_for([environment])

            if composer_on and configurations:
                names.append(add(f"composer-{environment}", "gcloud", "Composer DAGs"))

            if dataflow_templates and configurations:
                names.append(add(f"dataflow-{environment}", "gcloud", "Dataflow"))

        if environment in faro_environments:
            names.append(add(
                f"faroDeploy-{environment}", settings["faro.deploy_job"],
                f"Faro deploy ({faro_target(environment)} build)"))

        lanes[environment] = names

    charts = ["jira", "developPullRequest", "developMerge", "productionPullRequest", "productionMerge"]

    run.update(
        join=["tags", "closeTickets"], lanes=lanes, layout="tree",
        stageLabels=labels, trunk=["asahBuild"],
        tracks={"Charts": charts, "Faro builds": faro_builds})
    run["stageOrder"] = [
        "asahBuild", *charts, *faro_builds,
        *[name for names in lanes.values() for name in names], "tags", "closeTickets"]


# Resuming an ended run: the step that ended it (cancelled or failed) and the
# steps that never ran start again; every step that succeeded stays done and
# is not repeated, so a run cancelled during the Faro deploy deploys the image
# its build already pushed. The engine then goes on as after a restart.

OUTSIDE_STEPS = {
    "closeTickets": "closes Jira tickets", "developPullRequest": "opens a charts pull request",
    "jira": "creates or moves Jira tickets", "productionPullRequest": "opens a charts pull request",
    "tags": "creates GitHub tags",
}


def resume_plan(run):
    retried = [name for name, stage in run["stages"].items() if stage["status"] in ("failed", "aborted")]
    kept = [name for name, stage in run["stages"].items() if stage_done(run, name)]

    return retried, kept


def resume(run):
    with runs_lock:
        if run["finishedAt"] is None:
            raise ValueError("The run is still running")

        if run.get("kind") in ("composer", "dataflow") or run["status"] not in ("aborted", "failed"):
            raise ValueError("Only a cancelled or failed release can be resumed")

        stamp = time.strftime("%Y-%m-%d %H:%M:%S")

        for stage in run["stages"].values():
            if stage["status"] in ("failed", "aborted"):
                stage.update(
                    failure=None, finishedAt=None, logOffset=0, needsGcloudLogin=False, number=None,
                    queueUrl=None, result=None, startedAt=None, status="pending", url=None, why=None)
                stage["log"] = (stage["log"] + f"\n{stamp} resumed: this step runs again\n")[-LOG_TAIL_CHARS:]

            # A step the run never reached (as opposed to one it ran and
            # skipped by a decision, which logged it) gets its chance again;
            # the engine skips it once more if the run does not need it.

            elif stage["status"] == "skipped" and not stage["log"] and not stage["startedAt"]:
                stage["status"] = "pending"

        run.update(
            cancelRequested=False, error=None, finishedAt=None, halted=False, status="running",
            resumedAt=[*run.get("resumedAt", []), time.time()])

    on_change(run)


def discard_release_ticket(jira, run, task):
    closed_status = settings["jira.workflow.closed_status"]
    resolution = settings["jira.workflow.discarded_resolution"]

    # The Task can only be closed once its Technical Tasks are closed.

    for key in [*task["technicalTasks"], task["key"]]:
        if jira.status(key) == closed_status:
            continue

        jira.transition_to(key, closed_status, {"resolution": {"name": resolution}})

        append_log(run, "jira", f"Closed {key} as {resolution}\n")


def execute(jenkins, jira, run):

    # Runs created as a tree run their steps as a graph; runs saved before
    # run them one after the other.

    if run.get("layout") == "tree":
        return execute_tree(jenkins, jira, run)

    steps = [
        ("asahBuild", bool(run["asahBranch"]), lambda: build_asah(jenkins, run)),
        (
            "jira", bool(run["releaseEnvironments"]),
            lambda: run_local_stage(
                run, "jira", lambda: create_release_tickets(jira, run)),
        ),
        (
            "developPullRequest", bool(run["releaseEnvironments"]),
            lambda: run_local_stage(
                run, "developPullRequest",
                lambda: open_develop_pull_request(run)),
        ),
        (
            "developMerge", bool(run["releaseEnvironments"]),
            lambda: run_local_stage(
                run, "developMerge",
                lambda: wait_for_merge(
                    run, "developMerge", run["developPullRequest"])),
        ),
        (
            "productionPullRequest", needs_production(run),
            lambda: run_local_stage(
                run, "productionPullRequest",
                lambda: open_production_pull_request(run)),
        ),
        (
            "productionMerge", needs_production(run),
            lambda: run_local_stage(
                run, "productionMerge",
                lambda: wait_for_merge(
                    run, "productionMerge", run["productionPullRequest"])),
        ),
        *rollout_steps(run),
        (
            "composer", bool(run.get("composer")),
            lambda: run_local_stage(run, "composer", lambda: composer_step(run)),
        ),
        (
            "dataflow", bool(run.get("dataflow")),
            lambda: run_local_stage(run, "dataflow", lambda: dataflow_step(run)),
        ),
        *faro_steps(jenkins, run),
        (
            "tags", bool(run["version"]),
            lambda: run_local_stage(run, "tags", lambda: create_release_tags(run)),
        ),
        (
            "closeTickets", bool(run["releaseEnvironments"]),
            lambda: run_local_stage(
                run, "closeTickets", lambda: close_release_tickets(jira, run)),
        ),
    ]

    try:
        activate_release_configuration(run)

        result = "SUCCESS"

        for stage_name, enabled, action in steps:
            if not enabled or stage_done(run, stage_name):
                continue

            result = action()

            if result != "SUCCESS":
                break

        if result == "SUCCESS":
            update_run(run, status="success")
        elif result == "ABORTED":
            update_run(run, status="aborted")
        else:
            update_run(run, status="failed")
    except Exception as exception:
        update_run(run, error=str(exception), status="failed")
    finally:
        finish(run)


# gcloud keeps one active configuration for the whole machine, so the steps
# that switch it (Composer DAGs, Dataflow) take turns across branches.

gcloud_lock = threading.Lock()


def with_gcloud(run, stage_name, function):
    def locked():
        if gcloud_lock.locked():
            update_stage(run, stage_name, why="Waits for another environment's gcloud step")

        with gcloud_lock:
            update_stage(run, stage_name, why=None)

            return function()

    return run_local_stage(run, stage_name, locked)


def tree_steps(jenkins, jira, run):

    # (stage, the stages it needs, whether it runs, what it does). A stage
    # that does not run counts as done for the stages that need it.

    from . import composer, dataflow

    releasing = bool(run["releaseEnvironments"])
    production = needs_production(run)
    production_environments = set(settings["charts.production.environments"])
    local = lambda name, function: lambda: run_local_stage(run, name, function)

    steps = [
        ("asahBuild", [], bool(run["asahBranch"]), lambda: build_asah(jenkins, run)),
        ("jira", ["asahBuild"], releasing, local("jira", lambda: create_release_tickets(jira, run))),
        ("developPullRequest", ["jira"], releasing, local("developPullRequest", lambda: open_develop_pull_request(run))),
        ("developMerge", ["developPullRequest"], releasing,
         local("developMerge", lambda: wait_for_merge(run, "developMerge", run["developPullRequest"]))),
        ("productionPullRequest", ["developMerge"], production,
         local("productionPullRequest", lambda: open_production_pull_request(run))),
        ("productionMerge", ["productionPullRequest"], production,
         local("productionMerge", lambda: wait_for_merge(run, "productionMerge", run["productionPullRequest"]))),
    ]

    for name in run["tracks"]["Faro builds"]:
        target = name.partition("-")[2]

        steps.append((name, ["asahBuild"], True, lambda target=target, name=name: build_faro(jenkins, run, target, name)))

    for environment, names in run["lanes"].items():

        # A rollout needs the merge that changed its environment's values:
        # develop for STG, production for INT and PRD.

        previous = None

        if environment in run["releaseEnvironments"]:
            previous = "productionMerge" if production and environment in production_environments else "developMerge"

        for name in names:
            needs = [previous] if previous else []

            if name.startswith("rollout-"):
                action = local(name, lambda environment=environment, name=name: wait_for_rollout_confirmation(run, [environment], name))
            elif name.startswith("composer-"):
                action = lambda environment=environment, name=name: with_gcloud(
                    run, name, lambda: composer.release_step(run, name, [environment]))
            elif name.startswith("dataflow-"):
                action = lambda environment=environment, name=name: with_gcloud(
                    run, name, lambda: dataflow.release_step(run, name, [environment]))
            else:
                needs.append(f"faroBuild-{faro_target(environment)}")
                action = lambda environment=environment, name=name: run_stage(
                    jenkins, run, name, settings["faro.deploy_job"],
                    {
                        settings["faro.environment_parameter"]: environment,
                        settings["faro.image_parameter"]: faro_image(run, environment),
                    })

            steps.append((name, needs, True, action))
            previous = name

    everything = [name for name, _, _, _ in steps]

    steps.append(("tags", everything, bool(run["version"]), local("tags", lambda: create_release_tags(run))))
    steps.append(("closeTickets", [*everything, "tags"], releasing, local("closeTickets", lambda: close_release_tickets(jira, run))))

    return steps


def execute_tree(jenkins, jira, run):

    # Each step starts as soon as the steps it needs are done, so the charts
    # and the Faro builds run side by side and every environment goes on once
    # its own rollout is confirmed. After a failure or a cancel nothing new
    # starts; the steps already running end on their own.

    steps = tree_steps(jenkins, jira, run)
    update_run(run, halted=False)
    done = set()
    started = {}
    result = "SUCCESS"
    error = None

    try:
        activate_release_configuration(run)

        with concurrent.futures.ThreadPoolExecutor(max_workers=len(steps)) as pool:
            while True:
                for name, needs, enabled, action in steps:
                    if name in done or name in started:
                        continue

                    if not enabled or stage_done(run, name):
                        done.add(name)

                        continue

                    if result == "SUCCESS" and not run["cancelRequested"] and all(need in done for need in needs):
                        started[name] = pool.submit(action)

                running = {future: name for name, future in started.items() if not future.done()}
                finished = [name for name, future in started.items() if future.done() and name not in done]

                for name in finished:
                    try:
                        outcome = started[name].result()
                    except Exception as exception:
                        outcome = "FAILURE"
                        error = error or str(exception)

                    if outcome in ("SUCCESS", None):
                        done.add(name)
                    else:
                        if result == "SUCCESS":
                            result = "ABORTED" if outcome == "ABORTED" else "FAILURE"

                            # The other branches stop waiting for people.

                            update_run(run, halted=True)

                        done.add(name)

                if finished:
                    continue

                if not running:
                    break

                concurrent.futures.wait(running, return_when=concurrent.futures.FIRST_COMPLETED)

        if error:
            update_run(run, error=error, status="failed")
        elif result == "SUCCESS" and not run["cancelRequested"]:
            update_run(run, status="success")
        elif result == "ABORTED" or run["cancelRequested"]:
            update_run(run, status="aborted")
        else:
            update_run(run, status="failed")
    except Exception as exception:
        update_run(run, error=str(exception), status="failed")
    finally:
        finish(run)


def activate_release_configuration(run):

    # Starting a release makes the first gcloud configuration of its
    # environments the active one, so gcloud commands run by hand during the
    # release reach the environment being released. A run resumed after a
    # restart keeps whatever is active by then.

    if run.get("gcloudConfiguration") or run.get("gcloudProblem"):
        return

    names = [
        name for environment in settings.environments()
        if environment in run["releaseEnvironments"]
        for name in (settings["gcloud.environments"] or {}).get(environment, [])
    ]

    if not names:
        return

    problem = gcloud.activate(names[0])

    if problem:
        update_run(run, gcloudProblem=f"Could not activate gcloud configuration {names[0]}: {problem}")
    else:
        update_run(run, gcloudConfiguration=names[0])


def build_asah(jenkins, run):
    params = {settings["asah.branch_parameter"]: run["asahBranch"]}

    reused = reuse_build(
        jenkins, run, "asahBuild", settings["asah.build_job"], params,
        settings["asah.repository"], run["asahBranch"],
        settings.compiled("asah.image_pattern"))

    if reused:
        commit, images = reused
        result = "SUCCESS"
    else:
        result = run_stage(
            jenkins, run, "asahBuild", settings["asah.build_job"], params)

        if result != "SUCCESS":
            return result

        commit, images = inspect_build(
            jenkins, settings.compiled("asah.image_pattern"), run, "asahBuild")

    update_run(run, asahCommit=commit, asahImages=images)

    if run["releaseEnvironments"]:
        update_run(run, tagSuffix=extract_tag_suffix(images))

    return result


def build_faro(jenkins, run, target, stage_name):
    params = {
        settings["faro.branch_parameter"]: run["faroBranch"],
        settings["faro.environment_parameter"]: target,
    }

    reused = reuse_build(
        jenkins, run, stage_name, settings["faro.build_job"], params,
        settings["faro.repository"], run["faroBranch"],
        settings.compiled("faro.image_pattern"))

    if reused:
        commit, images = reused
        result = "SUCCESS"
    else:
        result = run_stage(
            jenkins, run, stage_name, settings["faro.build_job"], params)

        if result != "SUCCESS":
            return result

        commit, images = inspect_build(
            jenkins, settings.compiled("faro.image_pattern"), run, stage_name)

    if not images:
        raise RuntimeError(
            "The Faro build passed, but its log has no image matching "
            "faro.image_pattern")

    # Every environment of the run built for this target deploys this image.

    environments = [
        environment for environment in faro_environments(run)
        if faro_target(environment) == target
    ]

    with runs_lock:
        run["faroBuilds"] = {**run.get("faroBuilds", {}), target: {"commit": commit, "image": images[-1]}}
        run["faroCommits"] = {**run.get("faroCommits", {}), **{environment: commit for environment in environments}}
        run["faroImages"] = {**run.get("faroImages", {}), **{environment: images[-1] for environment in environments}}

    on_change(run)

    return result


def faro_environments(run):

    # Runs saved before Faro took several environments have one.

    if "faroEnvironments" in run:
        return run["faroEnvironments"]

    if run["faroBranch"]:
        return [run["environment"]]

    return []


def faro_image(run, environment):
    return run.get("faroImages", {}).get(environment) or run.get("faroImage")


def faro_target(environment):
    return settings["faro.build_environments"][environment]


def faro_stage_names(run, environment):

    # Runs saved before shared builds have a build per environment.

    if run.get("faroSharedBuilds"):
        return f"faroBuild-{faro_target(environment)}", f"faroDeploy-{environment}"

    if f"faroBuild-{environment}" in run["stages"]:
        return f"faroBuild-{environment}", f"faroDeploy-{environment}"

    return "faroBuild", "faroDeploy"


def faro_steps(jenkins, run):

    # Build then deploy, environment by environment: the next build starts
    # only once the previous deploy has finished, and a target already built
    # in the run is not built again.

    steps = []
    planned = set()

    for environment in faro_environments(run):
        build_name, deploy_name = faro_stage_names(run, environment)

        if build_name not in planned:
            planned.add(build_name)

            target = environment

            if run.get("faroSharedBuilds") or build_name == "faroBuild":
                target = faro_target(environment)

            if run.get("faroSharedBuilds"):
                steps.append(
                    (
                        build_name, True,
                        lambda target=target, build_name=build_name: build_faro(
                            jenkins, run, target, build_name),
                    ))
            else:
                steps.append(
                    (
                        build_name, True,
                        lambda environment=environment, build_name=build_name: build_faro_legacy(
                            jenkins, run, environment, build_name),
                    ))

        steps.append(
            (
                deploy_name, True,
                lambda environment=environment, deploy_name=deploy_name: run_stage(
                    jenkins, run, deploy_name, settings["faro.deploy_job"],
                    {
                        settings["faro.environment_parameter"]: environment,
                        settings["faro.image_parameter"]: faro_image(run, environment),
                    }),
            ))

    return steps


def build_faro_legacy(jenkins, run, environment, stage_name):

    # A run saved before shared builds, resumed: one build per environment.

    result = run_stage(
        jenkins, run, stage_name, settings["faro.build_job"],
        {
            settings["faro.branch_parameter"]: run["faroBranch"],
            settings["faro.environment_parameter"]: faro_target(environment),
        })

    if result != "SUCCESS":
        return result

    commit, images = inspect_build(
        jenkins, settings.compiled("faro.image_pattern"), run, stage_name)

    if not images:
        raise RuntimeError(
            "The Faro build passed, but its log has no image matching "
            "faro.image_pattern")

    with runs_lock:
        run["faroCommits"] = {**run.get("faroCommits", {}), environment: commit}
        run["faroImages"] = {**run.get("faroImages", {}), environment: images[-1]}

    on_change(run)

    return result


def composer_step(run):

    # Imported here: the Composer module builds on this one.

    from . import composer

    return composer.release_step(run)


def dataflow_step(run):
    from . import dataflow

    return dataflow.release_step(run)


def extract_tag_suffix(images):
    pattern = settings.compiled("asah.tag_suffix_pattern")

    suffixes = {
        match.group(0)
        for match in (pattern.search(image.rsplit(":", 1)[-1]) for image in images)
        if match
    }

    if len(suffixes) != 1:
        raise RuntimeError(
            f"Expected one tag suffix in the Asah images, found "
            f"{sorted(suffixes) or 'none'}")

    return suffixes.pop()


def finish(run):
    with runs_lock:
        for stage in run["stages"].values():
            if stage["status"] == "pending":
                stage["status"] = "skipped"
            elif stage["status"] in ("queued", "running"):
                stage["finishedAt"] = time.time()
                stage["status"] = "failed"
                stage["why"] = None

        run["finishedAt"] = time.time()

    on_change(run)


def format_duration(seconds):
    if seconds < 90:
        return f"{round(seconds)}s"

    return f"{round(seconds / 60)} min"


def free_branch_name(run, stage_name):
    key = run["jiraTechnicalTask"]

    output = run_command(
        run, stage_name,
        ["git", "-C", settings["charts.checkout"], "ls-remote", "--heads",
         "origin"],
        quiet=True)

    names = {
        line.split("refs/heads/", 1)[1] for line in output.splitlines()
        if "refs/heads/" in line
    }

    branch = key
    number = 2

    while branch in names:
        branch = f"{key}-{number}"
        number += 1

    return branch


def inspect_build(jenkins, image_pattern, run, stage_name):
    console = jenkins.get_text(
        run["stages"][stage_name]["url"] + "consoleText")

    commit = None
    commits = settings.compiled("jenkins.commit_pattern").findall(console)

    if commits:
        commit = commits[-1]

    return commit, list(dict.fromkeys(image_pattern.findall(console)))


# A build already made of the branch's current head is used again instead of
# building the same commit twice, so every environment gets exactly the
# images the earlier ones got (STG on Monday, INT and PRD on Thursday). Any
# successful build of the job with the same parameters counts, whoever
# started it. A new commit on the branch, or a run started with
# reuseBuilds off, builds again.

REUSE_CANDIDATES = 3


def branch_head(repository, branch):
    completed = subprocess.run(
        ["gh", "api", f"repos/{repository}/commits/{urllib.parse.quote(branch, safe='')}", "--jq", ".sha"],
        capture_output=True, text=True, timeout=60)

    if completed.returncode != 0:
        lines = (completed.stderr or completed.stdout).strip().splitlines()

        raise RuntimeError(lines[-1] if lines else "gh failed")

    return completed.stdout.strip()


def find_reusable_build(jenkins, job, params, head, image_pattern):
    data = jenkins.get_json(
        jenkins.job_url(job) + "api/json",
        "builds[number,url,result,actions[parameters[name,value]]]{0,40}")

    # Newest first; the newest successful build with these parameters is the
    # only one that can have built the current head.

    checked = 0

    for build in data.get("builds", []):
        if build.get("result") != "SUCCESS":
            continue

        values = {
            parameter["name"]: parameter.get("value")
            for action in build.get("actions", []) or []
            for parameter in (action or {}).get("parameters", [])
        }

        if any(values.get(name) != value for name, value in params.items()):
            continue

        checked += 1

        console = jenkins.get_text(build["url"] + "consoleText")
        commits = settings.compiled("jenkins.commit_pattern").findall(console)
        images = list(dict.fromkeys(image_pattern.findall(console)))

        if commits and commits[-1] == head and images:
            return build, commits[-1], images

        if checked >= REUSE_CANDIDATES:
            break

    return None


def reuse_build(jenkins, run, stage_name, job, params, repository, branch, image_pattern):

    # Only before the stage reached Jenkins: a resumed stage keeps its build.

    stage = run["stages"][stage_name]

    if not run.get("reuseBuilds", True) or stage["url"] or stage["queueUrl"]:
        return None

    try:
        head = branch_head(repository, branch)
        found = find_reusable_build(jenkins, job, params, head, image_pattern)
    except Exception as exception:
        append_log(run, stage_name, f"Could not look for an earlier build to reuse ({exception}); building\n")

        return None

    if not found:
        append_log(run, stage_name, f"No earlier build of {branch} at {head[:7]}; building\n")

        return None

    build, commit, images = found

    if stage_name == "asahBuild":
        try:
            extract_tag_suffix(images)
        except RuntimeError as exception:
            append_log(run, stage_name, f"Build #{build['number']} built {commit[:7]} but cannot be reused ({exception}); building\n")

            return None

    now = time.time()

    update_stage(
        run, stage_name, finishedAt=now, number=build["number"], parameters=params,
        result="SUCCESS", reused=True, startedAt=now, status="success", url=build["url"])
    append_log(
        run, stage_name,
        f"Reused build #{build['number']}: it built {commit[:7]}, the head of {branch}, so there is nothing new to build.\n"
        + "".join(f"  {image}\n" for image in images))

    return commit, images


def is_prd_release(faro_branch, faro_environments, release_environments):
    prd_environment = settings["release.prd_environment"]

    if prd_environment in release_environments:
        return True

    return bool(faro_branch) and prd_environment in faro_environments


def list_environments(environments):
    names = [
        environment.upper() for environment in settings.environments()
        if environment in environments
    ]

    if len(names) == 1:
        return names[0]

    return ", ".join(names[:-1]) + " and " + names[-1]


def move_to_in_progress(jira, run, key, statuses_key, log=None):

    # Some issue types reach In Progress through intermediate statuses, so
    # the configured path is followed from wherever the ticket is now.

    if log is None:
        def log(text):
            append_log(run, "jira", text)

    status_names = list(settings[statuses_key])

    status_name = jira.status(key)

    if status_name in status_names:
        status_names = status_names[status_names.index(status_name) + 1:]

    for status_name in status_names:
        jira.transition_to(key, status_name)

        log(f"Moved {key} to {status_name}\n")


def needs_production(run):
    return bool(
        set(settings["charts.production.environments"]) &
        set(run["releaseEnvironments"]))


def new_stage(job, enabled):
    status = "skipped"

    if enabled:
        status = "pending"

    return {
        "finishedAt": None,
        "job": job,
        "links": [],
        "log": "",
        "logOffset": 0,
        "number": None,
        "parameters": None,
        "queueUrl": None,
        "result": None,
        "startedAt": None,
        "status": status,
        "url": None,
        "why": None,
    }


def notify_slack(run, stage_name, pull_request_url):

    # A missed notification must not block a release, so failures are only
    # logged.

    webhook_url = settings["slack.webhook_url"]

    if not webhook_url:
        append_log(
            run, stage_name,
            "No Slack webhook in the team settings, so no one was notified\n")

        return

    environments = ", ".join(
        environment.upper() for environment, _ in release_files(run))

    request = urllib.request.Request(
        webhook_url,
        data=json.dumps(
            {"chartsUrl": pull_request_url, "environments": environments}
        ).encode(),
        headers={"Content-Type": "application/json"}, method="POST")

    try:
        with urllib.request.urlopen(request, timeout=30):
            pass

        append_log(run, stage_name, f"Notified Slack about {pull_request_url}\n")
    except (OSError, urllib.error.URLError) as exception:
        append_log(run, stage_name, f"Could not notify Slack: {exception}\n")


def open_develop_pull_request(run):
    develop_branch = settings["charts.develop_branch"]

    pull_request_url = reuse_open_pull_request(
        run, "developPullRequest", develop_branch)

    if pull_request_url:
        update_run(run, developPullRequest=pull_request_url)

        return

    branch = free_branch_name(run, "developPullRequest")

    with charts_worktree(
            run, "developPullRequest", develop_branch, branch) as worktree:

        for _, relative_path in release_files(run):
            set_tag_suffix(relative_path, run["tagSuffix"], worktree)

        run_command(
            run, "developPullRequest", ["git", "-C", str(worktree), "add", "-A"])
        run_command(
            run, "developPullRequest",
            [
                "git", "-C", str(worktree), "commit", "-m",
                f"{run['jiraTechnicalTask']} {run['releaseSummary']}",
            ])

        update_run(
            run,
            developPullRequest=create_pull_request(
                run, "developPullRequest", develop_branch,
                pull_request_body(run), branch, worktree))


def open_production_pull_request(run):
    merge = run["developMerge"]
    production_branch = settings["charts.production_branch"]

    pull_request_url = reuse_open_pull_request(
        run, "productionPullRequest", production_branch)

    if pull_request_url:
        update_run(run, productionPullRequest=pull_request_url)

        return

    branch = free_branch_name(run, "productionPullRequest")

    with charts_worktree(
            run, "productionPullRequest", production_branch,
            branch) as worktree:

        parents = run_command(
            run, "productionPullRequest",
            [
                "git", "-C", str(worktree), "rev-list", "--parents", "-n", "1",
                merge["commit"],
            ]).split()

        # A squash merge leaves one commit on the develop branch. A merge
        # commit has two parents, so pick the pull request's own commits.

        commits = [merge["commit"]]

        if len(parents) > 2:
            commits = merge["commits"]

        cherry_pick(commits, run, worktree)

        update_run(
            run,
            productionPullRequest=create_pull_request(
                run, "productionPullRequest", production_branch,
                pull_request_body(
                    run,
                    f"Cherry-pick of #{merge['number']} onto "
                    f"`{production_branch}`.\n\n"),
                branch, worktree))


def new_release_task(jira, summary, description):

    # Creates a release Task, assigned to the person, in the board's active
    # sprint. Returns its key and the sprint name (None without a sprint).

    task_key = jira.request(
        "POST", "/rest/api/3/issue",
        {
            "fields": {
                "assignee": {"accountId": jira.check()["accountId"]},
                "components": [{"name": settings["jira.component"]}],
                "description": adf_paragraph(description),
                "issuetype": {"name": settings["jira.task_type"]},
                "project": {"key": settings["jira.project"]},
                "summary": summary,
            },
        })["key"]

    sprints = jira.request(
        "GET",
        f"/rest/agile/1.0/board/{settings['jira.board_id']}/sprint"
        f"?state=active")["values"]

    if not sprints:
        return task_key, None

    jira.request(
        "POST", f"/rest/agile/1.0/sprint/{sprints[0]['id']}/issue",
        {"issues": [task_key]})

    return task_key, sprints[0]["name"]


def open_release_tickets(jira):

    # Any open release Task of the team's component can be picked up, by
    # whoever starts the next release.

    title_prefix = settings["jira.title"].split("{", 1)[0].strip()

    jql = (
        f'project = "{settings["jira.project"]}" AND '
        f'issuetype = "{settings["jira.task_type"]}" AND '
        f'component = "{settings["jira.component"]}" AND '
        f'statusCategory != Done')

    if title_prefix:
        jql += f' AND summary ~ "\\"{title_prefix}\\""'

    issues = jira.request(
        "GET",
        "/rest/api/3/search/jql?" + urllib.parse.urlencode(
            {
                "fields": "assignee,status,subtasks,summary",
                "jql": jql + " ORDER BY created DESC",
                "maxResults": 10,
            }))["issues"]

    # Tickets of runs still in progress here belong to those runs, not to a
    # release that could not be completed.

    active_keys = set()

    with runs_lock:
        for run in runs.values():
            if run["finishedAt"] is not None:
                continue

            if run.get("kind") in ("composer", "dataflow"):
                continue

            active_keys.add(run["jiraTask"])
            active_keys.update(ticket["key"] for ticket in run["discardTickets"])

            if run["reuseTicket"]:
                active_keys.add(run["reuseTicket"]["key"])

    closed_status = settings["jira.workflow.closed_status"]
    tickets = []

    for issue in issues:
        if issue["key"] in active_keys:
            continue

        fields = issue["fields"]

        if title_prefix and not fields["summary"].startswith(title_prefix):
            continue

        assignee = fields.get("assignee") or {}

        tickets.append(
            {
                "assignee": {
                    "accountId": assignee.get("accountId"),
                    "displayName": assignee.get("displayName") or "Unassigned",
                },
                "key": issue["key"],
                "status": fields["status"]["name"],
                "summary": fields["summary"],
                # Only the release's own Technical Task: others, such as a
                # vulnerability fix, keep their own titles.

                "technicalTasks": [
                    subtask["key"] for subtask in fields["subtasks"]
                    if subtask["fields"]["issuetype"]["name"] ==
                        settings["jira.technical_task_type"] and
                    subtask["fields"]["status"]["name"] != closed_status and
                    subtask["fields"].get("summary", "").startswith(title_prefix)
                ],
            })

    return tickets


def pull_request_body(run, note=""):
    key = run["jiraTechnicalTask"]
    jira_url = settings["jira.url"].rstrip("/")
    tag_suffix_key = settings["charts.tag_suffix_key"]

    file_lines = "\n".join(
        f"- `{relative_path}` ({environment.upper()})"
        for environment, relative_path in release_files(run))

    return (
        f"# [{key}]({jira_url}/browse/{key})\n\n"
        f"## Description of the change\n\n"
        f"Sets `{tag_suffix_key}` to `{run['tagSuffix']}` in:\n\n"
        f"{file_lines}\n\n"
        f"{note}"
        f"## Additional information\n\n"
        f"### Checklist\n\n"
        f"- [x] Title of the pull request follows this "
        f"[pattern]({settings['charts.contributing_url']});\n")


def recent_branches(jenkins, job, parameter):
    data = jenkins.get_json(
        jenkins.job_url(job) + "api/json",
        "builds[actions[parameters[name,value]]]{0,40}")
    branches = {}

    for build in data.get("builds", []):
        for action in build.get("actions", []):
            for build_parameter in action.get("parameters", []):
                if build_parameter["name"] == parameter:
                    branches.setdefault(build_parameter["value"], None)

    return list(branches)


def release_files(run):
    return [
        (environment, relative_path)
        for environment, relative_path in settings["charts.files"].items()
        if environment in run["releaseEnvironments"]
    ]


def release_summary(environments):
    plural = ""

    if len(environments) > 1:
        plural = "s"

    return settings["jira.title"].format(
        environments=list_environments(environments), plural=plural)


def release_tags(asah_branch, faro_branch, version):

    # Each tag points at the commit its own build checked out, so only the
    # projects built in the run are tagged.

    tags = []

    if version and asah_branch:
        tags.append(
            (
                "asah", settings["asah.repository"],
                settings["asah.tag_format"].format(version=version),
            ))

    if version and faro_branch:
        tags.append(
            (
                "faro", settings["faro.repository"],
                settings["faro.tag_format"].format(version=version),
            ))

    return tags


def reuse_open_pull_request(run, stage_name, base_branch):

    # A reused ticket may already have an open PR from the attempt that did
    # not finish. It is reused only when it carries this run's tag suffix.

    if not run["reuseTicket"]:
        return None

    key = run["jiraTechnicalTask"]
    checkout = settings["charts.checkout"]

    pull_requests = json.loads(
        run_command(
            run, stage_name,
            [
                "gh", "pr", "list", "--base", base_branch, "--json",
                "headRefName,title,url", "--repo", settings["charts.repository"],
                "--search", f"{key} in:title", "--state", "open",
            ]))

    for pull_request in pull_requests:
        if not pull_request["title"].startswith(f"{key} "):
            continue

        run_command(
            run, stage_name,
            ["git", "-C", checkout, "fetch", "origin",
             pull_request["headRefName"]])

        matches = all(
            tag_suffix_line(run["tagSuffix"]) in run_command(
                run, stage_name,
                ["git", "-C", checkout, "show", f"FETCH_HEAD:{relative_path}"],
                check=False, quiet=True)
            for _, relative_path in release_files(run))

        if matches:
            append_log(
                run, stage_name,
                f"Reusing the open pull request {pull_request['url']}\n")
            add_stage_link(run, stage_name, "Pull request", pull_request["url"])

            return pull_request["url"]

        append_log(
            run, stage_name,
            f"The open pull request {pull_request['url']} has another tag "
            f"suffix, so a new one is opened. Close the old one by hand.\n")

    return None


def reuse_release_ticket(jira, run, task):
    summary = run["releaseSummary"]
    task_key = task["key"]

    update_run(run, jiraTask=task_key)
    add_stage_link(run, "jira", f"{task_key} (Task)", jira.issue_url(task_key))

    if task["technicalTasks"]:
        technical_task_key = task["technicalTasks"][0]
    else:
        technical_task_key = jira.request(
            "POST", "/rest/api/3/issue",
            {
                "fields": {
                    "assignee": {"accountId": jira.check()["accountId"]},
                    "components": [{"name": settings["jira.component"]}],
                    "issuetype": {"name": settings["jira.technical_task_type"]},
                    "parent": {"key": task_key},
                    "project": {"key": settings["jira.project"]},
                    "summary": summary,
                },
            })["key"]

        append_log(run, "jira", f"Created {technical_task_key}\n")

    update_run(run, jiraTechnicalTask=technical_task_key)
    add_stage_link(
        run, "jira", f"{technical_task_key} (Technical Task)",
        jira.issue_url(technical_task_key))

    append_log(
        run, "jira",
        f"Reusing {task_key} ({task['assignee']['displayName']}) and "
        f"{technical_task_key}\n")

    # The title says which environments the release covers, so it follows
    # this run.

    for key in (task_key, technical_task_key):
        current_summary = jira.request(
            "GET", f"/rest/api/3/issue/{key}?fields=summary")["fields"]["summary"]

        if current_summary != summary:
            jira.request(
                "PUT", f"/rest/api/3/issue/{key}", {"fields": {"summary": summary}})

            append_log(
                run, "jira",
                f"Renamed {key} from \"{current_summary}\" to \"{summary}\"\n")

    move_to_in_progress(jira, run, task_key, "jira.workflow.task_to_in_progress")
    move_to_in_progress(
        jira, run, technical_task_key,
        "jira.workflow.technical_task_to_in_progress")


def run_command(run, stage_name, args, check=True, cwd=None, quiet=False):
    append_log(run, stage_name, "$ " + " ".join(args[:4]) + "\n")

    completed = subprocess.run(
        args, capture_output=True, cwd=cwd, text=True, timeout=300)

    if quiet:
        append_log(run, stage_name, completed.stderr)
    else:
        append_log(run, stage_name, completed.stdout + completed.stderr)

    if check and completed.returncode:
        raise RuntimeError(
            f"{' '.join(args[:3])} failed with exit code {completed.returncode}")

    return completed.stdout


def run_local_stage(run, stage_name, function):
    if run["cancelRequested"]:
        update_stage(run, stage_name, status="aborted")

        return "ABORTED"

    if not run["stages"][stage_name]["startedAt"]:
        update_stage(run, stage_name, startedAt=time.time())

    update_stage(run, stage_name, status="running")

    try:
        result = function()
    except Exception as exception:
        append_log(run, stage_name, f"{exception}\n")
        update_stage(
            run, stage_name, finishedAt=time.time(), status="failed", why=None)

        raise

    if result == "ABORTED":
        update_stage(
            run, stage_name, finishedAt=time.time(), status="aborted", why=None)

        return "ABORTED"

    status = "success"

    if result == "SKIPPED":
        status = "skipped"

    update_stage(run, stage_name, finishedAt=time.time(), status=status, why=None)

    return "SUCCESS"


def run_stage(jenkins, run, stage_name, job, params):
    if run["cancelRequested"]:
        update_stage(run, stage_name, status="aborted")

        return "ABORTED"

    stage = run["stages"][stage_name]

    # After a restart, a stage that already reached Jenkins re-attaches to
    # its queue item or build instead of starting another one.

    build_url = stage["url"]
    queue_url = stage["queueUrl"]

    if not build_url and not queue_url:
        update_stage(run, stage_name, parameters=params, status="queued")

        queue_url = jenkins.trigger(job, params)

        update_stage(run, stage_name, queueUrl=queue_url)

    cancel_sent = False

    while build_url is None:
        try:
            item = with_retries(
                run, stage_name,
                lambda: jenkins.get_json(
                    queue_url + "api/json",
                    "cancelled,executable[number,url],why"))
        except urllib.error.HTTPError as exception:
            if exception.code == 404:
                raise RuntimeError(
                    f"Jenkins no longer knows the queue item {queue_url}, so "
                    f"check {job} in Jenkins and start a new run") from exception

            raise

        if item.get("cancelled"):
            update_stage(
                run, stage_name, finishedAt=time.time(), result="ABORTED",
                status="aborted")

            return "ABORTED"

        executable = item.get("executable")

        if executable:
            build_url = executable["url"]

            update_stage(
                run, stage_name, number=executable["number"],
                startedAt=time.time(), status="running", url=build_url, why=None)

            break

        update_stage(run, stage_name, status="queued", why=item.get("why"))

        if run["cancelRequested"] and not cancel_sent:
            queue_id = queue_url.rstrip("/").rsplit("/", 1)[1]

            jenkins.post(f"{jenkins.base_url}/queue/cancelItem", {"id": queue_id})

            cancel_sent = True

        time.sleep(POLL_SECONDS)

    update_stage(run, stage_name, status="running")

    stop_sent = False

    while True:
        offset = run["stages"][stage_name]["logOffset"]

        text, offset = with_retries(
            run, stage_name, lambda: jenkins.read_log(build_url, offset))

        append_log(run, stage_name, text)
        update_stage(run, stage_name, logOffset=offset)

        build = with_retries(
            run, stage_name,
            lambda: jenkins.get_json(build_url + "api/json", "building,result"))

        if not build.get("building") and build.get("result"):
            text, offset = with_retries(
                run, stage_name, lambda: jenkins.read_log(build_url, offset))

            append_log(run, stage_name, text)

            result = build["result"]
            status = "failed"

            if result == "SUCCESS":
                status = "success"
            elif result == "ABORTED":
                status = "aborted"

            update_stage(
                run, stage_name, finishedAt=time.time(), logOffset=offset,
                result=result, status=status)

            return result

        if run["cancelRequested"] and not stop_sent:
            jenkins.post(build_url + "stop")

            stop_sent = True

        time.sleep(POLL_SECONDS)


def set_tag_suffix(relative_path, tag_suffix, worktree):
    path = worktree / relative_path
    key = re.escape(settings["charts.tag_suffix_key"])

    text, count = re.subn(
        rf'^(\s*{key}:\s*)"[^"]*"', rf'\g<1>"{tag_suffix}"', path.read_text(),
        flags=re.MULTILINE)

    if count != 1:
        raise RuntimeError(
            f"Expected one {settings['charts.tag_suffix_key']} line in "
            f"{relative_path}, found {count}")

    path.write_text(text)


def sleep_unless_cancelled(run, seconds, halt=False):

    # With halt, also stops once another branch of a tree run failed: a step
    # waiting for a person or for Argo CD would otherwise keep the run open.

    def stop():
        return run["cancelRequested"] or (halt and run.get("halted"))

    deadline = time.time() + seconds

    while time.time() < deadline:
        if stop():
            return True

        time.sleep(min(1, seconds))

    return bool(stop())


def stage_done(run, stage_name):

    # A step finished before a restart is not repeated. A step skipped by a
    # decision (e.g. keeping the tickets open) counts as done; it logged it.

    stage = run["stages"][stage_name]

    # A build only counts once what was read from its log is saved too.

    if stage_name == "asahBuild" and not run["asahImages"]:
        return False

    if stage_name.startswith("faroBuild"):
        suffix = stage_name.partition("-")[2]

        if run.get("faroSharedBuilds"):
            if not run.get("faroBuilds", {}).get(suffix):
                return False
        elif not faro_image(run, suffix or run.get("environment")):
            return False

    if stage["status"] == "success":
        return True

    return stage["status"] == "skipped" and bool(stage["log"])


def tag_suffix_line(tag_suffix):
    return f'{settings["charts.tag_suffix_key"]}: "{tag_suffix}"'


def update_run(run, **changes):
    with runs_lock:
        run.update(changes)

    on_change(run)


def update_stage(run, stage_name, **changes):
    with runs_lock:
        run["stages"][stage_name].update(changes)

    on_change(run)


def validate_run(payload, verify_tags):
    environments = settings.environments()
    faro_environments = settings.faro_environments()

    mode = payload.get("mode") or "release"

    if mode not in MODES:
        raise ValueError(f"Unknown mode {mode}")

    asah_branch = str(payload.get("asahBranch", "")).strip()

    if asah_branch and not BRANCH_PATTERN.fullmatch(asah_branch):
        raise ValueError("Invalid Asah branch name")

    release_environments = payload.get("releaseEnvironments") or []

    if not isinstance(release_environments, list) or not set(
            release_environments) <= set(environments):

        raise ValueError(
            f"Release environments must be among {', '.join(environments)}")

    release_environments = [
        environment for environment in environments
        if environment in release_environments
    ]

    tag_suffix = None

    if release_environments and not asah_branch:
        tag_suffix = str(payload.get("tagSuffix", "")).strip()

        if not settings.compiled("asah.tag_suffix_pattern").fullmatch(
                tag_suffix):

            raise ValueError(
                "Enter an Asah branch to build, or the tag suffix of an "
                "existing Asah build to release")

    faro_branch = str(payload.get("faroBranch", "")).strip()

    if faro_branch and not BRANCH_PATTERN.fullmatch(faro_branch):
        raise ValueError("Invalid Faro branch name")

    selected = payload.get("faroEnvironments")

    if selected is None and payload.get("environment"):
        selected = [payload["environment"]]

    selected = selected or []

    if not isinstance(selected, list) or not set(selected) <= set(faro_environments):
        raise ValueError(
            f"Faro environments must be among {', '.join(faro_environments)}")

    if faro_branch and not selected:
        raise ValueError("Pick at least one Faro environment")

    selected_faro_environments = []

    if faro_branch:
        selected_faro_environments = [
            environment for environment in faro_environments if environment in selected
        ]

    if not (asah_branch or faro_branch or release_environments):
        raise ValueError("Nothing to do: fill in at least one step")

    # The Asah and Faro tabs only start their own part of the flow.

    if mode == "asah" and faro_branch:
        raise ValueError("The Asah tab does not build Faro")

    if mode == "faro" and (asah_branch or release_environments):
        raise ValueError("The Faro tab does not build or release Asah")

    composer = bool(payload.get("composer"))

    if composer:
        validate_composer(release_environments)

    dataflow_templates = payload.get("dataflow") or []

    if dataflow_templates:
        dataflow_templates = validate_dataflow(release_environments, dataflow_templates)

    version = str(payload.get("version", "")).strip() or None

    if version:
        if not VERSION_PATTERN.fullmatch(version):
            raise ValueError("The version must look like 1.2.3")

        if not (asah_branch or faro_branch):
            raise ValueError(
                "Tags point at the commits built in the run, so enter an "
                "Asah or Faro branch to tag")

        if not is_prd_release(
                faro_branch, selected_faro_environments, release_environments):
            prd_environment = settings["release.prd_environment"]

            raise ValueError(
                f"Tags are only created for {prd_environment.upper()} "
                f"releases, so leave the version empty")

        if verify_tags:
            verify_tags_available(asah_branch, faro_branch, version)

    return (
        asah_branch, selected_faro_environments, faro_branch,
        release_environments, tag_suffix, version, composer, mode, dataflow_templates)


def validate_dataflow(release_environments, templates):
    from . import dataflow

    if not dataflow.enabled(settings):
        raise ValueError("The team settings have no Dataflow setup")

    if not release_environments:
        raise ValueError(
            "The Dataflow templates go to the released environments, so pick at least one")

    problem = next(iter(dataflow.problems(settings)), None)

    if problem:
        raise ValueError(problem)

    return dataflow.validate_templates(release_environments, templates)


def validate_composer(release_environments):
    from . import composer

    if not composer.composer_enabled(settings):
        raise ValueError("The team settings have no Composer setup")

    if not release_environments:
        raise ValueError(
            "The Composer DAGs go to the released environments, so pick at least one")

    missing = [
        environment for environment in release_environments
        if environment not in (settings["gcloud.environments"] or {})
    ]

    if missing:
        raise ValueError(
            f"No gcloud configurations for {', '.join(missing)} in the team settings")

    problem = next(iter(composer.problems(settings)), None)

    if problem:
        raise ValueError(problem)


def verify_charts_change(run):

    # Fail before creating Jira tickets when the charts PR would be empty.

    checkout = settings["charts.checkout"]
    develop_branch = settings["charts.develop_branch"]

    run_command(run, "jira", ["git", "-C", checkout, "fetch", "origin", develop_branch])

    pending = [
        environment for environment, relative_path in release_files(run)
        if tag_suffix_line(run["tagSuffix"]) not in run_command(
            run, "jira",
            ["git", "-C", checkout, "show", f"origin/{develop_branch}:{relative_path}"],
            quiet=True)
    ]

    if not pending:
        raise RuntimeError(
            f"The tag suffix is already {run['tagSuffix']} on {develop_branch} "
            f"for every selected environment, so there is nothing to release")


def verify_tags_available(asah_branch, faro_branch, version):

    # Checked before the run starts so an existing tag does not surface only
    # after hours of builds, merges and deploys.

    for _, repository, tag in release_tags(asah_branch, faro_branch, version):
        completed = subprocess.run(
            ["gh", "api", f"repos/{repository}/git/ref/tags/{tag}"],
            capture_output=True, text=True, timeout=60)

        if completed.returncode == 0:
            raise ValueError(f"Tag {tag} already exists in {repository}")

        if "Not Found" not in completed.stdout + completed.stderr:
            raise ValueError(
                f"Could not check tag {tag} in {repository}: "
                f"{completed.stderr.strip()}")


def check_states(pull_request, names):

    # SUCCESS, FAILED, PENDING or MISSING for each check the team requires.
    # Check runs report a conclusion once done; commit statuses a state.

    found = {}

    for check in pull_request.get("statusCheckRollup") or []:
        name = check.get("name") or check.get("context")
        state = (check.get("conclusion") or check.get("state") or "").upper()

        if state in ("SUCCESS", "NEUTRAL", "SKIPPED"):
            found[name] = "SUCCESS"
        elif state in ("ACTION_REQUIRED", "CANCELLED", "ERROR", "FAILURE", "STARTUP_FAILURE",
                       "TIMED_OUT"):

            found[name] = "FAILED"
        else:
            found[name] = "PENDING"

    return {name: found.get(name, "MISSING") for name in names}


def merge_method(stage_name):
    if stage_name == "developMerge":
        return settings["charts.merge.develop_method"]

    return settings["charts.merge.production_method"]


def merge_when_ready(pull_request, pull_request_url, run, stage_name):

    # Merges once the pull request is approved and the team's checks pass,
    # and returns what it is waiting for, to be logged when it changes.

    if pull_request.get("reviewDecision") != "APPROVED":
        return "waiting for an approval"

    states = check_states(pull_request, settings["charts.merge.checks"] or [])

    failed = [name for name, state in states.items() if state == "FAILED"]

    if failed:
        return (
            f"not merging: {', '.join(failed)} failed; re-run it or merge by hand")

    pending = [name for name, state in states.items() if state != "SUCCESS"]

    if pending:
        return f"approved, waiting for {', '.join(pending)}"

    method = merge_method(stage_name)

    completed = subprocess.run(
        ["gh", "pr", "merge", pull_request_url, f"--{method}"],
        capture_output=True, text=True, timeout=120)

    if completed.returncode:
        return f"could not merge yet: {completed.stderr.strip()[:300]}"

    return f"approved and checks passed, merged ({method})"


def wait_for_merge(run, stage_name, pull_request_url):
    auto_merge = bool(merge_method(stage_name))

    why = "Waiting for the pull request to be merged"

    if auto_merge:
        checks = " and ".join(settings["charts.merge.checks"] or [])

        why = "Waiting for an approval"

        if checks:
            why += f" and {checks}"

        why += "; it is then merged automatically"

    add_stage_link(run, stage_name, "Pull request", pull_request_url)
    update_stage(run, stage_name, why=why)

    last_merge_note = None
    last_state = None

    while True:
        completed = subprocess.run(
            [
                "gh", "pr", "view", pull_request_url, "--json",
                "commits,mergeCommit,number,reviewDecision,state,statusCheckRollup",
            ],
            capture_output=True, text=True, timeout=120)

        # A failed check (network, GitHub hiccup) is logged and retried on
        # the next poll instead of failing a wait that can last hours.

        if completed.returncode:
            append_log(run, stage_name, completed.stderr)
        else:
            pull_request = json.loads(completed.stdout)

            review = (pull_request.get("reviewDecision") or
                      "REVIEW_REQUIRED").lower().replace("_", " ")
            state = pull_request["state"]

            if (review, state) != last_state:
                append_log(
                    run, stage_name,
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} {state.lower()} "
                    f"({review})\n")

                last_state = (review, state)

            if state == "MERGED":
                update_run(
                    run,
                    **{
                        stage_name: {
                            "commit": pull_request["mergeCommit"]["oid"],
                            "commits": [
                                commit["oid"]
                                for commit in pull_request["commits"]
                            ],
                            "number": pull_request["number"],
                        },
                    })

                return "SUCCESS"

            if state == "CLOSED":
                raise RuntimeError(
                    f"{pull_request_url} was closed without being merged")

            if auto_merge:
                note = merge_when_ready(pull_request, pull_request_url, run, stage_name)

                if note != last_merge_note:
                    append_log(
                        run, stage_name,
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} {note}\n")

                    last_merge_note = note

                # Right after merging, look at the pull request again
                # instead of waiting a whole poll.

                if note.endswith(f"merged ({merge_method(stage_name)})"):
                    continue

        if sleep_unless_cancelled(run, MERGE_POLL_SECONDS, halt=True):
            return "ABORTED"


def rollout_steps(run):

    # Runs saved before the split have one rollout for all environments
    # (runs saved later also get a skipped "rollout" stage when loaded).

    split = any(name.startswith("rollout-") for name in run["stages"])

    if not split:
        return [
            (
                "rollout", bool(run["releaseEnvironments"]),
                lambda: run_local_stage(
                    run, "rollout",
                    lambda: wait_for_rollout_confirmation(run, run["releaseEnvironments"], "rollout")),
            ),
        ]

    return [
        (
            f"rollout-{environment}", True,
            lambda environment=environment: run_local_stage(
                run, f"rollout-{environment}",
                lambda: wait_for_rollout_confirmation(run, [environment], f"rollout-{environment}")),
        )
        for environment in run["releaseEnvironments"]
    ]


def rollout_confirmed(run, stage_name):
    if stage_name == "rollout":
        return run["rolloutConfirmed"]

    return run.get("rolloutsConfirmed", {}).get(stage_name.partition("-")[2], False)


def wait_for_rollout_confirmation(run, environments, stage_name):

    # Environments with an Argo CD token are checked automatically; the
    # others wait for a person to confirm. The button always works, as an
    # override.

    from . import argocd

    search = settings["argocd.search"]
    instances = {}

    for environment in environments:
        url = settings["argocd.urls"][environment].rstrip("/")

        instances.setdefault(url, []).append(environment.upper())

    for url, names in instances.items():
        add_stage_link(
            run, stage_name, f"Argo CD ({', '.join(names)})",
            f"{url}/applications?search={urllib.parse.quote(search)}")

    automatic = [
        environment for environment in environments
        if environment in argocd.watched(settings)
    ]
    manual = [
        environment for environment in environments
        if environment not in automatic
    ]

    manual_note = ""

    if manual:
        manual_note = (
            f"Check in Argo CD that the {search} apps for {list_environments(manual)} "
            f"run tag suffix {run['tagSuffix']} and are Healthy, then confirm.")

    if not automatic:
        update_stage(run, stage_name, why=manual_note)

    last_summaries = {}

    while not rollout_confirmed(run, stage_name):
        if automatic:
            states = rollout_states(automatic, run)

            for name, status, summary in states:
                if last_summaries.get(name) != (status, summary):
                    append_log(
                        run, stage_name,
                        f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name}: {status}, {summary}\n")

                    last_summaries[name] = (status, summary)

            failed = [(name, summary) for name, status, summary in states if status == "failed"]

            if failed:
                raise RuntimeError(
                    "The rollout failed in Argo CD: " +
                    "; ".join(f"{name}: {summary}" for name, summary in failed))

            pending = [name for name, status, _ in states if status not in ("done", "superseded")]

            if not pending and not manual:
                for name, status, summary in states:
                    if status == "superseded":
                        append_log(run, stage_name, f"Warning: {name} was {summary}\n")

                append_log(
                    run, stage_name,
                    f"{time.strftime('%Y-%m-%d %H:%M:%S')} rollout confirmed by Argo CD\n")

                return None

            checked = f"Checking {list_environments(automatic)} in Argo CD"

            if pending:
                checked += f": waiting for {', '.join(pending)}"
            else:
                checked += ": done"

            update_stage(
                run, stage_name,
                why=" ".join(part for part in (checked + ".", manual_note) if part))

        if sleep_unless_cancelled(run, ARGOCD_POLL_SECONDS, halt=True):
            return "ABORTED"

    append_log(
        run, stage_name, f"{time.strftime('%Y-%m-%d %H:%M:%S')} rollout confirmed by hand\n")


def rollout_states(environments, run):
    from . import argocd

    states = []

    for environment in environments:
        merge = run["developMerge"]

        if environment in settings["charts.production.environments"]:
            merge = run["productionMerge"]

        try:
            states.extend(
                argocd.environment_states(environment, merge["commit"], run, settings))
        except (OSError, ValueError, urllib.error.URLError) as exception:
            states.append(
                (environment.upper(), "waiting", f"Argo CD is not reachable ({exception})"))

    return states


def with_retries(run, stage_name, function):

    # Watching a build means polling Jenkins for minutes, so one refused or
    # dropped request is retried instead of failing the whole run. While
    # Jenkins or its gateway is down (5xx, 429 or no connection) the build
    # keeps running there, so those are retried for JENKINS_OUTAGE_SECONDS;
    # other errors only POLL_RETRIES times.

    attempt = 0
    failing_since = None

    while True:
        attempt += 1

        try:
            result = function()
        except urllib.error.HTTPError as exception:
            if exception.code == 404:
                raise

            body = exception.read().decode("utf-8", "replace")
            detail = " ".join(re.sub(r"<[^>]+>", " ", body).split())[:300]
            failure = exception
            problem = f"{exception} {detail}"

            # Right after a restart Jenkins denies everyone Overall/Read until
            # its security setup is loaded again.

            outage = exception.code in TRANSIENT_HTTP_CODES or (
                exception.code == 403 and "Overall/Read" in detail)
        except (OSError, urllib.error.URLError) as exception:
            failure = exception
            problem = str(exception)
            outage = True
        else:
            if failing_since is not None:
                append_log(
                    run, stage_name,
                    f"[LDP Releaser] Jenkins answers again after "
                    f"{format_duration(time.time() - failing_since)}\n")
                update_stage(run, stage_name, why=None)

            return result

        if failing_since is None:
            failing_since = time.time()

        down_for = time.time() - failing_since

        if (outage and down_for >= JENKINS_OUTAGE_SECONDS) or (
                not outage and attempt >= POLL_RETRIES):

            raise failure

        if outage:
            append_log(
                run, stage_name,
                f"[LDP Releaser] Jenkins request failed ({problem}), retrying for up to "
                f"{format_duration(JENKINS_OUTAGE_SECONDS)}; the build keeps running in "
                f"Jenkins\n")
            update_stage(
                run, stage_name,
                why=f"Jenkins is not answering ({problem.split(':')[0]}) for "
                    f"{format_duration(down_for)}; still watching the build")
        else:
            append_log(
                run, stage_name,
                f"[LDP Releaser] Jenkins request failed ({problem}), "
                f"retrying ({attempt}/{POLL_RETRIES - 1})\n")

        time.sleep(min(30, POLL_SECONDS * attempt))
