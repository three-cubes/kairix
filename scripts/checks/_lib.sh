#!/usr/bin/env bash
# Shared helpers for architecture fitness function checks.
#
# Pattern: each check has a name (used in messages), a one-liner that emits
# violation files (one path per line, sorted, uniq'd), and a remediation
# message printed when any violation is found. There is no grandfathering:
# since tc-fitness v0.17 every check evaluates the full current tree.

set -u

# Fail on any current violation.
# Args:
#   $1: check name (e.g. "no-env-monkeypatch")
#   $2: remediation message
#   stdin: current violation file paths (one per line)
arch_gate() {
    local name="$1"
    local remediation="$2"

    local current
    current=$(sort -u | sed '/^$/d')  # consume stdin

    if [[ -n "$current" ]]; then
        printf '\033[0;31mFAIL [arch:%s]\033[0m — violation(s) found:\n' "$name"
        echo "$current" | sed 's/^/  /'
        printf '\n%s\n' "$remediation"
        return 1
    fi

    printf '\033[0;32mok [arch:%s]\033[0m — clean.\n' "$name"
    return 0
}
