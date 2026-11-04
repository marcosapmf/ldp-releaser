"""The API for other tools, such as a dashboard: the same information and
actions as the page under /api/v1, behind a token instead of the page's
same-site checks."""

import hmac
import secrets

from . import config, fixes

TOKEN_FILE = config.CONFIG_DIR / "api-token"

WAITING_ACTIONS = {"close-tickets", "confirm-rollout", "gcloud-login", "keep-tickets", "relaunch-dataflow"}


def allowed_origin(settings, origin):
    return bool(origin) and origin in (settings["api.allowed_origins"] or [])


def attention(runs, settings):

    # Everything that waits for a person, newest run first, with the call
    # that answers it.

    items = []

    for run in sorted(runs, key=lambda run: run["createdAt"], reverse=True):
        if run["finishedAt"]:
            continue

        waiting = [action for action in run_actions(run, settings) if action["kind"] in WAITING_ACTIONS]

        for stage in dict.fromkeys(action["stage"] for action in waiting):
            stage_actions = [action for action in waiting if action["stage"] == stage]

            items.append({
                "actions": stage_actions,
                "kind": stage_actions[0]["kind"],
                "links": run["stages"][stage]["links"],
                "runId": run["id"],
                "runTitle": run_title(run),
                "since": run["stages"][stage]["startedAt"],
                "stage": stage,
                "why": run["stages"][stage]["why"],
            })

    return items


def action(kind, label, path, stage=None, body=None):
    return {"body": body, "kind": kind, "label": label, "method": "POST", "path": path, "stage": stage}


def authorized(header):
    scheme, _, token = (header or "").partition(" ")

    return scheme.lower() == "bearer" and hmac.compare_digest(token.strip(), api_token())


def api_token():

    # Created on the first start, readable only by you.

    if not TOKEN_FILE.exists():
        config.write_private(TOKEN_FILE, secrets.token_urlsafe(32) + "\n")

    return TOKEN_FILE.read_text().strip()


def run_actions(run, settings):

    # The buttons the page shows for this run, as API calls.

    base = f"/api/v1/runs/{run['id']}"
    actions = []

    if run["finishedAt"] is None:
        actions.append(action("cancel", "Cancel", f"{base}/cancel"))

    for name in run.get("stageOrder", list(run["stages"])):
        stage = run["stages"][name]
        running = stage["status"] == "running"

        if name == "rollout" and running and not run["rolloutConfirmed"]:
            actions.append(action("confirm-rollout", "Confirm rollout", f"{base}/confirm-rollout", name))

        if (name == "closeTickets" and running and not run["prdRelease"] and
                not run["ticketsDecision"]):

            actions.append(action("close-tickets", "Close tickets", f"{base}/close-tickets", name))
            actions.append(action("keep-tickets", "Keep open", f"{base}/keep-tickets", name))

        if ((name == "dataflow" or name.startswith("dataflow-")) and running and
                run.get("dataflowRelaunch") and not run["dataflowRelaunch"]["approved"]):

            actions.append(
                action("relaunch-dataflow", "Drain and relaunch", f"{base}/relaunch-dataflow", name))

        if running and stage.get("needsGcloudLogin"):
            actions.append(action("gcloud-login", "Log in to gcloud", "/api/v1/gcloud/login", name))

        if stage["status"] == "failed" and stage["url"]:
            actions.append(
                action("explain-failure", "Show failure", f"{base}/stages/{name}/failure", name))

            fix = stage.get("fix")

            if (name == "asahBuild" and fixes.enabled(settings) and
                    (stage.get("failure") or {}).get("fixes") and
                    (not fix or fix["status"] == "failed")):

                actions.append(action("apply-fixes", "Apply fixes", f"{base}/stages/asahBuild/fix", name))

    return actions


def run_kind(run):
    return run.get("kind") or "release"


def run_title(run):
    if run.get("releaseSummary"):
        return run["releaseSummary"]

    environments = ", ".join(
        environment.upper()
        for environment in (
            run.get("releaseEnvironments") or run.get("environments") or
            run.get("faroEnvironments") or []))

    # Release runs started from the Faro or Asah tab are named after it.

    name = run_kind(run).capitalize()

    if name == "Release" and run.get("mode") in ("asah", "faro"):
        name = run["mode"].capitalize()

    return " ".join(part for part in (name, environments) if part)


def summary(run, settings):

    # A run without its logs, which can be long, plus what can be done with it.

    return {
        **run,
        "actions": run_actions(run, settings),
        "kind": run_kind(run),
        "stages": {name: stage_summary(stage) for name, stage in run["stages"].items()},
        "title": run_title(run),
    }


def stage_summary(stage):
    summary = {key: value for key, value in stage.items() if key not in ("failure", "log")}

    if stage.get("fix"):
        summary["fix"] = {key: value for key, value in stage["fix"].items() if key != "log"}

    return summary
