import asyncio
import json
import os
import re
import shutil
from urllib.parse import quote_plus, urlsplit

from selectolax.lexbor import LexborHTMLParser

from ..models import Item, Offer
from ..net import UA, Page, SourceUnavailable
from ..settings import blacklist, domain, on_list, whitelist
from .base import Source, clean, money, property_pairs
from .jsonld import _attrs, _products
from ..words import rub

_GAP = 2.5
_queue: dict[int, tuple[asyncio.Lock, list[float]]] = {}


async def _engine_slot() -> None:
    """Поисковая система отвечает 429 на параллельные запросы: запросы идут по одному с паузой."""
    loop = asyncio.get_running_loop()
    lock, last = _queue.setdefault(id(loop), (asyncio.Lock(), [0.0]))
    async with lock:
        wait = last[0] + _GAP - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        last[0] = loop.time()


_INN = re.compile(r"ИНН\D{0,12}(\d{10}|\d{12})\b")
_SKIP = ("youtube.", "wikipedia.", "reddit.", "vk.com", "t.me", "dzen.ru", "pikabu.", "otzovik.", "irecommend.")


class WebSearch(Source):
    """Открытый поиск: запрос в поисковую систему, затем страницы магазинов из выдачи.
    Цена берётся только из машиночитаемой разметки (schema.org, microdata, og:price), не из текста.
    Строгий режим — только сайты из списка разрешённых организации; открытый — любые, с оценкой доверия."""

    kind = "web"

    def __init__(self, fetcher, conf):
        super().__init__(fetcher, conf)
        self.template = conf.get("search_url", "https://search.brave.com/search?q={q}&source=web")
        self.suffix = conf.get("query_suffix", "цена купить")
        self.pages = int(conf.get("pages", 6))
        self.result_sel = conf.get("result_selector", '[data-type="web"] a[href^="http"]')
        self.policy = conf.get("policy", "open")

    def links(self, html: str, engine_host: str) -> list[str]:
        tree = LexborHTMLParser(html)
        nodes = tree.css(self.result_sel) or tree.css("a[href^='http']")
        out: list[str] = []
        allow, deny = whitelist(), blacklist()
        for a in nodes:
            href = (a.attributes.get("href") or "").split("#")[0]
            host = domain(href)
            if not host or engine_host.endswith(host) or "brave" in host or any(s in host for s in _SKIP):
                continue
            if on_list(host, deny) or self.policy == "strict" and not on_list(host, allow):
                continue
            if urlsplit(href).path in ("", "/") or href in out:
                continue
            out.append(href)
        return out

    async def search(self, item: Item, query: str) -> list[Offer]:
        q = f"{query} {self.suffix}".strip()
        key = os.environ.get("VYVERKA_BRAVE_API_KEY", "")
        if key:
            links = await self._api(q, key)
        else:
            url = self.template.replace("{q}", quote_plus(q))
            page = await self._serp(url)
            links = self.links(page.text, domain(url))
        links = links[: self.pages]
        got = await asyncio.gather(*(self._product(u, query) for u in links), return_exceptions=True)
        return [o for o in got if isinstance(o, Offer)][: self.limit]

    async def _api(self, q: str, key: str) -> list[str]:
        """Официальный Brave Search API: для промышленной эксплуатации, ключ в VYVERKA_BRAVE_API_KEY."""
        await _engine_slot()
        page = await self.fetcher.get("https://api.search.brave.com/res/v1/web/search",
                                      params={"q": q, "country": "RU", "search_lang": "ru", "count": 20},
                                      headers={"X-Subscription-Token": key, "Accept": "application/json"})
        results = (json.loads(page.text).get("web") or {}).get("results") or []
        html = "".join(f'<a href="{r.get("url", "")}"></a>' for r in results)
        return self.links(f'<div data-type="web">{html}</div>', "api.search.brave.com")

    async def _serp(self, url: str) -> Page:
        """Страница выдачи. Поисковики отвечают 429 клиентам с TLS-отпечатком Python, поэтому без ключа API
        выдача берётся системным curl, а копия сохраняется как доказательство наравне с остальными страницами."""
        f = self.fetcher
        cached = f.store.get(url)
        if f.mode == "offline" or (cached and f.mode == "cache" and f._fresh(cached)):
            return await f.get(url)
        curl = shutil.which("curl") if os.environ.get("VYVERKA_SEARCH_CURL", "1") != "0" else None
        if not curl:
            await _engine_slot()
            return await f.get(url)
        for attempt in range(3):
            await _engine_slot()
            proc = await asyncio.create_subprocess_exec(
                curl, "-sL", "--compressed", "--max-time", "20", "-A", UA, "-H", "Accept-Language: ru-RU,ru;q=0.9",
                "-w", "\n%{http_code}", url, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            out, _ = await proc.communicate()
            body, _, code = out.decode("utf-8", "replace").rpartition("\n")
            if code != "429":
                break
            await asyncio.sleep(3 + 3 * attempt)
        if not code.isdigit() or code == "000" or int(code) >= 400 or not body.strip():
            if cached:
                return cached
            if code == "429":
                raise SourceUnavailable("поисковая система временно ограничила частоту запросов, расчёт идёт по остальным "
                                        "источникам")
            raise SourceUnavailable(f"поисковая система ответила {code or 'ошибкой'}")
        return f.store.put(url, int(code), body)

    async def _product(self, url: str, query: str) -> Offer | None:
        page = await self.fetcher.get(url)
        found = extract(page.text)
        if not found:
            return None
        host = domain(page.url)
        title, price, attrs, desc, meta = found
        meta.update(domain=host)
        key = re.sub(r"\W+", "", page.url)[-48:]
        return Offer(id=f"{self.name}:{key}", source=self.name, source_title=f"{self.title}: {host}", kind="web",
                     url=page.url, title=title, price=price, unit="шт", supplier=host, doc_date=page.fetched_at.date(),
                     attributes=attrs, text=clean(f"{title}. {desc}")[:4000], query=query, meta=meta,
                     evidence=self.evidence(page, f"{title} — {rub(price)}. {desc[:300]}"))


def extract(html: str) -> tuple[str, float, dict, str, dict] | None:
    """Товар со страницы магазина: JSON-LD Product, затем microdata, затем мета-теги og/product."""
    meta: dict = {}
    tree = LexborHTMLParser(html)
    title, price, desc, attrs = "", None, "", {}
    for p in _products(html):
        offer = p.get("offers") or {}
        if isinstance(offer, list):
            offer = offer[0] if offer else {}
        cur = str(offer.get("priceCurrency") or "RUB").upper()
        if cur not in ("RUB", "RUR"):
            continue
        price = money(str(offer.get("price") or offer.get("lowPrice") or ""))
        if price:
            title, desc = clean(p.get("name")), clean(p.get("description"))
            attrs = _attrs(desc)
            for prop in p.get("additionalProperty") or []:
                if isinstance(prop, dict) and prop.get("name"):
                    attrs[clean(prop["name"])] = clean(str(prop.get("value", "")))
            avail = str(offer.get("availability", ""))
            meta["in_stock"] = "InStock" in avail
            if "OutOfStock" in avail or "Discontinued" in avail:
                return None
            meta["structured"] = "json-ld"
            break
    if not price:
        node = tree.css_first('[itemprop="price"]')
        if node is not None:
            price = money(node.attributes.get("content") or node.text())
            meta["structured"] = "microdata" if price else None
    if not price:
        node = tree.css_first('meta[property="product:price:amount"], meta[property="og:price:amount"]')
        if node is not None:
            price = money(node.attributes.get("content"))
            meta["structured"] = "og" if price else None
    if not price or price < 1:
        return None
    if not title:
        node = tree.css_first('[itemprop="name"], meta[property="og:title"], h1, title')
        if node is not None:
            title = clean(node.attributes.get("content") or node.text())
    if not desc:
        node = tree.css_first('meta[name="description"], meta[property="og:description"]')
        desc = clean(node.attributes.get("content")) if node is not None else ""
    attrs = {**property_pairs(tree), **attrs}
    body = tree.body.text(separator=" ") if tree.body else ""
    inn = _INN.search(body)
    if inn:
        meta["inn"] = inn.group(1)
    specs = "; ".join(f"{k}: {v}" for k, v in list(attrs.items())[:60])
    return title[:300] or "Товар", price, attrs, f"{desc} {specs}".strip(), meta
