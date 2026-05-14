# tests/

Pytest test suites organised by category. Every `test_*` carries one of
the markers from `pyproject.toml` (`unit`, `contract`, `bdd`,
`integration`, `e2e`, `slow`) — enforced by F8.

- `fakes.py` — canonical fake implementations; reach for these before
  defining inline stubs (see CLAUDE.md memory note on canonical-fakes-first)
- `fixtures/` — shared pytest fixtures
- `contracts/` — protocol-compliance tests, run first in CI
- `unit/` (and per-module siblings) — fast, no I/O
- `bdd/` — pytest-bdd feature files with happy-path scenarios (F12)
- `integration/` — multi-component, real DB + usearch index
- `e2e/` — full-pipeline against live Azure API (gated by `KAIRIX_E2E=1`)
- `fitness/` — the F1-F23 fitness-function tests themselves

Run `pytest -m <marker>` for a category, or `bash scripts/safe-commit.sh`
to mirror the CI gate. See
[../docs/architecture/ENGINEERING.md](../docs/architecture/ENGINEERING.md)
for the test pyramid and authoring rules.
