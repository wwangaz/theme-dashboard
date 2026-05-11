import json
import re
import anthropic
from analysis.schemas import StageResult

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a market cycle analyst. Classify the current stage of an investment theme.

Stages (pick exactly one):
- Early Discovery: Few mentions, limited price action, mostly specialist coverage
- Acceleration: Growing mentions, initial price breakouts, mainstream media starting coverage
- Momentum Expansion: Broad coverage, strong price action, institutional involvement visible
- Crowded Euphoria: Peak coverage, stretched valuations, retail FOMO, analyst upgrades peak
- Narrative Breakdown: Negative catalysts, coverage declining, price weakness, thesis challenged

Output only valid JSON."""


def _parse(text: str) -> StageResult:
    text = re.sub(r"^```[a-z]*\n?", "", text.strip()).rstrip("`").strip()
    return StageResult.model_validate_json(text)


def classify_stage(theme_name: str, signals: list[dict], price_signals: list[dict]) -> StageResult:
    prompt = f"""Classify the market cycle stage for theme: "{theme_name}"

Quantitative context:
- Signal count: {len(signals)}
- Price signals: {len(price_signals)}
{_price_summary(price_signals)}

Recent signals (last 30):
{json.dumps(signals[:30], ensure_ascii=False)}

Return JSON: {{"stage": "...", "stage_confidence": 1-10, "stage_reasoning": "..."}}"""

    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=512,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            return _parse(response.content[0].text)
        except Exception as e:
            if attempt == 1:
                raise ValueError(f"stage parse failed after 2 attempts: {e}")

    return StageResult(stage="Early Discovery", stage_confidence=5)


def _price_summary(price_signals: list[dict]) -> str:
    if not price_signals:
        return ""
    lines = [f"- {s['headline']}" for s in price_signals[:5]]
    return "Price action:\n" + "\n".join(lines)
