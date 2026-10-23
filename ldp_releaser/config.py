"""Loads and validates the team settings and the personal settings.

The team file describes the shared setup (Jenkins, Jira, repositories, the
Slack webhook) and is shared privately. The personal file holds each
person's credentials and local paths. Neither belongs in the repository.
"""

import os
import re
import stat
import tomllib
from pathlib import Path

CONFIG_DIR = Path(
    os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "ldp-releaser"
DATA_DIR = Path(
    os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share")) / \
    "ldp-releaser"
PERSONAL_FILE = CONFIG_DIR / "config.toml"
TEAM_FILE = CONFIG_DIR / "ldp-releaser.team.toml"

DEFAULT_PORT = 8766

# Dotted key -> kind. Every key is required unless listed in OPTIONAL_KEYS.

PERSONAL_SCHEMA = {
    "charts.checkout": "path",
    "jenkins.token": "secret",
    "jenkins.user": "str",
    "jira.email": "str",
    "jira.token": "secret",
    "server.port": "port",
}

TEAM_SCHEMA = {
    "argocd.search": "str",
    "argocd.urls": "url_table",
    "asah.branch_parameter": "str",
    "asah.build_job": "str",
    "asah.image_pattern": "pattern",
    "asah.repository": "repository",
    "asah.tag_format": "tag_format",
    "asah.tag_suffix_pattern": "pattern",
    "charts.contributing_url": "url",
    "charts.develop_branch": "str",
    "charts.files": "table",
    "charts.production.environments": "list",
    "charts.production_branch": "str",
    "charts.repository": "repository",
    "charts.tag_suffix_key": "str",
    "faro.branch_parameter": "str",
    "faro.build_environments": "table",
    "faro.build_job": "str",
    "faro.deploy_job": "str",
    "faro.environment_parameter": "str",
    "faro.image_parameter": "str",
    "faro.image_pattern": "pattern",
    "faro.repository": "repository",
    "faro.tag_format": "tag_format",
    "gcloud.configurations": "gcloud_configurations",
    "gcloud.environments": "table",
    "jenkins.commit_pattern": "pattern",
    "jenkins.folder": "str",
    "jenkins.url": "url",
    "jira.board_id": "int",
    "jira.component": "str",
    "jira.project": "str",
    "jira.task_type": "str",
    "jira.technical_task_type": "str",
    "jira.title": "title",
    "jira.url": "url",
    "jira.workflow.closed_status": "str",
    "jira.workflow.completed_resolution": "str",
    "jira.workflow.discarded_resolution": "str",
    "jira.workflow.task_to_in_progress": "list",
    "jira.workflow.technical_task_to_in_progress": "list",
    "release.prd_environment": "str",
    "slack.webhook_url": "url",
}

OPTIONAL_KEYS = {
    "gcloud.configurations", "gcloud.environments", "server.port",
    "slack.webhook_url",
}

# Environment variables that override a setting, first match wins. The
# JIRA_API_* names are only a fallback for people who already use them.

ENVIRONMENT_OVERRIDES = {
    "charts.checkout": ["LDP_CHARTS_CHECKOUT"],
    "jenkins.token": ["LDP_JENKINS_TOKEN"],
    "jenkins.user": ["LDP_JENKINS_USER"],
    "jira.email": ["LDP_JIRA_EMAIL"],
    "jira.token": ["LDP_JIRA_TOKEN"],
    "server.port": ["LDP_PORT"],
    "slack.webhook_url": ["LDP_SLACK_WEBHOOK_URL"],
}

ENVIRONMENT_FALLBACKS = {
    "jira.email": ["JIRA_API_USER"],
    "jira.token": ["JIRA_API_TOKEN"],
}


class ConfigError(Exception):

    def __init__(self, problems):
        super().__init__("; ".join(problems))

        self.problems = problems


class Settings:

    def __init__(self, values):
        self._values = values

    def __getitem__(self, key):
        return get_value(self._values, key)

    def compiled(self, key):
        return re.compile(self[key])

    def environments(self):

        # Release environments, in the order the team file lists them.

        return list(self["charts.files"])

    def faro_environments(self):
        return list(self["faro.build_environments"])

    def values(self):
        return self._values


def check_file_permissions(path):

    # Files holding secrets must only be readable by their owner.

    mode = stat.S_IMODE(path.stat().st_mode)

    if mode & 0o077:
        os.chmod(path, 0o600)


def get_value(values, key):
    value = values

    for part in key.split("."):
        if not isinstance(value, dict) or part not in value:
            return None

        value = value[part]

    return value


def load_settings():
    team = read_toml(TEAM_FILE)
    personal = read_toml(PERSONAL_FILE)

    problems = []

    if team is None:
        problems.append(f"team settings not found at {TEAM_FILE}")

    if personal is None:
        problems.append(f"personal settings not found at {PERSONAL_FILE}")

    if problems:
        raise ConfigError(problems)

    personal = with_environment(personal)

    problems = validate_team(team) + validate_personal(personal)

    if problems:
        raise ConfigError(problems)

    for path in (PERSONAL_FILE, TEAM_FILE):
        check_file_permissions(path)

    values = merge(team, personal)

    slack_override = environment_value("slack.webhook_url")

    if slack_override:
        set_value(values, "slack.webhook_url", slack_override)

    if get_value(values, "server.port") is None:
        set_value(values, "server.port", DEFAULT_PORT)

    set_value(
        values, "charts.checkout",
        str(Path(get_value(values, "charts.checkout")).expanduser()))

    return Settings(values)


def environment_value(key, fallbacks=True):
    names = list(ENVIRONMENT_OVERRIDES.get(key, []))

    if fallbacks:
        names += ENVIRONMENT_FALLBACKS.get(key, [])

    for name in names:
        if os.environ.get(name):
            return os.environ[name]

    return None


def merge(base, extra):
    merged = dict(base)

    for key, value in extra.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = merge(merged[key], value)
        else:
            merged[key] = value

    return merged


def read_toml(path):
    if not path.exists():
        return None

    with path.open("rb") as file:
        return tomllib.load(file)


def set_value(values, key, value):
    parts = key.split(".")

    for part in parts[:-1]:
        values = values.setdefault(part, {})

    values[parts[-1]] = value


def toml_string(value):
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')

    return f'"{escaped}"'


def validate(values, schema):
    problems = []

    for key, kind in schema.items():
        value = get_value(values, key)

        if value is None or value == "":
            if key not in OPTIONAL_KEYS:
                problems.append(f"{key} is missing")

            continue

        problem = validate_kind(kind, value)

        if problem:
            problems.append(f"{key} {problem}")

    return problems


def validate_kind(kind, value):
    if kind == "int":
        if not isinstance(value, int):
            return "must be a number"
    elif kind == "port":
        if isinstance(value, str) and value.isdigit():
            value = int(value)

        if not isinstance(value, int) or not 1024 <= value <= 65535:
            return "must be a port between 1024 and 65535"
    elif kind == "list":
        if not isinstance(value, list) or not all(
                isinstance(item, str) for item in value):

            return "must be a list of strings"
    elif kind == "gcloud_configurations":
        if not isinstance(value, dict) or not value:
            return "must be a table with at least one configuration"

        for name, configuration in value.items():
            if not re.fullmatch(r"[a-z][a-z0-9-]*", name):
                return (
                    f"has an invalid configuration name {name!r} (lowercase "
                    f"letters, digits and hyphens, starting with a letter)")

            if not isinstance(configuration, dict) or not isinstance(
                    configuration.get("project"), str):

                return f"must give a project for {name}"

            unknown = set(configuration) - {"project", "region", "zone"}

            if unknown:
                return f"has unknown keys for {name}: {', '.join(sorted(unknown))}"
    elif kind in ("table", "url_table"):
        if not isinstance(value, dict) or not value:
            return "must be a table with at least one entry"

        if kind == "url_table" and not all(
                str(url).startswith(("http://", "https://"))
                for url in value.values()):

            return "must map each environment to an http(s) URL"
    elif not isinstance(value, str):
        return "must be a string"
    elif kind == "pattern":
        try:
            pattern = re.compile(value)
        except re.error as exception:
            return f"is not a valid regular expression ({exception})"

        if pattern.groups > 1:
            return "must have at most one capturing group"
    elif kind == "repository":
        if not re.fullmatch(r"[\w.-]+/[\w.-]+", value):
            return "must look like owner/name"
    elif kind == "tag_format":
        if "{version}" not in value:
            return "must contain {version}"
    elif kind == "title":
        if "{environments}" not in value:
            return "must contain {environments}"
    elif kind == "url":
        if not value.startswith(("http://", "https://")):
            return "must be an http(s) URL"

    return None


def validate_personal(personal):
    return validate(personal, PERSONAL_SCHEMA)


def validate_team(team):
    problems = validate(team, TEAM_SCHEMA)

    if problems:
        return problems

    environments = set(get_value(team, "charts.files"))

    production = set(get_value(team, "charts.production.environments"))

    if not production <= environments:
        problems.append(
            "charts.production.environments must only list environments "
            "from charts.files")

    missing = environments - set(get_value(team, "argocd.urls"))

    if missing:
        problems.append(
            f"argocd.urls has no URL for {', '.join(sorted(missing))}")

    configurations = get_value(team, "gcloud.configurations") or {}
    gcloud_environments = get_value(team, "gcloud.environments") or {}

    if gcloud_environments and not configurations:
        problems.append("gcloud.environments needs gcloud.configurations")

    for environment, names in gcloud_environments.items():
        if environment not in environments:
            problems.append(
                f"gcloud.environments has {environment}, which is not in "
                f"charts.files")
        elif not isinstance(names, list) or not names:
            problems.append(
                f"gcloud.environments.{environment} must list configuration names")
        else:
            unknown = [name for name in names if name not in configurations]

            if unknown:
                problems.append(
                    f"gcloud.environments.{environment} names unknown "
                    f"configurations: {', '.join(unknown)}")

    prd_environment = get_value(team, "release.prd_environment")

    if prd_environment not in environments | set(
            get_value(team, "faro.build_environments")):

        problems.append(
            "release.prd_environment must be an environment from "
            "charts.files or faro.build_environments")

    return problems


def with_environment(personal):
    personal = merge({}, personal)

    for key in PERSONAL_SCHEMA:
        override = environment_value(key, fallbacks=False)

        if override:
            set_value(personal, key, override)
        elif get_value(personal, key) in (None, ""):
            fallback = environment_value(key)

            if fallback:
                set_value(personal, key, fallback)

    port = get_value(personal, "server.port")

    if isinstance(port, str) and port.isdigit():
        set_value(personal, "server.port", int(port))

    return personal


def write_personal(personal):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

    lines = [
        "# LDP Releaser personal settings. They contain secrets: never share",
        "# or commit this file. Run `./ldp-releaser --setup` to change them.",
        "",
    ]

    sections = {}

    for key in PERSONAL_SCHEMA:
        value = get_value(personal, key)

        if value in (None, ""):
            continue

        section, name = key.split(".")

        sections.setdefault(section, []).append((name, value))

    for section, entries in sections.items():
        lines.append(f"[{section}]")

        for name, value in entries:
            if isinstance(value, int):
                lines.append(f"{name} = {value}")
            else:
                lines.append(f"{name} = {toml_string(str(value))}")

        lines.append("")

    write_private(PERSONAL_FILE, "\n".join(lines))


def write_private(path, text):

    # Create the file readable only by its owner from the start, so the
    # secrets are never briefly world-readable.

    path.parent.mkdir(parents=True, exist_ok=True)

    temporary = path.with_suffix(path.suffix + ".tmp")

    descriptor = os.open(
        temporary, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)

    with os.fdopen(descriptor, "w") as file:
        file.write(text)

    os.chmod(temporary, 0o600)
    os.replace(temporary, path)
