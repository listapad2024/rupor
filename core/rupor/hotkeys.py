"""Глобальные хоткеи: низкоуровневый хук WH_KEYBOARD_LL + автомат жестов.

Автомат (GestureMachine) — чистая логика без Windows, покрыт тестами.
Клавиша диктовки (ptt) — одна клавиша («rctrl») или сочетание («ctrl+win», как в Wispr Flow).
Жесты (по умолчанию ptt = Ctrl+Win):
  удержание ≥ tap_max_ms           → push-to-talk: запись, по отпусканию — распознать и вставить;
  два коротких тапа ≤ double_tap_ms → hands-free: запись до следующего нажатия;
  ptt + другая клавиша (Ctrl+C…)    → обычное сочетание, запись тихо выбрасывается;
  Esc во время записи               → отмена (сам Esc проглатывается: с зажатым Ctrl
                                      система иначе получила бы Ctrl+Esc и открыла «Пуск»);
  broadcast_modifier (Shift) во время записи → текст уйдёт во все RDP-сессии;
  command_modifier (Alt) во время записи     → командный режим (правка выделенного).
Модификаторы рассылки/команды, нажатые во время удержания ptt, «проглатываются» —
не доходят до приложений (иначе Win откроет «Пуск», а Alt+Shift переключит раскладку);
отпускание проглоченной клавиши тоже проглатывается. Если во время записи нажали постороннюю
клавишу (чужое сочетание Ctrl+Alt+Shift+S), проглоченные Shift/Win вбрасываются обратно
вместе с ней. Перед отпусканием Alt/Win, входивших в жест, хук шлёт нейтральную клавишу vkE8:
меню окна и «Пуск» не откроются. «Без рук» останавливается по отпусканию ptt — если до него
нажата другая клавиша, это сочетание приложения, и запись продолжается.

Потерянное отпускание ptt (UAC, окно администратора, чужой хук): новое нажатие ptt, которое
автомат принял бы за автоповтор, но между ним и прошлым нажатием больше LOST_UP_MS, —
это новое нажатие; прежняя запись отменяется (CANCEL), и начинается новая.

Отдельно — сочетания «Вставить последнее» и «Скопировать последнее» (например,
ctrl+alt+shift+v): основная клавиша сочетания проглатывается.
"""
from __future__ import annotations

import ctypes
import logging
import threading
import time
from ctypes import wintypes as wt
from dataclasses import dataclass
from enum import Enum, auto
from typing import Callable

from . import _win as w
from .i18n import tr

log = logging.getLogger(__name__)

KEY_NAMES: dict[str, int] = {
    "rctrl": 0xA3, "lctrl": 0xA2, "rshift": 0xA1, "lshift": 0xA0,
    "ralt": 0xA5, "lalt": 0xA4, "rwin": 0x5C, "lwin": 0x5B,
    "esc": 0x1B, "capslock": 0x14, "scrolllock": 0x91, "pause": 0x13, "insert": 0x2D,
    **{f"f{i}": 0x70 + i - 1 for i in range(1, 25)},
}

# Основные клавиши сочетаний (помимо букв, цифр и KEY_NAMES)
_CHORD_KEYS: dict[str, int] = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
}
_MOD_VKS: dict[str, frozenset[int]] = {
    "ctrl": frozenset({0x11, 0xA2, 0xA3}),
    "alt": frozenset({0x12, 0xA4, 0xA5}),
    "shift": frozenset({0x10, 0xA0, 0xA1}),
    "win": frozenset({0x5B, 0x5C}),
}
_MOD_ALIASES = {"control": "ctrl", "menu": "alt", "option": "alt", "meta": "win", "super": "win"}
_ALL_MOD_VKS: frozenset[int] = frozenset().union(*_MOD_VKS.values())

# Автоповтор зажатой клавиши идёт не реже раза в секунду (задержка автоповтора Windows —
# до 1 с). Повторное «нажатие» ptt после паузы больше этой (с запасом) — новое нажатие,
# а отпускание прежнего хук не увидел.
LOST_UP_MS = 3000


def key_vk(name: str) -> int:
    n = name.strip().lower()
    if n.startswith("vk:"):
        try:
            vk = int(n[3:], 0)
        except ValueError:
            raise ValueError(tr("hk.bad_vk", name=repr(name))) from None
        if not 0 < vk < 0xFF:
            raise ValueError(tr("hk.vk_range", name=repr(name)))
        return vk
    if n not in KEY_NAMES:
        raise ValueError(tr("hk.unknown_key", name=repr(name), keys=", ".join(KEY_NAMES)))
    return KEY_NAMES[n]


@dataclass(frozen=True)
class Chord:
    mods: frozenset[str]   # ctrl / alt / shift / win
    vk: int


def parse_chord(spec: str) -> Chord:
    """«ctrl+alt+shift+v» → Chord. Нужен хотя бы один модификатор и ровно одна основная клавиша."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+")]
    if not spec.strip() or any(not p for p in parts):
        raise ValueError(tr("hk.chord_example", spec=spec))
    mods: set[str] = set()
    main: int | None = None
    for p in parts:
        p = _MOD_ALIASES.get(p, p)
        if p in _MOD_VKS:
            mods.add(p)
            continue
        if main is not None:
            raise ValueError(tr("hk.chord_two_main", spec=spec))
        if len(p) == 1 and ("a" <= p <= "z" or "0" <= p <= "9"):
            main = ord(p.upper())
        elif p in _CHORD_KEYS:
            main = _CHORD_KEYS[p]
        else:
            main = key_vk(p)
    if main is None:
        raise ValueError(tr("hk.chord_no_main", spec=spec))
    if main in _ALL_MOD_VKS:
        raise ValueError(tr("hk.chord_main_mod", spec=spec))
    if not mods:
        raise ValueError(tr("hk.chord_no_mod", spec=spec))
    return Chord(frozenset(mods), main)


class Kind(Enum):
    START = auto()       # начать запись (ещё не ясно, удержание это или тап)
    STOP = auto()        # закончить запись → распознать и вставить
    CANCEL = auto()      # пользователь отменил (Esc) — показать «отменено»
    DISCARD = auto()     # ложный старт (тап, сочетание клавиш) — выбросить молча
    HANDS_FREE = auto()  # запись продолжается в режиме «без рук»
    BROADCAST = auto()   # результат этой записи уйдёт во все RDP-сессии
    COMMAND = auto()     # эта запись — инструкция командного режима
    INSERT_LAST = auto()  # сочетание «Вставить последнее»
    COPY_LAST = auto()    # сочетание «Скопировать последнее»


@dataclass(frozen=True)
class Action:
    kind: Kind


class _S(Enum):
    IDLE = auto()
    HELD = auto()          # ptt зажат, идёт запись
    HANDS_FREE = auto()    # запись без рук
    HF_CHORD = auto()      # «без рук», ptt снова зажат: стоп по отпусканию, если это не чужое сочетание
    MOUSE_STOP = auto()    # «без рук», нажата кнопка мыши: стоп по её отпусканию
    WAIT_UP = auto()       # ждём отпускания ptt, всё игнорируем


# Кнопки мыши для диктовки (настройка mouse_button)
MOUSE_BUTTONS = ("middle", "x1", "x2")


KeyGroup = frozenset[int]
KeySpec = int | KeyGroup

_GENERIC_MODS = frozenset(_MOD_VKS)


def parse_keyset(spec: str) -> tuple[KeyGroup, ...]:
    """Клавиша или сочетание клавиш диктовки → группы VK.

    «rctrl» → ({правый Ctrl},); «ctrl+alt» → ({левый и правый Ctrl}, {левый и правый Alt}).
    Часть — имя из KEY_NAMES, vk:0x.. или общий модификатор ctrl / alt / shift / win
    (подходит любая из сторон). Сочетание «зажато», когда зажато по клавише из каждой группы."""
    parts = [p.strip().lower() for p in spec.replace(" ", "").split("+")]
    if not spec.strip() or any(not p for p in parts):
        raise ValueError(tr("hk.key_example", spec=spec))
    if len(parts) > 3:
        raise ValueError(tr("hk.max_three", spec=spec))
    groups: list[KeyGroup] = []
    for p in parts:
        p = _MOD_ALIASES.get(p, p)
        g = _MOD_VKS[p] if p in _MOD_VKS else frozenset({key_vk(p)})
        if any(g & other for other in groups):
            raise ValueError(tr("hk.repeated", spec=spec, key=p))
        groups.append(g)
    return tuple(groups)


def parse_ptt(spec: str) -> tuple[KeyGroup, ...]:
    """Клавиша диктовки: как parse_keyset, но не одиночный общий модификатор — с «любым Ctrl»
    каждое Ctrl+C и Ctrl+клик начинали бы запись."""
    groups = parse_keyset(spec)
    name = _MOD_ALIASES.get(spec.strip().lower(), spec.strip().lower())
    if len(groups) == 1 and name in _GENERIC_MODS:
        raise ValueError(tr("hk.lone_mod", spec=spec, name=name))
    return groups


def key_group(spec: str) -> KeyGroup:
    """Одна клавиша (модификатор рассылки/команды, отмена): «rshift», «shift», «esc»."""
    groups = parse_keyset(spec)
    if len(groups) != 1:
        raise ValueError(tr("hk.single_key", spec=spec))
    return groups[0]


# Названия клавиш для сообщений — как в интерфейсе (keyLabel в ui/src/lib/keys.ts);
# «Правый Ctrl», «Пробел» и «Клавиша 0x..» зависят от языка — каталог key.*
_KEY_TITLES: dict[str, str] = {
    "ctrl": "Ctrl", "alt": "Alt", "shift": "Shift", "win": "Win", "esc": "Esc",
    "capslock": "Caps Lock", "scrolllock": "Scroll Lock", "pause": "Pause", "insert": "Insert",
    "enter": "Enter", "tab": "Tab", "backspace": "Backspace", "delete": "Delete",
    "home": "Home", "end": "End", "pageup": "Page Up", "pagedown": "Page Down",
    "left": "←", "up": "↑", "right": "→", "down": "↓",
}
_LOCALIZED_KEYS = frozenset({"rctrl", "lctrl", "rshift", "lshift", "ralt", "lalt", "rwin",
                             "lwin", "space"})


def key_label(spec: str) -> str:
    """Клавиша или сочетание из настроек — как в интерфейсе: «ctrl+win» → «Ctrl + Win»,
    «rctrl» → «Right Ctrl» / «Правый Ctrl», «f13» → «F13», «vk:0x41» → «Key 0x41»."""
    parts = [p.strip().lower() for p in (spec or "").split("+") if p.strip()]
    if not parts:
        return spec
    out: list[str] = []
    for p in parts:
        p = _MOD_ALIASES.get(p, p)
        if p in _KEY_TITLES:
            out.append(_KEY_TITLES[p])
        elif p in _LOCALIZED_KEYS:
            out.append(tr(f"key.{p}"))
        elif p.startswith("vk:"):
            out.append(tr("key.vk", code=p[3:]))
        elif len(p) <= 3 and p.isalnum():
            out.append(p.upper())                  # f13, a, 7
        else:
            out.append(p)
    return " + ".join(out)


def _as_groups(ptt: "KeySpec | tuple[KeyGroup, ...]") -> tuple[KeyGroup, ...]:
    if isinstance(ptt, int):
        return (frozenset({ptt}),)
    if isinstance(ptt, frozenset):
        return (ptt,)
    return tuple(ptt)


def _as_group(k: KeySpec) -> KeyGroup:
    return frozenset({k}) if isinstance(k, int) else k


_MASK_VKS: frozenset[int] = _MOD_VKS["alt"] | _MOD_VKS["win"]
_WIN_VKS: frozenset[int] = frozenset({0x5B, 0x5C})
# mstsc вбрасывает копии модификаторов с этой меткой (синхронизация с удалённой машиной)
MSTSC_SYNC_EXTRA = 0x4321DCBA
_MENU_MASK_VK = 0xE8  # не назначенная клавиша: ей «маскируют» отпускание Alt/Win


class GestureMachine:
    """Автомат жестов. После каждого ``key_down``/``key_up`` хук читает:
    ``swallow`` — не передавать это событие приложениям;
    ``replay`` — VK, чьи нажатия нужно вбросить вместо проглоченного события (чужое
    сочетание вроде Ctrl+Alt+Shift+S: проглоченный Shift возвращается приложению);
    ``mask`` — перед отпусканием Alt/Win, участвовавших в жесте, вбросить нейтральную
    клавишу (иначе откроется меню окна или «Пуск»).
    ``presses`` — счётчик нажатий пользователя (режим ИИ «сначала сырой», командный режим).

    Клавиша диктовки (ptt) — одна клавиша или сочетание (``parse_keyset``): жест начинается,
    когда сочетание зажато целиком, и заканчивается, когда отпущена любая его клавиша.
    Потокобезопасен: хук зовёт key_*, UI — configure/reset из других потоков."""

    def __init__(self, ptt: "KeySpec | tuple[KeyGroup, ...]", broadcast_mod: KeySpec,
                 cancel: KeySpec, tap_max_ms: int = 250, double_tap_ms: int = 400,
                 command_mod: KeySpec | None = None,
                 insert_last: Chord | None = None, copy_last: Chord | None = None) -> None:
        self._lock = threading.Lock()
        self.state = _S.IDLE
        self._held: set[int] = set()
        self._swallowed: set[int] = set()
        self._seen: dict[int, int] = {}    # последнее нажатие/автоповтор клавиши (время хука)
        self._mask_vks: set[int] = set()   # Alt/Win жеста: их отпускание маскируем
        self._down_t = 0
        self._last_tap_up: int | None = None
        self._broadcast = False
        self._command = False
        self._mouse_held = False
        self._by_mouse = False     # идущий жест начат кнопкой мыши
        self.swallow = False
        self.replay: list[int] = []
        self.mask = False
        self.presses = 0
        self._chords: list[tuple[Kind, Chord]] = []
        self._apply(ptt, broadcast_mod, cancel, tap_max_ms, double_tap_ms, command_mod,
                    insert_last, copy_last)

    def _apply(self, ptt: "KeySpec | tuple[KeyGroup, ...]", broadcast_mod: KeySpec,
               cancel: KeySpec, tap_max_ms: int, double_tap_ms: int,
               command_mod: KeySpec | None, insert_last: Chord | None,
               copy_last: Chord | None) -> None:
        self.ptt_groups = _as_groups(ptt)
        self.ptt_vks: KeyGroup = frozenset().union(*self.ptt_groups)
        self.bmod = _as_group(broadcast_mod)
        self.cancel = _as_group(cancel)
        self.cmod = None if command_mod is None else _as_group(command_mod)
        self._gesture_mods: KeyGroup = self.bmod | (self.cmod or frozenset())
        self.tap_max, self.double_tap = tap_max_ms, double_tap_ms
        pairs = ((Kind.INSERT_LAST, insert_last), (Kind.COPY_LAST, copy_last))
        self._chords = [(k, c) for k, c in pairs if c is not None]

    def configure(self, ptt: "KeySpec | tuple[KeyGroup, ...]", broadcast_mod: KeySpec,
                  cancel: KeySpec, tap_max_ms: int, double_tap_ms: int,
                  command_mod: KeySpec | None = None,
                  insert_last: Chord | None = None, copy_last: Chord | None = None) -> None:
        """Новые хоткеи на лету (из настроек). Незавершённый жест сбрасывается."""
        with self._lock:
            self._apply(ptt, broadcast_mod, cancel, tap_max_ms, double_tap_ms, command_mod,
                        insert_last, copy_last)
            self.state = _S.IDLE
            self._last_tap_up = None

    # ---------- события клавиатуры ----------

    def is_held(self, vk: int) -> bool:
        """Пользователь сейчас держит клавишу vk (хук видел нажатие и не видел отпускания)."""
        with self._lock:
            return vk in self._held

    def note_input(self) -> None:
        """Ввод, который мимо автомата жестов (чужой программный ввод: экранная клавиатура,
        AutoHotkey, голосовой ввод Windows), — тоже «нажатие» для счётчика presses."""
        with self._lock:
            self.presses += 1

    def _active(self) -> bool:
        return all(g & self._held for g in self.ptt_groups)

    def interrupt(self) -> None:
        """Прервать жест снаружи (лимит длительности, смена микрофона): запись закончена.
        Если ptt ещё зажат — ждём его отпускания, иначе — покой."""
        with self._lock:
            self.state = _S.WAIT_UP if self._active() else _S.IDLE
            self._last_tap_up = None

    def desktop_switched(self, t: int | None = None) -> list[Action]:
        """Сменился рабочий стол (Ctrl+Alt+Del, UAC, блокировка): отпускания, сделанные
        там, хук не увидит. Забываем зажатые и проглоченные клавиши. Удержание ptt:
        короткое (сам Ctrl+Alt+Del) — тихо выбрасываем, долгое — отмена; «без рук» — пишем
        дальше."""
        with self._lock:
            self._held.clear()
            self._swallowed.clear()
            self._seen.clear()
            self._mask_vks.clear()
            self._last_tap_up = None
            st = self.state
            if st is _S.HELD:
                self.state = _S.IDLE
                short = t is not None and t - self._down_t < self.tap_max
                return [Action(Kind.DISCARD if short else Kind.CANCEL)]
            if st in (_S.HF_CHORD, _S.MOUSE_STOP):
                self.state = _S.HANDS_FREE
            elif st is _S.WAIT_UP:
                self.state = _S.IDLE
            self._mouse_held = False
            return []

    # ---------- кнопка мыши и клик по пилюле ----------

    def mouse_down(self, t: int) -> list[Action]:
        """Кнопка мыши диктовки нажата. Держать — запись, пока держите; короткое нажатие —
        «без рук»; в «без рук» нажатие — стоп (по отпусканию)."""
        with self._lock:
            self.presses += 1
            self._mouse_held = True
            st = self.state
            if st is _S.IDLE:
                acts = self._begin(t)
                self._by_mouse = True
                return acts
            if st is _S.HANDS_FREE:
                self.state = _S.MOUSE_STOP
                self._broadcast = bool(self.bmod & self._held)
                self._command = self.cmod is not None and bool(self.cmod & self._held)
            return []  # клавиатура уже держит жест — кнопку не трогаем

    def mouse_up(self, t: int) -> list[Action]:
        with self._lock:
            if not self._mouse_held:
                return []
            self._mouse_held = False
            st = self.state
            if st is _S.MOUSE_STOP:
                self.state = _S.IDLE
                acts = [Action(Kind.BROADCAST)] if self._broadcast else []
                if self._command:
                    acts.append(Action(Kind.COMMAND))
                return acts + [Action(Kind.STOP)]
            if st is _S.HELD and self._by_mouse:
                self._by_mouse = False
                if t - self._down_t >= self.tap_max:
                    self.state = _S.IDLE
                    return [Action(Kind.STOP)]
                self.state = _S.HANDS_FREE  # короткое нажатие — пишем «без рук»
                return [Action(Kind.HANDS_FREE)]
            return []

    def external_toggle(self, t: int) -> list[Action]:
        """Клик по пилюле: покой → запись «без рук», «без рук» → стоп. Пока жест держат
        клавишами или мышью, клик ничего не делает."""
        with self._lock:
            if self.state is _S.IDLE:
                self._down_t = t
                self.state = _S.HANDS_FREE
                self._broadcast = self._command = self._by_mouse = False
                return [Action(Kind.START), Action(Kind.HANDS_FREE)]
            if self.state is _S.HANDS_FREE:
                self.state = _S.IDLE
                return [Action(Kind.STOP)]
            return []

    def _mods_held(self) -> frozenset[str]:
        return frozenset(m for m, vks in _MOD_VKS.items() if vks & self._held)

    def _swallow(self, vk: int) -> None:
        self.swallow = True
        self._swallowed.add(vk)

    def _note_mask(self) -> None:
        self._mask_vks |= self._held & self.ptt_vks & _MASK_VKS

    def _begin(self, t: int) -> list[Action]:
        self._down_t = t
        self.state = _S.HELD
        self._by_mouse = False
        self._note_mask()
        acts = [Action(Kind.START)]
        self._broadcast = bool(self.bmod & self._held)
        self._command = self.cmod is not None and bool(self.cmod & self._held)
        if self._broadcast:
            acts.append(Action(Kind.BROADCAST))
        if self._command:
            acts.append(Action(Kind.COMMAND))
        return acts

    def _drop_stale(self, t: int) -> bool:
        """Сочетание сложилось из клавиши, по которой давно не было событий: её отпускание
        потерялось (окно администратора, чужой хук). Такие клавиши забываем. True — были."""
        stale: set[int] = set()
        for g in self.ptt_groups:
            hv = g & self._held
            if hv and all(t - self._seen.get(v, t) > LOST_UP_MS for v in hv):
                stale |= hv
        if stale:
            log.warning("Клавиши %s давно без событий — их отпускание потерялось",
                        sorted(hex(v) for v in stale))
            self._held -= stale
        return bool(stale)

    def key_down(self, vk: int, t: int) -> list[Action]:
        with self._lock:
            self.swallow = False
            self.replay = []
            self.mask = False
            self.presses += 1
            repeat = vk in self._held
            lost: list[Action] = []
            if vk in self.ptt_vks and repeat and t - self._seen.get(vk, t) > LOST_UP_MS:
                lost = self._lost_release()
                self._held.discard(vk)  # это новое нажатие, а не автоповтор
                repeat = False
            self._seen[vk] = t
            if repeat:
                self.swallow = vk in self._swallowed  # автоповтор проглоченной — тоже глотаем
                return []
            self._swallowed.discard(vk)  # новое нажатие: прежнее отпускание было, хоть и мимо нас
            was = self._active()
            self._held.add(vk)
            became = not was and self._active()
            if became and self._drop_stale(t):
                became = self._active()
            chord = self._match_chord(vk)
            if chord is not None:
                return lost + chord
            acts = self._down(vk, t, became)
            if became and vk in _WIN_VKS and self.state in (_S.HELD, _S.HF_CHORD):
                # Win, которым сложилось сочетание диктовки, никому не отдаём: в RDP он ушёл бы
                # на удалённую машину («Пуск» там), локально — открыл бы «Пуск»
                self._swallow(vk)
            return lost + acts

    def _lost_release(self) -> list[Action]:
        """Отпускание ptt не дошло до хука: прежняя запись отменяется, жест — с нуля."""
        log.warning("Отпускание клавиши диктовки потерялось — считаю это новым нажатием")
        acts = ([Action(Kind.CANCEL)]
                if self.state in (_S.HELD, _S.HANDS_FREE, _S.HF_CHORD, _S.MOUSE_STOP) else [])
        self.state = _S.IDLE
        self._last_tap_up = None
        return acts

    def _match_chord(self, vk: int) -> list[Action] | None:
        if vk in _ALL_MOD_VKS or not self._chords:
            return None
        mods = self._mods_held()
        for kind, ch in self._chords:
            if vk == ch.vk and mods == ch.mods:
                self._swallow(vk)
                # Основная клавиша проглочена, а Alt/Win сочетания дошли до системы: их
                # отпускание маскируем (vkE8), иначе Alt откроет меню окна, Win — «Пуск»
                # (Win+Alt+V; у Ctrl+Alt+Shift+V меню и так гасит Ctrl)
                self._mask_vks |= self._held & _MASK_VKS
                acts = [Action(kind)]
                if self.state is _S.HELD:  # ptt входил в сочетание — это не диктовка
                    self.state = _S.WAIT_UP
                    self._last_tap_up = None
                    acts.insert(0, Action(Kind.DISCARD))
                elif self.state is _S.HF_CHORD:
                    self.state = _S.HANDS_FREE
                return acts
        return None

    def _foreign_key(self, vk: int) -> None:
        """Во время жеста нажата посторонняя клавиша — это чужое сочетание (Ctrl+Alt+Shift+S).
        Проглоченные Shift/Win возвращаем приложению: вбрасываем их нажатия и саму клавишу."""
        mods = sorted(self._swallowed & self._held
                      & (self._gesture_mods | (self.ptt_vks & _WIN_VKS)))
        if mods:
            self.swallow = True
            self.replay = [*mods, vk]
            self._swallowed -= set(mods)  # их физическое отпускание пусть дойдёт

    def _end_held(self) -> None:
        """Жест HELD прерван (Esc, чужое сочетание). Клавиатурный ждёт отпускания ptt;
        начатый кнопкой мыши — сразу в покой: ptt никто не держит, и WAIT_UP не кончился бы
        никогда (кнопка мыши в WAIT_UP ничего не делает)."""
        if self._by_mouse:
            self._by_mouse = False
            self.state = _S.IDLE
        else:
            self.state = _S.WAIT_UP

    def _down(self, vk: int, t: int, became: bool) -> list[Action]:
        st = self.state
        if st is _S.IDLE:
            return self._begin(t) if became else []
        if st is _S.MOUSE_STOP:
            if vk in self.cancel:
                self._swallow(vk)
                self.state = _S.IDLE
                self._mouse_held = False
                return [Action(Kind.CANCEL)]
            return []
        if st is _S.HELD and self._by_mouse and became:
            return []  # кнопкой мыши уже пишем; сложившийся Ctrl+Alt ничего не меняет
        if st in (_S.HELD, _S.HF_CHORD):
            if vk in self.ptt_vks:
                self._note_mask()
                return []  # вторая сторона того же модификатора (левый и правый Ctrl)
            if vk in self.cancel:
                # Esc глотаем: с зажатым Ctrl система получила бы Ctrl+Esc и открыла «Пуск»
                self._swallow(vk)
                self._end_held()
                return [Action(Kind.CANCEL)]
            if vk in self.bmod:
                self._swallow(vk)
                if not self._broadcast:
                    self._broadcast = True
                    return [Action(Kind.BROADCAST)] if st is _S.HELD else []
                return []
            if self.cmod is not None and vk in self.cmod:
                self._swallow(vk)
                if not self._command:
                    self._command = True
                    return [Action(Kind.COMMAND)] if st is _S.HELD else []
                return []
            self._foreign_key(vk)
            if st is _S.HF_CHORD:  # сочетание приложения посреди «без рук» — пишем дальше
                self.state = _S.HANDS_FREE
                return []
            # сочетание (Ctrl+C и т. п.), а не диктовка
            self._end_held()
            self._last_tap_up = None
            return [Action(Kind.DISCARD)]
        if st is _S.HANDS_FREE:
            if vk in self.cancel:
                self._swallow(vk)  # Esc отменяет запись, а не закрывает диалог в приложении
                self.state = _S.IDLE
                return [Action(Kind.CANCEL)]
            if became:
                # Стоп — по отпусканию: если до него нажмут клавишу, это сочетание приложения.
                # Shift/Win считаются, только если нажаты уже после ptt.
                self.state = _S.HF_CHORD
                self._broadcast = self._command = False
                self._note_mask()
            return []  # в режиме «без рук» можно печатать руками
        return []

    def key_up(self, vk: int, t: int) -> list[Action]:
        with self._lock:
            self.replay = []
            self.swallow = vk in self._swallowed
            self._swallowed.discard(vk)
            self.mask = vk in self._mask_vks and not self.swallow
            self._mask_vks.discard(vk)
            was = self._active()
            self._held.discard(vk)
            if vk not in self.ptt_vks or not was or self._active():
                return []  # сочетание ptt этим отпусканием не «распалось»
            st = self.state
            if st is _S.WAIT_UP:
                self.state = _S.IDLE
                return []
            if st is _S.HELD and self._by_mouse:
                return []  # запись держит кнопка мыши — отпускание клавиш её не касается
            if st is _S.HF_CHORD:
                self.state = _S.IDLE
                acts = [Action(Kind.BROADCAST)] if self._broadcast else []
                if self._command:
                    acts.append(Action(Kind.COMMAND))
                return acts + [Action(Kind.STOP)]
            if st is _S.HELD:
                dur = t - self._down_t
                if dur >= self.tap_max:
                    self.state = _S.IDLE
                    self._last_tap_up = None
                    return [Action(Kind.STOP)]
                # короткий тап
                prev = self._last_tap_up
                if prev is not None and self._down_t - prev <= self.double_tap:
                    self.state = _S.HANDS_FREE
                    self._last_tap_up = None
                    return [Action(Kind.HANDS_FREE)]
                self.state = _S.IDLE
                self._last_tap_up = t
                return [Action(Kind.DISCARD)]
            return []

    def reset(self) -> None:
        """Сбросить жест (пауза, отмена). Проглоченные клавиши помним: их отпускание
        тоже не должно дойти до приложений."""
        with self._lock:
            self.state = _S.IDLE
            self._held.clear()
            self._last_tap_up = None


def _send_keys(events: list[tuple[int, int]]) -> None:
    """Вбросить (vk, флаги) одним SendInput, с меткой своего ввода RUPOR_MAGIC."""
    arr = (w.INPUT * len(events))()
    for i, (vk, flags) in enumerate(events):
        arr[i].type = w.INPUT_KEYBOARD
        arr[i].ki.wVk = vk
        arr[i].ki.wScan = int(w.user32.MapVirtualKeyW(vk, 0)) & 0xFF
        arr[i].ki.dwFlags = flags | (w.KEYEVENTF_EXTENDEDKEY if vk in _EXTENDED else 0)
        arr[i].ki.time = 0
        arr[i].ki.dwExtraInfo = w.RUPOR_MAGIC
    w.user32.SendInput(len(events), arr, ctypes.sizeof(w.INPUT))


# правые модификаторы, Win, стрелки и т. п. — «расширенные» клавиши
_EXTENDED = frozenset({0xA3, 0xA5, 0x5B, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27,
                       0x28, 0x2D, 0x2E, 0x6F, 0x90})


def send_menu_mask() -> None:
    """Нейтральная клавиша vkE8 нажата и отпущена: Alt/Win, отпускаемые после жеста
    диктовки, уже не «одиночные» — меню окна и «Пуск» не откроются (приём AutoHotkey)."""
    _send_keys([(_MENU_MASK_VK, 0), (_MENU_MASK_VK, w.KEYEVENTF_KEYUP)])


def send_replay(vks: list[int]) -> None:
    """Вернуть приложению проглоченные модификаторы и клавишу чужого сочетания."""
    _send_keys([(vk, 0) for vk in vks])


# ---------- хук Windows ----------

WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP, WM_SYSKEYDOWN, WM_SYSKEYUP = 0x100, 0x101, 0x104, 0x105
WM_QUIT = 0x0012
WM_APP_REHOOK = 0x8001
WM_APP_MOUSEHOOK = 0x8002
WH_MOUSE_LL = 14
WM_MBUTTONDOWN, WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP = 0x207, 0x208, 0x20B, 0x20C
_MOUSE_MSGS = frozenset({WM_MBUTTONDOWN, WM_MBUTTONUP, WM_XBUTTONDOWN, WM_XBUTTONUP})
_MOUSE_DOWNS = frozenset({WM_MBUTTONDOWN, WM_XBUTTONDOWN})
EVENT_SYSTEM_FOREGROUND = 0x0003
EVENT_SYSTEM_DESKTOPSWITCH = 0x0020
WINEVENT_OUTOFCONTEXT = 0x0000
_ALTGR_FAKE_CTRL_SCAN = 0x21D  # левый Ctrl, который Windows шлёт вместе с AltGr


class KeyboardHook:
    """Поток с циклом сообщений и LL-хуком. on_actions вызывается из потока хука —
    обработчик обязан быть мгновенным (просто положить в очередь)."""

    def __init__(self, machine: GestureMachine, on_actions: Callable[[list[Action]], None],
                 rehook_when: Callable[[int], bool] | None = None) -> None:
        self.machine = machine
        self._on_actions = on_actions
        self._rehook_when = rehook_when
        self._thread: threading.Thread | None = None
        self._tid = 0
        self._hook: int | None = None
        self._ready = threading.Event()
        # ссылки держим, иначе колбэки соберёт GC и процесс упадёт
        self._proc = w.HOOKPROC(self._hook_proc)
        self._mproc = w.HOOKPROC(self._mouse_proc)
        self._mhook: int | None = None
        self.mouse_button = ""  # "" — мышью не диктуем; middle / x1 / x2
        self._winproc = w.WINEVENTPROC(self._win_event)
        self._deskproc = w.WINEVENTPROC(self._desktop_event)
        self.paused = False
        self._alive_t = -1e9

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="rupor-hook", daemon=True)
        self._thread.start()
        self._ready.wait(5)
        if self._rehook_when:
            threading.Thread(target=self._rdp_watch, name="rupor-rehook", daemon=True).start()

    def _rdp_watch(self) -> None:
        """mstsc ставит свой LL-хук, когда его окно получает фокус, и тогда он в цепочке
        первый: Win уходит на удалённую машину, Ctrl — копией. Пока активно окно RDP,
        раз в секунду перевешиваем свой хук, чтобы клавиши диктовки ловил Рупор."""
        was_rdp = False
        while self.running:
            time.sleep(1.0)
            try:
                hwnd = w.user32.GetForegroundWindow()
                rdp = bool(hwnd and self._rehook_when and self._rehook_when(hwnd))
            except Exception:
                rdp = False
            if rdp and not self.paused:
                if not was_rdp:
                    log.info("Активно окно RDP — держу хук Рупора первым в цепочке")
                w.user32.PostThreadMessageW(self._tid, WM_APP_REHOOK, 0, 0)
            was_rdp = rdp

    def stop(self) -> None:
        if self._tid:
            w.user32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)

    def set_mouse_button(self, button: str) -> None:
        """Кнопка мыши для диктовки ("" — нет). Хук мыши ставится только когда она выбрана:
        без неё Python не обрабатывает каждое движение мыши."""
        self.mouse_button = button
        if self._tid:
            w.user32.PostThreadMessageW(self._tid, WM_APP_MOUSEHOOK, 0, 0)

    def _install(self) -> None:
        # Сначала новый хук, потом снимаем старый: без зазора, в который проскочит нажатие
        # (на миг хуков два — повтор события автомат примет за автоповтор)
        old = self._hook
        new = w.user32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc,
                                         w.kernel32.GetModuleHandleW(None), 0)
        if not new:
            raise OSError(tr("err.winapi", func="SetWindowsHookEx",
                             code=ctypes.get_last_error()))
        self._hook = new
        if old:
            w.user32.UnhookWindowsHookEx(old)
        self._install_mouse()

    def _install_mouse(self) -> None:
        if self._mhook:
            w.user32.UnhookWindowsHookEx(self._mhook)
            self._mhook = None
        if self.mouse_button:
            self._mhook = w.user32.SetWindowsHookExW(WH_MOUSE_LL, self._mproc,
                                                     w.kernel32.GetModuleHandleW(None), 0)
            if self._mhook:
                log.info("Хук мыши установлен: кнопка %s", self.mouse_button)
            else:
                log.error("Хук мыши не установлен: ошибка %s", ctypes.get_last_error())

    def _run(self) -> None:
        self._tid = w.kernel32.GetCurrentThreadId()
        self._install()
        win_hook = None
        if self._rehook_when:
            win_hook = w.user32.SetWinEventHook(EVENT_SYSTEM_FOREGROUND, EVENT_SYSTEM_FOREGROUND,
                                                None, self._winproc, 0, 0, WINEVENT_OUTOFCONTEXT)
        # Ctrl+Alt+Del, UAC, блокировка: отпускания на другом рабочем столе хук не видит
        desk_hook = w.user32.SetWinEventHook(EVENT_SYSTEM_DESKTOPSWITCH, EVENT_SYSTEM_DESKTOPSWITCH,
                                             None, self._deskproc, 0, 0, WINEVENT_OUTOFCONTEXT)
        self._ready.set()
        log.info("Хук клавиатуры установлен")
        msg = wt.MSG()
        while w.user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_APP_REHOOK:
                self._install()
                continue
            if msg.message == WM_APP_MOUSEHOOK:
                self._install_mouse()
                continue
            w.user32.TranslateMessage(ctypes.byref(msg))
            w.user32.DispatchMessageW(ctypes.byref(msg))
        if win_hook:
            w.user32.UnhookWinEvent(win_hook)
        if desk_hook:
            w.user32.UnhookWinEvent(desk_hook)
        if self._hook:
            w.user32.UnhookWindowsHookEx(self._hook)
        if self._mhook:
            w.user32.UnhookWindowsHookEx(self._mhook)
        log.info("Хук клавиатуры снят")

    def _win_event(self, _h: int, _ev: int, hwnd: int, _o: int, _c: int, _t: int, _ms: int) -> None:
        # mstsc ставит свой LL-хук при активации окна и «съедает» клавиши. Перевешиваем свой
        # сразу и ещё раз чуть позже (mstsc ставит хук после события смены окна);
        # дальше держит _rdp_watch.
        try:
            if hwnd and self._rehook_when and self._rehook_when(hwnd):
                w.user32.PostThreadMessageW(self._tid, WM_APP_REHOOK, 0, 0)
                t = threading.Timer(0.3, w.user32.PostThreadMessageW,
                                    (self._tid, WM_APP_REHOOK, 0, 0))
                t.daemon = True
                t.start()
        except Exception:
            log.exception("win event")

    def _desktop_event(self, _h: int, _ev: int, _hwnd: int, _o: int, _c: int, _t: int,
                       ms: int) -> None:
        try:
            acts = self.machine.desktop_switched(ms)  # dwmsEventTime — те же мс, что kb.time
            log.info("Смена рабочего стола — зажатые клавиши сброшены")
            if acts:
                self._on_actions(acts)
        except Exception:
            log.exception("desktop switch")

    def _alive(self, vk: int, injected: bool) -> None:
        """Раз в минуту — в журнал, что хук жив и видит клавиши жеста (диагностика)."""
        now = time.monotonic()
        if vk in self.machine.ptt_vks and now - self._alive_t > 60:
            self._alive_t = now
            log.info("Хук видит клавиши диктовки (%s ввод)",
                     "программный — через удалённый доступ/утилиту" if injected else "физический")

    def _mouse_proc(self, code: int, wparam: int, lparam: int) -> int:
        # Движения мыши — сразу дальше: никакой работы на каждое перемещение
        if code == 0 and wparam in _MOUSE_MSGS and not self.paused and self.mouse_button:
            try:
                ms = ctypes.cast(lparam, ctypes.POINTER(w.MSLLHOOKSTRUCT)).contents
                if ms.dwExtraInfo != w.RUPOR_MAGIC:
                    if wparam in (WM_MBUTTONDOWN, WM_MBUTTONUP):
                        button = "middle"
                    else:
                        button = "x1" if (ms.mouseData >> 16) & 0xFFFF == 1 else "x2"
                    if button == self.mouse_button:
                        m = self.machine
                        acts = (m.mouse_down(ms.time) if wparam in _MOUSE_DOWNS
                                else m.mouse_up(ms.time))
                        if acts:
                            self._on_actions(acts)
                        return 1  # кнопку глотаем: браузер не уйдёт «Назад», ссылка не откроется
            except Exception:
                log.exception("Ошибка в хуке мыши")
        return int(w.user32.CallNextHookEx(None, code, wparam, lparam))

    def _hook_proc(self, code: int, wparam: int, lparam: int) -> int:
        if code == 0 and not self.paused:
            try:
                kb = ctypes.cast(lparam, ctypes.POINTER(w.KBDLLHOOKSTRUCT)).contents
                # Свой ввод (метка RUPOR_MAGIC: печать, маска, возврат клавиш) пропускаем.
                # Чужой программный ввод — это тоже пользователь: клавиатура через AnyDesk,
                # RustDesk, Logitech Flow, Mouse Without Borders, переназначение клавиш.
                if kb.dwExtraInfo in (w.RUPOR_MAGIC, MSTSC_SYNC_EXTRA):
                    pass  # свой ввод и копии модификаторов, которые вбрасывает mstsc
                elif not (kb.vkCode == 0xA2 and kb.scanCode == _ALTGR_FAKE_CTRL_SCAN):
                    self._alive(kb.vkCode, bool(kb.flags & w.LLKHF_INJECTED))
                    m = self.machine
                    if wparam in (WM_KEYDOWN, WM_SYSKEYDOWN):
                        acts = m.key_down(kb.vkCode, kb.time)
                    elif wparam in (WM_KEYUP, WM_SYSKEYUP):
                        acts = m.key_up(kb.vkCode, kb.time)
                    else:
                        acts = []
                    swallow, replay, mask = m.swallow, m.replay, m.mask
                    if replay:
                        send_replay(replay)  # чужое сочетание: вернуть проглоченные Shift/Win
                    if mask:
                        send_menu_mask()     # отпускание Alt/Win жеста не откроет меню/«Пуск»
                    if acts:
                        self._on_actions(acts)
                    if swallow:
                        return 1  # не отдаём клавишу приложениям
            except Exception:
                log.exception("Ошибка в хуке клавиатуры")
        return int(w.user32.CallNextHookEx(None, code, wparam, lparam))
