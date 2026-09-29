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

The hook path must resolve to `scripts/githooks` or its configured equivalent. A new task starts from the accepted fetched `origin/main` tip and does not require synchronizing the primary `main` worktree. Never reset a task worktree to obtain a clean base. `.nvsop/` is policy-enforced as untracked local state, so preserving it cannot overwrite a committed path. Synchronizing the dedicated primary `main` worktree is a separate authorized operation owned by [§5.1](#51-confirm-the-exact-squash-merge).

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
| `make local-clean` | Delete only declared reproducible local artifacts; preserve `.nvsop/dev-main`, secrets and unknown ignored state |
| `make pr-check PR=<number>` | Read-only machine-state preflight; never substitutes for independent review or merge authorization |
| `make change-size` | Advisory size report from `BASE`, default `origin/main`; also prints diff review hints; never a bound |
| `make task-check BASE=<40hex> ALLOW='<paths>' [MAX_LINES=<N>]` | Fixed-base scope and cumulative added+deleted budget; also prints diff review hints; reports `task_check=within-bounds` or `task_check=pause` (script exits 3 on pause); not acceptance |
| `make contracts` | Generated OpenAPI compatibility and Web client; procedure in [maintenance.md](maintenance.md#generated-contracts) |

Frozen `uv.lock` and `pnpm-lock.yaml` own resolution. Do not replace applicable checks with ad-hoc installs. Save task-branch commits when useful; they record progress, not acceptance.

**Done:** all owned acceptance criteria and required current-phase evidence are accounted for, with actual commands/results or explicit gaps. Stop optional tests/refactors once this is true.

## 3. Review and repair

Self-review the complete diff, including untracked additions, affected callers and references. Require one consolidated **independent read-only review** for critical risks, public/cross-module interfaces, shared behavior, dependencies, CI, deployment, `vendor/`, repository policy, agent instructions or unbounded impact. Ordinary bounded changes need no independent reviewer by default.

Changes to gates, new exemptions and weakened tests require the independent reviewer to read the actual diff, with evidence bound to a fixed base/candidate. Fewer test assertions is not by itself weakening; a weakened test is one that no longer fails on the defect it claims to cover. A `harness_review=required` line from `pr-check` is a risk hint only — it enforces nothing server-side and is not review approval. Server-side review settings are configured separately by the maintainers, and the existing manual squash merge, explicit user merge authorization and independent-subagent review mechanisms are unchanged.

The reviewer reports **Spec** and **Standards** together against a fixed base/candidate and consumes still-valid evidence instead of rerunning everything. A separately executed, read-only subagent review is an acceptable independent review mechanism; the reviewer reads the actual task worktree, while the main implementation session owns repairs. The implementer cannot count self-review as independent review. Report whether review was separately executed or only a self-review. Do not move repairs to a review worktree.

Repair concrete findings as one bounded batch, rerun affected checks and review the repair increment plus affected callers. A missing-test finding blocks only for a concrete failure path, uncovered critical risk or unmet acceptance. Naming, size and optional cleanup block only when tied to a defect or violated invariant. Missing required review/validation keeps merge readiness pending.

After the user has seen the explicit candidate, the actual diff, the applicable independent Spec and Standards results and the verification evidence, an explicit confirmation from that user satisfies human approval where it is required — merge authorization and other gate-sensitive decisions — and needs no additional GitHub reviewer identity. This is the human-approval path, not a new independent-review requirement: independent technical review still applies only when this workflow requires it, and ordinary low-risk changes do not gain one. Record the real confirmation reference or content, the confirmed candidate and the authorized action scope. An agent self-report, a boolean in the PR body or a general "continue implementing" is not confirmation; a user confirmation that explicitly authorizes the specific action needs no second confirmation. This approval does not replace the independent technical review, applicable CI or existing server-side rules, and it grants no publication or merge permission beyond a named action; authorization is never presumed. It applies only to the confirmed candidate and does not carry over once that candidate changes. Do not build approval storage, machine fields or an extra registry.

**Done:** findings are resolved or explicitly blocking, and evidence covers the candidate's code, tests, configuration, dependencies and relevant external inputs. Evidence does not survive changes to those covered inputs.

## 4. Publish the candidate and evaluate CI

Publication requires user authorization for the action and target. Publish only the task branch and open its PR directly against `main`; never push `HEAD:main`. Use the [PR template](../../.github/pull_request_template.md) to record outcome, scope, risks, actual evidence and documentation impact, not another full rulebook.
Before merge, `make pr-check PR=<number>` may aggregate the PR head/base, `CI required`, branch-protection visibility and local candidate identity. Treat `unknown` or absent protection and a missing check as blocked. The exact PR head needs successful `CI required`, required independent review evidence and explicit user authorization. `pr-check` deliberately leaves independent review as manual confirmation and never grants merge authorization.
For candidates that change architecture/Issue/mechanism/ADR authority, the module-registry manifest or shared machine contracts, the same preflight reports `dispatch_impact_review=required`. Record the actual open/ready Issue scan and dispositions in the PR template; this mechanical evidence does not replace semantic review of whether the affected set is complete.

`pr-check` also reports `harness_review=required` when the candidate touches gate, policy, manifest or check-configuration entry points. Like the dispatch flag it is a preflight risk hint, never a server-enforced review or an approval; record the independent review in the PR template's existing Review section.

```sh
git push -u origin agent/a/<task-slug>
gh pr create --base main --head agent/a/<task-slug>
```

### CI gates

The supported CI path uses GitHub-hosted `ubuntu-24.04` runners. The repository is intentionally public; do not replace the hosted path with a local or self-hosted runner as an account-billing workaround. A runner-topology change is a separate CI-policy change and requires the same review as other workflow authority changes. Repository visibility, Actions permissions, branch protection/rulesets and repository secrets remain server-side settings; workflow files do not configure them.

The sole aggregate required PR status is `CI required` from [blocking-ci.yml](../../.github/workflows/blocking-ci.yml). Server-side repository settings must separately require it when protection is available. `pr-check` makes missing or unknown enforcement visible; regardless of server enforcement, the exact PR head still needs successful `CI required`. Its `always()` gatherer requires explicit success from scope, lockfile checks and every gate family; failed, cancelled or skipped dependencies are not success.

[ci_scope.py](../../scripts/ci_scope.py) compares the actual base/candidate commits with rename detection disabled. A missing comparison baseline forces all gates. The selector also owns integration, browser and media applicability; jobs consume its outputs instead of maintaining their own path regexes. Media-owned inputs select both real-infrastructure and browser lanes, then run `make media-system` and `make web-e2e-whep` with explicit live fixtures so environment-dependent tests cannot silently satisfy media evidence by skipping.

| Compared paths | Existing blocking lane |
|---|---|
| Only root `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `README.md` or Markdown under `docs/` | `make check-docs`; integration/browser/media report explicit no-change success |
| Center, Edge, shared contracts, system fixtures or system tests | `make check` plus the real-infrastructure lane |
| Web inputs | `make check` plus the browser lane |
| Media deployment/test inputs | `make check` plus real-infrastructure, playback and WHEP browser evidence |
| `.github/`, `Makefile`, the selector/actionlint installer, or unusable baseline | All blocking families |

Filtering is an optimization, not an exemption. Lockfiles are always checked; applicable code gates regenerate artifacts and verify tracked and untracked cleanliness. Keep immutable action pins, least-privilege permissions, frozen installs, job timeouts and cancellation of superseded PR runs. Do not relax this selection merely because a script change accompanies documentation. Workflow changes additionally install the pinned `actionlint` release through the checksum-verifying repository installer and run `make ci-lint`. Browser failures upload only `.nvsop/artifacts/web/test-results/` with a pinned upload action and short retention; do not upload `.nvsop/dev-main`, environment files or broader workspaces as diagnostic artifacts.

### Codex review and merge

Codex GitHub review is optional semantic review evidence; it does not replace deterministic CI, branch protection or the repository's independent-review rule. Its absence, delay, quota limit or delivery failure does not block delivery, and no exact-SHA Codex review is required. The repository does not run a second model reviewer, translate Codex comments into a custom status, or automatically merge after an AI verdict.

Evaluate any concrete Codex findings as review feedback and address confirmed defects through the normal repair process. Deterministic style, lint, generated-file and test gates remain owned by `blocking-ci`; Codex may focus on correctness, architecture, safety and repository-governance regressions.

The active server-side `main` ruleset is the mechanical merge boundary. It requires a pull request, successful `CI required`, an up-to-date branch before merge and resolved review conversations; force pushes and branch deletion are blocked. Repository-policy, CI, architecture, shared-contract and other critical changes still require the consolidated independent read-only Spec + Standards review from section 3. This review may be performed by a separate read-only subagent; Codex is not an additional requirement.

All accepted pull requests are merged manually with squash after the exact candidate satisfies the required CI and review evidence and the user explicitly authorizes merge. Do not enable a repository workflow that treats any AI verdict as merge authorization or races GitHub's branch rules.

**Done:** the published branch names the verified candidate, PR base is `main`, the final candidate has green `CI required`, required independent review is complete, required conversations are resolved, and merge authorization is explicit. Earlier CI or review evidence does not transfer across candidate changes.

## 5. Merge and clean up

Merges require explicit authorization and the required CI/review evidence. Use GitHub's squash merge path after the active `main` ruleset is satisfied; there is no repository-owned automatic AI merge path. Keep merge confirmation separate from local task retirement, and stop all task writers before starting cleanup.

### 5.1 Confirm the exact squash merge

From a clean worktree that is not the task worktree, fetch the accepted trunk and inspect the PR once:

```bash
git fetch origin main
gh pr view <pr-number> --json state,headRefName,headRefOid,baseRefName,mergeCommit
git merge-base --is-ancestor <merge-commit-oid> origin/main
```

Continue only when the response is `MERGED`, its `baseRefName` is `main`, its `headRefName` and `headRefOid` equal the reviewed branch and candidate SHA, its `mergeCommit.oid` is present, and that recorded commit is retained by `origin/main`. A squash merge does not make the pre-squash candidate an ancestor of `main`; do not substitute that check. This confirmation is read-only evidence for the operator, not shared state consumed by the cleanup command.

If the primary checkout must be synchronized, identify the dedicated primary `main` worktree from `git worktree list --porcelain` and record the accepted `origin/main` commit as `ACCEPTED_ORIGIN_MAIN`. Tracked, staged and ordinary untracked changes block synchronization; ignored personal configuration and caches that the update does not touch are preserved as they are and do not block it. Synchronize only by fast-forwarding that exact target:

```bash
MAIN_WORKTREE=<primary-main-worktree>
ACCEPTED_ORIGIN_MAIN=<accepted-origin-main-sha>
(
    set -eu
    test "$(git -C "$MAIN_WORKTREE" branch --show-current)" = main
    MAIN_STATUS="$(git -C "$MAIN_WORKTREE" status --porcelain=v1 --untracked-files=all)"
    test -z "$MAIN_STATUS"
    git -C "$MAIN_WORKTREE" fetch origin main
    test "$(git -C "$MAIN_WORKTREE" rev-parse origin/main)" = "$ACCEPTED_ORIGIN_MAIN"
    git -C "$MAIN_WORKTREE" merge --ff-only --no-overwrite-ignore "$ACCEPTED_ORIGIN_MAIN"
    test "$(git -C "$MAIN_WORKTREE" rev-parse HEAD)" = "$ACCEPTED_ORIGIN_MAIN"
)
```

Stop when the target branch is not `main`, when any scoped Git read fails, when the histories have diverged, when `origin/main` is not `ACCEPTED_ORIGIN_MAIN`, or when the update would overwrite an ignored path; never fall back to merge, rebase, reset or stash. Never reset a task worktree. This synchronization is a separate manual operation with its own authorization, not implied by implementation or merge work; `retire_task.py` never performs it. Use `make local-clean` when a task worktree must remove reproducible artifacts before retirement; the command deliberately preserves fixed-instance state and unknown ignored files.

### 5.2 Retire one verified task

With writers stopped, run the versioned single-task command from a different worktree and provide the exact reviewed head SHA:

```bash
python3 scripts/retire_task.py \
    --pr <pr-number> \
    --branch agent/<owner>/<task> \
    --candidate <reviewed-head-sha>
```

The command does not scan branches or historical PRs. Before any destructive command it verifies the direct local `refs/heads/agent/<owner>/<task>` tip, queries the supplied PR once for `state`, `headRefName`, `headRefOid`, `baseRefName`, and `mergeCommit`, fetches `origin main`, and verifies the recorded squash commit is retained by `origin/main`. It refuses symbolic or out-of-scope refs, a mismatched candidate, multiple registered worktrees, the primary or current worktree, dirty worktrees including ignored files, and Git read or configuration failures. A clean task worktree is removed without force; the exact local branch ref is then deleted with `git update-ref --no-deref` and its expected old SHA. Only the exact local `branch.<task>` configuration section is removed; global and similarly prefixed sections remain untouched.

All checks finish before the first cleanup command. The individual worktree removal, ref deletion, and local configuration removal are not a multi-command transaction: if a later command fails, earlier changes remain, the command exits nonzero, and no rollback is promised. Inspect the repository and reconcile that partial result manually. The script never resets or synchronizes `main`, deletes remote refs, closes Issues, uses force deletion, or sweeps other tasks.

**Done:** the explicitly supplied merged task is retired only after the exact squash proof, and every unproved or unsafe task remains untouched. Report the command, actual result, and any partial-failure or synchronization gap.

## Persistent continuity

Ordinary bounded single-session work needs no handoff file. Maintain ignored `.tmp/task-handoff.md` across sessions/compaction, multiple agents/worktrees, cross-module/critical changes or unfinished required review/validation. Keep the user request and complete acceptance; Entry / Reuse / Allowed writes / Preserve; fixed base and current candidate; dirty/untracked paths; findings and resolutions; valid command/environment/input evidence; next concrete action. Update facts rather than copying conversation. Handoffs grant neither authority nor a passing result.

## Local enforcement

[reference-transaction](../../scripts/githooks/reference-transaction) checks local ref transitions during `prepared`: `main` cannot be created/deleted/renamed or advanced to task work; an update must match an exact fetched remote-tracking `*/main`. New task branches use the declared namespace. Legacy `dev` creation/advancement is rejected; deletion requires its commits to be accounted for. `--no-verify` does not bypass this hook.

`pre-commit` rejects direct task commits on trunk and `pre-push` rejects direct pushes to remote `main`. Vendor and formatting checks remain enforced by the existing hooks. Removing `core.hooksPath` is not prevented by a repository-owned hook; do not claim it replaces managed Git or server-side protection.

## Target-environment and release gates

Target-environment scenarios, thresholds and pass criteria are owned by [roadmap §9](../design/solution-and-roadmap.md) and the [target-environment validation matrix](../research/target-environment-validation-matrix.md). This workflow only requires applicable evidence to be real and attributable; unavailable required hardware or services produces a gap, never silent mock success.

Release-promotion evidence is owned by [upgrade.md](../deployment/upgrade.md) together with the [target-environment validation matrix](../research/target-environment-validation-matrix.md). This workflow requires every applicable release item to have evidence or remain an explicit blocker; it does not duplicate the deployment inventory here.
