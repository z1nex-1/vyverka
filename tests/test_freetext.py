from vyverka.match import check, decide
from vyverka.tz.parser import parse_text

from conftest import offer

MONITOR = "Монитор Dell P2423, 24 дюйма, 1920x1200, IPS — 5 шт."
CARTRIDGE = "Картридж HP 59A (CF259A), черный, ресурс 3000 страниц — 10 шт."


def item(text):
    return parse_text(text).items[0]


def test_free_text_gives_model_codes_and_attributes():
    mon, cart = item(MONITOR), item(CARTRIDGE)
    assert (mon.brand, mon.model, mon.qty) == ("Dell", "P2423", 5)
    keys = {r.name: r.essential for r in mon.requirements}
    assert keys["Разрешение"] is True and keys["Диагональ экрана"] is False
    assert cart.codes == ["CF259A"] and cart.model == "59A"
    assert any(r.name.lower().startswith("ресурс") and r.constraint.value == 3000 for r in cart.requirements)


def test_resolution_is_exact_and_ignores_vesa():
    res = next(r for r in item(MONITOR).requirements if r.name == "Разрешение")
    assert check(res, offer("Монитор", text="крепление VESA 100x100, 1920x1200")).status == "ok"
    assert check(res, offer("Монитор", text="2560x1440, VESA 100x100")).status == "mismatch"
    assert check(res, offer("Монитор", text="VESA 100x100")).status == "unknown"


def test_model_with_suffix_is_another_model():
    d = decide(item(MONITOR), offer("Монитор Dell P2423DE, 23.8\", IPS, 2560x1440"))
    assert not d.included
    assert any("другая модель: Dell P2423DE" in t for _, t in d.reasons)


def test_article_code_counts_as_the_model():
    d = decide(item(CARTRIDGE), offer("Картридж для МФУ HP LaserJet Pro MFP M428 № HP CF259A", kind="contract"))
    assert d.included


def test_other_cartridge_and_refill_service_are_excluded():
    cart = item(CARTRIDGE)
    other = decide(cart, offer("Картридж HP 05A, черный"))
    service = decide(cart, offer("Заправка картриджа HP 59A (CF259A)"))
    assert not other.included and any(c == "other_model" for c, _ in other.reasons)
    assert not service.included and service.reasons[0][0] == "excluded_rule"


def test_refill_is_fine_when_refill_is_what_is_bought():
    d = decide(item("Заправка картриджа HP 59A (CF259A) — 10 шт."), offer("Заправка картриджа HP 59A (CF259A)"))
    assert not any(c == "excluded_rule" for c, _ in d.reasons)
