# Repository maintenance

Status: **normative**.

This document owns NVIDIA base maintenance, generated contracts and dependency/lockfile maintenance. Architecture boundaries are in [`repository-architecture.md`](repository-architecture.md); verification gates are in [`repository-verification.md`](repository-verification.md).

## NVIDIA base code

`vendor/sop-monitoring-blueprints/` is the NVIDIA base code that forms NVSOP's trunk. It changes only through a dedicated subtree update or a registered replayable patch within [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md).

The current registered patch set is maintained under [`docs/base/patches/`](../base/patches/). At the time of this document:

- `0001-stream-health-events.patch` — the inference pipeline health-event hook.
- `0002-annotation-upload-target-and-accessibility.patch` — the annotation integration compatibility patch.
- `0003-drop-unavailable-lfs-assets.patch` — removes upstream LFS-only documentation assets and LFS tracking metadata that subtree import cannot hydrate.

The ledger [`docs/base/verified-commits.md`](../base/verified-commits.md) records NVIDIA commits that have been verified. It is **not** a statement that the repository is permanently pinned to the last listed commit.

The vendored NVIDIA subtree must not depend on NVIDIA Git-LFS storage: `git subtree` imports Git blobs but does not copy LFS objects into the NVSOP LFS endpoint. After every subtree update, exclude upstream LFS-only assets (or deliberately vendor their real bytes) and remove `filter=lfs` rules before the update is accepted. Repository policy rejects both LFS tracking metadata and LFS pointer files under `vendor/sop-monitoring-blueprints/`.
The development snapshot path is strict as well: it strips inherited `GIT_LFS_SKIP_SMUDGE` and refuses to create a snapshot when any tracked LFS object is missing; there is no optional-asset bypass.

### Update procedure

Use the ledger's canonical subtree shape:

```sh
git subtree pull --prefix=vendor/sop-monitoring-blueprints \
  https://github.com/NVIDIA/sop-monitoring-blueprints.git <ref> --squash
```

Then:

1. Inspect the subtree diff and both registered patch surfaces; do not resolve conflicts by silently expanding patch scope.
2. Run `make check`, which includes `tests/contract/base/`.
3. If a base assumption changed, either adapt NVSOP deliberately or reopen the architecture decision; do not weaken a contract test just to make the update pass.
4. Append the NVIDIA commit, contract result and patch-adjustment outcome to `docs/base/verified-commits.md`.
5. Run the independent review required for `vendor/`/dependency changes.

Known NVIDIA defects recorded in the verification ledger remain registered facts. Do not opportunistically patch them unless the task explicitly changes the accepted patch boundary.

## Generated contracts

`make contracts` is the canonical generated-contract command. It exports `packages/contracts/openapi.json`, checks compatibility against `OPENAPI_BASE_REF` (default `origin/main`) and regenerates the committed Web client.

The `openapi-export`, `openapi-compat`, `openapi-generate` targets and Web `generate:api` script are implementation steps, not alternate documented workflows.

CI regenerates and checks the complete worktree, including untracked output. Commit generated output in the same change as its source when consumers need it during install/build.

The control-plane prefix `/api/v1` is fixed and is not a version axis; compatibility rules are in [ADR-0003](../adr/0003-api-v1-is-a-fixed-prefix.md) and the operational upgrade consequences are in [`../deployment/upgrade.md`](../deployment/upgrade.md).

## Dependency and toolchain changes

- Python resolution is owned by `pyproject.toml`, `.python-version` and `uv.lock`; a dependency change updates its lockfile in the same change.
- Web resolution is owned by `package.json`, `.nvmrc`, `pnpm-workspace.yaml` and `pnpm-lock.yaml`; keep the exact `packageManager` pin and lockfile aligned.
- CI and developer commands use frozen installs. Do not document an ad-hoc package install as a supported workflow when the repository lockfiles should own it.
- A runtime/toolchain bump carries its code compatibility, generated output, CI and deployment evidence in the same coherent change.

## Reference patterns

The repository structure was informed by the official OpenAI Codex repository and its AGENTS guidance: a short always-loaded instruction entry point, subsystem/detail guidance behind task-specific pointers, one stable task interface, package-owned tests and change-aware CI. NVSOP adopts the transferable structure, not Codex-specific Rust or UI conventions.

The same routing principle is used for NVSOP documentation: [`../README.md`](../README.md) is the engineering-doc index, while root `AGENTS.md` retains only universal invariants and task triggers. NVSOP source, manifests, ADRs and tests remain authoritative over any external reference repository.
