"""Starts LDP Releaser: runs the setup when needed, then the local server."""

import argparse
import atexit
import signal
import sys
import urllib.error

if sys.version_info < (3, 11):
    sys.exit("LDP Releaser needs Python 3.11 or newer")

from . import config, gcloud, release, server, store, wizard
from .clients import Jenkins, Jira


def main():
    parser = argparse.ArgumentParser(
        prog="ldp-releaser",
        description="Local web page that builds, releases and deploys.")
    parser.add_argument(
        "--setup", action="store_true",
        help="ask for the team file and your credentials again")

    arguments = parser.parse_args()

    try:
        wizard.ensure_configured(force=arguments.setup)
    except wizard.SetupError as exception:
        sys.exit(f"Setup stopped: {exception}")
    except KeyboardInterrupt:
        sys.exit("\nSetup cancelled")

    try:
        settings = config.load_settings()
    except config.ConfigError as exception:
        problems = "\n".join(f"  - {problem}" for problem in exception.problems)

        sys.exit(
            f"The settings are incomplete:\n{problems}\nRun ./ldp-releaser "
            f"--setup to fix them.")

    jenkins = Jenkins(
        settings["jenkins.url"], settings["jenkins.folder"],
        settings["jenkins.token"], settings["jenkins.user"])
    jira = Jira(settings["jira.url"], settings["jira.token"], settings["jira.email"])

    account = check_credentials(jenkins, jira)

    # Configurations added to the team file after someone's setup are
    # created on their next start. Existing ones are left untouched.

    for line in gcloud.sync(settings):
        if not line.endswith("already set up"):
            print(f"gcloud: {line}", flush=True)

    release.configure(settings, store.mark_dirty)

    unfinished = []

    for run in store.load():
        if store.prepare_resume(run):
            unfinished.append(run)

        store.mark_dirty(run)

    store.start()

    atexit.register(store.flush)
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    if unfinished:
        print(f"Resuming {len(unfinished)} unfinished run(s)", flush=True)

    try:
        server.serve(account, jenkins, jira, settings, unfinished)
    except KeyboardInterrupt:
        pass


def check_credentials(jenkins, jira):

    # A rejected token stops here with a clear message instead of failing in
    # the middle of a release. An unreachable server is only a warning; the
    # page shows it in the setup panel.

    try:
        jenkins.check()
    except urllib.error.HTTPError as exception:
        if exception.code in (401, 403):
            sys.exit(
                "Jenkins rejected your user and token. Run ./ldp-releaser "
                "--setup to update them.")
    except (OSError, urllib.error.URLError) as exception:
        print(f"Warning: Jenkins is not reachable ({exception})", flush=True)

    try:
        account = jira.check()

        # The page only needs to tell your tickets from your teammates'.

        return {
            "accountId": account.get("accountId"),
            "displayName": account.get("displayName"),
        }
    except RuntimeError as exception:
        if "(401)" in str(exception) or "(403)" in str(exception):
            sys.exit(
                "Jira rejected your email and token. Run ./ldp-releaser --setup "
                "to update them.")

        print(f"Warning: Jira check failed ({exception})", flush=True)
    except (OSError, urllib.error.URLError) as exception:
        print(f"Warning: Jira is not reachable ({exception})", flush=True)

    return None


if __name__ == "__main__":
    main()
