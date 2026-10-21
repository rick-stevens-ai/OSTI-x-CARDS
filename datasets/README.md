# Datasets — obtaining & replicating the OSTI-x-CARDS stack

This repo contains **code, prompts, and documentation only**. The data artifacts are large and live on dedicated storage. This directory tells you **exactly what every dataset is, where it lives, how big it is, and how to either download it or rebuild it from scratch.**

There are three tiers of data:

1. **Source corpus** — the raw OSTI PDFs + parsed text (hundreds of GB). Needed only to *re-extract* cards.
2. **Derived databases** — the SQLite stores that power the site (cards, supercards, entities, citations). Needed to *serve* the site.
3. **Reference/manifest files** — small TSV/JSON manifests that pin the corpus universe and provenance (checked into this repo under [`manifests/`](manifests/)).

---

## Tier 1 — Source corpus (to re-extract cards)

| Dataset | Description | Size | Canonical location | How to obtain |
|---|---|---|---|---|
| OSTI PDF corpus | Full-text PDFs, year-sharded `<year>/<osti_id>.pdf` | ~hundreds of GB | `SG-1-8TB:/osti/pdfs/` (primary); DGX `cels-rbdgx2:/rbstor/stevens/osti/`; Polaris/Eagle scratch | `scripts/fetch_osti_corpus.py` (OSTI PURL + discovery); or copy from a replica store |
| Parsed text (.md) | Marker-parsed markdown per paper | ~tens of GB | co-located with PDFs; catalog `md_path` | rebuild: `pipeline/extraction` Marker jobs (GPU) |
| Parsed text (.mmd) | Nougat-parsed markdown per paper | ~tens of GB | co-located; catalog `mmd_path` | rebuild: Nougat jobs (GPU) |
| OSTI catalog | SQLite: osti_id → title/path/product_type/year/subjects | ~1.5 GB | `SG-1-8TB:/osti/catalog/catalog.sqlite` | rebuild from OSTI JSON API (`scripts/build_catalog.py`) |

**Corpus universe (frozen denominator):** `manifests/papers_manifest_clean.tsv` — 234,705 papers (Journal Article + Conference + Technical Report only; excludes books/theses/datasets).

---

## Tier 2 — Derived databases (to serve the site)

These are produced by the pipeline. To **serve** the site you only need `osti_cards.sqlite` + `supercards.sqlite`.

| Database | Produced by | Size | Serves | Rebuild command |
|---|---|---|---|---|
| `osti_cards.sqlite` | `pipeline/extraction/sync_cards.py` | ~2.7 GB | all card browse/search; holds `review_md`, QA scores, DOE subjects | `python pipeline/extraction/sync_cards.py` |
| `supercards.sqlite` | `pipeline/supercards/supercards_build.py` | ~192 MB | SuperCard pages + review articles | `python pipeline/supercards/supercards_build.py` |
| `entities.sqlite` | `pipeline/enrichment/entity_resolve.py` | ~144 MB | entity/alias resolution feeding supercards | `python pipeline/enrichment/entity_resolve.py` |
| `citations.sqlite` | `pipeline/citations/citation_fetch.py` | ~21 MB | OpenAlex `cited_by_count` per DOI | `python pipeline/citations/citation_fetch.py` |
| `card_enrichment.sqlite` | `pipeline/enrichment/enrich_pipeline.py` | ~10 MB | cross-card enrichment overlay | `python pipeline/enrichment/enrich_pipeline.py` |
| `access_log.sqlite` | `web/app.py` middleware | grows | access analytics (`/api/access/stats`) | auto-created at runtime |

**Download (snapshot):** published snapshots of the Tier-2 DBs are staged at the locations listed in [`manifests/db_snapshots.json`](manifests/db_snapshots.json). Pull with `scripts/pull_databases.sh`.

---

## Tier 3 — Reference manifests (in this repo)

Small, version-controlled under [`manifests/`](manifests/):

- `papers_manifest_clean.tsv` — frozen 234,705-paper universe (header only in-repo; full file via `db_snapshots.json`)
- `db_snapshots.json` — where to download each derived DB snapshot + sha256 + build date
- `corpus_stores.json` — all replica store locations for the source corpus
- `taxonomy_doe_subjects.json` — DOE OSTI subject-code → domain mapping used for classification

---

## Full rebuild from zero

See [`../docs/REPLICATION.md`](../docs/REPLICATION.md) for the complete from-scratch sequence. Summary:

```
1. Build/obtain OSTI catalog           → scripts/build_catalog.py
2. Fetch PDF corpus                     → scripts/fetch_osti_corpus.py
3. Parse PDFs (Marker + Nougat, GPU)    → pipeline/extraction (HPC jobs)
4. Extract x-cards (LLM farm)           → pipeline/extraction/sync_cards.py
5. QA-score cards                        → pipeline/quality/qa_score.py
6. Enrich (cross-card, emails, entities) → pipeline/enrichment/*
7. Build SuperCards                      → pipeline/supercards/supercards_build.py
8. Synthesize review articles (LLM farm) → pipeline/reviews/review_synth.py
9. Citations                             → pipeline/citations/citation_fetch.py
10. Serve                                → web/app.py + mcp/mcp_server.py
```

Each stage is **restartable** and writes into the shared SQLite stores.
