import asyncio

import httpx
import pytest
import respx

from conftest import FIXTURES
from vyverka.models import Item
from vyverka.net import Fetcher, SnapshotStore, SourceUnavailable
from vyverka.pipeline import Pipeline
from vyverka.sources.html import HtmlCatalog, LayoutChanged

CONF = {"id": "shop", "type": "html", "title": "Магазин", "search_url": "https://shop.test/search/?q={q}", "limit": 10,
        "selectors": {"card": ".b-product-block__content", "name": ".b-product-block__main-link",
                      "title": ".b-product-block__name", "price": ".b-product-block__visible-price"}}
ITEM = Item(pos=1, name="3D принтер CreatBot D600 Pro", unit="шт", qty=1, brand="CreatBot", model="D600 Pro",
            category="printer3d")


def fetcher(tmp_path, mode="live"):
    return Fetcher(mode=mode, store=SnapshotStore(root=tmp_path / "snap", seed=tmp_path / "seed"), retries=0)


@respx.mock
def test_catalog_cards_become_offers_with_evidence(tmp_path):
    respx.get(url__startswith="https://shop.test/search/").respond(200, text=(FIXTURES / "catalog.html").read_text())
    f = fetcher(tmp_path)
    offers = asyncio.run(HtmlCatalog(f, CONF).search(ITEM, "CreatBot D600 PRO"))
    assert [o.title for o in offers][0] == "3D принтер CreatBot D600 PRO"
    assert offers[0].price == 900000
    assert offers[0].url == "https://shop.test/product/d600pro/"
    assert offers[0].evidence.sha256 and offers[0].evidence.snapshot


@respx.mock
def test_changed_layout_is_reported_not_crashing(tmp_path):
    respx.get(url__startswith="https://shop.test/search/").respond(200, text="<html><body>" + "<div>x</div>" * 400)
    with pytest.raises(LayoutChanged):
        asyncio.run(HtmlCatalog(fetcher(tmp_path), CONF).search(ITEM, "q"))


@respx.mock
def test_network_failure_falls_back_to_snapshot(tmp_path):
    route = respx.get(url__startswith="https://shop.test/search/")
    route.respond(200, text=(FIXTURES / "catalog.html").read_text())
    asyncio.run(HtmlCatalog(fetcher(tmp_path), CONF).search(ITEM, "q"))
    route.side_effect = httpx.ConnectError("down")
    offers = asyncio.run(HtmlCatalog(fetcher(tmp_path), CONF).search(ITEM, "q"))
    assert offers and offers[0].evidence.from_cache


@respx.mock
def test_network_failure_without_snapshot_is_unavailable(tmp_path):
    respx.get(url__startswith="https://shop.test/").mock(side_effect=httpx.ConnectTimeout("slow"))
    with pytest.raises(SourceUnavailable, match="таймаут"):
        asyncio.run(fetcher(tmp_path).get("https://shop.test/search/?q=x"))


def test_offline_mode_never_touches_network(tmp_path):
    with respx.mock(assert_all_called=False) as mock:
        route = mock.get(url__startswith="https://")
        with pytest.raises(SourceUnavailable):
            asyncio.run(fetcher(tmp_path, mode="offline").get("https://shop.test/search/?q=x"))
        assert not route.called


def test_pipeline_survives_all_sources_down(tmp_path, monkeypatch):
    from vyverka.tz.parser import parse_text
    monkeypatch.setenv("VYVERKA_DATA", str(tmp_path))
    spec = parse_text("Бумага офисная А4 SvetoCopy — 10 пачек", "форма")
    with respx.mock(assert_all_called=False) as mock:
        mock.route().mock(side_effect=httpx.ConnectError("down"))
        run = asyncio.run(Pipeline(mode="live").execute(spec))
    (res,) = run.results
    assert res.calc.status == "insufficient"
    assert res.sources and all(s.status in ("unavailable", "error", "empty") for s in res.sources)


def test_snapshot_found_after_search_window_moves(tmp_path):
    from vyverka.net import SnapshotStore
    store = SnapshotStore(root=tmp_path / "s", seed=tmp_path / "seed")
    base = "https://zakupki.gov.ru/epz/contract/search/results.html?searchString=бумага"
    store.put(base + "&contractDateFrom=12.11.2023", 200, "<html>вчера</html>")
    page = SnapshotStore(root=tmp_path / "s", seed=tmp_path / "seed").get(base + "&contractDateFrom=13.11.2023")
    assert page is not None and page.text == "<html>вчера</html>"
    assert store.get(base.replace("бумага", "картридж") + "&contractDateFrom=13.11.2023") is None


def test_paper_example_does_not_depend_on_calendar(monkeypatch):
    import asyncio
    from datetime import date
    from vyverka.sources import clearspending, eis
    from vyverka.pipeline import Pipeline
    from vyverka.tz.parser import parse_docx

    class Later(date):
        @classmethod
        def today(cls):
            return date(2026, 11, 15)

    monkeypatch.setattr(eis, "date", Later)
    monkeypatch.setattr(clearspending, "date", Later)
    from pathlib import Path
    tz = Path(__file__).resolve().parent.parent / "examples" / "tz_paper.docx"
    run = asyncio.run(Pipeline(mode="offline").execute(parse_docx(tz.read_bytes(), tz.name)))
    a4 = next(r for r in run.results if "А4 SvetoCopy" in r.item.name)
    assert a4.calc.n == 7 and a4.calc.total == 808425
