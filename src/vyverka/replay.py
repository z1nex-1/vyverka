"""Проверка сохранённого расчёта без сети: снимки сверяются по хешу, цены приводятся и считаются заново.

Ревизор получает журнал JSON и должен прийти к той же НМЦД, не доверяя интерфейсу. Поэтому пересчёт
не читает итоговые цифры расчёта, а повторяет шаги: снимок страницы → цена предложения → приведённая цена
на дату расчёта → выборка с учётом решений закупщика → среднее, V и НМЦД."""
import gzip
import hashlib
from dataclasses import dataclass, field

from .calc import calculate
from .config import pinned_rules, rules
from .models import Decision, ItemResult, Run
from .net import SnapshotStore
from .normalize import normalize


@dataclass
class ItemCheck:
    pos: int
    name: str
    saved: dict
    replayed: dict
    price_diffs: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.saved == self.replayed and not self.price_diffs


@dataclass
class Report:
    run_id: str
    items: list[ItemCheck] = field(default_factory=list)
    snapshots_ok: int = 0
    snapshots_bad: list[str] = field(default_factory=list)
    snapshots_missing: list[str] = field(default_factory=list)
    rules_pinned: bool = False
    saved_total: float | None = None
    replayed_total: float | None = None

    @property
    def ok(self) -> bool:
        return (all(i.ok for i in self.items) and not self.snapshots_bad
                and self.saved_total == self.replayed_total)


def _summary(status: str, n: int, cv: float | None, unit: float | None, total: float | None) -> dict:
    return {"status": status, "n": n, "cv": cv, "unit_price": unit, "total": total}


def _check_snapshot(store: SnapshotStore, key: str | None, sha: str | None) -> str:
    if not key or not sha:
        return "missing"
    gz = store.version_path(key, sha)
    if gz is None:
        return "bad" if store.body_path(key) else "missing"
    body = gzip.decompress(gz.read_bytes())
    return "ok" if hashlib.sha256(body).hexdigest() == sha else "bad"


def _pool(res: ItemResult, prices: dict[str, float]) -> dict[str, float]:
    """Выборка до расчёта: всё, что прошло сопоставление, плюс решения закупщика; отсев на этапе расчёта
    не учитывается, его calculate проводит сам."""
    pool = {}
    for d in res.decisions:
        price = prices.get(d.offer_id)
        if not price:
            continue
        if d.override:
            if d.override.get("action") == "include":
                pool[d.offer_id] = price
            continue
        if d.included or (d.reasons and d.reasons[-1][0] == "outlier"):
            pool[d.offer_id] = price
    return pool


def replay(run: Run, store: SnapshotStore | None = None) -> Report:
    saved = run.settings.get("rules")
    with pinned_rules(saved or rules()):
        rep = _replay(run, store)
    rep.rules_pinned = bool(saved)
    return rep


def _replay(run: Run, store: SnapshotStore | None) -> Report:
    store = store or SnapshotStore()
    day = run.created_at.date()
    rep = Report(run_id=run.id, saved_total=run.total)
    totals: list[float | None] = []
    for res in run.results:
        dec = {d.offer_id: d for d in res.decisions}
        prices: dict[str, float] = {}
        diffs: list[str] = []
        for o in res.offers:
            d = dec.get(o.id)
            if d is None or not d.unit_price:
                continue
            state = _check_snapshot(store, o.evidence.snapshot, o.evidence.sha256)
            if state == "ok":
                rep.snapshots_ok += 1
            elif state == "bad":
                rep.snapshots_bad.append(o.evidence.url)
            elif o.kind != "offer":
                rep.snapshots_missing.append(o.evidence.url)
            if o.source == "manual":
                again = round(o.price, 2)  # КП вносится уже в единицах ТЗ, его не приводят
            else:
                fresh = Decision(offer_id=o.id, included=True, score=d.score)
                normalize(res.item, o, fresh, today=day)
                again = fresh.unit_price
            if again != d.unit_price:
                diffs.append(f"{o.title[:50]}: в журнале {d.unit_price}, заново {again}")
            if again:
                prices[o.id] = again
        calc, _ = calculate(_pool(res, prices), res.item.qty)
        c = res.calc
        rep.items.append(ItemCheck(
            pos=res.item.pos, name=res.item.name,
            saved=_summary(c.status, c.n, c.cv, c.unit_price, c.total),
            replayed=_summary(calc.status, calc.n, calc.cv, calc.unit_price, calc.total),
            price_diffs=diffs))
        totals.append(calc.total)
    rep.replayed_total = round(sum(t for t in totals if t), 2) if totals and all(totals) else None
    return rep
