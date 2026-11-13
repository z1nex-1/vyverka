"""Хранилище прогонов и запуск расчёта в фоне — общее для веб-интерфейса и API."""

import asyncio
import json
import logging
import os
import re
from urllib.parse import urlsplit

import httpx

from ..config import data_dir
from ..models import Run, TraceEvent
from ..pipeline import Pipeline

log = logging.getLogger("vyverka")


class Runs:
    """Прогоны живут в памяти, пока считаются, и сохраняются в data/runs для повторного открытия."""

    def __init__(self):
        self.live: dict[str, Run] = {}
        self.waiters: dict[str, list[asyncio.Queue]] = {}

    @property
    def dir(self):
        d = data_dir() / "runs"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def get(self, rid: str) -> Run | None:
        if rid in self.live:
            return self.live[rid]
        if not re.fullmatch(r"[a-f0-9]{12}", rid):
            return None
        p = self.dir / f"{rid}.json"
        if p.exists():
            run = Run.model_validate_json(p.read_text(encoding="utf-8"))
            self.live[rid] = run
            return run
        return None

    def save(self, run: Run) -> None:
        (self.dir / f"{run.id}.json").write_text(run.model_dump_json(), encoding="utf-8")

    def recent(self, n: int = 8) -> list[dict]:
        out = []
        for p in sorted(self.dir.glob("*.json"), key=lambda x: x.stat().st_mtime, reverse=True)[:n]:
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except ValueError:
                continue
            out.append({"id": d["id"], "title": d["spec"]["title"], "total": d.get("total"),
                        "status": d.get("status"), "at": d["created_at"][:16].replace("T", " "),
                        "items": len(d["spec"]["items"]), "policy": d.get("settings", {}).get("policy", "strict"),
                        "ok": sum(1 for r in d.get("results", []) if r.get("calc", {}).get("status") == "ok"),
                        "file": d["spec"].get("source_name", "")})
        return out

    async def publish(self, rid: str, ev: TraceEvent | None) -> None:
        for q in self.waiters.get(rid, []):
            await q.put(ev)


RUNS = Runs()
_JOBS: set[asyncio.Task] = set()


def summary(run: Run) -> dict:
    return {"id": run.id, "status": run.status, "title": run.spec.title, "total": run.total,
            "policy": run.settings.get("policy"), "created_at": run.created_at.isoformat(),
            "items": [{"pos": r.item.pos, "name": r.item.name, "qty": r.item.qty, "unit": r.item.unit,
                       "status": r.calc.status, "n": r.calc.n, "cv": r.calc.cv, "unit_price": r.calc.unit_price,
                       "total": r.calc.total, "message": r.calc.message,
                       "estimate": (r.estimate or {}).get("value")} for r in run.results]}


def webhook_allowed(url: str) -> bool:
    """Обратный вызов — только на адреса из VYVERKA_WEBHOOK_HOSTS: иначе сервер можно заставить
    стучаться во внутреннюю сеть."""
    hosts = [h.strip().lower() for h in os.environ.get("VYVERKA_WEBHOOK_HOSTS", "").split(",") if h.strip()]
    parts = urlsplit(url)
    return parts.scheme in ("http", "https") and (parts.hostname or "").lower() in hosts


async def _notify(url: str, run: Run) -> None:
    try:
        async with httpx.AsyncClient(timeout=15) as c:
            await c.post(url, json={"event": "run.finished", "run": summary(run)})
    except httpx.HTTPError as e:
        log.warning("webhook %s: %s", url, e)


async def start(spec, mode: str, policy: str | None = None, user: str | None = None,
                callback: str | None = None) -> str:
    ready = asyncio.Event()
    holder: dict[str, str] = {}

    async def listener(ev: TraceEvent):
        if not ready.is_set():
            holder["id"] = pipe.run.id
            RUNS.live[pipe.run.id] = pipe.run
            ready.set()
        await RUNS.publish(pipe.run.id, ev)

    pipe = Pipeline(mode=mode, listener=listener, policy=policy, user=user)

    async def job():
        try:
            run = await pipe.execute(spec)
        except Exception as e:  # noqa: BLE001 — ошибка прогона показывается пользователю, а не роняет сервер
            run = pipe.run
            run.status = "failed"
            await pipe.emit("error", f"Расчёт прерван: {e}")
        RUNS.save(run)
        await RUNS.publish(run.id, None)
        if callback:
            await _notify(callback, run)

    task = asyncio.create_task(job())
    _JOBS.add(task)
    task.add_done_callback(_JOBS.discard)
    await ready.wait()
    return holder["id"]


async def wait(rid: str, timeout: float) -> Run | None:
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        run = RUNS.get(rid)
        if run and run.status != "running":
            return run
        await asyncio.sleep(0.3)
    return RUNS.get(rid)
