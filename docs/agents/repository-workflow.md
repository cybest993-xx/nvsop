# Repository workflow for agents

Status: **normative**.

This document owns task workspace isolation, continuity, the working/review cycle and repository-instruction loading. Branch/ref mechanics are authoritative in [`../design/local-branch-workflow.md`](../design/local-branch-workflow.md); verification and review triggers are authoritative in [`../design/repository-verification.md`](../design/repository-verification.md).

## Workspace isolation

`main` is the repository trunk and the pull-request target. Do not use it as a task workspace, commit task changes on it, or push task refs directly to it.

For new task work, create one `agent/<agent-id>/<task-slug>` branch from the current accepted `main` tip and give it its own worktree. Continue an existing task on its existing branch/worktree instead of creating another branch for each window or repair.

`dev` is retired from the delivery path. Do not create or advance it for new work; an existing legacy local `dev` ref may be deleted after its commits are accounted for. Publish each completed `agent/...` branch and open its PR directly against `main`. See the local branch workflow for the exact allowed ref transitions.

Use an isolated worktree whenever the primary checkout contains unrelated work, another writer is active, a fixed development instance owns the primary checkout, or switching branches would disrupt running work. Never discard, auto-stash or overwrite unrelated changes to make a task workspace convenient.

Before changing branches, worktrees or refs:

```sh
make hooks
git config --get core.hooksPath
```

The second command must resolve to the versioned `scripts/githooks` path (or its configured equivalent).

## Persistent continuity

An ordinary bounded single-session change needs no handoff file. Maintain the ignored `.tmp/task-handoff.md` when work crosses sessions/context compaction, involves multiple agents/worktrees, changes cross-module behavior or critical invariants, or leaves required review/validation to resume later.

Keep only durable continuation facts:

- the user request and complete acceptance criteria;
- the authoring checklist (Entry / Reuse / Allowed writes / Preserve);
- fixed comparison base and current candidate commit;
- uncommitted/untracked changes;
- review findings and their resolution state;
- valid checks with command/environment/covered inputs;
- the next concrete action.

Update facts instead of copying conversation history. A handoff is not authorization and is not proof that a check passed.

## Working and review cycle

1. **Scope and resume.** Confirm the task branch/worktree, applicable instructions, comparison base and complete acceptance criteria. Reuse still-valid evidence.
2. **Implement.** Follow repository architecture/authoring rules and keep writes inside authorized scope. Save task-branch commits when useful; a commit is progress, not merge acceptance.
3. **Verify the slice.** Apply [`repository-verification.md`](../design/repository-verification.md) and record actual results/gaps. An unavailable environment is a gap, not a pass.
4. **Self-review.** Inspect the complete diff and affected callers/references.
5. **Independent review when triggered.** Repository policy, agent instructions, deployment, CI, dependencies, public interfaces and critical invariants require the consolidated read-only review defined by the verification policy.
6. **Repair and recheck.** Fix blocking findings as one bounded batch, rerun affected checks and review the repair increment when required.
7. **Finish by state.** Report delivered behavior, checks and remaining gaps. Push the task branch, target its PR at `main`, and merge only with the user's authorization plus the repository's acceptance evidence; issue closure and other publication remain separately authorized.

Stop adding optional tests/refactors once acceptance behavior and required evidence are complete. A concrete blocker reopens only the affected work.

## Instruction hierarchy and loading

Root `AGENTS.md` is the short always-loaded entry point. It contains universal invariants plus one conditional pointer per task type. Detailed guidance lives in the linked repository documents and is read only when the task trigger applies; a Markdown link is not itself automatically loaded.

Keep each rule in one authoritative home. Point to tool configuration, manifests, commands, contracts and ADRs rather than copying their current values into instructions. Add a nested `AGENTS.md` only for a real local exception. `CLAUDE.md` remains a pointer to the root instructions unless a client-specific requirement is explicitly introduced.

The official Codex AGENTS mechanism discovers instructions from repository root toward the working directory and has a combined project-instruction byte budget. Treat that as a loading constraint, not as a source-file line-count target. Keep root instructions small enough that important routing and invariants stay reachable.

After material changes to repository instructions or the agent runtime, verify routing in a fresh session with representative tasks:

- a documentation/copy task should not require reading the full product roadmap;
- a bounded code task should reach authoring + verification rules;
- a `vendor/` or generated-contract task should reach maintenance rules;
- a product/architecture task should reach the roadmap, mechanism spec and relevant ADRs.

Do this after material instruction changes, not as ceremony for every task.
