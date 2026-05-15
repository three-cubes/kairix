# Runbooks — operator-facing recovery playbooks

You landed here because someone (probably you, or an agent) needs to bring a degraded kairix deployment back to green. Pick the runbook that matches your symptom — if none matches, start with retrieval health, which is the most common cross-cutting failure mode.

These are the **VM-facing** runbooks. Docker-compose and how-to-style operational guides live in [`../operations/runbooks/`](../operations/runbooks/INDEX.md).

| Symptom | Runbook | What it covers |
|---|---|---|
| Search wrong / empty, recall canary regressed, `kairix onboard check` reports failures | [kairix-retrieval-health](kairix-retrieval-health.md) | Diagnosis tree by failed subsystem (secrets → vector → BM25/FTS → agent knowledge → chunk_date → Neo4j); per-failure recovery; full reset; recall-canary regression triage |
| Package upgrade, unit-file change, secrets-fetcher change on a systemd-managed VM | [kairix-systemd-update](kairix-systemd-update.md) | Safe update sequence, in-flight drain, rollback, verification |
| Alpha-tag gate from `release-vm-deploy.yml` failing or stuck pending | [kairix-alpha-deploy-webhook](kairix-alpha-deploy-webhook.md) (lives in `three-cubes/tc-agent-zone`) | Bootstrap, install, triage for the VM-side webhook that runs onboard-check + benchmark on each alpha tag |

## Pre-flight before any recovery

Run this first, every time. If it returns green, you do not have a kairix-side problem — investigate the symptom instead (network, client config, transport).

```bash
kairix onboard check --json | jq '{passed, total, fully_passed, failures}'
```

A green envelope (`fully_passed: true`) with the dogfood agent still reporting problems usually means the issue is in the agent's MCP client config, not in kairix. Check [`../operations/MCP-CLIENT-MIGRATION.md`](../operations/MCP-CLIENT-MIGRATION.md).

## When you finish

After a recovery, confirm a real agent session produces the expected behaviour — the canaries and onboard-check probes are necessary but not sufficient signal. The primary signal is whether the dogfood symptom is resolved.

## Filing an issue

If a runbook does not resolve the symptom, or you hit a failure mode the runbook does not cover, open an issue at <https://github.com/three-cubes/kairix/issues> with title `<runbook-name>: <symptom>` and attach the diagnostic artefacts the relevant runbook lists in its escalation section.

## See also

- [`../operations/runbooks/INDEX.md`](../operations/runbooks/INDEX.md) — docker-compose / how-to operational guides
- [`../operations/OPERATIONS.md`](../operations/OPERATIONS.md) — full operations reference
- [`../agents/ADMIN-CONVERSATION.md`](../agents/ADMIN-CONVERSATION.md) — what an agent should say to its admin when kairix is degraded
