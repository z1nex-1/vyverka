from ..config import sources as sources_conf
from ..net import Fetcher
from .base import Source
from .clearspending import ClearSpending
from .eis import EisContracts
from .eis223 import EisContracts223
from .html import HtmlCatalog
from .jsonld import JsonLdCatalog
from .web import WebSearch

REGISTRY: dict[str, type[Source]] = {
    "eis": EisContracts,
    "eis223": EisContracts223,
    "clearspending": ClearSpending,
    "jsonld": JsonLdCatalog,
    "html": HtmlCatalog,
    "web": WebSearch,
}


def build(fetcher: Fetcher, only: list[str] | None = None, policy: str = "strict") -> list[Source]:
    """Строгий режим: только источники из утверждённого списка, поиск — только по разрешённым сайтам.
    Открытый: все включённые источники, поиск — по любым сайтам, кроме чёрного списка."""
    out = []
    for conf in sources_conf().get("sources", []):
        if not conf.get("enabled", True) or conf.get("type") not in REGISTRY:
            continue
        if only and conf["id"] not in only:
            continue
        if policy == "strict" and not conf.get("listed", True) and conf.get("type") != "web":
            continue
        src = REGISTRY[conf["type"]](fetcher, {**conf, "policy": policy} if conf.get("type") == "web" else conf)
        src.name = conf["id"]
        out.append(src)
    return out


def catalog() -> list[dict]:
    """Все источники с основанием, статусом и доверием — для отчёта и экрана «Источники»."""
    rows = []
    for c in sources_conf().get("sources", []):
        typ = c.get("type")
        if typ == "manual":
            state = "manual"
        elif c.get("enabled", True) and (typ in REGISTRY or typ == "site"):
            state = "on"
        elif c.get("until"):
            state = "paused"
        else:
            state = "off"
        rows.append({"id": c["id"], "title": c.get("title", c["id"]), "basis": c.get("basis", ""), "state": state,
                     "kind": c.get("kind", "catalog"), "trust": c.get("trust"), "tier": c.get("tier"),
                     "limit": c.get("limit"), "years": c.get("years"),
                     "listed": c.get("listed", True), "custom": c.get("custom", False), "type": typ,
                     "until": c.get("until"), "url": c.get("search_url") or c.get("domain") or "",
                     "categories": c.get("categories") or []})
    return rows
