"""Вставка текста в активное окно: печать через SendInput (Unicode) или через буфер обмена.

Защита от случайных сочетаний: перед каждым пакетом печати (а не один раз в начале) ждём,
пока пользователь отпустит Ctrl/Shift/Alt/Win. Клавишу, которую он держит сам (ptt следующей
диктовки — это видит автомат жестов через ``set_hold_probe``), программно не отпускаем:
автоповтор тут же нажал бы её снова, и перевод строки ушёл бы как Ctrl+Enter.

``expect_hwnd`` у печати и вставки: если активное окно сменилось посреди печати (щелчок,
Alt+Tab, всплывающее окно), остаток текста не уходит в чужое окно — ``FocusLost``.

Буфер обмена:
- временный текст кладётся с форматами, которые исключают его из журнала буфера Windows
  (Win+V), облачной синхронизации и сторонних менеджеров буфера;
- вставка — отложенным рендерингом: текст отдаётся только тогда, когда окно-получатель
  действительно читает буфер (WM_RENDERFORMAT), и лишь после этого возвращается прежнее
  содержимое. Медленное окно не вставит вместо диктовки старый буфер (например, пароль);
- чтение выделения — Ctrl+Insert (не шлёт SIGINT в консоли), Ctrl+C — только запасной путь;
  если буфер сменился уже после таймаута (медленный rdpclip), прежний буфер всё равно
  возвращается.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes as wt
from functools import lru_cache
from typing import Callable, NamedTuple

from . import _win as w
from .i18n import tr

log = logging.getLogger(__name__)


class KeyEvent(NamedTuple):
    vk: int
    scan: int
    flags: int


class FocusLost(OSError):
    """Активное окно сменилось посреди печати или вставки — остаток текста не отправлен."""


# ---------- клавиши со скан-кодами (RDP передаёт скан-коды, а не VK) ----------

w.user32.MapVirtualKeyW.argtypes = [ctypes.c_uint, ctypes.c_uint]
w.user32.MapVirtualKeyW.restype = ctypes.c_uint
_MAPVK_VK_TO_VSC = 0
_EXTENDED_VKS = {0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28, 0x2D, 0x2E, w.VK_RCONTROL,
                 w.VK_RMENU, w.VK_RWIN, w.VK_LWIN}


def _key(vk: int, up: bool = False) -> KeyEvent:
    """Нажатие/отпускание VK с правильным скан-кодом и флагом extended (стрелки — не NumPad)."""
    scan = int(w.user32.MapVirtualKeyW(vk, _MAPVK_VK_TO_VSC)) & 0xFF
    flags = (w.KEYEVENTF_KEYUP if up else 0) | (w.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED_VKS else 0)
    return KeyEvent(vk, scan, flags)


def _press(vk: int) -> list[KeyEvent]:
    return [_key(vk), _key(vk, up=True)]


def _unicode_units(ch: str) -> list[int]:
    """UTF-16 code units символа (эмодзи → суррогатная пара)."""
    b = ch.encode("utf-16-le")
    return [int.from_bytes(b[i:i + 2], "little") for i in range(0, len(b), 2)]


def build_events(text: str) -> list[list[KeyEvent]]:
    """Текст → список «символов», каждый — последовательность событий клавиатуры.

    Кириллица и любой Unicode идут через KEYEVENTF_UNICODE (от раскладки не зависит),
    перевод строки → VK_RETURN, табуляция → VK_TAB — со скан-кодами: клиент RDP передаёт
    на сервер скан-коды, и без них переносы строк в удалённой сессии теряются.
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    out: list[list[KeyEvent]] = []
    for ch in text:
        if ch == "\n":
            out.append(_press(w.VK_RETURN))
        elif ch == "\t":
            out.append(_press(w.VK_TAB))
        else:
            units = _unicode_units(ch)
            evs = [KeyEvent(0, u, w.KEYEVENTF_UNICODE) for u in units]
            evs += [KeyEvent(0, u, w.KEYEVENTF_UNICODE | w.KEYEVENTF_KEYUP) for u in units]
            out.append(evs)
    return out


def _send(events: list[KeyEvent]) -> None:
    if not events:
        return
    arr = (w.INPUT * len(events))()
    for i, e in enumerate(events):
        arr[i].type = w.INPUT_KEYBOARD
        arr[i].ki.wVk = e.vk
        arr[i].ki.wScan = e.scan
        arr[i].ki.dwFlags = e.flags
        arr[i].ki.time = 0
        arr[i].ki.dwExtraInfo = w.RUPOR_MAGIC
    sent = w.user32.SendInput(len(events), arr, ctypes.sizeof(w.INPUT))
    if sent != len(events):
        err = ctypes.get_last_error()
        raise OSError(tr("inj.sendinput_failed", sent=sent, total=len(events), code=err))


# ---------- модификаторы и фокус ----------

_MODIFIERS = (w.VK_LCONTROL, w.VK_RCONTROL, w.VK_LSHIFT, w.VK_RSHIFT,
              w.VK_LMENU, w.VK_RMENU, w.VK_LWIN, w.VK_RWIN)
HOLD_WAIT_MAX_S = 120.0   # пользователь держит клавишу сам — ждём, но не бесконечно

_hold_probe: Callable[[int], bool] | None = None


def set_hold_probe(fn: Callable[[int], bool] | None) -> None:
    """fn(vk) → True, если пользователь сейчас сам держит клавишу (автомат жестов видел её
    нажатие и не видел отпускания). Такую клавишу печать ждёт и программно не отпускает."""
    global _hold_probe
    _hold_probe = fn


def pressed_modifiers() -> list[int]:
    return [vk for vk in _MODIFIERS if w.user32.GetAsyncKeyState(vk) & 0x8000]


def _user_holds(vks: list[int]) -> bool:
    probe = _hold_probe
    if probe is None:
        return False
    try:
        return any(probe(vk) for vk in vks)
    except Exception:
        log.debug("hold probe", exc_info=True)
        return False


def wait_modifiers_released(timeout: float = 1.0, ignore: tuple[int, ...] = ()) -> None:
    """Иначе «Ctrl + буква» превратится в команду в целевом окне, а перевод строки — в
    Ctrl+Enter (в Outlook, Teams, Telegram это «отправить»).

    Модификатор, который держит пользователь (ptt следующей диктовки), ждём до
    HOLD_WAIT_MAX_S; прочие «залипшие» через timeout отпускаются программно."""
    held = [vk for vk in pressed_modifiers() if vk not in ignore]
    if not held:
        return
    start = time.monotonic()
    while True:
        time.sleep(0.02)
        held = [vk for vk in pressed_modifiers() if vk not in ignore]
        if not held:
            return
        limit = HOLD_WAIT_MAX_S if _user_holds(held) else timeout
        if time.monotonic() - start >= limit:
            break
    log.warning("Модификаторы всё ещё зажаты (%s) — отпускаю программно", held)
    _send([_key(vk, up=True) for vk in held])


def _check_focus(expect_hwnd: int | None) -> None:
    if not expect_hwnd:
        return
    fg = w.user32.GetForegroundWindow() or 0
    if fg != expect_hwnd:
        raise FocusLost(tr("inj.focus_lost"))


def _before_packet(expect_hwnd: int | None) -> None:
    """Перед каждым пакетом ввода: модификаторы отпущены, окно то же."""
    wait_modifiers_released()
    _check_focus(expect_hwnd)


def type_text(text: str, delay_ms: int = 0, chunk: int = 32,
              expect_hwnd: int | None = None) -> None:
    """Печать текста посимвольно через SendInput."""
    chars = build_events(text)
    if delay_ms <= 0:
        for i in range(0, len(chars), chunk):
            _before_packet(expect_hwnd)
            _send([e for c in chars[i:i + chunk] for e in c])
            time.sleep(0.002)  # дать очереди ввода окна прожеваться
    else:
        for c in chars:
            _before_packet(expect_hwnd)
            _send(c)
            time.sleep(delay_ms / 1000)


def press_key(vk: int) -> None:
    """Одно нажатие клавиши со скан-кодом (→ снять выделение и т. п.)."""
    wait_modifiers_released()
    _send(_press(vk))


def press_backspace(n: int) -> None:
    for _ in range(n):
        _send(_press(w.VK_BACK))


def build_select_left(n: int) -> list[KeyEvent]:
    """Shift + n×← + отпустить Shift (выделить n символов слева от курсора)."""
    evs = [_key(w.VK_LSHIFT)]
    for _ in range(max(0, n)):
        evs += [_key(w.VK_LEFT), _key(w.VK_LEFT, up=True)]
    evs.append(_key(w.VK_LSHIFT, up=True))
    return evs


def select_left(n: int, delay_ms: int = 0, chunk: int = 32,
                expect_hwnd: int | None = None) -> None:
    """Выделить n символов слева от курсора (режим ИИ «сначала сырой»: заменить вставленное)."""
    if n <= 0:
        return
    _before_packet(expect_hwnd)
    evs = build_select_left(n)
    shift_down, arrows, shift_up = evs[0], evs[1:-1], evs[-1]
    _send([shift_down])
    try:
        step = max(1, chunk) * 2
        for i in range(0, len(arrows), step):
            # Пользователь зажал Ctrl посреди выделения — стрелки пошли бы по словам:
            # отпускаем свой Shift, ждём, нажимаем снова.
            if [vk for vk in pressed_modifiers() if vk != w.VK_LSHIFT]:
                _send([shift_up])
                wait_modifiers_released()
                _send([shift_down])
            _check_focus(expect_hwnd)
            _send(arrows[i:i + step])
            time.sleep(max(0.002, delay_ms / 1000))
    finally:
        _send([shift_up])
    time.sleep(0.02)


def send_ctrl(letter: str) -> None:
    """Ctrl+<буква> со скан-кодами (Ctrl+C, Ctrl+V)."""
    _send_ctrl_vk(ord(letter.upper()))


def _send_ctrl_vk(vk: int) -> None:
    _send([_key(w.VK_LCONTROL), _key(vk), _key(vk, up=True), _key(w.VK_LCONTROL, up=True)])


# ---------- буфер обмена ----------

GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13
# GDI-форматы — не HGLOBAL, скопировать байтами нельзя (картинка сохранится через CF_DIB)
_SKIP_FORMATS = {2, 3, 9, 14, 0x0082, 0x0083, 0x008E}
_ZERO_DWORD = b"\0\0\0\0"
# Форматы-пометки «не сохранять в журнале буфера (Win+V), не отправлять в облако,
# не обрабатывать менеджерами буфера» — для временного текста и восстановленного буфера.
_PRIVATE_FORMAT_NAMES = ("ExcludeClipboardContentFromMonitorProcessing",
                         "CanIncludeInClipboardHistory", "CanUploadToCloudClipboard")
RENDER_WAIT_S = 2.0       # сколько ждать, пока окно прочитает вставку (WM_RENDERFORMAT)
LATE_RESTORE_S = 4.0      # Ctrl+C сработал уже после таймаута — столько ещё следим за буфером

_clip_lock = threading.RLock()  # все наши операции с буфером — по очереди
_clip_gen = 0                   # номер нашей последней операции (для отложенного возврата)
# Тесты: каждый поток модуля, который трогает буфер, сначала вызывает эту функцию
# (изолированная window station, чтобы не трогать буфер пользователя).
_thread_init: Callable[[], None] | None = None


def _init_thread() -> None:
    fn = _thread_init
    if fn is not None:
        fn()


def _bump_gen() -> int:
    global _clip_gen
    with _clip_lock:
        _clip_gen += 1
        return _clip_gen


@lru_cache(maxsize=1)
def _private_formats() -> tuple[tuple[int, bytes], ...]:
    out: list[tuple[int, bytes]] = []
    for name in _PRIVATE_FORMAT_NAMES:
        fmt = int(w.user32.RegisterClipboardFormatW(name) or 0)
        if fmt:
            out.append((fmt, _ZERO_DWORD))
    return tuple(out)


def _open_clipboard(retries: int = 20, hwnd: int | None = None) -> None:
    for _ in range(retries):
        if w.user32.OpenClipboard(hwnd):
            return
        time.sleep(0.02)
    raise OSError(tr("inj.clipboard_busy"))


def _read_global(h: int) -> bytes | None:
    size = w.kernel32.GlobalSize(h)
    ptr = w.kernel32.GlobalLock(h)
    if not ptr:
        return None
    try:
        return ctypes.string_at(ptr, size)
    finally:
        w.kernel32.GlobalUnlock(h)


def _alloc_global(data: bytes) -> int:
    h = w.kernel32.GlobalAlloc(GMEM_MOVEABLE, max(1, len(data)))
    if not h:
        raise MemoryError("GlobalAlloc")
    ptr = w.kernel32.GlobalLock(h)
    if data:
        ctypes.memmove(ptr, data, len(data))
    w.kernel32.GlobalUnlock(h)
    return int(h)


def _set_global(fmt: int, data: bytes) -> None:
    h = _alloc_global(data)
    if not w.user32.SetClipboardData(fmt, h):
        w.kernel32.GlobalFree(h)


def _text_bytes(text: str) -> bytes:
    return (text + "\0").encode("utf-16-le")


def clipboard_snapshot() -> list[tuple[int, bytes]]:
    _open_clipboard()
    try:
        items: list[tuple[int, bytes]] = []
        fmt = 0
        while True:
            fmt = w.user32.EnumClipboardFormats(fmt)
            if not fmt:
                break
            if fmt in _SKIP_FORMATS:
                continue
            h = w.user32.GetClipboardData(fmt)
            if not h:
                continue
            data = _read_global(h)
            if data is not None:
                items.append((fmt, data))
        return items
    finally:
        w.user32.CloseClipboard()


def clipboard_restore(items: list[tuple[int, bytes]], private: bool = True) -> None:
    """Вернуть снимок буфера. private — пометить, чтобы возврат не добавил в журнал
    буфера (Win+V) ещё одну копию прежнего содержимого."""
    _open_clipboard()
    try:
        w.user32.EmptyClipboard()
        have = {fmt for fmt, _ in items}
        for fmt, data in items:
            try:
                _set_global(fmt, data)
            except OSError as e:  # отдельный формат не критичен
                log.debug("Формат %s не восстановлен: %s", fmt, e)
        if private and items:
            for fmt, data in _private_formats():
                if fmt not in have:
                    _set_global(fmt, data)
    finally:
        w.user32.CloseClipboard()


def clipboard_set_text(text: str, private: bool = False) -> None:
    """Положить текст в буфер. private — временный текст Рупора: не в журнал Win+V,
    не в облако, не в менеджеры буфера. «Скопировать последнее» кладёт обычный текст."""
    _bump_gen()
    _open_clipboard()
    try:
        w.user32.EmptyClipboard()
        _set_global(CF_UNICODETEXT, _text_bytes(text))
        if private:
            for fmt, data in _private_formats():
                _set_global(fmt, data)
    finally:
        w.user32.CloseClipboard()


def clipboard_get_text() -> str | None:
    _open_clipboard()
    try:
        h = w.user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        data = _read_global(h)
        return data.decode("utf-16-le").split("\0", 1)[0] if data else None
    finally:
        w.user32.CloseClipboard()


# ---------- отложенный рендеринг: окно-владелец буфера ----------

WM_DESTROYCLIPBOARD = 0x0307
WM_RENDERFORMAT = 0x0305
WM_RENDERALLFORMATS = 0x0306
_WM_APP_OFFER = 0x8000 + 0x51  # WM_APP + …: положить «обещание» текста (из потока владельца)


class _ClipOwner:
    """Скрытое окно (message-only) в своём потоке с циклом сообщений.

    offer(text) кладёт в буфер «обещание» CF_UNICODETEXT (SetClipboardData с NULL) и
    пометки private-форматов. Когда окно-получатель читает буфер, Windows присылает
    WM_RENDERFORMAT — отдаём текст и отмечаем событие ``rendered``: вставка состоялась."""

    _CLASS = "RuporClipboardOwner"

    def __init__(self) -> None:
        self.hwnd = 0
        self._text = ""
        self.rendered = threading.Event()
        self._ready = threading.Event()
        self._error: str | None = None
        self._proc = w.WNDPROC(self._wndproc)  # ссылку держим, иначе GC
        threading.Thread(target=self._run, name="rupor-clip", daemon=True).start()
        if not self._ready.wait(5):
            raise OSError(tr("inj.clip_owner_failed"))
        if not self.hwnd:
            raise OSError(self._error or tr("inj.clip_owner_failed"))

    def _run(self) -> None:
        try:
            _init_thread()
            hinst = w.kernel32.GetModuleHandleW(None)
            wc = w.WNDCLASSW()
            wc.lpfnWndProc = self._proc
            wc.hInstance = hinst
            wc.lpszClassName = self._CLASS
            if not w.user32.RegisterClassW(ctypes.byref(wc)):
                err = ctypes.get_last_error()
                if err != 1410:  # ERROR_CLASS_ALREADY_EXISTS
                    raise OSError(tr("err.winapi", func="RegisterClassW", code=err))
            hwnd = w.user32.CreateWindowExW(0, self._CLASS, "Rupor clipboard", 0, 0, 0, 0, 0,
                                            w.HWND_MESSAGE, None, hinst, None)
            if not hwnd:
                raise OSError(tr("err.winapi", func="CreateWindowExW",
                                 code=ctypes.get_last_error()))
            self.hwnd = int(hwnd)
        except Exception as e:
            self._error = str(e)
            log.warning("Окно-владелец буфера не создано: %s", e)
            self._ready.set()
            return
        self._ready.set()
        msg = wt.MSG()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))

    def _render(self) -> bool:
        try:
            h = _alloc_global(_text_bytes(self._text))
        except MemoryError:
            return False
        if not w.user32.SetClipboardData(CF_UNICODETEXT, h):
            w.kernel32.GlobalFree(h)
            return False
        return True

    def _wndproc(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        try:
            if msg == _WM_APP_OFFER:
                return 1 if self._offer_here() else 0
            if msg == WM_RENDERFORMAT:
                if wparam == CF_UNICODETEXT and self._render():
                    self.rendered.set()
                return 0
            if msg == WM_RENDERALLFORMATS:
                # Окно закрывается, а в буфере наше «обещание» — отдать текст насовсем
                if w.user32.OpenClipboard(hwnd):
                    try:
                        if (w.user32.GetClipboardOwner() or 0) == hwnd:
                            self._render()
                    finally:
                        w.user32.CloseClipboard()
                return 0
            if msg == WM_DESTROYCLIPBOARD:
                return 0
        except Exception:
            log.exception("Окно-владелец буфера: ошибка обработки сообщения %#x", msg)
            return 0
        return int(w.user32.DefWindowProcW(hwnd, msg, wparam, lparam))

    def _offer_here(self) -> bool:
        if not w.user32.OpenClipboard(self.hwnd):
            return False
        try:
            w.user32.EmptyClipboard()
            w.user32.SetClipboardData(CF_UNICODETEXT, None)  # отложенный рендеринг
            for fmt, data in _private_formats():
                _set_global(fmt, data)
            return True
        finally:
            w.user32.CloseClipboard()

    def offer(self, text: str) -> None:
        """Положить «обещание» текста; ошибка — OSError (буфер занят)."""
        self._text = text
        self.rendered.clear()
        for _ in range(20):
            if w.user32.SendMessageW(self.hwnd, _WM_APP_OFFER, 0, 0):
                return
            time.sleep(0.02)
        raise OSError(tr("inj.clipboard_busy"))

    def owns(self) -> bool:
        return (w.user32.GetClipboardOwner() or 0) == self.hwnd


_owner: _ClipOwner | None = None
_owner_failed = False


def _clip_owner() -> _ClipOwner | None:
    """Окно-владелец (создаётся при первой вставке); не вышло — вставка по-старому."""
    global _owner, _owner_failed
    with _clip_lock:
        if _owner is None and not _owner_failed:
            try:
                _owner = _ClipOwner()
            except OSError as e:
                _owner_failed = True
                log.warning("Отложенный рендеринг буфера недоступен (%s) — восстанавливаю "
                            "буфер по таймеру", e)
        return _owner


def paste_text(text: str, restore_ms: int = 300, expect_hwnd: int | None = None) -> None:
    """Сохранить буфер → положить текст → Ctrl+V → дождаться, пока окно прочитает текст
    (до RENDER_WAIT_S) → через restore_ms вернуть прежний буфер."""
    wait_modifiers_released()
    with _clip_lock:
        _bump_gen()
        saved: list[tuple[int, bytes]] | None
        try:
            saved = clipboard_snapshot()
        except OSError as e:
            log.warning("Не удалось сохранить буфер: %s", e)
            saved = None
        owner = _clip_owner()
        if owner is not None:
            try:
                owner.offer(text)
            except OSError as e:
                log.warning("Отложенный рендеринг не удался (%s) — кладу текст сразу", e)
                owner = None
        if owner is None:
            clipboard_set_text(text, private=True)
        seq = w.user32.GetClipboardSequenceNumber()
        # Вставка сорвалась (фокус ушёл; SendInput отказал на заблокированном экране, UAC) —
        # прежний буфер возвращаем сразу, иначе в нём останется диктовка.
        try:
            _before_packet(expect_hwnd)
            _send_ctrl_vk(ord("V"))
        except BaseException:
            if saved is not None:
                try:
                    clipboard_restore(saved)
                except OSError as e:
                    log.warning("Не удалось восстановить буфер: %s", e)
            raise
        if owner is not None:
            if not owner.rendered.wait(RENDER_WAIT_S):
                log.info("Окно не прочитало вставку за %.0f с — возвращаю прежний буфер",
                         RENDER_WAIT_S)
            time.sleep(restore_ms / 1000)
            ours = owner.owns()
        else:
            time.sleep(restore_ms / 1000)
            ours = w.user32.GetClipboardSequenceNumber() == seq
        # Если за это время буфер поменял кто-то ещё — не затираем
        if saved is not None and ours:
            try:
                clipboard_restore(saved)
            except OSError as e:
                log.warning("Не удалось восстановить буфер: %s", e)


def _restore_late(saved: list[tuple[int, bytes]], seq: int, gen: int, window_s: float) -> None:
    """Ctrl+C/Ctrl+Insert сработал уже после таймаута (медленный rdpclip, занятое окно):
    как только буфер сменится, вернуть прежний. Любая наша следующая операция с буфером
    (gen сменился) отменяет возврат."""
    _init_thread()
    deadline = time.monotonic() + window_s
    while time.monotonic() < deadline:
        if _clip_gen != gen:
            return
        if w.user32.GetClipboardSequenceNumber() != seq:
            time.sleep(0.05)  # владелец мог ещё не дописать форматы
            with _clip_lock:
                if _clip_gen != gen:
                    return
                try:
                    clipboard_restore(saved)
                    log.info("Выделение пришло в буфер после таймаута — прежний буфер возвращён")
                except OSError as e:
                    log.warning("Не удалось восстановить буфер: %s", e)
            return
        time.sleep(0.02)


COPY_INSERT = "insert"   # Ctrl+Insert: копирует в edit/RichEdit, браузерах, Office, IDE, терминалах
COPY_CTRL_C = "c"        # Ctrl+C: запасной путь (в консоли без выделения — это SIGINT)


def copy_selection(timeout_ms: int = 400, via: str | tuple[str, ...] = COPY_INSERT,
                   late_restore_s: float = LATE_RESTORE_S) -> str | None:
    """Прочитать выделенный текст активного окна: сохранить буфер → Ctrl+Insert (или Ctrl+C;
    несколько способов — по очереди, пока буфер не сменится) → дождаться смены буфера
    (GetClipboardSequenceNumber, до timeout_ms на способ) → прочитать текст → восстановить
    буфер.

    None — буфер не изменился (выделения нет или окно не отдаёт его через буфер); если он
    сменится позже, прежний буфер всё равно вернётся. Буфер занят и снимок не сделать —
    OSError: копирование тогда не шлём вовсе, иначе содержимое буфера пользователя пропало бы."""
    vias = (via,) if isinstance(via, str) else tuple(via)
    wait_modifiers_released()
    with _clip_lock:
        gen = _bump_gen()
        saved = clipboard_snapshot()  # OSError — наверх, без Ctrl+C
        seq = w.user32.GetClipboardSequenceNumber()
        changed = False
        for v in vias:
            _send_ctrl_vk(w.VK_INSERT if v == COPY_INSERT else ord("C"))
            deadline = time.monotonic() + timeout_ms / 1000
            while time.monotonic() < deadline:
                if w.user32.GetClipboardSequenceNumber() != seq:
                    changed = True
                    break
                time.sleep(0.01)
            if changed:
                break
        if not changed:
            if late_restore_s > 0:
                threading.Thread(target=_restore_late, args=(saved, seq, gen, late_restore_s),
                                 name="rupor-clip-late", daemon=True).start()
            return None
        time.sleep(0.03)  # владелец буфера мог ещё не дописать все форматы
        text: str | None = None
        try:
            text = clipboard_get_text()
        except OSError as e:
            log.warning("Не удалось прочитать выделение из буфера: %s", e)
        try:
            clipboard_restore(saved)
        except OSError as e:
            log.warning("Не удалось восстановить буфер: %s", e)
        return text
