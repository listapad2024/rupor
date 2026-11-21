"""Голосовые чаты не слышат диктовку (rupor.appmute) — на фейковых окнах: кнопка «Отключить
микрофон» нажимается один раз на процесс и «Включить микрофон» — после записи; уже
выключенный пользователем не трогаем; включил сам во время записи — его; свои процессы,
чужие приложения; чат, начатый во время записи; возврат из appmute.json после «падения»
и чужой процесс на том же pid; старый файл (0.8.0/0.8.1) игнорируется."""
from __future__ import annotations

import json
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
