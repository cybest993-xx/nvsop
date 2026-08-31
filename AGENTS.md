# Repository instructions

This repository is a product monorepo for the SOP compliance system. Use the canonical terms in `CONTEXT.md`.

## Before changing the repository

- For **code placement, authoring rules, tests, dependencies, CI, deployment assets, or `vendor/`**, read [`docs/design/repository-harness.md`](docs/design/repository-harness.md). It is the normative repository harness.
- For **product behavior, architecture, performance budgets, or safety invariants**, read [`docs/design/solution-and-roadmap.md`](docs/design/solution-and-roadmap.md). It is the only current decision source and indexes the mechanism specs.
- For **an issue, a ticket, or a label**, read [`docs/agents/issue-tracker.md`](docs/agents/issue-tracker.md) and [`docs/agents/triage-labels.md`](docs/agents/triage-labels.md).
- Read the ADRs in [`docs/adr/`](docs/adr/) that touch the area you are changing. Contradicting one is allowed; doing so silently is not — say which ADR and why it should reopen.

## Invariants

These five hold before you read anything else. Everything else lives in the harness or the decision source.

- The NVIDIA base code in `vendor/sop-monitoring-blueprints/` is this system's trunk, not an external dependency. Reuse what it implements; keep our logic in `apps/edge-runtime/` behind the one recorded hook. Two implementations of one capability on the same path are a defect.
- The inference host is autonomous: judgment, violation latching, disposal, and evidence buffering keep working while the center is unreachable. The center is a management and aggregation plane, never on the real-time error-proofing path.
- The judgment core is a pure function over normalized observations, standard-library-only, and knows nothing of camera SDKs, inference frameworks, or connectors.
- A module owns its behavior, tables, and migrations behind one small interface. Callers and tests cross that same seam.
- Secrets, credentials, customer media, model weights, generated data, and production dumps stay out of Git. Fixtures are synthetic or explicitly sanitized.

## Writing rules down

Point at the source of truth instead of restating it. When a rule is already carried by an `import-linter` contract, a `make` target, a config file, or an ADR, cite that place — a second copy goes stale silently. Add a nested `AGENTS.md` only where a subtree has a real local exception, and delete a superseded conclusion once its surviving facts are merged into the current decision source; Git is the archive.

## Working on a change

- One ticket, one branch, one worktree, never `main`. `git worktree add ../nvsop-19 -b issue-19 origin/main`, outside the repository because `scripts/check_repo_policy.py` counts untracked files and would reject the directory as an undeclared top level. Delete both once merged; a long-lived shared branch fuses tickets, so the next merge carries whatever else was sitting on it. `main` only receives finished work.
- Write the failing test before the implementation: invoke the `tdd` skill and follow it. Harness §4 fixes where a test lives; this fixes when it is written.
- A session that writes code does not review or commit it. Hand both to a fresh session, because the context that produced the code has already argued itself into believing it correct.

## Completion gate

Run `make check`. Each change that adds a workspace extends that same target in the same change, so local and blocking CI run identical CPU-only checks. Run hardware or GPU suites only when the change or its acceptance criteria require them.
