# SuperCard Review-Synthesis Prompt

Synthesizes a ~600-900 word review/tutorial article for a SuperCard, grounded in consensus facts + member-paper evidence. Model: qwen3.8-27b (farm), `enable_thinking=False`.

## Per-modality section templates

```python
SECTIONS = {
 "data":"## Overview\n## What the Dataset Contains\n## How It Is Used in Research\n## Access & Format\n## How to Work With It (tutorial)\n## Provenance & Citation",
 "model":"## Overview\n## Architecture & Method\n## What It Predicts / Does\n## How It Is Used in This Corpus\n## How to Use It (tutorial)\n## Access & Licensing\n## Representative Works",
 "agent":"## Overview\n## What the Agent Does\n## Underlying Models & Tools\n## How It Is Used\n## How to Run It (tutorial)\n## Access & Licensing\n## Representative Works",
 "instrument":"## Overview\n## What It Measures & How\n## Specifications & Capabilities\n## How It Is Used in This Corpus\n## Typical Measurement Workflow (tutorial)\n## Access & Operation\n## Representative Works",
 "facility":"## Overview\n## Capabilities & Instruments\n## Science Enabled (synthesize across the corpus)\n## How Researchers Use It\n## How to Get Access (tutorial)\n## Operation & Stewardship\n## Representative Works",
 "simulation":"## Overview\n## Method & Physics\n## How It Is Used in This Corpus\n## Capabilities & Numerics\n## Typical Workflow (tutorial)\n## Access & Licensing\n## Representative Works",
 "workflow":"## Overview\n## What the Workflow Does\n## Steps & Data Flow\n## How It Is Used\n## How to Run It (tutorial)\n## Access & Licensing\n## Representative Works",
}
```

## Prompt builder (verbatim from `pipeline/reviews/review_synth.py`)

```python
def super_prompt(sc, evidence):
    mode=sc["mode"]; cons=json.loads(sc.get("consensus_json") or "{}")
    cons_text="; ".join(f"{k}={v.get('value')} (agreed by {v.get('support')})"
                        for k,v in cons.items() if isinstance(v,dict) and v.get('value'))
    secs=SECTIONS.get(mode, SECTIONS["simulation"])
    return f"""You are writing a REVIEW / TUTORIAL ARTICLE for a DOE research-resource knowledge base.
This is a SuperCard: it merges {sc['member_count']} papers (years {sc.get('first_year')}-{sc.get('last_year')}) that all use the same {NAME_KEY.get(mode,mode)} "{sc['canonical_name']}".

GROUND-TRUTH CONSENSUS (facts agreed across member papers): {cons_text or '(none extracted)'}

EVIDENCE — excerpts from member papers (cite as [OSTI:id] when you draw on one):
{evidence}

Write a substantive review article in Markdown with exactly these sections:
{secs}

Rules:
- Write flowing explanatory PROSE like a review/tutorial article — NOT bullet outlines of fields, NOT a sparse list.
- Synthesize the DOMINANT PATTERNS across the member papers; ground claims in the evidence with inline [OSTI:id] citations.
- Do NOT invent URLs, versions, emails, or facts not present in the evidence or consensus. If unknown, say so plainly.
- Be concrete and useful to a researcher. 600-900 words. Start directly with the # title line: "# {sc['canonical_name']}"."""
```
