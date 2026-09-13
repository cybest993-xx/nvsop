# Repository instructions

This repository is a product monorepo for the SOP compliance system. Use `CONTEXT.md` when naming or changing domain concepts; it is a glossary, not a mandatory prelude to unrelated tooling or styling work.

## Route the task

Read the matching subsections, not entire reference documents by default. Expand when affected callers, dependencies or conflicting evidence require it; scoped reading does not waive applicable rules. Verify current files and configuration before relying on descriptions or old session notes.

- **Plan or investigate**: deliver the requested findings or plan; repository edits start when requested.
- **Change code or check scripts**: start with the [test policy](docs/design/repository-harness.md#risk-and-test-additions) and [implementation checklist](docs/design/repository-harness.md#implementation-checklist). Locate the public entry point, affected callers and existing tests; follow the relevant evidence and authoring subsections for the change.
- **Change ownership, dependencies, CI, deployment or `vendor/`**: read the relevant [harness §1–§3](docs/design/repository-harness.md#1-architecture-rule) and [§6–§8](docs/design/repository-harness.md#6-stable-command-interface). This is the normative repository harness.
- **Change product behavior or architecture**: search [`solution-and-roadmap.md`](docs/design/solution-and-roadmap.md) for the affected mechanism, then read its spec and relevant ADRs. It is the current decision source; name any ADR that needs reopening and explain why.
- **Write instructions or documentation**: use [§4](docs/design/repository-harness.md#4-test-placement-and-evidence) for verification, [§5](docs/design/repository-harness.md#comments-and-documentation-language) for language and [§9](docs/design/repository-harness.md#9-agent-instruction-hierarchy) for instruction structure and loading checks.
- **Work with an issue or label**: read [`issue-tracker.md`](docs/agents/issue-tracker.md) and [`triage-labels.md`](docs/agents/triage-labels.md).

## Invariants

These five hold before you read anything else. Everything else lives in the harness or the decision source.

- The NVIDIA base code in `vendor/sop-monitoring-blueprints/` is this system's trunk, not an external dependency. Reuse what it implements; keep our logic in `apps/edge-runtime/` behind the one recorded hook. Two implementations of one capability on the same path are a defect.
- The inference host is autonomous: judgment, violation latching, disposal, and evidence buffering keep working while the center is unreachable. The center is a management and aggregation plane, never on the real-time error-proofing path.
- The judgment core is a pure function over normalized observations, standard-library-only, and knows nothing of camera SDKs, inference frameworks, or connectors.
- A module owns its behavior, tables, and migrations behind one small interface. Callers and tests cross that same seam.
- Secrets, credentials, customer media, model weights, generated data, and production dumps stay out of Git. Fixtures are synthetic or explicitly sanitized.

## Implement, verify and stop

Use an `agent/<agent-id>/<task-slug>` branch in its own worktree for task edits. Treat `main` and `dev` as shared refs; do not make direct task commits on either. Before creating or changing branches, worktrees or refs, read the [local branch workflow](docs/design/local-branch-workflow.md) and enable its versioned hooks with `make hooks`. Apply the [working cycle](docs/design/repository-harness.md#working-and-review-cycle) to select workspace isolation and persistent handoff; neither is mandatory ceremony for every bounded single-session change.

State risk, test reuse or additions, and intended checks in at most three lines, then proceed within the authorized scope. Existing evidence may justify zero new tests; follow §4 rather than a universal TDD workflow.

Use contract-driven implementations: cover the specified behavior and required failure paths directly, while keeping speculative, unrequested defensive branches and fallback paths out of the implementation. Preserve validation and error handling required by the contract, repository policy, callers, tests, or security/integrity boundaries. Keep hash verification targeted and non-redundant; avoid broad or repeated hashing of large files, directories, generated trees, or unchanged artifacts unless required by the contract, repository policy, callers, tests, or a security/integrity boundary.

During iteration, run the smallest affected Make targets with their prerequisites. `make check` is the final CPU-only code gate, not the default inner loop; documentation-only changes use `make check-docs`. Required CI, integration, browser and release evidence remain unchanged.

Self-review the complete diff and affected callers. Use the [risk-triggered review policy](docs/design/repository-harness.md#evidence-reuse-and-blockers) for independent review; changes to these instructions or repository policy do not exempt themselves. One responsible main session owns completion.

Keep every acceptance criterion through implementation. Once behavior and required evidence are complete, stop adding optional tests, refactors or cleanup. Report delivered behavior, actual checks and results, and any remaining review or validation gap; committed or pushed does not mean merge-ready. Merge and publication require the user's authorization.
