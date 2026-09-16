# Local branch workflow

**Status: normative.** This document is the single source of truth for local branch, worktree,
reference-transition, and task-PR topology. It does not itself configure remote branch protection.

## Branch roles

| Branch | Role | Normal writer |
|---|---|---|
| `main` | Permanent trunk, source for new task branches, and PR target | Remote PR merge; local clones only sync from fetched remote `main` |
| `agent/<agent-id>/<task-slug>` | Isolated work for one agent and one task | That agent only |

`dev` is retired from normal delivery. Do not create or advance it for new work. An existing legacy
local `dev` ref may be deleted after its commits are accounted for; it is not a staging hop between a
worker branch and `main`.

New task branches start from the accepted `main` tip. Completed work is pushed under its `agent/...`
name and opened as a pull request directly against `main`. `main` never becomes a task workspace.

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

The versioned local reference hook enforces this namespace for new local branches. `main` cannot be
created, deleted, renamed, or locally advanced to task work. Legacy `dev` can only be deleted.

## Required setup

Enable the versioned hooks in every clone before creating or changing local branch refs:

```bash
make hooks
git config --get core.hooksPath
```

The second command must print `scripts/githooks` (or its configured equivalent). The setting lives in
`.git/config`, not in a committed repository file.

## Task sequence

### 1. Refresh the local base

Fetch the remote `main`, then explicitly align the local `main` when that is the accepted base:

```bash
git fetch origin main
git switch main
git reset --hard origin/main
```

The reference hook permits this only when the new local `main` value matches a fetched remote-tracking
`*/main` ref. It does not permit a local merge, fast-forward, rebase, cherry-pick, or reset from a task
branch.

**Completion criterion:** `git rev-parse main` equals the fetched remote-tracking `*/main` tip selected
as the task base.

### 2. Give each task an isolated worktree

Create a worker branch from the accepted trunk tip:

```bash
git worktree add ../nvsop-agent-a -b agent/a/<task-slug> main
```

If `main` moves after the task started, do not silently rebase or reset the active worker. Finish it or
explicitly update it as a separate integration decision and rerun affected evidence.

The agent commits only in that worktree. Other tasks use different `agent/<agent-id>/...` branches and
worktrees. Never let two writers share a worktree, index, or worker branch.

**Completion criterion:** the task worktree is cleanly associated with one allowed worker branch and
contains no unrelated work.

### 3. Publish the worker and open a PR to `main`

After the candidate passes its applicable local checks and required review evidence, publish the exact
worker branch:

```bash
git push -u origin agent/a/<task-slug>
gh pr create --base main --head agent/a/<task-slug>
```

The versioned `pre-push` hook rejects any direct push whose destination is `refs/heads/main`, including
`git push origin HEAD:main`. The worker branch remains the publication unit; GitHub CI and required
review run on the pull request to `main`.

**Completion criterion:** the remote worker branch points at the verified candidate and its PR base is
`main`.

### 4. Merge remotely, synchronize local `main`, then retire merged task workspaces

Merge only after the repository-required CI and review evidence is green and merge authorization is
present. After GitHub reports the PR merged, local synchronization and safe task cleanup are part of the
merge closeout rather than a later optional step.

First fetch the accepted trunk and prove that the merged PR head is reachable from it:

```bash
git fetch --prune origin main
MERGED_TARGET=origin/main
CANDIDATE_SHA=<merged-pr-head-sha>
git merge-base --is-ancestor "$CANDIDATE_SHA" "$MERGED_TARGET"
```

Then synchronize the dedicated primary `main` worktree to that fetched commit. Identify that worktree
from `git worktree list --porcelain`; require it to be on `main` and completely clean first. If either
check fails, preserve the worktree and stop instead of resetting or cleaning it.

```bash
MAIN_WORKTREE=<primary-main-worktree>
test "$(git -C "$MAIN_WORKTREE" branch --show-current)" = main
test -z "$(git -C "$MAIN_WORKTREE" status --porcelain=v1 --untracked-files=all)"
git -C "$MAIN_WORKTREE" reset --hard "$MERGED_TARGET"
test "$(git -C "$MAIN_WORKTREE" rev-parse HEAD)" = "$(git rev-parse "$MERGED_TARGET")"
```

After `main` is synchronized, inspect all local `agent/*` task branches and their registered worktrees.
Retire every eligible merged task, including the just-merged task and any older merged residue, only when
all of these are true:

- GitHub reports that task's PR state as `MERGED`; ancestry by itself never classifies a task as merged;
- the **current local task-branch tip** is reachable from fetched `origin/main`, so no later local commits
  would be discarded;
- its task worktree, when present, is clean including untracked files; and
- its worktree is a dedicated task worktree, never the primary `main` checkout.

Use the fetched trunk to discover ancestry candidates, confirm each candidate's PR state, then remove each
eligible worktree from a different worktree and use ordinary safe branch deletion. A branch with no
registered worktree still requires both the merged-PR proof and ancestry proof before safe deletion.

```bash
git branch --merged "$MERGED_TARGET" --list 'agent/*'
git worktree list --porcelain
gh pr list --state merged --base main --head "$TASK_BRANCH" --json number,state,headRefName,headRefOid

git merge-base --is-ancestor "$TASK_BRANCH" "$MERGED_TARGET"
test -z "$(git -C "$TASK_WORKTREE" status --porcelain=v1 --untracked-files=all)"
git worktree remove "$TASK_WORKTREE"
git branch -d "$TASK_BRANCH"
```

Preserve a task when no merged PR is proven for it, its current branch tip is not contained in
`origin/main`, or its worktree has tracked or untracked changes. Those conditions identify ongoing,
unmerged, or divergent work. Leave its branch and worktree untouched; a cleanup pass never uses
`git clean`, task-worktree resets, forced worktree removal, or forced branch deletion to make the task
look merged. `git fetch --prune` may remove stale remote-tracking refs after GitHub deletes a merged head
branch, but remote branch deletion is separate from this local cleanup.

**Completion criterion:** local `main` exactly matches fetched `origin/main`; every eligible merged local
task is retired without force; every unmerged, divergent, or dirty task remains unchanged.

## Legacy `dev` handling

`dev` is not part of the current task flow. The local reference hook:

- rejects creating a new local `dev` ref;
- rejects advancing or otherwise changing an existing local `dev` ref;
- permits deleting an existing legacy local `dev` ref so stale integration state can be retired.

Before deleting a legacy `dev`, account for any commits that are not already represented by `main` or
an active task branch. Deleting or migrating historical work is a separate repository operation; do not
silently discard it while starting a new task.

## Mechanical enforcement

`scripts/githooks/reference-transaction` validates local ref transactions during Git's `prepared`
phase. It enforces the ref movement rather than relying on a commit message or agent instruction:

- `main` cannot be created, deleted, renamed, or locally advanced to task work;
- local `main` may move only to an exact commit already present at a fetched remote-tracking `*/main`;
- `dev` is retired: creation and advancement are rejected, deletion of an existing legacy ref is allowed;
- new local task branches must be `agent/<agent-id>/<task-slug>`;
- `--no-verify` does not bypass the reference-transaction check.

`pre-commit` gives an earlier readable failure for direct commits on `main` or legacy `dev`.
`pre-push` rejects direct pushes to remote `main` and directs the caller to publish the worker branch
and open a PR against `main`. Vendor/format checks remain separate local checks. Removing or replacing
`core.hooksPath` is outside what a repository-owned local hook can prevent; machine-level enforcement
would require a managed Git wrapper or server-side branch protection.
