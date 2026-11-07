"""Смена раскладки выделенного текста: механическое преобразование QWERTY ↔ ЙЦУКЕН."""
from __future__ import annotations

import pytest

from rupor import layout


@pytest.mark.parametrize("src, out", [
    ("ghbdtn", "привет"),
    ("Ghbdtn? vbh/", "Привет, мир."),
    ("'nj ntcn", "это тест"),
    ("gfhjkm123", "пароль123"),
    ("`~", "ёЁ"),
    ("[]{};':\",.<>", "хъХЪжэЖЭбюБЮ"),
    ("2@3#4$", "2\"3№4;"),   # «№» есть только в русской раскладке
    ("@#$^&|", "\"№;:?/"),
])
def test_en_to_ru(src: str, out: str) -> None:
    assert layout.convert(src) == out
    assert layout.convert(out) == src   # повторное нажатие возвращает исходное


@pytest.mark.parametrize("src, out", [
    ("руддщ", "hello"),
    ("зфыыцщкв", "password"),
    ("Зфыыцщкв!2024", "Password!2024"),
    ("ЫУДУСЕ * АКЩЬ ефиду;", "SELECT * FROM table$"),
])
def test_ru_to_en(src: str, out: str) -> None:
    assert layout.convert(src) == out
    assert layout.convert(out) == src


def test_whitespace_and_lines_kept() -> None:
    assert layout.convert("ghbdtn\r\n\tvbh  ") == "привет\r\n\tмир  "


def test_direction_per_word() -> None:
    # часть фразы набрана не в той раскладке: каждое слово — в свою сторону
    assert layout.convert("Привет ghbdtn") == "Ghbdtn привет"


def test_signs_follow_the_whole_text() -> None:
    # слово без букв идёт за направлением всего текста
    assert layout.convert("ghbdtn ,") == "привет б"
    assert layout.convert("привет ?") == "ghbdtn &"


def test_ambiguous_signs_go_to_russian() -> None:
    # «;',.» есть в обеих раскладках: без букв считаем, что набрано на английских клавишах
    assert layout.convert(";',.") == "жэбю"


def test_nothing_to_convert() -> None:
    assert layout.convert("123 - 456 = !") == "123 - 456 = !"
    assert layout.convert("") == ""


def test_belarusian_letters() -> None:
    assert layout.convert("прывітанне") == "ghsdsnfyyt"
    assert layout.convert("ў") == "o"


def test_forced_direction() -> None:
    assert layout.convert("ghbdtn", layout.RU_TO_EN_DIR) == "ghbdtn"
    assert layout.convert("Привет ghbdtn", layout.EN_TO_RU_DIR) == "Привет привет"


def test_tables_are_inverse() -> None:
    assert len(layout.EN_TO_RU) == len(set(layout.EN_TO_RU.values()))
    for en, ru in layout.EN_TO_RU.items():
        assert layout.RU_TO_EN[ru] == en
