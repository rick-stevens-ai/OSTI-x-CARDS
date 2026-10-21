# SuperCards Strategy — Merging x-CARDs About the Same Resource

Status: DRAFT for Rick review. Date: 2026-10-17. Owner: Kukla.
Separate collection from the per-paper x-CARDs. A **SuperCard** is the consolidated,
canonical record of ONE real-world resource (a tool, dataset, instrument, facility,
model, workflow, or agent) synthesized from EVERY x-CARD that mentions it.

## 1. Why
A resource like **VASP** (2,815 cards), **Advanced Photon Source** (7,330 cards),
or **LAMMPS** (533 cards) is described hundreds–thousands of times across the corpus.
Each mention is partial. A SuperCard:
- gives agents ONE authoritative, maximally-complete record per resource,
- is the natural home for a **validated canonical URL / contact / license** gathered
  across all mentions (fills gaps no single card could),
- yields a **usage graph**: which papers/labs/authors used this resource, when, how.

## 2. Measured feasibility (live corpus)
- 140,364 distinct named entities; **14,115 appear in >1 card** (SuperCard-eligible).
- Those 14,115 entities cover **111,222 cards (48% of corpus)**.
- Heavy hitters are facilities (APS, ALS, NERSC, SNS, OLCF) and codes (VASP, LAMMPS) —
  exactly the high-value shared infrastructure.
- The long tail (125k single-mention entities) become **singleton SuperCards** (1:1),
  still useful as the canonical entry but with no cross-card consensus.

## 3. The hard problem: entity resolution (who is "the same resource"?)
Name-exact matching is the floor but insufficient — aliases, abbreviations, versions:
- "Advanced Photon Source" = "APS" = "APS at Argonne"
- "VASP" = "Vienna Ab initio Simulation Package" = "vasp 5.4"
- "NERSC" = "National Energy Research Scientific Computing Center" = "NERSC Cori/Perlmutter"

**Resolution pipeline (blocking → matching → clustering):**
1. **Normalize** name (lowercase, strip version/punctuation) → cheap blocking key.
2. **Alias table** — curated + auto (acronym↔expansion detected from co-occurrence in
   the same card, e.g. a card with both "Advanced Photon Source (APS)").
3. **LLM adjudication within a block** (NOT regex) — for candidate clusters that share
   a blocking key or alias, an LLM confirms "same resource? yes/no/variant" using the
   cards' type + developer + description. This is the judgment step; use qwenfarm.
4. **Version handling** — same base resource, different version → ONE SuperCard with a
   `versions:` list, not separate SuperCards (VASP 5.4 and 6.3 = one VASP SuperCard).
5. **Cluster → SuperCard id** = `super-<mode>-<slug>` (stable, idempotent).

## 4. SuperCard synthesis (field consolidation)
For each cluster, build the canonical record by **evidence-weighted consensus**, NOT
by concatenation:
- **Scalar fields** (developer, license, language, location): take the value with the
  most support across member cards; record agreement count + dissenting values.
- **URLs/emails**: union of all observed, each **validated**, canonical one promoted.
- **Descriptions**: LLM synthesis of the best short + full description from members
  (cited to the richest members; no invention).
- **Provenance block**: `member_cards: [osti_id...]`, `member_count`, `first_seen`,
  `last_seen`, `labs`, `authors` (the usage graph).
- Conflicts surfaced, never silently resolved (`license_candidates: [...]`).

## 5. Anti-hallucination (same law as enrichment)
- A SuperCard asserts only facts PRESENT in ≥1 member card or verified via live web.
- No fabricated URL/email/version. Conflicts preserved, not averaged away.
- Every synthesized field tagged with member-card support count.

## 6. Storage / access
- New table `supercards` + `supercard_members(super_id, osti_id)` in the same DB
  (or a sibling DB `supercards.sqlite` — decide w/ Rick; sibling keeps per-paper
  cards pristine and lets SuperCards rebuild independently).
- **Website: SuperCards are a FIRST-CLASS, SEPARATE CATEGORY** (Rick, 2026-10-17) —
  their own browse/filter/detail section distinct from per-paper x-cards. A SuperCard
  detail page shows the consensus record + the list of member papers (usage graph).
- MCP: `supercard_search` / `supercard_get` for model-based access.
- Feeds the knowledge graph: SuperCard = canonical node, member cards = edges.

## 7. Build order (proof-first)
1. **Blocking + exact-name clustering** on the 14,115 multi-mention entities (cheap,
   deterministic) → baseline SuperCards for the heavy hitters. PROVE on top-50.
2. **Alias/acronym expansion** → merge obvious variants.
3. **LLM adjudication** on ambiguous blocks (qwenfarm) → clean clusters.
4. **Consensus synthesis** → populate SuperCard fields.
5. **Website view + MCP tools.**
6. Backfill singleton SuperCards for the long tail.

## 8. Relationship to the Enrichment thread
SuperCards and enrichment are **mutually reinforcing**:
- Enrichment's cross-card transfer (M-T2) IS the per-entity consensus — SuperCards are
  its persisted, first-class output.
- A field validated once on a SuperCard can propagate to ALL member cards.
- Build the entity-resolution layer ONCE; both threads consume it.

## 9. Open decisions for Rick
1. Separate `supercards.sqlite` vs same DB? (lean: sibling DB.)
2. Should a SuperCard's validated URL/email auto-propagate back to member cards
   (raising their tier), or stay only on the SuperCard?
3. Version policy confirm: one SuperCard per resource with a versions list (not per version).
