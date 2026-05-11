import json
import re
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a financial translator. Translate English investment research text into fluent, professional Chinese (简体中文).
Preserve proper nouns (company names, ticker symbols, technical terms) in English where appropriate.
Output only valid JSON with the same structure as input, all text values translated."""

NARRATIVE_FIELDS = [
    "name", "one_line_summary", "conviction_basis", "stage_reasoning",
    "bull_case", "bear_case", "current_drivers",
    "short_term_outlook", "mid_term_outlook", "long_term_outlook",
]


def _translate_one(snap: dict) -> dict:
    """Translate a single theme snapshot. Returns translation dict or {} on failure."""
    entry = {"id": snap["id"]}
    for f in NARRATIVE_FIELDS:
        if snap.get(f):
            entry[f] = snap[f]
    if snap.get("key_risks"):
        entry["key_risks"] = snap["key_risks"]
    entry["bull_evidence_headlines"] = [e.get("headline", "") for e in snap.get("bull_evidence", [])]
    entry["bear_evidence_headlines"] = [e.get("headline", "") for e in snap.get("bear_evidence", [])]
    entry["timeline_events"] = [t.get("event", "") for t in snap.get("timeline", [])]

    response = client.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=4096,
        system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": f"""Translate all text values to Chinese. Return JSON with same keys.
Keep company names, tickers, and numbers unchanged.

Input:
{json.dumps(entry, ensure_ascii=False)}"""}],
    )
    text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
    return json.loads(text)


def _apply_translation(snap: dict, tr: dict):
    for f in NARRATIVE_FIELDS:
        if tr.get(f):
            snap[f"{f}_zh"] = tr[f]
    if tr.get("key_risks"):
        snap["key_risks_zh"] = tr["key_risks"]

    bull_zh = tr.get("bull_evidence_headlines", [])
    for i, ev in enumerate(snap.get("bull_evidence", [])):
        ev["headline_zh"] = bull_zh[i] if i < len(bull_zh) else ev.get("headline", "")

    bear_zh = tr.get("bear_evidence_headlines", [])
    for i, ev in enumerate(snap.get("bear_evidence", [])):
        ev["headline_zh"] = bear_zh[i] if i < len(bear_zh) else ev.get("headline", "")

    tl_zh = tr.get("timeline_events", [])
    for i, tl in enumerate(snap.get("timeline", [])):
        tl["event_zh"] = tl_zh[i] if i < len(tl_zh) else tl.get("event", "")


def translate_themes(snapshots: list[dict]) -> list[dict]:
    for snap in snapshots:
        # Skip if already translated
        if snap.get("name_zh"):
            print(f"  [translation] {snap['id']} already translated, skipping")
            continue
        try:
            tr = _translate_one(snap)
            _apply_translation(snap, tr)
            print(f"  [translation] {snap.get('name', snap['id'])} ✓")
        except Exception as e:
            print(f"  [translation] {snap['id']} failed: {e} — keeping English")
    return snapshots
