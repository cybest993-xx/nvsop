# Repository architecture

Status: **normative**. Read for repository shape, module ownership, dependency direction and traffic boundaries. Product behavior and accepted delivery scope belong to [the roadmap](../design/solution-and-roadmap.md); workspace/module membership belongs to [pyproject.toml](../../pyproject.toml), not a copied file inventory.

## Ownership

A module owns coherent behavior and its tables/migrations behind one small interface. Callers and tests cross that same seam; concrete infrastructure is an adapter there. Dependencies point toward domain behavior, never from domain to HTTP, persistence, job, camera or connector adapters. Cross-module access calls the owner's `api.py`, not its tables. Introduce a seam for actual variation, not a generic plugin system inferred from one adapter and a fake.

The judgment core is a pure, standard-library-only transition over normalized observations; it knows no camera SDK, inference framework or connector. The inference host is autonomous: judgment, latching, disposal and evidence buffering do not wait for the center. Preserve the product's real-time latency and safety invariants; target-hardware evidence is required before claiming achieved performance.

The NVIDIA subtree is the product's trunk, not an ordinary replaceable dependency. Reuse its existing capability; NVSOP judgment remains in `apps/edge-runtime/` behind the registered minimal hook. Two active implementations of a capability on the same path are a defect. [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md) and [maintenance.md](maintenance.md) bound edits.

## Layout and manifests

Create directories with their first real content; empty scaffolding is not architecture.

| Location | Responsibility |
|---|---|
| `apps/control-api/` | Center management/aggregation, owning module tables and migrations |
| `apps/control-web/` | Vue product navigation, not a mirror of backend packages |
| `apps/edge-runtime/` | Inference-host autonomy and pure judgment |
| `packages/contracts/` | Shared wire contracts; shared code moves to packages only with at least two real callers |
| `tests/contract/`, `tests/system/`, `tests/performance/`, `tests/fixtures/` | Cross-app assumptions, black-box flows, explicit budgets, synthetic/sanitized inputs |
| `deploy/`, `vendor/`, `scripts/`, `.github/workflows/` | Deployment assets, NVIDIA subtree, thin repository automation, CI orchestration |
| `docs/` | Indexed engineering, product design, deployment, ADR and evidence; ownership in [documentation.md](documentation.md) |

Reserved production apps are `control-api`, `control-web`, `edge-runtime`; root `src/` is forbidden because it erases ownership. `pyproject.toml` owns Python tooling, workspace and `[tool.nvsop]` module membership; `.python-version` and `uv.lock` own interpreter/resolution. `edge-runtime` stays outside the uv workspace, with standard-library/shared-contract boundaries enforced by policy.

The Web workspace uses `package.json`, `pnpm-workspace.yaml`, `pnpm-lock.yaml` and `.nvmrc`; the root manifest owns the exact package-manager pin. Do not repeat version values in policy. Shared identity, layout and client routing stay outside Web feature slices only where genuinely shared.

## Center and edge boundaries

Product responsibilities are defined once in [roadmap §6–7](../design/solution-and-roadmap.md#六系统结构). The module list comes from `[tool.nvsop]`; update it deliberately when changing membership. `api.py` exposes only the cross-module subset, not all HTTP use cases. Authorization belongs at use-case boundaries. Request-scoped Unit of Work can compose modules but does not transfer their table ownership ([ADR-0002](../adr/0002-request-scoped-unit-of-work.md)).

Within the edge application, `judgment` owns pure transitions/reason codes, `stream_health` the vendor-hook wire shape, `supervisor` orchestration/timers/one-reaction transactions, `local_state` SQLite and queues, and `connectors` local adapter behavior. `runtime.py` is the composition root.

```text
supervisor -> local_state -> judgment
connectors -> judgment and supervisor input vocabulary
stream_health imports no edge_runtime sibling
composition root -> runtime packages
```

Storage may know domain types, never the orchestrator. Only the composition root should need all runtime packages.

**【已定目标】** `local_state` 通过小型公共 interface 拥有 Edge 本地持久状态；SQLite connection、SQL、schema、codec 与表布局属于其 implementation。发往 Center `monitor` 的结构化事实通过主机级上报对账 interface 排空。证据上传、Center→Edge 配置同步和物理处置保持各自的 owner 与生命周期，不合并为通用同步框架。详细运行语义见[推理机自治机制](../design/mechanisms/edge-autonomy.md#57-推理机是自治判定单元)。

## Traffic boundaries

Nginx serves Web and center control-plane HTTP. Runtime preview/signaling goes directly from browser to its assigned inference host's MediaMTX. The authorized annotation-derived-media gateway is the only recorded exception ([ADR-0011](../adr/0011-annotation-derived-media-gateway.md)), not a general video relay.

Control-plane `/api/v1` is a fixed prefix, not a version axis ([ADR-0003](../adr/0003-api-v1-is-a-fixed-prefix.md)). Training videos upload individually through presigned MinIO URLs; the center accepts no archives or unpacking. Device credentials stay on the inference host; the center keeps configuration status, not reusable secrets ([ADR-0008](../adr/0008-credentials-stay-on-the-inference-host.md)).

## Enforcement

Use existing import-linter contracts, migration ownership, repository policy, contract tests and generated-contract compatibility to enforce checkable boundaries. Documentation explains these controls and the reasons; it must not become another machine-readable registry. [workflow.md](workflow.md) owns commands and evidence.

Adding or deleting a product module, changing `[tool.nvsop].center_modules` membership, changing a cross-module `api.py` contract, moving table/state ownership, changing shared machine-contract semantics, or materially changing a cross-owner composition root is **architecture-sensitive**. PR preflight must mechanically require an independent semantic architecture review against the current roadmap/mechanism/ADR authority; the checker only identifies the sensitive surface and never decides whether the design is correct. New or retained product modules also use the [module preflight and deletion test](coding.md#size-and-decomposition).
