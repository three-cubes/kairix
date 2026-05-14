#!/usr/bin/env bash
# Shared helpers for architecture fitness function checks.
#
# Pattern: each check has a name (used in messages and baseline filename),
# a one-liner that emits violation files (one path per line, sorted, uniq'd),
# and a remediation message printed when a NEW violation is introduced.

set -u

ARCH_BASELINE_DIR="${ARCH_BASELINE_DIR:-.architecture/baseline}"

# Compare current violations against baseline; fail on net-new.
# Args:
#   $1: check name (e.g. "no-env-monkeypatch")
#   $2: remediation message
#   stdin: current violation file paths (one per line)
arch_gate() {
    local name="$1"
    local remediation="$2"
    local baseline_file="${ARCH_BASELINE_DIR}/${name}-files.txt"

    local current
    current=$(sort -u)  # consume stdin

    local baseline
    if [[ -f "$baseline_file" ]]; then
        baseline=$(sort -u <"$baseline_file")
    else
        baseline=""
    fi

    local new_files
    new_files=$(comm -23 <(echo "$current") <(echo "$baseline") | sed '/^$/d')

    if [[ -n "$new_files" ]]; then
        printf '\033[0;31mFAIL [arch:%s]\033[0m — new violation(s) introduced:\n' "$name"
        echo "$new_files" | sed 's/^/  /'
        printf '\n%s\n' "$remediation"
        printf '\nIf this is genuinely the only practical fix, document why in the\n'
        printf 'PR description and append the file to %s\n' "$baseline_file"
        printf '(but expect pushback at review time — adding to the baseline is rare).\n'
        return 1
    fi

    # Informational: how many grandfathered files remain.
    local remaining
    if [[ -n "$baseline" ]]; then
        remaining=$(echo "$baseline" | wc -l | tr -d ' ')
    else
        remaining=0
    fi
    if [[ "$remaining" -gt 0 ]]; then
        printf '\033[0;33mok [arch:%s]\033[0m — %d grandfathered file(s) still present in baseline.\n' "$name" "$remaining"
    else
        printf '\033[0;32mok [arch:%s]\033[0m — clean.\n' "$name"
    fi
    return 0
}
