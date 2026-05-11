from datetime import date
import feedparser
import httpx
from bs4 import BeautifulSoup
from db.models import RawSignal
from db.session import get_session

EDGAR_RSS = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=8-K&dateb=&owner=include&count=40&search_text=&output=atom"
HEADERS = {"User-Agent": "ThemeDashboard/0.1 contact@example.com"}


def _extract_text(filing_url: str) -> str:
    try:
        resp = httpx.get(filing_url, headers=HEADERS, timeout=15, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        return soup.get_text(separator=" ", strip=True)[:3000]
    except Exception:
        return ""


def fetch_edgar(lookback_days: int = 1) -> list[RawSignal]:
    signals: list[RawSignal] = []
    try:
        feed = feedparser.parse(EDGAR_RSS, request_headers=HEADERS)
        for entry in feed.entries:
            signals.append(RawSignal(
                source_type="filing",
                source_name="SEC EDGAR 8-K",
                ticker=None,
                headline=entry.get("title", "")[:500],
                content=entry.get("summary", "")[:2000],
                signal_date=date.today(),
                url=entry.get("link"),
            ))
    except Exception as e:
        print(f"[edgar] failed: {e}")
    return signals


def ingest_edgar():
    signals = fetch_edgar()
    with get_session() as session:
        for s in signals:
            session.add(s)
        session.commit()
    print(f"[edgar] ingested {len(signals)} signals")
