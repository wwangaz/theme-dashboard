from pydantic import BaseModel, Field, field_validator


class KeywordResult(BaseModel):
    id: int
    keywords: list[str]


class ThemeCluster(BaseModel):
    theme_id: str
    theme_name: str
    one_line_summary: str = ""
    signal_ids: list[int]
    representative_tickers: list[str] = []

    @field_validator("theme_id")
    @classmethod
    def slugify(cls, v: str) -> str:
        import re
        return re.sub(r"[^a-z0-9]+", "-", v.lower()).strip("-")


class StageResult(BaseModel):
    stage: str = Field(pattern=r"^(Early Discovery|Acceleration|Momentum Expansion|Crowded Euphoria|Narrative Breakdown)$")
    stage_confidence: int = Field(ge=1, le=10)
    stage_reasoning: str = ""


class EvidenceItem(BaseModel):
    headline: str
    source: str = ""
    url: str | None = None


class ConvictionResult(BaseModel):
    direction: str = Field(pattern=r"^(Bullish|Bearish|Neutral)$")
    conviction_score: int = Field(ge=1, le=10)
    bull_evidence: list[EvidenceItem] = []
    bear_evidence: list[EvidenceItem] = []
    conviction_basis: str = ""


class TimelineItem(BaseModel):
    date: str
    event: str


class CompressionResult(BaseModel):
    bull_case: str = ""
    bear_case: str = ""
    current_drivers: str = ""
    key_risks: list[str] = []
    short_term_outlook: str = ""
    mid_term_outlook: str = ""
    long_term_outlook: str = ""
    timeline: list[TimelineItem] = []
