"""Звук компьютера во время записи в движке: приглушение на подтверждении записи и возврат
на всех путях её окончания (стоп, отмена, выброс, лимит, пауза, выход, смена настроек,
проверочная запись мастера), настройки duck_mode/duck_level.

Приглушение — фейк (FakeDucker) или настоящий duck.Ducker на фейковых сессиях: звук
пользователя здесь не трогается (см. tests/conftest.py и tests/test_duck.py)."""
from __future__ import annotations

import os
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

import pytest

from rupor import config, duck, engine
from rupor.hotkeys import Action, Kind
from test_api import TARGET, Env, env  # noqa: F401 — фикстура env
from test_duck import FakeBackend, FakeSession, approx


class FakeDucker:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.fail = False
        self.lock = threading.Lock()

    def _add(self, *c: Any) -> None:
        with self.lock:
            self.calls.append(c)

    def start(self) -> None:
        self._add("start")

    def duck(self, mode: str, level: int) -> None:
        if self.fail:
            raise OSError("Core Audio: служба звука недоступна")
        self._add("duck", mode, level)

    def restore(self) -> None:
        if self.fail:
            raise OSError("Core Audio: служба звука недоступна")
        self._add("restore")

    def flush(self, timeout: float = 5.0) -> bool:
        self._add("flush")
        return True

    def close(self, timeout: float = 2.0) -> bool:
        self._add("close")
        return True

    def set_excluded(self, pids: frozenset[int], everything: bool = False) -> None:
        self._add("exclude", pids, everything)

    def ops(self) -> list[tuple[Any, ...]]:
        with self.lock:
            return [c for c in self.calls if c[0] in ("duck", "restore")]


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("условие не выполнилось")
        time.sleep(0.005)


@pytest.fixture
def made(monkeypatch: pytest.MonkeyPatch) -> list[FakeDucker]:
    out: list[FakeDucker] = []

    def factory(*a: Any, **k: Any) -> FakeDucker:
        d = FakeDucker()
        out.append(d)
        return d

    monkeypatch.setattr(duck, "Ducker", factory)
    monkeypatch.setattr(engine, "CONFIRM_MS", 10)     # подтверждение записи — быстро
    return out


@pytest.fixture
def denv(made: list[FakeDucker], env: Env) -> Iterator[tuple[Env, FakeDucker]]:  # noqa: F811
    assert len(made) == 1 and made[0].calls[0] == ("start",)
    yield env, made[0]


def act(env: Env, *kinds: Kind) -> None:
    env.eng._on_actions([Action(k) for k in kinds])


def started(env: Env, d: FakeDucker, n: int = 1) -> None:
    """Начать запись и дождаться подтверждения (звук начала → приглушение)."""
    act(env, Kind.START)
    wait_until(lambda: len([c for c in d.ops() if c[0] == "duck"]) >= n)


def restored(d: FakeDucker) -> None:
    wait_until(lambda: bool(d.ops()) and d.ops()[-1] == ("restore",))


# ---------- настройки ----------

def test_settings_defaults_and_validation(denv: tuple[Env, FakeDucker]) -> None:
    env, _ = denv
    s = config.Settings()
    assert s.duck_mode == "duck" and s.duck_level == 15
    c = env.client()
    got = c.call("get_settings")
    assert got["duck_mode"] == "duck" and got["duck_level"] == 15
    assert c.call("set_settings", {"patch": {"duck_mode": "mute", "duck_level": 30}})[
        "duck_mode"] == "mute"
    assert "Звук компьютера во время записи" in c.error("set_settings",
                                                         {"patch": {"duck_mode": "loud"}})
    assert "Громкость во время записи" in c.error("set_settings",
                                                  {"patch": {"duck_level": 101}})
    assert "Громкость во время записи" in c.error("set_settings",
                                                  {"patch": {"duck_level": 12.5}})
    bad = config.Settings(duck_mode="sometimes", duck_level=-3)   # файл руками — умолчания
    bad.validate()
    assert bad.duck_mode == "duck" and bad.duck_level == 15
    c.close()


# ---------- когда приглушать ----------

def test_ducks_on_confirm_and_restores_on_stop(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    env.rec.texts.append("привет")
    started(env, d)
    assert d.ops() == [("duck", "duck", 15)]
    assert env.eng.state == "recording"
    act(env, Kind.STOP)
    restored(d)
    assert d.ops() == [("duck", "duck", 15), ("restore",)]
    wait_until(lambda: len(env.calls.delivered) == 1)    # диктовка прошла как обычно


def test_short_tap_is_not_ducked(denv: tuple[Env, FakeDucker],
                                 monkeypatch: pytest.MonkeyPatch) -> None:
    env, d = denv
    monkeypatch.setattr(engine, "CONFIRM_MS", 5000)
    act(env, Kind.START, Kind.DISCARD)                   # удержание короче порога
    act(env, Kind.START, Kind.STOP)
    time.sleep(0.1)
    assert d.ops() == []


@pytest.mark.parametrize("end", [Kind.CANCEL, Kind.DISCARD, Kind.STOP])
def test_every_gesture_end_restores(denv: tuple[Env, FakeDucker], end: Kind) -> None:
    env, d = denv
    started(env, d)
    act(env, end)
    restored(d)
    assert d.ops() == [("duck", "duck", 15), ("restore",)]


def test_hands_free_and_pill_click(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    assert env.eng.toggle_dictation() == {"started": True}   # клик по пилюле: «без рук»
    wait_until(lambda: d.ops() == [("duck", "duck", 15)])
    assert env.eng.state == "hands_free"
    assert env.eng.toggle_dictation() == {"started": False}  # второй клик — стоп
    restored(d)


def test_double_tap_hands_free_ducks_once(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    started(env, d)
    act(env, Kind.HANDS_FREE)                    # удержание перешло в «без рук»
    time.sleep(0.05)
    assert d.ops() == [("duck", "duck", 15)]
    act(env, Kind.STOP)
    restored(d)


def test_record_limit_restores(denv: tuple[Env, FakeDucker],
                               monkeypatch: pytest.MonkeyPatch) -> None:
    env, d = denv
    monkeypatch.setattr(engine, "MAX_RECORD_S", 0.2)
    started(env, d)
    restored(d)                                   # лимит → стоп → звук вернулся


def test_pause_restores(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    started(env, d)
    env.eng.set_paused(True)
    restored(d)
    env.eng.set_paused(False)


def test_mic_or_hotkey_change_during_recording_restores(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    started(env, d)
    env.eng.update_settings({"cancel_key": "f13"})   # запись останавливается
    restored(d)


def test_exit_restores(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    started(env, d)
    env.eng.shutdown()
    # возврат ставится первым делом, close ждёт его
    assert d.calls[-2:] == [("restore",), ("close",)]


def test_quit_command_restores_before_core_exits(denv: tuple[Env, FakeDucker]) -> None:
    """«Выход» из UI (WS quit): оболочка ждёт выхода ядра недолго и потом снимает процесс —
    звук начинает возвращаться сразу по команде, не дожидаясь shutdown."""
    env, d = denv
    at_quit: list[list[tuple[Any, ...]]] = []
    env.eng.on_quit = lambda: at_quit.append(d.ops())
    started(env, d)
    c = env.client()
    assert c.call("quit") is None
    assert d.ops() == [("duck", "duck", 15), ("restore",)]   # уже в ответе на команду
    wait_until(lambda: bool(at_quit))
    assert at_quit[0][-1] == ("restore",)
    c.close()


def test_quit_without_recording_touches_nothing(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    fired = threading.Event()
    env.eng.on_quit = fired.set
    env.eng.request_quit(0.01)
    assert fired.wait(3)
    assert d.ops() == []


def test_windows_session_end(denv: tuple[Env, FakeDucker]) -> None:
    """WM_QUERYENDSESSION: вернуть звук и дождаться этого (оболочка на Tauri снимет ядро,
    не дожидаясь его выхода); завершение отменили, а запись идёт — приглушить снова."""
    env, d = denv
    env.eng.session_ending()                       # записи нет — ждать нечего
    assert d.ops() == [] and ("flush",) not in d.calls
    started(env, d)
    env.eng.session_ending()
    assert d.ops()[-1] == ("restore",) and d.calls[-1] == ("flush",)
    env.eng.session_end_cancelled()
    assert d.ops()[-1] == ("duck", "duck", 15)
    act(env, Kind.STOP)
    restored(d)
    env.eng.session_end_cancelled()                # записи уже нет — не приглушаем
    assert d.ops()[-1] == ("restore",)


def test_session_end_window() -> None:
    """Скрытое окно ядра получает WM_QUERYENDSESSION / WM_ENDSESSION (сообщения шлём сами —
    только своему окну; сеанс Windows не завершается)."""
    from rupor import _win as w
    from rupor import winutil
    for _ in range(2):                             # второй экземпляр — свой класс окна
        got: list[Any] = []
        sw = winutil.SessionEndWatcher(lambda: got.append("query"), got.append)
        assert sw.start()
        try:
            assert not w.user32.IsWindowVisible(sw.hwnd)
            assert w.user32.SendMessageW(sw.hwnd, winutil.WM_QUERYENDSESSION, 0, 0) == 1
            w.user32.SendMessageW(sw.hwnd, winutil.WM_ENDSESSION, 0, 0)
            w.user32.SendMessageW(sw.hwnd, winutil.WM_ENDSESSION, 1, 0)
            assert got == ["query", False, True]
        finally:
            sw.stop()
        assert sw.hwnd == 0

    def boom() -> None:
        raise OSError("ошибка обработчика")

    sw = winutil.SessionEndWatcher(boom, lambda ending: None)
    assert sw.start()
    try:
        # ошибка обработчика не мешает завершению сеанса
        assert w.user32.SendMessageW(sw.hwnd, winutil.WM_QUERYENDSESSION, 0, 0) == 1
    finally:
        sw.stop()


def test_new_dictation_after_lost_release(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    started(env, d)
    act(env, Kind.CANCEL)                          # потерянное отпускание: CANCEL + START
    started(env, d, n=2)
    assert d.ops() == [("duck", "duck", 15), ("restore",), ("duck", "duck", 15)]
    act(env, Kind.STOP)
    restored(d)


# ---------- настройки во время записи ----------

def test_settings_change_during_recording(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    started(env, d)
    env.eng.update_settings({"duck_level": 40})
    env.eng.update_settings({"duck_mode": "mute"})
    env.eng.update_settings({"duck_mode": "off"})
    env.eng.update_settings({"duck_mode": "duck"})
    assert d.ops() == [("duck", "duck", 15), ("duck", "duck", 40), ("duck", "mute", 40),
                       ("restore",), ("duck", "duck", 40)]
    act(env, Kind.STOP)
    restored(d)
    env.eng.update_settings({"duck_level": 20})    # записи нет — ничего не делаем
    assert d.ops()[-1] == ("restore",)


def test_off_mode_never_ducks(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    env.eng.update_settings({"duck_mode": "off"})
    act(env, Kind.START)
    wait_until(lambda: env.eng.state == "recording")
    act(env, Kind.STOP)
    time.sleep(0.05)
    assert d.ops() == []


# ---------- проверочная запись мастера ----------

def test_wizard_test_record(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    c = env.client()
    env.rec.texts.append("проверка")
    c.call("test_record_start")
    assert d.ops() == [("duck", "duck", 15)]
    c.call("test_record_stop")
    assert d.ops()[-1] == ("restore",)
    c.call("test_record_start")
    assert env.eng.cancel_test_record() is True    # окно мастера закрылось
    assert d.ops()[-2:] == [("duck", "duck", 15), ("restore",)]
    c.close()


# ---------- ошибки ----------

def test_ducker_errors_do_not_break_dictation(denv: tuple[Env, FakeDucker]) -> None:
    env, d = denv
    d.fail = True
    env.rec.texts.append("текст всё равно вставится")
    act(env, Kind.START)
    wait_until(lambda: env.eng.state == "recording")
    time.sleep(0.05)
    act(env, Kind.STOP)
    wait_until(lambda: len(env.calls.delivered) == 1)
    assert env.calls.delivered[0][0].startswith("текст всё равно вставится")


# ---------- настоящий Ducker на фейковых сессиях ----------

@pytest.fixture
def real(env: Env, monkeypatch: pytest.MonkeyPatch  # noqa: F811
         ) -> Iterator[tuple[Env, FakeBackend, list[FakeSession]]]:
    """Движок из env уже создан с NullBackend; здесь — второй движок не нужен: подменяем
    приглушение движка настоящим Ducker на фейковых сессиях."""
    monkeypatch.setattr(engine, "CONFIRM_MS", 10)
    core = FakeSession("rupor-core", os.getpid())       # своё: звуки начала/конца записи
    system = FakeSession("system", 0, system=True)
    music = FakeSession("spotify", 4001, vol=0.8)
    rdp = FakeSession("mstsc", 4002, vol=1.0)
    be = FakeBackend(core, system, music, rdp)
    eng = env.eng
    eng.ducker.close()
    eng.ducker = duck.Ducker(own_pids=eng.own_pids, backend_factory=lambda: be)
    eng.ducker.start()
    yield env, be, [core, system, music, rdp]


def test_engine_with_real_ducker(real: tuple[Env, FakeBackend, list[FakeSession]]) -> None:
    env, be, (core, system, music, rdp) = real
    dk = env.eng.ducker
    assert isinstance(dk, duck.Ducker)
    env.rec.texts.append("фраза")
    act(env, Kind.START)
    wait_until(lambda: env.eng.state == "recording")
    wait_until(lambda: dk.flush() and approx(music.vol, 0.12))
    assert approx(rdp.vol, 0.15)
    assert core.sets == 0 and system.sets == 0
    p = Path(os.environ["APPDATA"]) / "Rupor" / "duck.json"
    assert p.exists()                               # защита от «залипшего» звука
    act(env, Kind.STOP)
    wait_until(lambda: dk.flush() and approx(music.vol, 0.8) and rdp.vol == 1.0)
    assert dk.flush()                               # возврат мог ещё дописывать файл
    assert not p.exists()


def test_engine_session_end_with_real_ducker(
        real: tuple[Env, FakeBackend, list[FakeSession]]) -> None:
    """Конец сеанса Windows посреди «без рук»: к возврату из session_ending звук уже
    возвращён (дальше процесс ядра могут снять в любой момент)."""
    env, be, (core, system, music, rdp) = real
    dk = env.eng.ducker
    assert env.eng.toggle_dictation() == {"started": True}
    wait_until(lambda: dk.flush() and approx(music.vol, 0.12))
    env.eng.session_ending()
    assert approx(music.vol, 0.8) and rdp.vol == 1.0
    p = Path(os.environ["APPDATA"]) / "Rupor" / "duck.json"
    assert not p.exists()
    env.eng.session_end_cancelled()                 # не завершился — запись идёт дальше
    wait_until(lambda: dk.flush() and approx(music.vol, 0.12))
    assert env.eng.toggle_dictation() == {"started": False}
    wait_until(lambda: dk.flush() and approx(music.vol, 0.8))
