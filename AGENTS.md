# Repository instructions

This repository is a product monorepo for the SOP compliance system. Use the canonical terms in `CONTEXT.md`.

## Route the task

Read the matching sections and follow only the pointers relevant to the task. Verify current files and configuration before relying on a description of what exists.

- **Plan or investigate**: deliver the requested findings or plan; repository edits start when requested.
- **Change code or check scripts**: read the [harness §4–§5](docs/design/repository-harness.md#4-test-placement-and-evidence) for test policy, authoring and size guidance. Locate the public entry point, affected callers and existing tests before editing.
- **Change ownership, dependencies, CI, deployment or `vendor/`**: read the relevant [harness §1–§3](docs/design/repository-harness.md#1-architecture-rule) and [§6–§8](docs/design/repository-harness.md#6-stable-command-interface). This is the normative repository harness.
- **Change product behavior or architecture**: use [`solution-and-roadmap.md`](docs/design/solution-and-roadmap.md) to locate the affected mechanism spec and its ADRs. It is the current decision source; name any ADR that needs reopening and explain why.
- **Write instructions or documentation**: use [harness §4](docs/design/repository-harness.md#4-test-placement-and-evidence) for verification, [§5](docs/design/repository-harness.md#comments-and-documentation-language) for language and [§9](docs/design/repository-harness.md#9-agent-instruction-hierarchy) for instruction structure.
- **Work with an issue or label**: read [`issue-tracker.md`](docs/agents/issue-tracker.md) and [`triage-labels.md`](docs/agents/triage-labels.md).

## Invariants

These five hold before you read anything else. Everything else lives in the harness or the decision source.

- The NVIDIA base code in `vendor/sop-monitoring-blueprints/` is this system's trunk, not an external dependency. Reuse what it implements; keep our logic in `apps/edge-runtime/` behind the one recorded hook. Two implementations of one capability on the same path are a defect.
- The inference host is autonomous: judgment, violation latching, disposal, and evidence buffering keep working while the center is unreachable. The center is a management and aggregation plane, never on the real-time error-proofing path.
- The judgment core is a pure function over normalized observations, standard-library-only, and knows nothing of camera SDKs, inference frameworks, or connectors.
- A module owns its behavior, tables, and migrations behind one small interface. Callers and tests cross that same seam.
- Secrets, credentials, customer media, model weights, generated data, and production dumps stay out of Git. Fixtures are synthetic or explicitly sanitized.

## Implement, verify and hand off

Keep the primary branch named `main`.

Follow the [working and review cycle](docs/design/repository-harness.md#working-and-review-cycle): reuse the task's isolated worktree, apply the [test and evidence policy](docs/design/repository-harness.md#4-test-placement-and-evidence), and finish in the same main session. Keep scope, valid evidence and the next action in the ignored `.tmp/task-handoff.md`.

Keep the task's full acceptance criteria through every stage. Report the delivered behavior, checks and their results, and any outstanding review or validation; a partial implementation or a size report is not completion.
