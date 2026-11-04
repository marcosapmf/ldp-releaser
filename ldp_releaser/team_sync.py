"""Saves the team file for the setup page, from 1Password or from a file on
this machine, and restarts LDP Releaser once it is saved."""

import os
import subprocess
import sys
import time
import tomllib
from pathlib import Path

from . import config


def fetch(item, vault):
    command = ["op", "document", "get", item]

    if vault:
        command += ["--vault", vault]

    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exception:
        raise RuntimeError(f"1Password could not fetch the team file ({exception})")

    if completed.returncode:
        raise RuntimeError(
            f"1Password could not fetch the team file ({completed.stderr.strip()[:200]})")

    return completed.stdout


def problems():

    # What the team file lacks, or None when it is complete.

    team = config.read_toml(config.TEAM_FILE)

    if team is None:
        return [f"team settings not found at {config.TEAM_FILE}"]

    return config.validate_team(team) or None


def restart():

    # Lets the response reach the page first.

    time.sleep(0.5)

    os.chdir(Path(__file__).resolve().parent.parent)
    os.execv(sys.executable, [sys.executable, "-m", "ldp_releaser"])


def save(personal, text, item, vault, source):

    # Returns why the file cannot be used, or None once it is saved. source
    # names where it came from in the messages.

    try:
        team_problems = config.validate_team(tomllib.loads(text))
    except tomllib.TOMLDecodeError as exception:
        return f"{source} is not valid TOML ({exception})"

    if team_problems:
        return (
            f"{source} lacks settings this version needs: " + "; ".join(team_problems) +
            ". Ask the owner of the team file to add them.")

    config.write_private(config.TEAM_FILE, text)

    # The next sync, and --setup, offer the same item; a file from disk has
    # none.

    config.set_value(personal, "team.onepassword_item", item)
    config.set_value(personal, "team.onepassword_vault", vault)
    config.write_personal(personal)

    return None


def sync(personal, item, vault):

    # Returns why the file in 1Password cannot be used, or None once it is
    # saved.

    try:
        text = fetch(item, vault)
    except RuntimeError as exception:
        return str(exception)

    return save(personal, text, item, vault, "The file in 1Password")


def use_file(personal, path):

    # Returns why the file at path cannot be used, or None once it is saved.

    path = Path(path).expanduser()

    if not path.is_file():
        return f"{path} does not exist"

    return save(personal, path.read_text(), None, None, str(path))
