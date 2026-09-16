.PHONY: check check-docs check-integration change-size hooks lockfile sync policy policy-test migrations contract-base \
	contract-capability \
	contracts contracts-python-check contracts-python-format contracts-python-lint \
	contracts-python-type contracts-python-unit openapi-export openapi-compat openapi-generate \
	boundaries secret-scan center-format center-lint center-type center-unit center-integration \
	center-system edge-format edge-lint edge-type edge-unit edge-integration \
	web-install web-format web-lint web-type web-unit web-e2e web-e2e-whep web-build \
	dev-setup dev dev-status dev-logs dev-refresh dev-smoke dev-test-ui dev-down

# The CPU-only, Docker-free merge gate (harness §6). CI calls this exact target.
check: lockfile sync hooks policy-test policy migrations contract-base contract-capability \
	contracts-python-check \
	boundaries secret-scan center-format center-lint center-type center-unit \
	edge-format edge-lint edge-type edge-unit edge-integration \
	contracts web-format web-lint web-type web-unit web-build

# 文档快线复用冻结工具、仓库政策（含 Markdown 链接）与敏感信息扫描。
# 本地省略 HEAD 时检查从 BASE 到工作区的差异；CI 传入实际比较提交。
check-docs: lockfile sync hooks policy secret-scan
	git diff --check "$(BASE)" $(if $(HEAD),"$(HEAD)") --

# Git hooks that hold for whichever agent or person commits (harness §6): the local main/dev
# workflow and worker branch names, no push to main, no edit under vendor/, and no unformatted
# Python. Versioned under scripts/githooks/ and enabled by pointing git at that directory; part of
# `check`.
hooks:
	git config core.hooksPath scripts/githooks

# The second required target (harness §6): one application plus real local infrastructure,
# started as containers via testcontainers. `center-system` runs §5.15's acceptance scenarios.
check-integration: sync center-integration center-system

# Harness §5 的规模提示：省略 HEAD 时统计已提交、暂存、未暂存及未跟踪文件。
# CI 传入 BASE 与 HEAD，只读取该 PR 的固定提交。
BASE ?= origin/main
HEAD ?=
change-size:
	python3 scripts/check_change_size.py "$(BASE)" $(if $(HEAD),"$(HEAD)")

# A dependency change and its lockfile update land together. Frozen checks refuse stale locks.
lockfile:
	uv lock --check

# One frozen environment for the whole gate.
sync:
	uv sync --frozen --all-packages

VENV := $(CURDIR)/.venv/bin
RUFF := $(VENV)/ruff
MYPY := $(VENV)/mypy
PYTEST := $(VENV)/pytest
CONTRACT_PY := packages/contracts
OPENAPI := $(CONTRACT_PY)/openapi.json
OPENAPI_BASE_REF ?= origin/main

# Canonical generated-contract command (harness §8): export from FastAPI, reject breaking
# evolution against the PR base, then regenerate the committed TypeScript SDK. CI checks the
# complete worktree after this target, including untracked output.
contracts: openapi-compat openapi-generate

openapi-export:
	PYTHONPATH=$(CENTER)/src $(VENV)/python scripts/export_openapi.py $(OPENAPI)

openapi-compat: openapi-export
	$(VENV)/python scripts/check_openapi_compatibility.py $(OPENAPI_BASE_REF) $(OPENAPI)

openapi-generate: openapi-export web-install
	# The generator does not promise to remove files for operations deleted from the schema.
	# Start from the one generated directory so a committed SDK cannot retain stale operations.
	rm -rf $(WEB)/src/api/generated
	pnpm --filter control-web run generate:api

policy-test:
	command -v git-lfs >/dev/null || (echo "policy-test requires git-lfs; LFS tests must not be skipped" >&2; exit 1)
	git lfs version
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

policy:
	python3 scripts/check_repo_policy.py

migrations:
	python3 scripts/check_migration_ownership.py

contract-base:
	python3 -m unittest discover -s tests/contract/base -p 'test_*.py'

contract-capability:
	PYTHONPATH=$(CONTRACT_PY)/src:apps/edge-runtime/src:apps/control-api/src $(VENV)/python \
		-m unittest discover -s tests/contract/capability -p 'test_*.py'

contracts-python-check: contracts-python-format contracts-python-lint contracts-python-type \
	contracts-python-unit

contracts-python-format:
	$(RUFF) format --check --target-version py311 $(CONTRACT_PY)/src $(CONTRACT_PY)/tests

contracts-python-lint:
	$(RUFF) check --target-version py311 $(CONTRACT_PY)/src $(CONTRACT_PY)/tests

contracts-python-type:
	MYPYPATH=$(CONTRACT_PY)/src $(MYPY) --python-version 3.11 --strict \
		$(CONTRACT_PY)/src $(CONTRACT_PY)/tests

contracts-python-unit:
	PYTHONPATH=$(CONTRACT_PY)/src python3 -m unittest discover \
		-s $(CONTRACT_PY)/tests/unit -t $(CONTRACT_PY)/tests/unit -p 'test_*.py'

boundaries:
	PYTHONPATH=apps/control-api/src:apps/edge-runtime/src:$(CONTRACT_PY)/src $(VENV)/lint-imports

# `vendor/` is excluded because the policy check already reads its templates by value. The
# two lockfiles contain package integrity hashes, and the exported OpenAPI document is generated
# from FastAPI; neither is a credential-bearing authored input to this gate.
secret-scan:
	git ls-files -z --cached --others --exclude-standard \
		| grep -zvE '^(vendor/|uv\.lock$$|pnpm-lock\.yaml$$|packages/contracts/openapi\.json$$|\.secrets\.baseline$$)' \
		| xargs -0 $(VENV)/detect-secrets-hook --baseline .secrets.baseline

CENTER := apps/control-api
CENTER_PATHS := $(CENTER)/src $(CENTER)/tests scripts tests/contract tests/system

center-format:
	$(RUFF) format --check $(CENTER_PATHS)

center-lint:
	$(RUFF) check $(CENTER_PATHS)

center-type:
	MYPYPATH=$(CENTER)/src $(MYPY) $(CENTER)/src $(CENTER)/tests

center-unit:
	cd $(CENTER) && PYTHONPATH=src $(PYTEST) tests/unit -q

center-integration:
	cd $(CENTER) && PYTHONPATH=src $(PYTEST) tests/integration -q

center-system:
	PYTHONPATH=$(CENTER)/src:$(EDGE)/src:$(CONTRACT_PY)/src $(PYTEST) tests/system -q

EDGE := apps/edge-runtime

edge-format:
	cd $(EDGE) && $(RUFF) format --check src tests

edge-lint:
	cd $(EDGE) && $(RUFF) check src tests

edge-type:
	cd $(EDGE) && MYPYPATH=$(CURDIR)/$(CONTRACT_PY)/src $(MYPY) --strict src tests

edge-unit:
	cd $(EDGE) && PYTHONPATH=$(CURDIR)/$(CONTRACT_PY)/src:src python3 -m unittest \
		discover -s tests/unit -t tests/unit -p 'test_*.py'

edge-integration:
	cd $(EDGE) && PYTHONPATH=$(CURDIR)/$(CONTRACT_PY)/src:src python3 -m unittest \
		discover -s tests/integration -t tests/integration -p 'test_*.py'

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
	pnpm --filter control-web run test

# Browser-level evidence for SYS-22-07. CI sets PLAYWRIGHT_BRANDED=1 and installs stable Chrome
# and Edge; a developer runs the same scenarios against Playwright's pinned Chromium.
web-e2e:
	pnpm --filter control-web run test:e2e

# 使用固定 digest 的 MediaMTX 容器和合成 H.264 RTSP 源验证 SYS-34 WHEP。
web-e2e-whep:
	python3 scripts/test_whep.py -- pnpm --filter control-web exec playwright test tests/e2e/sys-34-media.spec.ts --workers 2

web-build:
	pnpm --filter control-web run build

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
