"""Голосовые чаты не слышат диктовку (rupor.appmute) — на фейковых процессах: звуковые
процессы приложений из списка приостанавливаются и возобновляются, свои/чужие/0 и 4 не
трогаются, появившиеся во время записи — тоже, смена списка, возобновление из
appmute.json после «падения» (и pid, занятый уже другим процессом, не трогается),
слишком долгая запись, файл 0.8.0 (беззвук сессий) игнорируется."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from rupor import appmute
from rupor.appmute import AppMuter


class FakeBackend:
    def __init__(self, procs: dict[int, str], audio: set[int]) -> None:
        self.procs = dict(procs)          # pid → путь exe
        self.audio = set(audio)           # pid с аудиосессиями
        self.born = {p: 1000 + p for p in procs}
        self.suspended: dict[int, int] = {}

    def audio_pids(self) -> set[int]:
        return set(self.audio)

    def process_name(self, pid: int) -> str:
        return self.procs.get(pid, "")

    def created(self, pid: int) -> int | None:
        return self.born.get(pid) if pid in self.procs else None

    def suspend(self, pid: int) -> bool:
        self.suspended[pid] = self.suspended.get(pid, 0) + 1
        return True

    def resume(self, pid: int) -> bool:
        self.suspended[pid] = self.suspended.get(pid, 0) - 1
        return True

    def held(self) -> set[int]:
        return {p for p, n in self.suspended.items() if n > 0}


CHAT_MAIN, CHAT_AUDIO, TG, OWN = 10, 11, 20, 30


def procs() -> FakeBackend:
    return FakeBackend({CHAT_MAIN: r"C:\Apps\Codex\ChatGPT.exe",
                        CHAT_AUDIO: r"C:\Apps\Codex\ChatGPT.exe",
                        TG: r"C:\Tg\Telegram.exe", OWN: r"C:\Rupor\chatgpt.exe",
                        4: "System"},
                       audio={CHAT_AUDIO, TG, OWN, 0, 4})


def make(tmp_path: Path, b: FakeBackend) -> AppMuter:
    m = AppMuter(own_pids=(OWN,), path=tmp_path / appmute.STATE_FILE, backend_factory=lambda: b)
    m.start()
    return m


def test_norm_app() -> None:
    assert appmute.norm_app("Codex") == "codex.exe"
    assert appmute.norm_app(r"C:\Apps\ChatGPT.EXE") == "chatgpt.exe"
    assert appmute.norm_app("  ") == ""


def test_suspends_only_audio_process_of_listed_app(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    m.mute(["codex.exe", "chatgpt.exe"])
    assert m.flush()
    assert b.held() == {CHAT_AUDIO}            # окно (без аудиосессии), чужие и свои — нет
    assert (tmp_path / appmute.STATE_FILE).exists()
    m.restore()
    assert m.flush()
    assert b.held() == set() and b.suspended[CHAT_AUDIO] == 0
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m.close()


def test_repeated_mute_does_not_double_suspend(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    for _ in range(3):
        m.mute(["chatgpt.exe"])
    assert m.flush()
    assert b.suspended[CHAT_AUDIO] == 1
    m.close()
    assert b.suspended[CHAT_AUDIO] == 0


def test_new_audio_process_during_recording(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    b.audio.add(CHAT_MAIN)
    m.mute(["chatgpt.exe"])                    # то же, что переопрос раз в POLL_S
    assert m.flush()
    assert b.held() == {CHAT_AUDIO, CHAT_MAIN}
    m.close()
    assert b.held() == set()


def test_list_change_resumes_removed_app(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe", "telegram"])
    assert m.flush()
    assert b.held() == {CHAT_AUDIO, TG}
    m.mute(["chatgpt.exe"])
    assert m.flush()
    assert b.held() == {CHAT_AUDIO}
    m.close()


def test_empty_list_does_nothing(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    m.mute([])
    assert m.flush()
    assert b.held() == set()
    m.close()


def test_resume_after_crash(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    data = json.loads((tmp_path / appmute.STATE_FILE).read_text(encoding="utf-8"))
    assert [e["pid"] for e in data["entries"]] == [CHAT_AUDIO]
    # «падение»: поток не возобновил, файл остался — следующий запуск возобновляет
    m2 = make(tmp_path, b)
    assert m2.flush()
    assert b.held() == set()
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m2.close()


def test_reused_pid_is_not_resumed(tmp_path: Path) -> None:
    b = procs()
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    b.born[CHAT_AUDIO] += 1                    # процесс закрылся, pid занял другой
    m.restore()
    assert m.flush()
    assert b.suspended[CHAT_AUDIO] == 1        # чужой процесс не трогали
    assert not (tmp_path / appmute.STATE_FILE).exists()
    m.close()


def test_too_long_recording_resumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(appmute, "POLL_S", 0.01)
    monkeypatch.setattr(appmute, "MAX_SUSPEND_S", 0.05)
    b = procs()
    m = make(tmp_path, b)
    m.mute(["chatgpt.exe"])
    assert m.flush()
    deadline = time.monotonic() + 3
    while b.held() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert b.held() == set()
    m.close()


def test_old_file_format_ignored(tmp_path: Path) -> None:
    (tmp_path / appmute.STATE_FILE).write_text(json.dumps(
        {"version": 1, "entries": [{"instance": "x", "session": "y", "pid": CHAT_AUDIO,
                                    "capture": True, "ts": 1.0}]}), encoding="utf-8")
    b = procs()
    m = make(tmp_path, b)
    assert m.flush()
    assert b.suspended == {}
    m.close()
