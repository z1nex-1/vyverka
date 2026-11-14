import io
import time

import openpyxl

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(monkeypatch):
    from vyverka.web import app as web
    from vyverka.web.runs import RUNS
    RUNS.live.clear()
    with TestClient(web.app) as c:
        yield c


def wait(client, rid, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        run = client.get(f"/api/runs/{rid}").json()
        if run["status"] != "running":
            return run
        time.sleep(0.3)
    raise TimeoutError(rid)


def test_home_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and "Рассчитать НМЦД" in r.text
    assert client.get("/healthz").json()["status"] == "ok"


def test_printer_example_offline_end_to_end(client):
    r = client.post("/runs", data={"example": "printer", "mode": "offline"}, follow_redirects=False)
    assert r.status_code == 303
    rid = r.headers["location"].rsplit("/", 1)[1]
    run = wait(client, rid)
    assert run["total"] == pytest.approx(1_183_333.33)
    page = client.get(f"/runs/{rid}")
    assert "1 183 333,33" in page.text and "Не вошли в расчёт" in page.text
    for ext, magic in (("docx", b"PK"), ("xlsx", b"PK"), ("json", b"{")):
        body = client.get(f"/runs/{rid}/report.{ext}").content
        assert body.startswith(magic)
    ws = openpyxl.load_workbook(io.BytesIO(client.get(f"/runs/{rid}/report.xlsx").content)).active
    formulas = [str(c.value) for c in ws[5] if isinstance(c.value, str) and c.value.startswith("=")]
    assert "=COUNT(E5:G5)" in formulas and "=ROUND(AVERAGE(E5:G5),2)" in formulas
    rep = client.get(f"/api/v1/runs/{rid}/replay").json()
    assert rep["ok"] and rep["replayed_total"] == pytest.approx(1_183_333.33)
    import docx as _docx
    full = client.get(f"/runs/{rid}/report.docx").content
    pub = client.get(f"/runs/{rid}/report.public.docx")
    assert pub.status_code == 200 and "-public.docx" in pub.headers["content-disposition"]
    text = lambda b: "\n".join(p.text for p in _docx.Document(io.BytesIO(b)).paragraphs)
    t_full, t_pub = text(full), text(pub.content)
    assert "3Dtool" in t_full and "http" in t_full
    assert "3Dtool" not in t_pub and "http" not in t_pub and "дилер" not in t_pub
    assert "наименования поставщиков" in t_pub and "1 183 333,33" in t_pub


def test_commercial_offer_completes_position(client):
    r = client.post("/runs", data={"example": "cytokines", "mode": "offline"}, follow_redirects=False)
    rid = r.headers["location"].rsplit("/", 1)[1]
    run = wait(client, rid)
    il7 = next(x for x in run["results"] if x["item"]["pos"] == 3)
    assert il7["calc"]["status"] == "insufficient" and il7["calc"]["n"] == 2
    client.post(f"/runs/{rid}/items/3/offers", data={"supplier": "ООО «Тест»", "price": "26000", "doc": "№ 1"})
    il7 = next(x for x in client.get(f"/api/runs/{rid}").json()["results"] if x["item"]["pos"] == 3)
    assert il7["calc"]["status"] == "ok" and il7["calc"]["n"] == 3
    rep = client.get(f"/api/v1/runs/{rid}/replay").json()["items"][2]
    assert rep["ok"], rep


def test_bad_uploads_get_clear_errors(client):
    r = client.post("/runs", files={"file": ("tz.pdf", b"%PDF-1.4", "application/pdf")})
    assert r.status_code == 415
    r = client.post("/runs", files={"file": ("tz.docx", b"not a zip", "application/octet-stream")})
    assert r.status_code == 422
    assert client.get("/runs/000000000000").status_code == 404
    assert client.get("/snapshot/../../etc/passwd").status_code == 404
