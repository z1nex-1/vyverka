import io
import time
from datetime import date, timedelta

import docx
import pytest
from fastapi.testclient import TestClient

from vyverka import settings, source_import
from vyverka.config import load, rules, sources
from vyverka.models import Decision
from vyverka.sources import catalog
from vyverka.sources.web import WebSearch, extract
from vyverka.trust import score

from conftest import EXAMPLES, offer

SHOP = """<html><head><title>Магазин</title>
<script type="application/ld+json">{"@context":"https://schema.org","@type":"Product","name":"Бумага SvetoCopy A3",
"description":"Формат А3, 80 г/м2, 500 листов","offers":{"@type":"Offer","price":"712.40","priceCurrency":"RUB",
"availability":"https://schema.org/InStock"}}</script></head>
<body><h1>Бумага SvetoCopy A3</h1><p>ООО «Канцмир», ИНН 7701234567</p></body></html>"""


@pytest.fixture
def client():
    from vyverka.web import app as web
    from vyverka.web.runs import RUNS
    RUNS.live.clear()
    with TestClient(web.app) as c:
        yield c


def wait(client, rid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = client.get(f"/api/v1/runs/{rid}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.3)
    raise TimeoutError(rid)


def test_profiles_and_overlay():
    assert {p["id"] for p in settings.profiles()} >= {"mfti", "vuz44", "research"}
    assert settings.org()["name"] == "МФТИ" and settings.policy() == "strict"
    settings.update(lambda d: d.update(profile="vuz44"))
    assert settings.org()["law"] == "44"
    settings.set_method({"cv_limit": "25", "min_offers": "5"})
    assert rules()["method"]["cv_limit"] == 25.0 and rules()["method"]["min_offers"] == 5
    assert rules()["method"]["outlier"] == load("rules")["method"]["outlier"]
    settings.reset()
    assert settings.profile_id() == "mfti"


def test_source_pause_until_date_and_custom_source():
    settings.set_source("ymarket", enabled=False, until=(date.today() + timedelta(days=5)).isoformat())
    row = next(c for c in catalog() if c["id"] == "ymarket")
    assert row["state"] == "paused"
    settings.set_source("ymarket", enabled=False, until=(date.today() - timedelta(days=1)).isoformat())
    assert next(c for c in catalog() if c["id"] == "ymarket")["state"] == "on"

    conf = settings.add_custom("ООО Лабтех", "https://www.labtech.ru/catalog", "catalog", 80)
    assert conf["type"] == "site" and "labtech.ru" in settings.whitelist()
    tpl = settings.add_custom("Лабтех поиск", "https://labtech.ru/search?q={q}", "catalog")
    eff = {c["id"]: c for c in sources()["sources"]}
    assert eff[tpl["id"]]["type"] == "jsonld" and eff[conf["id"]]["trust"] == 80
    with pytest.raises(ValueError):
        settings.add_custom("bad", "ftp://x/{q}")
    assert settings.remove_custom(conf["id"]) and not settings.remove_custom("eis")


def test_import_of_case_source_list_marks_every_connected_source():
    text = (EXAMPLES.parent / "materials" / "sources.txt")
    body = text.read_text(encoding="utf-8") if text.exists() else (
        "ЕИС zakupki.gov.ru\nГИСП gisp.gov.ru\nЯндекс Маркет\nкаталоги поставщиков\nкоммерческие предложения")
    found = source_import.parse(body)
    assert "zakupki.gov.ru" in found["domains"]
    marks = source_import.matches(found, settings.effective_sources(load("sources")["sources"]))
    assert marks["eis"] and marks["manual"]


def test_trust_strict_is_full_and_open_scores_page_signals():
    o = offer("Бумага А3", 700, kind="web", meta={"domain": "komus.ru", "structured": "json-ld", "inn": "7701234567",
                                                   "in_stock": True})
    d = Decision(offer_id=o.id, included=True, score=1.0)
    assert score(o, d, {"kind": "web"}, "strict")[0] == 100
    good, factors = score(o, d, {"kind": "web"}, "open")
    far = offer("Paper A3", 700, kind="web", meta={"domain": "shop.example.com"})
    bad, _ = score(far, Decision(offer_id=far.id, included=True, score=1.0), {"kind": "web"}, "open")
    assert good > 80 > 60 > bad
    assert any("ИНН" in f for f, _ in factors)


def test_web_extract_reads_schema_org_and_skips_out_of_stock():
    title, price, attrs, desc, meta = extract(SHOP)
    assert price == 712.40 and "A3" in title and meta["inn"] == "7701234567" and meta["in_stock"]
    assert extract(SHOP.replace("InStock", "OutOfStock")) is None
    assert extract(SHOP.replace("RUB", "USD")) is None


def test_web_search_strict_keeps_only_listed_sites():
    serp = ('<div data-type="web"><a href="https://komus.ru/p/123">a</a></div>'
            '<div data-type="web"><a href="https://random-shop.biz/p/1">b</a></div>'
            '<div data-type="web"><a href="https://ru.wikipedia.org/wiki/X">c</a></div>')
    strict = WebSearch(None, {"id": "web", "policy": "strict"}).links(serp, "search.brave.com")
    opened = WebSearch(None, {"id": "web", "policy": "open"}).links(serp, "search.brave.com")
    assert strict == ["https://komus.ru/p/123"]
    assert opened == ["https://komus.ru/p/123", "https://random-shop.biz/p/1"]


def test_analogs_cloud_estimate_and_user_decision(client):
    r = client.post("/runs", data={"example": "cytokines", "mode": "offline"}, follow_redirects=False)
    rid = r.headers["location"].rsplit("/", 1)[-1]
    run = wait(client, rid)
    full = client.get(f"/api/v1/runs/{rid}/full").json()
    il2 = next(x for x in full["results"] if "IL-2" in x["item"]["name"])
    titles = {o["id"]: o["title"] for o in il2["offers"]}
    near = [a for a in il2["analogs"] if a["ring"] != "distant"]
    assert not any("IL-4" in titles[a["offer_id"]] or "Интерлейкин-4" in titles[a["offer_id"]] for a in near)
    sims = [a["similarity"] for a in il2["analogs"]]
    assert sims == sorted(sims, reverse=True)

    page = client.get(f"/runs/{rid}")
    assert page.status_code == 200 and 'class="cloud"' in page.text and "Запрос КП" in page.text

    pick = next((x, d) for x in full["results"] for d in x["decisions"] if not d["included"] and d["unit_price"])
    pos, cand = pick[0]["item"]["pos"], pick[1]
    bad = client.post(f"/api/v1/runs/{rid}/decisions", json={"offer_id": cand["offer_id"], "action": "include"})
    assert bad.status_code == 422
    ok = client.post(f"/api/v1/runs/{rid}/decisions",
                     json={"offer_id": cand["offer_id"], "action": "include", "comment": "та же активность, другой производитель"})
    assert ok.status_code == 200 and ok.json()["calc"]["n"] >= 1
    item = next(x for x in client.get(f"/api/v1/runs/{rid}/full").json()["results"] if x["item"]["pos"] == pos)
    d = next(x for x in item["decisions"] if x["offer_id"] == cand["offer_id"])
    assert d["included"] and d["override"]["comment"].startswith("та же")
    client.post(f"/api/v1/runs/{rid}/decisions", json={"offer_id": cand["offer_id"], "action": "reset"})
    item = next(x for x in client.get(f"/api/v1/runs/{rid}/full").json()["results"] if x["item"]["pos"] == pos)
    assert not next(x for x in item["decisions"] if x["offer_id"] == cand["offer_id"])["override"]

    rfq = client.get(f"/runs/{rid}/rfq.docx")
    text = "\n".join(p.text for p in docx.Document(io.BytesIO(rfq.content)).paragraphs)
    assert rfq.status_code == 200 and "Запрос ценовой информации" in text
    assert run["status"] in ("ok", "partial")


def test_api_keys_and_settings_endpoints(client, monkeypatch):
    monkeypatch.setenv("VYVERKA_API_KEYS", "s3cret,other")
    assert client.get("/api/v1/sources").status_code == 401
    h = {"X-API-Key": "s3cret"}
    assert client.get("/api/v1/sources", headers={"Authorization": "Bearer other"}).status_code == 200
    r = client.patch("/api/v1/settings", headers=h, json={"policy": "open", "trust_min": 70})
    assert r.json()["policy"] == "open" and r.json()["trust_min"] == 70
    r = client.patch("/api/v1/sources/ymarket", headers=h, json={"until": "2099-01-01"})
    assert r.json()["state"] == "paused"
    r = client.post("/api/v1/sources", headers=h, json={"title": "Лабтех", "url": "labtech.ru", "trust": 85})
    assert r.status_code == 201
    assert client.delete(f"/api/v1/sources/{r.json()['id']}", headers=h).status_code == 200
    r = client.post("/api/v1/runs", headers=h, data={"example": "printer", "mode": "offline", "wait_seconds": "60"})
    assert r.json()["total"] == 1183333.33 and r.json()["policy"] == "open"
    assert client.post("/api/v1/runs", headers=h, data={"example": "paper", "callback_url": "http://10.0.0.1/x"}).status_code == 422
    monkeypatch.setenv("VYVERKA_LOCK_SETTINGS", "1")
    assert client.patch("/api/v1/settings", headers=h, json={"policy": "strict"}).status_code == 403


def test_settings_page_forms(client):
    assert client.get("/settings").status_code == 200 and client.get("/integration").status_code == 200
    client.post("/settings/policy", data={"policy": "open", "trust_min": "65"})
    assert settings.policy() == "open" and settings.trust_min() == 65
    client.post("/settings/sources/ymarket", data={"action": "off"})
    assert next(c for c in catalog() if c["id"] == "ymarket")["state"] == "off"
    client.post("/settings/sources", data={"title": "Химмед", "url": "https://chimmed.ru/search?q={q}", "listed": "1"})
    assert any(c["title"] == "Химмед" for c in catalog())
    client.post("/settings/import", data={"text": "ЕИС zakupki.gov.ru\nКомус komus.ru", "mode": "replace"})
    assert settings.whitelist() == ["komus.ru", "zakupki.gov.ru"]
    r = client.post("/settings/import", files={"file": ("list.txt", "Перечень: ГИСП gisp.gov.ru".encode())})
    assert r.status_code == 200 and "gisp.gov.ru" in settings.whitelist()
    exported = client.get("/settings/export.json").content
    client.post("/settings/reset")
    assert settings.policy() == "strict"
    client.post("/settings/restore", files={"file": ("s.json", exported)})
    assert settings.policy() == "open"
