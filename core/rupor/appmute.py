"""Голосовые чаты не слышат диктовку (live chat Codex, ChatGPT и т. п.).

Пока зажата клавиша диктовки, приложения из списка (``Settings.app_mute_apps``, имена
exe: ``chatgpt.exe`` — это и десктопный Codex, ``codex.exe``) не должны слышать голос: он
уходит в диктовку Рупора.

Почему не беззвук сессии захвата: на многих микрофонах (проверено на ПК автора) беззвук
одной сессии захвата в Windows глушит все сессии этого устройства — Рупор тоже получал бы
тишину. Поэтому на время записи **приостанавливаются процессы приложения, которые держат
аудиосессии** (NtSuspendProcess). У приложений на Electron/Chromium это отдельный звуковой
процесс (``ChatGPT.exe --utility-sub-type=audio.mojom…``): окно, сеть и сам разговор
продолжают работать, просто звук на эти секунды не пишется и не играет (ответ ассистента
доиграет после). Микрофон при этом свободно слышат Рупор и все остальные.

Процессы находятся через Core Audio: все сессии на активных устройствах записи и вывода
(IAudioSessionManager2, интерфейсы — из rupor.duck), pid сессии → имя exe
(QueryFullProcessImageNameW, без пути и регистра). Свои процессы, pid 0/4 и чужие имена не
трогаются никогда. Появившиеся во время записи — переопросом раз в ``POLL_S``.

Защита от «замёрзшего» звука чата: приостановленные процессы (pid + время создания,
чтобы не спутать с новым процессом на том же pid) пишутся в
``%APPDATA%\\Rupor\\appmute.json`` **до** приостановки; при старте ядра всё, что там
осталось, возобновляется. Дольше ``MAX_SUSPEND_S`` подряд процесс не держится (потерянное
отпускание клавиши) — возобновляется сам.

Все вызовы — в фоновом потоке ``rupor-appmute``; движок только ставит команды в очередь.
Приватность: в лог — только числа, имён процессов нет.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

log = logging.getLogger(__name__)

POLL_S = 0.5                 # во время записи: новые звуковые процессы приложений
MAX_SUSPEND_S = 660.0        # дольше записи (MAX_RECORD_S = 600) процесс не держим
CLOSE_TIMEOUT_S = 2.0
STATE_FILE = "appmute.json"
FILE_VERSION = 2
MAX_ENTRIES = 64

DEFAULT_APPS = ["codex.exe", "chatgpt.exe"]


def norm_app(name: str) -> str:
    """Имя exe так, как его сравниваем: без пути, нижний регистр, с «.exe»."""
    n = str(name).strip().strip('"').replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()
    if n and not n.endswith(".exe"):
        n += ".exe"
    return n


# ---------- процессы (Windows или фейк в тестах) ----------

class Backend(Protocol):
    def audio_pids(self) -> set[int]: ...          # pid всех процессов с аудиосессиями
    def process_name(self, pid: int) -> str: ...
    def created(self, pid: int) -> int | None: ...   # время создания (None — процесса нет)
    def suspend(self, pid: int) -> bool: ...
    def resume(self, pid: int) -> bool: ...


class NullBackend:
    """Нет Windows (тесты): процессов нет."""

    def audio_pids(self) -> set[int]:
        return set()

    def process_name(self, pid: int) -> str:
        return ""

    def created(self, pid: int) -> int | None:
        return None

    def suspend(self, pid: int) -> bool:
        return False

    def resume(self, pid: int) -> bool:
        return False


@dataclass(eq=False)
class Entry:
    pid: int
    created: int
    ts: float          # когда приостановили (unix, с)


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
        self._active = False
        self._since = 0.0
        self._apps: frozenset[str] = frozenset()
        self._saved: str | None = None
        self._warned = False
        self.changed = 0                    # сколько процессов сейчас приостановлено нами

    # ---------- API ----------

    def start(self) -> None:
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rupor-appmute",
                                                daemon=True)
                self._thread.start()

    def mute(self, apps: Iterable[str], output: bool = True) -> None:
        """Приостановить звуковые процессы приложений apps до restore(). output — прежний
        параметр (звук приостановленного процесса не играет в любом случае)."""
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
            left = self._load()
            if left:
                self._entries = left
                n = self._resume_all()
                log.info("Голосовые чаты: возобновлено после прошлого запуска %d", n)
        except Exception:
            log.exception("Голосовые чаты: не удалось возобновить после прошлого запуска")
        while True:
            try:
                cmd = self._q.get(timeout=POLL_S if self._active else None)
            except queue.Empty:
                try:
                    if self._active and time.monotonic() - self._since > MAX_SUSPEND_S:
                        log.warning("Голосовые чаты: запись слишком долгая — возобновлены")
                        self._active = False
                        self._resume_all()
                    elif self._active:
                        self._poll()
                except Exception:
                    log.debug("Голосовые чаты: переопрос не удался", exc_info=True)
                    self._backend = None
                continue
            try:
                if cmd[0] == "mute":
                    self._apps = cmd[1]
                    if not self._active:
                        self._since = time.monotonic()
                    self._active = bool(self._apps)
                    self._release_unwanted()
                    if self._active:
                        self._poll()
                elif cmd[0] == "restore":
                    self._active = False
                    self._resume_all()
                elif cmd[0] == "flush":
                    cmd[1].set()
                elif cmd[0] == "stop":
                    return
            except Exception:
                log.exception("Голосовые чаты: команда %s не выполнена", cmd[0])
                self._backend = None
                if cmd[0] == "flush":
                    cmd[1].set()

    def _wanted(self, b: Backend, pid: int) -> bool:
        if pid in self._own or pid in (0, 4):
            return False
        try:
            return norm_app(b.process_name(pid)) in self._apps
        except Exception:
            return False

    def _poll(self) -> None:
        b = self._ensure_backend()
        if b is None:
            return
        ours = {e.pid for e in self._entries}
        new: list[Entry] = []
        for pid in sorted(b.audio_pids()):
            if pid in ours or not self._wanted(b, pid):
                continue
            c = b.created(pid)
            if c is None:
                continue
            new.append(Entry(pid, c, time.time()))
        if not new:
            return
        self._entries += new
        self._save()                     # до приостановки: падение не оставит чат без звука
        n = 0
        for e in new:
            ok = False
            try:
                ok = b.suspend(e.pid)
            except Exception:
                log.debug("Голосовые чаты: не приостановлен", exc_info=True)
            if ok:
                n += 1
            else:
                self._entries.remove(e)
        self._save()
        self.changed = len(self._entries)
        if n:
            log.info("Голосовые чаты: приостановлено звуковых процессов %d", n)

    def _resume(self, b: Backend, e: Entry) -> bool:
        if b.created(e.pid) != e.created:
            return False                 # процесс закрылся (или pid уже чужой)
        return bool(b.resume(e.pid))

    def _release_unwanted(self) -> None:
        b = self._ensure_backend()
        if b is None:
            return
        drop = [e for e in self._entries if not self._active or not self._wanted(b, e.pid)]
        for e in drop:
            try:
                self._resume(b, e)
            except Exception:
                log.debug("Голосовые чаты: не возобновлён", exc_info=True)
            self._entries.remove(e)
        if drop:
            self.changed = len(self._entries)
            self._save()

    def _resume_all(self) -> int:
        if not self._entries:
            return 0
        b = self._ensure_backend()
        if b is None:
            return 0                     # остаются в файле — возобновит следующий запуск
        n = 0
        for e in list(self._entries):
            try:
                if self._resume(b, e):
                    n += 1
            except Exception:
                log.debug("Голосовые чаты: не возобновлён", exc_info=True)
            self._entries.remove(e)
        self.changed = 0
        self._save()
        if n:
            log.info("Голосовые чаты: возобновлено звуковых процессов %d", n)
        return n

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
            self._saved = None
            return []                    # 0.8.0 (беззвук сессий) — Windows не помнит его
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

_E_RENDER, _E_CAPTURE = 0, 1
_DEVICE_STATE_ACTIVE = 0x1
_STATE_EXPIRED = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_PROCESS_SUSPEND_RESUME = 0x0800


class WinBackend:
    """Процессы с аудиосессиями (Core Audio) и их приостановка (поток rupor-appmute)."""

    def __init__(self) -> None:
        from . import duck
        self._duck = duck
        comtypes = duck._com_init()
        self._i = duck._interfaces()
        self._enum = comtypes.CoCreateInstance(
            self._i.GUID(duck._CLSID_MMDEVICE_ENUMERATOR), interface=self._i.IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self._managers: dict[str, Any] = {}
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._nt = ctypes.WinDLL("ntdll")
        self._k32.OpenProcess.restype = ctypes.c_void_p
        self._k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        self._k32.CloseHandle.argtypes = [ctypes.c_void_p]
        self._k32.QueryFullProcessImageNameW.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
        self._k32.GetProcessTimes.argtypes = [ctypes.c_void_p] + [ctypes.c_void_p] * 4
        self._nt.NtSuspendProcess.argtypes = [ctypes.c_void_p]
        self._nt.NtResumeProcess.argtypes = [ctypes.c_void_p]

    def _open(self, pid: int, access: int) -> Any:
        return self._k32.OpenProcess(access, 0, int(pid))

    def process_name(self, pid: int) -> str:
        h = self._open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
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
        h = self._open(pid, _PROCESS_QUERY_LIMITED_INFORMATION)
        if not h:
            return None
        try:
            t = [ctypes.c_uint64() for _ in range(4)]
            if not self._k32.GetProcessTimes(h, *[ctypes.byref(x) for x in t]):
                return None
            return int(t[0].value)
        finally:
            self._k32.CloseHandle(h)

    def _call(self, pid: int, fn: Any) -> bool:
        h = self._open(pid, _PROCESS_SUSPEND_RESUME)
        if not h:
            return False
        try:
            return int(fn(h)) == 0       # NTSTATUS STATUS_SUCCESS
        finally:
            self._k32.CloseHandle(h)

    def suspend(self, pid: int) -> bool:
        return self._call(pid, self._nt.NtSuspendProcess)

    def resume(self, pid: int) -> bool:
        return self._call(pid, self._nt.NtResumeProcess)

    def audio_pids(self) -> set[int]:
        i, take = self._i, self._duck._take_str
        out: set[int] = set()
        for flow in (_E_CAPTURE, _E_RENDER):
            col = self._enum.EnumAudioEndpoints(flow, _DEVICE_STATE_ACTIVE)
            for k in range(int(col.GetCount())):
                try:
                    dev = col.Item(k)
                    dev_id = take(dev.GetId())
                    mgr = self._managers.get(dev_id)
                    if mgr is None:
                        if len(self._managers) > 32:
                            self._managers.clear()
                        unk = dev.Activate(i.IAudioSessionManager2._iid_, self._duck._CLSCTX_ALL,
                                           None)
                        mgr = unk.QueryInterface(i.IAudioSessionManager2)
                        self._managers[dev_id] = mgr
                    en = mgr.GetSessionEnumerator()
                    count = int(en.GetCount())
                except Exception:
                    continue
                for j in range(count):
                    try:
                        c2 = en.GetSession(j).QueryInterface(i.IAudioSessionControl2)
                        if int(c2.GetState()) == _STATE_EXPIRED:
                            continue
                        out.add(int(c2.GetProcessId()))
                    except Exception:
                        continue
        return out


def default_backend() -> Backend:
    return WinBackend()
