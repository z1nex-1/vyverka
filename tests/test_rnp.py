import asyncio
from datetime import datetime, timezone
from pathlib import Path

from vyverka import rnp
from vyverka.calc import anomalies, calculate
from vyverka.models import Decision, Evidence, Offer
from vyverka.net import Page

FIX = Path(__file__).parent / "fixtures"


def offer(i: str, supplier: str) -> Offer:
    return Offer(id=i, source="eis", source_title="ЕИС", kind="contract", url=f"https://x.test/{i}", title="Бумага",
                 price=300, supplier=supplier, evidence=Evidence(url="https://x.test", fetched_at=datetime.now(timezone.utc)))


class FakeFetcher:
    def __init__(self, pages: dict[str, str]):
        self.pages, self.calls = pages, 0

    async def get(self, url, params=None, headers=None):
        self.calls += 1
        return Page(url=url, status=200, text=self.pages[params["searchString"]], fetched_at=datetime.now(timezone.utc),
                    sha256="0" * 64, snapshot="0" * 32, from_cache=False)


def test_parse_listed_and_empty():
    listed = rnp.parse((FIX / "rnp_listed.html").read_text(encoding="utf-8"), "0269017441")
    assert listed == [{"number": "Р2621628", "name": 'ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ "ЗАВОД ТЕПЛОТЕХНИЧЕСКОГО ОБОРУДОВАНИЯ"',
                       "law": "223-ФЗ", "added": "2026-11-13"}]
    assert rnp.parse((FIX / "rnp_listed.html").read_text(encoding="utf-8"), "7700000000") == []
    assert rnp.parse((FIX / "rnp_empty.html").read_text(encoding="utf-8"), "7700000000") == []
    assert rnp.parse("<html>капча</html>", "7700000000") is None


def test_inn_of_skips_masked_individual():
    assert rnp.inn_of(offer("a", "ООО «Папирус», ИНН 1901069151")) == "1901069151"
    assert rnp.inn_of(offer("b", "ИП Иванов, ИНН 7701****5678")) is None


def test_listed_supplier_excluded_once_per_inn():
    listed = (FIX / "rnp_listed.html").read_text(encoding="utf-8")
    empty = (FIX / "rnp_empty.html").read_text(encoding="utf-8")
    offers = [offer("a", "ООО «Завод», ИНН 0269017441"), offer("b", "ООО «Завод», ИНН 0269017441"),
              offer("c", "ООО «Папирус», ИНН 1901069151")]
    dec = {o.id: Decision(offer_id=o.id, included=True, score=1) for o in offers}
    f = FakeFetcher({"0269017441": listed, "1901069151": empty})
    counts = asyncio.run(rnp.check(f, offers, dec))
    assert f.calls == 2 and counts == {"clean": 1, "listed": 2, "unknown": 0}
    assert not dec["a"].included and dec["a"].reasons[-1][0] == "rnp" and "Р2621628" in dec["a"].reasons[-1][1]
    assert dec["c"].included and rnp.label(offers[2]) == "нет в РНП"


def test_anomaly_hints_do_not_change_price():
    prices = {"a": 300, "b": 310, "c": 320, "d": 305, "e": 700}
    flags = anomalies(prices)
    assert len(flags) == 1 and flags[0].startswith("цена 700,00")
    calc, excluded = calculate(prices, 1)
    assert not excluded and calc.n == 5
    assert any(s.title == "Проверка аномалий" for s in calc.steps)
    assert anomalies({"a": 100, "b": 110, "c": 250}) and not anomalies({"a": 100, "b": 110, "c": 120})
