# Release checklist (develop → main)

Manual ad-hoc release flow. Track [#170](https://github.com/quanyeomans/kairix/issues/170) to automate this.

## Pre-merge

- [ ] PR #134 (or current release PR) all 38 checks SUCCESS.
- [ ] `codecov/patch` shows non-regression.
- [ ] SonarCloud Quality Gate passes.
- [ ] CHANGELOG.md `[Unreleased]` section is fully populated (no empty sub-sections).
- [ ] Version label matches calendar version: `vYYYY.M.D[.N]` where N is the same-day suffix.

## Merge

```bash
# Confirm PR is mergeable
gh pr view 134 --json mergeable,mergeStateStatus

# Merge with merge-commit (preserves history) or squash (cleaner main log).
# Kairix convention: merge-commit on release PRs to preserve fitness-function
# baseline ratchets; squash for feature PRs.
gh pr merge 134 --merge --delete-branch=false  # release PR keeps develop alive
```

## Tag and release

```bash
# Tag from main HEAD
git checkout main
git pull origin main
TAG="v2026.5.9"
git tag -a "$TAG" -m "kairix $TAG"
git push origin "$TAG"

# Create the GitHub Release — derives notes from CHANGELOG.md.
# The Docker publish + PyPI publish workflows fire automatically on
# release creation.
gh release create "$TAG" \
    --title "kairix $TAG" \
    --notes-from-tag \
    --verify-tag
```

## Post-release

- [ ] Confirm `3 · Docker publish (release)` workflow ran and pushed image to ghcr.io.
- [ ] Confirm `4 · PyPI publish (release)` workflow ran and pushed wheel/sdist to PyPI.
- [ ] Bump CHANGELOG: rename `[Unreleased]` → `[X.Y.Z] - YYYY-MM-DD`, open follow-up PR with new empty `[Unreleased]` block.
- [ ] Sync `develop` ↔ `main`: `git checkout develop && git merge main && git push origin develop` (so develop carries the release-bump commits).

## Deployment

```bash
# Pull image on the VM
ssh threecubes 'docker pull ghcr.io/quanyeomans/kairix:vYYYY.M.D'

# Restart the kairix container (preserves /data/kairix mounts)
ssh threecubes 'cd /opt/kairix && docker compose pull && docker compose up -d'

# Verify health
ssh threecubes 'curl -fsS http://127.0.0.1:8182/healthz'
```

## UAT

After deploy, run UAT smoke from a host that has CLI + MCP reach:

```bash
bash scripts/uat-smoke.sh --mcp-url http://<vm-host>:8182
```

The script exits 0 only if every check in the list passes.

For the dogfood agents (multi-agent UAT), distribute the script and ask each agent to run it against the deployed instance and report PASS/FAIL summary back. Agents should NOT fix their environment if a check fails — just report the failure summary so we have a per-agent UAT signal.
