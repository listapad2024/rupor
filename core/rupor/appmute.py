"""Голосовые чаты не слышат диктовку (live chat Codex, ChatGPT и т. п.).

Пока зажата клавиша диктовки, голосовой чат приложения из списка (``Settings.app_mute_apps``,
имена exe: ``chatgpt.exe`` — это и десктопный Codex, ``codex.exe``) не должен слышать голос:
он уходит в диктовку Рупора.

Как: Рупор сам нажимает в окне чата его кнопку **«Отключить микрофон»** (UI Automation,
rupor.uia), а после записи — «Включить микрофон». Чат при этом знает, что микрофон выключен,
и спокойно ждёт; разговор не рвётся.

Почему не средствами Windows (проверено на ПК автора):
- беззвук сессии захвата одного приложения (ISimpleAudioVolume на устройстве записи) на
  многих микрофонах глушит запись всем — Рупор получал тишину (0.8.0);
- приостановка звукового процесса чата (NtSuspendProcess, 0.8.1) ломает чат: Chromium через
  несколько секунд без данных микрофона считает поток сломанным и закрывает его.

Нажимается по одной кнопке на процесс приложения (окна Electron — один процесс; кнопка есть
и в главном окне, и в плавающем окне разговора, и это один и тот же микрофон). Если
микрофон уже выключен (видна только «Включить микрофон») — это сделал пользователь: не
трогаем и потом не включаем. Включил сам во время записи — тоже его, не выключаем снова.
Чат, начатый во время записи, — переопросом раз в ``POLL_S``.

Защита от «забытого» выключенного микрофона: процессы, где мы выключили микрофон (pid и
время создания), пишутся в ``%APPDATA%\\Rupor\\appmute.json`` **до** нажатия; при старте ядра
по свежим (``RESTORE_TTL_S``) записям микрофон включается обратно.

Все вызовы — в фоновом потоке ``rupor-appmute``; движок только ставит команды в очередь.
Приватность: в лог — только числа, имён процессов, окон и кнопок нет.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import re
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

log = logging.getLogger(__name__)

POLL_S = 0.7                 # во время записи: чат, начатый позже, и включённый вручную
SETTLE_S = 1.5               # после нажатия интерфейс чата успевает перерисоваться
RESTORE_TTL_S = 30 * 60.0    # при старте ядра включаем микрофон по записям не старше
CLOSE_TIMEOUT_S = 3.0
STATE_FILE = "appmute.json"
FILE_VERSION = 3
MAX_ENTRIES = 32

DEFAULT_APPS = ["codex.exe", "chatgpt.exe"]

# Кнопка микрофона голосового чата (Codex / ChatGPT, русский и английский интерфейс)
_MUTE = re.compile(r"^\s*(отключить|выключить)\s+микрофон|^\s*mute(\s+the)?\s+(mic|microphone)\b",
                   re.I)
_UNMUTE = re.compile(r"^\s*включить\s+микрофон|^\s*unmute(\s+the)?\s+(mic|microphone)\b", re.I)


def is_mute(name: str) -> bool:
    return bool(_MUTE.search(name))


def is_unmute(name: str) -> bool:
    return bool(_UNMUTE.search(name))


def norm_app(name: str) -> str:
    """Имя exe так, как его сравниваем: без пути, нижний регистр, с «.exe»."""
    n = str(name).strip().strip('"').replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()
    if n and not n.endswith(".exe"):
        n += ".exe"
    return n


# ---------- окна и кнопки (Windows + UIA или фейк в тестах) ----------

class Backend(Protocol):
    def windows(self, apps: frozenset[str]) -> dict[int, list[int]]: ...   # pid → окна
    def created(self, pid: int) -> int | None: ...
    def buttons(self, hwnd: int) -> list[str] | None: ...                 # None — не прочитано
    def press(self, hwnd: int, match: Callable[[str], bool]) -> bool: ...


class NullBackend:
    """Нет Windows (тесты): окон нет."""

    def windows(self, apps: frozenset[str]) -> dict[int, list[int]]:
        return {}

    def created(self, pid: int) -> int | None:
        return None

    def buttons(self, hwnd: int) -> list[str] | None:
        return None

    def press(self, hwnd: int, match: Callable[[str], bool]) -> bool:
        return False


@dataclass(eq=False)
class Entry:
    pid: int
    created: int
    ts: float          # когда выключили (unix, с)


def _state(b: Backend, hwnds: list[int]) -> tuple[int | None, int | None]:
    """(окно с «Отключить микрофон», окно с «Включить микрофон») среди окон процесса."""
    mute = unmute = None
    for h in hwnds:
        names = b.buttons(h) or []
        if mute is None and any(is_mute(n) for n in names):
            mute = h
        if unmute is None and any(is_unmute(n) for n in names):
            unmute = h
    return mute, unmute


class AppMuter:
    """``mute(apps)`` и ``restore()`` не блокируют (поток ``rupor-appmute``)."""

    def __init__(self, own_pids: Iterable[int] = (), path: Path | None = None,
                 backend_factory: Callable[[], Backend] | None = None) -> None:
        self._own = frozenset(int(p) for p in own_pids if p) | {os.getpid()}
        self._path = path
        self._factory = backend_factory
        self._q: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        # дальше — только поток rupor-appmute
        self._backend: Backend | None = None
        self._entries: list[Entry] = []
        self._pressed: dict[int, float] = {}    # pid → когда нажали (monotonic), эта запись
        self._skip: set[int] = set()            # в этой записи не трогаем (пользователь)
        self._active = False
        self._apps: frozenset[str] = frozenset()
        self._saved: str | None = None
        self._warned = False
        self.changed = 0                        # у скольких процессов микрофон выключен нами

    # ---------- API ----------

    def start(self) -> None:
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rupor-appmute",
                                                daemon=True)
                self._thread.start()

    def mute(self, apps: Iterable[str], output: bool = True) -> None:
        """Выключить микрофон голосовым чатам приложений apps до restore()."""
        self._q.put(("mute", frozenset(a for a in (norm_app(x) for x in apps) if a)))

    def restore(self) -> None:
        self._q.put(("restore",))

    def flush(self, timeout: float = 5.0) -> bool:
        if self._thread is None:
            return True
        ev = threading.Event()
        self._q.put(("flush", ev))
        return ev.wait(timeout)

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> bool:
        if self._thread is None:
            return True
        self._q.put(("restore",))
        ok = self.flush(timeout)
        self._q.put(("stop",))
        return ok

    # ---------- поток ----------

    def _ensure_backend(self) -> Backend | None:
        if self._backend is None:
            try:
                self._backend = (self._factory or default_backend)()
            except Exception as e:
                if not self._warned:
                    self._warned = True
                    log.warning("Голосовые чаты: недоступно (%s)", type(e).__name__)
                return None
        return self._backend

    def _run(self) -> None:
        try:
            left = [e for e in self._load() if time.time() - e.ts < RESTORE_TTL_S]
            if left:
                self._entries = left
                self._unmute_all()
            elif self._saved is None:
                self._save()             # старый или устаревший файл — убрать
        except Exception:
            log.exception("Голосовые чаты: не удалось включить микрофон после прошлого запуска")
        while True:
            try:
                cmd = self._q.get(timeout=POLL_S if self._active else None)
            except queue.Empty:
                try:
                    if self._active:
                        self._poll()
                except Exception:
                    log.debug("Голосовые чаты: переопрос не удался", exc_info=True)
                continue
            try:
                if cmd[0] == "mute":
                    self._apps = cmd[1]
                    if not self._active:
                        self._skip.clear()
                        self._pressed.clear()
                    self._active = bool(self._apps)
                    if self._active:
                        self._poll()
                    else:
                        self._unmute_all()
                elif cmd[0] == "restore":
                    self._active = False
                    self._unmute_all()
                elif cmd[0] == "flush":
                    cmd[1].set()
                elif cmd[0] == "stop":
                    return
            except Exception:
                log.exception("Голосовые чаты: команда %s не выполнена", cmd[0])
                if cmd[0] == "flush":
                    cmd[1].set()

    def _poll(self) -> None:
        b = self._ensure_backend()
        if b is None:
            return
        wins = {pid: hw for pid, hw in b.windows(self._apps).items()
                if pid not in self._own and pid not in (0, 4)}
        ours = {e.pid: e for e in self._entries}
        now = time.monotonic()
        for pid, hwnds in wins.items():
            if pid in self._skip:
                continue
            if pid in ours:
                # уже выключили: снова видна «Отключить» и нет «Включить» — пользователь
                # включил сам; это его, больше не трогаем
                if now - self._pressed.get(pid, 0.0) < SETTLE_S:
                    continue
                mute, unmute = _state(b, hwnds)
                if mute is not None and unmute is None:
                    self._entries.remove(ours[pid])
                    self._skip.add(pid)
                    self._save()
                continue
            mute, unmute = _state(b, hwnds)
            if mute is None:
                if unmute is not None:
                    self._skip.add(pid)  # уже выключен пользователем — не наше
                continue                 # голосового чата нет — переопросим
            c = b.created(pid)
            if c is None:
                continue
            e = Entry(pid, c, time.time())
            self._entries.append(e)
            self._save()                 # до нажатия: падение не оставит микрофон выключенным
            ok = False
            try:
                ok = b.press(mute, is_mute)
            except Exception:
                log.debug("Голосовые чаты: кнопка не нажата", exc_info=True)
            if ok:
                self._pressed[pid] = time.monotonic()
                log.info("Голосовые чаты: микрофон чата выключен")
            else:
                self._entries.remove(e)
                self._skip.add(pid)
            self._save()
        self.changed = len(self._entries)

    def _unmute_all(self) -> None:
        if not self._entries:
            return
        b = self._ensure_backend()
        if b is None:
            return                       # остаются в файле — включит следующий запуск
        wins = b.windows(self._apps) if self._apps else {}
        n = 0
        for e in list(self._entries):
            if b.created(e.pid) == e.created:
                hwnds = wins.get(e.pid)
                if hwnds is None:        # список приложений сменился — окна процесса заново
                    hwnds = b.windows(frozenset({"*"})).get(e.pid, [])
                wait = SETTLE_S - (time.monotonic() - self._pressed.get(e.pid, -1e9))
                if wait > 0:
                    time.sleep(min(wait, SETTLE_S))   # нажатие только что — дать перерисоваться
                _, unmute = _state(b, hwnds)
                if unmute is not None:
                    try:
                        if b.press(unmute, is_unmute):
                            n += 1
                    except Exception:
                        log.debug("Голосовые чаты: кнопка не нажата", exc_info=True)
            self._entries.remove(e)
        self._pressed.clear()
        self.changed = 0
        self._save()
        if n:
            log.info("Голосовые чаты: микрофон чата включён (%d)", n)

    # ---------- файл ----------

    def _file(self) -> Path:
        if self._path is not None:
            return self._path
        from . import config
        return config.app_dir() / STATE_FILE

    def _load(self) -> list[Entry]:
        try:
            raw = json.loads(self._file().read_text(encoding="utf-8"))
        except FileNotFoundError:
            self._saved = ""
            return []
        except Exception:
            log.warning("Голосовые чаты: %s не прочитан", STATE_FILE)
            return []
        if not isinstance(raw, dict) or raw.get("version") != FILE_VERSION:
            return []                    # 0.8.0/0.8.1 — другой способ, возвращать нечего
        out: list[Entry] = []
        for it in raw.get("entries", []):
            try:
                out.append(Entry(int(it["pid"]), int(it["created"]), float(it["ts"])))
            except Exception:
                continue
        return out[-MAX_ENTRIES:]

    def _save(self) -> None:
        all_ = self._entries[-MAX_ENTRIES:]
        text = "" if not all_ else json.dumps(
            {"version": FILE_VERSION, "entries": [asdict(e) for e in all_]})
        if text == self._saved:
            return
        p = self._file()
        try:
            if not text:
                p.unlink(missing_ok=True)
            else:
                p.parent.mkdir(parents=True, exist_ok=True)
                tmp = p.with_suffix(".tmp")
                tmp.write_text(text, encoding="utf-8")
                os.replace(tmp, p)
            self._saved = text
        except Exception:
            log.debug("Голосовые чаты: %s не записан", STATE_FILE, exc_info=True)


# ---------- Windows ----------

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class WinBackend:
    """Окна приложений (EnumWindows) и их кнопки (rupor.uia)."""

    def __init__(self) -> None:
        from ctypes import wintypes as wt
        self._wt = wt
        self._u32 = ctypes.WinDLL("user32", use_last_error=True)
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.OpenProcess.restype = ctypes.c_void_p
        self._k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self._k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        self._k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
        self._u32.GetWindowThreadProcessId.argtypes = [wt.HWND, ctypes.POINTER(wt.DWORD)]
        self._u32.IsWindowVisible.argtypes = [wt.HWND]
        self._names: dict[int, str] = {}

    def process_name(self, pid: int) -> str:
        h = self._k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.c_uint32(1024)
            if not self._k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return ""
            return buf.value
        finally:
            self._k32.CloseHandle(h)

    def created(self, pid: int) -> int | None:
        h = self._k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, 0, int(pid))
        if not h:
            return None
        try:
            t = [ctypes.c_uint64() for _ in range(4)]
            if not self._k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
                return None
            return int(t[0].value)
        finally:
            self._k32.CloseHandle(h)

    def windows(self, apps: frozenset[str]) -> dict[int, list[int]]:
        wt = self._wt
        out: dict[int, list[int]] = {}
        names: dict[int, str] = {}
        cb_t = ctypes.WINFUNCTYPE(wt.BOOL, wt.HWND, wt.LPARAM)

        def cb(h: Any, _l: Any) -> bool:
            if not self._u32.IsWindowVisible(h):
                return True
            pid = wt.DWORD()
            self._u32.GetWindowThreadProcessId(h, ctypes.byref(pid))
            p = int(pid.value)
            if p not in names:
                names[p] = norm_app(self.process_name(p))
            if "*" in apps or names[p] in apps:
                out.setdefault(p, []).append(int(h))
            return True

        self._u32.EnumWindows(cb_t(cb), 0)
        return out

    def buttons(self, hwnd: int) -> list[str] | None:
        from . import uia
        return uia.button_names(hwnd)

    def press(self, hwnd: int, match: Callable[[str], bool]) -> bool:
        from . import uia
        return uia.press_button(hwnd, match) is not None


def default_backend() -> Backend:
    return WinBackend()
