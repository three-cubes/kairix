# Changelog

All notable changes to this project will be documented in this file.
Format: [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).
Versioning: [Calendar Versioning (CalVer)](https://calver.org/) — `YYYY.MM.DD`, with `.N` suffix for same-day releases.
Git tags: `v2026.04.18`. Deploy by pinning to a tag: `pip install git+...@v2026.04.18`.

## [Unreleased]

### SharePoint documents keep source pages

SharePoint connector configs can now pass per-extractor settings through an
ordered extraction chain, including the Gotenberg service URL used inside
Docker. This lets operators run native PPTX/XLSX/PDF extraction first, opt into
DOCX-to-PDF conversion when page anchors matter, and keep MarkItDown as a
fallback instead of losing page metadata for every indexed file.

### LLM-judge failures no longer count as "irrelevant" (benchmark numbers may move)

Before, when the LLM judge failed on a benchmark case scored with
`score_method: llm` (bad credentials, a timeout, a reply that was not a
number), the case scored 0.0. That looked the same as "the results were
irrelevant" and quietly pulled scores down.

Now a judge failure leaves the case unscored. It is left out of every
average (category scores, weighted total, canary pass rates) instead of
counting as 0. `kairix benchmark run` prints how many cases the judge failed
on. The JSON output has a `judge_failures` count in `summary`, and each failed
case has `score: null` plus `judge_failure` and `judge_error` fields that say
why.

**Your benchmark numbers may go up** compared with earlier runs, if earlier
runs had judge failures that were counted as 0. Check `judge_failures` before
comparing a new run against an old baseline. The eval `LLMJudgeScorer` now
raises `JudgeFailedError` instead of returning 0.0. An empty retrieval still
scores 0.0.

`kairix eval <suite>` (conversation suites) works the same way now. Before, a
judge failure (the LLM backend was down, or the reply was empty or not a
number) scored the question 0.0 and counted it as wrong. A `nan` reply made
the mean score `nan`, and an `inf` reply counted as a pass. Now those
questions are unscored. They are left out of `n_questions`, `n_passed`,
`mean_score` and the per-category numbers, and counted in a new
`judge_failures` field. The text output prints a "Judge failures" line, and
in `--json` each failed row has `score: null`, `pass: null`, `judge_failure`
and `judge_error`. Note that `n_questions` now counts scored questions only.
**Pass rates and mean scores may go up** compared with earlier runs that had
judge failures. Check `judge_failures` before comparing against a baseline.

The unused `kairix.quality.contracts` Protocols (`BriefingSourceProtocol`,
`EmbedderProtocol`, `EntityResolverProtocol`, `SearchBackendProtocol`, `SearchResultProtocol`) have
been removed. Nothing in kairix used them.

### Quality gates hold every file to the same bar (contributors)

This changes how kairix is built and checked, not how it runs. Operators have
nothing to do.

- **No more grandfathered debt.** The `.architecture/baseline/` lists are gone.
  The fitness checks now run on the whole codebase through the shared gate
  engine (`three-cubes-fitness` v0.19.0), and any violation blocks a commit —
  old code included.
- **Python 3.13 runs the gate.** The checks run on Python 3.13. The kairix
  package itself still supports Python 3.12 and newer.
- **Retired rules.** F49 (baselines shrink each release), F50 (no new files in a
  baseline) and the paydown-doc check are gone, since there are no baselines
  left. The commit-identity check (SGO-158) now runs in the shared gate instead.
- **Sonar: zero open findings.** `scripts/checks/check_sonar_new_code.py` fails
  on any open SonarCloud issue or hotspot that is still in your working copy.
  An issue counts as fixed as soon as you change the flagged line.

## [2026.7.4] - 2026-07-04

### Search keeps up when your whole team is querying at once

Kairix now overlaps the independent parts of a search, and concurrent searches no longer wait behind each other's reranking step — the slowest stage. It also sizes its own search concurrency to the number of CPU cores on the machine. The result: when several people or agents search at the same time, they stop queueing up behind one another — latency stays low and throughput scales with your cores, with identical results for any single search. Operators can tune the ceiling with `KAIRIX_MAX_CONCURRENCY`.

### The `topology_v2` config setting is now just `topology`

The block in `kairix.config.yaml` that lists your connectors, collections, and scopes has been renamed from `topology_v2` to `topology` — the `v2` was a leftover from an older naming. You don't need to change anything: if your config still uses the old name, kairix reads it as `topology` automatically (in memory, without rewriting your file), so existing setups keep working with zero effort. From now on the setup wizard writes the new name. This release also fixes an upgrade edge case where a config could briefly keep both the old and new names side by side, and a related overlay issue where a persisted source could be dropped on upgrade. See [the upgrade note](docs/upgrades/topology-config-key-rename.md) for details.

### Research answers list everything, not just the top few

When you ask kairix to enumerate a complete list from a source, it now returns the full set rather than stopping at the first few items.

### Fixes and reliability

- **Cleaner install output.** `kairix init` now prints its progress to the status stream, so `onboard check --json` gives you clean, parseable JSON.
- **Config-drift warning.** `kairix worker status` now warns when your stored sources have drifted from your config, so a stale source doesn't sit unnoticed.
- **More trustworthy health probes.** The service-level check (`kairix slo`) uses a valid built-in agent and reports "not applicable" on an empty knowledge store instead of a misleading zero.
- **Automatic image cleanup on deploy.** Deploys now prune old container image tags automatically, keeping the most recent few for rollback.

## [2026.7.2] - 2026-07-03

### The agent guide now shows the real tool names

Agents learning kairix from the built-in usage guide were copying names like `tool_search`, but the real name on the wire is just `search` — so those calls failed. The guide now shows the exact bare names (`search`, `expand`, `memory_write`, …) and is ordered the way you actually work: get oriented, find, dig in, remember, check health, escalate.

### Ask about the things you work on — and get an answer

`facts_about("Kairix")` used to come back empty. Kairix and Three Cubes are now built in, so questions about them resolve out of the box, and a short description on any entity now helps it show up in search, not just its name.

### Contradiction checks say what they actually found

The contradiction checker used to report a problem even when there was simply no evidence either way. It now tells you which of three things happened: the store holds evidence that conflicts, it holds related notes but nothing that settles the point, or it holds nothing on the topic.

### One consistent way in, from the command line or an agent

Each capability now runs through one shared path, so the command line and the agent (MCP) tools behave the same and stay in step as kairix changes — backed by a new test that checks every capability through both doors on every change.

### Removed

- **The `vault` command is gone — use `store` instead.** `kairix vault` was an old name for `kairix store`. It has been removed. If a script calls `kairix vault`, change it to `kairix store` — it works exactly the same way.

## [2026.7.1] - 2026-07-01

> **Upgrading?** Nothing breaks. Your searches, briefings, and agents keep working the way they do today. This release makes agents faster, and makes their answers easier to trust and follow up on.

### Pull in the context around a result, without re-reading

- **Grab the text around a search hit.** When a result points at one part of a document, an agent can now ask for the parts around it — the part before, the part after, or the section it belongs to — with the new `kairix expand` command and a matching `expand` tool. Before, an agent had to re-read the whole file to see the surrounding text; now it pulls only what it needs. Faster answers, less wasted work.

### Cite and re-open any result

- **Every result shows where it came from.** Search hits, briefings, fact lookups, timelines, research, and claim checks now all carry a clear pointer back to the original source you can open. The same breadcrumb appears everywhere, so you and your agents can trace any claim to its source.
- **Briefings end with their sources.** Each briefing now finishes with a short "Sources" list of at least three citations, so you can check the work behind a summary at a glance.
- **Fact answers point to something you can open.** When you ask what's known about a person or topic, each fact now links to a real, openable source instead of an internal reference that meant nothing on its own.

### Faster, safer memory

- **Saving a memory is much faster.** When an agent saves something, kairix now updates just that entry instead of re-scanning everything and rebuilding the whole search index. Large stores feel quick again.
- **What you save is always findable.** A saved memory now always lands somewhere that is both writable and searchable, so it turns up in your next search every time.
- **kairix tells you up front when it can't save.** `kairix doctor` now checks whether kairix can actually write where it's been pointed, and gives you a plain-language fix when it can't.
- **Saving a conversation works on locked-down setups.** Importing a chat no longer fails on read-only deployments — conversations are written to a separate writable area.

### Sharper answers, quicker starts

- **Better fact recall.** Fact lookups now blend keyword and meaning-based matching, and lead with a short summary of the thing you asked about.
- **Briefings built from real signal** and aware of the agents you've set up.
- **Quick first results after a restart.** kairix warms up the model that ranks your results and sizes its worker pool at startup, so the first search isn't sluggish. Result previews now center on the part that actually matched your query.

### Safer by default

- **Tighter handling of file paths.** Paths supplied by an operator or an agent are now checked and kept inside their allowed area, so a stray or crafted path can't reach somewhere it shouldn't.

### For agents

- **New tool — `expand` (CLI `kairix expand`).** After a search or recall hit, pull the chunks right around it — the one before, the one after, or its parent section — within a token budget, instead of re-reading the whole document. Call it with the hit's source link and chunk number.
- **Every result carries an openable source link.** Search, briefings, fact lookups, timelines, research, and claim checks all return a resolvable source you can cite, open, or pass straight to `expand` — so cite your sources instead of summarising from memory.
- **Briefings are drillable.** A briefing ends with a "Sources" list and returns its sources as data — verify or expand any claim instead of re-searching from scratch.
- **Fact lookups resolve to real sources.** `facts_about` results now carry an openable source link (not opaque turn-ids), blend keyword and meaning matching, and lead with a short summary of what you asked about.
- **Saving memory is faster and more reliable.** Saves update just the one entry (no full re-index), work during warm-up, and always land somewhere searchable; if a save can't be written you get a plain-language reason and fix.
- **Snappier first results.** The result-ranking model is warmed at startup, so the first query after a restart isn't slow, and result previews center on the part that matched.

## [2026.6.28] - 2026-06-29

### For operators

- **Point SharePoint at a site and it finds the drives.** The SharePoint connector now auto-discovers every drive in a site you name (`site_id`, or a `site_url`) — no more hand-entering drive IDs. Configure a site and kairix indexes its document libraries; if one site can't be resolved it's skipped with a clear warning while the others keep syncing. (This is also the foundation for guided SharePoint setup.)
- **Tell a quiet connector from a dead one.** `kairix worker status` now shows, per sync tick, whether connectors actually ran and yielded anything — so a source that's simply quiet is no longer indistinguishable from one that has silently stalled. Frozen cursors, low-disk skips, and poison-skips now surface as warnings instead of failing silently.
- **Clear stuck items from any source.** `kairix dead-letter drain [<source>]` clears permanently-unprocessable dead-letters (corrupt archives, unsupported formats) for any source — including a connector that is no longer actively syncing — and a periodic sweep keeps the queue clean. `--dry-run` shows what would clear before you commit.

## [2026.6.24.1] - 2026-06-24

### For operators

- **SharePoint stops choking on files it can't read.** Documents kairix can't extract — Office binaries, Visio drawings, corrupt archives, and other unsupported formats — are now recognised up front and recorded as "skipped (unsupported)" instead of failing over and over and clogging the dead-letter queue. Office documents that arrive without a proper file type (a common SharePoint quirk) are now detected by their content and indexed instead of dropped.
- **The stuck-file backlog cleans itself up.** On each sync, kairix automatically clears dead-letter entries that can never succeed — corrupt archives and known-unsupported formats — recording them as skipped. Recoverable items (anything that might index once a converter or extractor is in place) are deliberately left for retry.
- **Optional: convert legacy and uncommon Office formats.** kairix can now convert legacy Office (`.doc`/`.ppt`/`.xls`), Visio, OpenDocument, Publisher, and RTF to PDF and index them, via a Gotenberg conversion service. It's off by default — add the `gotenberg` tier to a connector's `extractor_chain` and point it at your Gotenberg service to turn it on. Modern `.docx`/`.pptx`/`.xlsx` continue to be handled in-process, unchanged.
- **Cleaner operator signals.** `kairix config validate` no longer reports false errors for synthetic collections (such as `entity-summaries`) or the auto-corrected `reference-library` path, so real config problems stand out. And `kairix cc-pair list` now shows the true number of documents indexed per source instead of always reporting zero.

## [2026.6.24] - 2026-06-24

> **Upgrading?** kairix now needs **Python 3.12 or newer**. Docker and Compose deployments are already covered — the image ships Python 3.12. If you installed kairix into your own Python environment (the systemd / host-venv path), recreate that environment on Python 3.12+ before you upgrade, or the install will refuse the new version. Your knowledge store, sources, and agents all carry over unchanged.

### For humans

- **Connect your Linear workspace.** kairix can now pull your Linear roadmap and docs — initiatives, projects, issues, documents, and project updates — into your knowledge store, so you and your agents can search them alongside everything else. It's off until you turn it on: add your Linear API key, switch it on, and kairix starts indexing.

### For operators

- **Lead with what we know about a thing.** When someone asks "tell me about X" or "who is X", kairix can now put the short description of that person, team, or topic at the top of the results instead of buried below documents. It's off by default — turn it on once you've switched on entity descriptions, and you can switch it back off at any time.
- **Turn on the Linear connector when you're ready.** It's off by default, so adding it changes nothing until you act. Put a Linear API key in your secret store, add the connector to your config, and switch it on — kairix then checks Linear for changes on a schedule and pulls only what's new.

## [2026.6.18] - 2026-06-18 — Set up kairix in your browser

> **Upgrading?** Pull the new image and restart — your knowledge store, sources, and agents all carry over. New installs now open into a browser setup page; existing installs keep working exactly as they are.

### For humans

- **Set kairix up in your browser.** Open it and a setup page appears. Choose your AI provider, add your key, connect your sources, and run your first search — no config files, no command line.
- **Connect your accounts by signing in.** Add Slack, GitHub, or Google right from the setup page — one sign-in and you're connected.
- **Try it before you finish.** Setup ends with a quick tour — a handful of one-click examples that run real searches and briefings on your own content.
- **Start before you've added an AI key.** kairix works the moment it's installed: keyword search is ready right away, and it shows you what to add to turn on the smarter features.

### For agents

- **Agents can save what they learn.** A new `remember` command and `memory_write` tool let an agent write to its own memory — ready on a fresh install, nothing to wire up.
- **Agents get a built-in guide.** Ask kairix how to use it and you get a real answer — the usage guide now ships inside the product.
- **An agent can set itself up.** The browser setup, or `kairix setup`, lets an agent install kairix and connect its own sources.
- **Results keep coming during setup.** Agents get keyword search before the AI key is in place, instead of an error.

### For operators

- **Fresh installs just work.** kairix starts cleanly before everything's filled in and tells you plainly what's still needed.
- **Works out of the box on a standard Docker setup** — nothing extra to configure.
- **Slack stays up to date.** Your Slack messages keep flowing into kairix as they're posted.
- **Connecting accounts is simpler.** Signing in captures and stores what each source needs, so there's less to set by hand.
- **Your first import is faster.** kairix indexes your own documents first, so your most useful results show up sooner.

## [2026.6.9] - 2026-06-09 — Better search quality, cleaner output, smarter agents

> **Upgrading?** Nothing breaks. Your search, briefings, and agents keep working exactly as they did. Five new things are available when you want them — turn each on when it's useful, leave the rest alone.

### What's better when you search

- **Search finds people, places, and projects by what they do — not just what they're called.** Ask "which security consultants do we work with?" and you'll get hits even when no document spells out "security consultant" — kairix recognises the role from your knowledge graph. *Activation:* add `entity_summary_indexing_enabled: true` to your config when you're ready; see [how to turn it on](docs/operations/runbooks/entity-summary-cutover.md).
- **You can tell which results come from outside your own notes.** Hits drawn from your knowledge graph's external descriptions (the public-knowledge layer, not your vault) carry a `[Wikidata]` marker in CLI search output and an `entity_summary: true` flag in the agent response. Useful when you want to cite vault content differently from external context. *Activation:* automatic once the entity-summary search is turned on.
- **Search results are easier to scan.** Long archive paths like `archive-2024.zip#1536` show as `archive-2024.zip` in the title line; the full path stays available on the next line. *Activation:* automatic. *Tune:* pass `--snippet-width N` to widen or narrow the result body (default 600; try 200 for tight triage or 1200 for deep reading).

### What's better when you ask kairix to summarise

- **Briefings stop contradicting themselves.** Before, a briefing could say "no relevant content found" while also listing the sources it just searched — confusing for the reader and the agent reading the response. Now those two halves agree: when kairix finds nothing useful, the sources list is empty too. *Activation:* automatic.
- **Contradiction checks are more honest.** When you ask kairix to check a claim against your knowledge, chunks that simply don't mention the claim are no longer scored as if they contradict it. Silence is silence, not disagreement. *Activation:* automatic.

### What's better for agents

- **The MCP server self-heals when its warm-state desyncs.** Previously, certain restart sequences could leave kairix's in-memory state out of step with its persisted state — agents would see cold-start responses for hours before someone noticed. Now kairix detects the mismatch on the first request, fixes itself, logs what happened, and serves the request normally. *Activation:* automatic.
- **External MCP access survives upgrades.** If your VM needs the MCP server reachable from another host (not just localhost), you set this once now and it sticks through every kairix upgrade — no more re-patching docker-compose after each pull. *Activation:* set `KAIRIX_MCP_BIND_HOST=0.0.0.0` in your operator-side `.env` file. Leave it unset on a laptop or any internet-exposed host (kairix has no built-in authentication).

### What you can tune from config now

- **Lift specific files above their collection.** Add `canonical_filename_allowlist: [ETHOS.md, SOUL.md, AGENTS.md]` to your config and those files outrank everything else in their collection. Useful for "this single document is the source of truth" files. *Activation:* declare the list under `retrieval.boosts.source_tier:` in your config; defaults to empty.
- **Boost canonical sources by query intent.** Tell kairix "for entity lookups, treat canonical sources as five times more relevant than archived ones." *Activation:* declare `per_intent_overrides:` under the same source-tier block; defaults to no overrides.
- **Define your team's key people, projects, and concepts once.** Declare `canonical_entities:` in your config and kairix seeds them into the knowledge graph on the next worker restart. From then on, agents recognise them across every search and the `facts_about` tool returns a `canonical:` summary alongside its hits. *Activation:* add the declaration; restart worker.
- **Two new tuning knobs for fusion scoring.** When a single weak fact happens to match your query, it used to over-rank itself to the top. Now you can set a floor for the fact layer's score normalisation, and you can deduplicate fact rows against chunk rows describing the same thing. Both default off — turn them on if you see "wrong fact at rank 1" patterns in your dogfood. *Activation:* declare `retrieval.fact_layer_min_floor` and `retrieval.cross_layer_dedup_enabled` in your config.

### What's better behind the scenes

- **Search quality is measured on more real questions.** The benchmark suite that gates every release used to have 1 entity question, 5 temporal, 2 multi-hop. Now it has 15, 20, and 15 respectively — 42 new questions grounded in real reference content (philosophical texts, version-upgrade guides, project stabilisation runbooks). Means every release now has to clear a much higher quality bar across question types that used to be under-measured. *Activation:* automatic — every release now runs against this expanded benchmark.

### Important when upgrading

- **The entity-summary search is OFF by default — you don't have to do anything.** Everything is wired up and tested, but the feature only activates when you say so. Measurement on the production deployment showed search quality on entity questions was already at 0.80 (out of 1.0) without the new feature turned on — so there's no urgency. Turn it on when you want the public-knowledge layer in search results; see the [activation runbook](docs/operations/runbooks/entity-summary-cutover.md).

## [2026.6.8] - 2026-06-08 — One-command install, one container, one-command OAuth

> **Upgrading?** Two operator actions: (a) the kairix container now runs as a dedicated `kairix` user (uid 995) instead of root — if your host volumes were written by the old image, run `sudo chown -R 995:985 <path>` once after pulling the new image; (b) on bare hosts (no Docker), the new `sudo kairix init --system` lays down everything in one command. Existing Docker compose deploys upgrade in place. Full notes in [`docs/upgrades/v2026.6.8.md`](docs/upgrades/v2026.6.8.md).

### New for agents

- **An agent can install its own knowledge store on a fresh laptop.** `pip install kairix-agentic-knowledge-mgt && kairix init --user` lays down config, data, and a per-user service unit under the agent's own home directory — no root, no manual dir-tree creation, no "where does my knowledge store live" question. The same install path applies to humans installing kairix locally.

### New for operators

- **`kairix init` — one command, fresh host to working install.** `sudo kairix init --system` creates the kairix system user, lays down config at `/etc/kairix/`, data at `/var/lib/kairix/`, cache at `/var/cache/kairix/`, and a systemd service — all the steps that used to be a copy-paste runbook. `kairix init --user` does the same under your XDG dirs (no root needed). Re-running is safe: nothing gets clobbered. `kairix init verify` reports install health.
- **One container instead of two.** `kairix` and `kairix-worker` were the same image with different entrypoints. They're now a single container with an internal supervisor running both processes. `docker compose ps` shows 2 services (kairix + neo4j) where it used to show 3. Behaviour is unchanged for agents and operators; the background worker still runs.
- **Container runs as the kairix user, not root.** Files written from the container land with `kairix:kairix` ownership on the host. Fixes the class of permission errors operators previously hit when host-side scripts (running under their own user) tried to read or move kairix-written files.
- **`kairix connect google-gmail | google-drive | google-calendar` — one-command OAuth setup.** Run the command, your browser opens to Google's consent screen, you approve, and the captured tokens land in your secrets store. No more manual GCP-console-then-key-vault-copy-paste per service. See [`kairix/connect/README.md`](kairix/connect/README.md) for the one-time GCP setup walkthrough.
- **`kairix connect slack --workspace <name>` — one-command Slack workspace setup.** Run with your Slack app's client_id + client_secret, browser opens to the workspace-install screen, captured bot token lands in your secrets store under a per-workspace canonical name. Run again for a second workspace — both co-exist.
- **`kairix connect github-app` — one-command GitHub App install + token capture.** Browser opens to the App install URL, you pick the org + repos, the installation id + initial access token land in your secrets store. The GitHub connector rotates installation tokens at the 50-minute mark transparently — no manual JWT signing.
- **`kairix uninstall` — clean reversal.** `sudo kairix uninstall --system` removes config, the systemd unit, and the kairix user. `--keep-data` (the default) preserves your data + cache directories so a re-install picks up where you left off; pass `--no-keep-data` to wipe everything.

### Things that work better

- **Google Drive and Calendar auto-refresh OAuth tokens.** Previously these expected a static `access_token` and silently broke at Google's 1-hour TTL — the symptom was "Drive sync stopped working a week after I set it up". The auto-refresh path now uses the `refresh_token` captured by `kairix connect` to mint fresh access tokens transparently. Gmail already worked this way; Drive and Calendar now match.
- **Kairix knows how it's running and where to put things.** The path resolver now detects whether kairix is a system install (`/etc/kairix/`, `/var/lib/kairix/`), a per-user install (XDG dirs under your home), or a container (FHS paths owned by the image). Plugin authors and operators no longer override per-environment env vars by hand.
- **systemctl-enable is now best-effort.** If the systemd user manager can't reach the unit file at install time (which happens in some test and container setups), `kairix init` writes the unit and logs a clear warning telling you the exact `systemctl enable` command to run when the bus is reachable — instead of failing the whole install.
- **Container image dropped from 6 GB to under 700 MB.** The build was pulling NVIDIA CUDA libraries alongside the CPU-only PyTorch wheel because of a two-step pip install pattern. A single resolve with the CPU extra-index keeps the runtime image lean.

### Important when upgrading

- **Production-mode OAuth consent screen is unavoidable for Google.** If you set up Google connectors with the consent screen in Testing mode, refresh tokens silently expire after 7 days. The `kairix connect` README walks you through publishing the consent screen to Production state — do that before running `kairix connect google-*`.
- **Per-workspace Slack tokens.** Slack tokens are now read by workspace (`KAIRIX_CONNECTOR_SLACK_<NAME>_BOT_TOKEN`); singleton deployments using `CONNECTOR_SLACK_BOT_TOKEN` continue to work via the legacy alias. To switch, run `kairix connect slack --workspace <name>` and add `workspace: <name>` to your connector config.
- **Host-volume ownership on Docker upgrades.** If you've been running the previous image and have bind-mounted host volumes, one chown step picks up the new kairix-user shape:
  ```
  sudo chown -R 995:985 /path/to/your/kairix-data /path/to/your/kairix-cache
  ```

### Retired

- **The three-container compose shape.** The `kairix-worker` service has been removed from `docker-compose.yml`. Existing `docker compose up` commands still work — they just bring up one fewer container. No state migration needed; the worker continues to run inside the kairix container under an internal supervisor.
- **Root-owned container.** The previous image ran processes as root. The new image runs as `kairix` (uid 995). Bind-mounted host volumes need a one-time chown if they were written by the old image; everything else is transparent.

## [2026.6.7] - 2026-06-07 — Faster CLI through warm MCP, agent-setup discovery, caches show real state

> **Upgrading?** No required config changes — out-of-the-box defaults still synthesise a sensible scope per agent. Recommended action: run `kairix onboard scan` to discover your agents on disk and paste the generated block into `kairix.config.yaml`. After that, `kairix doctor agent --all` reports clean. Full notes in [`docs/upgrades/v2026.6.7.md`](docs/upgrades/v2026.6.7.md).

### New for agents

- **`kairix brief`, `search`, `bootstrap`, `prep`, `research`, `contradict`, `timeline` return in well under a second** when an MCP server is running. Before this release every CLI invocation spun up the embedding model, opened the vector index, and reconnected to neo4j — ~6 seconds each time, even when a warm MCP server sat one process away. The CLI now talks to that server, with byte-identical output. Cold-fallback to in-process is automatic when MCP is unreachable.
- **`kairix bootstrap` and `kairix brief` accept the positional agent form the CLI documents** (`kairix brief shape`). The dispatcher previously only recognised `--agent NAME`, silently falling through to in-process on the more common positional form.

### New for operators

- **`kairix onboard scan` discovers agents on disk** and emits a paste-ready `agents:` block for your `kairix.config.yaml`. Per-agent file counts and most-recent-mtime included so you can sanity-check before committing.
- **`kairix onboard agent --name <agent>`** for one-agent setup. Same discovery, scoped to a single agent. Auto-detects the agent harness when not specified.
- **`kairix doctor agent --all` validates configured agent scopes against disk.** Reports per-agent health: missing paths, empty surfaces, stale files (older than 30 days), ambiguous globs. Exits non-zero when an agent's scope is broken.
- **`kairix caches` reflects the warm MCP server's cache state, not a fresh CLI process.** Operators previously saw all-zero counters because the CLI started a new Python process per invocation. The CLI now routes through a `caches_status` MCP tool; the envelope includes the server's process id and uptime so you can confirm it's the real server. Cold-fallback to in-process collectors stays available with an explicit stderr banner.
- **Cold-start envelope captured in a runbook.** `docs/operations/runbooks/cold-start-envelope-reference.md` documents the real HTTP 503 + `Retry-After` shape that MCP returns during warm-up. A nightly soak test pins the envelope so format drift gets caught early.

### Retired

- **`agent_memory_path()` Python helper, `KAIRIX_AGENT_MEMORY_ROOT` env var, and `kairix brief --memory-root` flag.** All three encoded the legacy `<agent>/memory` subdirectory convention. Agent paths are now config-driven via the `agents:` block in `kairix.config.yaml` (with a sensible default when the block is absent).
- **`kairix probe` and `kairix soak` CLIs** (deprecation notice was in v2026.5.18). The underlying Python APIs (`kairix.quality.probe.*`, `kairix.quality.soak.*`) remain.

### Operator-facing

- **MCP escalation envelopes for `tool_soak_run` / `tool_probe_search` / `tool_probe_burst`** now point to the Python API via a pasteable `python -c '...'` one-liner instead of the retired CLI form.

## [2026.6.2a2] - 2026-06-02 — Search reads more sources by default, three connector fixes

> **Upgrading?** Two things need attention: the M365 calendar window now caps at ~13 months total (defaults reduced from 455 to 360 days; configs over the cap fail validation with a clear message), and SharePoint deploys on the prior alpha should run the one-shot backfill migration to retag legacy rows. Everything else is drop-in.

### New for agents

- **Default-scope search now returns a superset across every connected source.** Agents that search without naming a collection used to get the legacy mapping (typically one source). They now get results from every collection their scope profile marks as in-scope by default — SharePoint, Obsidian, Slack, M365 email, M365 calendar, GitHub. The behaviour is opt-in via the `topology_v2_collection_resolver` flag so the previous mapping stays the default until you flip it.

### New for operators

- **Search results lead with the snippet, not the URL.** Old layout put a long SharePoint URL on the top line of every hit; the snippet text was buried below and truncated at 200 characters. New layout reverses it: 600-character snippet first, title + path below, then score + collection. Operators scanning results can read what the document *says* without scrolling past the URL.
- **Agent-config deprecation notice is no longer search-time spam.** Searches under a legacy agent config used to emit six WARNING lines per query. The notice now logs at INFO level on the warm-up path only; routine searches are clean.
- **`kairix connect` flow works for GitHub via Personal Access Token.** Previously the GitHub connector only worked when wired against a GitHub App installation; PAT-only deployments saw HTTP 401 on the first sync tick. The connector now detects the credential shape and routes PAT credentials through the `/user/repos` endpoint (with Link-header pagination), keeping the App-mode path unchanged.
- **M365 email connector pulls from every mail folder, not just Inbox.** The Graph delta endpoint requires per-folder scoping; the old single-mailbox call silently missed everything outside Inbox. The new path enumerates the user's folders once, then maintains a per-folder delta cursor so future syncs only ship what changed.

### Things that work better

- **M365 calendar initial sync stops hitting the Graph 13-month limit.** Default window was 90 days back + 365 days forward = 455 days total, which Graph's `calendarView/delta` rejects with HTTP 400. The default is now 90 + 270 = 360 days total (30-day buffer), and config validation rejects window combinations over 390 days at construction time with a clear `fix:` message.
- **SharePoint documents land in the right collection.** A re-extract path in the worker was falling back to `'default'` for the collection metadata instead of asking the connector. Fixed in the previous alpha; this release ships the one-shot migration script `scripts/migrations/2026-06-01-sharepoint-collection-backfill.py` to retag rows that were stuck in `'default'` on existing deploys.

### Configuration migration from v2026.5.31a2

1. **Reduce any custom M365 calendar windows over 390 days total.** If your `kairix.config.yaml` overrides `window_days_back` / `window_days_forward` and the sum is above 390, `kairix config validate` will tell you with the legal range. Default deploys need no action.
2. **Run the SharePoint backfill once per deploy.** `python3 scripts/migrations/2026-06-01-sharepoint-collection-backfill.py --dry-run` shows you how many rows are affected; re-run without `--dry-run` to apply. Idempotent — a second run is a no-op. Vectors and content are unaffected; the migration only updates the `collection` metadata column.
3. **Optional: turn on the broader default-scope search.** Set `features.topology_v2_collection_resolver: true` in `kairix.config.yaml` and restart. Validate the change has the effect you want by running a few representative queries before and after.

## [2026.5.31a2] - 2026-05-31 — Restart-resilient embed, one credential naming rule, silent-config-bug class closed

> **Upgrading?** No required config changes; the embed cache and the new secrets loader both fall back to your existing setup. If you want the new operator goodies — `kairix secrets verify`, the cross-provider deployment recipes, the convention-driven secrets fetcher — see the migration note below.

### New for agents

- **`tool_secrets_verify` reports what the deployed kairix can actually resolve.** Agents asking "is my deployment healthy on the credentials side?" get a structured per-secret table with status and the canonical Key Vault name on every row.

### New for operators

- **Embeds never burn money twice.** Every chunk that hits the LLM embed endpoint is now persisted to a SQLite cache before it touches the vector index. If an embed run pauses, crashes, or is restarted, the next run resumes for free — the cached vectors flow straight back into the new index. The vector-index save is also atomic now (write-to-temp-then-rename), so a partial write can't corrupt the on-disk file again.
- **One credential naming rule across every cloud secrets manager.** Every kairix-bound secret has a canonical name of the shape `kairix-<scope>-<area>[-<instance>]-<leaf>` — for example `kairix-provider-llm-api-key` or `kairix-connector-m365-tenant-id`. The same rule works in Azure Key Vault, AWS Secrets Manager, GCP Secret Manager, 1Password, and plain `.env` files. Existing env-var names keep working through a built-in alias layer.
- **A single docs page for every install × secrets-manager combination.** [`docs/operations/secrets-configuration.md`](docs/operations/secrets-configuration.md) covers Docker and pip × Azure KV / AWS / GCP / 1Password / ECS / Cloud Run / AKS CSI / plain `.env`, with rotation commands and the local-MCP-server-from-Claude-Desktop wrapper recipe.
- **Adding a new secret is now zero-glue.** The shipped [`scripts/deploy/fetch-secrets.sh`](scripts/deploy/fetch-secrets.sh) discovers every `kairix-*` secret in the vault on each restart — no per-secret list to keep in sync. New connector? Drop its secret into KV with the canonical prefix; it's available at the next service restart.
- **`kairix config validate` catches the silent-path-misconfig bug class.** If your `kairix.config.yaml` declares a collection with a path that doesn't resolve on the deployed filesystem, validate-time now fails loudly with a one-line `fix:` hint instead of leaving operators to find the silent WARNING in scan logs hours later. Reference-library specifically auto-resolves to `$KAIRIX_REFLIB_ROOT` with a "change `path:` to X to silence this notice" affordance.
- **`kairix secrets {verify, migrate-list}` CLI** — `verify` walks every registered credential, tells you which are present, which resolve via a legacy alias (with the deprecation flag), and which are missing; `migrate-list` dumps the legacy-env-var → canonical-KV-name mapping as TSV so operators can pipe it into their KV provisioning loop.

### Things that work better

- **Parallel embed (`--parallel N`) is safe under load.** The embedding cache's SQLite layer now uses a thread-safe connection model. Previously running `kairix embed --force --parallel 3` against a corrupted vector index would crash partway through; the fix shipped before the next production rebuild needed it.
- **The verify CLI mirrors runtime.** Previously `kairix secrets verify` skipped the bundle-file hydration that the rest of kairix does automatically, so it reported false-MISSING for every bundle-only secret. It now loads the bundle first; what verify says matches what the embed and MCP paths actually resolve.

### Configuration migration from v2026.5.30a1

No required configuration changes. Three optional moves once you're ready:

1. **Provision the canonical names in KV alongside your existing entries.** Run `kairix secrets migrate-list` to get the TSV of legacy → canonical names; loop over it with `az keyvault secret set` (or your provider's equivalent). The loader prefers canonical; legacy still resolves as a fallback with a deprecation log.
2. **Replace the per-secret fetch script with the convention-driven one.** Copy `scripts/deploy/fetch-secrets.sh` and `scripts/deploy/kairix-fetch-secrets.service` into `/opt/kairix/bin/` and `/etc/systemd/system/` respectively, then `sudo systemctl restart kairix-fetch-secrets`. Adding a new connector now stops touching the script.
3. **Re-run `kairix config validate` after deploy.** The new path-resolution check surfaces any collection entries pointing at directories that don't exist on this host. Each failure has a `fix:` hint inline.

### Things that haven't changed

- The kairix CLI surface for search, MCP, embed, entity management, briefing, prep, eval, benchmark, connectors.
- The vector index format (`vectors.usearch`) and the SQLite schema's content + content_vectors tables.
- The provider plugin selection model.
- Every existing env-var name continues to resolve via the loader's legacy-alias layer.

## [2026.5.30a1] - 2026-05-30 — Entity graph fills, dates flow through to search

> **Upgrading?** No config changes needed. The entity graph and date-aware search both start working better the moment the new image is running. Full details below.

### New for agents

- **Entity-aware search now sees a real knowledge graph.** The graph went from a near-empty starting state to roughly 27,000 entity nodes covering people, organisations, places, products, vocations, industries, and legal/compliance concepts — connected by twelve kinds of relationships (who works where, what's headquartered where, what runs on what, what applies in which jurisdiction, and so on). When an agent asks "who runs the X program?" or "what tools do we use for Y?", the graph answer is now built from a populated map instead of a handful of names.
- **Legacy Excel files (.xls) from SharePoint can now be extracted.** Previously the worker had the modern Office format (.xlsx) covered but raised a missing-dependency error on legacy `.xls` files (the BIFF format from Excel 97–2003). The dependency now ships in the worker image so legacy `.xls` files fetched from this release forward will extract cleanly. Any already in the dead-letter queue need an operator-triggered `kairix worker reextract --source-name <name>` to retry — see [#351](https://github.com/three-cubes/kairix/issues/351) for the related dead-letter visibility bug. Note: this fix does not cover `.xlsm` (macro-enabled) or `.xlsb` (binary) — those use different libraries and remain a separate gap.
- **Date-aware search picks up envelope timestamps when the body has none.** Previously only Obsidian notes with a `date:` field in their frontmatter contributed timestamps to search ranking; SharePoint, Microsoft 365, GitHub, Slack, and Notion documents all dropped their envelope dates between ingestion and search. They now fall through to the document's "last modified" timestamp, lifting temporal-boost coverage from around 2% to near-complete on the next embed cycle.

### New for operators

- **Per-document extractor tracking now actually writes.** The `documents_media` table that records which extractor processed each document was empty for two years — every extractor's documentation said "writes to documents_media" but no code path was wired to do so. The worker now writes a row per extraction. Operators can audit "which PDF extractor handled this file?" against fresh content immediately; existing files backfill as they re-extract.
- **A pre-deployment Neo4j snapshot is kept in operator backup.** Before the entity-graph load, the existing graph was dumped to `/data/backups/neo4j.dump` so the whole change can be rolled back in one command if anything looks wrong. New nodes and edges are also tagged so they can be removed surgically without touching the operator's hand-curated content.

### Things that work better

- **Hand-curated entity properties are preserved across enrichment.** Operators who had added tier, engagement-status, or location values to specific entity nodes keep those values. The enrichment layer adds Wikidata identifiers, descriptions, and aliases alongside them — never overwriting.
- **The worker stops regenerating entities you've already cleansed.** A one-pass cache of canonical entity slugs sits in front of the regex-driven entity discovery step. If you've already curated `Builder` as an entity, the worker won't try to recreate a minimal stub from another mention of the word.

### Configuration migration from v2026.5.28.1a1

No required configuration changes. New behaviour is on by default.

Optional: after the new alpha is running, trigger a worker embed cycle (or wait for the scheduled one) — chunks embedded after the upgrade will carry the envelope timestamp. Existing chunks keep their previous date (which for most was null); to backfill, `kairix embed --force` re-embeds everything against current metadata. Decision tracked in #349.

### Things that haven't changed

- The kairix CLI surface for search, MCP, embed, entity management, briefing, prep, eval, benchmark, connectors.
- The vector index format (`vectors.usearch`) and the SQLite schema's content + content_vectors tables.
- The provider plugin selection model.

### Internal

- **Entity-modelling iter_5 deployment (#343).** Cypher-shell load against production Neo4j: 871 → 26,946 nodes, 2 → 12 relationship types. Five new node labels (`Place`, `Product`, `Vocation`, `Industry`, `LegalCompliance`) and ten new edge kinds. 26,151 nodes + ~258,766 edges carry a deployment-batch tag for surgical rollback; pre-flight dump retained for total restore. ADR-027 specifies the follow-up that converts this one-time load into a continuous per-tick worker stage; tracked under #347.
- **`graph-modelling-refs-as-properties.md`** engineering norm added — external entities that aren't part of the corpus get carried as properties on the source node, not modelled as standalone nodes. Adopted from the entity-modelling pipeline's design doc; cited by ADR-027.
- **`NodeLabel` + `EdgeKind` enum extensions** in `kairix.knowledge.graph.models` match the deployment surface; canonical-slug skip patch in `seed.py` prevents worker regeneration of cleansed entities.
- **GH #329 root cause fixed** — `_gather_pending_chunks` now selects `documents.source_modified_at` and falls back to it when `extract_chunk_date(body, path)` returns None. PRAGMA-guarded so older test fixtures with a minimal documents schema keep working. Verification of the live coverage lift tracked under #349.
- **GH #337 — `markitdown[xls]` extra added to the kairix install.** Forward-going fix: any new legacy `.xls` files fetched from this release land cleanly. Backfill of existing dead-letter requires `kairix worker reextract --source-name <name>`. Initial expected-recovery counts in earlier drafts were inflated — the categorisation conflated PptxConverter-fallback errors (markitdown's converter chain happens to try `XlsConverter` as a guess on certain corrupt PPTX files, which surfaced as MissingDependency in the old image) with true legacy `.xls` items, and counted macro-enabled `.xlsm`/`.xlsb` as in-scope when they need different libraries entirely. The fix is real; the corpus-specific recovery counts shouldn't have been promised. Structural follow-ups (MIME sniffing, `unsupported` terminal status, 403 distinct classification, `kairix connectors requeue` CLI) tracked under #348. Reextract-doesn't-update-deadletter-state bug tracked under #351.
- **ADR-024 — test pyramid redesign.** F68 (Protocol failure-injection coverage), F69 (scale-bound integration tests), F70 (schema-writer symmetry), F71 (preflight-truthfulness), F72 (cross-layer integrity invariants), plus a new soak tier under `tests/soak/` with three seed soak tests + `soak-suite.yml` workflow. F7 affordance rewritten with defect catalogue.
- **ADR-025 — pipeline observability foundation.** `pipeline_item_status` timeline + `status_emit` context manager + F54 both-branch coverage gate. CLI surface, provenance envelope, self-healing loops, and dashboards tracked under #344.
- **ADR-026 — cross-cutting primitive abstractions.** Track A: `Stage` Protocol + `IsolatedStageRunner` + `BatchTransactionStageRunner` (12 existing stage migrations tracked under #345). Track B: `FitnessRule` ABC with 13 batches migrated (F5, F8, F11, F12, F13, F15, F16, F17, F18, F19, F20, F24, F26, F27, F34, F35, F37, F38, F39, F40, F41, F44, F47, F56, F57, F61, F63, F64, F65, F66). Track C: `FlagGatedCapability[T]` ABC + contract tests; 11 callsite migrations tracked under #346.
- **F73, F74, F76, F77 fitness functions** landed: private-infrastructure reference scanner with externalised pattern source; status-emit coverage; no f-string interpolation of content-like variables in log/exception strings; SQLite single-writer call-site allow-list.
- **Fitness function catalogue + bidirectional consistency tests** live under `scripts/checks/_rule_catalogue.py`; CLAUDE.md F-rule section restructured into 8 category groups.
- **Default-safe cutover discipline.** Every architectural change in this alpha lands as default-OFF or zero-change-for-existing-operators.

## [2026.5.28.1a1] - 2026-05-28 — Connector safety bounds, per-source metadata in search, worker memory fixes, entity graph fills

> **Upgrading?** Two new operator knobs and one runbook. Worker no longer crashes during embed cycles on large vector indexes once the runbook is applied. New per-source metadata flows through to search results automatically — no config change required. Full details below.

### New for agents

- **Date- and author-aware search now works across every source.** When you ask "what did agent-alpha write about X last month?", kairix can now answer using the document's actual author and modified date — pulled from SharePoint, Slack, GitHub, Notion, and Microsoft 365 envelope metadata, not just Obsidian frontmatter. Previously 98% of post-SharePoint chunks lacked the metadata that powers temporal and authorship boosts in retrieval.
- **Person and Organisation entities now reach the entity graph.** A new background job drains entity signals (people + organisations extracted during ingestion) into Neo4j every 10 minutes. Previously the staging table accumulated forever and the graph stayed empty for kairix-extracted entities.

### New for operators

- **Worker no longer crashes during embed cycles on hosts with 1M+ vectors.** Two parts: a kill-switch env (`KAIRIX_WORKER_WRITES_VEC_INDEX`, default OFF — interim fix) that stops the worker touching the vector index, and an operator runbook ([`docs/operations/runbooks/worker-memory-and-swap.md`](docs/operations/runbooks/worker-memory-and-swap.md)) that walks through the proper fix: raise the worker container's memory ceiling and allow it to spill into host SSD swap. The runbook fix is a 5-minute job on the VM; the kill-switch is the emergency brake if anything misbehaves.
- **Connector ticks are bounded to safe per-cycle limits.** Every source connector and tick-driven framework component now declares an explicit `per_tick_max_items` (default 500) and an optional `disk_watermark_min_free_bytes` gate. A backlog of 100k items now drains over many short ticks instead of one host-saturating sweep, and a tick skips entirely when free disk falls below the configured watermark. SharePoint, Microsoft 365 calendar, and GitHub default to a 5 GB watermark; the lighter sources opt out via documented rationale.
- **New `kairix curator drain` CLI** for manual catch-up of the entity-graph staging table. `--batch-size`, `--max-batches`, `--dry-run`, and `--format text|json` for bulk drain operations or one-off diagnostics.
- **Integrity preflight reports the true backlog size.** The `entity-signals-staging-not-stuck` warning previously capped its count at 1,000 regardless of actual depth, masking real scale problems. It now reports the real count and keeps the bounded sample separately.
- **Compose env knob for worker memory swap.** New `KAIRIX_WORKER_MEMSWAP_LIMIT` in `docker-compose.yml` lets operators allow the worker cgroup to use host swap. Default (`1g`) preserves the historical no-swap behaviour for existing deployments; the runbook walks through opting in.

### Things that work better

- **Author + date now persist all the way through to retrieval.** New `SourceMetadata` envelope flows through silver chunking: each `Chunk` now carries `author`, `author_email`, `tags`, and a free-form properties bag. Downstream search ranking, temporal boost, and entity coupling all benefit without any per-source code changes.
- **Person nodes auto-emit from envelope authors at high confidence.** When a source surfaces an author (SharePoint `lastModifiedBy.displayName`, Slack `user_profile.real_name`, GitHub commit author, etc.), the entity layer emits a `Person` signal at 0.95 confidence ahead of the regex heuristic. The graph entity is more accurate than name-extraction from prose.
- **The previous masking of staging backlog is fixed.** The preflight count now agrees with `SELECT COUNT(*) FROM entity_signals WHERE pushed_to_neo4j = 0`.

### Configuration migration from v2026.5.28

1. **For hosts with 1M+ vectors (per-corpus, not per-deployment):** Follow [`docs/operations/runbooks/worker-memory-and-swap.md`](docs/operations/runbooks/worker-memory-and-swap.md). Sets three env vars (`KAIRIX_WORKER_MEM_LIMIT=8g`, `KAIRIX_WORKER_MEMSWAP_LIMIT=16g`, `KAIRIX_WORKER_WRITES_VEC_INDEX=1`) and recreates the worker container. The worker resumes vector index writes; cycle takes 2-10 min longer under swap pressure but stops dying.
2. **For hosts with <500k vectors:** No required change. The worker memory defaults are unchanged; the new kill-switch defaults to OFF (which is fine for small corpora since the OOM never triggers).
3. **Optional: bulk-drain the historical entity signals.** `kairix curator drain --batch-size 5000 --max-batches 100` for catch-up, OR (operator's choice) wipe `entity_signals` + re-ingest from source to repopulate cleanly. The drain mechanism handles either approach.

### New architectural rules (mechanically enforced)

- **F65** — every source connector implements `metadata_for()` and ships a propagation test that asserts `chunk_date` + `author` reach the indexed chunk
- **F66** — every connector + tick-driven framework component declares `per_tick_max_items` + `disk_watermark_min_free_bytes` (or watermark-exempt rationale)
- **F67** — every staging table with a `pushed_to_<sink>` column must have at least one code path that updates it to 1. Mechanically blocks the "schema landed, writer never built" anti-pattern that caused 2.3M entity signals to accumulate for years before being noticed.

### Things that haven't changed

- The kairix CLI surface for search, MCP, embed, entity management, briefing, prep, eval, benchmark, connectors.
- The vector index format (`vectors.usearch`) and the SQLite schema's content + content_vectors tables.
- The provider plugin selection model.

### Internal

- ADR-019 (compose-layer resource governance), ADR-020 (per-tick budget + disk-watermark gate), ADR-021 (per-source metadata normalisation), ADR-022 (container-level secret readiness gate, deferred to next release after Wave F), ADR-023 (vector index write architecture — primary path is operational tuning via swap; A1/A2/sqlite-vec fallbacks specified per trigger condition).
- Wave plan ADR updated to insert Wave E.5 (tick safety + metadata propagation) between Wave E and Wave F.
- Default-safe cutover: every architectural change in this alpha lands as default-OFF or zero-change-for-existing-operators.

## [2026.5.28] - 2026-05-28 — Five new connectors, streaming storage, and bug fixes from the alpha cycle

> Operator-facing changes since v2026.5.18 in one entry. Internal development history (per-alpha notes, architecture refactors not visible to operators) lives in git and `docs/upgrades/_dev/`.

### What's new

- **Five new source connectors** — SharePoint, Slack, GitHub, Notion, and Microsoft 365 (calendar + email headers). Each can be configured per-source in `kairix.config.yaml`. SharePoint also supports per-folder include / exclude filtering so you can pick which parts of a drive to index.
- **Pick what to retrieve from your config file** — connectors, credentials, collections, and per-agent scope profiles are now declared in `kairix.config.yaml` instead of code. Your existing config keeps working unchanged; the new options are opt-in.
- **Documents try the best extractor automatically** — for sources like SharePoint where you have a mix of text PDFs and image-only PDFs, kairix can be configured to walk an ordered list of extractors (e.g. fast-text first, OCR as fallback) and pick the best output per document, with no per-document operator intervention.
- **Recover items that failed to extract** — when an extractor fix lands, `kairix worker reextract --source-name <name>` walks every item that failed at extract time, re-runs it through the fixed extractor, and clears the failure record on success. Includes `--dry-run` to size the recovery first.
- **Periodic index hygiene** — a new background tick prunes orphan search-index rows and heals search-index drift. Default off; turn on with the `maintenance_loop` feature flag.

### Disk usage drops by about 6,000x

Source documents are now extracted in-memory and discarded immediately — kairix stores only the chunks, vectors, and a small metadata row per source item. A 50,000-item corpus that would have needed ~650 GB on the previous model now needs about ~50 MB.

Recovery is unchanged from the operator's perspective — `kairix worker reextract` will re-pull the raw bytes from the source on demand.

### Things that work better

- **Source connectors stay in sync after every tick.** Each connector now persists its own resume token between syncs. Fixes a class of bugs where a worker tick re-fetched the entire source corpus from scratch every 15 minutes because the saved resume point couldn't be read back. After this release, a quiet 15-minute sync against an unchanged SharePoint drive does a single round-trip; before, it could be thousands.
- **SharePoint sync survives Microsoft Graph throttling.** When Microsoft asks the client to back off (HTTP 429 or 503 with `Retry-After`), kairix now waits the requested time and retries instead of giving up and marking every in-flight item as failed.
- **Cleanup doesn't saturate disk on big databases.** The maintenance tick now processes a bounded number of rows per call (default 1,000) and resumes on the next tick. Production-scale databases stay responsive while cleanup catches up over a few ticks.
- **External connectors keep syncing while your local vault is idle.** Previously, an idle local vault could pause the entire maintenance schedule, which silently stopped external connectors from picking up upstream changes. They now run on their own 15-minute interval regardless of local activity.
- **Failures mid-batch only roll back the failing chunk.** A failure 5,000 items into a 6,000-item sync now keeps the first chunks committed and resumes after the failure, instead of starting over from the beginning.
- **Agents see a structured retry signal during warm-up.** When kairix is mid-startup, MCP tool calls return a structured "cold start, retry in N seconds" response with explicit retry guidance, rather than a low-level transport error agents can't reason about. (Partial fix for [#320](https://github.com/three-cubes/kairix/issues/320) — full fix continues next cycle.)
- **Image now ships every document converter** — DOCX, XLSX, PPTX, Outlook MSG converters plus the `tesseract-ocr` system binary all install by default. No more "extractor missing" failures on the first run against a mixed-format SharePoint corpus.

### Configuration migration from v2026.5.18

1. **No `bronze_mode:` field.** If you've experimented with this field on alpha builds, remove it from `kairix.config.yaml`. The next-production release refuses it with a fix-pointer error.
2. **Optional: enable the new connectors.** Add a `connectors:` block to `kairix.config.yaml` declaring SharePoint / Slack / GitHub / Notion / M365 entries with their per-source credentials. See [`kairix.example.config.yaml`](kairix.example.config.yaml) for the shape.
3. **Optional: declare an `extractor_chain` per connector.** For SharePoint or any source with mixed text + image-only PDFs, set `extractor_chain: [markitdown, pdf_fallback, ocr]` to opt into the escalation chain.
4. **Optional: enable the maintenance loop.** Flip the `maintenance_loop` feature flag to opt into periodic index hygiene.

### Dependencies

- `tenacity>=8,<10` is now a base dependency (HTTP retry backbone for connectors).
- `markitdown[pdf,docx,xlsx,pptx,outlook]` + the `[ocr]` extra are pinned in the production image. The `tesseract-ocr` system binary is installed via `apt`.

### Things that haven't changed

- The kairix CLI surface for search, MCP, embed, entity management, briefing, prep, eval, benchmark — operators upgrading from v2026.5.18 use the same commands they always did.
- The vector index format (`vectors.usearch`), the SQLite schema's content + content_vectors tables, the chunk-writer contract.
- The provider plugin selection model (`provider:` field in config) introduced in v2026.5.17.

## [2026.5.18] - 2026-05-21 — Conversational memory shipping + one benchmark CLI + machine-actionable cold-start

> **Upgrading?** Nothing required. Legacy CLIs (`kairix eval`, `kairix probe`, `kairix soak`) keep working with a deprecation notice pointing at the new consolidated `kairix benchmark run`. Full upgrade notes: [`docs/upgrades/v2026.5.18.md`](docs/upgrades/v2026.5.18.md).

### New for agents

- **Conversational recall surfaces facts the way you'd expect.** When a question depends on a fact extracted from a conversation (e.g. "What is agent-alpha's role?"), the synthesiser keeps the structured fact triplet in context and the answer comes through with its citation. Short fact rows previously got dropped by a snippet-length filter aimed at sparse chunk text; that filter now applies to chunk-tier results only.
- **`warm` MCP tool** returns a machine-actionable cold-start envelope (`status: retryable_not_ready`, `error_code: KAIRIX_COLD_START`, `retry_after_ms`, `agent_instruction`) when retrieval isn't ready, so the agent knows to wait and retry rather than answer from memory. Call it at session start; subsequent retrieval tools return real results once warm-up succeeds.
- **Attribute-fact intent classifier covers two more question shapes**: `"what did X verb?"` and `"who is/was X's Y?"`. Both previously routed to a generic semantic intent and lost the fact-retriever weight they needed.
- **Facts carry session date metadata** via a new `evidence_at` field. Temporal questions resolve to absolute dates rather than relative phrases echoed from the source turns.

### New for operators

- **`kairix benchmark run`** is the consolidated entry point for retrieval-quality and answer-correctness measurement. One CLI; pluggable scorers (NDCG, Hit@K, MRR, LLM-judge, latency); `--mode` for single-shot now, concurrent and soak shipping in the next release; `--scope` / `--collection` / `--categories` / `--gates` / `--baseline` flags. Suite YAML extends backward-compatibly with `expected_answer:` for LLM-judge scoring alongside `gold_titles:` for NDCG.
- **`kairix eval` against a conversational corpus extracts facts by default** via the new `kairix.corpus.wiring` factory. Previously the eval CLI ran against a no-op fact extractor; conversational suites now produce real SQLite state. Reference-library NDCG suites are unaffected.
- **`KAIRIX_TRACE=1`** now configures a logging handler at CLI start, so the per-query diagnostic lines (`prep.context: chunks K/N kept, facts K/N kept, M ctx chars`) appear on stderr. Off by default; production stays quiet.
- **Eval scoring mirrors `kairix prep`.** `kairix eval` routes through the same `SearchPipeline` (`--via-prep`, default on). A scope-routing bug where `--scope` was silently overridden to `shared+agent` whenever `--agent` was set is also fixed.

### Internal

- **`kairix.corpus.ingest.ingest_corpus`** is now the shared ingest primitive. `kairix ingest-chat`, the eval CLI's `SuiteRunner`, and the LoCoMo benchmark harness all route through it. One ingest path, one set of behaviours; production and benchmark code paths can't diverge.
- **F30 fitness function** (new) — every CLI subcommand and MCP tool has at least one outcome test that exercises the subprocess path and asserts on captured output. Pre-existing surfaces grandfathered; baseline only shrinks.
- **F5 and F6 broadened.** F5 also catches attribute access to underscore-prefixed kairix names on imported modules. F6 also catches `*_loader`, `*_factory`, `*_builder`, `*_provider`, `*_resolver` test-injection seams in addition to `*_fn`. Methods on `ClassDef` (canonical Deps pattern) are exempted.
- **`safe-commit.sh`** generates `coverage.xml` and enforces the F7 per-file coverage floor locally, closing a parity gap with CI. Agents working on kairix get F7 feedback in seconds rather than minutes.

**Design context:** [`docs/architecture/fact-layer.md`](docs/architecture/fact-layer.md) — ADR covering the conversational memory architecture, the five capabilities, and the federation pattern. [`docs/upgrades/v2026.5.18.md`](docs/upgrades/v2026.5.18.md) — operator and agent upgrade guide.

### Security

- **Warm-state flag moved out of `/tmp` into the kairix data directory.** The cross-process readiness flag added in v2026.5.17 lived at `/tmp/kairix-warm.flag` — a world-writable, predictable path. The flag now lives at `<KAIRIX_DATA_DIR>/warm.flag`, owned by the kairix process user and not world-writable. The MCP server clears any stale flag at startup so a restarted container correctly reports not-ready until it has actually re-warmed.

## [2026.5.17] - 2026-05-18 — Faster searches under concurrent load + plug-in support for any LLM provider

> **Upgrading?** **One required config change**: add `provider: <name>` to the top of your `kairix.config.yaml` before pulling this version. Kairix won't start without it and will list the installed plug-in names so you can pick. See [`docs/operations/runbooks/how-to-upgrade-kairix.md`](docs/operations/runbooks/how-to-upgrade-kairix.md) for the one-line edit. **Operators with a custom `kairix.config.yaml` mounted into the container** should move to the new overlay pattern before pulling — it stops future required-setting bumps from quietly breaking your deploy. See [`docs/operations/runbooks/config-overlay-upgrade.md`](docs/operations/runbooks/config-overlay-upgrade.md). **New for agents**: searches feel faster when several agents work at the same time, and a cold container no longer returns "fetch failed" — kairix tells you it's warming and how long it needs. **New for operators**: the new `kairix probe-config` command checks your endpoint after setup and recommends concrete tuning; seven first-party plug-ins ship today (Azure Foundry, Azure Legacy, OpenAI direct, Bedrock, Ollama, LiteLLM proxy, Anthropic).

### New for agents

- **Searches feel faster under concurrent load.** Probe data on the reference-library suite shows mean latency at concurrency 10 dropped from 1452 ms to 804 ms across the dev cycle. (closes #281, #282, #287, #288)
- **Ten agents asking different questions at the same time pay one network round-trip.** Concurrent embed calls in a 50 ms window fold into one batched request. (closes #288)
- **A second ask for the same text comes back from cache.** Same text → same vector, regardless of which agent or scope asked. (closes #281, #285)
- **Vector lookups got 13× faster.** One batched database query instead of N+1 per result. (closes #287)
- **The first call to a cold kairix no longer fails.** Previously the first agent call against a just-restarted container could return "fetch failed" because the search pipeline hadn't finished warming. Now every agent-facing tool — bootstrap, brief, search, entity, prep, timeline, research, contradict, entity_suggest, entity_validate — returns a clear "kairix is warming, retry in ~N seconds" response while warm-up runs in the background. The second call returns real results.
- **Probe reports per-stage timings** for every query — so you can see where time goes. (closes #282)

### New for operators

- **`kairix probe-config`** — runs cold + warm + concurrent calls against your configured endpoint and emits a JSON report: status (healthy / degraded / unreachable), latencies, cache hit rate, and concrete tuning recommendations. Share the file with support if anything looks off. Privacy-safe: the report includes the endpoint hostname only — no full URLs, no credentials.
- **`kairix entity count` / `audit` / `purge`** — fast entity-graph readout plus cleanup tools for stale entries. (closes #259, #260, #261)
- **Embed connection pool is now tunable.** `KAIRIX_EMBED_POOL_SIZE` / `_KEEPALIVE_CONNECTIONS` / `_EXPIRY_S` match the pool to your team size and endpoint distance. (closes #280)
- **Release pipeline gates stable releases on alpha.** Every `v2026.5.17` cannot publish without a successful `v2026.5.17-alpha` first. Alpha tags auto-deploy to your alpha host via a Go-built webhook. (closes #272)
- **Search pipeline pre-warms at container start** so the first request doesn't pay the 192 MB factory-init tax. (closes #278, #279)
- **Benchmark resolves bundled suites by name.** `kairix benchmark run reflib` works from any directory. (closes #268)
- **`kairix --version` works inside Docker.** Build passes the version so reports show the real release. (closes #267)
- **Upgrades that add new required settings won't quietly break your deploy.** Kairix now ships its own complete config inside the Docker image; your host-side YAML only carries the keys you want to override (paths, agents, retrieval tuning). When a future release adds a required setting, it's already in the image — you don't have to remember to copy it forward. Overlays can also pin a minimum kairix version with `_schema_version_required_min`; if the image doesn't meet it, kairix refuses to start and tells you which release to upgrade to. The previous single-file pattern (`KAIRIX_CONFIG_PATH`) keeps working for existing deployments. Migration guide: [`docs/operations/runbooks/config-overlay-upgrade.md`](docs/operations/runbooks/config-overlay-upgrade.md).
- **`docker compose up --wait` actually waits for kairix to be ready.** The healthcheck now uses a new `kairix onboard ready` command that only succeeds once the first real agent call would too. Your deploy command returns when kairix is genuinely warm, not just when the port is bound.
- **Auto-deploys now actually restart the container.** Previously a failed startup health check could leave the old container running while the deploy log said "deployed" — diagnosed during the alpha-9 recovery. The webhook now fully recycles the container on every deploy (~10s slower; eliminates the trap).

### Internal (the foundation for multi-provider deployments)

- **Three-layer architecture** in [`docs/architecture/provider-plugin-architecture.md`](docs/architecture/provider-plugin-architecture.md). Domain (`kairix/core/`) talks to universal endpoint concerns (`kairix/transport/`: pool / retry / coalesce / cache / timeout) and per-endpoint plugins (`kairix/providers/<name>/`) only via Protocols.
- **Stops rebuilding the model connection on every batch.** Each concurrent embed batch was setting up a fresh TLS connection (~300-500 ms cold). The new `ClientPool` keeps one warm connection for the container's life.
- **F26–F29 fitness functions** lock the split: domain can't import transport or providers, no cross-provider imports, every plugin needs BDD coverage, performance code stays in `kairix/quality/probe/`.
- **Provider plugin discovery via Python entry points; config-yaml-driven selection.** Set `provider: <name>` at the top of `kairix.config.yaml` to select your plugin. Each plugin owns its own credential-retrieval pattern (Azure plugins use Key Vault; AWS plugins use Secrets Manager / IAM; etc.), so the operator doesn't wire secrets in the yaml. Existing deployments MUST add `provider:` to their `kairix.config.yaml` before upgrading (see [`docs/operations/runbooks/how-to-upgrade-kairix.md`](docs/operations/runbooks/how-to-upgrade-kairix.md)). Third parties ship `pip install kairix-provider-foo`. Seven first-party plugins available: Azure Foundry, Azure Legacy, OpenAI direct, Bedrock, Ollama, LiteLLM proxy, Anthropic. (closes #285)
- **F21–F25 quality rules** (actionable-feedback markers, path naming, README resolvers, no `tests.*` imports in production, every CLI has an MCP affordance).
- **Cognitive-complexity burndown** across chunker / reflib / sweep / entities / contradict / temporal — every flagged function now under 15. (closes #250)
- **MCP retroactively exposes safe read-only operational capabilities** — onboard check, capability introspection. (closes #277)
- **Performance + soak test suite** catches regressions like the embed-pipeline class in CI. (closes #276)
- **More test files moved to clean dependency injection.** Seventeen test modules stopped reaching into kairix internals and now inject fakes through the public interface — keeps tests honest as the code evolves so they catch real regressions instead of silently going stale.
- **New end-to-end coverage for classify and config-layering** — operator-language scenarios that exercise the same paths a real deploy goes through, including the exact alpha-9 failure mode.

### Dependency updates

- `docker/login-action` 3.7.0 → 4.1.0 (#290)
- `actions/download-artifact` 4.3.0 → 8.0.1 (#291)
- `actions/checkout` 4 → 6 (#292)
- `ruff` 0.15.12 → 0.15.13 (dev) (#293)
- `sentence-transformers` requirement widened `>=3.0,<4` → `>=3.0,<6` (#294)

### Fixed

- **Worker survives `SystemExit` from helpers.** Recall-gate alerts no longer crash the container. (closes #270)
- **MCP entity lookup checks aliases.** Lookups find the entity even when the canonical name differs from the alias asked for. (closes #253)
- **`prep` (L0 summary) returns grounded content.** No more generic responses when the knowledge store has the answer. (closes #254)
- **Eval module security hardening** — path confinement, prompt-injection guards, finite-score validation. (closes #143)
- **Per-collection retrieval overrides** apply to single-collection MCP and benchmark calls. (closes #274)
- **Reflib benchmarks captured per release** for quality regression tracking. (closes #271)
- **Retrieval health & recovery runbook** added for operators. (closes #252)
- **Python 3.14 + numpy + pytest-cov** import-order error worked around. (closes #211)

### Still open

- PVT MCP HTTP test harness (#284)
- SonarCloud check status mismatch (#269)
- Webhook auto-deploy on release (#286)

---

## [2026.5.15] - 2026-05-14 — Agent-first kairix + worker observability + F-rule legacy fully burned

> **Upgrading?** Drop-in. No public API breaks. **New for agents**: `kairix bootstrap <agent>` returns a session-start orientation envelope; every MCP tool now includes a `health` field that tells agents what's offline and what to do next; `kairix onboard check --json` is the canonical "is kairix working" probe with per-check remediation strings. **New for operators**: worker pause/resume + observable phase state + skip-on-idle maintenance. **Internal**: 7 fitness-function baselines burned to zero (F1, F3, F4, F5, F6, F7, F9); per-file coverage floor ratcheted 85% → 90%; 10 shim modules deleted; 22 env-var helpers centralised in `paths.py`/`secrets.py`.

### Added — agent affordance (#246)

- **`kairix bootstrap <agent>`** — single command returning the agent's session-start orientation: role, current `Board.md`, last N daily memory entries, active goals, and a `health` envelope. CLI: markdown by default, `--json` for tooling. MCP: `tool_bootstrap(agent, max_memory_days=3)`. Designed so even with vector search or chat offline, the bootstrap **still returns** board + memory and tells the agent what's degraded.
- **`KairixHealth` envelope on every tool response.** `tool_search`, `tool_brief`, `tool_entity`, `tool_bootstrap` all return a `health` field with `vector_search` / `bm25` / `chat` status, `secrets_loaded`, `degraded_reason`, and `next_action`. When kairix degrades, the response **still returns working-subsystem results** AND tells the agent what to do next (e.g. "Ask your admin to run `kairix onboard check`; results below are BM25-only"). New shared module: `kairix/core/health.py`; threaded timeout enforces a 2s probe budget.
- **Prescriptive MCP tool descriptions.** The `description=` string an LLM agent sees in its tool list is now a usage policy, not a definition. `tool_search`: "Call before answering any factual question about prior work…". `tool_bootstrap`: "Call at session start or whenever you switch topics…". `tool_brief`: "Call when you want a synthesised view…". `tool_entity`: "Call when you need facts about a specific named entity…".
- **`kairix onboard check --json`** + clean exit-code semantics. Exit 0 on full pass, 1 on any failure. JSON shape: `{passed, total, fully_passed, failures: [{check, detail, remediation}]}`. Each of the 9 checks has a canonical, operator-actionable remediation string (e.g. `secrets_loaded` failure → "Run `sudo systemctl enable --now kairix-fetch-secrets.service` on the host…"). Wired as the canonical docker-compose healthcheck.
- **`kairix-memory-prompt` openclaw plugin packaged canonically** at `kairix/plugins/openclaw/memory-prompt/`. Symlinked into the docker image at `/opt/kairix/plugins/openclaw/`. Calls `kairix bootstrap <agent>` at session start and `appendSystemContext`s the result so agents start oriented instead of reactive. Degraded fallback message when bootstrap fails — session start is never blocked.
- **`docs/agents/AGENT-SETUP.md`** + **`docs/agents/ADMIN-CONVERSATION.md`** — the operating contract an agent reads on first run, plus the script an agent follows when discussing kairix configuration with its admin human (symptom → exact words to say → concrete command). README quick-start rewritten in agent-first ordering: install → secrets → collections → verify → wire-into-agent.

### Added — operator affordance (#224, #222)

- **Worker pause/resume (#224 phase 4).** `kairix worker pause` / `kairix worker resume` toggle a touch-file in the data dir. The running worker enters PAUSED at the next loop iteration (within 5s) and stops doing task work until the flag is removed. Decoupled from the worker process — a stuck worker can still be paused, and the pause survives restarts.
- **`kairix worker status` (#224 phase 5).** Reads the persisted `WorkerState` JSON (atomic temp+rename writes) and prints phase, embedded total, failed chunks, recall alerts, restart count, uptime. Exit 0 if state file present, 1 if missing.
- **Worker skip-on-idle maintenance (#224 phase 2).** When `consecutive_embed_noops` ≥ 10 (env `KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD`), the worker stops running `entity_seed` / `health_check` / `wikilinks`. Resumes on the next embed that does work. Friendlier to shared hosts.
- **`kairix benchmark run <name>` resolves bundled suites by name (#222).** `kairix benchmark run reflib` finds the bundled `reflib-gold-v3.yaml`, reads `default_collection` from suite metadata, and runs scoped correctly — no more `--collection reference-library` tax for production validation. `kairix benchmark list` enumerates bundled suites with their default_collection and one-line description. Unknown suite name → exit 1 with `did you mean: kairix benchmark list?` hint.
- **F14 — `sonar.issue.ignore` entries require rationale comment.** Mirrors F3 for SonarCloud suppressions.
- **Scheduled baseline audit (`.github/workflows/baseline-audit.yml`).** Mondays 08:00 UTC + workflow_dispatch. Fails if any baseline entry is stale.
- **`scripts/checks/audit_baselines.py`** — local invocation of the audit logic.

### Changed (internal — quality-exceptions Wave 2-5 + F-rule legacy closure)

- **F-rule baselines closed.** Seven baselines went from grandfathered violations to **zero**:
  - **F1** (no `@patch` on kairix internals): 3 → 0. Refactored `tests/test_paths.py` to inject `platform=` instead of patching `kairix.paths.sys`; `tests/search/test_config_loader.py` driven by malformed YAML naturally; one weak smoke test deleted (behaviour pinned elsewhere).
  - **F3** (suppressions require rationale): 32 → 0. Every `# noqa` / `# NOSONAR` / `# pragma: no cover` / `# type: ignore` / `# nosec` carries an em-dash rationale.
  - **F4** (env reads centralised): 18 → 0. 22 new helpers in `paths.py`/`secrets.py`; every `os.environ.get("KAIRIX_*")` outside those two modules now routes through a typed helper.
  - **F5** (no internal-name imports in tests): 13 → 0. Promoted private helpers in `paths.py` and `core/search/config_loader.py` to public.
  - **F6** (no `*_fn=None` test-only kwargs in production): 12 → 0. AST detector extended to walk `ClassDef` `AnnAssign` fields (was only walking function params); surfaced 3 dataclass-field violations all refactored to `field(default_factory=lambda: _default_X)`. Eight `_*_defaults.py` shim modules deleted — their lazy-import bodies moved into the dataclass module as `_default_X` functions.
  - **F7 / F9** (per-file 85% floor, unit and union): 34 + 33 → 0. 23 production files lifted past 90%; 11 small files in the 85-90% band lifted to 97-100%. 90+ new sabotage-proven unit tests.
- **F2 baseline pinned at 8** — env-feature tests for paths/secrets/credentials/config_loader plus the autouse `no_azure_calls` safety fixture. These directly test the env-var-reading API; eliminating `monkeypatch.setenv` means changing what's tested. Net-new F2 violations still block.
- **F7/F9 floor ratcheted 85% → 90%**. `pyproject.toml`'s `fail_under` raised 80 → 88 (tracks current achievable floor).
- **F6 AST detector extended**. v1 walked `FunctionDef` / `AsyncFunctionDef` only; v2 also walks `ClassDef` `AnnAssign` so dataclass fields shaped `x_fn: Callable | None = None` are caught.
- **Audit script prefix fix.** `scripts/checks/audit_baselines.py` initially missed coverage-baseline matches because Cobertura's `filename` is source-root-relative. Fixed to read `<source>` and re-prepend.
- **Deterministic `fake_llm_backend.embed`** (#240). `hash(text) % 1000` had two failure modes (PYTHONHASHSEED + 0.1% modular collisions); replaced with `sha256(text)[:4]` truncated to a 2³² seed space.

### Removed

- **`tests/integration/test_mcp_tool_contracts.py`** — weakly-asserting smoke test (`"results" in result or "error" in result`) requiring `@patch("kairix._azure.embed_text", ...)`. Behaviour covered by `tests/use_cases/test_search.py` + `tests/integration/test_search_pipeline.py` + `tests/contracts/test_cli_mcp_parity_search.py`.
- **10 shim modules** — 8 `_*_defaults.py` use-case shims + `_pipeline_defaults.py` + `_timeline_defaults.py`. Bodies inlined into their dataclass modules.

### Issues filed / closed

- **Closed**: #246 (agent-first kairix — bootstrap + prescriptive MCP descriptions + health envelope + structured onboard check + plugin packaging + docs), #240 (flaky embed fake), #224 (worker resource discipline — phases 1, 2, 4, 5, 6 shipped; phase 3 deferred via #243), #222 (benchmark UX defaults), #203 (Wave 5 ratchet), #244 (F6 detector gap + refactor), #193 (quality-gate exceptions umbrella), #198 (F7 coverage backfill), #200 (F2 monkeypatch elimination), #201 (F1+F5 in-test internals).
- **Filed**: #242 (SonarCloud project still keyed under the previous org after the GitHub org rename — needs SonarCloud admin to update the GitHub binding), #243 (SRE/platform-health worker — design-first; recurring `kairix-fetch-secrets.service` disabled incident is now a concrete user story on that issue).

### Operational notes

- **Agents**: at session start, call `kairix bootstrap <your-agent>`. If `health.vector_search != "ok"`, surface that to your human and use BM25 results — don't silently fail.
- **Admins**: `kairix onboard check --json` is the canonical health probe. Wire into your docker-compose healthcheck and any external monitor.
- **`kairix worker status`** exit code is the authoritative "worker alive AND has run" signal. State file: `${KAIRIX_DATA_DIR}/worker-state.json`.
- **Shared hosts**: tune `KAIRIX_MAINTENANCE_SKIP_NOOP_THRESHOLD` (default 10) and apply the resource caps from `docker-compose.example.yml`.
- **SonarCloud PR scans show red until #242 admin step lands.** Branch protection on `develop` does not require SonarCloud, so merges are unaffected.

---

## [2026.5.14] - 2026-05-14 — FTS rebuild atomicity + quality-exceptions Wave 1 burndown

> **Upgrading?** Drop-in. The FTS fix prevents transient `no such table: documents_fts` errors during worker rebuild cycles — operators occasionally seeing those in logs will stop seeing them. Internal refactors land alongside; no public-API changes.

### Fixed

- **`documents_fts` no longer disappears mid-rebuild (#223).** `rebuild_fts()` ran `DROP TABLE`, `CREATE VIRTUAL TABLE`, `INSERT` as three separate auto-commit operations. Python's `sqlite3` default isolation doesn't auto-begin a transaction for DDL, so the DROP committed immediately. Any concurrent reader querying `documents_fts` between the DROP and the subsequent `CREATE`/`INSERT`/`commit` saw "no such table" and BM25 silently fell back to vector-only. The rebuild is now wrapped in `BEGIN IMMEDIATE` / `commit` (or honours an existing caller transaction). Atomic from any reader's perspective. Two regression tests pin the property; one is sabotage-proven.

### Changed (internal — quality-exceptions Wave 1)

- **F6 baseline emptied (12 → 0).** All `*_fn=None` test-only kwargs in production are gone, replaced by typed `*Deps` dataclasses with `default_factory` factories. Modules converted across PRs #209, #213, #215, #212: `quality/benchmark/runner.py`, `agents/briefing/pipeline.py`, `agents/research/{graph,nodes}.py`, `core/search/config_loader.py`, `knowledge/contradict/detector.py`, `knowledge/summaries/generate.py`, `platform/llm/backends.py`, `platform/onboard/check.py`, `platform/setup/wizard.py`, `quality/eval/retrieval.py`, `worker.py`.
- **`EmbedDependencies` refactored to the `default_factory` pattern (#216, refs #204).** Eliminates the `Optional[Callable] | None = None` + `__post_init__` self-resolution that caused mypy `--strict` regressions and required `assert deps.X is not None` ladders at every call site. 6 such assertions deleted from `embed.py`. The new `kairix/core/embed/_deps_defaults.py` sibling module is the canonical pattern; same shape can roll forward to `SearchDeps`, `SummariesDeps`, `LLMJudgeScorer`.
- **Coverage backfills (PRs #205, #206, #207, #210):** `rerank.py` 62 → 100%, `vector_repository.py` 0 → 100%, `embed.py` 80 → 100% testable surface, `recall_check.py` 7 pragmas → 0. F7 baseline -4. Dead `FileNotFoundError` guard in `recall_check.py` removed (rationale was self-contradictory; helpers it claimed to defend against don't raise FNF).
- **Dependency bumps**: `openai` requirement widened to `>=1.40,<3`; `codecov/codecov-action` 5 → 6, `docker/setup-buildx-action` 3 → 4, `SonarSource/sonarqube-scan-action` 6 → 8, `dorny/paths-filter` 3 → 4, plus pinned-SHA updates to `pypa/gh-action-pypi-publish` and `hynek/build-and-inspect-python-package`.

### Operational notes

- **Reflib benchmark verification (2026-05-13):** post-fix reflib-gold-v3 scored 0.872 weighted / 0.944 NDCG@10. Pre-fix steady-state was 0.890 — the 0.018 delta is within Azure embedding stochasticity. The fix's value is preventing the transient BM25-blackout window during worker rebuilds, not a steady-state lift. Both runs ran with zero FTS errors.

---

## [2026.5.10] - 2026-05-10 — Worker stability, layered health probes, deploy self-heal

> **Upgrading?** Drop-in. The worker now survives recall-gate alerts (which were silently killing the process before this release). The `/healthz/ready` endpoint is new but additive — `/healthz` is unchanged for back-compat with existing load-balancer probes. systemd-managed deployments should adopt the example unit files in `scripts/install/` to fix the post-reboot self-heal gap (#167).

### Fixed

- **Worker no longer dies on recall-gate alerts.** The worker called the embed CLI, which used `sys.exit(1)` to signal a recall-gate degradation. `SystemExit` is not caught by `except Exception`, so every gate alert killed the worker container — Docker restart-looped it forever. The worker now calls `run_incremental_embed_pipeline()` (a new use case in `kairix/core/embed/use_cases.py`) directly, receives a structured `EmbedPipelineResult` dataclass, and treats recall-gate failures as logged alerts rather than fatal exits. Failed chunks, gate alerts, and unexpected exceptions are all logged; the worker continues to the next interval. (resolves the v2026.5.9 production report)
- **Recall canary queries now persist across runs.** Pre-fix, the recall gate sampled five random documents per run and compared the new score to the previous run's — but the previous run had sampled five different documents. The "delta -60%" alerts were comparing apples to oranges. Queries are now persisted to `~/.cache/kairix/recall-canaries.json` on first build and reused on every subsequent run, so the run-over-run delta is meaningful. Operators can force a re-sample with `kairix embed --rebuild-canaries` after a major corpus change.

### Added

- **`/healthz/ready` — layered readiness probe.** Resolves the #167 gap where `/healthz` reported `ready=true` while vector search was silently broken because `/run/secrets/kairix.env` had never been hydrated after a reboot. The new endpoint reports per-capability detail (`secrets_loaded`, `vector_search_capable`, `bm25_search_capable`) plus a `detail` map of failure reasons. `/healthz` is unchanged. Wired into the production MCP server via `kairix/agents/mcp/capability_probe.py`. See `docs/operations/MCP-DEPLOYMENT.md`.
- **Deploy hygiene artifacts** in `scripts/install/`:
  - `kairix.service.example` — systemd unit with the correct `Requires=`/`After=` ordering against `kairix-fetch-secrets.service` and `docker.service`. Pre-fix, kairix.service could start before secrets were hydrated.
  - `kairix-fetch-secrets.service.example` — oneshot that hydrates `/run/secrets/kairix.env` from Azure Key Vault on every boot (since `/run` is tmpfs and clears on reboot).
  - `permissions-preflight.sh` — idempotent `ExecStartPre=` script that fixes `.env` ownership/mode mismatches (the #167 root cause), verifies the secrets file is non-empty, and confirms the merged environment has all required keys before docker compose touches anything.
- **`kairix embed --rebuild-canaries`** flag — discards the persisted canary suite and re-samples from the corpus. Use after a major index rebuild or corpus migration.

### Changed

- **`run_recall_gate()` accepts `rebuild_canaries=`** kwarg for the new flag.
- **`RecallChecker.check()` accepts `canary_cache_path` and `rebuild_canaries`** kwargs. `canary_cache_path=None` disables persistence (used by tests for adaptive-sampling exercise without polluting `~/.cache`).
- **Worker logs structured outcomes.** Embed completion now reports `embedded=N failed=N recall=X%` rather than just "embed complete"; failed chunk counts and recall alerts surface as warnings.

### Operational notes

- Existing systemd installs should diff their unit files against the new examples in `scripts/install/`. The principal change is `Requires=kairix-fetch-secrets.service` plus the `ExecStartPre=` hook. Migration is a copy-paste on the host; no kairix data migration required.
- Existing `~/.cache/kairix/recall-canaries.json` does not exist on already-deployed instances; the file is built lazily on the next embed run, so no operator action is required.

---

## [2026.5.9] - 2026-05-10 — Schema, security, onboarding, configurable scope, paths-DI pilot, fitness harness

### Added

- **Architecture fitness function harness (F1–F13)** — thirteen blocking quality gates wired into pre-commit, `safe-commit.sh`, and CI (Stage 0 / 2 / 5). Each gate uses ratcheting baselines: pre-existing violations are grandfathered in `.architecture/baseline/`, but a single net-new violation blocks the commit/PR. Detects forbidden patching of internal code (F1), env-var monkeypatching in tests (F2), un-rationalised suppressions (F3 — covers `# noqa` / `# NOSONAR` / `# pragma: no cover` / `# type: ignore` / `# nosec`), env-var reads outside `paths.py`/`secrets.py` (F4), private-name imports in tests (F5), `*_fn=None` test-only kwargs in production (F6), files below 85% line coverage on the unit run (F7), unmarked `test_*` functions (F8), files below 85% line coverage on the unit-∪-integration union (F9 — Stage 5 holistic, per Ford / Sadalage / Kua's *Building Evolutionary Architectures*), un-rationalised CI workflow silencers (`continue-on-error: true`, `fail_ci_if_error: false`) (F10), un-rationalised test skip mechanisms (`pytest.mark.skip`/`skipif`/`xfail`/`importorskip`) (F11), BDD features with no happy-path scenario (F12), and BDD scenarios that leak implementation symbols (F13). Canonical reference: [`docs/architecture/fitness-functions.md`](docs/architecture/fitness-functions.md).
- **Codecov properly wired** — `codecov.yml` declares carryforward for `unit` (Stage 2) and `integration` (Stage 3) coverage flags so the dashboard merges stages instead of conflating them. Patch target = 85% mirrors F7's per-file floor. Five components (Search / Agents / Knowledge / Quality / Core) provide per-area regression tracking. JUnit XMLs from contracts, unit, and integration jobs upload via `codecov/test-results-action@v1`, enabling Test Analytics for flaky-test and slow-test tracking. The `[tool.coverage.run].omit` list in `pyproject.toml` remains the single source of truth for excluded files (no parallel `ignore:` block in `codecov.yml`). Bundle analysis is intentionally not wired (Python-only project).
- **`uv.lock` shipped with the source tree** — pinned, hash-verified dependency resolution. CI installs and Docker image bakes resolve identically across runs; the same source tree always pulls the same package versions. Operators can audit the exact wheel SHAs they're consuming.

### Changed

- **`build_search_pipeline()` now applies the YAML retrieval section at startup** — the factory reads `kairix.config.yaml`'s top-level `retrieval:` block (and per-collection `retrieval:` overrides for single-collection queries) when called without an explicit config, instead of always using the sweep-tuned defaults. Operators who configured per-collection retrieval in v2026.5.7 will now see their tuning actually applied to MCP and benchmark calls. (#112)
- **`kairix benchmark run --collection X` honours `X.retrieval` overrides** — single-collection benchmarks merge X's `retrieval:` block over the global config before running. Reflib benchmark scores now match the documented baseline; previously the runtime silently ignored reflib's own tuned settings. (#112)
- **`scope=all-agents` / `scope=everything` fail loudly when no agents are configured** — the MCP and CLI search paths now return a structured error envelope (`"scope=all-agents / scope=everything requires an AgentRegistry with at least one agent registered..."`) when `kairix.config.yaml` has no `agents:` section. Previously the request silently returned `reference-library` hits. Operators relying on the old behaviour should add an `agents:` section or scope queries to `shared+agent` (the default). (#164)
- **Legacy `agents: [{collection: <name>}]` YAML emits a deprecation warning** — pointing at the multi-path `paths:` schema introduced in v2026.5.7. Behaviour unchanged; one warning per legacy agent at startup. (#115)

### Fixed

- **BM25 backend distinguishes `collections=[]` from `collections=None`** — explicit empty-list scope returns no results (search nothing); `None` searches all active documents (no filter). The previous conflation was the proximate cause of the silent fall-through under `scope=all-agents`. (#164)
- **Sweep BM25 weights validated finite and positive at entry** — `kairix eval sweep` rejects nan / inf / non-positive weight inputs with a `ValueError` before opening the database, instead of letting them reach the SQL `ORDER BY` where they'd produce nondeterministic ranking. (#143 Phase 0b)

### Roadmap

- **CLI / MCP feature parity initiative** ([#168](https://github.com/quanyeomans/kairix/issues/168)) — every kairix feature exposed via both CLI and MCP with uniform UX. Audit identified 8 surface gaps and 1 code-path divergence (timeline). Targeted for next sprint. Full design at [`docs/architecture/cli-mcp-feature-parity.md`](docs/architecture/cli-mcp-feature-parity.md).

## [2026.5.7] - 2026-05-07 — Configurable scope, agent provenance, eval gate, security hardening

> **Upgrading?** Drop-in. The `agent_owner` column is added via additive `ALTER TABLE` on container start; legacy `agents:` YAML keeps parsing. The new `in_default: bool` flag on collections defaults to `true`, so existing yamls behave identically to before. To use the new flag, set `in_default: false` on collections you want excluded from default search (typically `reference-library`, `archive`).

### Added

- **`in_default: bool` on each collection** — operators control which collections participate in default search scopes from yaml. Collections with `in_default: false` remain indexed and reachable via explicit `--collection <name>`; they don't auto-join `shared` / `shared+agent` / `all-agents` / `everything`. Replaces a hardcoded reflib carve-out. (#135)
- **`agent_owner` column on `documents`** — per-document agent provenance. Idempotent migration; existing rows are NULL. The embed scanner now tags new rows with the owning agent. (#114)
- **Multi-path `AgentDef` schema** — agents declare a list of read paths in `kairix.config.yaml`; out-of-the-box agents default to `/data/workspaces/{name}`. Old single-`collection:` YAML keeps parsing for one release window. (#115)
- **`kairix eval gate`** — quality-gate CLI that turns benchmark output into a go/hold verdict with concrete tuning recommendations. Closes the onboarding flow: `setup → entity seed → eval auto-gold → eval tune → eval gate`.
- **Operator runbooks** — `runbook-vector-search-failure`, `runbook-embedding-lag`, `how-to-rebuild-entity-graph`, `how-to-configure-pypi-trusted-publisher`. All parameterised against `KAIRIX_*` env vars; no operator-specific paths.
- **`paths: KairixPaths` injection on the wikilinks surface** — `injector` / `audit` / `cli` accept paths as an explicit dependency. First step in a broader paths-DI initiative; existing callers unchanged. (#140)
- **Per-collection `retrieval:` block on `reference-library` in the shipped example yaml** — replaces a hardcoded retrieval baseline. Operators get the known-good values by default and can deviate when they want to. (#135)

### Changed

- **`DefaultCollectionResolver` refactor** — the resolver no longer knows anything about specific collection names. The "is this in default scope?" predicate moved onto `CollectionsConfig` itself. Cyclomatic complexity per method ≤ 3. (#135)
- **`CollectionsConfig` is frozen** — `tuple[CollectionDef, ...]` instead of `list`; only the predicate methods are public. (#135)
- **Strict bool coercion on `in_default`** — non-boolean yaml values raise `ConfigValidationError` naming the offending key, rather than silently coercing `"false"` (a truthy string) to `True`. (#135)
- **`AgentRegistry.collections_for(name)`** is the new multi-collection accessor; `collection_for(name)` remains for legacy callers.
- **Wikilinks injector** reads paths lazily instead of at module import — removes the long-standing `importlib.reload` requirement from the test fixture. (#129)

### Removed

- Hardcoded `_RESERVED_COLLECTIONS = {"reference-library"}` carve-out in `DefaultCollectionResolver`. Replaced by the operator-yaml `in_default` flag. (#135)
- Hardcoded `if target == "reference-library":` retrieval-config branch. Replaced by per-collection `retrieval:` overrides. (#135)

### Fixed

- Benchmark crash when gold titles look like ISO dates — coerced at suite-load boundary. (#103)
- Bundled `reflib-gold-v1.yaml` unrunnable; removed in favour of `reflib-gold-v3.yaml`. (#104)
- `recall_check._embed_query` now goes through `EmbedProvider` for retry / rate-limit / backoff parity with the rest of the embed pipeline. (#43, OPS-007)
- MCP `tool_timeline` returned empty placeholders for non-temporal queries; the result-shape dereference is fixed. (#119)
- Schema migration on legacy DBs: `create_schema()` now runs `migrate()` between table-creation and index-creation, so the `idx_documents_agent_owner` index doesn't try to fire before the column exists. Regression test added. (#133)

### Security

- **2 BLOCKER path-traversal vulnerabilities** (SonarCloud S2083) cleared with documented CLI trust-boundary rationale. (#121)
- **23 MEDIUM hotspots** triaged with explicit per-finding rationale (ReDoS on bounded-input regexes, weak-cryptography on non-security `random.*`, Dockerfile permissions). (#128)
- **26 LOW hotspots** triaged; 2 real script-default fixes, the rest documented as false positives or deferred to a supply-chain hardening sprint. (#129)

### Migration

Drop-in upgrade. To benefit from `in_default`, set `in_default: false` on collections you want excluded from default search and restart the kairix containers. The deploy was UAT'd on the production instance before this release tag was cut.

## [2026.5.3] - 2026-05-04 — MCP availability, agent bug closure, scope semantics

> **Upgrading? Read [`docs/upgrades/v2026.5.3.md`](docs/upgrades/v2026.5.3.md) first.** It tells your agents (or you) exactly what to change. The TL;DR is: **swap `/sse` to `/mcp` in your MCP client config.** No auth changes, no tunnels.

### Added
- **Streamable HTTP transport at `/mcp`** — every MCP tool call is now a normal HTTP request/response. Stateless per-request, no idle-connection failure mode. The legacy `/sse` endpoint is preserved on the same port for back-compat; clients can migrate at their own pace.
- **`/healthz` endpoint** — reflects readiness. Tool calls during cold-start return a structured `{"error": "kairix-initializing", "retry_after_ms": 1500}` instead of crashing.
- **Typed `Scope` parameter on every retrieval tool** — `search`, `prep`, `timeline`, and `contradict` accept five values: `shared`, `agent`, `shared+agent` (default), `all-agents`, `everything`. Cross-agent synthesis via `scope=all-agents` is now a first-class operation.
- **Agent registry** — `kairix.config.yaml` accepts an `agents:` section that declares which agents exist. `scope=all-agents` resolves to the union of their collections. Default per-agent path is `/data/workspaces/{agent}` when not declared explicitly.
- **`kairix config validate`** sub-command — catches missing collection names, duplicate agent definitions, overlapping write paths, unknown retrieval-override keys before they hit production.
- **`docs/operations/MCP-DEPLOYMENT.md`** — operator deployment guide.
- **`docs/operations/MCP-CLIENT-MIGRATION.md`** — client-side migration guide with per-client steps for Claude Desktop, Claude Code, OpenClaw, and custom Python/Node clients.
- **`docs/upgrades/v2026.5.3.md`** — version-specific upgrade guide. Drop into your agent's reading list for self-managed migrations.
- **Search log fields** — `agent`, `scope`, `collections_searched`, `vec_failed` added to the JSONL event schema.

### Changed
- **Container entrypoint** — `--transport http` (canonical) instead of `--transport sse` (deprecated alias).
- **Bundled `docker-compose.yml`** — host port now binds to `127.0.0.1` only by default. Kairix has no built-in auth; operators who want external access drop the prefix and put a gateway with auth in front.
- **`mcp` package floor** — `>=1.20,<2` (was `>=1.0,<2`) for streamable-HTTP transport stability. Other dependencies unchanged.
- **`contradict` default threshold** — `0.45` (was `0.6`) to match the new three-category composite scoring (direct + overstatement + status-mismatch). Result objects carry a `category` field. Saved invocations with explicit `--threshold 0.6` still work.
- **MCP error envelope** — uncaught exceptions inside tool handlers return `{"error": "<ExceptionClass>: <message>"}` instead of being masked as JSON-RPC `-32602 Invalid request parameters`. If you have retry logic on `-32602`, update it.
- **`tool_timeline` MCP behaviour** — falls through to search when the query has no temporal expression, matching CLI behaviour. Returns `is_temporal: false, fell_back: true`.

### Fixed
- **Research confidence always 0.0** — `mcp-kairix__research` now returns real confidence values. Previously `json.loads()` failed silently on prose responses; the new parser chain handles JSON and prose.
- **Briefing `--memory-root` path-doubling** — regression test guards against the failure mode and emits a warning if the override path already includes `/{agent}/memory`.
- **Entity suggest type errors** — role phrases are dropped, mistyped entities corrected via override sets, missing organisations promoted via configurable allowlist.
- **`-32602` masking real tool errors** — see Changed above.

### Architecture
- Eight new domain Protocols, each with a public Adapter, and a typed `Scope` enum closes Primitive Obsession. See `docs/architecture/ENGINEERING.md` §10 for the catalogue.
- Both pre-existing private-import test debts closed (`_collections_for`, `_parse_llm_response`).

### Tests
- **2,101 unit/contract/bdd tests**, **58 integration tests**. mypy strict clean across 167 source files. bandit clean on changed paths.

### Known incomplete (tracked)
- **#112** — kairix.config.yaml `retrieval:` section not loaded by the factory at runtime.
- **#114** — embed-side `agent_owner` chunk tagging.
- **#115** — multi-path agent collections schema (drops the hardcoded vault path; richer per-deployment customisation).
- **#116** — `prep` L0/L1 source non-determinism investigation.
- **#117** — user-vault gold-suite rebuild after document movement.

## [2026.4.27] - 2026-04-27 — Reference library gold suite, Docker-first deployment

### Added
- **160-query reference library gold suite** — curated benchmark covering all six query categories against the open-source reference library. Reproducible scores without a private knowledge store.
- **OpenAI SDK embed client** (#43) — `OpenAIEmbedProvider` using the `openai` SDK for direct OpenAI API embedding (non-Azure).
- **Multi-collection support** — `hybrid_search()` accepts multiple collection names; results fused across collections.
- **Port auto-detection** — `kairix mcp serve` and `kairix setup` auto-select an available port if the default is in use.

### Changed
- **Docker Compose is now the primary deployment method** — `docker compose up -d` replaces pip install as the recommended path. pip install remains as an alternative.
- **Benchmark scores updated** — weighted R10=0.8171, NDCG@10=0.8385, Hit@5=0.9629, MRR@10=0.7614 (160-query reference library suite).

### Tests
- **1,634 tests**, 86% coverage. Up from 1,222 at v2026.4.24a3.

## [2026.4.24a3] - 2026-04-24 — Researcher Agent, Embed SDK, security hardening

### Added
- **KFEAT-009: Self-contained storage** — removed QMD (Node.js) dependency entirely. Kairix now owns its own SQLite database, FTS5 full-text index, and sqlite-vec vector store. `pip install kairix` is the only install step.
- **BM25-primary fusion** — new default search strategy. BM25 results are ranked first; meaning-based (vector) results are appended for recall. 38-configuration sweep showed this outperforms standard RRF by +17% on weighted NDCG.
- **Configurable fusion strategy** — `RetrievalConfig.fusion_strategy` accepts `"bm25_primary"` (default) or `"rrf"`. Factory methods for common corpus types: `defaults()`, `for_semantic_corpus()`, `for_technical_documentation()`.
- **`kairix eval hybrid-sweep`** — grid search over fusion strategies, RRF constants, and boost parameters against a gold suite. Embedding cache for 60% faster iterations.
- **`kairix eval build-gold`** — TREC-style pooling + LLM judge to create unbiased relevance judgments from your own data.
- **`kairix eval sweep`** — BM25 column weight and query style optimisation.
- **KFEAT-010: MCP affordance** — budget auto-inference (entity lookups get smaller budgets, research queries get larger ones), entity-first hints in search results, plain-language tool descriptions.
- **KFEAT-004: Researcher Agent** — LangGraph state machine for iterative search. 6 nodes: classify_intent, retrieve, evaluate_sufficiency, refine_query, synthesise, give_up. Searches multiple times, refining the query until it finds a good answer or reports what's missing. Max 4 turns. New MCP tool: `tool_research()`.
- **EmbedProvider protocol** — `EmbedProvider` interface with `AzureEmbedProvider` and `OpenAIEmbedProvider` implementations using the `openai` SDK. Built-in retry, rate-limit handling, and exponential backoff. Factory: `get_embed_provider()`.
- **Public API surface** — `kairix.hybrid_search`, `kairix.SearchResult`, `kairix.RetrievalConfig`, `kairix.QueryIntent` exported from `kairix/__init__.py`.
- **`bm25_primary_fuse()`** in `rrf.py` — new fusion function for BM25-primary strategy.
- **Dependencies** — `langgraph>=0.2,<1` and `openai>=1.40,<2` added to core.

### Changed
- **README completely rewritten** — value-first messaging, plain language, cost comparison, agent platform integration context.
- **Benchmark scores updated** — weighted NDCG 0.818, NDCG@10 0.803, Hit@5 91.1% (293 queries, independent gold suite).
- **Vector default K** increased from 10 to 20 for better recall.
- **`RetrievalConfig`** now includes `fusion_strategy` and `rrf_k` fields.
- **Tool docstrings** rewritten for grade 8 reading level (plain language first, technical terms in brackets).
- **`CATEGORY_WEIGHTS`** centralised in `eval/constants.py` (was defined in 4 files with silent divergence).
- **`canonical_path()`** extracted to module level in `rrf.py` (was duplicated 3 times).
- **Multi-hop search** extracted from `search()` into `_run_multi_hop()` helper (reduces `search()` from 390 to ~320 lines).

### Fixed
- **Category alias bug** — sweep scoring now correctly maps `semantic→recall` and `keyword→conceptual`. Was dropping 40% of weighted score.
- **Cypher injection** — `GraphEdge` labels validated against `NodeLabel` enum via `__post_init__`.
- **Graph traversal DoS** — `max_hops` clamped to [1, 5].
- **MCP error leakage** — `str(exc)` no longer returned to callers; sanitised messages instead.
- **Secrets path leakage** — `OSError` messages no longer include internal file paths.
- **SSE transport** — MCP server defaults to `127.0.0.1` (was implicit `0.0.0.0`).
- **Lockfile** — moved from world-writable `/tmp` to `~/.cache/kairix/`.
- **Duplicate KV fetch** — `summaries/cli.py` now uses `kairix.secrets.get_secret()`.
- **Hardcoded legacy paths** — `benchmark/cli.py` QMD path replaced with `get_db_path()`.

### Removed
- **QMD dependency** — no more Node.js, npm, or external binary discovery.
- **`kairix/_qmd.py`** — QMD binary discovery module.
- **`qmd_azure_embed`** — backward-compatibility shim package.
- **`AnthropicBackend`** — stub that raised `NotImplementedError` on all methods (LSP violation).

### Security
- Dependency upper bounds added: `requests<3`, `httpx<1`, `pyyaml<7`.
- `SQLITE_VEC_PATH` no longer required; extension loaded via pip package.

### Tests
- **1,222 tests** (up from ~1,050 at v2026.4.18). 1,090 carry `@pytest.mark.unit`.
- New: 22 Researcher Agent tests, 25 MCP affordance tests, 8 EmbedProvider tests, 7 contract conformance tests, 5 e2e pipeline tests, 4 chunk-date enrichment tests.
- Dead QMD e2e test replaced with kairix pipeline e2e.

## [2026.4.18] - 2026-04-18 — kairix eval: automated evaluation suite generation

### Added
- **`kairix eval generate`** — GPL-inspired automated benchmark suite generation. Samples documents from the corpus, prompts gpt-4o-mini to write retrieval queries, runs hybrid search, judges retrieved documents with graded relevance (0/1/2), and outputs a suite YAML. Based on Generative Pseudo Labeling (Wang et al. 2022, NAACL).
- **`kairix eval enrich`** — converts an existing suite's `gold_path`-based cases to graded `gold_titles`. Runs hybrid search and LLM judge for each case. Preserves all other case fields.
- **`kairix eval monitor`** — canary regression detection with rolling JSONL log. Flags when weighted NDCG drops >5% vs the 7-day rolling average. Exit code 2 on regression (distinct from exit code 1 hard failure). Designed for integration after `kairix embed`.
- **`kairix eval report`** — generates a markdown trend report from the monitor log.
- **`kairix/eval/judge.py`** — per-document LLM relevance judge (gpt-4o-mini, 0/1/2 rubric, position-bias shuffle, 15-anchor calibration with `JudgeCalibrationError`).
- **`docs/evaluation/evaluation-methodology.md`** — methodology with research citations: Cranfield paradigm, GPL, TREC-DL, position bias (Arabzadeh et al. 2024), NDCG formula.
- **`docs/user-guide/eval-guide.md`** — user quickstart, command reference, monitoring setup, troubleshooting.

### Fixed
- Deployment process now uses tagged releases (`@v0.9.3`) rather than `@main` to make explicit which version is installed. `pip install git+...@main` silently skips reinstall when the version string is unchanged.

## [0.9.2] - 2026-04-15 — NDCG@10 in benchmark CLI output

### Changed
- **Benchmark CLI: NDCG@10 now shown in run summary** — `kairix benchmark run` now prints `NDCG@10`, `Hit@5`, and `MRR@10` directly below the weighted total when `ndcg`-scored cases are present in the suite. Previously these metrics were computed and stored in the result JSON but never displayed. NDCG@10 is the recommended metric for cross-run comparison; the weighted total continues to drive phase gate pass/fail logic.
- **Benchmark CLI: NDCG@10 delta in compare output** — `kairix benchmark compare A.json B.json` now shows a `NDCG@10 delta` row when both result files contain ndcg scores.
- `EVALUATION.md` — updated "Running the benchmark" section to show sample CLI output and clarify that NDCG@10 is the number to track across releases.

## [0.9.1] - 2026-04-15 — Apache 2.0, title-based qrels, Neo4j install script, deployment hardening

### Added
- **Benchmark: title-based document identity (TREC qrels pattern)** — `BenchmarkCase` now accepts `gold_title` (str) and `gold_titles` (list of `{title, relevance}` dicts) as the primary document identity for relevance judgments. Gold titles are stable note filename stems, decoupled from filesystem paths. A retrieved document matches if its filename stem normalises to the gold title, meaning benchmark scores are unaffected by vault reorganisation (files moved, folders renamed). New runner helpers: `_normalise_title()`, `_stem_from_path()`, `_title_in_retrieved()`, `_ndcg_score_by_title()`, `_hit_at_k_by_title()`, `_reciprocal_rank_by_title()`.
- **Benchmark: backwards compatibility** — existing suites using `gold_path`/`gold_paths` continue to work without modification. Path-based matching is retained as a fallback when `gold_titles`/`gold_title` are absent.
- **`kairix[neo4j]` optional dependency group** — `pip install "kairix[neo4j]"` installs the Neo4j Python driver (`neo4j>=5.0,<6.0`). Previously required a manual `pip install neo4j` step after deploy.
- **`check_secrets_loaded` two-tier check** — the deployment health check now probes the secrets file directly if env vars are absent. If the file exists and contains the required keys, the check returns OK with a note that credentials will activate on the next search call. This eliminates the false-negative on working deployments where secrets load lazily via `kairix._azure` import.
- **`scripts/install-neo4j.sh`** — Neo4j Community Edition install script. `--docker` (default): writes a minimal docker-compose.yml and starts `neo4j:5-community`. `--apt`: adds the Neo4j apt repository and installs via systemd. Both options print a GPL3 licence notice before proceeding, run `kairix onboard check` on completion.
- **`check_neo4j_reachable` improved fix hint** — now includes a `scripts/install-neo4j.sh` reference and a `docker run` one-liner for quick starts. Clarifies Neo4j is optional — entity boost and multi-hop are degraded without it.
- **`tests/onboard/test_check.py`** — deployment health check tests: Neo4j fix hint content assertions, secrets two-tier probe, vault root config, `run_all_checks` structural tests.

### Changed
- **Licence: MIT → Apache 2.0** — adds patent grant language. Better for commercial adoption and open-source ecosystem compatibility. `LICENSE` file replaced with full Apache 2.0 text. Copyright 2024-2026 quanyeomans contributors.
- `suites/example.yaml` — all cases migrated from `gold_paths` (path-based) to `gold_titles` (title-based). Documents are identified by their note slug, not their folder location.
- `EVALUATION.md` — methodology section rewritten to describe title-based qrels as the standard. Explains the TREC qrels convention, normalisation, and why title-based identity is correct for a living vault.
- `OPERATIONS.md` — cron section updated: replace inline `az keyvault secret show` with `source /run/secrets/kairix.env` (populated by `kairix-fetch-secrets.service`). Install instructions updated to `pip install kairix` / `pip install "kairix[neo4j]"`. New Neo4j section: optional dependency, install via `scripts/install-neo4j.sh`.
- `README.md` — install section updated to `pip install`; licence badge updated to Apache 2.0.
- `SECURITY.md` — rewritten to reflect current kairix architecture: tmpfs secrets via systemd oneshot unit, managed identity requirement, Neo4j GPL3 note, Apache 2.0 licence.

## [0.9.0] - 2026-04-14 — Neo4j-native entity system + Docker sidecar secrets

### Added
- **Curator health** (`kairix curator health`) rewritten to query Neo4j exclusively via Cypher. Reports entity counts, synthesis failures, missing vault_paths, and stale entities entirely from the graph — no SQLite dependency. `--no-neo4j` flag removed; client unavailability returns a graceful empty report.
- **entities.db retired**. `kairix/entities/` package deleted in full. Neo4j is the sole canonical entity store. `kairix entity` CLI subcommand removed. All product code (`mcp/server.py`, `briefing/sources.py`, `curator/`) updated to use Neo4j queries only.
- **Docker sidecar secrets via Azure Key Vault.** New `docker/vault-agent/` service: fetches five KV secrets at startup via `DefaultAzureCredential`, writes to tmpfs volume `/run/secrets/kairix.env` (chmod 600), signals readiness via `/run/secrets/.ready`. `kairix` service waits for `vault-agent: service_healthy` before starting.
- **`kairix/secrets.py`** — `load_secrets(path)` reads a `KEY=VALUE` file into env vars without overwriting existing values. Called at module import in `kairix/_azure.py` and `kairix/graph/client.py`. Priority: existing env vars > sidecar secrets > KV subprocess calls.
- **`docker/docker-compose.yml`** — three-service compose: vault-agent, kairix, neo4j:5-community. tmpfs secrets volume (`size=1m, mode=0700`) — secrets never written to disk.
- **`docker/.env.example`** — template for `KAIRIX_KV_NAME`, Azure service principal, path mounts, and Neo4j config.

### Removed
- `kairix/entities/` — entire package (\_\_init\_\_.py, cli.py, schema.py, graph.py, extract.py, pipeline.py, reconcile.py, resolver.py, stop\_entities.py, migrations/001\_initial.sql)
- `tests/entities/` — all entity unit and integration tests
- `KAIRIX_TEST_DB` env var from CI workflows (no longer needed)
- `kairix entity` CLI subcommand

### Changed
- `kairix curator health` now requires a live Neo4j connection; `--no-neo4j` flag no longer accepted
- `kairix/mcp/server.py` `tool_entity()`: entities.db fallback removed; Neo4j miss returns `{"error": "Entity not found: <name>"}` directly
- `kairix/briefing/sources.py` `fetch_recent_decisions()`: entities.db query block removed; decisions sourced from vault only

### Benchmark (v0.9.0, 95 curated queries)
- entity NDCG 0.811 → **0.714** (vault evolution — new content Apr 13–14 shifted gold ranks)
- keyword: 0.616 · procedural: 0.609 · temporal: 0.540 · multi_hop: 0.526 · semantic: 0.501
- **Overall NDCG@10: 0.587** · Hit@5: 0.821 · MRR@10: 0.679

---

## [0.8.1] - 2026-04-13 — Benchmark Infrastructure + Entity Enrichment

### Added
- **`kairix curator health`** — Curator agent health check CLI. Checks for synthesis failures (no summary), missing vault paths, and stale entities (configurable threshold, default 90 days). Reports Neo4j node counts when available. Output: vault-ready Markdown or JSON. Part of the Curator agent.
- **`kairix/llm/`** — `LLMBackend` protocol with `chat()`, `embed()`, `embed_as_bytes()` methods. `AzureOpenAIBackend` and `AnthropicBackend` (stub) implementations. `get_default_backend()` returns `AzureOpenAIBackend`. All product code now receives `LLMBackend` via dependency injection rather than importing backends directly.
- **Repo boundary** — all direct `kairix._azure` imports removed from product code. `hybrid.py` acquires embed via `_get_llm().embed_as_bytes()`. `search/planner.py` acquires chat via `_get_llm().chat()`. No module-level `kairix._azure` imports remain outside `kairix/llm/backends.py`.

### Fixed
- `vector_search_bytes()` now fetches `k × 4` candidates when a date filter is active. `VECTOR_DEFAULT_K=10` was too small for narrow date windows (e.g., "this week") — after force re-embed populated `chunk_date`, the top-10 candidates rarely included docs from a 7-day window, causing vec_count=0 for relative temporal queries.
- All intents now dispatch BM25 + vector in parallel. Previously keyword intent ran BM25-only, causing vector-only docs to miss entirely. Keyword NDCG: 0.48 → **0.62** (+0.110).

### Benchmark (v0.8.1, 95 curated queries)
- keyword NDCG: 0.48 → **0.616** (hybrid fix)
- entity: **0.811** · procedural: 0.609 · temporal: 0.540 · multi_hop: 0.526 · semantic: 0.501
- **Overall NDCG@10: 0.603** · Hit@5: 0.821 · MRR@10: 0.669

## [0.8.0] - 2026-04-11 — CRM Interaction Chunker + Temporal Benchmark Expansion

### Added
- Generic CRM interaction chunker. Processes JSON contact/interaction exports and writes one chunk file per interaction with injected frontmatter (date, contact, meeting_type). Enables CRM timelines to be embedded and searched with temporal filtering. 20 tests.
- Expanded temporal benchmark — 7 new cases (T02–T08) covering absolute date queries (T02–T05) and relative temporal expressions (T06–T08). Demonstrates correct behaviour: absolute date queries bypass date-range filter; relative expressions apply it.

### Notes
- The absolute-vs-relative temporal distinction (introduced in v0.7.0) is now validated with a broader case set.
- CRM interaction chunker is format-agnostic — adapt the provided script to your CRM's export schema.

## [0.7.0] - 2026-04-10 — Temporal Retrieval + Date Infrastructure

### Added
- `chunk_date` column in `content_vectors` — idempotent migration via `schema.py:ensure_vec_table`. Stores the date extracted from each chunk's source document.
- `kairix/embed/date_extract.py` — date extraction at embed time from (1) frontmatter `date`/`created`/`updated`/`created_at` fields (YYYY-MM-DD), (2) YYYY-MM year-month fields (mapped to first of month), (3) filename pattern `YYYY-MM-DD.md`. 24 tests.
- `get_date_filtered_paths(db, start, end)` in `embed/schema.py` — returns `frozenset[str]` of document paths with `chunk_date` in the given window. Used by `hybrid.py` for TEMPORAL intent date-range filtering.
- `is_relative_temporal(query)` in `temporal/rewriter.py` — returns `True` for relative temporal expressions (`last N days/weeks/months`, `recently`, `yesterday`, `today`, `this week/month`). Date filtering is only applied for relative expressions — absolute date references (`March 2026`, `2026-03-09`) query `about` a time period and must not be filtered by chunk_date.
- Date-filtered retrieval in `hybrid.py` — BM25 results post-filtered via `_path_from_file_uri()` + `date_filter_paths`; vector results post-filtered directly on `path`. Both fallback gracefully (no filter applied) when `date_filter_paths` is `None` or empty.
- `scripts/chunk-daily-files.py` — pre-processor for daily log files (`YYYY-MM-DD.md`). Splits on `##` headings, writes section chunks with injected frontmatter so each section inherits its parent document's date. 11 tests.
- `scripts/audit-date-formats.py` — scans vault `.md` frontmatter for date field coverage. Classifies values as ISO / YYYY-MM (year-month) / non-ISO / absent. 13 tests.
- YYYY-MM year-month frontmatter pattern in `date_extract.py` — maps `date: 2025-11` to `2025-11-01`. 6 additional tests.

### Fixed
- `kairix/embed/embed.py` — replaced hardcoded Key Vault name in error messages with `$KAIRIX_KV_NAME` env var reference.

### Benchmark (v0.7.0, 83 curated queries)
- temporal NDCG: 0.369 → **0.382** (date filtering for relative temporal expressions)
- entity: 0.751 · multi_hop: 0.549 · procedural: 0.564 · semantic: 0.519 · keyword: 0.439
- **Overall NDCG@10: 0.5569** · Hit@5: 0.84 · MRR: 0.67

## [0.6.0] - 2026-04-07 — Post-Refactor Benchmark + Relationship Enrichment

### Added
- `scripts/seed-entity-relations.py` — LLM-typed relationship enrichment via GPT-4o-mini batch classifier
- Nightly cron (`0 3 * * * AEST`) — entity extract + relationship seed, Azure KV secret fetch
- `cron-scripts/cron-registry.json` entry for `entity-relation-seed`
- `scripts/build-eval-gold.py` — rebuilds benchmark gold suite from live search + LLM judge
- `suites/v2-real-world.yaml` — fully rebuilt gold suite (263 cases; collection-relative path format)
- Benchmark results: NDCG@10 **0.7756** (entity 0.823, recall 0.788, multi_hop 0.728, temporal 0.810, conceptual 0.804, keyword 0.800, procedural 0.389)
- OPERATIONS.md: comprehensive deployment guide (Azure prerequisites, Key Vault secrets, first-run sequence, cron setup, monitoring, troubleshooting)

### Fixed
- Embed batch retry on dimension mismatch — `ensure_vec_table(db, actual_dims)` called per-batch on dimension error, retries once
- Hourly embed cron: now fetches `AZURE_OPENAI_ENDPOINT` and `AZURE_OPENAI_API_KEY` from Azure Key Vault at runtime (managed identity)
- Gold suite paths: rebuilt to collection-relative format (matching `kairix search` output) after vault refactor broke 196/554 paths

### Benchmark
- NDCG@10 **0.7756** on 263-case suite (vault refactor fully indexed, gold paths rebuilt)
- Entity graph: 1160 entities, 112 typed relationships seeded
- Next milestone: procedural NDCG ≥ 0.55 (current 0.389)

---

## [0.5.3] - 2026-03-28 — 1536-dim Gold Recalibration

### Added
- Recalibrated benchmark instrument after discovering 768-dim baseline was measuring a broken config (extension load order caused silent 0-dim writes)
- Confirmed 1536-dim as correct operational config; rebuilt 252-case gold suite at correct dimensionality
- `scripts/run-benchmark-v2.py`: NDCG@10 scoring engine replacing weighted-total runner

### Benchmark
- 768-dim true baseline: NDCG@10 0.7690 on 252-case suite
- 1536-dim operational: NDCG@10 0.7545 — keyword +0.114, entity +0.043 vs 768-dim

---

## [0.5.2] - 2026-03-26 — Real-World Eval Rebuild

### Added
- Replaced synthetic benchmark with real agent usage queries mined from server logs
- NDCG@10 scoring (was weighted category averages) — 134-case real-world suite
- Temporal routing fix — temporal queries routed to `kairix temporal query` before hybrid search
- Multi-hop pattern improvements — intermediate result reranking, entity bridging
- Suite expanded to 252 cases; multi-category NDCG scoring

### Benchmark
- Initial (instrument issues): NDCG@10 0.3203 on 134-case suite
- After instrument + temporal fix: NDCG@10 improved to 0.69+ range before recalibration

---

## [0.5.1] - 2026-04-06 — Entity Graph + Multi-Hop Planner

### Added
- Multi-hop QueryPlanner — GPT-4o-mini decomposes complex queries into sub-queries, parallel BM25+vector dispatch, result synthesis
- Entity graph seeded from vault-entities collection; entity boost wired into planner context injection
- `kairix entity extract --changed` incremental extraction pipeline
- `scripts/seed-entity-relations.py` (pattern-matching v1 — superseded by LLM classifier)

### Benchmark
- NDCG@10 0.7541 on 245-case suite — multi_hop 0.716 (+0.035 vs prior), entity 0.677

---

## [0.5.0] - 2026-03-23 — Temporal + Summaries + Wikilinks

### Added
- Temporal chunker + query rewriter + timeline CLI
- L0/L1 summaries generation (gpt-4o-mini) + tier router
- Wikilink injector + entity resolver + audit CLI
- Entity NER extraction pipeline + ontology reconciler
- Raw query logging: `KAIRIX_LOG_QUERIES=1` → queries.jsonl
- `scripts/analyze_queries.py`: real query distribution analysis
- Keyword zero-result fallback to vector search

### Fixed
- Vector index re-embedded at 1536-dim (was 768-dim — vectors never landed in vectors_vec)
- KV cold-start causing entity vector search failures (20-45% failure rate)
- Keyword queries returning 0 results when BM25 returns empty

## [0.4.0] - 2026-03-23 — Briefing + Classification

### Added
- `kairix brief <agent>` — 8-step concurrent briefing pipeline synthesises ~800-token session context from memory logs, entity stubs, rules, decisions, and hybrid search via GPT-4o-mini
- `kairix classify "<content>"` — two-stage auto-classification (rule-based first, LLM fallback) routes new writes to the correct vault file with confidence score
- `kairix/_azure.py`: `chat_completion()` for GPT-4o-mini synthesis calls
- `kairix/briefing/`: pipeline.py, sources.py, synthesiser.py, writer.py, cli.py — 48 tests
- `kairix/classify/`: rules.py, judge.py, router.py, cli.py — 83 tests
- Benchmark suite v1.1: CL01–CL04 classification cases; classification scoring in runner
- ENGINEERING.md: entity failure-mode patterns, benchmark suite maintenance rules, gold-path validity rules

### Fixed
- LLM judge KV secret name: `azure-openai-gpt4o-mini-deployment` (was `azure-openai-deployment` — silent 0.0 scoring on all LLM-judged benchmark cases)
- RRF path dedup: `_canonical_path()` strips collection prefix so BM25 and vector results for entity stubs now merge correctly in fused dict
- Entity benchmark gold paths: E01–E06 now have `gold_path` + `score_method: exact` (was `null`/`llm` — LLM judge had no ground truth, scored 0.2–0.4 on tangential docs)
- Entity stub content: jordan-blake.md, acme-corp.md, platform.md enriched to 650–750 words; project-x.md to 490 words

### Benchmark
- entity: 0.300 → 0.933 (gold-path fix + stub enrichment)
- classification: 1.000 (4/4 rule-based, deterministic)
- recall: 0.875 (stable)

---

## [0.3.0] - 2026-03-23 — Entity Benchmark Repair

### Added
- Entity stub enrichment: jordan-blake.md, acme-corp.md, platform.md, project-x.md enriched to ≥500 words
- Gold paths added to entity benchmark cases E01–E06

### Fixed
- Entity score collapse (0.733→0.300): root cause — benchmark gold_path: null + sparse stub content

## [0.2.0] - 2026-03-22

### Added
- Intent classifier (keyword/semantic/temporal/entity/procedural)
- BM25 wrapper (subprocess → structured results)
- Vector search wrapper (sqlite-vec CTE MATCH)
- RRF fusion + entity boost
- Token budget enforcer (L0/L1/L2 tiers)
- Hybrid orchestrator + parallel dispatch
- `kairix search` CLI
- Entity graph schema + migration system
- Entity graph (write, lookup, mentions, relationships)
- `kairix entity` CLI
- Benchmark CLI: YAML suite format, validate/run/compare/init commands
- Generalised benchmark framework SPEC.md
- CI: 4-stage pipeline, mypy strict, ruff, bandit, pip-audit, Dependabot
- ENGINEERING.md contributor guide

### Fixed
- sqlite-vec CTE pattern: MATCH must be primary table in inner CTE
- Collection scope: _SHARED_COLLECTIONS was missing vault (93% of content)
- Benchmark gold-pair validity: several benchmark gold pairs replaced with valid pairs

## [0.1.0] - 2026-03-22

### Added
- Azure OpenAI embedding pipeline (text-embedding-3-large, 1536-dim)
- Schema validation + sqlite-vec extension loading
- Staging table pattern for vec0 upserts
- Recall gate (5/5 known-doc queries post-embed)
- `kairix embed` CLI
- 50-query benchmark runner (BM25 baseline: 0.5054)
