# Individual-Card Review-Synthesis Prompt

Synthesizes a ~350-600 word review/tutorial article for one card from one paper. Model: qwen3.8-27b (farm), `enable_thinking=False`. Uses the same per-modality section templates as `review_supercard.md`.

## Prompt builder (verbatim from `pipeline/reviews/review_synth.py`)

```python
def card_prompt(c):
    mode=c["mode"]
    secs=SECTIONS.get(mode, SECTIONS["simulation"])
    desc=(c["description"] or "")[:1500]
    yaml=(c["yaml"] or "")[:3500]
    return f"""You are writing a REVIEW / TUTORIAL ARTICLE for a DOE research-resource knowledge base,
about the {NAME_KEY.get(mode,mode)} "{c['title']}" described in OSTI paper {c['osti_id']} ({c['year']}).

PAPER DESCRIPTION:
{desc}

EXTRACTED STRUCTURED CARD (source facts — do not contradict, but expand into prose):
{yaml}

Write a substantive review/tutorial article in Markdown with these sections:
{secs}

Rules:
- Flowing explanatory PROSE like a tutorial — NOT a bullet outline of fields, NOT sparse.
- Explain what the resource is, how it works, and how a researcher would actually use it.
- Do NOT invent URLs, versions, emails, or facts absent from the description/card. If unknown, say so.
- 350-600 words. Start directly with: "# {c['title']}"."""
```
