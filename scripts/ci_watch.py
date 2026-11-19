#!/usr/bin/env python3
"""Watch the GitHub Actions run for a commit -- the gate that follows a push.

Why this exists
---------------
The 2026-11-19 push looked green from here: the full local suite passed
(7623 passed), the pre-commit hook ran seven gates, and `git push` returned a
clean fast-forward. CI still went red in 1m27s -- `environment_preflight.ps1`
rejected a healthy `curl_cffi 0.17.0` because a third copy of the accepted range
lived in a PowerShell file no local gate reads.

That class of failure is only visible on the runner, so "watch the run" is not
optional aftercare; it is the last gate. This script makes it one command
instead of a browser tab.

    python scripts/ci_watch.py                 # newest run for HEAD, wait
    python scripts/ci_watch.py --no-wait       # report the current state
    python scripts/ci_watch.py --commit <sha>  # a specific commit
    python scripts/ci_watch.py --workflow release.yml

Exit codes: 0 the run is green, 1 the run failed, 2 usage or `gh` problem.
Requires the GitHub CLI, authenticated (`gh auth status`).
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import time

DEFAULT_WORKFLOW = "ci"
#: A run is created a few seconds after the push lands; polling too fast reports
#: "no run" for a workflow that is about to exist.
RUN_APPEAR_TIMEOUT_SECONDS = 180
POLL_SECONDS = 10


class WatchError(RuntimeError):
    """A usage or tooling problem -- never a CI verdict."""


#: GitHub's API drops a connection now and then (`EOF`, 502/503). Retrying is
#: the difference between "CI is red" and "the reporter could not ask", and the
#: second one must not look like the first.
TRANSIENT_GH_MARKERS = ("EOF", "connection reset", "502", "503", "504", "timeout")
GH_ATTEMPTS = 3
GH_RETRY_SECONDS = 3


def _gh(*args: str, timeout: int = 120) -> str:
    last: WatchError | None = None
    for attempt in range(1, GH_ATTEMPTS + 1):
        try:
            return _gh_once(*args, timeout=timeout)
        except WatchError as exc:
            last = exc
            transient = any(marker in str(exc) for marker in TRANSIENT_GH_MARKERS)
            if not transient or attempt == GH_ATTEMPTS:
                raise
            time.sleep(GH_RETRY_SECONDS)
    raise last or WatchError("gh failed")  # pragma: no cover - loop always returns or raises


def _gh_once(*args: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(
            ["gh", *args],
            capture_output=True,
            # gh prints UTF-8; the default locale codec (cp936 here) raises
            # UnicodeDecodeError on an em dash inside a commit subject.
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
        )
    except FileNotFoundError as exc:  # pragma: no cover - environment guard
        raise WatchError("the GitHub CLI (gh) is not installed") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - slow network
        raise WatchError(f"gh {' '.join(args)} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise WatchError(f"gh {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def resolve_commit(value: str) -> str:
    """The full commit sha for ``value`` (or HEAD).

    ``gh run list --commit`` matches the *full* sha: a short sha silently
    returns ``[]``, which would look exactly like "CI never started".
    """
    ref = str(value or "").strip() or "HEAD"
    out = subprocess.run(
        ["git", "rev-parse", "--verify", f"{ref}^{{commit}}"],
        capture_output=True,
        encoding="utf-8",
        errors="replace",
    )
    if out.returncode != 0:
        raise WatchError(f"cannot resolve {ref!r} to a commit")
    return out.stdout.strip()


def find_run(workflow: str, commit: str, *, wait_seconds: int = RUN_APPEAR_TIMEOUT_SECONDS) -> dict:
    """The newest run of ``workflow`` for ``commit``, waiting for it to appear."""
    deadline = time.time() + max(0, wait_seconds)
    while True:
        raw = _gh(
            "run",
            "list",
            "--workflow",
            workflow,
            "--commit",
            commit,
            "--limit",
            "1",
            "--json",
            "databaseId,status,conclusion,headSha,displayTitle,url,event",
        )
        runs = json.loads(raw or "[]")
        if runs:
            return runs[0]
        if time.time() >= deadline:
            raise WatchError(f"no {workflow} run for {commit[:12]} after {wait_seconds}s")
        time.sleep(POLL_SECONDS)


GREEN = {"success", "skipped", "neutral"}


def report(run: dict) -> int:
    """Print the job table and return the exit code the caller should use.

    The verdict comes from the **run**, not from counting jobs: a queued run has
    no per-job conclusion yet, and a job that is merely ``skipped`` is not a
    failure. An unfinished run is reported as such (exit 2) rather than being
    silently counted as green.
    """
    run_id = str(run["databaseId"])
    details = json.loads(_gh("run", "view", run_id, "--json", "jobs,conclusion,status,url") or "{}")
    status = str(details.get("status") or run.get("status") or "")
    conclusion = str(details.get("conclusion") or run.get("conclusion") or "")
    print(f"[ci_watch] {run_id} {status}/{conclusion} {details.get('url') or run.get('url', '')}")
    for job in details.get("jobs") or []:
        state = str(job.get("conclusion") or job.get("status") or "")
        print(f"  {state:<12} {job.get('name')}")
    if status != "completed":
        print("[ci_watch] run is not finished yet; re-run without --no-wait to wait for it")
        return 2
    if conclusion in GREEN:
        return 0
    print(f"[ci_watch] FAILED run ({conclusion}); inspect with: gh run view {run_id} --log-failed")
    return 1


def watch(workflow: str, commit: str, *, wait: bool) -> int:
    run = find_run(workflow, commit, wait_seconds=RUN_APPEAR_TIMEOUT_SECONDS if wait else 0)
    run_id = str(run["databaseId"])
    if wait and str(run.get("status")) != "completed":
        print(f"[ci_watch] watching {run_id} ({run.get('displayTitle', '')[:60]})")
        # ``--exit-status`` makes gh itself fail on a red run; the code below
        # still reports the per-job table, which is what a reader needs.
        subprocess.run(["gh", "run", "watch", run_id, "--exit-status", "--interval", str(POLL_SECONDS)], check=False)
        run = find_run(workflow, commit, wait_seconds=0)
    return report(run)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Watch the CI run for a commit.")
    parser.add_argument("--workflow", default=DEFAULT_WORKFLOW, help="workflow file name (default: ci)")
    parser.add_argument("--commit", default="", help="commit sha (default: HEAD)")
    parser.add_argument("--no-wait", action="store_true", help="report the current state without waiting")
    args = parser.parse_args(argv)
    try:
        return watch(args.workflow, resolve_commit(args.commit), wait=not args.no_wait)
    except WatchError as exc:
        print(f"[ci_watch] {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry
    raise SystemExit(main())
