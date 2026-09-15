# Repository architecture

Status: **normative**.

This document owns repository shape, module ownership, dependency direction and external traffic boundaries. Product behavior and delivery decisions remain authoritative in [`solution-and-roadmap.md`](solution-and-roadmap.md); machine-readable membership remains authoritative in manifests such as root `pyproject.toml`.

## Architecture rule

A product **module** owns behavior and data behind one small **interface**. Callers and tests cross the same **seam**. Concrete infrastructure is an adapter at that seam.

- Prefer a deep module over transport-, model- or table-shaped pass-through layers.
- Dependencies point toward domain behavior. HTTP handlers, jobs, persistence, cameras and connectors adapt to modules; modules do not import them.
- A module owns its tables and migrations. Cross-module access uses the owner's interface, never direct table reads.
- Introduce a seam when behavior actually varies. A production adapter plus a test fake does not establish a public plugin system.
- The judgment core consumes normalized observations and returns decisions. It is a pure, standard-library-only function and knows nothing of camera SDKs, inference frameworks or connectors.
- The inference host is autonomous: judgment, violation latching, disposal and evidence buffering continue while the center is unreachable. The center is the management and aggregation plane, not the real-time error-proofing path.
- NVIDIA base code under `vendor/sop-monitoring-blueprints/` is this system's trunk, not an ordinary external dependency. Reuse it when it already owns the capability; keep NVSOP-owned judgment behavior in `apps/edge-runtime/` behind the recorded minimal hook. Two active implementations of one capability on one path are a defect.
- Real-time successful results keep the latency budget defined by the product design; hardware claims require the target-environment gates rather than inference from code.

## Canonical layout

Create directories when the first real file needs them; empty scaffolding is not architecture.

```text
/
├── AGENTS.md                       # short, always-loaded agent entrypoint
├── CONTEXT.md                      # canonical domain language
├── Makefile                        # stable local/CI task interface
├── apps/
│   ├── control-api/                # center management/aggregation plane
│   ├── control-web/                # Vue operator/admin application
│   └── edge-runtime/               # inference-host autonomous runtime
├── packages/
│   └── contracts/                  # versioned wire schemas/shared contract logic
├── tests/
│   ├── contract/base/              # NVIDIA assumptions and patch regressions
│   ├── system/                     # cross-application black-box scenarios
│   ├── performance/                # explicit budgets and benchmarks
│   └── fixtures/                   # synthetic/sanitized immutable inputs
├── deploy/                         # Compose, MediaMTX and deployment assets
├── vendor/
│   └── sop-monitoring-blueprints/  # NVIDIA subtree plus recorded patches
├── docs/
│   ├── adr/                        # architecture decisions
│   ├── agents/                     # agent workflows and issue operations
│   ├── base/                       # NVIDIA verification ledger and patches
│   ├── deployment/                 # install, configuration, operations, upgrades
│   ├── design/                     # current architecture and mechanisms
│   └── research/                   # evidence and target-environment validation
├── scripts/                        # thin repository automation
└── .github/workflows/              # CI orchestration
```

Reserved production application names are `control-api`, `control-web` and `edge-runtime`. Shared code moves into `packages/` only after at least two real callers exist. Root `src/` is forbidden because it erases ownership.

## Workspace manifests

The root `pyproject.toml` owns the Python workspace, tool configuration and the machine-readable center-module declaration in `[tool.nvsop]`. `.python-version` and `uv.lock` own the interpreter pin and frozen resolution. `apps/edge-runtime/` deliberately remains outside the uv workspace; repository policy checks its standard-library and shared-contract boundaries.

The Web workspace is rooted at `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml` and `.nvmrc`. The root manifest pins one exact package-manager version. Do not copy those versions into policy rules; installation documentation may display the current values for operator convenience but the files remain authoritative.

`apps/control-web/src/modules/` follows product navigation, not backend package names. Cross-cutting browser identity/layout/client routing remains outside feature slices where it is genuinely shared.

## Center ownership

The authoritative list of center modules is root `pyproject.toml` `[tool.nvsop]`; add or remove a module there first. The current ownership model is:

| Module | Owns |
|---|---|
| `auth` | users, roles, permissions, sessions |
| `device` | inference hosts/backends, stations, cameras, connector configuration, runtime-parameter overrides, delegated commands |
| `execution` | station physical-execution grants, handover and lease safety state |
| `template` | import validation, drafts, immutable versions, release/binding and desired/reported reconciliation |
| `dataset` | training videos, annotation state, usage checks and derived artifacts |
| `monitor` | center-side reported mirror of instances, decisions, violations, disposal and stream health |
| `evidence` | evidence references, re-clip requests and human review |
| `retention` | retention policy, classification, impact estimation and reference protection |
| `job` | durable asynchronous application commands |

`api.py` is the cross-module contract, not a module's complete public entry set. Authorization belongs at use-case boundaries. A request-scoped unit of work may compose modules, but composition does not transfer table ownership.

## Edge-runtime ownership

The inference-host runtime remains one application with focused packages:

| Package | Owns |
|---|---|
| `judgment` | pure state transition, reason codes and verdict vocabulary |
| `stream_health` | the vendor-hook health-event wire shape |
| `supervisor` | station orchestration, timers and one-reaction transaction boundary |
| `local_state` | SQLite authority and report/evidence queues |
| `connectors` | connector seam and local adapters |
| `runtime.py` | composition root |

Dependency direction is mechanically enforced where possible:

```text
supervisor  -> local_state -> judgment
connectors  -> judgment and supervisor input vocabulary
stream_health imports no edge_runtime sibling
composition root -> all runtime packages
```

The storage package may know domain types; it must not know the orchestrator. The composition root is the only place that should need every package.

## External traffic boundary

- Nginx serves the Web application and center control-plane HTTP traffic. The authorized annotation-derived-media exception is recorded in [ADR-0011](../adr/0011-annotation-derived-media-gateway.md); it does not make Nginx a general video relay.
- Control-plane REST/JSON uses the fixed literal prefix `/api/v1`. It is not a version axis; compatibility rules live in [ADR-0003](../adr/0003-api-v1-is-a-fixed-prefix.md).
- Browsers connect directly to the assigned inference host's MediaMTX for runtime preview/media signaling.
- Training videos are uploaded one file at a time through presigned MinIO URLs. Archives are not accepted and the center does not unpack them.
- Device credentials stay on the inference host. The center records configuration state, not reusable secret material; see [ADR-0008](../adr/0008-credentials-stay-on-the-inference-host.md).

## Mechanical enforcement

Architecture boundaries that can be checked should be checked: import-linter contracts, migration ownership, contract tests, repository policy and generated-contract compatibility are preferable to review-only conventions. Explanatory prose must not become a second machine-readable registry.
