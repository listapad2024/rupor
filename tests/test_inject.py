import time
import types

import pytest

from rupor import _win as w
from rupor import inject
from rupor.inject import KeyEvent, build_events

U = w.KEYEVENTF_UNICODE
UP = w.KEYEVENTF_KEYUP


def test_cyrillic_is_unicode_packet() -> None:
    ev = build_events("Ж")
    assert ev == [[KeyEvent(0, ord("Ж"), U), KeyEvent(0, ord("Ж"), U | UP)]]


def test_each_char_is_separate_press() -> None:
    assert len(build_events("Привет")) == 6


def test_emoji_is_surrogate_pair() -> None:
    (ev,) = build_events("😀")
    assert [e.scan for e in ev] == [0xD83D, 0xDE00, 0xD83D, 0xDE00]
    assert [e.flags for e in ev] == [U, U, U | UP, U | UP]


def test_newlines_are_enter_with_scan_code() -> None:
    # RDP передаёт на сервер скан-коды: Enter без скан-кода в удалённой сессии теряется
    ev = build_events("а\r\nб\nв\rг")
    enter = [KeyEvent(w.VK_RETURN, 0x1C, 0), KeyEvent(w.VK_RETURN, 0x1C, UP)]
    assert ev[1] == enter and ev[3] == enter and ev[5] == enter
    assert len(ev) == 7


def test_tab_is_vk_tab_with_scan_code() -> None:
    assert build_events("\t") == [[KeyEvent(w.VK_TAB, 0x0F, 0), KeyEvent(w.VK_TAB, 0x0F, UP)]]


def test_mixed_latin_cyrillic() -> None:
    ev = build_events("SQL-запрос")
    assert all(e.flags & U for c in ev for e in c)
    assert "".join(chr(c[0].scan) for c in ev) == "SQL-запрос"


# ---------- печать: модификаторы и фокус (SendInput подменён) ----------

class Keys:
    """Вместо SendInput: какие пакеты ушли и что в этот момент было зажато."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.packets: list[list[KeyEvent]] = []
        self.held: list[int] = []
        self.held_at_send: list[list[int]] = []
        self.fg = 100
        monkeypatch.setattr(inject, "_send", self.send)
        monkeypatch.setattr(inject, "pressed_modifiers", lambda: list(self.held))
        monkeypatch.setattr(w.user32, "GetForegroundWindow", lambda: self.fg)
        # своё «время» только для модуля inject (глобальный time.sleep не трогаем)
        self.clock = types.SimpleNamespace(sleep=lambda s: None, monotonic=time.monotonic)
        monkeypatch.setattr(inject, "time", self.clock)

    def send(self, events: list[KeyEvent]) -> None:
        self.packets.append(list(events))
        self.held_at_send.append(list(self.held))


def test_typing_waits_while_user_holds_ptt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Правый Ctrl зажат для следующей диктовки посреди печати: печать ждёт отпускания
    и программно Ctrl не отпускает (иначе перевод строки ушёл бы как Ctrl+Enter)."""
    k = Keys(monkeypatch)
    checks = {"n": 0}

    def probe(vk: int) -> bool:
        checks["n"] += 1
        if checks["n"] > 5:          # «пользователь отпустил клавишу»
            k.held.clear()
        return vk == w.VK_RCONTROL

    monkeypatch.setattr(inject, "_hold_probe", probe)
    real_pressed = inject.pressed_modifiers

    def pressed() -> list[int]:
        if len(k.packets) == 2 and checks["n"] == 0 and not k.held:
            k.held.append(w.VK_RCONTROL)  # нажал после двух символов
        return real_pressed()

    monkeypatch.setattr(inject, "pressed_modifiers", pressed)
    inject.type_text("аб\nв", delay_ms=5)
    ups = [e for p in k.packets for e in p if e.vk == w.VK_RCONTROL]
    assert ups == []                                  # Ctrl не «отпускали» программно
    assert all(h == [] for h in k.held_at_send)       # ни один символ — с зажатым Ctrl
    assert len(k.packets) == 4


def test_stuck_modifier_is_released_after_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    k = Keys(monkeypatch)
    k.held.append(w.VK_LSHIFT)
    t = {"now": 0.0}

    def mono() -> float:
        t["now"] += 0.1
        if t["now"] > 5:
            k.held.clear()
        return t["now"]

    k.clock.monotonic = mono
    monkeypatch.setattr(inject, "_hold_probe", lambda vk: False)
    inject.wait_modifiers_released(timeout=1.0)
    assert k.packets and k.packets[0] == [inject._key(w.VK_LSHIFT, up=True)]


def test_typing_stops_when_focus_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    k = Keys(monkeypatch)
    real_send = k.send

    def send(events: list[KeyEvent]) -> None:
        real_send(events)
        if len(k.packets) == 2:
            k.fg = 200                   # фокус ушёл в другое окно
    monkeypatch.setattr(inject, "_send", send)
    with pytest.raises(inject.FocusLost):
        inject.type_text("абвгд", delay_ms=5, expect_hwnd=100)
    assert len(k.packets) == 2


def test_paste_and_copy_keys_have_scan_codes() -> None:
    assert inject._key(ord("V")).scan == 0x2F
    assert inject._key(w.VK_INSERT) == KeyEvent(w.VK_INSERT, 0x52, w.KEYEVENTF_EXTENDEDKEY)


def test_paste_restores_clipboard_when_send_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """SendInput отказал (экран заблокирован, secure desktop): прежний буфер возвращается,
    а не остаётся занятым диктовкой."""
    restored: list[object] = []
    saved = [(13, b"o\0l\0d\0\0\0")]
    monkeypatch.setattr(inject, "wait_modifiers_released", lambda: None)
    monkeypatch.setattr(inject, "clipboard_snapshot", lambda: saved)
    monkeypatch.setattr(inject, "_clip_owner", lambda: None)
    monkeypatch.setattr(inject, "clipboard_set_text", lambda text, private=False: None)
    monkeypatch.setattr(inject, "clipboard_restore", restored.append)
    monkeypatch.setattr(inject, "_before_packet", lambda hwnd: None)
    monkeypatch.setattr(w.user32, "GetClipboardSequenceNumber", lambda: 7)

    def fail(vk: int) -> None:
        raise OSError("SendInput: доступ запрещён")

    monkeypatch.setattr(inject, "_send_ctrl_vk", fail)
    with pytest.raises(OSError):
        inject.paste_text("диктовка", restore_ms=0)
    assert restored == [saved]
