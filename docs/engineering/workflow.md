# Engineering delivery workflow

Status: **normative**. This document owns task isolation, evidence, review, PR delivery and safe cleanup. Skills provide execution methods; the [Makefile](../../Makefile), [PR CI](../../.github/workflows/blocking-ci.yml) and [hooks](../../scripts/githooks/) own executable checks. Product acceptance belongs to [the roadmap](../design/solution-and-roadmap.md); Issue state belongs to [issues.md](issues.md).

The normal path is **isolated task → implement → verify → required review → authorized PR → exact-head CI → authorized squash merge → exact-task cleanup**. There is no repository landing queue or automatic agent-session recovery.

## 1. Establish the task workspace

Inspect cwd, branch, HEAD, worktrees, tracked/staged/untracked changes and acceptance criteria. Continue existing tasks where they are; preserve unrelated work. New work starts from fetched, accepted `origin/main` on one isolated `agent/<agent-id>/<task-slug>` branch/worktree. Variable parts use lowercase ASCII letters/digits, `.`, `_` or `-`, start with a letter/digit and contain no further `/`. One writer owns each worktree, index and branch.

`main` is the shared trunk and PR target, not a task workspace or direct-push destination; `dev` is retired. Enable `make hooks` before ref changes. Check `git config --get core.hooksPath`, `git status --short --untracked-files=all` and `git worktree list --porcelain`; fetch before choosing the fixed base. Do not reset or auto-stash to obtain a clean task. A new task need not first synchronize the primary `main` checkout.

`pre-commit` refuses trunk commits, unregistered vendor edits and unformatted staged Python. `pre-push` refuses direct pushes to `main`. These are lightweight local mistake guards, not a sandbox or complete ref policy; server rules protect the shared branch. Use task credentials with no merge/admin/production privileges during implementation.

**Done:** exact base, task branch/worktree, acceptance and allowed writes are known; unrelated work is preserved. No session-binding file is required for new tasks.

### Skills routing

Use installed Skills when their trigger applies; read the selected skill before its procedure. A manual-only skill runs only when requested, not merely because this document names it. Clear implementation work can use `/implement`, test-first changes `/tdd`, independent review `/code-review`, and PR writing `/pr`. Clarify only unresolved decisions; spec/ticket decomposition and prototypes are conditional, not mandatory stages for small tasks.

Read the referenced skill file with the host's actual tools; a missing Skill API does not prevent using an available file reader. Use other named tool operations only when an equivalent operation preserves the same behavior and authorization boundary; when no equivalent exists, report the step as not executed or unavailable rather than claiming completion. This does not weaken the manual-only trigger or the repository-rule precedence below, and the [review](#3-review-and-repair) section still requires a fixed candidate, affected callers and the actual specification.

Repository rules take precedence: domain language stays in [CONTEXT.md](../../CONTEXT.md), specs/acceptance/tickets and labels use existing GitHub Issue rules, and documentation uses its [current owner](documentation.md). Do not copy Skills into another workflow, create a parallel spec/tracker registry, or treat a skill's commit/reset/publish/merge/cleanup step as authorization. Unavailable Skills are reported, not claimed as executed.

## 2. Implement and select evidence

Record Entry / Reuse / Allowed writes / Preserve once, as defined in [coding.md](coding.md). State risk, test reuse/additions and intended checks in at most three lines. Implement required contracts and failure paths at their owner; stop for changed scope, design authority, unsafe separation or environment blockers. Run the smallest affected check after each coherent change. After two unsuccessful fixes to the same failure, escalate diagnosis rather than repeating guesses.

Use `git ls-files`/`git grep` for tracked inputs in this candidate. Exclude nested worktrees and `.nvsop/` from broader searches; verify returned paths and candidate identity before reading or editing. Do not share another worktree's installed environment or run concurrent Make processes that mutate this worktree's dependencies.

Scope is semantic, not a line-count contest. `make change-size` is advisory. Use `make task-check` only when an explicit task contract names a fixed 40-hex base, path allowlist and cumulative added+deleted budget. In that case run the approved checker from a trusted checkout against the candidate cwd after coherent changes and before handoff; candidate-controlled output is not independent evidence. `task_check=pause` stops expansion. Review hints identify suppressions, skipped tests and removed assertions; they do not prove weakening or replace review. Without an explicit budget, do not add repeated size-check ceremonies.

### Choose evidence by risk

- Copy/style or behavior-preserving cleanup normally reuses checks without new tests. Normative policy, security and gate changes are not low risk merely because they are Markdown.
- Bounded behavior reuses sufficient existing evidence; an uncovered behavior normally needs 1–3 independent scenarios. Each extra scenario needs a concrete risk, not coverage cosmetics or private-helper shapes.
- Confirmed defects and changed critical invariants need failure evidence at the public seam, then the minimum correction and red→green verification.

Keep required real-infrastructure evidence for persistence, authorization integration, queues/outboxes, disposal across restart, execution-right uniqueness/lease expiry, evidence protection and persisted supervisor effects. Pure-rule tests do not replace integration evidence. Preserve applicable browser coverage when behavior crosses the browser/applications. Field/hardware criteria remain open with their validation owner; synthetic checks cannot claim field acceptance. See [roadmap acceptance](../design/solution-and-roadmap.md#九验收门禁) and the [target-environment matrix](../research/target-environment-validation-matrix.md).

### Placement and evidence level

Unit tests belong to the owning app/package (sole-owner Vue tests may be colocated); integration tests exercise an application and real adapters/infrastructure at declared seams; contracts cover external/inter-process assumptions; system/browser tests cover published user flows. NVIDIA subtree updates retain both base-assumption and registered patch/reimplementation regressions under `tests/contract/base/`. Performance evidence names environment, model/digest, data, percentiles, queue depth and pass budget.

Fixtures are minimal, deterministic, synthetic or sanitized with provenance. Customer media, credentials, weights and production dumps stay out of Git. Configure units through public/configuration seams rather than mutating process environment. Prefer complete outputs and reuse existing fixtures.

### Stable commands

Inspect [Makefile](../../Makefile) for prerequisites and constituent checks; it is the one local/CI interface. Frozen `uv.lock`/`pnpm-lock.yaml` own resolution. `make check` installs/generates before its internal parallel stage; `check-suite` is not an alternative final gate. `CHECK_JOBS=1` constrains a host. Clear test-selection overrides for final gates; retain actual command, environment, base/candidate and results. Reuse still-valid evidence instead of rerunning unrelated suites.

| Entry | Purpose |
|---|---|
| `make docs-check` | Offline documentation inner loop |
| `make check-docs` | Final documentation gate: frozen setup, hooks, policy, secrets, whitespace |
| `make check` | Final Docker-free code gate |
| `make check-integration` | Center integration/system checks with real infrastructure |
| `make media-system` / `make web-e2e` / `make web-e2e-whep` | Affected real media/browser evidence; unavailable dependencies remain gaps |
| `make ci-plan BASE=<sha> HEAD=<sha>` / `make ci-lint` | CI scope explanation / offline pinned actionlint (`make ci-tools` bootstraps it explicitly) |
| `make contracts` | Canonical compatibility/export/Web generation; [maintenance](maintenance.md#generated-contracts) owns the procedure |
| `make pr-check PR=<number>` | Optional read-only classification of candidate, CI/protection and review obligations |
| `make local-clean` / `make local-purge` | Declared outputs / task-local reproducible state, not secrets or unknown ignored files |
| `make task-merge` | Verify local task and open PR identity before exact-head squash merge, then clean that single task |
| `make task-cleanup` / `make task-abandon` | Exact single-task cleanup after verified merge / explicit supersession proof |

**Done:** every acceptance criterion and required check has actual results or an explicit gap. Stop optional tests/refactors when sufficient evidence is complete.

## 3. Review and repair

Self-review the complete diff, new files, callers and references. Require independent read-only **Spec + Standards** review for critical risks, public/cross-module interfaces, shared behavior, dependencies, CI, deployment, vendor, repository policy, agent instructions or unbounded impact. Ordinary bounded changes need no independent reviewer by default.

Bind review to a fixed base and candidate. Separate read-only subagents satisfy independent review; implementer self-review does not. Gate changes, exemptions and weakened tests require substantive inspection of the actual diff. `pr-check` risk/evidence fields and PR-body booleans are hints, not enforcement or approval. Fewer assertions block only when they lose an actual regression/critical failure path. Report Spec and Standards separately; optional naming/size/refactoring preferences are not defects.

Consolidate concrete defects into one bounded repair batch owned by the implementation session. Verify the increment and affected callers; reuse unaffected evidence. Required missing review/checks keep readiness pending. Evidence is invalidated by changes to its covered source, configuration, dependencies or external inputs. Codex GitHub review is optional, non-blocking feedback: record/dispose actual findings without treating quota, delay or absence as a gate.

### PR lifecycle approval

Confirm one bounded task plan at task start, covering implementation, verification, review and every intended delivery action through completion. Record the real user confirmation, scope, branch/worktree, target `main`, candidate (or bind it when available), publish/update/refresh/merge actions, named Issues or `none`, and exact server-change and cleanup targets. An initial instruction explicitly covering that plan is the confirmation; otherwise collect the missing actions in one consolidated confirmation before executing them, not stage by stage. Agent reports, PR fields and a general request to continue are not approval.

Before confirming a plan that includes merge, read `gh repo view --json deleteBranchOnMerge --jq .deleteBranchOnMerge`. Disclose automatic remote-head deletion when true; otherwise remote deletion is excluded unless named. Once merge is authorized and verified, primary `main` synchronization and safe cleanup of that exact task are mandatory completion. Writers, dirt, unknown state or non-fast-forward updates stop the operation; never force or sweep.

Within the approved plan, proceed without repeated authorization after in-scope repairs, review findings, failed checks or successor SHAs. Bind scope/diff/review/final-gate evidence before each authorized action; renew affected evidence, not approval. The plan may anticipate the eventual SHA/PR/merge result. Base refresh must be explicitly covered: match the exact prior head, perform only a conflict-free native update, then revalidate base-sensitive evidence and CI on the new head. Added scope/actions, changed targets or manual conflicts are outside that authorization: stop and report the bounded decision rather than forcing completion.

**Done:** required independent findings are resolved; authorization and evidence refer to the actual candidate and named actions.

## 4. Publish the candidate and evaluate CI

Publish only the task branch and open/update its PR against `main` when authorized. `/pr` uses the [PR template](../../.github/pull_request_template.md); keep actual evidence and remaining work there, not a second rulebook. Confirm local branch/head matches the PR head.

`make pr-check` retains repository-specific review classification: authority/manifest/shared-contract changes need actual dispatch-impact scanning and dispositions; public seams need architecture evidence; gate/policy changes need independent review. It deliberately leaves independent review as manual confirmation. Missing/unknown protection or checks block; `migration-required` means the old server landing gate still needs an approved cutover. Machine readiness never authorizes merge.

### CI gates

[blocking-ci.yml](../../.github/workflows/blocking-ci.yml) runs on ordinary `pull_request` events and reads that PR's exact base/head with read-only repository permissions. It does not run candidate code with landing credentials, call a queue, or merge. `cancel-in-progress: true` replaces stale runs of the same PR; required evidence always belongs to the current head. [ci_scope.py](../../scripts/ci_scope.py) selects lanes from the paths each lane's checks actually read: documentation-only changes (`AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `README.md`, `docs/**/*.md`) take the fast lane; any other code change runs `make check`, and the deployment and training-vendor inputs those checks consume select integration; media and annotation changes also select the integration lane they depend on; CI control inputs (`.github/`, the `Makefile`, the selector) force every lane. Both frozen lockfiles are checked on every run. The selector owns the exact mappings; Make and the test runners keep automatic test discovery. Required checks are retained even when other lanes are unnecessary. A new dedicated gate lands with its Make entry, selector mapping, `required` aggregation and selector tests together. `CI required` passes only when every dependency succeeded: failure, cancellation, skip and noncompletion all fail it.

The server rules must require PRs, successful strict/latest-base `CI required` bound to the GitHub Actions App, resolved conversations, and protection against force pushes/deleting `main`. Implementation agents have no permission to bypass these rules. Candidate workflow/gate changes are independently reviewed; this maintainer-controlled model is not equivalent to an independent trusted App rerunning candidate checks. Skills do not create server enforcement.

**Cutover prerequisite:** verify native PR CI before changing live protections. Name removal of the old App-only update restriction and `Landing gate`, replacement of the App-bound CI source, and retirement of live queue/repair writers in the task's single approved plan; cutover is not another approval stage. Preserve existing queue/inbox state, receipts and failure evidence; assign unfinished work to a human/session rather than clearing it. Repository changes alone do not change GitHub rules, secrets, Environments or service state. Until that cutover is verified, ordinary PR delivery is not declared operational. No admin bypass is a migration method.

**Done:** the exact current PR head/base has all required checks and review evidence; failures return to the task owner for direct diagnosis/repair, with no automatic session resume.

## 5. Merge and complete the authorized task

Immediately re-read PR state, current head/base, required checks and review/conversation state. Stop on changed/unverified facts. For an authorized, current candidate, run `make task-merge PR=<number> BRANCH=<branch> CANDIDATE=<sha>` from the clean primary `main` checkout. This first verifies the invoking checkout is the primary worktree, then checks the exact local task branch/tip/worktree and the open PR head/base identity without changing state, then invokes native `gh pr merge <number> --squash --match-head-commit <sha>` without `--admin` and immediately runs the exact-task cleanup upon success. On a verified already-merged PR or after a cleanup failure, resume with `make task-cleanup` for that exact task; never re-merge it. `gh pr checks <number> --required` is a read-only check, not authorization. An explicitly authorized base update uses the [native REST endpoint](https://docs.github.com/en/rest/pulls/pulls#update-a-pull-request-branch) via `gh api --method PUT repos/{owner}/{repo}/pulls/<number>/update-branch -f expected_head_sha=<sha>`; a head mismatch is rejected. Its successor needs fresh affected evidence and exact-head CI.

### 5.1 Confirm the exact squash merge

Read `gh pr view <number> --json state,mergedAt,mergeCommit,headRefOid,baseRefName,headRefName`. Require `MERGED`, timestamp/merge commit, intended branch and `main`, and `headRefOid` equal to the merged candidate. Fetch `origin/main` and prove `git merge-base --is-ancestor <merge-commit> origin/main`. A timeout, green CI or ancestor task commit is not merge proof; stop before cleanup when proof is incomplete.

### 5.2 Synchronize main and clean one verified task

Stop writers and inspect the exact task plus primary checkout. Local task tip must already equal the verified candidate; cleanup never adopts a server-refreshed head. Safely synchronize the task beforehand only when authorized, or report the mismatch.

The `task-merge` target runs `make task-cleanup PR=<number> BRANCH=<branch> CANDIDATE=<sha>` immediately after a successful merge; when the merge happened separately, run this cleanup command directly as the mandatory local completion step. The versioned helper independently verifies PR identity, retained merge proof, canonical worktree/branch identity and tracked/untracked cleanliness, then automatically purges only declared task-local reproducible state (including recognized legacy cache paths). It rechecks ignored state and task identity after purge, fast-forwards only the dedicated clean primary `main`, and removes only this task's worktree/branch. An empty owned `.nvsop` directory may be removed; unknown state still blocks. Preserved primary ignored configuration is allowed only if the update cannot overwrite it. Remote refs are not deleted by this helper. This is a single-task local step performed by the delivery owner, not a background scan of merged PRs.

Tracked, staged, ordinary untracked or unknown ignored dirt blocks removal. Secrets, fixed development state and handoffs are preserved; do not delete them to make cleanup pass. Existing legacy session bindings are handled narrowly by [cleanup](../../scripts/cleanup_task.py) under [maintenance rules](maintenance.md#session-binding-state), never by recreating a recovery service. `git clean -fdx`, force removal and bulk retirement are not supported.

### 5.3 Clean one explicitly abandoned or superseded task

After explicit supersession/cleanup authorization, stop writers and use `make task-abandon BRANCH=<branch> CANDIDATE=<sha> REPLACED_BY=<accepted-main-sha> [PR=<closed-pr>]`. The helper requires the replacement to equal accepted `origin/main`; with a PR it must be closed/unmerged and match head/base. After proof it automatically purges declared task-local reproducible state, rechecks cleanliness and removes only the named task. Open/unverifiable PRs or dirt block cleanup; it never infers replacement or synchronizes `main` on this path.

**Done:** exact merge/replacement proof and local postconditions are recorded, or a concrete safety blocker is reported. Close named Issues only under [Issue closure rules](issues.md#close-with-evidence); implementation/CI alone does not close them. Report actual commands/results, candidate and remaining gaps, then stop.

## Persistent continuity

Create `.tmp/task-handoff.md` only when work must survive session loss/interruption or an unfinished task is explicitly handed over. Record task/acceptance, fixed base/head and branch/worktree, allowed writes/preserved work, actual commands/evidence, blockers, next bounded step and authorization status. Use repository-relative paths; keep secrets out. Do not create a handoff for every short task, duplicate GitHub task state in tracked docs or use it as approval. An existing handoff remains protected from automatic cleanup until its authorized owner resolves it.
