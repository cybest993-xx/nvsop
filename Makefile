.PHONY: check lockfile sync policy policy-test migrations contract-base boundaries \
	center-format center-lint center-type center-unit \
	edge-format edge-lint edge-type edge-unit edge-integration

# The CPU-only, Docker-free merge gate (harness §6). CI calls this exact target.
check: lockfile sync policy-test policy migrations contract-base boundaries \
	center-format center-lint center-type center-unit \
	edge-format edge-lint edge-type edge-unit edge-integration

# A dependency change and its lockfile update land in one commit (harness §5). `--frozen`
# below declines to UPDATE the lockfile, which is not the same as checking it: a manifest
# edit whose lockfile was never regenerated would install the old resolution and pass. This
# resolves without writing and fails on that difference. Cached when the two already agree,
# so it needs the network only when the manifest actually changed.
lockfile:
	uv lock --check

# One frozen environment for the whole gate. `--frozen` installs from `uv.lock` without
# resolving, so an offline or CI run cannot silently upgrade a dependency, and the tool
# versions below come from that same lockfile rather than from a second pinning mechanism.
sync:
	uv sync --frozen --all-packages

VENV := $(CURDIR)/.venv/bin
RUFF := $(VENV)/ruff
MYPY := $(VENV)/mypy
PYTEST := $(VENV)/pytest

policy-test:
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

policy:
	python3 scripts/check_repo_policy.py

# A migration may only touch tables its own module owns, which the module prefix in the
# filename and in each physical table name makes statically decidable (§六, §七). Returns an
# explicit success before the first migration exists (C3).
migrations:
	python3 scripts/check_migration_ownership.py

# NVIDIA base-code contract suite (docs/design/mechanisms/judgment-and-boundary.md §5.9).
# Standard library only, pure CPU: it must stay runnable without Docker or a GPU.
# Mandatory after every `git subtree pull`.
contract-base:
	python3 -m unittest discover -s tests/contract/base -p 'test_*.py'

# Module boundaries, enforced mechanically rather than by review (harness §3). Both source
# trees are on the path because one contract is about what must NOT cross between them.
boundaries:
	PYTHONPATH=apps/control-api/src:apps/edge-runtime/src $(VENV)/lint-imports

# apps/control-api: the center backend. Configuration, formatting, lint and type rules live
# in the repository root's pyproject.toml, so these run from the root.
CENTER := apps/control-api
CENTER_PATHS := $(CENTER)/src $(CENTER)/tests scripts tests/contract

center-format:
	$(RUFF) format --check $(CENTER_PATHS)

center-lint:
	$(RUFF) check $(CENTER_PATHS)

center-type:
	MYPYPATH=$(CENTER)/src $(MYPY) $(CENTER)/src $(CENTER)/tests

center-unit:
	cd $(CENTER) && PYTHONPATH=src $(PYTEST) tests/unit -q

# apps/edge-runtime: the judgment core and the rest of the inference host's autonomous unit.
# It is not a uv workspace member — membership would put every center dependency on its
# import path, leaving "standard library only" (edge-autonomy.md §5.11) as discipline rather
# than a fact; `scripts/check_repo_policy.py` resolves its every import instead. The tools
# still come from the one frozen environment above, so both applications are held to the
# same ruff and mypy build. Each reads this package's own pyproject.toml, which targets the
# older interpreter the NVIDIA base container sets.
EDGE := apps/edge-runtime

edge-format:
	cd $(EDGE) && $(RUFF) format --check src tests

edge-lint:
	cd $(EDGE) && $(RUFF) check src tests

edge-type:
	cd $(EDGE) && $(MYPY) --strict src tests

# The judgment core is a pure function, so its regressions need no clock, no fixture
# process and no GPU: construct a state, send events, assert on the output. Run on a bare
# interpreter with only `src` on the path: an import the package should not have fails here
# rather than resolving against whatever the developer happens to have installed.
edge-unit:
	cd $(EDGE) && PYTHONPATH=src python3 -m unittest discover -s tests/unit -t tests/unit -p 'test_*.py'

# The local state store against real SQLite. In `make check` rather than
# `make check-integration` because SQLite is embedded: no container, no daemon, no GPU, and
# nothing for a developer without Docker to install. Harness §6 separates the two targets so
# that container startup stays out of the fast loop — that reason does not reach this suite,
# while §4's reason for it being an integration suite does: SQLite is this store's real
# infrastructure, not a stand-in for it.
edge-integration:
	cd $(EDGE) && PYTHONPATH=src python3 -m unittest discover -s tests/integration -t tests/integration -p 'test_*.py'
