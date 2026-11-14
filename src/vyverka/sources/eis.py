import asyncio
import re
from datetime import date, timedelta

from selectolax.lexbor import LexborHTMLParser

from ..match import check, name_similarity
from ..models import Item, Offer
from ..net import SourceUnavailable
from .base import Source, clean, money, person_inn, ru_date

BASE = "https://zakupki.gov.ru"


class EisContracts(Source):
    """Реестр контрактов ЕИС: исполненные контракты за последние N лет, цена за единицу из карточки."""

    name = "eis"
    title = "ЕИС — реестр контрактов"
    kind = "contract"

    async def search(self, item: Item, query: str) -> list[Offer]:
        years = int(self.conf.get("years", 3))
        params = {
            "searchString": query,
            "morphology": "on",
            "fz44": "on",
            "contractStageList_1": "on",
            "contractStageList": "1",
            "contractDateFrom": (date.today() - timedelta(days=365 * years)).strftime("%d.%m.%Y"),
            "sortBy": "UPDATE_DATE",
            "sortDirection": "false",
            "recordsPerPage": "_50",
            "pageNumber": "1",
        }
        if self.conf.get("fz223", True):
            params["fz223"] = "on"
        page = await self.fetcher.get(f"{BASE}/epz/contract/search/results.html", params)
        hits = parse_search(page.text)
        hits = [h for h in hits if h["stage"].startswith("Исполнение завершено")][: self.limit]
        tasks = [self._contract(item, query, h) for h in hits]
        out: list[Offer] = []
        errors = []
        for res in await asyncio.gather(*tasks, return_exceptions=True):
            if isinstance(res, Offer):
                out.append(res)
            elif isinstance(res, Exception):
                errors.append(res)
        if errors and not out:
            raise errors[0]
        return out

    async def _contract(self, item: Item, query: str, hit: dict) -> Offer | None:
        reg = hit["reg"]
        card = await self.fetcher.get(f"{BASE}/epz/contract/contractCard/payment-info-and-target-of-order.html",
                                      {"reestrNumber": reg})
        rows = parse_card(card.text)
        if not rows:
            return None
        target = f"{item.name} {item.brand or ''} {item.model or ''}"
        sims = [name_similarity(target, r["name"]) for r in rows]
        top = max(sims)
        if top < 0.15:
            return None
        # В одном контракте бывают А4 и А3 под одинаковым названием — строку выбираем по характеристикам ТЗ.
        cands = [r for r, x in zip(rows, sims) if x >= top - 0.02][:4]
        scored = []
        for r in cands:
            attrs = dict(r["attrs"])
            if r.get("chars_url"):
                try:
                    ch = await self.fetcher.get(BASE + r["chars_url"])
                    attrs.update(parse_chars(ch.text))
                except SourceUnavailable:
                    pass
            scored.append((_fit(item, r, attrs), r, attrs))
        _, row, attrs = max(scored, key=lambda x: x[0])
        supplier, penalties = None, None
        try:
            common = await self.fetcher.get(f"{BASE}/epz/contract/contractCard/common-info.html", {"reestrNumber": reg})
            supplier = parse_supplier(common.text)
        except SourceUnavailable:
            pass
        try:
            proc = await self.fetcher.get(f"{BASE}/epz/contract/contractCard/process-info.html", {"reestrNumber": reg})
            penalties = parse_penalties(proc.text)
        except SourceUnavailable:
            pass
        url = f"{BASE}/epz/contract/contractCard/payment-info-and-target-of-order.html?reestrNumber={reg}"
        text = clean(f"{row['name']} {row['short']} " + " ".join(f"{k}: {v}" for k, v in attrs.items()))
        return Offer(
            id=f"eis:{reg}:{row['n']}", source=self.name, source_title=self.title, kind=self.kind, url=url,
            title=row["name"], price=row["price"], unit=row["unit"], qty=row["qty"],
            vat_included=True, supplier=supplier, customer=hit["customer"], doc_number=reg,
            doc_date=hit["signed"], okpd2=row["okpd2"], ktru=row["ktru"], attributes=attrs, text=text,
            penalties=penalties, stage=hit["stage"], evidence=self.evidence(card, row["excerpt"]), query=query)


def parse_search(html: str) -> list[dict]:
    t = LexborHTMLParser(html)
    out = []
    for b in t.css(".search-registry-entry-block"):
        num = b.css_first(".registry-entry__header-mid__number a")
        if not num:
            continue
        reg = re.sub(r"\D", "", num.text())
        stage = clean(b.css_first(".registry-entry__header-mid__title").text()) if b.css_first(
            ".registry-entry__header-mid__title") else ""
        titles = [clean(x.text()) for x in b.css(".registry-entry__body-title")]
        values = [clean(x.text()) for x in b.css(".registry-entry__body-value, .registry-entry__body-href")]
        at = titles.index("Заказчик") if "Заказчик" in titles else 0
        customer = values[at] if len(values) > at else None
        dates = {clean(x.text()): clean(y.text()) for x, y in
                 zip(b.css(".data-block__title"), b.css(".data-block__value"))}
        out.append({"reg": reg, "stage": stage, "customer": customer, "titles": titles,
                    "signed": ru_date(dates.get("Заключение контракта") or dates.get("Заключение договора")),
                    "price": money(b.css_first(".price-block__value").text() if b.css_first(".price-block__value") else None)})
    return out


def parse_card(html: str) -> list[dict]:
    t = LexborHTMLParser(html)
    tables = [x for x in t.css("table") if "Цена за единицу" in x.text()]
    if not tables:
        return []
    rows = []
    for tr in tables[-1].css("tr"):
        tds = tr.css("td")
        if len(tds) < 7:
            continue
        first = tds[1].text(separator="\n")
        m = re.match(r"\s*(\d+)\.\s*(.+)", first, re.S)
        if not m:
            continue
        lines = [clean(x) for x in m.group(2).split("\n") if clean(x)]
        name = lines[0]
        short = " ".join(x for x in lines[1:] if "Все характеристики" not in x)
        cls = clean(tds[2].text())
        ktru = re.search(r"\d{2}\.\d{2}\.\d{2}\.\d{3}-\d{8}", cls)
        okpd = re.findall(r"\((\d{2}\.\d{2}(?:\.\d{1,3}){0,3})\)", cls)
        qty_txt = clean(tds[4].text())
        qm = re.match(r"([\d\s.,]+)\s*(.*)", qty_txt.replace("\xa0", " "))
        price = money(tds[5].text())
        if price is None:
            continue
        chars = None
        for a in tr.css("a"):
            href = a.attributes.get("href") or ""
            if "ktru-characteristics" in href or "characteristics" in href:
                chars = href
        attrs = {}
        for line in lines[1:]:
            if line.startswith("Страна происхождения"):
                attrs["Страна происхождения"] = line.split(":", 1)[-1].strip()
                continue
            for part in re.split(r"\s,\s|;\s", line.replace("...", "")):
                mm = re.match(r"(.+?)\s+(\S+)$", part.strip())
                if mm and len(mm.group(1)) < 60:
                    attrs[mm.group(1).strip()] = unit_words(mm.group(2).strip())
        rows.append({
            "n": int(m.group(1)), "name": name, "short": short, "ktru": ktru.group(0) if ktru else None,
            "okpd2": okpd[-1] if okpd else None,
            "qty": float(qm.group(1).replace(" ", "").replace(",", ".")) if qm and qm.group(1).strip() else None,
            "unit": (qm.group(2).strip() if qm else None) or None, "price": price, "chars_url": chars,
            "attrs": attrs, "excerpt": clean(f"{name}. {short}. Цена за единицу: {tds[5].text()} ₽. Кол-во: {qty_txt}"),
        })
    return rows


_UNIT_WORDS = [("Грамм на квадратный метр", " г/м2"), ("Процент", " %"), ("Штука", " шт"), ("Миллиметр", " мм"),
               ("Микрометр", " мкм"), ("Микрограмм", " мкг"), ("Килограмм", " кг"), ("Ватт", " Вт")]


def unit_words(v: str) -> str:
    for a, b in _UNIT_WORDS:
        v = re.sub(rf"(?<=\d)\s*{a}", b, v)
    return v


def parse_chars(html: str) -> dict[str, str]:
    t = LexborHTMLParser(html)
    out = {}
    for sec in t.css(".blockInfo__section"):
        k, v = sec.css_first(".section__title"), sec.css_first(".section__info")
        if k and v and clean(k.text()):
            out[clean(k.text())] = unit_words(clean(v.text(separator=" ")))
    for tr in t.css("tr"):
        tds = [clean(x.text()) for x in tr.css("td")]
        tds = [x for x in tds if x]
        if len(tds) >= 2 and len(tds[0]) < 80:
            out[tds[0]] = " ".join(tds[1:])
    return out


def parse_supplier(html: str) -> str | None:
    txt = clean(LexborHTMLParser(html).body.text(separator=" | "))
    i = txt.find("Информация о поставщиках")
    if i < 0:
        return None
    seg = [s.strip() for s in txt[i:i + 1500].split("|") if s.strip()]
    try:
        k = seg.index("Статус")
    except ValueError:
        return None
    name = seg[k + 1] if len(seg) > k + 1 else None
    inn = None
    for j, s in enumerate(seg[k:k + 12]):
        if s == "ИНН:" and k + j + 1 < len(seg):
            inn = seg[k + j + 1]
            break
    if not name:
        return None
    if "Индивидуальный предприниматель" in seg[k + 1:k + 4] or (inn and len(inn) == 12):
        name = "ИП"
    return f"{name}, ИНН {person_inn(inn)}" if inn else name


def parse_penalties(html: str) -> bool | None:
    txt = clean(LexborHTMLParser(html).body.text(separator=" | "))
    if "Неустойки" not in txt:
        return None
    if re.search(r"Информация о (начислении )?неустой|Требование об уплате неустой|Начислено неустой", txt):
        return True
    return False


def _fit(item: Item, row: dict, attrs: dict) -> float:
    if not item.requirements:
        return 0.0
    probe = Offer.model_construct(id="probe", source="eis", source_title="", kind="contract", url="", title=row["name"],
                  price=row["price"] or 0, attributes=attrs,
                  text=clean(f"{row['name']} {row['short']} " + " ".join(f"{k}: {v}" for k, v in attrs.items())))
    res = [check(r, probe).status for r in item.requirements if r.essential]
    return res.count("ok") - 2 * res.count("mismatch")
