# Repository instructions

NVSOP is the SOP compliance product monorepo. [docs/README.md](docs/README.md) indexes the documentation; `CLAUDE.md` routes here so agent entrypoints share one policy source.

Verify actual code, configuration, branch, worktree and HEAD before relying on prose or prior session notes. Why: cached descriptions can refer to a different implementation or candidate.

## Route the task

Read guidance only when its trigger applies, expanding for affected callers or conflicting evidence. Why: unrelated mandatory reading obscures the constraints that govern the task.

- **Plan or investigate:** deliver findings or the requested plan; edit only when requested. Why: investigation is not authorization to change the repository.
- **Use Skills:** follow [Skills routing](docs/engineering/workflow.md#skills-routing); repository rules and operation authorization still apply. Why: a method does not grant permission or replace local contracts.
- **Implement code or scripts:** read [coding.md](docs/engineering/coding.md) and the implementation/verification sections of [workflow.md](docs/engineering/workflow.md); locate the public entry, affected callers and existing tests. Why: changes must reuse the current owner and preserve its callers.
- **Change repository shape, ownership or dependencies between modules:** read [architecture.md](docs/engineering/architecture.md). Why: file movement can change state ownership and dependency direction.
- **Add or change repository-local generated/ignored state; change CI, generated contracts, dependencies or vendor code:** read [maintenance.md](docs/engineering/maintenance.md) and [workflow.md](docs/engineering/workflow.md). Why: reproducible outputs and safe cleanup depend on their declared sources and lifecycle.
- **Write, move or delete documentation/instructions:** read [documentation.md](docs/engineering/documentation.md) and the applicable validation/review sections of [workflow.md](docs/engineering/workflow.md). Why: pruning must preserve authoritative requirements and live navigation.
- **Work with Issues, labels or task dispatch:** read [Issue rules](docs/engineering/issues.md) and the [GitHub tracker configuration](docs/agents/issue-tracker.md); before triage also read the [label mapping](docs/agents/triage-labels.md). Why: skills must use the existing tracker, acceptance owners and label meanings rather than a parallel registry.
- **Create branches/worktrees, review, publish/update/refresh a PR, merge or clean up:** read the relevant step of [workflow.md](docs/engineering/workflow.md); use native Git/GitHub operations and the versioned single-task cleanup. Why: the maintained delivery path operates on one verified task, not a separate landing/session-recovery service.
- **Change deployment or runtime configuration:** use the [deployment index](docs/README.md#部署与验证), then relevant architecture/verification rules. Why: deployment assumptions and real-environment evidence belong to the affected runtime owner.
- **Change Edge autonomy, local persistence or Edge→Center reporting:** read [edge-autonomy.md](docs/design/mechanisms/edge-autonomy.md); also read [architecture.md](docs/engineering/architecture.md) when moving ownership, a public seam or a dependency direction, and [the roadmap](docs/design/solution-and-roadmap.md) when changing approved behavior or system structure. Why: local failure handling and reporting must preserve autonomy and durable-state ownership.
- **Explore/name domain concepts or change product behavior:** use the single-context vocabulary in [CONTEXT.md](CONTEXT.md); before domain exploration read the [domain configuration](docs/agents/domain.md). For behavior/architecture read the affected mechanisms and ADRs through [the roadmap](docs/design/solution-and-roadmap.md), identifying any ADR that needs reopening. Why: one vocabulary and explicit decision owners prevent silent requirement changes.

## Invariants

- Reuse NVIDIA capabilities in `vendor/sop-monitoring-blueprints/` as the product trunk, not an ordinary dependency; keep NVSOP judgment in `apps/edge-runtime/` behind the registered minimal hook and one active implementation per capability/path. Why: parallel implementations drift from the base and make upgrades ambiguous.
- Keep judgment, violation latching, disposal and evidence buffering working when the center is unreachable; the center owns management/aggregation, not real-time error-proofing. Why: a center outage must not disable local protection.
- Keep the judgment core a pure, standard-library-only function over normalized observations, independent of camera SDKs, inference frameworks and connectors. Why: deterministic decisions must remain testable without external runtimes or devices.
- A module owns behavior, tables and migrations behind one small interface; callers and tests cross that same seam. Why: one ownership boundary prevents bypasses and couples tests to the supported contract rather than storage details.
- Keep secrets, credentials, customer media, model weights, generated data and production dumps out of Git; fixtures are synthetic or explicitly sanitized. Why: repository history must not retain private data or machine-specific artifacts.

## Execute and stop

Use an isolated `agent/<agent-id>/<task-slug>` worktree and continue existing tasks where they are; `main` is shared trunk and PR target, not a task workspace or direct-push destination, and `dev` is retired. Enable `make hooks` before ref changes and preserve unrelated work. Why: isolation and local guards protect the shared trunk and other writers' state.

Follow [workflow.md](docs/engineering/workflow.md) for implementation, evidence, review, publication and cleanup, including one bounded [task lifecycle plan](docs/engineering/workflow.md#pr-lifecycle-approval) for the intended actions; in-scope repairs renew evidence, not approval. Why: a single delivery authority preserves scoped consent without duplicating procedures or repeatedly requesting it.

Final code uses `make check`, documentation-only work `make check-docs`; repository-policy and instruction changes require the workflow's independent read-only review. Why: passing local checks alone cannot establish that changed rules preserve their intended meaning.

Close Issues through the lifecycle plan when it names them, or the separate explicit path in [Issue rules](docs/engineering/issues.md#close-with-evidence). Why: implementation or a commit alone is not evidence that an Issue's acceptance and closure conditions are met.
