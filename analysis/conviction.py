import json
import re
import anthropic

client = anthropic.Anthropic()

SOURCE_WEIGHTS = {
    "transcript": 1.0,
    "filing": 0.9,
    "news": 0.6,
    "price": 0.5,
}

SYSTEM_PROMPT = """You are a buy-side research analyst. Assess the conviction and directional bias of an investment theme.
Weigh evidence by source quality: earnings transcripts > SEC filings > mainstream media > price action.
Output only valid JSON."""


def score_conviction(theme_name: str, signals: list[dict]) -> dict:
    weighted_signals = [
        {**s, "weight": SOURCE_WEIGHTS.get(s.get("source_type", "news"), 0.4)}
        for s in signals
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": f"""Assess conviction for theme: "{theme_name}"

Signals (with source weights):
{json.dumps(weighted_signals[:40], ensure_ascii=False)}

Return JSON:
{{
  "direction": "<Bullish|Bearish|Neutral>",
  "conviction_score": <1-10>,
  "bull_evidence": [
    {{"headline": "...", "source": "...", "url": "..."}}
  ],
  "bear_evidence": [
    {{"headline": "...", "source": "...", "url": "..."}}
  ],
  "conviction_basis": "<1-2 sentence explanation of the score>"
}}

bull_evidence and bear_evidence: pick the 3 strongest signals for each side."""
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = text.rstrip("`").strip()
    return json.loads(text)
