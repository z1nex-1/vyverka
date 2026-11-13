import asyncio
import re
from urllib.parse import quote_plus, urljoin

from selectolax.lexbor import LexborHTMLParser

from ..match import name_similarity
from ..models import Item, Offer
from ..net import SourceUnavailable
from .base import Source, clean, money, property_pairs, text_pairs
from ..words import rub


class HtmlCatalog(Source):
    """Каталог дилера с обычной HTML-выдачей. Селекторы карточки задаются в config/sources.yaml,
    поэтому смена вёрстки правится конфигом, а не кодом."""

    name = "html"
    kind = "catalog"

    def __init__(self, fetcher, conf):
        super().__init__(fetcher, conf)
        self.name = conf.get("id", self.name)
        self.kind = conf.get("kind", self.kind)
        self.template = conf["search_url"]
        self.sel = conf["selectors"]

    async def search(self, item: Item, query: str) -> list[Offer]:
        url = self.template.replace("{q}", quote_plus(query))
        page = await self.fetcher.get(url)
        tree = LexborHTMLParser(page.text)
        cards = tree.css(self.sel["card"])
        if not cards and _CAPTCHA.search(page.text[:20000]):
            raise SourceUnavailable("сайт показал проверку «я не робот», цены с него не получены")
        if not cards and self.sel.get("required", True) and "{q}" in self.template and len(page.text) > 2000:
            raise LayoutChanged(f"{self.title}: не найдены карточки по селектору «{self.sel['card']}»")
        out: list[Offer] = []
        for card in cards:
            name_el = card.css_first(self.sel["name"])
            if not name_el:
                continue
            title_el = card.css_first(self.sel["title"]) if self.sel.get("title") else None
            title = clean((title_el or name_el).text())
            href = name_el.attributes.get("href") or ""
            link_el = card.css_first(self.sel.get("link", "a[href]"))
            if not href and link_el:
                href = link_el.attributes.get("href") or ""
            link = urljoin(url, href) if href else url
            attrs = {}
            if self.sel.get("prop_name"):
                for k, v in zip(card.css(self.sel["prop_name"]), card.css(self.sel["prop_value"])):
                    attrs[clean(k.text())] = clean(v.text())
            if self.sel.get("maker"):
                mk = card.css_first(self.sel["maker"])
                if mk and clean(mk.text()):
                    attrs["Производитель"] = clean(mk.text())
            variants = card.css(self.sel["variant"]) if self.sel.get("variant") else [card]
            for v in variants:
                price_el = v.css_first(self.sel["variant_price"] if self.sel.get("variant") else self.sel["price"])
                price = money(price_el.text()) if price_el else None
                if not price:
                    continue
                vt, vlink = title, link
                if self.sel.get("variant"):
                    amt = v.css_first(self.sel["variant_name"])
                    if amt and clean(amt.text()):
                        vt = f"{title}, {clean(amt.text())}"
                        vlink = f"{link}#{re.sub(r'\W+', '', clean(amt.text()))}"
                text = clean(vt + ". " + "; ".join(f"{k}: {x}" for k, x in attrs.items()))
                key = re.sub(r"\W+", "", vlink)[-48:]
                out.append(Offer(id=f"{self.name}:{key}", source=self.name, source_title=self.title, kind=self.kind,
                                 url=vlink, title=vt, price=price, unit="шт", supplier=self.conf.get("seller", self.title),
                                 doc_date=page.fetched_at.date(), attributes=dict(attrs), text=text,
                                 evidence=self.evidence(page, f"{vt} — {clean(price_el.text())}. {text[:300]}"), query=query))
            if len(out) >= self.limit:
                break
        await self._details(item, out)
        return out

    async def _details(self, item: Item, offers: list[Offer]) -> None:
        """Характеристики есть только на странице товара; открываем её для кандидатов той же марки."""
        conf = self.conf.get("detail")
        if conf is None:
            return
        brand = (item.brand or "").lower()
        by_page: dict[str, list[Offer]] = {}
        for o in offers:
            if re.search(r"\bдля\b", o.title.lower()):
                continue
            if brand and brand not in o.title.lower() or not brand and name_similarity(item.name, o.title) < 0.3:
                continue
            by_page.setdefault(o.url.split("#")[0], []).append(o)
        ranked = sorted(by_page.items(), key=lambda kv: -name_similarity(item.name, kv[1][0].title))
        pages = ranked[: int(conf.get("limit", 4))]
        await asyncio.gather(*(self._detail(u, group, conf) for u, group in pages), return_exceptions=True)

    async def _detail(self, url: str, group: list[Offer], conf: dict) -> None:
        page = await self.fetcher.get(url)
        tree = LexborHTMLParser(page.text)
        props = property_pairs(tree, conf.get("prop_name"), conf.get("prop_value"))
        if conf.get("description"):
            el = tree.css_first(conf["description"])
            if el and clean(el.text()):
                for k, v in text_pairs(el.text(separator="\n")).items():
                    props.setdefault(k, v)
                props.setdefault("Описание", clean(el.text())[:1500])
        for offer in group:
            if conf.get("price") and len(group) == 1:
                el = tree.css_first(conf["price"])
                if el and money(el.text()):
                    offer.price = money(el.text())
            if not props:
                continue
            offer.attributes.update(props)
            offer.text = clean(offer.title + ". " + "; ".join(f"{k}: {v}" for k, v in offer.attributes.items()))
            offer.evidence = self.evidence(page, f"{offer.title} — {rub(offer.price)}. " + offer.text[:400])


_CAPTCHA = re.compile(r"captcha|smartcaptcha|не робот|подтвердите, что вы человек|cf-chl|challenge-platform", re.I)


class LayoutChanged(Exception):
    pass
