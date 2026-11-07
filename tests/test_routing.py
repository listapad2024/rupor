import json
from pathlib import Path

import pytest

from rupor import config
from rupor.config import MODE_AUTO, MODE_CLIPBOARD, MODE_TYPE, Settings
from rupor.engine import finalize_text, resolve_mode, resolve_style
from rupor.winutil import WindowInfo, is_rdp


def win(cls: str, proc: str) -> WindowInfo:
    return WindowInfo(1, cls, "t", 1, proc)


def test_is_rdp() -> None:
    s = Settings()
    assert is_rdp("TscShellContainerClass", "mstsc.exe", s.rdp_classes, s.rdp_processes)
    assert is_rdp("WindowsForms10.Window", "mRemoteNG.exe", s.rdp_classes, s.rdp_processes)
    assert is_rdp("X", "rdcman.exe", s.rdp_classes, s.rdp_processes)
    assert not is_rdp("Notepad", "notepad.exe", s.rdp_classes, s.rdp_processes)


def test_auto_mode() -> None:
    s = Settings()
    assert resolve_mode(win("TscShellContainerClass", "mstsc.exe"), s) == MODE_TYPE
    assert resolve_mode(win("Chrome_WidgetWin_1", "chrome.exe"), s) == MODE_CLIPBOARD
    assert resolve_mode(None, s) == MODE_TYPE


def test_app_rule_wins() -> None:
    s = Settings(app_rules={"Telegram.exe": {"insert_mode": MODE_TYPE, "style": None},
                            "mstsc.exe": {"insert_mode": MODE_CLIPBOARD, "style": None}})
    s.validate()
    assert resolve_mode(win("Qt", "Telegram.exe"), s) == MODE_TYPE
    assert resolve_mode(win("TscShellContainerClass", "mstsc.exe"), s) == MODE_CLIPBOARD


def test_rule_with_only_style_keeps_auto_mode() -> None:
    s = Settings(app_rules={"mstsc.exe": {"insert_mode": None, "style": "business"}})
    s.validate()
    info = win("TscShellContainerClass", "mstsc.exe")
    assert resolve_mode(info, s) == MODE_TYPE
    assert resolve_style(info, s) == "business"


def test_old_format_rules_are_converted() -> None:
    s = Settings(app_rules={"telegram.exe": MODE_TYPE, "x.exe": "bogus",  # type: ignore[dict-item]
                            "y.exe": {"insert_mode": "nope", "style": None},
                            "z.exe": {"insert_mode": None, "style": "no-such-style"}})
    s.validate()
    assert s.app_rules == {"telegram.exe": {"insert_mode": MODE_TYPE, "style": None}}
    assert resolve_mode(win("Qt", "Telegram.exe"), s) == MODE_TYPE


def test_forced_mode() -> None:
    s = Settings(insert_mode=MODE_TYPE)
    assert resolve_mode(win("Chrome_WidgetWin_1", "chrome.exe"), s) == MODE_TYPE


def test_default_style_rules() -> None:
    s = Settings()
    assert resolve_style(win("Qt", "Telegram.exe"), s) == "messenger"
    assert resolve_style(win("x", "OUTLOOK.EXE"), s) == "business"
    assert resolve_style(win("x", "Code.exe"), s) == "code"
    assert resolve_style(win("x", "notepad.exe"), s) == "normal"
    assert resolve_style(None, s) == "normal"
    s.default_style = "as_is"
    assert resolve_style(win("x", "notepad.exe"), s) == "as_is"


def test_finalize_text() -> None:
    assert finalize_text("  привет ", True) == "привет "
    assert finalize_text("привет", False) == "привет"
    assert finalize_text("   ", True) == ""
    assert finalize_text("1. раз\n2. два\n", True) == "1. раз\n2. два "


# ---------- настройки: патч из UI ----------

def test_apply_patch_ok_and_changed_keys() -> None:
    s = Settings()
    changed = config.apply_patch(s, {"ai_enabled": True, "ai_min_words": 4, "theme": "dark",
                                     "insert_mode": "auto", "ai_timeout_s": 7.5})
    assert set(changed) == {"ai_enabled", "ai_min_words", "theme", "ai_timeout_s"}
    assert s.ai_enabled and s.ai_min_words == 4 and s.ai_timeout_s == 7.5


@pytest.mark.parametrize("patch, fragment", [
    ({"nope": 1}, "Неизвестная настройка"),
    ({"insert_mode": "fast"}, "Способ вставки"),
    ({"ai_timeout_s": 0}, "Таймаут ответа ИИ"),
    ({"ai_min_words": 2.5}, "целое"),
    ({"ai_enabled": "yes"}, "ИИ-очистка"),
    ({"ptt_key": "nokey"}, "Удерживай и говори"),
    ({"ptt_key": "rshift"}, "совпадает"),
    ({"default_style": "poetry"}, "стиль"),
    ({"accent": "red"}, "Акцент"),
    ({"pill_position": [1]}, "Положение пилюли"),
    ({"theme": "pink"}, "Тема"),
    ({"app_rules": {"a.exe": {"insert_mode": "x", "style": None}}}, "a.exe"),
    ({"app_rules": {"c:\\a.exe": {"insert_mode": None, "style": None}}}, "имя процесса"),
    ({"insert_last_hotkey": "v"}, "Вставить последнее"),
    ({"copy_last_hotkey": "ctrl+alt+shift+v"}, "совпадают"),
    ({"ollama_url": "localhost:11434"}, "Адрес Ollama"),
    ({"terms": "ClickHouse"}, "Термины"),
])
def test_apply_patch_rejects(patch: dict[str, object], fragment: str) -> None:
    s = Settings()
    before = config.to_dict(s)
    with pytest.raises(ValueError, match=fragment):
        config.apply_patch(s, patch)
    assert config.to_dict(s) == before  # ничего не применилось


def test_apply_patch_is_atomic() -> None:
    s = Settings()
    with pytest.raises(ValueError):
        config.apply_patch(s, {"theme": "dark", "insert_mode": "fast"})
    assert s.theme == "system"


def test_apply_patch_normalizes() -> None:
    s = Settings()
    config.apply_patch(s, {"app_rules": {" Telegram.EXE ": "type"}, "input_device": 3,
                           "pill_position": [10.0, 20.0], "accent": "#ABCDEF",
                           "terms": [" A ", "a", "B", ""], "ptt_key": "F13"})
    assert s.app_rules == {"telegram.exe": {"insert_mode": "type", "style": None}}
    assert s.input_device == "3" and s.pill_position == [10, 20] and s.accent == "#abcdef"
    assert s.terms == ["A", "B"] and s.ptt_key == "f13"


# ---------- настройки: файл ----------

def test_load_migrates_stage2_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "ptt_key": "rctrl", "insert_mode": "bogus", "type_delay_ms": 99,
        "app_rules": {"telegram.exe": "clipboard", "bad.exe": 5},
    }), encoding="utf-8")
    s = config.load()
    assert s.insert_mode == MODE_AUTO           # неверное → по умолчанию
    assert s.type_delay_ms == 0                 # вне диапазона → по умолчанию
    # правило пользователя сохранено и сконвертировано, остальные стили дописаны
    assert s.app_rules["telegram.exe"] == {"insert_mode": "clipboard", "style": None}
    assert s.app_rules["outlook.exe"]["style"] == "business"
    assert "bad.exe" not in s.app_rules
    saved = json.loads((d / "settings.json").read_text(encoding="utf-8"))
    assert saved["command_modifier"] == "alt" and saved["ai_enabled"] is False
    assert saved["ptt_key"] == "ctrl+win" and saved["hotkeys_version"] == 3  # миграция
    assert saved["app_rules"]["telegram.exe"] == {"insert_mode": "clipboard", "style": None}


def test_load_keeps_user_rules_of_new_file(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({"default_style": "code", "app_rules": {}}),
                                     encoding="utf-8")
    s = config.load()
    assert s.app_rules == {} and s.default_style == "code"  # пользователь удалил все правила


def test_load_broken_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    (d / "settings.json").write_text("{не json", encoding="utf-8")
    s = config.load()
    assert s == Settings()
    # испорченный файл отложен рядом, а не затёрт умолчаниями
    kept = list(d.glob("settings.broken-*.json"))
    assert len(kept) == 1 and kept[0].read_text(encoding="utf-8") == "{не json"
    assert json.loads((d / "settings.json").read_text(encoding="utf-8"))["ptt_key"] == "ctrl+win"


def test_load_unreadable_file_is_not_overwritten(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    p = d / "settings.json"
    p.write_text(json.dumps({"default_style": "code", "app_rules": {}}), encoding="utf-8")
    real = Path.read_text

    def locked(self: Path, *a: object, **k: object) -> str:
        if self == p:
            raise PermissionError(13, "файл занят")
        return real(self, *a, **k)  # type: ignore[arg-type]

    monkeypatch.setattr(Path, "read_text", locked)
    assert config.load() == Settings()          # на этот запуск — умолчания
    monkeypatch.setattr(Path, "read_text", real)
    assert json.loads(p.read_text(encoding="utf-8"))["default_style"] == "code"  # файл цел
    assert not list(d.glob("settings.broken-*.json"))


def test_load_old_file_with_bad_cancel_key(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    # миграция хоткеев v1 видит ещё не проверенные значения: cancel_key: null не роняет ядро
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({
        "broadcast_targets": [], "hotkeys_version": 1, "ptt_key": "rctrl",
        "broadcast_modifier": "rshift", "command_modifier": "ralt", "cancel_key": None,
    }), encoding="utf-8")
    s = config.load()
    assert s.cancel_key == "esc"


def test_conflicting_hotkeys_in_file_fall_back(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps({"ptt_key": "ctrl+win", "command_modifier": "lwin",
                                                 "hotkeys_version": 3}), encoding="utf-8")
    s = config.load()
    assert (s.ptt_key, s.command_modifier) == ("ctrl+win", "alt")  # lwin входит в ctrl+win


def _write(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, data: dict[str, object]) -> Path:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    d = tmp_path / "Rupor"
    d.mkdir()
    (d / "settings.json").write_text(json.dumps(data), encoding="utf-8")
    return d


def test_old_default_hotkeys_migrate_to_ctrl_win(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    """Файл 0.3.0 с нетронутыми умолчаниями (правые Ctrl/Shift/Alt) → Ctrl+Win / Shift / Alt."""
    _write(tmp_path, monkeypatch, {"ptt_key": "rctrl", "broadcast_modifier": "rshift",
                                   "command_modifier": "ralt", "default_style": "normal"})
    s = config.load()
    assert (s.ptt_key, s.broadcast_modifier, s.command_modifier) == ("ctrl+win", "shift", "alt")
    assert s.hotkeys_version == 3


def test_v2_ctrl_alt_defaults_migrate_to_ctrl_win(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, monkeypatch, {"ptt_key": "ctrl+alt", "broadcast_modifier": "shift",
                                   "command_modifier": "win", "hotkeys_version": 2,
                                   "default_style": "normal"})
    s = config.load()
    assert (s.ptt_key, s.command_modifier) == ("ctrl+win", "alt")


def test_v2_custom_hotkeys_are_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, monkeypatch, {"ptt_key": "ctrl+alt", "broadcast_modifier": "shift",
                                   "command_modifier": "rwin", "hotkeys_version": 2,
                                   "default_style": "normal"})
    s = config.load()
    assert (s.ptt_key, s.command_modifier) == ("ctrl+alt", "rwin")


def test_custom_hotkeys_are_not_migrated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, monkeypatch, {"ptt_key": "f13", "broadcast_modifier": "rshift",
                                   "command_modifier": "ralt", "default_style": "normal"})
    s = config.load()
    assert (s.ptt_key, s.command_modifier) == ("f13", "ralt")


def test_rctrl_chosen_after_migration_stays(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, monkeypatch, {"ptt_key": "rctrl", "broadcast_modifier": "rshift",
                                   "command_modifier": "ralt", "hotkeys_version": 3,
                                   "default_style": "normal"})
    assert config.load().ptt_key == "rctrl"  # пользователь вернул правый Ctrl сам


@pytest.mark.parametrize("patch, fragment", [
    ({"ptt_key": "ctrl+win", "command_modifier": "lwin"}, "совпадает"),
    ({"ptt_key": "ctrl+ctrl"}, "повторяется"),
    ({"ptt_key": "ctrl+alt+shift+win"}, "не больше трёх"),
    ({"broadcast_modifier": "ctrl+shift"}, "одна клавиша"),
    ({"ptt_key": "ctrl+nope"}, "Неизвестная клавиша"),
])
def test_keyset_patch_rejects(patch: dict[str, object], fragment: str) -> None:
    s = Settings()
    with pytest.raises(ValueError, match=fragment):
        config.apply_patch(s, patch)


def test_keyset_patch_normalizes() -> None:
    s = Settings()
    config.apply_patch(s, {"ptt_key": " Ctrl + Alt ", "command_modifier": "rwin"})
    assert s.ptt_key == "ctrl+alt" and s.command_modifier == "rwin"


@pytest.mark.parametrize("ver", [None, "2", [2], True])
def test_broken_hotkeys_version_does_not_crash(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                               ver: object) -> None:
    _write(tmp_path, monkeypatch, {"ptt_key": "rctrl", "hotkeys_version": ver,
                                   "default_style": "normal"})
    s = config.load()  # не TypeError
    assert s.ptt_key == "ctrl+win" and s.hotkeys_version == 3


def test_migration_skipped_on_conflict_keeps_user_tuning(tmp_path: Path,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    """Свой cancel_key = lwin конфликтует с новым ptt = ctrl+win: хоткеи не трогаем,
    пороги пользователя сохраняются."""
    _write(tmp_path, monkeypatch, {"ptt_key": "rctrl", "broadcast_modifier": "rshift",
                                   "command_modifier": "ralt", "cancel_key": "lwin",
                                   "tap_max_ms": 300, "double_tap_ms": 600,
                                   "default_style": "normal"})
    s = config.load()
    assert (s.ptt_key, s.cancel_key) == ("rctrl", "lwin")
    assert (s.tap_max_ms, s.double_tap_ms) == (300, 600)


def test_conflict_resets_only_keys_not_thresholds(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    _write(tmp_path, monkeypatch, {"ptt_key": "ctrl+win", "command_modifier": "lwin",
                                   "tap_max_ms": 300, "hotkeys_version": 3,
                                   "default_style": "normal"})
    s = config.load()
    assert s.command_modifier == "alt" and s.tap_max_ms == 300


def test_single_generic_modifier_is_not_ptt() -> None:
    s = Settings()
    with pytest.raises(ValueError, match="мешает"):
        config.apply_patch(s, {"ptt_key": "ctrl"})
