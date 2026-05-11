from datetime import date
import httpx
from bs4 import BeautifulSoup
from db.models import RawSignal
from db.session import get_session

MOTLEY_FOOL_TRANSCRIPTS = "https://www.fool.com/earnings-call-transcripts/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; ThemeDashboard/0.1)"}


def fetch_transcripts() -> list[RawSignal]:
    signals: list[RawSignal] = []
    try:
        resp = httpx.get(MOTLEY_FOOL_TRANSCRIPTS, headers=HEADERS, timeout=20, follow_redirects=True)
        soup = BeautifulSoup(resp.text, "html.parser")
        links = soup.select("a[href*='/earnings/call-transcript']")[:10]

        for link in links:
            title = link.get_text(strip=True)
            href = link.get("href", "")
            if not href.startswith("http"):
                href = "https://www.fool.com" + href
            try:
                page = httpx.get(href, headers=HEADERS, timeout=20, follow_redirects=True)
                page_soup = BeautifulSoup(page.text, "html.parser")
                article = page_soup.find("div", {"class": lambda c: c and "article" in c.lower()})
                text = article.get_text(separator=" ", strip=True)[:3000] if article else ""
                if text:
                    signals.append(RawSignal(
                        source_type="transcript",
                        source_name="Motley Fool",
                        ticker=None,
                        headline=title[:500],
                        content=text,
                        signal_date=date.today(),
                        url=href,
                    ))
            except Exception:
                pass
    except Exception as e:
        print(f"[transcripts] fetch failed: {e}")

    return signals


def ingest_transcripts():
    signals = fetch_transcripts()
    with get_session() as session:
        for s in signals:
            session.add(s)
        session.commit()
    print(f"[transcripts] ingested {len(signals)} signals")
