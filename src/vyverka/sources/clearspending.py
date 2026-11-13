import json
from datetime import date, timedelta

from ..match import name_similarity
from ..models import Item, Offer
from .base import Source, clean, person_inn, ru_date

API = "https://openapi.clearspending.ru/restapi/v3/contracts/search/"


class ClearSpending(Source):
    """Открытое зеркало реестра контрактов ЕИС (44-ФЗ и 223-ФЗ) с позициями и ценой за единицу."""

    name = "clearspending"
    title = "Госзатраты (зеркало реестра контрактов ЕИС)"
    kind = "contract"

    async def search(self, item: Item, query: str) -> list[Offer]:
        years = int(self.conf.get("years", 3))
        since = date.today() - timedelta(days=365 * years)
        params = {"productsearch": query, "sort": "-signDate", "perpage": str(max(self.limit * 3, 20))}
        page = await self.fetcher.get(API, params)
        data = json.loads(page.text).get("contracts", {}).get("data", [])
        target = f"{item.name} {item.brand or ''} {item.model or ''}"
        out: list[Offer] = []
        for c in data:
            signed = ru_date(c.get("signDate"))
            if signed and signed < since:
                continue
            stage = c.get("currentContractStage")
            if stage and stage not in ("EC", "E"):
                continue
            products = c.get("products") or []
            if not products:
                continue
            best = max(products, key=lambda p: name_similarity(target, p.get("name", "")))
            if name_similarity(target, best.get("name", "")) < 0.2 or not best.get("price"):
                continue
            reg = c.get("regNum")
            customer = (c.get("customer") or {}).get("fullName")
            suppliers = c.get("suppliers") or []
            sup = None
            if suppliers:
                s = suppliers[0]
                sup = s.get("organizationName") or ("ИП" if s.get("lastName") else None)
                if sup and (sup.upper().startswith("ИП ") or len(str(s.get("inn") or "")) == 12):
                    sup = "ИП"
                if s.get("inn"):
                    sup = f"{sup or 'Поставщик'}, ИНН {person_inn(s['inn'])}"
            url = f"https://zakupki.gov.ru/epz/contract/contractCard/common-info.html?reestrNumber={reg}"
            text = clean(best.get("name"))
            out.append(Offer(
                id=f"eis:{reg}:{best.get('ordinalNumber', 1)}", source=self.name, source_title=self.title, kind=self.kind,
                url=url, title=clean(best.get("name")), price=float(best["price"]),
                unit=(best.get("OKEI") or {}).get("name"), qty=_f(best.get("quantity")), supplier=sup,
                customer=customer, doc_number=reg, doc_date=signed, okpd2=(best.get("OKPD2") or {}).get("code"),
                text=text, stage="Исполнение завершено" if stage == "EC" else "Исполнение",
                penalties=None, evidence=self.evidence(page, f"{reg}: {best.get('name')} — {best.get('price')} ₽"),
                query=query))
            if len(out) >= self.limit:
                break
        return out


def _f(x) -> float | None:
    try:
        return float(x)
    except (TypeError, ValueError):
        return None
