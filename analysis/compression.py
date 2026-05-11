import json
import re
import anthropic
from analysis.schemas import CompressionResult

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a senior equity research analyst. Write structured, evidence-based investment research summaries.
Be specific — cite actual company names, data points, or events from the signals. Avoid generic statements.
Output only valid JSON."""


def compress_research(theme_name: str, signals: list[dict], stage: str, direction: str) -> CompressionResult:
    prompt = f"""Write a research summary for investment theme: "{theme_name}"
Stage: {stage} | Current bias: {direction}

Source signals:
{json.dumps(signals[:30], ensure_ascii=False)}

Return JSON:
{{
  "bull_case": "2-3 sentences",
  "bear_case": "2-3 sentences",
  "current_drivers": "1-2 sentences on what is driving the theme right now",
  "key_risks": ["risk 1", "risk 2", "risk 3"],
  "short_term_outlook": "1-4 week view",
  "mid_term_outlook": "1-6 month view",
  "long_term_outlook": "1-3 year structural view",
  "timeline": [{{"date": "YYYY-MM-DD", "event": "brief description"}}]
}}
timeline: extract 4-6 key recent events with dates from the signals."""

    for attempt in range(2):
        response = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=2048,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": prompt}],
        )
        try:
            text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
            return CompressionResult.model_validate_json(text)
        except Exception as e:
            if attempt == 1:
                raise ValueError(f"compression parse failed after 2 attempts: {e}")

    return CompressionResult()
