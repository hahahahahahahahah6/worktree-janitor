"""worktree-janitor CLI: list worktrees with claim status, prune dead ones."""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__
from .core import (
    claim as core_claim,
    claim_status,
    heartbeat as core_heartbeat,
    is_dirty,
    is_merged,
    list_worktrees,
    prune as core_prune,
    release as core_release,
    repo_root,
)


def _status_table(repo: str, stale_after: float, as_json: bool) -> int:
    root = repo_root(repo)
    rows = []
    for wt in list_worktrees(root):
        status = claim_status(wt, stale_after)
        dirty = is_dirty(wt)
        merged = is_merged(wt)
        rows.append(
            {
                "path": wt.path,
                "branch": wt.branch or "(detached)",
                "head": wt.head[:8],
                "claim": status,
                "dirty": dirty,
                "merged": merged,
                "main": wt.is_main,
                "locked": wt.locked,
            }
        )
    if as_json:
        print(json.dumps(rows, indent=2))
        return 0
    header = f"{'PATH':<52} {'BRANCH':<18} {'CLAIM':<8} {'DIRTY':<6} {'MERGED':<7}"
    print(header)
    print("-" * len(header))
    for r in rows:
        merged = "?" if r["merged"] is None else ("yes" if r["merged"] else "no")
        tags = []
        if r["main"]:
            tags.append("main")
        if r["locked"]:
            tags.append("locked")
        path = r["path"] + (f" [{','.join(tags)}]" if tags else "")
        print(
            f"{path:<52} {r['branch']:<18} {r['claim']:<8} "
            f"{'yes' if r['dirty'] else 'no':<6} {merged:<7}"
        )
    return 0


def _prune(repo: str, dry_run: bool, yes: bool, include_unclaimed: bool,
           stale_after: float) -> int:
    effective_dry_run = dry_run or not yes
    report = core_prune(
        repo=repo,
        dry_run=effective_dry_run,
        include_unclaimed=include_unclaimed,
        stale_after_minutes=stale_after,
    )
    if effective_dry_run:
        print("DRY RUN -- nothing removed.")
    for p in report["planned"]:
        print(f"{'would remove' if effective_dry_run else 'removed'}: {p}")
    for p in report["skipped_dirty"]:
        print(f"SKIPPED (uncommitted changes, left alone): {p}", file=sys.stderr)
    for p in report["skipped_active"]:
        print(f"skipped (still claimed / unclaimed): {p}", file=sys.stderr)
    for p in report["skipped_locked"]:
        print(f"skipped (locked): {p}", file=sys.stderr)
    for e in report["errors"]:
        print(f"ERROR: {e}", file=sys.stderr)
    return 1 if report["errors"] else 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(
        prog="wtj",
        description="Janitor for git worktrees left behind by dead agent sessions.",
    )
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--repo", default=".",
                    help="Path inside the repo to operate on (default: cwd).")
    ap.add_argument("--stale-after", type=float, default=30.0,
                    help="Minutes after which a heartbeat-less claim is dead "
                         "(default: 30).")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p_list = sub.add_parser("list", help="List worktrees with claim status.")
    p_list.add_argument("--json", action="store_true",
                        help="Machine-readable output.")

    p_claim = sub.add_parser("claim", help="Claim a worktree for a session.")
    p_claim.add_argument("path", help="Worktree path to claim.")
    p_claim.add_argument("--session-id", default=None,
                         help="Session id (default: current PID as string).")
    p_claim.add_argument("--note", default="", help="Free-text note.")

    p_release = sub.add_parser("release", help="Release a worktree claim.")
    p_release.add_argument("path", help="Worktree path.")

    p_hb = sub.add_parser("heartbeat", help="Refresh a worktree claim heartbeat.")
    p_hb.add_argument("path", help="Worktree path.")

    p_prune = sub.add_parser("prune", help="Remove dead-claim worktrees.")
    p_prune.add_argument("--dry-run", action="store_true",
                         help="Show what would be removed, remove nothing.")
    p_prune.add_argument("--yes", action="store_true",
                         help="Actually remove. Without it, acts as a dry run.")
    p_prune.add_argument("--include-unclaimed", action="store_true",
                         help="Also prune clean worktrees that have no claim.")

    args = ap.parse_args(argv)

    if args.cmd == "list":
        return _status_table(args.repo, args.stale_after, args.json)
    if args.cmd == "claim":
        sid = args.session_id or f"pid:{os.getpid()}"
        data = core_claim(args.path, sid, note=args.note)
        print(f"claimed {os.path.realpath(args.path)} for session {data['session_id']}")
        return 0
    if args.cmd == "release":
        if core_release(args.path):
            print(f"released claim on {os.path.realpath(args.path)}")
        else:
            print("no claim found", file=sys.stderr)
            return 1
        return 0
    if args.cmd == "heartbeat":
        core_heartbeat(args.path)
        print(f"heartbeat updated for {os.path.realpath(args.path)}")
        return 0
    if args.cmd == "prune":
        return _prune(args.repo, args.dry_run, args.yes,
                      args.include_unclaimed, args.stale_after)
    return 2


if __name__ == "__main__":
    sys.exit(main())
