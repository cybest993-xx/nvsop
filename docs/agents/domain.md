# Domain docs

This file maps engineering-skill domain-document conventions to the
repository's existing owners.

## Layout

**Single-context.**

- Glossary: [CONTEXT.md](../../CONTEXT.md), serving the role that skills
  call `GLOSSARY.md`.
- Architectural decisions: [ADR index](../adr/README.md).

## Consumer rules

- Before exploring or naming domain concepts, read `CONTEXT.md` and
  use its vocabulary.
- For product behavior or architecture, read the affected mechanisms
  and ADRs through the [roadmap](../design/solution-and-roadmap.md).
  Surface conflicting ADRs and the reason to reopen them.
- A skill's glossary-writing step targets `CONTEXT.md`, within the
  approved task scope, rather than a parallel `GLOSSARY.md` or
  `GLOSSARY-MAP.md`.
- Domain-document changes follow
  [documentation ownership](../engineering/documentation.md).
