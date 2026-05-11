import json
import anthropic
from db.models import RawSignal

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a financial market analyst. Extract 3-5 concise market-relevant keywords from each signal.
Focus on: sector themes, macro trends, company actions, technology shifts.
Respond only with valid JSON."""


def extract_keywords(signals: list[RawSignal]) -> list[dict]:
    if not signals:
        return []

    batch_input = [
        {"id": s.id, "headline": s.headline, "content": s.content[:500]}
        for s in signals
    ]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=2048,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": f"""Extract 3-5 market keywords for each signal. Return JSON array:
[{{"id": <signal_id>, "keywords": ["keyword1", "keyword2", ...]}}]

Signals:
{json.dumps(batch_input, ensure_ascii=False)}"""
        }],
    )

    text = response.content[0].text.strip()
    # Strip markdown code fences if present
    if text.startswith("```"):
        text = text.split("```")[1]
        if text.startswith("json"):
            text = text[4:]
    return json.loads(text)
