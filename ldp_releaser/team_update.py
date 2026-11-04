"""Notices a newer team file in 1Password and, when asked, replaces the local
copy and restarts so the new settings apply."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
import tomllib
from datetime import datetime
from pathlib import Path

from . import config, release, store

# 1Password may ask to authorize the CLI, so the check only runs while the
# page is open, and at most this often.

CHECK_SECONDS = float(os.environ.get("LDP_TEAM_CHECK_SECONDS", 3600))

state = {"changes": [], "checkedAt": 0, "problems": [], "status": "unknown"}
state_lock = threading.Lock()


def apply(settings):

    # Returns the reason the update cannot happen, or None once the restart
    # is on its way.

    with release.runs_lock:
        for run in release.runs.values():
            stage = store.unresumable_stage(run)

            if stage:
                return (
                    f"Run {run['id']} is in the {stage} step, which a restart "
                    f"would stop halfway. Update once it is past that step.")

    try:
        text = fetch(settings)
    except RuntimeError as exception:
        return str(exception)

    changes, problems = compare(text)

    if not changes:
        return "Your team settings are already the same as in 1Password"

    try:
        if local_is_newer(settings):
            return (
                "Your team settings were changed after the version in 1Password, "
                "so updating would undo those changes. Upload them instead.")
    except RuntimeError as exception:
        return str(exception)

    if problems:
        return "The file in 1Password cannot be used: " + "; ".join(problems)

    config.write_private(config.TEAM_FILE, text)

    threading.Thread(daemon=True, target=restart).start()

    return None


def changed_keys(old, new, prefix=""):

    # Only the names of the settings, never their values, which can be secret.

    keys = []

    for key in sorted(set(old) | set(new)):
        name = prefix + key

        if isinstance(old.get(key), dict) and isinstance(new.get(key), dict):
            keys += changed_keys(old[key], new[key], f"{name}.")
        elif old.get(key) != new.get(key):
            keys.append(name)

    return keys


def check(settings):
    with state_lock:
        if (state["status"] == "checking" or
                time.time() - state["checkedAt"] < CHECK_SECONDS):

            return

        state["status"] = "checking"

    try:
        changes, problems = compare(fetch(settings))

        if not changes:
            status = "current"
        elif local_is_newer(settings):
            status = "local-newer"
        elif problems:
            status = "invalid"
        else:
            status = "available"
    except RuntimeError:
        changes, problems, status = [], [], "unavailable"

    with state_lock:
        state.update(
            changes=changes, checkedAt=time.time(), problems=problems, status=status)


def compare(text):
    try:
        new = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exception:
        return ["the whole file"], [f"it is not valid TOML ({exception})"]

    return changed_keys(config.read_toml(config.TEAM_FILE) or {}, new), config.validate_team(new)


def current(settings):
    if not enabled(settings):
        return {"changes": [], "problems": [], "status": "off"}

    with state_lock:
        return {
            **state, "item": settings["team.onepassword_item"],
            "teamFile": str(config.TEAM_FILE), "vault": settings["team.onepassword_vault"],
        }


def enabled(settings):
    return bool(settings["team.onepassword_item"]) and shutil.which("op") is not None


def fetch(settings):
    command = ["op", "document", "get", settings["team.onepassword_item"]]

    if settings["team.onepassword_vault"]:
        command += ["--vault", settings["team.onepassword_vault"]]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exception:
        raise RuntimeError(f"1Password could not fetch the team file ({exception})")

    if completed.returncode:
        raise RuntimeError(
            f"1Password could not fetch the team file ({completed.stderr.strip()[:200]})")

    return completed.stdout


def local_is_newer(settings):

    # A different file in 1Password is not always a newer one: the owner may
    # have edited the local copy and not uploaded it yet.

    command = ["op", "item", "get", settings["team.onepassword_item"], "--format", "json"]

    if settings["team.onepassword_vault"]:
        command += ["--vault", settings["team.onepassword_vault"]]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
        updated_at = datetime.fromisoformat(json.loads(completed.stdout)["updated_at"])
    except (KeyError, OSError, ValueError, subprocess.TimeoutExpired) as exception:
        raise RuntimeError(
            f"1Password could not tell when the team file was updated ({exception})")

    return config.TEAM_FILE.stat().st_mtime > updated_at.timestamp()


def restart():

    # Lets the response reach the page first. The new process resumes the
    # unfinished runs, as after any restart.

    time.sleep(0.5)

    store.flush()

    os.chdir(Path(__file__).resolve().parent.parent)
    os.execv(sys.executable, [sys.executable, "-m", "ldp_releaser"])


def start_check(settings):
    if enabled(settings):
        threading.Thread(args=(settings,), daemon=True, target=check).start()
