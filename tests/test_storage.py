"""Тесты Storage: схема и миграции, история и поиск, статистика, словарь, сниппеты,
импорт/экспорт, черновик, конкурентный доступ из потоков."""
from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Any

import pytest

from rupor.storage import SCHEMA_VERSION, Storage

NOW = 1_790_000_000.0          # фиксированное «сейчас» для периодов
HOUR = 3600.0
DAY = 86400.0
TODAY = date(2026, 9, 26)


@pytest.fixture
def st(tmp_path: Path) -> Iterator[Storage]:
    s = Storage(tmp_path / "rupor.db")
    yield s
    s.close()


def noon(d: date) -> float:
    """Локальный полдень дня d (record_stats берёт локальную дату)."""
    return datetime.combine(d, dtime(12, 0)).timestamp()


def item(text: str, **kw: Any) -> dict[str, Any]:
    base: dict[str, Any] = {"ts": NOW, "app": "notepad.exe", "title": "Без имени", "raw": text,
                            "text": text, "duration_s": 2.5, "language": "ru",
                            "style": "normal", "ai": False, "latency_ms": 400,
                            "target": "window"}
    base.update(kw)
    return base


# ---------- схема ----------

def test_wal_and_schema_version(st: Storage, tmp_path: Path) -> None:
    con = sqlite3.connect(tmp_path / "rupor.db")
    try:
        assert con.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"history", "dictionary", "snippets", "stats_daily", "kv", "ai_usage", "vocab",
                "vocab_excluded"} <= tables
        idx = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='index'")}
        assert "history_ts" in idx
    finally:
        con.close()


def test_default_path_in_appdata(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    with Storage() as s:
        assert s.path == tmp_path / "Rupor" / "rupor.db"
        s.kv_set("x", "1")
    assert (tmp_path / "Rupor" / "rupor.db").exists()


def test_reopen_keeps_data(tmp_path: Path) -> None:
    p = tmp_path / "r.db"
    with Storage(p) as s:
        s.add_history(item("привет"))
        s.dict_upsert(None, "ClickHouse", ["клик хаус"])
        s.record_stats(5, 3.0)
    with Storage(p) as s:
        assert s.recovered is None
        assert s.list_history(None, 10, 0)[1] == 1
        assert s.dict_list()[0]["variants"] == ["клик хаус"]
        assert s.stats()["dictations_total"] == 1


def test_migration_from_empty_file(tmp_path: Path) -> None:
    p = tmp_path / "old.db"
    con = sqlite3.connect(p)
    con.execute("CREATE TABLE junk(x)")
    con.commit()
    con.close()
    with Storage(p) as s:
        s.add_history(item("текст"))
    con = sqlite3.connect(p)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert con.execute("SELECT count(*) FROM junk").fetchone()[0] == 0   # чужое не тронуто
    finally:
        con.close()


def test_newer_schema_opens_as_is(tmp_path: Path) -> None:
    p = tmp_path / "new.db"
    with Storage(p):
        pass
    con = sqlite3.connect(p)
    con.execute("PRAGMA user_version = 99")
    con.commit()
    con.close()
    with Storage(p) as s:
        s.kv_set("draft", "ok")
        assert s.kv_get("draft") == "ok"


def test_corrupt_file_is_quarantined(tmp_path: Path) -> None:
    p = tmp_path / "rupor.db"
    p.write_bytes(b"this is definitely not an sqlite database " * 200)
    with Storage(p) as s:
        assert s.add_history(item("после сбоя"))["id"] == 1
        recovered = s.recovered
    broken = list(tmp_path.glob("rupor.db.broken-*"))
    assert len(broken) == 1 and recovered == broken[0]
    assert broken[0].read_bytes().startswith(b"this is definitely")


# ---------- история ----------

def test_add_history_returns_full_item(st: Storage) -> None:
    it = st.add_history(item("Привет, ClickHouse!", raw="привет клик хаус", ai=True,
                             target="rdp_all", duration_s=3.25, latency_ms=512))
    assert it["id"] == 1
    assert it == {"id": 1, "ts": NOW, "app": "notepad.exe", "title": "Без имени",
                  "raw": "привет клик хаус", "text": "Привет, ClickHouse!", "duration_s": 3.25,
                  "words": 2, "language": "ru", "style": "normal", "ai": True,
                  "latency_ms": 512, "target": "rdp_all"}
    assert st.get_history(1) == it


def test_add_history_defaults_and_coercion(st: Storage) -> None:
    it = st.add_history({"text": "раз два три", "id": 999, "extra": "мусор"})
    assert it["id"] != 999
    assert it["raw"] == "раз два три"          # raw не передан — берётся text
    assert it["words"] == 3                     # words посчитан по text
    assert it["target"] == "window"
    assert it["ts"] > 1_700_000_000
    assert it["app"] == "" and it["ai"] is False
    it = st.add_history({"raw": "только сырой", "ts": "abc", "duration_s": "1,5",
                         "latency_ms": 12.7, "words": "7", "ai": 1})
    assert it["text"] == "только сырой"
    assert it["duration_s"] == 1.5 and it["latency_ms"] == 13 and it["words"] == 7
    assert it["ai"] is True
    it = st.add_history({"raw": None, "text": "только текст"})
    assert it["raw"] == "только текст"
    it = st.add_history({"text": "x", "duration_s": float("nan"), "latency_ms": -5,
                         "words": 0})
    assert it["duration_s"] == 0.0 and it["latency_ms"] == 0 and it["words"] == 0
    got = st.get_history(it["id"])
    assert got is not None and got["words"] == 0


def test_get_delete_history(st: Storage) -> None:
    a = st.add_history(item("первая"))
    b = st.add_history(item("вторая"))
    assert st.get_history(12345) is None
    st.delete_history(a["id"])
    st.delete_history(777)                      # несуществующая — без ошибки
    assert st.get_history(a["id"]) is None
    assert st.get_history(b["id"]) is not None


def test_list_history_order_and_paging(st: Storage) -> None:
    for i in range(25):
        st.add_history(item(f"запись {i}", ts=NOW + i))
    items, total = st.list_history(None, 10, 0)
    assert total == 25
    assert [x["text"] for x in items[:3]] == ["запись 24", "запись 23", "запись 22"]
    items, total = st.list_history(None, 10, 20)
    assert total == 25 and [x["text"] for x in items] == [f"запись {i}" for i in range(4, -1, -1)]
    items, total = st.list_history(None, 10, 100)
    assert items == [] and total == 25
    items, total = st.list_history(None, 0, 0)
    assert items == [] and total == 25
    items, total = st.list_history(None, 5, -3)
    assert len(items) == 5
    items, _ = st.list_history("", 100000, 0)
    assert len(items) == 25


def test_list_history_same_ts_newest_id_first(st: Storage) -> None:
    a = st.add_history(item("а", ts=NOW))
    b = st.add_history(item("б", ts=NOW))
    assert [x["id"] for x in st.list_history(None, 10, 0)[0]] == [b["id"], a["id"]]


def test_search_cyrillic_case_insensitive(st: Storage) -> None:
    st.add_history(item("Привет, ClickHouse! Всё ГОТОВО", raw="привет клик хаус"))
    st.add_history(item("Ёжик в тумане", app="telegram.exe", title="Чат с Машей"))
    st.add_history(item("Скидка 50% на user_name"))
    st.add_history(item("совсем другое"))

    def found(q: str) -> list[str]:
        items, total = st.list_history(q, 50, 0)
        assert total == len(items)
        return [x["text"] for x in items]

    assert found("привет") == ["Привет, ClickHouse! Всё ГОТОВО"]
    assert found("ПРИВЕТ") == found("пРиВеТ") == found("привет")
    assert found("готово") == ["Привет, ClickHouse! Всё ГОТОВО"]
    assert found("clickHOUSE") == ["Привет, ClickHouse! Всё ГОТОВО"]
    assert found("клик хаус") == ["Привет, ClickHouse! Всё ГОТОВО"]     # по сырому тексту
    assert found("все готово") == ["Привет, ClickHouse! Всё ГОТОВО"]     # ё = е
    assert len(found("все")) == 2                                         # подстрока: «совсем»
    assert found("ежик") == found("ЁЖИК") == ["Ёжик в тумане"]
    assert found("TELEGRAM") == ["Ёжик в тумане"]                         # по приложению
    assert found("машей") == ["Ёжик в тумане"]                            # по заголовку
    assert found("тумане ежик") == ["Ёжик в тумане"]                      # все слова, любой порядок
    assert found("тумане clickhouse") == []
    assert found("50%") == ["Скидка 50% на user_name"]                    # % и _ — буквально
    assert found("r_n") == ["Скидка 50% на user_name"]
    assert found("%") == ["Скидка 50% на user_name"]
    assert found("_") == ["Скидка 50% на user_name"]
    assert found("нет такого") == []
    assert len(found("   ")) == 4


def test_clear_history_periods(st: Storage) -> None:
    ages_h = [1, 23, 25, 24 * 6, 24 * 8, 24 * 29, 24 * 31, 24 * 400]
    for a in ages_h:
        st.add_history(item(f"{a} ч назад", ts=NOW - a * HOUR))
    assert st.clear_history("day", now=NOW) == 2
    assert st.clear_history("week", now=NOW) == 2
    assert st.clear_history("month", now=NOW) == 2
    remaining = [x["text"] for x in st.list_history(None, 50, 0)[0]]
    assert remaining == [f"{24 * 31} ч назад", f"{24 * 400} ч назад"]
    assert st.clear_history("all") == 2
    assert st.list_history(None, 50, 0) == ([], 0)
    assert st.clear_history("all") == 0


def test_clear_history_bad_period(st: Storage) -> None:
    st.add_history(item("x"))
    with pytest.raises(ValueError, match="период"):
        st.clear_history("year")
    assert st.list_history(None, 10, 0)[1] == 1


def test_purge_older_than(st: Storage) -> None:
    for d in (0, 5, 29, 31, 365):
        st.add_history(item(f"{d} дн", ts=NOW - d * DAY - 60))
    assert st.purge_older_than(0, now=NOW) == 0
    assert st.purge_older_than(-3, now=NOW) == 0
    assert st.purge_older_than(30, now=NOW) == 2
    assert [x["text"] for x in st.list_history(None, 10, 0)[0]] == ["0 дн", "5 дн", "29 дн"]


def test_cleared_text_not_left_in_files(tmp_path: Path) -> None:
    p = tmp_path / "priv.db"
    marker = "СЕКРЕТНАЯ-ФРАЗА-QWERTY-12345"
    with Storage(p) as s:
        for _ in range(20):
            s.add_history(item(marker * 5))
        assert s.clear_history("all") == 20
        blob = p.read_bytes()
        wal = Path(str(p) + "-wal")
        if wal.exists():
            blob += wal.read_bytes()
    assert marker.encode("utf-8") not in blob



def test_deleted_item_not_left_in_wal(tmp_path: Path) -> None:
    """Удалили одну запись (случайно надиктованный пароль): текста нет ни в базе, ни в -wal,
    даже если ядро потом убьют без закрытия базы (так Rust завершает ядро)."""
    p = tmp_path / "priv.db"
    marker = "ПАРОЛЬ-НАДИКТОВАН-9f3k2"
    s = Storage(p)
    keep = s.add_history(item("обычная фраза"))
    secret = s.add_history(item(marker))
    s.delete_history(secret["id"])
    blob = p.read_bytes()
    wal = Path(str(p) + "-wal")
    if wal.exists():
        blob += wal.read_bytes()
    assert marker.encode("utf-8") not in blob
    assert s.get_history(keep["id"]) is not None
    s.close()


def test_clear_all_removes_broken_db_copies(tmp_path: Path) -> None:
    p = tmp_path / "rupor.db"
    p.write_bytes(b"this is definitely not a database " * 100)
    s = Storage(p)
    assert s.recovered is not None and list(tmp_path.glob("rupor.db.broken-*"))
    s.add_history(item("x"))
    s.clear_history("week")
    assert list(tmp_path.glob("rupor.db.broken-*"))        # только за период — копия цела
    s.clear_history("all")
    assert not list(tmp_path.glob("rupor.db.broken-*"))    # «Очистить всё» — и копии
    s.close()

# ---------- статистика ----------

def test_stats_empty(st: Storage) -> None:
    s = st.stats(today=TODAY)
    assert s["words_today"] == s["words_week"] == s["words_total"] == 0
    assert s["wpm"] == 0 and s["saved_minutes"] == 0
    assert s["streak_days"] == 0 and s["dictations_total"] == 0
    assert len(s["daily"]) == 30
    assert s["daily"][0] == {"date": "2026-08-28", "words": 0}
    assert s["daily"][-1] == {"date": "2026-09-26", "words": 0}
    assert set(s) == {"words_today", "words_week", "words_total", "wpm", "saved_minutes",
                      "streak_days", "dictations_total", "daily"}


def test_stats_words_and_speed(st: Storage) -> None:
    st.record_stats(100, 60.0, when=noon(TODAY))
    st.record_stats(50, 30.0, when=noon(TODAY))
    st.record_stats(30, 18.0, when=noon(TODAY - timedelta(days=6)))   # ещё «неделя»
    st.record_stats(20, 12.0, when=noon(TODAY - timedelta(days=7)))   # уже нет
    st.record_stats(100, 60.0, when=noon(TODAY - timedelta(days=100)))
    s = st.stats(today=TODAY)
    assert s["words_today"] == 150
    assert s["words_week"] == 180
    assert s["words_total"] == 300
    assert s["dictations_total"] == 5
    assert s["wpm"] == 100.0                     # 300 слов / 3 мин речи
    assert s["saved_minutes"] == 4.5             # 300/40 − 3
    daily = {d["date"]: d["words"] for d in s["daily"]}
    assert daily["2026-09-26"] == 150 and daily["2026-09-20"] == 30 and daily["2026-09-19"] == 20
    assert "2026-06-18" not in daily             # за пределами 30 дней
    assert sum(daily.values()) == 200


def test_stats_saved_minutes_not_negative(st: Storage) -> None:
    st.record_stats(10, 600.0, when=noon(TODAY))
    s = st.stats(today=TODAY)
    assert s["saved_minutes"] == 0
    assert s["wpm"] == 1.0


@pytest.mark.parametrize("days_ago,expected", [
    ([0, 1, 2], 3),
    ([0, 1, 3], 2),           # разрыв
    ([1, 2, 3], 3),           # сегодня ещё не диктовали — серия до вчера
    ([2, 3], 0),              # ни сегодня, ни вчера
    ([0], 1),
    ([], 0),
    (list(range(45)), 45),    # серия длиннее окна графика
])
def test_stats_streak(st: Storage, days_ago: list[int], expected: int) -> None:
    for d in days_ago:
        st.record_stats(5, 2.0, when=noon(TODAY - timedelta(days=d)))
    st.record_stats(5, 2.0, when=noon(TODAY + timedelta(days=3)))   # «будущее» не мешает
    assert st.stats(today=TODAY)["streak_days"] == expected


def test_stats_zero_word_dictation_counts_for_streak(st: Storage) -> None:
    st.record_stats(0, 1.0, when=noon(TODAY))
    s = st.stats(today=TODAY)
    assert s["streak_days"] == 1 and s["words_today"] == 0 and s["dictations_total"] == 1


def test_stats_bad_input_clamped(st: Storage) -> None:
    st.record_stats(-5, float("nan"), when=noon(TODAY))
    st.record_stats(10, -3.0, when=noon(TODAY))
    s = st.stats(today=TODAY)
    assert s["words_total"] == 10 and s["wpm"] == 0 and s["dictations_total"] == 2


def test_stats_independent_of_history(st: Storage) -> None:
    for i in range(3):
        st.add_history(item(f"текст {i}"))
        st.record_stats(2, 1.0, when=noon(TODAY))
    before = st.stats(today=TODAY)
    st.clear_history("all")
    st.purge_older_than(1)
    assert st.stats(today=TODAY) == before
    assert before["words_today"] == 6


def test_stats_default_today(st: Storage) -> None:
    st.record_stats(7, 3.0)
    s = st.stats()
    assert s["words_today"] == 7 and s["streak_days"] == 1
    assert s["daily"][-1]["date"] == date.today().isoformat()


# ---------- словарь ----------

def test_dict_crud(st: Storage) -> None:
    a = st.dict_upsert(None, "  ClickHouse ", [" клик хаус ", "кликхаус", "", "КЛИК ХАУС",
                                                 "клик  хаус"])
    assert a == {"id": a["id"], "term": "ClickHouse", "variants": ["клик хаус", "кликхаус"]}
    b = st.dict_upsert(None, "metabase", ["метабейс"])
    assert [x["term"] for x in st.dict_list()] == ["ClickHouse", "metabase"]
    b2 = st.dict_upsert(b["id"], "Metabase", ["мета бейс"])        # по id — замена вариантов
    assert b2 == {"id": b["id"], "term": "Metabase", "variants": ["мета бейс"]}
    st.dict_delete(a["id"])
    st.dict_delete(424242)
    assert st.dict_list() == [b2]


def test_dict_upsert_same_term_merges_cyrillic(st: Storage) -> None:
    a = st.dict_upsert(None, "Сбербанк", ["сбер банк"])
    b = st.dict_upsert(None, "СБЕРБАНК", ["сбер", "Сбер Банк"])
    assert b["id"] == a["id"]
    assert b["variants"] == ["сбер банк", "сбер"]
    assert len(st.dict_list()) == 1
    c = st.dict_upsert(None, "Ёлка", [])
    d = st.dict_upsert(None, "елка", ["ёлочка"])
    assert c["id"] == d["id"] and len(st.dict_list()) == 2


def test_dict_upsert_conflict_and_validation(st: Storage) -> None:
    a = st.dict_upsert(None, "Python", [])
    b = st.dict_upsert(None, "Питон", [])
    with pytest.raises(ValueError, match="уже есть"):
        st.dict_upsert(b["id"], "PYTHON", [])
    with pytest.raises(ValueError, match="уже есть"):
        st.dict_upsert(a["id"], "питон", [])
    with pytest.raises(ValueError, match="пуст"):
        st.dict_upsert(None, "   ", ["x"])
    with pytest.raises(ValueError, match="пуст"):
        st.dict_upsert(None, "", [])
    same = st.dict_upsert(a["id"], "python", ["пайтон"])          # смена регистра своей записи
    assert same["id"] == a["id"] and same["term"] == "python"
    bad: list[Any] = ["ок", None, 5, {"x": 1}]
    assert st.dict_upsert(None, "Grafana", bad)["variants"] == ["ок"]


def test_dict_upsert_missing_id_creates(st: Storage) -> None:
    x = st.dict_upsert(999, "Grafana", ["графана"])
    assert x["term"] == "Grafana" and st.dict_list() == [x]


def test_dict_broken_json_in_db(st: Storage, tmp_path: Path) -> None:
    a = st.dict_upsert(None, "API", ["апи"])
    con = sqlite3.connect(tmp_path / "rupor.db")
    con.execute("UPDATE dictionary SET variants = '{битый' WHERE id = ?", (a["id"],))
    con.commit()
    con.close()
    assert st.dict_list() == [{"id": a["id"], "term": "API", "variants": []}]


# ---------- сниппеты ----------

def test_snippets_crud(st: Storage) -> None:
    a = st.snippet_upsert(None, " мой   имейл ", "ivan@example.com")
    assert a == {"id": a["id"], "trigger": "мой имейл", "text": "ivan@example.com"}
    b = st.snippet_upsert(None, "Подпись", "С уважением,\nИван\n")
    assert b["text"] == "С уважением,\nИван\n"                     # текст хранится как есть
    assert [x["trigger"] for x in st.snippets_list()] == ["мой имейл", "Подпись"]
    b2 = st.snippet_upsert(b["id"], "подпись коротко", "Иван")
    assert b2 == {"id": b["id"], "trigger": "подпись коротко", "text": "Иван"}
    st.snippet_delete(a["id"])
    st.snippet_delete(31337)
    assert st.snippets_list() == [b2]


def test_snippet_same_trigger_updates(st: Storage) -> None:
    a = st.snippet_upsert(None, "Подпись", "старый")
    b = st.snippet_upsert(None, "ПОДПИСЬ.", "новый")
    assert b["id"] == a["id"] and b["text"] == "новый"
    assert len(st.snippets_list()) == 1


def test_snippet_conflict_and_validation(st: Storage) -> None:
    a = st.snippet_upsert(None, "адрес", "Москва")
    st.snippet_upsert(None, "телефон", "+7")
    with pytest.raises(ValueError, match="уже есть"):
        st.snippet_upsert(a["id"], "Телефон", "x")
    with pytest.raises(ValueError, match="Триггер"):
        st.snippet_upsert(None, "  ...  ", "x")
    with pytest.raises(ValueError, match="Текст"):
        st.snippet_upsert(None, "пусто", "   \n")


# ---------- импорт / экспорт ----------

def fill(s: Storage) -> None:
    s.dict_upsert(None, "ClickHouse", ["клик хаус", "кликхаус"])
    s.dict_upsert(None, "Сбербанк", [])
    s.snippet_upsert(None, "мой имейл", "ivan@example.com")
    s.snippet_upsert(None, "подпись", "С уважением,\nИван")


def test_export_import_roundtrip(st: Storage, tmp_path: Path) -> None:
    fill(st)
    data = st.export_data()
    assert data["version"] == 1
    assert data["dictionary"] == [{"term": "ClickHouse", "variants": ["клик хаус", "кликхаус"]},
                                  {"term": "Сбербанк", "variants": []}]
    assert {"trigger": "подпись", "text": "С уважением,\nИван"} in data["snippets"]
    # через JSON, как в файле
    text = json.dumps(data, ensure_ascii=False)
    with Storage(tmp_path / "other.db") as other:
        assert other.import_data(json.loads(text), replace=False) == {"dictionary": 2,
                                                                       "snippets": 2}
        assert other.export_data() == data


def test_import_merge(st: Storage) -> None:
    fill(st)
    res = st.import_data({"version": 1,
                          "dictionary": [{"term": "clickhouse", "variants": ["клик-хаус"]},
                                         {"term": "Metabase", "variants": ["метабейс"]}],
                          "snippets": [{"trigger": "Подпись!", "text": "Иван"}]},
                         replace=False)
    assert res == {"dictionary": 2, "snippets": 1}
    d = {x["term"].casefold(): x["variants"] for x in st.dict_list()}
    assert d["clickhouse"] == ["клик хаус", "кликхаус", "клик-хаус"]   # варианты дополнены
    assert d["metabase"] == ["метабейс"] and "сбербанк" in d
    sn = {x["trigger"].casefold().strip("!"): x["text"] for x in st.snippets_list()}
    assert sn == {"мой имейл": "ivan@example.com", "подпись": "Иван"}


def test_import_replace_clears_both_first(st: Storage) -> None:
    fill(st)
    res = st.import_data({"dictionary": [{"term": "Grafana", "variants": ["графана"]}]},
                         replace=True)
    assert res == {"dictionary": 1, "snippets": 0}
    assert [x["term"] for x in st.dict_list()] == ["Grafana"]
    assert st.snippets_list() == []


def test_import_skips_broken_records(st: Storage) -> None:
    data: dict[str, Any] = {
        "version": 1,
        "dictionary": [
            {"term": "Python", "variants": ["питон", None, 5, "  "]},
            {"term": "", "variants": ["x"]},
            {"term": None},
            {"variants": ["y"]},
            "строка вместо объекта",
            42,
            {"term": "Rust", "variants": {"не": "список"}},
            {"term": "Go", "variants": "гоу"},                      # строка → один вариант
            {"term": "python", "variants": ["пайтон"]},             # дубль внутри файла
        ],
        "snippets": [
            {"trigger": "адрес", "text": "Москва"},
            {"trigger": "", "text": "x"},
            {"trigger": "пусто", "text": ""},
            {"trigger": "число", "text": 5},
            None,
        ],
    }
    assert st.import_data(data, replace=False) == {"dictionary": 2, "snippets": 1}
    d = {x["term"]: x["variants"] for x in st.dict_list()}
    assert d == {"Go": ["гоу"], "python": ["питон", "пайтон"]}
    assert [(x["trigger"], x["text"]) for x in st.snippets_list()] == [("адрес", "Москва")]


def test_import_invalid_data_changes_nothing(st: Storage) -> None:
    fill(st)
    before = st.export_data()
    bad: list[Any] = [None, [], "не json {", 42, {}, {"version": 1}, {"foo": []}]
    for b in bad:
        with pytest.raises(ValueError):
            st.import_data(b, replace=True)
    assert st.export_data() == before


def test_import_lenient_shapes(st: Storage) -> None:
    res = st.import_data(json.dumps({"dictionary": {"ClickHouse": ["клик хаус"]},  # type: ignore[arg-type]
                                     "snippets": {"подпись": "Иван"}}), replace=False)
    assert res == {"dictionary": 1, "snippets": 1}
    assert st.dict_list()[0]["variants"] == ["клик хаус"]
    assert st.snippets_list()[0]["text"] == "Иван"
    # неправильный тип раздела — пропуск, а не падение
    assert st.import_data({"dictionary": "мусор", "snippets": []}, replace=False) == \
        {"dictionary": 0, "snippets": 0}


def test_import_empty_replace_resets(st: Storage) -> None:
    fill(st)
    assert st.import_data({"version": 1, "dictionary": [], "snippets": []}, replace=True) == \
        {"dictionary": 0, "snippets": 0}
    assert st.dict_list() == [] and st.snippets_list() == []


def test_import_is_atomic(st: Storage, monkeypatch: pytest.MonkeyPatch) -> None:
    fill(st)
    before = st.export_data()

    def boom(*_a: Any, **_k: Any) -> Any:
        raise RuntimeError("сбой посреди импорта")

    monkeypatch.setattr(Storage, "_snippet_upsert_locked", boom)
    with pytest.raises(RuntimeError):
        st.import_data({"dictionary": [{"term": "Grafana", "variants": []}],
                        "snippets": [{"trigger": "x", "text": "y"}]}, replace=True)
    monkeypatch.undo()
    assert st.export_data() == before                  # ни очистки, ни частичных вставок
    st.kv_set("after", "ok")                           # соединение не застряло в транзакции
    assert st.kv_get("after") == "ok"


# ---------- kv / черновик ----------

def test_kv(st: Storage) -> None:
    assert st.kv_get("draft") == ""
    assert st.kv_get("draft", "по умолчанию") == "по умолчанию"
    st.kv_set("draft", "Черновик\nвторая строка 🙂")
    assert st.kv_get("draft") == "Черновик\nвторая строка 🙂"
    st.kv_set("draft", "")
    assert st.kv_get("draft", "x") == ""


# ---------- потоки ----------

def run_threads(n: int, fn: Callable[[int], None]) -> None:
    errors: list[BaseException] = []
    start = threading.Barrier(n)

    def wrap(i: int) -> None:
        try:
            start.wait()
            fn(i)
        except BaseException as e:   # noqa: BLE001 — пробрасываем в основной поток
            errors.append(e)

    threads = [threading.Thread(target=wrap, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
    assert not any(t.is_alive() for t in threads), "поток завис"
    assert not errors, errors


def test_concurrent_single_instance(st: Storage) -> None:
    per = 60

    def work(i: int) -> None:
        for j in range(per):
            st.add_history(item(f"поток{i}x запись {j} клик хаус", ts=NOW + j))
            st.record_stats(3, 1.0, when=noon(TODAY))
            if j % 10 == 0:
                st.dict_upsert(None, f"Термин{j}", [f"вариант {i}"])
                st.list_history("клик", 5, 0)
                st.stats(today=TODAY)
                st.kv_set("draft", f"{i}-{j}")

    run_threads(8, work)
    assert st.list_history(None, 1, 0)[1] == 8 * per
    assert st.list_history("ПОТОК3X", 1, 0)[1] == per
    s = st.stats(today=TODAY)
    assert s["dictations_total"] == 8 * per and s["words_today"] == 3 * 8 * per
    terms = st.dict_list()
    assert len(terms) == per // 10
    assert all(len(t["variants"]) == 8 for t in terms)   # слияние без потерь


def test_concurrent_two_instances_same_file(tmp_path: Path) -> None:
    p = tmp_path / "shared.db"
    stores = [Storage(p), Storage(p)]
    try:
        def work(i: int) -> None:
            s = stores[i % 2]
            for j in range(40):
                s.add_history(item(f"{i}-{j}"))
                s.record_stats(1, 0.5, when=noon(TODAY))

        run_threads(6, work)
        for s in stores:
            assert s.list_history(None, 1, 0)[1] == 240
            assert s.stats(today=TODAY)["dictations_total"] == 240
    finally:
        for s in stores:
            s.close()
