"""WebSocket-протокол ядра: настоящий сервер и настоящий WS-клиент, движок без хука,
без модели и без микрофона (фейковые Recognizer/Recorder).

Безопасность: вставка (deliver/replace_text/read_selection), активное окно, Credential
Manager и открытие проводника подменены — тесты ничего не печатают в окна пользователя
и не трогают его ключи; настройки, база и логи — во временной папке (APPDATA)."""
from __future__ import annotations

import base64
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np
import pytest

from rupor.hotkeys import parse_keyset
from websockets.exceptions import ConnectionClosed
from websockets.sync.client import ClientConnection, connect

from rupor import ai, api, config, credman, engine, fetch, textproc, winutil
from rupor.asr import Result
from rupor.engine import Delivery, Engine
from rupor.hotkeys import Action, Kind
from rupor.server import WsServer
from rupor.storage import Storage

TOKEN = "0123456789abcdef0123456789abcdef"
FAKE_KEY = "sk-or-v1-" + "f" * 61 + "XYZ"  # синтетический ключ
TARGET = winutil.WindowInfo(4242, "Notepad", "Заметки — Блокнот", 999_999, "notepad.exe")


# ---------- фейки ----------

class FakeRecorder:
    def __init__(self) -> None:
        self.monitor = False
        self.recording = False
        self.opened = 0
        self.seconds = 1.0

    def open(self) -> None:
        self.opened += 1

    def close(self) -> None:
        pass

    def start(self) -> None:
        self.recording = True

    def snapshot(self) -> np.ndarray:
        return np.zeros(8000, np.float32)

    def stop(self) -> np.ndarray:
        self.recording = False
        return np.zeros(int(16000 * self.seconds), np.float32)


class FakeRecognizer:
    def __init__(self) -> None:
        self.device: str | None = "cuda"
        self.last_error: str | None = None
        self.last_used = time.monotonic()
        self.prompt_provider: Callable[[], str | None] = lambda: None
        self.texts: list[str] = []
        self.prompts: list[str | None] = []
        self._loaded = True

    @property
    def loaded(self) -> bool:
        return self._loaded

    def load(self) -> None:
        self._loaded = True

    def unload(self) -> None:
        self._loaded = False

    def detect_language(self, audio: np.ndarray) -> str:
        return "ru"

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> Result:
        self.prompts.append(self.prompt_provider())
        text = self.texts.pop(0) if self.texts else ""
        return Result(text, language or "ru", len(audio) / 16000, 12.0)


class Calls:
    """Что движок пытался вставить/заменить/прочитать (вместо настоящего SendInput)."""

    def __init__(self) -> None:
        self.delivered: list[tuple[str, bool]] = []
        self.replaced: list[tuple[int, str]] = []
        self.selection: str | None = "привет, как дела"
        self.selections: list[str | None] = []   # по очереди при каждом чтении, затем selection
        self.selection_ok = True                  # replace_text: выделено то, что вставлено
        self.cleanup: list[tuple[str, str, list[str]]] = []
        self.cleanup_impl: Callable[[str], str] = lambda t: t
        self.commands: list[tuple[str, str]] = []
        self.command_out = "Hello, how are you"
        self.command_hook: Callable[[], None] = lambda: None
        self.opened: list[str] = []
        self.creds: dict[str, str] = {}
        self.event = threading.Event()
        self.fg: winutil.WindowInfo | None = TARGET  # активное окно
        self.focus: int | None = 7                   # фокус ввода внутри окна
        self.on_deliver: Callable[[], None] = lambda: None
        self.on_text: Callable[[str], None] = lambda text: None   # вставленный текст «в поле»
        self.deliver_info: winutil.WindowInfo | None = None  # куда «вставлено» (None — TARGET)
        # Поле с фокусом для обучения словаря (вместо UI Automation): по умолчанию его нет —
        # тесты никогда не читают настоящее поле пользователя
        self.field_reader: Callable[[], Any] = lambda: None
        # Текст перед курсором для подсказки (вместо UI Automation): по умолчанию нет
        self.context_reader: Callable[[], tuple[int, str] | None] = lambda: None
        # Проверка ключа OpenRouter (баланс) — без сети
        self.balance: dict[str, Any] = {"valid": True, "label": None, "balance_usd": 12.34,
                                        "usage_usd": 1.0, "limit_usd": None, "error": None}
        self.balance_checks: list[str] = []

    def read_selection(self) -> str | None:
        return self.selections.pop(0) if self.selections else self.selection


# ---------- WS-клиент ----------

class Client:
    def __init__(self, url: str) -> None:
        self.ws: ClientConnection = connect(url, open_timeout=5, max_size=None, legacy=True)
        self.msgs: list[dict[str, Any]] = []
        self.raw: list[str] = []
        self._id = 0
        self._seen = 0

    def _recv(self, timeout: float) -> dict[str, Any]:
        text = self.ws.recv(timeout=timeout)
        assert isinstance(text, str)
        self.raw.append(text)
        msg: dict[str, Any] = json.loads(text)
        self.msgs.append(msg)
        return msg

    def call_full(self, cmd: str, args: Any = None, timeout: float = 10) -> dict[str, Any]:
        self._id += 1
        rid = self._id
        req: dict[str, Any] = {"id": rid, "cmd": cmd}
        if args is not None:
            req["args"] = args
        self.ws.send(json.dumps(req, ensure_ascii=False))
        deadline = time.monotonic() + timeout
        while True:
            for m in self.msgs:
                if m.get("id") == rid:
                    return m
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"нет ответа на {cmd}")
            self._recv(left)

    def call(self, cmd: str, args: Any = None, timeout: float = 10) -> Any:
        r = self.call_full(cmd, args, timeout)
        assert r["ok"] is True, r
        return r["result"]

    def error(self, cmd: str, args: Any = None) -> str:
        r = self.call_full(cmd, args)
        assert r["ok"] is False, r
        assert isinstance(r["error"], str) and r["error"]
        return str(r["error"])

    def events(self, name: str) -> list[dict[str, Any]]:
        return [m["data"] for m in self.msgs if m.get("event") == name]

    def wait_event(self, name: str, pred: Callable[[dict[str, Any]], bool] = lambda d: True,
                   timeout: float = 10, fresh: bool = True) -> dict[str, Any]:
        """Ждать событие name (fresh — только пришедшее после прошлого wait_event)."""
        start = self._seen if fresh else 0
        deadline = time.monotonic() + timeout
        i = start
        while True:
            while i < len(self.msgs):
                m = self.msgs[i]
                i += 1
                if m.get("event") == name and pred(m["data"]):
                    self._seen = i
                    return dict(m["data"])
            left = deadline - time.monotonic()
            if left <= 0:
                raise TimeoutError(f"нет события {name}")
            try:
                self._recv(min(left, 0.5))
            except TimeoutError:
                pass

    def mark(self) -> None:
        self._seen = len(self.msgs)

    def close(self) -> None:
        self.ws.close()


# ---------- окружение ----------

class Env:
    def __init__(self, eng: Engine, srv: WsServer, calls: Calls, rec: FakeRecognizer,
                 recorder: FakeRecorder, tmp: Path) -> None:
        self.eng, self.srv, self.calls, self.rec, self.recorder, self.tmp = (
            eng, srv, calls, rec, recorder, tmp)
        self.url = f"ws://127.0.0.1:{srv.port}/?token={TOKEN}"

    def client(self) -> Client:
        c = Client(self.url)
        c.wait_event("hello", fresh=False)
        return c

    def dictate(self, text: str, *mods: Kind) -> None:
        """Продиктовать фразу: жесты напрямую, «распознаётся» text."""
        self.rec.texts.append(text)
        self.eng._on_actions([Action(Kind.START), *(Action(k) for k in mods), Action(Kind.STOP)])


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Env]:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    calls = Calls()

    def fake_deliver(text: str, s: config.Settings, to_all: bool,
                     notice: Callable[..., None]) -> Delivery:
        calls.on_deliver()
        calls.on_text(text)
        calls.delivered.append((text, to_all))
        calls.event.set()
        info = calls.deliver_info or TARGET
        return Delivery(ok=True, hwnd=info.hwnd, mode="type", info=info,
                        sent=["srv1"] if to_all else [])

    def fake_replace(n: int, text: str, d: Delivery, s: config.Settings,
                     expected: str) -> bool:
        if not calls.selection_ok:  # «выделено не то» (автозамена, ячейки Excel, щелчок)
            return False
        calls.replaced.append((n, text))
        return True

    def fake_cleanup(text: str, style: str, s: config.Settings, terms: list[str],
                     **_kw: Any) -> ai.AIResult:
        calls.cleanup.append((text, style, list(terms)))
        return ai.AIResult(calls.cleanup_impl(text), 5.0, 0.001, "openrouter", "test")

    def fake_command(instruction: str, selected: str, s: config.Settings,
                     **_kw: Any) -> ai.AIResult:
        calls.commands.append((instruction, selected))
        calls.command_hook()
        return ai.AIResult(calls.command_out, 7.0, None, "openrouter", "test")

    def fake_read(target: str) -> str | None:
        return calls.creds.get(target)

    def fake_write(target: str, secret: str, user: str = "Rupor") -> None:
        calls.creds[target] = secret

    def fake_delete(target: str) -> None:
        calls.creds.pop(target, None)

    monkeypatch.setattr(engine, "deliver", fake_deliver)
    monkeypatch.setattr(engine, "replace_text", fake_replace)
    monkeypatch.setattr(engine, "read_selection", lambda info, s: calls.read_selection())
    monkeypatch.setattr(winutil, "foreground", lambda: calls.fg)
    monkeypatch.setattr(winutil, "blocked_by_uipi", lambda info: False)
    monkeypatch.setattr(winutil, "is_window", lambda hwnd: hwnd == TARGET.hwnd)
    monkeypatch.setattr(winutil, "is_visible", lambda hwnd: hwnd == TARGET.hwnd)
    monkeypatch.setattr(winutil, "focus_state", lambda hwnd: calls.focus)
    def fake_check(key: str) -> dict[str, Any]:
        calls.balance_checks.append(key)
        return dict(calls.balance)

    monkeypatch.setattr(ai, "cleanup", fake_cleanup)
    monkeypatch.setattr(ai, "command", fake_command)
    monkeypatch.setattr(ai, "check_openrouter_key", fake_check)
    monkeypatch.setattr(credman, "read", fake_read)
    monkeypatch.setattr(credman, "write", fake_write)
    monkeypatch.setattr(credman, "delete", fake_delete)
    monkeypatch.setattr(fetch, "model_ready", lambda model, cache_dir=None: True)
    monkeypatch.setattr(fetch, "cuda_ready", lambda dirs=None: True)
    monkeypatch.setattr(os, "startfile", lambda p: calls.opened.append(str(p)), raising=False)

    s = config.Settings(sounds=False, first_run_done=True)
    storage = Storage(tmp_path / "rupor.db")
    rec = FakeRecognizer()
    recorder = FakeRecorder()
    eng = Engine(s, storage, recognizer=rec, recorder_factory=lambda dev, cb: recorder,
                 field_reader=lambda: calls.field_reader(),
                 context_reader=lambda: calls.context_reader())
    srv = WsServer(api.Api(eng), eng.bus, 0, TOKEN)
    srv.start()
    eng.start(hook=False, track_focus=False)
    e = Env(eng, srv, calls, rec, recorder, tmp_path)
    try:
        yield e
    finally:
        eng.shutdown()
        srv.stop()
        storage.close()


# ---------- авторизация, hello ----------

@pytest.mark.parametrize("query", ["", "?token=", "?token=wrong", "?tok=" + TOKEN])
def test_rejects_without_valid_token(env: Env, query: str) -> None:
    ws = connect(f"ws://127.0.0.1:{env.srv.port}/{query}", open_timeout=5, legacy=True)
    with pytest.raises(ConnectionClosed) as ei:
        ws.recv(timeout=5)
    assert ei.value.rcvd is not None and ei.value.rcvd.code == 4401


def test_hello_is_state(env: Env) -> None:
    c = Client(env.url)
    first = c._recv(5)
    assert first["event"] == "hello"
    st = first["data"]
    assert set(st) >= {"state", "paused", "device", "model_loaded", "insert_mode", "style",
                       "ai_enabled", "first_run_done", "version", "elevated", "hotkeys"}
    assert set(st["hotkeys"]) >= {"ptt", "broadcast", "command", "cancel"}
    assert st["hotkeys"]["command"] == "alt" and st["model_loaded"] is True
    assert st["device"] == "cuda" and st["style"] == "normal"
    got = c.call("get_state")
    assert got["version"] == st["version"] and got["state"] in ("idle", "loading")
    c.close()


def test_server_binds_localhost_only(env: Env) -> None:
    assert env.srv.host == "127.0.0.1"


# ---------- настройки ----------

def test_settings_roundtrip_and_validation(env: Env) -> None:
    c = env.client()
    full = c.call("get_settings")
    assert full["command_modifier"] == "alt" and full["ai_mode"] == "wait"
    assert full["app_rules"]["telegram.exe"] == {"insert_mode": None, "style": "messenger"}
    c.mark()
    new = c.call("set_settings", {"patch": {"ptt_key": "f13", "theme": "dark",
                                            "history_retention_days": 30}})
    assert new["ptt_key"] == "f13" and new["theme"] == "dark"
    ev = c.wait_event("settings")
    assert ev["ptt_key"] == "f13"
    st = c.wait_event("state", lambda d: d["hotkeys"]["ptt"] == "f13")
    assert st["hotkeys"]["ptt"] == "f13"
    assert env.eng.machine.ptt_groups == (frozenset({0x7C}),)  # хоткей применён на лету
    saved = json.loads((env.tmp / "Rupor" / "settings.json").read_text(encoding="utf-8"))
    assert saved["theme"] == "dark"
    # ошибки валидации — по-русски, настройки не меняются
    err = c.error("set_settings", {"patch": {"insert_mode": "fast"}})
    assert "Способ вставки" in err
    assert "Неизвестная настройка" in c.error("set_settings", {"patch": {"zzz": 1}})
    assert "совпадает" in c.error("set_settings", {"patch": {"broadcast_modifier": "f13"}})
    assert "patch" in c.error("set_settings", {})
    assert c.call("get_settings")["insert_mode"] == "auto"
    assert any("Неизвестная настройка" in r for r in c.raw)  # UTF-8 без \u-экранирования
    c.close()


def test_model_change_triggers_reload(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"compute_type": "int8_float16"}})
    c.wait_event("state", lambda d: d["state"] == "loading", fresh=False)
    c.wait_event("state", lambda d: d["state"] == "idle" and d["model_loaded"])
    c.close()


def test_mic_change_recreates_recorder(env: Env) -> None:
    c = env.client()
    before = env.recorder.opened
    c.call("set_settings", {"patch": {"input_device": "USB"}})
    assert env.recorder.opened == before + 1
    c.close()


def test_pause(env: Env) -> None:
    c = env.client()
    st = c.call("pause", {"on": True})
    assert st["paused"] is True and st["state"] == "paused"
    env.dictate("это не должно распознаться")
    time.sleep(0.3)
    assert env.calls.delivered == []
    st = c.call("pause", {"on": False})
    assert st["paused"] is False and st["state"] == "idle"
    assert "true или false" in c.error("pause", {"on": "да"})
    c.close()


# ---------- история, словарь, сниппеты, импорт/экспорт ----------

def test_dictionary_snippets_export_import(env: Env) -> None:
    c = env.client()
    d = c.call("dict_upsert", {"term": "ClickHouse", "variants": ["клик хаус", "кликхаус"]})
    assert d["id"] > 0 and d["term"] == "ClickHouse"
    assert c.call("dict_list") == [d]
    d2 = c.call("dict_upsert", {"id": d["id"], "term": "ClickHouse", "variants": ["клик хаус"]})
    assert d2["variants"] == ["клик хаус"]
    assert "пуст" in c.error("dict_upsert", {"term": "  ", "variants": []})
    assert "списком строк" in c.error("dict_upsert", {"term": "X", "variants": "a"})
    sn = c.call("snippet_upsert", {"trigger": "мой имейл", "text": "ivan@example.com"})
    assert c.call("snippets_list") == [sn]
    assert "пуст" in c.error("snippet_upsert", {"trigger": "подпись", "text": " "})
    exp = c.call("export_data")
    assert exp == {"version": 1,
                   "dictionary": [{"term": "ClickHouse", "variants": ["клик хаус"]}],
                   "snippets": [{"trigger": "мой имейл", "text": "ivan@example.com"}]}
    c.call("dict_delete", {"id": d["id"]})
    c.call("snippet_delete", {"id": sn["id"]})
    assert c.call("dict_list") == [] and c.call("snippets_list") == []
    n = c.call("import_data", {"data": exp, "replace": True})
    assert n == {"dictionary": 1, "snippets": 1}
    assert c.call("export_data") == exp
    assert "формат" in c.error("import_data", {"data": [1, 2], "replace": False})
    c.close()


def test_stats_and_draft(env: Env) -> None:
    c = env.client()
    st = c.call("stats_get")
    assert st["words_total"] == 0 and len(st["daily"]) == 30
    assert c.call("draft_get") == {"text": ""}
    assert c.call("draft_set", {"text": "Длинная заметка\nвторая строка"}) is None
    assert c.call("draft_get") == {"text": "Длинная заметка\nвторая строка"}
    c.close()


def test_history_commands(env: Env) -> None:
    c = env.client()
    c.mark()
    env.dictate("первая фраза")
    first = c.wait_event("history")
    env.dictate("вторая фраза про отчёт")
    c.wait_event("history")
    page = c.call("history_list", {"limit": 10})
    assert page["total"] == 2 and page["items"][0]["text"] == "вторая фраза про отчёт"
    assert set(page["items"][0]) >= {"id", "ts", "app", "title", "raw", "text", "duration_s",
                                     "words", "language", "style", "ai", "latency_ms", "target"}
    assert c.call("history_list", {"query": "ОТЧЁТ"})["total"] == 1
    env.calls.delivered.clear()
    env.calls.event.clear()
    assert c.call("history_reinsert", {"id": first["id"]}) is None
    assert env.calls.event.wait(5)
    assert env.calls.delivered == [("первая фраза ", False)]
    assert "не найдена" in c.error("history_reinsert", {"id": 99999})
    env.calls.delivered.clear()
    env.calls.event.clear()
    assert c.call("last_insert") is None
    assert env.calls.event.wait(5)
    assert env.calls.delivered == [("вторая фраза про отчёт ", False)]
    c.call("history_delete", {"id": first["id"]})
    assert c.call("history_list")["total"] == 1
    assert c.call("history_clear", {"period": "day"}) == {"deleted": 1}
    assert "Период" in c.error("history_clear", {"period": "year"})
    assert "целым" in c.error("history_delete", {"id": "abc"})
    c.close()


def test_history_disabled_but_stats_counted(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"history_enabled": False}})
    c.mark()
    env.dictate("раз два три четыре")
    c.wait_event("result")
    time.sleep(0.2)
    assert c.call("history_list")["total"] == 0
    assert c.call("stats_get")["words_today"] == 4
    c.close()


# ---------- сохранённые записи голоса ----------

def test_recordings_saved_rotated_and_retranscribed(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"recordings_keep": 2}})
    c.mark()
    env.dictate("первая")
    c.wait_event("history")
    env.dictate("вторая")
    c.wait_event("history")
    env.dictate("третья")
    h3 = c.wait_event("history")
    got = c.call("recordings_list")
    assert got["keep"] == 2 and got["max_bytes"] == 300 * 1024 * 1024
    items = got["items"]
    assert len(items) == 2 and got["total_bytes"] == sum(i["bytes"] for i in items)
    assert set(items[0]) >= {"id", "ts", "duration_s", "bytes", "app", "status", "chars",
                             "history_id"}
    newest = items[0]
    assert newest["status"] == "ok" and newest["chars"] == len("третья")
    assert newest["history_id"] == h3["id"] and newest["app"] == "notepad.exe"
    assert (env.tmp / "recordings" / f"{newest['id']}.wav").exists()
    # распознать заново: в историю и в «Вставить последнее», но не в окно и не в статистику
    words = c.call("stats_get")["words_today"]
    env.calls.delivered.clear()
    env.rec.texts.append("третья, но целиком")
    r = c.call("recording_transcribe", {"id": newest["id"]})
    assert r["text"] == "третья, но целиком" and r["raw"] == "третья, но целиком"
    assert r["id"] == newest["id"] and r["duration_s"] == 1.0
    assert c.call("history_list")["items"][0]["text"] == "третья, но целиком"
    assert env.calls.delivered == []
    assert c.call("stats_get")["words_today"] == words
    assert env.eng.last_text == "третья, но целиком"
    # прослушать: WAV целиком, тот же файл
    snd = c.call("recording_audio", {"id": newest["id"]})
    assert snd["mime"] == "audio/wav"
    assert base64.b64decode(snd["data"]) == (
        env.tmp / "recordings" / f"{newest['id']}.wav").read_bytes()
    assert "не найдена" in c.error("recording_audio", {"id": "../rupor.db"})
    # ошибки и удаление
    assert "не найдена" in c.error("recording_transcribe", {"id": "20200101-000000-000"})
    assert "не найдена" in c.error("recording_transcribe", {"id": "../rupor.db"})
    c.call("recording_delete", {"id": newest["id"]})
    assert [i["id"] for i in c.call("recordings_list")["items"]] == [items[1]["id"]]
    assert c.call("recordings_clear") == {"deleted": 1}
    assert c.call("recordings_list")["items"] == []
    c.call("recording_reveal", {})
    assert env.calls.opened and env.calls.opened[-1].endswith("recordings")
    c.close()


def test_lost_long_dictation_is_kept_on_disk(env: Env) -> None:
    """Длинная запись без текста (распознавание выбросило) — не пропадает: запись на диске,
    уведомление ведёт к ней."""
    c = env.client()
    env.recorder.seconds = 6.0
    c.mark()
    env.dictate("")
    n = c.wait_event("notice", lambda d: d.get("action") == "open_recordings")
    assert "Запись сохранена" in n["text"] and n["level"] == "warn"
    [item] = c.call("recordings_list")["items"]
    assert item["status"] == "empty" and item["duration_s"] == 6.0
    env.rec.texts.append("вот она, длинная мысль")
    assert c.call("recording_transcribe", {"id": item["id"]})["text"] == "вот она, длинная мысль"
    assert c.call("recordings_list")["items"][0]["status"] == "ok"
    c.close()


def test_recordings_off_saves_nothing_and_deletes(env: Env) -> None:
    c = env.client()
    c.mark()
    env.dictate("раз")
    c.wait_event("history")
    assert len(c.call("recordings_list")["items"]) == 1
    c.call("set_settings", {"patch": {"recordings_keep": 0}})
    deadline = time.monotonic() + 5
    while c.call("recordings_list")["items"] and time.monotonic() < deadline:
        time.sleep(0.05)
    assert c.call("recordings_list")["items"] == []
    env.dictate("два")
    c.wait_event("history")
    assert c.call("recordings_list")["items"] == []
    assert "от 0 до 500" in c.error("set_settings", {"patch": {"recordings_keep": 501}})
    c.close()


# ---------- конвейер диктовки ----------

def _enable_ai(c: Client, **extra: Any) -> None:
    c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})
    c.call("set_settings", {"patch": {"ai_enabled": True, "ai_min_words": 3, **extra}})


def test_pipeline_dictionary_snippets_ai_history_stats(env: Env) -> None:
    c = env.client()
    c.call("dict_upsert", {"term": "ClickHouse", "variants": ["клик хаус"]})
    c.call("snippet_upsert", {"trigger": "мой имейл", "text": "ivan@example.com"})
    _enable_ai(c)
    env.calls.cleanup_impl = lambda t: "Надо обновить витрину в ClickHouse сегодня вечером."
    c.mark()
    env.dictate("ну надо обновить витрину в клик хаусе сегодня вечером")
    rec = c.wait_event("result")
    hist = c.wait_event("history")
    # ИИ получил текст после словаря, стиль по правилу окна, термины словаря
    assert env.calls.cleanup == [("ну надо обновить витрину в ClickHouse сегодня вечером",
                                  "normal", ["ClickHouse"])]
    assert env.calls.delivered == [("Надо обновить витрину в ClickHouse сегодня вечером. ",
                                    False)]
    assert rec["ai"] is True and rec["style"] == "normal"
    assert rec["raw"] == "ну надо обновить витрину в клик хаусе сегодня вечером"
    assert set(rec) >= {"text", "raw", "language", "audio_s", "asr_ms", "latency_ms", "ai",
                        "style"}
    assert hist["ai"] is True and hist["app"] == "notepad.exe" and hist["target"] == "window"
    assert hist["text"] == "Надо обновить витрину в ClickHouse сегодня вечером."
    # подсказка распознаванию: термины настроек + словарь
    assert env.rec.prompts[-1] is not None and "ClickHouse" in env.rec.prompts[-1]
    # сниппет: вся фраза = триггер → текст сниппета, ИИ не вызывается
    env.calls.delivered.clear()
    env.calls.cleanup.clear()
    env.dictate("Мой имейл.")
    hist2 = c.wait_event("history")
    assert env.calls.delivered == [("ivan@example.com ", False)]
    assert env.calls.cleanup == [] and hist2["ai"] is False
    stats = c.call("stats_get")
    words = (textproc.count_words("Надо обновить витрину в ClickHouse сегодня вечером.")
             + textproc.count_words("ivan@example.com"))
    assert stats["dictations_total"] == 2 and stats["words_today"] == words
    c.close()


def test_short_phrase_skips_ai(env: Env) -> None:
    c = env.client()
    _enable_ai(c, ai_min_words=6)
    c.mark()
    env.dictate("коротко и ясно")
    c.wait_event("history")
    assert env.calls.cleanup == []
    assert env.calls.delivered == [("коротко и ясно ", False)]
    c.close()


def test_as_is_style_rule_skips_ai(env: Env) -> None:
    c = env.client()
    _enable_ai(c)
    c.call("app_rule_upsert", {"process": "Notepad.exe", "insert_mode": None, "style": "as_is"})
    c.mark()
    env.dictate("длинная фраза из нескольких слов для проверки")
    hist = c.wait_event("history")
    assert env.calls.cleanup == [] and hist["style"] == "as_is"
    c.close()


def test_ai_without_key_notice(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"ai_enabled": True, "ai_min_words": 1}})
    c.mark()
    env.dictate("фраза без ключа")
    n = c.wait_event("notice", lambda d: "ключа нет" in d["text"])
    assert n["action"] == "open_settings_ai" and n["level"] == "warn"
    c.wait_event("history")
    assert env.calls.cleanup == [] and env.calls.delivered == [("фраза без ключа ", False)]
    c.close()


def test_ai_timeout_inserts_text_without_ai(env: Env) -> None:
    c = env.client()
    c.call("dict_upsert", {"term": "Metabase", "variants": ["метабейс"]})
    _enable_ai(c)

    def boom(t: str) -> str:
        raise ai.AIError("OpenRouter не ответил за 5 с", "timeout")

    env.calls.cleanup_impl = boom
    c.mark()
    env.dictate("обнови дашборд в метабейс до вечера")
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert n["text"] == "ИИ не ответил — вставлен текст без обработки"
    hist = c.wait_event("history")
    assert env.calls.delivered == [("обнови дашборд в Metabase до вечера ", False)]
    assert hist["ai"] is False and hist["text"] == "обнови дашборд в Metabase до вечера"
    st = [e["state"] for e in c.events("state")]
    assert "ai" in st  # пилюля показывала обработку ИИ
    c.close()


def test_ai_auth_error_offers_settings(env: Env) -> None:
    c = env.client()
    _enable_ai(c)

    def boom(t: str) -> str:
        raise ai.AIError("OpenRouter отклонил ключ (401)", "auth", 401)

    env.calls.cleanup_impl = boom
    c.mark()
    env.dictate("какая-то длинная фраза для очистки")
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert n["action"] == "open_settings_ai" and "вставлен текст без обработки" in n["text"]
    c.wait_event("history")
    assert env.calls.delivered == [("какая-то длинная фраза для очистки ", False)]
    c.close()


def test_raw_first_replaces_when_untouched(env: Env) -> None:
    c = env.client()
    _enable_ai(c, ai_mode="raw_first")
    gate = threading.Event()

    def slow(t: str) -> str:
        gate.wait(5)
        return "Ну, это очищенный текст."

    env.calls.cleanup_impl = slow
    c.mark()
    env.dictate("ну это сырой текст из диктовки")
    first = c.wait_event("result")
    assert first["ai"] is False
    assert env.calls.event.wait(5)
    assert env.calls.delivered == [("ну это сырой текст из диктовки ", False)]
    gate.set()
    hist = c.wait_event("history")
    assert env.calls.replaced == [(len("ну это сырой текст из диктовки "),
                                   "Ну, это очищенный текст. ")]
    assert hist["ai"] is True and hist["text"] == "Ну, это очищенный текст."
    c.close()


def test_raw_first_keeps_raw_after_keypress(env: Env) -> None:
    c = env.client()
    _enable_ai(c, ai_mode="raw_first")
    gate = threading.Event()

    def slow(t: str) -> str:
        gate.wait(5)
        return "Очищено."

    env.calls.cleanup_impl = slow
    c.mark()
    env.dictate("ну это сырой текст из диктовки")
    c.wait_event("result")
    assert env.calls.event.wait(5)
    env.eng.machine.key_down(ord("A"), 1)  # пользователь продолжил печатать
    env.eng.machine.key_up(ord("A"), 2)
    gate.set()
    n = c.wait_event("notice", lambda d: "не подставлен" in d["text"])
    assert "нажатия клавиш" in n["text"]
    hist = c.wait_event("history")
    assert env.calls.replaced == [] and hist["text"] == "Очищено."
    c.close()


def test_raw_first_ai_error_keeps_raw(env: Env) -> None:
    c = env.client()
    _enable_ai(c, ai_mode="raw_first")

    def boom(t: str) -> str:
        raise ai.AIError("OpenRouter не ответил за 5 с", "timeout")

    env.calls.cleanup_impl = boom
    c.mark()
    env.dictate("ну это сырой текст из диктовки")
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert n["text"] == "ИИ не ответил — оставлен текст без обработки"
    hist = c.wait_event("history")
    assert hist["ai"] is False and env.calls.replaced == []
    c.close()


def test_broadcast_dictation(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rdp = [winutil.WindowInfo(1, "TscShellContainerClass",
                              "srv1 - Подключение к удалённому рабочему столу", 5, "mstsc.exe")]
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda classes, procs: rdp)
    c = env.client()
    c.call("rdp_toggle", {"key": "srv1", "included": True})
    c.mark()
    env.dictate("во все сессии", Kind.BROADCAST)
    c.wait_event("broadcast", lambda d: d["on"] is True)
    hist = c.wait_event("history")
    assert env.calls.delivered == [("во все сессии ", True)]
    assert hist["target"] == "rdp_all"
    c.close()


def test_command_mode(env: Env) -> None:
    c = env.client()
    c.mark()
    env.dictate("переведи на английский", Kind.COMMAND)
    hist = c.wait_event("history")
    assert env.calls.commands == [("переведи на английский", "привет, как дела")]
    assert env.calls.delivered == [("Hello, how are you", False)]  # без пробела: поверх выделения
    assert hist["target"] == "command" and hist["raw"] == "переведи на английский"
    assert hist["text"] == "Hello, how are you" and hist["ai"] is True
    c.close()


def test_command_mode_without_selection(env: Env) -> None:
    c = env.client()
    env.calls.selection = None
    c.mark()
    env.dictate("сделай вежливее", Kind.COMMAND)
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert n["text"] == "Выделите текст для командного режима"
    assert env.calls.commands == [] and env.calls.delivered == []
    c.close()


def test_convert_layout_of_selection(env: Env) -> None:
    env.calls.selection = "Ghbdtn? vbh/"
    c = env.client()
    c.mark()
    env.eng._on_actions([Action(Kind.CONVERT_LAYOUT)])
    assert env.calls.event.wait(5)
    # поверх выделения, без пробела в конце; без ИИ, не в историю и не в «Вставить последнее»
    assert env.calls.delivered == [("Привет, мир.", False)]
    assert env.calls.commands == [] and env.calls.cleanup == []
    assert env.eng.last_text == ""
    c.close()


@pytest.mark.parametrize("selection, text", [
    (None, "Выделите текст, раскладку которого нужно сменить"),
    ("   ", "Выделите текст, раскладку которого нужно сменить"),
    ("123 + 456", "В выделенном тексте нечего переводить в другую раскладку"),
])
def test_convert_layout_nothing_to_do(env: Env, selection: str | None, text: str) -> None:
    env.calls.selection = selection
    c = env.client()
    c.mark()
    env.eng._on_actions([Action(Kind.CONVERT_LAYOUT)])
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert n["text"] == text
    assert env.calls.delivered == []
    c.close()


def test_convert_layout_hotkey_in_state(env: Env) -> None:
    c = env.client()
    assert c.call("get_state")["hotkeys"]["convert_layout"] == "shift+pause"
    c.call("set_settings", {"patch": {"convert_layout_hotkey": ""}})   # выключить
    assert c.call("get_state")["hotkeys"]["convert_layout"] == ""
    assert all(k is not Kind.CONVERT_LAYOUT for k, _ in env.eng.machine._chords)
    c.close()


def test_recording_event_and_cancel(env: Env) -> None:
    c = env.client()
    c.mark()
    env.eng._on_actions([Action(Kind.START), Action(Kind.COMMAND)])
    ev = c.wait_event("recording")
    assert ev["command"] is True and ev["broadcast"] is False and ev["started_at"] > 0
    c.wait_event("state", lambda d: d["state"] == "recording")
    env.eng._on_actions([Action(Kind.CANCEL)])
    n = c.wait_event("notice")
    assert n["text"] == "Запись отменена"
    c.wait_event("state", lambda d: d["state"] == "idle")
    assert env.calls.delivered == []
    c.close()


# ---------- мастер: запись, микрофон, загрузки ----------

def test_test_record(env: Env) -> None:
    c = env.client()
    env.rec.texts.append("проверка связи раз два")
    c.mark()
    assert c.call("test_record_start") is None
    c.wait_event("recording")
    assert "Уже идёт запись" in c.error("test_record_start")
    time.sleep(0.05)
    res = c.call("test_record_stop")
    assert res == {"text": "проверка связи раз два", "language": "ru", "audio_s": 1.0,
                   "asr_ms": 12}
    hist = c.wait_event("history")
    assert hist["target"] == "test"
    assert env.calls.delivered == []  # проверочная запись ничего не вставляет
    assert c.call("stats_get")["dictations_total"] == 0
    assert "не запущена" in c.error("test_record_stop")
    c.close()


def test_mic_monitor_levels(env: Env) -> None:
    c = env.client()
    c.call("mic_monitor", {"on": True})
    assert env.recorder.monitor is True
    c.mark()
    for _ in range(3):
        env.eng._on_level(0.25)
        time.sleep(0.04)
    lv = c.wait_event("level")
    assert lv == {"rms": 0.25}
    c.call("mic_monitor", {"on": False})
    assert env.recorder.monitor is False
    c.close()


def test_setup_status_and_download(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    c = env.client()
    st = c.call("setup_status")
    assert {k: st[k] for k in ("model_ready", "cuda_ready", "first_run_done")} == {
        "model_ready": True, "cuda_ready": True, "first_run_done": True}
    assert st["model_dir"] == st["model_dir_default"] == str(fetch.model_cache_dir(""))

    def fake_download(model: str, cb: Callable[[float, str], None],
                      cache_dir: Any = None) -> str:
        for p in (0, 50, 100):
            cb(float(p), f"Модель {model}: {p}%")
        return "x"

    def failing_cuda(cb: Callable[[float, str], None]) -> None:
        cb(10.0, "Скачиваю cuBLAS")
        raise fetch.FetchError("Нет интернета — повторите")

    monkeypatch.setattr(fetch, "download_model", fake_download)
    monkeypatch.setattr(fetch, "download_cuda", failing_cuda)
    c.mark()
    assert c.call("setup_download", {"what": "model"}) is None
    done = c.wait_event("progress", lambda d: d["done"])
    assert done == {"what": "model", "pct": 100.0, "text": "Модель скачана", "done": True,
                    "error": None}
    assert any(p["pct"] == 50.0 and not p["done"] for p in c.events("progress"))
    c.mark()
    c.call("setup_download", {"what": "cuda"})
    err = c.wait_event("progress", lambda d: d["done"])
    assert err["error"] == "Нет интернета — повторите" and err["what"] == "cuda"
    assert "model или cuda" in c.error("setup_download", {"what": "all"})
    c.close()


# ---------- правила приложений, окна ----------

def test_app_rules(env: Env) -> None:
    c = env.client()
    rules = c.call("app_rules_list")
    assert {"process": "telegram.exe", "insert_mode": None, "style": "messenger"} in rules
    r = c.call("app_rule_upsert", {"process": "Slack.exe", "insert_mode": "clipboard",
                                   "style": "messenger"})
    assert r == {"process": "slack.exe", "insert_mode": "clipboard", "style": "messenger"}
    assert r in c.call("app_rules_list")
    assert c.call("get_settings")["app_rules"]["slack.exe"]["insert_mode"] == "clipboard"
    assert "slack.exe" in c.error("app_rule_upsert", {"process": "slack.exe",
                                                      "insert_mode": "fast", "style": None})
    assert "стиль" in c.error("app_rule_upsert", {"process": "x.exe", "insert_mode": None,
                                                  "style": "poem"})
    c.call("app_rule_delete", {"process": "SLACK.exe"})
    assert all(x["process"] != "slack.exe" for x in c.call("app_rules_list"))
    c.close()


def test_rdp_and_recent_apps(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rdp = [winutil.WindowInfo(1, "TscShellContainerClass", "srv1 - Подключение", 5, "mstsc.exe"),
           winutil.WindowInfo(2, "TscShellContainerClass", "srv2", 6, "mstsc.exe")]
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda classes, procs: rdp)
    monkeypatch.setattr(winutil, "window_info", lambda hwnd: {
        10: winutil.WindowInfo(10, "Qt", "Чат", 11, "Telegram.exe"),
        20: TARGET,
        30: winutil.WindowInfo(30, "Shell_TrayWnd", "", 12, "explorer.exe"),
    }[hwnd])
    c = env.client()
    # Белый список: по умолчанию ни одна сессия не отмечена
    assert c.call("rdp_windows") == [
        {"key": "srv1", "title": "srv1 - Подключение", "process": "mstsc.exe",
         "included": False},
        {"key": "srv2", "title": "srv2", "process": "mstsc.exe", "included": False}]
    c.call("rdp_toggle", {"key": "srv2", "included": True})
    assert c.call("rdp_windows")[1]["included"] is True
    assert c.call("get_settings")["broadcast_targets"] == ["srv2"]
    # совместимость: {title, included} — ключ берётся из открытого окна
    c.call("rdp_toggle", {"title": "srv1 - Подключение", "included": True})
    assert c.call("get_settings")["broadcast_targets"] == ["srv2", "srv1"]
    c.call("rdp_toggle", {"key": "SRV2", "included": False})     # без учёта регистра
    assert c.call("get_settings")["broadcast_targets"] == ["srv1"]
    assert "true или false" in c.error("rdp_toggle", {"key": "srv1", "included": "да"})
    for h in (10, 20, 30, 10):
        env.eng._on_foreground(h)
    assert c.call("recent_apps") == [
        {"process": "Telegram.exe", "title": "Чат", "titles": ["Чат"]},
        {"process": "notepad.exe", "title": TARGET.title, "titles": [TARGET.title]}]
    c.close()


def test_misc_readonly(env: Env) -> None:
    c = env.client()
    styles = c.call("styles_list")
    assert [s["id"] for s in styles] == ["as_is", "normal", "business", "messenger", "code"]
    assert all(set(s) == {"id", "name", "description"} for s in styles)
    devs = c.call("list_devices")
    assert isinstance(devs, list) and all(set(d) == {"index", "name", "default"} for d in devs)
    cuda = c.call("cuda_check", timeout=30)
    assert set(cuda) == {"ok", "device", "gpu_name", "vram_total_mb", "error"}
    assert c.call("open_logs") is None
    assert env.calls.opened and env.calls.opened[0].endswith("logs")
    assert "окно Рупора" in c.error("relaunch_admin")
    c.close()


# ---------- ключи ИИ ----------

def test_ai_keys_never_leak(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    c = env.client()
    assert c.call("ai_key_status", {"provider": "openrouter"}) == {"has_key": False,
                                                                   "masked": None}
    assert c.call("ai_key_check")["error"] == "Ключ OpenRouter не задан"
    r = c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})
    assert r == {"masked": "sk-or-v1-…XYZ"}
    assert env.calls.creds == {"Rupor/openrouter": FAKE_KEY}
    assert c.call("ai_key_status", {"provider": "openrouter"}) == {"has_key": True,
                                                                   "masked": "sk-or-v1-…XYZ"}
    seen: list[str] = []

    def fake_check(key: str) -> dict[str, Any]:
        seen.append(key)
        return {"valid": True, "label": "sk-or-v1-abc…XYZ", "balance_usd": 12.3,
                "usage_usd": 1.0, "limit_usd": None, "error": None}

    monkeypatch.setattr(ai, "check_openrouter_key", fake_check)
    assert c.call("ai_key_check")["balance_usd"] == 12.3 and seen == [FAKE_KEY]
    assert "провайдер" in c.error("ai_key_set", {"provider": "google", "key": "x"})
    assert "пуст" in c.error("ai_key_set", {"provider": "anthropic", "key": "   "})
    c.call("ai_key_delete", {"provider": "openrouter"})
    assert env.calls.creds == {}
    # импорт из .env (временный файл, синтетический ключ)
    envf = env.tmp / "test.env"
    envf.write_text(f"# комментарий\nOTHER=1\nopenrouter_api_key = \"{FAKE_KEY}\"\n",
                    encoding="utf-8")
    assert c.call("ai_key_import_env", {"path": str(envf)}) == {"imported": True,
                                                                "masked": "sk-or-v1-…XYZ"}
    assert env.calls.creds["Rupor/openrouter"] == FAKE_KEY
    assert c.call("ai_key_import_env", {"path": str(env.tmp / "nope.env")}) == {
        "imported": False, "masked": None}
    # ключ целиком не уходил клиенту ни в одном сообщении
    assert not any(FAKE_KEY in raw for raw in c.raw)
    c.close()


def test_ai_test_command(env: Env) -> None:
    c = env.client()
    env.calls.cleanup_impl = lambda t: "Готово."
    r = c.call("ai_test", {"text": "ну готово", "style": "business"})
    assert r == {"text": "Готово.", "ms": 5, "cost_usd": 0.001, "provider": "openrouter",
                 "model": "test"}
    assert env.calls.cleanup[-1][1] == "business"
    assert "стиль" in c.error("ai_test", {"text": "x", "style": "poem"})

    def boom(t: str) -> str:
        raise ai.AIError("Нет ключа OpenRouter — добавьте его в Настройках → ИИ", "no_key")

    env.calls.cleanup_impl = boom
    assert "Нет ключа" in c.error("ai_test", {"text": "x", "style": "normal"})
    c.close()


# ---------- ошибки протокола ----------

def test_protocol_errors(env: Env) -> None:
    c = env.client()
    assert "Неизвестная команда" in c.error("launch_rockets")
    c.ws.send("это не json")
    bad = c._recv(5)
    while "id" not in bad:
        bad = c._recv(5)
    assert bad["ok"] is False and bad["id"] is None and "JSON" in bad["error"]
    assert "объектом" in c.error("history_list", [1, 2])
    assert "Не хватает" in c.error("dict_delete", {})
    assert "строкой" in c.error("history_list", {"query": 5})
    c.close()


def test_parallel_commands_and_two_clients(env: Env) -> None:
    c1, c2 = env.client(), env.client()
    c1.mark()
    c2.mark()
    c1.call("set_settings", {"patch": {"accent": "#112233"}})
    assert c1.wait_event("settings")["accent"] == "#112233"
    assert c2.wait_event("settings")["accent"] == "#112233"  # события — всем клиентам
    ids = []
    for i in range(20):
        c1._id += 1
        ids.append(c1._id)
        c1.ws.send(json.dumps({"id": c1._id, "cmd": "draft_set", "args": {"text": str(i)}}))
    deadline = time.monotonic() + 10
    while not all(any(m.get("id") == i for m in c1.msgs) for i in ids):
        assert time.monotonic() < deadline
        c1._recv(5)
    c1.close()
    c2.close()


def test_quit_calls_handler(env: Env) -> None:
    fired = threading.Event()
    env.eng.on_quit = fired.set
    c = env.client()
    t0 = time.monotonic()
    assert c.call("quit") is None
    assert fired.wait(3)
    assert time.monotonic() - t0 >= 0.15  # ответ успевает уйти до выхода
    c.close()


def test_last_client_disconnect_stops_mic_monitor(env: Env) -> None:
    c = env.client()
    c.call("mic_monitor", {"on": True})
    env.srv._on_clients = lambda n: env.eng.set_mic_monitor(False) if n == 0 else None
    c.close()
    deadline = time.monotonic() + 3
    while env.recorder.monitor and time.monotonic() < deadline:
        time.sleep(0.05)
    assert env.recorder.monitor is False


# ---------- отказоустойчивость: текст не теряется ----------

def test_insert_failure_keeps_text(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(text: str, s: config.Settings, to_all: bool,
               notice: Callable[..., None]) -> Delivery:
        raise OSError("SendInput отправил 0 из 10 событий")

    monkeypatch.setattr(engine, "deliver", broken)
    c = env.client()
    c.mark()
    env.dictate("важная мысль которую нельзя потерять")
    n = c.wait_event("notice", lambda d: d["level"] == "error")
    assert "Не удалось вставить текст" in n["text"] and "Вставить последнее" in n["text"]
    hist = c.wait_event("history")
    assert hist["text"] == "важная мысль которую нельзя потерять"
    assert env.eng.last_text == "важная мысль которую нельзя потерять"
    c.close()


class FakeWhisper:
    """Поддельная модель faster-whisper: на CUDA падает при распознавании (не при прогреве)."""
    created: list[tuple[str, str]] = []

    def __init__(self, path: str, device: str, compute_type: str) -> None:
        self.device = device
        FakeWhisper.created.append((device, compute_type))

    def transcribe(self, audio: np.ndarray, **kw: Any) -> tuple[list[Any], None]:
        if self.device == "cuda" and kw.get("beam_size") != 1:
            raise RuntimeError("CUDA failed with error out of memory")

        class Seg:
            text = " текст с процессора "
        return [Seg()], None

    def detect_language(self, audio: np.ndarray) -> tuple[str, float, list[Any]]:
        return "ru", 0.99, []


def test_cuda_failure_mid_run_falls_back_to_cpu(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from rupor import asr
    monkeypatch.setenv("APPDATA", str(tmp_path))
    monkeypatch.setattr(asr, "WhisperModel", FakeWhisper)
    monkeypatch.setattr(fetch, "model_path", lambda model, cache_dir=None: tmp_path)
    monkeypatch.setattr(fetch, "cuda_ready", lambda dirs=None: True)
    FakeWhisper.created = []
    notices: list[tuple[str, str]] = []
    rec = asr.Recognizer(config.Settings(), on_notice=lambda t, lv: notices.append((t, lv)))
    rec.load()
    assert rec.device == "cuda"
    res = rec.transcribe(np.zeros(16000, np.float32))
    assert res.text == "текст с процессора" and rec.device == "cpu"
    assert FakeWhisper.created == [("cuda", "float16"), ("cpu", "int8")]
    assert notices and notices[0][1] == "warn" and "CPU" in notices[0][0]
    assert rec.last_error is not None and "out of memory" in rec.last_error
    rec.unload()        # после выгрузки снова пробуем GPU
    rec.load()
    assert rec.device == "cuda"


def test_missing_model_is_reported(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from rupor import asr
    monkeypatch.setattr(fetch, "model_path", lambda model, cache_dir=None: None)
    rec = asr.Recognizer(config.Settings())
    with pytest.raises(asr.ModelMissing, match="не скачана"):
        rec.transcribe(np.zeros(16000, np.float32))


# ---------- первый запуск: модель ещё не скачана ----------

def _wait_for(pred: Callable[[], bool], timeout: float = 5) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        assert time.monotonic() < deadline, "не дождались"
        time.sleep(0.02)


@pytest.mark.parametrize("first_run_done", [False, True])
def test_missing_model_download_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     first_run_done: bool) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    ready = {"v": False}
    downloads: list[str] = []

    def fake_download(model: str, cb: Callable[[float, str], None],
                      cache_dir: Any = None) -> str:
        downloads.append(model)
        cb(50.0, "половина")
        ready["v"] = True
        return "x"

    monkeypatch.setattr(fetch, "model_ready", lambda model, cache_dir=None: ready["v"])
    monkeypatch.setattr(fetch, "download_model", fake_download)
    rec = FakeRecognizer()
    rec._loaded = False
    storage = Storage(tmp_path / "rupor.db")
    eng = Engine(config.Settings(sounds=False, first_run_done=first_run_done), storage,
                 recognizer=rec, recorder_factory=lambda dev, cb: FakeRecorder(),
                 auto_download_model=False, field_reader=lambda: None)
    events: list[tuple[str, dict[str, Any]]] = []
    eng.bus.subscribe(lambda e, d: events.append((e, d)))
    eng.start(hook=False, track_focus=False)
    try:
        if not first_run_done:
            # мастер ещё не пройден: ядро само ничего не качает и ждёт setup_download
            _wait_for(lambda: eng.state == "error")
            time.sleep(0.2)
            assert downloads == [] and eng.get_state()["model_loaded"] is False
            eng.setup_download("model")
        _wait_for(lambda: eng.state == "idle" and rec.loaded)
        assert downloads == ["large-v3-turbo"]
        prog = [d for e, d in events if e == "progress"]
        assert prog[-1]["done"] is True and prog[-1]["error"] is None
        assert any(p["pct"] == 50.0 for p in prog)
        assert eng.get_state()["device"] == "cuda"
    finally:
        eng.shutdown()
        storage.close()


# ---------- регрессии: куда и когда вставляется текст ----------

OTHER = winutil.WindowInfo(5151, "Chrome_WidgetWin_1", "Чат — Telegram", 888_888, "chrome.exe")
RDP = winutil.WindowInfo(6161, "TscShellContainerClass", "srv1 - Подключение", 777_777,
                         "mstsc.exe")


def _switch_window_during_asr(env: Env, to: winutil.WindowInfo | None) -> None:
    """Пока идёт распознавание, пользователь переключился в другое окно."""
    orig = env.rec.transcribe

    def transcribe(audio: np.ndarray, language: str | None = None) -> Result:
        env.calls.fg = to
        return orig(audio, language)

    env.rec.transcribe = transcribe  # type: ignore[method-assign]


def test_dictation_not_inserted_into_another_window(env: Env) -> None:
    c = env.client()
    _switch_window_during_asr(env, OTHER)
    c.mark()
    env.dictate("личное сообщение для почты")
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert "Окно сменилось" in n["text"] and "Вставить последнее" in n["text"]
    hist = c.wait_event("history")
    assert env.calls.delivered == []            # в чужое окно ничего не ушло
    assert hist["text"] == "личное сообщение для почты"
    assert env.eng.last_text == "личное сообщение для почты"
    c.close()


def test_snippet_trigger_with_dictionary_variant(env: Env) -> None:
    """Пример из ТЗ: сниппет «мой имейл», а в словаре имейл → email."""
    c = env.client()
    c.call("dict_upsert", {"term": "email", "variants": ["имейл"]})
    c.call("snippet_upsert", {"trigger": "мой имейл", "text": "ivan@example.com"})
    c.mark()
    env.dictate("Мой имейл.")
    hist = c.wait_event("history")
    assert env.calls.delivered == [("ivan@example.com ", False)]
    assert hist["text"] == "ivan@example.com"
    env.calls.delivered.clear()
    env.dictate("напиши на мой имейл завтра")   # сниппет внутри фразы
    c.wait_event("history")
    assert env.calls.delivered == [("напиши на ivan@example.com завтра ", False)]
    c.close()


def test_raw_first_keypress_during_insertion_cancels_replace(env: Env) -> None:
    """Нажатия, пока сырой текст ещё вставлялся (печать в RDP идёт секунды), — тоже
    «нажатия после вставки»: замена стёрла бы допечатанное."""
    c = env.client()
    _enable_ai(c, ai_mode="raw_first")
    gate = threading.Event()

    def slow(t: str) -> str:
        gate.wait(5)
        return "Очищено."

    def user_types() -> None:
        env.eng.machine.key_down(ord("A"), 10)
        env.eng.machine.key_up(ord("A"), 11)

    env.calls.cleanup_impl = slow
    env.calls.on_deliver = user_types
    c.mark()
    env.dictate("ну это сырой текст из диктовки")
    assert env.calls.event.wait(5)
    gate.set()
    n = c.wait_event("notice", lambda d: "не подставлен" in d["text"])
    assert "нажатия клавиш" in n["text"]
    c.wait_event("history")
    assert env.calls.replaced == []
    c.close()


def test_raw_first_wrong_selection_is_not_overwritten(env: Env) -> None:
    """Слева от курсора не то, что вставляли (щелчок мышью, автозамена, ячейки Excel) —
    замена отменяется, чужой текст цел."""
    c = env.client()
    _enable_ai(c, ai_mode="raw_first")
    env.calls.cleanup_impl = lambda t: "Очищено."
    env.calls.selection_ok = False
    c.mark()
    env.dictate("ну это сырой текст из диктовки")
    n = c.wait_event("notice", lambda d: "не подставлен" in d["text"])
    assert "не вставленный текст" in n["text"]
    hist = c.wait_event("history")
    assert env.calls.replaced == [] and hist["text"] == "Очищено."
    c.close()


def test_raw_first_focus_change_cancels_replace(env: Env) -> None:
    c = env.client()
    _enable_ai(c, ai_mode="raw_first")
    gate = threading.Event()

    def slow(t: str) -> str:
        gate.wait(5)
        return "Очищено."

    env.calls.cleanup_impl = slow
    c.mark()
    env.dictate("ну это сырой текст из диктовки")
    assert env.calls.event.wait(5)
    env.calls.focus = 8        # щелчок мышью в другое поле того же окна
    gate.set()
    n = c.wait_event("notice", lambda d: "не подставлен" in d["text"])
    assert "фокус ввода" in n["text"]
    assert env.calls.replaced == []
    c.close()


def test_command_mode_selection_changed_while_waiting(env: Env) -> None:
    c = env.client()
    env.calls.selections = ["привет, как дела", "совсем другой абзац"]
    c.mark()
    env.dictate("переведи на английский", Kind.COMMAND)
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert "Выделение изменилось" in n["text"] and "Вставить последнее" in n["text"]
    hist = c.wait_event("history")
    assert env.calls.delivered == [] and hist["text"] == "Hello, how are you"
    c.close()


def test_command_mode_keypress_while_waiting(env: Env) -> None:
    c = env.client()
    env.calls.command_hook = lambda: (env.eng.machine.key_down(ord("A"), 10),
                                      env.eng.machine.key_up(ord("A"), 11)) and None
    c.mark()
    env.dictate("переведи на английский", Kind.COMMAND)
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert "нажатия клавиш" in n["text"]
    assert env.calls.delivered == []
    c.close()


def test_command_mode_extra_lines_not_typed_into_rdp(env: Env) -> None:
    """Ответ модели с лишними строками в RDP-окно (печать клавишами) не печатается:
    каждый перевод строки стал бы Enter в удалённой консоли."""
    c = env.client()
    env.calls.fg = RDP
    env.calls.selection = "ERROR: disk full"
    env.calls.command_out = "ОШИБКА: диск заполнен\nRemove-Item C:\\data -Recurse\n"
    c.mark()
    env.dictate("переведи", Kind.COMMAND)
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert "больше строк" in n["text"]
    assert env.calls.delivered == []
    env.calls.command_out = "ОШИБКА: диск заполнен"   # столько же строк — вставляется
    env.dictate("переведи", Kind.COMMAND)
    c.wait_event("history")
    c.wait_event("history")
    assert env.calls.delivered == [("ОШИБКА: диск заполнен", False)]
    c.close()


# ---------- регрессии: запись, микрофон, модель ----------

def test_start_during_test_record_gives_notice(env: Env) -> None:
    c = env.client()
    c.call("test_record_start")
    c.mark()
    env.dictate("это не должно распознаться")
    n = c.wait_event("notice", lambda d: d["level"] == "warn")
    assert "проверочная запись" in n["text"]
    assert env.eng.cancel_test_record() is True     # UI отключился — запись отменена
    c.wait_event("state", lambda d: d["state"] == "idle")
    env.rec.texts.clear()
    env.dictate("теперь диктовка работает")
    c.wait_event("history")
    assert env.calls.delivered == [("теперь диктовка работает ", False)]
    c.close()


def test_test_record_has_time_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "TEST_RECORD_MAX_S", 0.2)
    c = env.client()
    c.mark()
    c.call("test_record_start")
    n = c.wait_event("notice", lambda d: "Проверочная запись остановлена" in d["text"])
    assert n["level"] == "warn"
    assert c.call("get_state")["state"] == "idle"
    assert env.recorder.recording is False
    assert "не запущена" in c.error("test_record_stop")
    c.close()


def test_dead_microphone_is_reported_and_reopened(env: Env) -> None:
    """Гарнитуру переподключили: поток молчит, запись пустая — не молчать, а сказать
    и переоткрыть микрофон."""
    c = env.client()
    env.recorder.seconds = 0.0
    before = env.recorder.opened
    c.mark()
    env.eng._on_actions([Action(Kind.START)])
    time.sleep(1.1)
    env.eng._on_actions([Action(Kind.STOP)])
    n = c.wait_event("notice", lambda d: d["level"] == "error")
    assert "Микрофон не даёт звук" in n["text"]
    deadline = time.monotonic() + 5              # переоткрытие идёт сразу после уведомления
    while env.recorder.opened < before + 1 and time.monotonic() < deadline:
        time.sleep(0.01)
    assert env.recorder.opened == before + 1
    assert env.calls.delivered == []
    c.close()


def test_model_change_keeps_old_model_while_downloading(env: Env,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    ready = {"large-v3-turbo": True}
    gate = threading.Event()
    unloads: list[int] = []

    def fake_download(model: str, cb: Callable[[float, str], None],
                      cache_dir: Any = None) -> str:
        gate.wait(5)
        ready[model] = True
        return "x"

    orig_unload = env.rec.unload

    def unload() -> None:
        unloads.append(1)
        orig_unload()

    monkeypatch.setattr(fetch, "model_ready", lambda model, cache_dir=None: ready.get(model,
                                                                                       False))
    monkeypatch.setattr(fetch, "download_model", fake_download)
    env.rec.unload = unload  # type: ignore[method-assign]
    c = env.client()
    c.mark()
    c.call("set_settings", {"patch": {"model": "small"}})
    c.wait_event("notice", lambda d: "пока распознаёт прежняя" in d["text"])
    env.dictate("пока качается новая модель")
    c.wait_event("history")
    assert env.calls.delivered == [("пока качается новая модель ", False)]
    assert unloads == [] and env.rec.loaded      # прежняя модель не выгружена
    gate.set()
    c.wait_event("progress", lambda d: d["done"] is True)
    _wait_for(lambda: unloads == [1] and env.rec.loaded)   # подменили на новую
    c.close()


def test_model_download_failure_keeps_old_model(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    def failing(model: str, cb: Callable[[float, str], None],
                cache_dir: Any = None) -> str:
        raise fetch.FetchError("Нет интернета")

    monkeypatch.setattr(fetch, "model_ready", lambda model, cache_dir=None: model != "small")
    monkeypatch.setattr(fetch, "download_model", failing)
    c = env.client()
    c.mark()
    c.call("set_settings", {"patch": {"model": "small"}})
    n = c.wait_event("notice", lambda d: "не скачалась" in d["text"])
    assert "прежней модели" in n["text"]
    assert env.rec.loaded
    env.dictate("диктовка не теряется")
    c.wait_event("history")
    assert env.calls.delivered == [("диктовка не теряется ", False)]
    c.close()


def test_cuda_downloaded_while_model_loads_on_cpu(env: Env,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    """Модель начала грузиться на CPU до конца скачивания CUDA — после загрузки её
    перезапускают на GPU (раньше проверка rec.loaded шла мимо очереди и терялась)."""
    gate = threading.Event()
    loads: list[str] = []
    env.rec.unload()

    def load() -> None:
        if not loads:
            gate.wait(5)
            env.rec.device = "cpu"
        else:
            env.rec.device = "cuda"
        loads.append(str(env.rec.device))
        env.rec._loaded = True

    env.rec.load = load  # type: ignore[method-assign]
    monkeypatch.setattr(fetch, "download_cuda", lambda cb: None)
    c = env.client()
    c.mark()
    env.eng._jobs.put(("load", None))
    time.sleep(0.1)                       # загрузка идёт (на CPU)
    env.eng.setup_download("cuda")
    c.wait_event("progress", lambda d: d["what"] == "cuda" and d["done"])
    gate.set()
    c.wait_event("notice", lambda d: "перезапускаю распознавание на GPU" in d["text"])
    _wait_for(lambda: loads == ["cpu", "cuda"])
    c.close()


def test_settings_save_failure_does_not_half_apply(env: Env,
                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    real_save = config.save

    def locked(s: config.Settings) -> None:
        raise PermissionError(13, "Отказано в доступе")

    monkeypatch.setattr(config, "save", locked)
    c = env.client()
    assert "Системная ошибка" in c.error("set_settings", {"patch": {"ptt_key": "f13"}})
    assert c.call("get_settings")["ptt_key"] == "ctrl+win"  # в памяти тоже не изменилось
    assert env.eng.machine.ptt_groups == parse_keyset("ctrl+win")
    monkeypatch.setattr(config, "save", real_save)
    c.call("set_settings", {"patch": {"ptt_key": "f13"}})  # повтор применяется целиком
    assert env.eng.machine.ptt_groups == (frozenset({0x7C}),)
    c.close()


def test_mic_change_during_hands_free_recognizes_recording(env: Env) -> None:
    c = env.client()
    env.rec.texts.append("надиктовано без рук")
    c.mark()
    env.eng._on_actions([Action(Kind.START), Action(Kind.HANDS_FREE)])
    c.wait_event("state", lambda d: d["state"] == "hands_free")
    c.call("set_settings", {"patch": {"input_device": "USB"}})
    n = c.wait_event("notice", lambda d: "изменились настройки" in d["text"])
    assert n["toast"] is True
    hist = c.wait_event("history")
    assert hist["text"] == "надиктовано без рук"      # записанное не потеряно
    assert env.eng._session is None
    c.close()


def test_recording_has_duration_limit(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine, "MAX_RECORD_S", 0.3)
    c = env.client()
    env.rec.texts.append("забытая запись без рук")
    c.mark()
    env.eng._on_actions([Action(Kind.START), Action(Kind.HANDS_FREE)])
    n = c.wait_event("notice", lambda d: "Запись остановлена" in d["text"])
    assert "дольше" in n["text"]
    c.wait_event("history")
    assert env.eng._session is None
    c.close()


# ---------- регрессии: протокол ----------

def test_commands_of_one_client_run_in_order(env: Env) -> None:
    """mic_monitor false→true и быстрые set_settings от ползунка применяются по порядку."""
    c = env.client()
    ids: list[int] = []

    def send(cmd: str, args: dict[str, Any]) -> None:
        c._id += 1
        ids.append(c._id)
        c.ws.send(json.dumps({"id": c._id, "cmd": cmd, "args": args}))

    for _ in range(30):
        send("mic_monitor", {"on": False})
        send("mic_monitor", {"on": True})
    for v in range(1, 9):
        send("set_settings", {"patch": {"type_delay_ms": v}})
    deadline = time.monotonic() + 10
    while not all(any(m.get("id") == i for m in c.msgs) for i in ids):
        assert time.monotonic() < deadline
        c._recv(5)
    assert env.recorder.monitor is True
    assert c.call("get_settings")["type_delay_ms"] == 8
    c.close()


def test_long_command_does_not_block_the_connection(env: Env) -> None:
    c = env.client()
    gate = threading.Event()

    def slow(t: str) -> str:
        gate.wait(5)
        return "Готово."

    env.calls.cleanup_impl = slow
    c._id += 1
    rid = c._id
    c.ws.send(json.dumps({"id": rid, "cmd": "ai_test",
                          "args": {"text": "ну готово", "style": "normal"}}))
    t0 = time.monotonic()
    assert c.call("draft_get") == {"text": ""}       # не ждёт ответа ИИ
    assert time.monotonic() - t0 < 2
    gate.set()
    deadline = time.monotonic() + 10
    while not any(m.get("id") == rid for m in c.msgs):
        assert time.monotonic() < deadline
        c._recv(5)
    c.close()


def test_parallel_rdp_toggles_are_not_lost(env: Env) -> None:
    c1, c2 = env.client(), env.client()
    ids: dict[Client, list[int]] = {c1: [], c2: []}
    for i in range(6):
        for n, c in enumerate((c1, c2)):
            c._id += 1
            ids[c].append(c._id)
            key = f"srv{n}-{i}"
            c.ws.send(json.dumps({"id": c._id, "cmd": "rdp_toggle",
                                  "args": {"key": key, "included": True}}))
    for c in (c1, c2):
        deadline = time.monotonic() + 10
        while not all(any(m.get("id") == i for m in c.msgs) for i in ids[c]):
            assert time.monotonic() < deadline
            c._recv(5)
    assert len(c1.call("get_settings")["broadcast_targets"]) == 12
    c1.close()
    c2.close()


def test_notices_before_ui_connects_are_delivered(env: Env) -> None:
    """Откат на CPU, нет микрофона: ядро сообщает об этом раньше, чем подключается UI."""
    env.eng.notice("Библиотеки CUDA не скачаны — распознавание на CPU", level="warn")
    env.eng.notice("Скачиваю модель", toast=True)   # info не копится
    c = env.client()
    n = c.wait_event("notice", fresh=False)
    assert n["text"].startswith("Библиотеки CUDA") and n["level"] == "warn"
    assert all(e["level"] != "info" for e in c.events("notice"))
    pill = env.client()                              # второе окно (пилюля) — тоже
    assert pill.wait_event("notice", fresh=False)["text"].startswith("Библиотеки CUDA")
    c.close()
    pill.close()


def test_browser_pages_cannot_connect(env: Env) -> None:
    from websockets.exceptions import InvalidStatus
    from websockets.typing import Origin
    with pytest.raises(InvalidStatus) as ei:
        connect(env.url, open_timeout=5, origin=Origin("https://evil.example"), legacy=True)
    assert ei.value.response.status_code == 403
    ok = connect(env.url, open_timeout=5, origin=Origin("http://tauri.localhost"), legacy=True)
    first = json.loads(ok.recv(timeout=5))
    assert first["event"] == "hello"
    ok.close()


def test_rejected_tokens_are_not_logged_every_time(env: Env,
                                                   caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("WARNING", logger="rupor.server")
    for _ in range(5):
        ws = connect(f"ws://127.0.0.1:{env.srv.port}/?token=wrong", open_timeout=5,
                     legacy=True)
        with pytest.raises(ConnectionClosed):
            ws.recv(timeout=5)
    rejected = [r for r in caplog.records if "без верного токена" in r.getMessage()]
    assert len(rejected) <= 1


def test_any_command_error_is_scrubbed_of_keys(env: Env) -> None:
    """Ключ в тексте исключения (ValueError http.client о заголовке) не уходит в UI."""
    c = env.client()

    def boom(t: str) -> str:
        raise ValueError(f"Invalid header value b'Bearer {FAKE_KEY}\\r\\n'")

    env.calls.cleanup_impl = boom
    err = c.error("ai_test", {"text": "проверка", "style": "normal"})
    assert FAKE_KEY not in err and "sk-or-v1-fff" not in err
    assert not any(FAKE_KEY in raw for raw in c.raw)
    c.close()
