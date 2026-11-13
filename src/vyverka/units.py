import re

_UNIT_ALIASES: dict[str, tuple[str, float]] = {
    "г/м2": ("g/m2", 1), "г/м²": ("g/m2", 1), "г/кв.м": ("g/m2", 1), "г/кв. м": ("g/m2", 1), "гр/м2": ("g/m2", 1),
    "мкм": ("mm", 0.001), "μm": ("mm", 0.001), "um": ("mm", 0.001), "микрон": ("mm", 0.001), "мм": ("mm", 1), "см": ("mm", 10), "м": ("mm", 1000),
    "метр": ("mm", 1000), "метра": ("mm", 1000), "метров": ("mm", 1000), "m": ("mm", 1000), "mm": ("mm", 1), "cm": ("mm", 10),
    "%": ("%", 1), "кг": ("g", 1000), "г": ("g", 1), "гр": ("g", 1), "мг": ("ug", 1000), "мкг": ("ug", 1), "µg": ("ug", 1),
    "ug": ("ug", 1), "mg": ("ug", 1000), "нг": ("ng", 1), "нг/мл": ("ng/ml", 1), "ng/ml": ("ng/ml", 1),
    "вт": ("W", 1), "w": ("W", 1), "квт": ("W", 1000), "в": ("V", 1), "гц": ("Hz", 1),
    "град": ("C", 1), "градусов": ("C", 1), "°c": ("C", 1), "0c": ("C", 1), "°": ("C", 1),
    "мм/с": ("mm/s", 1), "мл": ("ml", 1), "л": ("ml", 1000),
    "шт": ("pcs", 1), "шт.": ("pcs", 1), "штука": ("pcs", 1), "листов": ("sheet", 1), "лист": ("sheet", 1),
}

_QTY_UNITS = {
    "шт": "pcs", "шт.": "pcs", "штука": "pcs", "штук": "pcs", "единица": "pcs", "ед": "pcs", "ед.": "pcs",
    "пачка": "pack", "пач": "pack", "пачек": "pack", "упак": "pack", "упаковка": "pack", "уп": "pack", "уп.": "pack",
    "комплект": "set", "компл": "set", "набор": "set",
    "кг": "kg", "килограмм": "kg", "г": "g", "мкг": "ug", "мг": "mg", "л": "l", "литр": "l", "м": "m", "метр": "m",
    "лист": "sheet", "листов": "sheet", "рулон": "roll", "флакон": "vial", "фл": "vial",
}

UNIT_TITLES = {"pcs": "шт", "pack": "пачка", "set": "компл", "kg": "кг", "g": "г", "ug": "мкг", "mg": "мг",
               "l": "л", "m": "м", "sheet": "лист", "roll": "рулон", "vial": "флакон"}


def canon_unit(u: str | None) -> tuple[str | None, float]:
    if not u:
        return None, 1.0
    s = u.strip().lower().replace(" ", "").rstrip(".")
    s = s.replace("кв.м", "м2").replace("квм", "м2")
    for k in (s, s + "."):
        if k in _UNIT_ALIASES:
            return _UNIT_ALIASES[k]
    k = s.replace(" ", "")
    for alias, val in _UNIT_ALIASES.items():
        if k == alias.replace(" ", ""):
            return val
    return s, 1.0


def canon_qty_unit(u: str | None) -> str | None:
    if not u:
        return None
    s = re.sub(r"[^\wа-яё.]", "", u.strip().lower())
    s = s.rstrip(".")
    if s in _QTY_UNITS:
        return _QTY_UNITS[s]
    for k, v in _QTY_UNITS.items():
        if s.startswith(k.rstrip(".")) and len(k) >= 3:
            return v
    return s or None


def to_float(s: str) -> float | None:
    s = s.strip().replace("−", "-").replace(" ", "").replace("\xa0", "")
    s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


NUM = r"[-−]?\s?\d+(?:[.,]\d+)?"
