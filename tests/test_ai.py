import asyncio
import json
from datetime import datetime

from vyverka import ai
from vyverka.match import decide
from vyverka.models import Evidence, Offer
from vyverka.tz.parser import parse_text


def _offer(text: str) -> Offer:
    return Offer(id="o1", source="cat", source_title="Каталог", kind="catalog", url="https://example.ru/1",
                 title="Бумага офисная А4 SVETOCOPY", price=300, text=text,
                 evidence=Evidence(url="https://example.ru/1", fetched_at=datetime(2026, 11, 1)))


def test_quote_must_be_in_source_and_hold_the_number():
    text = "Бумага А4, 500 л., марка С, Россия, 146% (CIE)"
    assert ai.verify("146 %", "146% (CIE)", text) is None
    assert ai.verify("Белый", "", text) == "нет цитаты"
    assert ai.verify("80 г/м2", "80 г/м2", text) == "цитаты нет в тексте источника"
    assert ai.verify("150 %", "146% (CIE)", text) == "числа значения нет в цитате"
    assert ai.verify("С", "марка С", text) is None


def test_model_value_goes_through_rules_and_hallucination_is_dropped(monkeypatch, tmp_path):
    monkeypatch.setenv("VYVERKA_DATA", str(tmp_path))
    item = parse_text("Картридж для принтера\nРесурс барабана: не менее 12000 страниц\nЦвет: чёрный").items[0]
    offer = _offer("Барабан рассчитан на 12000 отпечатков.")
    drum = next(r.name for r in item.requirements if "барабан" in r.name.lower())
    colour = next(r.name for r in item.requirements if "вет" in r.name)
    assert {c.name: c.status for c in decide(item, offer).checks}[drum] == "unknown"

    async def fake(system, user):
        return json.dumps({"found": [
            {"name": drum, "value": "12000 страниц", "quote": "рассчитан на 12000 отпечатков"},
            {"name": colour, "value": "чёрный", "quote": "цвет чёрный"}]}, ensure_ascii=False)

    monkeypatch.setattr(ai, "_chat", fake)
    acc, rej = asyncio.run(ai.Reader().read(offer, item.requirements))
    assert [v["name"] for v in acc.values()] == [drum]
    assert rej[0]["why"] == "цитаты нет в тексте источника"
    offer.meta["ai"] = {v["name"]: v for v in acc.values()}
    assert {c.name: c.status for c in decide(item, offer).checks}[drum] == "ok"


def test_vague_text_is_rejected():
    import pytest
    from vyverka.tz.parser import check

    with pytest.raises(ValueError):
        check(parse_text("Нужно что-то купить"))
    check(parse_text("Купить бумагу А4"))
