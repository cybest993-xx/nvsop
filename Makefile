.PHONY: check policy policy-test

check: policy-test policy

policy-test:
	python3 -m unittest discover -s scripts/tests -p 'test_*.py'

policy:
	python3 scripts/check_repo_policy.py
