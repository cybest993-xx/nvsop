# Repository authoring rules

Status: **normative**.

This document owns implementation and documentation authoring conventions. Verification policy is in [`repository-verification.md`](repository-verification.md); ownership and dependency rules are in [`repository-architecture.md`](repository-architecture.md).

## Implementation checklist

Before editing, establish these four items once per task and update them only when scope changes:

- **Entry** — public entry point and affected callers.
- **Reuse** — existing implementation to call, or where you searched if none applies.
- **Allowed writes** — fields and state this operation may change.
- **Preserve** — fields and state it must retain.

Keep the checklist in working context for an ordinary bounded task. Use the persistent handoff described in [`../agents/repository-workflow.md`](../agents/repository-workflow.md) only when its continuity triggers apply.

## Implementation rules

1. **Search before adding logic.** Reuse validation, conversion or logging behavior when its meaning matches.
2. **Keep the business flow visible.** Extract a private helper when it names one complete responsibility or makes the caller materially easier to understand; keep trivial pass-throughs inline.
3. **Share identical business rules.** Revalidation calls the same rule. Keep authorization and validation at their required boundaries rather than duplicating them in transports.
4. **Limit data changes.** Check reconstruction and assignments against Allowed writes and Preserve. Do not regenerate adjacent state because it is convenient.
5. **Preserve one production path.** Refactoring must not leave a parallel implementation active behind another entry point.
6. **Prefer contract-driven failure behavior.** Implement required validation and failure paths directly; do not add speculative fallback layers, retries or compatibility knobs without a demonstrated contract need.

## Size and decomposition

Size is advisory; correctness, ownership and compatibility are mandatory.

| Object | Guidance |
|---|---|
| Authored production file | Below roughly 500 physical lines is a readability target. Around 800 lines, assess whether an independent responsibility belongs in a focused private file. A small fix in a large file does not require unrelated refactoring. |
| Product module | No cumulative line cap. Behavior, state ownership, change driver and interface define the boundary. |
| Pull request | Around 800 added+deleted implementation lines prompts a scope review; mechanical changes are different from new logic. `make change-size` is the repository reporter. |

Decompose only when it reduces mixed responsibilities or coupling. Keep the public interface and state owner stable, keep orchestration at the existing entry point, and move related tests/explanations beside extracted behavior. A separately landed stage must be valid and observable on its own; splitting commits does not make an incoherent change smaller.

Do not create product modules from CRUD verbs, transport layers, tables, screen sections, file types, utility buckets, or an adapter plus its fake. A new module needs a coherent invariant/behavior, a distinct change driver, one small acyclic interface and a clear state owner (or pure behavior).

## Comments and documentation language

- Write code comments and docstrings in concise Chinese. Explain non-obvious reasons, constraints, safety or compatibility boundaries; do not narrate the code.
- Use Chinese for human-facing product/domain/deployment documentation.
- Use English for agent-facing instructions, skills and repository operating guidance.
- Keep identifiers, API names, protocol fields and established technical terms unchanged.
- Keep one maintained home for each durable fact. Prefer links to manifests, commands, contracts and ADRs over copied inventories.
- When an implementation fact is easy to verify in a manifest or source file, documentation may explain how to use it but must name the authoritative source.
- When guidance is superseded, migrate surviving facts and update incoming links; Git history is the archive.

## Interface shape

- A parameter takes neither a bare boolean nor an ambiguous optional. Prefer an enum, keyword-only argument or two named functions so the call site states its meaning. Where a signature cannot change and a literal must be passed, name it at the call site with a comment carrying the callee's parameter name exactly.
- Branch on local enumerations exhaustively. At cross-process wire boundaries, preserve forward compatibility required by ADR-0003: unknown reason codes render as the raw code plus a generic hint.
- Every cross-module entry added to `api.py` documents its role and expected caller.
- Do not move behavior into `api.py`, `packages/`, `scripts/` or an adapter merely to reduce file/change counts.

## What a change carries with it

A breaking or compatibility-sensitive change searches the affected surfaces before merging: `/api/v1` OpenAPI, inference-host report/pull contracts, migration/table ownership, resolved runtime parameters and in-flight instance behavior across restart. State which surfaces were checked.

A dependency change and its lockfile update land together. Generated output is regenerated in the same change as its source, using the canonical command documented in [`repository-maintenance.md`](repository-maintenance.md).
