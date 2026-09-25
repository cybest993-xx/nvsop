# Repository instructions

NVSOP is the SOP compliance product monorepo. Verify actual code, configuration, branch, worktree and HEAD before relying on prose or prior session notes. [docs/README.md](docs/README.md) is the document index.

## Route the task

Read guidance only when its trigger applies, expanding for affected callers or conflicting evidence:

- **Plan or investigate:** deliver findings or the requested plan; start edits only when requested.
- **Implement code or scripts:** read [coding.md](docs/engineering/coding.md) and the implementation/verification sections of [workflow.md](docs/engineering/workflow.md). Locate the public entry, affected callers and existing tests first.
- **Change repository shape, ownership or dependencies between modules:** read [architecture.md](docs/engineering/architecture.md).
- **Add or change repository-local generated/ignored state; change CI, generated contracts, dependencies or vendor code:** read [maintenance.md](docs/engineering/maintenance.md) and [workflow.md](docs/engineering/workflow.md).
- **Write, move or delete documentation/instructions:** read [documentation.md](docs/engineering/documentation.md) and the applicable validation/review sections of [workflow.md](docs/engineering/workflow.md).
- **Work with Issues, labels or task dispatch:** read [issues.md](docs/engineering/issues.md).
- **Create branches/worktrees, review, publish, merge or clean up:** read the relevant step of [workflow.md](docs/engineering/workflow.md).
- **Change deployment or runtime configuration:** use the [deployment index](docs/README.md#部署与验证), then relevant architecture/verification rules.
- **Change Edge autonomy, local persistence or Edge→Center reporting:** read [edge-autonomy.md](docs/design/mechanisms/edge-autonomy.md). Also read [architecture.md](docs/engineering/architecture.md) when moving ownership, a public seam or a dependency direction, and [the roadmap](docs/design/solution-and-roadmap.md) when changing approved product behavior or system structure.
- **Name domain concepts or change product behavior:** use [CONTEXT.md](CONTEXT.md); for behavior/architecture read the affected mechanism and ADRs through [the roadmap](docs/design/solution-and-roadmap.md). Identify an ADR that needs reopening rather than silently overriding it.

## Invariants

- NVIDIA code in `vendor/sop-monitoring-blueprints/` is the product trunk, not an ordinary dependency. Reuse its capabilities; NVSOP judgment stays in `apps/edge-runtime/` behind the registered minimal hook. Two active implementations of one capability on one path are a defect.
- The inference host is autonomous: judgment, violation latching, disposal and evidence buffering keep working while the center is unreachable. The center is management/aggregation, never the real-time error-proofing path.
- The judgment core is a pure, standard-library-only function over normalized observations, independent of camera SDKs, inference frameworks and connectors.
- A module owns behavior, tables and migrations behind one small interface; callers and tests cross that same seam.
- Keep secrets, credentials, customer media, model weights, generated data and production dumps out of Git. Fixtures are synthetic or explicitly sanitized.

## Execute and stop

`main` is shared trunk and PR target, not a task workspace or direct-push destination. Use an isolated `agent/<agent-id>/<task-slug>` worktree; continue existing tasks where they are. Enable `make hooks` before ref changes and follow workflow.md without discarding unrelated work. `dev` is retired.

State risk, test reuse/additions and intended checks in at most three lines. Implement required contracts/failure paths directly; speculative fallback, retry, compatibility or integrity layers are not default work. Preserve required safety/security validation.

Use the smallest affected checks while iterating; final code uses `make check`, documentation-only work `make check-docs`. Repository-policy and instruction changes still require the workflow's independent read-only review. One main session owns repairs and completion.

Retain every acceptance criterion and report actual evidence/gaps. Stop optional tests/refactors once acceptance and required evidence are complete. Committed or pushed does not mean merge-ready; downstream stages follow the task plan and gates defined by workflow.md without repeated stage confirmation.
