"""Saves runs to disk and resumes the unfinished ones after a restart."""

import json
import os
import threading
import time

from . import config, release

KEEP_RUNS = 100
RUNS_DIR = config.DATA_DIR / "runs"
SAVE_INTERVAL_SECONDS = 1

dirty = set()
dirty_lock = threading.Condition()


def flush():
    with dirty_lock:
        run_ids = list(dirty)

        dirty.clear()

    for run_id in run_ids:
        save(run_id)


def load():

    # Returns the unfinished runs, which the caller resumes.

    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    unfinished = []

    for path in sorted(RUNS_DIR.glob("*.json")):
        try:
            run = json.loads(path.read_text())
        except (OSError, ValueError):
            continue

        with release.runs_lock:
            release.runs[run["id"]] = run

        if run["finishedAt"] is None:
            unfinished.append(run)

    prune()

    return unfinished


def mark_dirty(run):
    with dirty_lock:
        dirty.add(run["id"])
        dirty_lock.notify()


def prune():
    with release.runs_lock:
        finished = sorted(
            (run for run in release.runs.values() if run["finishedAt"]),
            key=lambda run: run["createdAt"], reverse=True)

        for run in finished[KEEP_RUNS:]:
            release.runs.pop(run["id"], None)

            (RUNS_DIR / f"{run['id']}.json").unlink(missing_ok=True)


def prepare_resume(run):

    # Decides how an unfinished run continues. Builds re-attach and waits
    # wait again; a step that was changing Jira, git or GitHub when the
    # server stopped fails the run, because repeating it could duplicate
    # tickets or pull requests.

    with release.runs_lock:
        if run["cancelRequested"]:
            interrupted = None
        else:
            interrupted = next(
                (
                    name for name in run.get("stageOrder", release.STAGE_NAMES)
                    if run["stages"][name]["status"] in ("queued", "running") and
                    name not in release.RESUMABLE_STAGES and
                    run.get("kind") != "composer"
                ),
                None)

        if run["cancelRequested"] or interrupted:
            for stage in run["stages"].values():
                if stage["status"] in ("queued", "running"):
                    stage["finishedAt"] = time.time()
                    stage["status"] = "failed"
                    stage["why"] = None

            if interrupted:
                run["error"] = (
                    f"LDP Releaser stopped while the "
                    f"{interrupted} step was running, so it was not resumed. "
                    f"Check the links above, then start a new run; it offers "
                    f"to reuse the open tickets and pull requests.")
                run["status"] = "failed"
            else:
                run["status"] = "aborted"

            run["finishedAt"] = time.time()

            return False

        for stage in run["stages"].values():
            if stage["status"] in ("queued", "running"):
                stage["log"] += "[LDP Releaser] Resumed after a restart\n"
                stage["needsGcloudLogin"] = False

    return True


def save(run_id):
    with release.runs_lock:
        run = release.runs.get(run_id)

        if run is None:
            return

        text = json.dumps(run)

    path = RUNS_DIR / f"{run_id}.json"
    temporary = path.with_suffix(".tmp")

    temporary.write_text(text)
    os.replace(temporary, path)


def save_loop():

    # Saving on every log line would mean a write every few seconds per
    # build, so changes are batched and written at most once a second.

    while True:
        with dirty_lock:
            while not dirty:
                dirty_lock.wait()

            run_ids = list(dirty)

            dirty.clear()

        for run_id in run_ids:
            save(run_id)

        time.sleep(SAVE_INTERVAL_SECONDS)


def start():
    RUNS_DIR.mkdir(parents=True, exist_ok=True)

    threading.Thread(daemon=True, target=save_loop).start()
