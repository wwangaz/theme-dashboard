import json
import re
import anthropic
from db.models import RawSignal, Theme, SignalThemeMap
from db.session import get_session
from datetime import date

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a senior market analyst. Cluster market signals into coherent investment themes.
Identify 5-10 distinct themes. Be specific — avoid generic themes like "Tech" or "Market".
Output only valid JSON."""


def cluster_themes(signals: list[RawSignal], keyword_map: dict[int, list[str]]) -> list[dict]:
    if not signals:
        return []

    signals_json = [
        {
            "id": s.id,
            "source_type": s.source_type,
            "ticker": s.ticker,
            "headline": s.headline,
            "keywords": keyword_map.get(s.id, []),
        }
        for s in signals
    ]

    response = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{
            "role": "user",
            "content": f"""Cluster these market signals into 5-10 investment themes.

Return JSON array:
[{{
  "theme_id": "slug-name",
  "theme_name": "Human Readable Name",
  "one_line_summary": "What is happening in this theme today",
  "signal_ids": [list of signal IDs],
  "representative_tickers": [list of tickers, max 5]
}}]

Signals:
{json.dumps(signals_json, ensure_ascii=False)}"""
        }],
    )

    text = response.content[0].text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-z]*\n?", "", text)
        text = text.rstrip("`").strip()
    return json.loads(text)


def save_themes(clusters: list[dict], today: date):
    with get_session() as session:
        for cluster in clusters:
            theme_id = cluster["theme_id"]
            existing = session.get(Theme, theme_id)
            if existing:
                existing.last_active = today
                existing.representative_tickers_json = json.dumps(cluster.get("representative_tickers", []))
            else:
                session.add(Theme(
                    id=theme_id,
                    name=cluster["theme_name"],
                    first_seen=today,
                    last_active=today,
                    representative_tickers_json=json.dumps(cluster.get("representative_tickers", [])),
                ))

            for signal_id in cluster.get("signal_ids", []):
                existing_map = session.get(SignalThemeMap, (signal_id, theme_id))
                if not existing_map:
                    session.add(SignalThemeMap(signal_id=signal_id, theme_id=theme_id))

        session.commit()
    return clusters
