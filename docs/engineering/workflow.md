# Engineering delivery workflow

Status: **normative**. This is the home for task isolation, verification, review, PR/CI, Codex review, merge and local cleanup. Read the relevant section for the current step; product acceptance belongs to [the roadmap](../design/solution-and-roadmap.md), and Issue state to [issues.md](issues.md). The [Makefile](../../Makefile), [blocking CI workflow](../../.github/workflows/blocking-ci.yml) and [versioned hooks](../../scripts/githooks/) own executable repository behavior.

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

The hook path must resolve to `scripts/githooks` or its configured equivalent. A new task starts from the accepted fetched `origin/main` tip and does not require synchronizing the primary `main` worktree. Never reset a task worktree to obtain a clean base. `.nvsop/` is policy-enforced as untracked local state, so preserving it cannot overwrite a committed path. After a verified merge, synchronizing the dedicated primary `main` worktree is a mandatory completion step owned by [§5.2](#52-synchronize-main-and-clean-one-verified-task), not a second authorization boundary.

```sh
git worktree add ../nvsop-task -b agent/a/<task-slug> origin/main
```

These are conditional operation examples, not a script to paste without checking each result. If `main` moves after the task starts, keep the task's fixed base; an integration update is a separate explicit decision with affected evidence rechecked.

**Done:** one task owns one branch/worktree, its exact base is known and unrelated work is preserved.

## 2. Implement and select evidence

Before editing, record Entry / Reuse / Allowed writes / Preserve as defined in [coding.md](coding.md). State risk, intended test reuse/additions and intended checks in at most three lines. Classify behavior and failure paths, not file extensions.

State a bounded task as Entry / Reuse / Allowed writes / Preserve plus a runnable acceptance: the exact command and observable result that close it. After each coherent small change, run the smallest affected check; do not defer all verification to the end. After at most two rounds on the same code failure, stop and escalate the diagnosis instead of repeating the same fix; report an environment blocker or a contract conflict immediately rather than working around it. A change that needs broader scope or design authorization stops for that decision instead of expanding itself.

Execution identity is provided by the operator with least privilege: no merge, repository-admin or production credentials. The task worktree is collaboration isolation, not a security sandbox; it separates writers, branches and refs and does not contain a hostile process.

Before starting, lock the guardrail command and its arguments: the fixed 40-hex base, the paths the user authorized, and the line budget. Run `make task-check BASE=<40hex> ALLOW='<paths>' MAX_LINES=<N>` after each coherent small change and before any handoff. `task_check=within-bounds` is a mechanical bound, never acceptance or approval. `task_check=pause` stops expanding the implementation; only necessary read-only diagnosis continues, and the session reports the choice to narrow scope, split an independent stage, or request explicit user authorization. A semantic out-of-scope change — an unauthorized new API, state owner or dependency — stops even when every changed path is allowed. The ordinary budget is 800 changed lines; the main scheduler may pre-set 500 for complex logic. Line counts are a diagnostic, not a design-quality conclusion. The main scheduler selects the approved checker and runs it against the candidate cwd from a trusted read-only checkout: `make -C <candidate-worktree> -f <trusted-checkout>/Makefile task-check BASE=<40hex> ALLOW='<paths>' MAX_LINES=<N>`. The recipe resolves the checker next to the trusted Makefile, so the candidate's own `scripts/` copy never runs and the trusted checkout's empty diff is not measured; output from a checker the candidate modified is not independent protection. This phase adds no OS sandbox or persistent agent scheduler.

Both `make change-size` and `make task-check` also print **review hints** taken from the actual diff: newly added `noqa`, `type: ignore`, `eslint-disable` or TypeScript ignore directives, new pytest `skip`/`skipif`/`xfail` or `test`/`it`/`describe` `skip`/`only`/`todo` markers, and removed test definitions or assertions in Python and existing TS/Vue tests. Each hint names the file and line; generated and binary files are skipped, unchanged markers are not reported, and an addition is never offset by a removal elsewhere. These are limited text/syntax clues, not conclusions: a match does not prove a test was weakened (a legitimate complete-object assertion merge can trigger one), and the absence of a hint does not prove safety. Hints never fail the check or replace the scope/budget exit codes; when they appear, the human reviewer reads the named lines and decides whether the change actually weakens a test or hides a failure. The trusted-checkout checker remains the mechanical gate, so hints added by the candidate are not independent protection.

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

Synthetic tests cannot prove target-hardware or field acceptance. The current MVP software/validation split is owned by [roadmap §8](../design/solution-and-roadmap.md#八开发路线), while the exact acceptance gates are owned by [roadmap §9](../design/solution-and-roadmap.md#九验收门禁) and the [target-environment validation matrix](../research/target-environment-validation-matrix.md). Keep every deferred criterion linked to an open validation owner; necessary software integration evidence stays with implementation.

Fixtures are minimal, deterministic, synthetic or sanitized and provenance-documented. Customer media, credentials, model weights and production exports are not fixtures. Prefer complete output assertions; reuse helpers; put tests outside production files. Configure a unit through its public/configuration seam, not by mutating process environment variables.

### Stable commands

The [Makefile](../../Makefile) is the local/CI command interface. During iteration use the smallest affected targets and their prerequisites; inspect their definitions rather than assuming every target installs dependencies.

`make check` completes frozen environment setup and contract generation before running its internal `check-suite` stage with two Make jobs by default. `make check-integration` completes setup before running the two independent pytest sessions with the same limit; use `CHECK_JOBS=1` for a serial comparison or a constrained host. Do not run two separate Make processes that synchronize and consume the same worktree environment concurrently. Standalone targets still require their prerequisites; `check-suite` is an internal stage, not an alternative final gate.

pytest targets report the slowest setup/call/teardown phases and write JUnit results under `.nvsop/artifacts/pytest/<target>.xml`. Override `PYTEST_ARGS` for a focused iteration, for example `make center-unit PYTEST_ARGS='-k password --durations=20'` after `make sync`. Clear selection overrides for final gates. Compare the same test set and record environment, base/candidate and cold/warm state; reuse normal gate runs for timing rather than repeating full suites just to collect numbers. Optimize immutable setup data first, never production security parameters or required real-infrastructure evidence. Final evidence must cover the final candidate; reviewers consume still-valid evidence instead of repeating every gate.

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
| `make local-clean` | Delete declared build/test outputs while preserving installed environments and caches |
| `make local-purge` | Before authorized task cleanup, also remove task-local environments, tools, caches and legacy generated paths; preserve shared caches, fixed-instance state, secrets and unknown files |
| `make task-cleanup PR=<number> BRANCH=<branch> CANDIDATE=<sha>` | Remove one verified merged task's clean local worktree and branch; never sweep tasks |
| `make task-abandon BRANCH=<branch> CANDIDATE=<sha> REPLACED_BY=<main-sha> [PR=<closed-pr>]` | Remove one explicitly abandoned/superseded clean local task after exact replacement proof; never infer or sweep |
| `make task-session-bind SESSION=<resumable-id>` | Create or verify this worktree's `.nvsop/session-binding.json`; exclusive, idempotent for the same session, never overwrites |
| `make pr-check PR=<number>` | Read-only machine-state preflight; never substitutes for independent review or merge authorization |
| `make pr-land-status PR=<number>` | Read one PR landing state and next machine action; never grants authorization |
| `make pr-land-refresh PR=<number> EXPECTED_HEAD=<sha>` | Request one conflict-free server-side base refresh guarded by the exact PR head |
| `make pr-land-merge PR=<number> EXPECTED_HEAD=<sha>` | Attempt one exact-head squash merge after required evidence and authorization exist |
| `make pr-land-enqueue PR=<number> EXPECTED_HEAD=<sha> ATTESTATION=<comment-id> [REPAIR_CLAIM=<token>]` | Post one durable enqueue request referencing an existing human-maintainer attestation comment; validates the bound, clean frozen candidate; never grants authorization |
| `make pr-land-dequeue PR=<number>` | Post one durable dequeue request; releases the landing writer and preserves the local binding |
| `make pr-land-queue` | Read the durable queue state (read-only) |
| `make pr-land-repair-event PR=<number>` | Print one machine-readable repair event for a repairable blocked entry; refuses stale facts; read-only |
| `make pr-land-repair PR=<number> ACTION=<claim\|resume\|preflight> [CLAIM=<token>] [WORKTREE=<path>] [EXPECTED_GENERATION=<64hex>]` | Post one exclusive repair claim, resume the accepted claim under its token, or re-verify facts; never a session id |
| `make change-size` | Advisory size report from `BASE`, default `origin/main`; also prints diff review hints; never a bound |
| `make task-check BASE=<40hex> ALLOW='<paths>' [MAX_LINES=<N>]` | Fixed-base scope and cumulative added+deleted budget; also prints diff review hints; reports `task_check=within-bounds` or `task_check=pause` (script exits 3 on pause); not acceptance |
| `make contracts` | Generated OpenAPI compatibility and Web client; procedure in [maintenance.md](maintenance.md#generated-contracts) |

Frozen `uv.lock` and `pnpm-lock.yaml` own resolution. Do not replace applicable checks with ad-hoc installs. Save task-branch commits when useful; they record progress, not acceptance.

**Done:** all owned acceptance criteria and required current-phase evidence are accounted for, with actual commands/results or explicit gaps. Stop optional tests/refactors once this is true.

## 3. Review and repair

Self-review the complete diff, including untracked additions, affected callers and references. Require one consolidated **independent read-only review** for critical risks, public/cross-module interfaces, shared behavior, dependencies, CI, deployment, `vendor/`, repository policy, agent instructions or unbounded impact. Ordinary bounded changes need no independent reviewer by default.

Changes to gates, new exemptions and weakened tests require the independent reviewer to read the actual diff, with evidence bound to a fixed base/candidate. Fewer test assertions is not by itself weakening; a weakened test is one that no longer fails on the defect it claims to cover. A `harness_review=required` line from `pr-check` is a risk hint only — it enforces nothing server-side and is not review approval. Server-side review settings are configured separately by the maintainers, and the existing explicit user merge authorization and independent-subagent review mechanisms are unchanged.

The reviewer reports **Spec** and **Standards** together against a fixed base/candidate and consumes still-valid evidence instead of rerunning everything. A separately executed, read-only subagent review is an acceptable independent review mechanism; the reviewer reads the actual task worktree, while the main implementation session owns repairs. The implementer cannot count self-review as independent review. Report whether review was separately executed or only a self-review. Do not move repairs to a review worktree.

Repair concrete findings as one bounded batch, rerun affected checks and review the repair increment plus affected callers. A missing-test finding blocks only for a concrete failure path, uncovered critical risk or unmet acceptance. Naming, size and optional cleanup block only when tied to a defect or violated invariant. Missing required review/validation keeps merge readiness pending.

After the user has seen the explicit candidate, the actual diff, the applicable independent Spec and Standards results and the verification evidence, an explicit confirmation from that user satisfies human approval where it is required and needs no additional GitHub reviewer identity. This is the human-approval path, not a new independent-review requirement: independent technical review still applies only when this workflow requires it, and ordinary low-risk changes do not gain one. Record the real confirmation reference or content, the confirmed candidate and the authorized action scope. An agent self-report, a boolean in the PR body or a general "continue implementing" is not confirmation. Do not build approval storage, machine fields or an extra registry.

**Done:** findings are resolved or explicitly blocking, and evidence covers the candidate's code, tests, configuration, dependencies and relevant external inputs. Evidence does not survive changes to those covered inputs.

### PR lifecycle approval

Publication, integration refresh, merge, Issue closure and task cleanup are one bounded delivery, not one approval per action. One explicit user confirmation of a presented **PR lifecycle plan** authorizes every action the plan names, in one pass. The plan is bounded and names:

- the confirmed task candidate SHA, the task branch and worktree, and the target `main`;
- the action scope: push the task branch, create or update its PR against `main`, perform any controlled conflict-free base refresh against the exact expected prior head, observe `CI required` on each landing head, hand off to the queue for the queue-controlled exact-head squash merge when authorized, name any automatic remote PR-branch deletion that the current repository setting makes a consequence of merge, and close the named Issue(s) when their closure criteria are satisfied — or `none`;
- the exact cleanup target: the verified merged task's local branch and worktree. Once squash merge is authorized and exact merge proof succeeds, primary `main` synchronization and this single-task cleanup are mandatory completion steps rather than separate actions that need another authorization.

Before presenting the plan, read the live repository setting with `gh repo view --json deleteBranchOnMerge --jq .deleteBranchOnMerge`. When it is `true`, deletion of the remote PR head branch is an inseparable server-side consequence of merge and the plan must disclose it with the merge action; when it is `false`, remote branch deletion is excluded unless separately named. The user may authorize a subset that the current server configuration can actually separate, but local synchronization and exact task cleanup are inseparable post-merge completion once the merge itself is authorized. A mere implementation or "continue" request and an agent-authored plan are not lifecycle approval.

Every technical gate stays a precondition, never a second human approval gate: the applicable independent read-only review, exact-head green `CI required`, server protection/rules and resolved review conversations, the queue-controlled exact-head squash merge, exact merge proof, stopped writers, dirty/unknown-state refusal and the ban on sweeping or unowned force cleanup. A controlled base refresh invalidates the old exact-head CI and any base-sensitive technical evidence; rerun or revalidate the affected evidence on the refreshed landing head before merge. Approval never bypasses evidence or grants a generic permission; pause and report the actual stage, command, result and gaps on any blocker.

The confirmed task candidate is the authorization root. A later landing head inherits that approval only when the authorized delivery flow itself performs a server-side conflict-free base refresh against the exact expected prior head, without manual conflict resolution or task-code edits. That mechanical integration transition does not require another user confirmation, but its new head must satisfy the technical gates above. Any other head change, manual conflict resolution, task-code or scope change, added action, or changed target invalidates the plan and requires revalidation plus a new approval. Later facts the plan already anticipated are not changes: phase completion, a green CI run, the PR number allocated by the approved create, the squash merge commit becoming known, and post-merge cleanup becoming provably safe.

Sequence once approved: publish and open or update the PR ([§4](#4-publish-the-candidate-and-evaluate-ci)) → when `main` advances, perform only the authorized conflict-free refresh against the exact expected prior head and re-establish affected evidence → observe `CI required` on the exact landing head ([§4](#ci-gates)) → with every gate satisfied, hand off to the queue for the exact-head squash merge → confirm the recorded squash commit is retained by `origin/main` ([§5.1](#51-confirm-the-exact-squash-merge)) → purge declared reproducible state from the exact task worktree and run the versioned single-task cleanup, which fast-forwards the clean primary `main` checkout to the accepted `origin/main` and removes the exact merged task → close the named Issues when their closure criteria are satisfied ([issues.md](issues.md#close-with-evidence)). The concrete landing mechanics live with their versioned execution entrypoints when present; this policy layer owns authorization rather than duplicating those mechanics. Completion is observable only after the merged task has either been synchronized and cleaned or a concrete safety blocker has been reported.

## 4. Publish the candidate and evaluate CI

Publication, the PR create/update and CI observation are authorized by the [§3 PR lifecycle plan](#pr-lifecycle-approval) that names them; a plan that omits them leaves them unauthorized. Publish only the task branch and open its PR directly against `main`; never push `HEAD:main`. Use the [PR template](../../.github/pull_request_template.md) to record outcome, scope, risks, actual evidence and documentation impact, not another full rulebook.
Before merge, `make pr-check PR=<number>` may aggregate the PR head/base, `CI required`, `Landing gate`, branch-protection visibility and local candidate identity. Treat `unknown` or absent protection and a missing check as blocked. The exact PR head needs successful `CI required` **and** `Landing gate`, required independent review evidence and the merge authorization in the [PR lifecycle plan](#pr-lifecycle-approval). `pr-check` deliberately leaves independent review as manual confirmation and never grants merge authorization.
For candidates that change architecture/Issue/mechanism/ADR authority, the module-registry manifest or shared machine contracts, the same preflight reports `dispatch_impact_review=required`. Record the actual open/ready Issue scan and dispositions in the PR template; this mechanical evidence does not replace semantic review of whether the affected set is complete.

`pr-check` also reports `harness_review=required` when the candidate touches gate, policy, manifest or check-configuration entry points. Like the dispatch flag it is a preflight risk hint, never a server-enforced review or an approval; record the independent review in the PR template's existing Review section.

```sh
git push -u origin agent/a/<task-slug>
gh pr create --base main --head agent/a/<task-slug>
```

### Serial landing without a queue service

Status: **normative**. This heading anchors the serial landing rule, not a second landing path. The durable automatic queue in [landing queue and single-flight CI](#landing-queue-and-single-flight-ci) is the supported flow; [scripts/land_pr.py](../../scripts/land_pr.py) keeps the one-shot CAS primitives (`pr-land-status`, head-guarded `pr-land-refresh`, exact-head `pr-land-merge`) that the queue calls, not a second direct landing path. The trusted controller is the single landing writer: a frozen candidate is enqueued under an explicit human-maintainer attestation, the controller refreshes and merges the exact head with the dedicated App, and `blocking-ci.yml` no longer runs directly on `pull_request`/`push main`. Human bounded authorization is unchanged: required independent review evidence and explicit merge authorization must already exist, and the queue re-reads the attestation at every step. See [PR lifecycle approval](#pr-lifecycle-approval) and [landing ownership and session binding](#landing-ownership-and-session-binding).

### CI gates

The supported CI path uses GitHub-hosted `ubuntu-24.04` runners. The repository is intentionally public; do not replace the hosted path with a local or self-hosted runner as an account-billing workaround. A runner-topology change is a separate CI-policy change and requires the same review as other workflow authority changes. Repository visibility, Actions permissions, branch protection/rulesets and repository secrets remain server-side settings; workflow files do not configure them.

The aggregate required PR status is `CI required` from [blocking-ci.yml](../../.github/workflows/blocking-ci.yml). Server-side repository settings must separately require it when protection is available. `pr-check` makes missing or unknown enforcement visible; regardless of server enforcement, the exact PR head still needs successful `CI required`. Its `always()` gatherer requires explicit success from scope, lockfile checks and every gate family; failed, cancelled or skipped dependencies are not success.

`blocking-ci.yml` is the one gate implementation and is reusable-only: it has no `pull_request`/`push` trigger and is invoked only by the trusted [landing-queue.yml](../../.github/workflows/landing-queue.yml) controller through a `workflow_call` interface with required `head_sha`/`base_sha` inputs. Every checkout resolves the exact controller-supplied head (`ref: ${{ inputs.head_sha }}` — the PR head, not the virtual merge commit), and every base/`OPENAPI_BASE_REF` uses `inputs.base_sha`. The call keys its concurrency group on the candidate head with `cancel-in-progress: false`, so it never shares the controller's global `landing-queue` mutex and cannot self-deadlock. The checked-out candidate HEAD is untrusted candidate code and is given only `contents: read` with no secrets. The queue finalizer publishes the `CI required` and `Landing gate` commit statuses on the exact landing head with the dedicated landing App. See [landing queue](#landing-queue-and-single-flight-ci).

[ci_scope.py](../../scripts/ci_scope.py) compares the actual base/candidate commits with rename detection disabled. A missing comparison baseline forces all gates. The selector also owns integration, browser and media applicability; jobs consume its outputs instead of maintaining their own path regexes. Media-owned inputs select both real-infrastructure and browser lanes, then run `make media-system` and `make web-e2e-whep` with explicit live fixtures so environment-dependent tests cannot silently satisfy media evidence by skipping.

| Compared paths | Existing blocking lane |
|---|---|
| Only root `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `README.md` or Markdown under `docs/` | `make check-docs`; integration/browser/media report explicit no-change success |
| Center, Edge, shared contracts, system fixtures or system tests | `make check` plus the real-infrastructure lane |
| Web inputs | `make check` plus the browser lane |
| Media deployment/test inputs | `make check` plus real-infrastructure, playback and WHEP browser evidence |
| `.github/`, `Makefile`, the selector/actionlint installer, or unusable baseline | All blocking families |

The real-infrastructure lane uses a two-entry matrix: `center-integration` and `center-system` run on separate hosted runners after frozen setup. The matrix keeps `fail-fast: false`; both entries must succeed for its existing `CI required` dependency to succeed. Each entry owns its containers, while real MediaMTX playback runs only in the system entry when selected. No-change success remains explicit in both entries.

Filtering is an optimization, not an exemption. Lockfiles are always checked; applicable code gates regenerate artifacts and verify tracked and untracked cleanliness. Keep immutable action pins, least-privilege permissions, frozen installs, job timeouts and cancellation of superseded PR runs. Do not relax this selection merely because a script change accompanies documentation. Workflow changes additionally install the pinned `actionlint` release through the checksum-verifying repository installer and run `make ci-lint`. Browser failures upload only `.nvsop/artifacts/web/test-results/` with a pinned upload action and short retention; do not upload `.nvsop/dev-main`, environment files or broader workspaces as diagnostic artifacts.

### Codex review and merge

Codex GitHub review is optional semantic review evidence; it does not replace deterministic CI, branch protection or the repository's independent-review rule. Its absence, delay, quota limit or delivery failure does not block delivery, and no exact-SHA Codex review is required. The repository does not run a second model reviewer, translate Codex comments into a custom status, or automatically merge after an AI verdict.

Evaluate any concrete Codex findings as review feedback and address confirmed defects through the normal repair process. Deterministic style, lint, generated-file and test gates remain owned by `blocking-ci`; Codex may focus on correctness, architecture, safety and repository-governance regressions.

The active server-side `main` ruleset is the mechanical merge boundary. It requires a pull request, successful `CI required` and `Landing gate`, an up-to-date branch before merge and resolved review conversations; force pushes and branch deletion are blocked. Repository-policy, CI, architecture, shared-contract and other critical changes still require the consolidated independent read-only Spec + Standards review from section 3. This review may be performed by a separate read-only subagent; Codex is not an additional requirement.

All accepted pull requests are merged by the trusted queue as an exact-head squash after that head satisfies the required CI (`CI required` and `Landing gate`) and review evidence and the [PR lifecycle plan](#pr-lifecycle-approval) authorizes the merge. The human-maintainer attestation, not any AI verdict, is the authorization; do not enable a repository workflow that treats any AI verdict as merge authorization or races GitHub's branch rules.

**Done:** the published branch names the verified landing head, PR base is `main`, that exact landing head has green `CI required` and `Landing gate`, required independent review is complete, required conversations are resolved, and the merge is covered by an explicit [PR lifecycle plan](#pr-lifecycle-approval). Earlier CI or review evidence does not transfer across candidate changes.

## 5. Merge and clean up

Merges require explicit authorization and the required CI/review evidence. Use GitHub's squash merge path after the active `main` ruleset is satisfied; there is no repository-owned automatic AI merge path: the queue merges the exact head only after the human-maintainer attestation and required evidence, and no AI verdict authorizes a merge. Exact merge proof gates the automatic local synchronization and task cleanup; stop all task writers before removing a task worktree or branch.

### 5.1 Confirm the exact squash merge

From a clean worktree that is not the task worktree, fetch the accepted trunk and inspect the PR once:

```bash
git fetch origin main
gh pr view <pr-number> --json state,headRefName,headRefOid,baseRefName,mergeCommit
git merge-base --is-ancestor <merge-commit-oid> origin/main
```

Continue only when the response is `MERGED`, its `baseRefName` is `main`, its `headRefName` and `headRefOid` equal the reviewed branch and exact landing-head SHA, its `mergeCommit.oid` is present, and that recorded commit is retained by `origin/main`. A squash merge does not make the pre-squash landing head an ancestor of `main`; do not substitute that check. This confirmation is read-only evidence for the operator and the same proof is rechecked by the cleanup command.

### 5.2 Synchronize `main` and clean one verified task

With writers stopped and exact merge proof complete, merged delivery runs the existing cleanup commands automatically; they do not require another authorization:

```bash
make -C <task-worktree> local-purge
make task-cleanup \
    PR=<pr-number> \
    BRANCH=agent/<owner>/<task> \
    CANDIDATE=<landing-head-sha>
```

The purge removes only repository-declared reproducible task-local state and preserves the session binding. `task-cleanup` then verifies the direct local `refs/heads/agent/<owner>/<task>` tip, queries the supplied PR once for `state`, `headRefName`, `headRefOid`, `baseRefName`, and `mergeCommit`, fetches `origin main`, and verifies the recorded squash commit is retained by the exact fetched `origin/main`. It requires the primary worktree to be on `main` with no tracked, staged or ordinary untracked changes and fast-forwards it only with `git merge --ff-only --no-overwrite-ignore <accepted-origin-main>`. Ignored personal configuration and caches remain untouched unless Git itself reports that the fast-forward would overwrite them. There is no stash, reset, rebase or non-fast-forward fallback.

Only after primary `main` reaches the accepted `origin/main` does the command remove the exact merged task. It refuses symbolic or out-of-scope refs, a mismatched candidate, multiple registered task worktrees, the primary/current worktree as the cleanup target, dirty task worktrees including ignored files, and Git read or configuration failures. The sole allowed ignored task exception is the worktree's exact validated `.nvsop/session-binding.json`; any other ignored file, directory or symlink refuses. A clean task worktree is removed without force, the exact local branch ref is deleted with its expected old SHA, and only the exact local `branch.<task>` configuration section is removed.

Synchronization and task removal are sequential, not transactional. All task-safety checks and merge proof happen before synchronization; if synchronization cannot proceed, the task remains untouched. If a later task-removal command fails after `main` has already fast-forwarded, report the partial result and reconcile only that exact task. The command never deletes remote refs, closes Issues, force-removes a worktree, sweeps other tasks or discards dirty/unknown state.

A task with no unique commit, or whose tip still equals its starting main commit, is not a cleanup candidate on that basis; never use a progress commit as a substitute for writer coordination or exact merge proof. After a verified merge, the delivery sequence runs `make local-purge` automatically in the exact task worktree before `task-cleanup`; this deterministic cleanup step needs no separate authorization. Shared external caches, fixed-instance state and unknown ignored files are preserved. Explicitly abandoned/superseded tasks continue to use `make local-purge` before `task-abandon`.

**Done:** the verified merge is reflected in the primary `main` checkout and the exact merged task worktree/local branch are removed. Any unsafe synchronization or cleanup condition is a reported blocker, not a reason to leave silent residue.

### 5.3 Clean one explicitly abandoned or superseded task

An abandoned or superseded task has no merge proof, so cleanup requires a separate explicit operator decision naming the exact local branch, its current candidate SHA, and the accepted `main` commit that replaces the task. The command does not infer supersession from patch equivalence, branch age, a closed PR, or the fact that a newer implementation exists. Stop all writers first and use `make local-purge` to remove reproducible ignored state.

```bash
make task-abandon \
    BRANCH=agent/<owner>/<task> \
    CANDIDATE=<exact-local-tip> \
    REPLACED_BY=<accepted-main-sha> \
    [PR=<closed-unmerged-pr>]
```

`make task-abandon` reuses the same local-target checks and non-force removal path as `task-cleanup`: the direct local branch ref must equal `CANDIDATE`, at most one registered task worktree may own it, the target cannot be the primary/current worktree, and the worktree must be clean including ignored files except its exact validated `.nvsop/session-binding.json`. It fetches `origin/main`, requires `REPLACED_BY` to resolve to that exact commit and to be retained by `origin/main`, then releases the binding immediately before removing the worktree and exact local branch/config. `REPLACED_BY` proves only that the named replacement is accepted trunk; the operator decision supplies the semantic assertion that the task is obsolete. The candidate itself need not be an ancestor of `main`.

When `PR` is supplied, the command queries that PR once and additionally requires `state=CLOSED`, `baseRefName=main`, the exact `headRefName`/`headRefOid` matching `BRANCH`/`CANDIDATE`, and no merge commit. A merged PR belongs to `task-cleanup`, while an open PR must first be deliberately closed or otherwise resolved. No-PR mode exists for explicitly abandoned local/intermediate tasks; it does not authorize deleting a task merely because no PR exists.

As with merged cleanup, all checks finish before the first destructive command. The command never deletes remote refs, closes PRs/Issues, changes queue state, synchronizes `main`, force-removes a worktree, sweeps other tasks, or discards dirty/unknown state. A failure after cleanup begins is reported as a partial result with no rollback.

**Done:** only the explicitly authorized obsolete local task is removed after exact candidate/replacement proof; unresolved, dirty, open-PR, or semantically unreviewed tasks remain untouched.

## Landing ownership and session binding

Status: **normative**. This section owns the development/landing ownership state machine, the automatic queue seam and the local implementation-session binding. The queue is implemented by [scripts/landing_queue.py](../../scripts/landing_queue.py) and driven by the trusted [landing-queue.yml](../../.github/workflows/landing-queue.yml) workflow; [scripts/land_pr.py](../../scripts/land_pr.py) keeps the one-shot CAS primitives that the queue calls under the bounded [PR lifecycle plan](#pr-lifecycle-approval). The controller is the landing path: its App-bound jobs are guarded on the `LANDING_APP_ID`/`LANDING_QUEUE_ISSUE` repository variables and execute subject to the protected `landing` environment, so the queue runs the reusable `blocking-ci.yml` and performs the authorized exact-head squash merge under the human-maintainer attestation.

### States and the single writer

```text
DEVELOPMENT -- freeze + handoff --> QUEUED --> ACTIVE --> MERGED
      ^                                          |
      |                                          v
      +------------- BLOCKED_* <-----------------+
```

- **DEVELOPMENT** — exactly one implementation session owns the task branch and worktree and is the only writer that may edit task code.
- **QUEUED** — the implementation session has stopped writing and handed a frozen candidate plus approved actions to landing. Landing owns only controlled integration mutation; the implementation session stays dormant.
- **ACTIVE** — landing performs the authorized integration actions (controlled base refresh, exact-head squash merge) against the frozen candidate. Task code is not edited here.
- **MERGED** — the exact landing head is squash-merged and retained by `origin/main`; verified cleanup may follow.
- **BLOCKED_CI / BLOCKED_CONFLICT / BLOCKED_REVIEW / BLOCKED_MUTATION / BLOCKED_SCOPE / BLOCKED_AUTHORITY / BLOCKED_INFRA** — landing stops and returns repair ownership to the original implementation session. Landing never performs semantic or code repair, conflict resolution, rebase, reset or scope change.
- **BLOCKED_AGENT_UNAVAILABLE** — a `BLOCKED` entry whose `evidence.repair_state` is `unavailable` is the normative encoding of "the original session cannot be recovered"; it is not a separate machine state, selects no replacement, and has no automatic rebind/recovery path. Continuing the same PR after this terminal state is an explicit token-bearing re-enqueue that produces a new generation, never a re-claim of the old one.

While landing is QUEUED/ACTIVE the implementation session is dormant and must not write the task branch. A BLOCKED_* outcome returns the task to DEVELOPMENT for repair; the repaired work produces a new validated candidate and is re-enqueued.

### Freeze and handoff preconditions

Before a task enters QUEUED, the implementation session records — and landing consumes — the exact task branch and worktree, the frozen candidate commit SHA, the target `main`, the bounded [PR lifecycle plan](#pr-lifecycle-approval) that names the authorized actions, and the applicable review/CI evidence bound to that candidate.

[scripts/landing_queue.py](../../scripts/landing_queue.py) consumes these facts, keeps the durable FIFO and drives the controlled integration described in [landing queue and single-flight CI](#landing-queue-and-single-flight-ci). The controller's App-bound jobs are guarded only on the `LANDING_APP_ID`/`LANDING_QUEUE_ISSUE` repository variables: when either is unset they are skipped, and when both are set they execute subject to the protected `landing` environment. Missing state branch, inbox issue, environment, private key or ruleset protections are not detected by the guard and fail closed at runtime.

### Repair handoff contract

A BLOCKED_* outcome is public only as the entry's opaque `blocked_reason` plus `evidence` (handoff id, expected/observed head, `main`, run/attempt, reason, category) in the queue state; it carries no credentials or runtime session IDs. [scripts/dispatch_landing_repair.py](../../scripts/dispatch_landing_repair.py) derives one machine-readable repair event for a repairable blocked entry from that evidence and the actual PR and existing attestation; `make pr-land-repair-event PR=<number>` prints it and refuses a stale event (the entry is no longer `BLOCKED` with released intents, the attestation branch no longer matches the open PR, or the recorded observed head/`main` is no longer current):

```json
{"version": 1, "event": "64-hex blocked-generation digest", "handoff": "32-hex", "repository": "owner/nvsop", "pr": 123, "branch": "agent/a/task", "blocked_state": "BLOCKED_CI", "authorization_root": "40-hex", "landing_head": "40-hex", "main": "40-hex", "failure": {"reason": "BLOCKED_CI", "category": "candidate", "run": "123", "attempt": "1"}, "evidence": {"handoff": "32-hex", "expected_head": "40-hex", "observed_head": "40-hex", "main": "40-hex", "run": "123", "attempt": "1", "reason": "BLOCKED_CI", "category": "candidate"}, "evidence_location": {"state_branch": "landing-queue-state", "path": "queue.json", "run": "123", "attempt": "1"}}
```

`blocked_reason` is exactly one of `BLOCKED_CI`, `BLOCKED_CONFLICT`, `BLOCKED_REVIEW`, `BLOCKED_MUTATION`, `BLOCKED_SCOPE`, `BLOCKED_AUTHORITY` or `BLOCKED_INFRA`; only the first five are code-repair handoffs. A private resume handoff adds `worktree`, `session_id` and `claim` and carries `ownership: DEVELOPMENT` plus `expected_next_action: repair`; it never enters public state, comments or Actions inputs. `make pr-land-repair PR=<number> ACTION=claim` posts one opaque `repair-claim` request (exact handoff, blocked generation and the claim's sha256 digest; the plaintext token never enters public state, comments or Actions inputs) that the trusted controller accepts exclusively with the state CAS; a second claim is rejected; while the claim is pending (`claimed`/`resuming`) every re-enqueue is rejected regardless of token, and only a terminal `resumed`/`unavailable` generation may re-enqueue with the matching completion digest. `ACTION=resume` reads the accepted claim back before it may call the host and asks the provider-neutral `HostBridge` to prove the exact bound session is recoverable with the old writer stopped and to resume it under the claim; a missing, corrupt or taken-over binding/worktree/receipt, an unrecoverable session, or an unproven host seam reports `repair-unavailable` and keeps the entry `BLOCKED` with no replacement. Only a confirmed `repair-resumed` returns the task to DEVELOPMENT, and an uncertain host result keeps the claim so a second writer cannot be created, while a re-enqueue clears the claim only with the matching token and re-queues at the tail; before it may call the host, `ACTION=resume` advances the accepted claim through the `resuming` intermediate state with the same CAS, so a token-bearing re-enqueue is accepted only from the terminal `resumed`/`unavailable` states and can never turn the PR back to `QUEUED`/`ACTIVE` while a host resume is in flight. Resume is single-flight: before any host call the invocation re-verifies the claim digest (including in the `resuming` state), the event generation and the bound worktree/binding, then atomically reserves the actual resume with a per-blocked-generation one-shot marker under the bound task worktree's `.nvsop/artifacts/landing/<64-hex event>.resume` (`O_CREAT|O_EXCL`, mode `0600`). Only the marker winner may call the host; a duplicate or an uncertain/crashed attempt reports `in-progress`/`uncertain` and never removes the marker or retries the host. The first invocation that advances `claimed`→`resuming` may report `pending-resume` when the asynchronous controller has not yet confirmed the transition, and an explicit rerun with the matching token continues once the claim is `resuming`. A claim stuck in `claimed` is retried by re-running `resume` (the digest and generation are re-checked); a stale event (moved head/`main` or changed evidence) releases it to the terminal `unavailable` rather than leaving it claimed. A claim stuck in `resuming`, or a resume marker left behind after a crash between its reservation and the host result, has no CLI release path by design — no second writer may touch it — so it is an operator action: after confirming the old writer is stopped, the operator posts one manual `repair-unavailable` inbox request carrying the same handoff, generation and claim digest (or waits for the next never-claimed generation after an explicit rebind), which the trusted controller accepts from the `resuming` pending state. Once the claim is terminal (`resumed`/`unavailable`) the same PR continues only through a token-bearing re-enqueue that produces a new generation, and a fresh `repair-claim` applies only to a new, never-claimed generation — there is no controller-side claim reset. `ACTION=resume --claim <token>` requires the plaintext token (public state stores only its sha256) and `ACTION=preflight [WORKTREE=<path>] [EXPECTED_GENERATION=<64hex>]` re-verifies repository, worktree, branch, PR, current `main`, blocked generation and released intents before the resumed session writes code; the local task worktree HEAD must still equal the event's `authorization_root`, while `observed_head` remains remote failure evidence rather than a local checkout target. The exclusivity is proven by the exclusive binding, the local receipt, the single accepted claim and the per-generation resume marker together — there is no always-on writer-detection service. Any code or head change invalidates the old candidate's technical evidence and requires rebuilding it; adding only review evidence with an unchanged head rebuilds the invalidated evidence without a new commit. An unrecoverable session is the manual rebind/recovery path: after the old writer is stopped and the exact task is verified, an operator removes and recreates the binding and receipt and claims again — no new machine state. The manual `ACTION=resume` CLI still defaults to `UnavailableHostBridge` and therefore fails closed unless a caller supplies a proven host bridge. The supported automatic host integration is the standalone local [landing repair service](../../scripts/landing_repair_service.py); it does not use WebCodex and does not change the GitHub queue's authority model.

`make pr-land-repair-service` runs that service against the same public queue with ordinary local `gh` authentication. For each repairable `BLOCKED` generation it re-derives the current event, resolves the exclusive local binding/receipt, verifies exactly one local adapter can resume the bound session, persists a `0600` plaintext claim only under the bound worktree, and then posts only the claim digest to the GitHub inbox. The service is restart-safe: the private claim is reused for the same generation, and a durable local claim-request receipt suppresses repeated public claim requests while the controller state is still catching up; the existing queue CAS plus the per-generation `.resume` marker remain the single-flight authority. The built-in Pi adapter locates exactly one existing Pi session file by the bound session id and uses Pi's exported `SessionManager.open(..., cwdOverride=<bound-worktree>)` runtime path so the same session resumes in the task worktree rather than its historical cwd. Pi exposes no session lock, so any live Pi writer in either the stored session cwd or the bound worktree—including CLI `pi` processes and service-launched resume helpers—is treated as a possible writer and the service refuses to claim until that ambiguity is removed. Other agent runtimes use repeated `--adapter-command <absolute-executable>` adapters with the narrow JSON `probe`/`resume` contract; an adapter is context recovery only and grants no publish, merge, scope, rebind or Issue authority. If zero adapters prove the session the entry remains unclaimed and `BLOCKED`; if more than one proves it, dispatch fails closed. A resumed agent must run repair `preflight` before tracked edits and then follows the ordinary DEVELOPMENT verification/review/lifecycle-approval path; the service never repairs code, re-enqueues a changed candidate, or merges on the agent's behalf.

### Implementation-session binding

`.nvsop/session-binding.json` maps one task worktree to the session that owns it. The bound id is the primary task-owner session — the resumable main-owner id — never an ephemeral worker or subagent id; an implementation subagent acts under that owner but does not become the binding. It is context lookup for dispatch and repair only — never publish, merge or scope authorization — while `.tmp/task-handoff.md` remains the continuity record. Its schema and repository-local-state rules are owned by [maintenance.md](maintenance.md#session-binding-state), and it is created or verified with:

```sh
make task-session-bind SESSION=<resumable-id>
```

Binding is required before the session's first tracked source edit. It is idempotent for the same session and refuses to overwrite an existing binding; another session cannot take over even during concurrent first binds, and the normal bind has no overwrite or force path. An unrecoverable original session is `BLOCKED_AGENT_UNAVAILABLE` and fails closed: replacement requires an explicit rebind/recovery decision after the old writer is stopped and the exact task is verified. Cleanup releases the binding only during verified exact merged cleanup in [§5.2](#52-synchronize-main-and-clean-one-verified-task) or explicitly abandoned/superseded cleanup in [§5.3](#53-clean-one-explicitly-abandoned-or-superseded-task).

### Landing queue and single-flight CI

[scripts/landing_queue.py](../../scripts/landing_queue.py) owns the durable automatic landing queue. GitHub is the source of truth: one JSON state file on the dedicated `landing-queue-state` branch, updated with a Contents-API blob-SHA compare-and-swap, plus an append-only issue-comment inbox that carries enqueue/dequeue wake requests. There is no daemon, lock service, database or polling loop. The trusted [landing-queue.yml](../../.github/workflows/landing-queue.yml) workflow runs default-branch code as the only writer of the state branch; the CLI never writes it.

States are `QUEUED`, `ACTIVE`, `BLOCKED`, `MERGED` and `CANCELLED` with at most one `ACTIVE` entry. Order is FIFO by server request id, with the PR number only as a deterministic tie-break; a repaired or replaced candidate re-enqueues at the tail, and a queued candidate that changes head is `BLOCKED_MUTATION` rather than a silent replacement of its authorization root. The state file records a consumed-inbox high-water id together with the entries, so an already-processed request never reactivates a terminal PR on replay; terminal entries stay terminal until a new authorized enqueue. `make pr-land-enqueue` validates the open non-draft PR, `main` base, exact `--expected-head`, the bound and clean local task worktree, and a referenced attestation comment (below), then posts one durable request and returns an opaque handoff id — it never grants authorization, and a failed wake dispatch leaves the durable request in place. `make pr-land-dequeue` releases the landing writer (a queued or active entry becomes an observable `CANCELLED`) and preserves the local binding. A blocked entry also accepts the opaque repair requests described in [repair handoff](#repair-handoff-contract), which serialize repair against re-enqueue with the same CAS. The public state and comments carry no runtime session identity and no worktree path; a private local receipt under `.nvsop/artifacts/landing/` maps the opaque id to local context.

Authorization is a human-maintainer attestation on the PR, not a boolean: an existing comment marked `<!-- landing-attestation -->` with a stable JSON body naming the exact repository, PR, root SHA, head branch and `main` target, the `refresh` and `squash-merge` actions, the original user confirmation source, completed exact-root `make check` evidence, and applicable independently executed Spec + Standards evidence (or an explicit justification). A reference alone is not proof and an agent-authored boolean or an `APPROVED` review grants nothing; the authenticated human comment is the trust seam. The consumer rejects outsider, bot and edited comments (`created_at == updated_at` required), binds the candidate to the comment id plus body digest and author, and re-reads the attestation at every integration action so a deleted, edited or dismissed authority blocks fail-closed. The queue does not claim automated semantic verification of quoted human evidence. The accepted body is exactly:

```json
{
  "version": 1,
  "repository": "owner/nvsop",
  "pr": 123,
  "root": "40-hex",
  "head_branch": "agent/a/task",
  "base": "main",
  "actions": ["refresh", "squash-merge"],
  "confirmation": {"source": "user message 2026-10-03", "quote": "approve the bounded plan"},
  "check": {"root": "40-hex", "command": "make check", "result": "passed", "evidence": "make check output"},
  "review": {"spec": "passed", "standards": "passed", "root": "40-hex", "evidence": "independent Spec + Standards review"}
}
```

`review` may instead be `{"not-applicable": "<reason>"}` for a candidate that needs no independent Spec/Standards review; `check.root`, `check.evidence`, `confirmation.source` and `confirmation.quote` must be non-empty and `check.root` must equal `root`.

The trusted workflow runs default-branch code as the only writer of the state branch and is the only place privileged. `prepare` consumes requests, authenticates each request author as a repository collaborator, then drives the single `ACTIVE` phase: when the PR is behind it persists a `REFRESHING` intent (old head, latest `main`, authorization root) with a CAS and calls the one `land_pr.refresh` seam exactly once. The new head is frozen and CI started only when its parents prove the controlled integration (`{old head, recorded main}`) and a matching App `synchronize` event (same PR, exact old/new head and App actor) proves the App performed it. A parent mismatch is `BLOCKED_MUTATION` even without an event, and an explicit non-matching event is `BLOCKED_MUTATION`; an ordinary wake with no App evidence keeps the `REFRESHING` intent unchanged, inherits no authorization and starts no CI while it waits for the matching App event. It then persists a `TESTING` intent with the exact head, base, Actions run id and attempt before the reusable `blocking-ci.yml` runs, so a repeated or unrelated event cannot rerun active CI or inherit another run's result. The controller distinguishes the trigger head from the tested head: a `pull_request_target` run executes the default-branch workflow, but its `head_sha`/`head_branch` belong to the triggering pull request, which may not be the `ACTIVE` candidate, so the run head is never bound to the tested head. For that event the trusted source is proven from the run's `referenced_workflows` — only a same-repository `blocking-ci.yml` at the fixed `main` controller commit (`ref: refs/heads/main`) is accepted, and a missing, malformed, foreign-repository or otherwise non-matching entry fails closed before any status or merge — while the other trusted events keep the exact `head_sha`/`head_branch` check because they run on the controller commit on `main`. Because the controller calls the CI with the relative `uses: ./.github/workflows/blocking-ci.yml`, the reusable workflow is from the same commit as the caller ([GitHub: reusing workflows](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows#calling-a-reusable-workflow)), so the recorded `referenced_workflows` SHA is the controller's own source commit. `finalize` re-reads the run identity, current head and `main`, review state and attestation; `main` moving during CI revokes the old statuses and releases as `BLOCKED_SCOPE`. The controller never resolves conflicts, rebases, resets, repairs code or retries arbitrary CI failures; a cancelled or timed-out run is runtime infrastructure, distinct from a genuine test failure (`BLOCKED_CI`).

Before any status or merge write, `finalize` reads the active branch rulesets and fails closed unless exactly three, each with an explicit `refs/heads/...` scope and no exclusions, hold: (a) a bypass-free `main` evidence ruleset requiring `pull_request` (squash-only, resolved threads) and strict up-to-date checks with `CI required` and `Landing gate` bound to the landing App integration id; (b) a `main` update restriction bypassing exactly the App; and (c) a `landing-queue-state` update restriction bypassing exactly the App. Wildcards, `~ALL`, exclusions, a foreign bypass or an unexpected applicable ruleset fail closed. A missing protection is `BLOCKED_AUTHORITY` or an actionable setup error, never a doc-only assertion. Only then does it publish both App-bound statuses on the exact tested head, persist a `MERGING` intent and call the one `land_pr.merge` CAS seam. A `MERGED` entry requires the exact-head squash proof retained on `main` and the tested tree equal to the merged tree; a crash after the merge response is recovered from the merged PR on the next trusted wake rather than re-running CI or re-merging. The post-`main`-push job verifies the recorded proof only.

The dedicated landing GitHub App creates attributable exact-head `CI required` and `Landing gate` statuses and receives the refresh/merge events that `GITHUB_TOKEN` suppresses. Its private key belongs to a protected GitHub Environment named `landing`, restricted to `main`, so only the privileged `prepare`/`finalize`/`integrity` jobs can read it; the [blocking-ci.yml](../../.github/workflows/blocking-ci.yml) jobs inherit no secret, hold only `contents: read`, and check out the candidate HEAD read-only as untrusted candidate code. `pull_request_review` is deliberately not a trigger, because it can run candidate workflow code with the App token; `finalize` reads reviews instead and an operator can wake the queue. The controller fails closed until the server-side protections are configured; code does not claim a ruleset or environment was applied.

The server-side prerequisites are operator-owned and normative: initialize `queue.json` version 3 (`{"version": 3, "consumed_request_id": 0, "entries": []}`) on the `landing-queue-state` branch, create the inbox issue, set the `LANDING_QUEUE_ISSUE`/`LANDING_APP_ID`/`LANDING_APP_BOT` repository variables, create a protected `landing` environment restricted to `main` with the App private key stored there as an environment secret rather than a repo-wide secret, and add the three branch rulesets that `finalize` requires (above). Repository code neither creates nor claims these settings.

The App-bound jobs are guarded on the `LANDING_APP_ID`/`LANDING_QUEUE_ISSUE` repository variables: when either is unset the guard job is skipped and `prepare`/`finalize`/`integrity` never start, so a `pull_request_target`, `issue_comment` or `push main` event cannot request the App secret or fail the run. When both are set the jobs execute subject to the protected `landing` environment's policy. The guard does not detect a missing state branch, inbox issue, environment, private key or ruleset; those requirements fail closed at runtime when the controller runs. The controller remains the sole writer of the queue state branch and no repository code creates server settings.

## Persistent continuity

Ordinary bounded single-session work needs no handoff file. Maintain ignored `.tmp/task-handoff.md` across sessions/compaction, multiple agents/worktrees, cross-module/critical changes or unfinished required review/validation. Keep the user request and complete acceptance; Entry / Reuse / Allowed writes / Preserve; fixed base and current candidate; dirty/untracked paths; findings and resolutions; valid command/environment/input evidence; next concrete action. Update facts rather than copying conversation. Handoffs grant neither authority nor a passing result.

## Local enforcement

[reference-transaction](../../scripts/githooks/reference-transaction) checks local ref transitions during `prepared`: `main` cannot be created/deleted/renamed or advanced to task work; an update must match an exact fetched remote-tracking `*/main`. New task branches use the declared namespace. Legacy `dev` creation/advancement is rejected; deletion requires its commits to be accounted for. `--no-verify` does not bypass this hook.

`pre-commit` rejects direct task commits on trunk and `pre-push` rejects direct pushes to remote `main`. Vendor and formatting checks remain enforced by the existing hooks. Removing `core.hooksPath` is not prevented by a repository-owned hook; do not claim it replaces managed Git or server-side protection.

## Target-environment and release gates

Target-environment scenarios, thresholds and pass criteria are owned by [roadmap §9](../design/solution-and-roadmap.md) and the [target-environment validation matrix](../research/target-environment-validation-matrix.md). This workflow only requires applicable evidence to be real and attributable; unavailable required hardware or services produces a gap, never silent mock success.

Release-promotion evidence is owned by [upgrade.md](../deployment/upgrade.md) together with the [target-environment validation matrix](../research/target-environment-validation-matrix.md). This workflow requires every applicable release item to have evidence or remain an explicit blocker; it does not duplicate the deployment inventory here.
