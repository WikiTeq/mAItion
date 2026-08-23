.PHONY: test-e2e test-e2e-validate test-e2e-keep

# End-to-end regression tests (require Docker; skipped with a warning if unavailable).
test-e2e:
	./tests/e2e/run.sh

# Keep the stack running after the run for debugging.
test-e2e-keep:
	./tests/e2e/run.sh --keep-up

# Fast syntax/wiring pre-flight; does not need Docker.
test-e2e-validate:
	python3 tests/e2e/validate.py
