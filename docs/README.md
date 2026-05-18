# docs/

User-facing documentation. Internal sprint planning and PM artefacts
live in the operator's private knowledge store, not here (this repo
stays user-facing).

- `getting-started/` — quick-start guide for new users
- `architecture/` — ENGINEERING.md, fitness-functions.md, ADRs
- `evaluation/` — EVALUATION.md (benchmark methodology, suite design)
- `operations/` — OPERATIONS.md (deploy, monitor, secret rotation)
- [`runbooks/`](runbooks/README.md) — incident-response playbooks (one per scenario); see `operations/runbooks/INDEX.md` for the docker-compose how-to set
- `agents/` — AGENT-SETUP.md, ADMIN-CONVERSATION.md (agent-facing docs)
- `upgrades/` — per-release upgrade notes referenced from CHANGELOG.md
- `user-guide/` — end-user task guides
- `reference/` — CLI / API reference material
- `project/` — ROADMAP.md and other living project pages

Keep entries at grade 8 reading level. CHANGELOG entries are 15-45 lines
max — link out to docs/upgrades/ for detail.
