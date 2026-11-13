from datetime import datetime, timezone
from pathlib import Path

import pytest

from vyverka.models import Constraint, Evidence, Item, Offer, Op, Requirement
from vyverka.tz.attrs import parse_row

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"
FIXTURES = Path(__file__).resolve().parent / "fixtures"


def offer(title: str, price: float = 100.0, attrs: dict | None = None, text: str = "", **kw) -> Offer:
    data = dict(id=kw.pop("id", f"t:{title}"), source=kw.pop("source", "test"), source_title="Тест",
                kind=kw.pop("kind", "offer"), url=kw.pop("url", "https://example.test/p"), title=title, price=price,
                attributes=attrs or {}, text=text,
                evidence=Evidence(url="https://example.test/p", fetched_at=datetime.now(timezone.utc)))
    data.update(kw)
    return Offer(**data)


def req(name: str, value: str, unit: str | None = None) -> Requirement:
    return parse_row(name, value, unit)


@pytest.fixture
def paper_a3() -> Item:
    return Item(pos=2, name="Бумага для офисной техники А3 SvetoCopy", unit="пачка", qty=25, category="paper",
                requirements=[Requirement(key="format", name="Формат", raw="А3",
                                          constraint=Constraint(op=Op.TEXT, text="А3"))])


@pytest.fixture(autouse=True)
def _isolated_data(tmp_path, monkeypatch):
    monkeypatch.setenv("VYVERKA_DATA", str(tmp_path / "data"))
    monkeypatch.setenv("VYVERKA_SEARCH_CURL", "0")
