"""Голосовые чаты в движке: выключаются сразу при нажатии клавиши диктовки (не ждут
подтверждения записи), возвращаются при её окончании (стоп, отмена, выброс) и при смене
настроек; выключено в настройках — не трогаются. Фейк AppMuter: звук пользователя здесь не
трогается."""
from __future__ import annotations

import threading
from typing import Any, Iterator

import pytest

from rupor import appmute, config, engine
from rupor.hotkeys import Kind
from test_api import Env, env  # noqa: F401 — фикстура env
from test_duck_engine import act, wait_until


class FakeMuter:
    def __init__(self, *_a: Any, **_k: Any) -> None:
        self.calls: list[tuple[Any, ...]] = []
        self.lock = threading.Lock()

    def _add(self, *c: Any) -> None:
        with self.lock:
            self.calls.append(c)

    def start(self) -> None:
        self._add("start")

    def mute(self, apps: Any, output: bool = True) -> None:
        self._add("mute", tuple(apps), output)

    def restore(self) -> None:
        self._add("restore")

    def flush(self, timeout: float = 5.0) -> bool:
        return True

    def close(self, timeout: float = 2.0) -> bool:
        self._add("close")
        return True

    def ops(self) -> list[tuple[Any, ...]]:
        with self.lock:
            return [c for c in self.calls if c[0] in ("mute", "restore")]


@pytest.fixture
def menv(monkeypatch: pytest.MonkeyPatch, env: Env  # noqa: F811
         ) -> Iterator[tuple[Env, FakeMuter]]:
    m = FakeMuter()
    env.eng.app_muter = m
    monkeypatch.setattr(engine, "CONFIRM_MS", 10)
    yield env, m


def test_defaults_and_validation() -> None:
    s = config.Settings()
    assert s.app_mute_enabled and s.app_mute_output
    assert s.app_mute_apps == ["codex.exe", "chatgpt.exe"]
    assert appmute.DEFAULT_APPS == s.app_mute_apps
    s2 = config.Settings()
    config.apply_patch(s2, {"app_mute_apps": ["Codex.exe", "codex.exe", " ", "x.exe"]})
    assert s2.app_mute_apps == ["Codex.exe", "x.exe"]
    with pytest.raises(ValueError):
        config.apply_patch(s2, {"app_mute_apps": "codex.exe"})


def test_mutes_on_press_and_restores_on_stop(menv: tuple[Env, FakeMuter]) -> None:
    env, m = menv
    act(env, Kind.START)
    assert m.ops() and m.ops()[0] == ("mute", ("codex.exe", "chatgpt.exe"), True)
    act(env, Kind.CANCEL)
    wait_until(lambda: m.ops()[-1] == ("restore",))


def test_short_press_restores(menv: tuple[Env, FakeMuter]) -> None:
    env, m = menv
    act(env, Kind.START, Kind.DISCARD)
    wait_until(lambda: bool(m.ops()) and m.ops()[-1] == ("restore",))


def test_disabled_does_nothing(menv: tuple[Env, FakeMuter]) -> None:
    env, m = menv
    env.client().call("set_settings", {"patch": {"app_mute_enabled": False}})
    act(env, Kind.START)
    act(env, Kind.CANCEL)
    assert not [c for c in m.ops() if c[0] == "mute"]


def test_turned_off_during_recording_restores(menv: tuple[Env, FakeMuter]) -> None:
    env, m = menv
    act(env, Kind.START)
    env.client().call("set_settings", {"patch": {"app_mute_output": False}})
    assert ("mute", ("codex.exe", "chatgpt.exe"), False) in m.ops()
    env.client().call("set_settings", {"patch": {"app_mute_enabled": False}})
    assert m.ops()[-1] == ("restore",)
    act(env, Kind.CANCEL)
