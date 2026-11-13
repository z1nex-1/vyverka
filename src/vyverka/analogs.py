"""Облако похожих товаров и оценка цены по аналогам.

Точно такого же товара в открытых источниках может не быть: уникальный прибор, реактив другой фасовки,
снятая с производства модель. Тогда закупщику нужны ближайшие однородные товары и понимание, чем они
отличаются. Каждое найденное предложение, включая отклонённые, получает меру близости к позиции ТЗ из
четырёх частей: характеристики (с учётом того, насколько промахнулось значение), текст (TF-IDF по
символьным триграммам), классификатор ОКПД2 и марка/модель. Облако строится классическим
многомерным шкалированием: похожие друг на друга предложения ложатся рядом, расстояние до центра —
непохожесть на ТЗ.

Оценка по аналогам — взвешенные k ближайших соседей; если среди соседей цена заметно зависит от
одной числовой характеристики (объём печати, фасовка), делается поправка по степенной регрессии
log(цена) ~ log(характеристика). Это справочная величина: ориентир для запроса КП и проверка
разумности НМЦД, в формальный расчёт по Приказу № 567 она не входит."""

import math
import re
from collections import Counter

from .config import rules
from .match import tokens
from .models import Analog, Decision, Item, ItemResult, Offer, Op
from .normalize import normalize

RINGS = (("identical", 0.8), ("homogeneous", 0.55), ("distant", 0.0))
# Отличия, после которых товар уже не аналог: другое вещество, другой вид, расходник, запрет правила категории.
HARD = {"other_product", "other_species", "accessory", "excluded_rule", "penalties"}
RING_TEXT = {"identical": "идентичный", "homogeneous": "однородный", "distant": "дальний аналог"}


def conf() -> dict:
    base = {"k": 5, "min_similarity": 0.45, "weights": {"attrs": 0.45, "text": 0.3, "code": 0.1, "identity": 0.15}}
    base.update(rules().get("analogs", {}))
    return base


def _grams(s: str) -> Counter:
    out: Counter = Counter()
    for w in tokens(s):
        w = f"#{w}#"
        out.update(w[i:i + 3] for i in range(len(w) - 2))
    return out


def _tfidf(docs: list[Counter]) -> list[dict[str, float]]:
    n = len(docs)
    df: Counter = Counter()
    for d in docs:
        df.update(d.keys())
    out = []
    for d in docs:
        v = {g: (1 + math.log(c)) * math.log((1 + n) / (1 + df[g])) + 1e-9 for g, c in d.items()}
        norm = math.sqrt(sum(x * x for x in v.values())) or 1.0
        out.append({g: x / norm for g, x in v.items()})
    return out


def _cos(a: dict[str, float], b: dict[str, float]) -> float:
    if len(a) > len(b):
        a, b = b, a
    return sum(x * b.get(g, 0.0) for g, x in a.items())


def item_text(item: Item) -> str:
    parts = [item.name, item.brand or "", item.model or ""]
    parts += [f"{r.name} {r.raw}" for r in item.requirements if r.essential]
    return " ".join(parts)


def _magnitude(s: str | None) -> float | None:
    if not s:
        return None
    t = s.lower().replace(",", ".")
    m = re.search(r"(\d+(?:\.\d+)?)\s*[xх×*]\s*(\d+(?:\.\d+)?)(?:\s*[xх×*]\s*(\d+(?:\.\d+)?))?", t)
    if m:
        vals = [float(x) for x in m.groups() if x]
        return math.prod(vals)
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    return float(m.group(0)) if m else None


def _closeness(required: str, found: str | None) -> float:
    a, b = _magnitude(required), _magnitude(found)
    if a is None or b is None or a <= 0 or b <= 0:
        return 0.1
    return 0.8 * min(a, b) / max(a, b)


def attr_part(item: Item, dec: Decision) -> float | None:
    """Доля совпавших характеристик; промах по числу засчитывается частично (400 мм при 600 мм — ближе,
    чем 100 мм), промах по перечислению (А4 вместо А3, другой цвет) — ноль."""
    if not dec.checks:
        return None
    ops = {r.key: r.constraint.op for r in item.requirements}
    w = {"ok": 1.0, "partial": 0.6, "unknown": 0.35}
    vals = [w[c.status] if c.status in w else
            (0.0 if ops.get(c.key) in (Op.TEXT, Op.FLAG) else _closeness(c.required, c.found)) for c in dec.checks]
    return sum(vals) / len(vals)


def code_part(item: Item, offer: Offer) -> float:
    if not item.okpd2 or not offer.okpd2:
        return 0.5
    a, b = item.okpd2.split("."), offer.okpd2.split(".")
    same = 0
    for x, y in zip(a, b):
        if x != y:
            break
        same += 1
    return {0: 0.0, 1: 0.2, 2: 0.4, 3: 0.7}.get(same, 1.0)


def identity_part(item: Item, offer: Offer) -> float:
    text = f"{offer.title} {offer.text}".lower()
    if item.model and item.model.lower() in text:
        return 1.0
    if item.brand and item.brand.lower() in text:
        return 0.6
    return 0.3 if not (item.brand or item.model) else 0.0


def similarity(item: Item, offer: Offer, dec: Decision, text_sim: float) -> tuple[float, dict[str, float]]:
    w = dict(conf()["weights"])
    parts = {"text": round(text_sim, 3), "code": code_part(item, offer), "identity": identity_part(item, offer)}
    a = attr_part(item, dec)
    if a is not None:
        parts["attrs"] = round(a, 3)
    else:
        w.pop("attrs", None)
    if not (item.brand or item.model):
        w.pop("identity", None)
        parts.pop("identity", None)
    total = sum(w.values()) or 1.0
    sim = sum(w[k] * parts[k] for k in w if k in parts) / total
    if any(code in HARD for code, _ in dec.reasons):
        sim *= 0.4
    if any(c.status == "mismatch" and c.key in _categorical(item) for c in dec.checks):
        sim *= 0.6
    return round(sim, 3), parts


def _categorical(item: Item) -> set[str]:
    return {r.key for r in item.requirements if r.constraint.op in (Op.TEXT, Op.FLAG)}


def _price(item: Item, offer: Offer, dec: Decision) -> float | None:
    """Цена за единицу ТЗ и для отклонённых предложений: пересчёт фасовки без влияния на решение."""
    if dec.unit_price:
        return dec.unit_price
    tmp = Decision(offer_id=offer.id, included=True, score=0)
    try:
        normalize(item, offer, tmp)
    except Exception:  # noqa: BLE001 — оценка аналога не должна ронять расчёт
        return None
    return tmp.unit_price if tmp.included else None


def _mds(dist: list[list[float]]) -> list[tuple[float, float]]:
    """Классическое многомерное шкалирование в 2D степенным методом — без numpy, точек немного."""
    n = len(dist)
    if n < 3:
        return [(math.cos(i * 2.4), math.sin(i * 2.4)) for i in range(n)]
    d2 = [[x * x for x in row] for row in dist]
    rm = [sum(r) / n for r in d2]
    gm = sum(rm) / n
    b = [[-0.5 * (d2[i][j] - rm[i] - rm[j] + gm) for j in range(n)] for i in range(n)]
    vecs = []
    for k in range(2):
        v = [math.sin(i + 1.3 * k + 0.7) for i in range(n)]
        lam = 0.0
        for _ in range(80):
            u = [sum(b[i][j] * v[j] for j in range(n)) for i in range(n)]
            for pv, pl in vecs:
                dot = sum(a * c for a, c in zip(u, pv))
                u = [a - dot * c for a, c in zip(u, pv)]
            lam = math.sqrt(sum(x * x for x in u)) or 1e-12
            v = [x / lam for x in u]
        vecs.append((v, lam))
    return [(vecs[0][0][i] * math.sqrt(vecs[0][1]), vecs[1][0][i] * math.sqrt(vecs[1][1])) for i in range(n)]


def build(res: ItemResult) -> list[Analog]:
    item = res.item
    dec = {d.offer_id: d for d in res.decisions}
    offers = [o for o in res.offers if o.id in dec]
    if not offers:
        return []
    vecs = _tfidf([_grams(item_text(item))] + [_grams(f"{o.title} {o.text[:1500]}") for o in offers])
    target = vecs[0]
    out: list[Analog] = []
    for o, v in zip(offers, vecs[1:]):
        d = dec[o.id]
        sim, parts = similarity(item, o, d, _cos(target, v))
        d.similarity = sim
        ring = next(name for name, lo in RINGS if sim >= lo)
        differs = [f"{c.name}: {c.found or 'не указано'} (нужно {c.required})"
                   for c in d.checks if c.status in ("mismatch", "unknown")][:4]
        out.append(Analog(offer_id=o.id, similarity=sim, parts=parts, ring=ring, differs=differs,
                          price=_price(item, o, d)))
    # Расстояния между предложениями: текст и характеристики, чтобы похожие друг на друга ложились рядом.
    pts = [target] + vecs[1:]
    attrs = [1.0] + [a.parts.get("attrs", a.similarity) for a in out]
    n = len(pts)
    dist = [[0.0] * n for _ in range(n)]
    for i in range(n):
        for j in range(i + 1, n):
            dd = 0.6 * (1 - _cos(pts[i], pts[j])) + 0.4 * abs(attrs[i] - attrs[j])
            dist[i][j] = dist[j][i] = dd
    xy = _mds(dist)
    cx, cy = xy[0]
    raw = [math.atan2(y - cy, x - cx) if (x, y) != (cx, cy) else 0.0 for x, y in xy[1:]]
    # Проекция задаёт только порядок по кругу: похожие друг на друга товары остаются рядом,
    # но точки не слипаются, когда все они одинаково далеки от ТЗ.
    order = sorted(range(len(out)), key=lambda i: raw[i])
    step = 2 * math.pi / max(len(out), 1)
    for k, i in enumerate(order):
        a = out[i]
        ang = raw[order[0]] + k * step
        r = min(0.97, 1 - a.similarity)
        a.x, a.y = round(r * math.cos(ang), 4), round(r * math.sin(ang), 4)
    out.sort(key=lambda a: -a.similarity)
    return out


def _target_magnitude(item: Item, key: str) -> float | None:
    req = next((r for r in item.requirements if r.key == key), None)
    if not req:
        return None
    c = req.constraint
    if c.value is not None:
        return c.value
    if c.low is not None:
        return c.low
    return _magnitude(c.text or req.raw)


def estimate(res: ItemResult, analogs: list[Analog]) -> dict | None:
    cf = conf()
    dec = {d.offer_id: d for d in res.decisions}
    pool = [a for a in analogs if a.price and a.similarity >= cf["min_similarity"]
            and not any(r[0] in HARD | {"low_trust"} for r in dec[a.offer_id].reasons)
            and not any(c.status == "mismatch" and c.key in _categorical(res.item) for c in dec[a.offer_id].checks)]
    if len(pool) < 2:
        return None
    # Прошедшие проверку сопоставимости предложения надёжнее: у контрактов ЕИС характеристик в карточке мало,
    # и по тексту они ближе к ТЗ, чем выглядят.
    rank = {a.offer_id: a.similarity + (0.2 if dec[a.offer_id].included else 0.0) for a in pool}
    pool.sort(key=lambda a: -rank[a.offer_id])
    k = int(cf["k"])
    near = pool[:k]
    w = [rank[a.offer_id] ** 2 * (dec[a.offer_id].trust or 100) / 100 for a in near]
    base = sum(wi * a.price for wi, a in zip(w, near)) / sum(w)
    out = {"value": round(base, 2), "k": len(near), "neighbors": [a.offer_id for a in near],
           "low": round(min(a.price for a in near), 2), "high": round(max(a.price for a in near), 2),
           "method": f"взвешенное среднее {len(near)} ближайших аналогов (вес — квадрат близости × доверие)",
           "similarity": round(sum(a.similarity for a in near) / len(near), 3)}
    adj = _hedonic(res, pool[:12])
    if adj:
        key, name, b, r2, n, tgt, mags = adj
        prices = [a.price * (tgt / mags[a.offer_id]) ** b if a.offer_id in mags else a.price for a in near]
        val = sum(wi * p for wi, p in zip(w, prices)) / sum(w)
        out.update(value=round(val, 2), adjusted_from=round(base, 2), attr=name, elasticity=round(b, 3),
                   r2=round(r2, 3), n_fit=n,
                   method=out["method"] + f", поправка по характеристике «{name}»: цена ∝ x^{b:.2f}, R² = {r2:.2f}")
    if res.calc.status == "ok" and res.calc.unit_price:
        out["deviation_pct"] = round((res.calc.unit_price - out["value"]) / out["value"] * 100, 1)
    return out


def _hedonic(res: ItemResult, pool: list[Analog]):
    """Степенная регрессия цены по одной числовой характеристике ТЗ, которая сильнее всего связана с ценой."""
    dec = {d.offer_id: d for d in res.decisions}
    best = None
    for req in res.item.requirements:
        tgt = _target_magnitude(res.item, req.key)
        if not tgt or tgt <= 0:
            continue
        mags: dict[str, float] = {}
        for a in pool:
            c = next((c for c in dec[a.offer_id].checks if c.key == req.key and c.found), None)
            m = _magnitude(c.found) if c else None
            if m and m > 0:
                mags[a.offer_id] = m
        pts = [(math.log(mags[a.offer_id]), math.log(a.price)) for a in pool if a.offer_id in mags]
        if len(pts) < 4 or len({round(x, 3) for x, _ in pts}) < 3:
            continue
        mx = sum(x for x, _ in pts) / len(pts)
        my = sum(y for _, y in pts) / len(pts)
        sxx = sum((x - mx) ** 2 for x, _ in pts)
        sxy = sum((x - mx) * (y - my) for x, y in pts)
        syy = sum((y - my) ** 2 for _, y in pts)
        if sxx <= 0 or syy <= 0:
            continue
        b = sxy / sxx
        r2 = sxy * sxy / (sxx * syy)
        if r2 >= 0.5 and 0 < b < 3 and (best is None or r2 > best[3]):
            best = (req.key, req.name, b, r2, len(pts), tgt, mags)
    return best


def attach(res: ItemResult) -> None:
    res.analogs = build(res)
    res.estimate = estimate(res, res.analogs)
