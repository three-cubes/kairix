#!/usr/bin/env bash
# download-additional-sources.sh
# Download the 45 new T1-T3 sources identified from deep research scan.
# Run from same directory as download-reference-library.sh

set -euo pipefail

BASE="${1:-$(pwd)/reference-library}"
mkdir -p "$BASE"

clone_repo() {
    local dest="$1" url="$2"
    local target="$BASE/$dest"
    if [[ -d "$target" ]]; then
        echo "  SKIP $dest (already exists)"
        return
    fi
    echo -n "  CLONE $dest ... "
    local tmp
    tmp=$(mktemp -d)
    git clone --depth 1 --quiet "$url" "$tmp/repo" 2>/dev/null || {
        echo "FAIL (clone error)" >&2
        rm -rf "$tmp"
        return
    }
    mkdir -p "$target"
    (cd "$tmp/repo" && find . \( -name "*.md" -o -name "*.mdx" -o -name "*.rst" \) -not -path "./.git/*" -print0 | while IFS= read -r -d '' f; do
        rel="${f#./}"
        dir="$(dirname -- "$rel")"
        mkdir -p -- "$target/$dir"
        cp -- "$f" "$target/$rel"
    done)
    for lic in "$tmp/repo/LICENSE" "$tmp/repo/LICENSE.md" "$tmp/repo/LICENSE.txt" "$tmp/repo/COPYING"; do
        [[ -f "$lic" ]] && cp "$lic" "$target/" && break
    done
    rm -rf "$tmp"
    local count
    count=$(find "$target" \( -name "*.md" -o -name "*.mdx" -o -name "*.rst" \) 2>/dev/null | wc -l | tr -d ' ')
    echo "OK ($count files)"
}

echo "=== Downloading additional sources ==="

echo ""
echo "--- agentic-ai/ (AI1) ---"
clone_repo "agentic-ai/autogen-docs" "https://github.com/microsoft/autogen.git"
clone_repo "agentic-ai/eleutherai-lm-eval" "https://github.com/EleutherAI/lm-evaluation-harness.git"
clone_repo "agentic-ai/stanford-helm" "https://github.com/stanford-crfm/helm.git"

echo ""
echo "--- engineering/ (AI3) ---"
clone_repo "engineering/microsoft-api-guidelines" "https://github.com/microsoft/api-guidelines.git"
clone_repo "engineering/microsoft-code-with-playbook" "https://github.com/microsoft/code-with-engineering-playbook.git"
clone_repo "engineering/google-eng-practices" "https://github.com/google/eng-practices.git"
clone_repo "engineering/gds-way" "https://github.com/alphagov/gds-way.git"
clone_repo "engineering/opentelemetry-docs" "https://github.com/open-telemetry/opentelemetry.io.git"
clone_repo "engineering/arc42-template" "https://github.com/arc42/arc42-template.git"
clone_repo "engineering/dropbox-career-framework" "https://github.com/dropbox/dbx-career-framework.git"
clone_repo "engineering/engineering-ladders" "https://github.com/jorgef/engineeringladders.git"

echo ""
echo "--- data-and-analysis/ (AI2) ---"
clone_repo "data-and-analysis/turing-way" "https://github.com/the-turing-way/the-turing-way.git"
clone_repo "data-and-analysis/causal-inference-handbook" "https://github.com/matheusfacure/python-causality-handbook.git"
clone_repo "data-and-analysis/growthbook-docs" "https://github.com/growthbook/growthbook.git"

echo ""
echo "--- security/ (AI10-AI11) ---"
clone_repo "security/owasp-cheat-sheets" "https://github.com/OWASP/CheatSheetSeries.git"
clone_repo "security/slsa-spec" "https://github.com/slsa-framework/slsa.git"
clone_repo "security/cyclonedx-spec" "https://github.com/CycloneDX/specification.git"
clone_repo "security/openlane-grc" "https://github.com/theopenlane/core.git"

echo ""
echo "--- foundations/ (G4, G6) ---"
clone_repo "foundations/open-logic-project" "https://github.com/OpenLogicProject/OpenLogic.git"
clone_repo "foundations/neuromatch-compneuro" "https://github.com/NeuromatchAcademy/course-content.git"

echo ""
echo "--- futures/ (F8, F12) ---"
# These are PDFs — placeholder directories
mkdir -p "$BASE/futures"

echo ""
echo "--- leadership-and-culture/ (T1-T2) ---"
clone_repo "leadership-and-culture/mozilla-open-leadership" "https://github.com/mozilla/open-leadership-framework.git"
clone_repo "leadership-and-culture/ontario-service-design" "https://github.com/ongov/Service-Design-Playbook.git"

echo ""
echo "--- economics-and-strategy/ (M2) ---"
clone_repo "economics-and-strategy/meta-robyn-mmm" "https://github.com/facebookexperimental/Robyn.git"
clone_repo "economics-and-strategy/google-meridian-mmm" "https://github.com/google/meridian.git"
clone_repo "economics-and-strategy/pymc-marketing" "https://github.com/pymc-labs/pymc-marketing.git"

echo ""
echo "=== Additional sources complete ==="
total=$(find "$BASE" -name "*.md" -o -name "*.mdx" -o -name "*.rst" 2>/dev/null | wc -l | tr -d ' ')
echo "Total docs in reference library: $total"
