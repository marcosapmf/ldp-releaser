#!/usr/bin/env python3
"""Fails when a file in the repository contains a secret or a value from the
private settings.

This repository is public, so it must not name the team's internal systems.
Instead of listing those names here (which would publish them), the check
reads the real team and personal settings from this machine and rejects any
of their values, on top of generic secret patterns.

    scripts/check_leaks.py            check every tracked file
    scripts/check_leaks.py --staged   check what is about to be committed
"""

import argparse
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ldp-releaser"

GENERIC_PATTERNS = {
    "Atlassian API token": re.compile(r"ATATT[0-9A-Za-z_\-=]{20,}"),
    "GitHub token": re.compile(r"\bgh[oprsu]_[A-Za-z0-9]{20,}"),
    "Jenkins API token": re.compile(r"\b11[0-9a-f]{32}\b"),
    "Slack webhook": re.compile(r"hooks\.slack\.com/(?:services|triggers|workflows)/\S+"),
    "internal host name": re.compile(r"\b[\w-]+\.internal\.[a-z][\w.-]*\.[a-z]{2,}\b"),
    "private key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
}

# Setting values that are ordinary words, not private details.

COMMON_VALUES = {
    "closed", "completed", "develop", "discarded", "in progress", "production",
    "selected for development", "tagsuffix", "task", "technical task",
}

PUBLIC_HOSTS = {"github.com", "id.atlassian.com"}

MINIMUM_LENGTH = 6


def private_values():
    values = set()

    for name in ("ldp-releaser.team.toml", "config.toml"):
        path = CONFIG_DIR / name

        if not path.exists():
            continue

        with path.open("rb") as file:
            collect(tomllib.load(file), values)

    return {
        value for value in values
        if len(value) >= MINIMUM_LENGTH and value.lower() not in COMMON_VALUES
    }


def collect(node, values):
    if isinstance(node, dict):
        for value in node.values():
            collect(value, values)
    elif isinstance(node, list):
        for value in node:
            collect(value, values)
    elif isinstance(node, str):
        add_value(node, values)


def add_value(value, values):

    # Placeholders like {version} and regular expressions are split into the
    # literal pieces that would identify the setup (hosts, repositories).

    host = re.match(r"https?://([^/:]+)", value)

    if host and host.group(1) not in PUBLIC_HOSTS:
        values.add(host.group(1))

    if "{" in value or "\\" in value or "(" in value:
        for piece in re.findall(r"[\w.-]+(?:/[\w.-]+)+|[\w-]+(?:\.[\w-]+){2,}", value):
            if piece not in PUBLIC_HOSTS:
                values.add(piece)

        return

    if value.startswith(("http://", "https://")):
        path = value.split("/", 3)[3:]

        if host and host.group(1) in PUBLIC_HOSTS and path:
            values.add(path[0].split("/blob/")[0])

        return

    values.add(value.strip())


def files(staged):
    if staged:
        names = run(["git", "diff", "--cached", "--name-only", "--diff-filter=ACMR"])
    else:

        # Tracked files plus new ones not yet added, skipping what
        # .gitignore excludes (such as real settings files).

        names = run(["git", "ls-files", "--cached", "--others", "--exclude-standard"])

    for name in names.splitlines():
        if staged:
            content = subprocess.run(
                ["git", "show", f":{name}"], capture_output=True).stdout
        else:
            path = Path(name)

            if not path.is_file():
                continue

            content = path.read_bytes()

        yield name, content.decode("utf-8", "replace")


def find_leaks(text, values):
    leaks = []
    lowered = text.lower()

    for label, pattern in GENERIC_PATTERNS.items():
        for match in pattern.finditer(text):
            leaks.append((line_number(text, match.start()), label))

    for value in values:

        # Whole words only, so a setting like ENVIRONMENT does not match an
        # identifier like environments.

        match = re.search(rf"(?<!\w){re.escape(value.lower())}(?!\w)", lowered)

        if match:
            leaks.append(
                (line_number(text, match.start()), "a value from your private settings"))

    return leaks


def line_number(text, position):
    return text.count("\n", 0, position) + 1


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--staged", action="store_true")

    arguments = parser.parse_args()

    values = private_values()
    found = False

    for name, text in files(arguments.staged):
        for line, label in sorted(set(find_leaks(text, values))):
            print(f"{name}:{line}: {label}")

            found = True

    if found:
        print(
            "\nPrivate data found. Remove it before committing; this repository "
            "is public.")

        return 1

    if not values:
        print(
            "Note: no private settings on this machine, so only generic secret "
            "patterns were checked.")

    return 0


def run(command):
    return subprocess.run(command, capture_output=True, check=True, text=True).stdout


if __name__ == "__main__":
    sys.exit(main())
