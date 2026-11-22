"""Захват звука встречи (rupor.loopback).

Логика — без звука: разбор цели, шаблоны встречи, корень дерева процессов и группировка
сессий по фейковой таблице процессов, склейка времени по QPC (Timeline), LoopbackStream с
фейковым «помощником» (python -c пишет известные float32), настоящий помощник
``python -m rupor --loopback`` на целях, которые до звука не доходят (неверная цель, процесса
нет, родителя нет).

Настоящий захват — ОДИН тест и только с ``RUPOR_AUDIO_TESTS=1``: свой дочерний процесс играет
тихий тон 1 кГц (амплитуда 0,003), помощник захватывает его по ``pid:``. Чужой звук здесь не
захватывается, громкость не меняется ни у кого."""
from __future__ import annotations

import logging
import os
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Collection, Iterator, Mapping

import numpy as np
import pytest

from rupor import duck, i18n, loopback, winutil
from rupor.loopback import (AudioSource, LoopbackStream, SessionInfo, Target, Timeline,
                            group_sources, matches_meeting, parse_target, root_pid)

# Интерпретатор без запускателя .venv: фейковый помощник — один процесс (снимается сразу)
BASE_PY = getattr(sys, "_base_executable", sys.executable)
PATTERNS = ["Google Meet", "Meet –", "Zoom", "Телемост"]
NO_PID = 0xFFFFFFF0                    # такого процесса не бывает (pid кратны 4 и меньше)


def wait_until(pred: Callable[[], bool], timeout: float = 10.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("условие не выполнилось")
        time.sleep(0.01)


# ---------- цель ----------

@pytest.mark.parametrize("spec,kind,pid", [
    ("pid:1234", "pid", 1234), ("PID:8", "pid", 8), (" system ", "system", None),
    ("exclude:42", "exclude", 42),
])
def test_parse_target(spec: str, kind: str, pid: int | None) -> None:
    t = parse_target(spec)
    assert (t.kind, t.pid) == (kind, pid)
    assert parse_target(str(t)) == t


@pytest.mark.parametrize("spec", ["", "pid", "pid:", "pid:0", "pid:-5", "pid:abc", "pid:²",
                                  "proc:12", "exclude:", "pid:4294967296", "systems"])
def test_parse_target_rejects(spec: str) -> None:
    with pytest.raises(ValueError):
        parse_target(spec)


def test_target_for_id() -> None:
    assert loopback.target_for_id("proc:77") == "pid:77"
    assert loopback.target_for_id("system") == "system"
    for bad in ("proc:", "pid:5", "77", ""):
        with pytest.raises(ValueError):
            loopback.target_for_id(bad)
    src = AudioSource("proc:9", "process", 9, "a.exe", "A", "", False, False)
    assert src.target == "pid:9" and loopback.system_source().target == "system"


def test_matches_meeting() -> None:
    assert matches_meeting("Daily standup – Google Meet - Brave", PATTERNS)
    assert matches_meeting("meet – abc-defg-hij", PATTERNS)          # без учёта регистра
    assert matches_meeting("Встреча — ТЕЛЕМОСТ", PATTERNS)
    assert not matches_meeting("Meeting notes - Google Docs", PATTERNS)
    assert not matches_meeting("", PATTERNS)
    assert not matches_meeting("Zoom", [])
    assert not matches_meeting("Zoom", ["", ""])                     # пустой шаблон — не «всё»


# ---------- дерево процессов и источники ----------

# explorer(10) → brave(100) → brave(101, служба звука), brave(102, вкладка) → brave(103, внук)
# explorer → chrome(200) → chrome(201, звук); telegram(300) играет сам; rupor.exe(400, окно)
# → msedgewebview2(401) → msedgewebview2(402, звук окна Рупора); rupor-core(500) — свой
TABLE: dict[int, tuple[int, str]] = {
    10: (4, "explorer.exe"),
    100: (10, "brave.exe"), 101: (100, "brave.exe"), 102: (100, "brave.exe"),
    103: (102, "brave.exe"),
    200: (10, "chrome.exe"), 201: (200, "chrome.exe"),
    300: (10, "Telegram.exe"),
    400: (10, "rupor.exe"), 401: (400, "msedgewebview2.exe"), 402: (401, "msedgewebview2.exe"),
    500: (400, "rupor-core.exe"),
}
PATHS = {p: f"C:\\Apps\\{name}" for p, (_pp, name) in TABLE.items()}
DESCRIPTIONS = {"C:\\Apps\\brave.exe": "Brave Browser", "C:\\Apps\\chrome.exe": "Google Chrome"}


def test_root_pid_walks_up_same_exe() -> None:
    assert root_pid(101, TABLE) == 100
    assert root_pid(103, TABLE) == 100             # внук — тоже к главному процессу
    assert root_pid(100, TABLE) == 100
    assert root_pid(300, TABLE) == 300             # родитель — explorer, другой exe
    assert root_pid(999, TABLE) == 999             # процесса нет в таблице
    cyc = {1: (2, "a.exe"), 2: (1, "a.exe")}
    assert root_pid(1, cyc) in (1, 2)              # цикл (переиспользованные pid) не виснет


def test_root_pid_stops_at_other_path_with_same_name() -> None:
    # .venv\Scripts\python.exe (запускатель) → C:\Python313\python.exe: имя то же, exe другой
    table = {1: (0, "cmd.exe"), 7: (1, "python.exe"), 8: (7, "python.exe")}
    paths = {7: r"D:\proj\.venv\Scripts\python.exe", 8: r"C:\Python313\python.exe"}
    assert root_pid(8, table) == 7
    assert root_pid(8, table, lambda p: paths.get(p, "")) == 8
    assert root_pid(8, table, lambda p: "") == 7   # путь неизвестен — решает имя


class Calls:
    def __init__(self) -> None:
        self.titles: list[set[int]] = []

    def titles_of(self, titles: Mapping[int, list[str]]) -> Callable[[Collection[int]],
                                                                     dict[int, list[str]]]:
        def get(pids: Collection[int]) -> dict[int, list[str]]:
            self.titles.append(set(pids))
            return {p: t for p, t in titles.items() if p in pids}
        return get


def group(sessions: list[SessionInfo], titles: Mapping[int, list[str]] | None = None,
          own: Collection[int] = (400, 500), calls: Calls | None = None) -> list[AudioSource]:
    calls = calls or Calls()
    return group_sources(sessions, own, PATTERNS, table=TABLE, path_of=lambda p: PATHS.get(p, ""),
                         describe=lambda path: DESCRIPTIONS.get(path, ""),
                         titles_of=calls.titles_of(titles or {}))


def test_group_by_root_with_meeting_title_first() -> None:
    sources = group(
        [SessionInfo(101, False), SessionInfo(103, True), SessionInfo(201, True),
         SessionInfo(300, False)],
        titles={100: ["Почта - Brave", "Daily standup – Google Meet - Brave"],
                200: ["YouTube - Google Chrome"], 300: ["Telegram"]})
    assert [s.id for s in sources] == ["proc:100", "proc:200", "proc:300", "system"]
    brave, chrome, tg, system = sources
    assert brave == AudioSource("proc:100", "process", 100, "brave.exe", "Brave Browser",
                                "Daily standup – Google Meet - Brave", True, True)
    assert chrome.name == "Google Chrome" and chrome.active and not chrome.meeting
    assert chrome.title == "YouTube - Google Chrome"
    assert tg.name == "Telegram" and not tg.active          # описания нет — имя exe
    assert system.kind == "system" and system.pid is None and system.active
    assert system.name == "Весь звук компьютера" and system.target == "system"
    assert brave.target == "pid:100"
    assert brave.as_dict()["id"] == "proc:100"


def test_sort_meeting_then_active_then_name() -> None:
    sources = group([SessionInfo(201, False), SessionInfo(300, True), SessionInfo(101, False)],
                    titles={200: ["Zoom Meeting"]})
    # chrome — встреча (не играет), telegram — играет, brave — по имени
    assert [s.pid for s in sources] == [200, 300, 100, None]


def test_own_processes_and_their_descendants_are_hidden() -> None:
    sources = group([SessionInfo(402, True), SessionInfo(500, True), SessionInfo(300, False)])
    assert [s.pid for s in sources] == [300, None]
    assert not sources[-1].active                 # свой звук «весь звук» не делает активным


def test_dead_session_pid_skipped_and_no_sessions_no_work() -> None:
    calls = Calls()
    assert [s.id for s in group([SessionInfo(777, True)], calls=calls)] == ["system"]
    assert calls.titles == []                      # окна не перебирались
    called: list[str] = []
    out = group_sources([], (), PATTERNS, table=None,
                        path_of=lambda p: called.append("path") or "",
                        titles_of=lambda pids: called.append("titles") or {})
    assert [s.id for s in out] == ["system"] and called == []


def test_titles_requested_for_whole_trees_only() -> None:
    calls = Calls()
    group([SessionInfo(101, True)], titles={102: ["Google Meet - Brave"]}, calls=calls)
    assert calls.titles == [{100, 101, 102, 103}]


def test_title_from_child_window_when_root_has_none() -> None:
    (brave, _system) = group([SessionInfo(101, True)], titles={102: ["Zoom - Brave"]})
    assert brave.title == "Zoom - Brave" and brave.meeting


def test_system_name_follows_language() -> None:
    i18n.set_language(i18n.EN)
    assert loopback.system_source().name == "All computer sound"


class FakeSession:
    def __init__(self, pid: int, active: bool, system: bool = False) -> None:
        self.pid, self.active, self.system = pid, active, system


class FakeBackend:
    def __init__(self, *sessions: FakeSession) -> None:
        self.list = list(sessions)
        self.thread = ""

    def sessions(self) -> list[Any]:
        self.thread = threading.current_thread().name
        return list(self.list)


def test_list_sources_without_audio_sessions_is_system_only() -> None:
    # tests/conftest.py: duck.default_backend → NullBackend (сессий нет, окна не читаются)
    out = loopback.list_sources(PATTERNS, frozenset({os.getpid()}))
    assert [s.id for s in out] == ["system"]


def test_list_sources_runs_core_audio_in_its_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = FakeBackend(FakeSession(101, True), FakeSession(0, True, system=True),
                          FakeSession(300, False))
    monkeypatch.setattr(duck, "default_backend", lambda: backend)
    monkeypatch.setattr(winutil, "process_table", lambda: dict(TABLE))
    monkeypatch.setattr(winutil, "process_path", lambda pid: PATHS.get(pid, ""))
    monkeypatch.setattr(winutil, "file_description", lambda path: DESCRIPTIONS.get(path, ""))
    monkeypatch.setattr(winutil, "window_titles",
                        lambda pids: {100: ["Google Meet - Brave"]} if 100 in pids else {})
    out = loopback.list_sources(PATTERNS, (400, 500))
    assert [(s.id, s.meeting, s.active) for s in out] == [
        ("proc:100", True, True), ("proc:300", False, False), ("system", False, True)]
    assert backend.thread == "rupor-sources"


def test_list_sources_errors_and_hangs_give_system_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class Broken:
        def sessions(self) -> list[Any]:
            raise OSError("COM: служба звука недоступна")

    monkeypatch.setattr(duck, "default_backend", Broken)
    assert [s.id for s in loopback.list_sources(PATTERNS)] == ["system"]
    gate = threading.Event()

    class Hung:
        def sessions(self) -> list[Any]:
            gate.wait(5)
            return []

    monkeypatch.setattr(duck, "default_backend", Hung)
    t0 = time.monotonic()
    assert [s.id for s in loopback.list_sources(PATTERNS, timeout=0.2)] == ["system"]
    assert time.monotonic() - t0 < 2
    gate.set()


# ---------- склейка времени ----------

def q(seconds: float) -> int:
    """Секунды → единицы QPC пакетов (100 нс)."""
    return int(round(seconds * 10_000_000))


def test_timeline_contiguous_packets_need_no_zeros() -> None:
    t0 = q(100.0)
    tl = Timeline(t0)
    for i in range(100):                  # пакеты по 10 мс, с дрожанием ±3 мс
        jitter = (0.003 if i % 3 == 0 else -0.002 if i % 3 == 1 else 0.0)
        assert tl.packet(160, t0 + q(i * 0.01 + max(0.0, jitter))) == 0
    assert tl.written == 16000


def test_timeline_fills_lost_packets_with_zeros() -> None:
    t0 = q(5.0)
    tl = Timeline(t0)
    assert tl.packet(160, t0) == 0
    assert tl.packet(160, t0 + q(0.01)) == 0
    # потеряно 50 мс (молча, как при задержке читателя): следующий пакет — с 70 мс
    assert tl.packet(160, t0 + q(0.07)) == 800
    assert tl.written == 1120 + 160
    assert tl.packet(160, t0 + q(0.08)) == 0
    assert tl.packet(160, 0) == 0          # без QPC — просто подряд


def test_timeline_caps_huge_gap_and_resyncs() -> None:
    t0 = q(1.0)
    tl = Timeline(t0)
    tl.packet(160, t0)
    assert tl.packet(160, t0 + q(3600.0)) == loopback.MAX_FILL_SAMPLES   # компьютер спал
    assert tl.packet(160, t0 + q(3600.01)) == 0                           # время перескочило


def test_timeline_idle_fills_silence_behind_now() -> None:
    t0 = q(10.0)
    tl = Timeline(t0)
    assert tl.idle(t0 + q(0.1)) == 0       # отстаём меньше чем на 50 + 100 мс
    z = tl.idle(t0 + q(1.0))
    assert z == 16000 - loopback.IDLE_LAG_SAMPLES
    assert tl.idle(t0 + q(1.05)) == 0      # мельче 100 мс не пишем
    # заиграло: первый пакет — «сейчас» (у system QPC даже чуть впереди)
    assert tl.packet(160, t0 + q(1.011)) == 16176 - (16000 - loopback.IDLE_LAG_SAMPLES) - 0
    assert tl.written == 16176 + 160


def test_hr_text() -> None:
    class COMError(Exception):
        hresult = -2147024891              # 0x80070005

    assert loopback.hr_text(COMError()) == "0x80070005 E_ACCESSDENIED"
    assert loopback.hr_name(0x88890010) == "0x88890010 AUDCLNT_E_SERVICE_NOT_RUNNING"
    assert loopback.hr_text(ValueError("плохо\nвторая строка")) == "ValueError: плохо"


def test_describe_exit() -> None:
    assert loopback.describe_exit(3) == "Программа, звук которой расшифровывался, закрылась"
    assert loopback.describe_exit(2, "activation failed: 0x80070005") == \
        "Не удалось начать захват звука: activation failed: 0x80070005"
    assert "code 1" in loopback.describe_exit(1)


# ---------- LoopbackStream с фейковым помощником ----------

FAKE_HELPER = r'''
import struct, sys, time
mode = sys.argv[1]
data = struct.pack("<1600f", *[i / 1600.0 - 0.5 for i in range(1600)])
out = sys.stdout.buffer
for i in range(0, len(data), 333):         # куски неровного размера
    out.write(data[i:i + 333])
    out.flush()
sys.stderr.write("loopback INFO: fake started " + sys.argv[2] + "\n")
sys.stderr.flush()
if mode == "exit3":
    sys.stderr.write("target process 42 exited\n")
    sys.stderr.flush()
    sys.exit(3)
if mode == "killed":                       # сняли без причины: последняя строка — журнал
    sys.stderr.write("loopback WARNING: capture reopened\n")
    sys.stderr.flush()
    sys.exit(1)
if mode == "hang":
    time.sleep(30)
sys.stdin.read()                           # EOF — выход 0
'''
EXPECTED = np.array([i / 1600.0 - 0.5 for i in range(1600)], dtype=np.float32)


class Got:
    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self.exits: list[tuple[int, str]] = []
        self.lock = threading.Lock()

    def audio(self, a: np.ndarray) -> None:
        with self.lock:
            self.chunks.append(a)

    def exit(self, code: int, reason: str) -> None:
        self.exits.append((code, reason))

    @property
    def samples(self) -> int:
        with self.lock:
            return sum(len(c) for c in self.chunks)

    def all(self) -> np.ndarray:
        with self.lock:
            return np.concatenate(self.chunks) if self.chunks else np.zeros(0, np.float32)


def fake(mode: str, seen: list[str] | None = None) -> Callable[[str], list[str]]:
    def cmd(target: str) -> list[str]:
        if seen is not None:
            seen.append(target)
        return [BASE_PY, "-c", FAKE_HELPER, mode, target]
    return cmd


def test_stream_delivers_float32_and_stops_cleanly() -> None:
    got = Got()
    seen: list[str] = []
    st = LoopbackStream(got.audio, got.exit, command=fake("eof", seen))
    st.start("PID:77")
    assert seen == ["pid:77"] and st.target == "pid:77" and st.alive
    wait_until(lambda: got.samples >= 1600)
    a = got.all()
    assert a.dtype == np.float32 and np.array_equal(a, EXPECTED)
    assert all(len(c) == 800 for c in got.chunks)        # куски по 3200 байт
    assert st.stop() == 0
    assert not st.alive and st.target is None and got.exits == []
    assert st.stop() is None                              # повторный stop — ничего


def test_stream_reports_helper_exit_with_reason() -> None:
    got = Got()
    st = LoopbackStream(got.audio, got.exit, command=fake("exit3"))
    st.start("pid:42")
    wait_until(lambda: bool(got.exits))
    assert got.exits == [(3, "target process 42 exited")]
    assert not st.alive and st.target is None
    assert np.array_equal(got.all(), EXPECTED)
    assert st.stop() is None


def test_stream_exit_without_reason_line_has_empty_reason() -> None:
    """Помощник умер, не написав причину (снят диспетчером задач, антивирусом, упал в Core
    Audio): строка его журнала причиной не становится — ядро скажет «код N» на языке
    интерфейса."""
    got = Got()
    st = LoopbackStream(got.audio, got.exit, command=fake("killed"))
    st.start("system")
    wait_until(lambda: bool(got.exits))
    assert got.exits == [(1, "")]


def test_stream_kills_helper_that_ignores_eof() -> None:
    got = Got()
    st = LoopbackStream(got.audio, got.exit, command=fake("hang"))
    st.start("system")
    wait_until(lambda: got.samples >= 1600)
    pid = st.pid
    t0 = time.monotonic()
    code = st.stop(timeout=0.3)
    assert time.monotonic() - t0 < 3 and code not in (None, 0)
    assert got.exits == [] and pid is not None and not st.alive


def test_stream_restart_on_new_target() -> None:
    got = Got()
    seen: list[str] = []
    st = LoopbackStream(got.audio, got.exit, command=fake("eof", seen))
    st.start("pid:1")
    first = st.pid
    wait_until(lambda: got.samples >= 1600)
    st.start("exclude:5")
    assert seen == ["pid:1", "exclude:5"] and st.target == "exclude:5" and st.pid != first
    wait_until(lambda: got.samples >= 3200)
    assert st.stop() == 0 and got.exits == []


def test_stream_survives_failing_on_audio(caplog: pytest.LogCaptureFixture) -> None:
    calls: list[int] = []

    def bad(a: np.ndarray) -> None:
        calls.append(len(a))
        raise RuntimeError("сломался обработчик")

    st = LoopbackStream(bad, command=fake("eof"), chunk_bytes=400)
    with caplog.at_level(logging.INFO, logger="rupor.loopback"):
        st.start("pid:3")
        wait_until(lambda: sum(calls) >= 1600)
        assert st.stop() == 0
    assert len(calls) == 16                                # чтение не остановилось
    assert sum("ошибка обработки звука" in r.getMessage() for r in caplog.records) == 1
    assert any("loopback: loopback INFO: fake started pid:3" in r.getMessage()
               for r in caplog.records)


def test_stream_bad_target_spawns_nothing() -> None:
    seen: list[str] = []
    st = LoopbackStream(lambda a: None, command=fake("eof", seen))
    with pytest.raises(ValueError):
        st.start("proc:12")
    assert seen == [] and not st.alive


def test_helper_command_dev_and_frozen(monkeypatch: pytest.MonkeyPatch) -> None:
    assert loopback.helper_command("pid:5", 77) == [
        sys.executable, "-m", "rupor", "--loopback", "pid:5", "--parent", "77"]
    env = loopback.helper_env()
    assert env is not None
    core = str(Path(loopback.__file__).resolve().parent.parent)
    assert env["PYTHONPATH"].split(os.pathsep)[0] == core
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Rupor\core\rupor-core.exe")
    assert loopback.helper_command("system") == [r"C:\Rupor\core\rupor-core.exe",
                                                 "--loopback", "system"]
    assert loopback.helper_env() is None


# ---------- настоящий помощник: до звука не доходит ----------

def run_helper(*args: str) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(loopback.helper_command(*args[:1]) + list(args[1:]),
                          env=loopback.helper_env(), capture_output=True, timeout=60,
                          creationflags=loopback.CREATE_NO_WINDOW)


def last_line(r: subprocess.CompletedProcess[bytes]) -> str:
    lines = r.stderr.decode("utf-8", "replace").strip().splitlines()
    return lines[-1] if lines else ""


def test_real_helper_rejects_bad_target() -> None:
    r = run_helper("proc:12")
    assert r.returncode == loopback.EXIT_ACTIVATION
    assert r.stdout == b"" and "bad loopback target" in last_line(r)


def test_real_helper_missing_target_and_parent() -> None:
    r = run_helper(f"pid:{NO_PID}")
    assert r.returncode == loopback.EXIT_TARGET_GONE
    assert r.stdout == b"" and last_line(r) == f"target process {NO_PID} is not running"
    r = run_helper("system", "--parent", str(NO_PID))
    assert r.returncode == loopback.EXIT_OK
    assert r.stdout == b"" and last_line(r) == f"parent process {NO_PID} is not running"


def test_stream_with_real_helper_reports_missing_target() -> None:
    got = Got()
    st = LoopbackStream(got.audio, got.exit)
    st.start(f"pid:{NO_PID}")
    wait_until(lambda: bool(got.exits), 60)
    assert got.exits == [(3, f"target process {NO_PID} is not running")]
    assert got.samples == 0


# ---------- настоящий захват (RUPOR_AUDIO_TESTS=1) ----------

TONE = r'''
import os, sys
import numpy as np
import sounddevice as sd

amp, freq, sr = float(sys.argv[1]), 1000.0, 48000
st = {"n": 0}

def cb(out, frames, t, status):
    n = np.arange(st["n"], st["n"] + frames)
    out[:, 0] = (amp * np.sin(2 * np.pi * freq * n / sr)).astype(np.float32)
    st["n"] += frames

with sd.OutputStream(samplerate=sr, channels=1, dtype="float32", callback=cb, latency="low"):
    print(os.getpid(), flush=True)
    sys.stdin.read()
'''
AMP = 0.003


@pytest.fixture
def tone_player() -> Iterator[subprocess.Popen[str]]:
    """Свой процесс, тихо играющий 1 кГц (амплитуда 0,003) — только он и захватывается."""
    if os.environ.get("RUPOR_AUDIO_TESTS") != "1":
        pytest.skip("настоящий захват звука — только с RUPOR_AUDIO_TESTS=1")
    if not loopback.available():
        pytest.skip("process loopback недоступен (Windows старее сборки 20348)")
    try:
        import sounddevice as sd
        sd.query_devices(kind="output")
    except Exception as e:
        pytest.skip(f"нет устройства вывода: {type(e).__name__}")
    proc = subprocess.Popen([sys.executable, "-c", TONE, str(AMP)], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            creationflags=loopback.CREATE_NO_WINDOW)
    try:
        yield proc
    finally:
        try:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(10)
        except Exception:
            proc.kill()


def analyze(a: np.ndarray) -> dict[str, float]:
    rms = float(np.sqrt(np.mean(a.astype(np.float64) ** 2)))
    spec = np.abs(np.fft.rfft(a * np.hanning(len(a))))
    freqs = np.fft.rfftfreq(len(a), 1 / loopback.SAMPLE_RATE)
    band = (freqs > 50) & (freqs < 7900)
    near = (freqs > 980) & (freqs < 1020)
    return {"rms": rms, "peak_hz": float(freqs[band][np.argmax(spec[band])]),
            "tone_share": float((spec[near] ** 2).sum() / max((spec[band] ** 2).sum(), 1e-30))}


def test_real_process_loopback_captures_own_tone(tone_player: subprocess.Popen[str],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    # настоящий Core Audio: сессии только перечисляются (громкость не читается и не меняется)
    monkeypatch.setattr(duck, "default_backend", duck.CoreAudioBackend)
    assert tone_player.stdout is not None
    pid = int(tone_player.stdout.readline())
    # 1. Источник: сессия тона видна, корень — сам интерпретатор (не запускатель .venv)
    src: AudioSource | None = None
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        src = next((s for s in loopback.list_sources(PATTERNS, ()) if s.pid == pid), None)
        if src is not None and src.active:
            break
        time.sleep(0.1)
    assert src is not None and src.active, "сессия тестового тона не появилась"
    assert src.id == f"proc:{pid}" and src.kind == "process" and src.exe.lower() == "python.exe"
    # 2. Захват по pid: звук идёт непрерывно, это наш тон
    got = Got()
    st = LoopbackStream(got.audio, got.exit)
    t0 = time.monotonic()
    st.start(src.target)
    wait_until(lambda: got.samples >= loopback.SAMPLE_RATE * 3, 20)
    wall = time.monotonic() - t0
    a = got.all()
    res = analyze(a[loopback.SAMPLE_RATE // 2:])
    print(f"\nзахват тона: {len(a)} отсчётов за {wall:.2f} с, rms {res['rms']:.6f} "
          f"(ожидается {AMP / np.sqrt(2):.6f}), пик {res['peak_hz']:.0f} Гц, "
          f"доля тона {res['tone_share']:.3f}")
    assert abs(res["peak_hz"] - 1000) <= 5 and res["tone_share"] > 0.9
    # громкость сессии python.exe Windows помнит между запусками; приглушить мог Рупор
    assert 0.3 * AMP / np.sqrt(2) < res["rms"] < 1.2 * AMP / np.sqrt(2)
    assert float(np.abs(a).max()) <= AMP * 1.05
    # 3. Программа-цель закрылась — помощник выходит сам с кодом 3
    assert tone_player.stdin is not None
    tone_player.stdin.close()
    tone_player.wait(10)
    wait_until(lambda: bool(got.exits), 10)
    assert got.exits == [(3, f"target process {pid} exited")]
    assert not st.alive and st.stop() is None
    # 4. Самопроверка сборки (как в --uia-selftest) проходит
    ok, summary = loopback.selftest()
    print(summary)
    assert ok and summary.startswith("захват звука: ok")
