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

REMEDIATION="Refactor: read the env var inside KairixPaths.resolve()
(kairix/paths.py) and expose it as a field; inner code reads
KairixPaths.resolve().<field>. For credentials, use kairix/secrets.py.
The boundary-only pattern (#139) is: env-var reads happen ONCE at
startup, never scattered across modules."

# Match os.environ.get("KAIRIX_..."), os.environ["KAIRIX_..."], or
# os.environ.pop("KAIRIX_...") — any read/mutation of a KAIRIX_* key.
grep -rEl 'os\.environ.*KAIRIX_' kairix/ --include='*.py' 2>/dev/null \
    | grep -vE '^kairix/(paths|secrets)\.py$' \
    | arch_gate "env-reads-in-paths" "$REMEDIATION"
