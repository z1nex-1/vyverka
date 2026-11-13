import re
from abc import ABC, abstractmethod
from datetime import date

from selectolax.lexbor import LexborHTMLParser

from ..models import Evidence, Item, Offer
from ..net import Fetcher, Page


class Source(ABC):
    name: str = ""
    title: str = ""
    kind: str = "catalog"

    def __init__(self, fetcher: Fetcher, conf: dict):
        self.fetcher = fetcher
        self.conf = conf
        self.title = conf.get("title", self.title)
        self.limit = int(conf.get("limit", 10))

    @abstractmethod
    async def search(self, item: Item, query: str) -> list[Offer]: ...

    def evidence(self, page: Page, excerpt: str | None = None) -> Evidence:
        return Evidence(url=page.url, fetched_at=page.fetched_at, sha256=page.sha256, snapshot=page.snapshot,
                        excerpt=(excerpt or "")[:600] or None, from_cache=page.from_cache)


def money(s: str | None) -> float | None:
    if not s:
        return None
    s = s.replace("\xa0", "").replace("\u202f", "").replace(" ", "").replace("₽", "").replace("руб.", "").replace("руб", "")
    m = re.search(r"\d[\d.,]*", s)
    if not m:
        return None
    n = m.group(0).rstrip(".,")
    if "," in n and "." in n:
        dec = "," if n.rfind(",") > n.rfind(".") else "."
        n = n.replace("." if dec == "," else ",", "").replace(dec, ".")
    elif n.count(",") > 1 or n.count(".") > 1 or re.fullmatch(r"\d{1,3}([.,])\d{3}", n) and "." in n:
        n = n.replace(",", "").replace(".", "")
    else:
        n = n.replace(",", ".")
    try:
        return float(n)
    except ValueError:
        return None


def ru_date(s: str | None) -> date | None:
    if not s:
        return None
    m = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", s)
    if m:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", s)
    if m:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    return None


def person_inn(inn: str) -> str:
    """ИНН индивидуального предпринимателя — персональные данные: в отчёте остаются первые и последние две цифры."""
    inn = str(inn)
    return inn[:2] + "•" * 8 + inn[-2:] if len(inn) == 12 else inn


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", s or "").strip()


def property_pairs(tree: LexborHTMLParser, name_sel: str | None = None, value_sel: str | None = None) -> dict[str, str]:
    """Таблица характеристик со страницы товара: двухколоночные строки, dt/dd или пары из конфига."""
    out: dict[str, str] = {}
    if name_sel and value_sel:
        for k, v in zip(tree.css(name_sel), tree.css(value_sel)):
            out.setdefault(clean(k.text()), clean(v.text()))
    for tr in tree.css("tr"):
        cells = tr.css("td, th")
        if len(cells) == 2:
            out.setdefault(clean(cells[0].text()), clean(cells[1].text()))
    for dt in tree.css("dt"):
        dd = dt.next
        while dd is not None and dd.tag != "dd":
            dd = dd.next
        if dd is not None:
            out.setdefault(clean(dt.text()), clean(dd.text()))
    return {k: v for k, v in out.items() if 1 < len(k) <= 80 and 0 < len(v) <= 300}


def text_pairs(text: str) -> dict[str, str]:
    """Описание вида «чистота – более 98%; уровень эндотоксина: менее 0,1 …» → пары характеристик."""
    out: dict[str, str] = {}
    for seg in re.split(r"[\n|;]+|(?<=[а-яa-z\)])\.\s+(?=[А-ЯA-Z])", text):
        m = re.match(r"\s*([^:–—]{2,60}?)\s*(?::|\s[–—]\s)\s*(.{1,300})$", seg.strip())
        if m:
            out.setdefault(clean(m.group(1)), clean(m.group(2)).rstrip(".;"))
    return out
