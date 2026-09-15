# Repository harness

Status: **normative routing and compatibility index**.

The former monolithic repository harness has been split by task trigger so agents and humans can load only the rules relevant to the current change. This file remains intentionally small because repository policy, existing comments and historical links still refer to `repository-harness.md` and to harness §1–§10.

Use the direct document for new references:

| Task | Authoritative document |
|---|---|
| Repository shape, module ownership, dependency and traffic boundaries | [`repository-architecture.md`](repository-architecture.md) |
| Implementation/documentation authoring, decomposition and interface shape | [`repository-authoring.md`](repository-authoring.md) |
| Tests, evidence, stable commands, CI and independent review | [`repository-verification.md`](repository-verification.md) |
| Agent workspace, handoff, working cycle and instruction loading | [`../agents/repository-workflow.md`](../agents/repository-workflow.md) |
| NVIDIA subtree, generated contracts, dependencies and toolchain maintenance | [`repository-maintenance.md`](repository-maintenance.md) |
| Branch/worktree/ref mechanics | [`local-branch-workflow.md`](local-branch-workflow.md) |
| Product behavior and architecture decisions | [`solution-and-roadmap.md`](solution-and-roadmap.md) plus its mechanism specs/ADRs |
| Install, runtime configuration, limitations and upgrades | [`../deployment/`](../deployment/) |

## Legacy § mapping

Existing `harness §N` references remain valid through this table. Do not add new section-number references; link the direct document/heading instead.

| Legacy section | Current home |
|---|---|
| §1 Architecture rule | [`repository-architecture.md#architecture-rule`](repository-architecture.md#architecture-rule) |
| §2 Canonical layout / workspace manifests | [`repository-architecture.md#canonical-layout`](repository-architecture.md#canonical-layout), [`#workspace-manifests`](repository-architecture.md#workspace-manifests) |
| §3 Module ownership and seams | [`repository-architecture.md#center-ownership`](repository-architecture.md#center-ownership), [`#edge-runtime-ownership`](repository-architecture.md#edge-runtime-ownership) |
| §4 Test placement and evidence | [`repository-verification.md`](repository-verification.md) |
| §5 Code authoring rules | [`repository-authoring.md`](repository-authoring.md) |
| §6 Stable command interface | [`repository-verification.md#stable-commands`](repository-verification.md#stable-commands) |
| §6 Working and review cycle | [`../agents/repository-workflow.md#working-and-review-cycle`](../agents/repository-workflow.md#working-and-review-cycle) |
| §7 CI gates | [`repository-verification.md#ci-gates`](repository-verification.md#ci-gates) |
| §8 Inherited base/generated artifacts | [`repository-maintenance.md`](repository-maintenance.md) |
| §9 Agent instruction hierarchy | [`../agents/repository-workflow.md#instruction-hierarchy-and-loading`](../agents/repository-workflow.md#instruction-hierarchy-and-loading) |
| §10 Reference-baseline patterns | [`repository-maintenance.md#reference-patterns`](repository-maintenance.md#reference-patterns) |

## Reference baselines

The split keeps the transferable patterns previously checked against the official OpenAI Codex repository: a short root agent entry point, detailed guidance behind scoped pointers, one stable task interface, package-owned tests, change-aware CI and one aggregate required status. External repositories are design references only; NVSOP manifests, code, tests, ADRs and product design remain authoritative for NVSOP behavior.
