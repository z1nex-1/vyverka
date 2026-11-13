import argparse
import asyncio
import os
import sys
from pathlib import Path


def _one(src: Path, args: argparse.Namespace) -> int:
    from . import report, settings
    from .pipeline import Pipeline
    from .tz.parser import check, parse_docx, parse_text, read_text

    data = src.read_bytes()
    try:
        spec = parse_docx(data, src.name) if src.suffix.lower() == ".docx" else parse_text(read_text(data), src.name)
        check(spec)
    except Exception as e:  # noqa: BLE001
        print(f"{src.name}: {e if isinstance(e, ValueError) else 'файл повреждён или это не документ Word'}", file=sys.stderr)
        return 2
    if not spec.items:
        print(f"{src.name}: в документе не найдена таблица с позициями закупки", file=sys.stderr)
        return 2

    def listen(ev):
        if not args.quiet:
            print(f"  {ev.at:%H:%M:%S}  {ev.message}", file=sys.stderr, flush=True)

    user = os.environ.get("USER")
    run = asyncio.run(Pipeline(mode=args.mode, listener=listen, policy=args.policy, user=user).execute(spec))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    for ext, make in (("docx", report.docx), ("xlsx", report.xlsx), ("json", report.json_trace)):
        (out / report.filename(run, ext)).write_bytes(make(run))
    if run.total is None:
        (out / report.filename(run, "rfq.docx")).write_bytes(report.rfq(run, settings.org()))
    for r in run.results:
        price = report.money(r.calc.total) if r.calc.status == "ok" else report.STATUS[r.calc.status]
        est = f"  по аналогам ≈ {report.money(r.estimate['value'])}" if r.estimate and r.calc.status != "ok" else ""
        print(f"{r.item.pos:>3}  {r.item.name[:60]:<60}  n={r.calc.n:<3} {price}{est}")
    print("НМЦД:", report.money(run.total) if run.total is not None else "определена не полностью")
    print("Отчёты:", out.resolve())
    return 0 if run.total is not None else 1


def _run(args: argparse.Namespace) -> int:
    return _one(Path(args.tz), args)


def _batch(args: argparse.Namespace) -> int:
    """Пакет ТЗ из папки: удобно для ночной выгрузки из ЭДО."""
    files = sorted(p for p in Path(args.dir).iterdir() if p.suffix.lower() in (".docx", ".txt"))
    if not files:
        print("В папке нет ТЗ в формате DOCX или TXT", file=sys.stderr)
        return 2
    codes = []
    for f in files:
        print(f"== {f.name}")
        codes.append(_one(f, args))
    print(f"Готово: {codes.count(0)} из {len(files)} обоснованы полностью")
    return max(codes)


def _sources(args: argparse.Namespace) -> int:
    from . import settings, source_import
    from .config import load
    from .sources import catalog

    if args.action == "list":
        state = {"on": "подключён", "off": "выключен", "paused": "отключён до", "manual": "вручную"}
        for c in catalog():
            mark = "+" if c["listed"] else " "
            extra = f" {c['until']}" if c["state"] == "paused" else ""
            print(f"{mark} {c['id']:<16} {c['trust'] or '—':>3} %  {state[c['state']] + extra:<22} {c['title']}")
        print("+ — входит в утверждённый список; режим:", settings.policy(), "· порог доверия", settings.trust_min(), "%")
        return 0
    if args.action == "import":
        p = Path(args.target)
        found = source_import.parse(source_import.text_of(p.read_bytes(), p.name))
        marks = source_import.matches(found, settings.effective_sources(load("sources")["sources"]))

        def fn(d):
            for sid, ok in marks.items():
                d.setdefault("sources", {}).setdefault(sid, {})["listed"] = ok
            d["whitelist"] = sorted(set(settings.whitelist()) | set(found["domains"]))
        settings.update(fn)
        print(f"Сайтов в списке: {len(found['domains'])}; источников в списке: {sum(marks.values())} из {len(marks)}")
        return 0
    if args.action == "add":
        conf = settings.add_custom(args.target, args.url or "", args.kind, args.trust, args.basis or "")
        print("Добавлен:", conf["id"])
        return 0
    ids = {c["id"] for c in catalog()}
    if args.target not in ids:
        print("Нет такого источника. Список: vyverka sources list", file=sys.stderr)
        return 2
    if args.action == "on":
        settings.set_source(args.target, enabled=True)
    elif args.action == "off":
        settings.set_source(args.target, enabled=False, until=args.until)
    elif args.action == "trust":
        settings.set_source(args.target, trust=args.trust)
    elif args.action == "remove" and not settings.remove_custom(args.target):
        print("Удалять можно только свои источники", file=sys.stderr)
        return 2
    print("ok")
    return 0


def _settings(args: argparse.Namespace) -> int:
    import json

    from . import settings

    if args.action == "show":
        print(json.dumps(settings.public(), ensure_ascii=False, indent=2))
    elif args.action == "reset":
        settings.reset()
        print("Настройки сброшены к профилю", settings.profile_id())
    elif args.action == "profiles":
        for p in settings.profiles():
            print(f"{'*' if p['id'] == settings.profile_id() else ' '} {p['id']:<10} {p['title']}")
    elif args.action == "set":
        key, _, val = (args.value or "").partition("=")
        if key == "policy" and val in ("strict", "open"):
            settings.update(lambda d: d.__setitem__("policy", val))
        elif key == "trust_min" and val.isdigit():
            settings.update(lambda d: d.__setitem__("trust_min", int(val)))
        elif key == "profile" and val in {p["id"] for p in settings.profiles()}:
            settings.update(lambda d: d.__setitem__("profile", val))
        elif key in settings.EDITABLE_METHOD:
            settings.set_method({key: val})
        else:
            print("Ключи: policy=strict|open, trust_min=60, profile=mfti, min_offers=3, cv_limit=33 …", file=sys.stderr)
            return 2
        print("ok")
    return 0


def _serve(args: argparse.Namespace) -> int:
    import os

    import uvicorn

    os.environ["VYVERKA_MODE"] = args.mode
    uvicorn.run("vyverka.web.app:app", host=args.host, port=args.port)
    return 0


def _replay(args: argparse.Namespace) -> int:
    from . import report
    from .config import data_dir
    from .models import Run
    from .replay import replay

    p = Path(args.run)
    if not p.exists():
        p = data_dir() / "runs" / f"{args.run}.json"
    if not p.exists():
        print(f"{args.run}: нет такого файла или расчёта", file=sys.stderr)
        return 2
    rep = replay(Run.model_validate_json(p.read_text(encoding="utf-8")))

    def show(s: dict) -> str:
        if s["status"] != "ok":
            return f"{report.STATUS.get(s['status'], s['status'])}, n={s['n']}"
        return f"{report.money(s['total'])}, n={s['n']}, V={s['cv']} %"

    print(f"Расчёт {rep.run_id}")
    if not rep.rules_pinned:
        print("  В журнале нет правил метода на момент расчёта (журнал до версии с replay): пересчёт по текущим config/rules.yaml")
    for i in rep.items:
        mark = "совпадает" if i.ok else "РАСХОДИТСЯ"
        print(f"{i.pos:>3}  {i.name[:50]:<50}  {show(i.replayed)}  — {mark}")
        if not i.ok and i.saved != i.replayed:
            print(f"       в журнале: {show(i.saved)}")
        for d in i.price_diffs:
            print(f"       {d}")
    print(f"Снимки страниц: {rep.snapshots_ok} совпадают по SHA-256", end="")
    if rep.snapshots_missing:
        print(f", {len(rep.snapshots_missing)} нет на диске", end="")
    print(f", {len(rep.snapshots_bad)} изменены" if rep.snapshots_bad else "")
    for u in rep.snapshots_bad:
        print(f"       изменён: {u}")
    saved = report.money(rep.saved_total) if rep.saved_total is not None else "не определена"
    again = report.money(rep.replayed_total) if rep.replayed_total is not None else "не определена"
    print(f"НМЦД: в журнале {saved}, заново {again} — {'воспроизводится' if rep.ok else 'не воспроизводится'}")
    return 0 if rep.ok else 1


def main() -> None:
    p = argparse.ArgumentParser(prog="vyverka", description="Обоснование НМЦД методом сопоставимых рыночных цен")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run", help="рассчитать НМЦД по файлу ТЗ и сохранить DOCX, XLSX и журнал")
    r.add_argument("tz", help="ТЗ в формате DOCX или TXT")
    r.add_argument("--out", default="reports", help="папка для отчётов")
    r.add_argument("--mode", choices=["live", "cache", "offline"], default="cache",
                   help="live — только сеть, cache — сохранённые копии и сеть, offline — только сохранённые копии")
    r.add_argument("-q", "--quiet", action="store_true", help="не печатать ход расчёта")
    r.add_argument("--policy", choices=["strict", "open"], help="strict — только утверждённые источники, open — любые с доверием")
    r.set_defaults(fn=_run)
    b = sub.add_parser("batch", help="рассчитать все ТЗ из папки")
    b.add_argument("dir")
    for a, kw in (("--out", {"default": "reports"}), ("--mode", {"choices": ["live", "cache", "offline"], "default": "cache"}),
                  ("--policy", {"choices": ["strict", "open"]})):
        b.add_argument(a, **kw)
    b.add_argument("-q", "--quiet", action="store_true")
    b.set_defaults(fn=_batch)
    so = sub.add_parser("sources", help="источники: list, on, off, trust, add, remove, import")
    so.add_argument("action", choices=["list", "on", "off", "trust", "add", "remove", "import"])
    so.add_argument("target", nargs="?", default="", help="id источника, название нового или файл списка")
    so.add_argument("--until", help="отключить до даты ГГГГ-ММ-ДД")
    so.add_argument("--trust", type=int)
    so.add_argument("--url", help="сайт или шаблон поиска с {q}")
    so.add_argument("--kind", default="catalog")
    so.add_argument("--basis")
    so.set_defaults(fn=_sources)
    st = sub.add_parser("settings", help="настройки: show, set ключ=значение, profiles, reset")
    st.add_argument("action", choices=["show", "set", "profiles", "reset"])
    st.add_argument("value", nargs="?")
    st.set_defaults(fn=_settings)
    rp = sub.add_parser("replay", help="пересчитать сохранённый расчёт без сети и сверить снимки по хешу")
    rp.add_argument("run", help="журнал JSON или id расчёта из data/runs")
    rp.set_defaults(fn=_replay)

    s = sub.add_parser("serve", help="запустить веб-интерфейс")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--mode", choices=["live", "cache", "offline"], default="cache")
    s.set_defaults(fn=_serve)
    args = p.parse_args()
    sys.exit(args.fn(args))
