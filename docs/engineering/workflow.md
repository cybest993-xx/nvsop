# Engineering delivery workflow

Status: **normative**. This is the home for task isolation, verification, review, PR/CI, merge and local cleanup. Read the relevant section for the current step; product acceptance belongs to [the roadmap](../design/solution-and-roadmap.md), and Issue state to [issues.md](issues.md). The [Makefile](../../Makefile), [CI workflow](../../.github/workflows/blocking-ci.yml) and [versioned hooks](../../scripts/githooks/) own executable behavior.

## 1. Establish the task workspace

Inspect the actual branch, HEAD, worktree, existing changes and complete acceptance criteria. Continue an existing task in its existing worktree. New work starts from the accepted fetched `main` tip on one `agent/<agent-id>/<task-slug>` branch in its own worktree. Both variable parts start with a lowercase ASCII letter or digit and contain only lowercase letters, digits, `.`, `_` or `-`, with no further `/`.

`main` is the shared trunk and PR target, never a task workspace or direct-push destination. `dev` is retired; an existing legacy ref can only be deleted after its commits are accounted for. Preserve unrelated work without auto-stashing, overwriting or cleaning it. Never share a writer's worktree, index or branch.

Before changing branches, worktrees or refs, enable the versioned hooks:

```sh
make hooks
git config --get core.hooksPath
git status --short
git worktree list --porcelain
git fetch origin main
```

The hook path must resolve to `scripts/githooks` or its configured equivalent. Locate the dedicated primary `main` worktree from the actual list. Synchronize it only when it is on `main` and clean, including untracked files; otherwise preserve it and stop that synchronization. Never reset a task worktree to obtain a clean base.

```sh
MAIN_WORKTREE=<primary-main-worktree>
test "$(git -C "$MAIN_WORKTREE" branch --show-current)" = main
test -z "$(git -C "$MAIN_WORKTREE" status --porcelain=v1 --untracked-files=all)"
git -C "$MAIN_WORKTREE" reset --hard origin/main
git -C "$MAIN_WORKTREE" worktree add ../nvsop-task -b agent/a/<task-slug> main
```

These are conditional operation examples, not a script to paste without checking each result. If `main` moves after the task starts, keep the task's fixed base; an integration update is a separate explicit decision with affected evidence rechecked.

**Done:** one task owns one branch/worktree, its exact base is known and unrelated work is preserved.

## 2. Implement and select evidence

Before editing, record Entry / Reuse / Allowed writes / Preserve as defined in [coding.md](coding.md). State risk, intended test reuse/additions and intended checks in at most three lines. Classify behavior and failure paths, not file extensions.

### Choose evidence by risk

- **Low risk:** copy, pure styling or cleanup without behavior change normally adds no automated tests. Use applicable documentation/static checks. Normative instructions, security rules and gate policy are not low risk merely because they are Markdown.
- **Ordinary bounded behavior:** reuse valid tests first. Zero additions can be sufficient; an uncovered behavior normally needs 1–3 independent scenarios. Before exceeding that budget, identify the additional risk each scenario proves.
- **Confirmed defect or changed critical safety invariant:** demonstrate the failure at its owning public seam, implement the minimum correction and rerun that red/green evidence.

Every new scenario must explain which failure it prevents and why existing evidence misses it. Field counts, coverage cosmetics, private helper shapes and hypothetical behavior outside scope are not acceptance criteria.

### Placement and evidence level

| Evidence | Owner and purpose |
|---|---|
| Unit | Deterministic module behavior in the owning app/package test tree; sole-owner Vue component tests may be colocated |
| Integration | One application plus a real adapter or local infrastructure; replacements only at recorded seams |
| Contract | External/inter-process assumptions; `tests/contract/base/` includes NVIDIA assumptions and registered patch/reimplementation regressions, both mandatory after every subtree update |
| System/browser | User-visible flows across applications and published interfaces |
| Performance/hardware | Explicit environment, model/digest, data set, p50/p95/p99, queue depth and pass budget |

Critical risks still require evidence at their owning seam; the ordinary scenario budget does not waive them. Product-specific safety invariants, accepted sequences and failure semantics are owned by the affected [mechanism specifications](../design/solution-and-roadmap.md) and ADRs. Follow those sources instead of copying their acceptance details into this workflow.

Use real infrastructure or adapter-backed integration evidence for affected persistence, authorization integration, queue/outbox behavior, disposal across restart, execution-right uniqueness/lease expiry, evidence protection and supervisor persistence of judgment effects. A pure-rule test does not replace that integration evidence. Preserve applicable browser/UI coverage when the risk crosses the browser or applications.

Synthetic tests cannot prove target-hardware or field acceptance. The current MVP software/validation split and exact deferred criteria are owned by [roadmap §8–9](../design/solution-and-roadmap.md#八开发路线) and the [target-environment validation matrix](../research/target-environment-validation-matrix.md). Keep every deferred criterion linked to an open validation owner; necessary software integration evidence stays with implementation.

Fixtures are minimal, deterministic, synthetic or sanitized and provenance-documented. Customer media, credentials, model weights and production exports are not fixtures. Prefer complete output assertions; reuse helpers; put tests outside production files. Configure a unit through its public/configuration seam, not by mutating process environment variables.

### Stable commands

The [Makefile](../../Makefile) is the local/CI command interface. During iteration use the smallest affected targets and their prerequisites; inspect their definitions rather than assuming every target installs dependencies.

| Command | Use |
|---|---|
| `make docs-check` | Offline, standard-library documentation links and index check; not the final documentation gate |
| `make check-docs` | Final documentation lane: frozen Python resolution, hooks, policy/documentation checks, secrets and diff whitespace |
| `make check` | Final CPU-only, Docker-free code gate |
| `make check-integration` | Center integration/system evidence with real local infrastructure |
| `make media-system` | Real fixed-digest MediaMTX recording/playback evidence; missing live URLs are not counted as a pass |
| `make web-e2e` | Browser scenarios |
| `make web-e2e-whep` | Real MediaMTX WHEP browser evidence for media-owned changes |
| `make ci-plan BASE=<sha> HEAD=<sha>` | Read-only report of the same CI scope selector used by GitHub Actions |
| `make ci-lint` | Offline GitHub Actions static lint after one explicit `make ci-tools` install of the pinned binary |
| `make pr-check PR=<number>` | Read-only machine-state preflight; never substitutes for independent review or merge authorization |
| `make change-size` | Advisory size report from `BASE`, default `origin/main` |
| `make contracts` | Generated OpenAPI compatibility and Web client; procedure in [maintenance.md](maintenance.md#generated-contracts) |

Frozen `uv.lock` and `pnpm-lock.yaml` own resolution. Do not replace applicable checks with ad-hoc installs. Save task-branch commits when useful; they record progress, not acceptance.

**Done:** all owned acceptance criteria and required current-phase evidence are accounted for, with actual commands/results or explicit gaps. Stop optional tests/refactors once this is true.

## 3. Review and repair

Self-review the complete diff, including untracked additions, affected callers and references. Require one consolidated **independent read-only review** for critical risks, public/cross-module interfaces, shared behavior, dependencies, CI, deployment, `vendor/`, repository policy, agent instructions or unbounded impact. Ordinary bounded changes need no independent reviewer by default.

The reviewer reports **Spec** and **Standards** together against a fixed base/candidate and consumes still-valid evidence instead of rerunning everything. Use an independent Workflow Session in the current window on the actual task worktree; the reviewer reads only and the main implementation session owns repairs. A session identifier alone is not proof of an independent reviewer: report whether review was separately executed or only a self-review. Do not move repairs to a review worktree.

Repair concrete findings as one bounded batch, rerun affected checks and review the repair increment plus affected callers. A missing-test finding blocks only for a concrete failure path, uncovered critical risk or unmet acceptance. Naming, size and optional cleanup block only when tied to a defect or violated invariant. Missing required review/validation keeps merge readiness pending.

**Done:** findings are resolved or explicitly blocking, and evidence covers the candidate's code, tests, configuration, dependencies and relevant external inputs. Evidence does not survive changes to those covered inputs.

## 4. Publish the candidate and evaluate CI

Publication requires user authorization for the action and target. Publish only the task branch and open its PR directly against `main`; never push `HEAD:main`. Use the [PR template](../../.github/pull_request_template.md) to record outcome, scope, risks, actual evidence and documentation impact, not another full rulebook.
Before requesting merge, `make pr-check PR=<number>` may aggregate the PR head/base, `CI required`, branch-protection visibility and local candidate identity. Treat `unknown` protection or a missing check as blocked. Its output deliberately leaves independent review as manual confirmation and never grants merge authorization.

```sh
git push -u origin agent/a/<task-slug>
gh pr create --base main --head agent/a/<task-slug>
```

### CI gates

The sole aggregate required PR status is `CI required` from [blocking-ci.yml](../../.github/workflows/blocking-ci.yml). Server-side repository settings must separately require it; a workflow file alone does not enable protection. Its `always()` gatherer requires explicit success from scope, lockfile checks and every gate family; failed, cancelled or skipped dependencies are not success.

[ci_scope.py](../../scripts/ci_scope.py) compares the actual base/candidate commits with rename detection disabled. A missing comparison baseline forces all gates.
The selector also owns integration, browser and media applicability; jobs consume its outputs instead of maintaining their own path regexes. Media-owned inputs select both real-infrastructure and browser lanes, then run `make media-system` and `make web-e2e-whep` with explicit live fixtures so environment-dependent tests cannot silently satisfy media evidence by skipping.

| Compared paths | Existing blocking lane |
|---|---|
| Only root `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `README.md` or Markdown under `docs/` | `make check-docs`; integration/browser/media report explicit no-change success |
| Center, Edge, shared contracts, system fixtures or system tests | `make check` plus the real-infrastructure lane |
| Web inputs | `make check` plus the browser lane |
| Media deployment/test inputs | `make check` plus real-infrastructure, playback and WHEP browser evidence |
| `.github/`, `Makefile`, the selector/actionlint installer, or unusable baseline | All blocking families |

Filtering is an optimization, not an exemption. Lockfiles are always checked; applicable code gates regenerate artifacts and verify tracked and untracked cleanliness. Keep immutable action pins, least-privilege permissions, frozen installs, job timeouts and cancellation of superseded PR runs. Do not relax this selection merely because a script change accompanies documentation.
Workflow changes additionally install the pinned `actionlint` release through the checksum-verifying repository installer and run `make ci-lint`. Browser failures upload only Playwright `test-results/` with a pinned upload action and short retention; do not upload `.tmp/dev-main`, environment files or broader workspaces as diagnostic artifacts.

**Done:** the published branch names the verified candidate, PR base is `main` and that final candidate has applicable green CI and review evidence. An earlier candidate's green CI does not transfer across content changes.

## 5. Merge and clean up

Merge only with explicit authorization and the required CI/review evidence. After GitHub reports `MERGED`, synchronize the dedicated primary `main` and safely retire eligible merged tasks as part of closeout.

```sh
git fetch --prune origin main
MERGED_TARGET=origin/main
CANDIDATE_SHA=<merged-pr-head-sha>
git merge-base --is-ancestor "$CANDIDATE_SHA" "$MERGED_TARGET"
```

If ancestry does not prove the candidate is retained, stop this cleanup; do not force-delete it. Identify `MAIN_WORKTREE` from `git worktree list --porcelain`, require branch `main` and a completely clean worktree, then synchronize:

```sh
test "$(git -C "$MAIN_WORKTREE" branch --show-current)" = main
test -z "$(git -C "$MAIN_WORKTREE" status --porcelain=v1 --untracked-files=all)"
git -C "$MAIN_WORKTREE" reset --hard "$MERGED_TARGET"
test "$(git -C "$MAIN_WORKTREE" rev-parse HEAD)" = "$(git rev-parse "$MERGED_TARGET")"
```

Inspect all local `agent/*` branches and registered task worktrees, including older merged residue. Retire a task only if its PR is actually `MERGED`, its **current local tip** is reachable from fetched `origin/main`, and its dedicated worktree is clean including untracked files and is not the primary checkout. A branch with no worktree still needs both PR and ancestry evidence.

```sh
git branch --merged "$MERGED_TARGET" --list 'agent/*'
git worktree list --porcelain
gh pr list --state merged --base main --head "$TASK_BRANCH" --json number,state,headRefName,headRefOid
git merge-base --is-ancestor "$TASK_BRANCH" "$MERGED_TARGET"
test -z "$(git -C "$TASK_WORKTREE" status --porcelain=v1 --untracked-files=all)"
git worktree remove "$TASK_WORKTREE"
git branch -d "$TASK_BRANCH"
```

Remove a worktree from a different worktree. Preserve unmerged, divergent and dirty tasks. `git clean`, task resets, forced worktree removal and forced branch deletion are not cleanup tools here. Pruning remote-tracking refs is not remote branch deletion; remote deletion and Issue closure require separate authorization.

**Done:** local `main` equals the fetched accepted trunk, eligible merged local tasks are retired without force, and all other work is untouched. Report delivered behavior, checks, review, publication state and remaining gaps.

## Persistent continuity

Ordinary bounded single-session work needs no handoff file. Maintain ignored `.tmp/task-handoff.md` across sessions/compaction, multiple agents/worktrees, cross-module/critical changes or unfinished required review/validation. Keep the user request and complete acceptance; Entry / Reuse / Allowed writes / Preserve; fixed base and current candidate; dirty/untracked paths; findings and resolutions; valid command/environment/input evidence; next concrete action. Update facts rather than copying conversation. Handoffs grant neither authority nor a passing result.

## Local enforcement

[reference-transaction](../../scripts/githooks/reference-transaction) checks local ref transitions during `prepared`: `main` cannot be created/deleted/renamed or advanced to task work; an update must match an exact fetched remote-tracking `*/main`. New task branches use the declared namespace. Legacy `dev` creation/advancement is rejected; deletion requires its commits to be accounted for. `--no-verify` does not bypass this hook.

`pre-commit` rejects direct task commits on trunk and `pre-push` rejects direct pushes to remote `main`. Vendor and formatting checks remain enforced by the existing hooks. Removing `core.hooksPath` is not prevented by a repository-owned hook; do not claim it replaces managed Git or server-side protection.

## Target-environment and release gates

Target-environment scenarios, thresholds and pass criteria are owned by [roadmap §9](../design/solution-and-roadmap.md) and the [target-environment validation matrix](../research/target-environment-validation-matrix.md). This workflow only requires applicable evidence to be real and attributable; unavailable required hardware or services produces a gap, never silent mock success.

Release-promotion evidence is owned by [upgrade.md](../deployment/upgrade.md) together with the [target-environment validation matrix](../research/target-environment-validation-matrix.md). This workflow requires every applicable release item to have evidence or remain an explicit blocker; it does not duplicate the deployment inventory here.
