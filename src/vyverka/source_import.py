"""Список источников из файла организации: DOCX, TXT, CSV или XLSX в свободной форме.

Ищутся адреса сайтов, известные названия площадок («Сбербанк-АСТ», «Берёзка», «Комус») и виды
источников («каталоги производителей», «маркетплейсы»). Результат — утверждённый список: разрешённые
сайты для поиска и отметка, какие подключённые источники в него входят."""

import io
import re

from .settings import domain

KNOWN = {
    r"\bеис\b|zakupki\.gov|единая информационная система": "zakupki.gov.ru",
    r"\bгисп\b|gisp": "gisp.gov.ru",
    r"сбербанк.?аст|sberbank-ast": "sberbank-ast.ru",
    r"ртс.?тендер|rts-tender": "rts-tender.ru",
    r"росэлторг|roseltorg": "roseltorg.ru",
    r"этп\s*гпб|etpgpb": "etpgpb.ru",
    r"заказ\s*рф|zakazrf": "zakazrf.ru",
    r"тэк.?торг|tektorg": "tektorg.ru",
    r"фабрикант|fabrikant": "fabrikant.ru",
    r"\bрад\b|lot-online|российский аукционный дом": "lot-online.ru",
    r"бер[её]зк|agregatoreat": "agregatoreat.ru",
    r"портал поставщиков|zakupki\.mos": "zakupki.mos.ru",
    r"\bэтм\b|etm\.ru": "etm.ru",
    r"чип\s*и\s*дип|chipdip": "chipdip.ru",
    r"яндекс.?маркет|market\.yandex": "market.yandex.ru",
    r"\bozon\b|озон": "ozon.ru",
    r"вс[её]инструменты|vseinstrumenti": "vseinstrumenti.ru",
    r"комус|komus": "komus.ru",
    r"госзатраты|clearspending": "clearspending.ru",
}
KINDS = {
    "contract": r"реестр контрактов|исполненн\w* контракт",
    "registry": r"реестр контрактов|зеркал",
    "catalog": r"каталог\w* производител|дистрибьютор|прайс",
    "marketplace": r"маркетплейс|интернет.магазин",
    "offer": r"коммерческ\w* предложени|\bкп\b",
}
_URL = re.compile(r"(?:https?://)?(?:www\.)?((?:[a-z0-9-]+\.)+(?:ru|su|com|net|org|рф|online|shop|store|io))\b", re.I)


def text_of(data: bytes, filename: str) -> str:
    name = filename.lower()
    if name.endswith(".docx"):
        from docx import Document

        doc = Document(io.BytesIO(data))
        parts = [p.text for p in doc.paragraphs]
        for t in doc.tables:
            for row in t.rows:
                parts.append(" | ".join(c.text for c in row.cells))
        return "\n".join(parts)
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        return "\n".join(" | ".join(str(c) for c in row if c is not None)
                         for ws in wb.worksheets for row in ws.iter_rows(values_only=True))
    return data.decode("utf-8-sig", "replace")


def parse(text: str) -> dict:
    low = text.lower().replace("ё", "е")
    domains: list[str] = []
    for pat, d in KNOWN.items():
        if re.search(pat.replace("ё", "е"), low) and d not in domains:
            domains.append(d)
    for m in _URL.finditer(text):
        d = domain(m.group(1))
        if d and d not in domains and not re.fullmatch(r"\d+(\.\d+)+", d):
            domains.append(d)
    kinds = sorted(k for k, pat in KINDS.items() if re.search(pat, low))
    if "zakupki.gov.ru" in domains:
        kinds = sorted(set(kinds) | {"contract", "registry"})
    return {"domains": domains, "kinds": kinds}


def matches(found: dict, sources: list[dict]) -> dict[str, bool]:
    """Какие подключённые источники входят в утверждённый список: по сайту или по виду источника."""
    out = {}
    for c in sources:
        url = c.get("search_url") or c.get("domain") or ""
        host = domain(url) if url else ""
        by_site = bool(host) and any(host == d or host.endswith("." + d) for d in found["domains"])
        by_kind = c.get("kind") in found["kinds"]
        if c.get("type") == "eis":
            by_site = by_site or "zakupki.gov.ru" in found["domains"]
        out[c["id"]] = c.get("type") in ("manual", "web") or by_site or by_kind
    return out
