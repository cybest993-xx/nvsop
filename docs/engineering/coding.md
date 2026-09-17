# Code authoring

Status: **normative**. This document owns implementation conventions, interface shape and decomposition. [workflow.md](workflow.md) owns evidence and delivery; [architecture.md](architecture.md) owns dependency boundaries; [documentation.md](documentation.md) owns documentation language, placement and lifecycle.

## Establish the change

Record these once per task, updating only when scope changes: **Entry** (public entry and affected callers), **Reuse** (existing behavior or where searched), **Allowed writes** (fields/state permitted to change), **Preserve** (fields/state retained). Keep them in working context unless [continuity](workflow.md#persistent-continuity) requires a handoff.

## Implementation rules

1. Search before adding validation, conversion, logging or another implementation. Reuse behavior when its meaning matches.
2. Keep the business flow visible. A private helper should name one complete responsibility or materially clarify its caller; keep trivial pass-throughs inline.
3. Share identical business rules. Revalidation uses the same rule; authorization and validation stay at their required boundaries rather than being duplicated in transports.
4. Check reconstruction and assignments against Allowed writes and Preserve. Do not regenerate adjacent state for convenience.
5. Retain one production path; a refactor cannot leave two active implementations of one capability.
6. Implement required validation and failure paths directly. Fallback, retry, compatibility, integrity, caching or generic infrastructure needs a current contract, demonstrated failure or security/integrity boundary, not speculation. Preserve required protection when its role is uncertain and inspect the owner.

## Size and decomposition

Size is advisory; correctness, ownership and compatibility are mandatory. Roughly 500 physical lines is a production-file readability target; around 800, assess an independent responsibility for extraction. A small fix in a large file does not require unrelated refactoring. Product modules have no cumulative line cap. Around 800 added/deleted implementation lines prompts PR scope review; mechanical migration is different from new logic. Use `make change-size`.

Extract only to reduce mixed responsibilities or coupling. Keep public interfaces/state owners stable, orchestration at its existing entry and related tests/explanations beside the behavior. A separately landed stage must be valid and observable on its own; splitting commits does not make an incoherent change smaller.

A new product module needs a coherent invariant/behavior, distinct change driver, small acyclic interface and clear state owner or pure behavior. CRUD verbs, transport layers, tables, screen sections, file types, utility buckets and an adapter plus its fake are not module boundaries by themselves.

## Interface shape

- Prefer enums, keyword-only arguments or named functions over bare boolean or ambiguous optional parameters. If a signature cannot change, name an opaque literal at the call site with a comment containing the exact callee parameter name.
- Branch exhaustively on local enumerations. Preserve ADR-0003's required wire compatibility: unknown reason codes render as raw code plus a generic hint.
- Every cross-module `api.py` entry documents its role and expected caller; `api.py` is not a place to move implementation just to reduce file size.
- Do not relocate behavior into `packages/`, `scripts/` or adapters merely to reduce file/change counts. Keep ownership explicit.

Code comments and docstrings use concise Chinese for non-obvious reasons, constraints and contracts; they do not narrate code or contain the implementation/review transcript.

## Changes carried together

Before merging a breaking or compatibility-sensitive change, inspect affected `/api/v1` OpenAPI, inference-host report/pull contracts, migration/table ownership, resolved runtime parameters and in-flight instances across restart. Record which surfaces were checked.

Dependency changes include their lockfile updates. Generated output lands with its source through the canonical [maintenance procedure](maintenance.md). Update the relevant public contract/documentation with the behavior, without adding unrelated documentation churn.
