# Kairix — Benchmark Regression (NDCG Dropped)

**Symptom:** `kairix benchmark run` reports NDCG@10 below acceptable levels. This occurs after a config change, full re-embed cycle, embedding model change, or kairix binary upgrade.

**Production baseline (v2026.4.27):** weighted 0.8171, NDCG@10 0.8385.

Note: NDCG thresholds below are suggested starting points — calibrate against your own baseline before treating them as hard gates.

---

## Quick Diagnosis

```bash
# Run the benchmark suite
kairix benchmark run --suite suites/your-suite.yaml

# Check what changed recently
git log --oneline -10

# Check kairix version
kairix --version

# Check embed log for recent activity
tail -30 ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/azure-embed.log
```

---

## Root Cause A — Config Change Degraded Ranking

If NDCG dropped after an edit to kairix config (RRF weights, category scores, BM25/vector balance):

```bash
# Compare config to last known-good
git diff HEAD~1 -- kairix.yaml

# Re-run benchmark to confirm regression is reproducible
kairix benchmark run --suite suites/your-suite.yaml
```

**Fix:** Revert the ranking config to the previous version via git, then redeploy.

```bash
git checkout HEAD~1 -- kairix.yaml
kairix benchmark run --suite suites/your-suite.yaml
```

---

## Root Cause B — Index Re-embed Changed Chunk Quality

If NDCG dropped after a full re-embed (`kairix embed --force`):

```bash
# Check embed log for anomalies
tail -50 ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/azure-embed.log
# Look for: failed= count > 0, dimension mismatch errors, partial completion

# Check database state
sqlite3 ~/.cache/kairix/index.sqlite \
  'SELECT model, COUNT(*) FROM content_vectors GROUP BY model;'
# If mixed models present: dimension mismatch likely — run kairix embed --force again

# Run benchmark to narrow the regression
kairix benchmark run --suite suites/your-suite.yaml
```

Possible causes:
- Embed failed mid-run, leaving partial re-embed
- Vault file deleted/renamed — previously high-scoring chunk is gone
- Dimension mismatch (check content_vectors for mixed embedding models; run `kairix embed --force` to rebuild)

---

## Root Cause C — Binary Upgrade Introduced Regression

If NDCG dropped immediately after `kairix` was upgraded to a new version:

```bash
# Check installed version
kairix --version
```

**Rollback to previous version:**

```bash
pip install git+https://github.com/three-cubes/kairix@<PREVIOUS_TAG>
kairix onboard check
kairix benchmark run --suite suites/your-suite.yaml
```

---

## Root Cause D — Measurement Error

Before assuming a real regression, rule out measurement issues:

```bash
# Re-run benchmark twice — scores should be stable within ~0.01
kairix benchmark run --suite suites/your-suite.yaml
kairix benchmark run --suite suites/your-suite.yaml

# Check if gold paths in the suite still exist in the index
# (vault reorganisation can break gold path references)
tail -20 ${KAIRIX_DATA_DIR:-/var/lib/kairix}/logs/kairix-embed.log | grep -i "gold suite"
# "WARN: N/M gold paths missing" → suite needs rebuilding, not index
```

If the gold suite itself is stale: `kairix benchmark init` to scaffold a new suite, then curate it.

---

## Verify Fix

```bash
# Benchmark must pass
kairix benchmark run --suite suites/your-suite.yaml

# Live search sanity check
kairix search "platform architecture"
kairix search "embedding cron"
# Results: BM25=N, vec=M (vec > 0), top result relevant

# System health
kairix onboard check
```

---

## Related

- [how-to-run-benchmark](how-to-run-benchmark.md) — full benchmark procedure
- [how-to-upgrade-kairix](how-to-upgrade-kairix.md) — safe upgrade with eval gate
- [INDEX](INDEX.md) — full runbook registry
