import json
import re
from datetime import date
from urllib.parse import quote_plus, urljoin

from ..models import Item, Offer
from .base import Source, clean, money


class JsonLdCatalog(Source):
    """Любой каталог, отдающий schema.org Product/Offer в JSON-LD: Яндекс Маркет, дилеры, дистрибьюторы.
    Новый магазин добавляется строкой в config/sources.yaml с шаблоном URL поиска."""

    name = "jsonld"
    kind = "marketplace"

    def __init__(self, fetcher, conf):
        super().__init__(fetcher, conf)
        self.name = conf.get("id", self.name)
        self.kind = conf.get("kind", self.kind)
        self.template = conf["search_url"]

    async def search(self, item: Item, query: str) -> list[Offer]:
        url = self.template.replace("{q}", quote_plus(query))
        page = await self.fetcher.get(url)
        products = list(_products(page.text))
        out: list[Offer] = []
        for p in products:
            offer = p.get("offers") or {}
            if isinstance(offer, list):
                offer = offer[0] if offer else {}
            if offer.get("@type") == "AggregateOffer":
                price = money(str(offer.get("lowPrice") or offer.get("price") or ""))
            else:
                price = money(str(offer.get("price") or ""))
            if not price:
                continue
            link = urljoin(url, p.get("url") or offer.get("url") or p.get("@id") or url)
            title = clean(p.get("name"))
            desc = clean(p.get("description"))
            attrs = _attrs(desc)
            for prop in p.get("additionalProperty") or []:
                if isinstance(prop, dict) and prop.get("name"):
                    attrs[clean(prop["name"])] = clean(str(prop.get("value", "")))
            seller = offer.get("seller") or {}
            sup = seller.get("name") if isinstance(seller, dict) else None
            avail = str(offer.get("availability", ""))
            if "OutOfStock" in avail or "Discontinued" in avail:
                continue
            key = re.sub(r"\W+", "", link)[-40:]
            out.append(Offer(
                id=f"{self.name}:{key}", source=self.name, source_title=self.title, kind=self.kind, url=link,
                title=title, price=price, currency=offer.get("priceCurrency") or "RUB", unit="шт",
                supplier=sup or self.title, doc_date=date.today(), attributes=attrs,
                text=clean(f"{title}. {desc}"), evidence=self.evidence(page, f"{title} — {price} ₽. {desc}"),
                query=query))
            if len(out) >= self.limit:
                break
        return out


def _products(html: str):
    for block in re.findall(r"<script[^>]*application/ld\+json[^>]*>(.*?)</script>", html, re.S | re.I):
        try:
            data = json.loads(block.strip())
        except json.JSONDecodeError:
            continue
        yield from _walk(data)


def _walk(node):
    if isinstance(node, list):
        for x in node:
            yield from _walk(x)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = t if isinstance(t, list) else [t]
        if "Product" in types:
            yield node
            return
        for k in ("itemListElement", "item", "@graph", "mainEntity"):
            if k in node:
                yield from _walk(node[k])


def _attrs(desc: str) -> dict[str, str]:
    out = {}
    for part in re.split(r";\s*", desc):
        if ":" in part:
            k, v = part.split(":", 1)
            if 1 < len(k) < 50:
                out.setdefault(k.strip(), v.strip())
    return out
