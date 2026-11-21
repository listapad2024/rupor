"""Голосовые чаты не слышат диктовку (rupor.appmute) — на фейковых окнах: кнопка «Отключить
микрофон» нажимается один раз на процесс и «Включить микрофон» — после записи; уже
выключенный пользователем не трогаем; включил сам во время записи — его; свои процессы,
чужие приложения; чат, начатый во время записи; возврат из appmute.json после «падения»
и чужой процесс на том же pid; старый файл (0.8.0/0.8.1) игнорируется."""
from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from rupor import appmute
from rupor.appmute import AppMuter


class Chat:
    """Процесс голосового чата: два окна (главное и плавающее) с общим микрофоном."""

    def __init__(self, exe: str = "chatgpt.exe", muted: bool = False, voice: bool = True) -> None:
        self.exe, self.muted, self.voice = exe, muted, voice
        self.presses = 0


class FakeBackend:
    def __init__(self, chats: dict[int, Chat]) -> None:
        self.chats = chats
        self.born = {p: 1000 + p for p in chats}

    def windows(self, apps: frozenset[str]) -> dict[int, list[int]]:
        return {p: [p * 10, p * 10 + 1] for p, c in self.chats.items()
                if "*" in apps or c.exe in apps}

    def created(self, pid: int) -> int | None:
        return self.born.get(pid) if pid in self.chats else None

    def buttons(self, hwnd: int) -> list[str] | None:
        c = self.chats.get(hwnd // 10)
        if c is None:
            return None
        names = ["Начать новый чат"]
        if c.voice:
            names += ["Включить микрофон" if c.muted else "Отключить микрофон",
                      "Отключить звук голосового чата"]
        return names

    def press(self, hwnd: int, match: Callable[[str], bool]) -> bool:
        c = self.chats[hwnd // 10]
        for n in self.buttons(hwnd) or []:
            if match(n):
                c.muted = not c.muted
                c.presses += 1
                return True
        return False


@pytest.fixture(autouse=True)
def _fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appmute, "SETTLE_S", 0.0)


def make(tmp_path: Path, b: FakeBackend, own: tuple[int, ...] = ()) -> AppMuter:
    m = AppMuter(own_pids=own, path=tmp_path / appmute.STATE_FILE, backend_factory=lambda: b)
    m.start()
    return m


def test_names() -> None:
    assert appmute.is_mute("Отключить микрофон") and appmute.is_mute("Mute microphone")
    assert appmute.is_unmute("Включить микрофон") and appmute.is_unmute("Unmute mic")
    assert not appmute.is_mute("Отключить звук голосового чата")
    assert not appmute.is_mute("Unmute microphone") and not appmute.is_unmute("Mute mic")
    assert appmute.norm_app(r"C:\Apps\ChatGPT.EXE") == "chatgpt.exe"


def test_mutes_once_per_process_and_unmutes(tmp_path: Path) -> None:
    chat = Chat()
    b = FakeBackend({7: chat, 8: Chat(exe="telegram.exe")})
    m = make(tmp_path, b)
    m.mute(["codex.exe", "chatgpt.exe"])
    assert m.flush()
    assert chat.muted and chat.presses == 1          # два окна — одно нажатие
    assert (tmp_path / appmute.STATE_FILE).exists()
    m.mute(["codex.exe", "chatgpt.exe"])              # повтор — не переключает обратно
    assert m.flush()
    assert chat.muted and chat.presses == 1
    m.restore()
    assert m.flush()
    assert not chat.muted and chat.presses == 2
    assert b.chats[8].presses == 0
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m.close()


def test_already_muted_by_user_left_alone(tmp_path: Path) -> None:
    chat = Chat(muted=True)
    m = make(tmp_path, FakeBackend({7: chat}))
    m.mute(["chatgpt.exe"])
    assert m.flush()
    m.restore()
    assert m.flush()
    assert chat.muted and chat.presses == 0
    m.close()


def test_user_unmuted_during_recording(tmp_path: Path) -> None:
    chat = Chat()
    m = make(tmp_path, FakeBackend({7: chat}))
    m.mute(["chatgpt.exe"])
    assert m.flush()
    chat.muted = False                               # включил сам
    m.mute(["chatgpt.exe"])                          # переопрос
    assert m.flush()
    assert not chat.muted
    m.restore()
    assert m.flush()
    assert not chat.muted and chat.presses == 1
    m.close()


def test_no_voice_chat_then_started(tmp_path: Path) -> None:
    chat = Chat(voice=False)
    m = make(tmp_path, FakeBackend({7: chat}))
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert chat.presses == 0
    chat.voice = True                                # начал голосовой чат во время записи
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert chat.muted
    m.close()
    assert not chat.muted


def test_own_process_skipped(tmp_path: Path) -> None:
    chat = Chat()
    m = make(tmp_path, FakeBackend({7: chat}), own=(7,))
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert chat.presses == 0
    m.close()


def test_disabled_mid_recording_unmutes(tmp_path: Path) -> None:
    chat = Chat()
    m = make(tmp_path, FakeBackend({7: chat}))
    m.mute(["chatgpt.exe"])
    assert m.flush()
    m.mute([])
    assert m.flush()
    assert not chat.muted
    m.close()


def test_unmute_after_crash(tmp_path: Path) -> None:
    chat = Chat()
    b = FakeBackend({7: chat})
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert chat.muted
    m2 = make(tmp_path, b)                           # «падение»: следующий запуск
    assert m2.flush()
    assert not chat.muted
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m2.close()


def test_reused_pid_not_touched(tmp_path: Path) -> None:
    chat = Chat()
    b = FakeBackend({7: chat})
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    b.born[7] += 1                                   # процесс перезапустился на том же pid
    m.restore()
    assert m.flush()
    assert chat.muted and chat.presses == 1
    m.close()


def test_stale_and_old_files_ignored(tmp_path: Path) -> None:
    chat = Chat(muted=True)
    b = FakeBackend({7: chat})
    p = tmp_path / appmute.STATE_FILE
    p.write_text(json.dumps({"version": 2, "entries": [{"pid": 7, "created": 1007, "ts": 1.0}]}),
                 encoding="utf-8")
    m = make(tmp_path, b)
    assert m.flush()
    assert chat.presses == 0 and not p.exists()
    m.close()
    p.write_text(json.dumps({"version": appmute.FILE_VERSION, "entries": [
        {"pid": 7, "created": 1007, "ts": time.time() - appmute.RESTORE_TTL_S - 5}]}),
        encoding="utf-8")
    m = make(tmp_path, b)
    assert m.flush()
    assert chat.presses == 0
    m.close()


# ---------- очередь команд: действует только последняя ----------

class SlowBackend(FakeBackend):
    """Чтение окна чата «висит», пока не откроют gate (UI Automation небыстрый)."""

    def __init__(self, chats: dict[int, Chat]) -> None:
        super().__init__(chats)
        self.gate = threading.Event()
        self.reading = threading.Event()

    def buttons(self, hwnd: int) -> list[str] | None:
        self.reading.set()
        self.gate.wait(5)
        return super().buttons(hwnd)


def test_stale_commands_are_not_replayed(tmp_path: Path) -> None:
    """Два коротких касания клавиши и диктовка, пока окно чата ещё читается: кнопка
    нажимается один раз, а не «выключить — включить — выключить»."""
    chat = Chat()
    b = SlowBackend({7: chat})
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert b.reading.wait(5)                         # поток занят чтением окна
    m.restore()
    m.mute(["chatgpt.exe"])
    m.restore()
    m.mute(["chatgpt.exe"])                          # идёт диктовка
    b.gate.set()
    assert m.flush()
    assert chat.muted and chat.presses == 1
    m.restore()
    assert m.flush()
    assert not chat.muted and chat.presses == 2
    m.close()


def test_short_tap_does_not_press(tmp_path: Path) -> None:
    """Клавишу отпустили раньше, чем окно чата прочиталось, — микрофон чата не трогаем."""
    chat = Chat()
    b = SlowBackend({7: chat})
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert b.reading.wait(5)
    m.restore()
    b.gate.set()
    assert m.flush()
    assert not chat.muted and chat.presses == 0
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m.close()


def test_new_dictation_right_after_release_keeps_muted(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Отпустили и сразу диктуют снова, пока чат перерисовывается после нажатия: микрофон
    остаётся выключенным (не «включить — выключить»), включается после второй записи."""
    monkeypatch.setattr(appmute, "SETTLE_S", 0.4)
    chat = Chat()
    m = make(tmp_path, FakeBackend({7: chat}))
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert chat.muted and chat.presses == 1
    m.restore()
    time.sleep(0.05)                                 # поток уже ждёт перерисовки чата
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert chat.muted and chat.presses == 1
    assert (tmp_path / appmute.STATE_FILE).exists()  # запись о выключенном микрофоне цела
    m.restore()
    assert m.flush()
    assert not chat.muted and chat.presses == 2
    m.close()


# ---------- активное окно после нажатия кнопки чата ----------

class Desktop:
    """Активное окно по шагам опроса; окна 70 и 71 — процесса чата (pid 7)."""

    def __init__(self, steps: list[int]) -> None:
        self.steps, self.i = steps, 0
        self.fg = steps[0]
        self.activated: list[int] = []
        self.activate_ok = True

    def foreground(self) -> int:
        if self.i < len(self.steps):
            self.fg = self.steps[self.i]
            self.i += 1
        return self.fg

    def pid_of(self, hwnd: int) -> int:
        return hwnd // 10

    def activate(self, hwnd: int) -> bool:
        self.activated.append(hwnd)
        if self.activate_ok:
            self.fg = hwnd
            self.steps = []
        return self.activate_ok


def _hold(d: Desktop, before: int) -> int:
    return appmute.hold_foreground(before, 7, d.foreground, d.pid_of, d.activate,
                                   guard_s=0.2, step_s=0.001)


def test_chat_window_coming_forward_is_undone() -> None:
    d = Desktop([50, 50, 71, 71])                    # диктовали в окно 50, чат вышел вперёд
    assert _hold(d, 50) == 1
    assert d.activated == [50] and d.fg == 50


def test_foreground_left_alone_when_nothing_changes_or_user_moves_on() -> None:
    d = Desktop([50, 50, 50])
    assert _hold(d, 50) == 0 and d.activated == []
    d = Desktop([50, 90, 90])                        # пользователь сам ушёл в третье приложение
    assert _hold(d, 50) == 0 and d.activated == []
    d = Desktop([70, 71, 70])                        # диктуют в сам чат
    assert _hold(d, 70) == 0 and d.activated == []
    d = Desktop([50, 71])                            # активного окна до нажатия не было
    assert _hold(d, 0) == 0 and d.activated == []


def test_foreground_return_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    d = Desktop([71])
    d.activate_ok = False                            # вернуть не удалось — не долбим
    assert _hold(d, 50) == 1 and d.activated == [50]

    class Stubborn(Desktop):                         # чат каждый раз выходит вперёд снова
        def foreground(self) -> int:
            return 71

    s = Stubborn([71])
    assert _hold(s, 50) == appmute.FOCUS_RETURNS
    assert s.activated == [50] * appmute.FOCUS_RETURNS
