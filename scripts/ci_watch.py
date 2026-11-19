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


def _gh(*args: str, timeout: int = 120) -> str:
    try:
        result = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError as exc:  # pragma: no cover - environment guard
        raise WatchError("the GitHub CLI (gh) is not installed") from exc
    except subprocess.TimeoutExpired as exc:  # pragma: no cover - slow network
        raise WatchError(f"gh {' '.join(args)} timed out after {timeout}s") from exc
    if result.returncode != 0:
        raise WatchError(f"gh {' '.join(args)} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def resolve_commit(value: str) -> str:
    commit = str(value or "").strip()
    if commit:
        return commit
    out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True)
    if out.returncode != 0:
        raise WatchError("cannot read HEAD; pass --commit")
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


def report(run: dict) -> int:
    """Print the job table and return the exit code the caller should use."""
    run_id = str(run["databaseId"])
    jobs = json.loads(_gh("run", "view", run_id, "--json", "jobs,conclusion,status,url") or "{}")
    print(f"[ci_watch] {run_id} {jobs.get('status')}/{jobs.get('conclusion')} {jobs.get('url') or run.get('url', '')}")
    failed = []
    for job in jobs.get("jobs") or []:
        conclusion = str(job.get("conclusion") or job.get("status") or "")
        print(f"  {conclusion:<12} {job.get('name')}")
        if conclusion not in {"success", "skipped", "neutral"}:
            failed.append(str(job.get("name")))
    if failed:
        print(f"[ci_watch] FAILED job(s): {', '.join(failed)}")
        return 1
    return 0


def watch(workflow: str, commit: str, *, wait: bool) -> int:
    run = find_run(workflow, commit)
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
