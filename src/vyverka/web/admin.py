"""Экран настроек: профиль организации, режим источников, свои источники, импорт утверждённого списка."""

import json
from datetime import date

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from .. import settings, source_import
from ..config import load
from ..sources import catalog

router = APIRouter()


def _tpl():
    from .app import tpl
    return tpl


def _back(note: str = "", anchor: str = "") -> RedirectResponse:
    from urllib.parse import quote
    q = f"?note={quote(note)}" if note else ""
    return RedirectResponse(f"/settings{q}{'#' + anchor if anchor else ''}", status_code=303)


def _guard():
    if settings.locked():
        raise HTTPException(403, "Настройки закрыты администратором (VYVERKA_LOCK_SETTINGS)")


@router.get("/settings", response_class=HTMLResponse)
async def page(request: Request, note: str = ""):
    from ..config import rules
    return _tpl().TemplateResponse(request, "settings.html", {
        "s": settings.public(), "profiles": settings.profiles(), "sources": catalog(), "note": note,
        "groups": settings.rule_groups(rules(), load("rules")), "method": rules()["method"], "kinds": settings.KIND_TRUST,
        "today": date.today().isoformat(), "nav": "settings"})


@router.post("/settings/rules/{part}")
async def set_rules(request: Request, part: str):
    _guard()
    form = await request.form()
    try:
        settings.set_rules(part, dict(form))
    except KeyError:
        raise HTTPException(404)
    except ValueError:
        raise HTTPException(422, "Параметры — числа")
    return _back("Параметры сохранены, действуют для новых расчётов", f"r-{part}")


@router.post("/settings/rules/{part}/reset")
async def reset_rules(part: str):
    _guard()
    if part not in {g["part"] for g in settings.RULE_GROUPS}:
        raise HTTPException(404)
    settings.reset_rules(part)
    return _back("Возвращены значения по умолчанию", f"r-{part}")


@router.post("/settings/profile")
async def set_profile(profile: str = Form(...), org_name: str = Form(""), contact: str = Form(""),
                      executor: str = Form(""), position: str = Form("")):
    _guard()
    if profile not in {p["id"] for p in settings.profiles()}:
        raise HTTPException(422, "Нет такого профиля")

    def fn(d):
        d["profile"] = profile
        org = d.setdefault("org", {})
        org["name"], org["contact"] = org_name.strip()[:120], contact.strip()[:200]
        org["executor"], org["position"] = executor.strip()[:120], position.strip()[:160]
    settings.update(fn)
    return _back("Профиль сохранён", "org")


@router.post("/settings/policy")
async def set_policy(policy: str = Form(...), trust_min: int = Form(60)):
    _guard()
    if policy not in ("strict", "open"):
        raise HTTPException(422, "Режим: strict или open")
    settings.update(lambda d: d.update(policy=policy, trust_min=max(0, min(100, trust_min))))
    return _back("Режим источников сохранён", "policy")


@router.post("/settings/sources/{sid}")
async def set_source(sid: str, action: str = Form(...), until: str = Form(""), trust: str = Form(""),
                     tier: str = Form(""), limit: str = Form(""), years: str = Form("")):
    _guard()
    if sid not in {c["id"] for c in catalog()}:
        raise HTTPException(404)
    if action == "on":
        settings.set_source(sid, enabled=True)
    elif action == "off":
        settings.set_source(sid, enabled=False, until=None)
    elif action == "pause":
        try:
            day = date.fromisoformat(until)
        except ValueError:
            raise HTTPException(422, "Укажите дату, до которой источник отключён")
        settings.set_source(sid, enabled=False, until=day.isoformat())
    elif action == "trust":
        if not trust.strip().isdigit():
            raise HTTPException(422, "Доверие — число от 0 до 100")
        settings.set_source(sid, trust=int(trust))
    elif action == "params":
        vals = {}
        for k, v, lo, hi in (("trust", trust, 0, 100), ("tier", tier, 1, 9), ("limit", limit, 1, 50), ("years", years, 1, 10)):
            if v.strip():
                if not v.strip().isdigit():
                    raise HTTPException(422, "Параметры источника — целые числа")
                vals[k] = max(lo, min(hi, int(v)))
        settings.set_source(sid, **vals)
    elif action in ("list", "unlist"):
        settings.set_source(sid, listed=action == "list")
    elif action == "delete":
        settings.remove_custom(sid)
    else:
        raise HTTPException(422, "Неизвестное действие")
    return _back("Источник обновлён", f"src-{sid}")


@router.post("/settings/sources")
async def add_source(title: str = Form(...), url: str = Form(""), kind: str = Form("catalog"),
                     trust: str = Form(""), basis: str = Form(""), listed: str = Form("")):
    _guard()
    try:
        conf = settings.add_custom(title, url, kind, int(trust) if trust.strip().isdigit() else None, basis,
                                   listed=bool(listed))
    except ValueError as e:
        raise HTTPException(422, str(e))
    return _back(f"Добавлен источник «{conf['title']}»", f"src-{conf['id']}")


@router.post("/settings/import")
async def import_list(file: UploadFile | None = File(None), text: str = Form(""), mode: str = Form("merge")):
    """Утверждённый список источников из файла или текста: отмечаем входящие в список подключённые источники,
    сайты из списка становятся разрешёнными для открытого поиска."""
    _guard()
    body = text
    if file and file.filename:
        body = source_import.text_of(await file.read(), file.filename)
    found = source_import.parse(body or "")
    if not found["domains"] and not found["kinds"]:
        return _back("В списке не нашлось ни сайтов, ни видов источников", "import")
    marks = source_import.matches(found, [c for c in settings.effective_sources(load("sources")["sources"])])

    def fn(d):
        over = d.setdefault("sources", {})
        for sid, ok in marks.items():
            over.setdefault(sid, {})["listed"] = ok
        base = [] if mode == "replace" else settings.whitelist()
        d["whitelist"] = sorted(set(base) | set(found["domains"]))
    settings.update(fn)
    n = sum(marks.values())
    return _back(f"Список применён: {len(found['domains'])} сайтов, {n} из {len(marks)} источников в списке", "import")


@router.post("/settings/domains")
async def set_domains(whitelist: str = Form(""), blacklist: str = Form("")):
    _guard()
    split = lambda s: [x for x in s.replace(",", "\n").split() if x]  # noqa: E731
    settings.set_domains("whitelist", split(whitelist))
    settings.set_domains("blacklist", split(blacklist))
    return _back("Списки сайтов сохранены", "domains")


@router.post("/settings/method")
async def set_method(request: Request):
    _guard()
    form = await request.form()
    vals = {k: form.get(k) for k in settings.EDITABLE_METHOD if k != "one_offer_per_supplier"}
    vals["one_offer_per_supplier"] = "1" if form.get("one_offer_per_supplier") else "0"
    try:
        settings.set_method(vals)
    except ValueError:
        raise HTTPException(422, "Пороги методики — числа")
    return _back("Параметры методики сохранены", "method")


@router.post("/settings/reset")
async def reset():
    _guard()
    settings.reset()
    return _back("Настройки сброшены к профилю организации")


@router.get("/settings/export.json")
async def export():
    """Настройки одним файлом — перенести на другой стенд или в другой вуз."""
    return Response(json.dumps(settings.raw(), ensure_ascii=False, indent=2), media_type="application/json",
                    headers={"Content-Disposition": 'attachment; filename="vyverka-settings.json"'})


@router.post("/settings/restore")
async def restore(file: UploadFile = File(...)):
    _guard()
    try:
        data = json.loads((await file.read()).decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise HTTPException(422, "Нужен файл, выгруженный кнопкой «Скачать настройки»")
    if not isinstance(data, dict):
        raise HTTPException(422, "Неверный формат настроек")
    settings.save(data)
    return _back("Настройки загружены из файла")


@router.get("/integration", response_class=HTMLResponse)
async def integration(request: Request):
    base = str(request.base_url).rstrip("/")
    return _tpl().TemplateResponse(request, "integration.html", {"base": base, "s": settings.public(), "nav": "integration"})


@router.get("/guide", response_class=HTMLResponse)
async def guide(request: Request):
    from ..config import rules
    m = rules()["method"]
    return _tpl().TemplateResponse(request, "guide.html", {"s": settings.public(), "m": m, "nav": "guide"})
