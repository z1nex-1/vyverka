"""Проверка поставщиков по реестру недобросовестных поставщиков ЕИС (44-ФЗ и 223-ФЗ).

Цена договора с поставщиком из РНП — плохой ориентир рыночной цены: договор с ним могли расторгнуть,
а сам он не может участвовать в закупках. Поэтому такая цена исключается с причиной, а решение
можно переиграть вручную. Ответ реестра хранится как копия страницы, как и любой другой источник."""

import asyncio
import re
from datetime import date

from selectolax.lexbor import LexborHTMLParser

from .config import rules
from .models import Decision, Offer
from .net import Fetcher, SourceUnavailable
from .sources.base import ru_date

URL = "https://zakupki.gov.ru/epz/dishonestsupplier/search/results.html"
EMPTY = "Поиск не дал результатов"


def inn_of(o: Offer) -> str | None:
    """ИНН юрлица из реквизитов поставщика или со страницы магазина. ИНН ИП в отчёте скрыт, его не проверяем."""
    for text in (o.supplier or "", str((o.meta or {}).get("inn") or "")):
        m = re.search(r"(?<!\d)(\d{10})(?!\d)", text)
        if m:
            return m.group(1)
    return None


def params(inn: str) -> dict:
    return {"searchString": inn, "fz44": "on", "fz223": "on"}


def link(inn: str) -> str:
    return f"{URL}?searchString={inn}&fz44=on&fz223=on"


def parse(html: str, inn: str) -> list[dict] | None:
    """Записи реестра с этим ИНН. None — страница не похожа на выдачу реестра."""
    if EMPTY in html:
        return []
    tree = LexborHTMLParser(html)
    blocks = tree.css("div.search-registry-entry-block")
    if not blocks:
        return None
    found = []
    for b in blocks:
        parts = [re.sub(r"\s+", " ", t).strip() for t in b.text(separator="\n").split("\n")]
        parts = [p for p in parts if p]
        val = lambda label: next((parts[i + 1] for i, p in enumerate(parts[:-1]) if p.startswith(label)), None)
        if (val("ИНН") or "").strip() != inn:
            continue
        num = next((p for p in parts if p.startswith("№")), "")
        added = ru_date(val("Включено") or "")
        found.append({"number": num.lstrip("№ ").strip(), "name": val("Наименование") or "",
                      "law": next((p for p in parts if p.endswith("-ФЗ")), ""),
                      "added": added.isoformat() if isinstance(added, date) else None})
    return found


async def lookup(fetcher: Fetcher, inn: str) -> dict:
    try:
        page = await fetcher.get(URL, params(inn))
    except SourceUnavailable as e:
        return {"status": "unknown", "inn": inn, "url": link(inn), "error": str(e)}
    entries = parse(page.text, inn)
    if entries is None:
        return {"status": "unknown", "inn": inn, "url": link(inn), "error": "реестр вернул неожиданную страницу"}
    return {"status": "listed" if entries else "clean", "inn": inn, "url": link(inn), "entries": entries,
            "checked_at": page.fetched_at.isoformat(), "sha256": page.sha256, "snapshot": page.snapshot}


async def check(fetcher: Fetcher, offers: list[Offer], decisions: dict[str, Decision]) -> dict[str, int]:
    """Проверяет поставщиков цен, которые пока идут в расчёт. Результат — в offer.meta["rnp"]."""
    conf = rules().get("suppliers", {})
    if not conf.get("rnp_check", True):
        return {}
    todo = {o.id: inn for o in offers if decisions[o.id].included and (inn := inn_of(o))}
    found = dict(zip(set(todo.values()), await asyncio.gather(*(lookup(fetcher, i) for i in set(todo.values())))))
    counts = {"clean": 0, "listed": 0, "unknown": 0}
    for o in offers:
        if o.id not in todo:
            continue
        res = found[todo[o.id]]
        o.meta = {**(o.meta or {}), "rnp": res}
        counts[res["status"]] += 1
        if res["status"] == "listed" and conf.get("rnp_exclude", True):
            d = decisions[o.id]
            e = res["entries"][0]
            when = f", включён {date.fromisoformat(e['added']):%d.%m.%Y}" if e.get("added") else ""
            d.included = False
            d.reasons.append(("rnp", f"поставщик в реестре недобросовестных поставщиков ЕИС: запись № {e['number']}{when}"))
    return counts


def label(o: Offer) -> str:
    r = (o.meta or {}).get("rnp")
    if not r:
        return "не проверялся"
    return {"clean": "нет в РНП", "listed": "в РНП", "unknown": "не удалось проверить"}[r["status"]]
