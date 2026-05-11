from datetime import date, datetime, timezone
import feedparser
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


def fetch_rss(lookback_days: int = 1) -> list[RawSignal]:
    cutoff = date.today()
    signals: list[RawSignal] = []

    for source_name, url in RSS_FEEDS:
        try:
            feed = feedparser.parse(url)
            for entry in feed.entries:
                entry_date = _parse_date(entry)
                if (date.today() - entry_date).days > lookback_days:
                    continue
                content = getattr(entry, "summary", "") or getattr(entry, "description", "")
                signals.append(RawSignal(
                    source_type="news",
                    source_name=source_name,
                    ticker=None,
                    headline=entry.get("title", "")[:500],
                    content=content[:2000],
                    signal_date=entry_date,
                    url=entry.get("link"),
                ))
        except Exception as e:
            print(f"[rss] {source_name} failed: {e}")

    return signals


def ingest_rss():
    signals = fetch_rss()
    with get_session() as session:
        for s in signals:
            session.add(s)
        session.commit()
    print(f"[rss] ingested {len(signals)} signals")
