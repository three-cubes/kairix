# benchmark-results/

Pinned baseline JSON snapshots from `kairix benchmark run`. Each file is
the gold reference for one suite: `contract-baseline.json` (the contract
suite under `suites/contract-suite.yaml`), `reflib-contract-baseline.json`
(reference-library contract suite), and `reflib-gold-v3-baseline.json`
(reference-library gold v3 query set).

CI's `1a · Benchmark gate` and `1b · Reference library benchmark gate`
diff fresh runs against these baselines and fail merge on regression.

When a baseline drift is intentional, regenerate with
`kairix benchmark run --suite <name> --write-baseline` and commit the
diff in the same PR as the underlying change. See
[docs/evaluation/EVALUATION.md](../docs/evaluation/EVALUATION.md) for
the full methodology.
