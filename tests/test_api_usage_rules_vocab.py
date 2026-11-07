"""WS-команды и события новых возможностей: расход ИИ и баланс, правила по названию
окна, recent_apps с заголовками, рассылка только в отмеченные RDP-сессии (recording.targets),
личный словарь частот и контекст окна в подсказке распознаванию.

Окружение — фикстура env из test_api (настоящий WS-сервер, движок без хука, фейковые
распознавание/вставка/ИИ/Credential Manager; проверка ключа OpenRouter — без сети; текст
перед курсором — фейк calls.context_reader, настоящее поле пользователя не читается)."""
from __future__ import annotations

import threading
import time
from typing import Any, Callable

import pytest

from rupor import ai, winutil
from rupor.hotkeys import Action, Kind
from test_api import FAKE_KEY, TARGET, Env, _enable_ai, env  # noqa: F401 — фикстура env

RDP_WIN = winutil.WindowInfo(5151, "TscShellContainerClass",
                             "srv1 - Подключение к удалённому рабочему столу", 777_777,
                             "mstsc.exe")


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("условие не выполнилось")
        time.sleep(0.01)


def usage_rows(e: Env) -> list[dict[str, Any]]:
    cur = e.eng.storage._conn.execute("SELECT * FROM ai_usage ORDER BY id")
    return [dict(r) for r in cur.fetchall()]


# ---------- расход ИИ ----------

def test_cleanup_is_recorded_with_event_and_stats(env: Env) -> None:
    c = env.client()
    _enable_ai(c)
    env.calls.cleanup_impl = lambda t: "Обновил витрину сегодня вечером."
    c.mark()
    env.dictate("ну короче обновил витрину сегодня вечером")
    ev = c.wait_event("ai_usage")
    assert ev == {"kind": "cleanup", "cost_usd": 0.001, "tokens_in": 0, "tokens_out": 0,
                  "today_cost_usd": 0.001}
    st = c.call("ai_usage_stats")
    assert st["today"] == {"calls": 1, "tokens_in": 0, "tokens_out": 0, "cost_usd": 0.001,
                           "unknown_cost_calls": 0}
    assert st["total"]["calls"] == 1 and st["avg_cost_per_call"] == 0.001
    assert st["by_kind"][0] == {"kind": "cleanup", "calls": 1, "cost_usd": 0.001}
    assert st["by_model"] == [{"model": "test", "calls": 1, "cost_usd": 0.001,
                               "unknown_cost_calls": 0}]
    assert len(st["daily"]) == 30 and st["daily"][-1]["calls"] == 1
    row = usage_rows(env)[0]
    assert (row["kind"], row["provider"], row["app"], row["ok"]) == (
        "cleanup", "openrouter", "notepad.exe", 1)
    # после вызова OpenRouter баланс обновляется в фоне (кэш был пуст)
    wait_until(lambda: env.calls.balance_checks == [FAKE_KEY])
    c.close()


def test_failed_cleanup_is_recorded_with_zero_cost(env: Env) -> None:
    c = env.client()
    _enable_ai(c)

    def boom(t: str) -> str:
        raise ai.AIError("OpenRouter не ответил за 5 с", "timeout")

    env.calls.cleanup_impl = boom
    c.mark()
    env.dictate("длинная фраза для очистки ИИ")
    ev = c.wait_event("ai_usage")
    assert ev["cost_usd"] == 0.0 and ev["kind"] == "cleanup"
    row = usage_rows(env)[0]
    assert (row["ok"], row["error_kind"], row["cost_usd"]) == (0, "timeout", 0.0)
    assert (row["provider"], row["model"]) == ("openrouter", "anthropic/claude-haiku-4.5")
    wait_until(lambda: bool(env.calls.delivered))   # ai_usage приходит раньше вставки
    assert env.calls.delivered == [("длинная фраза для очистки ИИ ", False)]  # текст цел
    c.close()


def test_command_and_test_calls_are_recorded(env: Env) -> None:
    c = env.client()
    c.mark()
    env.dictate("переведи на английский", Kind.COMMAND)
    ev = c.wait_event("ai_usage")
    assert ev["kind"] == "command" and ev["cost_usd"] == 0.0     # стоимость неизвестна
    r = c.call("ai_test", {"text": "ну проверка", "style": "normal"})
    assert r["text"] == "ну проверка"
    kinds = [(x["kind"], x["app"]) for x in usage_rows(env)]
    assert kinds == [("command", "notepad.exe"), ("test", "")]
    by_kind = {k["kind"]: k["calls"] for k in c.call("ai_usage_stats")["by_kind"]}
    assert by_kind == {"cleanup": 0, "command": 1, "test": 1}
    c.close()


def test_as_is_test_is_not_a_call(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    c = env.client()
    monkeypatch.setattr(ai, "cleanup", lambda text, style, s, terms, **kw: ai.AIResult(
        text, 0.0, 0.0, "none", ""))
    c.call("ai_test", {"text": "как есть", "style": "as_is"})
    assert usage_rows(env) == []
    c.close()


def test_ai_balance_cache_force_and_no_key(env: Env) -> None:
    c = env.client()
    r = c.call("ai_balance")
    assert r == {"balance_usd": None, "usage_usd": None, "limit_usd": None,
                 "checked_at": None,
                 "error": "Ключей ИИ нет — добавьте ключ в Настройках → ИИ",
                 "key_id": None, "provider": None, "label": None, "estimate": False}
    c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})
    r = c.call("ai_balance")
    assert r["balance_usd"] == 12.34 and r["usage_usd"] == 1.0 and r["error"] is None
    assert isinstance(r["checked_at"], int) and abs(r["checked_at"] - time.time()) < 5
    assert c.call("ai_balance") == r and len(env.calls.balance_checks) == 1   # кэш 5 мин
    c.call("ai_balance", {"force": True})
    assert len(env.calls.balance_checks) == 2
    c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})       # новый ключ
    c.call("ai_balance")
    assert len(env.calls.balance_checks) == 3
    assert "true или false" in c.error("ai_balance", {"force": "да"})
    assert not any(FAKE_KEY in raw for raw in c.raw)
    c.close()


def test_low_balance_notice_once_a_day(env: Env) -> None:
    c = env.client()
    c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})
    env.calls.balance["balance_usd"] = 0.42
    c.mark()
    assert c.call("ai_balance", {"force": True})["balance_usd"] == 0.42
    n = c.wait_event("notice", lambda d: "меньше $1" in d["text"])
    assert n["text"].startswith("На балансе OpenRouter меньше $1 — пополните")
    assert n["level"] == "warn" and n["action"] == "open_settings_ai"
    c.call("ai_balance", {"force": True})
    time.sleep(0.2)
    assert len([e for e in c.events("notice") if "меньше $1" in e["text"]]) == 1
    c.close()


# ---------- правила по названию окна ----------

def test_title_rules_commands(env: Env) -> None:
    c = env.client()
    assert c.call("title_rules_list") == []
    r = c.call("title_rule_upsert", {"process": "Notepad.EXE", "title": " Заметки ",
                                     "insert_mode": None, "style": "business"})
    assert r == {"process": "notepad.exe", "title": "Заметки", "insert_mode": None,
                 "style": "business"}
    r2 = c.call("title_rule_upsert", {"process": "notepad.exe", "title": "ЗАМЕТКИ",
                                      "insert_mode": "type", "style": None})
    assert c.call("title_rules_list") == [r2]                   # тот же ключ — заменено
    star = c.call("title_rule_upsert", {"process": "*", "title": "Блокнот",
                                        "insert_mode": None, "style": "code"})
    assert c.call("title_rules_list") == [star, r2]
    assert c.call("get_settings")["title_rules"] == [r2, star]
    assert "не короче 2" in c.error("title_rule_upsert", {"process": "*", "title": "x"})
    assert "стиль" in c.error("title_rule_upsert", {"process": "*", "title": "xy",
                                                    "style": "poem"})
    assert "title" in c.error("title_rule_upsert", {"process": "*"})
    c.call("title_rule_delete", {"process": "NOTEPAD.exe", "title": "заметки"})
    assert c.call("title_rules_list") == [star]
    c.call("title_rule_delete", {"process": "*", "title": "нет такого"})   # не ошибка
    c.close()


def test_title_rule_priority_in_pipeline(env: Env) -> None:
    c = env.client()
    _enable_ai(c)
    c.call("app_rule_upsert", {"process": "notepad.exe", "insert_mode": None,
                               "style": "business"})
    c.call("title_rule_upsert", {"process": "*", "title": "заметки", "insert_mode": None,
                                 "style": "messenger"})
    c.mark()
    env.dictate("надо проверить отчёт по продажам")
    c.wait_event("history")
    assert env.calls.cleanup[-1][1] == "messenger"                # название окна важнее
    c.call("title_rule_upsert", {"process": "*", "title": "заметки", "insert_mode": None,
                                 "style": None})
    c.mark()
    env.dictate("надо проверить отчёт по продажам")
    c.wait_event("history")
    assert env.calls.cleanup[-1][1] == "business"                 # null — правило процесса
    c.close()


def test_recent_apps_keep_last_titles(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    wins = {i: winutil.WindowInfo(i, "Chrome_WidgetWin_1", f"Вкладка {i % 12}", 50,
                                  "chrome.exe") for i in range(1, 15)}
    wins[99] = TARGET
    monkeypatch.setattr(winutil, "window_info", lambda hwnd: wins[hwnd])
    c = env.client()
    for h in [*range(1, 15), 99]:
        env.eng._on_foreground(h)
    apps = c.call("recent_apps")
    assert apps[0] == {"process": "notepad.exe", "title": TARGET.title,
                       "titles": [TARGET.title]}
    chrome = apps[1]
    assert chrome["process"] == "chrome.exe" and chrome["title"] == "Вкладка 2"
    assert chrome["titles"] == [f"Вкладка {i}" for i in (2, 1, 0, 11, 10, 9, 8, 7, 6, 5)]
    c.close()


# ---------- рассылка только в отмеченные сессии ----------

def test_recording_event_lists_targets(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    other = winutil.WindowInfo(5252, "TscShellContainerClass",
                               "srv2 - Remote Desktop Connection", 1, "mstsc.exe")
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda c, p: [RDP_WIN, other])
    c = env.client()
    c.call("rdp_toggle", {"key": "SRV1", "included": True})
    c.mark()
    env.eng._on_actions([Action(Kind.START), Action(Kind.BROADCAST)])
    ev = c.wait_event("recording")
    assert ev["broadcast"] is True and ev["targets"] == ["srv1"]
    env.eng._on_actions([Action(Kind.CANCEL)])
    c.wait_event("state", lambda d: d["state"] == "idle")
    c.mark()
    env.eng._on_actions([Action(Kind.START)])
    ev = c.wait_event("recording")
    assert ev["broadcast"] is False and ev["targets"] == []
    env.eng._on_actions([Action(Kind.CANCEL)])
    c.close()


@pytest.mark.parametrize("selected", [[], ["closed-srv"]])
def test_broadcast_without_selected_sessions(env: Env, monkeypatch: pytest.MonkeyPatch,
                                             selected: list[str]) -> None:
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda c, p: [RDP_WIN])
    c = env.client()
    for k in selected:
        c.call("rdp_toggle", {"key": k, "included": True})
    c.mark()
    env.dictate("во все сессии", Kind.BROADCAST)
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert n["text"] == "Не выбраны RDP-сессии для рассылки"
    assert n["action"] == "open_settings_insert"
    hist = c.wait_event("history")
    assert env.calls.delivered == [] and hist["target"] == "rdp_all"
    assert env.eng.last_text == "во все сессии"            # не потерян: «Вставить последнее»
    c.close()


def test_test_record_recording_event_has_empty_targets(env: Env) -> None:
    c = env.client()
    c.mark()
    c.call("test_record_start")
    assert c.wait_event("recording")["targets"] == []
    c.call("test_record_stop", timeout=20)
    c.close()


# ---------- личный словарь частот ----------

def _say(env: Env, c: Any, text: str, *mods: Kind) -> dict[str, Any]:
    c.mark()
    env.dictate(text, *mods)
    return dict(c.wait_event("history"))


def test_vocab_list_exclude_and_prompt(env: Env) -> None:
    c = env.client()
    for _ in range(2):
        _say(env, c, "Задеплой в Kubernetes и проверь Airflow")
    assert c.call("vocab_list") == [{"word": "Airflow", "count": 2, "excluded": False},
                                    {"word": "Kubernetes", "count": 2, "excluded": False}]
    assert c.call("vocab_list", {"process": "NOTEPAD.EXE", "limit": 1}) == [
        {"word": "Airflow", "count": 2, "excluded": False}]
    assert c.call("vocab_list", {"process": "telegram.exe"}) == []
    _say(env, c, "третья фраза")
    prompt = env.rec.prompts[-1] or ""
    assert "Kubernetes" in prompt and "Airflow" in prompt
    assert c.call("vocab_exclude", {"word": "AIRFLOW", "excluded": True}) is None
    assert {"word": "Airflow", "count": 2, "excluded": True} in c.call("vocab_list")
    _say(env, c, "четвёртая фраза")
    prompt = env.rec.prompts[-1] or ""
    assert "Kubernetes" in prompt and "Airflow" not in prompt
    c.call("set_settings", {"patch": {"vocab_enabled": False}})
    _say(env, c, "пятая фраза")
    assert "Kubernetes" not in (env.rec.prompts[-1] or "")
    assert "пуст" in c.error("vocab_exclude", {"word": " ", "excluded": True})
    assert "true или false" in c.error("vocab_exclude", {"word": "x", "excluded": 1})
    assert "строкой" in c.error("vocab_list", {"process": 5})
    assert "отрицательным" in c.error("vocab_list", {"limit": -1})
    c.close()


def test_vocab_terms_go_to_ai_prompt(env: Env) -> None:
    c = env.client()
    for _ in range(2):
        _say(env, c, "Обнови дашборд в Metabase и напиши Тимуру")
    _enable_ai(c)
    _say(env, c, "обнови ещё раз дашборд и витрину пожалуйста")
    terms = env.calls.cleanup[-1][2]
    assert "Metabase" in terms and "Тимуру" in terms
    assert "дашборд" not in terms                  # слабые (строчные) — только в Whisper
    c.close()


# ---------- словарь — последнее слово ----------

def _xenoposter_in_history(env: Env, c: Any) -> None:
    """Ошибка распознавания попала в историю до словаря и стала «частым словом»,
    потом пользователь занёс её в словарь как вариант термина."""
    for _ in range(3):
        _say(env, c, "Открой шаблон в Xenoposter")
    assert "Xenoposter" in (env.rec.prompts[-1] or "")        # уже подсказывается Whisper
    c.call("dict_upsert", {"term": "Zennoposter", "variants": ["Xenoposter"]})


def test_dictionary_beats_ai_and_frequent_words(env: Env) -> None:
    """ИИ вернул вариант-ошибку («Xenoposter») — вставляется термин; сама ошибка больше
    не подсказывается ни Whisper, ни ИИ."""
    c = env.client()
    _xenoposter_in_history(env, c)
    _enable_ai(c)
    env.calls.cleanup_impl = lambda t: "Открой шаблон в Xenoposter и проверь прокси."
    env.calls.delivered.clear()
    hist = _say(env, c, "ну открой шаблон в Zenobowser и проверь прокси")
    assert env.calls.delivered == [("Открой шаблон в Zennoposter и проверь прокси. ", False)]
    assert hist["text"] == "Открой шаблон в Zennoposter и проверь прокси."
    terms = env.calls.cleanup[-1][2]
    assert "Zennoposter" in terms and "Xenoposter" not in terms
    prompt = env.rec.prompts[-1] or ""
    assert "Zennoposter" in prompt and "Xenoposter" not in prompt
    # ошибка в частых словах осталась (история не переписывается) — её просто не подсказывают
    assert any(v["word"] == "Xenoposter" for v in c.call("vocab_list"))
    c.close()


def test_dictionary_after_ai_in_raw_first_mode(env: Env) -> None:
    c = env.client()
    c.call("dict_upsert", {"term": "Zennoposter", "variants": ["Xenoposter"]})
    _enable_ai(c, ai_mode="raw_first")
    env.calls.cleanup_impl = lambda t: "Открой Xenoposter и проверь."
    hist = _say(env, c, "ну открой зенопостер и проверь")
    assert env.calls.replaced and env.calls.replaced[-1][1] == "Открой Zennoposter и проверь. "
    assert hist["text"] == "Открой Zennoposter и проверь."
    c.close()


def test_window_context_is_corrected_by_dictionary(env: Env) -> None:
    c = env.client()
    c.call("dict_upsert", {"term": "Zennoposter", "variants": ["Xenoposter"]})
    env.calls.context_reader = lambda: (TARGET.pid, "Шаблон для Xenoposter готов, пишу про")
    env.rec.texts.append("прокси")
    _start_and_wait_context(env)
    c.mark()
    env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("history")
    prompt = env.rec.prompts[-1] or ""
    assert prompt.startswith("Шаблон для Zennoposter готов, пишу про")
    assert "Xenoposter" not in prompt
    c.close()


def test_ai_test_applies_dictionary_like_dictation(env: Env) -> None:
    c = env.client()
    c.call("dict_upsert", {"term": "Zennoposter", "variants": ["Xenoposter"]})
    _enable_ai(c)
    env.calls.cleanup_impl = lambda t: t + " и Xenoposter."
    r = c.call("ai_test", {"text": "проверь Xenoposter", "style": "normal"})
    assert env.calls.cleanup[-1][0] == "проверь Zennoposter"
    assert r["text"] == "проверь Zennoposter и Zennoposter."
    c.close()


# ---------- похожие написания (dict_fuzzy) ----------

def test_one_term_is_enough_for_similar_spellings(env: Env) -> None:
    """Термин без вариантов: похожие по звучанию написания заменяются им; выключено —
    работают только точные варианты."""
    c = env.client()
    c.call("dict_upsert", {"term": "ZennoBrowser", "variants": []})
    hist = _say(env, c, "открой Zenobowser и Xena браузер")
    assert env.calls.delivered[-1] == ("открой ZennoBrowser и ZennoBrowser ", False)
    assert hist["raw"] == "открой Zenobowser и Xena браузер"
    c.call("set_settings", {"patch": {"dict_fuzzy": False}})
    _say(env, c, "открой Zenobowser и Xena браузер")
    assert env.calls.delivered[-1] == ("открой Zenobowser и Xena браузер ", False)
    err = c.error("set_settings", {"patch": {"dict_fuzzy": "да"}})
    assert "похожие написания" in err and "true/false" in err
    c.close()


def test_similar_spellings_after_ai_and_out_of_prompts(env: Env) -> None:
    """Ошибка из истории без варианта в словаре: одного термина хватает, чтобы её не
    подсказывать Whisper и ИИ и чтобы исправить её в ответе ИИ."""
    c = env.client()
    for _ in range(3):
        _say(env, c, "Открой шаблон в Xenoposter")
    c.call("dict_upsert", {"term": "Zennoposter", "variants": []})
    _enable_ai(c)
    env.calls.cleanup_impl = lambda t: "Открой шаблон в Xenoposter и проверь прокси."
    hist = _say(env, c, "ну открой шаблон и проверь прокси")
    assert hist["text"] == "Открой шаблон в Zennoposter и проверь прокси."
    assert "Xenoposter" not in env.calls.cleanup[-1][2]
    prompt = env.rec.prompts[-1] or ""
    assert "Zennoposter" in prompt and "Xenoposter" not in prompt
    c.close()


def test_history_disabled_collects_no_vocab(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"history_enabled": False}})
    for _ in range(3):
        c.mark()
        env.dictate("Kubernetes снова")
        c.wait_event("result")
    time.sleep(0.1)
    assert c.call("vocab_list") == []
    c.close()


# ---------- контекст окна ----------

def _start_and_wait_context(env: Env) -> None:
    env.eng._on_actions([Action(Kind.START)])
    wait_until(lambda: env.eng._session is not None and env.eng._session.context_done.is_set())


def test_window_context_goes_first_in_prompt(env: Env) -> None:
    c = env.client()
    env.calls.context_reader = lambda: (TARGET.pid, "Добрый день. Раз. Отчёт по Metabase "
                                                     "готов? Пишу про")
    env.rec.texts.append("витрину по комиссиям")
    _start_and_wait_context(env)
    c.mark()
    env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("history")
    prompt = env.rec.prompts[-1] or ""
    assert prompt.startswith("Отчёт по Metabase готов? Пишу про ClickHouse,")
    assert "Добрый день" not in prompt
    c.close()


def test_context_is_read_in_background_and_never_delays(env: Env) -> None:
    c = env.client()
    started = threading.Event()

    def slow() -> tuple[int, str]:
        started.set()
        time.sleep(0.8)
        return TARGET.pid, "Опоздавший контекст."

    env.calls.context_reader = slow
    c.mark()
    t0 = time.monotonic()
    env.dictate("быстрая фраза")
    c.wait_event("history")
    assert time.monotonic() - t0 < 0.6                   # распознавание не ждало контекст
    assert started.is_set() and "Опоздавший" not in (env.rec.prompts[-1] or "")
    c.close()


def test_context_slower_than_timeout_is_dropped(env: Env) -> None:
    c = env.client()

    def late() -> tuple[int, str]:
        time.sleep(0.45)                                  # > CONTEXT_TIMEOUT_S (0,3 с)
        return TARGET.pid, "Поздний контекст."

    env.calls.context_reader = late
    env.rec.texts.append("фраза")
    _start_and_wait_context(env)
    assert env.eng._session is not None and env.eng._session.context is None
    c.mark()
    env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("history")
    assert "Поздний" not in (env.rec.prompts[-1] or "")
    c.close()


def test_context_not_used_when_window_changed(env: Env) -> None:
    """Контекст прочитан в одном окне, а диктовка закончилась в другом — не подсказываем."""
    c = env.client()
    env.calls.context_reader = lambda: (TARGET.pid, "Текст первого окна.")
    env.rec.texts.append("фраза")
    _start_and_wait_context(env)
    other = winutil.WindowInfo(777, "Chrome_WidgetWin_1", "Другое", 4321, "chrome.exe")
    env.calls.fg = other
    env.calls.deliver_info = other
    c.mark()
    env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("history")
    assert "первого окна" not in (env.rec.prompts[-1] or "")
    c.close()


@pytest.mark.parametrize("case", ["rdp", "disabled", "other_pid", "command"])
def test_context_not_used(env: Env, case: str) -> None:
    c = env.client()
    reads: list[int] = []

    def reader() -> tuple[int, str]:
        reads.append(1)
        return (TARGET.pid + 1 if case == "other_pid" else TARGET.pid), "Текст в поле."

    env.calls.context_reader = reader
    if case == "rdp":
        env.calls.fg = RDP_WIN
        env.calls.deliver_info = RDP_WIN
    if case == "disabled":
        c.call("set_settings", {"patch": {"context_enabled": False}})
    env.rec.texts.append("переведи" if case == "command" else "фраза")
    _start_and_wait_context(env)
    c.mark()
    if case == "command":
        env.eng._on_actions([Action(Kind.COMMAND), Action(Kind.STOP)])
    else:
        env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("history")
    assert "Текст в поле" not in (env.rec.prompts[-1] or "")
    assert (reads == []) == (case in ("rdp", "disabled"))
    c.close()


# ---------- регрессии исправлений ----------

def test_ai_balance_skips_network_when_openrouter_unused(env: Env) -> None:
    """Регрессия: при очистке и командах через Ollama ядро каждые 5 минут ходило в
    OpenRouter с оставшимся ключом и предупреждало «пополните OpenRouter»."""
    c = env.client()
    c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})
    env.calls.balance["balance_usd"] = 0.42
    c.call("set_settings", {"patch": {"ai_provider": "ollama", "command_provider": "ollama"}})
    c.mark()
    r = c.call("ai_balance")
    assert r["balance_usd"] is None and r["checked_at"] is None
    assert "не используются" in r["error"] and env.calls.balance_checks == []
    # и кнопка «Обновить» (force) в сеть не ходит: ключ ни во что не идёт
    assert c.call("ai_balance", {"force": True})["balance_usd"] is None
    time.sleep(0.2)
    assert not [e for e in c.events("notice") if "меньше $1" in e["text"]]
    assert env.calls.balance_checks == []
    c.call("set_settings", {"patch": {"command_provider": "openrouter"}})
    c.call("ai_balance", {"force": True})
    c.wait_event("notice", lambda d: "меньше $1" in d["text"])
    c.close()


def test_asr_prompt_is_measured_by_model_tokenizer(env: Env) -> None:
    """Подсказка меряется токенизатором модели, если распознаватель его даёт (здесь —
    «токен на символ»): ≤ 200 токенов, контекст — не больше CONTEXT_TOKENS."""
    from rupor import vocab
    c = env.client()
    env.rec.token_counter = lambda: len          # type: ignore[attr-defined]
    env.calls.context_reader = lambda: (TARGET.pid, "Очень длинный текст перед курсором. " * 20
                                        + "Пишу про")
    env.rec.texts.append("витрину по комиссиям")
    _start_and_wait_context(env)
    c.mark()
    env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("history")
    prompt = env.rec.prompts[-1] or ""
    assert 0 < len(prompt) <= vocab.PROMPT_TOKENS
    assert "Пишу про" in prompt and len(prompt.split("Пишу про")[0]) < vocab.CONTEXT_TOKENS
    c.close()
