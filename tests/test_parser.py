import pytest

from conftest import EXAMPLES
from vyverka.tz.parser import parse_docx, parse_text


def load(name):
    return parse_docx((EXAMPLES / name).read_bytes(), name)


def test_paper_three_positions():
    spec = load("tz_paper.docx")
    assert [i.pos for i in spec.items] == [1, 2, 3]
    a4, a3, offset = spec.items
    assert "А4" in a4.name and "А3" in a3.name
    assert all(i.qty > 0 for i in spec.items)
    density = next(r for r in offset.requirements if "плотност" in r.name.lower())
    assert density.constraint.value == pytest.approx(65)


def test_printer_model_and_requirements():
    spec = load("tz_3d_printer.docx")
    (item,) = spec.items
    assert item.brand and item.brand.lower() == "creatbot"
    assert item.model and "d600" in item.model.lower()
    assert len(item.requirements) >= 14
    heat = next(r for r in item.requirements if "нагрев экструдера" in r.name.lower())
    assert heat.constraint.value == pytest.approx(420)


def test_cytokines_pack_amounts():
    spec = load("tz_cytokines.docx")
    assert len(spec.items) == 5
    assert all(i.pack_amount for i in spec.items)
    assert all(i.category == "reagents" for i in spec.items)


def test_plain_text_description():
    spec = parse_text("1. Бумага А4 офисная, 80 г/м², 500 листов — 10 пачек", "форма")
    assert spec.items and spec.items[0].qty == 10
