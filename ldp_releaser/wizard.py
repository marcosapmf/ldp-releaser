"""First-run setup: asks for the team file and the personal settings in the
terminal, checks each answer, and saves them where the server reads them.
The checks are shared with the setup page."""

import getpass
import os
import shutil
import stat
import subprocess
import sys
import tomllib
import urllib.error
from pathlib import Path

from . import composer, config, gcloud, health
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


def ask_asah(personal, settings, force):

    # Only the Composer flow uses the Asah clone, so it is asked only when
    # the team settings have the Composer setup.

    key = "asah.checkout"

    if not composer.composer_enabled(settings) or not needs_answer(personal, key, force):
        return

    while True:
        answer = ask(labels(settings)[key], config.get_value(personal, key))

        problem, note = check_asah(settings, answer)

        if problem:
            print(f"  {problem}, try again")

            continue

        if note:
            print(f"  {note}")

        keep(personal, key, answer)

        return


def ask_charts(personal, settings, force):
    key = "charts.checkout"

    if not needs_answer(personal, key, force):
        return

    while True:
        answer = ask(labels(settings)[key], config.get_value(personal, key))

        problem = check_charts(settings, answer)

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
        user = ask_value(personal, "jenkins.user", labels(settings)["jenkins.user"])
        token = ask_value(
            personal, "jenkins.token", labels(settings)["jenkins.token"], secret=True)

        problem = check_jenkins(settings, user, token)

        if problem:
            reject(problem, ["jenkins.user", "jenkins.token"])

            continue

        print("  Jenkins: OK")

        keep(personal, "jenkins.user", user)
        keep(personal, "jenkins.token", token)

        return


def ask_jira(personal, settings, force):
    if not (needs_answer(personal, "jira.email", force) or
            needs_answer(personal, "jira.token", force)):

        return

    while True:
        email = ask_value(personal, "jira.email", labels(settings)["jira.email"])
        token = ask_value(
            personal, "jira.token", labels(settings)["jira.token"], secret=True)

        problem, account = check_jira(settings, email, token)

        if problem:
            reject(problem, ["jira.email", "jira.token"])

            continue

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


def ask_team_file(problems, personal):
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
            source = (None, None)
        elif use_op:
            text, *source = fetch_with_op()

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

        # The setup page fetches it again from this item when settings are missing.

        config.set_value(personal, "team.onepassword_item", source[0])
        config.set_value(personal, "team.onepassword_vault", source[1])

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


def can_ask():

    # Answers come from a terminal, or from a pipe (as in the tests). Started
    # with stdin at /dev/null, as the Claude Code skill and dashboards start
    # it, nobody can answer, so the setup page asks instead.

    try:
        status = os.fstat(sys.stdin.fileno())
    except (AttributeError, OSError, ValueError):
        return False

    null = os.stat(os.devnull)

    return not (stat.S_ISCHR(status.st_mode) and status.st_rdev == null.st_rdev)


def check_asah(settings, answer):

    # Returns why the path cannot be used, and a note that does not stop it.

    path = Path(answer).expanduser()

    if not (path / ".git").exists():
        return f"{path} is not a git clone", None

    if composer.matching_remote(str(path), settings["asah.repository"]) is None:
        return None, (
            f"No remote points to {settings['asah.repository']}; versions will "
            f"be fetched from GitHub over SSH")

    return None, None


def check_charts(settings, answer):

    # Returns why the path cannot be used, or None.

    path = Path(answer).expanduser()

    if not (path / ".git").exists():
        return f"{path} is not a git checkout"

    return health.check_charts(
        config.Settings(
            config.merge(settings.values(), {"charts": {"checkout": str(path)}})))


def check_jenkins(settings, user, token):

    # Returns why Jenkins rejected them, or None. An unreachable Jenkins
    # stops the setup instead, since no answer can fix it.

    jenkins = Jenkins(settings["jenkins.url"], settings["jenkins.folder"], token, user)

    try:
        jenkins.check()
    except urllib.error.HTTPError as exception:
        return f"Jenkins rejected that user and token ({exception.code})"
    except (OSError, urllib.error.URLError) as exception:
        raise SetupError(f"Jenkins is not reachable: {exception}")

    return None


def check_jira(settings, email, token):

    # Returns why Jira rejected them, or None and the account behind them.

    try:
        account = Jira(settings["jira.url"], token, email).check()
    except RuntimeError:
        return "Jira rejected that email and token", None
    except (OSError, urllib.error.URLError) as exception:
        raise SetupError(f"Jira is not reachable: {exception}")

    return None, account


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
                team = ask_team_file([], personal)
        else:
            team = ask_team_file(team_problems, personal)

    settings = config.Settings(team)

    print()
    print(
        "Your personal settings (saved only on this machine, readable only "
        "by you):")

    ask_jenkins(personal, settings, force)
    ask_jira(personal, settings, force)
    ask_charts(personal, settings, force)
    ask_asah(personal, settings, force)
    ask_port(personal, force)

    config.write_personal(personal)

    print(f"  Saved to {config.PERSONAL_FILE}")

    report = gcloud.sync(settings)

    if report:
        print()
        print("gcloud configurations (your own gcloud login is used):")

        for line in report:
            print(f"  {line}")

    print()


def fetch_with_op():
    item = ask("1Password item name")

    if not item:
        return None, None, None

    vault = ask("1Password vault (press Enter for any)")

    command = ["op", "document", "get", item]

    if vault:
        command += ["--vault", vault]

    completed = subprocess.run(command, capture_output=True, text=True, timeout=120)

    if completed.returncode:
        print(f"  1Password could not fetch it: {completed.stderr.strip()}")

        return None, None, None

    return completed.stdout, item, vault or None


def keep(personal, key, value):

    # Values coming from environment variables stay there; only answers
    # typed in the terminal are saved to the file.

    if config.environment_value(key, fallbacks=False) != value:
        config.set_value(personal, key, value)


def labels(settings):

    # The personal settings the setup asks for, in the terminal and on the
    # setup page.

    return {
        "asah.checkout": "Path to your clone of " + settings["asah.repository"],
        "charts.checkout": "Path to your checkout of " + settings["charts.repository"],
        "jenkins.token": "Jenkins API token",
        "jenkins.user": "Jenkins user ID",
        "jira.email": "Atlassian account email",
        "jira.token": "Atlassian API token",
    }


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
