import asyncio
import gzip
import hashlib
import json
import random
import re
import ssl
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import certifi
import httpx

from .config import ROOT, data_dir

UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36"


class SourceUnavailable(Exception):
    pass


_DATE_VALUE = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{4}$|^\d{4}-\d{2}-\d{2}")


def _dated(url: str) -> bool:
    return any(_DATE_VALUE.match(v) for _, v in parse_qsl(urlsplit(url).query))


def undated(url: str) -> str:
    """Адрес без параметров-дат: окно поиска «за 3 года до сегодня» сдвигается каждый день,
    а сохранённая страница должна находиться и завтра."""
    u = urlsplit(url)
    q = [(k, v) for k, v in parse_qsl(u.query, keep_blank_values=True) if not _DATE_VALUE.match(v)]
    return urlunsplit((u.scheme, u.netloc, u.path, urlencode(sorted(q), doseq=True), ""))


@dataclass
class Page:
    url: str
    status: int
    text: str
    fetched_at: datetime
    sha256: str
    snapshot: str
    from_cache: bool


def _ssl_context() -> ssl.SSLContext:
    ctx = ssl.create_default_context(cafile=certifi.where())
    bundle = ROOT / "certs" / "ru_bundle.crt"
    if bundle.exists():
        ctx.load_verify_locations(str(bundle))
    return ctx


class SnapshotStore:
    """Хранит каждый полученный ответ как доказательство: тело (gzip), URL, время, хеш."""

    def __init__(self, root: Path | None = None, seed: Path | None = None):
        self.root = root or data_dir() / "snapshots"
        self.root.mkdir(parents=True, exist_ok=True)
        self.seed = seed or ROOT / "seed" / "snapshots"
        self._undated: dict[str, tuple[str, str]] | None = None

    @staticmethod
    def key(url: str) -> str:
        return hashlib.sha256(url.encode()).hexdigest()[:32]

    def _paths(self, key: str) -> list[Path]:
        return [self.root / f"{key}.json", self.seed / f"{key}.json"]

    def get(self, url: str) -> Page | None:
        k = self.key(url)
        page = self._load(k)
        if page is None and _dated(url):
            near = self._index().get(undated(url))
            if near:
                page = self._load(near[1])
        return page

    def _load(self, key: str) -> Page | None:
        for meta_path in self._paths(key):
            if meta_path.exists():
                meta = json.loads(meta_path.read_text(encoding="utf-8"))
                body = gzip.decompress(meta_path.with_suffix(".gz").read_bytes()).decode("utf-8", "replace")
                return Page(url=meta["url"], status=meta["status"], text=body,
                            fetched_at=datetime.fromisoformat(meta["fetched_at"]), sha256=meta["sha256"],
                            snapshot=key, from_cache=True)
        return None

    def _index(self) -> dict[str, tuple[str, str]]:
        if self._undated is None:
            self._undated = {}
            for d in (self.seed, self.root):
                if d.exists():
                    for mp in d.glob("*.json"):
                        try:
                            meta = json.loads(mp.read_text(encoding="utf-8"))
                        except (OSError, ValueError):
                            continue
                        self._remember(meta["url"], meta["fetched_at"], mp.stem)
        return self._undated

    def _remember(self, url: str, fetched_at: str, key: str) -> None:
        if self._undated is None or not _dated(url):
            return
        u = undated(url)
        prev = self._undated.get(u)
        if prev is None or prev[0] < fetched_at:
            self._undated[u] = (fetched_at, key)

    def put(self, url: str, status: int, text: str) -> Page:
        k = self.key(url)
        body = text.encode("utf-8")
        sha = hashlib.sha256(body).hexdigest()
        now = datetime.now(timezone.utc)
        self._archive(k, sha)
        (self.root / f"{k}.gz").write_bytes(gzip.compress(body, 6))
        meta = {"url": url, "status": status, "fetched_at": now.isoformat(), "sha256": sha}
        (self.root / f"{k}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        self._remember(url, meta["fetched_at"], k)
        return Page(url=url, status=status, text=text, fetched_at=now, sha256=sha, snapshot=k, from_cache=False)

    def _archive(self, key: str, new_sha: str) -> None:
        """Прошлые расчёты ссылаются на хеш своей копии, поэтому при обновлении страницы старая копия
        не перезаписывается, а уходит в versions/ под своим хешем."""
        meta_path = self.root / f"{key}.json"
        if not meta_path.exists():
            return
        try:
            old = json.loads(meta_path.read_text(encoding="utf-8"))["sha256"]
        except (OSError, ValueError, KeyError):
            return
        if old == new_sha:
            return
        vdir = self.root / "versions"
        vdir.mkdir(exist_ok=True)
        for ext in (".gz", ".json"):
            src = meta_path.with_suffix(ext)
            if src.exists():
                src.replace(vdir / f"{key}.{old[:16]}{ext}")

    def version_path(self, key: str, sha: str) -> Path | None:
        """Тело снимка с указанным хешем: текущая копия или одна из прошлых версий."""
        for p in self._paths(key):
            if p.exists():
                try:
                    if json.loads(p.read_text(encoding="utf-8")).get("sha256") == sha:
                        return p.with_suffix(".gz")
                except (OSError, ValueError):
                    pass
            v = p.parent / "versions" / f"{key}.{sha[:16]}.gz"
            if v.exists():
                return v
        return None

    def versions(self, key: str) -> list[dict]:
        out = []
        for p in self._paths(key):
            for mp in sorted((p.parent / "versions").glob(f"{key}.*.json")) + ([p] if p.exists() else []):
                try:
                    out.append(json.loads(mp.read_text(encoding="utf-8")))
                except (OSError, ValueError):
                    pass
        return sorted(out, key=lambda m: m.get("fetched_at", ""))

    def body_path(self, key: str) -> Path | None:
        for p in self._paths(key):
            gz = p.with_suffix(".gz")
            if gz.exists():
                return gz
        return None


class Breaker:
    def __init__(self, threshold: int = 3, cooldown: float = 120):
        self.threshold, self.cooldown = threshold, cooldown
        self.fails: dict[str, int] = {}
        self.opened: dict[str, float] = {}

    def allow(self, host: str) -> bool:
        t = self.opened.get(host)
        if t and time.monotonic() - t < self.cooldown:
            return False
        return True

    def ok(self, host: str) -> None:
        self.fails.pop(host, None)
        self.opened.pop(host, None)

    def fail(self, host: str) -> None:
        self.fails[host] = self.fails.get(host, 0) + 1
        if self.fails[host] >= self.threshold:
            self.opened[host] = time.monotonic()


class Fetcher:
    """mode: live — сеть, при сбое кеш; cache — сначала кеш, потом сеть; offline — только кеш."""

    def __init__(self, mode: str = "live", store: SnapshotStore | None = None, timeout: float = 20,
                 retries: int = 2, concurrency: int = 4, max_age_hours: float = 72):
        self.mode = mode
        self.store = store or SnapshotStore()
        self.timeout = timeout
        self.retries = retries
        self.max_age = max_age_hours * 3600
        self.breaker = Breaker()
        self.sem: dict[str, asyncio.Semaphore] = {}
        self.concurrency = concurrency
        self.client = httpx.AsyncClient(verify=_ssl_context(), timeout=timeout, follow_redirects=True,
                                        headers={"User-Agent": UA, "Accept-Language": "ru-RU,ru;q=0.9"})

    async def close(self) -> None:
        await self.client.aclose()

    def _fresh(self, page: Page) -> bool:
        age = (datetime.now(timezone.utc) - page.fetched_at).total_seconds()
        return age < self.max_age

    async def get(self, url: str, params: dict | None = None, headers: dict | None = None) -> Page:
        full = f"{url}?{urlencode(params, doseq=True)}" if params else url
        cached = self.store.get(full)
        if self.mode == "offline":
            if cached:
                return cached
            raise SourceUnavailable("нет сохранённого снимка (офлайн-режим)")
        if self.mode == "cache" and cached and self._fresh(cached):
            return cached
        host = urlsplit(full).hostname or ""
        if not self.breaker.allow(host):
            if cached:
                return cached
            raise SourceUnavailable(f"{host}: источник временно отключён после серии ошибок")
        sem = self.sem.setdefault(host, asyncio.Semaphore(self.concurrency))
        last: Exception | None = None
        async with sem:
            for attempt in range(self.retries + 1):
                try:
                    r = await self.client.get(full, headers=headers)
                    if r.status_code in (429, 502, 503, 504):
                        raise httpx.HTTPStatusError(f"HTTP {r.status_code}", request=r.request, response=r)
                    if r.status_code >= 400:
                        if r.status_code in (401, 403):
                            self.breaker.fail(host)
                        raise SourceUnavailable(f"HTTP {r.status_code}")
                    self.breaker.ok(host)
                    return self.store.put(full, r.status_code, r.text)
                except SourceUnavailable:
                    if cached:
                        return cached
                    raise
                except (httpx.HTTPError, ssl.SSLError) as e:
                    last = e
                    await asyncio.sleep(0.6 * (2 ** attempt) + random.random() * 0.3)
        self.breaker.fail(host)
        if cached:
            return cached
        raise SourceUnavailable(_human(last))


def _human(e: Exception | None) -> str:
    if isinstance(e, httpx.TimeoutException):
        return "таймаут соединения"
    if isinstance(e, httpx.ConnectError):
        return "нет соединения с сервером"
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        if code == 429:
            return "сайт временно ограничил частоту запросов, расчёт идёт по остальным источникам"
        if code in (401, 403):
            return "сайт закрыл доступ автоматическим запросам"
        if code >= 500:
            return "сайт временно не работает"
        return f"страница не найдена (код {code})"
    return str(e) or e.__class__.__name__
