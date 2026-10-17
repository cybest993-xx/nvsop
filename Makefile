.PHONY: check check-suite check-docs docs-check check-integration media-system edge-bench change-size task-check ci-plan ci-tools ci-lint pr-check issue-check hooks local-clean local-purge lockfile sync policy policy-test migrations contract-base \
	contract-capability \
	contracts contracts-python-check contracts-python-format contracts-python-lint \
	contracts-python-type contracts-python-unit openapi-export openapi-compat openapi-generate \
	boundaries secret-scan center-format center-lint center-type center-unit center-integration \
	center-system edge-format edge-lint edge-type edge-unit edge-integration \
	web-install web-format web-lint web-type web-unit web-e2e web-e2e-whep web-build \
	dev-setup dev dev-status dev-logs dev-refresh dev-smoke dev-test-ui dev-down

.PHONY: annotation-lock annotation-lock-check annotation-image training-lock training-lock-check training-build-inputs
.PHONY: task-merge task-cleanup task-abandon

LOCAL_STATE := $(CURDIR)/.nvsop
LOCAL_CACHE := $(LOCAL_STATE)/cache
LOCAL_ARTIFACTS := $(LOCAL_STATE)/artifacts
LOCAL_TOOLS := $(LOCAL_STATE)/tools
UV_PROJECT_ENVIRONMENT := $(LOCAL_STATE)/venv
# 包缓存跨 worktree 复用；已安装的 editable 环境仍由当前 worktree 独占。
UV_CACHE_DIR ?= $(or $(XDG_CACHE_HOME),$(HOME)/.cache)/uv
export UV_PROJECT_ENVIRONMENT UV_CACHE_DIR
export PYTHONDONTWRITEBYTECODE := 1
export RUFF_CACHE_DIR := $(LOCAL_CACHE)/ruff
CHECK_JOBS ?= 2
# 并发硬上限：无论调用方把 CHECK_JOBS/PYTEST_WORKERS 设成多大，实际并发都不超过上限，
# 避免误设的并行把主机压死；需要更大并发时显式提高对应的 *_MAX。
CHECK_JOBS_MAX ?= 4
PYTEST_WORKERS_MAX ?= 4
# pytest 并行：默认按核数分发 worker；容器/网关套件按文件分组（--dist loadfile），
# 让同一文件的固定端口与模块级夹具留在同一 worker。0 表示进程内串行。
PYTEST_WORKERS ?= auto
CHECK_JOBS_EFFECTIVE = $(shell python3 scripts/concurrency_limit.py CHECK_JOBS "$(CHECK_JOBS)" "$(CHECK_JOBS_MAX)")
PYTEST_WORKERS_EFFECTIVE = $(shell python3 scripts/concurrency_limit.py PYTEST_WORKERS "$(PYTEST_WORKERS)" "$(PYTEST_WORKERS_MAX)")

# 安装与生成先完成，再由同一个 Make jobserver 限制检查并发。
check: sync hooks contracts
	+$(MAKE) --jobs=$(CHECK_JOBS_EFFECTIVE) --output-sync=target check-suite

# check 的内部执行阶段；单项检查继续保留自己的最小入口。
check-suite: policy-test policy migrations contract-base contract-capability \
	contracts-python-check \
	boundaries secret-scan center-format center-lint center-type center-unit \
	edge-format edge-lint edge-type edge-unit edge-integration \
	web-format web-lint web-type web-unit web-build

# 文档内循环只需本机 Python 标准库和 Git；最终门禁仍使用 check-docs/check。
docs-check:
	python3 scripts/check_repo_policy.py --docs-only

# 文档快线复用冻结工具、仓库政策（含 Markdown 链接）与敏感信息扫描。
# 本地省略 HEAD 时检查从 BASE 到工作区的差异；CI 传入实际比较提交。
check-docs: sync hooks
	+$(MAKE) policy secret-scan
	git diff --check "$(BASE)" $(if $(HEAD),"$(HEAD)") --

# 轻量 hooks：提交时检查 trunk/vendor 与暂存 Python 格式；推送时拒绝 main。
# 完整验证属于 check/CI，不在 hooks 中重复运行。
hooks:
	git config core.hooksPath scripts/githooks

# 日常只清输出；任务清理时才清除独占环境、工具和旧缓存。
local-clean:
	python3 scripts/clean_local_artifacts.py

local-purge:
	python3 scripts/clean_local_artifacts.py --purge

# 从干净的主工作树执行：原生 squash merge 成功后立即清理此 PR 对应的本地任务。
task-merge:
	@test -n "$(PR)" && test -n "$(BRANCH)" && test -n "$(CANDIDATE)" || (echo "usage: make task-merge PR=<number> BRANCH=agent/<owner>/<task> CANDIDATE=<sha>" >&2; exit 2)
	@branch="$$(git symbolic-ref --quiet HEAD)" || { echo "task-merge cannot verify primary HEAD" >&2; exit 2; }; test "$$branch" = "refs/heads/main" || { echo "task-merge requires primary main" >&2; exit 2; }
	@status="$$(git status --porcelain=v1 --untracked-files=all)" || { echo "task-merge cannot verify primary status" >&2; exit 2; }; test -z "$$status" || { echo "task-merge requires a clean primary main checkout" >&2; exit 2; }
	python3 scripts/cleanup_task.py --preflight-merge --pr "$(PR)" --branch "$(BRANCH)" --candidate "$(CANDIDATE)"
	gh pr merge "$(PR)" --squash --match-head-commit "$(CANDIDATE)"
	$(MAKE) task-cleanup PR="$(PR)" BRANCH="$(BRANCH)" CANDIDATE="$(CANDIDATE)"

# 单任务已合并验证后自动 purge 并收尾，不扫描其他工作树。
task-cleanup:
	test -n "$(PR)" && test -n "$(BRANCH)" && test -n "$(CANDIDATE)" || (echo "usage: make task-cleanup PR=<number> BRANCH=agent/<owner>/<task> CANDIDATE=<sha>" >&2; exit 2)
	python3 scripts/cleanup_task.py --pr "$(PR)" --branch "$(BRANCH)" --candidate "$(CANDIDATE)"

# 清理一个已被 accepted main 明确取代的本地任务；不扫描、不 force、不推断 supersession。
task-abandon:
	test -n "$(BRANCH)" && test -n "$(CANDIDATE)" && test -n "$(REPLACED_BY)" || (echo "usage: make task-abandon BRANCH=agent/<owner>/<task> CANDIDATE=<sha> REPLACED_BY=<accepted-main-sha> [PR=<closed-pr>]" >&2; exit 2)
	python3 scripts/cleanup_task.py $(if $(strip $(PR)),--pr "$(PR)",) --branch "$(BRANCH)" --candidate "$(CANDIDATE)" --replaced-by "$(REPLACED_BY)"

# 两个 pytest 会话分别拥有容器和缓存，不在同一数据库上并发清表。
check-integration: sync
	+$(MAKE) --jobs=$(CHECK_JOBS_EFFECTIVE) --output-sync=target center-integration center-system

# 固定 digest 的真实 MediaMTX 录像/回放证据；夹具使用独立 Compose project 并在结束时清理。
media-system: sync
	$(PYTHON) scripts/test_media_playback.py -- $(PYTEST) tests/system/test_sys_34_media.py -q

# Harness §5 的规模提示：省略 HEAD 时统计已提交、暂存、未暂存及未跟踪文件。
# CI 传入 BASE 与 HEAD，只读取该 PR 的固定提交。
BASE ?= origin/main
HEAD ?=
change-size:
	python3 scripts/check_change_size.py "$(BASE)" $(if $(HEAD),"$(HEAD)")

# 显式约定范围/预算的任务才使用此检查；--check 要求完整 40hex base 且为 HEAD 祖先。
# ALLOW 与 MAX_LINES 由主调度按真实用户授权确定，worker 不得自行扩大或移动基线。
# 可信入口：脚本路径取自当前 Makefile 同目录，而非候选 cwd 的相对路径。主调度以
# `make -C <candidate> -f <trusted-checkout>/Makefile task-check ...` 运行：cwd 是候选、
# 脚本来自可信 checkout，候选内同名伪造脚本不会被执行，也不会误查可信目录的空 diff。
MAX_LINES ?= 800
TASK_CHECK ?= $(dir $(abspath $(firstword $(MAKEFILE_LIST))))scripts/check_change_size.py
task-check:
	python3 "$(TASK_CHECK)" --check \
		$(foreach path,$(ALLOW),--allow "$(path)") \
		--max-lines $(MAX_LINES) "$(BASE)" $(if $(HEAD),"$(HEAD)")

# 与 CI 使用同一个路径选择器，只读说明固定 base/candidate 会运行哪些 lane。
ci-plan:
	python3 scripts/ci_scope.py "$(BASE)" "$(if $(HEAD),$(HEAD),HEAD)"

ACTIONLINT_VERSION := 1.7.12
ACTIONLINT := $(LOCAL_TOOLS)/actionlint-$(ACTIONLINT_VERSION)/actionlint

# 工具安装和 lint 分开：安装显式联网且校验官方 SHA256；ci-lint 本身只读取本地固定版本。
ci-tools:
	python3 scripts/install_actionlint.py "$(ACTIONLINT)"

ci-lint:
	test -x "$(ACTIONLINT)" || (echo "ci-lint requires actionlint $(ACTIONLINT_VERSION); run 'make ci-tools' once" >&2; exit 1)
	"$(ACTIONLINT)" -shellcheck= -pyflakes=

pr-check:
	test -n "$(PR)" || (echo "usage: make pr-check PR=<number>" >&2; exit 2)
	python3 scripts/check_pr_readiness.py "$(PR)"

issue-check:
	test -n "$(ISSUE)" || (echo "usage: make issue-check ISSUE=<number>" >&2; exit 2)
	python3 scripts/check_issue_readiness.py "$(ISSUE)"

UV ?= $(or $(shell command -v uv 2>/dev/null),$(HOME)/.local/bin/uv)

# A dependency change and its lockfile update land together. Frozen checks refuse stale locks.
lockfile: annotation-lock-check training-lock-check
	$(UV) lock --check

# 标注解释器与根工作区隔离，锁由基座 requirements 和部署约束共同生成。
annotation-lock:
	python3 scripts/annotation_dependencies.py --uv "$(UV)" --write

annotation-lock-check:
	python3 scripts/annotation_dependencies.py --uv "$(UV)"

training-lock:
	python3 scripts/training_dependencies.py --uv "$(UV)" --write

training-lock-check:
	python3 scripts/training_dependencies.py --uv "$(UV)"

training-build-inputs: training-lock-check
	python3 scripts/training_dependencies.py --prepare

annotation-image: sync annotation-lock-check
	$(PYTHON) scripts/test_annotation_image.py $(if $(NVSOP_DEV_BUILD_NETWORK),--build-network "$(NVSOP_DEV_BUILD_NETWORK)")

# One frozen environment for the whole gate.
sync: lockfile
	$(UV) sync --frozen --all-packages

VENV := $(UV_PROJECT_ENVIRONMENT)/bin
# 路径可能含空格（worktree 名）：命令里对可执行文件与缓存/产物参数加引号，不做其它语义改动。
PYTHON := "$(VENV)/python"
RUFF := "$(VENV)/ruff"
MYPY := "$(VENV)/mypy"
PYTEST_ARGS ?= --durations=20
PYTEST = "$(VENV)/pytest" -o cache_dir="$(LOCAL_CACHE)/pytest/$@" \
	--junitxml="$(LOCAL_ARTIFACTS)/pytest/$@.xml" $(PYTEST_ARGS)
CONTRACT_PY := packages/contracts
OPENAPI := $(CONTRACT_PY)/openapi.json
OPENAPI_BASE_REF ?= origin/main

# Canonical generated-contract command (harness §8): export from FastAPI, reject breaking
# evolution against the PR base, then regenerate the committed TypeScript SDK. CI checks the
# complete worktree after this target, including untracked output.
contracts: openapi-compat openapi-generate

openapi-export: sync
	PYTHONPATH="$(CENTER)/src:$(EDGE)/src" "$(VENV)/python" scripts/export_openapi.py $(OPENAPI)

openapi-compat: openapi-export
	"$(VENV)/python" scripts/check_openapi_compatibility.py $(OPENAPI_BASE_REF) $(OPENAPI) $(CONTRACT_PY)/breaking-changes.json

openapi-generate: openapi-export web-install
	# The generator does not promise to remove files for operations deleted from the schema.
	# Start from the one generated directory so a committed SDK cannot retain stale operations.
	rm -rf $(WEB)/src/api/generated
	pnpm --filter control-web run generate:api
	# 在清理与 SDK 生成之后追加派生步骤：原因码提示常量同样由 core 派生。
	"$(VENV)/python" scripts/generate_reason_codes.py $(OPENAPI) $(WEB)/src/api/generated/reasonCodes.gen.ts

policy-test:
	command -v git-lfs >/dev/null || (echo "policy-test requires git-lfs; LFS tests must not be skipped" >&2; exit 1)
	git lfs version
	$(PYTHON) -m unittest discover -s scripts/tests -p 'test_*.py'

policy:
	$(PYTHON) scripts/check_repo_policy.py

migrations:
	$(PYTHON) scripts/check_migration_ownership.py

contract-base:
	$(PYTHON) -m unittest discover -s tests/contract/base -p 'test_*.py'

contract-capability:
	PYTHONPATH="$(CONTRACT_PY)/src:apps/edge-runtime/src:apps/control-api/src" "$(VENV)/python" \
		-m unittest discover -s tests/contract/capability -p 'test_*.py'

contracts-python-check: contracts-python-format contracts-python-lint contracts-python-type \
	contracts-python-unit

contracts-python-format:
	$(RUFF) format --check --target-version py311 $(CONTRACT_PY)/src $(CONTRACT_PY)/tests

contracts-python-lint:
	$(RUFF) check --target-version py311 $(CONTRACT_PY)/src $(CONTRACT_PY)/tests

contracts-python-type:
	MYPYPATH="$(CONTRACT_PY)/src" $(MYPY) --cache-dir "$(LOCAL_CACHE)/mypy/$@" \
		--python-version 3.11 --strict \
		$(CONTRACT_PY)/src $(CONTRACT_PY)/tests

contracts-python-unit:
	PYTHONPATH="$(CONTRACT_PY)/src" $(PYTHON) -m unittest discover \
		-s $(CONTRACT_PY)/tests/unit -t $(CONTRACT_PY)/tests/unit -p 'test_*.py'

boundaries:
	PYTHONPATH="apps/control-api/src:apps/edge-runtime/src:$(CONTRACT_PY)/src" \
		"$(VENV)/lint-imports" --cache-dir "$(LOCAL_CACHE)/import-linter"

# `vendor/` is excluded because the policy check already reads its templates by value. The
# two lockfiles contain package integrity hashes, and the exported OpenAPI document is generated
# from FastAPI; neither is a credential-bearing authored input to this gate.
secret-scan:
	git ls-files -z --cached --others --exclude-standard \
		| grep -zvE '^(vendor/|uv\.lock$$|pnpm-lock\.yaml$$|packages/contracts/openapi\.json$$|\.secrets\.baseline$$)' \
		| xargs -0 "$(VENV)/detect-secrets-hook" --baseline .secrets.baseline

CENTER := apps/control-api
CENTER_PATHS := $(CENTER)/src $(CENTER)/tests scripts tests/contract tests/system

center-format:
	$(RUFF) format --check $(CENTER_PATHS)

center-lint:
	$(RUFF) check $(CENTER_PATHS)

center-type:
	MYPYPATH="$(CENTER)/src" $(MYPY) --cache-dir "$(LOCAL_CACHE)/mypy/$@" $(CENTER)/src $(CENTER)/tests

center-unit:
	cd $(CENTER) && PYTHONPATH=src $(PYTEST) tests/unit -q -n $(PYTEST_WORKERS_EFFECTIVE)

# 并行前提：提交真实状态的测试文件自带清理（如 test_auth_administration_lock 的 autouse
# 夹具），文件分组后不再依赖其他文件的夹具顺带清库。
center-integration:
	cd $(CENTER) && PYTHONPATH=src $(PYTEST) tests/integration -q -n $(PYTEST_WORKERS_EFFECTIVE) --dist loadfile

# 并行前提：端口由 _free_port 按 xdist worker 分配互不相交的端口段，避免「探测后关闭」
# 的跨 worker 踩踏（另一个 worker 的网关会应答存活探测，请求被转发到错误的 app）。
center-system:
	PYTHONPATH="$(CENTER)/src:$(EDGE)/src:$(CONTRACT_PY)/src" $(PYTEST) tests/system -q -n $(PYTEST_WORKERS_EFFECTIVE) --dist loadfile

EDGE := apps/edge-runtime

edge-format:
	cd $(EDGE) && $(RUFF) format --check src tests

edge-lint:
	cd $(EDGE) && $(RUFF) check src tests

edge-type:
	cd $(EDGE) && MYPYPATH="$(CURDIR)/$(CONTRACT_PY)/src" $(MYPY) \
		--cache-dir "$(LOCAL_CACHE)/mypy/$@" --strict src tests

edge-unit:
	cd $(EDGE) && PYTHONPATH="$(CURDIR)/$(CONTRACT_PY)/src:src" $(PYTHON) -m unittest \
		discover -s tests/unit -t tests/unit -p 'test_*.py'

edge-integration:
	cd $(EDGE) && PYTHONPATH="$(CURDIR)/$(CONTRACT_PY)/src:src" $(PYTHON) -m unittest \
		discover -s tests/integration -t tests/integration -p 'test_*.py'

# 手动运行的性能基线：不在 check-suite/CI 阻塞门禁中，不对应 500ms 现场验收。
edge-bench: sync
	mkdir -p "$(LOCAL_ARTIFACTS)/performance"
	cd $(EDGE) && PYTHONPATH="$(CURDIR)/$(CONTRACT_PY)/src:src:tests/unit:tests/integration" \
		$(PYTHON) tests/performance/benchmark_edge.py \
		--output "$(LOCAL_ARTIFACTS)/performance/edge-baseline.json" $(PERF_ARGS)

WEB := apps/control-web

web-install:
	pnpm install --frozen-lockfile --prefer-offline

web-format:
	pnpm --filter control-web run format

web-lint:
	pnpm --filter control-web run lint

web-type:
	pnpm --filter control-web run typecheck

web-unit:
	NODE_ENV=test pnpm --filter control-web run test

# Browser-level evidence for SYS-22-07. CI sets PLAYWRIGHT_BRANDED=1 and installs stable Chrome
# and Edge; a developer runs the same scenarios against Playwright's pinned Chromium.
web-e2e:
	PLAYWRIGHT_OUTPUT_DIR="$(LOCAL_ARTIFACTS)/web/test-results" \
		pnpm --filter control-web run test:e2e

# 使用固定 digest 的 MediaMTX 容器和合成 H.264 RTSP 源验证 SYS-34 WHEP。
web-e2e-whep:
	PLAYWRIGHT_OUTPUT_DIR="$(LOCAL_ARTIFACTS)/web/test-results" \
		python3 scripts/test_whep.py -- pnpm --filter control-web exec playwright test tests/e2e/sys-34-media.spec.ts --workers 2

web-build:
	pnpm --filter control-web exec vite build --outDir "$(LOCAL_ARTIFACTS)/web/dist" --emptyOutDir

# 固定 main 开发实例（Issue #119），默认 HTTP；显式 NVSOP_DEV_PROTOCOL=https 才启用本地 TLS。
# 脚本只编排 Tilt/Compose，不承载产品业务逻辑。
# Docker bridge 无法访问包镜像时，可仅为构建设置 NVSOP_DEV_BUILD_NETWORK=host；运行时仍使用 Compose 网络。
# SERVICE、TAIL 可由调用方覆盖，例如 `make dev-logs SERVICE=worker TAIL=200`。

dev-setup:
	python3 scripts/dev.py setup
dev:
	python3 scripts/dev.py run
dev-status:
	python3 scripts/dev.py status
dev-logs:
	python3 scripts/dev.py logs $(if $(SERVICE),--service "$(SERVICE)") $(if $(TAIL),--tail "$(TAIL)")
dev-refresh:
	python3 scripts/dev.py refresh
dev-smoke:
	python3 scripts/dev.py smoke
dev-test-ui:
	python3 scripts/dev.py test-ui
dev-down:
	python3 scripts/dev.py down
