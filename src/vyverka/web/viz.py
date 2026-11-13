"""Линейка цен: все найденные цены позиции на одной шкале, зона однородности и стрелка средней."""

import math
from html import escape

from ..models import ItemResult
from ..words import plural

PAD_L, PAD_R = 24, 24
BASE = 104


def _nice(lo: float, hi: float, n: int = 6) -> list[float]:
    span = hi - lo
    if span <= 0:
        return [lo]
    raw = span / n
    mag = 10 ** math.floor(math.log10(raw))
    step = min((s * mag for s in (1, 2, 2.5, 5, 10) if s * mag >= raw), default=raw)
    start = math.ceil(lo / step) * step
    out = []
    x = start
    while x <= hi + 1e-9:
        out.append(x)
        x += step
    return out


def _short(x: float) -> str:
    if x >= 1_000_000:
        return f"{x / 1_000_000:.2f}".rstrip("0").rstrip(".").replace(".", ",") + " млн"
    if x >= 10_000:
        return f"{x / 1000:.0f} тыс."
    return f"{x:,.0f}".replace(",", "\u00a0")


def ruler(res: ItemResult, limit_cv: float = 33.0, W: int = 960, H: int = 150) -> str:
    dec = {d.offer_id: d for d in res.decisions}
    pts = []
    for o in res.offers:
        d = dec[o.id]
        if d.unit_price:
            code = d.reasons[-1][0] if d.reasons else ""
            kind = "in" if d.included else ("out" if code == "outlier" else "off")
            pts.append((d.unit_price, kind, o, d))
    if not pts:
        return ""
    c = res.calc
    vals = [p[0] for p in pts if p[1] != "off"] or [p[0] for p in pts]
    center = c.mean or sorted(vals)[len(vals) // 2]
    lo = min(min(vals), center * (1 - limit_cv / 100 * 1.6))
    hi = max(max(vals), center * (1 + limit_cv / 100 * 1.6))
    lo = max(0.0, lo)

    def x(v: float) -> float:
        v = min(max(v, lo), hi)
        return PAD_L + (v - lo) / (hi - lo) * (W - PAD_L - PAD_R)

    s = [f'<svg class="ruler" viewBox="0 0 {W} {H}" role="img" aria-label="Линейка цен позиции {res.item.pos}: '
         f'{len(pts)} цен, в расчёте {sum(1 for p in pts if p[1] == "in")}">']
    if c.mean:
        a, b = x(c.mean * (1 - limit_cv / 100)), x(c.mean * (1 + limit_cv / 100))
        s.append(f'<rect class="r-allow" x="{a:.1f}" y="18" width="{b - a:.1f}" height="{BASE - 18}"/>')
        s.append(f'<text class="r-note" x="{a + 6:.1f}" y="32">допуск V ≤ {limit_cv:g} %</text>')
        if c.std:
            a2, b2 = x(c.mean - c.std), x(c.mean + c.std)
            s.append(f'<rect class="r-sigma" x="{a2:.1f}" y="{BASE - 10}" width="{b2 - a2:.1f}" height="10"/>')
    s.append(f'<line class="r-base" x1="{PAD_L}" y1="{BASE}" x2="{W - PAD_R}" y2="{BASE}"/>')
    for t in _nice(lo, hi):
        tx = x(t)
        s.append(f'<line class="r-tick" x1="{tx:.1f}" y1="{BASE}" x2="{tx:.1f}" y2="{BASE + 7}"/>')
        s.append(f'<text class="r-lab" x="{tx:.1f}" y="{BASE + 22}">{_short(t)}</text>')
    steps = 40
    for i in range(steps + 1):
        tx = PAD_L + i * (W - PAD_L - PAD_R) / steps
        s.append(f'<line class="r-tick r-minor" x1="{tx:.1f}" y1="{BASE}" x2="{tx:.1f}" y2="{BASE + 3}"/>')

    lanes: dict[int, list[float]] = {}
    order = sorted(pts, key=lambda p: (p[1] != "in", p[0]))
    for i, (v, kind, o, d) in enumerate(order):
        px = x(v)
        lane = 0
        while any(abs(px - q) < 11 for q in lanes.get(lane, [])):
            lane += 1
        lanes.setdefault(lane, []).append(px)
        py = BASE - 12 - lane * 12
        if py < 22:
            py = 22
        why = escape(d.reasons[-1][1] if d.reasons and not d.included else (d.reasons[0][1] if d.reasons else ""))
        tip = escape(f"{o.source_title}: {o.title[:80]}") + f" — {_short(v)} ₽. {why}"
        s.append(f'<a href="#o-{escape(_anchor(o.id))}" class="r-pt r-{kind}" style="--d:{i * 40}ms">'
                 f'<title>{tip}</title><circle class="r-hit" cx="{px:.1f}" cy="{py:.1f}" r="22"/><circle cx="{px:.1f}" cy="{py:.1f}" r="5"/></a>')
    if c.unit_price:
        mx = x(c.unit_price)
        s.append(f'<line class="r-needle" x1="{mx:.1f}" y1="10" x2="{mx:.1f}" y2="{BASE + 2}"/>')
        anchor = "end" if mx > W * 0.7 else "start"
        dx = -8 if anchor == "end" else 8
        s.append(f'<text class="r-needle-lab" x="{mx + dx:.1f}" y="14" text-anchor="{anchor}">среднее '
                 f'{_short(c.unit_price)} ₽</text>')
    s.append("</svg>")
    return "".join(s)


def _anchor(oid: str) -> str:
    return "".join(ch if ch.isalnum() else "-" for ch in oid)


def cloud(res: ItemResult, S: int = 420) -> str:
    """Облако похожих: в центре позиция ТЗ, расстояние до центра — непохожесть, кольца — идентичные,
    однородные и дальние аналоги. Похожие друг на друга предложения ложатся рядом (MDS)."""
    if not res.analogs:
        return ""
    dec = {d.offer_id: d for d in res.decisions}
    offers = {o.id: o for o in res.offers}
    c = S / 2
    R = S / 2 - 18
    s = [f'<svg class="cloud" viewBox="0 0 {S} {S}" role="img" aria-label="Облако похожих товаров позиции {res.item.pos}: '
         f'{plural(len(res.analogs), "предложение", "предложения", "предложений")}">']
    for name, r, lab in (("distant", 1.0, "дальние"), ("homogeneous", 0.45, "однородные"), ("identical", 0.2, "идентичные")):
        s.append(f'<circle class="c-ring c-{name}" cx="{c}" cy="{c}" r="{R * r:.1f}"/>')
        s.append(f'<text class="c-lab" x="{c}" y="{c - R * r + 13:.1f}">{lab}</text>')
    near = set((res.estimate or {}).get("neighbors", []))
    for i, a in enumerate(sorted(res.analogs, key=lambda a: a.similarity)):
        o, d = offers.get(a.offer_id), dec.get(a.offer_id)
        if not o or not d:
            continue
        px, py = c + a.x * R, c + a.y * R
        kind = "in" if d.included else ("near" if a.offer_id in near else "off")
        price = f" — {_short(a.price)} ₽" if a.price else ""
        tip = escape(f"{o.title[:90]}{price}. Близость {a.similarity * 100:.0f} %")
        if a.differs:
            tip += escape(". Отличия: " + "; ".join(a.differs[:2]))
        rr = 4 + 5 * a.similarity
        s.append(f'<a href="#o-{escape(_anchor(o.id))}" class="c-pt c-{kind}" style="--d:{i * 25}ms"><title>{tip}</title>'
                 f'<circle cx="{px:.1f}" cy="{py:.1f}" r="{rr:.1f}"/></a>')
    s.append(f'<g class="c-target"><circle cx="{c}" cy="{c}" r="7"/><circle cx="{c}" cy="{c}" r="13" class="c-halo"/></g>')
    s.append("</svg>")
    return "".join(s)
