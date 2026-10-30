"""Мусор распознавания: иероглифы, фразы субтитров, зацикливание, угадывание — не вставлять.

Строки — настоящие ответы Whisper из истории пользователя (VS Code, 01.10.2026)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from rupor import asr
from rupor.asr import garbage_reason, guessing, low_confidence


@pytest.mark.parametrize("text", [
    "так, 隙, 隙 隅,隙,隅 隅",
    "Заппу, зап測, зап測,測,測",
    "ДИНАМИЧНАЯ МУЗЫКА",
    "Динамичная музыка.",
    "Субтитры сделал DimaTorzok",
    "Thanks for watching!",
    "пора пора пора пора пора пора пора пора пора пора пора пора пора",
])
def test_garbage(text: str) -> None:
    assert garbage_reason(text) is not None


@pytest.mark.parametrize("text", [
    "",
    "Проверь статус работы по серверам.",
    "Сделаю нит в эту папку.",
    "Спасибо за внимание, коллеги.",
    "Открой ZennoBrowser и проверь прокси в GeoNode.",
    "Deploy it to staging, please.",
    "Включи музыку погромче.",
    "Нет, нет, нет, не так.",
])
def test_not_garbage(text: str) -> None:
    assert garbage_reason(text) is None


def test_confidence_signals() -> None:
    assert guessing([(0.0, -0.3), (0.8, -0.9)])          # перераспознан с температурой 0.8
    assert not guessing([(0.0, -0.3), (0.2, -0.5)])
    assert not guessing([(None, -0.3)])
    assert low_confidence([(0.0, -1.8)])
    assert not low_confidence([(0.0, -0.9)])


def _rec(second: tuple[str, list[tuple[float | None, float]]] | None) -> tuple[Any, list[Any]]:
    """Распознаватель без модели: второй проход (_decode) возвращает second."""
    r: Any = asr.Recognizer.__new__(asr.Recognizer)
    calls: list[Any] = []

    def decode(audio: Any, lang: str, prompt: str | None, guess: bool) -> Any:
        calls.append((lang, prompt, guess))
        assert second is not None, "повтора быть не должно"
        return second
    r._decode = decode
    return r, calls


AUDIO = np.zeros(16000, np.float32)


def test_clean_first_pass_is_not_repeated() -> None:
    r, calls = _rec(None)
    assert r._second_opinion(AUDIO, "ru", "Проверь серверы.", [(0.0, -0.4)]) == "Проверь серверы."
    assert calls == []


def test_garbage_is_retried_without_prompt_and_guessing() -> None:
    r, calls = _rec(("Запушь в мастер.", [(0.0, -0.6)]))
    assert r._second_opinion(AUDIO, "ru", "Заппу, зап測, зап測,測,測", [(1.0, -2.0)]) == \
        "Запушь в мастер."
    assert calls == [("ru", None, False)]


def test_garbage_twice_inserts_nothing() -> None:
    r, _ = _rec(("так, 隙, 隙 隅", [(0.0, -2.0)]))
    assert r._second_opinion(AUDIO, "ru", "隙 隅 隙", [(0.0, -2.0)]) == ""


def test_guessed_latin_junk_is_replaced_by_retry() -> None:
    """«jay, jes, jpuy…» — без чужих символов, но сегмент угадан с температурой 0.8."""
    junk = "Западка, м, запр,ij, п, jay, jes, jy, jp, jpouluh, jpac, jpuy, jpuy, jfli, jpuy,"
    r, _ = _rec(("Загрузи в папку.", [(0.0, -0.7)]))
    assert r._second_opinion(AUDIO, "ru", junk, [(0.8, -1.1)]) == "Загрузи в папку."


def test_low_confidence_keeps_the_more_confident() -> None:
    r, _ = _rec(("Второй вариант.", [(0.0, -0.8)]))
    assert r._second_opinion(AUDIO, "ru", "Первый вариант.", [(0.0, -1.7)]) == "Второй вариант."
    r, _ = _rec(("Второй вариант.", [(0.0, -1.9)]))
    assert r._second_opinion(AUDIO, "ru", "Первый вариант.", [(0.0, -1.7)]) == "Первый вариант."
    # мусорный повтор не заменяет просто неуверенный первый проход
    r, _ = _rec(("ДИНАМИЧНАЯ МУЗЫКА", [(0.0, -0.5)]))
    assert r._second_opinion(AUDIO, "ru", "Тихая речь.", [(0.0, -1.7)]) == "Тихая речь."


@pytest.mark.parametrize("text, expected", [
    ("Проверь тесты и собери релиз. Продолжение следует...",
     "Проверь тесты и собери релиз."),
    ("Проверь тесты, Продолжение следует…", "Проверь тесты"),
    ("Отправь отчёт. Субтитры сделал DimaTorzok", "Отправь отчёт."),
    ("Send the report. Thanks for watching!", "Send the report."),
    ("Отправь отчёт. Редактор субтитров А.Синецкая Корректор А.Егорова",
     "Отправь отчёт."),
])
def test_tail_hallucination_is_cut(text: str, expected: str) -> None:
    assert asr.strip_tail_hallucination(text) == expected


@pytest.mark.parametrize("text", [
    "Продолжение следует...",                  # весь текст — это забота garbage_reason
    "Напиши, что продолжение следует завтра",  # фраза внутри, не в конце
    "Сериал хороший и продолжение следует",    # без конца предложения перед ней
    "Обычная фраза без мусора.",
])
def test_tail_hallucination_keeps_text(text: str) -> None:
    assert asr.strip_tail_hallucination(text) == text
