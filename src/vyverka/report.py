"""Выходные документы: обоснование НМЦД (DOCX), расчётная таблица (XLSX), полный журнал (JSON)."""

import io
from datetime import datetime

from docx import Document
from docx.enum.section import WD_ORIENT
from docx.enum.table import WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from . import rnp
from .models import ItemResult, Offer, Run
from .settings import legal_basis, org
from .words import plural

STATUS = {
    "ok": "обоснована",
    "heterogeneous": "не обоснована: цены неоднородны",
    "insufficient": "не обоснована: мало сопоставимых цен",
}
KIND = {"contract": "контракт", "registry": "реестр", "catalog": "каталог", "marketplace": "маркетплейс", "offer": "КП", "web": "сайт"}
CHECK = {"ok": "соответствует", "partial": "совместимо", "mismatch": "не соответствует", "unknown": "нет данных"}


def money(x: float | None) -> str:
    if x is None:
        return "—"
    return f"{x:,.2f}".replace(",", "\u00a0").replace(".", ",") + "\u00a0₽"


def used(r: ItemResult) -> list[Offer]:
    by_id = {o.id: o for o in r.offers}
    return [by_id[i] for i in r.calc.offer_ids if i in by_id]


def source_line(o: Offer) -> str:
    seen = o.evidence.fetched_at.strftime("%d.%m.%Y")
    if o.kind == "contract":
        d = o.doc_date.strftime("%d.%m.%Y") if o.doc_date else "дата не указана"
        fine = {True: "неустойки начислялись", False: "неустойки не начислялись"}.get(o.penalties, "сведения о неустойках в карточке нет")
        return (f"Контракт № {o.doc_number} от {d}, {o.supplier or 'поставщик не указан'}; {fine}; "
                f"источник — {o.source_title}, копия страницы от {seen}")
    return f"{o.source_title}: «{o.title}», цена на {seen}"


def price_note(o: Offer, unit_price: float | None, factor: float | None) -> str:
    """У контракта прошлых лет видна цена до индексации; месяцы и коэффициент — в строке «Приведение»."""
    if not unit_price or not factor or abs(factor - 1) < 1e-6:
        return ""
    return f" (до индексации {money(unit_price / factor)})"


def source_price(o: Offer, d) -> str:
    """Цена и единица так, как они записаны в источнике, если расчёт её пересчитал."""
    base = (d.unit_price or 0) / (d.factor or 1)
    if not d.unit_price or abs(o.price - base) < 0.01:
        return ""
    unit = f" за {o.qty:g} {o.unit}" if o.qty and o.qty != 1 and o.unit else (f" за {o.unit}" if o.unit else "")
    return f"; в источнике {money(o.price)}{unit}"


def price_of(r: ItemResult, o: Offer) -> float | None:
    d = next(x for x in r.decisions if x.offer_id == o.id)
    return d.unit_price


# ---------- XLSX ----------

THIN = Side(style="thin", color="B7B2A6")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
HEAD = PatternFill("solid", fgColor="EDE8DC")
OK = PatternFill("solid", fgColor="E3EEDD")
BAD = PatternFill("solid", fgColor="F4E1DC")


def _head(ws, row: int, values: list[str]) -> None:
    for c, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.font = Font(bold=True)
        cell.fill = HEAD
        cell.border = BOX
        cell.alignment = Alignment(wrap_text=True, vertical="top")


def _row(ws, row: int, values: list, fill=None) -> None:
    for c, v in enumerate(values, 1):
        cell = ws.cell(row=row, column=c, value=v)
        cell.border = BOX
        cell.alignment = Alignment(wrap_text=True, vertical="top")
        if fill:
            cell.fill = fill
        if isinstance(v, float):
            cell.number_format = '#,##0.00'


def _widths(ws, widths: list[int]) -> None:
    for i, w in enumerate(widths, 1):
        ws.column_dimensions[get_column_letter(i)].width = w


def xlsx(run: Run) -> bytes:
    wb = Workbook()
    ws = wb.active
    ws.title = "Расчёт НМЦД"
    ws["A1"] = "Расчёт начальной (максимальной) цены договора методом сопоставимых рыночных цен"
    ws["A1"].font = Font(bold=True, size=13)
    ws["A2"] = f"ТЗ: {run.spec.title}. Сформировано {run.created_at:%d.%m.%Y %H:%M}, расчёт {run.id}"
    n_max = max((len(r.calc.prices) for r in run.results), default=0)
    cols = ["№", "Наименование", "Ед.", "Кол-во"] + [f"Ц{i + 1}" for i in range(n_max)] + [
        "n", "Среднее", "σ", "V, %", "Цена за ед.", "Сумма", "Итог"]
    _head(ws, 4, cols)
    row = 5
    sums = []
    for r in run.results:
        prices = [price_of(r, o) for o in used(r)]
        c = r.calc
        stats = [c.n, c.mean, c.std, c.cv, c.unit_price, c.total]
        if prices and n_max:
            rng = f"E{row}:{get_column_letter(4 + n_max)}{row}"
            mean, std = f"AVERAGE({rng})", f"STDEV({rng})"
            stats[:4] = [f"=COUNT({rng})", f"=ROUND({mean},2)",
                         f"=IF(COUNT({rng})>1,ROUND({std},2),0)" if len(prices) > 1 else 0,
                         f"=IF(COUNT({rng})>1,ROUND({std}/{mean}*100,2),0)" if len(prices) > 1 else 0]
            if c.status == "ok":
                p_col = get_column_letter(4 + n_max + 5)
                stats[4] = f"=ROUND({mean},2)"
                stats[5] = f"=ROUND({p_col}{row}*D{row},2)"
                sums.append(f"{get_column_letter(4 + n_max + 6)}{row}")
        vals = [r.item.pos, r.item.name, r.item.unit, r.item.qty] + prices + [None] * (n_max - len(prices)) + stats + [
            STATUS.get(c.status, c.status)]
        _row(ws, row, vals, OK if c.status == "ok" else BAD)
        for k in range(2, 6):
            ws.cell(row=row, column=4 + n_max + k).number_format = '#,##0.00'
        row += 1
    ws.cell(row=row + 1, column=2, value="НМЦД, итого").font = Font(bold=True)
    total = run.total
    if total is not None and sums and len(sums) == len(run.results):
        total = f"=SUM({','.join(sums)})" if len(sums) > 1 else f"={sums[0]}"
    tot = ws.cell(row=row + 1, column=len(cols) - 1, value=total)
    tot.font = Font(bold=True)
    tot.number_format = '#,##0.00'
    if run.total is None:
        ws.cell(row=row + 1, column=len(cols), value="не все позиции обоснованы")
    _widths(ws, [5, 46, 7, 8] + [13] * n_max + [5, 13, 12, 8, 14, 15, 26])
    ws.freeze_panes = "C5"

    src = wb.create_sheet("Источники цен")
    _head(src, 1, ["Поз.", "Цена в расчёте", "Источник", "Документ / ссылка", "Снимок страницы (SHA-256)", "Приведение", "РНП ЕИС"])
    row = 2
    for r in run.results:
        for o in used(r):
            d = next(x for x in r.decisions if x.offer_id == o.id)
            _row(src, row, [r.item.pos, d.unit_price, source_line(o), o.url,
                            (o.evidence.sha256 or "")[:16], d.normalized_note or "без пересчёта", rnp.label(o)])
            src.cell(row=row, column=4).hyperlink = o.url
            row += 1
    _widths(src, [6, 14, 60, 50, 20, 50, 16])

    for r in run.results:
        sh = wb.create_sheet(f"Поз. {r.item.pos}")
        sh["A1"] = f"{r.item.pos}. {r.item.name}"
        sh["A1"].font = Font(bold=True)
        sh["A2"] = r.calc.message
        reqs = [q for q in r.item.requirements if q.essential]
        _head(sh, 4, ["В расчёте", "Источник", "Наименование", "Цена", "Цена за ед. ТЗ", "Причина"] + [q.name[:40] for q in reqs] + ["Ссылка"])
        row = 5
        for o, d in zip(r.offers, r.decisions):
            checks = {c.key + c.name: c for c in d.checks}
            cells = []
            for q in reqs:
                c = checks.get(q.key + q.name)
                cells.append((f"{CHECK.get(c.status, c.status)}: {c.found or '—'}"
                              + (f" (ИИ, цитата: «{c.quote}»)" if c.via == "ai" else "")) if c else "—")
            _row(sh, row, ["да" if d.included else "нет", o.source_title, o.title, o.price, d.unit_price,
                           d.reasons[0][1] if d.reasons else ""] + cells + [o.url], OK if d.included else None)
            row += 1
        _widths(sh, [9, 22, 50, 12, 14, 50] + [24] * len(reqs) + [40])
        sh.freeze_panes = "D5"

    log = wb.create_sheet("Журнал")
    _head(log, 1, ["Время", "Этап", "Событие"])
    for i, ev in enumerate(run.trace, 2):
        _row(log, i, [ev.at.strftime("%H:%M:%S"), ev.stage, ev.message])
    _widths(log, [10, 10, 100])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


# ---------- DOCX ----------

def _font(doc: Document) -> None:
    st = doc.styles["Normal"]
    st.font.name = "Times New Roman"
    st.font.size = Pt(11)
    st.element.rPr.rFonts.set(qn("w:eastAsia"), "Times New Roman")
    st.paragraph_format.space_after = Pt(4)


def _cell(cell, text: str, bold: bool = False, size: int = 9, align=None) -> None:
    cell.text = ""
    p = cell.paragraphs[0]
    run = p.add_run(text)
    run.bold = bold
    run.font.size = Pt(size)
    if align:
        p.alignment = align


def _shade(cell, hex_color: str) -> None:
    tc = cell._tc.get_or_add_tcPr()
    shd = OxmlElement("w:shd")
    shd.set(qn("w:val"), "clear")
    shd.set(qn("w:fill"), hex_color)
    tc.append(shd)


def docx(run: Run) -> bytes:
    doc = Document()
    _font(doc)
    sec = doc.sections[0]
    sec.orientation = WD_ORIENT.LANDSCAPE
    sec.page_width, sec.page_height = sec.page_height, sec.page_width
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(sec, side, Cm(1.8))

    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    t = h.add_run("Обоснование начальной (максимальной) цены договора")
    t.bold = True
    t.font.size = Pt(14)
    p = doc.add_paragraph()
    p.alignment = WD_ALIGN_PARAGRAPH.CENTER
    p.add_run(f"Предмет закупки: {run.spec.title}").italic = True
    req = [x for x in (f"№ {run.spec.notice}" if run.spec.notice else "", run.spec.method or "") if x]
    if req:
        p2 = doc.add_paragraph()
        p2.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p2.add_run("Закупка " + ", ".join(req)).italic = True

    doc.add_paragraph(
        legal_basis(run.settings.get("org") or org()) + " "
        "Использована общедоступная ценовая информация: исполненные контракты из реестра контрактов ЕИС "
        "(напрямую или через открытое зеркало реестра «Госзатраты», clearspending.ru) за последние три года, "
        "контракты с начисленными неустойками исключены; при недостатке контрактов — публичные оферты поставщиков. "
        "От одного поставщика в расчёт берётся одна цена. "
        "Цены контрактов прошлых лет приведены к текущему уровню индексом потребительских цен Росстата; "
        "у каждой такой цены указаны исходная цена и коэффициент. "
        "Совокупность цен признаётся однородной при коэффициенте вариации V ≤ 33 %; "
        "цена единицы — средняя арифметическая однородной совокупности.")

    if run.settings.get("ai"):
        doc.add_paragraph(
            "Характеристики, которые правила не нашли в карточке товара, дочитаны языковой моделью "
            f"{run.settings['ai']['model']}, развёрнутой в контуре организации. Значение модели принимается, только "
            "если приведённая ею цитата дословно есть в копии страницы-источника; вывод о сопоставимости "
            "делают правила, как и без модели.")
    pol = run.settings.get("policy", "strict")
    doc.add_paragraph(
        "Источники: только из утверждённого заказчиком перечня (доверие 100 %)." if pol == "strict" else
        f"Источники: утверждённый перечень и иные открытые источники; у каждой цены рассчитано доверие, "
        f"цены с доверием ниже {run.settings.get('trust_min', 60)} % в расчёт не включены.")

    widths = [Cm(0.9), Cm(6.2), Cm(1.2), Cm(1.4), Cm(9.0), Cm(1.4), Cm(2.6), Cm(2.6)]
    tb = doc.add_table(rows=1, cols=8)
    tb.style = "Table Grid"
    tb.alignment = WD_TABLE_ALIGNMENT.CENTER
    tb.autofit = False
    for c, v in zip(tb.rows[0].cells, ["№", "Наименование", "Ед.", "Кол-во", "Цены, использованные в расчёте",
                                        "V, %", "Цена за ед.", "Сумма"]):
        _cell(c, v, bold=True)
        _shade(c, "EDE8DC")
    for r in run.results:
        cells = tb.add_row().cells
        prices = "; ".join(money(price_of(r, o)) for o in used(r)) or "—"
        c = r.calc
        vals = [str(r.item.pos), r.item.name, r.item.unit, f"{r.item.qty:g}", prices,
                f"{c.cv:.2f}".replace(".", ",") if c.cv is not None else "—",
                money(c.unit_price) if c.status == "ok" else STATUS.get(c.status, c.status),
                money(c.total) if c.status == "ok" else "—"]
        for cell, v in zip(cells, vals):
            _cell(cell, v)
    for row in tb.rows:
        for cell, w in zip(row.cells, widths):
            cell.width = w

    doc.add_paragraph()
    total = doc.add_paragraph()
    if run.total is not None:
        total.add_run("Начальная (максимальная) цена договора: ").bold = True
        total.add_run(money(run.total) + ", включая НДС.")
        terms = doc.add_paragraph()
        terms.add_run("Цены контрактов указаны так, как они зафиксированы в реестре, с НДС и доставкой, если они "
                      "входили в цену контракта; цены каталогов — публичные цены продавца на дату копии страницы "
                      "(как правило, с НДС), без доставки. Условия поставки: " + (run.spec.delivery_term or "по техническому заданию")
                      + (f", {run.spec.delivery_place}" if run.spec.delivery_place else "") + ".").italic = True
    else:
        ok = [r for r in run.results if r.calc.status == "ok"]
        total.add_run("НМЦД не может быть определена полностью: ").bold = True
        total.add_run(f"обоснованы {len(ok)} из {plural(len(run.results), 'позиции', 'позиций', 'позиций')} на сумму "
                      f"{money(sum(r.calc.total or 0 for r in ok))}. По остальным позициям требуется запрос "
                      "коммерческих предложений не менее чем у пяти поставщиков (Рекомендации, приказ № 567).")
        rest = [str(r.item.pos) for r in run.results if r.calc.status != "ok"]
        f = doc.add_paragraph()
        f.add_run(f"НМЦД = {money(sum(r.calc.total or 0 for r in ok))} (обоснованные позиции) + сумма по "
                  f"коммерческим предложениям для {'позиции' if len(rest) == 1 else 'позиций'} № {', '.join(rest)}.").italic = True

    doc.add_paragraph().add_run("Источники ценовой информации").bold = True
    for r in run.results:
        para = doc.add_paragraph()
        para.add_run(f"Позиция {r.item.pos}. {r.item.name}. ").bold = True
        para.add_run(r.calc.message)
        nameless = [o for o in used(r) if o.kind == "contract" and not o.supplier]
        if nameless:
            para.add_run(f" У {plural(len(nameless), 'контракта', 'контрактов', 'контрактов')} поставщик в источнике "
                         "не указан: это разные контракты (номера ниже), но различие поставщиков не подтверждено.")
        for i, o in enumerate(used(r), 1):
            d = next(x for x in r.decisions if x.offer_id == o.id)
            line = doc.add_paragraph()
            line.paragraph_format.left_indent = Cm(0.8)
            line.add_run(f"Ц{i} = {money(d.unit_price)}{price_note(o, d.unit_price, d.factor)} — {source_line(o)}"
                         f"{source_price(o, d)}. ")
            extra = [x[1] for x in d.reasons if x[0] in ("modification", "equivalent")]
            if extra:
                line.add_run(extra[0][0].upper() + extra[0][1:] + ". ")
            by_ai = [c for c in d.checks if c.via == "ai"]
            if by_ai:
                line.add_run("Найдено моделью по цитате: " + "; ".join(f"{c.name} — «{c.quote}»" for c in by_ai) + ". ").italic = True
            if d.normalized_note:
                line.add_run(f"Приведение: {d.normalized_note}. ").italic = True
            rn = (o.meta or {}).get("rnp")
            if rn and rn["status"] == "clean":
                line.add_run(f"В реестре недобросовестных поставщиков ЕИС не значится (проверено "
                             f"{rn['checked_at'][8:10]}.{rn['checked_at'][5:7]}.{rn['checked_at'][:4]}). ")
            elif rn and rn["status"] == "unknown":
                line.add_run("Проверить поставщика по РНП ЕИС не удалось, проверьте вручную. ")
            if pol == "open" and d.trust is not None:
                line.add_run(f"Доверие {d.trust} %. ")
            if d.override:
                line.add_run(f"Решение пользователя{' ' + d.override['by'] if d.override.get('by') else ''}: "
                             f"{d.override['comment']}. ").italic = True
            link = line.add_run(o.url)
            link.font.size = Pt(8)
            link.font.color.rgb = RGBColor(0x55, 0x55, 0x55)
        gone = [(o, d) for o in r.offers for d in r.decisions if d.offer_id == o.id and d.override
                and d.override["action"] == "exclude"]
        for o, d in gone:
            line = doc.add_paragraph()
            line.paragraph_format.left_indent = Cm(0.8)
            line.add_run(f"Исключено пользователем: «{o.title[:90]}» — {d.override['comment']}.").italic = True
        if r.estimate:
            e = r.estimate
            line = doc.add_paragraph()
            line.paragraph_format.left_indent = Cm(0.8)
            line.add_run("Справочно, оценка по аналогам: ").italic = True
            line.add_run(f"{money(e['value'])} за единицу; {e['method']}"
                         + (f"; отклонение НМЦД {e['deviation_pct']:+.1f} %".replace(".", ",") if "deviation_pct" in e else "")
                         + ". В расчёт НМЦД не входит.")
        for st in r.calc.steps:
            s = doc.add_paragraph()
            s.paragraph_format.left_indent = Cm(0.8)
            s.add_run(f"{st.title}: ").italic = True
            s.add_run(st.detail)

    guessed = any("по годовому индексу" in (d.normalized_note or "") for r in run.results for d in r.decisions if d.included)
    if guessed:
        doc.add_paragraph().add_run(
            "Внимание: за месяцы, по которым Росстат ещё не опубликовал индекс, цены проиндексированы по годовому "
            "индексу из настроек организации. После публикации индекса расчёт стоит повторить.").italic = True
    doc.add_paragraph()
    doc.add_paragraph(f"Расчёт выполнен {run.created_at:%d.%m.%Y в %H:%M}, идентификатор {run.id}. "
                      "Копии страниц-источников сохранены с контрольными суммами SHA-256 и приложены к журналу расчёта.")
    o = run.settings.get("org") or org()
    who = o.get("executor") or "____________________"
    role = o.get("position") or "Ответственный за расчёт"
    sign = doc.add_paragraph()
    sign.paragraph_format.space_before = Pt(18)
    sign.add_run(f"{role}  ____________________  /{who}/      «___» ____________ 20___ г.")

    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def rfq(run: Run, org: dict | None = None) -> bytes:
    """Запрос ценовой информации поставщикам (п. 3.7.1 Рекомендаций, приказ № 567): описание товара с
    характеристиками ТЗ, количество, условия поставки и список адресатов — поставщики, найденные в расчёте."""
    org = org or {}
    doc = Document()
    _font(doc)
    pending = [r for r in run.results if r.calc.status != "ok"] or run.results
    h = doc.add_paragraph()
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    t = h.add_run("Запрос ценовой информации")
    t.bold = True
    t.font.size = Pt(14)
    who = org.get("full") or org.get("name") or "Заказчик"
    doc.add_paragraph(
        f"{who} в целях определения начальной (максимальной) цены договора просит в срок до «___» ________ 20___ г. "
        "представить коммерческое предложение на поставку товара, указанного ниже. Цена указывается за единицу "
        "с учётом НДС (или с отметкой «НДС не облагается»), доставки, разгрузки и всех иных расходов поставщика. "
        "Просим указать срок действия цены, реквизиты (ИНН) и номер с датой предложения.")
    doc.add_paragraph("Настоящий запрос не является извещением об осуществлении закупки и не влечёт обязательств "
                      "заказчика заключить договор.")
    if run.spec.delivery_place or run.spec.delivery_term:
        doc.add_paragraph(f"Место поставки: {run.spec.delivery_place or '—'}. Срок поставки: {run.spec.delivery_term or '—'}.")
    for r in pending:
        p = doc.add_paragraph()
        p.add_run(f"{r.item.pos}. {r.item.name}").bold = True
        p.add_run(f" — {r.item.qty:g} {r.item.unit}" + (f", ОКПД2 {r.item.okpd2}" if r.item.okpd2 else ""))
        reqs = [x for x in r.item.requirements if x.essential]
        if reqs:
            tb = doc.add_table(rows=1, cols=2)
            tb.style = "Table Grid"
            for c, v in zip(tb.rows[0].cells, ["Характеристика", "Требуемое значение"]):
                _cell(c, v, bold=True)
            for x in reqs:
                cells = tb.add_row().cells
                _cell(cells[0], x.name)
                _cell(cells[1], x.raw or x.constraint.describe())
        doc.add_paragraph("Цена за единицу, руб. с НДС: ____________   Сумма, руб. с НДС: ____________")
    doc.add_paragraph().add_run("Адресаты запроса (поставщики, найденные при расчёте)").bold = True
    seen: set[str] = set()
    for r in pending:
        for o in sorted(r.offers, key=lambda x: x.kind != "contract"):
            name = (o.supplier or "").strip()
            if not name or name in seen or o.source == "manual":
                continue
            seen.add(name)
            doc.add_paragraph(f"{name} — {KIND.get(o.kind, o.kind)}, {o.url}", style="List Number")
            if len(seen) >= 12:
                break
    if len(seen) < 5:
        doc.add_paragraph("Найдено меньше пяти поставщиков: дополните список, запрос направляется не менее чем пяти "
                          "поставщикам, имеющим опыт поставки такого товара.")
    sign = doc.add_paragraph()
    sign.paragraph_format.space_before = Pt(18)
    sign.add_run(f"Ответ направить: {org['contact']}" if org.get("contact")
                 else "Контактное лицо  ____________________   тел. ____________   e-mail ____________")
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


def json_trace(run: Run) -> bytes:
    return run.model_dump_json(indent=2).encode("utf-8")


def filename(run: Run, ext: str) -> str:
    stamp = datetime.now().strftime("%Y%m%d-%H%M")
    return f"nmck-{run.id}-{stamp}.{ext}"
