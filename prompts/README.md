# LLM Prompts

All prompts used across the OSTI-x-CARDS pipeline, in one place for review and iteration. These are the actual templates the pipeline runs — keep this directory in sync with the code that uses them.

| Stage | Prompt file | Used by | Model(s) |
|---|---|---|---|
| Card extraction | `extraction.md` | `pipeline/extraction/sync_cards.py` (+ extract-o-matic) | qwen farm / Argo Sonnet |
| Review synthesis (SuperCards) | `review_supercard.md` | `pipeline/reviews/review_synth.py --kind super` | qwen3.8-27b farm |
| Review synthesis (individual cards) | `review_card.md` | `pipeline/reviews/review_synth.py --kind card` | qwen3.8-27b farm |
| Enrichment (cross-card consensus) | `enrichment.md` | `pipeline/enrichment/enrich_crosscard.py` | rule-based + LLM gap-fill |
| QA scoring | see `../docs/QA_RUBRIC.md` | `pipeline/quality/qa_score.py` | deterministic v4 rubric |

## Core principles baked into every prompt

1. **Zero hallucination** — never invent URLs, emails, versions, or facts absent from the evidence. If unknown, say so plainly.
2. **Review/tutorial prose, not sparse outlines** — synthesized explanatory articles grounded in source papers.
3. **Inline `[OSTI:id]` citations** — every synthesized claim points back to the paper(s) it came from.
4. **Per-modality section structure** — a dataset review ≠ a facility review ≠ a simulation-code review.
