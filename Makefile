.PHONY: check policy policy-test contract-base

check: policy-test policy contract-base

policy-test:
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

policy:
	python3 scripts/check_repo_policy.py

# NVIDIA base-code contract suite (docs/design/solution-and-roadmap.md §5.9).
# Standard library only, pure CPU: it must stay runnable without Docker or a GPU.
# Mandatory after every `git subtree pull`.
contract-base:
	python3 -m unittest discover -s tests/contract/base -p 'test_*.py'
