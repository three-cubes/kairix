# How-to — Configure PyPI trusted publisher for first release

**When to use:** One-time setup before publishing kairix to PyPI for the first time, OR after migrating the PyPI project to a new GitHub repo.

**Time:** ~10 minutes (mostly waiting for PyPI to verify the OIDC token).

---

## What this configures

GitHub Actions can publish to PyPI without storing a long-lived API token by using **PyPI Trusted Publishers** — an OIDC trust relationship between a specific repo+workflow+environment and the PyPI project. The `publish-pypi.yml` workflow in this repo is already wired to use this pattern; what's missing on first run is the PyPI-side configuration.

The pieces:

| Piece | Where it lives |
|---|---|
| `publish-pypi.yml` workflow | `.github/workflows/publish-pypi.yml` (already present) |
| `release` environment | GitHub repo settings (you create it once) |
| Trusted Publisher rule | PyPI project settings (you create it once) |
| `id-token: write` permission | Already declared in the workflow |

## One-time setup

### Step 1 — Create the GitHub `release` environment

GitHub repo → Settings → Environments → New environment → name it `release`.

Optional but recommended:

- **Required reviewers**: add yourself / a release-approver list. Each release will pause for approval before publishing — gives you a chance to bail if the build looks wrong.
- **Deployment branches**: restrict to `main` (so a feature-branch tag can't accidentally publish).

The workflow's `publish` job has `environment: release`; matching that name here is what gates it.

### Step 2 — Create the PyPI project (first publish only)

If `kairix` doesn't exist on PyPI yet, you have two options:

**Option A — Pre-create via GitHub OIDC (recommended for new projects):**
1. Go to https://pypi.org/manage/account/publishing/
2. Add a new "pending publisher" with these fields:
   - **PyPI project name**: `kairix`
   - **Owner**: `quanyeomans`
   - **Repository name**: `kairix`
   - **Workflow filename**: `publish-pypi.yml`
   - **Environment name**: `release`
3. The first GitHub Actions run that matches this rule will **claim the project name** and become its owner.

**Option B — If `kairix` already exists on PyPI:**
1. Sign in to PyPI as the project owner.
2. Project → Publishing → Add a new publisher → fill in the same four fields as Option A.
3. Existing API tokens / passwords can stay configured but no longer needed for publishes.

### Step 3 — Verify the trust chain

Before cutting a real release, verify the wiring with a dry-run release. Easiest path:

1. Create a pre-release tag: `git tag v2026.5.6-rc1 && git push origin v2026.5.6-rc1`.
2. On GitHub: Releases → Draft new release → pick the tag → mark as **pre-release** → publish.
3. Watch the `7 · PyPI Publish` workflow run.

Expected outcome:
- `build` job runs (creates the wheel + sdist artefact).
- `publish` job pauses for approval if you set required reviewers.
- After approval, `pypa/gh-action-pypi-publish` exchanges the GitHub OIDC token for PyPI upload credentials and pushes the wheel.
- `verify` job runs in a clean python environment, installs `kairix=={version}` from PyPI, and runs `kairix --version` to confirm.

If the dry run succeeds, you're ready for real releases.

## Routine release flow (after setup)

1. Merge the release PR into `main`.
2. Tag: `git tag v2026.5.6 && git push origin v2026.5.6` — version follows CalVer per `CHANGELOG.md` convention.
3. Create a GitHub Release pointing at the tag → publish release notes.
4. Workflow fires automatically. Approve the `release` environment if reviewers are required. Verify job confirms the wheel installs.

## Troubleshooting

**"trusted-publisher token rejected"**
PyPI couldn't match the OIDC token to a configured publisher. Re-check the four fields in Step 2 — they must match exactly (case-sensitive). The repo owner / name / workflow filename / environment name must all align.

**"package name is taken" on first publish**
Someone else already registered `kairix`. Check the project page on PyPI; if it's a name-squat, [contact PyPI support](https://pypi.org/help/#project-name) for a transfer. If it's a legit project, this repo will need a different package name (update `pyproject.toml`).

**Workflow runs but skips publish**
Check the run's logs for the `publish` job. The most common reasons:
- Required-reviewer approval pending — the job is waiting on you.
- The release event is a `prereleased` rather than `created` — the workflow currently triggers on `types: [created]`. Adjust the trigger if you want pre-releases to publish.

**Verify job fails with `pip install kairix==X.Y.Z` not found**
PyPI's CDN can lag a few seconds after publish. The workflow already has `sleep 60` before the install attempt. If it still fails, manually `pip install kairix==X.Y.Z` from your machine — if that works too, the verify job's network may be slow; bump the sleep.

## See also

- [`docs/upgrades/`](../../upgrades/) — version-specific migration guides for end users.
- [`CHANGELOG.md`](../../../CHANGELOG.md) — what gets published per release.
- PyPI trusted publishers docs: https://docs.pypi.org/trusted-publishers/
