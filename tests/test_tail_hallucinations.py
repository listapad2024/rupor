"""Служебные фразы субтитров («Продолжение следует...») в конце диктовки убираются."""
from __future__ import annotations

import pytest

from rupor.textproc import strip_tail_hallucinations


@pytest.mark.parametrize("src,expected", [
    ("Надо обновить дашборд. Продолжение следует...", "Надо обновить дашборд."),
    ("Надо обновить дашборд Продолжение следует", "Надо обновить дашборд"),
    ("Вот так. ПРОДОЛЖЕНИЕ СЛЕДУЕТ!", "Вот так."),
    ("Текст. Продолжение следует... Субтитры сделал DimaTorzok", "Текст."),
    ("Send the report. Thanks for watching!", "Send the report."),
    ("Продолжение следует...", ""),
])
def test_tail_phrase_is_removed(src: str, expected: str) -> None:
    assert strip_tail_hallucinations(src) == expected


@pytest.mark.parametrize("src", [
    "Сериал, продолжение следует за первым сезоном.",   # не в конце
    "Это непродолжение следует",                          # часть слова
    "Обычный текст без хвоста.",
    "",
])
def test_other_text_is_untouched(src: str) -> None:
    assert strip_tail_hallucinations(src) == src
