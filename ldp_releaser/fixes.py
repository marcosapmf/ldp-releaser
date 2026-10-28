"""Fixes the vulnerabilities that made an Asah image scan fail.

`plan` turns the new vulnerabilities of a failed scan into fixes:
- a copy of the vulnerable library itself gets its version raised to the
  fixed one, wherever the Gradle files declare or force it
- a copy bundled inside an annotation processor (only needed to compile)
  gets that processor excluded from the runtime
Anything else, such as a copy bundled inside a library used at runtime, is
listed for a person.

`apply` puts each fix in its own commit on top of the team's main branch and
of the released branch, pushes them to the person's fork, opens the pull
requests and tracks the work in a Jira subtask of the release Task.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

from . import composer, release

ID_PATTERN = re.compile(r" \((?:CVE|GHSA|PRISMA)-[^)]*\)")
JAR_PATTERN = re.compile(r"^(?P<name>.+?)-(?P<version>\d[\w.+-]*)\.jar$")
MAVEN_SEARCH_URL = os.environ.get(
    "LDP_MAVEN_SEARCH_URL", "https://search.maven.org/solrsearch/select")
MAVEN_URL = os.environ.get("LDP_MAVEN_URL", "https://repo1.maven.org/maven2")
PROCESSOR_ENTRY = "META-INF/services/javax.annotation.processing.Processor"
VERSION_PATTERN = re.compile(r"\d+(?:\.\d+)*")


def apply(jira, run, stage_name):
    stage = run["stages"][stage_name]
    fixes = stage["failure"]["fixes"]
    settings = release.settings

    try:
        cves = sorted({cve for fix in fixes for cve in fix["cves"]})

        task_key = release_task(jira, run, stage_name)
        subtask_key = create_subtask(jira, run, stage_name, cves, task_key)

        clone = str(Path(settings["asah.checkout"]).expanduser())
        login = command(run, stage_name, ["gh", "api", "user", "--jq", ".login"]).strip()
        fork = f"{login}/{settings['asah.repository'].split('/', 1)[1]}"
        fork_remote = composer.matching_remote(clone, fork)

        if fork_remote is None:
            raise RuntimeError(
                f"No remote of {clone} points to your fork {fork}, so add it with "
                f"git remote add")

        bases = list(dict.fromkeys([settings["asah.main_branch"], run["asahBranch"]]))

        for base in bases:
            fix_branch(run, stage_name, base, fixes, fork_remote, login, subtask_key)

        update_fix(run, stage_name, finishedAt=time.time(), status="success")
    except Exception as exception:
        log(run, stage_name, f"Failed: {exception}\n")
        update_fix(
            run, stage_name, error=str(exception), finishedAt=time.time(), status="failed")


def apply_exclude(fix, worktree):
    path = worktree / release.settings["asah.dependency_file"]
    text = path.read_text()
    line = f'exclude group: "{fix["group"]}", module: "{fix["name"]}"'

    if line in text:
        return []

    path.write_text(insert_line(text, "exclude group: ", line, None))

    return [release.settings["asah.dependency_file"]]


def apply_update(fix, worktree):

    # Raises every declaration or force of the library that is older than
    # the fixed version; when nothing declares it, forces the fixed version.

    pattern = re.compile(
        r'(group: "(?P<group>[^"]+)", name: "' + re.escape(fix["name"]) +
        r'", version: ")(?P<version>[^"]+)(")')

    found = False
    changed = []

    files = subprocess.run(
        ["git", "-C", str(worktree), "ls-files", "*.gradle"], capture_output=True,
        check=True, text=True).stdout.split()

    for relative_path in files:
        path = worktree / relative_path
        text = path.read_text()

        def replace(match):
            nonlocal found

            if fix["group"] and match["group"] != fix["group"]:
                return match[0]

            found = True

            if version_key(match["version"]) >= version_key(fix["version"]):
                return match[0]

            return match[1] + fix["version"] + match[4]

        updated = pattern.sub(replace, text)

        if updated != text:
            path.write_text(updated)

            changed.append(relative_path)

    if found or not fix["group"]:
        return changed

    path = worktree / release.settings["asah.dependency_file"]
    line = f'force group: "{fix["group"]}", name: "{fix["name"]}", version: "{fix["version"]}"'

    path.write_text(insert_line(path.read_text(), "force group: ", line, "resolutionStrategy {"))

    return [release.settings["asah.dependency_file"]]


def bundling_artifact(name, version):

    # (group, is an annotation processor) of a jar from the scan, looked up
    # in the local Gradle caches first and on Maven Central otherwise.

    checkout = release.settings["asah.checkout"]
    homes = [Path.home() / ".gradle"]

    if checkout:
        homes.insert(0, Path(checkout).expanduser() / ".gradle")

    for home in homes:
        for jar in sorted(
                (home / "caches" / "modules-2" / "files-2.1").glob(
                    f"*/{name}/{version}/*/{name}-{version}.jar")):

            return jar.parts[-5], is_processor(jar)

    query = urllib.parse.urlencode(
        {"q": f'a:"{name}" AND v:"{version}"', "rows": 5, "wt": "json"})

    try:
        with urllib.request.urlopen(f"{MAVEN_SEARCH_URL}?{query}", timeout=60) as response:
            groups = {doc["g"] for doc in json.load(response)["response"]["docs"]}
    except (OSError, ValueError, KeyError):
        return None

    if len(groups) != 1:
        return None

    group = groups.pop()
    url = f"{MAVEN_URL}/{group.replace('.', '/')}/{name}/{version}/{name}-{version}.jar"

    with tempfile.TemporaryDirectory(prefix="ldp-releaser-") as directory:
        jar = Path(directory) / f"{name}-{version}.jar"

        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                jar.write_bytes(response.read())
        except OSError:
            return None

        return group, is_processor(jar)


def choose_version(current, candidates):

    # The smallest fixed version newer than the current one, preferring the
    # same major version, with the current one's suffix (such as -jre).

    current_key = version_key(current)
    newer = [
        candidate for candidate in candidates if version_key(candidate) > current_key
    ]
    same_major = [
        candidate for candidate in newer if version_key(candidate)[:1] == current_key[:1]
    ]

    choices = same_major or newer

    if not choices:
        return None

    chosen = min(choices, key=version_key)
    suffix = current[len(VERSION_PATTERN.match(current)[0]):]

    if chosen == VERSION_PATTERN.match(chosen)[0]:
        chosen += suffix

    return chosen


def command(run, stage_name, args, cwd=None):
    log(run, stage_name, "$ " + " ".join(args[:5]) + "\n")

    completed = subprocess.run(args, capture_output=True, cwd=cwd, text=True, timeout=900)

    log(run, stage_name, completed.stderr)

    if completed.returncode:
        raise RuntimeError(
            f"{' '.join(args[:3])} failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()[-300:]}")

    return completed.stdout


def commit_message(fix, key):

    # The messages do not name the vulnerabilities: the commits stay in the
    # history, while the private Jira ticket says what they fix.

    if fix["kind"] == "update":
        return f"{key} Update {fix['name']} to {fix['version']}"

    return f"{key} Exclude the {fix['name']} annotation processor from the runtime"


def create_subtask(jira, run, stage_name, cves, task_key):

    # Vulnerabilities must not be public before they are fixed, so the
    # subtask is created with the security level and a title without the
    # CVEs, which it only gets once Jira confirms the level. Jira can keep a
    # subtask at its parent's level, so the level is read back, not assumed.

    settings = release.settings
    level = settings["jira.fix_security_level"]

    key = jira.request(
        "POST", "/rest/api/3/issue",
        {
            "fields": {
                "assignee": {"accountId": jira.check()["accountId"]},
                "components": [{"name": settings["jira.component"]}],
                "issuetype": {"name": settings["jira.technical_task_type"]},
                "parent": {"key": task_key},
                "project": {"key": settings["jira.project"]},
                "security": {"name": level},
                "summary": "Fix vulnerabilities found by the image scan",
            },
        })["key"]

    log(run, stage_name, f"Created {key} under {task_key}\n")
    link(run, stage_name, f"{key} (fix)", jira.issue_url(key))
    update_fix(run, stage_name, subtask=key)

    current = security_level(jira, key)

    if current != level:
        jira.request("PUT", f"/rest/api/3/issue/{key}", {"fields": {"security": {"name": level}}})

        current = security_level(jira, key)

    if current != level:
        raise RuntimeError(
            f"Jira did not make {key} {level} (it is {current or 'public'}), so its title "
            f"does not name the vulnerabilities. Make {key} {level} by hand, then try again")

    log(run, stage_name, f"{key} is {level}\n")

    jira.request(
        "PUT", f"/rest/api/3/issue/{key}",
        {"fields": {"summary": f"Fix {', '.join(cves)} found by the image scan"}})

    release.move_to_in_progress(
        jira, run, key, "jira.workflow.technical_task_to_in_progress",
        lambda text: log(run, stage_name, text))

    return key


def enabled(settings):
    return bool(
        settings["asah.checkout"] and settings["asah.dependency_file"] and
        settings["asah.main_branch"] and settings["jira.fix_security_level"])


def fix_branch(run, stage_name, base, fixes, fork_remote, login, subtask_key):
    settings = release.settings
    clone = str(Path(settings["asah.checkout"]).expanduser())
    source = composer.matching_remote(clone, settings["asah.repository"])

    if source is None:
        source = f"git@github.com:{settings['asah.repository']}.git"

    branch = f"{subtask_key}-{base}"
    directory = Path(tempfile.mkdtemp(prefix="ldp-releaser-"))
    worktree = directory / "asah"

    try:
        command(run, stage_name, ["git", "-C", clone, "fetch", "--no-tags", source, base])
        command(
            run, stage_name,
            ["git", "-C", clone, "worktree", "add", "--detach", str(worktree), "FETCH_HEAD"])

        commits = []

        for fix in fixes:
            if fix["kind"] == "update":
                paths = apply_update(fix, worktree)
            else:
                paths = apply_exclude(fix, worktree)

            if not paths:
                log(run, stage_name, f"{base}: {fix['name']} needs no change\n")

                continue

            message = commit_message(fix, subtask_key)

            command(run, stage_name, ["git", "-C", str(worktree), "add", *paths])
            command(run, stage_name, ["git", "-C", str(worktree), "commit", "-m", message])

            commits.append(message)

        if not commits:
            log(run, stage_name, f"{base}: nothing to change, so no pull request\n")

            return

        command(
            run, stage_name,
            ["git", "-C", str(worktree), "push", fork_remote, f"HEAD:refs/heads/{branch}"])

        output = command(
            run, stage_name,
            [
                "gh", "pr", "create", "--repo", settings["asah.repository"], "--base", base,
                "--head", f"{login}:{branch}", "--title", f"{subtask_key} | {base}",
                "--body", pull_request_body(run, stage_name, commits, subtask_key),
            ])

        url = output.strip().splitlines()[-1]

        link(run, stage_name, f"Pull request to {base}", url)
    finally:
        if worktree.exists():
            subprocess.run(
                ["git", "-C", clone, "worktree", "remove", "--force", str(worktree)],
                capture_output=True, text=True, timeout=120)

        shutil.rmtree(directory, ignore_errors=True)


def fix_versions(text):
    return VERSION_PATTERN.findall(text.removeprefix("fixed in"))


def insert_line(text, prefix, line, block_opening):

    # Adds the line to configurations.all, sorted among the lines that
    # start with the same prefix, or opens the block it belongs in.

    lines = text.splitlines(keepends=True)
    start = next(
        (index for index, value in enumerate(lines)
         if value.strip() == "configurations.all {"), None)

    if start is None:
        body = f"\t\t{line}\n" if block_opening else f"\t{line}\n"

        if block_opening:
            body = f"\t{block_opening}\n{body}\t}}\n"

        return text.rstrip("\n") + f"\n\nconfigurations.all {{\n{body}}}\n"

    depth = 0
    end = start

    for index in range(start, len(lines)):
        depth += lines[index].count("{") - lines[index].count("}")

        if depth == 0:
            end = index

            break

    matches = [
        index for index in range(start, end) if lines[index].strip().startswith(prefix)
    ]

    if matches:
        indent = lines[matches[0]][:len(lines[matches[0]]) - len(lines[matches[0]].lstrip())]
        position = next(
            (index for index in matches if lines[index].strip() > line), matches[-1] + 1)

        lines.insert(position, f"{indent}{line}\n")

        return "".join(lines)

    opening = next(
        (index for index in range(start, end)
         if block_opening and lines[index].strip() == block_opening), None)

    if opening is not None:
        lines.insert(opening + 1, f"\t\t{line}\n")
    elif block_opening:
        lines.insert(end, f"\n\t{block_opening}\n\t\t{line}\n\t}}\n")
    else:
        lines.insert(start + 1, f"\t{line}\n")

    return "".join(lines)


def is_processor(jar):
    try:
        with zipfile.ZipFile(jar) as archive:
            return PROCESSOR_ENTRY in archive.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def link(run, stage_name, label, url):
    with release.runs_lock:
        run["stages"][stage_name]["fix"]["links"].append({"label": label, "url": url})

    release.on_change(run)


def log(run, stage_name, text):
    if not text:
        return

    with release.runs_lock:
        fix = run["stages"][stage_name]["fix"]
        fix["log"] = (fix["log"] + text)[-release.LOG_TAIL_CHARS:]

    release.on_change(run)


def plan(vulnerabilities):

    # (fixes, problems): fixes are applied by `apply`; problems need a
    # person.

    updates = {}
    excludes = {}
    problems = []

    for item in vulnerabilities:
        group, _, name = item["package"].rpartition("_")
        candidates = fix_versions(item["fix"])

        if not item.get("jars"):
            problems.append(
                f"{item['package']} {item['version']} ({item['id']}): the scan report does "
                f"not say which jar it is in")

        for jar in item.get("jars") or []:
            match = JAR_PATTERN.match(jar)

            if match and match["name"] == name:
                version = choose_version(match["version"], candidates)

                if version is None:
                    problems.append(
                        f"{jar} ({item['id']}): no fixed version of {name} is newer")

                    continue

                fix = updates.setdefault(
                    name,
                    {"cves": [], "group": None, "kind": "update", "name": name,
                     "version": version})

                fix["group"] = fix["group"] or group or None

                if version_key(version) > version_key(fix["version"]):
                    fix["version"] = version

                if item["id"] not in fix["cves"]:
                    fix["cves"].append(item["id"])

                continue

            artifact = None

            if match:
                artifact = bundling_artifact(match["name"], match["version"])

            if artifact and artifact[1]:
                fix = excludes.setdefault(
                    (artifact[0], match["name"]),
                    {"bundles": f"{name} {item['version']}", "cves": [], "group": artifact[0],
                     "kind": "exclude", "name": match["name"]})

                if item["id"] not in fix["cves"]:
                    fix["cves"].append(item["id"])

                continue

            problems.append(
                f"{jar} bundles {name} {item['version']} ({item['id']}) and is needed at "
                f"runtime: it needs a release of {jar.rsplit('-', 1)[0]} with a fixed "
                f"{name}, or a scan exception")

    fixes = sorted(updates.values(), key=lambda fix: fix["name"]) + sorted(
        excludes.values(), key=lambda fix: fix["name"])

    return fixes, list(dict.fromkeys(problems))


def pull_request_body(run, stage_name, commits, subtask_key):
    stage = run["stages"][stage_name]
    lines = [
        f"Jira ticket: {release.settings['jira.url'].rstrip('/')}/browse/{subtask_key}",
        "",
        f"Fixes the vulnerabilities that failed the image scan of {stage['url']}:",
        "",
        *[f"- {message.split(' ', 1)[1]}" for message in commits],
    ]

    # Like the commits, the pull requests do not name the vulnerabilities;
    # the private Jira ticket does.

    problems = [ID_PATTERN.sub("", problem) for problem in stage["failure"]["problems"]]

    if problems:
        lines += ["", "Not fixed here, needs a person:", "", *[f"- {line}" for line in problems]]

    return "\n".join(lines)


def release_task(jira, run, stage_name):

    # The release Task the fix belongs to: the run's own, an open one of
    # the team, or a new one that the next release run offers to reuse.

    settings = release.settings
    closed_status = settings["jira.workflow.closed_status"]

    if run["jiraTask"] and jira.status(run["jiraTask"]) != closed_status:
        return run["jiraTask"]

    tickets = release.open_release_tickets(jira)

    if tickets:
        task = tickets[0]

        log(
            run, stage_name,
            f"Using the open release Task {task['key']} "
            f"({task['assignee']['displayName']})\n")
        link(run, stage_name, f"{task['key']} (Task)", jira.issue_url(task["key"]))

        return task["key"]

    if not run["releaseEnvironments"]:
        raise RuntimeError(
            "This run releases no environment and no release Task is open, so there is "
            "no Task to add the fix to")

    key, sprint = release.new_release_task(
        jira, run["releaseSummary"],
        f"Release LDP to {release.list_environments(run['releaseEnvironments'])}.")

    log(run, stage_name, f"Created the release Task {key}: {run['releaseSummary']}\n")

    if sprint:
        log(run, stage_name, f"Added {key} to sprint {sprint}\n")

    link(run, stage_name, f"{key} (Task)", jira.issue_url(key))

    return key


def security_level(jira, key):
    fields = jira.request("GET", f"/rest/api/3/issue/{key}?fields=security")["fields"]

    return (fields.get("security") or {}).get("name")


def start(jira, run, stage_name):

    # Returns an error message, or None once the fix started in the
    # background.

    with release.runs_lock:
        stage = run["stages"].get(stage_name)

        if stage is None or not (stage.get("failure") or {}).get("fixes"):
            return "There is nothing to fix for this step"

        if (stage.get("fix") or {}).get("status") in ("running", "success"):
            return "The fixes were already applied"

        stage["fix"] = {
            "error": None,
            "finishedAt": None,
            "links": [],
            "log": "",
            "startedAt": time.time(),
            "status": "running",
            "subtask": None,
        }

    release.on_change(run)

    threading.Thread(args=(jira, run, stage_name), daemon=True, target=apply).start()

    return None


def update_fix(run, stage_name, **changes):
    with release.runs_lock:
        run["stages"][stage_name]["fix"].update(changes)

    release.on_change(run)


def version_key(version):
    match = VERSION_PATTERN.match(version)

    if not match:
        return ()

    return tuple(int(part) for part in match[0].split("."))
