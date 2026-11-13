import re

from .match import _target_key
from .models import Item

_NOISE = re.compile(r"\b(производитель|страна|модель|марка|артикул)\b.*$", re.I)


def _generic(item: Item) -> str:
    s = _NOISE.sub("", item.name)
    for w in (item.brand, item.model, item.manufacturer):
        if w:
            s = re.sub(re.escape(w), " ", s, flags=re.I)
    s = re.sub(r"[«»\"(),]", " ", s)
    words: list[str] = []
    for w in s.split():
        if re.fullmatch(r"rh[\w\-]+", w, re.I) or w.lower() in (x.lower() for x in words):
            continue
        words.append(w)
    return " ".join(words[:6]).strip()


def plan(item: Item) -> list[str]:
    """Запросы от самого точного к самому широкому. Источник перебирает их, пока не наберёт выборку:
    ЕИС не находит «CreatBot D600», но находит «CreatBot», а лишнее потом отсекает сопоставимость."""
    q: list[str] = []
    generic = _generic(item)
    if item.brand and item.model:
        q.append(f"{item.brand} {item.model}")
        base = item.model.split()[0]
        if base.lower() != item.model.lower():
            q.append(f"{item.brand} {base}")
    if item.category == "reagents":
        t = _target_key(item.name)
        if t:
            num = re.sub(r"\D", "", t) if t.startswith("il") else ""
            if t.startswith("il"):
                q += [f"интерлейкин {num} рекомбинантный", f"IL-{num} human recombinant"]
            elif t.startswith("tgfb"):
                q += [f"трансформирующий фактор роста бета {t[-1]}", f"TGF-beta {t[-1]} human recombinant"]
    if item.brand and not item.model:
        fmt = next((r.constraint.text for r in item.requirements if r.key == "paper_format" and r.constraint.text), "")
        q.append(f"бумага {item.brand} {fmt}".strip() if item.category == "paper" else f"{generic} {item.brand}")
    if item.brand and item.model:
        q.append(item.brand)
    if item.category == "paper" and not item.brand:
        dens = next((r.constraint for r in item.requirements if r.key == "paper_density"), None)
        fmt = next((r.constraint.text for r in item.requirements if r.key == "paper_format" and r.constraint.text), "")
        kind = "офсетная" if "офсет" in item.name.lower() else ""
        g = f"{dens.value:g} г/м2" if dens and dens.value else ""
        q.append(" ".join(x for x in ["бумага", kind, fmt, g] if x))
        two = " ".join(item.name.split()[:2]).lower()
        q.append(" ".join(x for x in [two, fmt, g] if x))
    if generic and item.category != "reagents":
        q.append(generic)
    seen, out = set(), []
    for x in q:
        k = re.sub(r"\s+", " ", x).strip()
        if k and k.lower() not in seen:
            seen.add(k.lower())
            out.append(k)
    return out
