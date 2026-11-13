import re
from datetime import date

from .config import rules
from .models import Decision, Item, Offer
from .units import canon_qty_unit, canon_unit, to_float
from .words import rub


def _amount_ug(text: str) -> float | None:
    t = text.lower().replace("µ", "мк").replace("μ", "мк")
    best = None
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*(мкг|ug|мг|mg)\b", t):
        x = to_float(m.group(1))
        if x is None:
            continue
        u, f = canon_unit(m.group(2))
        if u == "ug":
            v = x * f
            if best is None or v > best:
                best = v if v < 100000 else best
    return best


def _sheets(text: str) -> tuple[float | None, float]:
    t = text.lower()
    packs = 1.0
    m = re.search(r"(\d+)\s*(?:пач|уп\b|упак)", t) or re.search(r"количество:\s*(\d+)\s*штук", t)
    if m and 1 < int(m.group(1)) <= 50:
        packs = float(m.group(1))
    m = re.search(r"(\d{3,5})\s*(?:л\b|л\.|лист)", t) or re.search(r"(\d{1,5})\s*лист", t)
    sheets = float(m.group(1)) if m else None
    if sheets and packs > 1 and sheets in (packs * 500, packs * 250):
        return sheets / packs, packs
    return sheets, packs


def index_factor(d: date | None, today: date | None = None) -> tuple[float, str | None]:
    """Цена контракта переводится в цены последнего опубликованного месяца: перемножаются месячные ИПЦ Росстата
    с месяца после заключения. Годовой индекс из настроек нужен только для месяцев, которых в ряду ещё нет."""
    conf = rules().get("indexation", {})
    if not d or not conf.get("enabled"):
        return 1.0, None
    today = today or date.today()
    monthly = {str(k): float(v) for k, v in (conf.get("cpi_monthly") or {}).items()}
    yearly = {int(k): float(v) for k, v in (conf.get("cpi") or {}).items()}
    f, used, guessed = 1.0, [], 0
    y, mo = d.year, d.month
    while True:
        mo += 1
        if mo > 12:
            mo, y = 1, y + 1
        if (y, mo) >= (today.year, today.month):
            break
        key = f"{y}-{mo:02d}"
        if key in monthly:
            f *= monthly[key] / 100
            used.append(key)
        elif y in yearly:
            f *= (1 + yearly[y] / 100) ** (1 / 12)
            guessed += 1
    if not used and not guessed:
        return 1.0, None
    span = f"{used[0][5:]}.{used[0][:4]}–{used[-1][5:]}.{used[-1][:4]}" if used else ""
    note = f"индексация по ИПЦ Росстата за {span} ({len(used)} мес.)" if used else "индексация"
    if guessed:
        note += f", ещё {guessed} мес. по годовому индексу из настроек"
    return f, note + ": ×" + f"{f:.4f}".replace(".", ",")


def normalize(item: Item, offer: Offer, dec: Decision, today: date | None = None) -> None:
    cat = rules().get("categories", {}).get(item.category or "generic", {})
    basis = cat.get("price_basis", "unit")
    price = offer.price
    notes: list[str] = []
    text = f"{offer.title} {offer.text}"
    if (offer.currency or "RUB").upper() not in ("RUB", "RUR", "₽", "РУБ"):
        dec.included = False
        dec.reasons.append(("currency", f"цена в {offer.currency}, а не в рублях; пересчёт по курсу методикой не предусмотрен"))
        return

    if basis == "amount" and item.pack_amount:
        amt = _amount_ug(text)
        if not amt:
            dec.included = False
            dec.reasons.append(("no_pack_size", "не указана фасовка (мкг) — нельзя привести цену к единице ТЗ"))
            return
        per = price / amt
        price = per * item.pack_amount
        notes.append(f"фасовка {amt:g} мкг → {rub(per)}/мкг × {item.pack_amount:g} мкг".replace(".", ","))
    elif basis == "pack":
        sheets, packs = _sheets(text)
        want = item.pack_amount or 500
        if packs > 1:
            price = price / packs
            notes.append(f"упаковка из {packs:g} пачек → цена за пачку")
        if sheets and abs(sheets - want) / want > 0.02:
            price = price * want / sheets
            notes.append(f"{sheets:g} листов в пачке → пересчёт на {want:g}")
    else:
        u_item = canon_qty_unit(item.unit)
        u_off = canon_qty_unit(offer.unit) if offer.unit else u_item
        if u_off and u_item and u_off != u_item and ({u_off, u_item} - {"pcs", "pack", "set"} or offer.kind == "contract"):
            dec.included = False
            dec.reasons.append(("unit_mismatch", f"единица «{offer.unit}» не приводится к «{item.unit}»"))
            return

    f, note = index_factor(offer.doc_date, today) if offer.kind == "contract" else (1.0, None)
    if note:
        price *= f
        notes.append(note)
        dec.factor = f
    dec.unit_price = round(price, 2)
    dec.normalized_note = "; ".join(notes) or None
