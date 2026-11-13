import asyncio

from conftest import EXAMPLES
from vyverka.match import interval
from vyverka.pipeline import Pipeline
from vyverka.tz.parser import parse_docx


def test_range_unit_after_last_number():
    assert interval("≥ 1,5 и < 2 Метр", "mm") == (1500.0, 2000.0)
    assert interval("1,8 м", "mm") == (1800.0, 1800.0)
    assert interval("≥ 0,3 и < 0,5 мм", "mm") == (0.3, 0.5)


def test_new_category_without_settings():
    tz = EXAMPLES / "tz_mouse.docx"
    spec = parse_docx(tz.read_bytes(), tz.name)
    assert len(spec.items) == 1 and spec.items[0].category == "generic"
    run = asyncio.run(Pipeline(mode="offline").execute(spec))
    calc = run.results[0].calc
    assert calc.status == "ok" and calc.n == 9
    assert run.total == 17836.2
    reasons = [d.reasons[-1][1] for d in run.results[0].decisions if not d.included and d.reasons]
    assert not any("длина кабеля" in r for r in reasons)
