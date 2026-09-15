# Repository verification

Status: **normative**.

This document owns test selection, evidence reuse, review triggers, stable verification commands and CI gate selection. Product/hardware acceptance remains in [`solution-and-roadmap.md`](solution-and-roadmap.md) and the target-environment validation matrix.

## Choose evidence by risk

Before implementation, state risk, intended test reuse/additions and intended checks in at most three lines. Classify the changed behavior and failure paths, not the file extension.

- **Low risk** — documentation, copy, pure styling and cleanup with no behavior change normally add no automated tests. Run the applicable documentation/static gate. Normative instructions, security rules and gate policy are not low risk merely because they are Markdown.
- **Ordinary bounded functionality** — reuse existing tests first. Sufficient existing evidence permits zero additions; a real gap normally needs 1–3 independent scenarios, not a quota of test functions.
- **Confirmed defect or changed critical safety invariant** — use minimal red/green evidence at the owning public seam: demonstrate the threatened behavior, implement the minimum correction, then rerun it.

Every new scenario must answer: what specific failure does it prevent, and why would existing evidence miss it? Do not add tests for field counts, coverage cosmetics or private implementation details.

## Placement and evidence level

Tests belong to the owner/seam whose behavior they prove:

- **Unit** — deterministic module behavior under the owning app/package.
- **Integration** — one application plus a real adapter or local infrastructure. Replace dependencies at recorded seams rather than mocking through call chains.
- **Contract** — external/inter-process assumptions. `tests/contract/base/` owns NVIDIA assumptions and registered patch regressions.
- **System/browser** — user-visible flows across applications/published interfaces.
- **Performance/hardware** — explicit suites with environment identity, model/digest, data set, p50/p95/p99, queue depth and pass budget.

Critical risks require evidence at the level where the risk actually exists: judgment safety/violation latching, authentication/authorization, data isolation, transaction/migration behavior, report/queue/disposal idempotence, physical-execution rights and evidence protection. Synthetic tests do not replace target-hardware or field gates.

Fixtures are synthetic or explicitly sanitized, minimal, deterministic and provenance-documented. Customer video, credentials, model weights and production exports are never fixtures.

## Test authoring

- Assert coherent outputs/objects when practical rather than enumerating only expected fields and letting new wrong fields pass unread.
- Unit tests live in owning test trees; production files contain no test-only functions.
- Reuse existing helpers and fixtures before adding another.
- A test does not mutate process environment variables to configure the unit under test; pass configuration through the public/configuration seam.

## Evidence reuse and review

Evidence remains valid while the covered code, tests, configuration, dependencies and relevant external inputs remain unchanged. Record commands, results and relevant versions/inputs. Persist them in `.tmp/task-handoff.md` only when the workflow continuity rules require it.

Self-review every complete diff and affected caller set. Require one consolidated **independent read-only review** when a change affects a critical risk, public/cross-module interface, shared behavior, dependencies, CI, deployment, `vendor/`, repository policy or agent instructions, or when impact cannot be confidently bounded. Instruction changes do not exempt themselves.

The reviewer reports **Spec** and **Standards** together and consumes valid evidence rather than rerunning everything. The main session repairs findings. After a repair, review the increment plus affected callers and rerun affected checks. If required review or validation is unavailable, record the gap and keep merge readiness pending.

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

The aggregate pull-request status is `CI required` from `.github/workflows/blocking-ci.yml`; repository settings must separately require it.

| Changed paths | Complete blocking lane |
|---|---|
| Only root `AGENTS.md`, `CLAUDE.md`, `CONTEXT.md`, `README.md`, or Markdown under `docs/` | `make check-docs`; integration/browser lanes return explicit no-change success |
| Any other path | `make check`, plus applicable integration/browser filters |
| `.github/`, root `Makefile`, the selector itself, or unusable comparison baseline | Force all blocking families |

Path filtering is an optimization, not an exemption. Lockfile checks always run. Applicable code gates verify generated-artifact cleanliness. GPU, camera, connector, multi-stream, 72-hour and 7-day suites remain non-blocking/manual or scheduled target-environment evidence until release policy says otherwise.

The final candidate commit needs its own applicable green CI status; an earlier commit's green status does not transfer across content changes.
