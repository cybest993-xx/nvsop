.PHONY: check policy policy-test contract-base edge-format edge-lint edge-type edge-unit

check: policy-test policy contract-base edge-format edge-lint edge-type edge-unit

policy-test:
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

policy:
	python3 scripts/check_repo_policy.py

# NVIDIA base-code contract suite (docs/design/mechanisms/judgment-and-boundary.md §5.9).
# Standard library only, pure CPU: it must stay runnable without Docker or a GPU.
# Mandatory after every `git subtree pull`.
contract-base:
	python3 -m unittest discover -s tests/contract/base -p 'test_*.py'

# apps/edge-runtime: the judgment core and the rest of the inference host's autonomous
# unit. Tool versions are pinned here rather than in a lockfile, because the package
# itself has no dependencies to lock (edge-autonomy.md §5.11). `uv` is already the
# repository's declared Python toolchain (solution-and-roadmap.md §六).
EDGE := apps/edge-runtime
RUFF := uv tool run ruff@0.16.3
MYPY := uv tool run mypy@1.18.2

edge-format:
	cd $(EDGE) && $(RUFF) format --check src tests

edge-lint:
	cd $(EDGE) && $(RUFF) check src tests

edge-type:
	cd $(EDGE) && $(MYPY) --strict src tests

# The judgment core is a pure function, so its regressions need no clock, no fixture
# process and no GPU: construct a state, send events, assert on the output.
edge-unit:
	cd $(EDGE) && PYTHONPATH=src python3 -m unittest discover -s tests/unit -t tests/unit -p 'test_*.py'
