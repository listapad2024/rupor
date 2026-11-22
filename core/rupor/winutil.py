"""Окна: активное окно, класс/процесс, детект RDP, перебор RDP-окон, фокус, права администратора."""
from __future__ import annotations

import ctypes
import functools
import logging
import ntpath
import os
import re
import sys
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from typing import Callable, Collection

from . import _win as w
from .i18n import tr

log = logging.getLogger(__name__)

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20
SW_RESTORE = 9
GW_OWNER = 4
HWND_TOP = 0
SWP_NOSIZE, SWP_NOMOVE, SWP_ASYNCWINDOWPOS = 0x0001, 0x0002, 0x4000
DWMWA_CLOAKED = 14


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    cls: str
    title: str
    pid: int
    process: str  # имя exe, напр. "mstsc.exe"


def _class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    w.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _title(hwnd: int) -> str:
    n = w.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 1)
    w.user32.GetWindowTextW(hwnd, buf, n + 1)
    return buf.value


def _pid(hwnd: int) -> int:
    pid = wt.DWORD()
    w.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return pid.value


def process_path(pid: int) -> str:
    h = w.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return ""
    try:
        size = wt.DWORD(1024)
        buf = ctypes.create_unicode_buffer(size.value)
        if w.kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return buf.value
        return ""
    finally:
        w.kernel32.CloseHandle(h)


def window_info(hwnd: int) -> WindowInfo:
    pid = _pid(hwnd)
    return WindowInfo(hwnd, _class_name(hwnd), _title(hwnd), pid,
                      os.path.basename(process_path(pid)))


# ---------- процессы: дерево, описание exe, заголовки окон (захват звука встречи) ----------

TH32CS_SNAPPROCESS = 0x00000002
_INVALID_HANDLE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wt.DWORD), ("cntUsage", wt.DWORD), ("th32ProcessID", wt.DWORD),
        ("th32DefaultHeapID", ctypes.c_void_p), ("th32ModuleID", wt.DWORD),
        ("cntThreads", wt.DWORD), ("th32ParentProcessID", wt.DWORD),
        ("pcPriClassBase", ctypes.c_long), ("dwFlags", wt.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


w.kernel32.CreateToolhelp32Snapshot.argtypes = [wt.DWORD, wt.DWORD]
w.kernel32.CreateToolhelp32Snapshot.restype = wt.HANDLE
w.kernel32.Process32FirstW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
w.kernel32.Process32FirstW.restype = wt.BOOL
w.kernel32.Process32NextW.argtypes = [wt.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
w.kernel32.Process32NextW.restype = wt.BOOL


def process_table() -> dict[int, tuple[int, str]]:
    """pid → (pid родителя, имя exe) для всех процессов — снимок Toolhelp32 (прав не нужно;
    pid родителя может оказаться уже чужим: Windows переиспользует pid завершившихся)."""
    snap = w.kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if not snap or snap == _INVALID_HANDLE:
        return {}
    out: dict[int, tuple[int, str]] = {}
    e = PROCESSENTRY32W()
    e.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    try:
        ok = w.kernel32.Process32FirstW(snap, ctypes.byref(e))
        while ok:
            out[int(e.th32ProcessID)] = (int(e.th32ParentProcessID), e.szExeFile)
            ok = w.kernel32.Process32NextW(snap, ctypes.byref(e))
    finally:
        w.kernel32.CloseHandle(snap)
    return out


def parent_map() -> dict[int, int]:
    """pid → pid родителя (снимок Toolhelp32)."""
    return {pid: ppid for pid, (ppid, _exe) in process_table().items()}


class _PROCESS_BASIC_INFORMATION(ctypes.Structure):
    _fields_ = [
        ("ExitStatus", ctypes.c_void_p), ("PebBaseAddress", ctypes.c_void_p),
        ("AffinityMask", ctypes.c_size_t), ("BasePriority", ctypes.c_void_p),
        ("UniqueProcessId", ctypes.c_size_t), ("InheritedFromUniqueProcessId", ctypes.c_size_t),
    ]


_ntdll: ctypes.WinDLL | None = None


def parent_pid(pid: int) -> int | None:
    """pid родителя одного процесса (NtQueryInformationProcess — микросекунды, а снимок
    Toolhelp32 — десятки мс); None — процесса нет или к нему не пускают."""
    global _ntdll
    if _ntdll is None:
        nt = ctypes.WinDLL("ntdll")
        nt.NtQueryInformationProcess.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p,
                                                 wt.ULONG, ctypes.POINTER(wt.ULONG)]
        nt.NtQueryInformationProcess.restype = ctypes.c_long
        _ntdll = nt
    h = w.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return None
    try:
        pbi = _PROCESS_BASIC_INFORMATION()
        st = _ntdll.NtQueryInformationProcess(h, 0, ctypes.byref(pbi), ctypes.sizeof(pbi), None)
        return int(pbi.InheritedFromUniqueProcessId) if st == 0 else None
    finally:
        w.kernel32.CloseHandle(h)


_version: ctypes.WinDLL | None = None


def _version_dll() -> ctypes.WinDLL:
    global _version
    if _version is None:
        dll = ctypes.WinDLL("version", use_last_error=True)
        dll.GetFileVersionInfoSizeW.argtypes = [wt.LPCWSTR, ctypes.POINTER(wt.DWORD)]
        dll.GetFileVersionInfoSizeW.restype = wt.DWORD
        dll.GetFileVersionInfoW.argtypes = [wt.LPCWSTR, wt.DWORD, wt.DWORD, ctypes.c_void_p]
        dll.GetFileVersionInfoW.restype = wt.BOOL
        dll.VerQueryValueW.argtypes = [ctypes.c_void_p, wt.LPCWSTR,
                                       ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wt.UINT)]
        dll.VerQueryValueW.restype = wt.BOOL
        _version = dll
    return _version


@functools.lru_cache(maxsize=256)
def file_description(path: str) -> str:
    """Описание файла из ресурса версии exe (FileDescription: «Brave Browser», «Google
    Chrome», «Telegram Desktop»); "" — нет ресурса или описания."""
    if not path:
        return ""
    try:
        dll = _version_dll()
        size = int(dll.GetFileVersionInfoSizeW(path, None))
        if not size:
            return ""
        data = ctypes.create_string_buffer(size)
        if not dll.GetFileVersionInfoW(path, 0, size, data):
            return ""
        ptr = ctypes.c_void_p()
        n = wt.UINT()
        langs: list[str] = []
        if dll.VerQueryValueW(data, "\\VarFileInfo\\Translation", ctypes.byref(ptr),
                              ctypes.byref(n)) and ptr.value and n.value >= 4:
            pairs = (wt.WORD * (n.value // 2)).from_address(ptr.value)
            langs = [f"{pairs[i]:04x}{pairs[i + 1]:04x}" for i in range(0, len(pairs) - 1, 2)]
        for lang in [*langs, "040904b0", "040904e4", "000004b0"]:
            key = f"\\StringFileInfo\\{lang}\\FileDescription"
            if dll.VerQueryValueW(data, key, ctypes.byref(ptr), ctypes.byref(n)) \
                    and ptr.value and n.value > 1:
                text = ctypes.wstring_at(ptr.value, n.value).split("\0", 1)[0].strip()
                if text:
                    return text
    except (OSError, ValueError) as e:
        log.debug("Описание файла не прочитано (%s)", type(e).__name__)
    return ""


def window_titles(pids: Collection[int]) -> dict[int, list[str]]:
    """Заголовки видимых окон верхнего уровня (без владельца, с текстом) процессов pids — по
    pid, в z-порядке (сверху вниз). Свёрнутые окна и окна на других рабочих столах — тоже."""
    want = frozenset(pids)
    found: dict[int, list[str]] = {}
    if not want:
        return found

    def _cb(hwnd: int, _lp: int) -> bool:
        if not w.user32.IsWindowVisible(hwnd) or w.user32.GetWindow(hwnd, GW_OWNER):
            return True
        pid = _pid(hwnd)
        if pid in want:
            title = _title(hwnd).strip()
            if title:
                found.setdefault(pid, []).append(title)
        return True

    w.user32.EnumWindows(w.WNDENUMPROC(_cb), 0)
    return found


def foreground() -> WindowInfo | None:
    hwnd = w.user32.GetForegroundWindow()
    return window_info(hwnd) if hwnd else None


def is_rdp(cls: str, process: str, rdp_classes: list[str], rdp_processes: list[str]) -> bool:
    """Окно — RDP-клиент? По классу окна или по имени процесса (без учёта регистра)."""
    classes = {c.lower() for c in rdp_classes}
    procs = {p.lower() for p in rdp_processes}
    return cls.lower() in classes or process.lower() in procs


MSTSC_CLASS = "TscShellContainerClass"
MSTSC_PROCESS = "mstsc.exe"
# Хвост заголовка mstsc на разных языках Windows: «srv - Подключение к удалённому рабочему
# столу», «srv - Remote Desktop Connection» (перед ним ещё бывает имя .rdp-файла)
_MSTSC_SEPARATORS = (" - ", " – ", " — ")
# Менеджеры подключений с вкладками: одно окно на все сессии, а заголовок меняется при
# выборе другого подключения (mRemoteNG: «mRemoteNG - confCons.xml - srv-01»). Ключ — имя
# программы (у mRemoteNG — и файл подключений), без хвоста с подключением: иначе галочка
# «терялась» бы при переключении вкладки. Текст всё равно получает только активная вкладка.
_TABBED_CLIENTS = {"mremoteng.exe": "mRemoteNG", "rdcman.exe": "RDCMan"}
_CONN_FILE_RE = re.compile(r"(?i)\.xml$")


def rdp_session_key(title: str, process: str = "", cls: str = "") -> str:
    """Ключ RDP-сессии для галочек рассылки. Сравнивать ключи — без учёта регистра.

    - mstsc: имя сервера — часть заголовка до « - » («srv-analytics - Подключение к
      удалённому рабочему столу» → «srv-analytics», «srv-analytics - Remote Desktop
      Connection» → «srv-analytics»);
    - mRemoteNG: «mRemoteNG - <файл подключений>» (заголовок «mRemoteNG - confCons.xml -
      srv-01» → «mRemoteNG - confCons.xml»; без файла — «mRemoteNG»), RDCMan — «RDCMan»:
      не зависит от выбранной вкладки;
    - прочие клиенты (добавленные в rdp_processes/rdp_classes) — заголовок целиком."""
    t = " ".join((title or "").split())
    proc = (process or "").lower()
    if proc == MSTSC_PROCESS or cls == MSTSC_CLASS:
        cut = min((i for i in (t.find(sep) for sep in _MSTSC_SEPARATORS) if i > 0),
                  default=-1)
        if cut > 0:
            return t[:cut].strip()
        return t
    name = _TABBED_CLIENTS.get(proc)
    if name is not None:
        parts = [p.strip() for p in t.split(" - ")]
        if len(parts) >= 2 and _CONN_FILE_RE.search(parts[1]):
            return f"{name} - {ntpath.basename(parts[1])}"
        return name
    return t


def list_rdp_windows(rdp_classes: list[str], rdp_processes: list[str]) -> list[WindowInfo]:
    """Видимые окна верхнего уровня RDP-клиентов в z-порядке."""
    found: list[WindowInfo] = []
    my_pid = os.getpid()

    def _cb(hwnd: int, _lp: int) -> bool:
        if not w.user32.IsWindowVisible(hwnd) or w.user32.GetWindow(hwnd, GW_OWNER):
            return True
        info = window_info(hwnd)
        if info.pid != my_pid and info.title and is_rdp(
                info.cls, info.process, rdp_classes, rdp_processes):
            found.append(info)
        return True

    w.user32.EnumWindows(w.WNDENUMPROC(_cb), 0)
    return found


def is_hung(hwnd: int) -> bool:
    """Окно «не отвечает» (Windows так считает, если оно не берёт сообщения ~5 с)."""
    return bool(hwnd) and bool(w.user32.IsHungAppWindow(hwnd))


def is_cloaked(hwnd: int) -> bool:
    """Окно «спрятано» DWM (UWP в фоне, окно на другом виртуальном рабочем столе)."""
    if w.dwmapi is None or not hwnd:
        return False
    val = ctypes.c_int(0)
    hr = w.dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(val),
                                        ctypes.sizeof(val))
    return hr == 0 and val.value != 0


def is_visible(hwnd: int) -> bool:
    """Окно видно пользователю: не скрыто (свёрнуто в трей) и не спрятано DWM."""
    return is_window(hwnd) and bool(w.user32.IsWindowVisible(hwnd)) and not is_cloaked(hwnd)


def activate(hwnd: int, timeout: float = 1.0) -> bool:
    """Вывести окно на передний план. Windows мешает «воровать» фокус, поэтому
    подцепляемся к потоку ввода текущего переднего окна и делаем фиктивное нажатие.

    Зависшее окно не трогаем (False): ShowWindow/BringWindowToTop для окна чужого потока
    синхронны и повесили бы рабочий поток ядра; поэтому и сами вызовы — асинхронные."""
    if is_hung(hwnd):
        log.warning("Окно %#x не отвечает — не переключаюсь в него", hwnd)
        return False
    if w.user32.IsIconic(hwnd):
        w.user32.ShowWindowAsync(hwnd, SW_RESTORE)
    fg = w.user32.GetForegroundWindow()
    if fg == hwnd:
        return True
    me = w.kernel32.GetCurrentThreadId()
    fg_thread = w.user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_thread and fg_thread != me and not is_hung(fg or 0)
                    and w.user32.AttachThreadInput(me, fg_thread, True))
    try:
        w.user32.SetWindowPos(hwnd, HWND_TOP, 0, 0, 0, 0,
                              SWP_NOMOVE | SWP_NOSIZE | SWP_ASYNCWINDOWPOS)
        if not w.user32.SetForegroundWindow(hwnd):
            # Запрет снимается, если «последний ввод» пришёл от нашего процесса.
            # Шлём отпускание неназначенной клавиши: Alt тут нельзя — в RDP он
            # уйдёт на удалённый компьютер и откроет там меню.
            from .inject import KeyEvent, _send
            _send([KeyEvent(0x07, 0, w.KEYEVENTF_KEYUP)])
            w.user32.SetForegroundWindow(hwnd)
    finally:
        if attached:
            w.user32.AttachThreadInput(me, fg_thread, False)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if w.user32.GetForegroundWindow() == hwnd:
            return True
        time.sleep(0.02)
    return False


def _token_elevated(h_process: int) -> bool | None:
    tok = wt.HANDLE()
    if not w.advapi32.OpenProcessToken(h_process, TOKEN_QUERY, ctypes.byref(tok)):
        return None
    try:
        val = wt.DWORD()
        ret = wt.DWORD()
        if w.advapi32.GetTokenInformation(tok, TokenElevation, ctypes.byref(val),
                                          ctypes.sizeof(val), ctypes.byref(ret)):
            return bool(val.value)
        return None
    finally:
        w.kernel32.CloseHandle(tok)


def self_elevated() -> bool:
    return bool(_token_elevated(w.kernel32.GetCurrentProcess()))


def process_elevated(pid: int) -> bool:
    """True, если процесс запущен от администратора. Если токен не открыть — это тоже
    почти всегда признак elevated-процесса (UIPI не пускает)."""
    h = w.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not h:
        return True
    try:
        r = _token_elevated(h)
        return True if r is None else r
    finally:
        w.kernel32.CloseHandle(h)


def blocked_by_uipi(info: WindowInfo) -> bool:
    return not self_elevated() and process_elevated(info.pid)


def relaunch_as_admin() -> bool:
    """Перезапустить Рупор с правами администратора (появится окно UAC)."""
    exe = sys.executable
    args = " ".join(f'"{a}"' for a in ["-m", "rupor", *sys.argv[1:]])
    r = w.shell32.ShellExecuteW(None, "runas", exe, args, os.getcwd(), 1)
    return (r or 0) > 32


def is_window(hwnd: int) -> bool:
    return bool(hwnd) and bool(w.user32.IsWindow(hwnd))


# Окна оболочки, которые на мгновение становятся активными (Alt+Tab, панель задач, «Пуск»,
# рабочий стол) — это не «куда пользователь печатает».
SHELL_CLASSES = frozenset({
    "Shell_TrayWnd", "Shell_SecondaryTrayWnd", "MultitaskingViewFrame", "TaskSwitcherWnd",
    "XamlExplorerHostIslandWindow", "ForegroundStaging", "TaskListThumbnailWnd",
    "Windows.UI.Core.CoreWindow", "NotifyIconOverflowWindow",
    "TopLevelWindowForOverflowXamlIsland", "Progman", "WorkerW", "#32768", "#32770_tray",
})

# Консоли: Ctrl+C там — прерывание процесса, командный режим туда не шлём.
TERMINAL_CLASSES = frozenset({
    "ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "PuTTY", "KiTTY",
    "VirtualConsoleClass", "org.wezfurlong.wezterm", "Alacritty",
})


TERMINAL_PROCESSES = frozenset({
    "windowsterminal.exe", "wt.exe", "conhost.exe", "openconsole.exe", "putty.exe",
    "kitty.exe", "mintty.exe", "wezterm-gui.exe", "alacritty.exe", "tabby.exe", "hyper.exe",
    "warp.exe", "termius.exe", "mobaxterm.exe", "kitty_portable.exe", "cmder.exe",
})

# Окна, где Ctrl+C может оказаться прерыванием (SIGINT): встроенные терминалы IDE, вкладки
# SSH/RDP в менеджерах подключений, удалённые сессии. Выделение там читаем только Ctrl+Insert.
COPY_UNSAFE_PROCESSES = TERMINAL_PROCESSES | frozenset({
    "code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "vscodium.exe",
    "idea64.exe", "pycharm64.exe", "webstorm64.exe", "rider64.exe", "goland64.exe",
    "clion64.exe", "datagrip64.exe", "phpstorm64.exe", "rubymine64.exe", "studio64.exe",
    "fleet.exe", "zed.exe", "sublime_text.exe", "mremoteng.exe", "rdcman.exe", "mstsc.exe",
    "royalts.exe", "securecrt.exe", "xshell.exe", "wfica32.exe", "cdviewer.exe",
    "vmconnect.exe", "vmware.exe", "vmplayer.exe", "virtualboxvm.exe",
})

# Удалённые и виртуальные окна вне списка RDP: буфер у них синхронизируется асинхронно
# по сети, поэтому в режиме «авто» текст туда печатается, а не вставляется через буфер.
# RAIL_WINDOW — окна RemoteApp клиента mstsc; wfica32/cdviewer — Citrix; vmconnect — Hyper-V.
REMOTE_TYPE_CLASSES = frozenset({"RAIL_WINDOW"})
REMOTE_TYPE_PROCESSES = frozenset({
    "wfica32.exe", "cdviewer.exe", "vmconnect.exe", "vmware.exe", "vmplayer.exe",
    "virtualboxvm.exe",
})


def is_shell_window(info: WindowInfo) -> bool:
    return info.cls in SHELL_CLASSES or not info.process


def is_terminal(info: WindowInfo) -> bool:
    return info.cls in TERMINAL_CLASSES or info.process.lower() in TERMINAL_PROCESSES


def ctrl_c_unsafe(info: WindowInfo) -> bool:
    """Ctrl+C сюда слать нельзя: это может быть прерывание процесса (SIGINT)."""
    return is_terminal(info) or info.process.lower() in COPY_UNSAFE_PROCESSES


def is_remote_typing(info: WindowInfo) -> bool:
    """RemoteApp, Citrix, консоли виртуальных машин: печатать, а не вставлять через буфер."""
    return info.cls in REMOTE_TYPE_CLASSES or info.process.lower() in REMOTE_TYPE_PROCESSES


def focus_state(hwnd: int) -> int | None:
    """Окно с фокусом ввода в потоке окна hwnd (поле, вкладка, панель внутри окна);
    None — не узнать."""
    if not hwnd:
        return None
    tid = int(w.user32.GetWindowThreadProcessId(hwnd, None) or 0)
    if not tid:
        return None
    gti = w.GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(w.GUITHREADINFO)
    if not w.user32.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return None
    return int(gti.hwndFocus or 0)


# ---------- слежение за активным окном ----------

EVENT_SYSTEM_FOREGROUND = 0x0003
WINEVENT_OUTOFCONTEXT = 0x0000
WM_QUIT = 0x0012


class ForegroundWatcher:
    """Поток с WinEvent-хуком EVENT_SYSTEM_FOREGROUND: on_change(hwnd) при каждой смене
    активного окна. Хук пассивный — ввод не перехватывает и ничего не шлёт."""

    def __init__(self, on_change: Callable[[int], None]) -> None:
        self._on_change = on_change
        self._proc = w.WINEVENTPROC(self._event)  # держим ссылку, иначе GC
        self._tid = 0
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rupor-focus", daemon=True)
        self._thread.start()
        self._ready.wait(5)

    def stop(self) -> None:
        if self._tid:
            w.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def _run(self) -> None:
        self._tid = w.kernel32.GetCurrentThreadId()
        hook = w.user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND, None,
                                        self._proc, 0, 0, WINEVENT_OUTOFCONTEXT)
        self._ready.set()
        if not hook:
            log.warning("SetWinEventHook не удался: последнее окно отслеживаться не будет")
            return
        msg = wt.MSG()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))
        w.user32.UnhookWinEvent(hook)

    def _event(self, _h: int, _ev: int, hwnd: int, _o: int, _c: int, _t: int, _ms: int) -> None:
        try:
            if hwnd:
                self._on_change(hwnd)
        except Exception:
            log.exception("Ошибка обработки смены окна")


# ---------- ожидание завершения процесса (слежение за UI) ----------

SYNCHRONIZE = 0x00100000
INFINITE = 0xFFFFFFFF
w.kernel32.WaitForSingleObject.argtypes = [wt.HANDLE, wt.DWORD]
w.kernel32.WaitForSingleObject.restype = wt.DWORD


def watch_process(pid: int, on_exit: Callable[[], None]) -> threading.Thread:
    """Позвать on_exit, когда процесс pid завершится (или если его уже нет)."""
    def run() -> None:
        h = w.kernel32.OpenProcess(SYNCHRONIZE, False, pid)
        if not h:
            log.warning("Процесс %d не найден (ошибка %d)", pid, ctypes.get_last_error())
            on_exit()
            return
        try:
            w.kernel32.WaitForSingleObject(h, INFINITE)
        finally:
            w.kernel32.CloseHandle(h)
        on_exit()

    th = threading.Thread(target=run, name=f"rupor-watch-{pid}", daemon=True)
    th.start()
    return th


# ---------- конец сеанса Windows (выход из системы, перезагрузка, выключение) ----------

WM_QUERYENDSESSION = 0x0011
WM_ENDSESSION = 0x0016


class SessionEndWatcher:
    """Скрытое окно верхнего уровня, которому Windows шлёт WM_QUERYENDSESSION и
    WM_ENDSESSION, когда завершает сеанс (выход из системы, перезагрузка, выключение) или
    установщик закрывает программу через Restart Manager. Окна-сообщения (HWND_MESSAGE) эти
    рассылки не получают — поэтому обычное, но невидимое окно (без WS_VISIBLE).

    Windows сначала опрашивает все программы (WM_QUERYENDSESSION) и только потом рассылает
    WM_ENDSESSION — то, что сделано в on_query(), успевает до того, как оболочка на Tauri
    получит WM_ENDSESSION и снимет ядро. on_query() держать недолго (Windows ждёт ответа
    ~5 с, потом показывает «программа мешает завершению»). on_end(ending): True — сеанс
    завершается (после ответа процесс могут снять в любой момент), False — отменено."""

    def __init__(self, on_query: Callable[[], None], on_end: Callable[[bool], None]) -> None:
        self._on_query = on_query
        self._on_end = on_end
        self._proc = w.WNDPROC(self._wndproc)  # ссылку держим, иначе GC
        # класс окна — свой у каждого экземпляра: класс хранит указатель на _proc
        self._class = f"RuporSessionEnd{id(self):x}"
        self.hwnd = 0
        self._tid = 0
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> bool:
        """Создать окно (в своём потоке). False — не создалось (тогда просто без него)."""
        self._thread = threading.Thread(target=self._run, name="rupor-session-end",
                                        daemon=True)
        self._thread.start()
        self._ready.wait(5)
        return bool(self.hwnd)

    def stop(self) -> None:
        if self._tid:
            w.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        if self._thread is not None:
            self._thread.join(2)

    def _run(self) -> None:
        self._tid = w.kernel32.GetCurrentThreadId()
        hinst = w.kernel32.GetModuleHandleW(None)
        try:
            wc = w.WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self._class
            if not w.user32.RegisterClassW(ctypes.byref(wc)):
                raise OSError(tr("err.winapi", func="RegisterClassW",
                                 code=ctypes.get_last_error()))
            hwnd = w.user32.CreateWindowExW(0, self._class, "Rupor session end", 0, 0, 0, 0, 0,
                                            None, None, hinst, None)
            if not hwnd:
                err = ctypes.get_last_error()
                w.user32.UnregisterClassW(self._class, hinst)
                raise OSError(tr("err.winapi", func="CreateWindowExW", code=err))
            self.hwnd = int(hwnd)
        except Exception as e:
            log.warning("Окно для конца сеанса Windows не создано: %s", e)
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))
        w.user32.DestroyWindow(hwnd)
        w.user32.UnregisterClassW(self._class, hinst)
        self.hwnd = 0

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        if msg == WM_QUERYENDSESSION:
            try:
                self._on_query()
            except Exception:
                log.exception("Ошибка обработки конца сеанса")
            return 1                      # завершению не мешаем
        if msg == WM_ENDSESSION:
            try:
                self._on_end(bool(wparam))
            except Exception:
                log.exception("Ошибка обработки конца сеанса")
            return 0
        return int(w.user32.DefWindowProcW(hwnd, msg, wparam, lparam))
