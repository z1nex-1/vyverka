import os
from contextlib import contextmanager
from contextvars import ContextVar
from functools import cache
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]
_pinned: ContextVar[dict | None] = ContextVar("pinned_rules", default=None)


def config_dir() -> Path:
    return Path(os.environ.get("VYVERKA_CONFIG", ROOT / "config"))


def data_dir() -> Path:
    p = Path(os.environ.get("VYVERKA_DATA", ROOT / "data"))
    p.mkdir(parents=True, exist_ok=True)
    return p


@cache
def load(name: str) -> dict:
    path = config_dir() / f"{name}.yaml"
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def attribute_keys() -> dict[str, list[str]]:
    return load("attributes")


def _merge(base: dict, over: dict) -> dict:
    out = dict(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def rules() -> dict:
    """Заводские правила rules.yaml с поправками из настроек организации (data/settings.json)."""
    if (pinned := _pinned.get()) is not None:
        return pinned
    from .settings import rules_overrides

    over = rules_overrides()
    return _merge(load("rules"), over) if over else load("rules")


@contextmanager
def pinned_rules(r: dict):
    """Правила метода, действовавшие на момент расчёта: с ними журнал пересчитывается так же, как тогда."""
    token = _pinned.set(r)
    try:
        yield
    finally:
        _pinned.reset(token)


def sources() -> dict:
    """Источники sources.yaml и добавленные пользователем, с включением, отключением и доверием."""
    from .settings import effective_sources

    return {"sources": effective_sources(load("sources").get("sources", []))}
