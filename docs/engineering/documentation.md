# Documentation ownership and lifecycle

Status: **normative**. Read this when adding, rewriting, relocating or deleting documentation and agent instructions. The [document index](../README.md) routes readers; this file defines ownership and maintenance, not product requirements. [workflow.md](workflow.md) owns verification and review.

## One home per fact

| Home | Owns | Keep elsewhere |
|---|---|---|
| Root `README.md` | Project purpose, shortest supported start, document entry | Detailed deployment, duplicated workflow |
| Root `AGENTS.md` | Always-needed invariants and conditional task pointers | Full procedures and repeated reference text |
| `CONTEXT.md` | Domain terms and excluded synonyms | Code layout, task progress |
| `docs/engineering/` | Repository workflow, issues, authoring, ownership, maintenance | Product mechanisms and field claims |
| `docs/design/solution-and-roadmap.md` | Approved product scope, decisions still pending, delivery phases and acceptance | Live task status, copied executable inventories |
| `docs/design/mechanisms/` | Behavior and invariants of each named mechanism | Session transcripts and implementation walkthroughs |
| `docs/adr/` | Architectural decision, alternatives, tradeoffs and consequences | Routine changes or task checklists |
| `docs/deployment/` and asset-local READMEs | Installation, operation, configuration, upgrades, limitations; local assets own their exact usage | Duplicated development policy |
| `docs/research/`, measured facts and base ledger | Dated evidence, environment, inspected revision, uncertainty, base-patch provenance | Claims that historical measurements describe all current environments |
| Source, manifests, generated contracts | Actual schemas, dependencies, parameters, commands and executable checks | Hand-maintained parallel catalogs |
| GitHub Issues/PRs and ignored task handoff | Current work, acceptance ownership, review and execution evidence | Permanent duplicate plans in the repository root |

Code establishes current implementation, not permission to change an approved requirement. When prose and code disagree, classify the discrepancy: obsolete description, approved-but-unimplemented behavior, or code defect. Correct the first; keep the requirement and link the owner for the others. An unmeasured hardware claim stays unmeasured. Q25/Q36 and other unresolved choices stay unresolved until their decision authority acts.

Use the [glossary](../../CONTEXT.md) in Issue titles, test names, proposals and commits. A missing term requires checking for a real domain gap, not inventing a synonym. Name a conflicting ADR and the reason to reopen it; do not silently override it.

## Create, update, merge or split

Before adding a file, search the existing owner and incoming references. Extend that owner unless the material has a distinct durable subject, reader or invocation condition. A new document starts by saying what it owns, when to read it and which source is authoritative. Link it from the document index or a reachable owning document in the same change. Create directories only with their first real content.

Update the owning explanation with the source/behavior change. Other documents link there. A procedure uses prerequisites, ordered operations and observable completion; a reference groups definitions, rules, caveats and source links under the same subject. Separate substantial tutorials from references, but do not create a file for every heading.

Merge documents when one task repeatedly crosses them for the same rule. Reconcile conflicts before merging, rather than concatenating both policies. Split only for distinct scope, reader or reading condition; length is a review signal, not a mandatory file cap. Keep acceptance and failure behavior even when condensing prose.

## Delete or supersede

A deletion is ready when all of these hold:

1. Every surviving rule, decision, fact and unfinished acceptance criterion has an identified owner. Use the PR migration table `old path -> action/new owner -> preserved facts or rationale`; this is task evidence, not a permanent second registry.
2. Search local Markdown links, bare paths, section references, scripts, tests, generated inputs, agent pointers and relevant live Issue/PR bodies. Migrate executable dependencies and active entrypoints in the same change.
3. Preserve historical evidence in Git/PR history and the original decision/evidence record where it has independent value. Do not create a general `archive/` of obsolete operating instructions. ADR identifiers and measured/base verification records are not deleted merely because implementation finished.
4. Run the applicable checks and review the deletion for semantic loss, not just broken links.

When a verified live external consumer cannot be migrated in this change, retain only a routing stub with its current destination, consumer and removal condition. Keep new entrypoints pointed at the real owner. Remove the stub once the consumer is migrated; a speculative future reader is not a reason to retain it.

Temporary audit notes and migration inventories belong in `.tmp/` or the PR. A completed `issue-*-spec.md` is not a second current product specification: migrate durable facts and acceptance ownership, then delete it.

## Write for the reader

Use Chinese for human-facing product, domain and deployment material; English for agent instructions and repository operations. Keep identifiers, commands and protocol fields unchanged. Code comments/docstrings explain non-obvious reasons, constraints and contracts in concise Chinese, not the reasoning transcript.

Describe current mechanisms in present tense; distinguish approved targets and pending decisions explicitly. Keep dated measurements and revision identifiers where they establish evidence. Put historical change narration in PRs, and lasting architectural reasons in ADRs. Use concrete actors, operations and conditions; reserve emphasis for behavior-changing constraints.

Link to local current sources using relative Markdown paths. Avoid copied dependency versions, API inventories, file counts or test counts that source already owns. Examples may show required values, but identify their source and environment. Use real failure/skip semantics; never present a simulated production path or skipped field test as successful acceptance.

## Agent routing

Root `AGENTS.md` is the short entrypoint. One task pointer identifies the guidance and when to load it. Put detailed reference behind those pointers and add a nested `AGENTS.md` only for a genuine subtree exception. `CLAUDE.md` stays `@AGENTS.md`. Do not make every task read the whole product roadmap.

After material instruction/runtime changes, check routing in a fresh session with a documentation task, bounded code task, vendor/generated-contract task and product/architecture task. Confirm each reaches its required owner without unrelated mandatory reading. This is not a new ceremony for every edit.

## Mechanical checks

`make docs-check` runs the offline, standard-library document check. It verifies repository-local inline Markdown targets, heading/explicit-anchor fragments, that local targets belong to the repository inputs rather than ignored machine-only files, and that maintained first-party Markdown is reachable from `README.md` or `AGENTS.md`. Directory links enter that directory's `README.md` when one exists; they do not silently index all descendants. Use inline Markdown links for maintained navigation. Vendor content is excluded from editorial/link enforcement under [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md).

The check is also part of the existing repository policy, hence `make check-docs` and `make check`. It does not crawl external URLs, classify duplicated meaning or decide whether a requirement is obsolete; review owns those judgments. No document database, hash registry, mandatory timestamp refresh, word-count quota or new dependency is needed.

## Transitional links

Open dispatched Issues still refer to [architecture](../design/repository-architecture.md), [authoring](../design/repository-authoring.md) and [verification](../design/repository-verification.md) at their legacy paths. Those files contain only pointers to the engineering owners. Migrate Issue-body links under an authorized tracker update before deleting these three stubs.

The [historical harness map](../design/repository-harness.md) retains only §1–§10 destinations for existing source/test comments. Retire it after those recorded consumers are migrated; neither it nor the three Issue stubs owns rules. The installed review skill also locates [the old tracker entry](../agents/issue-tracker.md); keep that routing pointer until the skill's path contract is updated.

## Reference methods adopted

The inspected [DeepSeek Harness documentation standard](https://github.com/deepseek-ai/deepseek-harness/blob/ddefc45fbc7f8e46dd73185e68295696d1297887/docs/AGENTS.md) informs subject ownership, one-home-per-fact, tutorial/reference separation and local link checks. Its [development guide](https://github.com/deepseek-ai/deepseek-harness/blob/ddefc45fbc7f8e46dd73185e68295696d1297887/docs/development.md) informs colocated daily workflow and CI explanations with scripts as inventory authority. The inspected [Codex instructions](https://github.com/openai/codex/blob/e269f2164cbb9f499e4f22301c393500e2a831f3/AGENTS.md) inform affected-project checks, colocated tests and source/schema updates together.

These are design references, not imported operating policies. NVSOP retains its Make interface, risk-selected evidence, hardware gates, local deployment model and authorized PR workflow. Do not import their language/toolchain choices, external-contribution restrictions, blanket test exceptions, documentation websites, bilingual pairing system or comprehensive budget infrastructure.
