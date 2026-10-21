# Replication — rebuild the full stack from zero

This is the complete from-scratch sequence. Each stage is restartable and writes into shared SQLite stores. GPU stages (parsing) run on HPC; LLM stages run on the qwen farm.

## Prerequisites

- Python 3.13 venv; `pip install -r web/requirements.txt`
- Access to the pecans qwen3.8-27b farm (endpoint pool + `DLS-2026` key) OR the Argo LiteLLM aggregator
- Storage: ~hundreds of GB for the corpus, ~3.5 GB for derived DBs
- For GPU parsing: Marker + Nougat on A100/PVC (ALCF Polaris/Aurora or uicgpu)

## Stage 0 — Catalog

```bash
python scripts/build_catalog.py        # OSTI JSON API → catalog.sqlite
```
Authoritative universe: `datasets/manifests/papers_manifest_clean.tsv` (234,705 papers).

## Stage 1 — Fetch corpus

```bash
python scripts/fetch_osti_corpus.py    # PURL + discovery → <year>/<osti_id>.pdf
```
> OSTI fetches must run from a host OSTI doesn't blackhole (e.g. cels-rbdgx2), not a home Comcast IP.

## Stage 2 — Parse PDFs (GPU)

Marker → `.md`, Nougat → `.mmd`, one of each per paper. Run as HPC batch jobs (restartable, work-queue claims). Paths recorded in catalog `md_path`/`mmd_path`.

## Stage 3 — Extract x-cards (LLM farm)

```bash
python pipeline/extraction/sync_cards.py     # parsed text → 7-modality cards → osti_cards.sqlite
python pipeline/extraction/sync_mmd_pdfs.py  # link source PDFs/.mmd for render
```
⚠️ `sync_cards.py` DROPS/RECREATES the `cards` table — run enrichment/QA/review AFTER a full sync, or they'll be lost.

## Stage 4 — QA score

```bash
python pipeline/quality/qa_score.py          # v4 rubric → qa_tier, qa_* columns
python pipeline/quality/url_validate.py      # validate/annotate URLs
```

## Stage 5 — Enrich

```bash
python pipeline/enrichment/entity_resolve.py     # entities.sqlite
python pipeline/enrichment/enrich_crosscard.py   # cross-card consensus fact transfer
python pipeline/enrichment/email_mine.py         # author emails from paper text / sibling cards
python pipeline/enrichment/enrich_pipeline.py    # apply/merge → card_enrichment.sqlite
```

## Stage 6 — SuperCards

```bash
python pipeline/supercards/supercards_build.py   # merge same-resource cards → supercards.sqlite
```

## Stage 7 — Review articles (LLM farm)

```bash
# SuperCards first (best evidence):
python pipeline/reviews/review_synth.py --kind super --workers 34
# then individual cards (sharded across workers):
python pipeline/reviews/review_synth.py --kind card --shard 0/4 --workers 20   # ×4 shards
```
Restartable (claims via `review_md IS NULL`). Zero-hallucination; `enable_thinking=False`.

## Stage 8 — Citations

```bash
python pipeline/citations/citation_fetch.py      # OpenAlex cited_by_count → citations.sqlite
```
> OpenAlex now has a per-IP daily budget; run from a fresh-quota IP (e.g. prokko at UChicago) if m1 is capped.

## Stage 9 — Serve

```bash
cd web && uvicorn app:app --host 0.0.0.0 --port 8094
python ../mcp/mcp_server.py                       # agent access
# public: tailscale funnel → :8094
```

## Verify

```bash
python tests/test_site_e2e.py                     # end-to-end
curl localhost:8094/api/stats
curl localhost:8094/api/supercards/stats
```
