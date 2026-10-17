# Web frontend authoring

Status: **normative**. This document owns the Web UI's visual language, its Chinese type, its component layering and its layout invariants. Read it when building, restyling or deleting anything under `apps/control-web/src`. [coding.md](coding.md) owns authoring in general and still applies; [architecture.md](architecture.md) owns module ownership and dependency direction; [workflow.md](workflow.md) owns evidence and delivery. Where this document and the code disagree, classify the discrepancy the way [documentation.md](documentation.md) requires rather than assuming the code is right.

## The visual language

The product is a control surface an operator watches and drives for a shift, on a desktop, at 1366×768 or larger. The layout commits to one idea: **a dark sidebar against a light content area**. The sidebar is where the operator is — which section, which account, which session. The content area is where the work is — tables, forms, evidence. The contrast between the two is the page's structure, and it is the one bold move; everything inside the content area stays quiet so the data reads.

A panel carries a dark header bar, because a panel is a piece of the same system rather than a white box that happens to sit on a grey page. A table carries a tinted header row, which separates the column labels from the data without a second border.

Every colour, radius and type step comes from `src/styles/tokens.css`. That file is the single source; this document describes what each token is *for* and never restates a value. A component composes tokens and shared components. When a colour needs to change, it changes in the token file, and every surface follows.

Status colour means status. It appears on state — verified, reachable, failed, disabled — and nowhere else. An inventory count is not a state, so a count is never painted green: an operator should not have to work out whether a number is a measurement or a verdict.

## Chinese type

The interface is Simplified Chinese (Q32). Chinese sets differently from Latin, and the difference is not cosmetic.

**Line height runs looser than Latin.** Chinese glyphs fill their em box; Latin ascenders and descenders leave air. The body step is set looser than a Latin page's for that reason, and `base.css` applies it once so components inherit it.

**The type scale has five steps**, and a component picks one rather than inventing a size. The values live in `tokens.css`; this table says which step a given piece of text takes.

| Step | Tokens | Used for |
|---|---|---|
| Page title | `--font-size-title`, `--line-height-title` | The one `h1` per page |
| Panel title | `--font-size-panel`, `--line-height-panel` | `h2` inside a panel header |
| Body | `--font-size-body`, `--line-height-body` | Running text, table cells, field values |
| Secondary | `--font-size-secondary`, `--line-height-secondary` | Hints, help text, sub-values |
| Label | `--font-size-label`, `--line-height-label` | Table headers, small tags, badges |
| Metric | `--font-size-metric`, `--line-height-metric` | The one large figure on a summary card |

**Letter spacing stays at zero for body text.** Chinese characters are already set on a fixed rhythm; spacing them out breaks the rhythm rather than adding emphasis. `--letter-spacing-label` is the ceiling, and it is only for the label step.

**Numerals align in columns.** Any figure that appears in a column — counts, revisions, byte sizes, durations — takes `font-variant-numeric: tabular-nums`. Proportional digits make a table's numbers ragged for no gain.

**Chinese and Latin set in different faces.** The stack in `base.css` puts a Simplified-Chinese face ahead of the Latin fallbacks so each script renders in the face drawn for it. A Latin-only face first would set Chinese in a fallback anyway, with the Latin numerals mismatched against it.

**Punctuation follows the Chinese convention.** Full-width marks in Chinese text; no space inserted around them by hand. Line breaking respects the prohibition rules (no opening bracket at line end, no closing bracket at line start) — the browser does this correctly when the text is plain and `word-break` is left alone.

The reference the visual language was derived from is a dark-sidebar admin console. Where it conflicts with the rules above, the rules win, and the three conflicts are known: it marks section headings in all caps (Chinese has no case — this product uses size, weight and a colour bar instead); it uses icon-only buttons (this product gives every icon control an accessible name); it signals confirmation with a red tick alone (this product states the state in words as well).

A live specimen of the palette, the type steps, the states and the component shapes is in [frontend-reference.html](frontend-reference.html). Open it in a browser: it links `tokens.css` rather than restating it, so it shows the values the product is using now, and it cannot drift from them.

## The theme layer

`src/styles/naive-theme.ts` is the only place the component library's theme is overridden. A component that needs a different colour, radius or density adds it there, keyed by the library's own theme contract. Naive UI needs concrete colours rather than `var(--…)` because it derives its hover, pressed and subtle shades arithmetically, so that file and `tokens.css` are a pair — a change to a brand colour belongs in both. Reading the computed value at runtime is not an option: unit tests run with CSS processing off, so it would come back empty.

A rule that reaches into the library's internal class names is not a theme override. If a component seems to need one, the shared component that should own the appearance is missing.

## Component layering

| Directory | Owns |
|---|---|
| `src/shell/` | The application frame: sidebar, account strip, the region that scrolls |
| `src/components/` | Presentation shared across features |
| `src/modules/<feature>/` | One feature's views and panels |
| `src/styles/` | Tokens, theme overrides, the global baseline |
| `src/api/` | The only door to the control plane |

A shared component is one **two real callers** need. That is the same bar `packages/` has, and it is applied the same way: build it in the feature that needs it first, and move it to `src/components/` when the second caller arrives. A component with one caller that lives in `src/components/` is a guess about the future wearing the clothes of a shared component.

A feature slice holds its own views and its own panels. It reaches shared presentation through `src/components/`, data through `src/api/`, and identity through `src/session/store.ts`.

Two things about Naive UI shape every component that uses it, and both fail silently if missed:

- **`v-model` does not bind.** Naive UI's inputs take `value` / `onUpdateValue`, so a field binds with `v-model:value` (`v-model:checked` for a checkbox, `v-model:show` for a modal). A bare `v-model` binds `modelValue`, which lands as an inert attribute on the wrapper element. `vue-tsc` does not report it.
- **Native attributes belong to the inner control.** `name`, `autocomplete`, `type` and `inputmode` on `NInput` land on the wrapper `div`, where the browser's form machinery never sees them. They go in `:input-props`, which also carries the `id` that associates the label.

## Data and state

`src/api/` is the only door to the control plane. `eslint.config.ts` enforces this for production code; the one exception is the browser reading runtime media straight from the owning inference host, recorded in [ADR-0012](../adr/0012-media-ownership-and-center-training-files.md) and carried as a line-scoped disable comment in the component that does it.

The session lives in `src/session/store.ts`. Nothing else reads or writes it, and nothing stores a token: the cookie is `HttpOnly`, so `localStorage` is refused by lint for the same reason §六 refuses it.

A feature renders what the caller may see. Navigation and the router guard both filter on the caller's permissions, and a section the caller holds no permission for is not rendered at all — "there but refused" is a dead end, not a warning.

## Accessibility and the fixed shell

Q32 sets the floor: keyboard operable, every field labelled, errors announced, and colour never the only expression of a state. `eslint-plugin-vuejs-accessibility` and `vue-tsc` hold part of it; the rest is this section.

**Every form field is built from `FieldRow`.** Naive UI's `NFormItem` renders its `<label>` with no `for` and sets no `aria-*`, so a field built from it directly has no accessible name. `FieldRow` derives ids for the label and the control and hands both to its slot, so the association cannot be forgotten. A control with an inner input takes `id` through `:input-props` and is named by the label's `for`; a control that renders no id-bearing element — `NSelect` without `filterable`, a radio or checkbox group — takes `label-id` as `aria-labelledby` instead. `NFormItem` is not used directly anywhere in `src/`.

**An icon-only control carries an accessible name.** The reference's eye and gear buttons are text controls here, or they are icons with `aria-label`.

**A state is stated in words as well as colour.** A status tag pairs its tint with a dot and a label; a disabled section says why; the current navigation item is marked by a rule and a weight change, not by hue alone.

**The shell is fixed; the panel area scrolls.** The document never scrolls vertically and never scrolls horizontally. `AppShell` fills the viewport, the page title, its actions and the counts stay put, and the panel area is the application's only scrolling region. An operator reading a table should not have the surface move under them, and a page whose header scrolls away makes them re-find their place.

**1366×768 is the floor and 1920×1080 the ceiling.** Nothing overflows horizontally at either. A wide table scrolls inside its own container, never the page.

## Checklist

Work the branch that matches the change. Each branch ends on a checkable bound.

**Adding a page.** Register the route with its `requires` permissions and its title. Compose the page from `PageHeader`, `Panel`, `Toolbar`, `DataTable` and the state components rather than writing the equivalents. Take every colour and size from tokens. Finish when the page renders at both viewports with no document scroll and no horizontal overflow, every field has an accessible name, and the views and markup tests cover it.

**Adding a shared component.** Confirm two real callers exist. Give it a slot-based interface rather than props that mirror one caller's shape. Give every icon-only control an accessible name. Finish when both callers render through it and no caller keeps its own copy.

**Changing the look.** Change `tokens.css` for a value, `naive-theme.ts` for a library component's appearance. Finish when no component file carries the old value as a literal and both viewports still pass.

**Adding a dependency.** Read [maintenance.md](maintenance.md) first. Land the manifest and `pnpm-lock.yaml` together. Finish when the lockfile check passes and the build's chunk sizes are recorded against the previous run.

**Deleting frontend code.** Search for the feature's routes, tests, e2e flows and `requires` permission strings. Finish when the route, its tests, its snapshot entries and any navigation entry are gone, and nothing references the deleted symbols.

## Mechanical checks

Some of the above is enforced by machines; the rest is enforced by review, and the split is deliberate so a reviewer knows what has already been checked.

| Rule | Enforced by |
|---|---|
| Colours and font sizes come from tokens | `scripts/check_repo_policy.py` |
| Theme overrides live only in `naive-theme.ts` | `scripts/check_repo_policy.py` |
| Every form control has an accessible name | Integration test |
| The document does not scroll; nothing overflows horizontally | Playwright, both viewports |
| Text contrast reaches AA | Playwright with axe-core |

Three rules cannot be mechanised and are the reviewer's: whether a colour is carrying meaning rather than decorating, whether a type step's value suits the text it sets, and whether a shared component's two callers are real. They are listed here so that "no machine complained" is never mistaken for "this was checked".
