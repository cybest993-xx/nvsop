# Repository instructions

NVSOP is the SOP compliance product monorepo. Verify actual code, configuration, branch, worktree and HEAD before relying on prose or prior session notes. [docs/README.md](docs/README.md) is the document index; `CLAUDE.md` routes here.

## Route the task

Read guidance when its trigger applies, expanding for affected callers or conflicting evidence:

- **Plan or investigate:** deliver findings or the requested plan; edit only when requested.
- **Use Skills:** follow [Skills routing](docs/engineering/workflow.md#skills-routing); repository rules and operation authorization still apply.
- **Implement code or scripts:** read [coding.md](docs/engineering/coding.md) and the implementation/verification sections of [workflow.md](docs/engineering/workflow.md); locate the public entry, affected callers and existing tests.
- **Build, restyle or delete Web UI:** read [frontend.md](docs/engineering/frontend.md).
- **Change repository shape, ownership or dependencies between modules:** read [architecture.md](docs/engineering/architecture.md).
- **Change generated/ignored state, CI, generated contracts, dependencies or vendor code:** read [maintenance.md](docs/engineering/maintenance.md) and the relevant [workflow](docs/engineering/workflow.md) sections.
- **Write, move or delete documentation/instructions:** read [documentation.md](docs/engineering/documentation.md) and the validation/review sections of [workflow.md](docs/engineering/workflow.md).
- **Work with Issues, labels or task dispatch:** read [Issue rules](docs/engineering/issues.md) and [tracker configuration](docs/agents/issue-tracker.md); before triage also read the [label mapping](docs/agents/triage-labels.md).
- **Create branches/worktrees, review, publish/update/refresh a PR, merge or clean up:** follow the relevant [workflow](docs/engineering/workflow.md) step using native Git/GitHub operations and the versioned single-task cleanup.
- **Change deployment or runtime configuration:** use the [deployment index](docs/README.md#部署与验证), then relevant architecture/verification rules.
- **Change Edge autonomy, local persistence or Edge→Center reporting:** read [edge-autonomy.md](docs/design/mechanisms/edge-autonomy.md); also read [architecture.md](docs/engineering/architecture.md) when moving ownership, a public seam or a dependency direction, and [the roadmap](docs/design/solution-and-roadmap.md) when changing approved behavior or system structure.
- **Explore/name domain concepts or change product behavior:** use [CONTEXT.md](CONTEXT.md); before domain exploration read the [domain configuration](docs/agents/domain.md). For behavior/architecture read affected mechanisms and ADRs through [the roadmap](docs/design/solution-and-roadmap.md); identify any ADR that needs reopening.

## Invariants

- Reuse NVIDIA capabilities in `vendor/sop-monitoring-blueprints/` as the product trunk; keep NVSOP judgment in `apps/edge-runtime/` behind the registered minimal hook and one active implementation per capability/path.
- The inference host is autonomous: judgment, violation latching, disposal and evidence buffering continue when the center is unreachable. The center owns management/aggregation, not real-time error-proofing.
- Keep the judgment core pure and standard-library-only over normalized observations, independent of camera SDKs, inference frameworks and connectors.
- A module owns behavior, tables and migrations behind one small interface; callers and tests cross that same seam.
- Keep secrets, credentials, customer media, model weights, generated data and production dumps out of Git; fixtures are synthetic or explicitly sanitized.

## Execute and stop

Use an isolated `agent/<agent-id>/<task-slug>` worktree; continue existing tasks where they are. `main` is shared trunk and PR target, not a task workspace or direct-push destination; `dev` is retired. Enable `make hooks` before ref changes and preserve unrelated work.

Confirm one bounded [task lifecycle plan](docs/engineering/workflow.md#pr-lifecycle-approval) for intended delivery actions; in-scope repairs renew evidence, not approval. Final code uses `make check`, documentation-only work `make check-docs`; policy and instruction changes require independent read-only review. Close Issues only under [Issue closure rules](docs/engineering/issues.md#close-with-evidence).
