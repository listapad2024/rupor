"""Короткое зацикливание Whisper посреди нормальной фразы: распознать ещё раз без подсказки.

Строки — настоящие ответы Whisper из истории пользователя (Claude, 08.11.2026)."""
from __future__ import annotations

from typing import Any

import numpy as np
import pytest

from rupor import asr
from rupor.asr import collapse_loops, garbage_reason, loop_words, looping

LOOPED = ("Затем вместо WS-кода добавь старый интерфейс, ну старую кнопку скриншота, типа, типа "
          "какого интерфа, какого интерфа, какого интерфа, интерфа, интерфа интерф")
CLEAN = ("Затем вместо WS кода добавь старую кнопку скриншота, типа как раньше была, которая "
         "открывает скриншот методами Windows.")
AUDIO = np.zeros(16000, np.float32)


def test_real_loop_is_not_caught_by_compression() -> None:
    # Поэтому и нужен отдельный признак: весь текст сжимается как обычный
    assert garbage_reason(LOOPED) is None
    assert looping(LOOPED)


@pytest.mark.parametrize("text", [
    "сделай так, сделай так, сделай так",
    "Проверь сервер Проверь сервер Проверь сервер.",
    "пора пора пора пора",
    "Ну и вот. Ну и вот. Ну и вот.",
])
def test_looping(text: str) -> None:
    assert looping(text)


@pytest.mark.parametrize("text", [
    "",
    CLEAN,
    "Нет, нет, нет, не так.",
    "да да да да да да да",                 # короткое слово — обычная речь
    "очень-очень хорошо",
    "Сделай так, сделай так, и всё.",       # два раза — бывает
    "типа, типа как раньше",
])
def test_not_looping(text: str) -> None:
    assert not looping(text)


def test_collapse_inhuman_repeats_only() -> None:
    assert collapse_loops("пиши тест, пиши тест, пиши тест, пиши тест, пиши тест.") == "пиши тест."
    assert collapse_loops("ладно ладно ладно ладно ладно ладно ладно, дальше") == "ладно, дальше"
    assert collapse_loops("ну ну ну ну ну ну ну дальше") == "ну ну ну ну ну ну ну дальше"
    # три раза человек сказать может — не трогаем
    keep = "какая интерфейс, какая интерфейс, какая интерфейс."
    assert collapse_loops(keep) == keep
    assert collapse_loops(CLEAN) == CLEAN


def test_loop_words() -> None:
    assert loop_words(CLEAN) == 0
    assert loop_words(LOOPED) >= 4


def _rec(second: tuple[str, list[tuple[float | None, float]]]) -> tuple[Any, list[Any]]:
    r: Any = asr.Recognizer.__new__(asr.Recognizer)
    calls: list[Any] = []

    def decode(audio: Any, lang: str, prompt: str | None, guess: bool) -> Any:
        calls.append((lang, prompt, guess))
        return second
    r._decode = decode
    return r, calls


def test_loop_redecoded_without_prompt() -> None:
    r, calls = _rec((CLEAN, [(0.0, -0.14)]))
    assert r._second_opinion(AUDIO, "ru", LOOPED, [(0.0, -0.3)]) == CLEAN
    assert calls == [("ru", None, False)]


def test_loop_both_passes_keeps_genuine_repeat() -> None:
    said = "Какая интерфейс, какая интерфейс, какая интерфейс. Проверьте."
    r, _ = _rec((said, [(0.0, -0.2)]))
    assert r._second_opinion(AUDIO, "ru", said, [(0.0, -0.2)]) == said


def test_loop_both_passes_collapses_inhuman() -> None:
    first = CLEAN + " Пиши тест" + ", пиши тест" * 4
    second = CLEAN + " Пиши тест" + ", пиши тест" * 3
    assert garbage_reason(first) is None and garbage_reason(second) is None
    r, _ = _rec((second, [(0.0, -0.2)]))
    assert r._second_opinion(AUDIO, "ru", first, [(0.0, -0.2)]) == CLEAN + " Пиши тест"
