# suites/

Benchmark query suites consumed by `kairix benchmark run`. Each YAML
file lists queries with expected-hit assertions; runs produce a
score-and-recall report that gets diffed against the pinned baselines
in `../benchmark-results/`.

- `contract-suite.yaml` — small contract set, runs in CI's benchmark gate
- `reflib-contract-suite.yaml` — reference-library contract set
- `reflib-gold-v3.yaml` — full gold suite for retrieval regressions
- `example.yaml` — template for adding new suites

Gate thresholds (overall / temporal / entity / contextual_prep) live in
`pyproject.toml` under `[tool.kairix.benchmark.gates]`. See
[docs/evaluation/EVALUATION.md](../docs/evaluation/EVALUATION.md) for
adding a suite and interpreting the report.
