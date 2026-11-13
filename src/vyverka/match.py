import re

from .config import attribute_keys, rules
from .models import AttrCheck, Decision, Item, Offer, Op, Requirement
from .units import canon_unit, to_float
from .words import plural

_STOP = {"для", "и", "в", "на", "с", "из", "по", "товар", "шт", "человека", "белок", "the", "of"}
_TR = str.maketrans({"a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "k": "к", "m": "м", "t": "т",
                     "b": "в", "h": "н", "y": "у"})


def tokens(s: str) -> set[str]:
    s = s.lower().replace("ё", "е")
    s = re.sub(r"[^a-zа-я0-9]+", " ", s)
    return {w for w in s.split() if w not in _STOP and len(w) > 1}


def name_similarity(a: str, b: str) -> float:
    ta, tb = tokens(a), tokens(b)
    if not ta or not tb:
        return 0.0
    stems_a = {w[:5] for w in ta}
    stems_b = {w[:5] for w in tb}
    return len(stems_a & stems_b) / len(stems_a)


def _candidates(req: Requirement, offer: Offer) -> list[str]:
    """Значения характеристики в предложении, от самого надёжного: точное имя, синоним, окно текста."""
    syn = [x for x in attribute_keys().get(req.key, []) if len(x) > 3]
    name = req.name.lower()
    want_unit = req.constraint.unit
    ranked: list[tuple[int, str]] = []
    for k, v in offer.attributes.items():
        head, _, key_unit = k.partition(",")
        if want_unit and key_unit.strip() and canon_unit(key_unit)[0] != want_unit:
            continue
        kl = re.sub(r"\s+", " ", re.sub(r"[^a-zа-я0-9 ]+", " ", head.lower().replace("ё", "е"))).strip()
        if not v or len(kl) < 3:
            continue
        if kl == name or name.startswith(kl) and len(kl) > 8:
            ranked.append((0, v))
        elif any(sy == kl or kl.startswith(sy) for sy in syn):
            ranked.append((1, v))
        elif any(sy in kl for sy in syn):
            ranked.append((2, v))
    out = [v for _, v in sorted(ranked, key=lambda x: x[0])]
    if ai := (offer.meta.get("ai") or {}).get(req.name):
        out.insert(0, ai["value"])
    text = offer.text.lower()
    for sy in [name] + syn:
        i = text.find(sy)
        if i >= 0:
            frag = re.sub(r"^[^0-9a-zа-я≥≤<>]*", "", text[i + len(sy):i + len(sy) + 40])
            lead = re.match(r"([a-zа-я°%][a-zа-я0-9°%/²³.]*)", frag)
            if want_unit and lead and canon_unit(lead.group(1))[0] not in (want_unit, lead.group(1).rstrip(".")):
                continue
            if want_unit and lead and re.search(r"\d", lead.group(1)):
                continue
            if frag:
                out.append(frag.split(";")[0].split(",")[0].strip())
    return out


def _find_value(req: Requirement, offer: Offer) -> str | None:
    c = _candidates(req, offer)
    return c[0] if c else None


_BOUND = re.compile(r"(≥|>=|≤|<=|>|<|не менее|не более|более|менее|свыше|от|до)?\s*(-?\d+(?:[.,]\d+)?)", re.I)


def interval(found: str, unit: str | None) -> tuple[float, float] | None:
    """«≥ 0,05 мм», «> 200», «≥ 0,3 и < 0,5 мм», «от 2 до 8», «420 °C» → [lo, hi] в единицах ТЗ.
    Так записаны характеристики КТРУ в ЕИС: не точкой, а диапазоном."""
    t = found.lower().replace("−", "-").replace("℃", "°c")
    t = re.sub(r"площад\w*\s*1\s*(?:м2|м²|кв\.?\s*м)\.?", " ", t)
    t = re.sub(r"(?<=[a-zа-я])[23²³](?![\d.,])", "", t)
    lo, hi, any_num = float("-inf"), float("inf"), False
    # в диапазоне «≥ 1,5 и < 2 метр» единица стоит после последнего числа
    last = re.search(r"\d\s*([a-zа-я°%μµ][a-zа-я°%μµ/.²³23]*)[^\d]*$", t)
    tail = last.group(1) if last else re.sub(r"^[^a-zа-я°%μµ]*", "", re.sub(r"[\d.,≥≤<>=\s-]+", " ", t, count=1)).strip()[:10]
    u, f = canon_unit(tail) if tail else (unit, 1.0)
    f = f if (u == unit or not unit) else 1.0
    rng = re.match(r"\s*(?:от\s*)?(-?\d+(?:[.,]\d+)?)\s*[a-zа-я°]*\s*(?:[–~]|-(?=\s*\d)|\s+до\s+)\s*(-?\d+(?:[.,]\d+)?)", t)
    if rng:
        a, b = to_float(rng.group(1)), to_float(rng.group(2))
        if a is not None and b is not None:
            return min(a, b) * f, max(a, b) * f
    lst = re.match(r"\s*(\d+(?:[.,]\d+)?)\s*[a-zа-я°]*\s*(?:/\s*(\d+(?:[.,]\d+)?)\s*[a-zа-я°]*)+", t)
    if lst:
        nums = [to_float(x) for x in re.findall(r"\d+(?:[.,]\d+)?", lst.group(0))]
        nums = [x * f for x in nums if x is not None]
        if len(nums) > 1:
            return min(nums), max(nums)
    for m in _BOUND.finditer(t):
        x = to_float(m.group(2))
        if x is None:
            continue
        x *= f
        op = (m.group(1) or "").lower()
        any_num = True
        if op in ("≥", ">=", "не менее", "от", "более", "свыше", ">"):
            lo = max(lo, x)
        elif op in ("≤", "<=", "не более", "до", "менее", "<"):
            hi = min(hi, x)
        else:
            if lo == float("-inf") and hi == float("inf"):
                lo = hi = x
            break
        if lo != float("-inf") and hi != float("inf"):
            break
    return (lo, hi) if any_num else None


def _need(req: Requirement) -> tuple[float, float]:
    c = req.constraint
    v = c.value if c.value is not None else 0.0
    inf = float("inf")
    peaks = rules().get("comparability", {}).get("peak_prefixes", ["максимальн"])
    lows = rules().get("comparability", {}).get("le_prefixes", [])
    if c.op == Op.EQ:
        if not c.tolerance and any(req.name.lower().startswith(p) for p in peaks):
            return v, inf
        if not c.tolerance and any(req.name.lower().startswith(p) for p in lows):
            return -inf, v
        pct = rules().get("comparability", {}).get("tolerance_pct", {}).get(req.key)
        if not c.tolerance and pct:
            return v * (1 - pct / 100), v * (1 + pct / 100)
        t = c.tolerance or 0.0
        return v - t, v + t
    return {Op.GE: (v, inf), Op.GT: (v, inf), Op.LE: (-inf, v), Op.LT: (-inf, v)}.get(
        c.op, (c.low if c.low is not None else -inf, c.high if c.high is not None else inf))


def _fmt_iv(lo: float, hi: float, unit: str | None) -> str:
    f = lambda x: f"{x:g}".replace(".", ",")
    u = f" {unit}" if unit else ""
    if lo == hi:
        return f"{f(lo)}{u}"
    if hi == float("inf"):
        return f"≥ {f(lo)}{u}"
    if lo == float("-inf"):
        return f"≤ {f(hi)}{u}"
    return f"{f(lo)}–{f(hi)}{u}"


def _numbers_with_unit(text: str, unit: str | None) -> list[float]:
    out = []
    for m in re.finditer(r"(\d+(?:[.,]\d+)?)\s*([a-zA-Zа-яА-Я°%μµ/²2\.]{1,8})", text):
        u, f = canon_unit(m.group(2))
        if unit and u == unit:
            x = to_float(m.group(1))
            if x is not None:
                out.append(x * f)
    return out


_RES = re.compile(r"(?<!\d)(\d{3,5})\s*[xх×*]\s*(\d{3,5})(?!\d)")


def check(req: Requirement, offer: Offer) -> AttrCheck:
    c = req.constraint
    found = _find_value(req, offer)
    base = dict(key=req.key, name=req.name, required=c.describe())
    if c.text and (want := _RES.fullmatch(c.text.strip())):
        w = (want.group(1), want.group(2))
        got = {(a, b) for a, b in _RES.findall(f"{offer.title} {offer.text} {' '.join(offer.attributes.values())}")
               if min(int(a), int(b)) >= 240}  # 100x100 и 75x75 — крепление VESA, а не разрешение
        if w in got:
            return AttrCheck(**base, found="x".join(w), status="ok")
        if got:
            return AttrCheck(**base, found=", ".join("x".join(g) for g in sorted(got)[:2]), status="mismatch")
        return AttrCheck(**base, found=None, status="unknown")
    want_set = _num_set(req.raw)
    if len(want_set) >= 3:
        for cand in _candidates(req, offer)[:2]:
            got = _num_set(cand, listed=False)
            iv = interval(cand, c.unit) if re.search(r"[≥≤<>]|не (?:менее|более)|от|до", cand) else None
            if iv and iv[0] != iv[1]:
                inside = {w for w in want_set if iv[0] - 1e-9 <= w <= iv[1] + 1e-9}
                status = "ok" if inside == want_set else ("partial" if inside else "mismatch")
                return AttrCheck(**base, found=cand[:60], status=status)
            if got:
                status = "ok" if want_set <= got else ("partial" if want_set & got else "mismatch")
                return AttrCheck(**base, found=cand[:60], status=status)
        return AttrCheck(**base, found=None, status="unknown")
    if c.op in (Op.EQ, Op.GE, Op.GT, Op.LE, Op.LT, Op.RANGE):
        need_lo, need_hi = _need(req)
        ivs = [iv for iv in (interval(x, c.unit) for x in _candidates(req, offer)[:3]) if iv]
        if not ivs and c.unit:
            # Число без названия характеристики может относиться к чему угодно в тексте (диаметр нити вместо
            # толщины слоя), поэтому оно только подтверждает требование, но не служит причиной отказа.
            vals = [v for v in set(_numbers_with_unit(offer.text, c.unit)) if need_lo - 1e-9 <= v <= need_hi + 1e-9]
            if len(vals) == 1:
                return AttrCheck(**base, found=_fmt_iv(vals[0], vals[0], c.unit), status="ok")
        if not ivs:
            return AttrCheck(**base, found=None, status="unknown")
        lo, hi = ivs[0]
        eps = 1e-9
        inf = float("inf")
        if need_hi == inf and need_lo > -inf and hi < inf:
            lo = hi
        elif need_lo == -inf and need_hi < inf and lo > -inf:
            hi = lo
        finite = lo > -inf and hi < inf
        if lo >= need_lo - eps and hi <= need_hi + eps:
            status = "ok"
        elif finite and lo <= need_lo + eps and hi >= need_hi - eps:
            status = "ok"
        elif hi < need_lo - eps or lo > need_hi + eps:
            status = "mismatch"
        else:
            status = "partial"
        return AttrCheck(**base, found=_fmt_iv(lo, hi, c.unit), status=status)
    if c.op == Op.TEXT and c.text:
        want = c.text.lower()
        if re.fullmatch(r"[\d.x]+", want):
            return _dims(req, offer, want, base)
        if found:
            found = re.split(r"\s(?:страна|производ|товарный)", found, maxsplit=1)[0][:40].strip(" ;,.")
        if found and found.lower() in ("да", "нет", "есть", "yes", "no"):
            found = None
        hay = _stem_text(f"{found or ''} {offer.title} {offer.text}".lower().translate(_TR))
        hay = re.sub(r"(?<![a-zа-я0-9])([a-zа-я])[\s-](\d)(?!\d|[.,]\d)", r"\1\2", hay)
        w = _stem_text(want.translate(_TR))
        key_tokens = [t for t in re.findall(r"[a-zа-я0-9][a-zа-я0-9\-]*", w) if len(t) > 1 or t in "асвд"]
        if not key_tokens:
            return AttrCheck(**base, found=found, status="unknown")
        if len(w) <= 4:
            hit = re.search(rf"(?<![a-zа-я0-9]){re.escape(w)}(?![a-zа-я0-9])", hay)
            if hit:
                return AttrCheck(**base, found=found or w, status="ok")
            sib = re.fullmatch(r"([a-zа-я])(\d+)", w)
            if sib:
                other = re.search(rf"(?<![a-zа-я0-9]){sib.group(1)}(\d+)(?![a-zа-я0-9])", hay)
                if other and other.group(1) != sib.group(2):
                    return AttrCheck(**base, found=other.group(0).upper(), status="mismatch")
            short_found = found and len(found.split()) <= 2 and len(found) <= 20
            return AttrCheck(**base, found=found, status="mismatch" if short_found else "unknown")
        if found:
            ft = [t for t in re.findall(r"[a-zа-я0-9]{3,}", _stem_text(found.lower().translate(_TR)))]
            if ft and any(t in key_tokens for t in ft):
                return AttrCheck(**base, found=found, status="ok")
            ft2 = re.findall(r"[a-zа-я0-9]{2,}", _stem_text(found.lower().translate(_TR)))
            if len(key_tokens) >= 3 and ft2 and all(t in key_tokens for t in ft2):
                return AttrCheck(**base, found=found, status="partial")
        share = sum(1 for t in key_tokens if t in hay) / len(key_tokens)
        if share >= 0.6:
            return AttrCheck(**base, found=found or c.text, status="ok")
        return AttrCheck(**base, found=found, status="mismatch" if found and share < 0.2 else "unknown")
    return AttrCheck(**base, found=found, status="unknown")


_EDGE_R = r"(?![a-z0-9])"


def _model_re(model: str) -> re.Pattern:
    parts = [re.escape(t) for t in re.split(r"[\s\-]+", model.lower()) if t]
    return re.compile(r"(?<![a-z0-9])" + r"[\s\-]*".join(parts) + _EDGE_R)


def _has_digit_neighbour(flat: str, model: str) -> bool:
    """«59a» внутри «cf259a» или «1059a» — другой артикул, а не та же модель."""
    i = flat.find(model)
    return i > 0 and flat[i - 1].isdigit()


def identity_mode(item: Item) -> str:
    mode = rules().get("categories", {}).get(item.category or "generic", {}).get("identity", "soft")
    if mode == "soft" and item.model and re.search(r"\d", item.model):
        return "strict"
    return mode


def identity(item: Item, offer: Offer) -> tuple[float, list[tuple[str, str]]]:
    cat = rules().get("categories", {}).get(item.category or "generic", {})
    mode = identity_mode(item)
    reasons: list[tuple[str, str]] = []
    text = f"{offer.title} {offer.text}".lower()
    sim = name_similarity(item.name, offer.title + " " + offer.text)
    item_text = " ".join([item.name] + [r.raw for r in item.requirements]).lower()
    for rule in cat.get("exclude", []):
        if re.search(rule["pattern"], offer.title if rule.get("title_only") else text, re.I):
            if rule.get("unless") and re.search(rule["unless"], item_text, re.I):
                continue
            if rule.get("when") and not re.search(rule["when"], item_text, re.I):
                continue
            return 0.0, [("excluded_rule", rule["reason"])]
    want_no = _sizes(item.name)
    got_no = _sizes(offer.title)
    if want_no and got_no and not want_no & got_no:
        return 0.0, [("other_model", f"другой типоразмер: №{', '.join(sorted(got_no))}, в ТЗ №{', '.join(sorted(want_no))}")]
    own = {re.sub(r"[\s\-]", "", c.lower()) for c in [item.model or "", *item.codes] if c}
    # после «для» идут совместимые устройства, это не другие товары в строке
    named = re.sub(r"\bдля\b[^;]*", " ", offer.title, flags=re.I)
    foreign = {c.lower().replace("-", "") for c in _CODE.findall(named)} - own
    if len(foreign) >= 4:
        return 0.0, [("bundle", f"сборная позиция: в одной строке {plural(len(foreign), 'другой артикул', 'других артикула', 'других артикулов')}, цена не относится к товару из ТЗ")]
    if mode == "strict" and item.model:
        model = re.sub(r"\s+", "", item.model.lower())
        flat = re.sub(r"\s+", "", text)
        brand_ok = not item.brand or item.brand.lower() in text
        exact = _model_re(item.model)
        glued = re.search(exact.pattern[:-len(_EDGE_R)] + r"([a-z]{1,3})(?![a-z0-9])", text)
        hit = exact.search(text) or any(_model_re(code).search(text) for code in item.codes)
        if item.brand and re.search(r"совместим|аналог|compatible", offer.title.lower()) and hit:
            return 0.5, [("equivalent", f"эквивалент: совместимый товар другого производителя вместо оригинала {item.brand} — другая модель")]
        own_head = (_ordered_tokens(item.name) or [""])[0][:5]
        if (re.search(r"\bдля\b.{0,60}" + re.escape(item.model.lower().split()[0]), offer.title.lower())
                and (_ordered_tokens(offer.title) or [""])[0][:5] != own_head):
            return 0.0, [("accessory", "комплектующие или расходник, а не сам товар")]
        if not hit and glued and brand_ok and not item.codes:
            return 0.0, [("other_model", f"другая модель: {item.brand or ''} {item.model}{glued.group(1).upper()}, в ТЗ {item.model}".replace("  ", " "))]
        if (hit or (model in flat and not glued and not _has_digit_neighbour(flat, model))) and brand_ok:
            title = re.sub(r"\s+", " ", offer.title.lower())
            m = re.search(re.escape(item.model.lower()) + r"((?:\s*(?:\d|plus|max|hs|hf|v\d|lite|mini)\b)+)", title)
            if m and m.group(1).strip():
                return 0.7, [("modification", f"модификация модели из ТЗ: {item.brand or ''} {item.model}{m.group(1).upper()}".replace("  ", " "))]
            return 1.0, reasons
        want = f"{item.brand or ''} {item.model}".strip()
        if item.brand and item.brand.lower() in offer.title.lower():
            after = re.split(re.escape(item.brand), offer.title, maxsplit=1, flags=re.I)[1]
            other = " ".join(re.sub(r"^[\s.,:;«»\"]+", "", after).split("(")[0].split()[:2]).strip(" .,;")
            if re.search(r"\d", other):
                return 0.0, [("other_model", f"другая модель: {item.brand} {other}, в ТЗ {item.model}")]
            return 0.0, [("other_model", f"указан только товарный знак {item.brand}, модель не названа, сопоставимость с {want} не подтверждается")]
        return 0.0, [("other_model", f"не {want}: другой производитель или модель не указана")]
    if mode == "target":
        target = _target_key(item.name)
        if target and target not in _target_key(offer.title + " " + offer.text, all_=True):
            return 0.0, [("other_product", f"другое вещество: нужен {target}")]
        human = "человек" in item.name.lower() or "human" in item.name.lower()
        # Вид белка — в названии или в поле «вид»; клетки для теста активности (мышиные CTLL-2) сюда не относятся.
        own = " ".join([offer.title] + [v for k, v in offer.attributes.items() if re.search(r"\bвид\b|species|организм", k.lower())])
        if human and re.search(r"\b(mouse|murine|мыши|мышин|rat|крыс)", own.lower()):
            return 0.0, [("other_species", "белок другого вида (не человека)")]
        return 1.0, reasons
    if item.okpd2 and offer.okpd2 and _okpd3(item.okpd2) != _okpd3(offer.okpd2):
        return 0.0, [("other_product", f"другой товар: ОКПД2 {offer.okpd2} вместо {item.okpd2}")]
    head = next(iter(_ordered_tokens(item.name)), None)
    if head and head[:5] not in {t[:5] for t in _ordered_tokens(offer.title)[:3]}:
        return 0.0, [("other_product", f"другой товар: «{offer.title[:40]}» — не «{head}»")]
    first = (_ordered_tokens(offer.title) or [""])[0]
    if head and first[:5] != head[:5] and re.search(r"\bдля\b[^,;]{0,40}" + re.escape(head[:5]), offer.title.lower()):
        return 0.0, [("accessory", f"комплектующие или расходник для товара, а не сам «{head}»")]
    if item.brand and item.model and item.brand.lower() in text:
        if not re.search(r"(?<![a-zа-я0-9])" + re.escape(item.model.lower()) + r"(?![a-zа-я0-9])", text):
            named = [n.strip() for n in re.findall(r"[«\"“]([^»\"”]{2,30})[»\"”]", offer.title)]
            other = next((n for n in named if item.brand.lower() not in n.lower()), None)
            if other:
                return 0.0, [("other_model", f"другая модель: {item.brand} {other}, в ТЗ {item.brand} {item.model}")]
            return 0.0, [("other_model", f"указан только товарный знак {item.brand}, модель {item.model} не названа")]
    score = min(1.0, sim * 1.2)
    if item.brand:
        if item.brand.lower() in text.translate(str.maketrans("", "")) or item.brand.lower().replace("copy", "сору") in text:
            score = max(score, 1.0)
        else:
            reasons.append(("equivalent", f"эквивалент: марка не {item.brand}"))
            score *= 0.8
    return score, reasons


_CODE = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]{1,4}-?\d{3,}[A-Za-z0-9]*(?![A-Za-z0-9])")
_SIZE = re.compile(r"№\s*(\d+(?:/\d+)?)((?:\s*(?:,|-|–|/|и)\s*\d+/\d+)*)")


def _sizes(s: str) -> set[str]:
    """Типоразмеры вида «№24/6, 26/6» или «№10» — у степлеров, скоб, бит и т. п. это разные товары."""
    out: set[str] = set()
    for m in _SIZE.finditer(s):
        out.add(m.group(1))
        out.update(re.findall(r"\d+/\d+", m.group(2)))
    return out


def _target_key(s: str, all_: bool = False) -> str | set[str] | None:
    s = s.lower().replace("β", "beta").replace("-", "").replace(" ", "")
    s = re.sub(r"трансформирующ\w*?факторростабета(\d)", r"tgfb\1", s)
    s = re.sub(r"(?<![а-яa-z])ил(\d{1,2})", r"il\1", s)
    found = set(re.findall(r"(il\d{1,2}|интерлейкин\d{1,2}|tgfbeta\d|tgfb\d|tgfβ\d|ifn[a-z]|tnf[a-z]?|gmcsf|gcsf)", s))
    norm = {re.sub(r"интерлейкин", "il", x).replace("tgfbeta", "tgfb") for x in found}
    if all_:
        return norm
    return sorted(norm)[0] if norm else None


def decide(item: Item, offer: Offer) -> Decision:
    conf = rules().get("comparability", {})
    reasons: list[tuple[str, str]] = []
    id_score, id_reasons = identity(item, offer)
    cat = rules().get("categories", {}).get(item.category or "generic", {})
    skip = {cat.get("pack_attr")} if cat.get("price_basis") in ("amount", "pack") else set()
    checks = [check(r, offer) for r in item.requirements if r.essential and r.key not in skip]
    mism = [c for c in checks if c.status == "mismatch"]
    okc = [c for c in checks if c.status == "ok"]
    part = [c for c in checks if c.status == "partial"]
    known = len(okc) + len(mism) + len(part)
    attr_score = ((len(okc) + 0.5 * len(part)) / len(checks)) if checks else 0.5
    if known == 0 and checks:
        attr_score = 0.3
    if (id_score == 0 and id_reasons and id_reasons[0][0] == "other_model" and checks and not mism
            and len(okc) / len(checks) >= conf.get("equivalent_share", 0.6)):
        id_score, id_reasons = 0.5, [("equivalent", f"эквивалент: другая модель, подтверждено {len(okc)} из {len(checks)} характеристик ТЗ")]
    w = conf.get("identity_weight", 0.35)
    score = round(w * id_score + (1 - w) * attr_score, 3)
    included = True
    if id_score == 0:
        included = False
        reasons += id_reasons
    else:
        reasons += [r for r in id_reasons if r[0] in ("equivalent", "modification")]
    if mism:
        included = False
        reasons.append(("spec_mismatch", "не соответствует ТЗ: " + "; ".join(
            f"{short(c.name)} {c.found} при требовании {c.required}" for c in mism[:3])))
    if offer.penalties:
        included = False
        reasons.append(("penalties", "по контракту начислялись неустойки"))
    mode = identity_mode(item)
    # модель из ТЗ заменяет проверку характеристик, только если ТЗ — на конкретный товар («target»)
    # или совпал артикул, или существенных характеристик нет; иначе «IdeaPad 15» с 4 ГБ прошёл бы за 16 ГБ
    flat = re.sub(r"[\s\-]", "", f"{offer.title} {offer.text}".lower())
    article = any(re.sub(r"[\s\-]", "", c.lower()) in flat for c in item.codes)
    exact_model = id_score >= 1.0 and (mode == "target" or bool(item.model) and mode == "strict"
                                       and (article or not checks))
    if included and score < conf.get("include_score", 0.6) and not (id_score >= 1.0 and (okc or exact_model)):
        included = False
        reasons.append(("low_similarity", f"мало подтверждённых характеристик ({len(okc)} из {len(checks)})"))
    if included:
        if okc or part:
            head = (f"сопоставимо: {len(okc)} из {len(checks)} существенных характеристик подтверждено"
                    + (f", {plural(len(part), 'совместима', 'совместимы', 'совместимы')} по диапазону" if part else ""))
        else:
            head = "сопоставимо по наименованию и составу; характеристики ТЗ в документе не указаны, противоречий нет"
        reasons.insert(0, ("comparable", head + (", " + id_reasons[0][1] if id_reasons else "")))
    return Decision(offer_id=offer.id, included=included, score=score, reasons=reasons, checks=checks)


def short(name: str, n: int = 48) -> str:
    s = re.split(r"\s*\(", name, maxsplit=1)[0].strip().lower()
    return s if len(s) <= n else s[: n - 1].rsplit(" ", 1)[0] + "…"


_END = re.compile(r"(ый|ий|ой|ая|яя|ое|ее|ые|ие|ого|его|ую|юю)\b")


def _stem_text(s: str) -> str:
    return _END.sub("", s)


def _okpd3(code: str) -> str:
    return ".".join(code.split(".")[:3])


def _ordered_tokens(s: str) -> list[str]:
    s = re.sub(r"[^a-zа-я0-9]+", " ", s.lower().replace("ё", "е"))
    return [w for w in s.split() if w not in _STOP and len(w) > 1 and not w.isdigit()]


_DIM = re.compile(r"(\d+(?:[.,]\d+)?)\s*[xх×*]\s*(\d+(?:[.,]\d+)?)(?:\s*[xх×*]\s*(\d+(?:[.,]\d+)?))?")


def _dims(req: Requirement, offer: Offer, want: str, base: dict) -> AttrCheck:
    need = [float(x) for x in want.split("x") if x]
    at_least = req.key in rules().get("comparability", {}).get("at_least", [])
    quals = [w[:4] for w in re.findall(r"[а-я]{5,}", req.name.lower()) if w[:4] in ("одни", "двум", "одно", "двух")]
    for cand in _candidates(req, offer)[:2]:
        segs = re.split(r"[;,]\s*(?=\d)", cand.lower())
        if quals:
            segs = [x for x in segs if any(q in x for q in quals)] or ([segs[0]] if quals[0] in ("одни", "одно") else [])
        if not segs:
            continue
        m = _DIM.search(segs[0])
        if not m:
            continue
        got = [to_float(x) or 0 for x in m.groups() if x]
        shown = "x".join(f"{x:g}" for x in got)
        if len(got) != len(need):
            return AttrCheck(**base, found=shown, status="unknown")
        if at_least:
            ok = all(g >= n - 1e-9 for g, n in zip(got, need))
        else:
            ok = all(abs(g - n) <= max(1.0, n * 0.01) for g, n in zip(got, need))
        return AttrCheck(**base, found=shown, status="ok" if ok else "mismatch")
    return AttrCheck(**base, found=None, status="unknown")


def _num_set(s: str | None, listed: bool = True) -> set[float]:
    """«0.3/0.4/0.6/0.8/1.0 мм» — перечень значений, а не одно число."""
    if not s or listed and s.count("/") < 2:
        return set()
    return {round(x, 3) for x in (to_float(v) for v in re.findall(r"\d+(?:[.,]\d+)?", s)) if x is not None}
