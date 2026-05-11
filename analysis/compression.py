import json
import re
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a senior equity research analyst. Write structured investment research summaries.
Be specific and evidence-based. Avoid generic statements. Output only valid JSON."""


def compress_research(theme_name: str, signals: list[dict], stage: str, direction: str) -> dict:
    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": f"""Write a research summary for investment theme: "{theme_name}"
Stage: {stage} | Current bias: {direction}

Source signals:
{json.dumps(signals[:30], ensure_ascii=False)}

Return JSON:
{{
  "bull_case": "<2-3 sentences>",
  "bear_case": "<2-3 sentences>",
  "current_drivers": "<1-2 sentences on what is driving the theme right now>",
  "key_risks": ["risk 1", "risk 2", "risk 3"],
  "short_term_outlook": "<1-4 week view, 1-2 sentences>",
  "mid_term_outlook": "<1-6 month view, 1-2 sentences>",
  "long_term_outlook": "<1-3 year structural view, 1-2 sentences>",
  "timeline": [
    {{"date": "YYYY-MM-DD", "event": "brief description"}}
  ]
}}

timeline: extract 4-6 key recent events from the signals with their dates."""
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = text.rstrip("`").strip()
    return json.loads(text)
