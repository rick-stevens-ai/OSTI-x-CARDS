# x-CARD Enrichment Strategy — Technical Info + Author Emails

Status: DRAFT for Rick review. Date: 2026-10-17. Owner: Kukla.
Governing rule: **transfer/observe known facts; never hallucinate URLs or emails.**
All enriched values are written **bracketed** with their source tag, e.g.
`license: "[enriched:cross-card] BSD-3-Clause"`, `location: "[enriched:web https://...] Argonne, IL"`.

## 0. Quality target (Rick, 2026-10-17)
HIGH = **validated URL + names + emails + key technical info present.**
Funding acknowledgement is a *nice-to-have*, NOT a tier driver.
Email goal: **≥1 good author email per card (more if possible).**

## 1. Measured gap (live DB, 232,035 cards / 103,017 papers)
| Component | Coverage | Gap |
|---|---|---|
| Names (authors) | 95.3% | nearly solved |
| Actionable URL (pre-validation) | 71.0% | validate + recover |
| **Emails** | 52.1% of papers | **49,358 papers lack any email** |
| **Key technical info (comp≥0.85)** | 20.9% | **dominant limiter** |

Most-missing technical fields (corpus-wide):
location 116k · license 110k · version 69k · access 53k · steps_components 40k ·
model_number 32k · manufacturer 21k · base_model 13k · operated_by 12k.

Top-3 missing per mode:
- instrument: location, model_number, manufacturer
- facility: location, access, operator
- simulation: license, version, developed_by
- model: version, license, base_model
- data: access, version, license
- workflow: steps_components, license, provider
- agent: architecture, license, provider

## 2. Source ladder (cheapest + most trustworthy first)

### Email sources
- **S-E1 Parse-mining (zero-hallucination, biggest win).** 61% of email-less papers
  have an author email printed in their OWN md parse that the extractor missed.
  Mine header/affiliation region. **Author-email classifier required** — reject
  boilerplate (`reports@*.osti.gov`, `orders@*ntis*`, `webmaster@`, `info@`,
  generic `@ee.doe.gov` ordering). Prefer emails adjacent to the author/affiliation
  block; keep the corresponding-author one when flagged.
- **S-E2 contacts.db** — EXHAUSTED (only 2 of 49,358 remain). Skip.
- **S-E3 Cross-card transfer** — same author appears in another paper that DOES have
  their email → transfer the observed address (person-name keyed, not entity).
- **S-E4 Web (OpenAlex/Crossref/ORCID + targeted search)** — resolve corresponding
  author → verified email from an authoritative record. Cite source URL. Last resort.
- **NEVER** pattern-construct `first.last@lab.gov`. Verified-or-empty.

### Technical-info sources
- **S-T1 Re-parse-mining.** Many missing fields ARE in the parse but were dropped by
  the dual-source bug or a terse prompt. Re-extract the specific missing fields from
  the GOOD md (md-only), per mode, targeting the mode's top-3 gaps.
- **S-T2 Cross-card transfer (zero-hallucination).** 14,115 named entities appear in
  >1 card (covers 111,222 cards / 48%). Build a consensus record per entity
  (e.g. VASP: developer, license, language, URL) from cards that HAVE each field,
  propagate to sibling cards that lack it. Confidence = agreement across cards.
- **S-T3 Web search (cited).** For named tools/datasets/facilities, resolve stable
  facts (official URL, license, developer, location) from the canonical source and
  attach with `[enriched:web <url>]`. URL must be live-validated before attach.
- **S-T4 Parametric (tightest guard).** ONLY for stable, verifiable facts about
  well-known entities (e.g. "LAMMPS is C++", "APS is at Argonne") tagged
  `[enriched:parametric]`. NEVER for URLs or emails. NEVER for version/access.

## 3. Module build order
1. **M-E1 parse-mine emails** (author-email classifier) — recovers ~30k papers, zero cost.
2. **M-T2 cross-card transfer** (entity consensus) — recovers tech fields for ~111k cards.
3. **M-E3/M-T1 targeted re-extraction** on qwenfarm — fields still missing after 1+2.
4. **M-url validate** — classify + HTTP-verify URLs → sets `url_validated` for the HIGH gate.
5. **M-T3/M-E4 web enrichment** — cited, verified, last; most guarded.

## 4. Guardrails (every module)
- Bracketed provenance tag on every enriched value; original paper content untouched.
- Idempotent; re-runnable; writes to a staging column first, merged on review.
- Email + URL: **verified-or-empty**, never fabricated.
- Smoke-test before scale (standing rule). Run on qwenfarm, pull back to m1.
- New `qa_*` recompute after each pass; track HIGH% climb per module.

## 5. Success metric
HIGH% (new definition) climbs from today's ~8.6% toward and past 10%, with each
point traceable to a named module and a verifiable source — no point earned by
relabeling or by fabricated fields.
