"""API v1 для встраивания в контур заказчика: ЭДО, 1С, портал закупок, скрипты закупщика.

Ключи задаются переменной VYVERKA_API_KEYS (через запятую). Если она пуста, API открыт —
так удобно на стенде, в закрытом контуре ключи включаются одной переменной."""

import asyncio
import hmac
import os

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from .. import report, settings
from ..config import load
from ..pipeline import override
from ..sources import catalog
from .runs import RUNS, start, summary, wait, webhook_allowed

DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"


def _keys() -> list[str]:
    return [k.strip() for k in os.environ.get("VYVERKA_API_KEYS", "").split(",") if k.strip()]


def auth(x_api_key: str | None = Header(None), authorization: str | None = Header(None)) -> str | None:
    keys = _keys()
    if not keys:
        return None
    got = x_api_key or (authorization[7:] if authorization and authorization.lower().startswith("bearer ") else "")
    for k in keys:
        if got and hmac.compare_digest(got.encode(), k.encode()):
            return "key:" + k[:4]
    raise HTTPException(401, "Нужен ключ API: заголовок X-API-Key или Authorization: Bearer")


router = APIRouter(prefix="/api/v1", dependencies=[Depends(auth)], tags=["API v1"])


def _user(request: Request) -> str | None:
    from .app import who
    return who(request)


def _run(rid: str, ready: bool = True):
    run = RUNS.get(rid)
    if not run:
        raise HTTPException(404, "Расчёт не найден")
    if ready and run.status == "running":
        raise HTTPException(409, "Расчёт ещё идёт")
    return run


def _item(run, pos: int):
    res = next((r for r in run.results if r.item.pos == pos), None)
    if not res:
        raise HTTPException(404, "Нет такой позиции")
    return res


@router.post("/runs", summary="Запустить расчёт НМЦД")
async def create(request: Request, file: UploadFile | None = File(None), text: str | None = Form(None),
                 example: str | None = Form(None), mode: str | None = Form(None), policy: str | None = Form(None),
                 callback_url: str | None = Form(None), wait_seconds: int = Form(0)):
    """ТЗ — файлом DOCX/TXT, текстом или ключом примера (paper, printer, cytokines, mouse).
    `policy`: strict — только утверждённые источники, open — любые с оценкой доверия.
    `callback_url` — POST со сводкой по завершении (хост должен быть в VYVERKA_WEBHOOK_HOSTS).
    `wait_seconds` до 120 — дождаться результата в том же ответе."""
    from .app import pick_mode, read_spec
    if callback_url and not webhook_allowed(callback_url):
        raise HTTPException(422, "Адрес обратного вызова не разрешён (VYVERKA_WEBHOOK_HOSTS)")
    if policy and policy not in ("strict", "open"):
        raise HTTPException(422, "policy: strict или open")
    data = await file.read() if file and file.filename else None
    spec = read_spec(data, file.filename if file else "", text, example)
    rid = await start(spec, pick_mode(mode), policy=policy, user=_user(request), callback=callback_url)
    if wait_seconds > 0:
        run = await wait(rid, min(wait_seconds, 120))
        return summary(run)
    return {"id": rid, "status": "running", "url": f"/runs/{rid}", "summary": f"/api/v1/runs/{rid}"}


@router.get("/runs", summary="Последние расчёты")
async def recent(limit: int = 20):
    return RUNS.recent(max(1, min(limit, 100)))


@router.get("/runs/{rid}", summary="Сводка расчёта: НМЦД и статус по позициям")
async def get_summary(rid: str):
    return summary(_run(rid, ready=False))


@router.get("/runs/{rid}/full", summary="Полный журнал: предложения, причины, доказательства")
async def get_full(rid: str):
    return Response(report.json_trace(_run(rid)), media_type="application/json")


@router.get("/runs/{rid}/replay", summary="Пересчитать журнал без сети и сверить снимки страниц по SHA-256")
async def get_replay(rid: str):
    from dataclasses import asdict

    from ..replay import replay

    rep = replay(_run(rid))
    return {**asdict(rep), "ok": rep.ok, "items": [{**asdict(i), "ok": i.ok} for i in rep.items]}


@router.get("/runs/{rid}/report.{ext}", summary="Обоснование DOCX, таблица XLSX или запрос КП")
async def get_report(rid: str, ext: str):
    run = _run(rid)
    kinds = {"docx": (report.docx, DOCX), "xlsx": (report.xlsx, XLSX),
             "rfq.docx": (lambda r: report.rfq(r, settings.org()), DOCX)}
    if ext not in kinds:
        raise HTTPException(404, "Форматы: docx, xlsx, rfq.docx")
    make, mime = kinds[ext]
    return Response(make(run), media_type=mime,
                    headers={"Content-Disposition": f'attachment; filename="{report.filename(run, ext.split(".")[-1])}"'})


@router.get("/runs/{rid}/items/{pos}/analogs", summary="Облако похожих товаров и справочная оценка")
async def get_analogs(rid: str, pos: int):
    res = _item(_run(rid), pos)
    return {"estimate": res.estimate, "analogs": [a.model_dump() for a in res.analogs]}


class Kp(BaseModel):
    supplier: str = Field(..., max_length=200)
    price: float = Field(..., gt=0, lt=1e11, allow_inf_nan=False)
    doc: str = ""
    note: str = ""


@router.post("/runs/{rid}/items/{pos}/offers", summary="Добавить коммерческое предложение и пересчитать")
async def add_kp(rid: str, pos: int, kp: Kp):
    from .app import add_kp as add
    run = _run(rid)
    res = _item(run, pos)
    offer = add(run, res, kp.supplier, kp.price, kp.doc, kp.note)
    await asyncio.to_thread(RUNS.save, run)
    return {"offer_id": offer.id, "calc": res.calc.model_dump()}


class Choice(BaseModel):
    offer_id: str
    action: str = Field(..., pattern="^(include|exclude|reset)$")
    comment: str = Field("", max_length=500)


@router.post("/runs/{rid}/decisions", summary="Включить или исключить предложение с комментарием")
async def decide(request: Request, rid: str, c: Choice):
    run = _run(rid)
    res = next((r for r in run.results if any(o.id == c.offer_id for o in r.offers)), None)
    if not res:
        raise HTTPException(404, "Предложение не найдено")
    try:
        override(run, res, c.offer_id, c.action, c.comment, _user(request))
    except ValueError as e:
        raise HTTPException(422, str(e))
    await asyncio.to_thread(RUNS.save, run)
    return {"pos": res.item.pos, "calc": res.calc.model_dump()}


@router.get("/settings", summary="Действующие настройки рабочего пространства")
async def get_settings():
    return {**settings.public(), "method": {**load("rules")["method"], **settings.rules_overrides().get("method", {})},
            "profiles": settings.profiles()}


class SettingsPatch(BaseModel):
    profile: str | None = None
    policy: str | None = Field(None, pattern="^(strict|open)$")
    trust_min: int | None = Field(None, ge=0, le=100)
    whitelist: list[str] | None = None
    blacklist: list[str] | None = None
    method: dict | None = None


def _guard():
    if settings.locked():
        raise HTTPException(403, "Настройки закрыты администратором")


@router.patch("/settings", summary="Изменить настройки")
async def patch_settings(p: SettingsPatch):
    _guard()
    if p.profile and p.profile not in {x["id"] for x in settings.profiles()}:
        raise HTTPException(422, "Нет такого профиля")

    def fn(d):
        for k in ("profile", "policy", "trust_min"):
            if getattr(p, k) is not None:
                d[k] = getattr(p, k)
    settings.update(fn)
    if p.whitelist is not None:
        settings.set_domains("whitelist", p.whitelist)
    if p.blacklist is not None:
        settings.set_domains("blacklist", p.blacklist)
    if p.method:
        try:
            settings.set_method(p.method)
        except ValueError:
            raise HTTPException(422, "Пороги методики — числа")
    return await get_settings()


@router.get("/sources", summary="Источники: состояние, доверие, входит ли в утверждённый список")
async def list_sources():
    return catalog()


class SourcePatch(BaseModel):
    enabled: bool | None = None
    until: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$", description="временно отключить до даты")
    trust: int | None = Field(None, ge=0, le=100)
    listed: bool | None = None


@router.patch("/sources/{sid}", summary="Включить, отключить (в том числе до даты), задать доверие")
async def patch_source(sid: str, p: SourcePatch):
    _guard()
    if sid not in {c["id"] for c in catalog()}:
        raise HTTPException(404, "Нет такого источника")
    fields = p.model_dump(exclude_none=True)
    if "until" in fields:
        fields["enabled"] = False
    settings.set_source(sid, **fields)
    return next(c for c in catalog() if c["id"] == sid)


class NewSource(BaseModel):
    title: str = Field(..., max_length=120)
    url: str = Field("", description="сайт поставщика или шаблон поиска с {q}")
    kind: str = "catalog"
    trust: int | None = Field(None, ge=0, le=100)
    basis: str = ""
    listed: bool = True


@router.post("/sources", status_code=201, summary="Добавить свой источник")
async def add_source(s: NewSource):
    _guard()
    try:
        return settings.add_custom(s.title, s.url, s.kind, s.trust, s.basis, s.listed)
    except ValueError as e:
        raise HTTPException(422, str(e))


@router.delete("/sources/{sid}", summary="Удалить свой источник")
async def delete_source(sid: str):
    _guard()
    if not settings.remove_custom(sid):
        raise HTTPException(404, "Удалять можно только добавленные пользователем источники")
    return {"deleted": sid}


@router.post("/sources/import", summary="Применить утверждённый список источников из DOCX, XLSX или текста")
async def import_sources(file: UploadFile | None = File(None), text: str = Form(""), mode: str = Form("merge")):
    from .admin import import_list
    await import_list(file, text, mode)
    return {"whitelist": settings.whitelist(), "sources": catalog()}
