# Repository maintenance

Status: **normative**. Read when adding or changing repository-local generated/ignored state, NVIDIA base code, generated contracts, dependencies or locks. [workflow.md](workflow.md) owns checks/review; [upgrade.md](../deployment/upgrade.md) owns deployment compatibility and rollout consequences.

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

Prefer a lightweight maintained library over hand-written infrastructure such as HTTP clients, retries, parsing or UI widgets. A new runtime dependency ships prebuilt wheels or is pure Python, pulls few transitive packages, is actively maintained under a license compatible with distribution, and its reason is commented beside the declaration. Reuse a package already locked in `uv.lock` or `pnpm-lock.yaml` before adding an alternative. `apps/edge-runtime/pyproject.toml` is the reviewed list of edge third-party packages; `scripts/check_repo_policy.py` rejects undeclared edge imports, and the judgment core and vendor-hook boundary stay standard-library-only.

Use frozen installs through repository commands, not ad-hoc package installs documented as an alternative. A runtime/toolchain bump includes code compatibility, generated artifacts, CI and deployment evidence in the same coherent change. Select affected checks through workflow.md and retain all applicable release requirements.

隔离的基座运行环境由部署所有者维护独立的完整运行依赖锁，不因根工作区锁通过就视为已验证。标注后端的基座 requirements 与 [`annotation-constraints.txt`](../../deploy/dev/annotation-constraints.txt) 经 `make annotation-lock` 生成 [`annotation-requirements.lock`](../../deploy/dev/annotation-requirements.lock)；`make lockfile` 同时核对该锁，安装只消费已解析的固定版本并执行兼容性检查。输入、锁、构建与实际启动证据随部署变更一起维护，具体运行命令与冻结边界见[标注组合证据](../deployment/annotation-evidence.md)。

GitHub Actions syntax uses the repository-pinned `actionlint` version and upstream SHA256 values in `scripts/install_actionlint.py`. `make ci-tools` is the explicit network/bootstrap step; `make ci-lint` never installs or silently substitutes another version. Update version, supported architecture checksums, installer behavior and CI evidence together.

## Repository-local state

Supported repository commands place repository-owned local state under the ignored `.nvsop/` root:

- `venv/` owns the frozen Python environment used by Make targets and hooks;
- `cache/` owns Ruff, mypy, pytest and import-linter caches; mypy/pytest use separate target subdirectories for parallel checks;
- `tools/` owns explicitly bootstrapped repository tools such as `actionlint`;
- `artifacts/` owns disposable local build and test output;
- `dev-main/` owns the fixed development instance, including local credentials, TLS material and Docker secret files.

Classify every new path before creating it. Durable source, configuration, documentation, test assets and other repository content stay tracked under their existing owner. Repository-owned untracked state created by supported commands belongs under `.nvsop/`. A new repository-owned ignored path outside `.nvsop/` is an explicit exception: use it only when the responsible tool requires that layout, record the reason and exact cleanup ownership here, and update the policy allowlists only when that path is intentionally safe to preserve. `.tmp/task-handoff.md` is the sole repository-defined continuity exception and is created only under [workflow persistent continuity](workflow.md#persistent-continuity). User-supplied secrets remain governed by their documented configuration/security paths rather than being treated as generated state.

uv's package cache is a tool-owned shared-cache exception, like the pnpm store: Make honors `UV_CACHE_DIR`, defaulting to `$XDG_CACHE_HOME/uv` or `$HOME/.cache/uv`. Sharing this cache avoids downloading/building identical dependencies per worktree. It never shares the installed `.nvsop/venv`: editable installs belong to one worktree and must not be copied or linked to another. Resume the existing task environment with `make sync`; do not recreate it for each session. Cache eviction belongs to an explicit operator-run `uv cache prune` using the same cache location, never task cleanup.

`make local-clean` removes only declared build/test outputs. It preserves the environment, tool and analysis caches, `node_modules/` and editable metadata. After writers stop, `make local-purge` additionally removes those task-local reproducible paths and legacy generated paths before task cleanup. Neither command deletes the shared external package cache, `.nvsop/dev-main/`, `.env*`, key material, `.tmp/task-handoff.md` or unknown ignored state. A legacy cache symlink is unlinked, not traversed. Do not replace these commands with `git clean -fdx`.

pnpm still requires workspace `node_modules/` layout for the current Vue toolchain, and editable Python installs can write `*.egg-info/` beside sources. These are explicit tool-layout exceptions rather than a second state root; `make local-purge` owns their removal. Direct tool invocation may also recreate legacy ignored cache paths, but supported Make commands use the locations above.

The primary `main` checkout synchronizes to accepted `origin/main` only through the fast-forward procedure owned by [workflow.md](workflow.md#51-confirm-the-exact-squash-merge). Tracked, staged and ordinary untracked changes block it; ignored personal configuration and caches that the update does not touch are preserved as they are and do not block it, and synchronization stops when the update would overwrite an ignored path. Task cleanup remains stricter and requires a fully clean task worktree, so run `make local-purge` before cleanup when reproducible artifacts are present.
