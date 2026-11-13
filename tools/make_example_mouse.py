"""Пример ТЗ новой категории в формате МФТИ: собирается из шаблона ТЗ на 3D-принтер заменой текстов,
чтобы разметка таблиц была та же, что у заказчика. Код Выверки под категорию не меняется."""

import sys
from pathlib import Path

from docx import Document

ROOT = Path(__file__).resolve().parents[1]

SPECS = [
    "Тип: проводная оптическая",
    "Модель: Logitech B100 или эквивалент",
    "Интерфейс подключения: USB",
    "Количество кнопок: 3 (две кнопки и колесо прокрутки)",
    "Разрешение датчика: не менее 800 dpi",
    "Длина кабеля: не менее 1,8 м",
    "Цвет: черный",
    "Совместимость: Windows, macOS, Linux",
]


def put(cell, lines: list[str]) -> None:
    paras = cell.paragraphs
    for p in paras[len(lines):]:
        p._element.getparent().remove(p._element)
    for p, text in zip(cell.paragraphs, lines):
        for r in p.runs[1:]:
            r._element.getparent().remove(r._element)
        if p.runs:
            p.runs[0].text = text
        else:
            p.add_run(text)
    for text in lines[len(paras):]:
        cell.add_paragraph(text)


def main() -> int:
    doc = Document(ROOT / "examples" / "tz_3d_printer.docx")
    for p in doc.paragraphs:
        if p.text.strip() == "на поставку 3D принтера":
            put_par(p, "на поставку компьютерных мышей")
    goods, terms, tech, _ = doc.tables
    row = goods.rows[2].cells
    put(row[1], ["Мышь компьютерная Logitech B100"])
    put(row[2], ["шт."])
    put(row[3], ["30"])
    put(row[4], ["26.20.16.170"])
    t = terms.rows[2].cells
    put(t[2], ["26.20.16.170"])
    put(t[3], ["Мышь компьютерная"])
    put(t[4], ["В течение 10 (десяти) рабочих дней с даты заключения Договора"])
    put(tech.rows[2].cells[1], ["Мышь компьютерная Logitech B100"])
    put(tech.rows[2].cells[2], SPECS)
    out = ROOT / "examples" / "tz_mouse.docx"
    doc.save(out)
    print(out, file=sys.stderr)
    return 0


def put_par(p, text: str) -> None:
    for r in p.runs[1:]:
        r._element.getparent().remove(r._element)
    p.runs[0].text = text


if __name__ == "__main__":
    sys.exit(main())
