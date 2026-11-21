"""Выключение голосовых чатов на время диктовки (live chat Codex, ChatGPT и т. п.).

Пока зажата клавиша диктовки, приложения из списка (``Settings.app_mute_apps``, имена
exe: ``codex.exe``, ``chatgpt.exe``) не должны слышать голос: он уходит в диктовку Рупора.
Для этого выключается (беззвук) их **сессия захвата** микрофона — та же аудиосессия Core
Audio, что видна в микшере громкости, только на устройстве записи: Windows отдаёт такому
приложению тишину, а Рупор и все прочие слышат микрофон как обычно (у каждого процесса своя
сессия). С ``app_mute_output`` выключается и их звук (ответ голосового ассистента не
перебивает диктовку). После записи всё возвращается как было.

Core Audio: IMMDeviceEnumerator → все активные устройства записи и вывода →
IAudioSessionManager2 → IAudioSessionControl2 + ISimpleAudioVolume (описания интерфейсов —
из rupor.duck). Процесс сессии узнаётся по pid (QueryFullProcessImageNameW, имя exe без
пути, без учёта регистра); у приложений на Electron/Chromium звук пишет дочерний процесс
с тем же именем exe — он тоже подходит.

Приглушается всё, что подходит, сразу при нажатии клавиши (не дожидаясь подтверждения
записи: первые слова не должны уйти в чат), и то, что появилось во время записи, —
переопросом раз в ``POLL_S``. Возвращается только то, что выключили мы. Если пользователь во
время записи сам включил звук выключенной сессии — это его: не трогаем до конца записи.

Защита от «залипшего» беззвука — как в rupor.duck: выключенные сессии пишутся в
``%APPDATA%\\Rupor\\appmute.json`` **до** изменения; при старте ядра всё, что там осталось,
включается обратно. Windows помнит беззвук приложения между запусками (по идентификатору
сессии без pid), поэтому запись о закрывшемся приложении ждёт его нового экземпляра
не дольше ``PENDING_TTL_S``.

Все вызовы Core Audio — в фоновом потоке ``rupor-appmute`` (COM MTA); движок только ставит
команды в очередь. Приватность: в лог — только числа, имён процессов нет.
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

POLL_S = 0.5                 # во время записи: новые сессии, включённые пользователем
PENDING_POLL_S = 3.0         # вне записи, пока есть невозвращённые записи
PENDING_TTL_S = 7 * 86400.0
MAX_ENTRIES = 256
CLOSE_TIMEOUT_S = 2.0
STATE_FILE = "appmute.json"
FILE_VERSION = 1

DEFAULT_APPS = ["codex.exe", "chatgpt.exe"]


def norm_app(name: str) -> str:
    """Имя exe так, как его сравниваем: без пути, нижний регистр, с «.exe»."""
    n = str(name).strip().strip('"').replace("/", "\\").rsplit("\\", 1)[-1].strip().lower()
    if n and not n.endswith(".exe"):
        n += ".exe"
    return n


# ---------- сессии ----------

class AppSession(Protocol):
    @property
    def instance(self) -> str: ...      # идентификатор экземпляра (с pid)
    @property
    def session(self) -> str: ...       # идентификатор сессии (приложение + устройство)
    @property
    def pid(self) -> int: ...
    @property
    def capture(self) -> bool: ...      # сессия на устройстве записи
    @property
    def system(self) -> bool: ...

    def muted(self) -> bool: ...
    def set_muted(self, m: bool) -> None: ...


class Backend(Protocol):
    def sessions(self) -> list[AppSession]: ...
    def process_name(self, pid: int) -> str: ...


class NullBackend:
    """Нет Core Audio (тесты): сессий нет."""

    def sessions(self) -> list[AppSession]:
        return []

    def process_name(self, pid: int) -> str:
        return ""


@dataclass(eq=False)
class Entry:
    instance: str
    session: str
    pid: int
    capture: bool
    ts: float


class AppMuter:
    """``mute(apps, output)`` и ``restore()`` не блокируют (поток ``rupor-appmute``)."""

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
        self._pending: list[Entry] = []     # приложение закрылось выключенным — ждём его
        self._active = False
        self._apps: frozenset[str] = frozenset()
        self._output = True
        self._user_off: set[str] = set()
        self._names: dict[int, str] = {}
        self._saved: str | None = None
        self._warned = False
        self.changed = 0                    # сколько сессий сейчас выключено нами

    # ---------- API ----------

    def start(self) -> None:
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rupor-appmute",
                                                daemon=True)
                self._thread.start()

    def mute(self, apps: Iterable[str], output: bool = True) -> None:
        self._q.put(("mute", frozenset(a for a in (norm_app(x) for x in apps) if a),
                     bool(output)))

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
                    log.warning("Голосовые чаты: Core Audio недоступен (%s)", type(e).__name__)
                return None
        return self._backend

    def _run(self) -> None:
        try:
            self._pending = self._load()
            if self._pending:
                self._settle_pending()
        except Exception:
            log.exception("Голосовые чаты: не удалось вернуть звук после прошлого запуска")
        while True:
            wait = POLL_S if self._active else (PENDING_POLL_S if self._pending else None)
            try:
                cmd = self._q.get(timeout=wait)
            except queue.Empty:
                try:
                    if self._active:
                        self._poll()
                    elif self._pending:
                        self._settle_pending()
                except Exception:
                    log.debug("Голосовые чаты: переопрос не удался", exc_info=True)
                    self._backend = None
                continue
            try:
                if cmd[0] == "mute":
                    self._apps, self._output = cmd[1], cmd[2]
                    if not self._active:
                        self._user_off.clear()
                    self._active = bool(self._apps)
                    # список или «и звук» поменялись — лишнее включаем обратно
                    self._release(lambda e: not self._wanted_entry(e))
                    if self._active:
                        self._poll()
                elif cmd[0] == "restore":
                    self._active = False
                    self._user_off.clear()
                    self._release(lambda e: True)
                elif cmd[0] == "flush":
                    cmd[1].set()
                elif cmd[0] == "stop":
                    return
            except Exception:
                log.exception("Голосовые чаты: команда %s не выполнена", cmd[0])
                self._backend = None
                if cmd[0] == "flush":
                    cmd[1].set()

    def _name(self, b: Backend, pid: int) -> str:
        n = self._names.get(pid)
        if n is None:
            try:
                n = norm_app(b.process_name(pid))
            except Exception:
                n = ""
            if len(self._names) > 512:
                self._names.clear()
            self._names[pid] = n
        return n

    def _wanted_entry(self, e: Entry) -> bool:
        if not self._active:
            return False
        if not e.capture and not self._output:
            return False
        b = self._backend
        return b is not None and self._name(b, e.pid) in self._apps

    def _poll(self) -> None:
        b = self._ensure_backend()
        if b is None:
            return
        sessions = b.sessions()
        live = {s.instance: s for s in sessions}
        ours = {e.instance for e in self._entries}
        # включил сам пользователь — его, отпускаем
        for e in list(self._entries):
            s = live.get(e.instance)
            if s is not None:
                try:
                    if not s.muted():
                        self._entries.remove(e)
                        self._user_off.add(e.instance)
                except Exception:
                    pass
        new: list[tuple[AppSession, Entry]] = []
        for s in sessions:
            if (s.instance in ours or s.instance in self._user_off or s.system
                    or s.pid in self._own or not s.pid):
                continue
            if not s.capture and not self._output:
                continue
            if self._name(b, s.pid) not in self._apps:
                continue
            try:
                if s.muted():
                    self._user_off.add(s.instance)   # уже без звука — не наше
                    continue
            except Exception:
                continue
            new.append((s, Entry(s.instance, s.session, s.pid, s.capture, time.time())))
        if not new:
            self._save()
            return
        self._entries += [e for _, e in new]
        self._save()                     # до изменения: падение не оставит беззвук
        n = 0
        for s, e in new:
            try:
                s.set_muted(True)
                n += 1
            except Exception:
                self._entries.remove(e)
        self._save()
        self.changed = len(self._entries)
        if n:
            log.info("Голосовые чаты: выключено сессий %d (захват %d)", n,
                     sum(1 for _, e in new if e.capture))

    def _release(self, which: Callable[[Entry], bool]) -> None:
        sel = [e for e in self._entries if which(e)]
        if not sel:
            return
        b = self._ensure_backend()
        live: dict[str, AppSession] = {}
        if b is not None:
            try:
                live = {s.instance: s for s in b.sessions()}
            except Exception:
                self._backend = None
                log.debug("Голосовые чаты: сессии не перечислены", exc_info=True)
                return                   # остаются в файле — вернём при следующем случае
        back = 0
        for e in sel:
            s = live.get(e.instance)
            self._entries.remove(e)
            if s is None:
                self._pending.append(e)  # приложение закрылось — ждём его
                continue
            try:
                if s.muted():
                    s.set_muted(False)
                    back += 1
            except Exception:
                self._pending.append(e)
        self.changed = len(self._entries)
        self._save()
        if back:
            log.info("Голосовые чаты: звук возвращён сессиям %d", back)

    def _settle_pending(self) -> None:
        b = self._ensure_backend()
        if b is None:
            return
        now = time.time()
        self._pending = [e for e in self._pending if now - e.ts < PENDING_TTL_S]
        sessions = b.sessions()
        by_inst = {s.instance: s for s in sessions}
        by_sess: dict[str, AppSession] = {}
        for x in sessions:
            by_sess.setdefault(x.session, x)
        left: list[Entry] = []
        for e in self._pending:
            s = by_inst.get(e.instance) or by_sess.get(e.session)
            if s is None:
                left.append(e)
                continue
            try:
                if s.muted():
                    s.set_muted(False)
            except Exception:
                left.append(e)
        self._pending = left
        self._save()

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
        out: list[Entry] = []
        for it in raw.get("entries", []) if isinstance(raw, dict) else []:
            try:
                out.append(Entry(str(it["instance"]), str(it["session"]), int(it["pid"]),
                                 bool(it["capture"]), float(it["ts"])))
            except Exception:
                continue
        return out[-MAX_ENTRIES:]

    def _save(self) -> None:
        all_ = (self._pending + self._entries)[-MAX_ENTRIES:]
        text = "" if not all_ else json.dumps(
            {"version": FILE_VERSION, "entries": [asdict(e) for e in all_]}, ensure_ascii=False)
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


# ---------- Core Audio ----------

_E_RENDER, _E_CAPTURE = 0, 1
_DEVICE_STATE_ACTIVE = 0x1
_STATE_EXPIRED = 2
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


class _CoreSession:
    def __init__(self, instance: str, session: str, pid: int, capture: bool, system: bool,
                 vol: Any) -> None:
        self.instance, self.session, self.pid = instance, session, pid
        self.capture, self.system = capture, system
        self._vol = vol

    def muted(self) -> bool:
        return bool(self._vol.GetMute())

    def set_muted(self, m: bool) -> None:
        self._vol.SetMute(1 if m else 0, None)


class CoreAudioBackend:
    """Сессии на всех активных устройствах записи и вывода (поток rupor-appmute)."""

    def __init__(self) -> None:
        from . import duck
        self._duck = duck
        comtypes = duck._com_init()
        self._i = duck._interfaces()
        self._enum = comtypes.CoCreateInstance(
            self._i.GUID(duck._CLSID_MMDEVICE_ENUMERATOR), interface=self._i.IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self._managers: dict[str, Any] = {}

    def process_name(self, pid: int) -> str:
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not h:
            return ""
        try:
            buf = ctypes.create_unicode_buffer(1024)
            n = ctypes.c_uint(1024)
            if not k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(n)):
                return ""
            return buf.value
        finally:
            k32.CloseHandle(h)

    def sessions(self) -> list[AppSession]:
        i, take = self._i, self._duck._take_str
        out: list[AppSession] = []
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
                        ctl = en.GetSession(j)
                        c2 = ctl.QueryInterface(i.IAudioSessionControl2)
                        if int(c2.GetState()) == _STATE_EXPIRED:
                            continue
                        out.append(_CoreSession(
                            take(c2.GetSessionInstanceIdentifier()),
                            take(c2.GetSessionIdentifier()),
                            int(c2.GetProcessId()), flow == _E_CAPTURE,
                            c2.IsSystemSoundsSession() == 0,
                            ctl.QueryInterface(i.ISimpleAudioVolume)))
                    except Exception:
                        continue
        return out


def default_backend() -> Backend:
    return CoreAudioBackend()
