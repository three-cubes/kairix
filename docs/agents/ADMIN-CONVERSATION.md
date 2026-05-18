# Admin conversation: scripts for talking to your kairix admin

You are an agent connected to a kairix deployment. When kairix is misconfigured, degraded, or returning wrong results, you should not try to fix it yourself — that's an admin action and you do not have host shell access. Instead, follow the scripts below: each maps an **agent symptom** to **exact words you can say to your admin** and **the file or command they need to act on**.

The lines under each "Script:" heading are written for you to say verbatim to your human or to your admin. Do not paraphrase them — they are designed so the admin can act on them without further translation.

---

## "I need bootstrap context at session start"

**Symptom:** your session starts without role / board / sprint context. You're reacting to user prompts instead of being oriented when you arrive.

**Script:**

> Ask your admin to verify three things in `openclaw.json`:
>
> 1. `plugins.entries.kairix-memory-prompt.hooks.allowPromptInjection` is set to `true`
> 2. `kairix-memory-prompt` appears in `plugins.allow`
> 3. `kairix-memory-prompt` is on a path listed in `plugins.load.paths`
>
> Without these three, my session-start prompt does not include kairix context and I will be reactive instead of oriented. Once they are set, restart my session.

**Files / commands the admin needs:**

- `~/.openclaw/openclaw.json` — the config file
- The plugin ships with kairix at `/opt/kairix/plugins/openclaw/memory-prompt/` in the container image and at `<site-packages>/kairix/plugins/openclaw/memory-prompt/` for non-Docker installs. Set `plugins.load.paths` to `/opt/kairix/plugins/openclaw` and openclaw discovers it on startup. Full plugin notes live in `kairix/plugins/openclaw/memory-prompt/README.md` in the kairix repo.

---

## "My `health.secrets_loaded` is false"

**Symptom:** a kairix tool response shows `health.secrets_loaded: false`, or `kairix onboard check` reports `secrets_loaded: not ok`. Vector search and synthesis features are offline.

**Script:**

> Tell your admin: kairix cannot reach its credentials. On the host, ask them to run:
>
> ```
> systemctl status kairix-fetch-secrets.service
> kairix onboard check
> ```
>
> The most common cause is the secrets-fetch unit being disabled after an investigation. The fix is:
>
> ```
> sudo systemctl enable --now kairix-fetch-secrets.service
> ```
>
> Then confirm `/run/secrets/kairix.env` is populated and re-run `kairix onboard check`. Until this is resolved, I am running BM25-only and synthesis features (`tool_brief`, contradiction checks) are offline.

**Files / commands the admin needs:**

- `systemctl status kairix-fetch-secrets.service` — current state of the secrets-fetch unit
- `/run/secrets/kairix.env` — the tmpfs file the unit writes to (should contain `KAIRIX_LLM_API_KEY`, `KAIRIX_LLM_ENDPOINT`)
- `kairix onboard check` — full diagnostic

---

## "I want to be indexed against a different document collection"

**Symptom:** your default search results miss content you know is in the team's reference library or a sibling agent's workspace. You can see the collection exists but it is not in your default scope.

**Script:**

> Ask your admin to edit `kairix.config.yaml`. Each collection has an `in_default: true|false` field:
>
> - `in_default: true` means I see this collection in default searches
> - `in_default: false` means it is indexed but only available when I explicitly scope to it (with `--collection <name>` on the CLI or the `agent` parameter on `tool_search`)
>
> If you want me looking at the team's reference library by default, set `in_default: true` on that collection. If you want to keep large reference corpora out of default results but still searchable, leave it `false` and I will reach for it deliberately when the query warrants it.
>
> After the edit, ask them to re-run `kairix embed` so the index reflects the new scope.

**Files / commands the admin needs:**

- `kairix.config.yaml` (referenced via `KAIRIX_CONFIG_PATH` if not at the default location) — see `kairix.example.config.yaml` in the repo for the full schema
- `kairix embed` — re-runs the indexer after collection changes

---

## "The benchmark suite is failing"

**Symptom:** quality of search results has visibly regressed — wrong documents are ranking higher than they used to, or recently-added content is not surfacing.

**Script:**

> Ask your admin to confirm the bundled benchmark suites are reachable, then run the canonical health-of-search benchmark:
>
> ```
> kairix benchmark list
> kairix benchmark run reflib
> ```
>
> If the weighted score is below 0.85, something has regressed in the search pipeline. Ask them to check the docker logs for embed worker errors and the most recent eval results:
>
> ```
> docker compose logs --tail=200 kairix-worker
> ```

**Files / commands the admin needs:**

- `kairix benchmark list` — lists bundled suites (lands in #222)
- `kairix benchmark run reflib` — canonical search-quality benchmark (lands in #222)
- `docker compose logs kairix-worker` — embed worker logs

---

## "My results look stale"

**Symptom:** you can see content you know was written recently, but `tool_search` is not finding it; or `tool_search` is returning old versions of documents that have since been updated.

**Script:**

> Ask your admin to check whether the kairix worker is running and whether the most recent embed run completed:
>
> ```
> docker compose ps                # if Docker
> systemctl status kairix-worker   # if VM systemd
> kairix worker status
> ```
>
> In the `kairix worker status` output, look at `Last embed` (how long ago the worker last ran) and `Last embed did work` (whether it actually embedded anything new). If `Last embed` is hours stale but new content has been written, ask them to look at the worker logs for embed errors.

**Files / commands the admin needs:**

- `kairix worker status` — prints last embed time, items embedded, failed chunks, pause state
- `docker compose logs kairix-worker` (Docker) or `journalctl -u kairix-worker` (systemd) — recent worker logs

---

## "I keep getting the same wrong information"

**Symptom:** specific entities are being misclassified or missed. `tool_search` returns the wrong agent's content, or `tool_entity` says `not found` for a known person / company / project.

**Script:**

> Ask your admin to check the entity allowlist in the obsidian vault. The NER model misses some entities by default, and there is an override path:
>
> ```
> 04-Agent-Knowledge/_entity-overrides.md
> ```
>
> Adding the missing entity to that file picks it up on the next `kairix entity suggest` call — overrides are read at call time, no re-crawl needed. The file format is `- "<term>": <LABEL>` (one per line). See [docs/user-guide/entity-overrides.md](../user-guide/entity-overrides.md) for the full grammar and worked examples.
>
> If `tool_search` is returning the wrong agent's content, the issue is collection scope — check the agent's `paths` in `kairix.config.yaml` and confirm the agent name in my search call matches an agent declared there.

**Files / commands the admin needs:**

- `04-Agent-Knowledge/_entity-overrides.md` — entity allowlist (path relative to `KAIRIX_DOCUMENT_ROOT`)
- [`docs/user-guide/entity-overrides.md`](../user-guide/entity-overrides.md) — override-file format reference
- `kairix.config.yaml` — agent declarations and per-agent `paths`
- Issue #166 — entity allowlist mechanism

---

## Quick reference card

| Symptom | Admin action | File / command |
|---------|--------------|----------------|
| No bootstrap context at session start | Verify openclaw plugin config | `~/.openclaw/openclaw.json` |
| `health.secrets_loaded: false` | Enable secrets-fetch unit | `systemctl enable --now kairix-fetch-secrets.service` |
| Wrong default collection scope | Edit `in_default` flags | `kairix.config.yaml` |
| Benchmark score below 0.85 | Run benchmark + check worker logs | `kairix benchmark run reflib` |
| Stale search results | Check worker state + embed logs | `kairix worker status` |
| Specific entity missed / misclassified | Edit entity allowlist | `04-Agent-Knowledge/_entity-overrides.md` |

---

## Related

- [`AGENT-SETUP.md`](AGENT-SETUP.md) — your operating contract; read this first
- [Quick start](../getting-started/quick-start.md) — full install / configure flow for your admin
- [Operations](../operations/OPERATIONS.md) — host-side runbook for your admin
- [Retrieval health runbook](../runbooks/kairix-retrieval-health.md) — the playbook your admin runs when search returns wrong or empty results
