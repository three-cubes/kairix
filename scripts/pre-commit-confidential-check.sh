#!/bin/bash
# Pre-commit hook: block commits containing confidential data patterns.
#
# Install: cp scripts/pre-commit-confidential-check.sh .git/hooks/pre-commit
#
# This script checks for:
#   - API keys and tokens (AWS, OpenAI patterns)
#   - Patterns defined in .confidential-patterns (gitignored, operator-specific)
#
# Operators: create .confidential-patterns with one regex per line for your
# specific blocked terms (personal paths, resource names, client names).

set -e

STAGED_FILES=$(git diff --cached --name-only --diff-filter=ACM | grep -E '\.(py|md|yaml|yml|sh|json|toml)$' | grep -v "pre-commit-confidential-check.sh" || true)

if [[ -z "$STAGED_FILES" ]]; then
    exit 0
fi

# Built-in patterns (safe to have in a public repo)
BLOCKED_PATTERNS=(
    "sk-[a-zA-Z0-9]{20,}"
    "AKIA[A-Z0-9]{16}"
    "ghp_[a-zA-Z0-9]{36}"
    "glpat-[a-zA-Z0-9_-]{20,}"
)

# Load operator-specific patterns from .confidential-patterns (gitignored)
if [[ -f ".confidential-patterns" ]]; then
    while IFS= read -r line; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" == \#* ]] && continue
        BLOCKED_PATTERNS+=("$line")
    done < ".confidential-patterns"
fi

FAILURES=0

for pattern in "${BLOCKED_PATTERNS[@]}"; do
    MATCHES=$(echo "$STAGED_FILES" | xargs grep -lnE "$pattern" 2>/dev/null || true)
    if [[ -n "$MATCHES" ]]; then
        echo "BLOCKED: confidential pattern found in staged files:"
        echo "$MATCHES" | while read -r file; do
            echo "  $file:"
            grep -nE "$pattern" "$file" | head -3 | sed 's/^/    /'
        done
        FAILURES=$((FAILURES + 1))
    fi
done

if [[ "$FAILURES" -gt 0 ]]; then
    echo ""
    echo "Commit blocked: $FAILURES confidential pattern(s) found in staged files."
    echo "To bypass (emergencies only): git commit --no-verify"
    exit 1
fi
