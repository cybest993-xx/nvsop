# Local branch workflow

**Status: normative.** This document is the single source of truth for the local branch, worktree,
and reference-transition rules. It does not configure or describe remote branch protection.

## Branch roles

| Branch | Role | Normal writer |
|---|---|---|
| `main` | Permanent local promotion target | The integration operator only |
| `dev` | The single local integration branch | The integration operator only |
| `agent/<agent-id>/<task-slug>` | Isolated work for one agent and one task | That agent only |

`main` and `dev` are shared refs, not task workspaces. New local branches and worker renames must
use `dev` or the worker pattern below. Existing legacy branch names may be deleted, but they must
not be advanced as task branches.

### Worker branch names

Use lowercase ASCII in both variable parts:

```text
agent/<agent-id>/<task-slug>
```

Each part starts with a letter or digit and then contains only letters, digits, `.`, `_`, or `-`.
Do not put another `/` inside either part. Examples:

```text
agent/pi/auth-bootstrap
agent/camera-2/reconnect-timeout
```

The versioned local reference hook enforces this namespace for new local branch refs. Use the
same pattern when renaming a worker branch. Git has no pre-branch hook for the destination of its
built-in worker-branch rename operation; `main` and `dev` renames are nevertheless hard-blocked by
their protected-ref deletion checks.

## Required setup

Enable the versioned hooks in every clone before creating or changing shared refs:

```bash
make hooks
git config --get core.hooksPath
```

The second command must print `scripts/githooks` (or its configured equivalent). The first command
is complete only when that local configuration exists; the setting is intentionally kept in
`.git/config`, not committed to the repository.

If the local `dev` branch does not exist, create it from the fetched development ref or the local
`main` ref:

```bash
git fetch origin main dev
git branch dev origin/dev       # when origin/dev exists
git branch dev main            # otherwise
```

The hook permits only those initial sources for `dev`. It never permits deleting or renaming
`main` or `dev`.

## Multi-agent sequence

### 1. Refresh the local base

Fetch the remote `main`, then explicitly replace the local `main` when that is the intended base:

```bash
git fetch origin main
git switch main
git reset --hard origin/main
```

**Completion criterion:** `git rev-parse main` equals the fetched remote-tracking `*/main` ref.
This is the one intentional operation that may overwrite the local `main` history.

If `dev` contains work that must be retained, integrate the refreshed `main` into `dev` with the
integration procedure below; do not reset `dev` over that work.

### 2. Give each agent an isolated worktree

Create a worker branch from the current integration tip:

```bash
git worktree add ../nvsop-agent-a -b agent/a/<task-slug> dev
```

The agent commits only in that worktree. Other agents use different `agent/<agent-id>/...`
branches and worktrees. Never let two agents share a worktree, index, or worker branch.

**Completion criterion:** the agent's worktree is cleanly associated with one allowed worker branch,
and the shared `dev` worktree is not used for task edits.

### 3. Integrate workers serially into `dev`

One integration operator owns the `dev` worktree and merges completed worker branches one at a
time:

```bash
git switch dev
git merge --no-ff agent/a/<task-slug>
git merge --no-ff agent/b/<task-slug>
```

A fast-forward to the exact current source tip is also accepted by the hook, because Git has no
local pre-merge hook for fast-forward ref updates. Prefer `--no-ff` so each integration remains
visible in history.

**Completion criterion:** each accepted `dev` update is either a merge from the current `main` tip
or a merge/fast-forward from the current tip of an `agent/...` branch. Direct commits on `dev` are
rejected.

After verification, delete a worker branch only when its work is safely represented in `dev`:

```bash
git branch -d agent/a/<task-slug>
git worktree remove ../nvsop-agent-a
```

### 4. Promote `dev` into `main`

When the integration state is accepted:

```bash
git switch main
git merge --no-ff dev
```

A fast-forward to the exact current `dev` tip is also valid. The hook rejects every other local
`main` update, including direct commits, resets to worker branches, rebases, cherry-picks,
reverts, and branch deletion or renaming.

**Completion criterion:** the new `main` tip is the current `dev` tip or a merge commit whose second
parent is the current `dev` tip.

## Mechanical enforcement

`scripts/githooks/reference-transaction` validates local ref transactions during Git's `prepared`
phase. It enforces the actual ref movement rather than relying on a commit message or agent
instruction:

- `main` cannot be created, deleted, renamed, or directly advanced;
- `main` may move to the current `dev` tip, merge the current `dev` tip, or match a fetched remote
  tracking `*/main` ref;
- `dev` cannot be deleted or renamed and may advance only from `main` or an `agent/...` tip;
- new local branches must be `dev` or `agent/<agent-id>/<task-slug>`;
- `--no-verify` does not bypass this ref-transition check.

`pre-commit` remains an early, readable failure for direct commits on `main` or `dev`; the reference hook is
the final local guard. The existing `pre-push` and vendor/format checks remain separate local
checks. Removing or replacing `core.hooksPath` is outside what a repository-owned local hook can
prevent; a machine-level managed Git wrapper would be required for anti-tamper enforcement.
