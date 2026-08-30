# Repository instructions

This repository is a product monorepo for the SOP compliance system. Use the canonical terms in `CONTEXT.md`; read relevant decisions in `docs/adr/` when that directory exists.

## Before changing the repository

- For code placement, tests, dependencies, CI, deployment assets, or `vendor/`, read [`docs/design/repository-harness.md`](docs/design/repository-harness.md). It is the normative repository harness.
- For current product behavior, architecture, performance constraints, and safety invariants, read [`docs/design/solution-and-roadmap.md`](docs/design/solution-and-roadmap.md). It is the only current decision source.
- For issue and planning workflows, follow `docs/agents/issue-tracker.md`, `docs/agents/triage-labels.md`, and `docs/agents/domain.md` as applicable.

## Engineering constraints

- Put behavior in the owning module and expose it through that module's small interface. Callers and tests cross the same seam; do not reach into another module's implementation or tables.
- Keep the judgment core independent of camera SDKs, inference frameworks, and concrete connectors. Normalize those inputs through adapters before evaluation. It lives in `apps/edge-runtime/`, runs inside the inference host, and depends only on the Python standard library.
- The NVIDIA base code in `vendor/sop-monitoring-blueprints/` is this system's trunk, not an external dependency. Reuse what it already implements; modify it in place only within the scope recorded in `docs/adr/0007-base-is-the-trunk-not-a-dependency.md` (extended checker, pipeline stream-health events, disposal actions), keeping patch logic in `apps/edge-runtime/` and only a minimal hook inside `vendor/`. Update the subtree through a dedicated change, then run the base-code contract suite and record the verified commit. Two implementations of one capability are a defect.
- Align runtime integrations with the inference service's public HTTP/SSE contract before adding another transport, broker, or proxy. Performance-critical additions require measurements against the 500 ms end-to-end target, whose start point differs by result type (last source frame for arrival-time verdicts, the moment the closing condition holds for instance-closing verdicts); every over-budget successful result is an SLO violation, not an average to hide.
- The inference host is autonomous: judgment, violation latching, disposal (including output-point writes), and evidence buffering keep working while the center is unreachable. Never put the center on the real-time error-proofing path, and never make the inference host depend on the center to start a stream.
- Keep secrets, credentials, customer media, model weights, generated data, and production dumps out of Git. Tests use synthetic or explicitly sanitized fixtures.
- Add nested `AGENTS.md` files only when a subtree has real local exceptions. They refine these rules; they do not duplicate them.
- Delete superseded conclusions from the repository and issue tracker after merging any still-valid facts into the current decision source. Do not retain deprecated design documents or stale alternatives “for history”; Git already provides history.

## Completion gate

Run `make check`. During early scaffolding this checks the repository policy; as language workspaces land, extend the same target so local and blocking CI execute the same CPU-only checks. Run hardware or GPU suites only when the change or its acceptance criteria require them.
