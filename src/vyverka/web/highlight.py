"""Подсветка цены и характеристик в сохранённой копии страницы.

Закупщику и ревизору мало знать, что копия есть: им нужно увидеть, откуда взята цифра. Разметка
страницы не меняется, отметки ставятся только в текст между тегами, поэтому копия остаётся
той же страницей, а хеш в шапке относится к исходному телу ответа."""
import html
import json
import re

_SKIP = re.compile(r"<(script|style|noscript|textarea|title)\b.*?</\1\s*>", re.S | re.I)
_TAG = re.compile(r"(<[^>]*>)")
_SEP = r"(?:[\s\u00a0\u202f.']|&nbsp;|&#160;|&thinsp;)?"


def price_pattern(price: float) -> str:
    """1 183 333,33 / 1183333.33 / 1 183 333 — цена записывается на сайтах по-разному."""
    whole = f"{int(price):d}"
    groups = []
    while whole:
        groups.insert(0, whole[-3:])
        whole = whole[:-3]
    body = _SEP.join(groups)
    frac = f"{price:.8f}".split(".")[1].rstrip("0")
    # ЕИС пишет цену за единицу со всеми знаками («343,3333333»), каталоги — до копеек
    tail = rf"[.,]{frac[:2]}\d*" if frac else r"(?:[.,]0+)?"
    return rf"(?<![\d]){body}{tail}(?![\d])"


def term_pattern(term: str) -> str | None:
    term = term.strip()
    if len(term) < 3:
        return None
    parts = [re.escape(p) for p in re.split(r"\s+", term) if p]
    return r"\s*".join(parts)


def _compile(price: float | None, terms: list[str]) -> re.Pattern | None:
    pats = [price_pattern(price)] if price else []
    pats += [p for p in (term_pattern(t) for t in terms) if p]
    if not pats:
        return None
    pats.sort(key=len, reverse=True)
    return re.compile("|".join(f"(?:{p})" for p in pats), re.I)


def _mark(text: str, rx: re.Pattern, counter: list[int]) -> str:
    def sub(m: re.Match) -> str:
        counter[0] += 1
        return f'<mark class="vv-hl" id="vv-{counter[0]}">{m.group(0)}</mark>'
    return rx.sub(sub, text)


def _banner(meta: dict, found: int, price: float | None, terms: list[str]) -> str:
    what = []
    if price:
        what.append(f"цена {price:,.2f}".replace(",", " ").replace(".", ","))
    what += [f"«{t}»" for t in terms[:8]]
    if found:
        # скрипты в копии запрещены, а часть отметок может оказаться в скрытых блоках страницы,
        # поэтому переход даётся к каждой
        links = " ".join(f'<a href="#vv-{i}" style="color:#e0b43a">{i}</a>' for i in range(1, min(found, 30) + 1))
        jump = f" Перейти: {links}"
    else:
        jump = " В тексте копии не найдено, проверьте карточку по ссылке."
    return (
        '<div style="all:initial;display:block;position:sticky;top:0;z-index:2147483647;background:#1f2a24;'
        'color:#f4f1e8;font:13px/1.45 system-ui,sans-serif;padding:8px 14px;border-bottom:3px solid #e0b43a">'
        f'Копия страницы <b>{html.escape(meta.get("url", ""))}</b>, сохранена {html.escape(meta.get("fetched_at", "")[:16].replace("T", " "))} UTC, '
        f'SHA-256 <code style="color:#e0b43a">{html.escape(meta.get("sha256", "")[:16])}…</code>. '
        f'Подсвечено: {html.escape(", ".join(what) or "—")}; отметок {found}.'
        f'<span style="color:#e0b43a">{jump}</span></div>'
    )


_STYLE = ("<style>:root{color-scheme:light}:where(html){background:#fff;color:#1a1a1a}mark.vv-hl{background:#ffe066!important;color:#111!important;outline:2px solid #e8590c;"
          "border-radius:2px;scroll-margin-top:64px}</style>")


def render(body: bytes, meta: dict, price: float | None, terms: list[str]) -> str:
    text = body.decode("utf-8", "replace")
    rx = _compile(price, terms)
    counter = [0]
    if text.lstrip()[:1] in ("{", "["):
        try:
            text = json.dumps(json.loads(text), ensure_ascii=False, indent=2)
        except ValueError:
            pass
        esc = html.escape(text)
        inner = _mark(esc, rx, counter) if rx else esc
        page = f'<pre style="white-space:pre-wrap;font:13px/1.5 ui-monospace,monospace;padding:16px">{inner}</pre>'
        return f"<!doctype html><meta charset=utf-8>{_STYLE}{_banner(meta, counter[0], price, terms)}{page}"

    if rx:
        out, pos = [], 0
        for m in _SKIP.finditer(text):
            out.append(_mark_html(text[pos:m.start()], rx, counter))
            out.append(m.group(0))
            pos = m.end()
        out.append(_mark_html(text[pos:], rx, counter))
        text = "".join(out)
    head = _STYLE + _banner(meta, counter[0], price, terms)
    body_tag = re.search(r"<body\b[^>]*>", text, re.I)
    if body_tag:
        return text[:body_tag.end()] + head + text[body_tag.end():]
    return f"<meta charset=utf-8>{head}{text}"


def _mark_html(chunk: str, rx: re.Pattern, counter: list[int]) -> str:
    return "".join(p if p.startswith("<") else _mark(p, rx, counter) for p in _TAG.split(chunk))
