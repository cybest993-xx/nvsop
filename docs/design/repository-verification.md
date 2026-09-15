# Repository verification

Status: **normative**.

This document owns test selection, evidence reuse, review triggers, stable verification commands and CI gate selection. Product/hardware acceptance remains in [`solution-and-roadmap.md`](solution-and-roadmap.md) and the target-environment validation matrix.

## Choose evidence by risk

Before implementation, state risk, intended test reuse/additions and intended checks in at most three lines. Classify the changed behavior and failure paths, not the file extension.

- **Low risk** — documentation, copy, pure styling and cleanup with no behavior change normally add no automated tests. Run the applicable documentation/static gate. Normative instructions, security rules and gate policy are not low risk merely because they are Markdown.
- **Ordinary bounded functionality** — reuse existing tests first. Sufficient existing evidence permits zero additions; a real gap normally needs 1–3 independent scenarios. Count independent scenarios rather than test functions; before exceeding that budget, state the specific additional risk each scenario covers.
- **Confirmed defect or changed critical safety invariant** — use minimal red/green evidence at the owning public seam: first demonstrate the observed defect or threatened invariant, implement the minimum correction, then rerun that evidence.

Every new scenario must answer: what specific failure does it prevent, and why would existing evidence miss it? Do not add tests for field counts, coverage cosmetics, private implementation details or hypothetical behavior outside the task's accepted scope.

## Placement and evidence level

Tests belong to the owner/seam whose behavior they prove:

- **Unit** — deterministic module behavior under the owning app/package test tree; Vue component tests may be colocated when the component is the sole owner.
- **Integration** — one application plus a real adapter or local infrastructure. Replace dependencies only at recorded seams rather than mocking through internal call chains.
- **Contract** — external/inter-process assumptions. `tests/contract/base/` owns both NVIDIA assumptions and registered patch/reimplementation regressions; both families are mandatory after every subtree update.
- **System/browser** — user-visible flows across applications/published interfaces.
- **Performance/hardware** — explicit suites with environment identity, model/digest, data set, p50/p95/p99, queue depth and pass budget.

Critical risks require evidence at the level where the risk actually exists: judgment safety/violation latching, authentication/authorization, data isolation, transaction/migration behavior, report/queue/disposal idempotence, physical-execution rights and evidence protection. The ordinary test budget cannot omit that evidence. Judgment safety is proven at the core public interface, including that invalid observation periods never become a false failure verdict and that the rework sequence `1,2,3,2,4,5` remains compliant.

Use unit/contract evidence for deterministic rules and fixed wire/base assumptions. Require integration evidence when the risk crosses an adapter or real local infrastructure, including database transactions/migrations, authentication/authorization integration, data isolation, queue/outbox behavior, disposal idempotence across restart, execution-right uniqueness/lease expiry, evidence protection, and supervisor persistence of judgment effects. Use the smallest real-infrastructure or adapter-backed scenario that proves the invariant; a fake may stand in only at the recorded seam. Testing a pure judgment/authorization rule does not waive its affected integration evidence. Use system/browser evidence when the risk is a user-visible cross-application/browser contract and preserve existing browser/UI coverage.

Synthetic tests do not replace target-hardware or field gates. For the current MVP only, full cross-module end-to-end, combined-fault, long-stability, scale-performance and field-hardware evidence may be deferred only when the original acceptance criteria remain explicitly tracked under [`solution-and-roadmap.md` §8](solution-and-roadmap.md#八开发路线).

Fixtures are synthetic or explicitly sanitized, minimal, deterministic and provenance-documented. Customer video, credentials, model weights and production exports are never fixtures.

## Test authoring

- Assert coherent outputs/objects when practical rather than enumerating only expected fields and letting new wrong fields pass unread.
- Unit tests live in owning test trees; production files contain no test-only functions.
- Reuse existing helpers and fixtures before adding another.
- A test does not mutate process environment variables to configure the unit under test; pass configuration through the public/configuration seam.

## Evidence reuse and review

Evidence remains valid while the covered code, tests, configuration, dependencies and relevant external inputs remain unchanged. Record commands, results and relevant versions/inputs. Persist them in `.tmp/task-handoff.md` only when the workflow continuity rules require it.

Self-review every complete diff and affected caller set. Low-risk and ordinary bounded changes need no independent reviewer by default. Require one consolidated **independent read-only review** when a change affects a critical risk, public/cross-module interface, shared behavior, dependencies, CI, deployment, `vendor/`, repository policy or agent instructions, or when impact cannot be confidently bounded. State the trigger and classify behavior rather than a file suffix; instruction changes do not exempt themselves.

The reviewer reports **Spec** and **Standards** together and consumes valid evidence rather than rerunning everything. The main session repairs findings. After a repair, review only the increment plus affected callers and rerun affected checks; do not add review rounds without a concrete risk or unresolved finding. If required review or validation is unavailable, record the gap and keep merge readiness pending.

A missing-test finding blocks only when it identifies a concrete failure path, uncovered critical risk or unmet acceptance criterion. Naming, size and optional cleanup advice block only when tied to a concrete defect or violated invariant.

## Stable commands

The root `Makefile` is the human/CI command interface.

| Command | Purpose |
|---|---|
| `make check-docs` | Documentation lane: frozen Python resolution, hooks, repository policy/link checks, secret scan and `git diff --check` |
| `make check` | Final CPU-only, Docker-free code gate |
| `make check-integration` | Center integration/system evidence with real local infrastructure |
| `make web-e2e` | Browser scenarios |
| `make change-size` | Advisory change/file size report from `BASE` (default `origin/main`) |
| `make contracts` | Regenerate/check OpenAPI and Web generated client; details in repository maintenance |

During iteration run the smallest affected Make targets with their prerequisites. `make check` is the final code gate, not the default inner loop. Documentation-only work uses `make check-docs`.

`make check` and `make check-docs` use the frozen `uv.lock`; Web checks use the frozen `pnpm-lock.yaml`. Package-specific targets may assume setup already ran, so inspect the Makefile instead of assuming every target provisions dependencies.

## CI gates

The sole aggregate pull-request status to require is `CI required` from `.github/workflows/blocking-ci.yml`; the workflow alone does not enable server-side branch protection, so repository settings must separately require it. Its `always()` gatherer requires explicit success from scope selection, lockfile checks and every gate family; failed, cancelled or skipped dependencies cannot become a successful aggregate.

`scripts/ci_scope.py BASE HEAD` selects the documentation fast lane from the actual compared commits with rename detection disabled. An unusable comparison baseline forces all gates rather than being treated as no-change.

| Changed paths | Complete blocking lane |
|---|---|
| Only root `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `README.md`, or Markdown under `docs/` | `make check-docs`; integration/browser lanes return explicit no-change success |
| Any other path | `make check`, plus applicable integration/browser filters |
| `.github/`, root `Makefile`, the selector itself, or unusable comparison baseline | Force all blocking families |

Path filtering is an optimization, not an exemption: inapplicable integration/browser jobs return explicit success, while selector/check failures remain failures. Lockfile checks always run. Applicable code gates verify generated-artifact cleanliness. CI actions are pinned to immutable commits, permissions remain least-privilege, installs are frozen, jobs have timeouts and superseded PR runs may be cancelled.

### Target-environment and release gates

GPU, camera, connector, multi-stream, 72-hour and 7-day suites remain non-blocking/manual or scheduled target-environment evidence until release policy says otherwise. They publish real evidence and never silently fall back to mocks when required hardware/services are unavailable.

Release promotion additionally requires the applicable SBOM/license review, image and dependency scanning, migration/rollback rehearsal, offline-start verification, and immutable image/model digests. A missing required release environment is a recorded blocker/gap rather than an implicit pass.

The final candidate commit needs its own applicable green CI status; an earlier commit's green status does not transfer across content changes.
