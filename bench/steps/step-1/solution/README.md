# step-1 (L1-01) — oracle solution

- `app/` — the reference source tree for `/app`: `src/tracksvc/`, `alembic.ini`,
  `migrations/`, `tests/`. It adds files only; `pyproject.toml`, `uv.lock`, `ruff.toml`
  and `mypy.ini` of the initial commit stay untouched.
- `solve.sh` — copies `app/` into `/app` (override the target with `APP_DIR`). Idempotent;
  runs as the `agent` user or as root.
