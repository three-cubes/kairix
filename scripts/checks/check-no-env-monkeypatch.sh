#!/usr/bin/env bash
# F2: No test writes to a KAIRIX_* process-env key.
#
# Per the boundary-only KairixPaths pattern (#139), env vars are read once at
# the boundary into KairixPaths. Tests construct KairixPaths directly via
# tests.fakes.FakePaths (or pass an env= mapping / Deps seam), never via
# process-env mutation — neither through monkeypatch.setenv/delenv nor through
# direct os.environ writes, which skip monkeypatch's auto-undo and leak into
# every later test in the process.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="KAIRIX_* process-env write found in a test. Refactor to an explicit
env= mapping / paths=FakePaths(...) / Deps seam to pass.

Covers monkeypatch.setenv / delenv / setattr / setitem / delitem AND the
direct forms that skip monkeypatch's auto-undo: os.environ['KAIRIX_X'] = v,
del os.environ['KAIRIX_X'], os.environ.pop / .setdefault / .update, and
patch.dict(os.environ, ...). A key held in a variable bound from a
KAIRIX_* literal counts too.

fix: pass the value through the production seam instead of the process
env — paths=FakePaths(...) from tests/fakes.py, an env={...} mapping on
the reader (read_int_env(..., env=...), get_embed_provider(env=...)), or a
*Deps dataclass. If the production function reads the env var directly,
add an ``env: Mapping[str, str] | None = None`` parameter that production
leaves as None (reads os.environ at the kairix.paths boundary) — the
boundary-only pattern from #139. A non-KAIRIX_ test-only variable name is
fine when the code under test hydrates arbitrary keys.
next: re-run ``bash scripts/checks/check-no-env-monkeypatch.sh`` to
confirm the gate goes green.
run: bash scripts/safe-commit.sh \"test(<area>): inject env via seam instead of mutating os.environ\"

Pass example:
  paths = FakePaths(data_dir=tmp_path, log_dir=tmp_path / 'logs')
  result = some_use_case(paths=paths)
  assert resolve_dispatch_concurrency(env={'KAIRIX_MAX_CONCURRENCY': '3'}) == 3

Forbidden example:
  monkeypatch.setenv('KAIRIX_DATA_DIR', str(tmp_path))
  os.environ['KAIRIX_DB_PATH'] = str(tmp_path / 'db.sqlite')
  os.environ.pop('KAIRIX_DB_PATH', None)
  with patch.dict(os.environ, {'KAIRIX_MAX_CONCURRENCY': '3'}): ...

Recognised structurally (not violations): writes inside a conftest.py
@pytest.fixture(scope='session', autouse=True) hermetic baseline, and the
teardown of a fixture that snapshots dict(os.environ) before its yield and
restores from that snapshot after it.

KAIRIX_* env-var reads happen ONCE at the boundary inside KairixPaths
(kairix/paths.py). Tests construct paths directly; they never mutate
process env to influence the production read."

# Delegate to AST-based detector — a grep-based check would match
# docstring text and produce false positives.
python3 "${SCRIPT_DIR}/check_no_env_monkeypatch.py" \
    | arch_gate "no-env-monkeypatch" "$REMEDIATION"
