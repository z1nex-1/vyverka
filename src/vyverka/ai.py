"""ИИ-версия: языковая модель дочитывает карточку товара, когда правила не нашли характеристику ТЗ.

Модель только извлекает значение и обязана привести цитату. Значение принимается, если цитата дословно
есть в тексте источника и в ней есть цифры значения; решение о сопоставимости по-прежнему принимают правила.
По умолчанию модель локальная (Ollama), данные закупки никуда не уходят.
"""
import asyncio
import hashlib
import json
import os
import re
import time

import httpx

from .config import ROOT, data_dir
from .models import Offer, Requirement

PROMPT_VERSION = "1"
SYSTEM = ("Ты читаешь карточку товара из закупки. Для каждой характеристики из списка найди её значение в тексте "
          "карточки. Отвечай только JSON: {\"found\": [{\"name\": <характеристика из списка>, \"value\": <значение "
          "с единицей>, \"quote\": <точный фрагмент текста карточки, где это значение написано>}]}. "
          "Если значения в тексте нет — не включай характеристику. Ничего не придумывай и не выводи из общих знаний.")
MAX_TEXT = 3500


def enabled() -> bool:
    return os.environ.get("VYVERKA_AI", "") not in ("", "0", "false")


def model() -> str:
    return os.environ.get("VYVERKA_LLM_MODEL", "yandexgpt-lite:8b")


def info() -> dict:
    return {"model": model(), "url": _url(), "api": _api()}


def _url() -> str:
    return os.environ.get("VYVERKA_LLM_URL", "http://localhost:11434").rstrip("/")


def _api() -> str:
    """ollama — родной API Ollama; openai — совместимый (vLLM, YandexGPT, GigaChat через шлюз)."""
    return os.environ.get("VYVERKA_LLM_API", "ollama")


def _norm(s: str) -> str:
    s = s.lower().replace("ё", "е").replace("\u00a0", " ")
    s = re.sub(r"[«»\"'`]", "", s)
    s = re.sub(r"\s*([,.:;()/%×x*-])\s*", r"\1", s)
    return re.sub(r"\s+", " ", s).strip()


def source_text(offer: Offer) -> str:
    attrs = "\n".join(f"{k}: {v}" for k, v in offer.attributes.items())
    return f"{offer.title}\n{attrs}\n{offer.text}"[:MAX_TEXT]


def verify(value: str, quote: str, text: str) -> str | None:
    """Почему находка отклонена, или None, если цитата подтверждает значение."""
    if not value or not quote:
        return "нет цитаты"
    q = _norm(quote)
    if len(q) < 2 or q not in _norm(text):
        return "цитаты нет в тексте источника"
    digits = re.findall(r"\d+(?:[.,]\d+)?", value)
    if digits:
        qd = {d.replace(",", ".") for d in re.findall(r"\d+(?:[.,]\d+)?", quote)}
        if not all(d.replace(",", ".") in qd for d in digits):
            return "числа значения нет в цитате"
    elif _norm(value) not in q:
        return "значения нет в цитате"
    return None


class Reader:
    def __init__(self, concurrency: int = 1):
        self.sem = asyncio.Semaphore(concurrency)
        self.cache = data_dir() / "ai_cache"
        self.seed = ROOT / "seed" / "ai_cache"
        self.calls = 0
        self.cached = 0
        self.seconds = 0.0

    def _key(self, reqs: list[str], text: str) -> str:
        raw = json.dumps([PROMPT_VERSION, model(), reqs, text], ensure_ascii=False)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def _load(self, key: str) -> list | None:
        for d in (self.cache, self.seed):
            p = d / f"{key}.json"
            if p.exists():
                return json.loads(p.read_text(encoding="utf-8"))["found"]
        return None

    async def ask(self, reqs: list[str], text: str) -> list[dict]:
        key = self._key(reqs, text)
        if (hit := self._load(key)) is not None:
            self.cached += 1
            return hit
        user = f"Характеристики: {json.dumps(reqs, ensure_ascii=False)}\n\nКарточка:\n{text}"
        async with self.sem:
            t0 = time.monotonic()
            content = await _chat(SYSTEM, user)
            self.seconds += time.monotonic() - t0
            self.calls += 1
        try:
            found = json.loads(content).get("found") or []
        except (json.JSONDecodeError, AttributeError):
            found = []
        found = [f for f in found if isinstance(f, dict)]
        self.cache.mkdir(parents=True, exist_ok=True)
        (self.cache / f"{key}.json").write_text(json.dumps({"model": model(), "reqs": reqs, "found": found},
                                                           ensure_ascii=False, indent=1), encoding="utf-8")
        return found

    async def read(self, offer: Offer, reqs: list[Requirement]) -> tuple[dict, list[dict]]:
        """Принятые значения {key: {...}} и отклонённые находки с причиной."""
        text = source_text(offer)
        by_name = {r.name: r for r in reqs}
        accepted, rejected = {}, []
        for f in await self.ask([r.name for r in reqs], text):
            r = by_name.get(str(f.get("name", "")).strip())
            value, quote = str(f.get("value") or "").strip(), str(f.get("quote") or "").strip()
            if not r or not value or value.lower() in ("не указано", "нет", "—", "-", "null", "none"):
                continue
            if why := verify(value, quote, text):
                rejected.append({"name": r.name, "value": value, "quote": quote, "why": why})
            else:
                accepted[r.key] = {"name": r.name, "value": value, "quote": quote, "model": model()}
        return accepted, rejected


async def _chat(system: str, user: str) -> str:
    msgs = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    async with httpx.AsyncClient(timeout=float(os.environ.get("VYVERKA_LLM_TIMEOUT", "120"))) as c:
        if _api() == "openai":
            headers = {"Authorization": f"Bearer {os.environ['VYVERKA_LLM_KEY']}"} if os.environ.get("VYVERKA_LLM_KEY") else {}
            r = await c.post(f"{_url()}/chat/completions", headers=headers, json={
                "model": model(), "temperature": 0, "messages": msgs, "response_format": {"type": "json_object"}})
            r.raise_for_status()
            return r.json()["choices"][0]["message"]["content"]
        r = await c.post(f"{_url()}/api/chat", json={
            "model": model(), "think": False, "stream": False, "format": "json",
            "options": {"temperature": 0, "num_ctx": 4096}, "messages": msgs})
        r.raise_for_status()
        return r.json()["message"]["content"]
