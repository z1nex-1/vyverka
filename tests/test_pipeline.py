from conftest import offer
from vyverka.models import Decision, Item
from vyverka.pipeline import _one_per_product, dedup


def _decisions(offers):
    return {o.id: Decision(offer_id=o.id, included=True, score=1.0, unit_price=o.price) for o in offers}


def test_one_price_per_product_takes_nearest_pack_not_smaller_than_tz():
    item = Item(pos=1, name="rhIL-7", unit="шт", qty=1, pack_amount=10, pack_unit="мкг", category="reagents")
    packs = [offer(f"Интерлейкин 7, {a}", p, id=f"d:{a}", url=f"https://dia-m.test/il7#{a}")
             for a, p in (("2 мкг", 9000), ("10 мкг", 33573), ("50 мкг", 120000))]
    dec = _decisions(packs)
    _one_per_product(item, packs, dec)
    kept = [o.title for o in packs if dec[o.id].included]
    assert kept == ["Интерлейкин 7, 10 мкг"]
    assert all(dec[o.id].reasons[-1][0] == "same_product" for o in packs if not dec[o.id].included)


def test_contract_and_its_mirror_are_one_price():
    a = offer("Бумага А4", 300, id="contract:123", source="eis", kind="contract")
    b = offer("Бумага А4", 300, id="contract:123", source="clearspending", kind="contract")
    kept, dups = dedup([a, b])
    assert len(kept) == 1 and len(dups) == 1
