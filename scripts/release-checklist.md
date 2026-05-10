# Release checklist (develop → main)

Tag-creation, GitHub-Release authoring, and CHANGELOG-section extraction
are automated by the **`5 · Release`** workflow (`.github/workflows/release.yml`).
This checklist covers the parts a human still drives — develop→main merge,
post-release CHANGELOG bump, and the deploy/UAT loop.

## Pre-merge

- [ ] Release PR all checks SUCCESS — including `SonarCloud Code Analysis` + `codecov/patch`.
- [ ] CHANGELOG.md `[Unreleased]` section is fully populated (no empty sub-sections).
- [ ] Version label matches calendar version: `vYYYY.M.D[.N]` where N is the same-day suffix.

## Merge

```bash
# Confirm PR is mergeable; squash-merge if the repo is configured to allow only squash.
gh pr view <N> --json mergeable,mergeStateStatus
gh pr merge <N> --squash --delete-branch=false  # release PR keeps develop alive
```

## Tag + release (automated)

```bash
# Trigger the release workflow with the version label.
# Inputs:
#   version: vYYYY.M.D[.N]
#   changelog_label: Unreleased   (default — the [Unreleased] section)
gh workflow run "5 · Release (tag + GitHub release from CHANGELOG)" \
    -f version=v2026.5.9 \
    -f changelog_label=Unreleased

# Watch the run.
gh run watch
```

The workflow:
1. Validates the version matches CalVer (`vYYYY.M.D[.N]`) and isn't already tagged.
2. Extracts the `[Unreleased]` (or supplied) CHANGELOG section as release notes.
3. Tags `main` HEAD and pushes the tag.
4. Creates the GitHub Release with the extracted notes — which fires the
   downstream `3 · Docker publish (release)` and `4 · PyPI publish (release)` workflows.

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
