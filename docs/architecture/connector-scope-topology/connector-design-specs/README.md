# Connector design specs

Per-connector implementation contracts for Wave E of the connector-scope-topology migration. Each spec covers the same five operating dimensions:

1. **Functions / actions** — current → target method surface mapped to source-side API endpoints
2. **Observability** — counters, gauges, structured-log events, where they surface
3. **Agent affordance** — MCP tools + CLI verbs (read-only vs side-effecting)
4. **Failure modes / proactive resolution** — detection → reactive baseline → proactive behaviour → escalation
5. **Performance** — references `../05-non-functionals.md` for the row; no restating

Plus four cross-cutting additions every spec carries:

6. **Retrieval-quality contract** — per-connector gold-suite shape that catches IM-6-class regressions on this connector specifically
7. **Implementation sequence** — explicit order of methods to land (lowest-novelty-risk first)
8. **Test-fixture corpus contract** — what fixtures the E2E ships with under `tests/fixtures/<connector>/`
9. **Expected F-rule impact** — which F-rules the slice touches and how it lands at zero violations (there are no baseline files since PLA-472), so cherry-pick review has the comparison point

## Index

| Connector | Spec | Status | Wave E status |
|---|---|---|---|
| SharePoint | [`sharepoint.md`](sharepoint.md) | ✅ canonical (the bar) | shipped behind `connector_sharepoint` + `topology_sharepoint` |
| Slack | [`slack.md`](slack.md) | ✅ design spec | shipped behind `connector_slack` + `topology_slack` |
| GitHub | [`github.md`](github.md) | ✅ design spec | shipped behind `connector_github` + `topology_github` |
| Notion | [`notion.md`](notion.md) | ✅ design spec | shipped behind `connector_notion` + `topology_notion` |
| Linear | [`linear.md`](linear.md) | ✅ design spec | shipped behind `connector_linear` (MVP: incremental poll + API-key auth; Container/per-team scoping deferred) |
| Google Drive | (next) | — | backlog — spec when the shipped connectors complete cutover soak and validate the proactive-failure-mode patterns |
| Jira | (last — needs `01-source-analysis` profile first) | — | backlog |

## Ordering rationale

Slack first because its proactive-failure-mode design carries the most novel surface (Socket Mode reconnect, rate-limit backoff, workspace-admin app removal) — once that's canonical, GitHub abuse-detection / token-rotation and Drive sharing-change failure modes inherit cleanly. After Slack + GitHub land, extract a shared proactive-failure-mode template so remaining specs reference rather than restate. Jira last because it lacks an `../01-source-analysis.md §Jira` profile and needs source research first.

## How to use a spec

- **Reading**: §0 is the fastest orientation — "current state → target" + the mermaid diagram. §2 is the implementation contract for a subagent.
- **Implementing**: follow the §7.5 implementation sequence end-to-end. Each method's acceptance criterion is the §2 row.
- **Reviewing a cherry-pick**: §8 names the expected F-rule impact; if reality differs, ask why before merging.
- **Operating**: §3 + §4 are the runbook — the dashboards / log queries / failure-mode catalogue.

## See also

- [`../ADR.md`](../ADR.md) — topology architectural decision record
- [`../01-source-analysis.md`](../01-source-analysis.md) — per-source API research
- [`../05-non-functionals.md`](../05-non-functionals.md) — performance envelopes (specs reference, don't restate)
- [`../08-chunking-and-entity-strategies.md`](../08-chunking-and-entity-strategies.md) — Wave F chunker designs (downstream of these specs)
