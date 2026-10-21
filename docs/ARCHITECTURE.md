# Architecture

OSTI-x-CARDS is a batch knowledge-extraction pipeline feeding a read-serving layer. Everything is SQLite-backed, farm-parallel at the LLM stages, and restartable.

## Data model

### `cards` (osti_cards.sqlite)
One row per extracted resource-card. Key columns:

- `osti_id`, `year`, `mode` (data/model/agent/instrument/facility/simulation/workflow)
- `title`, `description`, `yaml` (full structured card)
- `urls`, `emails`, `authors` (JSON arrays; zero-hallucination)
- `qa_version`, `qa_tier` (HIGH/…), `qa_*` component scores (v4 rubric)
- `doe_codes`, `doe_subject_names`, `doe_domains` (official DOE OSTI classification)
- `review_md`, `review_model`, `review_at` (synthesized review/tutorial article)

FTS5 full-text index for search.

### `supercard` (supercards.sqlite)
One row per real-world resource, merging many cards.

- `super_id`, `canonical_name`, `mode`, `member_count`, `first_year`/`last_year`
- `consensus_json` (evidence-weighted field consensus with support/dissent counts)
- `urls`, `emails`, `labs` (observed across members)
- `evidence` (pre-joined member-paper descriptions, for review synthesis)
- `review_md`, `review_model`, `review_at`
- `supercard_member(super_id, osti_id)` join table

### `entities.sqlite`
Entity + alias resolution (names/tools/datasets) that feeds SuperCard merging.

## LLM infrastructure

- **Extraction + review synthesis** run on the **pecans qwen3.8-27b farm** — an endpoint pool (`~/bvbrc_all/endpoints/*.url`, ~17 live), auth `Bearer DLS-2026`, OpenAI-compatible `/v1/chat/completions`.
- `enable_thinking=False` for review synthesis (tokens → article, not reasoning).
- Jobs are sharded (`--shard M/N`, rowid%N) and claim work atomically (`review_md IS NULL`), so a kill loses only in-flight items.
- Fallback / high-quality path: Argo Sonnet via the LiteLLM aggregator (`localhost:4000`).

## Serving

- **`web/app.py`** — FastAPI + uvicorn on `:8094`, bound `0.0.0.0`.
  - Browse/search cards, SuperCards, download YAML/zip.
  - Review articles rendered as primary view (markdown + `[OSTI:id]` citation chips).
  - Durable access-log middleware → `access_log.sqlite`, `/api/access/stats`.
- **`mcp/mcp_server.py`** — MCP server exposing cards to agents.
- **Public access** — Tailscale Funnel: `https://<tailnet-funnel-host>` (no VPN). Tailnet: `http://<tailnet-host>:8094`.
- **launchd** service `com.stevens.osti-cards-site` (KeepAlive). Recycle: `launchctl kickstart -k gui/$(id -u)/com.stevens.osti-cards-site`.

## Quality (v4 rubric)

A card is **HIGH** when it has: a validated/actionable **URL** + author **names** + at least one author **email** + key **technical info**. Funding acknowledgement is *not* a quality trigger. See `QA_RUBRIC.md`.

## Storage & durability

Three-tier corpus replication (SG-1-8TB primary · DGX /rbstor mirror · Polaris/Eagle scratch). Derived DBs are rebuildable from the corpus at any time. See `datasets/`.
