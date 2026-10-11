"""Smoke tests for worktree-janitor. All fixtures use real temporary git repos."""

import json
import os
import subprocess
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from worktree_janitor.core import (  # noqa: E402
    CLAIM_FILENAME,
    claim as core_claim,
    claim_status,
    heartbeat as core_heartbeat,
    is_dirty,
    list_worktrees,
    prune,
    release as core_release,
)


def _git(cwd, *args):
    subprocess.run(["git", "-C", cwd, *args], check=True,
                   capture_output=True, text=True)


def make_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(str(repo), "init", "-b", "main")
    _git(str(repo), "config", "user.email", "test@example.com")
    _git(str(repo), "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n")
    _git(str(repo), "add", ".")
    _git(str(repo), "commit", "-m", "init")
    return str(repo)


def add_worktree(repo, name, branch):
    wt_path = os.path.join(os.path.dirname(repo), name)
    _git(repo, "worktree", "add", wt_path, "-b", branch)
    return wt_path


def find_wt(repo, path):
    path = os.path.realpath(path)
    for wt in list_worktrees(repo):
        if wt.path == path:
            return wt
    raise AssertionError(f"worktree not found: {path}")


def dead_pid():
    # A PID that cannot exist on a normal machine.
    return 999_999_999


# ---------------------------------------------------------------------------
# Claim detection
# ---------------------------------------------------------------------------

def test_claim_active_with_live_pid(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt1", "feat-1")
    core_claim(wt_path, "sess-live", pid=os.getpid())
    assert claim_status(find_wt(repo, wt_path)) == "active"


def test_claim_dead_with_dead_pid(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt2", "feat-2")
    core_claim(wt_path, "sess-dead", pid=dead_pid())
    assert claim_status(find_wt(repo, wt_path)) == "dead"


def test_claim_dead_with_stale_heartbeat(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt3", "feat-3")
    core_claim(wt_path, "sess-stale", pid=dead_pid())
    # Rewrite the claim with no PID and an ancient heartbeat.
    wt = find_wt(repo, wt_path)
    claim_file = os.path.join(wt.gitdir, CLAIM_FILENAME)
    with open(claim_file, "w") as fh:
        json.dump({"session_id": "sess-stale", "pid": None,
                   "created": 0, "heartbeat": 1, "note": ""}, fh)
    assert claim_status(find_wt(repo, wt_path), stale_after_minutes=30) == "dead"


def test_claim_active_with_fresh_heartbeat(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt4", "feat-4")
    core_claim(wt_path, "sess-fresh", pid=dead_pid())
    wt = find_wt(repo, wt_path)
    claim_file = os.path.join(wt.gitdir, CLAIM_FILENAME)
    with open(claim_file, "w") as fh:
        json.dump({"session_id": "sess-fresh", "pid": None,
                   "created": time.time(), "heartbeat": time.time(),
                   "note": ""}, fh)
    assert claim_status(find_wt(repo, wt_path), stale_after_minutes=30) == "active"


def test_claim_none_when_no_claim_file(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt5", "feat-5")
    assert claim_status(find_wt(repo, wt_path)) == "none"


def test_heartbeat_refreshes_claim(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt6", "feat-6")
    core_claim(wt_path, "sess-hb", pid=os.getpid())
    before = core_heartbeat(wt_path)["heartbeat"]
    assert before <= time.time()
    assert claim_status(find_wt(repo, wt_path)) == "active"


def test_release_removes_claim(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt7", "feat-7")
    core_claim(wt_path, "sess-rel", pid=os.getpid())
    assert core_release(wt_path) is True
    assert claim_status(find_wt(repo, wt_path)) == "none"


# ---------------------------------------------------------------------------
# Pruning
# ---------------------------------------------------------------------------

def test_prune_removes_dead_clean_worktree(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt8", "feat-8")
    core_claim(wt_path, "sess-dead", pid=dead_pid())
    report = prune(repo=repo, dry_run=False)
    assert wt_path in report["removed"]
    assert not os.path.isdir(wt_path)


def test_prune_skips_dirty_worktree_with_warning(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt9", "feat-9")
    core_claim(wt_path, "sess-dead", pid=dead_pid())
    with open(os.path.join(wt_path, "uncommitted.txt"), "w") as fh:
        fh.write("do not delete me\n")
    assert is_dirty(find_wt(repo, wt_path))
    report = prune(repo=repo, dry_run=False)
    assert wt_path in report["skipped_dirty"]
    assert os.path.isdir(wt_path), "dirty worktree must never be pruned"


def test_dry_run_deletes_nothing(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt10", "feat-10")
    core_claim(wt_path, "sess-dead", pid=dead_pid())
    report = prune(repo=repo, dry_run=True)
    assert wt_path in report["planned"]
    assert report["removed"] == []
    assert os.path.isdir(wt_path)


def test_prune_skips_unclaimed_by_default(tmp_path):
    repo = make_repo(tmp_path)
    wt_path = add_worktree(repo, "wt11", "feat-11")
    report = prune(repo=repo, dry_run=False, include_unclaimed=False)
    assert os.path.isdir(wt_path)
    assert wt_path not in report["removed"]
    report2 = prune(repo=repo, dry_run=False, include_unclaimed=True)
    assert wt_path in report2["removed"]


def test_prune_never_touches_main_worktree(tmp_path):
    repo = make_repo(tmp_path)
    # Even an unclaimed, clean setup must not remove the main checkout.
    report = prune(repo=repo, dry_run=False, include_unclaimed=True)
    assert os.path.isdir(repo)
    mains = [w for w in list_worktrees(repo) if w.is_main]
    assert len(mains) == 1
