# Repository harness

Status: **normative**  
Reference baseline: [`openai/codex@f4f85add`](https://github.com/openai/codex/tree/f4f85add41288c2059dc1a4326a598f739e47fe9), inspected 2026-08-29 — repository shape and gates (§2–§4, §6–§8).  
Authoring recheck: [`openai/codex@c1f1467`](https://github.com/openai/codex/tree/c1f1467f3028bd433c8f2063ecc28dd5be206df6), inspected 2026-09-09 — file and change-size guidance (§5). Instruction loading follows the official guide linked in §9.

This document defines repository shape, authoring conventions and verification. It adopts the reference baseline's transferable patterns: one root task interface, package-owned tests, scoped agent instructions, change-aware CI, one aggregate required status, and coherent changes that can be reviewed together.

Current product and architecture decisions live only in [`solution-and-roadmap.md`](solution-and-roadmap.md). Research files contain evidence that is still used by that decision source; once evidence or recommendations are superseded, merge any surviving facts and delete the stale file. Git history is the archive.

## 1. Architecture rule

A product **module** owns behavior and data behind one small **interface**. Its callers and tests cross the same **seam**. Concrete infrastructure is an **adapter** at that seam.

- Prefer a deep module over transport-, model-, or table-shaped pass-through layers.
- Dependencies point toward domain behavior. HTTP handlers, jobs, persistence, cameras, and connectors adapt to modules; modules do not import them.
- A module owns its tables and migrations. Cross-module access uses the owner's interface, never direct table reads.
- Introduce a seam when behavior actually varies. A production adapter plus a test fake is not by itself evidence that the product needs a public plugin system.
- The judgment core consumes normalized observations and returns decisions. It must not know whether an observation came from a chunk, a camera, or a connector. It runs on the inference host, in that host's supervisor process rather than inside the base's process (see [ADR-0005](../adr/0005-judgment-runs-inside-the-inference-host.md)). It depends only on the Python standard library, kept for testability rather than forced by its runtime; the hook inside `vendor/` is bound by the same rule because the NVIDIA base image sets that interpreter.
- The inference host is an autonomous judgment unit: it keeps judging, latching violations, dispatching disposals, and buffering evidence while the center is unreachable. The center is the management and aggregation plane; it must never sit on the real-time error-proofing path.
- Reuse before rebuild. The NVIDIA base code is this system's trunk, not an external dependency. Where the base already implements a capability, reuse it; where it is close but insufficient, modify it in place within the recorded patch scope; build new only where the base has nothing or has coupled it too tightly to patch (see [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md)). Two implementations of one capability on the same path are a defect, not a boundary: where we reimplement, the base's version must no longer be called on that path.
- Optimize for end-to-end latency and bounded queues. A successful real-time result must render in the Web UI no more than 500 ms after its start point, which differs by result type: the last source frame for arrival-time verdicts, and the moment the closing condition holds for instance-closing verdicts. Report p50/p95/p99 per class, count every over-budget result, and require zero over-budget results in the target-hardware gate.

## 2. Canonical layout

Create directories lazily when the first real file needs them; empty scaffolding is not architecture.

```text
/
├── AGENTS.md                       # short, always-loaded agent entrypoint
├── CONTEXT.md                      # canonical domain language only
├── Makefile                        # stable local/CI task interface
├── apps/
│   ├── control-api/                # FastAPI center backend (management and
│   │   │                            # aggregation plane; not on the live path)
│   │   ├── src/factory_sop/        # one package per center module; the list is
│   │   │                            # `[tool.nvsop]` in the root pyproject.toml
│   │   ├── migrations/             # one linear history; file prefix names the owning module
│   │   └── tests/{unit,integration}/
│   ├── control-web/                # Vue 3 operator/admin application
│   │   ├── src/modules/            # feature slices, one per §5.4 navigation item
│   │   ├── src/{session,shell}/    # identity and layout: needed by every module,
│   │   │                            # owned by none (see below)
│   │   ├── src/{api,router}/       # the control-plane client and the route table
│   │   └── tests/{integration,e2e}/
│   └── edge-runtime/               # inference-host autonomous judgment unit
│       ├── src/edge_runtime/
│       │   ├── judgment/           # pure state-transition function; owns the reason codes
│       │   ├── stream_health.py    # the one module the vendor/ hook may import
│       │   ├── supervisor/         # drives the core, holds the timer, persists one
│       │   │                        # reaction per transaction
│       │   ├── local_state/        # SQLite authority: instances, decisions, latched
│       │   │                        # violations, disposal records, the two queues
│       │   ├── connectors/         # the connector seam, one runtime per configured
│       │   │                        # connector, and its adapters
│       │   └── runtime.py          # the composition root
│       └── tests/{unit,integration}/
├── packages/
│   └── contracts/                  # versioned wire schemas and generated clients
├── tests/
│   ├── contract/base/              # NVIDIA base-code assumptions and patch regressions
│   ├── system/                     # cross-application black-box scenarios
│   ├── performance/                # explicit benchmark entrypoints and budgets
│   └── fixtures/                   # synthetic/sanitized, immutable test inputs
├── deploy/                         # Compose, mediamtx, and offline deployment assets
├── vendor/
│   └── sop-monitoring-blueprints/  # git subtree; changed only by subtree pull or a
│                                    # recorded replayable patch (ADR-0007)
├── docs/{adr,agents,base,design,research}/
├── scripts/                        # thin repository automation, not product behavior
└── .github/workflows/              # CI orchestration only
```

Reserved production application names are `control-api`, `control-web`, and `edge-runtime`. `edge-runtime` holds the judgment core and everything else that must keep working while the center is unreachable; `vendor/` carries only a minimal hook that imports and calls it, which keeps our logic under lint, type checking, and unit tests while holding the patch surface small (see [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md)). Shared code enters `packages/` only after at least two real callers exist; otherwise it remains in its owning application. Root `src/` is forbidden because it erases ownership.

The center backend pins Python to 3.12 with a committed `.python-version`, one root `pyproject.toml` uv workspace, and a committed `uv.lock`; CI runs `uv sync --frozen`. `edge-runtime`'s judgment core is standard-library-only and therefore not bound to that pin. The rule is kept for testability: it kept the base's own checker modules runnable on a bare CPU, and it keeps ours the same. The hook inside `vendor/` is standard-library-only for a stricter reason — it executes inside the NVIDIA base container, whose interpreter version is set by the base image.

### Workspace manifests

The root `pyproject.toml` declares the Python workspace members and all formatting, lint, type and `import-linter` configuration; read that manifest for the current membership. `uv.lock` freezes dependencies and `.python-version` pins the interpreter. `apps/edge-runtime/` deliberately remains outside the workspace: the policy checker validates its standard-library imports and the approved shared-contract seam, so §1's isolation is checked rather than assumed from an environment.

The web workspace has landed as well: a root `package.json` pinning the package manager through `packageManager`, `pnpm-workspace.yaml` with `apps/control-web` as its only member, a committed `pnpm-lock.yaml`, and `.nvmrc` pinning the Node runtime the way `.python-version` pins the interpreter. CI installs with `pnpm install --frozen-lockfile`, which fails on a manifest whose lockfile was never regenerated rather than resolving afresh. `scripts/check_repo_policy.py` requires all four once `apps/control-web/` exists, and requires `packageManager` to name one exact version — corepack accepts a range, and with one CI resolves a different pnpm than a developer runs.

`src/modules/` holds one feature slice per navigation item, which [`control-plane.md`](mechanisms/control-plane.md) §5.4 fixes as 概览 / 工位与设备 / SOP 模板 / 训练数据集 / 用户与权限 — the front end is divided by navigation rather than by backend module, so "feature slices matching backend language" means the slice names come from the domain vocabulary, not that they mirror `factory_sop`'s packages one for one. The navigation list is a product boundary, not a completion claim: each slice is delivered by its scoped implementation ticket, while the reused annotation UI remains outside this Vue slice list.

`session/` and `shell/` sit outside `modules/` deliberately, and this is the one place the layout departs from "every directory under `src/` is a feature slice". Neither is a navigation item: `session/` is who the caller is, which every module needs and none owns, and `shell/` is the frame they all render inside. Making either a sixth slice would give it a peer's shape while every other slice imports it — the same reason `problem.py` and `observability/` sit beside the center backend's modules rather than among them. `api/` and `router/` are there on the same grounds one level down: one place parses `problem+json`, one place declares the routes.

One exception to §1's strictness posture is recorded where it is taken, in `apps/control-web/tsconfig.app.json`: `exactOptionalPropertyTypes` is off, because element-plus's prop descriptors declare `validator` as a required property whose type includes `undefined` while Vue's `ExtractPropTypes` matches against an optional one. Under that flag the match fails and every affected prop reports the descriptor object instead of the prop's type, on ordinary template usage across `ElSelect`, `ElMenuItem`, `ElDatePicker`, `ElTableColumn` and `ElConfigProvider`. The alternative was a cast at each such prop in every web slice.

## 3. Module ownership and seams

### Center modules

The machine-readable list of center modules is `[tool.nvsop]` in the root `pyproject.toml`. Until the gate scripts read that table, the migration-ownership check carries a copy of the list that must match it; this table and the roadmap's §六 explain it. Adding or removing a module starts there — a module named in prose but not in the declaration owns no tables and passes no gate.

| Module | Owns | Interface examples | Adapters live outside the behavior |
|---|---|---|---|
| `auth` | users, roles, permissions, sessions | authorize command; open/revoke session | FastAPI auth, PostgreSQL |
| `device` | inference hosts, inference backends, stations, cameras, connector configuration and measured capability declarations, station runtime-parameter overrides, delegated-command queue | resolve station topology; report health | Hikvision, board card, inference health |
| `execution` | which host may drive a station's output points: grant, handover handshake, two-person forced rebind, physical-execution lease | request/confirm handover; renew lease | center HTTP, inference-host pull |
| `template` | Excel validation, immutable versions, release/binding, desired-vs-reported reconciliation | import draft; release; resolve active version | workbook parser, object storage |
| `dataset` | training videos, action time-range annotation, usage checks, derived artifacts | register video; annotate ranges; run usage check; emit artifact | presigned object storage, reused annotator UI |
| `monitor` | reported mirror of SOP instances, decisions, latched violations, disposal records and stream health; dashboard state ([ADR-0010](../adr/0010-alert-merges-into-monitor.md)) | upsert reported decision; upsert reported violation; query dashboard | inference-host report endpoint |
| `evidence` | evidence references, re-clip requests and human review | request clip; attach review | edge runtime, MinIO |
| `retention` | retention policy, verdict-class resolution, change-impact estimate, reference protection — policy, never data | resolve class and duration; estimate a change | scheduled sweep |
| `job` | durable asynchronous application commands | enqueue/query application job | ARQ/Redis |

`execution` is separate from `device` because their change drivers differ: `device` models what equipment exists and its use cases are reversible configuration CRUD, while `execution` is a cross-host safety state machine with a TTL and carries the system's only two-person operation. It enforces one invariant that must hold inside the module rather than in its callers: at most one inference host holds an unexpired physical-execution right for a station at any moment.

`monitor` and `evidence` stay separate even though one report-intake adapter writes both, because `monitor` is written by machines and changes only with the report contract, while `evidence` carries human review — a different actor and a different lifetime (evidence outlives recording retention; review never rewrites the original decision). Archived violations and disposal records were once planned as a third module, `alert`; its stated change driver — the set of disposal actions — belongs to the inference host, so it changed with the report contract exactly as `monitor` does and has been folded in ([ADR-0010](../adr/0010-alert-merges-into-monitor.md)). The intake adapter composes the two inside one request-scoped Unit of Work and carries no judgment logic.

### Edge-runtime packages

The inference host's autonomous unit is one application with five packages and one composition root. Depth is the design rule: each package puts a lot of behavior behind a small interface, and a rule the package knows is enforced inside it, never handed to its caller as a contract to honor.

| Package | Owns | Interface | Adapters and seams |
|---|---|---|---|
| `judgment` | the pure state transition, the reason codes and their verdict classes, the event and decision vocabulary | `advance(state, event) → Outcome` | none — it is data in, data out |
| `stream_health` | both halves of the synthetic health-event wire shape | `note_pipeline_message` (producer, called by the `vendor/` hook), `decode` (consumer) | `HealthSink` (the base's queue) |
| `supervisor` | one station's drive: normalizing arrivals into core events, holding the deadline the core declared, and **persisting the state and every effect of one reaction as one transaction** — including concluding the interrupted pass on start-up | receive / wake / interrupt / timeout | the persistence seam below it: SQLite in production, in-memory in tests |
| `local_state` | the SQLite authority: instances, decisions, latched violations, disposal records (which are also the point-write ledger), the report and evidence queues, and their migrations | expressed only in `judgment` types: persist one reaction; resume; the two queue interfaces for the sender and the uploader | SQLite |
| `connectors` | the connector seam and one **connector runtime per configured connector**: polling cadence from its own capability declaration, rising-edge detection, the capability gate and idempotent point writes | per runtime: poll / write / next due; per seam: `Connector`, `IsapiTransport` | Hikvision ISAPI over `urllib`; the board card is the second adapter (P12) |

Dependency direction, enforced by `import-linter` contracts in the root `pyproject.toml`:

```text
supervisor  → local_state → judgment
connectors  → judgment, supervisor's input vocabulary
stream_health imports nothing from edge_runtime
run loop    → everything; the only place that knows every package
```

The storage package knows domain types, never the thing that orchestrates it. The reverse direction was tried (`local_state` importing `supervisor`) and made "one reaction, one transaction" a rule the caller had to remember instead of one the module held; that is why `supervisor` owns the transaction and `local_state` only offers it.

### Mechanical enforcement

An inference backend is one process endpoint carrying one template configuration, not one machine; a host runs as many backends as it has distinct templates, and `device_inference_host` models the machine separately. The judgment core is not a center module: it lives in `edge-runtime`, owns no tables, and stays a pure function. `monitor` holds the center-side mirror written by idempotent upserts from each inference host, so its rows are reports rather than the authority — the authority for decisions, latched violations, and disposal execution is the inference host's local SQLite.

`dataset` is separate from `template` on purpose: the glossary defines a training data set as something that does **not** define an SOP template, and lists SOP template as an avoided synonym. Merging them in code would re-fuse two concepts the language deliberately separates.

Module boundaries are enforced mechanically, not by review. Each center module is a Python package with a `usecases/` layer where authorization is enforced, and an `api.py` that exposes only the subset other modules actually call — `api.py` is the cross-module contract, not the module's full outward entry set, so a configuration module's CRUD use cases stay in `usecases/` rather than becoming pass-through re-exports. Authorization is enforced in `usecases/`, never only at the HTTP route, because jobs, report intake, and smoke scripts call the same use cases. `api.py` is the only permitted cross-module import target; `import-linter` contracts fail the gate when the judgment core imports an adapter, when a module imports past another's `api`, when a domain layer imports `adapters/*`, or when the hook inside `vendor/` imports anything other than the one designated stream-health entrypoint module in `edge-runtime`. That last contract is what keeps the patch surface at one append-only call: code under `vendor/` is outside this repository's lint and type coverage, so without a mechanical contract the hook can silently grow dependencies on arbitrary `edge-runtime` internals. Physical table names carry their owning module's prefix (`device_camera`, `template_version`, `auth_session`) so migration ownership is statically decidable. Cross-module use cases share one request-scoped Unit of Work opened and committed by the HTTP adapter layer; module facades participate but never commit. The HTTP adapter layer may compose several modules' `summary()` use cases into one aggregate response (the overview page); that composition carries no judgment logic.

`packages/contracts` contains wire contracts used across processes (the inference-host report and pull contracts, the exported `openapi.json`), their compatibility fixtures, and **pure functions over that contract data that both processes must evaluate identically** — the binding-time fitness rule over a capability declaration (`unfit_for`) is the first such function, and it moves there when the center becomes its second real caller (C4). Such a function is standard-library-only, reads no configuration, and performs no I/O, because the inference host imports it from outside the uv workspace and `scripts/check_repo_policy.py` resolves the import. Anything with a table, a session, or a clock is domain logic and stays in its owning application; `packages/contracts` must not become a shared domain-logic bucket.

### External traffic boundary

- Nginx serves the Web application, routes center-backend HTTP traffic, and reverse-proxies the reused annotation UI and training microservices with authorization added at the gateway. It does not relay video.
- REST/JSON + OpenAPI is the control-plane contract under the fixed literal prefix `/api/v1`, which is not a version axis; see [ADR-0003](../adr/0003-api-v1-is-a-fixed-prefix.md). Each inference host reports to the center and pulls its own configuration; the center exposes normalized live updates as SSE for the dashboard only. A pull response is scoped to the requesting host — it carries only that host's own stations, cameras, backends, connectors, points, resolved runtime parameters, and execution-right state, never another host's topology. Reason-code enumerations grow by addition, and every client must render an unknown reason code as the raw code plus a generic hint rather than treating it as a failure.
- Browsers connect directly to the MediaMTX instance on the assigned inference host for WebRTC signaling and media. MediaMTX is protected by the trusted factory network boundary; no per-viewer JWT/JWKS service is part of the first deployment.
- Training videos are uploaded one file at a time through presigned URLs directly to MinIO; archives are never uploaded and the backend never unpacks one. The control plane only issues the presigned URL, registers the video, and verifies it — the low-volume gateway rule stands.
- Device credentials live only on the inference host that uses them, encrypted with a key from a read-only deployment secret file (see [ADR-0008](../adr/0008-credentials-stay-on-the-inference-host.md)). The center stores a configured/not-configured flag, never ciphertext, and no center-generated artifact or download contains a secret.

## 4. Test placement and evidence

Tests belong to the module or seam whose behavior they prove:

- **Unit**: deterministic module behavior, under the owning app's `tests/unit/`; Vue component tests may be colocated as `*.spec.ts` when the component is the sole owner.
- **Integration**: one app plus real local infrastructure or one adapter, under that app's `tests/integration/`. Replace the adapter at the seam; do not mock through internal call chains.
- **Contract**: an external or inter-process interface. `tests/contract/base/` holds two families, both mandatory after every subtree update: assertions that base behavior we depend on but do not change is still true, and assertions that the recorded patch still applies and that our own reimplementation behaves correctly.
- **System**: user-visible flows across applications, black-box through published interfaces.
- **Performance/hardware**: explicit suites, never hidden in unit tests. Record hardware, model/digest, data set, p50/p95/p99, queue depth, and pass budget.

Every bug fix starts with the narrowest regression test that fails for the observed behavior. New behavior starts with the smallest failing test before implementation, then gains the least evidence that makes its risk visible at the owning seam. Safety invariants require tests at the judgment core's interface, especially that invalid observation periods never become a false failure verdict. The rework sequence `1,2,3,2,4,5` must be judged compliant: the base heuristic reports it as two separate violations, so this is the first regression test the judgment core has to pass and the sharpest line between our behavior and the base's.

The evidence level is risk-driven rather than a blanket test-type rule:

- **Unit or contract evidence** is sufficient for deterministic pure behavior and fixed wire/base assumptions.
- **Integration evidence** is required where the risk crosses an adapter or real local infrastructure: database transactions and migrations, authentication and authorization integration, queue/outbox behavior, disposal idempotence across restart, execution-right enforcement, and the supervisor's persistence of judgment effects. Use the smallest real-infrastructure or adapter-backed scenario that proves the invariant; a test fake may stand in only at the recorded seam. Pure judgment and authorization rules still use unit/contract evidence at their public interfaces; testing the pure rule does not waive testing its integration.
- **System or browser evidence** is required when a user-visible cross-application or browser contract is the risk. Keep existing browser and UI coverage; add focused component, integration, snapshot, or end-to-end coverage when the changed behavior needs it, but do not require a snapshot for every UI change.

This risk rule does not reduce the command or CI gates in §6–§7, waive existing tests, or make synthetic tests a substitute for target-hardware and field validation. For the current MVP only, full cross-module end-to-end, combined-fault, long-stability, scale-performance, and field-hardware evidence may be a later validation stage when tracked against the original acceptance criteria. Scope, exit conditions, and the need to reconfirm this phasing for later iterations are defined in [`solution-and-roadmap.md` §8](solution-and-roadmap.md#八开发路线).

Fixtures must be synthetic or sanitized, minimal, deterministic, and documented with provenance. Customer video, credentials, model weights, and production exports are never fixtures.

### Test authoring

- Assert on whole objects rather than field by field, so a field that changes unexpectedly fails the test instead of passing unread.
- Statically defined values get no test, and deleted logic leaves no negative test behind. Both pin the implementation in place of the behavior.
- Unit tests live in the owning app's `tests/` tree, never inline in the implementation file, and implementation code carries no test-only function.
- Look for an existing helper or fixture before writing another one.
- A test never mutates process environment variables; the value under test arrives through a parameter.
- A user-visible UI change gets the necessary focused UI evidence for its risk; existing snapshot and browser tests remain mandatory and are not removed.

## 5. Code authoring rules

Technology-neutral by intent: the reference baseline's crate layout, named clippy lints, ratatui styling, and ASCII-only default are its own and are not adopted here.

### Size and decomposition guidance

The official Codex [file guidance](https://github.com/openai/codex/blob/c1f1467f3028bd433c8f2063ecc28dd5be206df6/AGENTS.md#L49-L59) targets Rust modules below 500 lines excluding tests; beyond roughly 800 lines in a **file**, it prefers a new module for new functionality unless there is a strong documented reason. Its separate [change guidance](https://github.com/openai/codex/blob/c1f1467f3028bd433c8f2063ecc28dd5be206df6/AGENTS.md#L125-L131) concerns the **whole change**, with mechanical changes excepted and smaller stages to be explored. Neither sets a lifetime cap on a product module.

Here, size is advisory. It never fails a gate by itself and needs no exception allowlist or separate approval. Correctness, safety, ownership, compatibility and the task's acceptance criteria remain mandatory.

| Object | Measure | Action |
|---|---|---|
| Authored production file | Physical lines in a changed Python, TypeScript or Vue source file, including comments, docstrings and blank lines; all sections of a Vue file count together. Tests and generated files are excluded. | Below 500 is a readability target. At roughly 800, assess whether an independent responsibility belongs in a focused private file. A small fix in an existing large file does not require a surrounding refactor. |
| Product module | Behavior, state ownership, change driver and interface from §1 and §3 | No cumulative line cap. Private files and adapters remain part of their existing owner. |
| Pull request | Added **plus deleted** implementation lines across the complete diff; display additions and deletions separately | Around 800 prompts scope review. Complex logic deserves earlier review; the reporter flags 500 changed judgment lines. Module breakdowns locate work, not separate allowances. |

`make change-size` reports the task from its merge-base with `BASE` (default `origin/main`), including committed, staged, unstaged and untracked local files. Supplying `HEAD` selects that immutable commit and excludes worktree edits, as CI does. The report separates tests, generated output, migrations, `vendor/`, other files and binary files from authored production implementation and repository scripts. Pure renames recognized by Git do not count their moved contents as new logic. A Git or read failure still fails the command.

Decompose when it reduces mixed responsibilities or coupling. Keep the public interface and table owner, leave orchestration at the existing entry point, and move related tests and explanations beside the extracted behavior. If cohesion is safer, record that reason in the normal review handoff and continue. For mechanical edits or pure deletions, describe their nature rather than staging them solely to reduce the count.

A separately landed stage must have observable behavior, its own evidence, and a valid intermediate state: no duplicate production path, incompatible migration or caller waiting for a later repair. Preserve every acceptance criterion across stages. Splitting commits alone does not split a pull request.

Create a new product module only when all five hold: a coherent behavior or invariant; a distinct change driver; one small interface for callers and tests with no dependency cycle; one state owner (or pure behavior without state); and one implementation on the original path. Users versus roles, CRUD verbs, commands versus queries, transport layers, tables, file types, screen sections, utility buckets, or an adapter plus its fake do not by themselves establish that boundary.

Keep useful explanations and normal formatting. Moving behavior into `api.py`, `packages/`, `scripts/` or adapters solely to alter counts, or adding pass-through interfaces for that purpose, violates ownership. Shared contracts and public interfaces grow only for real callers under §3.

### Comments and documentation language

- Write code comments and docstrings in concise Chinese; explain only non-obvious reasons, constraints, safety or compatibility boundaries, and keep them current.
- Use Chinese for human-facing product and domain docs, English for agent-facing instructions, skills and operating guidance. Keep identifiers, API names, protocol fields and established terms unchanged.

### Interface shape

- A parameter takes neither a bare boolean nor an ambiguous optional, because both force the call site to read `f(False)`. Use an enum, a keyword-only argument, or two named functions so the call site states its own meaning.
- Where a signature cannot change and a literal must be passed, name it at the call site with a comment carrying the callee's parameter name exactly.
- Branch on an enumeration exhaustively; a catch-all arm swallows the next value added. One exception, and it is mandatory rather than permitted: at a cross-process wire boundary a forward-compatible fallback is required, because the inference host and the center upgrade independently — an unknown reason code renders as the raw code plus a generic hint (see [ADR-0003](../adr/0003-api-v1-is-a-fixed-prefix.md)).
- Every entry added to a module's `api.py` carries a docstring giving its role and the caller's expected use. `api.py` is the cross-module contract, so an undocumented entry there is an unbounded promise.
- Extract a private helper when it names a complete responsibility or makes its caller easier to understand, even with one caller; keep trivial pass-through helpers inline.

### What a change carries with it

- A breaking change searches a fixed surface before merging: the `/api/v1` OpenAPI contract, the inference-host report and pull contracts, migration and table ownership, resolved runtime parameters, and in-flight instance behavior across a restart. State which of those you checked.
- A dependency change and its lockfile update land in one commit.
- Generated output is regenerated in the same change as its source, and CI verifies a clean worktree.

## 6. Stable command interface

`make check` is the CPU-only, infrastructure-free merge gate and must work from the repository root, without Docker. CI calls it exactly as developers do. It runs repository policy, migration table-ownership, the base-code contract suite, the `import-linter` contracts, content-based secret scanning (`detect-secrets`, with an empty baseline and inline allowlisting so a false positive is explained where it sits), and each workspace's formatting, lint, type, and unit checks; each workspace-adding change must extend it in the same change with that workspace's build checks and generated-artifact cleanliness.

Its first two targets are `lockfile` (`uv lock --check`, so a manifest edit whose lockfile was never regenerated fails rather than installing the old resolution) and `sync` (`uv sync --frozen --all-packages`). Every gate tool is resolved from `uv.lock` rather than installed separately, so a developer, the edge targets, and CI all execute the same build of ruff and mypy, and no run can silently upgrade a dependency. `apps/edge-runtime/` is not a workspace member and its tests run on a bare interpreter, but its tools come from that same environment. The web targets follow the same shape: `web-install` is `pnpm install --frozen-lockfile`, and the checks after it run from that installed tree. `web-build` is a gate rather than a packaging step — `vite build` resolves every dynamic `import()` the router declares, so a route that only breaks when built breaks in the gate instead of at deployment.

`make check-integration` is the second required target: one application plus real local infrastructure (PostgreSQL, Redis, MinIO) started as containers via testcontainers. It is separate because a developer without Docker must still be able to run `make check`, and because container startup does not belong in the fast feedback loop. Both targets feed the blocking gatherer, so merge protection strength is unchanged. SQLite and in-memory fakes are not substitutes for the integration target: transaction isolation, `JSONB`, timezone, and deferred foreign key behavior differ enough to produce false green.

Package-specific commands may exist for a tight feedback loop, but they do not replace these two targets. Automation in `scripts/` stays thin: product behavior belongs in an app or package where it can be tested through its interface.

Use the Make targets for gate evidence: they carry CI's flags, environment and ordering. For a narrow red/green loop, the underlying command may select a test when it preserves the target's interpreter, paths and flags; that run does not replace the required target. Run the formatter on changed code without asking first. Let container startup, model loading and integration bring-up finish before judging their result.

### Working and review cycle

1. **Scope.** Record the ticket or direct user request, acceptance criteria, affected entry point/callers and necessary evidence under §4. One ticket or direct task, one branch, one worktree outside the repository, never `main`. Start from `origin/main`; use a fresh branch for the next task and delete the old branch/worktree once merged. Existing approved scope and test seams remain approved; ask only about missing decisions that affect behavior or safety.
2. **Implement.** Invoke `tdd` for behavior changes, including repository checks. Use the agreed public seam: one failing test, the minimum implementation, then the next behavior. Run the narrow affected tests during this loop. Pure wording changes use document checks; they do not enter TDD. Finish the full acceptance scope before requesting the normal review, rather than reviewing each test or private extraction separately.
3. **Verify.** One owner runs `make check` for the completed batch, plus the applicable integration, browser, contract or hardware gates from §4 and §7. `make -k check` can collect independent failures in one run without changing success criteria. Capture commands, results, environment and the tested commit plus any uncommitted diff/untracked files in one handoff, alongside the spec and fixed review base. Missing infrastructure is an explicit validation gap, never a pass. The reviewer consumes this evidence rather than launching a duplicate full run.
4. **Review together.** A fresh session reviews the same complete diff and affected callers against both **Standards** and **Spec**. With `code-review`, its two review subagents run in parallel and return one response retaining both axes. Collect all findings before the author starts the repair batch. Blocking findings identify an unmet requirement, violated invariant or concrete defect; size, optional cleanup and subjective smells alone are advisory. Tool-enforced checks use the existing results.
5. **Repair and recheck.** Fix the collected defects together, then rerun the affected checks and review the fixes plus their affected callers. Evidence is reusable only while the covered code, tests, configuration, dependencies and relevant external inputs remain unchanged. Repeat a broader check or review only for invalidated evidence, expanded scope or a new concrete concern; explain the trigger. Unchanged areas do not start another full cycle. All blocking findings and required validation still need resolution.
6. **Hand off and commit.** A session that writes code neither reviews nor commits that change. One fresh session may coordinate the consolidated review, verify its resolution and commit; subagents are for review only. Record acceptance coverage, review resolution, valid check results and any tracked deferred validation before calling implementation ready. Closing a ticket still follows the roadmap's exit conditions and the issue-tracker evidence rules.

Four of this document's rules also run as Git hooks, versioned in `scripts/githooks/` and enabled by `make hooks` (which `make check` runs, so a fresh clone has them after its first gate run): a commit is refused on `main`, a push to `main` is refused from any branch, a staged change under `vendor/` is refused with a pointer to ADR-0007, and unformatted staged Python is refused. They sit in Git rather than in any one agent's configuration because every agent and every person commits through Git, so one implementation holds for all of them. An instruction file is advisory; a hook holds on the turn where the instruction has already scrolled out of context. Each hook is a few standard-library lines that cite the rule it enforces, which is what keeps the two in agreement.

## 7. CI gates

### Blocking pull-request gate

The sole branch-protection status is `CI required` from `.github/workflows/blocking-ci.yml`. The gatherer runs with `always()` and fails if any required dependency fails or is cancelled. This prevents a skipped downstream job from appearing green.

As workspaces appear, split checks into reusable workflows while retaining the gatherer:

1. repository policy and lockfile cleanliness — always, for both `uv.lock` and `pnpm-lock.yaml`;
2. backend/edge/web format, lint, type, unit, boundary, secret-scanning, base-code contract checks and the web production build (`make check`) — on relevant paths; plus the advisory size report (`make change-size`) on pull requests;
3. backend integration checks against real containerized infrastructure (`make check-integration`) — on relevant paths;
4. migration and cross-process contract compatibility — when schemas or contracts change;
5. workflow changes — run every blocking family.

The web checks are inside family 2 rather than a family of their own. §6 admits exactly one target besides `make check`, and `check-integration` earns it by needing Docker — a developer without it must still be able to run the gate. Node and pnpm are a frozen toolchain like uv's, not infrastructure to bring up, so that reason does not reach the web; splitting it out would leave the local target and the blocking gate running different checks, which is the thing §6 exists to prevent.

Path filtering is an optimization, not an exemption: every reusable workflow must return an explicit success when no relevant files changed. Actions are pinned to immutable commit SHAs, permissions are least-privilege, dependency installs are frozen, jobs have timeouts, and cancellation is enabled for superseded PR runs.

### Non-blocking and release gates

GPU, camera, connector, multi-stream, 72-hour, and 7-day suites run on labeled self-hosted runners by manual dispatch or schedule. They publish evidence and never silently fall back to mocks. Release promotion additionally requires SBOM/license review, image and dependency scanning, migration/rollback rehearsal, offline-start verification, and immutable image/model digests.

## 8. Inherited base code and generated artifacts

`vendor/sop-monitoring-blueprints/` is the NVIDIA base code, this system's trunk. It changes only through a dedicated `git subtree pull` or a recorded replayable patch within the scope fixed by [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md): one patch, which only adds output — the pipeline message callback that surfaces stream-health events as a synthetic chunk. Sequence comparison and boundary solving are reimplemented in `apps/edge-runtime/` rather than patched, because the base interleaves boundary detection with sequence comparison inside one method; the base checker and the base disposal are switched off through their existing environment variables rather than replaced. `vendor/` therefore carries no patch for either. Everything else stays as delivered; capabilities the base already provides are reused rather than rebuilt. Patch logic lives in `apps/edge-runtime/` with only a minimal hook inside `vendor/`, so the patch surface stays small and our code remains testable. The update change records the NVIDIA commit in [`docs/base/verified-commits.md`](../base/verified-commits.md) and runs all tests in `tests/contract/base/`.

Generated clients, schemas, and deployment output must have one documented source command. For the
OpenAPI chain in this repository that command is `make contracts`: it exports
`packages/contracts/openapi.json`, checks compatibility against the supplied base ref, and invokes
the Web workspace's generator. The `openapi-*` Make targets and `generate:api` package script are
implementation steps of that command, not alternate documented workflows. CI regenerates and
checks the complete worktree, including untracked output. Commit generated output only when
consumers cannot generate it during install or build.

## 9. Agent instruction hierarchy

Keep `AGENTS.md` as the always-present entry point: the invariants needed before reading files and one conditional pointer per task branch. Name the triggering task and the relevant section. Keep the steps and their completion conditions together; move branch-specific reference behind a pointer. Add a nested `AGENTS.md` only for a real local exception. `CLAUDE.md` remains a pointer to the same root instructions.

Each rule has one authoritative home. Point to tool configuration, commands, contracts and ADRs instead of copying their current values or directory inventories. Keep instruction text in English under §5. When guidance is superseded, merge surviving facts into the current source and delete the stale guidance; Git is the archive. Use `writing-for-agents` when updating these instructions.

The official [AGENTS.md guide](https://developers.openai.com/codex/guides/agents-md/) defines a default **32 KiB combined project-instruction byte budget**, configured by `project_doc_max_bytes`. That is a loading limit, not a source-file line limit or a per-document word target. Codex discovers the instruction chain at session start; start a fresh session to verify instruction changes. Keep important guidance reachable within the budget; do not turn a suggested document length into another hard size gate.

## 10. Reference-baseline patterns used

- One always-loaded root [`AGENTS.md`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/AGENTS.md) and no nested instruction file: the baseline organizes by subsystem *inside* that one file and pushes bulky reference (`codex-rs/tui/styles.md`) behind a pointer. Verified against the repository tree, which holds exactly one `AGENTS.md`.
- Authoring conventions stated as one-line imperatives with the reason attached only where the reason changes the behavior.
- One task interface shared by humans and automation: [`justfile`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/justfile).
- Package-owned integration suites: [`codex-rs/core/tests`](https://github.com/openai/codex/tree/f4f85add41288c2059dc1a4326a598f739e47fe9/codex-rs/core/tests).
- Reusable checks aggregated into one required result: [`blocking-ci.yml`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/.github/workflows/blocking-ci.yml).
- Change detection with a required gatherer and explicit no-change success: [`rust-ci.yml`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/.github/workflows/rust-ci.yml).
- Repository-specific invariant checks and clean-worktree verification: [`repo-checks.yml`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/.github/workflows/repo-checks.yml).
