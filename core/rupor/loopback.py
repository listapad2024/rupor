r"""Захват звука для перевода встреч: звук одной программы (process loopback — только то, что
играет её дерево процессов: голоса собеседников из браузера, без своего микрофона) или весь
звук компьютера (loopback устройства вывода по умолчанию).

**Источники** (``list_sources``) — процессы с аудиосессией (в любом состоянии, кроме
завершённой) на активных устройствах вывода, сгруппированные по КОРНЮ: у браузеров и
Electron звук играет дочерний процесс (служба звука), поэтому поднимаемся по родителям, пока
у родителя тот же exe (и тот же путь, если он известен). Захват идёт по корню в режиме
INCLUDE_TARGET_PROCESS_TREE: Windows берёт сам процесс и его ПРЯМЫХ потомков (внуки не
слышны), а служба звука браузера — прямой потомок главного процесса (и переживает свой
перезапуск). Слышны все вкладки браузера, не одна. Свои процессы (``own_pids``) и их
потомки (WebView2 окна Рупора) не показываются. Плюс одна запись ``system`` — весь звук
компьютера (всегда последняя). Громкость сессий не читается и не меняется.

**Захват — в отдельном процессе** (``rupor-core.exe --loopback <цель>``, в разработке
``python -m rupor --loopback <цель>``): буфер виртуального устройства всего 10 мс (около 30 мс
переживают задержку читателя, остальное теряется молча), а в ядре поток захвата делил бы GIL
с распознаванием — измерено: до 22 % звука терялось. Помощник:

- цель: ``pid:<корень>`` | ``system`` | ``exclude:<pid>`` (всё, кроме дерева этого pid);
- stdout — ТОЛЬКО звук: float32 little-endian, моно, 16 кГц, блоками по 320 отсчётов
  (20 мс). Пропуски (молча потерянные пакеты; у ``system`` — тишина без пакетов)
  заполняются нулями по QPC — время потока непрерывно. Запись в канал — в своём потоке:
  цикл захвата не ждёт ядро;
- stderr — журнал (строки «loopback УРОВЕНЬ: …», ядро переписывает их в свой лог);
  последняя строка при ошибке — причина (кратко, по-английски: коды Core Audio);
- выход: stdin закрыт (EOF: ядро закрыло его или умерло) или завершился ``--parent <pid>`` —
  код 0; завершилась программа-цель — 3 (её нет уже при старте — тоже 3); активация не
  удалась (старая Windows, отказ Core Audio, неверная цель) — 2; захват сломался и не
  восстановился — 1. Сломавшийся захват (служба звука перезапустилась, у ``system`` —
  сменилось устройство вывода по умолчанию) переоткрывается, время потока при этом идёт.

Активация process loopback: ``ActivateAudioInterfaceAsync("VAD\\Process_Loopback",
IID_IAudioClient, PROPVARIANT{VT_BLOB → AUDIOCLIENT_ACTIVATION_PARAMS})`` с обработчиком
завершения на ``comtypes.COMObject``, который ОБЯЗАН быть agile (IAgileObject; иначе
E_ILLEGAL_METHOD_CALL — вызов приходит в другом потоке). Формат 16 кГц / моно / float32
принимается как есть (AUTOCONVERTPCM); GetMixFormat на виртуальном устройстве — E_NOTIMPL.
Несуществующий или завершившийся pid ошибок не даёт (идут нулевые пакеты) — помощник держит
дескриптор процесса-цели и выходит сам. Нужна Windows 11 / Server 2022 (сборка ≥ 20348);
``system`` работает и на Windows 10.

Звук захватывается ПОСЛЕ громкости сессии программы (микшер, приглушение Рупора) и до общей
громкости устройства — поэтому на время встречи ``duck.Ducker.set_excluded``.

В ядре ``LoopbackStream`` запускает помощника (CREATE_NO_WINDOW), читает stdout в своём
потоке (блокирующее чтение отпускает GIL) и отдаёт ``on_audio(np.float32[])``;
``on_exit(code, reason)`` — помощник завершился сам.
"""
from __future__ import annotations

import ctypes
import logging
import ntpath
import os
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from concurrent.futures import Future
from ctypes import wintypes as wt
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import (TYPE_CHECKING, Any, Callable, Collection, Iterable, Literal, Mapping,
                    NoReturn, Sequence)

from . import duck, winutil
from .i18n import tr

if TYPE_CHECKING:
    import numpy as np

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000
SAMPLE_BYTES = 4                        # float32
BLOCK_SAMPLES = 320                     # блок stdout помощника: 20 мс
BLOCK_BYTES = BLOCK_SAMPLES * SAMPLE_BYTES
CHUNK_BYTES = 3200                      # ядро читает stdout кусками по 50 мс
MIN_BUILD = 20348                       # process loopback: Windows 11 / Server 2022
SYSTEM_ID = "system"

EXIT_OK = 0             # stdin закрыт, родитель завершился
EXIT_FAILED = 1         # захват сломался и не восстановился
EXIT_ACTIVATION = 2     # захват не начался (причина — последней строкой stderr)
EXIT_TARGET_GONE = 3    # программа-цель завершилась (или её не было)

GAP_SAMPLES = SAMPLE_RATE // 50         # пропуск по QPC длиннее 20 мс — заполнить нулями
IDLE_LAG_SAMPLES = SAMPLE_RATE // 20    # нет пакетов: нули — до «сейчас − 50 мс»...
IDLE_MIN_SAMPLES = SAMPLE_RATE // 10    # ...кусками не мельче 100 мс
MAX_FILL_SAMPLES = SAMPLE_RATE * 10     # больше 10 с нулей разом не пишем (сон компьютера)
WAIT_MS = 50                            # ожидание пакета (и проверок выхода) не дольше
POLL_MS = 10                            # без события (старая Windows) — опрос раз в 10 мс
OUT_MAX_BYTES = SAMPLE_RATE * SAMPLE_BYTES * 10   # ядро не читает 10 с — старое выбрасываем
DEVICE_CHECK_S = 2.0                    # system: сменилось ли устройство вывода по умолчанию
REOPEN_TRIES = 10                       # захват сломался: попыток переоткрыть
REOPEN_PAUSE_S = 0.5
ACTIVATE_TIMEOUT_S = 5.0
STOP_TIMEOUT_S = 1.0
LIST_TIMEOUT_S = 3.0
STDERR_LINES_MAX = 200                  # столько строк журнала помощника переписываем в лог

CREATE_NO_WINDOW = 0x08000000
SYNCHRONIZE = 0x00100000
ERROR_ACCESS_DENIED = 5
WAIT_OBJECT_0, WAIT_TIMEOUT, WAIT_FAILED = 0, 0x102, 0xFFFFFFFF
STD_INPUT_HANDLE, STD_OUTPUT_HANDLE, STD_ERROR_HANDLE = -10, -11, -12

# Core Audio
_VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
_MODE_INCLUDE_TREE, _MODE_EXCLUDE_TREE = 0, 1
_VT_BLOB = 65
_SHAREMODE_SHARED = 0
_STREAMFLAGS_LOOPBACK = 0x00020000
_STREAMFLAGS_EVENTCALLBACK = 0x00040000
_STREAMFLAGS_SRC_DEFAULT_QUALITY = 0x08000000
_STREAMFLAGS_AUTOCONVERTPCM = 0x80000000
_BUFFERFLAGS_SILENT = 0x2
_BUFFER_HNS = 200_000                   # 20 мс (у process loopback всё равно 10 мс)
_E_RENDER, _E_CONSOLE = 0, 0
_CLSCTX_ALL = 0x17
_E_NOTFOUND = 0x80070490
_HR_NAMES = {
    0x80004001: "E_NOTIMPL", 0x80004002: "E_NOINTERFACE", 0x80004005: "E_FAIL",
    0x80070005: "E_ACCESSDENIED", 0x80070057: "E_INVALIDARG",
    0x8000000E: "E_ILLEGAL_METHOD_CALL", _E_NOTFOUND: "E_NOTFOUND",
    0x88890004: "AUDCLNT_E_DEVICE_INVALIDATED", 0x88890008: "AUDCLNT_E_UNSUPPORTED_FORMAT",
    0x8889000A: "AUDCLNT_E_DEVICE_IN_USE", 0x88890010: "AUDCLNT_E_SERVICE_NOT_RUNNING",
    0x88890021: "AUDCLNT_E_INVALID_STREAM_FLAG",
}


# ---------- цель захвата ----------

TargetKind = Literal["pid", "system", "exclude"]


@dataclass(frozen=True)
class Target:
    """Что захватывать: дерево процесса (``pid``), всё, кроме дерева (``exclude``), или весь
    звук устройства вывода по умолчанию (``system``)."""
    kind: TargetKind
    pid: int | None = None

    def __str__(self) -> str:
        return SYSTEM_ID if self.kind == "system" else f"{self.kind}:{self.pid}"


def parse_target(spec: str) -> Target:
    """``pid:<pid>`` | ``system`` | ``exclude:<pid>`` → Target; иначе ValueError."""
    s = (spec or "").strip().lower()
    if s == SYSTEM_ID:
        return Target("system")
    kind, sep, num = s.partition(":")
    if sep and num.isascii() and num.isdigit() and 0 < int(num) < 2 ** 32:
        if kind == "pid":
            return Target("pid", int(num))
        if kind == "exclude":
            return Target("exclude", int(num))
    raise ValueError(f"bad loopback target {spec!r} (pid:<pid> | system | exclude:<pid>)")


def target_for_id(source_id: str) -> str:
    """Источник ``AudioSource.id`` (``proc:<pid>`` | ``system``) → цель помощника; иначе
    ValueError."""
    if source_id == SYSTEM_ID:
        return SYSTEM_ID
    kind, sep, num = source_id.partition(":")
    if kind == "proc" and sep:
        return str(parse_target(f"pid:{num}"))
    raise ValueError(f"bad audio source id {source_id!r}")


def available() -> bool:
    """Захват звука отдельной программы поддерживается (Windows сборки ≥ 20348)."""
    try:
        if sys.getwindowsversion().build < MIN_BUILD:
            return False
        return hasattr(_mmdevapi(), "ActivateAudioInterfaceAsync")
    except (AttributeError, OSError):
        return False


def _windows_build() -> int:
    try:
        return int(sys.getwindowsversion().build)
    except AttributeError:
        return 0


# ---------- источники звука ----------

SourceKind = Literal["process", "system"]


@dataclass
class AudioSource:
    """Источник звука встречи (WS ``meeting_sources``)."""
    id: str                 # "proc:<корневой pid>" | "system"
    kind: SourceKind
    pid: int | None         # корень дерева процессов (None — весь звук)
    exe: str                # имя exe корня: "brave.exe"
    name: str               # понятное имя: описание exe («Brave Browser») или имя без .exe
    title: str              # заголовок окна программы (предпочтительно — со встречей)
    meeting: bool           # заголовок похож на встречу (meeting_title_patterns)
    active: bool            # звук сейчас играет (сессия AudioSessionStateActive)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def target(self) -> str:
        """Цель помощника: ``pid:<корень>`` или ``system``."""
        return f"pid:{self.pid}" if self.kind == "process" else SYSTEM_ID


@dataclass(frozen=True)
class SessionInfo:
    """Аудиосессия программы: pid процесса, который играет звук, и играет ли сейчас."""
    pid: int
    active: bool


ProcessTable = Mapping[int, tuple[int, str]]      # pid → (pid родителя, имя exe)


def matches_meeting(title: str, patterns: Iterable[str]) -> bool:
    """Заголовок окна похож на встречу: содержит один из шаблонов (без учёта регистра)."""
    t = (title or "").casefold()
    return bool(t) and any(p and p.casefold() in t for p in patterns)


def root_pid(pid: int, table: ProcessTable,
             path_of: Callable[[int], str] | None = None) -> int:
    """Корень программы: поднимаемся по родителям, пока у родителя то же имя exe (и тот же
    полный путь, если path_of его знает: запускатель .venv\\Scripts\\python.exe — не тот же
    exe, что сам Python). Звук браузера играет служба звука — дочерний процесс главного."""
    name = table.get(pid, (0, ""))[1].casefold()
    cur, seen = pid, {pid}
    if not name:
        return pid
    while True:
        ppid = table.get(cur, (0, ""))[0]
        if not ppid or ppid in seen or ppid not in table:
            break
        if table[ppid][1].casefold() != name:
            break
        if path_of is not None:
            a, b = path_of(cur), path_of(ppid)
            if a and b and a.casefold() != b.casefold():
                break
        seen.add(ppid)
        cur = ppid
    return cur


def _children(table: ProcessTable) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for pid, (ppid, _exe) in table.items():
        if ppid != pid:
            out.setdefault(ppid, []).append(pid)
    return out


def _tree(root: int, children: Mapping[int, list[int]]) -> set[int]:
    """Корень и все его потомки."""
    out, stack = {root}, [root]
    while stack:
        for c in children.get(stack.pop(), ()):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


def _descends_from(pid: int, ancestors: Collection[int], table: ProcessTable) -> bool:
    cur, seen = pid, {pid}
    for _ in range(64):
        ppid = table.get(cur, (0, ""))[0]
        if not ppid or ppid in seen:
            return False
        if ppid in ancestors:
            return True
        seen.add(ppid)
        cur = ppid
    return False


def system_source(active: bool = False) -> AudioSource:
    """Запись «весь звук компьютера» (loopback устройства вывода по умолчанию)."""
    return AudioSource(SYSTEM_ID, "system", None, "", tr("loopback.system_name"), "", False,
                       active)


def group_sources(sessions: Iterable[SessionInfo], own_pids: Collection[int],
                  patterns: Sequence[str], *,
                  table: ProcessTable | None = None,
                  path_of: Callable[[int], str] | None = None,
                  describe: Callable[[str], str] | None = None,
                  titles_of: Callable[[Collection[int]], Mapping[int, list[str]]] | None = None,
                  ) -> list[AudioSource]:
    """Сессии → источники по корням программ (без своих процессов и их потомков), в порядке:
    встреча, играет, имя; последней — «весь звук компьютера». Процессы, окна и описания exe
    по умолчанию — настоящие (``winutil``); тесты подставляют свои."""
    sess = [s for s in sessions if s.pid]
    out: list[AudioSource] = []
    any_active = False
    if sess:
        tbl = winutil.process_table() if table is None else table
        get_path = path_of or winutil.process_path
        get_desc = describe or winutil.file_description
        get_titles = titles_of or winutil.window_titles
        own = frozenset(own_pids)
        paths: dict[int, str] = {}

        def path(p: int) -> str:
            if p not in paths:
                try:
                    paths[p] = get_path(p)
                except OSError:
                    paths[p] = ""
            return paths[p]

        roots: dict[int, bool] = {}
        for s in sess:
            if s.pid not in tbl or s.pid in own or _descends_from(s.pid, own, tbl):
                continue                  # процесс уже завершился / свой
            r = root_pid(s.pid, tbl, path)
            if r in own:
                continue
            roots[r] = roots.get(r, False) or s.active
        children = _children(tbl)
        trees = {r: _tree(r, children) for r in roots}
        wanted: set[int] = set()
        for t in trees.values():
            wanted |= t
        titles = get_titles(wanted) if wanted else {}
        for r, active in roots.items():
            full = path(r)
            exe = ntpath.basename(full) if full else tbl[r][1]
            name = (get_desc(full) if full else "") or ntpath.splitext(exe)[0] or exe
            cand = [*titles.get(r, []),
                    *(t for p in sorted(trees[r] - {r}) for t in titles.get(p, []))]
            title = next((t for t in cand if matches_meeting(t, patterns)),
                         cand[0] if cand else "")
            out.append(AudioSource(f"proc:{r}", "process", r, exe, name, title,
                                   matches_meeting(title, patterns), active))
            any_active = any_active or active
    out.sort(key=lambda a: (not a.meeting, not a.active, a.name.casefold(), a.pid or 0))
    out.append(system_source(any_active))
    return out


def list_sources(patterns: Sequence[str], own_pids: Collection[int] = frozenset(),
                 timeout: float = LIST_TIMEOUT_S) -> list[AudioSource]:
    """Источники звука встречи (дёшево: десятки мс; ничего не меняет). Core Audio — в своём
    потоке (COM MTA); не ответил за timeout или ошибка — только «весь звук компьютера»."""
    pats, own = tuple(patterns), frozenset(own_pids)
    try:
        found: list[AudioSource] = _com_call(lambda: _list_sources_now(pats, own), timeout)
        return found
    except TimeoutError:
        log.warning("Источники звука: Core Audio не ответил за %.0f с", timeout)
    except Exception as e:
        log.warning("Источники звука: не перечислены (%s)", type(e).__name__)
    return [system_source()]


def _list_sources_now(patterns: Sequence[str], own: frozenset[int]) -> list[AudioSource]:
    infos: list[SessionInfo] = []
    for s in duck.default_backend().sessions():   # тесты: NullBackend (tests/conftest.py)
        try:
            if s.system or not s.pid:
                continue
            infos.append(SessionInfo(int(s.pid), bool(s.active)))
        except Exception:
            continue
    return group_sources(infos, own, patterns)


class _ComWorker:
    """Один фоновый поток с COM (MTA) для коротких вызовов Core Audio из любого потока."""

    def __init__(self, name: str) -> None:
        self._name = name
        self._q: queue.SimpleQueue[tuple[Callable[[], Any], Future[Any]]] = queue.SimpleQueue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def call(self, fn: Callable[[], Any], timeout: float) -> Any:
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name=self._name, daemon=True)
                self._thread.start()
        fut: Future[Any] = Future()
        self._q.put((fn, fut))
        try:
            return fut.result(timeout)
        except TimeoutError:
            fut.cancel()
            raise

    def _run(self) -> None:
        try:
            duck._com_init()
        except Exception as e:
            log.debug("Источники звука: COM не инициализирован (%s)", type(e).__name__)
        while True:
            fn, fut = self._q.get()
            if not fut.set_running_or_notify_cancel():
                continue
            try:
                fut.set_result(fn())
            except BaseException as e:
                fut.set_exception(e)


_worker = _ComWorker("rupor-sources")


def _com_call(fn: Callable[[], Any], timeout: float) -> Any:
    return _worker.call(fn, timeout)


# ---------- Win32 и Core Audio (comtypes) ----------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wt.BOOL, wt.BOOL, wt.LPCWSTR]
_k32.CreateEventW.restype = wt.HANDLE
_k32.SetEvent.argtypes = [wt.HANDLE]
_k32.SetEvent.restype = wt.BOOL
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
_k32.WaitForSingleObject.restype = wt.DWORD
_k32.WaitForMultipleObjects.argtypes = [wt.DWORD, ctypes.POINTER(wt.HANDLE), wt.BOOL,
                                        wt.DWORD]
_k32.WaitForMultipleObjects.restype = wt.DWORD
_k32.GetStdHandle.argtypes = [wt.DWORD]
_k32.GetStdHandle.restype = wt.HANDLE
_k32.WriteFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                           ctypes.c_void_p]
_k32.WriteFile.restype = wt.BOOL
_k32.ReadFile.argtypes = [wt.HANDLE, ctypes.c_void_p, wt.DWORD, ctypes.POINTER(wt.DWORD),
                          ctypes.c_void_p]
_k32.ReadFile.restype = wt.BOOL
_k32.QueryPerformanceCounter.argtypes = [ctypes.POINTER(ctypes.c_longlong)]
_k32.QueryPerformanceFrequency.argtypes = [ctypes.POINTER(ctypes.c_longlong)]

_INVALID_HANDLE = ctypes.c_void_p(-1).value


def _qpf() -> int:
    f = ctypes.c_longlong()
    _k32.QueryPerformanceFrequency(ctypes.byref(f))
    return int(f.value) or 1


_QPF = _qpf()


def qpc_now() -> int:
    """QueryPerformanceCounter в единицах 100 нс — те же часы, что u64QPCPosition пакетов."""
    v = ctypes.c_longlong()
    _k32.QueryPerformanceCounter(ctypes.byref(v))
    return int(v.value) * 10_000_000 // _QPF


class WAVEFORMATEX(ctypes.Structure):
    _pack_ = 1
    _fields_ = [("wFormatTag", wt.WORD), ("nChannels", wt.WORD), ("nSamplesPerSec", wt.DWORD),
                ("nAvgBytesPerSec", wt.DWORD), ("nBlockAlign", wt.WORD),
                ("wBitsPerSample", wt.WORD), ("cbSize", wt.WORD)]


class _LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", wt.DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class AUDIOCLIENT_ACTIVATION_PARAMS(ctypes.Structure):
    # union из одного члена (AUDIOCLIENT_PROCESS_LOOPBACK_PARAMS) — 12 байт
    _fields_ = [("ActivationType", ctypes.c_int), ("ProcessLoopbackParams", _LOOPBACK_PARAMS)]


class _BLOB(ctypes.Structure):
    _fields_ = [("cbSize", wt.ULONG), ("pBlobData", ctypes.c_void_p)]


class PROPVARIANT(ctypes.Structure):
    # vt и три резервных WORD, затем union со смещения 8; BLOB на x64 — {ULONG, выравнивание,
    # BYTE*}: 24 байта всего. PropVariantClear не звать — blob наш
    _fields_ = [("vt", wt.USHORT), ("wReserved1", wt.USHORT), ("wReserved2", wt.USHORT),
                ("wReserved3", wt.USHORT), ("blob", _BLOB)]


def _float_format() -> WAVEFORMATEX:
    """16 кГц, моно, IEEE float 32 — то, что нужно распознаванию, без пересчёта в Python."""
    return WAVEFORMATEX(3, 1, SAMPLE_RATE, SAMPLE_RATE * SAMPLE_BYTES, SAMPLE_BYTES, 32, 0)


_mmdev: ctypes.WinDLL | None = None


def _mmdevapi() -> ctypes.WinDLL:
    global _mmdev
    if _mmdev is None:
        _mmdev = ctypes.WinDLL("Mmdevapi.dll")
    return _mmdev


_ifaces_lock = threading.Lock()
_ifaces: Any = None


def _interfaces() -> Any:
    """Интерфейсы захвата (создаются один раз, после импорта comtypes в потоке с COM MTA)."""
    global _ifaces
    with _ifaces_lock:
        if _ifaces is None:
            _ifaces = _define_interfaces()
        return _ifaces


def _define_interfaces() -> Any:
    from ctypes import HRESULT, POINTER, c_long, c_uint32, c_uint64, c_void_p, c_wchar_p
    from types import SimpleNamespace

    from comtypes import COMMETHOD, GUID, COMObject, IUnknown

    REFERENCE_TIME = ctypes.c_longlong
    LPCGUID = POINTER(GUID)

    class IAudioClient(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Initialize", (["in"], ctypes.c_int, "ShareMode"),
                      (["in"], wt.DWORD, "StreamFlags"),
                      (["in"], REFERENCE_TIME, "hnsBufferDuration"),
                      (["in"], REFERENCE_TIME, "hnsPeriodicity"), (["in"], c_void_p, "pFormat"),
                      (["in"], LPCGUID, "AudioSessionGuid")),
            COMMETHOD([], HRESULT, "GetBufferSize",
                      (["out"], POINTER(c_uint32), "pNumBufferFrames")),
            COMMETHOD([], HRESULT, "GetStreamLatency",
                      (["out"], POINTER(REFERENCE_TIME), "phnsLatency")),
            COMMETHOD([], HRESULT, "GetCurrentPadding",
                      (["out"], POINTER(c_uint32), "pNumPaddingFrames")),
            COMMETHOD([], HRESULT, "IsFormatSupported", (["in"], ctypes.c_int, "ShareMode"),
                      (["in"], c_void_p, "pFormat"),
                      (["out"], POINTER(c_void_p), "ppClosestMatch")),
            COMMETHOD([], HRESULT, "GetMixFormat", (["out"], POINTER(c_void_p), "ppDeviceFormat")),
            COMMETHOD([], HRESULT, "GetDevicePeriod",
                      (["out"], POINTER(REFERENCE_TIME), "phnsDefaultDevicePeriod"),
                      (["out"], POINTER(REFERENCE_TIME), "phnsMinimumDevicePeriod")),
            COMMETHOD([], HRESULT, "Start"),
            COMMETHOD([], HRESULT, "Stop"),
            COMMETHOD([], HRESULT, "Reset"),
            COMMETHOD([], HRESULT, "SetEventHandle", (["in"], wt.HANDLE, "eventHandle")),
            COMMETHOD([], HRESULT, "GetService", (["in"], LPCGUID, "riid"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppv")),
        ]

    class IAudioCaptureClient(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetBuffer", (["out"], POINTER(c_void_p), "ppData"),
                      (["out"], POINTER(c_uint32), "pNumFramesToRead"),
                      (["out"], POINTER(wt.DWORD), "pdwFlags"),
                      (["out"], POINTER(c_uint64), "pu64DevicePosition"),
                      (["out"], POINTER(c_uint64), "pu64QPCPosition")),
            COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], c_uint32, "NumFramesRead")),
            COMMETHOD([], HRESULT, "GetNextPacketSize",
                      (["out"], POINTER(c_uint32), "pNumFramesInNextPacket")),
        ]

    class IActivateAudioInterfaceAsyncOperation(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetActivateResult",
                      (["out"], POINTER(c_long), "activateResult"),
                      (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
        ]

    class IActivateAudioInterfaceCompletionHandler(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
        _methods_ = [
            # сырой указатель нарочно: входной POINTER(интерфейс) в вызове «к нам» comtypes
            # отпустил бы (Release) при сборке мусора без парного AddRef
            COMMETHOD([], HRESULT, "ActivateCompleted",
                      (["in"], c_void_p, "activateOperation")),
        ]

    class IAgileObject(IUnknown):  # type: ignore[misc]
        """Метка без методов: объект можно звать из любого апартамента (без неё активация —
        E_ILLEGAL_METHOD_CALL: обработчик зовётся в рабочем потоке Windows)."""
        _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
        _methods_: list[Any] = []

    class CompletionHandler(COMObject):  # type: ignore[misc]
        _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

        def __init__(self) -> None:
            super().__init__()
            self.done = threading.Event()

        def ActivateCompleted(self, activateOperation: Any) -> int:  # noqa: N803 — поток Windows
            self.done.set()
            return 0

    activate = _mmdevapi().ActivateAudioInterfaceAsync
    activate.restype = HRESULT
    activate.argtypes = [c_wchar_p, POINTER(GUID), POINTER(PROPVARIANT),
                         POINTER(IActivateAudioInterfaceCompletionHandler),
                         POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    return SimpleNamespace(
        IAudioClient=IAudioClient, IAudioCaptureClient=IAudioCaptureClient,
        IActivateAudioInterfaceAsyncOperation=IActivateAudioInterfaceAsyncOperation,
        IActivateAudioInterfaceCompletionHandler=IActivateAudioInterfaceCompletionHandler,
        CompletionHandler=CompletionHandler, activate=activate, GUID=GUID, POINTER=POINTER)


def _hresult(e: BaseException) -> int:
    """HRESULT ошибки comtypes (COMError.hresult) или ctypes (OSError.winerror); 0 — нет."""
    hr = getattr(e, "hresult", None)
    if hr is None:
        hr = getattr(e, "winerror", None)
    return hr & 0xFFFFFFFF if isinstance(hr, int) else 0


def hr_name(hr: int) -> str:
    """«0x80070005 E_ACCESSDENIED»."""
    hr &= 0xFFFFFFFF
    name = _HR_NAMES.get(hr)
    return f"0x{hr:08X}" + (f" {name}" if name else "")


def hr_text(e: BaseException) -> str:
    """Ошибка COM/Win32 кратко: «0x80070005 E_ACCESSDENIED» (или имя исключения)."""
    hr = _hresult(e)
    if hr:
        return hr_name(hr)
    text = str(e).strip().splitlines()[0] if str(e).strip() else ""
    return type(e).__name__ + (f": {text}" if text else "")


class CaptureError(Exception):
    """Захват не начался или сломался; текст — причина одной строкой (для stderr)."""


class NoOutputDevice(CaptureError):
    """Весь звук компьютера: нет устройства вывода."""


def _default_render(enum: Any = None) -> tuple[Any, str]:
    """(устройство вывода по умолчанию, его id); нет устройства — NoOutputDevice."""
    comtypes = duck._com_init()
    di = duck._interfaces()
    if enum is None:
        enum = comtypes.CoCreateInstance(di.GUID(duck._CLSID_MMDEVICE_ENUMERATOR),
                                         interface=di.IMMDeviceEnumerator,
                                         clsctx=comtypes.CLSCTX_INPROC_SERVER)
    try:
        dev = enum.GetDefaultAudioEndpoint(_E_RENDER, _E_CONSOLE)
    except Exception as e:
        if _hresult(e) == _E_NOTFOUND:
            raise NoOutputDevice("no audio output device") from e
        raise
    return dev, duck._take_str(dev.GetId())


class _Capture:
    """Один IAudioClient в режиме loopback (процесс или устройство вывода по умолчанию).
    Создаётся и используется в одном потоке (COM MTA)."""

    def __init__(self, target: Target) -> None:
        self.target = target
        self.fmt = _float_format()          # держим: Initialize получает его адрес
        self.ac: Any = None
        self.cap: Any = None
        self.event: int | None = None
        self.device_id = ""
        self.packets = 0

    def open(self) -> None:
        """Активировать и инициализировать (сначала с событием, не вышло — опросом)."""
        first: BaseException | None = None
        for event in (True, False):
            try:
                self._open(event)
                return
            except NoOutputDevice:
                self.close()
                raise
            except Exception as e:
                self.close()
                if first is None:
                    first = e
        assert first is not None
        if isinstance(first, CaptureError):
            raise first
        raise CaptureError(hr_text(first)) from first

    def _open(self, event: bool) -> None:
        ns = _interfaces()
        if self.target.kind == "system":
            dev, self.device_id = _default_render()
            self.ac = dev.Activate(ns.IAudioClient._iid_, _CLSCTX_ALL, None).QueryInterface(
                ns.IAudioClient)
        else:
            assert self.target.pid is not None
            self.ac = _activate_process(self.target.pid, self.target.kind == "pid")
        flags = (_STREAMFLAGS_LOOPBACK | _STREAMFLAGS_AUTOCONVERTPCM
                 | _STREAMFLAGS_SRC_DEFAULT_QUALITY)
        if event:
            flags |= _STREAMFLAGS_EVENTCALLBACK
        self.ac.Initialize(_SHAREMODE_SHARED, flags, _BUFFER_HNS, 0, ctypes.addressof(self.fmt),
                           None)
        if event:
            ev = _k32.CreateEventW(None, False, False, None)
            if not ev:
                raise CaptureError(f"CreateEventW failed ({ctypes.get_last_error()})")
            self.event = int(ev)
            self.ac.SetEventHandle(self.event)
        iid = ns.GUID(str(ns.IAudioCaptureClient._iid_))
        self.cap = self.ac.GetService(ctypes.byref(iid)).QueryInterface(ns.IAudioCaptureClient)

    def start(self) -> None:
        self.ac.Start()

    def read(self) -> list[tuple[bytes | int, int]]:
        """Все готовые пакеты: (данные или число отсчётов тишины, QPC начала пакета)."""
        out: list[tuple[bytes | int, int]] = []
        cap = self.cap
        while cap.GetNextPacketSize():
            addr, frames, flags, _devpos, qpc = cap.GetBuffer()
            try:
                if frames:
                    if flags & _BUFFERFLAGS_SILENT or not addr:
                        out.append((int(frames), int(qpc)))
                    else:
                        out.append((ctypes.string_at(addr, frames * SAMPLE_BYTES), int(qpc)))
            finally:
                cap.ReleaseBuffer(frames)
        self.packets += len(out)
        return out

    def close(self) -> None:
        if self.ac is not None:
            try:
                self.ac.Stop()
            except Exception:
                pass
        self.cap = None
        self.ac = None
        if self.event:
            _k32.CloseHandle(self.event)
            self.event = None


def _activate_process(pid: int, include: bool) -> Any:
    """IAudioClient виртуального устройства process loopback для дерева pid."""
    ns = _interfaces()
    params = AUDIOCLIENT_ACTIVATION_PARAMS()
    params.ActivationType = _ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = pid
    params.ProcessLoopbackParams.ProcessLoopbackMode = (_MODE_INCLUDE_TREE if include
                                                        else _MODE_EXCLUDE_TREE)
    pv = PROPVARIANT()
    pv.vt = _VT_BLOB
    pv.blob.cbSize = ctypes.sizeof(params)
    pv.blob.pBlobData = ctypes.addressof(params)
    handler = ns.CompletionHandler()
    hp = handler.QueryInterface(ns.IActivateAudioInterfaceCompletionHandler)
    op = ns.POINTER(ns.IActivateAudioInterfaceAsyncOperation)()
    iid = ns.GUID(str(ns.IAudioClient._iid_))
    try:
        ns.activate(_VAD_PROCESS_LOOPBACK, ctypes.byref(iid), ctypes.byref(pv), hp,
                    ctypes.byref(op))
    except OSError as e:
        raise CaptureError(f"ActivateAudioInterfaceAsync: {hr_text(e)}") from e
    if not handler.done.wait(ACTIVATE_TIMEOUT_S):
        raise CaptureError("ActivateAudioInterfaceAsync: no completion in "
                           f"{ACTIVATE_TIMEOUT_S:g} s")
    hr, unk = op.GetActivateResult()
    if hr < 0:
        raise CaptureError(f"GetActivateResult: {hr_name(hr)}")
    del hp, handler, op
    return unk.QueryInterface(ns.IAudioClient)


# ---------- время потока ----------

class Timeline:
    """Время потока помощника в отсчётах от старта захвата (часы — QPC пакетов, 100 нс).
    ``packet`` — сколько нулей вставить перед пакетом (пропуск длиннее 20 мс: пакеты
    потеряны молча или их не было), ``idle`` — сколько нулей дописать, когда пакетов нет
    (``system`` в тишине): до «сейчас − 50 мс», кусками от 100 мс. Больше 10 с нулей разом
    не выдаётся — дальше время просто перескакивает (компьютер спал)."""

    def __init__(self, t0: int, rate: int = SAMPLE_RATE) -> None:
        self.t0 = t0
        self.rate = rate
        self.written = 0                  # позиция конца уже выданного (отсчёты)

    def _pos(self, qpc: int) -> int:
        return (qpc - self.t0) * self.rate // 10_000_000

    def packet(self, frames: int, qpc: int) -> int:
        zeros = 0
        if qpc:
            gap = self._pos(qpc) - self.written
            if gap > GAP_SAMPLES:
                zeros = min(gap, MAX_FILL_SAMPLES)
                self.written += gap
        self.written += frames
        return zeros

    def idle(self, now_qpc: int) -> int:
        gap = self._pos(now_qpc) - IDLE_LAG_SAMPLES - self.written
        if gap < IDLE_MIN_SAMPLES:
            return 0
        self.written += gap
        return min(gap, MAX_FILL_SAMPLES)


# ---------- помощник (отдельный процесс) ----------

def _write_all(handle: int, data: bytes) -> bool:
    """WriteFile целиком (канал может принять часть); False — канал закрыт."""
    if not data:
        return True
    buf = ctypes.create_string_buffer(data, len(data))
    base = ctypes.addressof(buf)
    off = 0
    done = wt.DWORD()
    while off < len(data):
        if not _k32.WriteFile(handle, base + off, len(data) - off, ctypes.byref(done), None) \
                or not done.value:
            return False
        off += done.value
    return True


def _std_handle(which: int) -> int | None:
    h = _k32.GetStdHandle(which)
    return None if not h or h == _INVALID_HANDLE else int(h)


class _StderrHandler(logging.Handler):
    """Журнал помощника → stderr (ядро переписывает строки в свой лог)."""

    def __init__(self, handle: int) -> None:
        super().__init__()
        self._h = handle

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = self.format(record).replace("\r", " ").replace("\n", " | ")
            _write_all(self._h, (line + "\n").encode("utf-8", "replace"))
        except Exception:
            pass


class _Writer:
    """Поток записи в stdout: цикл захвата не ждёт канал (ядро читает с задержками GIL).
    Ядро не читает дольше 10 с (OUT_MAX_BYTES) — старые блоки выбрасываются."""

    def __init__(self, handle: int | None, on_broken: Callable[[], None]) -> None:
        self._h = handle
        self._on_broken = on_broken
        self._q: deque[bytes] = deque()
        self._size = 0
        self._cv = threading.Condition()
        self._closed = False
        self.broken = handle is None
        self.dropped = 0
        self._thread = threading.Thread(target=self._run, name="loopback-out", daemon=True)

    def start(self) -> None:
        self._thread.start()

    def put(self, data: bytes) -> None:
        warn = False
        with self._cv:
            self._q.append(data)
            self._size += len(data)
            while self._size > OUT_MAX_BYTES and len(self._q) > 1:
                old = self._q.popleft()
                self._size -= len(old)
                warn = warn or not self.dropped
                self.dropped += len(old)
            self._cv.notify()
        if warn:
            log.warning("ядро не читает звук больше %d с — старое выбрасываю",
                        OUT_MAX_BYTES // (SAMPLE_RATE * SAMPLE_BYTES))

    def close(self, timeout: float) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify()
        self._thread.join(timeout)

    def _run(self) -> None:
        if self._h is None:
            self._on_broken()
            return
        while True:
            with self._cv:
                while not self._q and not self._closed:
                    self._cv.wait()
                if not self._q:
                    return
                data = self._q.popleft()
                self._size -= len(data)
            if not _write_all(self._h, data):
                self.broken = True
                self._on_broken()
                return


def _watch_stdin(handle: int | None, on_eof: Callable[[], None]) -> None:
    """Поток: читать stdin до EOF (ядро закрыло канал или умерло) — тогда on_eof()."""
    if handle is None:
        return

    def run() -> None:
        buf = ctypes.create_string_buffer(256)
        n = wt.DWORD()
        while _k32.ReadFile(handle, buf, 256, ctypes.byref(n), None) and n.value:
            pass
        on_eof()

    threading.Thread(target=run, name="loopback-stdin", daemon=True).start()


def _wait_any(handles: Sequence[int], ms: int) -> int:
    """Индекс сработавшего объекта, -1 — время вышло (WAIT_FAILED — OSError)."""
    arr = (wt.HANDLE * len(handles))(*handles)
    r = int(_k32.WaitForMultipleObjects(len(handles), arr, False, ms))
    if r == WAIT_TIMEOUT:
        return -1
    if r == WAIT_FAILED or not WAIT_OBJECT_0 <= r < WAIT_OBJECT_0 + len(handles):
        raise OSError(ctypes.get_last_error(), "WaitForMultipleObjects failed")
    return r - WAIT_OBJECT_0


class _Helper:
    """Цикл помощника: события захвата, stdin, родитель, цель; склейка времени; переоткрытие."""

    def __init__(self, target: Target, stop_ev: int, watch: list[tuple[int, int]],
                 out: _Writer) -> None:
        self.target = target
        self.stop_ev = stop_ev
        self.watch = watch                # (дескриптор, код выхода, когда он сработал)
        self.out = out
        self.cap: _Capture | None = None
        self.timeline = Timeline(0)
        self.pending = bytearray()
        self.reason = ""
        self._enum: Any = None

    def _exit_handles(self) -> list[int]:
        return [self.stop_ev, *(h for h, _code in self.watch)]

    def _exit_code(self, idx: int) -> int:
        return EXIT_OK if idx == 0 else self.watch[idx - 1][1]

    def _emit(self, data: bytes | int) -> None:
        self.pending += bytes(data * SAMPLE_BYTES) if isinstance(data, int) else data

    def _flush(self) -> None:
        n = len(self.pending) - len(self.pending) % BLOCK_BYTES
        if n:
            self.out.put(bytes(self.pending[:n]))
            del self.pending[:n]

    def _idle_fill(self) -> None:
        z = self.timeline.idle(qpc_now())
        if z:
            self._emit(z)

    def run(self, cap: _Capture) -> int:
        self.cap = cap
        cap.start()
        self.timeline = Timeline(qpc_now())
        log.info("Захват звука начат (%s, %s)", self.target,
                 "по событию" if cap.event else "опросом")
        next_check = time.monotonic() + DEVICE_CHECK_S
        while True:
            assert self.cap is not None
            handles = self._exit_handles()
            ev = self.cap.event
            idx = _wait_any([*handles, ev] if ev else handles, WAIT_MS if ev else POLL_MS)
            if 0 <= idx < len(handles):
                return self._exit_code(idx)
            failure: Exception | None = None
            try:
                for data, qpc in self.cap.read():
                    frames = data if isinstance(data, int) else len(data) // SAMPLE_BYTES
                    z = self.timeline.packet(frames, qpc)
                    if z:
                        self._emit(z)
                    self._emit(data)
            except Exception as e:
                failure = e
            self._idle_fill()
            self._flush()
            if self.out.broken:
                log.info("ядро закрыло канал звука — выхожу")
                return EXIT_OK
            why = ""
            if failure is not None:
                why = f"capture error {hr_text(failure)}"
            elif self.target.kind == "system" and time.monotonic() >= next_check:
                next_check = time.monotonic() + DEVICE_CHECK_S
                why = self._device_changed()
            if why:
                code = self._reopen(why)
                if code is not None:
                    return code

    def _device_changed(self) -> str:
        """system: сменилось устройство вывода по умолчанию — причина переоткрыть ("" — нет)."""
        assert self.cap is not None
        try:
            if self._enum is None:
                comtypes = duck._com_init()
                di = duck._interfaces()
                self._enum = comtypes.CoCreateInstance(
                    di.GUID(duck._CLSID_MMDEVICE_ENUMERATOR), interface=di.IMMDeviceEnumerator,
                    clsctx=comtypes.CLSCTX_INPROC_SERVER)
            _dev, dev_id = _default_render(self._enum)
        except NoOutputDevice:
            return ""                     # устройства нет — ждём на старом
        except Exception as e:
            self._enum = None
            log.debug("устройство вывода по умолчанию не прочитано (%s)", hr_text(e))
            return ""
        return "default output device changed" if dev_id != self.cap.device_id else ""

    def _reopen(self, why: str) -> int | None:
        """Переоткрыть захват (время потока идёт: нули по часам). None — снова захватываем,
        иначе код выхода."""
        log.warning("Захват звука: %s — переоткрываю", why)
        assert self.cap is not None
        self.cap.close()
        last = why
        for _ in range(REOPEN_TRIES):
            idx = _wait_any(self._exit_handles(), int(REOPEN_PAUSE_S * 1000))
            if idx >= 0:
                return self._exit_code(idx)
            self._idle_fill()
            self._flush()
            cap = _Capture(self.target)
            try:
                cap.open()
                cap.start()
            except CaptureError as e:
                last = str(e)
                continue
            self.cap = cap
            log.info("Захват звука переоткрыт (%s)", "по событию" if cap.event else "опросом")
            return None
        self.reason = f"capture failed: {last}"
        return EXIT_FAILED


def run_helper(spec: str, parent: int | None) -> int:
    """Режим помощника ``--loopback <цель> [--parent <pid>]``: звук — в stdout, журнал — в
    stderr. Возвращает код выхода (0 / 1 / 2 / 3, см. описание модуля)."""
    err = _std_handle(STD_ERROR_HANDLE)
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if os.environ.get("RUPOR_LOOPBACK_DEBUG") else logging.INFO)
    if err is not None:
        handler = _StderrHandler(err)
        handler.setFormatter(logging.Formatter("loopback %(levelname)s: %(message)s"))
        root.addHandler(handler)

    def fail(code: int, reason: str) -> int:
        if err is not None:
            _write_all(err, (reason + "\n").encode("utf-8", "replace"))
        return code

    try:
        target = parse_target(spec)
    except ValueError as e:
        return fail(EXIT_ACTIVATION, str(e))
    handles: list[int] = []
    try:
        watch: list[tuple[int, int]] = []
        if parent:
            hp = _k32.OpenProcess(SYNCHRONIZE, False, parent)
            if not hp:
                return fail(EXIT_OK, f"parent process {parent} is not running")
            handles.append(int(hp))
            watch.append((int(hp), EXIT_OK))
        if target.kind == "pid":
            assert target.pid is not None
            ht = _k32.OpenProcess(SYNCHRONIZE, False, target.pid)
            if ht:
                handles.append(int(ht))
                watch.append((int(ht), EXIT_TARGET_GONE))
            elif ctypes.get_last_error() == ERROR_ACCESS_DENIED:
                # процесс есть, но к нему не пускают (запущен от другого пользователя):
                # захватываем, но за его выходом не следим
                log.warning("Захват звука: за процессом-целью не уследить (нет доступа)")
            else:
                return fail(EXIT_TARGET_GONE, f"target process {target.pid} is not running")
        if target.kind != "system" and not available():
            return fail(EXIT_ACTIVATION, "process loopback needs Windows build "
                                         f"{MIN_BUILD}+ (this is {_windows_build()})")
        stop_ev = _k32.CreateEventW(None, True, False, None)
        if not stop_ev:
            return fail(EXIT_FAILED, f"CreateEventW failed ({ctypes.get_last_error()})")
        handles.append(int(stop_ev))
        try:
            duck._com_init()
            cap = _Capture(target)
            cap.open()
        except CaptureError as e:
            return fail(EXIT_ACTIVATION, f"activation failed: {e}")
        except Exception as e:
            return fail(EXIT_ACTIVATION, f"activation failed: {hr_text(e)}")

        def stop() -> None:
            _k32.SetEvent(stop_ev)

        out = _Writer(_std_handle(STD_OUTPUT_HANDLE), stop)
        out.start()
        _watch_stdin(_std_handle(STD_INPUT_HANDLE), stop)
        helper = _Helper(target, int(stop_ev), watch, out)
        try:
            code = helper.run(cap)
        except Exception as e:
            log.exception("Захват звука: ошибка")
            helper.reason = f"capture failed: {hr_text(e)}"
            code = EXIT_FAILED
        finally:
            if helper.cap is not None:
                helper.cap.close()
            out.close(0.5)
        log.info("Захват звука закончен (код %d, пакетов %d%s)", code,
                 helper.cap.packets if helper.cap is not None else 0,
                 f", выброшено {out.dropped // BLOCK_BYTES} блоков" if out.dropped else "")
        if code == EXIT_TARGET_GONE and not helper.reason:
            helper.reason = f"target process {target.pid} exited"
        return fail(code, helper.reason) if helper.reason else code
    finally:
        for h in handles:
            _k32.CloseHandle(h)


def helper_main(spec: str, parent: int | None) -> NoReturn:
    """Точка входа ``--loopback`` (из ``rupor.__main__``): без финализации интерпретатора —
    потоки чтения/записи каналов могут быть заблокированы в вызовах ядра Windows."""
    try:
        code = run_helper(spec, parent)
    except BaseException:
        code = EXIT_FAILED
        try:
            log.exception("Захват звука: необработанная ошибка")
        except Exception:
            pass
    os._exit(code)


# ---------- ядро: запуск помощника и чтение звука ----------

def helper_command(target: str, parent: int | None = None) -> list[str]:
    """Командная строка помощника: собранное ядро — сам rupor-core.exe, в разработке —
    ``python -m rupor``."""
    if getattr(sys, "frozen", False):
        cmd = [sys.executable]
    else:
        cmd = [sys.executable, "-m", "rupor"]
    cmd += ["--loopback", target]
    if parent:
        cmd += ["--parent", str(parent)]
    return cmd


def helper_env() -> dict[str, str] | None:
    """Окружение помощника: в разработке — этот же код rupor (PYTHONPATH на core/), а не
    установленная копия; собранному ядру ничего не нужно (None — окружение ядра)."""
    if getattr(sys, "frozen", False):
        return None
    core = str(Path(__file__).resolve().parent.parent)
    env = dict(os.environ)
    old = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = core + (os.pathsep + old if old else "")
    return env


def describe_exit(code: int, reason: str = "") -> str:
    """Текст для пользователя: почему захват звука закончился сам."""
    if code == EXIT_TARGET_GONE:
        return tr("loopback.target_closed")
    if code == EXIT_ACTIVATION:
        return tr("loopback.start_failed", reason=reason or f"code {code}")
    return tr("loopback.stopped", reason=reason or f"code {code}")


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    for f in (proc.stdin, proc.stdout, proc.stderr):
        try:
            if f is not None:
                f.close()
        except (OSError, ValueError):
            pass


class _Run:
    """Один запущенный помощник."""

    def __init__(self, proc: subprocess.Popen[bytes], target: str) -> None:
        self.proc = proc
        self.target = target
        self.stopping = threading.Event()
        self.reason = ""
        self.samples = 0
        self.reader: threading.Thread | None = None
        self.errs: threading.Thread | None = None


class LoopbackStream:
    """Захват звука встречи в процессе-помощнике (ядро в режиме ``--loopback``).

    ``start(target)`` — запустить (уже идёт — перезапустить на новой цели);
    ``on_audio(np.ndarray float32)`` зовётся в потоке ``rupor-loopback`` кусками от
    ``chunk_bytes`` (по умолчанию 50 мс); ``stop()`` — закрыть stdin помощника, подождать
    до 1 с, иначе снять; ``on_exit(code, reason)`` — помощник завершился сам (не по stop):
    3 — программа-источник закрылась, 2 — захват не начался, 1 — сломался. ``command`` —
    командная строка вместо настоящего помощника (тесты)."""

    def __init__(self, on_audio: Callable[[np.ndarray], None],
                 on_exit: Callable[[int, str], None] | None = None, *,
                 command: Callable[[str], Sequence[str]] | None = None,
                 chunk_bytes: int = CHUNK_BYTES) -> None:
        self._on_audio = on_audio
        self._on_exit = on_exit
        self._command = command
        self._chunk = max(SAMPLE_BYTES, chunk_bytes - chunk_bytes % SAMPLE_BYTES)
        self._lock = threading.Lock()
        self._run: _Run | None = None

    @property
    def alive(self) -> bool:
        run = self._run
        return run is not None and run.proc.poll() is None

    @property
    def target(self) -> str | None:
        run = self._run
        return run.target if run is not None else None

    @property
    def pid(self) -> int | None:
        """pid процесса-помощника (в разработке — запускателя .venv)."""
        run = self._run
        return run.proc.pid if run is not None else None

    def start(self, target: str) -> None:
        """Запустить помощника на цели (``pid:<pid>`` | ``system`` | ``exclude:<pid>``);
        неверная цель — ValueError, не запустился — OSError."""
        spec = str(parse_target(target))
        self.stop()
        if self._command is not None:
            cmd, env = list(self._command(spec)), None
        else:
            cmd, env = helper_command(spec, os.getpid()), helper_env()
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, env=env,
                                creationflags=CREATE_NO_WINDOW)
        run = _Run(proc, spec)
        run.errs = threading.Thread(target=self._read_errs, args=(run,),
                                    name="rupor-loopback-log", daemon=True)
        run.reader = threading.Thread(target=self._read, args=(run,), name="rupor-loopback",
                                      daemon=True)
        with self._lock:
            self._run = run
        run.errs.start()
        run.reader.start()
        log.info("Захват звука: помощник запущен (%s, pid %d)", spec, proc.pid)

    def stop(self, timeout: float = STOP_TIMEOUT_S) -> int | None:
        """Остановить помощника: закрыть stdin (он выходит сам), не вышел за timeout —
        снять. Возвращает код выхода (None — не был запущен)."""
        with self._lock:
            run, self._run = self._run, None
        if run is None:
            return None
        run.stopping.set()
        proc = run.proc
        try:
            if proc.stdin is not None:
                proc.stdin.close()
        except OSError:
            pass
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            log.warning("Захват звука: помощник не вышел за %.1f с — снимаю", timeout)
            proc.kill()
            try:
                code = proc.wait(2)
            except subprocess.TimeoutExpired:
                code = -1
        me = threading.current_thread()
        for t in (run.reader, run.errs):
            if t is not None and t is not me:
                t.join(1.0)
        if me is not run.reader:
            _close_pipes(proc)
        log.info("Захват звука: помощник остановлен (код %s, звука %.1f с)", code,
                 run.samples / SAMPLE_RATE)
        return code

    def _read(self, run: _Run) -> None:
        import numpy as np
        out = run.proc.stdout
        assert out is not None
        rem = b""
        warned = False
        try:
            while True:
                data = out.read(self._chunk)
                if not data:
                    break
                if rem:
                    data = rem + data
                n = len(data) - len(data) % SAMPLE_BYTES
                rem = data[n:]
                if not n or run.stopping.is_set():
                    continue
                arr = np.frombuffer(data, dtype="<f4", count=n // SAMPLE_BYTES).astype(
                    np.float32)
                run.samples += len(arr)
                try:
                    self._on_audio(arr)
                except Exception:
                    if not warned:
                        warned = True
                        log.exception("Захват звука: ошибка обработки звука")
        except (OSError, ValueError):
            pass                          # канал закрыт (stop)
        try:
            code = run.proc.wait(3)
        except subprocess.TimeoutExpired:
            run.proc.kill()
            code = run.proc.wait()
        if run.errs is not None:
            run.errs.join(1.0)
        _close_pipes(run.proc)
        if run.stopping.is_set():
            return
        with self._lock:
            if self._run is run:
                self._run = None
        log.warning("Захват звука: помощник завершился сам (код %d%s)", code,
                    f": {run.reason}" if run.reason else "")
        if self._on_exit is not None:
            try:
                self._on_exit(code, run.reason)
            except Exception:
                log.exception("Захват звука: ошибка обработчика выхода")

    @staticmethod
    def _read_errs(run: _Run) -> None:
        err = run.proc.stderr
        assert err is not None
        lines = 0
        try:
            for raw in iter(err.readline, b""):
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                prefix, sep, _rest = line.partition(": ")
                # Причина — только строка не из журнала помощника: её пишет fail() последней
                # (или хвост трассировки). Запись журнала («loopback INFO: …» — по-русски, для
                # лога) причиной не бывает и сбрасывает прежнюю: помощника сняли (диспетчер
                # задач, антивирус, падение в Core Audio) — пусто, ядро скажет «код N»
                record = bool(sep) and prefix.startswith("loopback ") and prefix[9:].isupper()
                run.reason = "" if record else line[:300]
                lines += 1
                if lines <= STDERR_LINES_MAX:
                    log.info("loopback: %s", line)
        except (OSError, ValueError):
            pass


# ---------- самопроверка сборки ----------

def selftest(timeout: float = 30.0) -> tuple[bool, str]:
    """Для ``--uia-selftest``: захват звука работает в этой сборке — comtypes.COMObject
    (обработчик активации), process loopback своего процесса (пакеты идут), loopback
    устройства вывода и помощник ``--loopback`` (звук приходит по каналу, на закрытый stdin
    выходит с кодом 0). Ничего не играет и громкость не трогает. «захват звука: ok (…)»;
    Windows старее сборки 20348 — «захват звука: недоступен (…)» (это не ошибка сборки)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["r"] = _selftest_now()
        except CaptureError as e:
            box["e"] = str(e)
        except Exception as e:
            box["e"] = hr_text(e)

    t = threading.Thread(target=run, name="rupor-loopback-selftest", daemon=True)
    t.start()
    t.join(timeout)
    if "r" in box:
        result: tuple[bool, str] = box["r"]
        return result
    if "e" in box:
        return False, f"захват звука: ошибка ({box['e']})"
    return False, f"захват звука: нет ответа за {timeout:g} с"


def _selftest_now() -> tuple[bool, str]:
    duck._com_init()
    parts: list[str] = []
    process_ok = available()
    if process_ok:
        cap = _Capture(Target("pid", os.getpid()))
        try:
            cap.open()
            cap.start()
            deadline = time.monotonic() + 2.0
            while not cap.packets and time.monotonic() < deadline:
                if cap.event:
                    _k32.WaitForSingleObject(cap.event, 100)
                else:
                    time.sleep(POLL_MS / 1000)
                cap.read()
        finally:
            cap.close()
        if not cap.packets:
            return False, "захват звука: ошибка (программа: нет пакетов за 2 с)"
        parts.append("программы: да")
    else:
        parts.append(f"программы: нет (сборка Windows {_windows_build()}, нужна {MIN_BUILD})")
    device_ok = True
    try:
        cap = _Capture(Target("system"))
        try:
            cap.open()
            cap.start()
        finally:
            cap.close()
        parts.append("весь звук: да")
    except NoOutputDevice:
        device_ok = False
        parts.append("весь звук: нет устройства вывода")
    if not process_ok and not device_ok:
        return True, f"захват звука: недоступен ({', '.join(parts)})"
    got: list[int] = []
    ev = threading.Event()

    def on_audio(a: np.ndarray) -> None:
        got.append(len(a))
        if sum(got) >= BLOCK_SAMPLES:
            ev.set()

    stream = LoopbackStream(on_audio, lambda _c, _r: ev.set())
    stream.start(f"pid:{os.getpid()}" if process_ok else SYSTEM_ID)
    ev.wait(15)
    alive = stream.alive
    code = stream.stop(3.0)
    if not got:
        return False, f"захват звука: ошибка (помощник: нет звука, код {code})"
    if not alive or code != 0:
        return False, f"захват звука: ошибка (помощник: код выхода {code})"
    parts.append("помощник: да")
    head = "ok" if process_ok else "недоступен"
    return True, f"захват звука: {head} ({', '.join(parts)})"
