from __future__ import annotations

from datetime import date, datetime
from enum import StrEnum

from pydantic import BaseModel, Field


class Op(StrEnum):
    EQ = "eq"
    GE = "ge"
    LE = "le"
    GT = "gt"
    LT = "lt"
    RANGE = "range"
    TEXT = "text"
    FLAG = "flag"


class Constraint(BaseModel):
    op: Op
    value: float | None = None
    low: float | None = None
    high: float | None = None
    tolerance: float | None = None
    text: str | None = None
    unit: str | None = None

    def satisfied_by(self, x: float) -> bool:
        if self.op == Op.EQ:
            tol = self.tolerance or 0.0
            return abs(x - (self.value or 0)) <= tol + 1e-9
        if self.op == Op.GE:
            return x >= (self.value or 0) - 1e-9
        if self.op == Op.GT:
            return x > (self.value or 0)
        if self.op == Op.LE:
            return x <= (self.value or 0) + 1e-9
        if self.op == Op.LT:
            return x < (self.value or 0)
        if self.op == Op.RANGE:
            lo = self.low if self.low is not None else float("-inf")
            hi = self.high if self.high is not None else float("inf")
            return lo - 1e-9 <= x <= hi + 1e-9
        return False

    def describe(self) -> str:
        u = f" {self.unit}" if self.unit else ""
        match self.op:
            case Op.EQ:
                tol = f" ±{_fmt(self.tolerance)}" if self.tolerance else ""
                return f"{_fmt(self.value)}{tol}{u}"
            case Op.GE:
                return f"не менее {_fmt(self.value)}{u}"
            case Op.GT:
                return f"более {_fmt(self.value)}{u}"
            case Op.LE:
                return f"не более {_fmt(self.value)}{u}"
            case Op.LT:
                return f"менее {_fmt(self.value)}{u}"
            case Op.RANGE:
                return f"{_fmt(self.low)}–{_fmt(self.high)}{u}"
            case _:
                return self.text or ""


def _fmt(x: float | None) -> str:
    if x is None:
        return "?"
    return f"{x:g}".replace(".", ",")


class Requirement(BaseModel):
    key: str
    name: str
    raw: str
    constraint: Constraint
    essential: bool = True


class Item(BaseModel):
    pos: int
    name: str
    unit: str
    qty: float
    okpd2: str | None = None
    brand: str | None = None
    model: str | None = None
    codes: list[str] = Field(default_factory=list)
    manufacturer: str | None = None
    country: str | None = None
    category: str | None = None
    requirements: list[Requirement] = Field(default_factory=list)
    pack_amount: float | None = None
    pack_unit: str | None = None


class Spec(BaseModel):
    title: str
    source_name: str
    customer: str | None = None
    delivery_place: str | None = None
    delivery_term: str | None = None
    warranty: str | None = None
    notice: str | None = None
    method: str | None = None
    items: list[Item]
    notes: list[str] = Field(default_factory=list)


class Evidence(BaseModel):
    url: str
    fetched_at: datetime
    sha256: str | None = None
    snapshot: str | None = None
    excerpt: str | None = None
    from_cache: bool = False


class Offer(BaseModel):
    id: str
    source: str
    source_title: str
    kind: str
    url: str
    title: str
    price: float
    currency: str = "RUB"
    unit: str | None = None
    qty: float | None = None
    vat_included: bool | None = True
    supplier: str | None = None
    customer: str | None = None
    doc_number: str | None = None
    doc_date: date | None = None
    okpd2: str | None = None
    ktru: str | None = None
    attributes: dict[str, str] = Field(default_factory=dict)
    text: str = ""
    penalties: bool | None = None
    stage: str | None = None
    evidence: Evidence
    query: str | None = None
    meta: dict = Field(default_factory=dict)


class AttrCheck(BaseModel):
    key: str
    name: str
    required: str
    found: str | None = None
    status: str
    via: str | None = None
    quote: str | None = None


class Decision(BaseModel):
    offer_id: str
    included: bool
    score: float
    reasons: list[tuple[str, str]] = Field(default_factory=list)
    checks: list[AttrCheck] = Field(default_factory=list)
    unit_price: float | None = None
    normalized_note: str | None = None
    factor: float = 1.0
    trust: int | None = None
    trust_factors: list[tuple[str, int]] = Field(default_factory=list)
    similarity: float | None = None
    override: dict | None = None


class CalcStep(BaseModel):
    title: str
    detail: str


class Calculation(BaseModel):
    status: str
    n: int
    prices: list[float] = Field(default_factory=list)
    offer_ids: list[str] = Field(default_factory=list)
    mean: float | None = None
    std: float | None = None
    cv: float | None = None
    minimum: float | None = None
    median: float | None = None
    unit_price: float | None = None
    total: float | None = None
    steps: list[CalcStep] = Field(default_factory=list)
    message: str = ""


class SourceStatus(BaseModel):
    source: str
    title: str
    status: str
    queries: list[str] = Field(default_factory=list)
    found: int = 0
    error: str | None = None
    elapsed_ms: int = 0


class ItemResult(BaseModel):
    item: Item
    offers: list[Offer] = Field(default_factory=list)
    decisions: list[Decision] = Field(default_factory=list)
    calc: Calculation
    sources: list[SourceStatus] = Field(default_factory=list)
    duplicates: list[tuple[str, str]] = Field(default_factory=list)
    analogs: list["Analog"] = Field(default_factory=list)
    estimate: dict | None = None


class Analog(BaseModel):
    """Точка облака похожих товаров: насколько предложение близко к позиции ТЗ и чем отличается."""
    offer_id: str
    similarity: float
    parts: dict[str, float] = Field(default_factory=dict)
    ring: str
    x: float = 0.0
    y: float = 0.0
    differs: list[str] = Field(default_factory=list)
    price: float | None = None


class TraceEvent(BaseModel):
    at: datetime
    stage: str
    message: str
    data: dict | None = None


class Run(BaseModel):
    id: str
    created_at: datetime
    mode: str
    spec: Spec
    results: list[ItemResult] = Field(default_factory=list)
    trace: list[TraceEvent] = Field(default_factory=list)
    total: float | None = None
    status: str = "running"
    settings: dict = Field(default_factory=dict)


ItemResult.model_rebuild()
