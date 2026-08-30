# Domain docs

## Use the glossary's vocabulary

When your output names a domain concept — an issue title, a test name, a refactor proposal, a commit message — use the term as [`CONTEXT.md`](../../CONTEXT.md) defines it, and not a synonym its _Avoid_ list rules out. A concept missing from the glossary is a signal: either you are inventing language the project does not use, or there is a real gap worth naming.

## Flag ADR conflicts

When your output contradicts an ADR in [`docs/adr/`](../adr/), say so rather than silently overriding it:

> _Contradicts [ADR-0006](../adr/0006-cycle-boundary-is-declared-not-inferred.md) (cycle boundary is declared) — but worth reopening because…_
