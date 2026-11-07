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


# ---------- длинная диктовка (07.11.2026: 153 с речи пропали как «зацикленный повтор») ----------

LONG = (
    "Я нашёл ещё одну серьёзную проблему в Рупоре. Я сейчас диктовал длинную запись, и она "
    "просто исчезла. Предлагаю исправить конвейер так, чтобы последние тридцать голосовых "
    "сохранялись на диске, но место не тратилось без меры. Даже если записи будут длинными, "
    "нужен разумный лимит памяти. Кроме того, хочется видеть, почему запись пропала, и уметь "
    "распознать её заново, не надиктовывая всё с начала. Проверь журналы ядра, найди место, "
    "где текст выбрасывается, и поправь фильтр так, чтобы он не срабатывал на обычной речи. "
    "Потом оформи коммиты в ветке и открой запрос на слияние, чтобы я мог посмотреть правки. "
    "Параллельно идут задачи про несколько ключей и смену раскладки, конфликтов быть не должно. "
)
MORE = (
    "Вчера вечером мы созвонились с командой и обсудили план на следующую неделю: сначала "
    "сводим факт с планом по комиссиям мерчантов, затем готовим техническое задание для "
    "бэкенда, а в пятницу показываем дашборд в Метабейсе руководителю. Отдельно нужно "
    "проверить выгрузку из Кликхауса за октябрь, потому что там расходятся суммы возвратов, "
    "и понять, откуда взялись дубли в таблице транзакций. Если времени хватит, посмотрим ещё "
    "отчёт по конверсии и договоримся, кто отвечает за ночные проверки. Напомни мне в "
    "понедельник утром написать коллегам из поддержки, они просили прислать список вопросов "
    "заранее, чтобы подготовить ответы до встречи. И не забудь, что в среду у нас созвон с "
    "партнёрами из Казахстана, к нему нужна короткая презентация на пять слайдов с цифрами."
)


def test_long_normal_speech_is_not_repetitive() -> None:
    """Обычная речь на 2,5 минуты: окна по REPEAT_WINDOW символов — как у Whisper сегменты."""
    text = LONG + MORE
    assert len(text) > 1400
    assert garbage_reason(text) is None


def test_whole_text_ratio_grows_with_length() -> None:
    """Почему окна: русский UTF-8 сжимается тем сильнее, чем длиннее текст, без всяких
    повторов — у короткого куска сжимаемость заметно ниже, чем у всего текста."""
    text = LONG + MORE
    assert asr._ratio(text[:300]) < asr._ratio(text) - 0.3


def test_loop_inside_long_text_is_still_caught() -> None:
    text = LONG + " и тогда" * 80 + " " + LONG
    assert garbage_reason(text) == "зацикленный повтор"


def test_long_text_with_stray_glyph_is_salvaged() -> None:
    """Оба прохода «мусор» из-за одного иероглифа посреди длинной речи — текст спасается."""
    first = LONG[:300] + " 隙 " + LONG[300:]
    second = LONG[:500] + "測" + LONG[500:]
    r, _ = _rec((second, [(0.0, -0.4)]))
    out = r._second_opinion(AUDIO, "ru", first, [(0.0, -0.4)])
    assert out and not asr._foreign(out) and "серьёзную проблему" in out
    assert garbage_reason(out) is None


@pytest.mark.parametrize("junk", ["так, 隙, 隙 隅,隙,隅 隅", "Заппу, зап測, зап測,測,測", "隙 隅 隙"])
def test_salvage_does_not_resurrect_short_junk(junk: str) -> None:
    assert asr.salvage(junk) == ""
