"""Приглушение звука компьютера во время диктовки (как «Mute music while dictating» в Wispr Flow).

Меняется уровень **аудиосессий** Windows (микшер громкости: у каждого приложения своя
сессия), а не общий уровень устройства. Core Audio: IMMDeviceEnumerator → IMMDevice →
IAudioSessionManager2 → IAudioSessionEnumerator → IAudioSessionControl2 + ISimpleAudioVolume.
Приглушаются сессии на устройствах вывода по умолчанию (роли eConsole, eMultimedia,
eCommunications — обычно это одно-два устройства), а возвращаются — на любом активном
устройстве вывода: если во время записи устройство по умолчанию сменилось (подключили
наушники), приглушённая сессия на прежнем устройстве всё равно вернётся. Приложения, которым
в Windows явно назначено другое устройство, не приглушаются.
Интерфейсы описаны здесь же на comtypes (без pycaw и psutil; IID и порядок методов — по
заголовкам Windows SDK audiopolicy.h / audioclient.h / mmdeviceapi.h): в сборке ничего не
генерируется, comtypes и так входит в ядро (UI Automation).

Режимы (``Settings.duck_mode``): ``"duck"`` — громкость сессии × ``duck_level``/100,
``"mute"`` — беззвук, ``"off"`` — не трогать.

Не трогаются: сессии процессов Рупора (звуки начала/конца записи играет само ядро —
``own_pids``), сессия системных звуков (IsSystemSoundsSession), сессии, которые уже без звука
или тише цели, и сессии исключённых деревьев процессов (``set_excluded``: пока идёт перевод
встречи, браузер со встречей не приглушается — захват звука идёт после громкости сессии, и
приглушённая встреча не расшифровалась бы; встреча слушает весь звук компьютера —
``everything``, не приглушается ничего). Приглушаются играющие сессии
(AudioSessionStateActive); те, что заиграли во время записи, — переопросом раз в ``POLL_S``.
RDP: звук удалённой машины играет mstsc — обычная сессия, приглушается.

Возвращается только то, что поменяли мы, и к исходному значению. Громкость и беззвук
ведутся порознь: если пользователь во время записи сам поменял то, что поставили мы
(громкость в режиме «приглушать», беззвук в режиме «выключать»), — это его, не трогаем и не
возвращаем, и сессию до конца записи больше не приглушаем (в том числе после смены
настроек). Поменял другое (выключил звук приглушённой программы, подвинул громкость
выключенной) — своё всё равно возвращаем: иначе программа осталась бы тихой или без звука.

Все вызовы Core Audio — в одном фоновом потоке ``rupor-duck`` (CoInitializeEx MTA). Движок
только ставит команды в очередь (``duck``/``restore``) — старт записи и хук клавиатуры их не
ждут, зависший вызов задерживает лишь этот поток. Ошибки COM запись не роняют.

Защита от «залипшего» беззвука: изменённые сессии (идентификаторы сессии и её экземпляра,
pid, исходные и поставленные громкость/беззвук) пишутся в ``%APPDATA%\\Rupor\\duck.json``
**до** изменения; файл переписывается, только когда записи изменились. При старте ядра всё,
что там осталось (падение, снятие процесса, выключение компьютера), возвращается,
возвращённое из файла убирается (пустой — файл удаляется).
Windows помнит громкость приложения между его запусками (по идентификатору сессии:
приложение + устройство, без pid), поэтому запись о закрывшемся приложении ждёт (не дольше
``PENDING_TTL_S``) его нового экземпляра: пришёл с той громкостью, что поставили мы, —
возвращаем исходную; с другой — запись забывается.

Приватность: в лог — только числа (сессий, изменено, возвращено), имён процессов и окон нет.
"""
from __future__ import annotations

import ctypes
import json
import logging
import os
import queue
import sys
import threading
import time
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any, Callable, Iterable, Protocol

log = logging.getLogger(__name__)

OFF, DUCK, MUTE = "off", "duck", "mute"
MODES = (OFF, DUCK, MUTE)

POLL_S = 0.5                 # во время записи: переопрос сессий (новые, изменённые пользователем)
PENDING_POLL_S = 3.0         # вне записи, пока есть невозвращённые записи: ждём их приложения
PENDING_TTL_S = 7 * 86400.0  # столько ждём приложение, закрывшееся приглушённым
MAX_ENTRIES = 256            # записей в duck.json не больше (старые забываются)
VOL_EPS = 0.005              # громкость «та же», если отличается меньше (float32 и округления)
CLOSE_TIMEOUT_S = 2.0        # выход ядра: ждать возврата звука не дольше
EXCLUDE_TTL_S = 30.0         # столько помним, входит ли pid сессии в исключённое дерево
EXCLUDE_DEPTH = 16           # предков pid сессии проверяем не больше
STATE_FILE = "duck.json"
FILE_VERSION = 1


# ---------- сессии (Core Audio или фейк в тестах) ----------

class AudioSession(Protocol):
    """Аудиосессия приложения на устройстве вывода (снимок свойств + управление уровнем)."""

    @property
    def instance(self) -> str: ...       # идентификатор экземпляра сессии (уникален, с pid)
    @property
    def session(self) -> str: ...        # идентификатор сессии: приложение + устройство, без pid
    @property
    def pid(self) -> int: ...
    @property
    def system(self) -> bool: ...        # сессия системных звуков
    @property
    def active(self) -> bool: ...        # сейчас играет (AudioSessionStateActive)
    @property
    def default(self) -> bool: ...       # на устройстве вывода по умолчанию (только такие
                                         # приглушаются; возвращаются — на любом)

    def volume(self) -> float: ...
    def muted(self) -> bool: ...
    def set_volume(self, v: float) -> None: ...
    def set_muted(self, m: bool) -> None: ...


class Backend(Protocol):
    def sessions(self) -> list[AudioSession]: ...


class NullBackend:
    """Нет Core Audio (или тесты): сессий нет, ничего не меняется."""

    def sessions(self) -> list[AudioSession]:
        return []


@dataclass(eq=False)
class Entry:
    """Сессия, которую поменяли мы: исходные значения, что поставили и что из этого ещё
    наше. Громкость и беззвук — порознь: пользователь поменял одно — отпускаем только его."""
    instance: str
    session: str
    pid: int
    volume: float        # исходная громкость
    muted: bool          # исходный беззвук
    set_volume: float    # что поставили мы
    set_muted: bool
    ts: float            # когда поменяли (unix, с)
    vol_ours: bool = True    # громкость меняли мы, и в сессии всё ещё поставленная нами
    mute_ours: bool = True   # то же — беззвук


def _default_parent_of() -> Callable[[int], int | None]:
    from .winutil import parent_pid
    return parent_pid


def _new_entry(s: AudioSession, st: tuple[float, bool], tgt: tuple[float, bool],
               ts: float) -> Entry:
    """Запись о сессии, которой ставим tgt вместо st: наше — только то, что меняется."""
    return Entry(s.instance, s.session, s.pid, st[0], st[1], tgt[0], tgt[1], ts,
                 vol_ours=abs(tgt[0] - st[0]) > VOL_EPS, mute_ours=tgt[1] != st[1])


def _f32(x: float) -> float:
    """Громкость так, как её хранит Windows (float32)."""
    return float(ctypes.c_float(x).value)


# ---------- логика ----------

class Ducker:
    """Приглушает аудиосессии на время записи и возвращает их (поток ``rupor-duck``).

    ``duck(mode, level)``, ``restore()`` и ``set_excluded(pids)`` не блокируют: команды
    выполняются по очереди в своём потоке. ``close()`` — вернуть звук и остановить поток (ждёт
    до ``timeout``). ``backend_factory`` вызывается в потоке ``rupor-duck`` (COM
    инициализируется там); ``parent_of(pid)`` — pid родителя (тесты; по умолчанию
    ``winutil.parent_pid``)."""

    def __init__(self, own_pids: Iterable[int] = (), path: Path | None = None,
                 backend_factory: Callable[[], Backend] | None = None,
                 parent_of: Callable[[int], int | None] | None = None) -> None:
        self._own = frozenset(int(p) for p in own_pids if p) | {os.getpid()}
        self._path = path
        self._factory = backend_factory     # None — default_backend (ищется при запуске)
        self._parent_of = parent_of         # None — winutil.parent_pid
        self._q: queue.Queue[tuple[Any, ...]] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._start_lock = threading.Lock()
        # Дальше — только поток rupor-duck
        self._backend: Backend | None = None
        self._entries: list[Entry] = []
        self._active = False
        self._mode = OFF
        self._level = 100
        # Экземпляры, которые в этой записи не приглушаем: пользователь поменял сам (и после
        # смены настроек — тоже его) и «трогать незачем» при текущих настройках (уже без
        # звука, тише цели, не приглушилась) — после смены настроек решается заново
        self._user_off: set[str] = set()
        self._skip: set[str] = set()
        self._known: set[str] = set()       # экземпляры, виденные при прошлом переборе
        # Корни деревьев процессов, которые не приглушаем (перевод встречи), и решения по
        # pid сессий: входит ли pid в такое дерево (pid → (да/нет, когда решено))
        self._excluded: frozenset[int] = frozenset()
        self._exclude_all = False           # встреча слушает весь звук — не приглушать ничего
        self._excl_cache: dict[int, tuple[bool, float]] = {}
        self._saved: str | None = None      # что сейчас в duck.json ("" — файла нет; None — ?)
        self._list_warned = False
        self._save_warned = False
        self.changed = 0                    # сколько сессий сейчас изменено нами (тесты, лог)
        self.file_writes = 0                # сколько раз duck.json записан или удалён (тесты)

    # ---------- API (любой поток) ----------

    def start(self) -> None:
        """Запустить поток; первым делом он возвращает то, что осталось в duck.json."""
        with self._start_lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rupor-duck",
                                                daemon=True)
                self._thread.start()

    def duck(self, mode: str, level: int) -> None:
        """Приглушить (mode "duck": до level % исходной громкости; "mute" — беззвук) и
        держать приглушёнными до restore(), приглушая и заигравшие позже сессии. Во время
        приглушения другой режим (или уровень в "duck") — вернуть и приглушить заново,
        кроме сессий, которые пользователь поменял сам; "off" — вернуть."""
        self._q.put(("duck", mode, int(level)))

    def restore(self) -> None:
        """Вернуть всё, что поменяли (кроме сессий, которые пользователь поменял сам)."""
        self._q.put(("restore",))

    def set_excluded(self, pids: Iterable[int], everything: bool = False) -> None:
        """Не приглушать сессии этих процессов и всех их потомков (дерево процессов, звук
        которого захватывается для перевода встречи; frozenset() — снять исключение).
        ``everything`` — не приглушать ничего (встреча слушает весь звук компьютера: любая
        приглушённая программа пропала бы из захвата). Уже приглушённые сессии исключённого
        возвращаются сразу, не дожидаясь конца записи."""
        self._q.put(("exclude", frozenset(int(p) for p in pids if p), bool(everything)))

    def flush(self, timeout: float = 5.0) -> bool:
        """Дождаться, пока поток выполнит всё, что уже в очереди (тесты). True — успел."""
        if self._thread is None:
            return True
        ev = threading.Event()
        self._q.put(("flush", ev))
        return ev.wait(timeout)

    def close(self, timeout: float = CLOSE_TIMEOUT_S) -> bool:
        """Выход ядра: вернуть звук и остановить поток. False — не успел (тогда звук
        вернёт следующий запуск по duck.json)."""
        t = self._thread
        if t is None:
            return True
        self._q.put(("stop",))
        t.join(timeout)
        if t.is_alive():
            log.warning("Звук компьютера: не успел вернуть громкость за %.1f с — вернётся при "
                        "следующем запуске", timeout)
            return False
        return True

    # ---------- поток ----------

    def _ensure_backend(self) -> Backend | None:
        """Core Audio (создаётся в этом потоке). Не создалась или сломалась (служба звука
        перезапускалась) — пробуем снова при следующем обращении."""
        if self._backend is None:
            try:
                self._backend = (self._factory or default_backend)()
            except Exception as e:
                if not self._list_warned:
                    self._list_warned = True
                    log.warning("Звук компьютера: Core Audio недоступна (%s) — приглушение "
                                "не работает", type(e).__name__)
        return self._backend

    def _run(self) -> None:
        self._ensure_backend()
        try:
            self._entries = self._load()
            if self._entries:
                log.info("Звук компьютера: после прошлого запуска остались изменённые сессии "
                         "(%d) — возвращаю", len(self._entries))
                restored, user, pending = self._settle()
                log.info("Звук компьютера: возвращено %d, пользователь менял сам %d, ждут "
                         "своего приложения %d", restored, user, pending)
            else:
                self._save()              # пустой или битый файл — убрать
        except Exception:
            log.exception("Звук компьютера: не удалось вернуть громкость после прошлого запуска")
        while True:
            timeout: float | None = None
            if self._active:
                timeout = POLL_S
            elif self._entries:
                timeout = PENDING_POLL_S
            try:
                item = self._q.get(timeout=timeout)
            except queue.Empty:
                item = ("tick",)
            cmd = item[0]
            try:
                if cmd == "duck":
                    self._duck(str(item[1]), int(item[2]))
                elif cmd in ("restore", "stop"):
                    self._restore()
                elif cmd == "exclude":
                    self._set_excluded(item[1], bool(item[2]) if len(item) > 2 else False)
                elif cmd == "tick":
                    if self._active:
                        self._poll()
                    elif self._entries:
                        self._settle()
            except Exception:
                log.exception("Звук компьютера: ошибка (%s)", cmd)
            finally:
                if cmd == "flush":
                    item[1].set()
            if cmd == "stop":
                return

    def _list(self) -> list[AudioSession] | None:
        """Сессии, которые можно трогать (не свои, не системные); None — перебор не удался."""
        backend = self._ensure_backend()
        if backend is None:
            return None
        try:
            got = backend.sessions()
        except Exception as e:
            if not self._list_warned:
                self._list_warned = True
                log.warning("Звук компьютера: сессии не перечислены (%s)", type(e).__name__)
            self._backend = None          # в следующий раз — заново (COM-объекты могли умереть)
            return None
        self._list_warned = False
        out: list[AudioSession] = []
        for s in got:
            try:
                if s.system or not s.pid or s.pid in self._own or not s.instance:
                    continue
            except Exception:
                continue
            out.append(s)
        return out

    @staticmethod
    def _read(s: AudioSession) -> tuple[float, bool] | None:
        try:
            return float(s.volume()), bool(s.muted())
        except Exception as e:
            log.debug("Звук компьютера: уровень сессии не прочитан (%s)", type(e).__name__)
            return None

    @staticmethod
    def _release(st: tuple[float, bool], e: Entry) -> bool:
        """Отпустить то, что пользователь поменял сам (в сессии уже не поставленное нами), —
        громкость и беззвук порознь. True — что-то в сессии ещё наше."""
        if e.vol_ours and abs(st[0] - e.set_volume) > VOL_EPS:
            e.vol_ours = False
        if e.mute_ours and st[1] != e.set_muted:
            e.mute_ours = False
        return e.vol_ours or e.mute_ours

    @staticmethod
    def _matches(st: tuple[float, bool], e: Entry) -> bool:
        """В сессии стоит всё, что по записи ещё наше (новый экземпляр приложения пришёл с
        запомненными Windows значениями, которые поставили мы)."""
        return ((not e.vol_ours or abs(st[0] - e.set_volume) <= VOL_EPS)
                and (not e.mute_ours or st[1] == e.set_muted))

    def _target(self, st: tuple[float, bool]) -> tuple[float, bool] | None:
        """Что поставить сессии (громкость, беззвук) или None — трогать незачем."""
        vol, muted = st
        if muted:
            return None                   # уже без звука
        if self._mode == MUTE:
            return vol, True
        target = _f32(vol * max(0, min(100, self._level)) / 100)
        if vol - target <= VOL_EPS:
            return None                   # уже тише цели (или уровень 100 %)
        return target, False

    @staticmethod
    def _put_back(s: AudioSession, e: Entry) -> bool:
        """Вернуть исходное — только то, что меняли мы и что ещё наше."""
        try:
            if e.mute_ours and e.set_muted != e.muted:
                s.set_muted(e.muted)
            if e.vol_ours and abs(e.set_volume - e.volume) > VOL_EPS:
                s.set_volume(e.volume)
            return True
        except Exception as ex:
            log.debug("Звук компьютера: уровень сессии не возвращён (%s)", type(ex).__name__)
            return False

    def _drop(self, e: Entry) -> None:
        self._entries = [x for x in self._entries if x is not e]

    def _duck(self, mode: str, level: int) -> None:
        if mode not in (DUCK, MUTE):
            self._restore()
            return
        if self._active and (mode != self._mode or (mode == DUCK and level != self._level)):
            # Настройки сменились во время записи: вернуть и приглушить заново (от исходной
            # громкости). Сессии, которые пользователь поменял сам, остаются его. В "mute"
            # уровень ни при чём — его смена ничего не меняет (и звук не «прорывается»).
            self._restore(end=False)
        first = not self._active
        self._mode, self._level, self._active = mode, level, True
        added = self._poll()
        if first:
            log.info("Звук компьютера: приглушено сессий %d (%s)", added,
                     "беззвук" if mode == MUTE else f"до {level} %")

    def _poll(self) -> int:
        """Во время записи: то, что пользователь поменял сам, — отпустить; новый экземпляр
        перезапущенного приложения с нашими значениями — считать нашим; заигравшие сессии на
        устройствах по умолчанию — приглушить. Возвращает, сколько приглушено в этот раз."""
        sessions = self._list()
        if sessions is None:
            return 0
        live = {s.instance: s for s in sessions}
        user = 0
        for e in list(self._entries):
            s = live.get(e.instance)
            st = self._read(s) if s is not None else None
            if st is not None and not self._release(st, e):
                self._drop(e)                 # пользователь поменял сам — сессия его
                self._user_off.add(e.instance)
                user += 1
        self._resolve_lost(sessions, live, adopt=True)
        claimed = {e.instance for e in self._entries}
        plan: list[tuple[AudioSession, Entry]] = []
        now = time.time()
        for s in sessions:
            if s.instance in claimed or s.instance in self._user_off or s.instance in self._skip:
                continue
            try:
                if not s.default or not s.active:
                    continue              # не на устройстве по умолчанию / не играет (пока)
            except Exception:
                continue
            if self._is_excluded(s.pid):
                continue                  # звук встречи захватывается — не приглушаем
            st = self._read(s)
            if st is None:
                continue
            tgt = self._target(st)
            if tgt is None:
                self._skip.add(s.instance)    # уже без звука или тише цели — не наше
                continue
            plan.append((s, _new_entry(s, st, tgt, now)))
        added = 0
        if plan:
            # Сначала файл, потом громкость: упадём между ними — при старте увидим, что в
            # сессии исходное (не «наше»), и ничего не тронем
            self._entries.extend(e for _, e in plan)
            self._save()
            for s, e in plan:
                try:
                    if e.mute_ours:
                        s.set_muted(e.set_muted)
                    if e.vol_ours:
                        s.set_volume(e.set_volume)
                    added += 1
                except Exception as ex:
                    log.debug("Звук компьютера: сессия не приглушена (%s)", type(ex).__name__)
                    self._drop(e)
                    self._skip.add(e.instance)
        if user:
            log.info("Звук компьютера: пользователь сам поменял громкость сессий (%d) — их не "
                     "трогаю", user)
        self._known = set(live)
        self._save()                      # пишется, только если записи изменились
        self.changed = sum(1 for e in self._entries if e.instance in live)
        return added

    def _is_excluded(self, pid: int) -> bool:
        """pid сессии — в исключённом дереве (сам корень или его потомок любой глубины:
        у браузеров звук играет дочерний процесс). Решение помнится EXCLUDE_TTL_S: pid
        сессии живёт, пока жив процесс, а цепочка предков — несколько вызовов ядра.
        Исключено всё (встреча слушает весь звук) — любой pid."""
        if self._exclude_all:
            return True
        if not self._excluded or not pid:
            return False
        if pid in self._excluded:
            return True
        now = time.monotonic()
        hit = self._excl_cache.get(pid)
        if hit is not None and now - hit[1] < EXCLUDE_TTL_S:
            return hit[0]
        parent_of = self._parent_of or _default_parent_of()
        found = False
        cur, seen = pid, {pid}
        for _ in range(EXCLUDE_DEPTH):
            try:
                p = parent_of(cur)
            except Exception:
                p = None
            if not p or p in seen:
                break
            if p in self._excluded:
                found = True
                break
            seen.add(p)
            cur = p
        if len(self._excl_cache) > 512:
            self._excl_cache.clear()
        self._excl_cache[pid] = (found, now)
        return found

    def _set_excluded(self, pids: frozenset[int], everything: bool = False) -> None:
        """Сменить исключённые деревья (``everything`` — исключить всё); приглушённые нами
        сессии нового исключения вернуть сразу (как в конце записи: только то, что ещё
        наше)."""
        self._excluded = pids
        self._exclude_all = everything
        self._excl_cache.clear()
        if not (pids or everything) or not self._entries:
            return
        sessions = self._list()
        if sessions is None:
            return
        live = {s.instance: s for s in sessions}
        back = 0
        for e in list(self._entries):
            s = live.get(e.instance)
            if s is None or not self._is_excluded(e.pid):
                continue
            st = self._read(s)
            if st is None:
                continue
            if self._release(st, e) and not self._put_back(s, e):
                continue                  # не вернулась — вернём в конце записи
            self._drop(e)
            back += 1
        if back:
            self._save()
            self.changed = sum(1 for e in self._entries if e.instance in live)
            log.info("Звук компьютера: %s исключено из приглушения, отпущено сессий %d",
                     "всё" if everything else "дерево процессов", back)

    def _resolve_lost(self, sessions: list[AudioSession], live: dict[str, AudioSession],
                      adopt: bool) -> int:
        """Записи о сессиях, которых больше нет (приложение закрылось или перезапустилось),
        сверяются с НОВЫМИ экземплярами того же приложения (тот же идентификатор сессии):
        Windows даёт новому экземпляру запомненную громкость. Пришёл с нашей — вернуть
        исходную (adopt — во время записи: считать его приглушённым нами, вернём в конце);
        с другой — записи этого приложения забыть (Windows нашу не запомнила).
        Возвращает, сколько экземпляров вернули или взяли себе."""
        lost = [e for e in self._entries if e.instance not in live]
        if not lost:
            return 0
        taken = {e.instance for e in self._entries}
        gone: set[int] = set()            # id() решённых записей
        done = 0
        for s in sessions:
            if s.instance in self._known or s.instance in taken:
                continue
            same_app = [e for e in lost if e.session == s.session and id(e) not in gone]
            if not same_app:
                continue
            st = self._read(s)
            if st is None:
                continue
            hit = next((e for e in same_app if self._matches(st, e)), None)
            if hit is not None and adopt:
                hit.instance, hit.pid = s.instance, s.pid
                taken.add(s.instance)
            elif hit is not None and not self._put_back(s, hit):
                continue                  # не вернулась — записи ждут дальше
            if hit is not None:
                done += 1
            for e in same_app:
                if not (adopt and e is hit):
                    gone.add(id(e))
        if gone:
            self._entries = [e for e in self._entries if id(e) not in gone]
        return done

    def _restore(self, end: bool = True) -> None:
        """Вернуть всё наше. end=False — не конец записи, а смена настроек: сессии, которые
        пользователь поменял сам, остаются его и после нового приглушения."""
        was = self._active
        self._active = False
        self._skip.clear()
        if self._entries:
            n = len(self._entries)
            restored, user, pending = self._settle(None if end else self._user_off)
            if was or restored:
                log.info("Звук компьютера: возвращено сессий %d из %d%s%s", restored, n,
                         f", пользователь менял сам {user}" if user else "",
                         f", ждут своего приложения {pending}" if pending else "")
        self.changed = 0
        if end:
            self._user_off.clear()

    def _settle(self, user_off: set[str] | None = None) -> tuple[int, int, int]:
        """Вернуть исходное всем записям, чьи сессии сейчас с нашими значениями (на любом
        активном устройстве); записи о закрытых приложениях ждут их нового экземпляра (не
        дольше PENDING_TTL_S). user_off — куда сложить сессии, которые пользователь поменял
        сам (смена настроек посреди записи). (возвращено, менял пользователь, ждут)."""
        sessions = self._list()
        if sessions is None:
            self._save()
            return 0, 0, len(self._entries)
        live = {s.instance: s for s in sessions}
        keep: list[Entry] = []
        restored = user = 0
        for e in self._entries:
            s = live.get(e.instance)
            if s is None:
                keep.append(e)                # сессии нет — ждём приложение
                continue
            st = self._read(s)
            if st is None:
                keep.append(e)                # не прочиталась — попробуем позже
            elif not self._release(st, e):
                user += 1                     # пользователь поменял сам — не трогаем
                if user_off is not None:
                    user_off.add(e.instance)
            elif self._put_back(s, e):
                restored += 1
            else:
                keep.append(e)
        self._entries = keep
        restored += self._resolve_lost(sessions, live, adopt=False)
        now = time.time()
        fresh = [e for e in self._entries if now - e.ts < PENDING_TTL_S]
        if len(fresh) != len(self._entries):
            log.info("Звук компьютера: забыто записей о давно закрытых приложениях: %d",
                     len(self._entries) - len(fresh))
        self._entries = fresh[-MAX_ENTRIES:]
        self._known = set(live)
        self._save()                      # пишется, только если записи изменились
        return restored, user, len(self._entries)

    # ---------- duck.json ----------

    def _file(self) -> Path:
        if self._path is None:
            from .config import app_dir
            self._path = app_dir() / STATE_FILE
        return self._path

    def _load(self) -> list[Entry]:
        p = self._file()
        try:
            raw = json.loads(p.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as e:
            log.warning("Звук компьютера: %s не прочитан (%s) — пропускаю", STATE_FILE,
                        type(e).__name__)
            return []
        items = raw.get("entries") if isinstance(raw, dict) else None
        out: list[Entry] = []
        for it in items if isinstance(items, list) else []:
            entry = _entry(it)
            if entry is not None:
                out.append(entry)
        return out[-MAX_ENTRIES:]

    def _save(self) -> None:
        """Записать duck.json, если записи изменились (иначе — ничего: пока есть ожидающие
        записи, _settle зовётся раз в PENDING_POLL_S, и файл не переписывается зря)."""
        data = ""
        if self._entries:
            data = json.dumps({"version": FILE_VERSION,
                               "entries": [asdict(e) for e in self._entries]},
                              ensure_ascii=False, indent=1)
        if data == self._saved:
            return
        p = self._file()
        try:
            if data:
                tmp = p.with_suffix(".tmp")
                tmp.write_text(data, encoding="utf-8")
                tmp.replace(p)
            else:
                p.unlink(missing_ok=True)
        except OSError as e:
            if not self._save_warned:
                self._save_warned = True
                log.warning("Звук компьютера: %s не записан (%s) — после падения громкость "
                            "сама не вернётся", STATE_FILE, type(e).__name__)
            return
        self._saved = data
        self.file_writes += 1


_ENTRY_TYPES: dict[str, type] = {"instance": str, "session": str, "pid": int, "volume": float,
                                 "muted": bool, "set_volume": float, "set_muted": bool,
                                 "ts": float, "vol_ours": bool, "mute_ours": bool}
# Поля, которых нет в файлах первых версий: выводятся из значений (наше — то, что меняли)
_ENTRY_OPTIONAL = frozenset({"vol_ours", "mute_ours"})


def _entry(it: Any) -> Entry | None:
    """Запись из duck.json или None (битая)."""
    if not isinstance(it, dict):
        return None
    vals: dict[str, Any] = {}
    for f in fields(Entry):
        if f.name in _ENTRY_OPTIONAL and f.name not in it:
            continue
        v = it.get(f.name)
        want = _ENTRY_TYPES[f.name]
        if want is float and isinstance(v, (int, float)) and not isinstance(v, bool):
            v = float(v)
        elif want is int and isinstance(v, bool):
            return None
        if not isinstance(v, want):
            return None
        vals[f.name] = v
    if "vol_ours" not in vals:
        vals["vol_ours"] = abs(vals["set_volume"] - vals["volume"]) > VOL_EPS
    if "mute_ours" not in vals:
        vals["mute_ours"] = vals["set_muted"] != vals["muted"]
    e = Entry(**vals)
    if not e.instance or not 0.0 <= e.volume <= 1.0 or not 0.0 <= e.set_volume <= 1.0:
        return None
    if not (e.vol_ours or e.mute_ours):
        return None                       # возвращать нечего
    return e


# ---------- Core Audio (comtypes) ----------

_E_RENDER = 0
_ROLES = (0, 1, 2)               # eConsole, eMultimedia (их можно развести: SoundSwitch, nircmd),
                                 # eCommunications
_DEVICE_STATE_ACTIVE = 0x1
_STATE_ACTIVE, _STATE_EXPIRED = 1, 2
_CLSID_MMDEVICE_ENUMERATOR = "{BCDE0395-E52F-467C-8E3D-C4579291692E}"
_CLSCTX_ALL = 0x17
_COINIT_MULTITHREADED = 0

_import_lock = threading.Lock()
_ifaces_lock = threading.Lock()
_ifaces: Any = None


def _com_init() -> Any:
    """COM в текущем потоке (MTA) и comtypes: при первом импорте comtypes сам зовёт
    CoInitializeEx(sys.coinit_flags) — ставим MTA, чтобы не получить ошибку смены режима
    (как rupor.uia)."""
    ctypes.windll.ole32.CoInitializeEx(None, _COINIT_MULTITHREADED)
    mod = sys.modules.get("comtypes")
    if mod is not None:
        return mod
    with _import_lock:
        missing = object()
        prev = getattr(sys, "coinit_flags", missing)
        sys.coinit_flags = _COINIT_MULTITHREADED  # type: ignore[attr-defined]
        try:
            import comtypes
        finally:
            if prev is missing:
                try:
                    del sys.coinit_flags  # type: ignore[attr-defined]
                except AttributeError:
                    pass
            else:
                sys.coinit_flags = prev  # type: ignore[attr-defined]
    return comtypes


def _interfaces() -> Any:
    """Описания интерфейсов Core Audio (создаются один раз, после импорта comtypes)."""
    global _ifaces
    with _ifaces_lock:
        if _ifaces is None:
            _ifaces = _define_interfaces()
        return _ifaces


def _define_interfaces() -> Any:
    from ctypes import HRESULT, POINTER, c_float, c_int, c_uint, c_void_p
    from ctypes.wintypes import BOOL, DWORD, LPCWSTR
    from types import SimpleNamespace

    from comtypes import COMMETHOD, GUID, IUnknown

    LPCGUID = POINTER(GUID)
    OUT_STR = POINTER(c_void_p)          # LPWSTR, освобождается CoTaskMemFree (_take_str)

    class ISimpleAudioVolume(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{87CE5498-68D6-44E5-9215-6DA47EF883D8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "SetMasterVolume",
                      (["in"], c_float, "fLevel"), (["in"], LPCGUID, "EventContext")),
            COMMETHOD([], HRESULT, "GetMasterVolume", (["out"], POINTER(c_float), "pfLevel")),
            COMMETHOD([], HRESULT, "SetMute",
                      (["in"], BOOL, "bMute"), (["in"], LPCGUID, "EventContext")),
            COMMETHOD([], HRESULT, "GetMute", (["out"], POINTER(BOOL), "pbMute")),
        ]

    class IAudioSessionControl(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{F4B1A599-7266-4319-A8CA-E70ACB11E8CD}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(c_int), "pRetVal")),
            COMMETHOD([], HRESULT, "GetDisplayName", (["out"], OUT_STR, "pRetVal")),
            COMMETHOD([], HRESULT, "SetDisplayName",
                      (["in"], LPCWSTR, "Value"), (["in"], LPCGUID, "EventContext")),
            COMMETHOD([], HRESULT, "GetIconPath", (["out"], OUT_STR, "pRetVal")),
            COMMETHOD([], HRESULT, "SetIconPath",
                      (["in"], LPCWSTR, "Value"), (["in"], LPCGUID, "EventContext")),
            COMMETHOD([], HRESULT, "GetGroupingParam", (["out"], POINTER(GUID), "pRetVal")),
            COMMETHOD([], HRESULT, "SetGroupingParam",
                      (["in"], LPCGUID, "Override"), (["in"], LPCGUID, "EventContext")),
            COMMETHOD([], HRESULT, "RegisterAudioSessionNotification",
                      (["in"], c_void_p, "NewNotifications")),
            COMMETHOD([], HRESULT, "UnregisterAudioSessionNotification",
                      (["in"], c_void_p, "NewNotifications")),
        ]

    class IAudioSessionControl2(IAudioSessionControl):
        _iid_ = GUID("{BFB7FF88-7239-4FC9-8FA2-07C950BE9C6D}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetSessionIdentifier", (["out"], OUT_STR, "pRetVal")),
            COMMETHOD([], HRESULT, "GetSessionInstanceIdentifier",
                      (["out"], OUT_STR, "pRetVal")),
            COMMETHOD([], HRESULT, "GetProcessId", (["out"], POINTER(DWORD), "pRetVal")),
            COMMETHOD([], HRESULT, "IsSystemSoundsSession"),     # S_OK — да, S_FALSE — нет
            COMMETHOD([], HRESULT, "SetDuckingPreference", (["in"], BOOL, "optOut")),
        ]

    class IAudioSessionEnumerator(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{E2F5BB11-0570-40CA-ACDD-3AA01277DEE8}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_int), "SessionCount")),
            COMMETHOD([], HRESULT, "GetSession", (["in"], c_int, "SessionCount"),
                      (["out"], POINTER(POINTER(IAudioSessionControl)), "Session")),
        ]

    class IAudioSessionManager(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{BFA971F1-4D5E-40BB-935E-967039BFBEE4}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetAudioSessionControl",
                      (["in"], LPCGUID, "AudioSessionGuid"), (["in"], DWORD, "StreamFlags"),
                      (["out"], POINTER(POINTER(IAudioSessionControl)), "SessionControl")),
            COMMETHOD([], HRESULT, "GetSimpleAudioVolume",
                      (["in"], LPCGUID, "AudioSessionGuid"), (["in"], DWORD, "StreamFlags"),
                      (["out"], POINTER(POINTER(ISimpleAudioVolume)), "AudioVolume")),
        ]

    class IAudioSessionManager2(IAudioSessionManager):
        _iid_ = GUID("{77AA99A0-1BD6-484F-8BC7-2C654C9A9B6F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetSessionEnumerator",
                      (["out"], POINTER(POINTER(IAudioSessionEnumerator)), "SessionEnum")),
            COMMETHOD([], HRESULT, "RegisterSessionNotification",
                      (["in"], c_void_p, "SessionNotification")),
            COMMETHOD([], HRESULT, "UnregisterSessionNotification",
                      (["in"], c_void_p, "SessionNotification")),
            COMMETHOD([], HRESULT, "RegisterDuckNotification",
                      (["in"], LPCWSTR, "sessionID"), (["in"], c_void_p, "duckNotification")),
            COMMETHOD([], HRESULT, "UnregisterDuckNotification",
                      (["in"], c_void_p, "duckNotification")),
        ]

    class IMMDevice(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{D666063F-1587-4E43-81F1-B948E807363F}")
        _methods_ = [
            COMMETHOD([], HRESULT, "Activate", (["in"], POINTER(GUID), "iid"),
                      (["in"], DWORD, "dwClsCtx"), (["in"], c_void_p, "pActivationParams"),
                      (["out"], POINTER(POINTER(IUnknown)), "ppInterface")),
            COMMETHOD([], HRESULT, "OpenPropertyStore", (["in"], DWORD, "stgmAccess"),
                      (["out"], POINTER(c_void_p), "ppProperties")),
            COMMETHOD([], HRESULT, "GetId", (["out"], OUT_STR, "ppstrId")),
            COMMETHOD([], HRESULT, "GetState", (["out"], POINTER(DWORD), "pdwState")),
        ]

    class IMMDeviceCollection(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{0BD7A1BE-7A1A-44DB-8397-CC5392387B5E}")
        _methods_ = [
            COMMETHOD([], HRESULT, "GetCount", (["out"], POINTER(c_uint), "pcDevices")),
            COMMETHOD([], HRESULT, "Item", (["in"], c_uint, "nDevice"),
                      (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
        ]

    class IMMDeviceEnumerator(IUnknown):  # type: ignore[misc]
        _iid_ = GUID("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
        _methods_ = [
            COMMETHOD([], HRESULT, "EnumAudioEndpoints", (["in"], c_int, "dataFlow"),
                      (["in"], DWORD, "dwStateMask"),
                      (["out"], POINTER(POINTER(IMMDeviceCollection)), "ppDevices")),
            COMMETHOD([], HRESULT, "GetDefaultAudioEndpoint", (["in"], c_int, "dataFlow"),
                      (["in"], c_int, "role"), (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
            COMMETHOD([], HRESULT, "GetDevice", (["in"], LPCWSTR, "pwstrId"),
                      (["out"], POINTER(POINTER(IMMDevice)), "ppDevice")),
            COMMETHOD([], HRESULT, "RegisterEndpointNotificationCallback",
                      (["in"], c_void_p, "pClient")),
            COMMETHOD([], HRESULT, "UnregisterEndpointNotificationCallback",
                      (["in"], c_void_p, "pClient")),
        ]

    return SimpleNamespace(
        ISimpleAudioVolume=ISimpleAudioVolume, IAudioSessionControl=IAudioSessionControl,
        IAudioSessionControl2=IAudioSessionControl2,
        IAudioSessionEnumerator=IAudioSessionEnumerator,
        IAudioSessionManager2=IAudioSessionManager2, IMMDevice=IMMDevice,
        IMMDeviceCollection=IMMDeviceCollection, IMMDeviceEnumerator=IMMDeviceEnumerator,
        GUID=GUID)


def _take_str(addr: Any) -> str:
    """Строка Core Audio (LPWSTR от CoTaskMemAlloc): прочитать и освободить."""
    if not addr:
        return ""
    try:
        return ctypes.wstring_at(addr)
    finally:
        ctypes.windll.ole32.CoTaskMemFree(ctypes.c_void_p(addr))


class _CoreSession:
    """Сессия Core Audio: свойства читаются при переборе, уровень — каждый раз заново."""

    def __init__(self, instance: str, session: str, pid: int, system: bool, active: bool,
                 default: bool, vol: Any) -> None:
        self.instance, self.session, self.pid = instance, session, pid
        self.system, self.active, self.default = system, active, default
        self._vol = vol

    def volume(self) -> float:
        return float(self._vol.GetMasterVolume())

    def muted(self) -> bool:
        return bool(self._vol.GetMute())

    def set_volume(self, v: float) -> None:
        self._vol.SetMasterVolume(max(0.0, min(1.0, float(v))), None)

    def set_muted(self, m: bool) -> None:
        self._vol.SetMute(1 if m else 0, None)


class CoreAudioBackend:
    """Аудиосессии на всех активных устройствах вывода; ``default`` — у сессий на
    устройствах по умолчанию (eConsole, eMultimedia, eCommunications). Создаётся и
    используется в одном потоке (COM)."""

    def __init__(self) -> None:
        comtypes = _com_init()
        self._i = _interfaces()
        self._enum = comtypes.CoCreateInstance(
            self._i.GUID(_CLSID_MMDEVICE_ENUMERATOR), interface=self._i.IMMDeviceEnumerator,
            clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self._managers: dict[str, Any] = {}
        self.devices = 0          # устройств в последнем переборе
        self.defaults = 0         # из них — по умолчанию
        self.errors = 0           # сессий, которые не удалось прочитать (всего)
        self.enum_error: str | None = None   # перебор всех устройств не удался (имя ошибки)

    def _manager(self, dev: Any, dev_id: str) -> Any:
        mgr = self._managers.get(dev_id)
        if mgr is None:
            if len(self._managers) > 16:
                self._managers.clear()
            unk = dev.Activate(self._i.IAudioSessionManager2._iid_, _CLSCTX_ALL, None)
            mgr = unk.QueryInterface(self._i.IAudioSessionManager2)
            self._managers[dev_id] = mgr
        return mgr

    def _devices(self) -> list[tuple[str, Any, bool]]:
        """(id, устройство, по умолчанию): сначала устройства по умолчанию, затем прочие
        активные устройства вывода (на них возвращаем приглушённое до смены устройства)."""
        out: list[tuple[str, Any, bool]] = []
        ids: set[str] = set()
        for role in _ROLES:
            try:
                dev = self._enum.GetDefaultAudioEndpoint(_E_RENDER, role)
            except Exception:
                continue                  # устройства нет (E_NOTFOUND)
            dev_id = _take_str(dev.GetId())
            if dev_id not in ids:
                ids.add(dev_id)
                out.append((dev_id, dev, True))
        self.defaults = len(out)
        self.enum_error = None
        try:
            col = self._enum.EnumAudioEndpoints(_E_RENDER, _DEVICE_STATE_ACTIVE)
            count = int(col.GetCount())
        except Exception as e:
            self.enum_error = type(e).__name__
            log.debug("Звук компьютера: устройства вывода не перечислены (%s)", self.enum_error)
            return out
        for k in range(count):
            try:
                dev = col.Item(k)
                dev_id = _take_str(dev.GetId())
            except Exception as e:        # устройство как раз отключают — в другой раз
                log.debug("Звук компьютера: устройство не прочитано (%s)", type(e).__name__)
                continue
            if dev_id not in ids:
                ids.add(dev_id)
                out.append((dev_id, dev, False))
        return out

    def sessions(self) -> list[AudioSession]:
        i = self._i
        out: list[AudioSession] = []
        devices = self._devices()
        if not devices and self.enum_error:
            # ни одного устройства и перебор не удался — объекты Core Audio, видимо, мертвы
            # (служба звука перезапускалась): пусть Ducker создаст их заново
            raise OSError(f"устройства вывода не перечислены ({self.enum_error})")
        for dev_id, dev, default in devices:
            try:
                en = self._manager(dev, dev_id).GetSessionEnumerator()
                count = int(en.GetCount())
            except Exception:
                self._managers.pop(dev_id, None)
                if default:
                    raise                 # служба звука перезапускалась — Core Audio заново
                continue                  # прочее устройство (отключается) — в другой раз
            for k in range(count):
                try:
                    ctl = en.GetSession(k)
                    c2 = ctl.QueryInterface(i.IAudioSessionControl2)
                    state = int(c2.GetState())
                    if state == _STATE_EXPIRED:
                        continue
                    s = _CoreSession(
                        _take_str(c2.GetSessionInstanceIdentifier()),
                        _take_str(c2.GetSessionIdentifier()),
                        int(c2.GetProcessId()),
                        c2.IsSystemSoundsSession() == 0,     # S_OK — системные звуки
                        state == _STATE_ACTIVE,
                        default,
                        ctl.QueryInterface(i.ISimpleAudioVolume))
                except Exception as e:
                    self.errors += 1
                    log.debug("Звук компьютера: сессия не прочитана (%s)", type(e).__name__)
                    continue
                out.append(s)
        self.devices = len(devices)
        return out


def default_backend() -> Backend:
    """Настоящий Core Audio (тесты подменяют эту функцию — см. tests/conftest.py)."""
    return CoreAudioBackend()


def selftest(timeout: float = 10.0) -> tuple[bool, str]:
    """Для ``--uia-selftest``: Core Audio работает в этой сборке — перебрать устройства вывода
    и сессии и прочитать их уровни, ничего не меняя. Строка без имён:
    «Core Audio: ok, сессий N, устройств вывода D»."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            b = CoreAudioBackend()
            ss = b.sessions()
            bad = 0
            for s in ss:
                try:
                    s.volume()
                    s.muted()
                except Exception:
                    bad += 1
            box["r"] = (b.devices, len(ss), bad + b.errors, b.enum_error)
        except Exception as e:
            box["e"] = type(e).__name__

    t = threading.Thread(target=run, name="rupor-duck-selftest", daemon=True)
    t.start()
    t.join(timeout)
    if "r" in box:
        devices, n, bad, enum_error = box["r"]
        if enum_error:
            return False, f"Core Audio: ошибка перебора устройств вывода {enum_error}"
        if not devices:
            return True, "Core Audio: ok, устройства вывода нет"
        return True, (f"Core Audio: ok, сессий {n}, устройств вывода {devices}"
                      + (f", не прочитано {bad}" if bad else ""))
    if "e" in box:
        return False, f"Core Audio: ошибка {box['e']}"
    return False, f"Core Audio: нет ответа за {timeout:g} с"
