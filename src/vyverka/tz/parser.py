import io
import math
import re
from pathlib import Path

import docx

from ..config import rules
from ..models import Item, Op, Requirement, Spec
from ..units import canon_qty_unit, to_float
from .attrs import parse_line, parse_row


def _cells(row) -> list[str]:
    out: list[str] = []
    prev = None
    for c in row.cells:
        if c._tc is prev:
            continue
        prev = c._tc
        out.append(c.text.strip())
    return out


def _pos(s: str) -> int | None:
    m = re.match(r"^\s*(\d{1,3})\.?\s*$", s)
    return int(m.group(1)) if m else None


def parse_docx(data: bytes, name: str = "ТЗ.docx") -> Spec:
    d = docx.Document(io.BytesIO(data))
    items: dict[int, Item] = {}
    blocks: dict[int, str] = {}
    rows_by_pos: dict[int, list[Requirement]] = {}
    customer = place = term = warranty = None
    notes: list[str] = []

    for table in d.tables:
        rows = [_cells(r) for r in table.rows]
        header = " ".join(" ".join(r) for r in rows[:3]).lower()
        if "количество" in header and "окпд" in header and "наименование" in header and "срок" not in header:
            for r in rows:
                p = _pos(r[0]) if r else None
                if p is None or len(r) < 5 or r[1] in ("2", ""):
                    continue
                qty = to_float(r[3].replace(" ", "")) or 0
                items[p] = Item(pos=p, name=_one_line(r[1]), unit=r[2], qty=qty, okpd2=_okpd(r[4]))
        elif "срок поставки" in header:
            for r in rows:
                if r and _pos(r[0]) is not None and len(r) >= 3:
                    term = r[-1]
                    customer = r[1].split("\n")[0].strip()
                    place = _address(r[1])
        elif "наименование характеристики" in header:
            for r in rows:
                if len(r) < 4 or "наименование характеристики" in " ".join(r).lower():
                    continue
                p = _pos(r[0])
                if p is None:
                    p = _match_pos(r[1], items)
                if p is None:
                    continue
                name_, value = r[2], r[3]
                unit = r[4] if len(r) > 4 else None
                if not name_ or name_ == value:
                    continue
                rows_by_pos.setdefault(p, []).append(parse_row(_one_line(name_), _one_line(value), _one_line(unit or "")))
        elif "требования обязательные" in header or "технические характеристики" in header:
            for r in rows:
                p = _pos(r[0]) if r else None
                if p is None or len(r) < 3 or r[2] in ("3", ""):
                    continue
                blocks[p] = r[1]
                reqs = [q for q in (parse_line(x) for x in r[2].split("\n")) if q]
                rows_by_pos.setdefault(p, []).extend(reqs)
        elif len(table.columns) == 1:
            for r in rows:
                t = " ".join(r).strip()
                if not t:
                    continue
                if "гарант" in t.lower() and ("месяц" in t.lower()):
                    m = re.search(r"(\d+)\s*\(?[а-я]*\)?\s*месяц", t)
                    warranty = f"{m.group(1)} мес." if m else warranty
                notes.append(t)

    for p, it in items.items():
        it.requirements = _unique_keys(rows_by_pos.get(p, []))
        _enrich(it, blocks.get(p, ""))

    title = _title(d) or Path(name).stem
    return Spec(title=title, source_name=name, customer=customer, delivery_place=place, delivery_term=term,
                warranty=warranty, items=[items[k] for k in sorted(items)], notes=notes)


_VAGUE = re.compile(r"\b(?:что[- ]?(?:то|нибудь)|как(?:ой|ая|ое|ие)[- ]?(?:то|нибудь)|нужн\w*|надо|хочу|купить|"
                    r"приобрести|товар\w*|что угодно|всякое)\b", re.I)


_FILLER = {"что", "это", "для", "нам", "мне", "нас", "очень", "срочно", "кое", "пожалуйста", "нибудь", "еще", "ещё"}


def check(spec: Spec) -> None:
    for it in spec.items:
        meaning = [w for w in re.findall(r"[a-zа-яё]{3,}", _VAGUE.sub(" ", it.name.lower())) if w not in _FILLER]
        if not it.requirements and not it.codes and not meaning:
            raise ValueError("Не понятно, что закупается. Назовите товар и хотя бы одну характеристику, "
                             "например: «Монитор 24 дюйма, 1920x1200, IPS, 10 шт»")
        if not (math.isfinite(it.qty) and 0 < it.qty < 1e9):
            raise ValueError(f"Позиция {it.pos}: количество «{it.qty:g}» должно быть положительным числом")


def read_text(data: bytes) -> str:
    """Текст ТЗ из файла: UTF-8, иначе Windows-1251; двоичный файл отклоняется, а не превращается в запрос."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            text = data.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        raise ValueError("Не удалось прочитать текст: ожидается UTF-8 или Windows-1251")
    junk = sum(1 for ch in text if ord(ch) < 32 and ch not in "\t\r\n")
    if "\x00" in text or junk > max(3, len(text) // 50):
        raise ValueError("Файл не похож на текстовое описание закупки")
    return text


_QTY = re.compile(r"(\d[\d\s]*(?:[.,]\d+)?)\s*(шт|пар[аы]?|пач\w*|упак\w*|уп|компл\w*|набор\w*|флак\w*|рул\w*|кор\w*|усл\.?\s*ед\w*|лиц\w*|кг|л|м|ед\w*)\.?", re.I)


def parse_text(text: str, name: str = "Описание") -> Spec:
    lines = [x.strip() for x in text.splitlines() if x.strip()]
    if not lines:
        return Spec(title=name, source_name=name, items=[])
    head = re.sub(r"^\d{1,3}[.)]\s+", "", lines[0])
    qty, unit, okpd = 1.0, "шт", None
    tail = re.search(r"\s[—–-]\s*(\d[\d\s]*(?:[.,]\d+)?)\s*(шт|пар[аы]?|пач\w*|упак\w*|уп|компл\w*|набор\w*|флак\w*|рул\w*|кор\w*|усл\.?\s*ед\w*|лиц\w*|кг|л|м|ед\w*)\.?\s*$", head, re.I)
    if tail:
        qty = to_float(tail.group(1).replace(" ", "")) or 1.0
        unit = tail.group(2)
        head = head[:tail.start()].strip(" ,;")
    reqs: list[Requirement] = []
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith(("количество:", "кол-во:")):
            m = re.search(r"([\d\s,.]+)\s*([^\d\s].*)?$", ln.split(":", 1)[1])
            if m:
                qty = to_float(m.group(1).replace(" ", "")) or 1
                unit = (m.group(2) or unit).strip()
            continue
        if low.startswith("окпд"):
            okpd = _okpd(ln)
            continue
        q = parse_line(ln)
        if q:
            reqs.append(q)
    parts = [x.strip() for x in re.split(r"(?<!\d),(?![^()]*\))|,(?!\d)(?![^()]*\))", head) if x.strip()]
    if len(parts) > 1 and (m := _QTY.fullmatch(parts[-1])):
        qty = to_float(m.group(1).replace(" ", "")) or qty
        unit = m.group(2)
        parts = parts[:-1]
    head = parts[0]
    if m := re.search(r"\s(\d{1,2}(?:[.,]\d)?)\s*(?:\"|″|”|дюйм\w*)$", head):
        parts.insert(1, m.group(1) + '"')
        head = head[:m.start()].strip()
    reqs = [r for r in (_free_attr(x) for x in parts[1:]) if r] + reqs
    it = Item(pos=1, name=head, unit=unit, qty=qty, okpd2=okpd, requirements=_unique_keys(reqs))
    it.codes = re.findall(r"\(([A-Za-z]{1,4}[\w-]*\d[\w-]*)\)", head)
    _enrich(it, head)
    return Spec(title=head, source_name=name, items=[it])


_COLORS = r"(?:ч[её]рн|бел|сер|син|красн|ж[её]лт|зел[её]н|голуб|пурпурн|cyan|magenta|yellow|black)\w*"


def _free_attr(seg: str) -> Requirement | None:
    """Характеристика из строки свободного описания: «24 дюйма», «1920x1200», «ресурс 3000 страниц», «черный»."""
    if ":" in seg:
        return parse_line(seg)
    if m := re.fullmatch(r"(\d{3,5})\s*[xх×*]\s*(\d{3,5})(?:\s*пикс\w*)?", seg, re.I):
        return parse_row("Разрешение", f"{m.group(1)}x{m.group(2)}", None)
    if m := re.fullmatch(r"(\d+(?:[.,]\d+)?)\s*(?:\"|″|дюйм\w*)", seg, re.I):
        r = parse_row("Диагональ экрана", m.group(1) + " дюйм", None)
        r.essential = False  # в продаже 24″ — это и 23,8″, точное совпадение не требуется
        return r
    if re.fullmatch(_COLORS, seg, re.I):
        r = parse_row("Цвет", seg, None)
        r.essential = False
        return r
    return parse_line(seg)


def _unique_keys(reqs: list[Requirement]) -> list[Requirement]:
    seen: dict[str, int] = {}
    for r in reqs:
        n = seen.get(r.key, 0)
        seen[r.key] = n + 1
        if n:
            r.key = f"{r.key}_{n + 1}"
    return reqs


def _one_line(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


def _okpd(s: str) -> str | None:
    m = re.search(r"\d{2}\.\d{2}(?:\.\d{1,3}){0,3}", s)
    return m.group(0) if m else None


def _address(s: str) -> str | None:
    for line in s.split("\n"):
        if "почтовый адрес" in line.lower() or "место поставки" in line.lower():
            return line.split(":", 1)[-1].strip()
    m = re.search(r"Долгопрудный[^\n]*", s)
    return m.group(0) if m else None


def _match_pos(name: str, items: dict[int, Item]) -> int | None:
    key = _one_line(name).lower()[:25]
    for p, it in items.items():
        if it.name.lower()[:25] == key:
            return p
    return None


def _title(d) -> str | None:
    texts = [p.text.strip() for p in d.paragraphs if p.text.strip()]
    for i, t in enumerate(texts):
        if t.lower().startswith("техническое задание") and i + 1 < len(texts):
            return f"{t} {texts[i + 1]}"
    return None


_LATIN = re.compile(r"\b[A-Za-z][A-Za-z0-9\-]*(?:\s+[A-Z0-9][A-Za-z0-9\-]*)*")


def _enrich(it: Item, block: str) -> None:
    text = f"{it.name}\n{block}"
    m = re.search(r"Производитель:?\s*\n?\s*([^\n/]+)", text)
    if m:
        it.manufacturer = m.group(1).strip()
    m = re.search(r"Страна происхождения:?\s*\n?\s*([А-ЯA-Z][^\n/]+)", text)
    if m:
        it.country = m.group(1).strip()
    latin = [x.strip() for x in _LATIN.findall(it.name) if len(x.strip()) > 2]
    latin = [x for x in latin if not re.fullmatch(r"(rh)?[A-Z]{1,3}\d?", x)]
    if latin and not re.match(r"^rh", latin[0]):
        words = latin[0].split()
        it.brand = words[0].capitalize() if words[0].isupper() else words[0]
        if len(words) > 1:
            it.model = " ".join(words[1:])
        if it.manufacturer and it.manufacturer.lower() in latin[0].lower():
            it.brand = it.manufacturer
    it.name = re.split(r"\s+Производитель", it.name)[0].strip()
    it.category = _category(it)
    for r in it.requirements:
        if r.key == "amount" and r.constraint.op in (Op.GE, Op.EQ, Op.GT) and r.constraint.value:
            it.pack_amount, it.pack_unit = r.constraint.value, r.constraint.unit
        if r.key == "sheets_per_pack" and r.constraint.value:
            it.pack_amount, it.pack_unit = r.constraint.value, "sheet"
    it.unit = it.unit.strip() or "шт"
    if canon_qty_unit(it.unit) is None:
        it.unit = "шт"


def _category(it: Item) -> str | None:
    cats = rules().get("categories", {})
    best, best_len = None, -1
    for cid, c in cats.items():
        for pref in c.get("okpd2", []):
            if it.okpd2 and it.okpd2.startswith(pref) and len(pref) > best_len:
                best, best_len = cid, len(pref)
        for kw in c.get("keywords", []):
            if kw.lower() in it.name.lower() and best is None:
                best = cid
    return best or "generic"
