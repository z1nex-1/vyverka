from conftest import offer, req
from vyverka.match import check, decide, interval
from vyverka.models import Constraint, Item, Op, Requirement
from vyverka.sources.base import text_pairs


def test_interval_reads_ktru_ranges():
    assert interval("≥ 0,3 и < 0,5 мм", "мм") == (0.3, 0.5)
    assert interval("от 2 до 8", None) == (2, 8)
    assert interval("420 °C", "C")[0] == 420


def test_interval_ignores_area_of_one_square_metre():
    lo, hi = interval("масса бумаги площадью 1 м2 80 г", "г/м2")
    assert lo == hi == 80


def test_unit_with_digit_is_not_a_value():
    r = req("Максимальная скорость печати", "120", "мм/с")
    o = offer("3D-принтер FDM", attrs={"Максимальная скорость печати,": "см3/ч",
                                        "Максимальная скорость печати, см3/ч": "100"})
    assert check(r, o).status == "unknown"


def test_stray_number_with_same_unit_cannot_reject():
    r = req("Толщина слоя", "0,05–0,4", "мм")
    o = offer("Пластик для 3D-принтера", text="Пластик PLA, диаметр нити 1,75 мм, катушка 1 кг")
    assert check(r, o).status != "mismatch"


def test_open_bound_against_listed_values_is_partial():
    r = req("Диаметр сопел", "0.3/0.4/0.6/0.8/1.0", "мм")
    o = offer("3D-принтер", attrs={"Диаметр сопла": "≥ 0,5 мм"})
    assert check(r, o).status == "partial"


def test_subset_of_listed_materials_is_partial_not_mismatch():
    r = Requirement(key="materials", name="Поддерживаемые пластики", raw="PLA, ABS, PETG, PP, TPU",
                    constraint=Constraint(op=Op.TEXT, text="PLA, ABS, PETG, PP, TPU"))
    o = offer("3D-принтер", attrs={"Поддерживаемые пластики": "PP"})
    assert check(r, o).status == "partial"


def test_a4_contract_does_not_pass_as_a3(paper_a3):
    d = decide(paper_a3, offer("Бумага SvetoCopy А-4, 80 г/м2, 500 л.", 300, kind="contract"))
    assert not d.included
    assert any("А4" in c.found for c in d.checks if c.found and c.status == "mismatch")


def test_brand_without_model_explained():
    item = Item(pos=1, name="3D принтер CreatBot D600 Pro", unit="шт", qty=1, brand="CreatBot", model="D600 Pro",
                category="printer3d")
    d = decide(item, offer("Принтер 3D. CreatBot", 1_115_240, kind="contract"))
    assert not d.included
    assert "модель не названа" in d.reasons[-1][1]
    d = decide(item, offer("Принтер 3D. CreatBot PEEK 300", 1_207_983, kind="contract"))
    assert "другая модель: CreatBot PEEK 300" in d.reasons[-1][1]


def test_modification_is_marked():
    item = Item(pos=1, name="3D принтер CreatBot D600 Pro", unit="шт", qty=1, brand="CreatBot", model="D600 Pro",
                category="printer3d")
    d = decide(item, offer("3D принтер CreatBot D600 Pro 2 HS", 1_350_000))
    assert any(code == "modification" for code, _ in d.reasons)


def test_text_pairs_from_supplier_description():
    pairs = text_pairs("Чистота – более 98%; Уровень эндотоксина: менее 0,1 EU/мкг. Форма: лиофилизат")
    assert pairs["Чистота"] == "более 98%"
    assert pairs["Уровень эндотоксина"].startswith("менее 0,1")
    assert pairs["Форма"] == "лиофилизат"
