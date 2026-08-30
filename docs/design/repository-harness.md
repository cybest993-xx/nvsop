# Repository harness

Status: **normative**  
Reference baseline: [`openai/codex@f4f85add`](https://github.com/openai/codex/tree/f4f85add41288c2059dc1a4326a598f739e47fe9), inspected 2026-08-29 — repository shape and gates (§2–§4, §6–§8).  
Second inspection: [`openai/codex@main`](https://github.com/openai/codex), read 2026-08-31 — authoring conventions (§5) and instruction-file limits (§9). Read at a different time than the baseline above; do not treat the two as one pinned observation.

This document fixes the repository shape, authoring conventions, and verification interface before product code is added. It adopts the reference baseline's transferable patterns—not its Rust/Bazel technology choices: one root task interface, package-owned tests, scoped agent instructions, change-aware CI, one aggregate required status, and size-bounded changes.

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
│   │   ├── src/factory_sop/        # auth, device, template, dataset,
│   │   │                            # monitor, alert, evidence, job
│   │   ├── migrations/             # one linear history; file prefix names the owning module
│   │   └── tests/{unit,integration}/
│   ├── control-web/                # Vue 3 operator/admin application
│   │   ├── src/modules/            # feature slices matching backend language
│   │   └── tests/{integration,e2e}/
│   └── edge-runtime/               # inference-host autonomous judgment unit:
│       │                            # judgment core, boundary solver, local state,
│       │                            # supervisor, connector runtime, evidence clipping
│       ├── src/
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

When the first Python workspace lands, use one root `pyproject.toml` and committed `uv.lock`. When the web workspace lands, use a root `package.json`, `pnpm-workspace.yaml`, and committed `pnpm-lock.yaml`. Pin runtimes and package-manager versions; CI installs from lockfiles without updating them. Do not add these manifests before a real workspace exists.

## 3. Module ownership and seams

| Module | Owns | Interface examples | Adapters live outside the behavior |
|---|---|---|---|
| `auth` | users, roles, permissions, sessions | authorize command; open/revoke session | FastAPI auth, PostgreSQL |
| `device` | inference hosts, inference backends, stations, cameras, connector configuration and measured capability declarations, station runtime-parameter overrides | resolve station topology; report health | Hikvision, board card, inference health |
| `execution` | which host may drive a station's output points: grant, handover handshake, two-person forced rebind, physical-execution lease | request/confirm handover; renew lease | center HTTP, inference-host pull |
| `template` | Excel validation, immutable versions, release/binding, desired-vs-reported reconciliation | import draft; release; resolve active version | workbook parser, object storage |
| `dataset` | training videos, action time-range annotation, usage checks, derived artifacts | register video; annotate ranges; run usage check; emit artifact | presigned object storage, reused annotator UI |
| `monitor` | reported mirror of SOP instances, decisions, and stream health; dashboard state | upsert reported decision; query dashboard | inference-host report endpoint |
| `alert` | archived violations and disposal records | archive violation; archive disposal result | UI notification |
| `evidence` | evidence references and human review | request clip; attach review | edge runtime, MinIO |
| `job` | durable asynchronous application commands | enqueue/query application job | ARQ/Redis |

`execution` is separate from `device` because their change drivers differ: `device` models what equipment exists and its use cases are reversible configuration CRUD, while `execution` is a cross-host safety state machine with a TTL and carries the system's only two-person operation. It enforces one invariant that must hold inside the module rather than in its callers: at most one inference host holds an unexpired physical-execution right for a station at any moment.

`monitor`, `alert`, and `evidence` stay separate even though one report-intake adapter writes all three, because `monitor` changes with the report contract, `alert` with the set of disposal actions, and `evidence` carries human review — a different actor and a different lifetime (evidence outlives recording retention; review never rewrites the original decision). The intake adapter composes them inside one request-scoped Unit of Work and carries no judgment logic.

An inference backend is one process endpoint carrying one template configuration, not one machine; a host runs as many backends as it has distinct templates, and `device_inference_host` models the machine separately. The judgment core is not a center module: it lives in `edge-runtime`, owns no tables, and stays a pure function. `monitor` holds the center-side mirror written by idempotent upserts from each inference host, so its rows are reports rather than the authority — the authority for decisions, latched violations, and disposal execution is the inference host's local SQLite.

`dataset` is separate from `template` on purpose: the glossary defines a training data set as something that does **not** define an SOP template, and lists SOP template as an avoided synonym. Merging them in code would re-fuse two concepts the language deliberately separates.

Module boundaries are enforced mechanically, not by review. Each center module is a Python package with a `usecases/` layer where authorization is enforced, and an `api.py` that exposes only the subset other modules actually call — `api.py` is the cross-module contract, not the module's full outward entry set, so a configuration module's CRUD use cases stay in `usecases/` rather than becoming pass-through re-exports. Authorization is enforced in `usecases/`, never only at the HTTP route, because jobs, report intake, and smoke scripts call the same use cases. `api.py` is the only permitted cross-module import target; `import-linter` contracts fail the gate when the judgment core imports an adapter, when a module imports past another's `api`, when a domain layer imports `adapters/*`, or when the hook inside `vendor/` imports anything other than the one designated stream-health entrypoint module in `edge-runtime`. That last contract is what keeps the patch surface at one append-only call: code under `vendor/` is outside this repository's lint and type coverage, so without a mechanical contract the hook can silently grow dependencies on arbitrary `edge-runtime` internals. Physical table names carry their owning module's prefix (`device_camera`, `template_version`, `auth_session`) so migration ownership is statically decidable. Cross-module use cases share one request-scoped Unit of Work opened and committed by the HTTP adapter layer; module facades participate but never commit. The HTTP adapter layer may compose several modules' `summary()` use cases into one aggregate response (the overview page); that composition carries no judgment logic.

`packages/contracts` contains only wire contracts used across processes (the inference-host report and pull contracts, the exported `openapi.json`) and their compatibility fixtures. It must not become a shared domain-logic bucket.

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

Every bug fix starts with the narrowest regression test that fails for the observed behavior. Safety invariants require tests at the judgment core's interface, especially that invalid observation periods never become a false failure verdict. The rework sequence `1,2,3,2,4,5` must be judged compliant: the base heuristic reports it as two separate violations, so this is the first regression test the judgment core has to pass and the sharpest line between our behavior and the base's.

Fixtures must be synthetic or sanitized, minimal, deterministic, and documented with provenance. Customer video, credentials, model weights, and production exports are never fixtures.

### Test authoring

- A behavior change lands with an integration test. A unit test alone does not prove that a module's observable behavior changed at its seam.
- Assert on whole objects rather than field by field, so a field that changes unexpectedly fails the test instead of passing unread.
- Statically defined values get no test, and deleted logic leaves no negative test behind. Both pin the implementation in place of the behavior.
- Unit tests live in the owning app's `tests/` tree, never inline in the implementation file, and implementation code carries no test-only function.
- Look for an existing helper or fixture before writing another one.
- A test never mutates process environment variables; the value under test arrives through a parameter.
- A user-visible UI change lands with snapshot coverage.

## 5. Code authoring rules

Technology-neutral by intent: the reference baseline's crate layout, named clippy lints, ratatui styling, and ASCII-only default are its own and are not adopted here.

### Size budgets

- A change stays under 800 lines. A change to judgment, boundary-solving, or retention logic stays under 500. Past that, split it into stages that each stand on their own and land the smallest self-consistent stage first. The budget is per landed stage and counts implementation lines, not the tests that land with them: a stage is measured on what a reviewer must hold in their head to judge it correct, and summing the stages the rule just asked for would forbid the split it prescribes. What the sum of a feature's stages must satisfy is that each one stood on its own when it landed.
- A module file stays under 500 lines excluding tests. Prefer a new module over growing an existing one past that.
- Resist growth in shared ground. A new capability belongs to the module that owns it, or to a new module. `packages/contracts` and a module's `api.py` are where an unnecessary addition costs the most, because every other module pays for it.

### Interface shape

- A parameter takes neither a bare boolean nor an ambiguous optional, because both force the call site to read `f(False)`. Use an enum, a keyword-only argument, or two named functions so the call site states its own meaning.
- Where a signature cannot change and a literal must be passed, name it at the call site with a comment carrying the callee's parameter name exactly.
- Branch on an enumeration exhaustively; a catch-all arm swallows the next value added. One exception, and it is mandatory rather than permitted: at a cross-process wire boundary a forward-compatible fallback is required, because the inference host and the center upgrade independently — an unknown reason code renders as the raw code plus a generic hint (see [ADR-0003](../adr/0003-api-v1-is-a-fixed-prefix.md)).
- Every entry added to a module's `api.py` carries a docstring giving its role and the caller's expected use. `api.py` is the cross-module contract, so an undocumented entry there is an unbounded promise.
- A helper with one call site stays inlined.

### What a change carries with it

- A breaking change searches a fixed surface before merging: the `/api/v1` OpenAPI contract, the inference-host report and pull contracts, migration and table ownership, resolved runtime parameters, and in-flight instance behavior across a restart. State which of those you checked.
- A dependency change and its lockfile update land in one commit.
- Generated output is regenerated in the same change as its source, and CI verifies a clean worktree.

## 6. Stable command interface

`make check` is the CPU-only, infrastructure-free merge gate and must work from the repository root, without Docker. CI calls it exactly as developers do. Today it runs repository policy checks; each workspace-adding change must extend it in the same change with that workspace's formatting, lint, type, unit, contract, and build checks, plus the boundary checks (`import-linter` contracts, migration table-ownership, generated-artifact cleanliness).

`make check-integration` is the second required target: one application plus real local infrastructure (PostgreSQL, Redis, MinIO) started as containers via testcontainers. It is separate because a developer without Docker must still be able to run `make check`, and because container startup does not belong in the fast feedback loop. Both targets feed the blocking gatherer, so merge protection strength is unchanged. SQLite and in-memory fakes are not substitutes for the integration target: transaction isolation, `JSONB`, timezone, and deferred foreign key behavior differ enough to produce false green.

Package-specific commands may exist for a tight feedback loop, but they do not replace these two targets. Automation in `scripts/` stays thin: product behavior belongs in an app or package where it can be tested through its interface.

Reach for these targets rather than the tool underneath. They carry the flags, environment, and ordering that CI uses, so invoking `pytest`, `ruff`, or `vitest` directly runs a different check than the gate runs. Run the formatter after changing code without asking first. Container startup, model loading, and integration bring-up are slow by nature: wait for them instead of killing the process and reporting a failure.

## 7. CI gates

### Blocking pull-request gate

The sole branch-protection status is `CI required` from `.github/workflows/blocking-ci.yml`. The gatherer runs with `always()` and fails if any required dependency fails or is cancelled. This prevents a skipped downstream job from appearing green.

As workspaces appear, split checks into reusable workflows while retaining the gatherer:

1. repository policy and lockfile cleanliness — always;
2. backend/edge format, lint, type, unit, boundary, and base-code contract checks (`make check`) — on relevant paths;
3. backend integration checks against real containerized infrastructure (`make check-integration`) — on relevant paths;
4. web format, lint, type, unit, and production build — on relevant paths;
5. migration and cross-process contract compatibility — when schemas or contracts change;
6. workflow changes — run every blocking family.

Path filtering is an optimization, not an exemption: every reusable workflow must return an explicit success when no relevant files changed. Actions are pinned to immutable commit SHAs, permissions are least-privilege, dependency installs are frozen, jobs have timeouts, and cancellation is enabled for superseded PR runs.

### Non-blocking and release gates

GPU, camera, connector, multi-stream, 72-hour, and 7-day suites run on labeled self-hosted runners by manual dispatch or schedule. They publish evidence and never silently fall back to mocks. Release promotion additionally requires SBOM/license review, image and dependency scanning, migration/rollback rehearsal, offline-start verification, and immutable image/model digests.

## 8. Inherited base code and generated artifacts

`vendor/sop-monitoring-blueprints/` is the NVIDIA base code, this system's trunk. It changes only through a dedicated `git subtree pull` or a recorded replayable patch within the scope fixed by [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md): one patch, which only adds output — the pipeline message callback that surfaces stream-health events as a synthetic chunk. Sequence comparison and boundary solving are reimplemented in `apps/edge-runtime/` rather than patched, because the base interleaves boundary detection with sequence comparison inside one method; the base checker and the base disposal are switched off through their existing environment variables rather than replaced. `vendor/` therefore carries no patch for either. Everything else stays as delivered; capabilities the base already provides are reused rather than rebuilt. Patch logic lives in `apps/edge-runtime/` with only a minimal hook inside `vendor/`, so the patch surface stays small and our code remains testable. The update change records the NVIDIA commit in [`docs/base/verified-commits.md`](../base/verified-commits.md) and runs all tests in `tests/contract/base/`.

Generated clients, schemas, and deployment output must have one documented source command. CI regenerates and checks a clean worktree. Commit generated output only when consumers cannot generate it during install or build.

## 9. Agent instruction hierarchy

The root `AGENTS.md` is loaded on every turn, so each of its lines is paid whether or not it fires. It carries only the invariants that must hold before any file is opened, plus one pointer per branch of work; the rules themselves live here or in the decision source. Add a nested `AGENTS.md` only where a subtree has a real local exception, and keep each rule in one place rather than copying this document into an agent file.

Two limits from the reference baseline bear on that directly. An agent runtime caps the instruction bytes it loads — the baseline's default is 32 KiB with later files truncated — so an instruction file that keeps growing silently stops being read in full. And that same project's own `AGENTS.md` generator specifies 200–400 words, while its hand-written file runs roughly 550 lines. Follow the specification rather than the example: material only some branches need belongs behind a pointer. Formatting and lint rules belong in the tool config and the gate, never in an instruction file, where they cost context on every turn and go stale without failing anything.

## 10. Reference-baseline patterns used

- One always-loaded root [`AGENTS.md`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/AGENTS.md) and no nested instruction file: the baseline organizes by subsystem *inside* that one file and pushes bulky reference (`codex-rs/tui/styles.md`) behind a pointer. Verified against the repository tree, which holds exactly one `AGENTS.md`.
- Authoring conventions stated as one-line imperatives with the reason attached only where the reason changes the behavior.
- One task interface shared by humans and automation: [`justfile`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/justfile).
- Package-owned integration suites: [`codex-rs/core/tests`](https://github.com/openai/codex/tree/f4f85add41288c2059dc1a4326a598f739e47fe9/codex-rs/core/tests).
- Reusable checks aggregated into one required result: [`blocking-ci.yml`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/.github/workflows/blocking-ci.yml).
- Change detection with a required gatherer and explicit no-change success: [`rust-ci.yml`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/.github/workflows/rust-ci.yml).
- Repository-specific invariant checks and clean-worktree verification: [`repo-checks.yml`](https://github.com/openai/codex/blob/f4f85add41288c2059dc1a4326a598f739e47fe9/.github/workflows/repo-checks.yml).
