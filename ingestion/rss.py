import hashlib
from datetime import date, datetime, timezone
import feedparser
from sqlmodel import select
from db.models import RawSignal
from db.session import get_session

RSS_FEEDS = [
    ("Reuters Finance", "https://feeds.reuters.com/reuters/businessNews"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
    ("Seeking Alpha", "https://seekingalpha.com/feed.xml"),
    ("Barron's", "https://www.barrons.com/xml/rss/3_7014.xml"),
    ("MarketWatch", "https://feeds.marketwatch.com/marketwatch/topstories/"),
]


def _parse_date(entry) -> date:
    for attr in ("published_parsed", "updated_parsed"):
        t = getattr(entry, attr, None)
        if t:
            return datetime(*t[:6], tzinfo=timezone.utc).date()
    return date.today()


def _dedup_key(url: str | None, headline: str) -> str:
    raw = (url or headline).strip().lower()
    return hashlib.md5(raw.encode()).hexdigest()


def fetch_rss(lookback_days: int = 1) -> list[RawSignal]:
    signals: list[RawSignal] = []
    seen: set[str] = set()

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                entry_date = _parse_date(entry)
                if (date.today() - entry_date).days > lookback_days:
                    continue
                headline = entry.get("title", "")[:500]
                entry_url = entry.get("link")
                key = _dedup_key(entry_url, headline)
                if key in seen:
                    continue
                seen.add(key)
                content = getattr(entry, "summary", "") or getattr(entry, "description", "")
                signals.append(RawSignal(
                    source_type="news",
                    source_name=source_name,
                    ticker=None,
                    headline=headline,
                    content=content[:2000],
                    signal_date=entry_date,
                    url=entry_url,
                ))
        except Exception as e:
            print(f"[rss] {source_name} failed: {e}")

    return signals


def ingest_rss(today: date | None = None) -> int:
    today = today or date.today()
    with get_session() as session:
        existing_urls = {
            r.url for r in session.exec(
                select(RawSignal).where(
                    RawSignal.signal_date == today,
                    RawSignal.source_type == "news",
                )
            )
        }

    signals = fetch_rss()
    new_signals = [s for s in signals if s.url not in existing_urls]

    with get_session() as session:
        for s in new_signals:
            session.add(s)
        session.commit()
    print(f"[rss] ingested {len(new_signals)} new signals ({len(signals) - len(new_signals)} deduped)")
    return len(new_signals)
