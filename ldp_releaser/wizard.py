"""First-run setup: asks for the team file and the personal settings in the
terminal, checks each answer, and saves them where the server reads them."""

import getpass
import shutil
import subprocess
import sys
import tomllib
import urllib.error
from pathlib import Path

from . import config, health
from .clients import Jenkins, Jira


class SetupError(Exception):
    pass


def ask(prompt, current=None, secret=False):
    if current not in (None, ""):
        if secret:
            prompt += " [press Enter to keep the current one]"
        else:
            prompt += f" [{current}]"

    prompt += ": "

    # getpass needs a terminal; piped answers (as in the tests) are read
    # as plain lines.

    if secret and sys.stdin.isatty():
        answer = getpass.getpass(prompt)
    else:
        print(prompt, end="", flush=True)

        answer = sys.stdin.readline()

        if not answer:
            raise SetupError("setup was interrupted")

        if not sys.stdin.isatty():
            print()

    answer = answer.strip()

    if not answer and current not in (None, ""):
        return current

    return answer


def ask_charts(personal, settings, force):
    key = "charts.checkout"

    if not needs_answer(personal, key, force):
        return

    while True:
        answer = ask(
            "Path to your checkout of " + settings["charts.repository"],
            config.get_value(personal, key))

        path = Path(answer).expanduser()

        if not (path / ".git").exists():
            print(f"  {path} is not a git checkout, try again")

            continue

        problem = health.check_charts(
            config.Settings(
                config.merge(
                    settings.values(), {"charts": {"checkout": str(path)}})))

        if problem:
            print(f"  {problem}, try again")

            continue

        keep(personal, key, answer)

        return


def ask_jenkins(personal, settings, force):
    if not (needs_answer(personal, "jenkins.user", force) or
            needs_answer(personal, "jenkins.token", force)):

        return

    while True:
        user = ask_value(personal, "jenkins.user", "Jenkins user ID")
        token = ask_value(
            personal, "jenkins.token", "Jenkins API token", secret=True)

        jenkins = Jenkins(
            settings["jenkins.url"], settings["jenkins.folder"], token, user)

        try:
            jenkins.check()
        except urllib.error.HTTPError as exception:
            reject(
                f"Jenkins rejected that user and token ({exception.code})",
                ["jenkins.user", "jenkins.token"])

            continue
        except (OSError, urllib.error.URLError) as exception:
            raise SetupError(f"Jenkins is not reachable: {exception}")

        print("  Jenkins: OK")

        keep(personal, "jenkins.user", user)
        keep(personal, "jenkins.token", token)

        return


def ask_jira(personal, settings, force):
    if not (needs_answer(personal, "jira.email", force) or
            needs_answer(personal, "jira.token", force)):

        return

    while True:
        email = ask_value(personal, "jira.email", "Atlassian account email")
        token = ask_value(
            personal, "jira.token", "Atlassian API token", secret=True)

        try:
            account = Jira(settings["jira.url"], token, email).check()
        except RuntimeError:
            reject(
                "Jira rejected that email and token", ["jira.email", "jira.token"])

            continue
        except (OSError, urllib.error.URLError) as exception:
            raise SetupError(f"Jira is not reachable: {exception}")

        print(f"  Jira: logged in as {account.get('displayName')}")

        keep(personal, "jira.email", email)
        keep(personal, "jira.token", token)

        return


def ask_port(personal, force):
    if not force and config.get_value(personal, "server.port") is not None:
        return

    if config.environment_value("server.port"):
        return

    while True:
        answer = ask(
            "Port for the local page",
            config.get_value(personal, "server.port") or config.DEFAULT_PORT)

        if config.validate_kind("port", answer) is None:
            config.set_value(personal, "server.port", int(answer))

            return

        print("  Use a port between 1024 and 65535")


def ask_team_file(problems):
    print()

    if problems:
        print("The team settings need to be replaced:")

        for problem in problems:
            print(f"  - {problem}")
    else:
        print("Team settings not found.")

    print(
        "Get ldp-releaser.team.toml from the team owner (it is shared through "
        "1Password).")

    use_op = shutil.which("op") is not None

    while True:
        if use_op:
            answer = ask(
                "Path to ldp-releaser.team.toml, or press Enter to fetch it "
                "with the 1Password CLI")
        else:
            answer = ask("Path to ldp-releaser.team.toml")

        if answer:
            path = Path(answer).expanduser()

            if not path.is_file():
                print(f"  {path} does not exist, try again")

                continue

            text = path.read_text()
        elif use_op:
            text = fetch_with_op()

            if text is None:
                continue
        else:
            continue

        try:
            team = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exception:
            print(f"  That file is not valid TOML ({exception}), try again")

            continue

        team_problems = config.validate_team(team)

        if team_problems:
            print("  That file is incomplete:")

            for problem in team_problems:
                print(f"    - {problem}")

            continue

        config.write_private(config.TEAM_FILE, text)

        print(f"  Saved to {config.TEAM_FILE}")

        return team


def ask_value(personal, key, label, secret=False):
    override = config.environment_value(key, fallbacks=False)

    if override:
        print(f"{label}: taken from the environment")

        return override

    current = config.get_value(personal, key)

    if current in (None, ""):
        current = config.environment_value(key)

    while True:
        answer = ask(label, current, secret=secret)

        if answer:
            return answer

        print(f"  {label} is required")


def ensure_configured(force=False):

    # Runs before the server starts: asks only for what is missing, or for
    # everything with --setup.

    team = config.read_toml(config.TEAM_FILE)
    team_problems = []

    if team is not None:
        team_problems = config.validate_team(team)

    personal = config.read_toml(config.PERSONAL_FILE) or {}

    personal_problems = config.validate_personal(config.with_environment(personal))

    if not force and team is not None and not team_problems and not personal_problems:
        return

    print("LDP Releaser setup")

    if force or team is None or team_problems:
        if force and team is not None and not team_problems:
            if ask("Replace the team settings? (y/N)").lower().startswith("y"):
                team = ask_team_file([])
        else:
            team = ask_team_file(team_problems)

    settings = config.Settings(team)

    print()
    print(
        "Your personal settings (saved only on this machine, readable only "
        "by you):")

    ask_jenkins(personal, settings, force)
    ask_jira(personal, settings, force)
    ask_charts(personal, settings, force)
    ask_port(personal, force)

    config.write_personal(personal)

    print(f"  Saved to {config.PERSONAL_FILE}")
    print()


def fetch_with_op():
    item = ask("1Password item name")

    if not item:
        return None

    vault = ask("1Password vault (press Enter for any)")

    command = ["op", "document", "get", item]

    if vault:
        command += ["--vault", vault]

    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)

    if completed.returncode:
        print(f"  1Password could not fetch it: {completed.stderr.strip()}")

        return None

    return completed.stdout


def keep(personal, key, value):

    # Values coming from environment variables stay there; only answers
    # typed in the terminal are saved to the file.

    if config.environment_value(key, fallbacks=False) != value:
        config.set_value(personal, key, value)


def needs_answer(personal, key, force):
    if force:
        return True

    value = config.get_value(personal, key)

    if value in (None, ""):
        value = config.environment_value(key)

    return value in (None, "")


def reject(message, keys):
    from_environment = [
        key for key in keys if config.environment_value(key, fallbacks=False)
    ]

    if from_environment:
        raise SetupError(
            f"{message}; the values come from environment variables, so fix "
            f"those and start again")

    print(f"  {message}, try again")
