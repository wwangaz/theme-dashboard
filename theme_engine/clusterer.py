import json
import re
from datetime import date, timedelta
import anthropic
from sqlmodel import select
from db.models import RawSignal, Theme, SignalThemeMap
from db.session import get_session

client = anthropic.Anthropic()

CLUSTER_SYSTEM = """You are a senior market analyst. Cluster market signals into coherent investment themes.
Identify 5-10 distinct themes. Be specific — avoid generic labels like "Tech" or "Market".
Output only valid JSON."""

MATCH_SYSTEM = """You are a market analyst. Match today's new themes against existing historical themes.
If a new theme is semantically equivalent to an existing theme (>80% overlap), return the existing theme's ID.
Otherwise return null to create a new theme. Output only valid JSON."""


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
        system=[{"type": "text", "text": CLUSTER_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"""Cluster these market signals into 5-10 investment themes.

Return JSON array:
[{{
  "theme_id": "slug-name",
  "theme_name": "Human Readable Name",
  "one_line_summary": "What is happening in this theme today",
  "signal_ids": [list of signal IDs],
  "representative_tickers": [list of tickers, max 5]
}}]

Signals:
{json.dumps(signals_json, ensure_ascii=False)}"""}],
    )

    text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
    return json.loads(text)


def match_existing_themes(clusters: list[dict], today: date) -> list[dict]:
    cutoff = today - timedelta(days=7)
    with get_session() as s:
        recent_themes = list(s.exec(
            select(Theme).where(Theme.last_active >= cutoff, Theme.is_active == True)
        ))

    if not recent_themes:
        return clusters

    existing = [{"id": t.id, "name": t.name} for t in recent_themes]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{"type": "text", "text": MATCH_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"""Match each new theme to an existing theme if semantically equivalent (>80% overlap).

Existing themes:
{json.dumps(existing, ensure_ascii=False)}

New themes:
{json.dumps([{{"theme_id": c["theme_id"], "theme_name": c["theme_name"]}} for c in clusters], ensure_ascii=False)}

Return JSON array — one entry per new theme:
[{{"new_id": "...", "matched_existing_id": "existing-id-or-null"}}]"""}],
    )

    try:
        text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
        matches = {m["new_id"]: m["matched_existing_id"] for m in json.loads(text)}
        for cluster in clusters:
            existing_id = matches.get(cluster["theme_id"])
            if existing_id:
                existing_theme = next((t for t in recent_themes if t.id == existing_id), None)
                if existing_theme:
                    cluster["theme_id"] = existing_id
                    cluster["theme_name"] = existing_theme.name
    except Exception as e:
        print(f"  [clusterer] theme matching failed: {e}")

    return clusters


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
                if not session.get(SignalThemeMap, (signal_id, theme_id)):
                    session.add(SignalThemeMap(signal_id=signal_id, theme_id=theme_id))

        session.commit()
