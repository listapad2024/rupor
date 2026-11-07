"""Хранилище: миграции схемы 1 → 3, расход ИИ (ai_usage) и личный словарь частот (vocab).

База — во временной папке; тексты синтетические."""
from __future__ import annotations

import re
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterator

import pytest

from rupor import storage as storage_mod
from rupor import vocab
from rupor.storage import SCHEMA_VERSION, Storage

TODAY = date(2026, 9, 27)


def ts_of(d: date, hour: int = 12) -> float:
    return datetime(d.year, d.month, d.day, hour).timestamp()


def hist(text: str, app: str = "notepad.exe", target: str = "window",
         ts: float | None = None) -> dict[str, Any]:
    return {"ts": ts or ts_of(TODAY), "app": app, "title": "t", "raw": text, "text": text,
            "duration_s": 1.0, "language": "ru", "style": "normal", "ai": False,
            "latency_ms": 100, "target": target}


@pytest.fixture
def st(tmp_path: Path) -> Iterator[Storage]:
    s = Storage(tmp_path / "rupor.db")
    try:
        yield s
    finally:
        s.close()


# ---------- миграции ----------

_V1 = storage_mod._MIGRATIONS[0]


def _make_v1(p: Path, texts: list[tuple[str, str, str]]) -> None:
    con = sqlite3.connect(p)
    for sql in _V1:
        assert isinstance(sql, str)
        con.execute(sql)
    for text, app, target in texts:
        con.execute("INSERT INTO history(ts, app, title, raw, text, target) VALUES(?,?,?,?,?,?)",
                    (ts_of(TODAY), app, "", text, text, target))
    con.execute("PRAGMA user_version = 1")
    con.commit()
    con.close()


def test_migration_1_to_3_builds_vocab_from_history(tmp_path: Path) -> None:
    p = tmp_path / "old.db"
    _make_v1(p, [("Обнови дашборд в Metabase", "chrome.exe", "window"),
                 ("Metabase снова упал, пишу Тимуру", "Telegram.exe", "window"),
                 ("проверка Metabase", "Рупор", "test"),          # проверки — не считаем
                 ("переведи Metabase", "x.exe", "command")])     # команды — не считаем
    with Storage(p) as s:
        words = {w["word"]: w for w in s.vocab_list(limit=50)}
        assert words["Metabase"]["count"] == 2 and not words["Metabase"]["excluded"]
        # в Telegram слово было один раз — «частым» для процесса не считается, но общее
        assert s.vocab_prompt_words("telegram.exe") == ([], ["Metabase"])
        assert s.ai_usage_stats(TODAY)["total"]["calls"] == 0     # таблица создана
    con = sqlite3.connect(p)
    try:
        assert con.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION == 4
        ver = con.execute("SELECT value FROM kv WHERE key = 'vocab_version'").fetchone()[0]
        assert ver == vocab.version()
        rows = dict(con.execute("SELECT app, sum(count) FROM vocab WHERE word = 'Metabase' "
                                "GROUP BY app").fetchall())
        assert rows == {"chrome.exe": 1, "telegram.exe": 1}
    finally:
        con.close()


def test_new_vocab_version_rebuilds(tmp_path: Path) -> None:
    p = tmp_path / "r.db"
    with Storage(p) as s:
        s.add_history(hist("ClickHouse ClickHouse"))
    con = sqlite3.connect(p)
    con.execute("DELETE FROM vocab")
    con.execute("UPDATE kv SET value = 'старая' WHERE key = 'vocab_version'")
    con.commit()
    con.close()
    with Storage(p) as s:
        assert s.vocab_list() == [{"word": "ClickHouse", "count": 2, "excluded": False}]


# ---------- расход ИИ ----------

def usage(kind: str = "cleanup", cost: float | None = 0.001, ok: bool = True,
          day: date = TODAY, model: str = "anthropic/claude-haiku-4.5",
          **kw: Any) -> dict[str, Any]:
    return {"ts": ts_of(day), "kind": kind, "provider": "openrouter", "model": model,
            "upstream": "Amazon Bedrock", "tokens_in": 1000, "tokens_out": 40,
            "cost_usd": cost, "ms": 1200, "ok": ok, "error_kind": kw.pop("error_kind", None),
            "app": "notepad.exe", **kw}


def test_ai_usage_record_and_error_costs_zero(st: Storage) -> None:
    r = st.add_ai_usage(usage())
    assert r["id"] > 0 and r["cost_usd"] == 0.001 and r["ok"] is True and r["error_kind"] is None
    e = st.add_ai_usage(usage(cost=0.5, ok=False, error_kind="timeout", tokens_in=None,
                              tokens_out=None))
    assert e["cost_usd"] == 0.0 and e["ok"] is False and e["error_kind"] == "timeout"
    assert e["tokens_in"] is None
    cli = st.add_ai_usage(usage(kind="command", cost=None, model="claude -p"))
    assert cli["cost_usd"] is None                       # claude -p: стоимость неизвестна
    with pytest.raises(ValueError, match="вид вызова"):
        st.add_ai_usage(usage(kind="poem"))


def test_ai_usage_stats_periods_daily_kinds_models(st: Storage) -> None:
    st.add_ai_usage(usage(cost=0.002))                                   # сегодня
    st.add_ai_usage(usage(cost=0.0, ok=False, error_kind="timeout"))     # сегодня, ошибка
    st.add_ai_usage(usage(kind="command", cost=0.01, day=TODAY - timedelta(days=3)))
    st.add_ai_usage(usage(kind="test", cost=0.004, day=TODAY - timedelta(days=20),
                          model="anthropic/claude-sonnet-5"))
    st.add_ai_usage(usage(cost=1.0, day=TODAY - timedelta(days=45)))    # старше месяца
    r = st.ai_usage_stats(TODAY)
    assert r["today"] == {"calls": 2, "tokens_in": 2000, "tokens_out": 80, "cost_usd": 0.002,
                          "unknown_cost_calls": 0}
    assert r["week"]["calls"] == 3 and r["week"]["cost_usd"] == pytest.approx(0.012)
    assert r["month"]["calls"] == 4 and r["month"]["cost_usd"] == pytest.approx(0.016)
    assert r["total"]["calls"] == 5 and r["total"]["cost_usd"] == pytest.approx(1.016)
    assert r["avg_cost_per_call"] == pytest.approx(1.016 / 4)            # только успешные
    assert len(r["daily"]) == 30
    assert r["daily"][-1]["date"] == TODAY.isoformat() and r["daily"][-1]["calls"] == 2
    assert r["daily"][0]["date"] == (TODAY - timedelta(days=29)).isoformat()
    assert r["daily"][-4]["cost_usd"] == pytest.approx(0.01)
    assert sum(d["calls"] for d in r["daily"]) == 4 and r["daily"][5]["calls"] == 0
    assert set(r["daily"][0]) == {"date", "calls", "tokens_in", "tokens_out", "cost_usd"}
    assert r["by_kind"] == [{"kind": "cleanup", "calls": 3, "cost_usd": 1.002},
                            {"kind": "command", "calls": 1, "cost_usd": 0.01},
                            {"kind": "test", "calls": 1, "cost_usd": 0.004}]
    assert r["by_model"][0] == {"model": "anthropic/claude-haiku-4.5", "calls": 4,
                                "cost_usd": pytest.approx(1.012), "unknown_cost_calls": 0}
    assert r["by_model"][1]["model"] == "anthropic/claude-sonnet-5"


def test_ai_usage_stats_empty(st: Storage) -> None:
    r = st.ai_usage_stats(TODAY)
    assert r["total"] == {"calls": 0, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.0,
                          "unknown_cost_calls": 0}
    assert r["avg_cost_per_call"] == 0.0 and len(r["daily"]) == 30
    assert [k["kind"] for k in r["by_kind"]] == ["cleanup", "command", "test"]
    assert r["by_model"] == []


def test_ai_usage_today_cost(st: Storage) -> None:
    now = datetime.now()
    st.add_ai_usage({**usage(cost=0.003), "ts": now.timestamp()})
    st.add_ai_usage({**usage(cost=0.5), "ts": (now - timedelta(days=2)).timestamp()})
    assert st.ai_usage_today_cost() == pytest.approx(0.003)


# ---------- словарь частот ----------

def test_vocab_counts_incrementally_by_process(st: Storage) -> None:
    for _ in range(2):
        st.add_history(hist("Задеплой в Kubernetes и проверь Grafana", app="Code.exe"))
    st.add_history(hist("Kubernetes опять лежит", app="Telegram.exe"))
    st.add_history(hist("Kubernetes в проверке", target="test"))            # не считается
    assert st.vocab_list(limit=2) == [{"word": "Kubernetes", "count": 3, "excluded": False},
                                      {"word": "Grafana", "count": 2, "excluded": False}]
    # поровну и одновременно — по алфавиту
    assert st.vocab_list("code.exe") == [{"word": "Grafana", "count": 2, "excluded": False},
                                         {"word": "Kubernetes", "count": 2, "excluded": False}]
    # один раз — ещё не «частое»: в список и подсказку не попадает
    st.add_history(hist("созвон в Zoom", app="Telegram.exe"))
    assert all(w["word"] != "Zoom" for w in st.vocab_list())
    proc, gen = st.vocab_prompt_words("telegram.exe")
    assert proc == [] and gen == ["Kubernetes", "Grafana"]
    proc, gen = st.vocab_prompt_words("Code.exe")
    assert proc == ["Grafana", "Kubernetes"] and gen == []


def test_vocab_weak_words_need_more_repeats(st: Storage) -> None:
    for i in range(vocab.WEAK_MIN):
        st.add_history(hist("обнови дашборд"))
        listed = [w["word"] for w in st.vocab_list()]
        assert ("дашборд" in listed) == (i + 1 >= vocab.WEAK_MIN)
    assert st.vocab_prompt_words(None, strong_only=True) == ([], [])


def test_vocab_exclude_and_include(st: Storage) -> None:
    for _ in range(2):
        st.add_history(hist("Redis и Postgres"))
    st.vocab_exclude("REDIS", True)
    assert {"word": "Redis", "count": 2, "excluded": True} in st.vocab_list()
    assert st.vocab_prompt_words(None) == ([], ["Postgres"])
    st.vocab_exclude("redis", False)
    assert st.vocab_prompt_words(None) == ([], ["Postgres", "Redis"])
    with pytest.raises(ValueError, match="пуст"):
        st.vocab_exclude("  ", True)


def test_vocab_follows_history_deletions(st: Storage) -> None:
    a = st.add_history(hist("Kubernetes и Grafana", ts=ts_of(TODAY - timedelta(days=40))))
    st.add_history(hist("Kubernetes и Grafana"))
    b = st.add_history(hist("Kubernetes"))
    assert st.vocab_list()[0] == {"word": "Kubernetes", "count": 3, "excluded": False}
    st.delete_history(b["id"])
    assert st.vocab_list()[0]["count"] == 2
    st.purge_older_than(30, now=ts_of(TODAY))                  # запись «a» старше 30 дней
    assert st.get_history(a["id"]) is None
    con = st._conn
    assert {r[0]: r[1] for r in con.execute("SELECT word, count FROM vocab")} == {
        "Kubernetes": 1, "Grafana": 1}
    st.clear_history("all")
    assert con.execute("SELECT count(*) FROM vocab").fetchone()[0] == 0


def test_vocab_clear_period_subtracts(st: Storage) -> None:
    st.add_history(hist("Kubernetes", ts=ts_of(TODAY) - 5 * 86400))
    st.add_history(hist("Kubernetes", ts=ts_of(TODAY)))
    st.clear_history("day", now=ts_of(TODAY) + 60)
    assert [r[1] for r in st._conn.execute("SELECT word, count FROM vocab")] == [1]


def test_vocab_prompt_cache_invalidated(st: Storage) -> None:
    st.add_history(hist("Kubernetes Kubernetes"))
    assert st.vocab_prompt_words(None) == ([], ["Kubernetes"])
    st.add_history(hist("Grafana Grafana Grafana"))
    assert st.vocab_prompt_words(None) == ([], ["Grafana", "Kubernetes"])
    st.vocab_exclude("grafana", True)
    assert st.vocab_prompt_words(None) == ([], ["Kubernetes"])


def test_vocab_rebuild_matches_incremental(st: Storage) -> None:
    for t in ("Metabase и Тимур", "в Metabase", "дашборд дашборд дашборд"):
        st.add_history(hist(t))
    before = sorted(tuple(r) for r in st._conn.execute("SELECT word, app, count FROM vocab"))
    st.vocab_rebuild()
    after = sorted(tuple(r) for r in st._conn.execute("SELECT word, app, count FROM vocab"))
    assert before == after


# ---------- удаление истории: вычитание частот одним проходом ----------

def _fill(st: Storage, n: int, start_days_ago: int = 60) -> None:
    apps = ("code.exe", "telegram.exe", "chrome.exe")
    names = ("Тимуру", "Наташе", "Кириллу")
    terms = ("Kubernetes", "Grafana", "Metabase", "Superset", "Airflow")
    base = ts_of(TODAY) - start_days_ago * 86400
    for i in range(n):
        text = (f"{terms[i % 5]} и {terms[(i * 3) % 5]}, напиши {names[i % 3]} про "
                f"Term{i % 17} и дашборд{i % 4}")
        st.add_history(hist(text, app=apps[i % 3], ts=base + i * 3600 * 6))


def _vocab_rows(st: Storage) -> list[tuple[Any, ...]]:
    return sorted(tuple(r) for r in st._conn.execute("SELECT word, app, count FROM vocab"))


def test_purge_subtracts_in_one_pass_and_matches_rebuild(st: Storage) -> None:
    """Регрессия: срок хранения на большой истории держал замок базы десятки секунд —
    после каждой записи шёл полный просмотр vocab. Теперь один DELETE на всё удаление."""
    _fill(st, 240)
    sql: list[str] = []
    st._conn.set_trace_callback(sql.append)
    n = st.purge_older_than(30, now=ts_of(TODAY))
    st._conn.set_trace_callback(None)
    assert 100 < n < 240
    assert sum(1 for q in sql if q.startswith("DELETE FROM vocab")) == 1
    after = _vocab_rows(st)
    st.vocab_rebuild()
    assert after == _vocab_rows(st)
    assert st.clear_history("week", now=ts_of(TODAY)) > 0
    after = _vocab_rows(st)
    st.vocab_rebuild()
    assert after == _vocab_rows(st)


def test_delete_parses_texts_outside_write_lock(st: Storage,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    _fill(st, 60)
    owned: list[bool] = []
    orig = vocab.counts

    def spy(text: str) -> Any:
        owned.append(st._lock._is_owned())  # type: ignore[attr-defined]
        return orig(text)

    monkeypatch.setattr(vocab, "counts", spy)
    assert st.purge_older_than(30, now=ts_of(TODAY)) > 0
    assert owned and not any(owned)             # разбор — без замка базы


# ---------- кэш частых слов ----------

def _vocab_selects(sql: list[str]) -> int:
    return sum(1 for q in sql
               if q.lstrip().startswith("SELECT") and re.search(r"FROM vocab\b", q))


def test_prompt_cache_is_shared_and_survives_new_dictation(st: Storage) -> None:
    """Регрессия: запись истории сбрасывала весь кэш, а подсказка Whisper и термины ИИ
    считали частоты по всей таблице каждый своим проходом."""
    for _ in range(2):
        st.add_history(hist("Kubernetes и Grafana", app="code.exe"))
    sql: list[str] = []
    st._conn.set_trace_callback(sql.append)
    assert st.vocab_prompt_words("code.exe") == (["Grafana", "Kubernetes"], [])
    first = _vocab_selects(sql)
    assert first == 2                                     # процесс + все процессы
    st.vocab_prompt_words("code.exe", vocab.MAX_AI_WORDS, strong_only=True)
    st.vocab_list(limit=50)
    assert _vocab_selects(sql) == first                   # тот же кэш, без запросов
    st.add_history(hist("Redis Redis", app="telegram.exe"))
    # другой процесс: общий топ устарел, топ code.exe — нет
    assert st.vocab_prompt_words("code.exe") == (["Grafana", "Kubernetes"], ["Redis"])
    assert _vocab_selects(sql) == first + 1
    st._conn.set_trace_callback(None)


def test_background_refresh_after_new_dictation(tmp_path: Path) -> None:
    with Storage(tmp_path / "r.db", background=True) as s:
        assert s.vocab_ready
        s.add_history(hist("Kubernetes Kubernetes"))
        assert s.vocab_prompt_words(None) == ([], ["Kubernetes"])
        s.add_history(hist("Grafana Grafana Grafana"))
        deadline = time.monotonic() + 5
        while s._vocab_stale or s._vocab_refreshing:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert s.vocab_prompt_words(None) == ([], ["Grafana", "Kubernetes"])
        s.delete_history(s.list_history()[0][0]["id"])     # удаление — сразу, без фона
        assert s.vocab_prompt_words(None) == ([], ["Kubernetes"])


# ---------- пересборка: битые записи, фон, изменения во время пересборки ----------

def test_bad_utf8_row_does_not_break_migration(tmp_path: Path) -> None:
    """Регрессия: строка истории с битым UTF-8 роняла миграцию 2→3 при каждом запуске."""
    p = tmp_path / "bad.db"
    _make_v1(p, [("Metabase и Metabase", "chrome.exe", "window")])
    con = sqlite3.connect(p)
    con.execute("INSERT INTO history(ts, app, title, raw, text, target) "
                "VALUES(?, 'x.exe', '', CAST(x'd0ff41' AS TEXT), CAST(x'd0ff41' AS TEXT), "
                "'window')", (ts_of(TODAY),))
    con.commit()
    con.close()
    with Storage(p) as s:
        assert s.vocab_ready and s.recovered is None
        assert s.vocab_list() == [{"word": "Metabase", "count": 2, "excluded": False}]
        items, total = s.list_history()
        assert total == 2 and any("�" in it["text"] for it in items)
        assert s.list_history("metabase")[1] == 1


def test_background_open_defers_rebuild(tmp_path: Path) -> None:
    """Регрессия: пересборка частот по большой истории шла при открытии базы — до того,
    как ядро откроет WS-порт. С background открытие её не ждёт."""
    p = tmp_path / "old.db"
    _make_v1(p, [("Metabase и ClickHouse", "chrome.exe", "window")] * 3)
    with Storage(p, background=True) as s:
        assert not s.vocab_ready
        assert s.vocab_prompt_words("chrome.exe") == ([], [])     # пока без частых слов
        assert s.vocab_rebuild_async()
        deadline = time.monotonic() + 10
        while not s.vocab_ready:
            assert time.monotonic() < deadline
            time.sleep(0.01)
        assert s.vocab_prompt_words("chrome.exe") == (["ClickHouse", "Metabase"], [])
        assert not s.vocab_rebuild_async()                        # уже не нужно
    con = sqlite3.connect(p)
    try:
        assert con.execute("SELECT value FROM kv WHERE key = 'vocab_version'").fetchone()[0] \
            == vocab.version()
    finally:
        con.close()


def test_rebuild_accounts_for_changes_during_parse(st: Storage,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    _fill(st, 30)
    victim = st.list_history(limit=1)[0][0]["id"]
    orig = storage_mod._vocab_counts
    state = {"done": False, "busy": False}

    def racing(text: Any, target: Any) -> Any:
        owned = st._lock._is_owned()  # type: ignore[attr-defined]
        if not state["done"] and not state["busy"] and not owned:
            state["busy"] = True
            st.add_history(hist("Новый Superset и Superset", app="code.exe"))
            st.delete_history(victim)
            state["busy"], state["done"] = False, True
        return orig(text, target)

    monkeypatch.setattr(storage_mod, "_vocab_counts", racing)
    st.vocab_rebuild()
    assert state["done"]
    monkeypatch.setattr(storage_mod, "_vocab_counts", orig)
    during = _vocab_rows(st)
    st.vocab_rebuild()
    assert during == _vocab_rows(st)


# ---------- исключения и формы слов ----------

def test_case_forms_merge_and_exclusion_covers_them(st: Storage) -> None:
    for t in ("Напиши Тимуру", "Позвони Тимуру", "Спроси у Тимура", "Скажи Тимуру",
              "Встреча с Тимуром", "Вот и Тимур"):
        st.add_history(hist(t))
    assert st.vocab_list() == [{"word": "Тимур", "count": 6, "excluded": False}]
    assert st.vocab_prompt_words(None, strong_only=True) == ([], ["Тимур"])
    st.vocab_exclude("Тимуру", True)
    assert st.vocab_list() == [{"word": "Тимур", "count": 6, "excluded": True}]
    assert st.vocab_prompt_words(None) == ([], [])
    st.vocab_exclude("тимур", False)                 # снять — любой формой
    assert st.vocab_excluded() == set()
    assert st.vocab_prompt_words(None) == ([], ["Тимур"])


def test_excluded_words_stay_visible_and_clear_all_wipes_them(st: Storage) -> None:
    """Регрессия: исключённое слово без частот (или ниже limit) не показывалось, и снять
    исключение было нельзя; «Очистить всё» оставляло vocab_excluded на диске."""
    for _ in range(3):
        st.add_history(hist("Kubernetes и Grafana"))
    st.add_history(hist("Kubernetes"))
    st.vocab_exclude("Zoom", True)                  # частот нет вовсе
    st.vocab_exclude("grafana", True)
    assert st.vocab_list(limit=1) == [
        {"word": "Kubernetes", "count": 4, "excluded": False},
        {"word": "Grafana", "count": 3, "excluded": True},          # за пределом limit
        {"word": "zoom", "count": 0, "excluded": True}]
    assert st.vocab_list("notepad.exe", limit=1) == [
        {"word": "Kubernetes", "count": 4, "excluded": False},
        {"word": "Grafana", "count": 3, "excluded": True}]          # zoom в процессе нет
    st.clear_history("all")
    assert st.vocab_excluded() == set() and st.vocab_list() == []
    assert st._conn.execute("SELECT count(*) FROM vocab_excluded").fetchone()[0] == 0


# ---------- расход ИИ: неизвестная цена ----------

def test_unknown_cost_is_not_counted_as_zero_in_average(st: Storage) -> None:
    """Регрессия: вызовы с неизвестной ценой (cost NULL) занижали среднюю цену вызова."""
    st.add_ai_usage(usage(cost=0.003))
    for _ in range(3):
        st.add_ai_usage(usage(cost=None, provider="anthropic",
                              model="claude-haiku-9-9-20990101"))
    st.add_ai_usage(usage(cost=None, ok=False, error_kind="timeout"))     # ошибка — $0
    r = st.ai_usage_stats(TODAY)
    assert r["avg_cost_per_call"] == pytest.approx(0.003)
    assert r["today"]["calls"] == 5 and r["today"]["unknown_cost_calls"] == 3
    assert r["total"]["unknown_cost_calls"] == 3
    m = {x["model"]: x for x in r["by_model"]}
    assert m["claude-haiku-9-9-20990101"]["unknown_cost_calls"] == 3
    assert m["anthropic/claude-haiku-4.5"]["unknown_cost_calls"] == 0


def test_vocab_english_history_rare_words_are_weak(st: Storage) -> None:
    """Английская история: редкие строчные слова («deployment», «invoices») — слабые: в
    подсказку только с WEAK_MIN повторов и не в промпт ИИ-очистки как «термины»; имена и
    термины (Kubernetes посреди фразы) — сильные. Та же строчная латиница посреди русской
    речи — термин (сильное)."""
    text = ("We need to finalize the quarterly deployment report for Kubernetes and send "
            "the invoices.")
    for _ in range(vocab.STRONG_MIN):
        st.add_history(hist(text, app="outlook.exe"))
    assert st.vocab_prompt_words(None) == ([], ["Kubernetes"])
    assert st.vocab_prompt_words(None, strong_only=True) == ([], ["Kubernetes"])
    for _ in range(vocab.WEAK_MIN - vocab.STRONG_MIN):
        st.add_history(hist(text, app="outlook.exe"))
    proc, gen = st.vocab_prompt_words(None)
    assert gen[0] == "Kubernetes" and {"finalize", "deployment", "invoices"} <= set(gen)
    assert st.vocab_prompt_words(None, strong_only=True) == ([], ["Kubernetes"])
    listed = {w["word"] for w in st.vocab_list()}
    assert "deployment" in listed and not any(w.startswith(vocab.EN_WEAK_MARK) for w in listed)
    # в русской речи то же слово — термин: хватает двух повторов, и оно сильное
    for _ in range(vocab.STRONG_MIN):
        st.add_history(hist("проверь deployment на стенде"))
    assert "deployment" in st.vocab_prompt_words(None, strong_only=True)[1]


def test_vocab_weak_english_marks_survive_delete_and_rebuild(st: Storage) -> None:
    a = st.add_history(hist("Please review the deployment checklist."))
    st.add_history(hist("Please review the deployment checklist."))
    rows = dict(st._conn.execute("SELECT word, count FROM vocab").fetchall())
    assert rows == {vocab.EN_WEAK_MARK + "deployment": 2, vocab.EN_WEAK_MARK + "checklist": 2}
    st.delete_history(a["id"])
    rows = dict(st._conn.execute("SELECT word, count FROM vocab").fetchall())
    assert rows == {vocab.EN_WEAK_MARK + "deployment": 1, vocab.EN_WEAK_MARK + "checklist": 1}
    st.vocab_rebuild()
    assert dict(st._conn.execute("SELECT word, count FROM vocab").fetchall()) == rows
