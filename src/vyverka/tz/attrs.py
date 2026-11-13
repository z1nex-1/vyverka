import re

from ..config import attribute_keys
from ..models import Constraint, Op, Requirement
from ..units import NUM, canon_unit, to_float

_UNIT_TAIL = r"(?P<unit>[a-zA-Zа-яА-ЯёЁμµ°%²/\.\s0-9]{0,14})"


def norm_key(name: str) -> str:
    s = name.lower().replace("ё", "е")
    s = re.sub(r"\(.*?\)", " ", s)
    s = re.sub(r"[^a-zа-я0-9 ]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    for key, synonyms in attribute_keys().items():
        for syn in synonyms:
            pat = rf"(?<![а-яa-z0-9]){re.escape(syn)}" + (r"(?![а-яa-z])" if len(syn) <= 6 else "")
            if re.search(pat, s):
                return key
    words = [w for w in s.split() if len(w) > 2][:4]
    return "_".join(words) or "attr"


def _num(s: str) -> float | None:
    return to_float(s)


def parse_constraint(value: str, unit: str | None = None) -> Constraint:
    c = _parse_constraint(value, unit)
    if c.unit is None:
        return c
    raw_unit = unit or _unit_text(value)
    canon, factor = canon_unit(raw_unit) if raw_unit else (c.unit, 1.0)
    if factor != 1.0 and canon == c.unit:
        for f in ("value", "low", "high", "tolerance"):
            x = getattr(c, f)
            if x is not None:
                setattr(c, f, x * factor)
    return c


def _unit_text(value: str) -> str | None:
    m = re.search(r"[\d\s,.]([a-zA-Zа-яА-ЯёЁμµ°%²/\.]+)\s*$", value.strip())
    return m.group(1) if m else None


def _parse_constraint(value: str, unit: str | None = None) -> Constraint:
    v = value.strip().replace("−", "-")
    v = re.sub(r"\+\s*(?=\d)", "", v)
    low = v.lower()
    u = canon_unit(unit)[0] if unit else None

    if low in {"соответствие", "наличие", "да", "есть"} or low.endswith("- наличие"):
        return Constraint(op=Op.FLAG, text=v)

    d = re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×*]\s*(\d+(?:[.,]\d+)?))?\s*([а-яa-z]*)", low)
    if d:
        dims = "x".join(g.replace(",", ".") for g in d.groups()[:3] if g)
        return Constraint(op=Op.TEXT, text=dims, unit=u or _u(d.group(4)) or "mm")

    m = re.fullmatch(rf"(?:от\s*)?({NUM})\s*(?:до|-|–|—)\s*({NUM})\s*{_UNIT_TAIL}", v, re.I)
    if m and not re.search(r"не (выше|ниже)", low):
        lo, hi = _num(m.group(1)), _num(m.group(2))
        if lo is not None and hi is not None and lo <= hi:
            return Constraint(op=Op.RANGE, low=lo, high=hi, unit=u or _u(m.group("unit")))

    bounds: dict[str, float] = {}
    for word, val in re.findall(rf"(не\s+менее|не\s+более|не\s+выше|не\s+ниже|более|менее|свыше|до|от)\s*({NUM})", low):
        x = _num(val)
        if x is None:
            continue
        w = re.sub(r"\s+", " ", word)
        bounds.setdefault(w, x)
    if bounds:
        rest = re.sub(rf"(не\s+менее|не\s+более|не\s+выше|не\s+ниже|более|менее|свыше|до|от|и|\s|{NUM})", "", low)
        unit_guess = u or _u(rest)
        lo_keys = [k for k in ("не менее", "не ниже", "более", "свыше", "от") if k in bounds]
        hi_keys = [k for k in ("не более", "не выше", "менее", "до") if k in bounds]
        if lo_keys and hi_keys:
            return Constraint(op=Op.RANGE, low=bounds[lo_keys[0]], high=bounds[hi_keys[0]], unit=unit_guess)
        if lo_keys:
            k = lo_keys[0]
            return Constraint(op=Op.GT if k in ("более", "свыше") else Op.GE, value=bounds[k], unit=unit_guess)
        k = hi_keys[0]
        return Constraint(op=Op.LT if k == "менее" else Op.LE, value=bounds[k], unit=unit_guess)

    m = re.fullmatch(rf"({NUM})\s*(?:\(?\s*[±+]\s*-?\s*({NUM})\s*(%)?\s*\)?)?\s*{_UNIT_TAIL}", v)
    if m:
        x = _num(m.group(1))
        unit_part = (m.group("unit") or "").strip()
        tol = None
        if m.group(2):
            t = _num(m.group(2)) or 0
            pct = m.group(3) == "%" and unit_part != "%"
            tol = x * t / 100 if pct and x else t
        if x is not None:
            return Constraint(op=Op.EQ, value=x, tolerance=tol, unit=u or _u(unit_part))
    return Constraint(op=Op.TEXT, text=v)


def _u(s: str | None) -> str | None:
    if not s or not s.strip():
        return None
    return canon_unit(s.strip())[0]


def parse_line(line: str) -> Requirement | None:
    line = line.strip().strip(";").strip()
    if len(line) < 3:
        return None
    if ":" not in line and re.match(r"^[^-–]{4,40}[а-яa-z]\s?[-–]\s+\S", line):
        line = re.sub(r"\s?[-–]\s+", ": ", line, count=1)
    if ":" in line:
        name, value = line.split(":", 1)
        if not value.strip():
            return None
    else:
        m = re.match(rf"^(.*?[а-яa-z\)])\s+({NUM}.*|[A-ZА-Я][\w\-]{{0,6}}|[а-я]{{3,15}})$", line)
        if m and len(m.group(1)) > 3:
            name, value = m.group(1), m.group(2)
        elif re.search(r"\bГОСТ\b", line):
            name, value = "Соответствие стандарту", line
        else:
            name, value = line, "наличие"
    name = name.strip(" -–")
    value = value.strip()
    if value.lower().endswith("- наличие") or value.lower() == "наличие":
        value = "наличие"
    c = parse_constraint(value)
    return Requirement(key=norm_key(name), name=name, raw=line, constraint=c, essential=_essential(name, c))


def parse_row(name: str, value: str, unit: str | None) -> Requirement:
    c = parse_constraint(value, unit)
    if c.op == Op.TEXT and len(value) > 60:
        c = Constraint(op=Op.FLAG, text=value)
    raw = f"{name}: {value} {unit or ''}".strip()
    return Requirement(key=norm_key(name), name=name.strip(), raw=raw, constraint=c, essential=_essential(name, c))


_SOFT = ("окраска", "назначение", "хранени", "транспортир", "поддержка форматов", "материал рабочей", "цвет товара", "габарит", "вес ", "вес\u00a0", "оптимальн", "мощност", "гарант")


def _essential(name: str, c: Constraint) -> bool:
    n = name.lower() + " "
    if any(s in n for s in _SOFT):
        return False
    return c.op not in (Op.FLAG,) or "гост" in n
