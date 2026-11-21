"""Голосовые чаты не слышат диктовку (rupor.appmute) — на фейковых сессиях: выключить
микрофон и звук приложений из списка и вернуть, чужие/свои/системные не трогать,
пользователь включил сам — его, без звука — не наше, «и звук» выключен, смена списка во
время записи, возврат из appmute.json после «падения», приложение закрылось выключенным."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rupor import appmute
from rupor.appmute import AppMuter


class FakeSession:
    def __init__(self, app: str, pid: int, capture: bool = True, muted: bool = False,
                 system: bool = False, dev: str = "mic") -> None:
        self.app = app
        self.session = f"{{{dev}}}|\\Device\\HarddiskVolume3\\{app}%b{{0}}"
        self.instance = f"{self.session}|1%b{pid}"
        self.pid, self.capture, self.system, self.mute = pid, capture, system, muted
        self.sets = 0

    def muted(self) -> bool:
        return self.mute

    def set_muted(self, m: bool) -> None:
        self.sets += 1
        self.mute = m


class FakeBackend:
    def __init__(self, sessions: list[FakeSession]) -> None:
        self.list = sessions

    def sessions(self) -> list[FakeSession]:
        return list(self.list)

    def process_name(self, pid: int) -> str:
        for s in self.list:
            if s.pid == pid:
                return "C:\\Program Files\\X\\" + s.app
        return ""


def make(tmp_path: Path, sessions: list[FakeSession], own: tuple[int, ...] = ()
         ) -> tuple[AppMuter, FakeBackend]:
    b = FakeBackend(sessions)
    m = AppMuter(own_pids=own, path=tmp_path / appmute.STATE_FILE, backend_factory=lambda: b)
    m.start()
    return m, b


def test_norm_app() -> None:
    assert appmute.norm_app("Codex") == "codex.exe"
    assert appmute.norm_app(r"C:\Apps\ChatGPT.EXE") == "chatgpt.exe"
    assert appmute.norm_app("  ") == ""


def test_mutes_listed_apps_and_restores(tmp_path: Path) -> None:
    codex_mic = FakeSession("Codex.exe", 10)
    codex_out = FakeSession("Codex.exe", 11, capture=False, dev="spk")
    tg_mic = FakeSession("Telegram.exe", 20)
    m, _ = make(tmp_path, [codex_mic, codex_out, tg_mic])
    m.mute(["codex.exe"], output=True)
    assert m.flush()
    assert codex_mic.mute and codex_out.mute and not tg_mic.mute
    assert (tmp_path / appmute.STATE_FILE).exists()
    m.restore()
    assert m.flush()
    assert not codex_mic.mute and not codex_out.mute
    assert not (tmp_path / appmute.STATE_FILE).exists()
    assert tg_mic.sets == 0
    m.close()


def test_output_off_keeps_app_audio(tmp_path: Path) -> None:
    mic = FakeSession("codex.exe", 10)
    out = FakeSession("codex.exe", 10, capture=False, dev="spk")
    m, _ = make(tmp_path, [mic, out])
    m.mute(["codex"], output=False)
    assert m.flush()
    assert mic.mute and not out.mute
    m.close()
    assert not mic.mute


def test_skips_own_system_and_already_muted(tmp_path: Path) -> None:
    own = FakeSession("codex.exe", 5)
    sys_s = FakeSession("codex.exe", 6, system=True)
    pre = FakeSession("codex.exe", 7, muted=True)
    m, _ = make(tmp_path, [own, sys_s, pre], own=(5,))
    m.mute(["codex.exe"])
    assert m.flush()
    assert not own.mute and not sys_s.mute and pre.mute
    m.restore()
    assert m.flush()
    assert pre.mute and pre.sets == 0          # был без звука до нас — не наше
    m.close()


def test_user_unmuted_during_recording_is_left_alone(tmp_path: Path) -> None:
    mic = FakeSession("codex.exe", 10)
    m, _ = make(tmp_path, [mic])
    m.mute(["codex.exe"])
    assert m.flush()
    mic.mute = False                            # пользователь включил сам
    m.mute(["codex.exe"])                       # переопрос (как раз в POLL_S)
    assert m.flush()
    assert not mic.mute
    m.restore()
    assert m.flush()
    assert mic.sets == 1
    m.close()


def test_new_session_during_recording(tmp_path: Path) -> None:
    m, b = make(tmp_path, [])
    m.mute(["chatgpt.exe"])
    assert m.flush()
    late = FakeSession("ChatGPT.exe", 30)
    b.list.append(late)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert late.mute
    m.close()
    assert not late.mute


def test_list_change_releases_removed_app(tmp_path: Path) -> None:
    a = FakeSession("codex.exe", 10)
    c = FakeSession("chatgpt.exe", 11)
    m, _ = make(tmp_path, [a, c])
    m.mute(["codex.exe", "chatgpt.exe"])
    assert m.flush()
    assert a.mute and c.mute
    m.mute(["codex.exe"])
    assert m.flush()
    assert a.mute and not c.mute
    m.close()


def test_restore_after_crash(tmp_path: Path) -> None:
    mic = FakeSession("codex.exe", 10)
    m, _ = make(tmp_path, [mic])
    m.mute(["codex.exe"])
    assert m.flush()
    assert mic.mute
    # «падение»: поток не вернул звук, файл остался
    data = json.loads((tmp_path / appmute.STATE_FILE).read_text(encoding="utf-8"))
    assert len(data["entries"]) == 1
    m2, _ = make(tmp_path, [mic])
    assert m2.flush()
    assert not mic.mute
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m2.close()


def test_app_closed_muted_restored_on_relaunch(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appmute, "PENDING_POLL_S", 0.02)
    mic = FakeSession("codex.exe", 10)
    m, b = make(tmp_path, [mic])
    m.mute(["codex.exe"])
    assert m.flush()
    b.list.clear()                              # приложение закрылось выключенным
    m.restore()
    assert m.flush()
    assert (tmp_path / appmute.STATE_FILE).exists()
    again = FakeSession("codex.exe", 99, muted=True)   # Windows вспомнила беззвук
    b.list.append(again)
    deadline = time.monotonic() + 3
    while again.mute and time.monotonic() < deadline:   # переопрос раз в PENDING_POLL_S
        time.sleep(0.02)
    assert m.flush()
    assert not again.mute
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m.close()


def test_empty_list_does_nothing(tmp_path: Path) -> None:
    mic = FakeSession("codex.exe", 10)
    m, _ = make(tmp_path, [mic])
    m.mute([])
    assert m.flush()
    assert not mic.mute
    m.close()
