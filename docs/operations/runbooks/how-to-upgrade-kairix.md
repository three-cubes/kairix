# How To: Upgrade the Kairix Binary

**Purpose:** Install a new tagged release of kairix safely, with benchmark gate to confirm NDCG has not regressed before committing the upgrade.

---

## Docker Compose Upgrade (recommended)

```bash
cd /path/to/kairix/docker
docker compose pull
docker compose up -d
kairix onboard check   # verify after upgrade
```

Gate: overall >= 0.80 (current baseline: 0.8385 NDCG@10).

---

## Before You Start (pip install path)

```bash
# Record current version and baseline benchmark score
kairix --version

kairix benchmark run \
  --suite suites/your-suite.yaml \
  --output ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/benchmark-results/
# Note the output filename — compare against this after upgrade

# Verify current search is healthy
kairix search "test"
# Confirm vec > 0, no vec_failed
```

---

## Step 1 — Install New Version (Alternative: pip install, legacy)

Kairix is installed into a virtualenv at `/opt/kairix/.venv`.

```bash
# Install new version
sudo /opt/kairix/.venv/bin/pip install kairix==<NEW_VERSION>

kairix --version
# Should show new version
```

---

## Step 2 — Verify All Symlinks Still Intact

Pip install can overwrite or reset the bin directory. Re-check all symlinks immediately.

```bash
# Confirm /usr/local/bin/kairix still points to wrapper (not raw binary)
ls -la /usr/local/bin/kairix
# Must be: /usr/local/bin/kairix -> /opt/kairix/bin/kairix-wrapper.sh

# If your integration tool adds a second kairix symlink, check that too
ls -la /opt/<tool>/bin/kairix 2>/dev/null || echo "no integration symlink"

# If any symlink was overwritten — fix immediately
sudo ln -sf /opt/kairix/bin/kairix-wrapper.sh /usr/local/bin/kairix
# Repeat for any integration symlinks

# Verify wrapper exists and is executable
ls -la /opt/kairix/bin/kairix-wrapper.sh
```

If wrapper is missing → the new version may not have installed it:
```bash
find /opt/kairix -name "kairix-wrapper*" 2>/dev/null
# Restore from repo if missing
sudo cp scripts/kairix-wrapper.sh /opt/kairix/bin/
sudo chmod +x /opt/kairix/bin/kairix-wrapper.sh
```

---

## Step 3 — Run Onboard Check

```bash
kairix onboard check
# All tests must pass before running benchmark
# If secrets tests fail → check that your secrets file is populated and KAIRIX_KV_NAME is set
# If vector test fails → verify the kairix wrapper script is on PATH (not the raw Python binary)
```

---

## Step 4 — Run Benchmark (Upgrade Gate)

```bash
kairix benchmark run \
  --suite suites/your-suite.yaml \
  --output ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/benchmark-results/

# Compare against pre-upgrade baseline
kairix benchmark compare \
  ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/benchmark-results/<before>.json \
  ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/benchmark-results/<after>.json

# If any metric regressed significantly → rollback (Step 6), investigate regression
# See runbook-benchmark-regression for diagnosis
```

---

## Step 5 — Commit Upgrade (If Benchmark Passes)

```bash
# Update the version pin in your operator config/install script
git add <install-script-or-version-file>
git commit -m "chore: pin kairix to v<NEW_VERSION> (benchmark passed)"
```

---

## Step 6 — Rollback (If Benchmark Fails)

```bash
# Rollback to previous tagged release
pip install git+https://github.com/quanyeomans/kairix@<PREVIOUS_TAG>

# Re-run onboard check and benchmark to confirm baseline restored
kairix onboard check
kairix benchmark run --suite suites/your-suite.yaml
```

---

## Verify Upgrade Complete

```bash
kairix --version
# Shows new version

kairix search "platform architecture"
# vec > 0, no vec_failed

kairix onboard check
# All green

kairix benchmark run --suite suites/your-suite.yaml
# Scores not regressed vs pre-upgrade baseline
```

---

## Related

- [how-to-run-benchmark](how-to-run-benchmark.md) — detailed benchmark procedure
- [runbook-benchmark-regression](runbook-benchmark-regression.md) — if benchmark fails post-upgrade
- [INDEX](INDEX.md) — full runbook registry
