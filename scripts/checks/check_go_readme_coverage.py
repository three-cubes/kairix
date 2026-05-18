"""G9: every services/<name>/ has a README.md.

Mirrors F23 (every top-level directory has a README) but scoped to the
Go services tree. Operators landing on a service directory via a deploy
log, runbook reference, or path mention in a PR should hit a one-screen
orientation, not a bare directory listing.

Detection:

  - Walk every immediate child of ``services/`` that is a directory.
  - Require ``<dir>/README.md`` to exist.

Allow-list:

  - The ``services/`` root itself is exempt (it has its own README that
    documents the per-service convention).
  - Hidden directories (``.git``, ``.cache``, etc) are skipped — these
    shouldn't normally exist under ``services/`` but the exclusion mirrors
    F23's defensive posture.

Baseline:

  - ``.architecture/baseline/go-readme-coverage-files.txt`` lists missing
    README paths (one per line). New services land at zero violations by
    construction; the baseline file ships empty.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from _arch_lib import gate

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SERVICES_DIR = REPO_ROOT / "services"

REMEDIATION = """Add a README.md to the listed services/<name>/ directory.

fix: create services/<name>/README.md with a short orientation — what
this binary does, why it isn't Python (against the criteria in
docs/architecture/go-integration-plan.md §"Decision criteria for future
Go binaries"), how to run locally, how to deploy.
next: re-run python3 scripts/checks/check_go_readme_coverage.py to
confirm the gate goes green.
run: bash scripts/checks/run-all.sh

Pass example (services/alpha-deploy-webhook/README.md):
  # alpha-deploy-webhook
  Receives signed POSTs from release-vm-deploy.yml, pulls the alpha
  Docker image, runs onboard check + reflib benchmark, posts back via
  GitHub commit status. Justifies Go vs Python on the operability axis:
  runs on the VM without a Python venv; verifies HMAC with stdlib.
  Run: ./bin/alpha-deploy-webhook --listen :9443 --secret-file /run/secrets/webhook.key
  Deploy: see infra/scripts/install-alpha-deploy-webhook.sh in your sibling infrastructure repo.

Forbidden example:
  services/alpha-deploy-webhook/    # no README.md — fails G9

Why: every Go binary represents a deliberate decision to leave the
Python default. The README is where that decision is documented and
where the next operator-reader picks up the context. Net-new
violations block; pre-existing missing READMEs are grandfathered in
.architecture/baseline/go-readme-coverage-files.txt until written."""


def collect_violations(services_root: Path = SERVICES_DIR) -> set[Path]:
    """Walk services/<name>/; return repo-relative paths of missing README.md files."""
    violations: set[Path] = set()
    if not services_root.is_dir():
        return violations
    for child in sorted(services_root.iterdir()):
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        readme = child / "README.md"
        if not readme.is_file():
            violations.add(Path("services") / child.name / "README.md")
    return violations


def main() -> int:
    violations = collect_violations()
    return gate("go-readme-coverage", violations, REMEDIATION)


if __name__ == "__main__":
    sys.exit(main())
