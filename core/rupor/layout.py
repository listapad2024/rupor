"""Смена раскладки текста, набранного не в той раскладке: «ghbdtn» ↔ «привет».

Чисто механическое преобразование по положению клавиш: английская раскладка США (QWERTY)
↔ русская ЙЦУКЕН (стандартная раскладка Windows «Русская»). Без ИИ и без словарей.

Направление определяется по каждому слову отдельно: в слове больше кириллицы — оно
переводится в латиницу, больше латиницы — в кириллицу (знаки, которые есть только в одной
раскладке — «№» или «@», — считаются как буквы). Слова без таких символов (цифры, «,.;»)
следуют за направлением всего текста. Повторное преобразование возвращает исходный текст.
"""
from __future__ import annotations

import re

# Клавиши по рядам: без Shift и с Shift. Совпадающие символы (цифры, «-», «=», «\» …)
# не меняются и в таблицу не попадают.
_EN = ("`qwertyuiop[]asdfghjkl;'zxcvbnm,./"
       "~@#$^&QWERTYUIOP{}|ASDFGHJKL:\"ZXCVBNM<>?")
_RU = ("ёйцукенгшщзхъфывапролджэячсмитьбю."
       "Ё\"№;:?ЙЦУКЕНГШЩЗХЪ/ФЫВАПРОЛДЖЭЯЧСМИТЬБЮ,")
assert len(_EN) == len(_RU)

EN_TO_RU: dict[str, str] = dict(zip(_EN, _RU))
RU_TO_EN: dict[str, str] = dict(zip(_RU, _EN))
# Белорусская и украинская раскладки: «і» и «ў» стоят на клавишах S и O. Обратно они
# не восстанавливаются (по клавише S русская раскладка даёт «ы»).
RU_TO_EN.update({"і": "s", "І": "S", "ў": "o", "Ў": "O"})

EN_TO_RU_DIR = "en>ru"
RU_TO_EN_DIR = "ru>en"

_WORDS = re.compile(r"(\s+)")
# Знаки, которые набираются только в одной из раскладок: тоже голос за направление
_RU_ONLY = frozenset("№")
_EN_ONLY = frozenset("`~@#$^&|[]{}<>")


def _is_cyr(ch: str) -> bool:
    return (ch in RU_TO_EN and ch.isalpha()) or ch in _RU_ONLY


def _is_lat(ch: str) -> bool:
    return "a" <= ch.lower() <= "z" or ch in _EN_ONLY


def _counts(text: str) -> tuple[int, int]:
    cyr = lat = 0
    for ch in text:
        if _is_cyr(ch):
            cyr += 1
        elif _is_lat(ch):
            lat += 1
    return cyr, lat


def _direction(text: str, default: str) -> str:
    cyr, lat = _counts(text)
    if cyr > lat:
        return RU_TO_EN_DIR
    if lat > cyr:
        return EN_TO_RU_DIR
    return default


def detect(text: str) -> str:
    """Направление для всего текста: RU_TO_EN_DIR, если кириллицы больше, иначе EN_TO_RU_DIR
    (текст без букв — например «;',.» — набран на клавишах русских букв)."""
    return _direction(text, EN_TO_RU_DIR)


def _map(word: str, direction: str) -> str:
    table = RU_TO_EN if direction == RU_TO_EN_DIR else EN_TO_RU
    return "".join(table.get(ch, ch) for ch in word)


def convert(text: str, direction: str | None = None) -> str:
    """Перевести текст в другую раскладку. direction — EN_TO_RU_DIR / RU_TO_EN_DIR для всего
    текста; None — автоопределение по каждому слову (пробелы и переводы строк не меняются)."""
    if direction is not None:
        return _map(text, direction)
    overall = detect(text)
    return "".join(part if not part or part.isspace() else _map(part, _direction(part, overall))
                   for part in _WORDS.split(text))
