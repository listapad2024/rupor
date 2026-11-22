r"""Встреча замечается сама: программа держит микрофон и играет звук → предложить расшифровку
(событие ``meeting_suggest``, ответ — команда ``meeting_suggest_answer``).

**Сигнал — журнал доступа к микрофону Windows** (CapabilityAccessManager, только чтение):

- ``HKCU\Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\
  microphone\NonPackaged\<путь exe, «\» заменены на «#»>`` — обычные программы;
- ``...\microphone\<PackageFamilyName>`` — приложения Store (новый Teams, WhatsApp…);
- значения ``LastUsedTimeStart`` / ``LastUsedTimeStop`` (REG_QWORD, FILETIME): ``Stop == 0``
  при ``Start != 0`` — микрофон занят сейчас, с момента ``Start``.

Запись может «зависнуть» с ``Stop == 0`` (программа упала с открытым микрофоном; на машине
разработчика так висят старый путь Яндекс Браузера и Copilot): ей не верим, если программы с
этим путём (пакетом) нет среди запущенных или все такие процессы запущены ПОЗЖЕ ``Start``.

**Правило**: микрофон занят непрерывно ≥ ``MIC_MIN_S`` (10 с) И в дереве процессов этой
программы есть ИГРАЮЩАЯ аудиосессия (AudioSessionStateActive: во время звонка поток вывода
открыт, даже когда собеседники молчат) → встреча. Браузеры подходят всегда, настольные
программы встреч (Zoom, Teams, Telegram, Discord, MAX…) — при ``meeting_detect_desktop_apps``.
Микрофон держит помощник программы со своим exe (``MIC_HELPERS``: max-service.exe у MAX,
CptHost.exe у Zoom, WebView2) — предлагается сама программа. Браузер без заголовка встречи
(звук всех вкладок — одно дерево процессов, а микрофон берут и голосовые сообщения) — только
после ``BROWSER_UNNAMED_MIN_S`` (60 с) микрофона и не после долгой тишины с занятым микрофоном;
микрофон занят дольше ``MIC_MAX_AGE_S`` (3 ч: забытая вкладка, голосовой помощник) — не
разговор.
Не предлагаем никогда: свои exe (``rupor-core.exe``, ``rupor.exe`` — Рупор держит микрофон
всегда), свои процессы и ``meeting_ignore_apps`` (OBS, Dragon, ffmpeg, python…).

**Дёшево**: опрос раз в ``POLL_S`` (2 с) и только пока встреча не идёт — чтение реестра
(подключи NonPackaged открываются только у подходящих имён exe, ≈ 1 мс); снимок процессов
(≈ 30 мс) и аудиосессии (≈ 10 мс) — только когда есть кандидат, которого ещё не предлагали.
Предлагается один раз на (программа, начало занятия микрофона): «Не сейчас» — больше не
предлагать в этом разговоре (браузеру без заголовка встречи — и такие предложения 30 мин);
«Не предлагать для …» — exe в ``meeting_ignore_apps``.

**Название**: заголовки окон дерева (и заголовки, замеченные при смене активного окна) →
служба («Google Meet», «Zoom», …) или совпавший шаблон ``meeting_title_patterns``; не нашлось —
``title`` пустой, имя программы (описание exe) — в ``app``.

**Конец встречи** (пока идёт сеанс): раз в ``SESSION_CHECK_S`` читается только запись
микрофона программы сеанса; микрофон свободен ≥ ``END_MIC_OFF_S`` (30 с) и звук источника не
играет — подсказка ``MeetingStatus.hint = "ended"``. Та же запись — автоостановке
(``session_mic``: свободен ли микрофон и сколько; ``meeting.AutoStop`` решает сама).
"""
from __future__ import annotations

import collections
import ctypes
import logging
import ntpath
import re
import secrets
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable, Collection, Iterable, Mapping, Sequence

from . import config, winutil
from .meeting import BROWSERS as _MEETING_BROWSERS

log = logging.getLogger(__name__)

POLL_S = 2.0                    # опрос реестра, пока встреча не идёт (не чаще)
MIC_MIN_S = 10.0                # микрофон занят непрерывно дольше — кандидат во встречу
STALE_SLACK_S = 2.0             # запись микрофона старше запуска программы на столько — зависла
AUDIO_RETRY_S = 2.0             # микрофон занят, звук не играет — проверять снова через…
AUDIO_RETRY_SLOW_S = 10.0       # …а после AUDIO_SLOW_AFTER_S без звука — реже
AUDIO_SLOW_AFTER_S = 60.0
SESSION_CHECK_S = 5.0           # идёт сеанс: запись микрофона его программы — так часто
END_MIC_OFF_S = 30.0            # микрофон свободен дольше и звука нет — «встреча закончилась»
PENDING_MAX = 10                # предложений без ответа помним
PENDING_TTL_S = 600.0           # …не дольше 10 минут
DONE_MAX = 500                  # (программа, начало микрофона), которые уже разобраны
HINTS_MAX = 5                   # заголовков окон встреч на процесс (с активного окна)
HINT_TTL_S = 1800.0
HINT_ENDED = "ended"            # MeetingStatus.hint: встреча, похоже, закончилась
# Микрофон занят часами (забытая вкладка, голосовой помощник браузера) — это не разговор:
# не предлагать и не перепроверять раз в 10 с, пока его не отпустят
MIC_MAX_AGE_S = 3 * 3600.0
# Браузер без заголовка встречи (не Meet/Zoom/Teams/шаблон): звук играет любая вкладка
# (музыка), а микрофон берут и голосовые сообщения, и диктовка в вкладке — нужен занятый
# дольше микрофон; занятый без звука дольше QUIET_MAX_S до первого звука — не встреча
# (заигравшее потом — другая вкладка); «Не сейчас» — такие предложения этой программы не
# раньше чем через LATER_SNOOZE_S. Встречи с узнанным заголовком — как обычно.
BROWSER_UNNAMED_MIN_S = 60.0
QUIET_MAX_S = 900.0
LATER_SNOOZE_S = 1800.0
HELPER_DEPTH = 16               # предков помощника (MIC_HELPERS) смотрим не больше

MIC_KEY = (r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager"
           r"\ConsentStore\microphone")
NON_PACKAGED = "NonPackaged"
_FILETIME_EPOCH_S = 11644473600          # 1601-01-01 → 1970-01-01
_TICKS = 10_000_000                      # FILETIME: 100 нс

# Свои exe (любой путь): ядро Рупора держит микрофон открытым всегда
OWN_EXES = frozenset({"rupor-core.exe", "rupor.exe"})
# Браузеры: подходят всегда (имя exe → понятное имя, если у exe нет описания)
BROWSERS: dict[str, str] = {
    **_MEETING_BROWSERS,
    "comet.exe": "Comet", "maxthon.exe": "Maxthon", "atlas.exe": "ChatGPT Atlas",
    "dia.exe": "Dia", "sidekick.exe": "Sidekick", "palemoon.exe": "Pale Moon",
}
# Настольные программы встреч и звонков (meeting_detect_desktop_apps)
DESKTOP_APPS: dict[str, str] = {
    "zoom.exe": "Zoom", "ms-teams.exe": "Microsoft Teams", "teams.exe": "Microsoft Teams",
    "telegram.exe": "Telegram", "discord.exe": "Discord", "skype.exe": "Skype",
    "slack.exe": "Slack", "whatsapp.exe": "WhatsApp", "whatsapp.root.exe": "WhatsApp",
    "viber.exe": "Viber", "webex.exe": "Webex", "ciscocollabhost.exe": "Webex",
    "webexmta.exe": "Webex", "lync.exe": "Skype for Business", "signal.exe": "Signal",
    "element.exe": "Element", "mattermost.exe": "Mattermost", "ringcentral.exe": "RingCentral",
    "g2mcomm.exe": "GoTo Meeting", "gotomeeting.exe": "GoTo Meeting",
    "telemost.exe": "Yandex Telemost", "yandextelemost.exe": "Yandex Telemost",
    "ktalk.exe": "Kontur.Talk", "vk calls.exe": "VK Calls",
    # MAX: звонок (WebRTC: и микрофон, и звук собеседников) — в max-service.exe из папки
    # max-service рядом с max.exe; сам max.exe (интерфейс) микрофон не берёт, а это имя
    # носят и чужие программы (Cycling '74 Max) — его в таблице нет
    "max-service.exe": "MAX",
}
KNOWN_APPS = frozenset(BROWSERS) | frozenset(DESKTOP_APPS)
# Микрофон держит вспомогательный процесс со своим exe: exe помощника → exe программы, ради
# которой он работает ("" — любая программа из таблиц среди его предков). Программа ищется
# среди предков помощника, иначе — в той же папке установки; нашлась — её дерево процессов
# (звук может играть и она, и помощник), а предлагается она сама (её имя, «Не предлагать»).
MIC_HELPERS: dict[str, str] = {
    "max-service.exe": "max.exe",    # MAX (max.exe не в таблице — предлагается max-service.exe)
    "cpthost.exe": "zoom.exe",       # Zoom: демонстрация экрана со звуком компьютера
    "msedgewebview2.exe": "",        # WebView2 программ встреч (новый Teams, WhatsApp)
}
# Заголовок окна → служба встреч (слова целиком; кириллические названия — из
# meeting_title_patterns: «Телемост», «Контур.Толк»)
SERVICES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bgoogle meet\b|\bmeet\.google\.com\b|^meet\s*[-\u2013\u2014]", re.I),
     "Google Meet"),
    (re.compile(r"\bzoom\b", re.I), "Zoom"),
    (re.compile(r"\bmicrosoft teams\b|\bteams\b", re.I), "Microsoft Teams"),
    (re.compile(r"\bjitsi\b", re.I), "Jitsi Meet"),
    (re.compile(r"\bwebex\b", re.I), "Webex"),
    (re.compile(r"\bwhereby\b", re.I), "Whereby"),
    (re.compile(r"\btelemost\b", re.I), "Yandex Telemost"),
    (re.compile(r"\bktalk\b|\bkontur\.?\s?talk\b", re.I), "Kontur.Talk"),
    (re.compile(r"\bdiscord\b", re.I), "Discord"),
    (re.compile(r"\bskype\b", re.I), "Skype"),
    (re.compile(r"\bslack\b", re.I), "Slack"),
)

ProcessTable = Mapping[int, tuple[int, str]]          # pid → (pid родителя, имя exe)
Reader = Callable[[Callable[[str], bool] | None], list["MicUse"]]
SessionsFn = Callable[[], Iterable[tuple[int, bool]]]  # аудиосессии: (pid, играет)
SourcesFn = Callable[[], list[dict[str, Any]]]         # AudioSource (словари WS)


def filetime_now() -> int:
    """Текущее время как FILETIME (100 нс с 1601 года, UTC)."""
    return int((time.time() + _FILETIME_EPOCH_S) * _TICKS)


def exe_of(path: str) -> str:
    """Имя exe в нижнем регистре: «C:\\…\\Brave.exe» → «brave.exe»."""
    return ntpath.basename(path or "").strip().lower()


# ---------- реестр: кто держит микрофон ----------

@dataclass(frozen=True)
class MicUse:
    """Запись журнала микрофона: подключ (путь exe с «#» или PackageFamilyName) и время."""
    key: str
    packaged: bool
    start: int              # LastUsedTimeStart, FILETIME; 0 — не пользовалась
    stop: int               # LastUsedTimeStop; 0 — занят сейчас; -1 — значения нет

    @property
    def in_use(self) -> bool:
        return self.start > 0 and self.stop == 0

    @property
    def path(self) -> str:
        """Путь exe (обычная программа; для журнала и имени exe — «#» в самом пути тоже
        станет разделителем, сверка с процессами идёт по ключу, см. map_use); у приложения
        Store — ""."""
        return "" if self.packaged else self.key.replace("#", "\\")

    @property
    def exe(self) -> str:
        return "" if self.packaged else exe_of(self.path)

    @property
    def ident(self) -> tuple[str, int]:
        """(программа, начало занятия микрофона) — предлагаем один раз на такую пару."""
        return self.key.casefold(), self.start


def _subkeys(key: Any) -> list[str]:
    import winreg
    out: list[str] = []
    i = 0
    while True:
        try:
            out.append(winreg.EnumKey(key, i))
        except OSError:
            return out
        i += 1


def _qword(key: Any, name: str) -> int | None:
    import winreg
    try:
        v, _t = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return int(v) if isinstance(v, int) else None


def _read_use(parent: Any, name: str, packaged: bool) -> MicUse | None:
    import winreg
    try:
        with winreg.OpenKey(parent, name) as k:
            start = _qword(k, "LastUsedTimeStart")
            stop = _qword(k, "LastUsedTimeStop")
    except OSError:
        return None
    return MicUse(name, packaged, start or 0, -1 if stop is None else stop)


def read_mic_usage(want: Callable[[str], bool] | None = None) -> list[MicUse]:
    """Записи журнала микрофона текущего пользователя (только чтение). ``want(exe)`` —
    отбор обычных программ по имени exe ДО открытия подключа (их там сотни — дёшево
    остаются только нужные); приложения Store (десяток) читаются все."""
    import winreg
    out: list[MicUse] = []
    try:
        root = winreg.OpenKey(winreg.HKEY_CURRENT_USER, MIC_KEY)
    except OSError:
        return out
    with root:
        for name in _subkeys(root):
            if name == NON_PACKAGED:
                try:
                    np_key = winreg.OpenKey(root, name)
                except OSError:
                    continue
                with np_key:
                    for sub in _subkeys(np_key):
                        if want is not None and not want(exe_of(sub.replace("#", "\\"))):
                            continue
                        u = _read_use(np_key, sub, False)
                        if u is not None:
                            out.append(u)
            else:
                u = _read_use(root, name, True)
                if u is not None:
                    out.append(u)
    return out


# ---------- процессы ----------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.OpenProcess.argtypes = [wt.DWORD, wt.BOOL, wt.DWORD]
_k32.OpenProcess.restype = wt.HANDLE
_k32.CloseHandle.argtypes = [wt.HANDLE]
_k32.CloseHandle.restype = wt.BOOL
_k32.GetProcessTimes.argtypes = [wt.HANDLE, ctypes.POINTER(wt.FILETIME),
                                 ctypes.POINTER(wt.FILETIME), ctypes.POINTER(wt.FILETIME),
                                 ctypes.POINTER(wt.FILETIME)]
_k32.GetProcessTimes.restype = wt.BOOL
_HAS_PFN = hasattr(_k32, "GetPackageFamilyName")
if _HAS_PFN:
    _k32.GetPackageFamilyName.argtypes = [wt.HANDLE, ctypes.POINTER(wt.UINT), wt.LPWSTR]
    _k32.GetPackageFamilyName.restype = ctypes.c_long
_QUERY_LIMITED = 0x1000
_APPMODEL_ERROR_NO_PACKAGE = 15700


def process_created(pid: int) -> int | None:
    """Время запуска процесса (FILETIME); None — процесса нет или к нему не пускают."""
    h = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        t = [wt.FILETIME() for _ in range(4)]
        if not _k32.GetProcessTimes(h, *(ctypes.byref(x) for x in t)):
            return None
        return (int(t[0].dwHighDateTime) << 32) | int(t[0].dwLowDateTime)
    finally:
        _k32.CloseHandle(h)


def package_family(pid: int) -> str | None:
    """PackageFamilyName процесса (приложение Store); "" — обычный процесс; None — не узнать."""
    if not _HAS_PFN:
        return None
    h = _k32.OpenProcess(_QUERY_LIMITED, False, pid)
    if not h:
        return None
    try:
        n = wt.UINT(256)
        buf = ctypes.create_unicode_buffer(256)
        r = int(_k32.GetPackageFamilyName(h, ctypes.byref(n), buf))
        if r == 0:
            return buf.value
        return "" if r == _APPMODEL_ERROR_NO_PACKAGE else None
    finally:
        _k32.CloseHandle(h)


def audio_sessions() -> list[tuple[int, bool]]:
    """Аудиосессии программ (pid, играет ли сейчас) — Core Audio в потоке источников звука
    (``loopback``); громкость не читается и не меняется."""
    from . import duck, loopback

    def run() -> list[tuple[int, bool]]:
        out: list[tuple[int, bool]] = []
        for s in duck.default_backend().sessions():   # тесты: NullBackend (tests/conftest.py)
            try:
                if not s.system and s.pid:
                    out.append((int(s.pid), bool(s.active)))
            except Exception:
                continue
        return out

    found: list[tuple[int, bool]] = loopback._com_call(run, loopback.LIST_TIMEOUT_S)
    return found


def _children(table: ProcessTable) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for pid, (ppid, _exe) in table.items():
        if ppid != pid:
            out.setdefault(ppid, []).append(pid)
    return out


def _tree(roots: Iterable[int], children: Mapping[int, list[int]]) -> set[int]:
    """Корни и все их потомки."""
    out = set(roots)
    stack = list(out)
    while stack:
        for c in children.get(stack.pop(), ()):
            if c not in out:
                out.add(c)
                stack.append(c)
    return out


@dataclass(frozen=True)
class MicApp:
    """Запущенная программа, которая держит микрофон: главный процесс и всё её дерево."""
    use: MicUse
    exe: str                     # имя exe главного процесса («brave.exe»)
    path: str                    # его путь ("" — неизвестен)
    root: int                    # главный процесс (корень дерева)
    roots: frozenset[int]        # корни всех процессов с этим путём (пакетом)
    tree: frozenset[int]         # корни и все потомки


def map_use(u: MicUse, table: ProcessTable, *, path_of: Callable[[int], str],
            created: Callable[[int], int | None],
            package_of: Callable[[int], str | None],
            own_pids: Collection[int] = ()) -> tuple[str, MicApp | None]:
    """Запись микрофона → запущенная программа. Вердикт: ``ok`` (и MicApp), ``gone`` (такой
    программы нет — запись зависла), ``stale`` (все её процессы моложе записи), ``own``."""
    from .loopback import root_pid
    if u.packaged:
        key = u.key.casefold()
        pids = [p for p in table if p > 4 and (package_of(p) or "").casefold() == key]
    else:
        # сверка в виде ключа (путь с «#» вместо «\»): обратно ключ в путь не перевести —
        # «#» бывает и в самом пути (папка пользователя «Dev#1»)
        name, want = u.exe, u.key.casefold()
        pids = []
        for p, (_pp, exe) in table.items():
            if exe.lower() == name:
                full = path_of(p)
                if not full or full.replace(ntpath.sep, "#").casefold() == want:
                    pids.append(p)                        # путь не узнать — по имени
    if not pids:
        return "gone", None
    own = frozenset(own_pids)
    if own.intersection(pids) or any(table[p][1].lower() in OWN_EXES for p in pids):
        return "own", None
    times = [t for t in (created(p) for p in pids) if t]
    if times and min(times) > u.start + int(STALE_SLACK_S * _TICKS):
        return "stale", None
    paths: dict[int, str] = {}

    def path(p: int) -> str:
        if p not in paths:
            paths[p] = path_of(p)
        return paths[p]

    roots = sorted({root_pid(p, table, path) for p in pids})
    children = _children(table)
    helper = None if u.packaged else MIC_HELPERS.get(u.exe)
    if helper is not None:
        app_roots = _helper_app_roots(pids, table, helper, path)
        if app_roots:
            # программа помощника: её дерево; помощник вне его (запущен не ею, а из той же
            # папки установки) — и его дерево. Корни — оба: звук может играть любой
            app_tree = _tree(app_roots, children)
            extra = [r for r in roots if r not in app_tree] if helper else []
            all_roots = sorted(set(app_roots) | set(roots) if helper
                               else set(app_roots) | {r for r in roots if r in app_tree})
            main = app_roots[0]
            main_exe = exe_of(table.get(main, (0, ""))[1])
            ident = (main_exe if main_exe in KNOWN_APPS and main_exe not in MIC_HELPERS
                     else u.exe)
            return "ok", MicApp(u, ident, path(main), main, frozenset(all_roots),
                                frozenset(app_tree | _tree(extra, children)))
    known = [r for r in roots if exe_of(table.get(r, (0, ""))[1]) in KNOWN_APPS]
    main = (known or roots)[0]
    tree = _tree(roots, children)
    return "ok", MicApp(u, exe_of(table.get(main, (0, ""))[1]), path(main), main,
                        frozenset(roots), frozenset(tree))


def _helper_app_roots(pids: Sequence[int], table: ProcessTable, main_exe: str,
                      path: Callable[[int], str]) -> list[int]:
    """Корни программы, ради которой микрофон держит помощник (MIC_HELPERS): её процесс среди
    предков помощника (main_exe; "" — любая программа из таблиц, кроме помощников), иначе
    (main_exe задан) — её процесс из той же папки установки или папки выше (max.exe и
    max-service/max-service.exe). Не нашлась — []."""
    from .loopback import root_pid
    found: set[int] = set()
    for p in pids:
        cur, seen = p, {p}
        for _ in range(HELPER_DEPTH):
            pp = table.get(cur, (0, ""))[0]
            if not pp or pp in seen or pp not in table:
                break
            exe = exe_of(table[pp][1])
            if (exe == main_exe) if main_exe else (exe in KNOWN_APPS
                                                   and exe not in MIC_HELPERS):
                found.add(root_pid(pp, table, path))
                break
            seen.add(pp)
            cur = pp
    if found or not main_exe:
        return sorted(found)
    dirs = {ntpath.dirname(f).casefold() for f in (path(p) for p in pids) if f}
    for p, (_pp, exe) in table.items():
        if exe.lower() != main_exe:
            continue
        d = ntpath.dirname(path(p)).casefold()
        if d and any(h == d or h.startswith(ntpath.join(d, "")) for h in dirs):
            found.add(root_pid(p, table, path))
    return sorted(found)


# ---------- что подходит и как назвать ----------

def ignored(s: config.Settings) -> frozenset[str]:
    return frozenset(x.lower() for x in s.meeting_ignore_apps)


def eligible(exe: str, s: config.Settings) -> bool:
    """Эту программу можно предлагать: браузер или (настройка) настольная программа встреч;
    не своя и не в meeting_ignore_apps."""
    exe = exe.lower()
    if not exe or exe in OWN_EXES or exe in ignored(s):
        return False
    return exe in BROWSERS or (bool(s.meeting_detect_desktop_apps) and exe in DESKTOP_APPS)


def wanted(exe: str, s: config.Settings) -> bool:
    """Запись журнала микрофона этого exe стоит читать: программа подходит (eligible) или это
    помощник (MIC_HELPERS), чья программа может подойти, — её узнает map_use."""
    exe = exe.lower()
    if eligible(exe, s):
        return True
    if exe not in MIC_HELPERS or exe in OWN_EXES or exe in ignored(s):
        return False
    main = MIC_HELPERS[exe]
    return not main or eligible(main, s)


def service_of(title: str) -> str:
    """Служба встреч по заголовку окна («Meet – abc - Brave» → «Google Meet»); "" — не узнать."""
    for rx, name in SERVICES:
        if rx.search(title or ""):
            return name
    return ""


def has_word(text: str, pat: str) -> bool:
    """pat встречается в text отдельным словом: у краёв pat из букв и цифр соседи — не буквы
    и не цифры («Teams» есть в «Chat | Teams», но не в «Steamship»)."""
    if not pat:
        return False
    i = text.find(pat)
    while i >= 0:
        j = i + len(pat)
        if ((not pat[0].isalnum() or i == 0 or not text[i - 1].isalnum())
                and (not pat[-1].isalnum() or j == len(text) or not text[j].isalnum())):
            return True
        i = text.find(pat, i + 1)
    return False


def meeting_title(titles: Iterable[str], patterns: Sequence[str]) -> str:
    """Название встречи по заголовкам окон (первый узнанный): служба или совпавший словом
    шаблон meeting_title_patterns («Контур.Толк»); "" — ни один заголовок не похож на
    встречу."""
    pats = [(p, config.fold_title(p.strip())) for p in patterns if p and p.strip()]
    for t in titles:
        svc = service_of(t)
        if svc:
            return svc
        ft = config.fold_title(t or "")
        for p, fp in pats:
            if has_word(ft, fp):
                return service_of(p) or p.strip(" -\u2013\u2014|:")
    return ""


def app_label(exe: str, path: str, describe: Callable[[str], str]) -> str:
    """Имя программы для предложения: описание exe («Brave Browser»), иначе по таблице,
    иначе имя exe без .exe. Помощник из таблицы (max-service.exe) — по таблице: описание его
    exe — не имя программы."""
    if exe in MIC_HELPERS and exe in DESKTOP_APPS:
        return DESKTOP_APPS[exe]
    desc = ""
    if path:
        try:
            desc = describe(path)
        except OSError:
            desc = ""
    if desc:
        return desc
    known = BROWSERS.get(exe) or DESKTOP_APPS.get(exe)
    if known:
        return known
    stem = exe[:-4] if exe.endswith(".exe") else exe
    return stem[:1].upper() + stem[1:] if stem else exe


# ---------- предложение и сеанс ----------

@dataclass
class Suggestion:
    """Предложение начать расшифровку (событие meeting_suggest)."""
    id: str
    ident: tuple[str, int]       # (программа, начало занятия микрофона)
    exe: str                     # для «Не предлагать для …» (meeting_ignore_apps)
    app: str
    title: str
    source: dict[str, Any]       # AudioSource — meeting_start {source_id: source.id}
    detected_at: str
    created: float               # monotonic

    def payload(self) -> dict[str, Any]:
        return {"id": self.id, "source": dict(self.source), "app": self.app,
                "title": self.title, "detected_at": self.detected_at}


@dataclass
class _Watch:
    """Идущий сеанс: запись микрофона его программы — для подсказки «закончилась»."""
    started_at: str
    source: dict[str, Any]
    keys: frozenset[str]          # подключи журнала микрофона (casefold)
    exe: str                      # имя exe для отбора подключей NonPackaged
    exes: frozenset[str] = frozenset()    # …и имена exe самих подключей (помощник: max-service)
    mic_seen: bool = False
    mic_off_since: float | None = None
    next_check: float = 0.0
    next_audio: float = 0.0
    hint: str | None = None


@dataclass(frozen=True)
class SessionMic:
    """Микрофон программы идущего сеанса (для автоостановки): tracked — запись микрофона
    есть и в сеансе он был занят; free_s — сколько он свободен (None — занят или не следим);
    in_use — занят сейчас."""
    tracked: bool = False
    free_s: float | None = None
    in_use: bool = False


@dataclass
class _Label:
    """Источник звука (по корню) → программа, которая держит микрофон."""
    app: str                      # её имя
    exe: str                      # её exe
    src_exe: str                  # exe самого источника (pid мог достаться другой программе)
    keys: tuple[str, ...]         # подключи журнала микрофона
    at: float


@dataclass
class _State:
    done: dict[tuple[str, int], float] = field(default_factory=dict)
    first_seen: dict[tuple[str, int], float] = field(default_factory=dict)
    next_check: dict[tuple[str, int], float] = field(default_factory=dict)
    pending: collections.OrderedDict[str, Suggestion] = field(
        default_factory=collections.OrderedDict)
    labels: dict[int, _Label] = field(default_factory=dict)      # корень источника → программа
    hints: dict[int, collections.deque[tuple[str, float]]] = field(default_factory=dict)
    pfn: dict[int, tuple[str, str | None]] = field(default_factory=dict)  # pid → (exe, пакет)
    # браузер без заголовка встречи: с каких пор микрофон занят без звука (до первого звука) и
    # был ли первый звук «поздним» (после QUIET_MAX_S тишины); «Не сейчас» — до каких пор
    quiet_since: dict[tuple[str, int], float] = field(default_factory=dict)
    heard: dict[tuple[str, int], bool] = field(default_factory=dict)
    snoozed: dict[str, float] = field(default_factory=dict)          # подключ → monotonic
    watch: _Watch | None = None
    in_session: bool = False


class MeetingDetector:
    """Обнаружение встречи по микрофону. Все обращения к системе подменяемы (тесты:
    фейковый реестр, таблица процессов и источники звука).

    ``poll(s)`` — раз в POLL_S, пока встреча не идёт: новое предложение или None.
    ``session_tick(s, source, started_at)`` — пока сеанс идёт: подсказка ``"ended"`` или None.
    ``auto_sources(s, sources)`` — источники программ, которые сейчас держат микрофон (выбор
    источника «auto»). ``take(id)`` — предложение по id (ответ пользователя)."""

    def __init__(self, *, sources: SourcesFn, own_pids: Collection[int] = (),
                 sessions: SessionsFn | None = None, reader: Reader | None = None,
                 table: Callable[[], ProcessTable] | None = None,
                 path_of: Callable[[int], str] | None = None,
                 created: Callable[[int], int | None] | None = None,
                 package_of: Callable[[int], str | None] | None = None,
                 titles_of: Callable[[Collection[int]], Mapping[int, list[str]]] | None = None,
                 describe: Callable[[str], str] | None = None,
                 clock: Callable[[], float] = time.monotonic,
                 now_filetime: Callable[[], int] = filetime_now) -> None:
        # по умолчанию — настоящие функции, найденные в момент вызова (тесты подменяют их
        # в модулях: tests/conftest.py)
        self._sources = sources
        self._own = frozenset(own_pids)
        self._sessions = sessions
        self._reader: Reader = reader or (lambda want: read_mic_usage(want))
        self._table = table or (lambda: winutil.process_table())
        self._path_of = path_of or (lambda pid: winutil.process_path(pid))
        self._created = created or process_created
        self._package_of = package_of or package_family
        self._titles_of = titles_of or (lambda pids: winutil.window_titles(pids))
        self._describe = describe or (lambda path: winutil.file_description(path))
        self._clock = clock
        self._now_ft = now_filetime
        self._lock = threading.RLock()
        self._st = _State()

    # ---------- опрос, пока встреча не идёт ----------

    def poll(self, s: config.Settings) -> Suggestion | None:
        """Один опрос: новое предложение (уже запомнено как ждущее ответа) или None."""
        if not s.meeting_autosuggest:
            return None
        now = self._clock()
        with self._lock:
            resume, self._st.in_session = self._st.in_session, False
            self._st.watch = None
        uses = [u for u in self._reader(lambda exe: wanted(exe, s)) if u.in_use]
        live = {u.ident for u in uses}
        with self._lock:
            st = self._st
            for d in (st.done, st.first_seen, st.next_check, st.quiet_since, st.heard):
                for k in [k for k in d if k not in live]:
                    del d[k]
            for app_key in [x for x, until in st.snoozed.items() if until <= now]:
                del st.snoozed[app_key]
            if resume:
                # сеанс только что кончился: разговоры, которые шли во время него, не предлагать
                for u in uses:
                    st.done[u.ident] = now
                return None
            ft = self._now_ft()
            for u in uses:
                if u.ident not in st.done and ft - u.start > int(MIC_MAX_AGE_S * _TICKS):
                    log.debug("Микрофон %s занят с %s — не разговор", u.exe or u.key,
                              _ft_text(u.start))
                    self._mark_done(u.ident, now)
            due = [u for u in uses if u.ident not in st.done
                   and ft - u.start >= int(MIC_MIN_S * _TICKS)
                   and now >= st.next_check.get(u.ident, 0.0)]
        if not due:
            return None
        table = self._table()
        sessions: list[tuple[int, bool]] | None = None
        for u in sorted(due, key=lambda x: -x.start):
            verdict, app = self._map(u, table)
            if app is None or not eligible(app.exe, s):
                log.debug("Микрофон %s: не встреча (%s)", u.exe or u.key,
                          verdict if app is None else app.exe)
                self._mark_done(u.ident, now)
                continue
            if sessions is None:
                sessions = self._audio()
            playing = any(active and pid in app.tree for pid, active in sessions)
            src = self._source_for(app) if playing else None
            if src is None:
                if not playing:
                    self._quiet(u.ident, now)
                self._retry_later(u.ident, now)
                continue
            late = self._heard(u.ident, now)
            title = meeting_title(self._titles(app), s.meeting_title_patterns)
            if not title and app.exe in BROWSERS:
                why = self._unnamed_wait(u, ft, late, now)
                if why:
                    log.debug("Микрофон %s: браузер без заголовка встречи — пока не предлагаю "
                              "(%s)", app.exe, why)
                    self._retry_later(u.ident, now)
                    continue
            sugg = self._suggest(app, src, s, now, title)
            log.info("Встреча замечена: %s (микрофон с %s) — предлагаю расшифровку",
                     app.exe, _ft_text(u.start))
            return sugg
        return None

    def _map(self, u: MicUse, table: ProcessTable) -> tuple[str, MicApp | None]:
        return map_use(u, table, path_of=self._path_of, created=self._created,
                       package_of=self._pfn_cached(table), own_pids=self._own)

    def _pfn_cached(self, table: ProcessTable) -> Callable[[int], str | None]:
        """Пакет процесса — с кэшем по (pid, exe): процессы перебираются раз, не каждый опрос."""
        def get(pid: int) -> str | None:
            exe = table.get(pid, (0, ""))[1]
            with self._lock:
                hit = self._st.pfn.get(pid)
            if hit is not None and hit[0] == exe:
                return hit[1]
            v = self._package_of(pid)
            with self._lock:
                if len(self._st.pfn) > 4096:
                    self._st.pfn.clear()
                self._st.pfn[pid] = (exe, v)
            return v
        return get

    def _audio(self) -> list[tuple[int, bool]]:
        if self._sessions is not None:
            return list(self._sessions())
        out: list[tuple[int, bool]] = []
        for x in self._sources():
            pid = x.get("pid")
            if x.get("kind") == "process" and isinstance(pid, int):
                out.append((pid, bool(x.get("active"))))
        return out

    def _mark_done(self, ident: tuple[str, int], now: float) -> None:
        with self._lock:
            done = self._st.done
            done[ident] = now
            while len(done) > DONE_MAX:
                done.pop(next(iter(done)))

    def _quiet(self, ident: tuple[str, int], now: float) -> None:
        """Микрофон занят, звука нет: запомнить, с каких пор (только до первого звука)."""
        with self._lock:
            if ident not in self._st.heard:
                self._st.quiet_since.setdefault(ident, now)

    def _heard(self, ident: tuple[str, int], now: float) -> bool:
        """Звук дерева программы замечен; True — первый звук пришёл после QUIET_MAX_S тишины
        с занятым микрофоном (забытая вкладка, голосовой помощник: заиграла другая вкладка)."""
        with self._lock:
            st = self._st
            if ident not in st.heard:
                q = st.quiet_since.get(ident)
                st.heard[ident] = q is not None and now - q >= QUIET_MAX_S
            return st.heard[ident]

    def _unnamed_wait(self, u: MicUse, ft: int, late: bool, now: float) -> str:
        """Браузер без заголовка встречи: почему пока не предлагать ("" — предлагать)."""
        if ft - u.start < int(BROWSER_UNNAMED_MIN_S * _TICKS):
            return "микрофон занят меньше минуты"
        if late:
            return "звук заиграл после долгой тишины"
        with self._lock:
            if self._st.snoozed.get(u.key.casefold(), 0.0) > now:
                return "«Не сейчас»"
        return ""

    def _retry_later(self, ident: tuple[str, int], now: float) -> None:
        with self._lock:
            first = self._st.first_seen.setdefault(ident, now)
            slow = now - first >= AUDIO_SLOW_AFTER_S
            self._st.next_check[ident] = now + (AUDIO_RETRY_SLOW_S if slow else AUDIO_RETRY_S)

    def _matching(self, app: MicApp, sources: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        """Источники звука дерева программы: сам корень, потом играющие, потом остальные."""
        found = [x for x in sources if x.get("kind") == "process"
                 and isinstance(x.get("pid"), int) and x["pid"] in app.tree]
        found.sort(key=lambda x: (x["pid"] not in app.roots, not x.get("active"),
                                  not x.get("meeting")))
        return found

    def _source_for(self, app: MicApp) -> dict[str, Any] | None:
        found = self._matching(app, self._sources())
        return dict(found[0]) if found else None

    def _remember_label(self, app: MicApp, src: dict[str, Any], name: str, now: float) -> None:
        pid = src.get("pid")
        if not isinstance(pid, int):
            return
        with self._lock:
            labels = self._st.labels
            labels[pid] = _Label(name, app.exe, exe_of(str(src.get("exe") or "")),
                                 (app.use.key.casefold(),), now)
            for k in [k for k, v in labels.items() if now - v.at > PENDING_TTL_S * 6]:
                del labels[k]

    def _suggest(self, app: MicApp, src: dict[str, Any], s: config.Settings,
                 now: float, title: str | None = None) -> Suggestion:
        name = app_label(app.exe, app.path, self._describe)
        if title is None:
            title = meeting_title(self._titles(app), s.meeting_title_patterns)
        sugg = Suggestion(secrets.token_hex(6), app.use.ident, app.exe, name, title, src,
                          datetime.now().astimezone().isoformat(timespec="milliseconds"), now)
        self._remember_label(app, src, name, now)
        with self._lock:
            self._mark_done(app.use.ident, now)
            pend = self._st.pending
            pend[sugg.id] = sugg
            while len(pend) > PENDING_MAX:
                pend.popitem(last=False)
        return sugg

    def _titles(self, app: MicApp) -> list[str]:
        """Заголовки окон дерева (главный процесс первым, окна — сверху вниз), затем
        замеченные при смене активного окна (свежие первыми)."""
        pids = app.tree
        out: list[str] = []
        try:
            found = self._titles_of(pids)
        except Exception:
            log.debug("Заголовки окон не прочитаны", exc_info=True)
            found = {}
        for pid in sorted(pids, key=lambda p: (p != app.root, p not in app.roots, p)):
            out += found.get(pid, [])
        now = self._clock()
        with self._lock:
            seen = [(t, at) for pid in pids for t, at in self._st.hints.get(pid, ())
                    if now - at <= HINT_TTL_S]
        out += [t for t, _at in sorted(seen, key=lambda x: -x[1])]
        return out

    # ---------- подсказки и ответы ----------

    def title_hint(self, pid: int, title: str, patterns: Sequence[str]) -> None:
        """Активное окно сменилось: заголовок, похожий на встречу, запомнить для названия
        (вкладка встречи могла быть активной раньше, чем микрофон занят 10 с)."""
        if not title or not pid:
            return
        if not service_of(title) and not meeting_title([title], patterns):
            return
        now = self._clock()
        with self._lock:
            hints = self._st.hints
            q = hints.setdefault(pid, collections.deque(maxlen=HINTS_MAX))
            q.append((title, now))
            for p in [p for p, d in hints.items() if not d or now - d[-1][1] > HINT_TTL_S]:
                del hints[p]

    def session_over(self) -> None:
        """Сеанс кончился (и короткий — между двумя опросами): разговоры, которые идут
        сейчас, после него не предлагать."""
        with self._lock:
            self._st.in_session = True

    def snooze(self, sugg: Suggestion) -> None:
        """«Не сейчас» на предложение браузера без заголовка встречи (голосовое сообщение или
        диктовка в вкладке, пока в другой играет музыка): такие предложения этой программы —
        не раньше чем через LATER_SNOOZE_S. Встречи с узнанным заголовком — как обычно."""
        if sugg.title or sugg.exe not in BROWSERS:
            return
        with self._lock:
            self._st.snoozed[sugg.ident[0]] = self._clock() + LATER_SNOOZE_S

    def take(self, sid: str) -> Suggestion | None:
        """Предложение по id (ответ «Начать», «Не сейчас», «Не предлагать»); None — нет или
        устарело. Отвечают на него один раз."""
        now = self._clock()
        with self._lock:
            pend = self._st.pending
            for k in [k for k, v in pend.items() if now - v.created > PENDING_TTL_S]:
                del pend[k]
            return pend.pop(sid, None)

    def pending(self) -> list[Suggestion]:
        with self._lock:
            return list(self._st.pending.values())

    # ---------- выбор источника «auto» ----------

    def mic_apps(self, s: config.Settings) -> list[MicApp]:
        """Программы, которые держат микрофон прямо сейчас (подходящие, не свои, не из
        meeting_ignore_apps), свежие первыми. Без порога в 10 с: выбор источника по просьбе."""
        uses = [u for u in self._reader(lambda exe: wanted(exe, s)) if u.in_use]
        if not uses:
            return []
        table = self._table()
        out: list[MicApp] = []
        for u in sorted(uses, key=lambda x: -x.start):
            _verdict, app = self._map(u, table)
            if app is not None and eligible(app.exe, s):
                out.append(app)
        return out

    def auto_sources(self, s: config.Settings, sources: Sequence[dict[str, Any]],
                     active_only: bool = True) -> list[dict[str, Any]]:
        """Играющие (``active``) источники из sources, чьё дерево держит микрофон, — раньше
        просто играющих (браузер с YouTube не перебьёт браузер со встречей: во время звонка
        поток вывода открыт, даже когда все молчат). Не играющий источник программы с
        микрофоном (забытая вкладка, голосовой помощник) — не встреча: только с
        ``active_only=False`` (запасной выбор движка)."""
        try:
            apps = self.mic_apps(s)
        except Exception:
            log.debug("Кто держит микрофон — не узнать", exc_info=True)
            return []
        out: list[dict[str, Any]] = []
        seen: set[str] = set()
        now = self._clock()
        for app in apps:
            for src in self._matching(app, sources):
                if active_only and not src.get("active"):
                    continue
                if src["id"] not in seen:
                    seen.add(src["id"])
                    out.append(src)
                    self._remember_label(app, src, app_label(app.exe, app.path,
                                                             self._describe), now)
        return out

    def decorate(self, src: dict[str, Any]) -> dict[str, Any]:
        """Источник для сеанса: звук играет вспомогательный процесс (WebView2 нового Teams,
        max-service.exe MAX) — имя программы, которая держит микрофон, а не «Microsoft Edge
        WebView2»."""
        out = dict(src)
        pid = out.get("pid")
        exe = exe_of(str(out.get("exe") or ""))
        with self._lock:
            lab = self._st.labels.get(pid) if isinstance(pid, int) else None
        if (lab is not None and lab.src_exe == exe and (exe != lab.exe or exe in MIC_HELPERS)
                and lab.app):
            out["name"] = lab.app
        return out

    # ---------- идущий сеанс: «встреча, похоже, закончилась» ----------

    def session_tick(self, s: config.Settings, source: dict[str, Any],
                     started_at: str) -> str | None:
        """Пока идёт сеанс (раз в POLL_S; сама проверка — раз в SESSION_CHECK_S): микрофон
        программы сеанса свободен ≥ END_MIC_OFF_S, а звук источника не играет —
        ``"ended"``; иначе None. Микрофон в сеансе ни разу не был занят — подсказки нет."""
        now = self._clock()
        with self._lock:
            self._st.in_session = True
            w = self._st.watch
            if w is None or w.started_at != started_at:
                w = self._st.watch = self._new_watch(source, started_at)
            if now < w.next_check:
                return w.hint
            w.next_check = now + SESSION_CHECK_S
            keys, exe = w.keys, w.exe
        if not keys:
            return None
        exes = w.exes | {exe}
        uses = [u for u in self._reader(lambda e: e in exes) if u.key.casefold() in keys]
        in_use = any(u.in_use for u in uses)
        with self._lock:
            if in_use:
                w.mic_seen, w.mic_off_since, w.hint = True, None, None
                return None
            if not w.mic_seen:
                return None
            if w.mic_off_since is None:
                w.mic_off_since = now
            if now - w.mic_off_since < END_MIC_OFF_S or now < w.next_audio:
                return w.hint
            w.next_audio = now + SESSION_CHECK_S
        playing = self._source_playing(source)
        with self._lock:
            hint = None if playing else HINT_ENDED
            if hint != w.hint:
                log.info("Встреча: микрофон свободен %.0f с, звука нет — %s", now -
                         (w.mic_off_since or now), "похоже, закончилась" if hint else "звук есть")
            w.hint = hint
            return hint

    def session_mic(self, started_at: str) -> SessionMic:
        """Микрофон программы сеанса started_at по последней проверке session_tick (её зовёт
        движок раньше): ещё не проверяли, записи нет или микрофон в сеансе не был занят —
        SessionMic() (не следим)."""
        now = self._clock()
        with self._lock:
            w = self._st.watch
            if w is None or w.started_at != started_at or not w.keys or not w.mic_seen:
                return SessionMic()
            if w.mic_off_since is None:
                return SessionMic(tracked=True, in_use=True)
            return SessionMic(tracked=True, free_s=max(0.0, now - w.mic_off_since))

    def _new_watch(self, source: dict[str, Any], started_at: str) -> _Watch:
        """Какую запись микрофона смотреть: программы предложения/выбора «auto», иначе — по
        пути exe и пакету корня источника."""
        pid = source.get("pid") if source.get("kind") == "process" else None
        keys: set[str] = set()
        exe = ""
        if isinstance(pid, int) and pid > 0:
            lab = self._st.labels.get(pid)
            if lab is not None and lab.src_exe == exe_of(str(source.get("exe") or "")):
                keys.update(lab.keys)
                exe = lab.exe
            else:
                try:
                    path = self._path_of(pid)
                except OSError:
                    path = ""
                if path:
                    keys.add(path.replace("\\", "#").casefold())
                    exe = exe_of(path)
                pfn = self._package_of(pid)
                if pfn:
                    keys.add(pfn.casefold())
        exes = frozenset(MicUse(k, False, 0, 0).exe for k in keys) - {""}
        return _Watch(started_at, dict(source), frozenset(keys), exe, exes)

    def _source_playing(self, source: dict[str, Any]) -> bool:
        try:
            found = self._sources()
        except Exception:
            log.debug("Источники звука не перечислены", exc_info=True)
            return True                       # не узнать — не подсказывать
        return any(x.get("id") == source.get("id") and bool(x.get("active")) for x in found)


def _ft_text(ft: int) -> str:
    try:
        return datetime.fromtimestamp(ft / _TICKS - _FILETIME_EPOCH_S).strftime("%H:%M:%S")
    except (OverflowError, OSError, ValueError):
        return "?"
