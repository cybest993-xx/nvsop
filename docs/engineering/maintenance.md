# Repository maintenance

Status: **normative**. Read when changing NVIDIA base code, generated contracts, dependencies or locks. [workflow.md](workflow.md) owns checks/review; [upgrade.md](../deployment/upgrade.md) owns deployment compatibility and rollout consequences.

## NVIDIA base code

`vendor/sop-monitoring-blueprints/` changes only through a dedicated subtree update or registered replayable patch within [ADR-0007](../adr/0007-base-is-the-trunk-not-a-dependency.md). Inspect the actual [patches](../base/patches/) and [verification ledger](../base/verified-commits.md); do not maintain another patch-count inventory here. The ledger records verified NVIDIA commits, not a permanent version pin.

```sh
git subtree pull --prefix=vendor/sop-monitoring-blueprints \
  https://github.com/NVIDIA/sop-monitoring-blueprints.git <ref> --squash
```

After an update, inspect the subtree diff and every registered patch. Resolve changes deliberately without silently enlarging patch scope or weakening contract tests. Run `make check`, including both base-assumption and patch/reimplementation regression families under `tests/contract/base/`. Append the inspected NVIDIA commit, actual results and patch adjustments to the ledger; complete the independent review required by workflow.md. A changed architectural premise may require reopening its ADR. Known upstream defects are not authorization for opportunistic patches.

Subtree imports copy Git blobs, not upstream LFS objects. After each update exclude upstream LFS-only assets, or deliberately vendor their real bytes, and remove `filter=lfs` rules. Policy rejects LFS tracking metadata and pointers inside the NVIDIA subtree. Development snapshots strip inherited `GIT_LFS_SKIP_SMUDGE` and refuse any missing tracked LFS object; there is no optional-asset bypass.

## Generated contracts

`make contracts` is the canonical command: export `packages/contracts/openapi.json`, check compatibility against `OPENAPI_BASE_REF` (default `origin/main`), then regenerate the committed Web client. `openapi-export`, `openapi-compat`, `openapi-generate` and Web `generate:api` are constituent steps, not competing supported workflows.

Commit generated output with its source where consumers require it for installation/build. CI checks the complete worktree, including untracked generated files. Preserve fixed-prefix and compatibility rules from ADR-0003 and the upgrade document; do not substitute URL versioning.

## Dependencies and toolchains

Python resolution belongs to `pyproject.toml`, `.python-version` and `uv.lock`; Web resolution to `package.json`, `.nvmrc`, `pnpm-workspace.yaml` and `pnpm-lock.yaml`. Keep the exact package-manager pin aligned. A dependency change and lockfile update land together.

Use frozen installs through repository commands, not ad-hoc package installs documented as an alternative. A runtime/toolchain bump includes code compatibility, generated artifacts, CI and deployment evidence in the same coherent change. Select affected checks through workflow.md and retain all applicable release requirements.
