#!/usr/bin/env bash
# download-reference-library.sh
# Clone all T1-T3 (CC0/MIT/Apache/CC-BY) sources for the kairix reference library.
# Run from the directory where you want the reference-library/ folder created.
#
# Usage: bash download-reference-library.sh [--phase N]
#   --phase 2  Clone GitHub repos (native markdown)
#   --phase 3  Download PDFs and convert (requires markitdown/pandoc)
#   (no flag)  Run phase 2 only (safe default)

set -euo pipefail

PHASE="${1:-2}"
if [[ "${1:-}" == "--phase" ]]; then PHASE="${2:-2}"; fi

BASE="$(pwd)/reference-library"
mkdir -p "$BASE"

clone_repo() {
    local dest="$1" url="$2" extract="$3"
    local target="$BASE/$dest"
    if [[ -d "$target" ]]; then
        echo "  SKIP $dest (already exists)"
        return
    fi
    echo "  CLONE $dest"
    local tmp
    tmp=$(mktemp -d) || { echo "  FAIL $dest — mktemp failed"; return; }
    git clone --depth 1 --quiet "$url" "$tmp/repo" 2>/dev/null || {
        echo "  FAIL $dest — clone failed"
        rm -rf "$tmp"
        return
    }
    mkdir -p "$target"
    # Copy markdown files preserving directory structure (macOS compatible)
    (cd "$tmp/repo" && find . -name "*.md" -not -path "./.git/*" -print0 | while IFS= read -r -d '' f; do
        rel="${f#./}"
        dir="$(dirname -- "$rel")"
        mkdir -p -- "$target/$dir"
        cp -- "$f" "$target/$rel"
    done)
    # Also copy JSON files for structured data sources (exercise DBs, etc.)
    (cd "$tmp/repo" && find . -name "*.json" -not -path "./.git/*" -not -path "*/node_modules/*" -maxdepth 3 -print0 2>/dev/null | while IFS= read -r -d '' f; do
        rel="${f#./}"
        dir="$(dirname -- "$rel")"
        mkdir -p -- "$target/$dir"
        cp -- "$f" "$target/$rel"
    done)
    # Copy LICENSE file
    for lic in "$tmp/repo/LICENSE" "$tmp/repo/LICENSE.md" "$tmp/repo/LICENSE.txt" "$tmp/repo/COPYING"; do
        [[ -f "$lic" ]] && cp "$lic" "$target/" && break
    done
    rm -rf "$tmp"
    local count
    count=$(find "$target" -name "*.md" | wc -l | tr -d ' ') || count="?"
    echo "  OK   $dest — $count markdown files"
}

# ─────────────────────────────────────────────────────────────
# PHASE 2: Clone GitHub repos (native markdown)
# ─────────────────────────────────────────────────────────────
if [[ "$PHASE" == "2" || "$PHASE" == "all" ]]; then
    echo "=== Phase 2: Cloning GitHub repos ==="

    echo ""
    echo "--- agentic-ai/ ---"
    clone_repo "agentic-ai/openai-cookbook" "https://github.com/openai/openai-cookbook.git" "ALL"
    clone_repo "agentic-ai/dair-ai-prompts" "https://github.com/dair-ai/Prompt-Engineering-Guide.git" "ALL"
    clone_repo "agentic-ai/panaversity-agentic" "https://github.com/panaversity/learn-agentic-ai.git" "ALL"
    clone_repo "agentic-ai/ms-gen-ai-beginners" "https://github.com/microsoft/generative-ai-for-beginners.git" "ALL"
    clone_repo "agentic-ai/ms-prompts-edu" "https://github.com/microsoft/prompts-for-edu.git" "ALL"
    clone_repo "agentic-ai/awesome-ai-system-prompts" "https://github.com/dontriskit/awesome-ai-system-prompts.git" "ALL"

    echo ""
    echo "--- engineering/ ---"
    clone_repo "engineering/adr-examples" "https://github.com/joelparkerhenderson/architecture-decision-record.git" "ALL"
    clone_repo "engineering/madr" "https://github.com/adr/madr.git" "ALL"
    clone_repo "engineering/soc-docs" "https://github.com/madirish/ossocdocs.git" "ALL"
    clone_repo "engineering/18f-guides" "https://github.com/18F/guides.git" "ALL"
    clone_repo "engineering/12factor" "https://github.com/heroku/12factor.git" "ALL"

    echo ""
    echo "--- data-and-analysis/ ---"
    clone_repo "data-and-analysis/dbt-docs" "https://github.com/dbt-labs/docs.getdbt.com.git" "ALL"
    clone_repo "data-and-analysis/mlops-guide" "https://github.com/MLOps-Guide/MLOps-Guide.git" "ALL"
    clone_repo "data-and-analysis/posthog-docs" "https://github.com/PostHog/posthog.com.git" "ALL"

    echo ""
    echo "--- product-and-design/ ---"
    clone_repo "product-and-design/gong-practices" "https://github.com/gong-io/product-practices.git" "ALL"
    clone_repo "product-and-design/usds-playbook" "https://github.com/usds/playbook.git" "ALL"
    clone_repo "product-and-design/awesome-retrospectives" "https://github.com/josephearl/awesome-retrospectives.git" "ALL"

    echo ""
    echo "--- operating-models/ ---"
    clone_repo "operating-models/cncf-platform-model" "https://github.com/cncf/tag-app-delivery.git" "ALL"
    clone_repo "operating-models/jph-ways-of-working" "https://github.com/joelparkerhenderson/ways-of-working.git" "ALL"

    echo ""
    echo "--- leadership-and-culture/ ---"
    clone_repo "leadership-and-culture/awesome-open-company" "https://github.com/opencompany/awesome-open-company.git" "ALL"
    clone_repo "leadership-and-culture/jph-awesome-developing" "https://github.com/joelparkerhenderson/awesome-developing.git" "ALL"

    echo ""
    echo "--- economics-and-strategy/ ---"
    clone_repo "economics-and-strategy/jph-business-model-canvas" "https://github.com/joelparkerhenderson/business-model-canvas.git" "ALL"
    clone_repo "economics-and-strategy/jph-startup-guide" "https://github.com/SixArm/startup-business-guide.git" "ALL"

    echo ""
    echo "--- personal-effectiveness/ ---"
    clone_repo "personal-effectiveness/jph-okrs" "https://github.com/joelparkerhenderson/objectives-and-key-results.git" "ALL"
    clone_repo "personal-effectiveness/open-spaced-repetition" "https://github.com/open-spaced-repetition/fsrs4anki.git" "ALL"
    clone_repo "personal-effectiveness/mindful-programming" "https://github.com/code-in-flow/mindful-programming.git" "ALL"

    echo ""
    echo "--- health-and-fitness/ ---"
    clone_repo "health-and-fitness/free-exercise-db" "https://github.com/yuhonas/free-exercise-db.git" "ALL"
    clone_repo "health-and-fitness/awesome-quantified-self" "https://github.com/woop/awesome-quantified-self.git" "ALL"
    clone_repo "health-and-fitness/awesome-healthcare" "https://github.com/kakoni/awesome-healthcare.git" "ALL"
    clone_repo "health-and-fitness/awesome-mental-health" "https://github.com/dreamingechoes/awesome-mental-health.git" "ALL"
    clone_repo "health-and-fitness/circadiaware" "https://github.com/Circadiaware/VLiDACMel-entrainment-therapy-non24.git" "ALL"

    echo ""
    echo "--- philosophy/ ---"
    clone_repo "philosophy/suttacentral" "https://github.com/suttacentral/sc-data.git" "ALL"
    clone_repo "philosophy/bhagavad-gita-data" "https://github.com/vedicscriptures/bhagavad-gita-data.git" "ALL"
    clone_repo "philosophy/standard-ebooks-tao-te-ching" "https://github.com/standardebooks/laozi_tao-te-ching_james-legge.git" "ALL"
    clone_repo "philosophy/standard-ebooks-art-of-war" "https://github.com/standardebooks/sun-tzu_the-art-of-war_lionel-giles.git" "ALL"

    echo ""
    echo "--- family-and-education/ ---"
    clone_repo "family-and-education/awesome-parenting" "https://github.com/daugaard/awesome-parenting.git" "ALL"

    echo ""
    echo "--- industry-standards/ ---"
    clone_repo "industry-standards/bian-apis" "https://github.com/bian-official/public.git" "ALL"
    clone_repo "industry-standards/mosip-docs" "https://github.com/mosip/documentation.git" "ALL"

    echo ""
    echo "=== Phase 2 complete ==="
    total=$(find "$BASE" -name "*.md" 2>/dev/null | wc -l | tr -d ' ') || total="?"
    echo "Total markdown files: $total"
fi

# ─────────────────────────────────────────────────────────────
# PHASE 3: Download PDFs and public domain texts (requires conversion tools)
# ─────────────────────────────────────────────────────────────
if [[ "$PHASE" == "3" || "$PHASE" == "all" ]]; then
    echo "=== Phase 3: Downloading PDFs and public domain texts ==="
    echo "NOTE: This phase requires 'markitdown' or 'pandoc' for PDF/HTML conversion."
    echo "Install: pip install markitdown  OR  brew install pandoc"

    DOWNLOADS="$BASE/.downloads"
    mkdir -p "$DOWNLOADS"

    # Project Gutenberg texts (plain text → markdown)
    echo ""
    echo "--- Gutenberg public domain texts ---"
    declare -A GUTENBERG_TEXTS=(
        ["philosophy/classical-eastern/tao-te-ching.md"]="https://www.gutenberg.org/cache/epub/216/pg216.txt"
        ["philosophy/classical-eastern/dhammapada.md"]="https://www.gutenberg.org/cache/epub/2017/pg2017.txt"
        ["philosophy/classical-eastern/chuang-tzu.md"]="https://www.gutenberg.org/cache/epub/59709/pg59709.txt"
        ["philosophy/classical-eastern/bhagavad-gita.md"]="https://www.gutenberg.org/cache/epub/2388/pg2388.txt"
        ["philosophy/classical-eastern/confucian-analects.md"]="https://www.gutenberg.org/cache/epub/4094/pg4094.txt"
        ["philosophy/classical-western/meditations.md"]="https://www.gutenberg.org/cache/epub/2680/pg2680.txt"
        ["philosophy/classical-western/enchiridion.md"]="https://www.gutenberg.org/cache/epub/45109/pg45109.txt"
        ["philosophy/classical-western/discourses-epictetus.md"]="https://www.gutenberg.org/cache/epub/10661/pg10661.txt"
        ["philosophy/martial-arts-philosophy/bushido.md"]="https://www.gutenberg.org/cache/epub/12096/pg12096.txt"
        ["philosophy/indian-philosophy/yoga-sutras.md"]="https://www.gutenberg.org/cache/epub/2526/pg2526.txt"
        ["family-and-education/montessori-method.md"]="https://www.gutenberg.org/cache/epub/29635/pg29635.txt"
        ["family-and-education/dewey-democracy-education.md"]="https://www.gutenberg.org/cache/epub/852/pg852.txt"
    )

    for dest in "${!GUTENBERG_TEXTS[@]}"; do
        target="$BASE/$dest"
        if [[ -f "$target" ]]; then
            echo "  SKIP $dest (exists)"
            continue
        fi
        url="${GUTENBERG_TEXTS[$dest]}"
        echo "  GET  $dest"
        mkdir -p "$(dirname "$target")"
        if curl -sL "$url" -o "$target" 2>/dev/null; then
            # Add frontmatter
            title=$(head -20 "$target" | grep -m1 "Title:" | sed 's/Title: //' || basename "$dest" .md)
            tmpf=$(mktemp) || { echo "  FAIL $dest — mktemp failed"; continue; }
            echo "---" > "$tmpf"
            echo "title: \"$title\"" >> "$tmpf"
            echo "source: Project Gutenberg" >> "$tmpf"
            echo "licence: Public Domain" >> "$tmpf"
            echo "url: $url" >> "$tmpf"
            echo "---" >> "$tmpf"
            echo "" >> "$tmpf"
            cat "$target" >> "$tmpf"
            mv "$tmpf" "$target"
            echo "  OK   $dest"
        else
            echo "  FAIL $dest"
        fi
    done

    echo ""
    echo "=== Phase 3 complete ==="
    total=$(find "$BASE" -name "*.md" 2>/dev/null | wc -l | tr -d ' ') || total="?"
    echo "Total markdown files: $total"
fi

echo ""
echo "Reference library at: $BASE"
echo "Next steps:"
echo "  1. Verify licences: check LICENSE file in each collection"
echo "  2. Run normalisation: scripts/normalise-reference-library.sh"
echo "  3. Build eval queries: kairix eval build-queries --reference-library $BASE"
echo "  4. Run benchmark: kairix eval run --reference-library $BASE"
