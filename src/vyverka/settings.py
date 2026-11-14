"""Настройки рабочего пространства поверх config/*.yaml.

YAML в репозитории — заводские правила. Всё, что пользователь меняет в интерфейсе, API или консоли
(включить или временно выключить источник, доверие к нему, свои источники, список разрешённых сайтов,
пороги методики, профиль организации), хранится в data/settings.json и накладывается поверх.
Сбросить настройки — удалить этот файл."""

import json
import os
import re
from copy import deepcopy
from datetime import date
from functools import cache
from pathlib import Path
from urllib.parse import urlsplit

import yaml

from .config import config_dir, data_dir

# Доверие по умолчанию: исполненный контракт — фактически уплаченная цена, сайт из поиска — заявленная.
KIND_TRUST = {"contract": 100, "registry": 95, "offer": 90, "catalog": 85, "marketplace": 75, "web": 50}
EDITABLE_METHOD = {"min_offers": int, "cv_limit": float, "max_age_years": int, "sample_target": int,
                   "max_outlier_share": float, "one_offer_per_supplier": bool}

_cache: dict = {"mtime": None, "data": {}}


def _path() -> Path:
    return data_dir() / "settings.json"


def raw() -> dict:
    p = _path()
    try:
        m = p.stat().st_mtime_ns
    except FileNotFoundError:
        return {}
    if _cache["mtime"] != (str(p), m):
        _cache["data"] = json.loads(p.read_text(encoding="utf-8") or "{}")
        _cache["mtime"] = (str(p), m)
    return _cache["data"]


def save(data: dict) -> None:
    if locked():
        raise PermissionError("Настройки закрыты для изменения (VYVERKA_LOCK_SETTINGS)")
    p = _path()
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(p)


def update(fn) -> dict:
    data = deepcopy(raw())
    fn(data)
    save(data)
    return data


def reset() -> None:
    if locked():
        raise PermissionError("Настройки закрыты для изменения (VYVERKA_LOCK_SETTINGS)")
    _path().unlink(missing_ok=True)


def locked() -> bool:
    return os.environ.get("VYVERKA_LOCK_SETTINGS", "") not in ("", "0", "false")


@cache
def _profile_file(pid: str) -> dict:
    p = config_dir() / "profiles" / f"{pid}.yaml"
    if not re.fullmatch(r"[a-z0-9_-]{1,40}", pid) or not p.exists():
        return {}
    return yaml.safe_load(p.read_text(encoding="utf-8")) or {}


def profiles() -> list[dict]:
    d = config_dir() / "profiles"
    out = []
    for p in sorted(d.glob("*.yaml")) if d.exists() else []:
        conf = _profile_file(p.stem)
        out.append({"id": p.stem, "title": conf.get("title", p.stem), "org": conf.get("org", {})})
    return out


def profile_id() -> str:
    return raw().get("profile") or os.environ.get("VYVERKA_PROFILE", "mfti")


def profile() -> dict:
    return _profile_file(profile_id())


def org() -> dict:
    out = dict(profile().get("org", {}))
    out.update({k: v for k, v in raw().get("org", {}).items() if v})
    return out


METHOD_567 = "Методические рекомендации, утв. приказом Минэкономразвития России от 02.10.2013 № 567"


def legal_basis(o: dict | None = None, short: bool = False) -> str:
    o = o if o is not None else org()
    if str(o.get("law", "44")) == "223":
        reg = o.get("regulation") or "положение о закупке заказчика"
        if short:
            return f"Метод сопоставимых рыночных цен, 223-ФЗ, {reg[0].lower() + reg[1:]}, методика Приказа № 567"
        return (f"Метод: метод сопоставимых рыночных цен (анализа рынка) в порядке, установленном документом "
                f"«{reg}» (ч. 2 ст. 2 Федерального закона № 223-ФЗ); "
                f"порядок расчёта — {METHOD_567}, применяемые заказчиком в части, не противоречащей положению.")
    if short:
        return "Метод сопоставимых рыночных цен, 44-ФЗ ст. 22, Приказ № 567"
    return f"Метод: метод сопоставимых рыночных цен (анализа рынка), ч. 2–6 ст. 22 Федерального закона № 44-ФЗ, {METHOD_567}."


def policy() -> str:
    p = raw().get("policy") or profile().get("policy") or "strict"
    return p if p in ("strict", "open") else "strict"


def trust_min() -> int:
    return int(raw().get("trust_min", profile().get("trust_min", 60)))


def domain(url_or_host: str) -> str:
    s = url_or_host.strip().lower()
    host = urlsplit(s if "//" in s else f"//{s}").hostname or ""
    return host[4:] if host.startswith("www.") else host


def _domains(key: str) -> list[str]:
    base = profile().get(key, []) if key not in raw() else []
    return sorted({domain(x) for x in list(base) + list(raw().get(key, [])) if domain(x)})


def whitelist() -> list[str]:
    return _domains("whitelist")


def blacklist() -> list[str]:
    return _domains("blacklist")


def on_list(host: str, listed: list[str]) -> bool:
    h = domain(host)
    return any(h == d or h.endswith("." + d) for d in listed)


def rules_overrides() -> dict:
    """Части rules.yaml, которые меняются из настроек: методика, индексация, аналоги."""
    out: dict = {}
    for part in ("method", "indexation", "analogs", "comparability"):
        merged = {**profile().get(part, {}), **raw().get(part, {})}
        if merged:
            out[part] = merged
    return out


def _source_kind(c: dict) -> str:
    if c.get("type") in ("eis", "eis223"):
        return "contract"
    if c.get("type") == "clearspending":
        return "registry"
    if c.get("type") == "manual":
        return "offer"
    return c.get("kind") or ("web" if c.get("type") == "web" else "catalog")


def _active(over: dict) -> bool:
    if over.get("enabled", True):
        return True
    until = over.get("until")
    if until:
        try:
            return date.fromisoformat(until) < date.today()
        except ValueError:
            return False
    return False


def effective_sources(base: list[dict]) -> list[dict]:
    """Заводские источники + свои, с учётом включения, временного отключения и доверия."""
    over = raw().get("sources", {})
    out = []
    for c in list(base) + [dict(x, custom=True) for x in raw().get("custom", [])]:
        c = dict(c)
        o = over.get(c["id"], {})
        kind = _source_kind(c)
        c["kind"] = kind
        c["enabled"] = _active(o) if "enabled" in o else c.get("enabled", True)
        if o.get("until") and not o.get("enabled", True):
            c["until"] = o["until"]
        c["trust"] = int(o.get("trust", c.get("trust", KIND_TRUST.get(kind, 60))))
        for k in ("tier", "limit", "years"):
            if k in o:
                c[k] = int(o[k])
        c.setdefault("listed", kind != "web")
        if "listed" in o:
            c["listed"] = bool(o["listed"])
        out.append(c)
    return out


def set_source(sid: str, **fields) -> None:
    allowed = {"enabled", "trust", "until", "listed", "tier", "limit", "years"}

    def fn(d):
        cur = d.setdefault("sources", {}).setdefault(sid, {})
        for k, v in fields.items():
            if k not in allowed:
                continue
            if v is None:
                cur.pop(k, None)
            else:
                cur[k] = v
        if cur.get("enabled", True):
            cur.pop("until", None)
        if "trust" in cur:
            cur["trust"] = max(0, min(100, int(cur["trust"])))
    update(fn)


def slug(title: str) -> str:
    tr = str.maketrans("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", "abvgdeejziiklmnoprstufhccss_y_eua")
    s = re.sub(r"[^a-z0-9]+", "-", title.lower().translate(tr)).strip("-")
    return (s or "src")[:30]


def add_custom(title: str, url: str = "", kind: str = "catalog", trust: int | None = None, basis: str = "",
               listed: bool = True) -> dict:
    """Источник, добавленный пользователем словами: шаблон поиска с {q} — свой каталог (JSON-LD),
    просто адрес сайта — разрешённый домен для поиска по открытым источникам."""
    title = title.strip()[:120] or domain(url) or "Источник"
    url = url.strip()
    if kind not in KIND_TRUST:
        kind = "catalog"
    conf: dict = {"title": title, "kind": kind, "basis": basis.strip()[:300] or "добавлен пользователем",
                  "tier": 3, "limit": 8, "listed": listed}
    if "{q}" in url:
        if not url.startswith(("http://", "https://")):
            raise ValueError("Шаблон поиска должен начинаться с http:// или https://")
        conf.update(type="jsonld", search_url=url)
    elif url:
        conf.update(type="site", domain=domain(url))
    else:
        conf.update(type="site", domain="")
    if trust is not None:
        conf["trust"] = max(0, min(100, int(trust)))

    def fn(d):
        ids = {c["id"] for c in d.get("custom", [])}
        sid = base = "u-" + slug(title)
        n = 2
        while sid in ids:
            sid, n = f"{base}-{n}", n + 1
        conf["id"] = sid
        d.setdefault("custom", []).append(conf)
        if conf.get("domain") and listed:
            wl = d.setdefault("whitelist", list(profile().get("whitelist", [])))
            if conf["domain"] not in wl:
                wl.append(conf["domain"])
    update(fn)
    return conf


def remove_custom(sid: str) -> bool:
    found = {"ok": False}

    def fn(d):
        before = len(d.get("custom", []))
        d["custom"] = [c for c in d.get("custom", []) if c["id"] != sid]
        found["ok"] = len(d["custom"]) < before
        d.get("sources", {}).pop(sid, None)
    update(fn)
    return found["ok"]


def set_domains(key: str, items: list[str]) -> None:
    clean = sorted({domain(x) for x in items if domain(x)})
    update(lambda d: d.__setitem__(key, clean))


def set_method(values: dict) -> None:
    def fn(d):
        m = d.setdefault("method", {})
        for k, v in values.items():
            if k in EDITABLE_METHOD and v not in (None, ""):
                m[k] = EDITABLE_METHOD[k](v) if EDITABLE_METHOD[k] is not bool else v in (True, "1", "on", "true")
    update(fn)


# Все параметры расчёта, которые меняются из интерфейса. Ключ с точкой — вложенное значение (weights.attrs).
# Параметры, которых движок не читает, сюда не попадают: настройка без действия хуже, чем её отсутствие.
RULE_GROUPS = [
    {"part": "method", "title": "Методика расчёта",
     "about": "Приказ Минэкономразвития № 567. Положение о закупке организации может быть строже, мягче — нельзя.",
     "fields": [
         ("min_offers", int, 1, 20, 1, "Минимум цен", "Меньше — позиция получает «не обоснована» и форму запроса КП. Приказ № 567, п. 3.20.1: не менее трёх."),
         ("cv_limit", float, 5, 100, 0.5, "Предел разброса V, %", "Коэффициент вариации σ / среднее. Выше предела крайние цены исключаются по одной."),
         ("max_age_years", int, 1, 10, 1, "Давность контрактов, лет", "Исполненные контракты старше не берутся."),
         ("sample_target", int, 3, 30, 1, "Сколько цен собирать", "Пока цен меньше, подключаются источники следующего уровня: контракты → зеркало → каталоги."),
         ("max_outlier_share", float, 0, 0.9, 0.01, "Доля исключений, не больше", "0,34 — не больше трети выборки. Если однородности так не достичь — «цены неоднородны»."),
         ("outlier_mad_k", float, 1, 10, 0.5, "Порог грубого выброса, k × MAD", "Цена дальше k медианных отклонений от медианы отсекается до расчёта V."),
         ("outlier_mad_min_n", int, 3, 30, 1, "Отсекать выбросы с выборки от", "На малых выборках медианное отклонение неустойчиво — тогда только проверка V."),
         ("rounding", int, 0, 4, 1, "Знаков после запятой", "Округление цены за единицу и суммы."),
         ("one_offer_per_supplier", bool, None, None, None, "Одна цена от поставщика", "Несколько контрактов одного поставщика — в расчёт идёт один, ближайший по дате."),
     ]},
    {"part": "comparability", "title": "Сопоставимость",
     "about": "Когда предложение считается тем же товаром, что в ТЗ.",
     "fields": [
         ("include_score", float, 0, 1, 0.05, "Подтверждено характеристик, не меньше", "Доля существенных характеристик ТЗ, подтверждённых у предложения. 0,6 — шесть из десяти."),
         ("equivalent_share", float, 0, 1, 0.05, "Эквивалент другой модели от", "Если в ТЗ модель, другая модель принимается как эквивалент при такой доле совпадений."),
         ("identity_weight", float, 0, 1, 0.05, "Вес совпадения марки и модели", "Сколько в оценке сопоставимости весит совпадение названия модели из ТЗ."),
         ("tolerance_pct.supply", float, 0, 50, 1, "Допуск по напряжению питания, %", "220 В в ТЗ и 230 В у поставщика — одно и то же при допуске 10 % (ГОСТ 32144)."),
     ]},
    {"part": "indexation", "title": "Индексация цен прошлых лет",
     "about": "Цена контракта прошлых лет переводится в цены последнего опубликованного месяца по помесячному ИПЦ Росстата.",
     "fields": [
         ("enabled", bool, None, None, None, "Индексировать цены контрактов", "Выключите, если положение о закупке не требует приведения цен."),
     ], "cpi": True},
    {"part": "analogs", "title": "Похожие товары",
     "about": "Облако аналогов и справочная оценка цены по ближайшим соседям. В НМЦД не входит без решения закупщика.",
     "fields": [
         ("k", int, 1, 20, 1, "Соседей в оценке", "Сколько ближайших аналогов с ценой участвует в справочной оценке."),
         ("min_similarity", float, 0, 1, 0.05, "Близость не ниже", "Аналоги дальше порога в оценку не попадают, но видны в облаке."),
         ("weights.attrs", float, 0, 1, 0.05, "Вес характеристик", "Совпадение значений характеристик ТЗ."),
         ("weights.text", float, 0, 1, 0.05, "Вес названия", "Похожесть текста наименования."),
         ("weights.code", float, 0, 1, 0.05, "Вес кода ОКПД2 / КТРУ", "Совпадение классификатора."),
         ("weights.identity", float, 0, 1, 0.05, "Вес марки и модели", "Совпадение производителя и модели."),
     ]},
]


def _dig(d: dict, key: str):
    for k in key.split("."):
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def rule_groups(effective: dict, factory: dict) -> list[dict]:
    """Группы параметров для экрана: действующее значение, заводское и пометка «изменено»."""
    over = rules_overrides()
    out = []
    for g in RULE_GROUPS:
        part, eff, fac = g["part"], effective.get(g["part"], {}), factory.get(g["part"], {})
        if part == "analogs":
            from .analogs import conf as an_conf
            eff = an_conf()
            fac = {"k": 5, "min_similarity": 0.45, "weights": {"attrs": 0.45, "text": 0.3, "code": 0.1, "identity": 0.15}}
        fields = []
        for key, typ, lo, hi, step, label, hint in g["fields"]:
            v, f = _dig(eff, key), _dig(fac, key)
            fields.append({"key": key, "type": typ.__name__, "min": lo, "max": hi, "step": step, "label": label,
                           "hint": hint, "value": v if v is not None else f, "factory": f,
                           "changed": _dig(over.get(part, {}), key) is not None and _dig(over.get(part, {}), key) != f})
        item = {**g, "fields": fields}
        if g.get("cpi"):
            item["cpi"] = sorted({int(k): float(v) for k, v in (eff.get("cpi") or {}).items()}.items())
            item["cpi_factory"] = fac.get("cpi") or {}
        out.append(item)
    return out


def set_rules(part: str, form: dict) -> None:
    """Сохраняет все поля группы разом: частичная запись вложенного словаря (weights) потеряла бы соседей."""
    group = next((g for g in RULE_GROUPS if g["part"] == part), None)
    if not group:
        raise KeyError(part)
    vals: dict = {}
    for key, typ, lo, hi, *_ in group["fields"]:
        raw_v = form.get(key)
        if typ is bool:
            v = raw_v in ("1", "on", "true", True)
        elif raw_v in (None, ""):
            continue
        else:
            v = typ(float(str(raw_v).replace(",", ".")))
            v = max(lo, min(hi, v))
        cur = vals
        *path, last = key.split(".")
        for k in path:
            cur = cur.setdefault(k, {})
        cur[last] = v
    if group.get("cpi"):
        cpi = {}
        for k, v in form.items():
            m = re.fullmatch(r"cpi_(\d{4})", k)
            if m and str(v).strip():
                cpi[int(m.group(1))] = max(-50.0, min(100.0, float(str(v).replace(",", "."))))
        y, v = str(form.get("cpi_new_year") or "").strip(), str(form.get("cpi_new_value") or "").strip()
        if re.fullmatch(r"20\d\d", y) and v:
            cpi[int(y)] = float(v.replace(",", "."))
        if cpi:
            vals["cpi"] = cpi
    if part == "analogs" and "weights" in vals:
        from .analogs import conf as an_conf
        vals["weights"] = {**an_conf()["weights"], **vals["weights"]}

    def fn(d):
        d[part] = {**d.get(part, {}), **vals}
    update(fn)


def reset_rules(part: str) -> None:
    update(lambda d: d.pop(part, None))


def public() -> dict:
    """Снимок действующих настроек для экрана, API и журнала прогона."""
    return {"profile": profile_id(), "org": org(), "policy": policy(), "trust_min": trust_min(),
            "whitelist": whitelist(), "blacklist": blacklist(), "overrides": rules_overrides(),
            "locked": locked()}
