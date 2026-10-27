"""Checks a rollout in Argo CD with read-only API tokens.

For each application of a released environment, the rollout is done when the
charts merge is part of what Argo CD synced, the sync hooks (such as the
database upgrade job) succeeded, every image of the project carries the new
tag suffix, and the application is Synced, Succeeded and Healthy.
"""

import json
import subprocess
import urllib.error
import urllib.parse
import urllib.request

FAILED_PHASES = ("Error", "Failed")


def branch_for(environment, settings):

    # Environments that need the cherry-pick PR are deployed from the
    # production branch; the others from the develop branch.

    if environment in settings["charts.production.environments"]:
        return settings["charts.production_branch"]

    return settings["charts.develop_branch"]


def fetch(settings, url, token):
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"})

    with urllib.request.urlopen(request, timeout=60) as response:
        return json.load(response)


def git(settings, *args):
    return subprocess.run(
        ["git", "-C", settings["charts.checkout"], *args], capture_output=True,
        text=True, timeout=300)


def includes(settings, merge_commit, revision):

    # Whether the charts commit Argo CD synced contains the release's merge.
    # The live commit may be newer than what the local clone has, so fetch
    # once when git does not know it yet.

    if not revision:
        return False

    if git(settings, "cat-file", "-e", f"{revision}^{{commit}}").returncode:
        git(
            settings, "fetch", "--quiet", "origin", settings["charts.develop_branch"],
            settings["charts.production_branch"])

    return git(settings, "merge-base", "--is-ancestor", merge_commit, revision).returncode == 0


def project_images(application, settings):
    return [
        image for image in application["status"].get("summary", {}).get("images", [])
        if settings["argocd.image_name"] in image
    ]


def state(application, environment, merge_commit, run, settings, tree):

    # One application's rollout state: (status, summary). The status is
    # done, superseded, failed or waiting.

    status = application.get("status", {})
    operation = status.get("operationState") or {}
    sync = status.get("sync") or {}
    health = (status.get("health") or {}).get("status")
    tag_suffix = run["tagSuffix"]

    if not includes(settings, merge_commit, sync.get("revision")):
        return "waiting", (
            f"Argo CD has not synced the charts merge yet (live charts commit "
            f"{(sync.get('revision') or 'unknown')[:7]}, sync {sync.get('status')})")

    images = project_images(application, settings)
    stale = [image for image in images if not image.endswith(tag_suffix)]

    # The operation that applied our merge: a failed hook or sync there is
    # a failed rollout, not something to wait for.

    operation_ours = includes(
        settings, merge_commit, (operation.get("syncResult") or {}).get("revision"))

    hooks = [
        resource for resource in (operation.get("syncResult") or {}).get("resources", [])
        if resource.get("hookType")
    ]

    if operation_ours:
        for hook in hooks:
            if hook.get("hookPhase") in FAILED_PHASES:
                return "failed", (
                    f"{hook.get('kind')} {hook.get('name')} ({hook.get('hookType')} hook) "
                    f"failed: {hook.get('message') or 'no message'}")

        if operation.get("phase") in FAILED_PHASES:
            return "failed", f"The sync failed: {operation.get('message') or 'no message'}"

    if stale and images and not any(image.endswith(tag_suffix) for image in images):
        live_suffix = values_tag_suffix(environment, settings, sync.get("revision"))

        if live_suffix and live_suffix != tag_suffix:
            return "superseded", (
                f"superseded: the live charts commit {sync['revision'][:7]} sets "
                f"{live_suffix}")

    hook_notes = [
        f"{hook.get('name')} {hook.get('hookPhase', '').lower()}" for hook in hooks
        if operation_ours
    ]

    if operation.get("phase") == "Running":
        return "waiting", "syncing" + with_notes(hook_notes)

    unhealthy = unhealthy_pods(tree)

    if stale:
        return "waiting", (
            f"{len(images) - len(stale)} of {len(images)} images on {tag_suffix}" +
            with_notes(hook_notes + unhealthy))

    if sync.get("status") != "Synced" or operation.get("phase") not in (None, "Succeeded") or \
            health != "Healthy":

        return "waiting", (
            f"sync {sync.get('status')}, last operation {operation.get('phase')}, "
            f"health {health}" + with_notes(hook_notes + unhealthy))

    return "done", (
        f"{len(images)} images on {tag_suffix}, Synced, Healthy" + with_notes(hook_notes))


def unhealthy_pods(tree):
    notes = []

    for node in (tree or {}).get("nodes", []):
        if node.get("kind") != "Pod":
            continue

        health = (node.get("health") or {}).get("status")

        if health in (None, "Healthy"):
            continue

        info = {item.get("name"): item.get("value") for item in node.get("info", [])}

        notes.append(
            f"pod {node.get('name')} {health} ({info.get('Status Reason', '?')})")

    return notes


def values_tag_suffix(environment, settings, revision):

    # The tag suffix the charts commit sets for this environment.

    if not revision:
        return None

    completed = git(settings, "show", f"{revision}:{settings['charts.files'][environment]}")

    if completed.returncode:
        return None

    key = settings["charts.tag_suffix_key"]

    for line in completed.stdout.splitlines():
        stripped = line.strip()

        if stripped.startswith(f"{key}:"):
            return stripped.split(":", 1)[1].strip().strip('"').strip("'")

    return None


def watched(settings):

    # Environments whose rollout can be checked automatically: they have a
    # token and applications in the team settings.

    applications = settings["argocd.applications"] or {}
    tokens = settings["argocd.tokens"] or {}

    return {
        environment for environment in applications
        if tokens.get(environment) and applications[environment]
    }


def with_notes(notes):
    if not notes:
        return ""

    return " (" + "; ".join(notes) + ")"


def environment_states(environment, merge_commit, run, settings):

    # (application, status, summary) for every application of one
    # environment, from Argo CD's application list and resource trees.

    base = settings["argocd.urls"][environment].rstrip("/")
    token = settings["argocd.tokens"][environment]

    listing = fetch(
        settings,
        f"{base}/api/v1/applications?" + urllib.parse.urlencode(
            {"projects": settings["argocd.project"] or settings["argocd.search"]}),
        token)

    by_name = {item["metadata"]["name"]: item for item in listing.get("items") or []}

    results = []

    for name in settings["argocd.applications"][environment]:
        application = by_name.get(name)

        if application is None:
            results.append((name, "waiting", "not found in Argo CD"))

            continue

        try:
            tree = fetch(
                settings, f"{base}/api/v1/applications/{urllib.parse.quote(name)}/resource-tree",
                token)
        except (OSError, urllib.error.URLError):
            tree = None

        status, summary = state(application, environment, merge_commit, run, settings, tree)

        results.append((name, status, summary))

    return results
