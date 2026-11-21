"""Текст поля ввода через Windows UI Automation (comtypes) — для обучения словаря.

Все вызовы COM идут в одном фоновом потоке ``rupor-uia`` (``CoInitializeEx`` MTA; comtypes
импортируется там же с ``sys.coinit_flags = MTA``, чтобы не сделать чужой поток STA).
Остальные потоки зовут ``focused_field()`` / ``Field.read_text()`` синхронно, с таймаутом:
зависшее приложение не вешает ядро (плюс ``IUIAutomation2.ConnectionTimeout`` и
``TransactionTimeout`` ≈ 1 с, где они есть — Windows 8+).

Что читается: ValuePattern.CurrentValue, иначе TextPattern (весь документ до ``MAX_TEXT``
символов, у длинного документа — окно вокруг курсора). ValuePattern поля Win32 Edit
обрезает текст до 4096 символов (``VALUE_CLIP``) — значение такой длины и длиннее берётся
из TextPattern, а без TextPattern поле не читается. Поле пароля (IsPassword) не
читается никогда; поле только для чтения и элементы без этих паттернов — None.

Текст полей — переписка пользователя: в лог он не пишется никогда (только типы, длины,
коды ошибок). Любая ошибка COM — None и ``log.debug`` без текста.

Модуль-обёртка UIAutomationCore для comtypes (``comtypes.gen.UIAutomationClient``)
генерируется при сборке ядра (packaging/rupor-core.spec) и входит в неё; если его нет
(свежий .venv), он создаётся через ``comtypes.client.GetModule`` при первом вызове.
"""
from __future__ import annotations

import ctypes
import itertools
import logging
import queue
import sys
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from typing import Any, Callable, TypeVar

log = logging.getLogger(__name__)

MAX_TEXT = 20000          # больше — окно вокруг курсора (или None)
VALUE_CLIP = 4096         # столько отдаёт ValuePattern поля Win32 Edit, даже если текст длиннее
CALL_TIMEOUT_S = 3.0      # ожидание ответа потока UIA
UIA_TIMEOUT_MS = 1000     # IUIAutomation2.ConnectionTimeout / TransactionTimeout
INIT_TIMEOUT_S = 15.0     # первая инициализация (генерация обёртки может занять секунды)

UIA_INVOKE_PATTERN = 10000
UIA_VALUE_PATTERN = 10002
UIA_TOGGLE_PATTERN = 10015
UIA_LEGACY_PATTERN = 10018
UIA_CONTROL_TYPE_PROP = 30003
UIA_BUTTON = 50000
_SCOPE_DESCENDANTS = 4
UIA_TEXT_PATTERN = 10014
UIA_EDIT = 50004
UIA_DOCUMENT = 50030
TEXT_ONLY_TYPES = frozenset({UIA_EDIT, UIA_DOCUMENT})  # только TextPattern — только поле/документ
_ENDPOINT_START, _ENDPOINT_END, _UNIT_CHAR = 0, 1, 0
COINIT_MULTITHREADED = 0

T = TypeVar("T")


class UiaError(Exception):
    """UIA недоступна или не ответила вовремя."""


@dataclass(frozen=True)
class Field:
    """Поле ввода с фокусом (снимок свойств). Текст — ``read_text()`` (каждый раз заново)."""
    runtime_id: tuple[int, ...]
    pid: int
    control_type: int
    key: int = 0          # какой элемент держит поток UIA (внутреннее)

    def read_text(self, limit: int = MAX_TEXT) -> str | None:
        """Текст поля или None (фокус ушёл, поле стало паролем/только для чтения, ошибка)."""
        try:
            return _worker.call(lambda: _worker.read(self, limit))
        except UiaError as e:
            log.debug("UIA: текст поля не прочитан: %s", e)
            return None
        except Exception as e:
            log.debug("UIA: текст поля не прочитан (%s)", type(e).__name__)
            return None


class _Box:
    __slots__ = ("done", "result", "error")

    def __init__(self) -> None:
        self.done = threading.Event()
        self.result: Any = None
        self.error: BaseException | None = None


class _Worker:
    """Поток UIA: CoInitializeEx(MTA), объект CUIAutomation и очередь вызовов."""

    def __init__(self) -> None:
        self._q: queue.Queue[tuple[Callable[[], Any], _Box]] = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._init_timed_out = False      # инициализация не уложилась — больше не ждём
        self._busy_since = 0.0            # когда начался текущий вызов (0 — простаивает)
        self.error: str | None = None     # почему UIA недоступна
        self.auto: Any = None             # IUIAutomation(2)
        self.mod: Any = None              # comtypes.gen.UIAutomationClient
        self.timeouts = False             # удалось выставить таймауты IUIAutomation2
        self._keys = itertools.count(1)
        self._elem: tuple[int, Any] | None = None   # (key, IUIAutomationElement) — последний

    # ---------- поток ----------

    def ensure(self) -> bool:
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._run, name="rupor-uia", daemon=True)
                self._thread.start()
        # Ждём инициализацию; если она однажды не уложилась в срок — больше не блокируемся
        if not self._ready.is_set() and not self._init_timed_out:
            if not self._ready.wait(INIT_TIMEOUT_S):
                self._init_timed_out = True
        return self._ready.is_set() and self.auto is not None

    def _run(self) -> None:
        try:
            self._init()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            log.warning("UI Automation недоступна: %s", self.error)
        finally:
            self._ready.set()
        while True:
            fn, box = self._q.get()
            self._busy_since = time.monotonic()
            try:
                box.result = fn()
            except BaseException as e:
                box.error = e
            finally:
                self._busy_since = 0.0
                box.done.set()

    def _init(self) -> None:
        ctypes.OleDLL("ole32").CoInitializeEx(None, COINIT_MULTITHREADED)
        missing = object()
        prev = getattr(sys, "coinit_flags", missing)
        sys.coinit_flags = COINIT_MULTITHREADED  # type: ignore[attr-defined]
        try:
            import comtypes  # при первом импорте comtypes сам зовёт CoInitializeEx(flags)
        finally:
            if prev is missing:
                del sys.coinit_flags  # type: ignore[attr-defined]
            else:
                sys.coinit_flags = prev  # type: ignore[attr-defined]
        self.mod = _load_module()
        m = self.mod
        try:
            auto = comtypes.CoCreateInstance(m.CUIAutomation8._reg_clsid_,
                                             interface=m.IUIAutomation2,
                                             clsctx=comtypes.CLSCTX_INPROC_SERVER)
            auto.ConnectionTimeout = UIA_TIMEOUT_MS
            auto.TransactionTimeout = UIA_TIMEOUT_MS
            self.timeouts = True
        except Exception as e:
            log.info("UIA: IUIAutomation2 недоступен (%s) — без таймаутов", type(e).__name__)
            auto = comtypes.CoCreateInstance(m.CUIAutomation._reg_clsid_,
                                             interface=m.IUIAutomation,
                                             clsctx=comtypes.CLSCTX_INPROC_SERVER)
        self.auto = auto
        log.info("UI Automation готова%s", ", таймауты 1 с" if self.timeouts else "")

    def call(self, fn: Callable[[], T], timeout: float = CALL_TIMEOUT_S) -> T:
        if not self.ensure():
            raise UiaError(self.error or "UI Automation недоступна")
        since = self._busy_since
        if since and time.monotonic() - since > timeout:
            raise UiaError("поток UIA занят зависшим вызовом")
        box = _Box()
        self._q.put((fn, box))
        if not box.done.wait(timeout):
            raise UiaError("UIA не ответила вовремя")
        if box.error is not None:
            raise box.error
        result: T = box.result
        return result

    # ---------- работа в потоке UIA ----------

    def _pattern(self, el: Any, pid: int, iface: Any) -> Any:
        try:
            p = el.GetCurrentPattern(pid)
        except Exception:
            return None
        if not p:
            return None
        try:
            return p.QueryInterface(iface)
        except Exception:
            return None

    def describe(self, el: Any, keep: bool = True) -> Field | None:
        """Поле ввода, если элемент — редактируемое поле с текстом (не пароль).
        keep — запомнить элемент для следующих read() (наблюдение за полем); контекст окна
        его не запоминает, чтобы не подменить элемент, за которым следит обучение словаря."""
        if not el:
            return None
        if el.CurrentIsPassword:
            return None
        ct = int(el.CurrentControlType)
        vp = self._pattern(el, UIA_VALUE_PATTERN, self.mod.IUIAutomationValuePattern)
        if vp is not None:
            if vp.CurrentIsReadOnly:
                return None
        elif ct not in TEXT_ONLY_TYPES or self._pattern(
                el, UIA_TEXT_PATTERN, self.mod.IUIAutomationTextPattern) is None:
            return None
        rid = tuple(int(x) for x in (el.GetRuntimeId() or ()))
        if not keep:
            return Field(rid, int(el.CurrentProcessId), ct, 0)
        key = next(self._keys)
        self._elem = (key, el)     # прежний элемент освобождается здесь, в потоке UIA
        return Field(rid, int(el.CurrentProcessId), ct, key)

    def focused(self) -> Field | None:
        try:
            el = self.auto.GetFocusedElement()
        except Exception as e:     # нет фокуса, элемент исчез, таймаут
            log.debug("UIA: элемент с фокусом не получен (%s)", type(e).__name__)
            return None
        return self.describe(el)

    def from_handle(self, hwnd: int) -> Field | None:
        el = self.auto.ElementFromHandle(hwnd)
        return self.describe(el)

    def _element(self, f: Field) -> Any:
        """COM-элемент поля: тот, что держим, или снова элемент с фокусом — если это то же поле."""
        cur = self._elem
        if cur is not None and cur[0] == f.key:
            return cur[1]
        try:
            el = self.auto.GetFocusedElement()
        except Exception:
            return None
        if not el or tuple(int(x) for x in (el.GetRuntimeId() or ())) != f.runtime_id:
            return None
        return el

    def read(self, f: Field, limit: int) -> str | None:
        el = self._element(f)
        if el is None or el.CurrentIsPassword:
            return None
        vp = self._pattern(el, UIA_VALUE_PATTERN, self.mod.IUIAutomationValuePattern)
        value: str | None = None
        if vp is not None:
            if vp.CurrentIsReadOnly:
                return None
            value = str(vp.CurrentValue or "")
            # ValuePattern поля Win32 Edit отдаёт не больше VALUE_CLIP символов: ровно столько
            # (и больше) — возможно, обрезано; тогда читаем TextPattern, а без него — не читаем
            if value and len(value) < VALUE_CLIP and len(value) <= limit:
                return value
        tp = self._pattern(el, UIA_TEXT_PATTERN, self.mod.IUIAutomationTextPattern)
        if tp is None:
            return "" if value == "" else None
        doc = tp.DocumentRange.GetText(limit + 1) or ""
        if len(doc) <= limit:
            return str(doc)
        return self._caret_window(tp, limit)

    def _caret_window(self, tp: Any, limit: int) -> str | None:
        """Длинный документ (Word): текст вокруг курсора, по limit/2 символов в обе стороны."""
        sel = tp.GetSelection()
        if not sel or sel.Length < 1:
            return None
        rng = sel.GetElement(0).Clone()
        half = limit // 2
        rng.MoveEndpointByUnit(_ENDPOINT_START, _UNIT_CHAR, -half)
        rng.MoveEndpointByUnit(_ENDPOINT_END, _UNIT_CHAR, half)
        text = rng.GetText(limit) or ""
        return str(text)

    def before_caret(self, el: Any, limit: int) -> tuple[int, str] | None:
        """(pid, до limit символов перед курсором) редактируемого поля; пароль, поле
        только для чтения, не поле — None. TextPattern: диапазон от начала выделения назад;
        только ValuePattern — хвост значения (курсор обычно в конце поля ввода)."""
        f = self.describe(el, keep=False)   # пароль, только чтение, не поле — None
        if f is None:
            return None
        tp = self._pattern(el, UIA_TEXT_PATTERN, self.mod.IUIAutomationTextPattern)
        if tp is not None:
            sel = tp.GetSelection()
            if sel and sel.Length >= 1:
                caret = sel.GetElement(0)
                rng = caret.Clone()
                rng.MoveEndpointByRange(_ENDPOINT_END, caret, _ENDPOINT_START)
                rng.MoveEndpointByUnit(_ENDPOINT_START, _UNIT_CHAR, -limit)
                return f.pid, str(rng.GetText(limit) or "")
        vp = self._pattern(el, UIA_VALUE_PATTERN, self.mod.IUIAutomationValuePattern)
        if vp is None or vp.CurrentIsReadOnly:
            return None
        value = str(vp.CurrentValue or "")
        if len(value) >= VALUE_CLIP:   # возможно, обрезано — хвост не у курсора
            return None
        return f.pid, value[-limit:]

    def caret_focused(self, limit: int) -> tuple[int, str] | None:
        try:
            el = self.auto.GetFocusedElement()
        except Exception as e:
            log.debug("UIA: элемент с фокусом не получен (%s)", type(e).__name__)
            return None
        return self.before_caret(el, limit) if el else None

    def _buttons(self, hwnd: int) -> list[tuple[str, Any]]:
        cond = self.auto.CreatePropertyCondition(UIA_CONTROL_TYPE_PROP, UIA_BUTTON)
        arr = self.auto.ElementFromHandle(hwnd).FindAll(_SCOPE_DESCENDANTS, cond)
        out: list[tuple[str, Any]] = []
        for i in range(int(arr.Length)):
            try:
                e = arr.GetElement(i)
                out.append((str(e.CurrentName or ""), e))
            except Exception:
                continue
        return out

    def button_names(self, hwnd: int) -> list[str]:
        return [n for n, _ in self._buttons(hwnd)]

    def press(self, hwnd: int, match: Callable[[str], bool]) -> str | None:
        """Нажать первую кнопку окна, чьё имя подходит: Invoke, иначе Toggle, иначе
        действие по умолчанию (LegacyIAccessible). Имя нажатой или None."""
        m = self.mod
        for name, e in self._buttons(hwnd):
            if not match(name):
                continue
            for pid, iface, act in ((UIA_INVOKE_PATTERN, m.IUIAutomationInvokePattern, "Invoke"),
                                    (UIA_TOGGLE_PATTERN, m.IUIAutomationTogglePattern, "Toggle"),
                                    (UIA_LEGACY_PATTERN, m.IUIAutomationLegacyIAccessiblePattern,
                                     "DoDefaultAction")):
                pat = self._pattern(e, pid, iface)
                if pat is None:
                    continue
                try:
                    getattr(pat, act)()
                    return name
                except Exception as ex:
                    log.debug("UIA: %s не сработал (%s)", act, type(ex).__name__)
            return None
        return None

    def focus_summary(self) -> tuple[int | None, bool, int]:
        """Для самопроверки: (тип элемента с фокусом, поле с текстом, длина текста)."""
        try:
            el = self.auto.GetFocusedElement()
        except Exception:
            return None, False, 0
        if not el:
            return None, False, 0
        ct = int(el.CurrentControlType)
        f = self.describe(el)
        if f is None:
            return ct, False, 0
        text = self.read(f, MAX_TEXT)
        return ct, text is not None, len(text or "")


def _load_module() -> Any:
    """comtypes-обёртка UIAutomationCore: готовая (сборка) или сгенерированная сейчас."""
    try:
        from comtypes.gen import UIAutomationClient
        return UIAutomationClient
    except ImportError:
        pass
    import comtypes.client
    log.info("UIA: генерирую обёртку UIAutomationCore для comtypes")
    return comtypes.client.GetModule("UIAutomationCore.dll")


_worker = _Worker()


# ---------- API ----------

def available() -> bool:
    """UI Automation инициализирована (первый вызов запускает поток UIA)."""
    return _worker.ensure()


def focused_field(timeout: float = CALL_TIMEOUT_S) -> Field | None:
    """Поле ввода с клавиатурным фокусом (любого приложения) или None: фокуса нет, это не
    поле с текстом, пароль, только для чтения, ошибка UIA. Текст здесь не читается."""
    try:
        return _worker.call(_worker.focused, timeout)
    except UiaError as e:
        log.debug("UIA: %s", e)
        return None
    except Exception as e:
        log.debug("UIA: ошибка получения фокуса (%s)", type(e).__name__)
        return None


def field_from_handle(hwnd: int, timeout: float = CALL_TIMEOUT_S) -> Field | None:
    """Поле по окну (hwnd) без фокуса — для самопроверки и тестов на собственном окне."""
    try:
        return _worker.call(lambda: _worker.from_handle(hwnd), timeout)
    except UiaError as e:
        log.debug("UIA: %s", e)
        return None
    except Exception as e:
        log.debug("UIA: окно не прочитано (%s)", type(e).__name__)
        return None


def button_names(hwnd: int, timeout: float = CALL_TIMEOUT_S) -> list[str] | None:
    """Имена кнопок окна (UIA, все потомки) или None — UIA недоступна или не ответила.
    Имена — интерфейс чужого приложения: в лог не пишутся."""
    try:
        return _worker.call(lambda: _worker.button_names(hwnd), timeout)
    except Exception as e:
        log.debug("UIA: кнопки окна не прочитаны (%s)", type(e).__name__)
        return None


def press_button(hwnd: int, match: Callable[[str], bool],
                 timeout: float = CALL_TIMEOUT_S) -> str | None:
    """Нажать первую кнопку окна hwnd, имя которой подходит под match. Имя нажатой или None."""
    try:
        return _worker.call(lambda: _worker.press(hwnd, match), timeout)
    except Exception as e:
        log.debug("UIA: кнопка не нажата (%s)", type(e).__name__)
        return None


CONTEXT_TIMEOUT_S = 0.3   # контекст окна для подсказки: дольше — без него (не задерживаем ASR)
CONTEXT_CHARS = 600       # сколько читать перед курсором (движок оставит 1–2 предложения)


def caret_context(limit: int = CONTEXT_CHARS,
                  timeout: float = CONTEXT_TIMEOUT_S) -> tuple[int, str] | None:
    """(pid, текст перед курсором) поля ввода с клавиатурным фокусом — контекст окна для
    подсказки распознаванию. None: нет поля, пароль, только чтение, UIA не ответила за
    timeout. Текст никуда не пишется (ни в лог, ни на диск)."""
    try:
        return _worker.call(lambda: _worker.caret_focused(limit), timeout)
    except UiaError as e:
        log.debug("UIA: контекст не прочитан: %s", e)
        return None
    except Exception as e:
        log.debug("UIA: контекст не прочитан (%s)", type(e).__name__)
        return None


def context_from_handle(hwnd: int, limit: int = CONTEXT_CHARS,
                        timeout: float = CALL_TIMEOUT_S) -> tuple[int, str] | None:
    """То же для окна по hwnd (без фокуса) — тесты на собственном скрытом поле."""
    try:
        return _worker.call(lambda: _worker.before_caret(_worker.auto.ElementFromHandle(hwnd),
                                                         limit), timeout)
    except UiaError as e:
        log.debug("UIA: %s", e)
        return None
    except Exception as e:
        log.debug("UIA: контекст окна не прочитан (%s)", type(e).__name__)
        return None


# ---------- собственное скрытое поле (самопроверка, тесты) ----------

_u32 = ctypes.WinDLL("user32", use_last_error=True)
_u32.CreateWindowExW.argtypes = [wt.DWORD, wt.LPCWSTR, wt.LPCWSTR, wt.DWORD, ctypes.c_int,
                                 ctypes.c_int, ctypes.c_int, ctypes.c_int, wt.HWND, wt.HMENU,
                                 wt.HINSTANCE, ctypes.c_void_p]
_u32.CreateWindowExW.restype = wt.HWND
_u32.DestroyWindow.argtypes = [wt.HWND]
_u32.DestroyWindow.restype = wt.BOOL
_u32.SetWindowTextW.argtypes = [wt.HWND, wt.LPCWSTR]
_u32.SetWindowTextW.restype = wt.BOOL
_u32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), wt.HWND, wt.UINT, wt.UINT]
_u32.GetMessageW.restype = wt.BOOL
_u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
_u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
_u32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
_u32.PostThreadMessageW.restype = wt.BOOL
_u32.SendMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
_u32.SendMessageW.restype = wt.LPARAM
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetCurrentThreadId.restype = wt.DWORD
_EM_SETSEL = 0x00B1

ES_MULTILINE, ES_PASSWORD, ES_READONLY = 0x0004, 0x0020, 0x0800
_WS_CHILD = 0x40000000
_WM_QUIT = 0x0012


class HiddenEdit:
    """Своё НЕВИДИМОЕ окно с полем Edit (Win32) в отдельном потоке с циклом сообщений.

    Окно никогда не показывается и фокус не берёт: UIA читает его через
    ``field_from_handle`` — так проверяется механизм, не трогая окна пользователя."""

    def __init__(self, text: str = "", style: int = ES_MULTILINE) -> None:
        self._text, self._style = text, style
        self.hwnd = 0
        self._parent = 0
        self._tid = 0
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="rupor-uia-edit", daemon=True)

    def __enter__(self) -> HiddenEdit:
        self._thread.start()
        if not self._ready.wait(5) or not self.hwnd:
            raise OSError("Не удалось создать тестовое поле")
        return self

    def __exit__(self, *exc: object) -> None:
        if self._tid:
            _u32.PostThreadMessageW(self._tid, _WM_QUIT, 0, 0)
        self._thread.join(5)

    def set_text(self, text: str) -> None:
        """Сменить текст (WM_SETTEXT уходит потоку окна — он крутит цикл сообщений)."""
        _u32.SetWindowTextW(self.hwnd, text)

    def set_caret(self, pos: int) -> None:
        """Поставить курсор (EM_SETSEL pos, pos) — фокус окну не нужен."""
        _u32.SendMessageW(self.hwnd, _EM_SETSEL, pos, pos)

    def _run(self) -> None:
        self._tid = int(_k32.GetCurrentThreadId())
        try:
            # Родитель — невидимое окно верхнего уровня (без WS_VISIBLE), поле — его дочка.
            self._parent = _u32.CreateWindowExW(0, "STATIC", "Rupor UIA probe", 0, 0, 0, 320, 200,
                                                None, None, None, None) or 0
            if self._parent:
                self.hwnd = _u32.CreateWindowExW(0, "EDIT", self._text, _WS_CHILD | self._style,
                                                 0, 0, 300, 150, self._parent, None, None,
                                                 None) or 0
        finally:
            self._ready.set()
        msg = wt.MSG()
        while _u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            _u32.TranslateMessage(ctypes.byref(msg))
            _u32.DispatchMessageW(ctypes.byref(msg))
        if self._parent:
            _u32.DestroyWindow(self._parent)


def selftest() -> tuple[bool, str]:
    """Самопроверка для ``--uia-selftest``: инициализация, элемент с фокусом (тип, есть ли
    текст, его длина — без самого текста) и чтение собственного скрытого поля."""
    if not available():
        return False, f"UIA: недоступна ({_worker.error or 'нет ответа'})"
    parts = ["UIA: ok" + (", таймауты 1 с" if _worker.timeouts else ", без таймаутов")]
    try:
        ct, has_text, n = _worker.call(_worker.focus_summary)
        parts.append(f"фокус: тип {ct}" if ct is not None else "фокус: нет")
        parts.append(f"поле с текстом: {'да' if has_text else 'нет'}, длина {n}")
    except Exception as e:
        parts.append(f"фокус: ошибка {type(e).__name__}")
    ok = True
    sample = "Проверка UIA: ClickHouse"
    try:
        with HiddenEdit(sample) as ed:
            f = field_from_handle(ed.hwnd)
            got = f.read_text() if f is not None else None
        ok = got == sample
        parts.append("своё поле: " + ("ok" if ok else "не прочиталось"))
    except OSError as e:
        ok = False
        parts.append(f"своё поле: {e}")
    return ok, ", ".join(parts)
