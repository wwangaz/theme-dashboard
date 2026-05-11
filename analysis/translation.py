import json
import re
import anthropic

client = anthropic.Anthropic()

SYSTEM_PROMPT = """You are a financial translator. Translate English investment research text into fluent, professional Chinese (简体中文).
Preserve proper nouns (company names, ticker symbols, technical terms) in English where appropriate.
Output only valid JSON with the same structure as input, all text values translated."""

# Fields to translate per theme snapshot
NARRATIVE_FIELDS = [
    "one_line_summary", "conviction_basis", "stage_reasoning",
    "bull_case", "bear_case", "current_drivers",
    "short_term_outlook", "mid_term_outlook", "long_term_outlook",
]


def translate_themes(snapshots: list[dict]) -> list[dict]:
    if not snapshots:
        return snapshots

    # Build compact payload — only narrative fields + theme id
    payload = []
    for s in snapshots:
        entry = {"id": s["id"]}
        for f in NARRATIVE_FIELDS:
            if s.get(f):
                entry[f] = s[f]
        # key_risks is a list of strings
        if s.get("key_risks"):
            entry["key_risks"] = s["key_risks"]
        # evidence headlines
        entry["bull_evidence_headlines"] = [e.get("headline", "") for e in s.get("bull_evidence", [])]
        entry["bear_evidence_headlines"] = [e.get("headline", "") for e in s.get("bear_evidence", [])]
        # timeline events
        entry["timeline_events"] = [t.get("event", "") for t in s.get("timeline", [])]
        payload.append(entry)

    try:
        response = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=8192,
            system=[{"type": "text", "text": SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
            messages=[{"role": "user", "content": f"""Translate each theme's narrative fields to Chinese.
Return a JSON array with same structure (same keys), all text values in Chinese.
Keep company names, ticker symbols, and numeric data in their original form.

Input:
{json.dumps(payload, ensure_ascii=False)}"""}],
        )

        text = re.sub(r"^```[a-z]*\n?", "", response.content[0].text.strip()).rstrip("`").strip()
        translations: list[dict] = json.loads(text)
        trans_by_id = {t["id"]: t for t in translations}

        for snap in snapshots:
            tr = trans_by_id.get(snap["id"], {})

            # Scalar fields
            for f in NARRATIVE_FIELDS:
                if tr.get(f):
                    snap[f"{f}_zh"] = tr[f]

            # key_risks list
            if tr.get("key_risks"):
                snap["key_risks_zh"] = tr["key_risks"]

            # evidence headlines — patch back into evidence list
            bull_zh = tr.get("bull_evidence_headlines", [])
            for i, ev in enumerate(snap.get("bull_evidence", [])):
                ev["headline_zh"] = bull_zh[i] if i < len(bull_zh) else ev.get("headline", "")

            bear_zh = tr.get("bear_evidence_headlines", [])
            for i, ev in enumerate(snap.get("bear_evidence", [])):
                ev["headline_zh"] = bear_zh[i] if i < len(bear_zh) else ev.get("headline", "")

            # timeline events
            tl_zh = tr.get("timeline_events", [])
            for i, tl in enumerate(snap.get("timeline", [])):
                tl["event_zh"] = tl_zh[i] if i < len(tl_zh) else tl.get("event", "")

    except Exception as e:
        print(f"[translation] failed: {e} — skipping, English-only output")

    return snapshots
