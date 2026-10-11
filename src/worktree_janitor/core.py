"""Core logic for worktree-janitor: listing, claim detection, and pruning.

A "claim" is a small JSON file written into a worktree's git admin dir
(the `gitdir` reported by `git worktree list --porcelain`) by the agent
session that created the worktree. It records the session id, the
owner PID, and a heartbeat timestamp.

A worktree is considered "dead" when its claim is stale:
- the owner PID no longer exists, or
- (when no PID is recorded) the heartbeat is older than the configured
  staleness threshold.

Dead claims are how the janitor distinguishes a worktree its session
abandoned from one that is still in use. Worktrees with uncommitted
changes are NEVER pruned, whatever the claim state.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from dataclasses import dataclass
from typing import Optional

CLAIM_FILENAME = "worktree-claim.json"


class GitError(RuntimeError):
    pass


def _git(repo_path: str, *args: str, check: bool = True) -> str:
    """Run `git -C <repo_path> ...` and return stdout."""
    proc = subprocess.run(
        ["git", "-C", repo_path, *args],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if check and proc.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {repo_path}: "
            f"{proc.stderr.strip() or proc.stdout.strip()}"
        )
    return proc.stdout


def repo_root(path: str = ".") -> str:
    """Resolve the top-level directory of the git repo containing path."""
    return os.path.realpath(
        _git(path, "rev-parse", "--show-toplevel").strip()
    )


@dataclass
class Worktree:
    path: str
    gitdir: str
    head: str
    branch: Optional[str]  # None when detached
    bare: bool
    locked: bool

    @property
    def is_main(self) -> bool:
        # The main worktree's admin dir is <repo>/.git itself.
        return os.path.basename(os.path.dirname(self.gitdir)) != "worktrees"


def list_worktrees(repo: str = ".") -> list[Worktree]:
    """Parse `git worktree list --porcelain` into Worktree records."""
    root = repo_root(repo)
    out = _git(root, "worktree", "list", "--porcelain")
    worktrees: list[Worktree] = []
    cur: dict = {}
    for line in out.splitlines():
        if not line.strip():
            if cur:
                worktrees.append(_build_worktree(cur))
                cur = {}
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            cur["path"] = value
        elif key == "gitdir":
            cur["gitdir"] = value
        elif key == "HEAD":
            cur["head"] = value
        elif key == "branch":
            cur["branch"] = value
        elif key == "detached":
            cur["branch"] = None
        elif key == "bare":
            cur["bare"] = True
        elif key == "locked":
            cur["locked"] = True
    if cur:
        worktrees.append(_build_worktree(cur))
    return worktrees


def _gitdir_of(path: str) -> str:
    """Absolute git admin dir for a worktree path."""
    try:
        return _git(path, "rev-parse", "--absolute-git-dir").strip()
    except GitError:
        return ""


def _build_worktree(cur: dict) -> Worktree:
    path = os.path.realpath(cur.get("path", ""))
    gitdir = cur.get("gitdir") or _gitdir_of(path)
    return Worktree(
        path=path,
        gitdir=gitdir,
        head=cur.get("head", ""),
        branch=cur.get("branch"),
        bare=bool(cur.get("bare", False)),
        locked=bool(cur.get("locked", False)),
    )


# ---------------------------------------------------------------------------
# Claims
# ---------------------------------------------------------------------------

def claim_path(wt: Worktree) -> str:
    return os.path.join(wt.gitdir, CLAIM_FILENAME)


def read_claim(wt: Worktree) -> Optional[dict]:
    """Return the claim dict, or None if there is no claim file."""
    path = claim_path(wt)
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def claim(
    worktree_path: str,
    session_id: str,
    note: str = "",
    pid: Optional[int] = None,
) -> dict:
    """Claim a worktree for a session. Returns the claim dict."""
    root = repo_root(worktree_path)
    wts = [w for w in list_worktrees(root) if w.path == os.path.realpath(worktree_path)]
    if not wts:
        raise GitError(f"not a worktree of {root}: {worktree_path}")
    wt = wts[0]
    now = time.time()
    data = {
        "session_id": session_id,
        "pid": pid if pid is not None else os.getpid(),
        "created": now,
        "heartbeat": now,
        "note": note,
    }
    os.makedirs(wt.gitdir, exist_ok=True)
    with open(claim_path(wt), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


def release(worktree_path: str) -> bool:
    """Remove the claim from a worktree. Returns True if one existed."""
    root = repo_root(worktree_path)
    wts = [w for w in list_worktrees(root) if w.path == os.path.realpath(worktree_path)]
    if not wts:
        raise GitError(f"not a worktree of {root}: {worktree_path}")
    path = claim_path(wts[0])
    if os.path.isfile(path):
        os.remove(path)
        return True
    return False


def heartbeat(worktree_path: str) -> dict:
    """Refresh the claim heartbeat. Raises if the worktree is unclaimed."""
    root = repo_root(worktree_path)
    wts = [w for w in list_worktrees(root) if w.path == os.path.realpath(worktree_path)]
    if not wts:
        raise GitError(f"not a worktree of {root}: {worktree_path}")
    data = read_claim(wts[0])
    if data is None:
        raise GitError(f"worktree has no claim to heartbeat: {worktree_path}")
    data["heartbeat"] = time.time()
    with open(claim_path(wts[0]), "w", encoding="utf-8") as fh:
        json.dump(data, fh, indent=2)
    return data


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def claim_status(wt: Worktree, stale_after_minutes: float = 30.0) -> str:
    """Return 'active', 'dead', or 'none' for a worktree's claim."""
    data = read_claim(wt)
    if data is None:
        return "none"
    pid = data.get("pid")
    if isinstance(pid, int) and pid > 0:
        return "active" if _pid_alive(pid) else "dead"
    heartbeat_ts = data.get("heartbeat", 0)
    age = time.time() - float(heartbeat_ts or 0)
    return "active" if age <= stale_after_minutes * 60 else "dead"


# ---------------------------------------------------------------------------
# Worktree state
# ---------------------------------------------------------------------------

def is_dirty(wt: Worktree) -> bool:
    """True when the worktree has uncommitted changes or untracked files."""
    if not os.path.isdir(wt.path):
        return False
    try:
        out = _git(wt.path, "status", "--porcelain", check=False)
    except GitError:
        return False
    return bool(out.strip())


def is_merged(wt: Worktree, base_ref: Optional[str] = None) -> Optional[bool]:
    """True when the worktree HEAD is an ancestor of the base ref.

    Returns None when the check cannot be performed.
    """
    try:
        if base_ref is None:
            # Default: the HEAD of the main worktree of this repo.
            root = repo_root(wt.path)
            wts = list_worktrees(root)
            mains = [w for w in wts if w.is_main and not w.bare]
            base_ref = mains[0].head if mains else "HEAD"
        proc = subprocess.run(
            ["git", "-C", wt.path, "merge-base", "--is-ancestor", wt.head, base_ref],
            capture_output=True,
            timeout=60,
        )
        return proc.returncode == 0
    except (GitError, OSError):
        return None


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

@dataclass
class PrunePlan:
    to_remove: list[Worktree]
    skipped_dirty: list[Worktree]
    skipped_active: list[Worktree]
    skipped_locked: list[Worktree]
    skipped_main: list[Worktree]


def plan_prune(
    repo: str = ".",
    include_unclaimed: bool = False,
    stale_after_minutes: float = 30.0,
) -> PrunePlan:
    """Decide what a prune run would remove, without removing anything."""
    root = repo_root(repo)
    plan = PrunePlan([], [], [], [], [])
    for wt in list_worktrees(root):
        if wt.is_main or wt.bare:
            plan.skipped_main.append(wt)
            continue
        if wt.locked:
            plan.skipped_locked.append(wt)
            continue
        status = claim_status(wt, stale_after_minutes)
        if status == "active":
            plan.skipped_active.append(wt)
            continue
        if status == "none" and not include_unclaimed:
            plan.skipped_active.append(wt)  # unclaimed, left alone by default
            continue
        if is_dirty(wt):
            plan.skipped_dirty.append(wt)
            continue
        plan.to_remove.append(wt)
    return plan


def prune(
    repo: str = ".",
    dry_run: bool = True,
    include_unclaimed: bool = False,
    stale_after_minutes: float = 30.0,
) -> dict:
    """Remove dead-claim worktrees. Returns a report dict.

    Actual removal only happens when dry_run is False. Worktrees with
    uncommitted changes are never removed.
    """
    plan = plan_prune(repo, include_unclaimed, stale_after_minutes)
    removed: list[str] = []
    errors: list[str] = []
    if not dry_run:
        for wt in plan.to_remove:
            try:
                _git(repo, "worktree", "remove", wt.path)
                removed.append(wt.path)
            except GitError as exc:
                errors.append(f"{wt.path}: {exc}")
    return {
        "dry_run": dry_run,
        "removed": removed,
        "errors": errors,
        "planned": [w.path for w in plan.to_remove],
        "skipped_dirty": [w.path for w in plan.skipped_dirty],
        "skipped_active": [w.path for w in plan.skipped_active],
        "skipped_locked": [w.path for w in plan.skipped_locked],
    }
