import pytest

from conftest import offer
from vyverka.models import Decision, Item
from vyverka.normalize import normalize
from vyverka.sources.base import money
from vyverka.tz.parser import check, parse_text, read_text


@pytest.mark.parametrize("raw, value", [("1,299.00", 1299.0), ("1.299,00", 1299.0), ("1 299,50 ₽", 1299.5),
                                        ("324,40", 324.4), ("1.450.000", 1450000.0)])
def test_money_understands_both_separators(raw, value):
    assert money(raw) == value


def test_free_text_keeps_decimal_comma_and_trailing_quantity():
    it = parse_text('Ноутбук 15,6", 16 ГБ ОЗУ, SSD 512 ГБ, 10 шт').items[0]
    assert it.name == "Ноутбук" and it.qty == 10
    assert any("15,6" in r.raw for r in it.requirements)
    assert parse_text("Уборка помещений — 12 усл. ед.").items[0].qty == 12


def test_bad_quantity_and_binary_text_are_rejected():
    spec = parse_text("Перчатки нитриловые — 5 упак.")
    spec.items[0].qty = -5
    with pytest.raises(ValueError):
        check(spec)
    with pytest.raises(ValueError):
        read_text(b"\x00\x01\x02binary\x00" * 20)
    assert "Картридж" in read_text("Картридж HP 59A".encode("cp1251"))


def test_foreign_currency_price_is_excluded():
    item = Item(pos=1, name="Центрифуга", unit="шт", qty=1)
    o = offer("Центрифуга лабораторная", 1500, currency="USD")
    d = Decision(offer_id=o.id, included=True, score=1.0)
    normalize(item, o, d)
    assert not d.included and d.reasons[-1][0] == "currency"


def test_series_name_does_not_replace_checking_specs():
    from vyverka.match import decide
    item = parse_text("Ноутбук Lenovo IdeaPad 15\nОперативная память: не менее 16 ГБ\nОбъем SSD: не менее 512 ГБ").items[0]
    d = decide(item, offer("Ноутбук Lenovo IdeaPad 15.6 Celeron 4Gb SSD 128Gb", 31000, kind="contract"))
    assert not d.included


def test_commercial_offer_price_must_be_finite():
    from fastapi.testclient import TestClient
    from vyverka.web import app as web
    with TestClient(web.app) as c:
        r = c.post("/runs", data={"text": "Перчатки нитриловые — 5 упак.", "mode": "offline"}, follow_redirects=False)
        rid = r.headers["location"].rsplit("/", 1)[1]
        import time
        for _ in range(100):
            if c.get(f"/api/runs/{rid}").json()["status"] != "running":
                break
            time.sleep(0.2)
        assert c.post(f"/runs/{rid}/items/1/offers", data={"supplier": "a", "price": "inf"}).status_code == 400
        assert c.get(f"/runs/{rid}").status_code == 200


def test_contract_price_indexed_by_monthly_rosstat_series():
    from datetime import date

    from vyverka.normalize import index_factor

    f, note = index_factor(date(2024, 5, 20), date(2026, 11, 13))
    assert f == pytest.approx(1.17697, abs=1e-4) and "06.2024–09.2026 (28 мес.)" in note
    assert index_factor(date(2026, 10, 9), date(2026, 11, 13)) == (1.0, None)
