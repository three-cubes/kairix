# scripts/

Operational scripts — fitness-function checks, build helpers, deploy
preflight, and one-off maintenance utilities. Not shipped to PyPI;
invoked from CI, `safe-commit.sh`, or by operators directly.

- `checks/` — architecture fitness functions (F1-F23) and their drivers
  (`run-all.sh`); see
  [../docs/architecture/fitness-functions.md](../docs/architecture/fitness-functions.md)
- `install/` — installer fragments invoked by `kairix onboard`
- `safe-commit.sh` — the commit gate (lint + mypy + tests + security)
- `preflight.sh` — pre-deploy host check
- `release-checklist.md` — the steps for cutting a release

Linting overrides for this directory: `S` (subprocess), `T20` (print),
`B007` (unused loop vars) — see `pyproject.toml`
`[tool.ruff.lint.per-file-ignores]`. New scripts that add a pipeline
message must follow the universal affordance template (F21:
`fix:` / `next:` / `run:` markers).
