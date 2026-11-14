from datetime import date
from pathlib import Path

from vyverka.settings import _source_kind
from vyverka.sources.eis import parse_search
from vyverka.sources.eis223 import parse_subject, parse_subject_line

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def test_search_takes_customer_not_number():
    hits = parse_search(read("eis223_search.html"))
    assert len(hits) == 3
    first = hits[0]
    assert first["reg"] == "57703551276260003010000"
    assert first["customer"] == 'АКЦИОНЕРНОЕ ОБЩЕСТВО "МОСГОРСВЕТ"'
    assert first["stage"] == "Исполнение завершено" and first["signed"] == date(2026, 10, 14)


def test_subject_row_and_subject_line():
    rows = parse_subject(read("eis223_subject.html"))
    assert rows == [{"n": 1, "name": "Бумага для офисной техники", "okpd2": "17.12.14.111", "price": 292.06,
                     "qty": 200.0, "unit": "Пачка",
                     "excerpt": "Бумага для офисной техники. Цена за единицу: 292,06 , Российский рубль. Кол-во: 200 Пачка"}]
    assert parse_subject_line(read("eis223_info.html")) == "Бумага офисная SvetoCopy"


def test_223_registry_is_contract_source():
    # Договоры без поставщика не должны сворачиваться в «один сайт», как у каталога.
    assert _source_kind({"type": "eis223"}) == "contract"
