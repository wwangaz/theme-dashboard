import json
import os
from datetime import date
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from db.session import init_db, get_session
from db.models import RawSignal, Theme, ThemeDailySnapshot
from ingestion.rss import ingest_rss
from ingestion.edgar import ingest_edgar
from ingestion.price import ingest_prices
from ingestion.transcripts import ingest_transcripts
from theme_engine.extractor import extract_keywords
from theme_engine.clusterer import cluster_themes, save_themes
from analysis.stage import classify_stage
from analysis.conviction import score_conviction
from analysis.compression import compress_research

OUTPUT_PATH = Path(__file__).parent / "docs" / "data" / "latest.json"


def run_ingestion():
    print("=== Phase 1: Ingestion ===")
    ingest_rss()
    ingest_edgar()
    ingest_prices()
    ingest_transcripts()


def load_today_signals(session) -> list[RawSignal]:
    today = date.today()
    return [s for s in session.exec(
        __import__("sqlmodel").select(RawSignal).where(RawSignal.signal_date == today)
    )]


def run_theme_engine(signals: list[RawSignal]) -> list[dict]:
    print("=== Phase 2: Theme Engine ===")
    print(f"  Extracting keywords from {len(signals)} signals...")

    BATCH = 30
    keyword_map: dict[int, list[str]] = {}
    for i in range(0, len(signals), BATCH):
        batch = signals[i:i + BATCH]
        try:
            results = extract_keywords(batch)
            for r in results:
                keyword_map[r["id"]] = r.get("keywords", [])
        except Exception as e:
            print(f"  keyword extraction batch {i//BATCH} failed: {e}")

    print("  Clustering into themes...")
    clusters = cluster_themes(signals, keyword_map)
    save_themes(clusters, date.today())
    print(f"  Found {len(clusters)} themes")
    return clusters


def run_analysis(clusters: list[dict], all_signals: list[RawSignal]) -> list[dict]:
    print("=== Phase 3: Analysis ===")
    today = date.today()
    snapshots = []

    signal_by_id = {s.id: s for s in all_signals}

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
            stage_result = classify_stage(theme_name, theme_signals, price_signals)
        except Exception as e:
            print(f"    stage failed: {e}")
            stage_result = {"stage": "Early Discovery", "stage_confidence": 5, "stage_reasoning": ""}

        try:
            conviction_result = score_conviction(theme_name, theme_signals)
        except Exception as e:
            print(f"    conviction failed: {e}")
            conviction_result = {"direction": "Neutral", "conviction_score": 5, "bull_evidence": [], "bear_evidence": [], "conviction_basis": ""}

        try:
            compression_result = compress_research(
                theme_name, theme_signals,
                stage_result["stage"], conviction_result["direction"]
            )
        except Exception as e:
            print(f"    compression failed: {e}")
            compression_result = {
                "bull_case": "", "bear_case": "", "current_drivers": "",
                "key_risks": [], "short_term_outlook": "", "mid_term_outlook": "",
                "long_term_outlook": "", "timeline": []
            }

        snapshot = ThemeDailySnapshot(
            theme_id=theme_id,
            snapshot_date=today,
            direction=conviction_result.get("direction", "Neutral"),
            conviction_score=conviction_result.get("conviction_score", 5),
            stage=stage_result.get("stage", "Early Discovery"),
            stage_confidence=stage_result.get("stage_confidence", 5),
            stage_reasoning=stage_result.get("stage_reasoning", ""),
            bull_evidence_json=json.dumps(conviction_result.get("bull_evidence", [])),
            bear_evidence_json=json.dumps(conviction_result.get("bear_evidence", [])),
            conviction_basis=conviction_result.get("conviction_basis", ""),
            bull_case=compression_result.get("bull_case", ""),
            bear_case=compression_result.get("bear_case", ""),
            current_drivers=compression_result.get("current_drivers", ""),
            key_risks_json=json.dumps(compression_result.get("key_risks", [])),
            short_term_outlook=compression_result.get("short_term_outlook", ""),
            mid_term_outlook=compression_result.get("mid_term_outlook", ""),
            long_term_outlook=compression_result.get("long_term_outlook", ""),
            signal_count=len(theme_signals),
            timeline_json=json.dumps(compression_result.get("timeline", [])),
        )

        with get_session() as session:
            session.add(snapshot)
            session.commit()

        snapshots.append({
            "id": theme_id,
            "name": theme_name,
            "one_line_summary": cluster.get("one_line_summary", ""),
            "representative_tickers": cluster.get("representative_tickers", []),
            "first_seen": None,
            "direction": conviction_result.get("direction", "Neutral"),
            "conviction_score": conviction_result.get("conviction_score", 5),
            "stage": stage_result.get("stage", "Early Discovery"),
            "stage_confidence": stage_result.get("stage_confidence", 5),
            "stage_reasoning": stage_result.get("stage_reasoning", ""),
            "conviction_basis": conviction_result.get("conviction_basis", ""),
            "bull_evidence": conviction_result.get("bull_evidence", []),
            "bear_evidence": conviction_result.get("bear_evidence", []),
            "bull_case": compression_result.get("bull_case", ""),
            "bear_case": compression_result.get("bear_case", ""),
            "current_drivers": compression_result.get("current_drivers", ""),
            "key_risks": compression_result.get("key_risks", []),
            "short_term_outlook": compression_result.get("short_term_outlook", ""),
            "mid_term_outlook": compression_result.get("mid_term_outlook", ""),
            "long_term_outlook": compression_result.get("long_term_outlook", ""),
            "signal_count": len(theme_signals),
            "timeline": compression_result.get("timeline", []),
        })

    return snapshots


def generate_output(snapshots: list[dict]):
    print("=== Phase 4: Generate Output ===")
    from datetime import timezone, timedelta
    beijing = timezone(timedelta(hours=8))
    now = __import__("datetime").datetime.now(beijing).isoformat()

    # Enrich with first_seen from DB
    with get_session() as session:
        for snap in snapshots:
            theme = session.get(Theme, snap["id"])
            if theme:
                snap["first_seen"] = str(theme.first_seen)

    # Sort by conviction_score descending
    snapshots.sort(key=lambda x: x["conviction_score"], reverse=True)

    output = {"generated_at": now, "themes": snapshots}
    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(json.dumps(output, ensure_ascii=False, indent=2))
    print(f"  Written to {OUTPUT_PATH}")


def main():
    init_db()
    run_ingestion()

    with get_session() as session:
        signals = load_today_signals(session)

    print(f"  Loaded {len(signals)} signals for today")

    if not signals:
        print("No signals today, skipping analysis.")
        return

    clusters = run_theme_engine(signals)
    snapshots = run_analysis(clusters, signals)
    generate_output(snapshots)
    print("=== Done ===")


if __name__ == "__main__":
    main()
