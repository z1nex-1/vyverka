import asyncio
import gzip
import json
import math
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .. import ai, analogs, report, settings
from ..config import ROOT, data_dir, rules, sources
from ..models import Decision, Evidence, Offer, Run, TraceEvent
from ..net import SnapshotStore
from ..pipeline import _one_per_supplier, override, recalc
from ..sources import catalog
from ..tz.parser import check, parse_docx, parse_text, read_text
from .runs import RUNS, start
from .viz import _anchor, cloud, ruler

HERE = Path(__file__).parent
EXAMPLES = {
    "paper": ("Бумага офисная и писчая", "3 позиции · SvetoCopy А4 и А3, офсетная 65 г/м²", "tz_paper.docx"),
    "printer": ("3D-принтер CreatBot D600 Pro", "1 позиция · 16 технических характеристик", "tz_3d_printer.docx"),
    "cytokines": ("Рекомбинантные цитокины", "5 позиций · цена за мкг и фасовка", "tz_cytokines.docx"),
    "mouse": ("Мышь компьютерная Logitech B100", "1 позиция · категория без отдельной настройки", "tz_mouse.docx"),
}
MODE = os.environ.get("VYVERKA_MODE", "cache")

app = FastAPI(title="Выверка", docs_url="/api/docs", redoc_url=None)
app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
tpl = Jinja2Templates(directory=HERE / "templates")


def _money(x) -> str:
    return report.money(x) if x is not None else "—"


def _num(x, nd: int = 2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{nd}f}".replace(",", "\u00a0").replace(".", ",")


tpl.env.filters["money"] = _money
tpl.env.filters["num"] = _num
tpl.env.globals["cpi_source"] = rules().get("indexation", {}).get("source", "Росстат")
tpl.env.globals["status_text"] = report.STATUS
tpl.env.globals["check_text"] = report.CHECK
tpl.env.globals["ai_on"] = ai.enabled()
tpl.env.globals["kind_text"] = report.KIND
tpl.env.globals["ruler"] = ruler
tpl.env.globals["anchor"] = _anchor
tpl.env.globals["cloud"] = cloud
tpl.env.globals["ring_text"] = analogs.RING_TEXT
tpl.env.globals["org"] = settings.org
tpl.env.globals["legal_basis"] = settings.legal_basis
tpl.env.globals["locked"] = settings.locked
tpl.env.globals["cur_policy"] = settings.policy
tpl.env.globals["cur_trust_min"] = settings.trust_min


def who(request: Request) -> str | None:
    """Пользователь из единого входа: обратный прокси (Keycloak, ADFS, oauth2-proxy) кладёт логин в заголовок."""
    h = os.environ.get("VYVERKA_USER_HEADER", "X-Remote-User")
    return (request.headers.get(h) or "").strip()[:80] or None


def _demo() -> dict | None:
    p = ROOT / "examples" / "demo_printer.json"
    if not p.exists():
        return None
    run = Run.model_validate_json(p.read_text(encoding="utf-8"))
    if not run.results:
        return None
    keep = data_dir() / "runs" / f"{run.id}.json"
    if not keep.exists():
        keep.parent.mkdir(parents=True, exist_ok=True)
        keep.write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    res = run.results[0]
    dec = {d.offer_id: d for d in res.decisions}
    priced = sorted((o for o in res.offers if dec[o.id].unit_price), key=lambda o: (not dec[o.id].included, dec[o.id].unit_price))
    rows = [{"title": o.title.replace("3D принтеры ", ""), "source": o.source_title.split(" — ")[0], "price": dec[o.id].unit_price,
             "included": dec[o.id].included, "why": dec[o.id].reasons[-1][1] if dec[o.id].reasons else ""} for o in priced]
    rejected = [dec[o.id] for o in res.offers if not dec[o.id].included and not dec[o.id].unit_price]
    sample = next((d.reasons[-1][1] for d in rejected if d.reasons and d.reasons[-1][0] == "spec_mismatch"
                   and "скорость" not in d.reasons[-1][1]), "")
    return {"id": run.id, "ruler": ruler(res, W=680, H=150).replace("#o-", f"/runs/{run.id}#o-"), "total": run.total,
            "found": len(res.offers), "n": res.calc.n, "cv": res.calc.cv, "rows": rows, "rejected": len(rejected),
            "sample": sample}


DEMO = _demo()


@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return tpl.TemplateResponse(request, "index.html", {"examples": EXAMPLES, "recent": RUNS.recent(), "demo": DEMO,
                                                        "sources": catalog(), "mode": MODE,
                                                        "policy": settings.policy(), "trust_min": settings.trust_min(),
                                                        "nav": "new"})


@app.get("/runs", response_class=HTMLResponse)
async def runs_list(request: Request):
    return tpl.TemplateResponse(request, "runs.html", {"runs": RUNS.recent(200), "nav": "runs"})


@app.get("/healthz")
async def health():
    return {"status": "ok", "mode": MODE}


def read_spec(data: bytes | None = None, name: str = "", text: str | None = None, example: str | None = None):
    """ТЗ из файла, текста или примера кейса — одинаково для формы, API и консоли."""
    if example:
        if example not in EXAMPLES:
            raise HTTPException(404, "Нет такого примера")
        path = ROOT / "examples" / EXAMPLES[example][2]
        spec = parse_docx(path.read_bytes(), EXAMPLES[example][2])
    elif data:
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(413, "Файл больше 10 МБ")
        if name.lower().endswith(".docx"):
            try:
                spec = parse_docx(data, name)
            except Exception:  # noqa: BLE001
                raise HTTPException(422, "Не удалось прочитать DOCX: файл повреждён или это не документ Word")
        elif name.lower().endswith((".txt", ".md")):
            try:
                spec = parse_text(read_text(data), name)
            except ValueError as e:
                raise HTTPException(422, str(e))
        else:
            raise HTTPException(415, "Поддерживаются ТЗ в формате DOCX или TXT")
    elif text and text.strip():
        spec = parse_text(text, "Описание из формы")
    else:
        raise HTTPException(400, "Загрузите ТЗ, выберите пример или опишите товар")
    if not spec.items:
        raise HTTPException(422, "В документе не найдена таблица с позициями закупки")
    try:
        check(spec)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return spec


def pick_mode(mode: str | None) -> str:
    return mode if mode in ("live", "cache", "offline") else MODE


@app.post("/runs")
async def create(request: Request, file: UploadFile | None = File(None), example: str | None = Form(None),
                 text: str | None = Form(None), mode: str | None = Form(None), policy: str | None = Form(None),
                 notice: str | None = Form(None), method: str | None = Form(None)):
    data = await file.read() if file and file.filename else None
    spec = read_spec(data, file.filename if file else "", text, example)
    spec.notice = (notice or "").strip()[:60] or spec.notice
    spec.method = (method or "").strip()[:120] or spec.method
    rid = await start(spec, pick_mode(mode), policy=policy, user=who(request))
    return RedirectResponse(f"/runs/{rid}", status_code=303)


@app.get("/runs/{rid}", response_class=HTMLResponse)
async def show(request: Request, rid: str):
    run = RUNS.get(rid)
    if not run:
        raise HTTPException(404, "Расчёт не найден")
    tiers = {s["id"]: s.get("tier") for s in sources().get("sources", [])}
    return tpl.TemplateResponse(request, "run.html", {"run": run, "tiers": tiers, "sources": catalog(),
                                                      "open_mode": run.settings.get("policy") == "open",
                                                      "trust_min": run.settings.get("trust_min", 60), "nav": "runs"})


@app.get("/runs/{rid}/events")
async def events(rid: str):
    run = RUNS.get(rid)
    if not run:
        raise HTTPException(404)

    async def stream():
        q: asyncio.Queue = asyncio.Queue()
        RUNS.waiters.setdefault(rid, []).append(q)
        try:
            for ev in list(run.trace):
                yield _sse(ev)
            if run.status != "running":
                yield "event: end\ndata: {}\n\n"
                return
            while True:
                ev = await q.get()
                if ev is None:
                    yield "event: end\ndata: {}\n\n"
                    return
                yield _sse(ev)
        finally:
            RUNS.waiters[rid].remove(q)

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


def _sse(ev: TraceEvent) -> str:
    return "data: " + json.dumps({"stage": ev.stage, "message": ev.message, "at": ev.at.strftime("%H:%M:%S"),
                                  "data": ev.data}, ensure_ascii=False, default=str) + "\n\n"


@app.get("/api/runs/{rid}")
async def api_run(rid: str):
    run = RUNS.get(rid)
    if not run:
        raise HTTPException(404)
    return JSONResponse(json.loads(run.model_dump_json()))


@app.get("/runs/{rid}/rfq.docx")
async def download_rfq(rid: str):
    run = RUNS.get(rid)
    if not run or run.status == "running":
        raise HTTPException(404)
    return Response(report.rfq(run, settings.org()),
                    media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                    headers={"Content-Disposition": f'attachment; filename="zapros-cen-{run.id}.docx"'})


@app.post("/runs/{rid}/offers/{oid:path}/decision")
async def decision(request: Request, rid: str, oid: str, action: str = Form(...), comment: str = Form("")):
    """Закупщик принимает однородный товар или исключает предложение — с комментарием, позиция пересчитывается."""
    run = RUNS.get(rid)
    if not run or run.status == "running":
        raise HTTPException(404)
    res = next((r for r in run.results if any(o.id == oid for o in r.offers)), None)
    if not res:
        raise HTTPException(404, "Предложение не найдено")
    try:
        override(run, res, oid, action, comment, who(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    await asyncio.to_thread(RUNS.save, run)
    return RedirectResponse(f"/runs/{rid}#o-{_anchor(oid)}", status_code=303)


@app.get("/runs/{rid}/report.{ext}")
async def download(rid: str, ext: str):
    run = RUNS.get(rid)
    if not run or run.status == "running":
        raise HTTPException(404)
    kinds = {
        "docx": (report.docx, "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "public.docx": (lambda r: report.docx(r, public=True), "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        "xlsx": (report.xlsx, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        "json": (report.json_trace, "application/json"),
    }
    if ext not in kinds:
        raise HTTPException(404)
    make, mime = kinds[ext]
    return Response(make(run), media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{report.filename(run, ext.split(".")[-1], "-public" if ext.startswith("public") else "")}"'})


def add_kp(run: Run, res, supplier: str, price: float, doc: str = "", note: str = "") -> Offer:
    n = sum(1 for o in res.offers if o.source == "manual") + 1
    title = f"Коммерческое предложение: {supplier.strip()[:120]}"
    now = datetime.now(timezone.utc)
    pos = res.item.pos
    offer = Offer(id=f"manual:{pos}:{n}", source="manual", source_title="Коммерческое предложение", kind="offer",
                  url=f"/runs/{run.id}#item-{pos}", title=title, price=price, unit=res.item.unit,
                  supplier=supplier.strip()[:120], doc_number=doc.strip()[:60] or None, doc_date=date.today(),
                  text=note.strip()[:500], evidence=Evidence(url=f"/runs/{run.id}", fetched_at=now,
                                                            excerpt=note.strip()[:300] or "загружено пользователем"))
    res.offers.append(offer)
    res.decisions.append(Decision(offer_id=offer.id, included=True, score=1.0, unit_price=round(price, 2),
                                  reasons=[("comparable", "КП поставщика: соответствие ТЗ подтверждает поставщик")]))
    _one_per_supplier(res.offers, {d.offer_id: d for d in res.decisions})
    recalc(run, res, why=f"добавлено КП {supplier.strip()[:60]}")
    return offer


@app.post("/runs/{rid}/items/{pos}/offers")
async def add_offer(rid: str, pos: int, supplier: str = Form(...), price: float = Form(...),
                    doc: str = Form(""), note: str = Form("")):
    """Коммерческое предложение, полученное по запросу: добавляется в выборку позиции и запускает пересчёт."""
    run = RUNS.get(rid)
    if not run or run.status == "running":
        raise HTTPException(404)
    res = next((r for r in run.results if r.item.pos == pos), None)
    if not res or not math.isfinite(price) or not 0 < price < 1e11:
        raise HTTPException(400, "Неверная позиция или цена")
    add_kp(run, res, supplier, price, doc, note)
    await asyncio.to_thread(RUNS.save, run)
    return RedirectResponse(f"/runs/{rid}#item-{pos}", status_code=303)


@app.get("/snapshot/{key}")
async def snapshot(key: str, sha: str = "", price: float | None = None, hl: str = ""):
    """Сохранённая копия страницы-источника. Скрипты страницы отключены политикой sandbox.
    sha — версия копии, на которую ссылается расчёт; price и hl — что подсветить в тексте."""
    if not re.fullmatch(r"[a-f0-9]{32}", key) or (sha and not re.fullmatch(r"[a-f0-9]{64}", sha)):
        raise HTTPException(404)
    store = SnapshotStore()
    path = store.version_path(key, sha) if sha else store.body_path(key)
    if not path:
        raise HTTPException(404, "Копия не сохранена")
    meta = json.loads(path.with_suffix(".json").read_text(encoding="utf-8"))
    body = gzip.decompress(path.read_bytes())
    terms = [t for t in hl.split("|") if t.strip()][:12]
    if (price and math.isfinite(price)) or terms:
        from .highlight import render

        body = render(body, meta, price, terms).encode("utf-8")
        ctype = "text/html; charset=utf-8"
    else:
        ctype = "application/json" if body.lstrip()[:1] in (b"{", b"[") else "text/html; charset=utf-8"
    return Response(body, media_type=ctype, headers={
        "Content-Security-Policy": "sandbox; default-src 'none'; img-src * data:; style-src * 'unsafe-inline'",
        "X-Source-Url": meta["url"].encode("ascii", "ignore").decode(), "X-Fetched-At": meta["fetched_at"],
        "X-Sha256": meta["sha256"]})


from . import api, admin  # noqa: E402 — маршруты настроек и API подключаются к готовому приложению

app.include_router(admin.router)
app.include_router(api.router)
