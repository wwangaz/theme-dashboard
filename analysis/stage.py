import json
import re
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a market cycle analyst. Classify the current stage of an investment theme.

Stages:
- Early Discovery: Few mentions, limited price action, mostly specialist coverage
- Acceleration: Growing mentions, initial price breakouts, mainstream media starting coverage
- Momentum Expansion: Broad coverage, strong price action, institutional involvement visible
- Crowded Euphoria: Peak coverage, stretched valuations, retail FOMO, analyst upgrades peak
- Narrative Breakdown: Negative catalysts, coverage declining, price weakness, thesis challenged

Output only valid JSON."""


def classify_stage(theme_name: str, signals: list[dict], price_signals: list[dict]) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=1024,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": f"""Classify the market cycle stage for theme: "{theme_name}"

Recent signals (last 30 days):
{json.dumps(signals[:30], ensure_ascii=False)}

Price action:
{json.dumps(price_signals[:10], ensure_ascii=False)}

Return JSON:
{{
  "stage": "<one of the 5 stages>",
  "stage_confidence": <1-10>,
  "stage_reasoning": "<1-2 sentence explanation>"
}}"""
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = text.rstrip("`").strip()
    return json.loads(text)
