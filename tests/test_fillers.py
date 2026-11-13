"""Слова-паразиты (rupor.fillers): подсчёт по сырому тексту, список, хранение по дням,
пересчёт по истории, статистика для экрана «Речь», разбор ответа ИИ."""
from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rupor import ai, fillers
from rupor.storage import Storage

TODAY = date(2026, 11, 13)


@pytest.fixture
def st(tmp_path: Path) -> Iterator[Storage]:
    s = Storage(tmp_path / "rupor.db")
    yield s
    s.close()


def noon(d: date) -> float:
    return datetime.combine(d, dtime(12, 0)).timestamp()


def hist(st: Storage, raw: str, d: date, **kw: Any) -> None:
    item: dict[str, Any] = {"ts": noon(d), "app": "notepad.exe", "title": "", "raw": raw,
                            "text": raw, "duration_s": 2.0, "language": "ru",
                            "style": "normal", "ai": False, "latency_ms": 300,
                            "target": "window"}
    item.update(kw)
    st.add_history(item)


# ---------- подсчёт ----------

def test_count_phrases_longest_first_no_overlap() -> None:
    m = fillers.Matcher(["ну", "как бы", "в общем", "в общем-то", "вот"])
    found, words = m.count("Ну, короче, я как бы это… в общем-то, ну вот. В общем, всё.")
    assert found == {"ну": 2, "как бы": 1, "в общем-то": 1, "вот": 1, "в общем": 1}
    assert words == 13


def test_case_yo_and_punctuation_ignored() -> None:
    m = fillers.Matcher(["Ещё раз", "Типа"])
    found, _ = m.count("ЕЩЕ РАЗ, ТИПА! ещё раз типа?")
    assert found == {"еще раз": 2, "типа": 2}


@pytest.mark.parametrize("word,form", [
    ("э", "эээ"), ("Эээ", "эээ"), ("э-э-э", "эээ"), ("эм", "эээ"), ("эмм", "эээ"),
    ("мммм", "ммм"), ("ааа", "ааа"), ("аа", "ааа"),
    ("um", "um"), ("Ummm", "um"), ("uhh", "uh"), ("hmm", "hmm"),
])
def test_hesitations_collapse(word: str, form: str) -> None:
    assert fillers.tokens(word) == [form]


@pytest.mark.parametrize("word", ["а", "мм", "эхо", "мама", "ер"])
def test_ordinary_words_are_not_hesitations(word: str) -> None:
    assert fillers.tokens(word) == [word]


def test_clean_list_normalizes_and_dedups() -> None:
    assert fillers.clean_list(["  Как  БЫ, ", "как бы", "Э-э-э", "", 5, "эээ",
                               "a b c d e f g"]) == ["как бы", "эээ"]


def test_default_list_has_both_languages() -> None:
    d = fillers.default_list()
    assert {"ну", "короче", "типа", "как бы", "в общем", "эээ", "ммм"} <= set(d)
    assert {"um", "you know", "basically"} <= set(d)
    assert len(d) == len(set(d))


# ---------- хранение ----------

def test_record_accumulates_by_day(st: Storage) -> None:
    fillers.record(st, "ну короче ну", when=noon(TODAY))
    fillers.record(st, "ну и всё", when=noon(TODAY))
    fillers.record(st, "типа того", when=noon(TODAY - timedelta(days=1)))
    s = fillers.stats(st, 7, today=TODAY)
    assert s["words"] == 8 and s["dictations"] == 3 and s["fillers"] == 5
    assert s["per100"] == 62.5
    top = {t["phrase"]: t["count"] for t in s["top"]}
    assert top == {"ну": 3, "короче": 1, "типа": 1}
    assert s["top"][0]["phrase"] == "ну"
    assert len(s["daily"]) == 7
    assert s["daily"][-1] == {"date": TODAY.isoformat(), "words": 6, "fillers": 4,
                              "per100": 66.67}


def test_disabled_does_not_count(st: Storage) -> None:
    fillers.set_config(st, enabled=False)
    fillers.record(st, "ну ну ну", when=noon(TODAY))
    assert fillers.stats(st, 7, today=TODAY)["words"] == 0


def test_custom_list_is_used(st: Storage) -> None:
    out = fillers.set_config(st, phrases=["По факту", "ну"])
    assert out["phrases"] == ["по факту", "ну"]
    fillers.record(st, "по факту короче ну", when=noon(TODAY))
    top = {t["phrase"]: t["count"] for t in fillers.stats(st, 7, today=TODAY)["top"]}
    assert top == {"по факту": 1, "ну": 1}
    assert fillers.restore_defaults(st)["phrases"] == fillers.default_list()


def test_broken_list_in_db_falls_back_to_defaults(st: Storage) -> None:
    st.kv_set(fillers.KV_LIST, "{not json")
    assert fillers.get_list(st) == fillers.default_list()


def test_prev_period_for_trend(st: Storage) -> None:
    fillers.record(st, "ну ну раз два", when=noon(TODAY - timedelta(days=10)))  # 50 на 100
    fillers.record(st, "ну раз два три", when=noon(TODAY))                        # 25 на 100
    s = fillers.stats(st, 7, today=TODAY)
    assert s["per100"] == 25.0 and s["prev_per100"] == 50.0
    assert s["top"][0]["prev_per100"] == 50.0
    assert fillers.stats(st, 0, today=TODAY)["prev_per100"] is None


def test_bad_period(st: Storage) -> None:
    with pytest.raises(ValueError):
        fillers.stats(st, 5, today=TODAY)


# ---------- пересчёт по истории ----------

def test_first_stats_backfills_from_history(st: Storage) -> None:
    hist(st, "ну короче да", TODAY - timedelta(days=2))
    hist(st, "ну короче да", TODAY - timedelta(days=2))     # повторное распознавание
    hist(st, "типа тест микрофона", TODAY, target="test")   # проверка микрофона
    hist(st, "вот так вот", TODAY)
    s = fillers.stats(st, 7, today=TODAY)
    assert s["dictations"] == 2
    assert {t["phrase"]: t["count"] for t in s["top"]} == {"ну": 1, "короче": 1, "вот": 2}
    # второй раз не пересчитывает — живые записи добавляются к пересчитанному
    fillers.record(st, "ну", when=noon(TODAY))
    assert fillers.stats(st, 7, today=TODAY)["dictations"] == 3


def test_rebuild_keeps_days_without_history(st: Storage) -> None:
    old = TODAY - timedelta(days=40)
    fillers.record(st, "ну ну", when=noon(old))            # история этого дня уже удалена
    fillers.record(st, "ну", when=noon(TODAY))
    hist(st, "ну", TODAY)
    fillers.set_config(st, phrases=["ну", "короче"])
    assert fillers.rebuild(st) == {"days": 1, "dictations": 1}
    s = fillers.stats(st, 0, today=TODAY)
    assert s["fillers"] == 3 and s["dictations"] == 2      # день old — как был
    assert s["since"] == old.isoformat()


def test_reset(st: Storage) -> None:
    hist(st, "ну", TODAY)
    fillers.record(st, "ну", when=noon(TODAY))
    fillers.reset(st)
    s = fillers.stats(st, 30, today=TODAY)                 # без повторного пересчёта
    assert s["words"] == 0 and s["top"] == []


def test_history_retention_does_not_touch_filler_stats(st: Storage) -> None:
    fillers.record(st, "ну короче", when=noon(TODAY))
    st.clear_history("all")
    assert fillers.stats(st, 7, today=TODAY)["fillers"] == 2


# ---------- ИИ ----------

def test_parse_ai_answer() -> None:
    out = fillers._parse_ai('Вот: {"items": [{"phrase": "По ходу дела", "count": 3, '
                            '"example": "по ходу дела мы"}, {"x": 1}, {"phrase": "так"}]}')
    assert out == [{"phrase": "По ходу дела", "count": 3, "example": "по ходу дела мы"},
                   {"phrase": "так", "count": 0, "example": ""}]
    with pytest.raises(ai.AIError):
        fillers._parse_ai("нет json")


class _Eng:
    def __init__(self, st: Storage, answer: str) -> None:
        self.storage = st
        self.s = type("S", (), {"ai_provider": "keys", "ai_timeout_s": 5})()
        self.answer = answer
        self.calls: list[str] = []

    def _ai_call(self, kind: str, app: str, fn: Any) -> ai.AIResult:
        self.calls.append(kind)
        return ai.AIResult(text=self.answer, ms=1.0, cost_usd=0.0, provider="x", model="y")


def test_ai_suggest_filters_known_and_invented(st: Storage) -> None:
    hist(st, "по ходу дела мы ну по ходу дела", TODAY)
    answer = json.dumps({"items": [
        {"phrase": "по ходу дела", "count": 9, "example": "по ходу дела мы"},
        {"phrase": "ну", "count": 1},                        # уже в списке
        {"phrase": "как говорится", "count": 4},             # в текстах нет
    ]}, ensure_ascii=False)
    eng = _Eng(st, answer)
    out = fillers.ai_suggest(eng, "ru")  # type: ignore[arg-type]
    assert out["items"] == [{"phrase": "по ходу дела", "count": 2, "example": "по ходу дела мы"}]
    assert out["sampled"] == 1 and eng.calls == ["cleanup"]


def test_ai_suggest_needs_history(st: Storage) -> None:
    with pytest.raises(ValueError):
        fillers.ai_suggest(_Eng(st, "{}"), "ru")  # type: ignore[arg-type]
