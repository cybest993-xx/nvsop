# Repository instructions

This repository is a product monorepo for the SOP compliance system. Use `CONTEXT.md` when naming or changing domain concepts; it is a glossary, not a mandatory prelude to unrelated tooling or styling work.

## Route the task

Read only the guidance triggered by the task, then expand when affected callers, dependencies or conflicting evidence require it. Verify current code/configuration before relying on prose or old session notes.

- **Plan or investigate**: deliver the requested findings or plan; repository edits start when requested.
- **Change code or check scripts**: read [`repository-authoring.md`](docs/design/repository-authoring.md) and [`repository-verification.md`](docs/design/repository-verification.md). Locate the public entry point, affected callers and existing tests before editing.
- **Change repository shape, ownership or cross-module dependencies**: read [`repository-architecture.md`](docs/design/repository-architecture.md).
- **Change CI, generated contracts, dependencies or `vendor/`**: read [`repository-verification.md`](docs/design/repository-verification.md) and [`repository-maintenance.md`](docs/design/repository-maintenance.md).
- **Change deployment/runtime configuration or upgrade procedures**: read [`docs/deployment/`](docs/deployment/) through the [`docs/README.md`](docs/README.md) index plus the relevant architecture/verification rules.
- **Change product behavior or architecture**: search [`solution-and-roadmap.md`](docs/design/solution-and-roadmap.md) for the affected mechanism, then read its mechanism spec and relevant ADRs. Name any ADR that needs reopening and explain why.
- **Write instructions or documentation**: read [`repository-authoring.md`](docs/design/repository-authoring.md), [`repository-verification.md`](docs/design/repository-verification.md), and for agent instructions [`repository-workflow.md`](docs/agents/repository-workflow.md).
- **Work with an issue or label**: read [`issue-tracker.md`](docs/agents/issue-tracker.md) and [`triage-labels.md`](docs/agents/triage-labels.md).

[`repository-harness.md`](docs/design/repository-harness.md) remains the compatibility router for historical `harness §N` references; new instructions should link the direct document above.

## Invariants

These hold before loading task-specific guidance:

- The NVIDIA base code in `vendor/sop-monitoring-blueprints/` is this system's trunk, not an ordinary external dependency. Reuse what it implements; keep NVSOP-owned judgment behavior in `apps/edge-runtime/` behind the recorded minimal hook. Two active implementations of one capability on the same path are a defect.
- The inference host is autonomous: judgment, violation latching, disposal and evidence buffering keep working while the center is unreachable. The center is a management and aggregation plane, never on the real-time error-proofing path.
- The judgment core is a pure function over normalized observations, standard-library-only, and knows nothing of camera SDKs, inference frameworks or connectors.
- A module owns its behavior, tables and migrations behind one small interface. Callers and tests cross that same seam.
- Secrets, credentials, customer media, model weights, generated data and production dumps stay out of Git. Fixtures are synthetic or explicitly sanitized.

## Implement, verify and stop

`main` is the shared trunk and pull-request target; it must not receive direct task edits, task commits, or direct pushes. Start new task work from the accepted `main` tip on an `agent/<agent-id>/<task-slug>` branch in its own worktree, publish that branch, and open its PR directly against `main`. `dev` is retired and is not an integration path. Before creating or changing branches/worktrees/refs, read [`local-branch-workflow.md`](docs/design/local-branch-workflow.md) and enable the versioned hooks with `make hooks`. Use [`repository-workflow.md`](docs/agents/repository-workflow.md) for the working cycle and continuity rules.

State risk, test reuse/additions and intended checks in at most three lines, then proceed within the authorized scope. Existing evidence may justify zero new tests; use the verification policy rather than a universal TDD ceremony.

Use contract-driven implementations: cover specified behavior and required failure paths directly; do not add speculative fallback, retry, compatibility or integrity layers without a demonstrated contract need. Preserve validation/error handling required by callers, tests, repository policy or security/integrity boundaries.

During iteration run the smallest affected Make targets with their prerequisites. `make check` is the final CPU-only code gate, not the default inner loop; documentation-only changes use `make check-docs`. Required integration, browser, CI and release evidence remain unchanged.

Self-review the complete diff and affected callers/references. Use the independent-review triggers in [`repository-verification.md`](docs/design/repository-verification.md); instruction and repository-policy changes do not exempt themselves. One responsible main session owns completion.

Keep every acceptance criterion through implementation. Once behavior and required evidence are complete, stop adding optional tests/refactors/cleanup. Report delivered behavior, actual checks/results and remaining gaps; committed or pushed does not mean merge-ready. Merge and publication require the user's authorization.
