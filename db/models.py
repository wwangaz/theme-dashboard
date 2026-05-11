from datetime import date, datetime
from typing import Optional
from sqlmodel import SQLModel, Field


class RawSignal(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    source_type: str  # "news" | "filing" | "transcript" | "price"
    source_name: str
    ticker: Optional[str] = None
    headline: str
    content: str
    signal_date: date
    url: Optional[str] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)


class Theme(SQLModel, table=True):
    id: str = Field(primary_key=True)  # slug, e.g. "ai-infrastructure"
    name: str
    first_seen: date
    last_active: date
    representative_tickers_json: str = "[]"
    is_active: bool = True


class ThemeDailySnapshot(SQLModel, table=True):
    id: Optional[int] = Field(default=None, primary_key=True)
    theme_id: str = Field(foreign_key="theme.id")
    snapshot_date: date
    direction: str  # "Bullish" | "Bearish" | "Neutral"
    conviction_score: int
    stage: str
    stage_confidence: int
    stage_reasoning: str = ""
    bull_evidence_json: str = "[]"
    bear_evidence_json: str = "[]"
    conviction_basis: str = ""
    bull_case: str = ""
    bear_case: str = ""
    current_drivers: str = ""
    key_risks_json: str = "[]"
    short_term_outlook: str = ""
    mid_term_outlook: str = ""
    long_term_outlook: str = ""
    signal_count: int = 0
    timeline_json: str = "[]"


class SignalThemeMap(SQLModel, table=True):
    signal_id: int = Field(foreign_key="rawsignal.id", primary_key=True)
    theme_id: str = Field(foreign_key="theme.id", primary_key=True)
    relevance_score: float = 1.0
