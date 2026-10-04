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
- `artifacts/` owns local build/test output plus the landing repair continuity state described below;
- `dev-main/` owns the fixed development instance, including local credentials, TLS material and Docker secret files.

Classify every new path before creating it. Durable source, configuration, documentation, test assets and other repository content stay tracked under their existing owner. Repository-owned untracked state created by supported commands belongs under `.nvsop/`. A new repository-owned ignored path outside `.nvsop/` is an explicit exception: use it only when the responsible tool requires that layout, record the reason and exact cleanup ownership here, and update the policy allowlists only when that path is intentionally safe to preserve. `.tmp/task-handoff.md` is the sole repository-defined continuity exception and is created only under [workflow persistent continuity](workflow.md#persistent-continuity). User-supplied secrets remain governed by their documented configuration/security paths rather than being treated as generated state.

uv's package cache is a tool-owned shared-cache exception, like the pnpm store: Make honors `UV_CACHE_DIR`, defaulting to `$XDG_CACHE_HOME/uv` or `$HOME/.cache/uv`. Sharing this cache avoids downloading/building identical dependencies per worktree. It never shares the installed `.nvsop/venv`: editable installs belong to one worktree and must not be copied or linked to another. Resume the existing task environment with `make sync`; do not recreate it for each session. Cache eviction belongs to an explicit operator-run `uv cache prune` using the same cache location, never task cleanup.

`make local-clean` removes only declared build/test outputs. It preserves the environment, tool and analysis caches, `node_modules/` and editable metadata. After writers stop, `make local-purge` additionally removes those task-local reproducible paths and legacy generated paths before task cleanup. Neither command deletes the shared external package cache, `.nvsop/dev-main/`, `.env*`, key material, `.tmp/task-handoff.md` or unknown ignored state. A legacy cache symlink is unlinked, not traversed. Do not replace these commands with `git clean -fdx`.

pnpm still requires workspace `node_modules/` layout for the current Vue toolchain, and editable Python installs can write `*.egg-info/` beside sources. These are explicit tool-layout exceptions rather than a second state root; `make local-purge` owns their removal. Direct tool invocation may also recreate legacy ignored cache paths, but supported Make commands use the locations above.

The primary `main` checkout synchronizes to accepted `origin/main` only through the fast-forward procedure owned by [workflow.md](workflow.md#51-confirm-the-exact-squash-merge). Tracked, staged and ordinary untracked changes block it; ignored personal configuration and caches that the update does not touch are preserved as they are and do not block it, and synchronization stops when the update would overwrite an ignored path. Task cleanup remains stricter and requires a fully clean task worktree apart from the exact validated session binding, so run `make local-purge` before cleanup when reproducible artifacts are present; the binding is released only by the verified merged or explicitly abandoned single-task cleanup itself.

### Session binding state

`.nvsop/session-binding.json` is the one durable ignored file that maps a task worktree to the implementation session that owns it. The bound id is the primary task-owner session — the resumable main-owner id — never an ephemeral worker or subagent id. It is a versioned four-key object:

```json
{"version": 1, "session_id": "<resumable-id>", "branch": "agent/<owner>/<task>", "worktree": "<canonical absolute task worktree>"}
```

`make task-session-bind SESSION=<resumable-id>` ([scripts/bind_task_session.py](../../scripts/bind_task_session.py)) creates it with exclusive create and mode `0600`; the same session is idempotent, while another session, a malformed/unreadable/symlink file or a branch/worktree mismatch fails closed, and there is no overwrite or force path. It carries no provider, model, agent-name, token or permission fields. It is context lookup only, never landing authorization; the ownership and repair rules are owned by [workflow.md](workflow.md#landing-ownership-and-session-binding).

The task's ordinary ignored state (`.nvsop/venv`, caches, tools, artifacts) still blocks cleanup until `make local-purge` removes it. `make local-clean` and `make local-purge` preserve the binding; only the verified merged cleanup of [workflow.md §5.2](workflow.md#52-clean-one-verified-task-worktree-and-local-branch) or the explicitly abandoned/superseded cleanup of [workflow.md §5.3](workflow.md#53-clean-one-explicitly-abandoned-or-superseded-task) releases it.

### Landing queue state

The automatic landing queue keeps its durable state in the repository, not in local or server-side service state. A dedicated `landing-queue-state` branch holds one `queue.json` (version 3: `consumed_request_id` plus entries); the trusted [landing-queue.yml](../../.github/workflows/landing-queue.yml) controller is its only writer and updates it with a Contents-API blob-SHA compare-and-swap. The consumed-inbox high-water id lives in the same object as the entries, so the CAS write makes consumption and the transition atomic. Enqueue/dequeue wake requests are append-only comments on a single dedicated inbox issue, ordered by server comment id with the PR number as tie-break. `scripts/landing_queue.py` ([workflow.md](workflow.md#landing-queue-and-single-flight-ci)) is the owner.

Authorization is not a boolean in the state: each entry stores the id, body digest and author of an existing human-maintainer `<!-- landing-attestation -->` comment on the PR, and every integration action re-reads it so an edited or deleted authority blocks. The public state, comments and Actions inputs/checks carry no runtime session identity and no worktree path; the local `.nvsop/artifacts/landing/<handoff>.json` receipt maps the opaque handoff id to local context and is ordinary ignored task state. The inbox issue number is read from the `LANDING_QUEUE_ISSUE` repository variable; the landing App id and bot login come from the `LANDING_APP_ID`/`LANDING_APP_BOT` variables, and the private key is the `LANDING_APP_PRIVATE_KEY` secret in the protected `landing` GitHub Environment restricted to `main`. None of these are committed. The controller fails closed when the branch, inbox issue, App variables/secrets or the App-bound `CI required`/`Landing gate` ruleset protections are missing; repository code does not create or claim server settings. Before the App variables are set, a deployment guard skips the controller's App-bound jobs so a PR or `push main` event does not request the protected environment or secret. Once configured, the controller invokes [blocking-ci.yml](../../.github/workflows/blocking-ci.yml) only through its reusable `workflow_call` interface.

The per-blocked-generation `.nvsop/artifacts/landing/<64-hex event>.resume` one-shot marker (`O_CREAT|O_EXCL`, mode `0600`) reserves the single host resume; it is ordinary ignored task state. The standalone repair service also keeps `.nvsop/artifacts/landing/<64-hex event>.claim` as the `0600` plaintext claim for restart recovery and `.nvsop/artifacts/landing/<64-hex event>.claim-requested` as a `0600` local receipt after the public claim request is posted. The receipt suppresses repeated claim comments while the controller state lags, including across service restarts. These files never enter GitHub state or tracked source. `make local-clean` preserves the complete `.nvsop/artifacts/landing/` subtree because its receipt/claim/marker state is not reproducible while a repair generation is live; `make local-purge` removes it after writers stop, before verified task cleanup. Malformed, symlinked or group/world-readable claim files fail closed.
