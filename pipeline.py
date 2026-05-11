import argparse
import json
import os
from datetime import date, datetime, timezone, timedelta
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from sqlmodel import select
from db.session import init_db, get_session
from db.models import RawSignal, Theme, ThemeDailySnapshot
from analysis.translation import translate_themes
from ingestion.rss import ingest_rss
from ingestion.edgar import ingest_edgar
from ingestion.price import ingest_prices
from ingestion.transcripts import ingest_transcripts
from theme_engine.extractor import extract_keywords
from theme_engine.clusterer import cluster_themes, save_themes, match_existing_themes
from analysis.stage import classify_stage
from analysis.conviction import score_conviction
from analysis.compression import compress_research

OUTPUT_PATH = Path(__file__).parent / "docs" / "data" / "latest.json"


# ── Idempotency checks ────────────────────────────────────────────────────────

def _has_signals_today(today: date) -> bool:
    with get_session() as s:
        row = s.exec(select(RawSignal).where(RawSignal.signal_date == today).limit(1)).first()
        return row is not None


def _has_snapshots_today(today: date) -> bool:
    with get_session() as s:
        row = s.exec(select(ThemeDailySnapshot).where(ThemeDailySnapshot.snapshot_date == today).limit(1)).first()
        return row is not None


def _prev_conviction(theme_id: str, today: date) -> int | None:
    with get_session() as s:
        rows = s.exec(
            select(ThemeDailySnapshot)
            .where(ThemeDailySnapshot.theme_id == theme_id)
            .where(ThemeDailySnapshot.snapshot_date < today)
            .order_by(ThemeDailySnapshot.snapshot_date.desc())
            .limit(1)
        ).all()
        return rows[0].conviction_score if rows else None


# ── Phase 1: Ingestion ────────────────────────────────────────────────────────

def run_ingestion(today: date, force: bool = False):
    print("=== Phase 1: Ingestion ===")
    if not force and _has_signals_today(today):
        print("  [skip] signals already exist for today")
        return
    ingest_rss(today)
    ingest_edgar()
    ingest_prices()
    ingest_transcripts()


def load_today_signals(today: date) -> list[RawSignal]:
    with get_session() as s:
        return list(s.exec(select(RawSignal).where(RawSignal.signal_date == today)))


# ── Phase 2: Theme Engine ─────────────────────────────────────────────────────

def run_theme_engine(signals: list[RawSignal], today: date, force: bool = False) -> list[dict]:
    print("=== Phase 2: Theme Engine ===")

    # Check if themes were already clustered today
    with get_session() as s:
        existing_themes = list(s.exec(select(Theme).where(Theme.last_active == today)))
    if not force and existing_themes:
        print(f"  [skip] {len(existing_themes)} themes already clustered today")
        # Re-build cluster dicts from DB
        clusters = []
        for t in existing_themes:
            clusters.append({
                "theme_id": t.id,
                "theme_name": t.name,
                "one_line_summary": "",
                "signal_ids": [],
                "representative_tickers": json.loads(t.representative_tickers_json),
            })
        return clusters

    print(f"  Extracting keywords from {len(signals)} signals...")
    BATCH = 30
    keyword_map: dict[int, list[str]] = {}
    for i in range(0, len(signals), BATCH):
        batch = signals[i:i + BATCH]
        try:
            for r in extract_keywords(batch):
                keyword_map[r["id"]] = r.get("keywords", [])
        except Exception as e:
            print(f"  keyword batch {i // BATCH} failed: {e}")

    print("  Clustering into themes...")
    clusters = cluster_themes(signals, keyword_map)

    # Match against yesterday's themes to prevent ID drift
    clusters = match_existing_themes(clusters, today)

    save_themes(clusters, today)
    print(f"  Found {len(clusters)} themes")
    return clusters


# ── Phase 3: Analysis ─────────────────────────────────────────────────────────

def run_analysis(clusters: list[dict], all_signals: list[RawSignal], today: date, force: bool = False) -> list[dict]:
    print("=== Phase 3: Analysis ===")

    if not force and _has_snapshots_today(today):
        print("  [skip] snapshots already exist for today — loading from DB")
        return _load_snapshots_from_db(today)

    signal_by_id = {s.id: s for s in all_signals}
    snapshots = []

    for cluster in clusters:
        theme_id = cluster["theme_id"]
        theme_name = cluster["theme_name"]
        print(f"  Analyzing: {theme_name}")

        theme_signals = [
            {
                "id": sid,
                "headline": signal_by_id[sid].headline if sid in signal_by_id else "",
                "content": signal_by_id[sid].content[:300] if sid in signal_by_id else "",
                "source_type": signal_by_id[sid].source_type if sid in signal_by_id else "news",
                "source_name": signal_by_id[sid].source_name if sid in signal_by_id else "",
                "url": signal_by_id[sid].url if sid in signal_by_id else None,
                "ticker": signal_by_id[sid].ticker if sid in signal_by_id else None,
                "signal_date": str(signal_by_id[sid].signal_date) if sid in signal_by_id else str(today),
            }
            for sid in cluster.get("signal_ids", [])
            if sid in signal_by_id
        ]
        price_signals = [s for s in theme_signals if s["source_type"] == "price"]

        try:
            stage_r = classify_stage(theme_name, theme_signals, price_signals)
        except Exception as e:
            print(f"    stage failed: {e}")
            from analysis.schemas import StageResult
            stage_r = StageResult(stage="Early Discovery", stage_confidence=5)

        try:
            conv_r = score_conviction(theme_name, theme_signals)
        except Exception as e:
            print(f"    conviction failed: {e}")
            from analysis.schemas import ConvictionResult
            conv_r = ConvictionResult(direction="Neutral", conviction_score=5)

        try:
            comp_r = compress_research(theme_name, theme_signals, stage_r.stage, conv_r.direction)
        except Exception as e:
            print(f"    compression failed: {e}")
            from analysis.schemas import CompressionResult
            comp_r = CompressionResult()

        prev_score = _prev_conviction(theme_id, today)
        conviction_delta = (conv_r.conviction_score - prev_score) if prev_score is not None else None

        snapshot = ThemeDailySnapshot(
            theme_id=theme_id,
            snapshot_date=today,
            direction=conv_r.direction,
            conviction_score=conv_r.conviction_score,
            stage=stage_r.stage,
            stage_confidence=stage_r.stage_confidence,
            stage_reasoning=stage_r.stage_reasoning,
            bull_evidence_json=json.dumps([e.model_dump() for e in conv_r.bull_evidence]),
            bear_evidence_json=json.dumps([e.model_dump() for e in conv_r.bear_evidence]),
            conviction_basis=conv_r.conviction_basis,
            bull_case=comp_r.bull_case,
            bear_case=comp_r.bear_case,
            current_drivers=comp_r.current_drivers,
            key_risks_json=json.dumps(comp_r.key_risks),
            short_term_outlook=comp_r.short_term_outlook,
            mid_term_outlook=comp_r.mid_term_outlook,
            long_term_outlook=comp_r.long_term_outlook,
            signal_count=len(theme_signals),
            timeline_json=json.dumps([t.model_dump() for t in comp_r.timeline]),
        )
        with get_session() as session:
            session.add(snapshot)
            session.commit()

        snapshots.append(_snapshot_to_dict(snapshot, cluster, conviction_delta, today))

    return snapshots


def _snapshot_to_dict(snap: ThemeDailySnapshot, cluster: dict, delta: int | None, today: date) -> dict:
    with get_session() as s:
        theme = s.get(Theme, snap.theme_id)
        first_seen = str(theme.first_seen) if theme else str(today)
        tickers = json.loads(theme.representative_tickers_json) if theme else []

    return {
        "id": snap.theme_id,
        "name": cluster.get("theme_name", snap.theme_id),
        "one_line_summary": cluster.get("one_line_summary", ""),
        "representative_tickers": tickers,
        "first_seen": first_seen,
        "direction": snap.direction,
        "conviction_score": snap.conviction_score,
        "conviction_delta": delta,
        "stage": snap.stage,
        "stage_confidence": snap.stage_confidence,
        "stage_reasoning": snap.stage_reasoning,
        "conviction_basis": snap.conviction_basis,
        "bull_evidence": json.loads(snap.bull_evidence_json),
        "bear_evidence": json.loads(snap.bear_evidence_json),
        "bull_case": snap.bull_case,
        "bear_case": snap.bear_case,
        "current_drivers": snap.current_drivers,
        "key_risks": json.loads(snap.key_risks_json),
        "short_term_outlook": snap.short_term_outlook,
        "mid_term_outlook": snap.mid_term_outlook,
        "long_term_outlook": snap.long_term_outlook,
        "signal_count": snap.signal_count,
        "timeline": json.loads(snap.timeline_json),
    }


def _load_snapshots_from_db(today: date) -> list[dict]:
    with get_session() as s:
        snaps = list(s.exec(select(ThemeDailySnapshot).where(ThemeDailySnapshot.snapshot_date == today)))
    results = []
    for snap in snaps:
        prev = _prev_conviction(snap.theme_id, today)
        delta = (snap.conviction_score - prev) if prev is not None else None
        cluster = {"theme_name": snap.theme_id, "one_line_summary": "", "signal_ids": []}
        with get_session() as s:
            theme = s.get(Theme, snap.theme_id)
            if theme:
                cluster["theme_name"] = theme.name
        results.append(_snapshot_to_dict(snap, cluster, delta, today))
    return results


# ── Phase 4: Output ───────────────────────────────────────────────────────────

def generate_output(snapshots: list[dict], macro_context: dict | None = None):
    print("=== Phase 4: Generate Output ===")
    print("  Translating to Chinese...")
    snapshots = translate_themes(snapshots)

    beijing = timezone(timedelta(hours=8))
    now = datetime.now(beijing).isoformat()
    snapshots.sort(key=lambda x: x["conviction_score"], reverse=True)
    output = {
        "generated_at": now,
        "macro_context": macro_context or {},
        "themes": snapshots,
    }
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"  Written to {OUTPUT_PATH}")


# ── JSON checkpoint ───────────────────────────────────────────────────────────

def _json_state(today: date) -> str:
    """
    Returns the pipeline state based on latest.json:
      'done'             — generated today, _zh fields present
      'needs_translation'— generated today, _zh fields missing
      'needs_full_run'   — not today or empty
    """
    if not OUTPUT_PATH.exists():
        return "needs_full_run"
    try:
        data = json.loads(OUTPUT_PATH.read_text())
        generated_at = data.get("generated_at")
        if not generated_at:
            return "needs_full_run"
        gen_date = date.fromisoformat(generated_at[:10])
        if gen_date != today:
            return "needs_full_run"
        themes = data.get("themes", [])
        if not themes:
            return "needs_full_run"
        # All themes must have _zh fields — partial translation counts as needs_translation
        all_translated = all(t.get("name_zh") and t.get("bull_case_zh") for t in themes)
        any_translated = any(t.get("name_zh") or t.get("bull_case_zh") for t in themes)
        if all_translated:
            return "done"
        return "needs_translation" if any_translated or themes else "needs_full_run"
    except Exception:
        return "needs_full_run"


def _translate_from_json():
    """Translate latest.json in-place and refresh macro context — no DB needed."""
    print("=== Translation-only mode (loaded from latest.json) ===")
    data = json.loads(OUTPUT_PATH.read_text())
    snapshots = data.get("themes", [])
    snapshots = translate_themes(snapshots)
    data["themes"] = snapshots
    # Always refresh treasury yields — cheap yfinance call, no DB needed
    from ingestion.price import fetch_treasury_yields
    macro = fetch_treasury_yields()
    if macro:
        data["macro_context"] = macro
        print(f"  Macro context refreshed: 2Y={macro['yields'].get('2Y',0):.2%} 10Y={macro['yields'].get('10Y',0):.2%} risk={macro['risk_label']}")
    OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    print(f"  Written {len(snapshots)} themes → {OUTPUT_PATH}")


# ── Entrypoint ────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--render-only", action="store_true",
                        help="Skip ingestion & analysis, regenerate output from DB only")
    parser.add_argument("--force", action="store_true",
                        help="Re-run all phases even if today's data already exists")
    args = parser.parse_args()

    init_db()
    today = date.today()

    if args.render_only:
        print("=== Render-only mode ===")
        snapshots = _load_snapshots_from_db(today)
        macro_context = _load_macro_context()
        generate_output(snapshots, macro_context)
        return

    if not args.force:
        state = _json_state(today)
        if state == "done":
            print("=== Already complete for today — refreshing macro context only ===")
            _refresh_macro_in_json()
            return
        if state == "needs_translation":
            print("=== Analysis already done, translation missing — resuming from latest.json ===")
            _translate_from_json()
            _commit_json_if_changed()
            return

    run_ingestion(today, force=args.force)
    signals = load_today_signals(today)
    print(f"  Loaded {len(signals)} signals for today")

    if not signals:
        print("No signals today, skipping analysis.")
        return

    clusters = run_theme_engine(signals, today, force=args.force)
    snapshots = run_analysis(clusters, signals, today, force=args.force)
    macro_context = _load_macro_context()
    generate_output(snapshots, macro_context)
    print("=== Done ===")


def _commit_json_if_changed():
    """No-op in pipeline — the Actions workflow handles git commit. Placeholder for local use."""
    pass


def _refresh_macro_in_json():
    """Fetch treasury yields and patch macro_context in latest.json — no DB or API needed."""
    from ingestion.price import fetch_treasury_yields
    if not OUTPUT_PATH.exists():
        return
    try:
        data = json.loads(OUTPUT_PATH.read_text())
        macro = fetch_treasury_yields()
        if macro:
            data["macro_context"] = macro
            OUTPUT_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=2))
            print(f"  Macro context: 2Y={macro['yields'].get('2Y',0):.2%} 10Y={macro['yields'].get('10Y',0):.2%}  risk={macro['risk_label']}")
    except Exception as e:
        print(f"  Macro refresh failed: {e}")


def _load_macro_context() -> dict:
    # Populated by ingestion/price.py treasury fetch — returns {} if not available
    path = Path(__file__).parent / "data" / "macro_context.json"
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            pass
    return {}


if __name__ == "__main__":
    main()
