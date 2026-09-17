# Issue authoring and operations

Status: **normative**. This document owns work-item shape, classification, labels, dependencies, claim and closure. GitHub Issues own live specs and task state. Use `gh` inside the actual clone; infer its repository from `git remote -v`. [workflow.md](workflow.md) owns implementation, evidence, PRs and CI.

## Create one outcome

Use the [task form](../../.github/ISSUE_TEMPLATE/task.yml); when creating via `gh`, render the same sections. Inspect current accepted `main`, open PRs/Issues, the owning public seam and the roadmap/mechanism/ADR first. One Issue owns one observable, independently reviewable outcome. Split for different owners, real blockers, environments or independently valuable completion states, not line counts.

| Body section | Required meaning |
|---|---|
| Outcome | One observable result without an unnecessary internal design |
| Authority and current evidence | Approved source requirement and inspected implementation or measurement |
| Existing seam and reuse | Owning module/public entry and tests to reuse; concrete ownership/variation reason for a new seam |
| Scope | Allowed behavior/state changes and what must be preserved |
| Acceptance criteria | Checkable behavior, state transitions or required evidence owned by this Issue |
| Dependencies and blockers | Only actual prerequisites; native GitHub dependencies are authoritative |
| Evidence plan | Risk-selected evidence under workflow.md, reusing valid evidence first |
| Out of scope | Adjacent expansions not authorized |
| Traceability and linked validation | Original acceptance and precise deferred owner links where applicable |

Retain the form's task type, priority, stable Plan ID and parent map. Priority expresses impact/sequence, not readiness. A P0 can be blocked; P2 does not mean safe to execute. Apply classification labels after creation: the single form deliberately has no automatic wayfinder/readiness label because labels cannot vary by its task-type selection.

## Task types and labels

| Type | Owns | Labels/readiness |
|---|---|---|
| implementation | Bounded software behavior | `wayfinder:task`; `ready-for-agent` only with specified outcome, seam, acceptance and blockers |
| validation | Exact environment, prerequisite software, artifact and pass/fail evidence; not new product behavior | `wayfinder:task`; depends on software it exercises |
| decision | One unresolved choice and evidence needed to decide it | `wayfinder:grilling` when human judgment is the work product; blocks only behavior that depends on its outcome |
| needs-info | A named missing fact and who/source can provide it | `needs-info`, never also `ready-for-agent`; retain the relevant task/decision wayfinder label |

Canonical triage roles map directly to labels: `needs-triage` (maintainer evaluation), `needs-info` (missing information), `ready-for-agent` (fully specified autonomous work), `ready-for-human` (human implementation), `wontfix` (not actioned). A map uses `wayfinder:map`; child work uses the appropriate `wayfinder:research`, `wayfinder:prototype`, `wayfinder:grilling` or `wayfinder:task`. Labels express state but cannot replace body requirements or blockers.

## Acceptance and evidence

Acceptance describes external behavior or owned state, not private helper shapes, field counts, coverage percentages or speculative abstractions. Use [risk selection](workflow.md#choose-evidence-by-risk): sufficient existing evidence can mean zero new tests; ordinary gaps normally need 1–3 independent scenarios; critical invariants keep evidence at the real seam/infrastructure level. `make check` is a final code gate, not a demand to expand each inner loop.

Fallback, retry, compatibility, cache, integrity, recovery and generic framework behavior need an accepted contract, demonstrated failure, repository policy or security/safety boundary. State the concrete reason when they are required; do not invent them while dispatching work.

## Dependencies and delivery map

The current delivery map is [Issue #1](https://github.com/cybest993-xx/nvsop/issues/1). Product scope, software/validation phases and exit criteria are in [roadmap §8–9](../design/solution-and-roadmap.md#八开发路线). Original scope tickets and S-numbered dispatch slices are different tracking levels; do not duplicate their live status or full acceptance checklists in repository documents, or reopen superseded/cancelled tickets during synchronization.

The map keeps separate software and validation progress with links to owners. Before deferring an acceptance criterion, create/link an open validation owner, preserving original wording, source Issue/criterion, environment and evidence location. Update the implementation acceptance section explicitly; a generic footer cannot override a contradictory checkbox. Include full cross-module system/browser E2E when deferred, not just hardware suites. A documentation update never checks off implementation or validation work.

Native GitHub dependencies represent live blocking. Add an edge using the blocker's numeric **database id**, not Issue number or node id:

```sh
gh api repos/<owner>/<repo>/issues/<blocker> --jq .id
gh api --method POST repos/<owner>/<repo>/issues/<child>/dependencies/blocked_by -F issue_id=<blocker-database-id>
```

`issue_dependencies_summary.blocked_by` counts open blockers. Keep edges minimal: implementation depends on behavior it consumes, validation on needed software/environment, decisions only on affected choices. No reciprocal edges or serial chains merely to encode preferred execution order. Merge tickets that share owner/seam without independent outcome, blocker or evidence boundary. Split mixed software/field evidence without weakening or duplicating acceptance.

Link children as native sub-issues. Only where the tracker lacks that capability, use the map's task list plus `Part of #<map>` in the child. Only where native dependencies are unavailable, a top-level `Blocked by: #...` line is the documented representation; do not use it to bypass available native dependencies. A child is unblocked only after all actual blockers close.

## Read, claim and update

```sh
gh issue view <number> --comments
gh issue list --state open --limit <limit> --json number,title,labels,assignees
gh issue edit <number> --add-assignee @me
gh issue edit <number> --add-label <label> --remove-label <label>
gh issue comment <number> --body-file <file>
```

Before claiming, confirm the ticket is open, sufficiently specified, has no open native blocker and is unassigned. In a ticket-driven session, claiming is the first Issue write. The frontier is the map's open children with no open blocker or assignee; map order selects among them. Bound queries and read relevant comments/labels rather than copying every body into context.

A skill request to “publish to the issue tracker” means create a GitHub Issue; “fetch the relevant ticket” means read the Issue and comments. Resolve shared Issue/PR number space with `gh pr view <n>` or `gh issue view <n>` as appropriate.

**PRs as a request surface: no.** Product requests go to Issues; task PRs follow workflow.md. This does not import external-contribution restrictions from a reference repository.

## Close with evidence

An implementation closes only after owned behavior, required current-phase evidence, applicable independent review and merge evidence are complete, and each deferred original criterion has an open validation owner. A validation closes only with its actual environment and evidence. A decision closes after its authoritative design record and affected Issues are updated. Needs-info closes when the fact is resolved or the work is explicitly abandoned.

Read Git history, final CI and live Issue state before recording completion. Record commands/results, evidence location and remaining gaps. With closure authorization, comment the outcome, close via `gh issue close <number> --comment <summary>`, and add the result pointer to the map's Decisions-so-far where applicable. Stop once acceptance is met; optional refactors and hardening need a separate demonstrated requirement.
