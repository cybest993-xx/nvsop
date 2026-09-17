# Issue authoring for dispatched work

Status: **normative**.

This document owns the shape and creation rules for implementation, validation, decision, and needs-info work items. Tracker mechanics remain in [`issue-tracker.md`](issue-tracker.md); labels remain in [`triage-labels.md`](triage-labels.md); evidence selection remains in [`../design/repository-verification.md`](../design/repository-verification.md).

## Create from one outcome

Use `.github/ISSUE_TEMPLATE/task.yml` for dispatched work. One issue owns one observable outcome that can be completed and reviewed independently. Split only when the pieces have different owners, blockers, environments, or independently valuable completion states; do not split a coherent change merely to reduce line count.

Before creating the issue, inspect current `main`, open PRs/issues, the owning public seam, and the roadmap/mechanism/ADR that authorizes the behavior. Record current evidence rather than copying an old plan as if it were still fact.

The issue body must make these facts explicit:

1. **Outcome** — one observable result, stated without prescribing an unnecessary internal design.
2. **Authority and current evidence** — the roadmap/mechanism/ADR/source acceptance plus the current implementation or measured fact that was checked.
3. **Existing seam and reuse** — owning module/public entry point and existing implementation/tests to reuse. A new seam needs a concrete variation or ownership reason.
4. **Scope** — allowed behavior/state changes and what must be preserved.
5. **Acceptance criteria** — checkable behavior owned by this issue.
6. **Dependencies and blockers** — only prerequisites that actually prevent this outcome. GitHub native dependencies are authoritative.
7. **Evidence plan** — risk-selected evidence from `repository-verification.md`, reusing valid evidence first.
8. **Out of scope** — nearby expansions that are intentionally not authorized.
9. **Traceability and linked validation** — original acceptance wording or deferred validation links when needed.

## Classify before dispatch

- **implementation** — owns a bounded software behavior change. Use `wayfinder:task`; apply `ready-for-agent` only when the outcome, seam, acceptance, and blockers are sufficiently specified.
- **validation** — owns evidence, not product behavior. Use `wayfinder:task`. State the exact environment, prerequisite implementation, evidence artifact, and pass/fail criterion. Validation depends on the software it needs; implementation does not depend on deferred validation unless the roadmap makes that evidence a software-exit requirement.
- **decision** — owns one unresolved product/architecture choice and the evidence needed to decide it. Use `wayfinder:grilling` when human judgment is the work product. It blocks only tickets whose implementation genuinely changes with that choice.
- **needs-info** — the required fact is unavailable. Apply `needs-info`, name exactly what information is missing and who/source can provide it, and do not also mark it `ready-for-agent`. Keep the wayfinder label that identifies whether the unresolved item is a task or a decision.

The single GitHub form cannot vary labels by the selected task type, so it intentionally sets no automatic wayfinder/readiness label. Apply the classification labels immediately after creation; labels are state, not a substitute for the body fields above.

Priority is impact/sequence, not task type. A P0 issue may still be blocked; a P2 cleanup is not automatically safe to execute.

## Acceptance and evidence rules

Acceptance criteria describe externally observable behavior, owned state transitions, or required evidence. Do not require private helper shapes, field counts, coverage percentages, or a specific abstraction unless that shape is itself a contract.

Select evidence by risk using [`repository-verification.md`](../design/repository-verification.md):

- reuse valid existing evidence before adding tests;
- low-risk documentation/copy/pure cleanup may add no automated tests;
- an ordinary uncovered behavior gap normally needs 1–3 independent scenarios;
- critical invariants require evidence at the seam/infrastructure level where the risk exists;
- `make check` is the final CPU code gate, not a reason to expand every issue's inner-loop test plan;
- full cross-module/browser, hardware, scale, long-stability, and field evidence is carried by explicit validation issues when the roadmap permits deferral.

An issue must not invent speculative fallback, retry, compatibility, cache, integrity, recovery, or generic framework behavior. Add such behavior only when an accepted contract, demonstrated failure, repository policy, or security/safety boundary requires it. State the concrete reason in Scope or Acceptance when it is required.

## Dependency and split rules

Use native GitHub issue dependencies for live blocking. Text may explain the edge but does not replace the native dependency graph.

Keep the graph minimal:

- implementation depends on the smallest behavior it consumes, not on unrelated downstream UI or field validation;
- validation depends on all software/environment prerequisites it exercises;
- a decision blocks only implementations whose correct behavior differs by the decision outcome;
- a needs-info ticket is a blocker only when its missing fact is necessary to proceed safely;
- do not create reciprocal dependencies or serial chains solely to enforce a preferred execution order.

When two tickets touch the same owner/seam and neither has an independent outcome, blocker, or evidence boundary, merge them. When a ticket mixes software delivery with target-environment evidence, split the validation and link it instead of weakening or duplicating the acceptance criterion.

## Closure

Close an implementation issue when its owned behavior, required current-phase evidence, applicable independent review, and merge evidence are complete, and every deferred original criterion is linked to an open validation owner. Close a validation issue only when its stated environment and evidence exist. Close a decision when the decision is recorded in its authoritative design home and affected issues are updated. Close needs-info only after the missing information is resolved or the work is explicitly abandoned.

Once acceptance is met, stop. Optional refactors, extra tests, generic hardening, and adjacent cleanup need their own demonstrated requirement before they become work.
