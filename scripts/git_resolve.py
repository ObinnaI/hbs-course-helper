#!/usr/bin/env python3
"""
git_resolve.py — finish a `git pull --rebase` that stopped on conflicts.

Why: the cloud refresh and the Mac mirror both commit to the data repo. When
the Mac pushes while a cloud run is in flight (a role sheet dropped in iCloud,
a hand-written one-pager), the run's final `git pull --rebase` can stop on a
conflict — and on 2026-11-12 that threw away 70 minutes of work, two podcasts
included. Nothing in this repo needs a human to pick a side: generated files
belong to whoever made them last, and the JSON state files just need both
sides' keys.

Run it inside the repository right after the rebase stops. For every
unmerged path:
  * claude/*.json state files are union-merged: keys from both sides, and
    where both changed the same key the replayed commit's value wins;
  * everything else takes the replayed commit's copy. During a rebase
    "theirs" is the local commit being replayed (the run's or the Mac's own
    work) and "ours" is upstream, so this keeps what the caller just made.
The paths are staged and the rebase continued, repeating if later commits
conflict too.

Exit status: 0 when a rebase that was in progress has finished, 2 when no
rebase was in progress (the pull failed for another reason, e.g. offline),
1 when the conflicts could not be settled (the caller should abort).
"""
import json
import os
import subprocess
import sys
from pathlib import Path

STATE_PREFIX = "claude/"


def _git(*args, check=True):
    return subprocess.run(["git", *args], check=check, capture_output=True, text=True)


def rebase_in_progress() -> bool:
    git_dir = _git("rev-parse", "--git-dir").stdout.strip()
    return any(Path(git_dir, d).exists() for d in ("rebase-merge", "rebase-apply"))


def unmerged_paths() -> list:
    out = _git("diff", "--name-only", "--diff-filter=U").stdout
    return [p for p in out.splitlines() if p]


def union(a, b):
    """Deep union of two JSON values: dict keys from both sides, b wins elsewhere."""
    if isinstance(a, dict) and isinstance(b, dict):
        merged = dict(a)
        for key, value in b.items():
            merged[key] = union(a[key], value) if key in a else value
        return merged
    return b


def is_state_json(path: str) -> bool:
    return path.startswith(STATE_PREFIX) and path.endswith(".json")


def _stage(path: str, n: int):
    r = subprocess.run(["git", "show", f":{n}:{path}"], capture_output=True)
    return r.stdout if r.returncode == 0 else None


def resolve_one(path: str) -> str:
    ours, theirs = _stage(path, 2), _stage(path, 3)      # 2 = upstream, 3 = the replayed commit
    if theirs is None:                                    # the replayed commit deleted it
        _git("rm", "-q", "--cached", "--", path)
        Path(path).unlink(missing_ok=True)
        return "removed"
    if is_state_json(path):
        try:
            merged = union(json.loads(ours or b"{}"), json.loads(theirs))
        except ValueError:
            merged = None
        if merged is not None:
            Path(path).write_text(json.dumps(merged, indent=2, sort_keys=True) + "\n")
            _git("add", "--", path)
            return "union-merged"
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    Path(path).write_bytes(theirs)
    _git("add", "--", path)
    return "kept the replayed commit's copy"


def main(argv=None) -> int:
    if not rebase_in_progress():
        print("git_resolve: no rebase in progress — nothing to resolve")
        return 2
    env = dict(os.environ, GIT_EDITOR="true")
    for _round in range(50):
        paths = unmerged_paths()
        for path in paths:
            print(f"  ⇄ {path}: {resolve_one(path)}")
        if not paths and not _git("diff", "--cached", "--quiet", check=False).returncode:
            # The replayed commit became empty (upstream already had it): skip it.
            r = subprocess.run(["git", "rebase", "--skip"], env=env, capture_output=True, text=True)
        else:
            r = subprocess.run(["git", "rebase", "--continue"], env=env, capture_output=True, text=True)
        if not rebase_in_progress():
            print("git_resolve: rebase finished")
            return 0
        if r.returncode and not unmerged_paths():
            print(f"git_resolve: rebase could not continue: {(r.stderr or r.stdout).strip()[-400:]}")
            return 1
    print("git_resolve: gave up after 50 rounds")
    return 1


if __name__ == "__main__":
    sys.exit(main())
