# Project checks. `make check` is what the pre-commit hook runs.
# Do not modify this file.

# pytest gets an empty, unmigrated database of its own, next to the one
# DATABASE_URL points to; it is dropped again afterwards.
CHECK_DB := tracksvc_check

.PHONY: check lint complexity imports types test

check: lint complexity imports types test

lint:
	uv run ruff check .

complexity:
	uv run ruff check --select C901 --config 'lint.mccabe.max-complexity = 10' .

imports:
	PYTHONPATH=src lint-imports --config .importlinter --no-cache

types:
	uv run mypy --strict -p tracksvc

test:
	@set -eu; \
	base="$${DATABASE_URL%/*}"; \
	admin="postgresql://$${DATABASE_URL#*://}"; \
	quiet='SET client_min_messages = warning'; \
	psql "$$admin" -q -v ON_ERROR_STOP=1 -c "$$quiet" \
	  -c 'DROP DATABASE IF EXISTS $(CHECK_DB) WITH (FORCE)' -c 'CREATE DATABASE $(CHECK_DB)'; \
	status=0; \
	DATABASE_URL="$$base/$(CHECK_DB)" uv run pytest || status=$$?; \
	psql "$$admin" -q -c "$$quiet" -c 'DROP DATABASE IF EXISTS $(CHECK_DB) WITH (FORCE)' || true; \
	exit $$status
