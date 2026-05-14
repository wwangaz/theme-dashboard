import json
import re
from datetime import date, timedelta
from pathlib import Path
import anthropic
from sqlmodel import select
from db.models import RawSignal, Theme, SignalThemeMap
from db.session import get_session

client = anthropic.Anthropic()

THEMES_INDEX_PATH = Path(__file__).parent.parent / "docs" / "data" / "themes_index.json"

CLUSTER_SYSTEM = """You are a senior market analyst. Cluster market signals into coherent investment themes.

Rules:
1. Identify 5-10 themes. Be specific — avoid generic labels like "Tech" or "Market".
2. Each ticker should appear in at most one theme. If a stock drives multiple narratives, assign it to the most thematically dominant one.
3. Consolidate signals that share the same underlying macro driver into a single theme, even if they manifest through different angles (e.g., AI hardware capex + AI chip policy = one AI theme, not two). Themes are distinct only when their representative tickers AND investment implications differ meaningfully.
4. Output only valid JSON."""

MATCH_SYSTEM = """You are a market analyst. Match today's new themes against existing historical themes.
If a new theme is semantically equivalent to an existing theme (>80% overlap), return the existing theme's ID.
Otherwise return null to create a new theme. Output only valid JSON."""


def _load_themes_index() -> dict:
    if THEMES_INDEX_PATH.exists():
        try:
            return json.loads(THEMES_INDEX_PATH.read_text())
        except Exception:
            pass
    return {}


def _save_themes_index(index: dict):
    THEMES_INDEX_PATH.parent.mkdir(parents=True, exist_ok=True)
    THEMES_INDEX_PATH.write_text(json.dumps(index, ensure_ascii=False, indent=2))


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

Important: themes must be distinct in their investable tickers. If two candidate themes would share more than 2 tickers, merge them into one broader theme.

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
    # Load from persisted index (survives across CI runs) and supplement with DB
    index = _load_themes_index()
    cutoff = today - timedelta(days=30)

    # Build candidate list: index entries active within 30 days
    existing = [
        {"id": tid, "name": meta["name"]}
        for tid, meta in index.items()
        if meta.get("last_active", "") >= str(cutoff)
    ]

    # Also pull from DB in case of local runs with DB populated
    if not existing:
        with get_session() as s:
            recent_themes = list(s.exec(
                select(Theme).where(Theme.last_active >= cutoff, Theme.is_active == True)
            ))
        existing = [{"id": t.id, "name": t.name} for t in recent_themes]

    if not existing:
        return clusters

    new_themes_payload = json.dumps(
        [{"theme_id": str(c["theme_id"]), "theme_name": c["theme_name"]} for c in clusters],
        ensure_ascii=False,
    )
    existing_payload = json.dumps(existing, ensure_ascii=False)

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1024,
        system=[{"type": "text", "text": MATCH_SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": (
            "Match each new theme to an existing theme if semantically equivalent (>80% overlap).\n\n"
            f"Existing themes:\n{existing_payload}\n\n"
            f"New themes:\n{new_themes_payload}\n\n"
            'Return JSON array — one entry per new theme:\n'
            '[{"new_id": "...", "matched_existing_id": "existing-id-or-null"}]'
        )}],
    )

    try:
        text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
        matches = {m["new_id"]: m["matched_existing_id"] for m in json.loads(text)}
        for cluster in clusters:
            existing_id = matches.get(cluster["theme_id"])
            if existing_id and existing_id in index:
                cluster["theme_id"] = existing_id
                cluster["theme_name"] = index[existing_id]["name"]
    except Exception as e:
        print(f"  [clusterer] theme matching failed: {e}")

    return clusters


def save_themes(clusters: list[dict], today: date):
    today_str = str(today)
    index = _load_themes_index()

    with get_session() as session:
        for cluster in clusters:
            theme_id = cluster["theme_id"]

            # Update persisted index
            if theme_id in index:
                index[theme_id]["last_active"] = today_str
                index[theme_id]["representative_tickers"] = cluster.get("representative_tickers", [])
            else:
                index[theme_id] = {
                    "name": cluster["theme_name"],
                    "first_seen": today_str,
                    "last_active": today_str,
                    "representative_tickers": cluster.get("representative_tickers", []),
                }

            # Update DB (for local runs)
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

    _save_themes_index(index)
