import asyncio
import inspect
import os
import re
import time
import uuid
from collections.abc import Callable
from datetime import datetime
from urllib.parse import urlsplit

from . import ai, analogs, trust
from .calc import calculate
from .config import rules, sources as sources_conf
from .settings import org, policy as default_policy, public as settings_public, trust_min
from .match import check, decide, identity
from .models import Calculation, Decision, Item, ItemResult, Offer, Run, SourceStatus, Spec, TraceEvent
from .net import Fetcher, SourceUnavailable, _human
from .normalize import _amount_ug, normalize
from .planner import plan
from .sources import build
from .sources.base import Source
from .sources.html import LayoutChanged
from .words import plural, rub

Listener = Callable[[TraceEvent], object]


class Pipeline:
    def __init__(self, mode: str = "live", only: list[str] | None = None, listener: Listener | None = None,
                 max_queries: int = 4, policy: str | None = None, user: str | None = None):
        self.mode = mode
        self.only = only
        self.policy = policy if policy in ("strict", "open") else default_policy()
        self.user = user
        self.listener = listener
        self.max_queries = max_queries
        self.run: Run | None = None
        self.reader = ai.Reader() if ai.enabled() else None

    async def emit(self, stage: str, message: str, **data) -> None:
        ev = TraceEvent(at=datetime.now(), stage=stage, message=message, data=data or None)
        if self.run:
            self.run.trace.append(ev)
        if self.listener:
            res = self.listener(ev)
            if inspect.isawaitable(res):
                await res

    async def execute(self, spec: Spec) -> Run:
        self.run = Run(id=uuid.uuid4().hex[:12], created_at=datetime.now(), mode=self.mode, spec=spec,
                       settings={"method": rules().get("method", {}), "rules": rules(), "sources": self.only or "all",
                                 "policy": self.policy, "trust_min": trust_min(), "workspace": settings_public(),
                                 "org": org(), "user": self.user,
                                 **({"ai": ai.info()} if self.reader else {})})
        await self.emit("parse", f"ТЗ разобрано: {plural(len(spec.items), 'позиция', 'позиции', 'позиций')}", items=len(spec.items))
        fetcher = Fetcher(mode=self.mode)
        try:
            srcs = build(fetcher, self.only, self.policy)
            await self.emit("plan", ("Режим источников: только утверждённый список, доверие 100 %"
                                     if self.policy == "strict" else
                                     f"Режим источников: любые открытые источники, порог доверия {trust_min()} %")
                            + f"; подключено {len(srcs)}: " + ", ".join(s.title for s in srcs), policy=self.policy)
            for item in spec.items:
                self.run.results.append(await self._item(item, srcs))
        finally:
            await fetcher.close()
        totals = [r.calc.total for r in self.run.results]
        self.run.total = round(sum(t for t in totals if t), 2) if all(totals) else None
        self.run.status = "ok" if self.run.total is not None else "partial"
        await self.emit("done", "НМЦД рассчитана" if self.run.total else "Расчёт завершён, не все позиции обоснованы",
                        total=self.run.total)
        return self.run

    async def _item(self, item: Item, srcs: list[Source]) -> ItemResult:
        queries = plan(item)
        await self.emit("plan", f"Поз. {item.pos}: {plural(len(queries), 'поисковый запрос', 'поисковых запроса', 'поисковых запросов')}", pos=item.pos, queries=queries)
        applicable = [s for s in srcs if not s.conf.get("categories") or item.category in s.conf["categories"]]
        results = await asyncio.gather(*(self._collect(s, item, queries) for s in applicable))
        statuses = [st for _, st in results]
        offers, dups = dedup([o for offs, _ in results for o in offs])

        decisions = {o.id: decide(item, o) for o in offers}
        if self.reader:
            await self._ai_read(item, offers, decisions)
        for o in offers:
            if decisions[o.id].included:
                normalize(item, o, decisions[o.id])
        confs = {c["id"]: c for c in sources_conf().get("sources", [])}
        trust.apply(offers, decisions, confs, self.policy, trust_min())
        _one_per_supplier(offers, decisions)
        _one_per_product(item, offers, decisions)
        _by_identity(offers, decisions)
        _by_tier(offers, decisions, {s.name: int(s.conf.get("tier", 9)) for s in srcs})

        prices = {i: d.unit_price for i, d in decisions.items() if d.included and d.unit_price}
        calc, excluded = calculate(prices, item.qty)
        for i, why in excluded.items():
            decisions[i].included = False
            decisions[i].reasons.append(("outlier", why))
        await self.emit("calc", f"Поз. {item.pos}: {calc.message}", pos=item.pos, status=calc.status,
                        unit_price=calc.unit_price, n=calc.n)
        order = sorted(offers, key=lambda o: (not decisions[o.id].included, -decisions[o.id].score))
        res = ItemResult(item=item, offers=order, decisions=[decisions[o.id] for o in order], calc=calc,
                         sources=statuses, duplicates=dups)
        analogs.attach(res)
        if res.estimate:
            await self.emit("analogs", f"Поз. {item.pos}: облако из {len(res.analogs)} похожих товаров, "
                            f"оценка по {res.estimate['k']} ближайшим — {rub(res.estimate['value'])}",
                            pos=item.pos, estimate=res.estimate["value"])
        return res

    async def _ai_read(self, item: Item, offers: list[Offer], decisions: dict[str, Decision]) -> None:
        """Модель дочитывает карточки, где правила не нашли существенные характеристики; решают снова правила."""
        reqs = {r.name: r for r in item.requirements if r.essential}
        todo = []
        for o in offers:
            unknown = [reqs[c.name] for c in decisions[o.id].checks if c.status == "unknown" and c.name in reqs]
            if unknown and identity(item, o)[0] > 0:
                todo.append((decisions[o.id].score, o, unknown))
        todo.sort(key=lambda t: -t[0])
        todo = todo[: int(os.environ.get("VYVERKA_AI_MAX", "12"))]
        if not todo:
            return
        try:
            got = await asyncio.gather(*(self.reader.read(o, unk) for _, o, unk in todo))
        except Exception as e:  # noqa: BLE001 — без модели расчёт идёт по правилам
            await self.emit("ai", f"Поз. {item.pos}: ИИ недоступен ({_human(e)}), характеристики проверены только правилами",
                            pos=item.pos)
            return
        found = rejected = conflicts = changed = 0
        for (_, o, _), (acc, rej) in zip(todo, got):
            rejected += len(rej)
            if rej:
                o.meta["ai_rejected"] = rej
            # Модель может только подтвердить характеристику. Найденное ею противоречие не исключает цену,
            # а показывается закупщику: модель могла прочитать соседнее свойство.
            keep, odd = {}, {}
            for v in acc.values():
                o.meta["ai"] = {v["name"]: v}
                (keep if check(reqs[v["name"]], o).status in ("ok", "partial") else odd)[v["name"]] = v
            o.meta["ai"] = keep
            if odd:
                o.meta["ai_conflict"] = odd
            found, conflicts = found + len(keep), conflicts + len(odd)
            d = decide(item, o) if keep else decisions[o.id]
            for c in d.checks:
                if c.name in keep and c.status != "unknown":
                    c.via, c.quote = "ai", keep[c.name]["quote"]
                elif c.name in odd and c.status == "unknown":
                    c.via, c.found, c.quote = "ai_conflict", odd[c.name]["value"], odd[c.name]["quote"]
            changed += d.included != decisions[o.id].included
            decisions[o.id] = d
        await self.emit("ai", f"Поз. {item.pos}: ИИ дочитал {plural(len(todo), 'карточку', 'карточки', 'карточек')}, "
                        f"подтвердил с цитатой {plural(found, 'характеристику', 'характеристики', 'характеристик')}, "
                        f"нашёл {conflicts} возможных расхождений для проверки закупщиком, "
                        f"отклонил без подтверждения цитатой {rejected}; решение изменилось у {changed}",
                        pos=item.pos, read=len(todo), found=found, conflicts=conflicts, rejected=rejected,
                        changed=changed)

    async def _collect(self, src: Source, item: Item, queries: list[str]) -> tuple[list[Offer], SourceStatus]:
        st = SourceStatus(source=src.name, title=src.title, status="ok")
        t0 = time.monotonic()
        got: dict[str, Offer] = {}
        for q in queries[: self.max_queries]:
            st.queries.append(q)
            try:
                for o in await src.search(item, q):
                    got.setdefault(o.id, o)
            except SourceUnavailable as e:
                st.status, st.error = "unavailable", str(e)
                break
            except Exception as e:  # noqa: BLE001 — сбой одного источника не должен ронять расчёт
                # Пустая выдача по уточнённому запросу после удачного первого — не поломка источника.
                if got:
                    continue
                if isinstance(e, LayoutChanged):
                    st.error = "карточек нет: пустая выдача по запросу или изменилась вёрстка сайта"
                    continue
                st.status, st.error = "error", _human(e)
                break
            if len(got) >= src.limit:
                break
        st.found = len(got)
        st.elapsed_ms = int((time.monotonic() - t0) * 1000)
        if st.status == "ok" and not got:
            st.status = "empty"
        found = plural(st.found, "предложение", "предложения", "предложений")
        await self.emit("source", f"Поз. {item.pos} · {src.title}: "
                        + (found if st.status in ("ok", "empty")
                           else f"{found}, затем источник перестал отвечать" if st.found
                           else f"недоступен ({st.error})"),
                        pos=item.pos, source=src.name, status=st.status, found=st.found)
        return list(got.values()), st


def dedup(offers: list[Offer]) -> tuple[list[Offer], list[tuple[str, str]]]:
    """Один документ — одна цена: ЕИС и его зеркало дают один id, магазины дублируют карточки по URL."""
    rank = {"eis": 0, "clearspending": 1}
    keep: dict[str, Offer] = {}
    dups: list[tuple[str, str]] = []
    for o in sorted(offers, key=lambda x: rank.get(x.source, 5)):
        keys = [o.id, f"url:{o.url}"]
        if o.kind == "contract" and o.doc_date:
            who = o.supplier or o.customer
            if who:
                keys.append(f"sp:{who}:{o.price:.2f}:{o.doc_date}")
        hit = next((k for k in keys if k in keep), None)
        if hit:
            dups.append((o.id, keep[hit].id))
            continue
        for k in keys:
            keep[k] = o
    seen, out = set(), []
    for o in keep.values():
        if o.id not in seen:
            seen.add(o.id)
            out.append(o)
    return out, dups


def source_kinds() -> dict[str, str]:
    return {c["id"]: c.get("kind") or c.get("type", "") for c in sources_conf().get("sources", [])}


def supplier_key(o: Offer, kinds: dict[str, str]) -> str | None:
    """Кто продаёт: ИНН или название из документа, для каталога и сайта из поиска — сам сайт.
    У маркетплейса продавцов много, без названия продавца он не сворачивается."""
    if o.supplier:
        if "ИНН" in o.supplier:
            return "inn:" + o.supplier.split("ИНН")[-1].strip(" :")
        name = re.sub(r"\b(ооо|ао|пао|зао|оао|ип)\b|[«»\"'\s.,]", "", o.supplier.lower())
        return "name:" + name if name else None
    if kinds.get(o.source) in ("catalog", "web") and o.url.startswith("http"):
        host = (urlsplit(o.url).hostname or "").removeprefix("www.")
        return "site:" + host if host else None
    return None


def _one_per_supplier(offers: list[Offer], decisions: dict[str, Decision], kinds: dict[str, str] | None = None) -> None:
    """П. 3.19 Приказа № 567: цены от различных поставщиков. Из контрактов одного поставщика берётся самый свежий,
    из предложений одного сайта — самое близкое к ТЗ (сама модель раньше модификации)."""
    if not rules().get("method", {}).get("one_offer_per_supplier", True):
        return
    kinds = source_kinds() if kinds is None else kinds

    def rank(o: Offer) -> tuple:
        d = decisions[o.id]
        exact = not any(r[0] in ("modification", "equivalent") for r in d.reasons)
        return (o.kind == "contract", o.doc_date or datetime.min.date(), d.score, exact) if o.kind == "contract" \
            else (False, d.score, exact, o.doc_date or datetime.min.date())

    best: dict[str, Offer] = {}
    for o in offers:
        d = decisions[o.id]
        key = supplier_key(o, kinds) if d.included else None
        if not key:
            continue
        prev = best.get(key)
        if prev is None or rank(o) > rank(prev):
            if prev:
                _drop(decisions[prev.id], o)
            best[key] = o
        else:
            _drop(d, prev)


def _one_per_product(item: Item, offers: list[Offer], decisions: dict[str, Decision]) -> None:
    """Фасовки одного товара в каталоге — одна цена: берётся ближайшая к ТЗ не меньше требуемой, иначе крупнейшая."""
    groups: dict[str, list[Offer]] = {}
    for o in offers:
        if decisions[o.id].included and o.kind != "contract" and "#" in o.url:
            groups.setdefault(o.url.split("#")[0], []).append(o)
    need = item.pack_amount or 0
    for group in groups.values():
        if len(group) < 2:
            continue
        sized = [(_amount_ug(o.title) or 0, o) for o in group]
        enough = [x for x in sized if x[0] >= need]
        keep = min(enough, key=lambda x: x[0])[1] if enough else max(sized, key=lambda x: x[0])[1]
        for _, o in sized:
            if o is not keep:
                d = decisions[o.id]
                d.included = False
                d.reasons.append(("same_product", f"другая фасовка того же товара — в расчёт взята «{keep.title.split(', ')[-1]}»"))


def _drop(d: Decision, kept: Offer) -> None:
    d.included = False
    if kept.kind == "contract":
        d.reasons.append(("same_supplier", f"тот же поставщик — в расчёт взят более свежий контракт {kept.doc_number or kept.id}"))
    else:
        who = kept.supplier or (urlsplit(kept.url).hostname or "").removeprefix("www.")
        d.reasons.append(("same_supplier", f"тот же поставщик ({who}) — от него в расчёт взята одна цена: «{kept.title[:70]}»"))


def _by_identity(offers: list[Offer], decisions: dict[str, Decision]) -> None:
    """Если в ТЗ названа модель: сначала сама модель, затем её модификации, эквиваленты — только при нехватке."""
    need = int(rules().get("method", {}).get("min_offers", 3))
    strong = [o for o in offers if decisions[o.id].included
              and not any(r[0] == "equivalent" for r in decisions[o.id].reasons)]
    if len(strong) < need:
        return
    for o in offers:
        d = decisions[o.id]
        if d.included and any(r[0] == "equivalent" for r in d.reasons) and d.score < 1:
            if not any(r[0] == "modification" for r in d.reasons) and "другая модель" in " ".join(r[1] for r in d.reasons):
                d.included = False
                d.reasons.append(("reserve", f"резерв: по модели из ТЗ и её модификациям уже {len(strong)} {_prices(len(strong))}, другие модели в расчёт не берутся"))


def _by_tier(offers: list[Offer], decisions: dict[str, Decision], tiers: dict[str, int]) -> None:
    """Источники берутся по старшинству: свежие контракты ЕИС, затем зеркало реестра, затем публичные оферты.
    Следующий уровень подключается, только если выборка меньше целевой."""
    m = rules().get("method", {})
    target = int(m.get("sample_target", m.get("min_offers", 3)))
    taken = 0
    for tier in sorted(set(tiers.values())):
        group = [o for o in offers if tiers.get(o.source, 9) == tier and decisions[o.id].included]
        if taken >= target:
            for o in group:
                d = decisions[o.id]
                d.included = False
                d.reasons.append(("reserve", f"резерв: из более приоритетных источников уже {taken} {_prices(taken)}"))
        else:
            taken += len(group)


def empty_calc(message: str) -> Calculation:
    return Calculation(status="insufficient", n=0, prices=[], offer_ids=[], message=message)


def recalc(run: Run, res: ItemResult, why: str = "добавлено КП") -> None:
    """Пересчёт позиции после добавления КП: отсев аномалий и проверка однородности выполняются заново."""
    for d in res.decisions:
        if d.reasons and d.reasons[-1][0] == "outlier":
            d.reasons.pop()
            d.included = True
        if d.override:
            d.included = d.override["action"] == "include" and bool(d.unit_price)
    prices = {d.offer_id: d.unit_price for d in res.decisions if d.included and d.unit_price}
    res.calc, excluded = calculate(prices, res.item.qty)
    by_id = {d.offer_id: d for d in res.decisions}
    for i, why in excluded.items():
        by_id[i].included = False
        by_id[i].reasons.append(("outlier", why))
    totals = [r.calc.total for r in run.results]
    run.total = round(sum(t for t in totals if t), 2) if all(totals) else None
    run.status = "ok" if run.total is not None else "partial"
    analogs.attach(res)
    run.trace.append(TraceEvent(at=datetime.now(), stage="calc",
                                message=f"Поз. {res.item.pos}: {why}, пересчёт — {res.calc.message}"))


def _prices(n: int) -> str:
    return plural(n, "сопоставимая цена", "сопоставимые цены", "сопоставимых цен").split(" ", 1)[1]


def override(run: Run, res: ItemResult, offer_id: str, action: str, comment: str, user: str | None = None) -> Decision:
    """Решение закупщика поверх автоматического: принять однородный товар или исключить предложение.
    Записывается с автором, временем и комментарием и попадает в отчёт — ответственность остаётся за человеком."""
    if action not in ("include", "exclude", "reset"):
        raise ValueError("action: include, exclude или reset")
    d = next((x for x in res.decisions if x.offer_id == offer_id), None)
    o = next((x for x in res.offers if x.id == offer_id), None)
    if d is None or o is None:
        raise ValueError("Предложение не найдено")
    if action == "reset":
        if d.override:
            d.included = d.override.get("was", d.included)
            d.override = None
            d.reasons = [r for r in d.reasons if not r[0].startswith("user_")]
            recalc(run, res, why=f"отменено решение пользователя по «{o.title[:60]}»")
        return d
    comment = comment.strip()[:300]
    if not comment:
        raise ValueError("Нужен комментарий: почему принято решение")
    if action == "include" and not d.unit_price:
        probe = Decision(offer_id=o.id, included=True, score=d.score)
        normalize(res.item, o, probe)
        if not probe.unit_price or not probe.included:
            raise ValueError("Цену этого предложения нельзя привести к единице ТЗ: " +
                             (probe.reasons[-1][1] if probe.reasons else "нет цены"))
        d.unit_price, d.normalized_note, d.factor = probe.unit_price, probe.normalized_note, probe.factor
    was = d.override.get("was") if d.override else d.included
    d.override = {"action": action, "comment": comment, "by": user, "at": datetime.now().isoformat(timespec="seconds"),
                  "was": was}
    d.reasons = [r for r in d.reasons if not r[0].startswith("user_")]
    d.reasons.append(("user_include" if action == "include" else "user_exclude",
                      ("принято пользователем как однородный товар" if action == "include" else "исключено пользователем")
                      + f": {comment}"))
    d.included = action == "include"
    recalc(run, res, why=("включено" if action == "include" else "исключено") + f" пользователем предложение «{o.title[:60]}»")
    return d
