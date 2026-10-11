# worktree-janitor

Dead-session worktree cleanup for git. Multi-session agent fleets create
`git worktree`s all the time -- and when a session dies, its worktrees leak:
stale checkouts pile up, branches linger, and nobody remembers which
worktree belongs to a live session. `worktree-janitor` is the CLI that
finds them and prunes them, safely.

```bash
pip install worktree-janitor

wtj list                          # worktrees + claim status
wtj claim ./my-wt --session-id s1 # claim a worktree for your session
wtj heartbeat ./my-wt             # refresh the claim while you work
wtj prune --dry-run               # see what would go
wtj prune --yes                   # remove dead-claim worktrees
```

## How it works

A **claim** is a small JSON file (`worktree-claim.json`) stored in the
worktree's git admin dir (the `gitdir` reported by
`git worktree list --porcelain`), written by the session that created the
worktree. It records the session id, the owner PID, and a heartbeat
timestamp.

A claim is **dead** when:
- the owner PID no longer exists, or
- (no PID recorded) the heartbeat is older than `--stale-after` minutes
  (default 30).

`wtj prune` removes worktrees whose claim is dead, and leaves everything
else alone. The hard rule: **a worktree with uncommitted changes is never
pruned**, no matter what. It is reported as skipped with a warning. The
main worktree and locked worktrees are never touched either. Clean,
unclaimed worktrees are skipped by default too; pass
`--include-unclaimed` to prune those as well.

`prune` is report-only unless you pass `--yes` (or use `--dry-run` to
preview).

## Usage

```
wtj list [--json] [--stale-after MIN]
wtj claim <path> [--session-id ID] [--note TEXT]
wtj heartbeat <path>
wtj release <path>
wtj prune [--dry-run] [--yes] [--include-unclaimed] [--stale-after MIN]
wtj --repo /path/to/repo <command>   # operate on any repo
```

## Differentiation

There are two sane shapes for worktree hygiene:

- **Claim guards (interception style)**, e.g. Ben Newton's worktree claim
  guard Mod for session-fleet: they intercept at *creation time* and stop a
  session from touching a worktree another live session owns. Prevention.
- **worktree-janitor (cleanup style)**: it runs *after the fact*. It does
  not hook into your session runner or block anything -- it scans,
  classifies, and one-shot prunes what is verifiably dead. Janitor, not
  bouncer.

The two compose: guard at creation, janitor on a cron or after a session
fleet shuts down. This project is a sibling of `routine-sweeper` -- the
same "session hygiene" family, but for git worktrees instead of routine
files.

## Safety model

- dirty worktrees (uncommitted changes or untracked files) are never pruned
- the main worktree and `bare` repos are never pruned
- locked worktrees are skipped
- active claims are skipped
- unclaimed worktrees are skipped unless `--include-unclaimed`
- `--yes` is required for any deletion; default is report-only

## Development

```bash
python -m pytest tests/ -q   # 12 smoke tests, stdlib only
```

No dependencies -- the package is stdlib-only on purpose (it should work
in any agent sandbox without a network install).

## License

MIT. See [LICENSE](LICENSE).
