# OSTI x-CARD Quality Assurance Rubric & Completeness Schema
_v1 — 2026-10-10 — authoritative spec for the QA pipeline_

## Purpose
Every x-CARD must be **robust and complete for its paper**: all fields filled either (a) **directly** with
content from the paper, or (b) **derived** from paper content — and derived content MUST be bracketed.
A paper with N datasets / simulations / agents / workflows / instruments / facilities MUST yield **all N**,
not just the first. Every card must carry **provenance** back to the source (OSTI id, authors, emails,
laboratories), and **funding acknowledgements** from the paper. The QA pipeline tiers cards
**high / medium / low** so consumers can distinguish quality.

---

## 1. Derived-content bracketing convention (GLOBAL RULE)
- **Direct content**: copied/quoted/closely-paraphrased from the paper → written plain.
- **Derived content**: inferred, normalized, or synthesized by the model from paper content →
  wrapped in square brackets, e.g. `license: "[derived: CC-BY-4.0 from journal default]"`,
  `instrument_type: "[inferred: cryogenic detector]"`.
- **Unknown/absent**: value genuinely not present and not derivable → `"UNKNOWN"` (never guess unbracketed).
- Rationale: a reader (and the quality scorer) must be able to tell paper-grounded fact from model inference.

---

## 2. Multi-entity capture (HARD REQUIREMENT)
- Each card MODE emits a **list of entities**, not a single scalar. If a paper describes 3 instruments,
  the instrument card for that paper contains 3 instrument entries.
- Supersedes the legacy single-`<CARD>`-block / `CARD_RE.search` first-match-only behavior.
- Each entity within the card is independently completeness-scored; the card's entity_count is recorded.
- De-dup within a card: same named entity mentioned repeatedly = one entity (merge mentions).

---

## 3. Universal provenance block (REQUIRED on every card, every entity-set)
```
source:
  osti_id: "<id>"                      # REQUIRED, direct
  title: "<paper title>"               # REQUIRED, direct
  doi: "<doi or UNKNOWN>"              # direct
  url: "<landing/pdf url or UNKNOWN>"  # direct
  authors: ["Last, First", ...]        # REQUIRED, direct from paper/metadata
  author_emails: ["a@lab.gov", ...]    # direct if present in paper
  affiliations: ["Argonne National Laboratory", ...]   # direct/derived(bracket)
  primary_lab: "<DOE lab or UNKNOWN>"  # derived(bracket) from affiliations allowed
  funding:                             # REQUIRED if paper has an acknowledgements/funding section
    - agency: "DOE Office of Science"  # direct
      award: "DE-AC02-06CH11357"       # direct
    - agency: "NSF"
      award: "UNKNOWN"
  funding_text: "<verbatim acknowledgement sentence(s)>"   # direct
```

---

## 4. Per-card-type required fields
(All card types also require §3 provenance + §2 multi-entity list.)

| Card type | Required entity fields |
|---|---|
| **data** | dataset_name, dataset_type, short_description, full_description, provider{organization}, license, access/availability, repository_url (if any), size/format (direct or bracket), version |
| **model** | model_name, model_type, short_description, full_description, developed_by, base_model, license, code_url (if any), version, training_data_ref (if any) |
| **instrument** | instrument_name, instrument_type, role_in_paper (used/built/described), short_description, full_description, operated_by, manufacturer, model_number, location (facility/lab) |
| **facility** | facility_name, facility_type, role_in_paper, short_description, full_description, operator, location, capabilities, access |
| **simulation** | code_name, code_type, role_in_paper, short_description, full_description, developed_by, version, license, code_url (if any), inputs/physics (direct or bracket) |
| **workflow** | workflow_name, workflow_type, short_description, full_description, developed_by, provider{organization,url}, steps/components (direct or bracket), language, license, repository_url (if any) |
| **agent** | agent_name, agent_type, short_description, full_description, developed_by, provider{organization,url}, capabilities, architecture, license, code_url (if any) |

A field is **filled** if it is non-empty AND not bare `"UNKNOWN"` (bracketed-derived counts as filled).

---

## 5. Quality tiers (the scorer's output)
Per card, compute three sub-scores (0–1), then a tier:

- **completeness** = filled_required_fields / total_required_fields (averaged across entities)
- **provenance** = weighted presence of §3 block (osti_id .1, title .1, authors .2, emails .15,
  affiliations .15, funding .3) — funding weighted highest per Rick's emphasis
- **grounding** = fraction of entity-defining claims that are supported by (traceable to) the parsed
  full text — LLM-judged, not regex (per standing REGEX HARD RULE)

**Tier assignment:**
- **HIGH**: completeness ≥ 0.85 AND provenance ≥ 0.80 AND grounding ≥ 0.85 AND entity_count correct
  (no missed entities on a re-check)
- **MEDIUM**: completeness ≥ 0.60 AND provenance ≥ 0.50 AND grounding ≥ 0.70
- **LOW**: anything below medium, OR any hallucinated/ungrounded entity, OR parse was degenerate.

DB fields written: `qa_tier`, `qa_completeness`, `qa_provenance`, `qa_grounding`, `qa_entity_count`,
`qa_missing_fields` (csv), `qa_notes`, `qa_version`, `qa_scored_at`.

---

## 6. Pipeline stages (idempotent, restartable, each writes a ledger)
1. **AUDIT** — measure current cards vs rubric (read-only; produces the gap report).
2. **RE-EXTRACT (multi-entity)** — re-run extraction with list-emitting prompt on papers whose card is
   single-entity-but-paper-has-more, or low-tier. Needs good parse (.md/.mmd).
3. **ENRICH** — backfill §3 provenance + funding from parsed full text + OSTI metadata; bracket derived.
4. **SCORE** — compute tiers, write qa_* fields.
5. **SURFACE** — website quality-tier filter + badges; code-artifact URL filter.
6. **CONTINUOUS** — re-run 2–4 on any card whose source parse or extraction changed (hook off the
   existing 5-min self-chaining sweep).

---

## 7. Non-negotiables (Rick's standing rules applied)
- **No regex for judgment** — grounding, entity-detection, field-derivation all use the LLM.
  Regex only for mechanical structural parsing (splitting YAML, URL host extraction).
- **Don't change what's working** — QA writes NEW `qa_*` columns + NEW re-extracted card versions;
  never destructively overwrites the existing card corpus. Old cards remain; superseded by version.
- **Idempotent / restartable** — every stage claims work atomically, resumes cleanly.
- **Honest tiering** — a LOW card is labeled LOW; we surface the distinction, we don't hide it.
