"""Обучение словаря в движке: наблюдение за полем после вставки и команды
dict_learn_accept / dict_learn_undo / dict_learn_journal.

Поле ввода — фейк (FakeUI) вместо UI Automation: тест сам «правит» его текст по сценарию.
Настоящее поле пользователя здесь не читается никогда (см. tests/test_uia.py — там
механизм UIA проверяется на собственном скрытом окне)."""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Iterator

import pytest

from rupor import config, engine
from rupor.engine import Delivery
from rupor.winutil import WindowInfo
from test_api import FAKE_KEY, TARGET, Env, env  # noqa: F401 — фикстура env

RDP_WIN = WindowInfo(5151, "TscShellContainerClass", "srv — Подключение", 777_777, "mstsc.exe")


class FakeUI:
    """Поле с фокусом, как его видит UI Automation. Текст меняет тест."""

    def __init__(self, pid: int = TARGET.pid) -> None:
        self.text = ""
        self.pid = pid
        self.rid: tuple[int, ...] = (42, 7)
        self.focused = True
        self.reads = 0
        self.lookups = 0
        self.lock = threading.Lock()

    def reader(self) -> FakeField | None:
        self.lookups += 1
        return FakeField(self, self.rid, self.pid) if self.focused else None

    def insert(self, text: str) -> None:
        with self.lock:
            self.text += text


@dataclass
class FakeField:
    ui: FakeUI
    runtime_id: tuple[int, ...]
    pid: int
    control_type: int = field(default=50004)

    def read_text(self) -> str | None:
        with self.ui.lock:
            self.ui.reads += 1
            return self.ui.text if self.ui.focused else None


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("условие не выполнилось")
        time.sleep(0.01)


@pytest.fixture
def lenv(env: Env, monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[Env, FakeUI]]:  # noqa: F811
    monkeypatch.setattr(engine, "LEARN_POLL_S", 0.02)
    monkeypatch.setattr(engine, "LEARN_POLL_FAST_S", 0.02)
    monkeypatch.setattr(engine, "LEARN_BASELINE_POLL_S", 0.01)
    monkeypatch.setattr(engine, "LEARN_WATCH_S", 10.0)
    # Устойчивость правки во времени здесь не проверяется (см. раздел «недопечатанное слово»)
    monkeypatch.setattr(engine, "LEARN_MIN_HELD_S", 0.0)
    ui = FakeUI()
    env.calls.field_reader = ui.reader
    env.calls.on_text = ui.insert
    yield env, ui


def dictated(env: Env, ui: FakeUI, text: str) -> None:
    """Продиктовать и дождаться, пока наблюдение сняло основу (текст уже в поле)."""
    n = len(env.calls.delivered)
    reads = ui.reads
    env.dictate(text)
    wait_until(lambda: len(env.calls.delivered) > n and ui.reads > reads
               and env.eng._learn is not None)


def edit(env: Env, ui: FakeUI, new_text: str, keys: bool = True) -> None:
    """Пользователь правит поле (keys — нажимая клавиши); ждём, пока наблюдение это увидит."""
    if keys:
        env.eng.machine.note_input()
    with ui.lock:
        ui.text = new_text
    r = ui.reads
    wait_until(lambda: ui.reads >= r + 2)


def watch_done(env: Env) -> None:
    wait_until(lambda: env.eng._learn is None)


# ---------- режим auto ----------

def test_auto_adds_correction_and_undo(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    ui.text = "Итог: "
    dictated(env, ui, "запусти в кубернетис")
    assert ui.text == "Итог: запусти в кубернетис "
    edit(env, ui, "Итог: запусти в Kubernetes ")
    c.mark()
    ui.focused = False                       # фокус ушёл с поля — конец наблюдения
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["text"] == "В словарь добавлено: «кубернетис» → Kubernetes"
    assert n["level"] == "info" and n["toast"] is True
    data = n["data"]
    assert data["term"] == "Kubernetes" and data["variant"] == "кубернетис"
    assert data["created"] is True and isinstance(data["id"], int)
    assert c.call("dict_list") == [{"id": data["id"], "term": "Kubernetes",
                                    "variants": ["кубернетис"]}]
    watch_done(env)
    # Подсказка распознаванию считается перед каждой диктовкой — термин уже в ней
    ui.focused = True
    env.dictate("следующая фраза")
    wait_until(lambda: len(env.rec.prompts) >= 2)
    assert "Kubernetes" in (env.rec.prompts[-1] or "")
    assert "Kubernetes" not in (env.rec.prompts[0] or "")
    # «Отменить» — термин создан этой правкой, удаляется целиком
    assert c.call("dict_learn_undo", data) is None
    assert c.call("dict_list") == []
    c.close()


def test_undone_correction_is_not_learned_again(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    for round_ in range(2):
        ui.focused, ui.text = True, ""
        dictated(env, ui, "открой метабейс")
        edit(env, ui, "открой Metabase ")
        c.mark()
        ui.focused = False
        if round_ == 0:
            n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
            c.call("dict_learn_undo", n["data"])
        watch_done(env)
    assert c.call("dict_list") == []
    assert not [e for e in c.events("notice") if e.get("action") == "dict_undo"][1:]
    c.close()


def test_telegram_send_clears_field_previous_snapshot_is_used(
        lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "клик хаус упал")
    edit(env, ui, "ClickHouse упал ")
    c.mark()
    env.eng.machine.note_input()              # Enter: сообщение ушло, поле пустое
    with ui.lock:
        ui.text = ""
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["data"]["term"] == "ClickHouse" and n["data"]["variant"] == "клик хаус"
    assert n["text"] == "В словарь добавлено: «клик хаус» → ClickHouse"
    watch_done(env)
    c.close()


def test_case_only_correction_adds_term_without_variant(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "упала база clickhouse")
    edit(env, ui, "упала база ClickHouse ")
    c.mark()
    ui.focused = False
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["text"] == "В словарь добавлено: ClickHouse"
    assert n["data"]["variant"] == ""
    c.close()


def test_no_keypresses_nothing_learned(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "проверь метабейс")
    edit(env, ui, "проверь Metabase ", keys=False)   # текст сменился без нажатий (не мы)
    ui.focused = False
    watch_done(env)
    assert c.call("dict_list") == []
    assert not [e for e in c.events("notice") if e.get("data")]
    c.close()


def test_meaning_edit_is_not_learned(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "приду завтра вечером")
    edit(env, ui, "приду послезавтра вечером ")
    ui.focused = False
    watch_done(env)
    assert c.call("dict_list") == []
    c.close()


def test_rdp_window_is_not_watched(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    env.calls.fg = RDP_WIN
    env.calls.deliver_info = RDP_WIN
    env.dictate("проверь метабейс")
    wait_until(lambda: len(env.calls.delivered) == 1)
    time.sleep(0.1)
    assert ui.lookups == 0 and env.eng._learn is None


def test_off_mode_does_not_watch(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    assert c.call("set_settings", {"patch": {"learn_mode": "off"}})["learn_mode"] == "off"
    env.dictate("проверь метабейс")
    wait_until(lambda: len(env.calls.delivered) == 1)
    time.sleep(0.1)
    assert ui.lookups == 0
    assert "Учиться на ваших правках" in c.error("set_settings",
                                                 {"patch": {"learn_mode": "always"}})
    c.close()


def test_other_process_field_is_not_read(lenv: tuple[Env, FakeUI]) -> None:
    """Фокус в поле другого процесса (не того, куда вставляли) — текст не читается."""
    env, ui = lenv
    ui.pid = 12345
    env.dictate("проверь метабейс")
    wait_until(lambda: ui.lookups >= 3)
    assert ui.reads == 0


def test_new_dictation_ends_watch_and_evaluates(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "открой графану")
    edit(env, ui, "открой Grafana ")
    c.mark()
    env.dictate("вторая фраза")               # старт новой диктовки — оценка прошлой правки
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["data"]["term"] == "Grafana" and n["data"]["variant"] == "графана"
    c.close()


def test_watch_timeout_evaluates(lenv: tuple[Env, FakeUI],
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    """Минута прошла, фокус в поле — оценка по последнему снимку (печать в этот момент
    продлевает наблюдение, чтобы не взять недопечатанное слово)."""
    env, ui = lenv
    monkeypatch.setattr(engine, "LEARN_WATCH_S", 0.3)
    monkeypatch.setattr(engine, "LEARN_SETTLE_S", 0.1)
    c = env.client()
    dictated(env, ui, "открой графана дашборд")
    edit(env, ui, "открой Grafana дашборд ")
    c.mark()
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["data"]["term"] == "Grafana"
    watch_done(env)
    c.close()


def test_pause_cancels_watch(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "проверь метабейс")
    edit(env, ui, "проверь Metabase ")
    c.call("pause", {"on": True})
    watch_done(env)
    assert c.call("dict_list") == []
    c.close()


# ---------- режим suggest ----------

def test_suggest_then_second_time_adds(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    c.call("set_settings", {"patch": {"learn_mode": "suggest"}})
    for round_ in range(2):
        ui.focused, ui.text = True, ""
        dictated(env, ui, "проверь метабейс")
        edit(env, ui, "проверь Metabase ")
        c.mark()
        ui.focused = False
        n = c.wait_event("notice", lambda d: d.get("action") in ("dict_add", "dict_undo"))
        if round_ == 0:
            assert n["action"] == "dict_add"
            assert n["text"] == "Добавить в словарь: «метабейс» → Metabase?"
            assert n["data"] == {"term": "Metabase", "variant": "метабейс", "id": None,
                                 "created": False}
            assert n["level"] == "info" and n["toast"] is True
            assert c.call("dict_list") == []
        else:
            assert n["action"] == "dict_undo"               # та же правка второй раз — сама
            assert n["text"] == "В словарь добавлено: «метабейс» → Metabase"
            assert n["data"]["created"] is True
        watch_done(env)
    assert [d["term"] for d in c.call("dict_list")] == ["Metabase"]
    c.close()


def test_suggest_accept_button(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    c.call("set_settings", {"patch": {"learn_mode": "suggest"}})
    dictated(env, ui, "проверь метабейс")
    edit(env, ui, "проверь Metabase ")
    c.mark()
    ui.focused = False
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_add")
    item = c.call("dict_learn_accept", {"term": n["data"]["term"],
                                        "variant": n["data"]["variant"]})
    assert item["term"] == "Metabase" and item["variants"] == ["метабейс"]
    c.close()


# ---------- «сначала сырой»: правки ИИ — не правки пользователя ----------

def test_raw_first_ai_replacement_is_not_learned(lenv: tuple[Env, FakeUI],
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    env, ui = lenv
    calls = env.calls
    calls.creds["Rupor/openrouter"] = FAKE_KEY
    env.eng.update_settings({"ai_keys": [{"id": "openrouter", "provider": "openrouter"}]})
    calls.cleanup_impl = lambda t: "Задеплой в Kubernetes и метабейс."

    def replace(n: int, text: str, d: Delivery, s: config.Settings, expected: str) -> bool:
        with ui.lock:
            ui.text = ui.text.replace(expected, text)
        calls.replaced.append((n, text))
        return True

    monkeypatch.setattr(engine, "replace_text", replace)
    c = env.client()
    c.call("set_settings", {"patch": {"ai_enabled": True, "ai_mode": "raw_first",
                                      "ai_min_words": 0}})
    dictated(env, ui, "задеплой в кубернетис и метабейс")
    wait_until(lambda: bool(calls.replaced) and "Kubernetes" in ui.text)
    r = ui.reads
    wait_until(lambda: ui.reads >= r + 3)          # наблюдение взяло новую основу
    edit(env, ui, "Задеплой в Kubernetes и Metabase. ")
    c.mark()
    ui.focused = False
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["data"]["term"] == "Metabase"
    watch_done(env)
    assert [d["term"] for d in c.call("dict_list")] == ["Metabase"]   # Kubernetes — правка ИИ
    c.close()


# ---------- команды dict_learn_accept / dict_learn_undo ----------

def test_learn_accept_merges_and_validates(env: Env) -> None:  # noqa: F811
    c = env.client()
    d = c.call("dict_upsert", {"term": "Metabase", "variants": ["мета бейс"]})
    got = c.call("dict_learn_accept", {"term": "metabase", "variant": "метабейс"})
    assert got == {"id": d["id"], "term": "Metabase", "variants": ["мета бейс", "метабейс"]}
    again = c.call("dict_learn_accept", {"term": "METABASE", "variant": "Метабейс"})
    assert again["variants"] == ["мета бейс", "метабейс"]            # без дублей
    only_term = c.call("dict_learn_accept", {"term": "Grafana"})
    assert only_term["term"] == "Grafana" and only_term["variants"] == []
    same_case = c.call("dict_learn_accept", {"term": "ClickHouse", "variant": "clickhouse"})
    assert same_case["variants"] == []                                # вариант = сам термин
    assert "term" in c.error("dict_learn_accept", {})
    assert "пуст" in c.error("dict_learn_accept", {"term": "  "})
    assert "variant" in c.error("dict_learn_accept", {"term": "X", "variant": 5})
    err = c.error("dict_learn_accept", {"term": "Metabaza", "variant": "метабейс"})
    assert "уже относится" in err and "Metabase" in err
    assert len(c.call("dict_list")) == 3
    c.close()


def test_learn_undo_contract(env: Env) -> None:  # noqa: F811
    c = env.client()
    base = c.call("dict_upsert", {"term": "Metabase", "variants": ["мета бейс"]})
    got = c.call("dict_learn_accept", {"term": "Metabase", "variant": "метабейс"})
    # created=false — убрать только этот вариант, термин остаётся
    c.call("dict_learn_undo", {"term": "Metabase", "variant": "метабейс", "id": got["id"],
                               "created": False})
    assert c.call("dict_list") == [base]
    # created=true — удалить термин целиком (id неверный — ищем по термину)
    c.call("dict_learn_accept", {"term": "Kubernetes", "variant": "кубернетис"})
    c.call("dict_learn_undo", {"term": "kubernetes", "variant": "кубернетис", "id": 99999,
                               "created": True})
    assert [d["term"] for d in c.call("dict_list")] == ["Metabase"]
    # уже удалённое — не ошибка
    assert c.call("dict_learn_undo", {"term": "Nope", "variant": "", "id": None,
                                      "created": True}) is None
    assert "created" in c.error("dict_learn_undo", {"term": "Metabase", "variant": ""})
    assert "true или false" in c.error("dict_learn_undo", {"term": "Metabase",
                                                           "created": "да"})
    assert "целым" in c.error("dict_learn_undo", {"term": "Metabase", "id": "x",
                                                  "created": False})
    c.close()


# ---------- команда dict_learn_journal («Недавно добавлено» на странице «Словарь») ----------

def test_learn_journal_shows_auto_and_accepted(lenv: tuple[Env, FakeUI]) -> None:
    env, ui = lenv
    c = env.client()
    t0 = time.time()
    dictated(env, ui, "запусти в кубернетис")
    edit(env, ui, "запусти в Kubernetes ")
    c.mark()
    ui.focused = False
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    watch_done(env)
    c.call("dict_learn_accept", {"term": "Grafana", "variant": "графана"})
    got = c.call("dict_learn_journal")
    assert [(j["term"], j["variant"], j["created"], j["source"]) for j in got] == [
        ("Grafana", "графана", True, "accepted"),         # новые — первыми
        ("Kubernetes", "кубернетис", True, "auto")]
    assert set(got[0]) == {"id", "term", "variant", "created", "ts", "source"}
    assert got[1]["id"] == n["data"]["id"]
    assert all(t0 - 1 <= j["ts"] <= time.time() + 1 for j in got)
    assert c.call("dict_learn_journal", {"limit": 1}) == got[:1]
    assert c.call("dict_learn_journal", {"limit": 0}) == []
    # «Отменить» в строке — та же dict_learn_undo: строка пропадает вместе с термином
    row = got[0]
    assert c.call("dict_learn_undo", {k: row[k] for k in ("term", "variant", "id",
                                                          "created")}) is None
    assert [j["term"] for j in c.call("dict_learn_journal")] == ["Kubernetes"]
    assert [d["term"] for d in c.call("dict_list")] == ["Kubernetes"]
    c.close()


def test_learn_journal_limit_and_changed_terms(env: Env) -> None:  # noqa: F811
    c = env.client()
    a = c.call("dict_learn_accept", {"term": "Metabase", "variant": "метабейс"})
    b = c.call("dict_learn_accept", {"term": "Grafana", "variant": "графана"})
    c.call("dict_learn_accept", {"term": "Jira", "variant": "джира"})
    assert len(c.call("dict_learn_journal", {"limit": 300})) == 3
    # переименованный и удалённый вручную термины — отменять нечего, в списке их нет
    c.call("dict_upsert", {"id": b["id"], "term": "Kibana", "variants": ["графана"]})
    c.call("dict_delete", {"id": a["id"]})
    assert [j["term"] for j in c.call("dict_learn_journal")] == ["Jira"]
    for bad in (-1, 301):
        assert "от 0 до 300" in c.error("dict_learn_journal", {"limit": bad})
    for bad in ("10", 2.5, True):
        assert "целым" in c.error("dict_learn_journal", {"limit": bad})
    c.close()


def test_plain_notice_has_null_data(env: Env) -> None:  # noqa: F811
    c = env.client()
    c.call("get_state")                       # клиент уже в списке рассылки
    c.mark()
    env.eng.notice("Просто уведомление", toast=True)
    n = c.wait_event("notice")
    assert "data" in n and n["data"] is None
    c.close()


def test_learn_mode_setting_default() -> None:
    assert config.Settings().learn_mode == "auto"
    s = config.Settings(learn_mode="sometimes")
    s.validate()
    assert s.learn_mode == "auto"


# ---------- неуверенная правка: в auto — сразу с «Отменить», в suggest — вопрос ----------

def test_auto_unsure_correction_is_added_with_undo(lenv: tuple[Env, FakeUI]) -> None:
    """«петя» → «Петя» (только заглавная у русского слова) — может быть и оформлением, но
    в auto добавляется сразу, как любая правка: пользователь видит «В словарь добавлено …
    [Отменить]», а не вопрос, который пропадает."""
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "это сказал петя вчера")
    edit(env, ui, "это сказал Петя вчера ")
    c.mark()
    ui.focused = False
    n = c.wait_event("notice", lambda d: d.get("action") in ("dict_add", "dict_undo"))
    assert n["action"] == "dict_undo" and n["text"] == "В словарь добавлено: Петя"
    assert n["data"]["created"] is True and isinstance(n["data"]["id"], int)
    watch_done(env)
    assert [d["term"] for d in c.call("dict_list")] == ["Петя"]
    assert not [e for e in c.events("notice") if e.get("action") == "dict_add"]
    c.close()


def test_suggest_unsure_correction_is_asked_then_added(lenv: tuple[Env, FakeUI]) -> None:
    """Режим suggest спрашивает и про неуверенную правку; та же правка второй раз — сама."""
    env, ui = lenv
    c = env.client()
    c.call("set_settings", {"patch": {"learn_mode": "suggest"}})
    for round_ in range(2):
        ui.focused, ui.text = True, ""
        dictated(env, ui, "это сказал петя вчера")
        edit(env, ui, "это сказал Петя вчера ")
        c.mark()
        ui.focused = False
        n = c.wait_event("notice", lambda d: d.get("action") in ("dict_add", "dict_undo"))
        if round_ == 0:
            assert n["action"] == "dict_add" and n["text"] == "Добавить в словарь: Петя?"
            assert c.call("dict_list") == []
            assert c.call("dict_learn_journal") == []
        else:
            assert n["action"] == "dict_undo" and n["text"] == "В словарь добавлено: Петя"
        watch_done(env)
    assert [d["term"] for d in c.call("dict_list")] == ["Петя"]
    assert [(j["term"], j["source"]) for j in c.call("dict_learn_journal")] == [
        ("Петя", "auto")]                                   # добавил Рупор (на повторе)
    c.close()


def test_grammar_edit_is_not_learned(lenv: tuple[Env, FakeUI]) -> None:
    """Находка 1: падеж имени — правка грамматики, не распознавания."""
    env, ui = lenv
    c = env.client()
    dictated(env, ui, "передай Наташа что всё готово")
    edit(env, ui, "передай Наташе что всё готово ")
    ui.focused = False
    watch_done(env)
    assert c.call("dict_list") == []
    assert not [e for e in c.events("notice") if e.get("data")]
    c.close()


# ---------- недопечатанное слово: Enter, Alt+Tab или хоткей сразу после правки ----------

@pytest.fixture
def tenv(lenv: tuple[Env, FakeUI], monkeypatch: pytest.MonkeyPatch) -> tuple[Env, FakeUI]:
    """Устойчивость правки включена (масштаб времени меньше боевого): правка младше 0,1 с
    не учится."""
    monkeypatch.setattr(engine, "LEARN_MIN_HELD_S", 0.1)
    return lenv


def _type_word(env: Env, ui: FakeUI, prefix: str, word: str, gap: float) -> None:
    for i in range(1, len(word) + 1):
        env.eng.machine.note_input()
        with ui.lock:
            ui.text = prefix + word[:i] + " "
        time.sleep(gap)


def _clear(env: Env, ui: FakeUI) -> None:
    env.eng.machine.note_input()             # Enter: Telegram отправил, поле пустое
    with ui.lock:
        ui.text = ""


def test_truncated_word_on_fast_enter_is_not_learned(tenv: tuple[Env, FakeUI]) -> None:
    """Находки 3 и 11: слово допечатано и сразу Enter — последний снимок опроса мог застать
    «ClickHou». Недопечатанное в словарь не попадает ни в одной фазе опроса."""
    env, ui = tenv
    c = env.client()
    for k in range(4):
        ui.focused, ui.text = True, ""
        dictated(env, ui, "данные в клик хаус")
        time.sleep(0.005 * k)                # разная фаза относительно опроса
        _type_word(env, ui, "данные в ", "ClickHouse", gap=0.02)
        _clear(env, ui)
        watch_done(env)
    learned = {d["term"] for d in c.call("dict_list")}
    shown = {e["data"]["term"] for e in c.events("notice") if e.get("data")}
    assert learned <= {"ClickHouse"} and shown <= {"ClickHouse"}, (learned, shown)
    c.close()


def test_short_hold_is_added_in_auto(tenv: tuple[Env, FakeUI]) -> None:
    """Правка продержалась чуть дольше LEARN_MIN_HELD_S и сразу Enter — в auto она уже
    добавляется (с «Отменить»), а не только предлагается."""
    env, ui = tenv
    c = env.client()
    dictated(env, ui, "проверь метабейс")
    c.mark()
    env.eng.machine.note_input()
    with ui.lock:
        ui.text = "проверь Metabase "
    time.sleep(0.3)                          # ≥ 0,1 с — и Enter
    _clear(env, ui)
    n = c.wait_event("notice", lambda d: d.get("action") in ("dict_add", "dict_undo"))
    assert n["action"] == "dict_undo"
    assert n["data"]["term"] == "Metabase" and n["data"]["variant"] == "метабейс"
    watch_done(env)
    assert [d["term"] for d in c.call("dict_list")] == ["Metabase"]
    c.close()


def test_long_hold_is_added_in_auto(tenv: tuple[Env, FakeUI]) -> None:
    env, ui = tenv
    c = env.client()
    dictated(env, ui, "проверь метабейс")
    c.mark()
    env.eng.machine.note_input()
    with ui.lock:
        ui.text = "проверь Metabase "
    time.sleep(0.8)
    ui.focused = False                       # Alt+Tab
    n = c.wait_event("notice", lambda d: d.get("action") in ("dict_add", "dict_undo"))
    assert n["action"] == "dict_undo" and n["data"]["term"] == "Metabase"
    watch_done(env)
    c.close()


def test_new_dictation_rereads_field(lenv: tuple[Env, FakeUI],
                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """Правка после последнего опроса, сразу хоткей: поле перечитывается перед оценкой."""
    env, ui = lenv
    monkeypatch.setattr(engine, "LEARN_POLL_S", 30.0)          # опросов после основы нет
    monkeypatch.setattr(engine, "LEARN_POLL_FAST_S", 30.0)
    c = env.client()
    dictated(env, ui, "открой графану")
    reads = ui.reads
    env.eng.machine.note_input()
    with ui.lock:
        ui.text = "открой Grafana "
    c.mark()
    env.dictate("вторая фраза")
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    assert n["data"]["term"] == "Grafana" and ui.reads > reads
    c.close()


def test_log_has_no_words_from_field(lenv: tuple[Env, FakeUI],
                                     caplog: pytest.LogCaptureFixture) -> None:
    """Находка 9: ни термин, ни вариант (слова из переписки) в лог не попадают."""
    env, ui = lenv
    caplog.set_level(logging.DEBUG)
    c = env.client()
    dictated(env, ui, "запусти в кубернетис")
    edit(env, ui, "запусти в Kubernetes ")
    c.mark()
    ui.focused = False
    n = c.wait_event("notice", lambda d: d.get("action") == "dict_undo")
    c.call("dict_learn_undo", n["data"])
    c.call("dict_learn_accept", {"term": "Kubernetes", "variant": "кубернетис"})
    watch_done(env)
    # (отладочный лог клиента websockets — это тестовый клиент, не ядро)
    text = "\n".join(r.getMessage() for r in caplog.records if r.name.startswith("rupor"))
    assert "Обучение словаря" in text and "правка словаря" in text
    assert "Kubernetes" not in text and "кубернетис" not in text.casefold()
    c.close()
