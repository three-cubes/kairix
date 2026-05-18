# Reflib benchmark history

Per-release archive of `kairix benchmark run --suite reflib` results, captured
automatically by `.github/workflows/reflib-history-capture.yml` when a GitHub
release is created. Closes #271.

Each row links to the full JSON snapshot under
`benchmark-results/history/<tag>-<date>.json`. The columns are the headline
quality metrics the orchestrator monitors per release.

## How to read this table

| Column            | Meaning                                                                                  |
|-------------------|------------------------------------------------------------------------------------------|
| `tag`             | Release tag (e.g. `v2026.5.10.1`). Links to the per-tag JSON.                            |
| `date`            | Date the benchmark was captured (ISO 8601).                                              |
| `weighted_total`  | Suite-weighted retrieval quality (categories x weights). Higher is better. Range ~[0,1]. |
| `NDCG@10`         | Normalised discounted cumulative gain at depth 10. Higher is better.                     |
| `Hit@5`           | Recall@5 — fraction of cases where at least one gold doc is in top 5.                    |
| `conceptual`      | Category score: open-ended "how does X work" queries.                                    |
| `recall`          | Category score: exact gold-path match.                                                   |
| `temporal`        | Category score: time-anchored queries.                                                   |
| `entity`          | Category score: named-entity / person / project queries.                                 |
| `multi_hop`       | Category score: queries that require linking two facts.                                  |
| `procedural`      | Category score: "how do I do X" runbook-style queries.                                   |

## Comparable signal, not absolute scores

The CI workflow runs against a **fixed fixture corpus** (the same 30-document
sample under `tests/integration/reflib_fixture/`) so cross-release deltas are
meaningful. Absolute scores are **not** directly comparable to production
runs against the live reference-library index — for that, see
`benchmark-results/reflib-gold-v3-baseline.json` (the single pinned baseline
the merge gate still uses; this archive is additive, not a replacement, until
#272 / #273 land).

The first row below is **backfilled** from the existing pinned baseline so
the history starts with the v3 baseline era (2026-05-02). Subsequent rows
are captured automatically per release.

## Releases

| tag | date | weighted_total | NDCG@10 | Hit@5 | conceptual | recall | temporal | entity | multi_hop | procedural |
|-----|------|----------------|---------|-------|------------|--------|----------|--------|-----------|------------|
| [v2026.5.10.1](v2026.5.10.1-2026-05-02.json) | 2026-05-02 | 0.901 | 0.990 | 0.990 | 0.979 | 0.929 | 0.702 | 0.860 | 1.000 | 1.038 |
