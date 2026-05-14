#!/usr/bin/env bash
# F4: os.environ.get("KAIRIX_*") may only appear in kairix/paths.py and
# kairix/secrets.py — anywhere else bypasses the canonical KairixPaths
# boundary established by issue #139.
#
# F2 catches the test side (no monkeypatch.setenv); F4 catches the
# production side. Together they enforce: every KAIRIX_* env-var read
# happens at the boundary in paths.py / secrets.py, never scattered.

set -u
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=./_lib.sh
. "${SCRIPT_DIR}/_lib.sh"

cd "${SCRIPT_DIR}/../.." || exit 2

REMEDIATION="Refactor to read the env var inside KairixPaths.resolve()
(kairix/paths.py) — or kairix/secrets.py for credentials — and expose
the value as a field on the returned object. Then inner code reads
``KairixPaths.resolve().<field>`` instead of os.environ.

fix: move the ``os.environ.get('KAIRIX_...')`` call out of the listed
module into ``kairix/paths.py`` (or ``kairix/secrets.py`` for
credentials); expose the value as a field on KairixPaths and have the
caller read ``KairixPaths.resolve().<field>`` instead.
next: re-run ``bash scripts/checks/check-env-reads-stay-in-paths.sh``
to confirm the gate goes green.
run: bash scripts/safe-commit.sh \"refactor(paths): move KAIRIX_* env read into boundary\"

The boundary-only pattern (#139): every KAIRIX_* env-var read happens
ONCE at startup inside paths.py / secrets.py, never scattered.

Pass example:
  # in kairix/paths.py
  @dataclass
  class KairixPaths:
      data_dir: Path
      @classmethod
      def resolve(cls) -> 'KairixPaths':
          return cls(data_dir=Path(os.environ.get('KAIRIX_DATA_DIR', '/data')))

  # elsewhere
  data_dir = KairixPaths.resolve().data_dir

Forbidden example:
  # in any kairix/*.py except paths.py / secrets.py
  data_dir = Path(os.environ.get('KAIRIX_DATA_DIR', '/data'))"

# Match os.environ.get("KAIRIX_..."), os.environ["KAIRIX_..."], or
# os.environ.pop("KAIRIX_...") — any read/mutation of a KAIRIX_* key.
grep -rEl 'os\.environ.*KAIRIX_' kairix/ --include='*.py' 2>/dev/null \
    | grep -vE '^kairix/(paths|secrets)\.py$' \
    | arch_gate "env-reads-in-paths" "$REMEDIATION"
