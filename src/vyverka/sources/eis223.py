import asyncio
import re
from datetime import date, timedelta

from selectolax.lexbor import LexborHTMLParser

from ..match import name_similarity
from ..models import Item, Offer
from ..net import SourceUnavailable
from .base import Source, clean, money
from .eis import BASE, parse_search

DONE = "Исполнение завершено"


class EisContracts223(Source):
    """Реестр договоров ЕИС по 223-ФЗ: исполненные договоры за последние N лет.

    Это отдельный реестр, не реестр контрактов 44-ФЗ. Позиция договора обычно названа общо
    («Бумага для офисной техники»), а марка и характеристики записаны в предмете договора, поэтому
    в текст для сверки с ТЗ идёт и предмет. Поставщика реестр в открытой карточке не показывает."""

    name = "eis223"
    title = "ЕИС — реестр договоров 223-ФЗ"
    kind = "contract"

    async def search(self, item: Item, query: str) -> list[Offer]:
        years = int(self.conf.get("years", 3))
        params = {
            "searchString": query,
            "morphology": "on",
            "contractFz223": "on",
            "statuses": "1",
            "statuses_1": "on",
            "contract223DateFrom": (date.today() - timedelta(days=365 * years)).strftime("%d.%m.%Y"),
            "sortBy": "UPDATE_DATE",
            "sortDirection": "false",
            "recordsPerPage": "_50",
            "pageNumber": "1",
        }
        page = await self.fetcher.get(f"{BASE}/epz/contractfz223/search/results.html", params)
        hits = [h for h in parse_search(page.text) if h["stage"].startswith(DONE)][: self.limit]
        out: list[Offer] = []
        errors = []
        for res in await asyncio.gather(*(self._contract(item, query, h) for h in hits), return_exceptions=True):
            if isinstance(res, Offer):
                out.append(res)
            elif isinstance(res, Exception):
                errors.append(res)
        if errors and not out:
            raise errors[0]
        return out

    async def _contract(self, item: Item, query: str, hit: dict) -> Offer | None:
        reg = hit["reg"]
        card = await self.fetcher.get(f"{BASE}/epz/contractfz223/card/contract-subject.html", {"reestrNumber": reg})
        rows = parse_subject(card.text)
        if not rows:
            return None
        subject = None
        try:
            info = await self.fetcher.get(f"{BASE}/epz/contractfz223/card/contract-info.html", {"reestrNumber": reg})
            subject = parse_subject_line(info.text)
        except SourceUnavailable:
            pass
        target = f"{item.name} {item.brand or ''} {item.model or ''}"
        # Предмет договора описывает весь договор, поэтому к строке он добавляется, только если строка одна.
        texts = [f"{r['name']} {subject}" if subject and len(rows) == 1 else r["name"] for r in rows]
        sims = [name_similarity(target, t) for t in texts]
        best = max(range(len(rows)), key=lambda k: sims[k])
        if sims[best] < 0.15:
            return None
        row, text = rows[best], clean(texts[best])
        url = f"{BASE}/epz/contractfz223/card/contract-subject.html?reestrNumber={reg}"
        signed = hit.get("signed")
        return Offer(
            id=f"eis223:{reg}:{row['n']}", source=self.name, source_title=self.title, kind=self.kind, url=url,
            title=text[:200], price=row["price"], unit=row["unit"], qty=row["qty"], vat_included=True,
            supplier=None, customer=hit["customer"], doc_number=reg, doc_date=signed, okpd2=row["okpd2"],
            text=text, penalties=None, stage=hit["stage"],
            evidence=self.evidence(card, clean(f"{row['excerpt']}. Предмет договора: {subject or '—'}")), query=query,
            meta={"law": "223-ФЗ"})


def parse_subject(html: str) -> list[dict]:
    """Таблица «Наименование товаров, работ, услуг» карточки договора: строка, ОКПД2, количество, цена за единицу."""
    t = LexborHTMLParser(html)
    tables = [x for x in t.css("table") if "Цена за единицу" in x.text()]
    if not tables:
        return []
    rows = []
    for tr in tables[-1].css("tbody tr"):
        tds = tr.css("td")
        if len(tds) < 5:
            continue
        n = re.sub(r"\D", "", tds[0].text())
        name_lines = [clean(x) for x in tds[1].text(separator="\n").split("\n") if clean(x)]
        name = name_lines[0] if name_lines else ""
        okpd = re.search(r"\d{2}\.\d{2}(?:\.\d{1,3}){0,3}", tds[2].text())
        qty_txt = clean(tds[3].text())
        qm = re.match(r"([\d\s.,]+)\s*(.*)", qty_txt.replace("\xa0", " "))
        price_txt = clean(tds[4].text())
        if "рубль" not in price_txt.lower():
            continue
        price = money(price_txt)
        if not n or not name or price is None:
            continue
        rows.append({
            "n": int(n), "name": name, "okpd2": okpd.group(0) if okpd else None, "price": price,
            "qty": float(qm.group(1).replace(" ", "").replace(",", ".")) if qm and qm.group(1).strip() else None,
            "unit": (qm.group(2).strip() if qm else None) or None,
            "excerpt": clean(f"{name}. Цена за единицу: {price_txt}. Кол-во: {qty_txt or 'не указано'}"),
        })
    return rows


def parse_subject_line(html: str) -> str | None:
    t = LexborHTMLParser(html)
    for sec in t.css(".blockInfo__section, .section, tr"):
        title = sec.css_first(".section__title, td")
        info = sec.css_first(".section__info, td:nth-child(2)")
        if title and info and clean(title.text()) == "Предмет договора":
            return clean(info.text()) or None
    txt = clean(t.body.text(separator=" | ")) if t.body else ""
    m = re.search(r"Предмет договора \| (.+?) \|", txt)
    return m.group(1).strip() if m else None
