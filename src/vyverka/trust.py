"""Доверие к цене, %: насколько найденной цене можно верить как рыночной.

Строгий режим — работают только источники из утверждённого списка, доверие к ним 100 %.
Открытый режим — в выборку попадают и сайты из поиска, у каждой цены считается доверие:
база по виду источника плюс признаки самой страницы. Ниже порога — цена в расчёт не идёт,
но остаётся в отчёте с причиной."""

from datetime import date

from .models import Decision, Offer
from .settings import KIND_TRUST, blacklist, on_list, whitelist


def score(offer: Offer, dec: Decision, conf: dict | None, policy: str) -> tuple[int, list[tuple[str, int]]]:
    conf = conf or {}
    kind = conf.get("kind") or offer.kind
    if policy == "strict":
        return 100, [("источник из утверждённого списка", 100)]
    base = int(conf.get("trust", KIND_TRUST.get(kind, 60)))
    factors: list[tuple[str, int]] = [(f"вид источника: {_KIND.get(kind, kind)}", base)]
    m = offer.meta or {}
    host = m.get("domain") or ""
    if kind == "web":
        if host and on_list(host, whitelist()):
            factors.append(("сайт из списка разрешённых", 25))
        if m.get("structured"):
            factors.append(("цена в разметке schema.org, а не в тексте", 8))
        if m.get("inn"):
            factors.append(("на странице указан ИНН продавца", 10))
        if m.get("in_stock"):
            factors.append(("товар в наличии", 4))
        if host.endswith((".ru", ".su", ".рф")):
            factors.append(("российский магазин", 3))
        else:
            factors.append(("зарубежный сайт — цена может не включать доставку и пошлины", -15))
    if kind == "contract" and offer.doc_date:
        age = (date.today() - offer.doc_date).days / 365
        if age > 1:
            factors.append((f"контракт {age:.1f} г. назад — цена проиндексирована", -int(min(age, 3) * 3)))
    if dec.checks:
        ok = sum(1 for c in dec.checks if c.status == "ok")
        share = ok / len(dec.checks)
        if share >= 0.8:
            factors.append(("подтверждено ≥ 80 % характеристик ТЗ", 5))
        elif share < 0.3:
            factors.append(("характеристик ТЗ на странице почти нет", -10))
    if host and on_list(host, blacklist()):
        factors.append(("сайт в чёрном списке организации", -100))
    total = max(0, min(100, sum(v for _, v in factors)))
    return total, factors


def apply(offers: list[Offer], decisions: dict[str, Decision], confs: dict[str, dict], policy: str,
          threshold: int) -> None:
    for o in offers:
        d = decisions[o.id]
        d.trust, d.trust_factors = score(o, d, confs.get(o.source), policy)
        if policy == "open" and d.included and d.trust < threshold:
            d.included = False
            d.reasons.append(("low_trust", f"доверие к источнику {d.trust} % ниже порога {threshold} %"))


_KIND = {"contract": "исполненный контракт ЕИС", "registry": "зеркало реестра контрактов", "offer": "коммерческое предложение",
         "catalog": "каталог поставщика", "marketplace": "маркетплейс", "web": "сайт из открытого поиска"}
