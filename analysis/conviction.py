import json
import math
import re
from datetime import date
import anthropic
from analysis.schemas import ConvictionResult

client = anthropic.Anthropic()

SOURCE_WEIGHTS = {
    "transcript": 1.0,
    "filing": 0.9,
    "news": 0.6,
    "price": 0.5,
}

SYSTEM_PROMPT = """You are a buy-side research analyst. Assess conviction and directional bias for an investment theme.
Weight evidence by source quality: earnings transcripts > SEC filings > mainstream media > price action.
Recent signals (last 7 days) carry more weight than older ones.
Output only valid JSON."""


def _time_decay(signal_date_str: str, today: date) -> float:
    try:
        d = date.fromisoformat(signal_date_str)
        days_old = max(0, (today - d).days)
        return math.exp(-0.1 * days_old)
    except Exception:
        return 1.0


def score_conviction(theme_name: str, signals: list[dict]) -> ConvictionResult:
    today = date.today()
    weighted = [
        {
            **s,
            "weight": round(
                SOURCE_WEIGHTS.get(s.get("source_type", "news"), 0.4)
                * _time_decay(s.get("signal_date", str(today)), today),
                3,
            ),
        }
        for s in signals
    ]
    weighted.sort(key=lambda x: x["weight"], reverse=True)

    prompt = f"""Assess conviction for theme: "{theme_name}"

Signals (sorted by weight, higher = more credible & recent):
{json.dumps(weighted[:40], ensure_ascii=False)}

Return JSON:
{{
  "direction": "Bullish|Bearish|Neutral",
  "conviction_score": 1-10,
  "bull_evidence": [{{"headline": "...", "source": "...", "url": "..."}}],
  "bear_evidence": [{{"headline": "...", "source": "...", "url": "..."}}],
  "conviction_basis": "1-2 sentence explanation"
}}
Pick the 3 strongest signals for each evidence list."""

    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=1024,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
            return ConvictionResult.model_validate_json(text)
        except Exception as e:
            if attempt == 1:
                raise ValueError(f"conviction parse failed after 2 attempts: {e}")

    return ConvictionResult(direction="Neutral", conviction_score=5)
