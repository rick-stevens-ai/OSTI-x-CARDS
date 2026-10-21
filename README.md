# OSTI-x-CARDS

**Structured, agent-actionable knowledge cards extracted from the U.S. DOE OSTI scientific corpus — plus the full pipeline, prompts, web interface, and dataset pointers to replicate the entire stack.**

OSTI-x-CARDS turns a corpus of ~235,000 DOE scientific papers into **x-Cards**: structured, machine-readable records describing the research *resources* each paper introduces or uses — datasets, models, instruments, facilities, simulation codes, computational workflows, and software agents. Cards are extracted by LLM, quality-scored, enriched across the corpus, merged into consensus **SuperCards**, rendered as **review/tutorial articles**, and served through a browsable web interface + an MCP server for agent access.

---

## What's in a card

Each **x-Card** captures one research resource from one paper, across 7 modalities:

| Modality | What it captures | Example |
|---|---|---|
| `data` | Published datasets | FLUXNET2015 eddy-covariance flux data |
| `model` | Trained/parametric models | A crystallography refinement model |
| `agent` | Autonomous software agents | Peregrine (ORNL additive-manufacturing agent) |
| `instrument` | Physical measurement devices | An X-ray streak camera at APS Sector 7 |
| `facility` | User facilities / large infrastructure | Advanced Photon Source, NERSC |
| `simulation` | Simulation / modeling codes | VASP, LAMMPS |
| `workflow` | Computational pipelines | A genomics variant-calling workflow |

A **SuperCard** merges every card referring to the *same real-world resource* into one canonical record with evidence-weighted consensus fields (e.g. the VASP SuperCard consolidates 3,887 papers) and a synthesized **review article**.

---

## Repository layout

```
OSTI-x-CARDS/
├── pipeline/            # the extraction → serving pipeline (code only, no data)
│   ├── extraction/      # OSTI paper → raw x-cards (LLM farm extraction)
│   ├── enrichment/      # cross-card fact transfer, email/contact mining, entity resolution
│   ├── supercards/      # merge cards referring to the same resource
│   ├── reviews/         # synthesize review/tutorial articles (cards + supercards)
│   ├── quality/         # QA scoring, URL validation, repo filters
│   └── citations/       # OpenAlex / Semantic Scholar citation enrichment
├── web/                 # FastAPI browsing/search site + static frontend
├── mcp/                 # MCP server exposing cards to agents
├── prompts/             # all LLM prompts (extraction, enrichment, review synthesis)
├── docs/                # architecture, QA rubric, strategy docs, runbooks
├── datasets/            # pointers + manifests to replicate the whole stack (see below)
├── tests/               # end-to-end + performance tests
└── scripts/             # operational helpers
```

> **Data is NOT stored in this repo.** The SQLite databases (cards 2.7 GB, supercards 192 MB, entities 144 MB, etc.) and the source OSTI corpus (hundreds of GB of PDFs + parsed text) live on dedicated storage. See [`datasets/`](datasets/) for how to obtain or rebuild every piece.

---

## The pipeline, end to end

```
 OSTI corpus (PDFs)
      │  Marker / Nougat  →  parsed .md / .mmd  (see datasets/)
      ▼
 ┌─────────────┐
 │ extraction  │  LLM farm reads parsed text → 7-modality x-cards (YAML)  [pipeline/extraction]
 └─────────────┘
      ▼
 ┌─────────────┐
 │  quality    │  QA score each card (v4 rubric): URL + names + email + tech info  [pipeline/quality]
 └─────────────┘
      ▼
 ┌─────────────┐
 │ enrichment  │  cross-card fact transfer, email mining, entity resolution  [pipeline/enrichment]
 └─────────────┘
      ▼
 ┌─────────────┐
 │ supercards  │  merge cards for the same resource → consensus records  [pipeline/supercards]
 └─────────────┘
      ▼
 ┌─────────────┐
 │  reviews    │  LLM synthesizes review/tutorial articles (cards + supercards)  [pipeline/reviews]
 └─────────────┘
      ▼
 ┌─────────────┐   ┌──────────┐
 │  web (API)  │   │   mcp    │   browse / search / agent access  [web/, mcp/]
 └─────────────┘   └──────────┘
```

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for full detail and [`docs/REPLICATION.md`](docs/REPLICATION.md) for a from-scratch rebuild.

---

## Quick start (serve the existing site)

```bash
# 1. obtain the data (see datasets/README.md)
#    → places osti_cards.sqlite, supercards.sqlite into web/data/
# 2. serve
cd web
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
uvicorn app:app --host 0.0.0.0 --port 8094
# browse: http://localhost:8094   •   supercards: /supercards.html
```

---

## Current scale (snapshot)

- **~311,000 x-cards** across 134,950 distinct papers
- **18,862 SuperCards** (18,854 with review articles) merging 155,110 member cards
- Official DOE OSTI subject classification on 193,000+ cards
- Public endpoint via Tailscale Funnel; durable access logging

---

## Design principles

- **Zero hallucination.** Emails, URLs, versions are only recorded if observed in paper text, another card, or a verified contact DB — never pattern-constructed.
- **Review/tutorial, not sparse outlines.** Cards and SuperCards render as synthesized explanatory articles grounded in source papers with `[OSTI:id]` citations.
- **Replace-on-improvement.** Enrichment never regresses a good card.
- **Restartable at every stage.** Farm jobs claim work atomically and resume.

---

## License & provenance

Derived from U.S. Department of Energy OSTI public records. See [`docs/PROVENANCE.md`](docs/PROVENANCE.md).
