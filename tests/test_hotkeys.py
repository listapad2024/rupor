import pytest

from rupor.hotkeys import GestureMachine, Kind, key_vk, parse_chord

RCTRL, RSHIFT, ESC, C, A = 0xA3, 0xA1, 0x1B, ord("C"), ord("A")


def kinds(acts: list) -> list[Kind]:  # type: ignore[type-arg]
    return [a.kind for a in acts]


@pytest.fixture
def m() -> GestureMachine:
    return GestureMachine(RCTRL, RSHIFT, ESC, tap_max_ms=250, double_tap_ms=400)


def test_push_to_talk(m: GestureMachine) -> None:
    assert kinds(m.key_down(RCTRL, 1000)) == [Kind.START]
    assert kinds(m.key_down(RCTRL, 1100)) == []  # автоповтор
    assert kinds(m.key_up(RCTRL, 3000)) == [Kind.STOP]


def test_short_tap_is_discarded(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    assert kinds(m.key_up(RCTRL, 1100)) == [Kind.DISCARD]


def test_combo_is_not_dictation(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    assert kinds(m.key_down(C, 1400)) == [Kind.DISCARD]
    assert kinds(m.key_up(C, 1450)) == []
    assert kinds(m.key_up(RCTRL, 1600)) == []
    # после сочетания следующий тап не считается вторым тапом двойного
    m.key_down(RCTRL, 1700)
    assert kinds(m.key_up(RCTRL, 1750)) == [Kind.DISCARD]


def test_escape_cancels(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    assert kinds(m.key_down(ESC, 2000)) == [Kind.CANCEL]
    assert kinds(m.key_up(RCTRL, 2500)) == []


def test_double_tap_hands_free_then_tap_stops(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    m.key_up(RCTRL, 1080)
    assert kinds(m.key_down(RCTRL, 1250)) == [Kind.START]
    assert kinds(m.key_up(RCTRL, 1320)) == [Kind.HANDS_FREE]
    # руками можно печатать, запись не прерывается
    assert kinds(m.key_down(A, 2000)) == []
    m.key_up(A, 2050)
    assert kinds(m.key_down(RCTRL, 9000)) == []          # стоп — по отпусканию
    assert kinds(m.key_up(RCTRL, 9080)) == [Kind.STOP]
    assert kinds(m.key_down(RCTRL, 10000)) == [Kind.START]


def test_slow_double_tap_is_two_taps(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    m.key_up(RCTRL, 1080)
    m.key_down(RCTRL, 2000)
    assert kinds(m.key_up(RCTRL, 2080)) == [Kind.DISCARD]


def test_hands_free_escape(m: GestureMachine) -> None:
    for t in (1000, 1200):
        m.key_down(RCTRL, t)
        m.key_up(RCTRL, t + 50)
    assert kinds(m.key_down(ESC, 5000)) == [Kind.CANCEL]
    assert kinds(m.key_down(RCTRL, 6000)) == [Kind.START]


def test_broadcast_modifier_during_hold(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    assert kinds(m.key_down(RSHIFT, 1100)) == [Kind.BROADCAST]
    assert kinds(m.key_down(RSHIFT, 1150)) == []  # автоповтор
    m.key_up(RSHIFT, 1500)
    assert kinds(m.key_up(RCTRL, 3000)) == [Kind.STOP]


def test_broadcast_modifier_held_before(m: GestureMachine) -> None:
    m.key_down(RSHIFT, 900)
    assert kinds(m.key_down(RCTRL, 1000)) == [Kind.START, Kind.BROADCAST]


def test_hands_free_stop_with_broadcast(m: GestureMachine) -> None:
    for t in (1000, 1200):
        m.key_down(RCTRL, t)
        m.key_up(RCTRL, t + 50)
    assert kinds(m.key_down(RCTRL, 4100)) == []
    assert kinds(m.key_down(RSHIFT, 4200)) == []   # Shift после ptt — рассылка
    assert m.swallow is True
    assert kinds(m.key_up(RCTRL, 4300)) == [Kind.BROADCAST, Kind.STOP]


def test_hands_free_shift_held_before_ptt_is_not_broadcast(m: GestureMachine) -> None:
    for t in (1000, 1200):
        m.key_down(RCTRL, t)
        m.key_up(RCTRL, t + 50)
    m.key_down(RSHIFT, 4000)                      # Shift ещё до ptt — это чужое сочетание
    m.key_down(RCTRL, 4100)
    assert kinds(m.key_up(RCTRL, 4300)) == [Kind.STOP]


def test_other_keys_idle(m: GestureMachine) -> None:
    assert m.key_down(A, 1) == [] and m.key_up(A, 2) == [] and m.key_down(ESC, 3) == []


def test_key_names() -> None:
    assert key_vk("rctrl") == RCTRL and key_vk("F13") == 0x7C and key_vk("vk:0x91") == 0x91
    with pytest.raises(ValueError):
        key_vk("nope")


# ---------- командный режим, проглатывание модификаторов ----------

RALT, LALT, LCTRL, LSHIFT, V = 0xA5, 0xA4, 0xA2, 0xA0, ord("V")


@pytest.fixture
def mc() -> GestureMachine:
    return GestureMachine(RCTRL, RSHIFT, ESC, tap_max_ms=250, double_tap_ms=400,
                          command_mod=RALT,
                          insert_last=parse_chord("ctrl+alt+shift+v"),
                          copy_last=parse_chord("ctrl+alt+shift+c"))


def test_command_modifier_during_hold_is_swallowed(mc: GestureMachine) -> None:
    assert kinds(mc.key_down(RCTRL, 1000)) == [Kind.START]
    assert mc.swallow is False  # сам ptt приложениям отдаём
    assert kinds(mc.key_down(RALT, 1100)) == [Kind.COMMAND]
    assert mc.swallow is True
    assert kinds(mc.key_down(RALT, 1150)) == []  # автоповтор — тоже глотаем
    assert mc.swallow is True
    assert kinds(mc.key_up(RALT, 1500)) == []
    assert mc.swallow is True  # отпускание проглоченной клавиши
    assert kinds(mc.key_up(RCTRL, 3000)) == [Kind.STOP]
    assert mc.swallow is False


def test_broadcast_modifier_during_hold_is_swallowed(mc: GestureMachine) -> None:
    mc.key_down(RCTRL, 1000)
    assert kinds(mc.key_down(RSHIFT, 1100)) == [Kind.BROADCAST]
    assert mc.swallow is True
    mc.key_up(RCTRL, 2000)          # ptt отпущен раньше модификатора
    assert kinds(mc.key_up(RSHIFT, 2100)) == []
    assert mc.swallow is True


def test_modifier_held_before_ptt_is_not_swallowed(mc: GestureMachine) -> None:
    mc.key_down(RALT, 900)
    assert mc.swallow is False
    assert kinds(mc.key_down(RCTRL, 1000)) == [Kind.START, Kind.COMMAND]
    mc.key_up(RCTRL, 2000)
    mc.key_up(RALT, 2100)
    assert mc.swallow is False  # его нажатие дошло до приложения — отпускание тоже


def test_command_and_broadcast_together(mc: GestureMachine) -> None:
    mc.key_down(RCTRL, 1000)
    assert kinds(mc.key_down(RSHIFT, 1100)) == [Kind.BROADCAST]
    assert kinds(mc.key_down(RALT, 1200)) == [Kind.COMMAND]
    assert kinds(mc.key_up(RCTRL, 3000)) == [Kind.STOP]


def test_other_keys_not_swallowed(mc: GestureMachine) -> None:
    mc.key_down(RCTRL, 1000)
    assert kinds(mc.key_down(C, 1100)) == [Kind.DISCARD]
    assert mc.swallow is False
    mc.key_up(C, 1200)
    assert mc.swallow is False
    # в режиме «без рук» модификаторы не глотаются: ptt не удерживается
    for t in (2000, 2200):
        mc.key_down(RCTRL, t)
        mc.key_up(RCTRL, t + 50)
    assert kinds(mc.key_down(RALT, 3000)) == []
    assert mc.swallow is False


def test_hands_free_stop_with_command(mc: GestureMachine) -> None:
    for t in (1000, 1200):
        mc.key_down(RCTRL, t)
        mc.key_up(RCTRL, t + 50)
    mc.key_down(RCTRL, 4000)
    mc.key_down(RALT, 4100)
    assert kinds(mc.key_up(RALT, 4200)) == []
    assert kinds(mc.key_up(RCTRL, 4300)) == [Kind.COMMAND, Kind.STOP]


def test_machine_without_command_modifier(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    assert kinds(m.key_down(RALT, 1100)) == [Kind.DISCARD]  # просто сочетание


# ---------- сочетания «Вставить/Скопировать последнее» ----------

def test_insert_last_chord(mc: GestureMachine) -> None:
    for vk, t in ((LCTRL, 1), (LALT, 2), (LSHIFT, 3)):
        assert kinds(mc.key_down(vk, t)) == []
    assert kinds(mc.key_down(V, 4)) == [Kind.INSERT_LAST]
    assert mc.swallow is True
    mc.key_up(V, 5)
    assert mc.swallow is True
    for vk, t in ((LSHIFT, 6), (LALT, 7), (LCTRL, 8)):
        mc.key_up(vk, t)
        assert mc.swallow is False


def test_copy_last_chord_with_ptt_ctrl_discards_recording(mc: GestureMachine) -> None:
    assert kinds(mc.key_down(RCTRL, 1000)) == [Kind.START]   # правый Ctrl — это ptt
    assert kinds(mc.key_down(LALT, 1010)) == [Kind.DISCARD]  # сочетание, не диктовка
    mc.key_down(LSHIFT, 1020)
    assert kinds(mc.key_down(C, 1030)) == [Kind.COPY_LAST]
    assert mc.swallow is True
    assert kinds(mc.key_up(RCTRL, 1500)) == []


def test_chord_needs_exact_modifiers(mc: GestureMachine) -> None:
    mc.key_down(LCTRL, 1)
    mc.key_down(LALT, 2)
    assert kinds(mc.key_down(V, 3)) == []  # без Shift — не наше сочетание
    assert mc.swallow is False


def test_parse_chord() -> None:
    ch = parse_chord("Ctrl+Alt+Shift+V")
    assert ch.vk == V and ch.mods == frozenset({"ctrl", "alt", "shift"})
    assert parse_chord("win+f13").vk == 0x7C
    assert parse_chord("ctrl + space").vk == 0x20
    for bad in ("v", "ctrl", "ctrl+a+b", "ctrl+", "ctrl+nope", "ctrl+rshift"):
        with pytest.raises(ValueError):
            parse_chord(bad)


# ---------- счётчик нажатий, перенастройка на лету ----------

def test_presses_counter(mc: GestureMachine) -> None:
    assert mc.presses == 0
    mc.key_down(A, 1)
    mc.key_down(A, 2)  # автоповтор тоже считается: он печатает
    mc.key_up(A, 3)
    assert mc.presses == 2


def test_configure_on_the_fly(mc: GestureMachine) -> None:
    mc.key_down(RCTRL, 1000)
    F13 = 0x7C
    mc.configure(F13, LSHIFT, ESC, 300, 500, command_mod=None)
    assert mc.state.name == "IDLE"      # незавершённый жест сброшен
    mc.key_up(RCTRL, 1100)
    assert kinds(mc.key_down(RCTRL, 2000)) == []  # старый ptt больше не работает
    assert kinds(mc.key_down(F13, 3000)) == [Kind.START]
    assert kinds(mc.key_up(F13, 3200)) == [Kind.DISCARD]  # 200 мс < нового порога 300
    mc.key_down(LCTRL, 4000)
    mc.key_down(LALT, 4001)
    mc.key_down(LSHIFT, 4002)
    assert kinds(mc.key_down(V, 4003)) == []  # сочетания отключены


def test_key_vk_ranges() -> None:
    with pytest.raises(ValueError):
        key_vk("vk:0x1FF")
    with pytest.raises(ValueError):
        key_vk("vk:zz")


# ---------- регрессии ----------

def test_escape_cancel_is_swallowed(m: GestureMachine) -> None:
    """Esc при зажатом правом Ctrl: иначе система получает Ctrl+Esc и открывает «Пуск»."""
    m.key_down(RCTRL, 1000)
    assert kinds(m.key_down(ESC, 2000)) == [Kind.CANCEL]
    assert m.swallow is True
    assert kinds(m.key_up(ESC, 2050)) == []
    assert m.swallow is True          # отпускание тоже не доходит до приложений
    m.key_up(RCTRL, 2500)
    assert m.swallow is False         # сам ptt приложениям отдаём


def test_escape_cancel_in_hands_free_is_swallowed(m: GestureMachine) -> None:
    for t in (1000, 1200):
        m.key_down(RCTRL, t)
        m.key_up(RCTRL, t + 50)
    assert kinds(m.key_down(ESC, 5000)) == [Kind.CANCEL]
    assert m.swallow is True
    m.key_up(ESC, 5050)
    assert m.swallow is True
    assert kinds(m.key_down(ESC, 6000)) == [] and m.swallow is False  # вне записи Esc — обычный


def test_lost_ptt_release_starts_new_recording(m: GestureMachine) -> None:
    """Отпускание ptt не дошло до хука (UAC): следующее нажатие — не «автоповтор»."""
    assert kinds(m.key_down(RCTRL, 1000)) == [Kind.START]
    assert kinds(m.key_down(RCTRL, 1500)) == []       # настоящий автоповтор
    assert kinds(m.key_down(RCTRL, 1533)) == []
    # отпускание потерялось; через минуту пользователь снова зажал ptt
    assert kinds(m.key_down(RCTRL, 61_000)) == [Kind.CANCEL, Kind.START]
    assert kinds(m.key_up(RCTRL, 63_000)) == [Kind.STOP]


def test_lost_ptt_release_in_idle(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    m.key_down(ESC, 1100)                              # отмена → ждём отпускания ptt
    assert kinds(m.key_down(RCTRL, 30_000)) == [Kind.START]   # отпускание потерялось


def test_interrupt_waits_for_ptt_release(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    assert m.is_held(RCTRL) and not m.is_held(ESC)
    m.interrupt()                                      # лимит длительности, смена микрофона
    assert kinds(m.key_down(RCTRL, 1033)) == []        # автоповтор не начинает новую запись
    assert kinds(m.key_up(RCTRL, 2000)) == []
    assert not m.is_held(RCTRL)
    assert kinds(m.key_down(RCTRL, 3000)) == [Kind.START]


def test_foreign_input_counts_as_press(m: GestureMachine) -> None:
    before = m.presses
    m.note_input()
    assert m.presses == before + 1


def test_hook_counts_foreign_injected_input(m: GestureMachine,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Программный ввод не от Рупора — это тоже пользователь: клавиатура через AnyDesk,
    RustDesk, Logitech Flow, Mouse Without Borders, экранная клавиатура. Такие нажатия
    считаются и запускают диктовку. Свой ввод (метка RUPOR_MAGIC) пропускается.
    Хук не устанавливается — зовём обработчик напрямую."""
    import ctypes

    from rupor import _win as w
    from rupor.hotkeys import WM_KEYDOWN, WM_KEYUP, KeyboardHook

    monkeypatch.setattr(w.user32, "CallNextHookEx", lambda *a: 0)
    acts: list[list[object]] = []
    hook = KeyboardHook(m, lambda a: acts.append(list(a)))

    def event(vk: int, injected: bool, extra: int, wparam: int = WM_KEYDOWN) -> None:
        kb = w.KBDLLHOOKSTRUCT(vk, 0x1E, w.LLKHF_INJECTED if injected else 0, 1000, extra)
        hook._hook_proc(0, wparam, ctypes.addressof(kb))

    before = m.presses
    event(A, injected=True, extra=0)                   # чужой программный ввод
    event(A, injected=True, extra=0, wparam=WM_KEYUP)
    assert m.presses == before + 1
    event(A, injected=True, extra=w.RUPOR_MAGIC)       # своя печать
    assert m.presses == before + 1
    event(RCTRL, injected=True, extra=w.RUPOR_MAGIC)   # свой «правый Ctrl» — не жест
    assert acts == []
    event(RCTRL, injected=True, extra=0)               # правый Ctrl через удалённый доступ — жест
    assert [a.kind for a in acts[0]] == [Kind.START]  # type: ignore[attr-defined]
    assert m.presses == before + 2



# ---------- клавиша диктовки — сочетание Ctrl+Alt (как в Wispr Flow) ----------

from rupor.hotkeys import key_group, parse_keyset, parse_ptt  # noqa: E402

LWIN = 0x5B


@pytest.fixture
def ca() -> GestureMachine:
    return GestureMachine(parse_keyset("ctrl+alt"), key_group("shift"), key_group("esc"),
                          tap_max_ms=250, double_tap_ms=400, command_mod=key_group("win"),
                          insert_last=parse_chord("ctrl+alt+shift+v"))


def test_parse_keyset() -> None:
    assert parse_keyset("rctrl") == (frozenset({RCTRL}),)
    ctrl, alt = parse_keyset("Ctrl + Alt")
    assert {LCTRL, RCTRL} <= ctrl and {LALT, RALT} <= alt
    for bad in ("", "ctrl+", "ctrl+ctrl", "ctrl+lctrl", "ctrl+alt+shift+win", "nope"):
        with pytest.raises(ValueError):
            parse_keyset(bad)
    with pytest.raises(ValueError):
        key_group("ctrl+alt")


def test_chord_hold_starts_on_second_key_and_stops_on_any_release(ca: GestureMachine) -> None:
    assert kinds(ca.key_down(LCTRL, 1000)) == []            # один Ctrl — ещё не жест
    assert kinds(ca.key_down(LALT, 1050)) == [Kind.START]    # Ctrl+Alt зажаты
    assert kinds(ca.key_down(LALT, 1500)) == []              # автоповтор
    assert kinds(ca.key_up(LCTRL, 3000)) == [Kind.STOP]      # отпущен любой из двух
    assert kinds(ca.key_up(LALT, 3050)) == []


def test_chord_any_side_and_order(ca: GestureMachine) -> None:
    assert kinds(ca.key_down(RALT, 1000)) == []
    assert kinds(ca.key_down(RCTRL, 1010)) == [Kind.START]
    assert kinds(ca.key_down(LCTRL, 1100)) == []   # вторая сторона Ctrl — часть жеста
    assert kinds(ca.key_up(RCTRL, 1500)) == []     # левый Ctrl ещё держим — жест жив
    assert kinds(ca.key_up(LCTRL, 2000)) == [Kind.STOP]


def test_ctrl_alt_combo_is_not_dictation(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.key_down(ord("T"), 1100)) == [Kind.DISCARD]  # Ctrl+Alt+T
    assert ca.swallow is False                                    # T уходит в приложение
    assert kinds(ca.key_up(LALT, 1200)) == []
    assert kinds(ca.key_up(LCTRL, 1210)) == []
    assert kinds(ca.key_down(LCTRL, 2000)) == []                  # покой
    assert kinds(ca.key_down(ord("C"), 2010)) == []               # обычный Ctrl+C


def test_chord_double_tap_hands_free(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.key_up(LALT, 1100)) == [Kind.DISCARD]
    ca.key_up(LCTRL, 1110)
    ca.key_down(LCTRL, 1250)
    assert kinds(ca.key_down(LALT, 1260)) == [Kind.START]
    assert kinds(ca.key_up(LCTRL, 1330)) == [Kind.HANDS_FREE]
    ca.key_up(LALT, 1340)
    assert kinds(ca.key_down(LCTRL, 5000)) == []      # один Ctrl не останавливает
    assert kinds(ca.key_down(LALT, 5010)) == []
    assert kinds(ca.key_up(LALT, 5100)) == [Kind.STOP]  # стоп — по отпусканию


def test_chord_shift_broadcast_and_win_command_are_swallowed(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.key_down(LSHIFT, 1100)) == [Kind.BROADCAST]
    assert ca.swallow is True        # Alt+Shift не переключит раскладку
    assert kinds(ca.key_down(LWIN, 1200)) == [Kind.COMMAND]
    assert ca.swallow is True        # Win не откроет «Пуск»
    ca.key_up(LWIN, 1300)
    assert ca.swallow is True
    assert kinds(ca.key_up(LALT, 3000)) == [Kind.STOP]


def test_chord_escape_cancels(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.key_down(ESC, 1500)) == [Kind.CANCEL]
    assert ca.swallow is True        # Ctrl+Alt+Esc не уйдёт в систему
    assert kinds(ca.key_up(LALT, 1600)) == []


def test_insert_last_chord_with_ctrl_alt_ptt(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1005)
    ca.key_down(LSHIFT, 1010)
    assert kinds(ca.key_down(V, 1020)) == [Kind.DISCARD, Kind.INSERT_LAST]
    assert ca.swallow is True


def test_alt_release_after_gesture_is_masked(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    ca.key_up(LCTRL, 2000)
    assert ca.mask is False            # Ctrl меню не открывает
    ca.key_up(LALT, 2010)
    assert ca.mask is True             # перед отпусканием Alt — нейтральная клавиша
    ca.key_down(LALT, 3000)
    ca.key_up(LALT, 3100)
    assert ca.mask is False            # обычный Alt без жеста не трогаем


def test_single_key_ptt_has_no_mask(m: GestureMachine) -> None:
    m.key_down(RCTRL, 1000)
    m.key_up(RCTRL, 2000)
    assert m.mask is False


def test_foreign_combo_gets_swallowed_shift_back(ca: GestureMachine) -> None:
    """VS Code Ctrl+Shift+Alt+↓ (Ctrl, Alt, Shift, ↓): Shift проглочен как рассылка —
    при ↓ он вбрасывается обратно вместе с самой клавишей."""
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    ca.key_down(LSHIFT, 1020)
    assert ca.swallow is True
    assert kinds(ca.key_down(0x28, 1030)) == [Kind.DISCARD]
    assert ca.swallow is True and ca.replay == [LSHIFT, 0x28]
    ca.key_up(0x28, 1100)
    assert ca.swallow is False         # отпускание ↓ — физическое
    ca.key_up(LSHIFT, 1110)
    assert ca.swallow is False         # и Shift отпускается как обычно


def test_office_key_is_not_eaten(ca: GestureMachine) -> None:
    """Клавиша Office: LCtrl, LShift, LAlt, LWin + W. Win проглочен как команда, W — чужая
    клавиша: Win и W возвращаются приложению."""
    for vk, t in ((LCTRL, 1), (LSHIFT, 2), (LALT, 3)):
        ca.key_down(vk, t)
    assert kinds(ca.key_down(LWIN, 4)) == [Kind.COMMAND]
    assert kinds(ca.key_down(ord("W"), 5)) == [Kind.DISCARD]
    assert ca.replay == [LWIN, ord("W")]


def test_hands_free_foreign_combo_keeps_recording(ca: GestureMachine) -> None:
    """«Без рук», в VS Code жмут Ctrl, Shift, Alt, ↓: не стоп и не рассылка."""
    for t in (1000, 1200):
        ca.key_down(LCTRL, t)
        ca.key_down(LALT, t + 5)
        ca.key_up(LALT, t + 50)
        ca.key_up(LCTRL, t + 55)
    ca.key_down(LCTRL, 5000)
    ca.key_down(LSHIFT, 5010)
    assert kinds(ca.key_down(LALT, 5020)) == []
    assert kinds(ca.key_down(0x28, 5030)) == []     # сочетание приложения
    assert ca.swallow is False                       # Shift нажат до ptt — не глотали
    ca.key_up(0x28, 5100)
    assert kinds(ca.key_up(LALT, 5110)) == []        # запись «без рук» продолжается
    ca.key_up(LSHIFT, 5120)
    ca.key_up(LCTRL, 5130)
    ca.key_down(LCTRL, 9000)
    ca.key_down(LALT, 9010)
    assert kinds(ca.key_up(LALT, 9100)) == [Kind.STOP]


def test_stale_half_of_chord_does_not_start(ca: GestureMachine) -> None:
    """Отпускание Alt потерялось: один Ctrl не должен запускать диктовку."""
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    ca.key_up(LCTRL, 2000)             # STOP; отпускание Alt хук не увидел
    assert kinds(ca.key_down(LCTRL, 9000)) == []    # Alt «висит» 8 с без событий
    assert kinds(ca.key_up(LCTRL, 9500)) == []
    assert kinds(ca.key_down(LCTRL, 9600)) == []
    assert kinds(ca.key_down(LALT, 9610)) == [Kind.START]  # настоящее нажатие — работает


def test_swallowed_key_is_forgotten_on_new_press(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    ca.key_down(LWIN, 1020)            # Win проглочен (команда)
    ca.desktop_switched(1030)          # UAC: отпускание Win потерялось
    ca.key_down(LWIN, 5000)            # обычный тап Win
    assert ca.swallow is False
    ca.key_up(LWIN, 5050)
    assert ca.swallow is False         # отпускание дошло — Win не «залип»


def test_ctrl_alt_del_is_silent(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.desktop_switched(1050)) == [Kind.DISCARD]   # Del до хука не дошёл


def test_desktop_switch_forgets_held_keys(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    ca.key_down(0x2E, 1020)          # Ctrl+Alt+Del → DISCARD, экран безопасности
    assert kinds(ca.desktop_switched()) == []
    # отпускания на другом рабочем столе потерялись; обычный Ctrl+C после возврата — не жест
    assert kinds(ca.key_down(LCTRL, 9000)) == []
    assert kinds(ca.key_down(ord("C"), 9010)) == []
    ca.key_up(ord("C"), 9020)
    ca.key_up(LCTRL, 9030)
    assert kinds(ca.key_down(LCTRL, 9500)) == []
    assert kinds(ca.key_down(LALT, 9510)) == [Kind.START]


def test_desktop_switch_during_hold_cancels(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.desktop_switched()) == [Kind.CANCEL]
    assert kinds(ca.key_down(LCTRL, 5000)) == []
    assert kinds(ca.key_down(LALT, 5010)) == [Kind.START]


# ---------- кнопка мыши и клик по пилюле ----------

def test_mouse_hold_is_push_to_talk(ca: GestureMachine) -> None:
    assert kinds(ca.mouse_down(1000)) == [Kind.START]
    assert kinds(ca.mouse_up(2500)) == [Kind.STOP]


def test_mouse_click_toggles_hands_free(ca: GestureMachine) -> None:
    assert kinds(ca.mouse_down(1000)) == [Kind.START]
    assert kinds(ca.mouse_up(1100)) == [Kind.HANDS_FREE]     # короткое нажатие — «без рук»
    assert kinds(ca.key_down(ord("A"), 2000)) == []           # печатать руками можно
    assert kinds(ca.mouse_down(5000)) == []
    assert kinds(ca.mouse_up(5080)) == [Kind.STOP]            # второе нажатие — стоп


def test_mouse_stop_with_shift_broadcasts(ca: GestureMachine) -> None:
    ca.mouse_down(1000)
    ca.mouse_up(1100)
    ca.key_down(LSHIFT, 4000)
    ca.mouse_down(4100)
    assert kinds(ca.mouse_up(4200)) == [Kind.BROADCAST, Kind.STOP]


def test_mouse_during_keyboard_hold_is_ignored(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.mouse_down(1100)) == []
    assert kinds(ca.mouse_up(1200)) == []
    assert kinds(ca.key_up(LALT, 3000)) == [Kind.STOP]


def test_keyboard_during_mouse_hold(ca: GestureMachine) -> None:
    ca.mouse_down(1000)
    assert kinds(ca.key_down(LCTRL, 1100)) == []
    assert kinds(ca.key_down(LALT, 1110)) == []               # Ctrl+Alt сверху — ничего
    ca.key_up(LALT, 1200)
    ca.key_up(LCTRL, 1210)
    assert kinds(ca.mouse_up(3000)) == [Kind.STOP]
    assert kinds(ca.mouse_down(4000)) == [Kind.START]          # следующий жест работает


def test_mouse_escape_in_stop_pending(ca: GestureMachine) -> None:
    ca.mouse_down(1000)
    ca.mouse_up(1100)
    ca.mouse_down(3000)
    assert kinds(ca.key_down(ESC, 3050)) == [Kind.CANCEL]
    assert kinds(ca.mouse_up(3100)) == []


def test_mouse_escape_during_hold_keeps_mouse_working(ca: GestureMachine) -> None:
    # Esc посреди записи кнопкой мыши: ptt не нажимали — ждать его отпускания нечего
    ca.mouse_down(1000)
    assert kinds(ca.key_down(ESC, 2000)) == [Kind.CANCEL]
    assert kinds(ca.mouse_up(2500)) == []
    assert kinds(ca.mouse_down(5000)) == [Kind.START]
    assert kinds(ca.mouse_up(7000)) == [Kind.STOP]


def test_combo_during_mouse_hold_keeps_mouse_working(ca: GestureMachine) -> None:
    ca.mouse_down(1000)
    assert kinds(ca.key_down(C, 2000)) == [Kind.DISCARD]
    ca.key_up(C, 2050)
    assert kinds(ca.mouse_up(2500)) == []
    assert kinds(ca.mouse_down(5000)) == [Kind.START]
    assert kinds(ca.mouse_up(7000)) == [Kind.STOP]
    assert kinds(ca.key_down(LCTRL, 8000)) == []               # и клавиатура тоже
    assert kinds(ca.key_down(LALT, 8010)) == [Kind.START]


def test_pill_click_toggles(ca: GestureMachine) -> None:
    assert kinds(ca.external_toggle(1000)) == [Kind.START, Kind.HANDS_FREE]
    assert kinds(ca.key_down(ord("A"), 2000)) == []
    assert kinds(ca.external_toggle(5000)) == [Kind.STOP]
    assert kinds(ca.external_toggle(6000)) == [Kind.START, Kind.HANDS_FREE]
    ca.key_down(LCTRL, 7000)                                   # стоп и клавишами
    ca.key_down(LALT, 7010)
    assert kinds(ca.key_up(LALT, 7100)) == [Kind.STOP]


def test_pill_click_ignored_while_keys_held(ca: GestureMachine) -> None:
    ca.key_down(LCTRL, 1000)
    ca.key_down(LALT, 1010)
    assert kinds(ca.external_toggle(1100)) == []


# ---------- Ctrl+Win и RDP ----------

@pytest.fixture
def cw() -> GestureMachine:
    return GestureMachine(parse_ptt("ctrl+win"), key_group("shift"), key_group("esc"),
                          tap_max_ms=250, double_tap_ms=400, command_mod=key_group("alt"))


def test_win_completing_chord_is_swallowed(cw: GestureMachine) -> None:
    """Win не уходит ни в «Пуск», ни на удалённую машину в RDP."""
    cw.key_down(LCTRL, 1000)
    assert cw.swallow is False                      # Ctrl — как обычно
    assert kinds(cw.key_down(LWIN, 1050)) == [Kind.START]
    assert cw.swallow is True
    cw.key_down(LWIN, 1600)                         # автоповтор — тоже глотаем
    assert cw.swallow is True
    assert kinds(cw.key_up(LWIN, 3000)) == [Kind.STOP]
    assert cw.swallow is True and cw.mask is False  # отпускание проглочено, маска не нужна
    cw.key_up(LCTRL, 3010)
    assert cw.swallow is False


def test_ctrl_win_d_gets_win_back(cw: GestureMachine) -> None:
    """Ctrl+Win+D (новый рабочий стол): Win возвращается приложению вместе с D."""
    cw.key_down(LCTRL, 1000)
    cw.key_down(LWIN, 1010)
    assert kinds(cw.key_down(ord("D"), 1020)) == [Kind.DISCARD]
    assert cw.swallow is True and cw.replay == [LWIN, ord("D")]
    cw.key_up(ord("D"), 1100)
    cw.key_up(LWIN, 1110)
    assert cw.swallow is False                      # физическое отпускание Win дошло


def test_win_pressed_first_is_not_swallowed(cw: GestureMachine) -> None:
    cw.key_down(LWIN, 1000)
    assert cw.swallow is False
    assert kinds(cw.key_down(LCTRL, 1010)) == [Kind.START]
    assert cw.swallow is False
    cw.key_up(LCTRL, 2000)
    cw.key_up(LWIN, 2010)
    assert cw.mask is True                          # отпускание Win маскируем — «Пуск» не откроется


def test_win_alt_v_inserts_last_and_masks_menu_and_start() -> None:
    """Win+Alt+V при диктовке на Ctrl+Win: «вставить последнее», V проглочена, отпускание
    Alt и Win маскируется (не откроется ни меню окна, ни «Пуск»); диктовка не начинается."""
    m = GestureMachine(parse_ptt("ctrl+win"), key_group("shift"), key_group("esc"),
                       tap_max_ms=250, double_tap_ms=400, command_mod=key_group("alt"),
                       insert_last=parse_chord("win+alt+v"))
    assert kinds(m.key_down(LWIN, 1000)) == []
    assert kinds(m.key_down(LALT, 1010)) == []
    assert kinds(m.key_down(V, 1020)) == [Kind.INSERT_LAST]
    assert m.swallow is True
    m.key_up(V, 1100)
    assert m.swallow is True
    m.key_up(LALT, 1110)
    assert m.swallow is False and m.mask is True
    m.key_up(LWIN, 1120)
    assert m.swallow is False and m.mask is True
    # в другом порядке — так же
    m.key_down(LALT, 2000)
    m.key_down(LWIN, 2010)
    assert kinds(m.key_down(V, 2020)) == [Kind.INSERT_LAST]
    # без Alt — не наше сочетание (Win+V — журнал буфера обмена Windows)
    m.key_up(V, 2100)
    m.key_up(LALT, 2110)
    assert kinds(m.key_down(V, 2200)) == [] and m.swallow is False


def test_hook_ignores_mstsc_sync_copies(cw: GestureMachine,
                                        monkeypatch: pytest.MonkeyPatch) -> None:
    """mstsc вбрасывает копии Ctrl с меткой 0x4321DCBA — это не нажатия пользователя."""
    import ctypes

    from rupor import _win as w
    from rupor.hotkeys import MSTSC_SYNC_EXTRA, WM_KEYDOWN, KeyboardHook

    monkeypatch.setattr(w.user32, "CallNextHookEx", lambda *a: 0)
    acts: list[list[object]] = []
    hook = KeyboardHook(cw, lambda a: acts.append(list(a)))

    def event(vk: int, extra: int) -> None:
        kb = w.KBDLLHOOKSTRUCT(vk, 0x1D, w.LLKHF_INJECTED, 1000, extra)
        hook._hook_proc(0, WM_KEYDOWN, ctypes.addressof(kb))

    before = cw.presses
    event(LCTRL, MSTSC_SYNC_EXTRA)
    event(LWIN, MSTSC_SYNC_EXTRA)
    assert acts == [] and cw.presses == before
