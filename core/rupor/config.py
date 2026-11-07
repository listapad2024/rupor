"""Настройки Рупора: %APPDATA%\\Rupor\\settings.json.

Два режима проверки значений:
- при загрузке файла (``load``/``Settings.validate``) неверное значение заменяется значением
  по умолчанию с предупреждением в логе — Рупор всегда стартует;
- при изменении из UI (``apply_patch``) неверное значение — ``ValueError`` с текстом на
  языке интерфейса (``i18n.tr``), настройки не меняются вовсе (патч применяется целиком
  или никак).

Старый формат правил приложений ``{"x.exe": "type"}`` (этапы 1–2) читается и
конвертируется в ``{"x.exe": {"insert_mode": "type", "style": null}}``.
"""
from __future__ import annotations

import copy
import json
import logging
import math
import ntpath
import os
import re
import threading
import time
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any, Callable

from . import i18n
from .i18n import tr

log = logging.getLogger(__name__)

# Режимы вставки
MODE_AUTO = "auto"        # RDP → печать, иначе → буфер
MODE_TYPE = "type"        # всегда печать клавишами (SendInput)
MODE_CLIPBOARD = "clipboard"  # всегда через буфер
INSERT_MODES = (MODE_AUTO, MODE_TYPE, MODE_CLIPBOARD)

THEMES = ("system", "dark", "light")
AI_PROVIDERS = ("openrouter", "anthropic", "ollama")
AI_MODES = ("wait", "raw_first")
COMMAND_PROVIDERS = ("openrouter", "claude_cli", "anthropic", "ollama")
DEVICES = ("cuda", "cpu")
# Обучение словаря на правках: auto — добавлять сразу (с «Отменить»), suggest — предлагать
# (повторная такая же правка добавляется сама), off — не следить за полем
LEARN_MODES = ("auto", "suggest", "off")
# Звук компьютера во время записи (rupor.duck): off — не трогать, duck — приглушить до
# duck_level % исходной громкости, mute — выключить (уровень аудиосессий приложений)
DUCK_MODES = ("off", "duck", "mute")
# Язык интерфейса (сообщения ядра и UI): system — по языку Windows (i18n.resolve)
LANGUAGES = i18n.LANGUAGES            # system | ru | en
# Язык диктовки: auto — определять (приоритет у языка интерфейса: другой язык — только при
# уверенности ≥ en_threshold), ru / en — всегда этот язык, без определения
DICTATION_LANGUAGES = ("auto", "ru", "en")
COMPUTE_TYPES = ("float16", "int8_float16", "int8", "int8_float32", "float32", "bfloat16",
                 "int8_bfloat16", "auto", "default")

# Стили по приложениям «из коробки» (ТЗ: Telegram — мессенджер, Outlook — деловой,
# Cursor/VS Code — для кода). В файл этапов 1–2 дописываются при первой загрузке.
DEFAULT_APP_RULES: dict[str, dict[str, str | None]] = {
    "telegram.exe": {"insert_mode": None, "style": "messenger"},
    "outlook.exe": {"insert_mode": None, "style": "business"},
    "olk.exe": {"insert_mode": None, "style": "business"},        # новый Outlook
    "cursor.exe": {"insert_mode": None, "style": "code"},
    "code.exe": {"insert_mode": None, "style": "code"},
    "code - insiders.exe": {"insert_mode": None, "style": "code"},
}

HOTKEY_FIELDS = ("ptt_key", "broadcast_modifier", "command_modifier", "cancel_key",
                 "tap_max_ms", "double_tap_ms", "insert_last_hotkey", "copy_last_hotkey",
                 "mouse_button")
KEY_FIELDS = ("ptt_key", "broadcast_modifier", "command_modifier", "cancel_key")
# Смена любого из них — перезагрузка модели в фоне (model_dir: модель ищется в новой папке,
# нет там — качается туда, прежняя распознаёт, пока новая не готова)
MODEL_FIELDS = ("model", "device", "compute_type", "model_dir")


def app_dir() -> Path:
    base = os.environ.get("APPDATA") or str(Path.home())
    d = Path(base) / "Rupor"
    d.mkdir(parents=True, exist_ok=True)
    return d


@dataclass
class Settings:
    # Хоткеи: имена клавиш из hotkeys.KEY_NAMES или общие модификаторы ctrl/alt/shift/win.
    # Клавиша диктовки может быть сочетанием: «ctrl+win» (как в Wispr Flow) или «rctrl».
    ptt_key: str = "ctrl+win"
    broadcast_modifier: str = "shift"   # ptt_key + этот модификатор → во все RDP
    command_modifier: str = "alt"       # ptt_key + этот модификатор → командный режим
    cancel_key: str = "esc"
    # Кнопка мыши для диктовки: "" — нет, middle — колёсико, x1/x2 — боковые (4/5).
    # Держать — запись; короткое нажатие — «без рук», следующее — стоп.
    mouse_button: str = ""
    # Версия схемы хоткеев: 3 — умолчания Ctrl+Win / Shift / Alt, как в Wispr Flow
    # (2 — Ctrl+Alt / Shift / Win, 1 — правые Ctrl / Shift / Alt)
    hotkeys_version: int = 3
    tap_max_ms: int = 250
    double_tap_ms: int = 400
    # Сочетания «Вставить последнее» / «Скопировать последнее» (пустая строка — выключено)
    insert_last_hotkey: str = "ctrl+alt+shift+v"
    copy_last_hotkey: str = "ctrl+alt+shift+c"

    # Аудио
    input_device: str | None = None  # имя или индекс устройства; None — по умолчанию
    min_record_ms: int = 300

    # Распознавание
    model: str = "large-v3-turbo"
    device: str = "cuda"
    compute_type: str = "float16"
    # Папка моделей Whisper в формате кэша HuggingFace (models--<org>--<имя>/blobs|snapshots|
    # refs) — как download_root у faster-whisper, её можно указать и другим программам.
    # "" — кэш HuggingFace по умолчанию (обычно %USERPROFILE%\.cache\huggingface\hub)
    model_dir: str = ""
    beam_size: int = 5
    en_threshold: float = 0.8
    # auto | ru | en (asr.Recognizer читает через getattr): auto — язык интерфейса, другой —
    # только при уверенности ≥ en_threshold; ru / en — всегда этот язык
    dictation_language: str = "auto"
    terms: list[str] = field(default_factory=lambda: [
        "ClickHouse", "Metabase", "Python", "API", "SQL", "RDP", "GitHub", "Grafana",
    ])
    unload_after_min: int = 0  # 0 — не выгружать

    # Вставка
    insert_mode: str = MODE_AUTO
    type_delay_ms: int = 0
    rdp_type_delay_ms: int = 5
    type_chunk: int = 32
    clipboard_restore_ms: int = 300
    rdp_classes: list[str] = field(default_factory=lambda: ["TscShellContainerClass"])
    rdp_processes: list[str] = field(default_factory=lambda: ["mRemoteNG.exe", "RDCMan.exe"])
    # Правила по процессу: {"telegram.exe": {"insert_mode": null, "style": "messenger"}}
    app_rules: dict[str, dict[str, str | None]] = field(
        default_factory=lambda: copy.deepcopy(DEFAULT_APP_RULES))
    # Правила по названию окна (важнее правил процесса): [{process: "chrome.exe" | "*",
    # title: подстрока заголовка, insert_mode: … | null, style: … | null}]
    title_rules: list[dict[str, str | None]] = field(default_factory=list)
    # Рассылка только в отмеченные RDP-сессии: ключи сессий (winutil.rdp_session_key)
    broadcast_targets: list[str] = field(default_factory=list)
    # Устарело (до 0.4): заголовки RDP-окон, исключённые из рассылки. При загрузке файла без
    # broadcast_targets переводится в broadcast_targets (открытые сессии, кроме исключённых)
    broadcast_excluded: list[str] = field(default_factory=list)
    add_trailing_space: bool = True
    # Словарь учится на правках вставленного текста (UI Automation): auto | suggest | off
    learn_mode: str = "auto"
    # Похожие написания терминов словаря (fuzzy.py): «Xena браузер» → ZennoBrowser без
    # списка вариантов
    dict_fuzzy: bool = True
    # Подсказка распознаванию: частые слова из истории и текст перед курсором в окне
    vocab_enabled: bool = True
    context_enabled: bool = True

    # Звук компьютера во время записи: off | duck | mute; duck_level — до скольких процентов
    # исходной громкости приглушать (режим duck)
    duck_mode: str = "duck"
    duck_level: int = 15

    # Прочее
    sounds: bool = True
    # Автозапуск с Windows. Реестром управляет оболочка: при старте восстанавливает запись
    # HKCU\...\Run, если её стёрли (обновление, чистильщики), — пока тут true.
    autostart: bool = True

    # История, интерфейс
    default_style: str = "normal"
    history_enabled: bool = True
    history_retention_days: int = 0     # 0 — хранить всегда
    pill_enabled: bool = True
    pill_position: list[int] | None = None  # [x, y] в физических пикселях; None — снизу по центру
    theme: str = "system"
    accent: str = "#7c5cff"
    # Язык интерфейса: system (по языку Windows) | ru | en — сообщения ядра, трей, UI
    language: str = "system"
    first_run_done: bool = False

    # ИИ
    ai_enabled: bool = False
    ai_provider: str = "openrouter"
    ai_model: str = "anthropic/claude-haiku-4.5"
    anthropic_model: str = "claude-haiku-4-5"
    ollama_model: str = "qwen2.5:7b"
    ollama_url: str = "http://127.0.0.1:11434"
    ai_provider_order: list[str] = field(default_factory=list)
    ai_mode: str = "wait"               # wait | raw_first
    ai_min_words: int = 6
    ai_timeout_s: float = 5
    command_provider: str = "openrouter"
    env_path: str = ""                  # путь к .env с openrouter_api_key (необязательно)

    def validate(self) -> None:
        """Мягкая проверка (после загрузки файла): неверное → по умолчанию, с предупреждением."""
        defaults = Settings()
        for f in fields(self):
            v = getattr(self, f.name)
            if f.name == "app_rules":
                self.app_rules = _app_rules(v, strict=False)  # битые правила — поштучно
                continue
            if f.name == "title_rules":
                self.title_rules = _title_rules(v, strict=False)
                continue
            try:
                setattr(self, f.name, _VALIDATORS[f.name](v))
            except ValueError as e:
                log.warning("Настройка %s: %s — беру значение по умолчанию", f.name, e)
                setattr(self, f.name, copy.deepcopy(getattr(defaults, f.name)))
        try:
            _check_hotkeys(self)
        except ValueError as e:
            # Сбрасываем только клавиши жестов: пороги и сочетания пользователя не трогаем
            log.warning("%s — беру клавиши жестов по умолчанию", e)
            for k in KEY_FIELDS:
                setattr(self, k, getattr(defaults, k))


# ---------------------------------------------------------------------------
# Проверка значений. Каждый валидатор возвращает нормализованное значение или
# бросает ValueError с текстом на языке интерфейса (имя настройки добавляет вызывающий).
# ---------------------------------------------------------------------------

def _bool(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    raise ValueError(tr("cfg.expect_bool"))


def _int(lo: int, hi: int) -> Callable[[Any], int]:
    def f(v: Any) -> int:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(tr("cfg.expect_int"))
        if isinstance(v, float):
            if not math.isfinite(v) or v != int(v):
                raise ValueError(tr("cfg.expect_int"))
            v = int(v)
        if not lo <= v <= hi:
            raise ValueError(tr("cfg.range", lo=lo, hi=hi))
        return int(v)
    return f


def _float(lo: float, hi: float) -> Callable[[Any], float]:
    def f(v: Any) -> float:
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
            raise ValueError(tr("cfg.expect_number"))
        if not lo <= v <= hi:
            raise ValueError(tr("cfg.range", lo=f"{lo:g}", hi=f"{hi:g}"))
        return v
    return f


def _choice(options: tuple[str, ...]) -> Callable[[Any], str]:
    def f(v: Any) -> str:
        if not isinstance(v, str) or v not in options:
            raise ValueError(tr("cfg.choice", options=", ".join(options)))
        return v
    return f


def _text(allow_empty: bool = False, max_len: int = 500) -> Callable[[Any], str]:
    def f(v: Any) -> str:
        if not isinstance(v, str):
            raise ValueError(tr("cfg.expect_str"))
        out: str = v.strip()
        if not out and not allow_empty:
            raise ValueError(tr("cfg.empty"))
        if len(out) > max_len:
            raise ValueError(tr("cfg.too_long", max=max_len))
        return out
    return f


def _str_list(max_items: int = 500) -> Callable[[Any], list[str]]:
    def f(v: Any) -> list[str]:
        if not isinstance(v, list):
            raise ValueError(tr("cfg.expect_str_list"))
        out: list[str] = []
        seen: set[str] = set()
        for x in v:
            if not isinstance(x, str):
                raise ValueError(tr("cfg.expect_str_list"))
            x = x.strip()
            if x and x.lower() not in seen:
                seen.add(x.lower())
                out.append(x)
        if len(out) > max_items:
            raise ValueError(tr("cfg.too_many", max=max_items))
        return out
    return f


def _norm_keys(v: Any) -> str:
    if not isinstance(v, str) or not v.strip():
        raise ValueError(tr("cfg.expect_key"))
    return v.strip().lower().replace(" ", "")


def _key(v: Any) -> str:
    """Одна клавиша: «rshift», «shift», «esc», «f13», «vk:0x91»."""
    from .hotkeys import key_group
    n = _norm_keys(v)
    key_group(n)  # ValueError с понятным текстом
    return n


def _keyset(v: Any) -> str:
    """Клавиша или сочетание диктовки: «rctrl», «ctrl+alt», «ctrl+win»."""
    from .hotkeys import parse_ptt
    n = _norm_keys(v)
    parse_ptt(n)
    return n


def _chord(v: Any) -> str:
    from .hotkeys import parse_chord
    if v is None:
        return ""
    if not isinstance(v, str):
        raise ValueError(tr("cfg.expect_chord"))
    spec: str = v.strip().lower().replace(" ", "")
    if spec:
        parse_chord(spec)
    return spec


def _style(v: Any) -> str:
    from .ai import STYLE_IDS
    if not isinstance(v, str) or v not in STYLE_IDS:
        raise ValueError(tr("cfg.unknown_style", styles=", ".join(STYLE_IDS)))
    return v


def _input_device(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bool):
        raise ValueError(tr("cfg.expect_mic"))
    if isinstance(v, int):
        return str(v)
    if not isinstance(v, str):
        raise ValueError(tr("cfg.expect_mic"))
    v = v.strip()
    return v or None


def _pill_position(v: Any) -> list[int] | None:
    if v is None:
        return None
    if (isinstance(v, (list, tuple)) and len(v) == 2
            and all(isinstance(x, (int, float)) and not isinstance(x, bool)
                    and math.isfinite(x) for x in v)):
        return [int(v[0]), int(v[1])]
    raise ValueError(tr("cfg.expect_xy"))


_ACCENT_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}|[0-9a-fA-F]{6})$")


def _accent(v: Any) -> str:
    if not isinstance(v, str) or not _ACCENT_RE.match(v.strip()):
        raise ValueError(tr("cfg.expect_color"))
    return v.strip().lower()


def _url(v: Any) -> str:
    s = _text()(v)
    if not s.startswith(("http://", "https://")):
        raise ValueError(tr("cfg.expect_url"))
    return s.rstrip("/")


MODEL_DIR_MAX = 1000
_PATH_BAD_CHARS = frozenset('<>:"|?*')
_UNSET_VAR_RE = re.compile(r"%([^%\\/]+)%")


def normalize_model_dir(v: Any) -> str:
    """Папка моделей: "" — кэш HuggingFace по умолчанию; иначе полный путь (буква диска
    или UNC). %ПЕРЕМЕННЫЕ% и ~ раскрываются, кавычки вокруг вставленного пути снимаются,
    путь нормализуется (ntpath.normpath); кириллица и пробелы допустимы."""
    if not isinstance(v, str):
        raise ValueError(tr("cfg.expect_str"))
    s = v.strip()
    if len(s) >= 2 and s[0] == s[-1] == '"':   # «Копировать как путь» в проводнике
        s = s[1:-1].strip()
    if not s:
        return ""
    s = ntpath.expanduser(ntpath.expandvars(s))
    unset = _UNSET_VAR_RE.search(s)
    if unset:
        raise ValueError(tr("cfg.path_unknown_var", name=unset.group(1)))
    drive, rest = ntpath.splitdrive(s)
    unc = drive.startswith(("\\\\", "//"))
    if not drive or not (unc or rest[:1] in ("\\", "/")):
        raise ValueError(tr("cfg.path_not_absolute"))
    if any(c in _PATH_BAD_CHARS or ord(c) < 32 for c in rest):
        raise ValueError(tr("cfg.path_bad_chars"))
    out = ntpath.normpath(s)
    if len(out) > MODEL_DIR_MAX:
        raise ValueError(tr("cfg.too_long", max=MODEL_DIR_MAX))
    return out


def _mode_or_none(v: Any) -> str | None:
    if v is None or v == "":
        return None
    if v not in INSERT_MODES:
        raise ValueError(tr("cfg.mode_or_null", modes=", ".join(INSERT_MODES)))
    return str(v)


def _style_or_none(v: Any) -> str | None:
    if v is None or v == "":
        return None
    return _style(v)


def normalize_process(p: Any) -> str:
    if not isinstance(p, str) or not p.strip():
        raise ValueError(tr("cfg.process_empty"))
    name: str = p.strip().lower()
    if len(name) > 260 or any(c in name for c in '\\/:*?"<>|'):
        raise ValueError(tr("cfg.process_bad", name=name))
    return name


def app_rule(process: Any, insert_mode: Any, style: Any) -> dict[str, Any]:
    """Проверенное правило приложения ``{process, insert_mode, style}``."""
    proc = normalize_process(process)
    try:
        mode = _mode_or_none(insert_mode)
    except ValueError as e:
        raise ValueError(tr("cfg.rule_error", process=proc, error=e)) from None
    try:
        st = _style_or_none(style)
    except ValueError as e:
        raise ValueError(tr("cfg.rule_style_error", process=proc, error=e)) from None
    return {"process": proc, "insert_mode": mode, "style": st}


def _app_rules(v: Any, strict: bool = True) -> dict[str, dict[str, str | None]]:
    """Правила приложений. Старый формат значения — строка режима вставки."""
    if not isinstance(v, dict):
        if strict:
            raise ValueError(tr("cfg.app_rules_expect"))
        log.warning("app_rules: не объект — правила сброшены")
        return {}
    out: dict[str, dict[str, str | None]] = {}
    for proc, rule in v.items():
        try:
            if isinstance(rule, str):          # этапы 1–2: {"x.exe": "type"}
                r = app_rule(proc, rule, None)
            elif isinstance(rule, dict):
                r = app_rule(proc, rule.get("insert_mode"), rule.get("style"))
            else:
                raise ValueError(tr("cfg.rule_expect_obj", process=proc))
        except ValueError as e:
            if strict:
                raise
            log.warning("Правило приложения пропущено: %s", e)
            continue
        out[str(r["process"])] = {"insert_mode": r["insert_mode"], "style": r["style"]}
    return out


TITLE_MIN = 2
TITLE_MAX = 260
TITLE_RULES_MAX = 500


def title_rule_process(p: Any) -> str:
    """Процесс правила по названию окна: имя exe или «*» — любое окно."""
    if isinstance(p, str) and p.strip() == "*":
        return "*"
    return normalize_process(p)


def title_rule(process: Any, title: Any, insert_mode: Any, style: Any) -> dict[str, Any]:
    """Проверенное правило по названию окна ``{process, title, insert_mode, style}``."""
    proc = title_rule_process(process)
    if not isinstance(title, str):
        raise ValueError(tr("cfg.title_expect_str", process=proc))
    t = title.strip()
    if len(t) < TITLE_MIN:
        raise ValueError(tr("cfg.title_too_short", process=proc, min=TITLE_MIN))
    if len(t) > TITLE_MAX:
        raise ValueError(tr("cfg.title_too_long", process=proc, max=TITLE_MAX))
    where = tr("cfg.title_rule_where", process=proc, title=t)
    try:
        mode = _mode_or_none(insert_mode)
    except ValueError as e:
        raise ValueError(f"{where}: {e}") from None
    try:
        st = _style_or_none(style)
    except ValueError as e:
        raise ValueError(tr("cfg.where_style_error", where=where, error=e)) from None
    return {"process": proc, "title": t, "insert_mode": mode, "style": st}


def fold_title(title: str) -> str:
    """Название окна для сравнения: без учёта регистра и «ё»."""
    return title.casefold().replace("ё", "е")


def title_rule_key(process: str, title: str) -> tuple[str, str]:
    """Ключ правила: процесс и название без учёта регистра и «ё» — дубли запрещены."""
    return process.lower(), fold_title(title.strip())


def _title_rules(v: Any, strict: bool = True) -> list[dict[str, str | None]]:
    """Правила по названию окна: список; дубли (процесс + название без регистра) запрещены."""
    if not isinstance(v, list):
        if strict:
            raise ValueError(tr("cfg.title_rules_expect"))
        log.warning("title_rules: не список — правила по названию окна сброшены")
        return []
    out: list[dict[str, str | None]] = []
    seen: set[tuple[str, str]] = set()
    for rule in v:
        try:
            if not isinstance(rule, dict):
                raise ValueError(tr("cfg.title_rule_expect"))
            r = title_rule(rule.get("process"), rule.get("title"), rule.get("insert_mode"),
                           rule.get("style"))
            k = title_rule_key(str(r["process"]), str(r["title"]))
            if k in seen:
                raise ValueError(tr("cfg.title_rule_dup", process=r["process"],
                                    title=r["title"]))
        except ValueError as e:
            if strict:
                raise
            log.warning("Правило по названию окна пропущено: %s", e)
            continue
        seen.add(k)
        out.append(r)
    if len(out) > TITLE_RULES_MAX:
        if strict:
            raise ValueError(tr("cfg.too_many_rules", max=TITLE_RULES_MAX))
        out = out[:TITLE_RULES_MAX]
    return out


_VALIDATORS: dict[str, Callable[[Any], Any]] = {
    "ptt_key": _keyset,
    "broadcast_modifier": _key,
    "command_modifier": _key,
    "cancel_key": _key,
    "hotkeys_version": _int(0, 100),
    "tap_max_ms": _int(100, 1000),
    "double_tap_ms": _int(150, 1500),
    "insert_last_hotkey": _chord,
    "mouse_button": _choice(("", "middle", "x1", "x2")),
    "copy_last_hotkey": _chord,
    "input_device": _input_device,
    "min_record_ms": _int(0, 5000),
    "model": _text(max_len=300),
    "device": _choice(DEVICES),
    "compute_type": _choice(COMPUTE_TYPES),
    "model_dir": normalize_model_dir,
    "beam_size": _int(1, 10),
    "en_threshold": _float(0.0, 1.0),
    "dictation_language": _choice(DICTATION_LANGUAGES),
    "terms": _str_list(),
    "unload_after_min": _int(0, 1440),
    "insert_mode": _choice(INSERT_MODES),
    "type_delay_ms": _int(0, 30),
    "rdp_type_delay_ms": _int(0, 30),
    "type_chunk": _int(1, 1000),
    "clipboard_restore_ms": _int(50, 5000),
    "rdp_classes": _str_list(),
    "rdp_processes": _str_list(),
    "app_rules": _app_rules,
    "title_rules": _title_rules,
    "broadcast_targets": _str_list(2000),
    "broadcast_excluded": _str_list(2000),
    "add_trailing_space": _bool,
    "learn_mode": _choice(LEARN_MODES),
    "dict_fuzzy": _bool,
    "vocab_enabled": _bool,
    "context_enabled": _bool,
    "duck_mode": _choice(DUCK_MODES),
    "duck_level": _int(0, 100),
    "sounds": _bool,
    "autostart": _bool,
    "default_style": _style,
    "history_enabled": _bool,
    "history_retention_days": _int(0, 36500),
    "pill_enabled": _bool,
    "pill_position": _pill_position,
    "theme": _choice(THEMES),
    "accent": _accent,
    "language": _choice(LANGUAGES),
    "first_run_done": _bool,
    "ai_enabled": _bool,
    "ai_provider": _choice(AI_PROVIDERS),
    "ai_model": _text(max_len=200),
    "anthropic_model": _text(max_len=200),
    "ollama_model": _text(max_len=200),
    "ollama_url": _url,
    "ai_provider_order": _str_list(20),
    "ai_mode": _choice(AI_MODES),
    "ai_min_words": _int(0, 1000),
    "ai_timeout_s": _float(1, 60),
    "command_provider": _choice(COMMAND_PROVIDERS),
    "env_path": _text(allow_empty=True, max_len=1000),
}

def label(name: str) -> str:
    """Название настройки для сообщений об ошибках (каталог: setting.<имя>)."""
    return i18n.tr_or(f"setting.{name}", name)


def _check_hotkeys(s: Settings) -> None:
    """Клавиши жестов не должны пересекаться: «правый Alt» конфликтует и с «alt»,
    и с сочетанием «ctrl+alt»."""
    from .hotkeys import key_group, key_label, parse_keyset
    names = {"ptt_key": s.ptt_key, "broadcast_modifier": s.broadcast_modifier,
             "command_modifier": s.command_modifier, "cancel_key": s.cancel_key}
    seen: dict[int, str] = {}
    for k, v in names.items():
        vks = frozenset().union(*parse_keyset(v)) if k == "ptt_key" else key_group(v)
        for vk in vks:
            if vk in seen and seen[vk] != k:
                raise ValueError(tr("cfg.hotkey_conflict", label=label(k),
                                    other=label(seen[vk]), key=key_label(v)))
            seen[vk] = k
    if s.insert_last_hotkey and s.insert_last_hotkey == s.copy_last_hotkey:
        raise ValueError(tr("cfg.chords_same"))


def apply_patch(s: Settings, patch: Any) -> list[str]:
    """Проверить патч и применить к ``s`` на месте. Возвращает список изменившихся ключей.

    Любая ошибка — ``ValueError`` на языке интерфейса, ``s`` при этом не меняется."""
    if not isinstance(patch, dict):
        raise ValueError(tr("cfg.patch_not_object"))
    known = {f.name for f in fields(Settings)}
    cand = copy.deepcopy(s)
    for k, v in patch.items():
        if k not in known:
            raise ValueError(tr("cfg.unknown_setting", key=k))
        try:
            setattr(cand, k, _VALIDATORS[k](v))
        except ValueError as e:
            raise ValueError(f"{label(k)}: {e}") from None
    _check_hotkeys(cand)
    changed: list[str] = []
    for k in patch:
        new = getattr(cand, k)
        if getattr(s, k) != new:
            changed.append(k)
            setattr(s, k, copy.deepcopy(new))
    return changed


def to_dict(s: Settings) -> dict[str, Any]:
    return asdict(s)


def settings_path() -> Path:
    return app_dir() / "settings.json"


_OLD_DEFAULT_HOTKEYS = {1: ("rctrl", "rshift", "ralt"), 2: ("ctrl+alt", "shift", "win")}


def _migrate_hotkeys(s: Settings, raw: dict[str, Any]) -> None:
    """Файл с нетронутыми умолчаниями прежней версии (v1: правые Ctrl/Shift/Alt, v2: Ctrl+Alt /
    Shift / Win) → текущие Ctrl+Win / Shift / Alt. Переводим, только если новая тройка не
    конфликтует с остальными клавишами пользователя."""
    ver = raw.get("hotkeys_version", 1)
    if not isinstance(ver, int) or isinstance(ver, bool):
        ver = 1
    d = Settings()
    s.hotkeys_version = d.hotkeys_version
    if ver >= d.hotkeys_version:
        return
    defaults_then = _OLD_DEFAULT_HOTKEYS.get(ver, _OLD_DEFAULT_HOTKEYS[1])
    old = (raw.get("ptt_key", defaults_then[0]), raw.get("broadcast_modifier", defaults_then[1]),
           raw.get("command_modifier", defaults_then[2]))
    if old not in _OLD_DEFAULT_HOTKEYS.values():
        return
    cand = copy.deepcopy(s)
    cand.ptt_key, cand.broadcast_modifier, cand.command_modifier = (
        d.ptt_key, d.broadcast_modifier, d.command_modifier)
    try:
        _check_hotkeys(cand)
    except (ValueError, TypeError, AttributeError) as e:
        # значения из файла ещё не проверены (validate — позже): cancel_key: null и т. п.
        log.warning("Хоткеи не переведены на новые умолчания: %s", e)
        return
    s.ptt_key, s.broadcast_modifier, s.command_modifier = (
        cand.ptt_key, cand.broadcast_modifier, cand.command_modifier)
    log.info("Хоткеи переведены на новые умолчания: %s, +%s, +%s",
             s.ptt_key, s.broadcast_modifier, s.command_modifier)


def _migrate_broadcast(s: Settings) -> None:
    """Файл до 0.4 (нет broadcast_targets): рассылка была «во все, кроме снятых галочек».
    Отмечаем открытые сейчас RDP-сессии, кроме исключённых (по заголовку или ключу);
    окон нет — пустой список (рассылка попросит выбрать сессии)."""
    from . import winutil
    try:
        wins = winutil.list_rdp_windows(s.rdp_classes, s.rdp_processes)
    except Exception as e:  # ошибка перебора окон — просто без отметок
        log.warning("Рассылка: открытые RDP-сессии не получены (%s)", type(e).__name__)
        wins = []
    excluded = {t.casefold() for t in s.broadcast_excluded}
    targets: list[str] = []
    for wi in wins:
        key = winutil.rdp_session_key(wi.title, wi.process, wi.cls)
        if (not key or wi.title.casefold() in excluded or key.casefold() in excluded
                or any(key.casefold() == t.casefold() for t in targets)):
            continue
        targets.append(key)
    s.broadcast_targets = targets
    log.info("Рассылка: переход на выбранные сессии — отмечено %d из %d открытых",
             len(targets), len(wins))


def _keep_broken(p: Path) -> None:
    """Нечитаемый settings.json не затираем умолчаниями молча: откладываем рядом копию
    (settings.broken-<время>.json) — правила, хоткеи и цели рассылки можно вернуть руками."""
    dst = p.with_name(f"{p.stem}.broken-{time.strftime('%Y%m%d-%H%M%S')}{p.suffix}")
    try:
        p.replace(dst)
        log.error("Испорченный файл настроек сохранён как %s", dst)
    except OSError as e:
        log.error("Испорченный файл настроек не отложен (%s)", e)


def load() -> Settings:
    p = settings_path()
    s = Settings()
    migrate_rdp = False
    write = True
    text: str | None = None
    if p.exists():
        try:
            text = p.read_text(encoding="utf-8")
        except OSError as e:
            # файл занят (антивирус, OneDrive) — работаем на умолчаниях, но файл не трогаем:
            # иначе временная блокировка стёрла бы все настройки пользователя
            log.error("Не удалось прочитать %s: %s — беру настройки по умолчанию", p, e)
            write = False
    if text is not None:
        try:
            raw: Any = json.loads(text)
            if not isinstance(raw, dict):
                raise ValueError(tr("cfg.expect_json_object"))
            known = {f.name for f in fields(Settings)}
            for k, v in raw.items():
                if k in known:
                    setattr(s, k, v)
            _migrate_hotkeys(s, raw)
            migrate_rdp = "broadcast_targets" not in raw
            if "default_style" not in raw and isinstance(s.app_rules, dict):
                # Файл этапов 1–2: дописываем стили по приложениям, не трогая правила пользователя
                for proc, rule in DEFAULT_APP_RULES.items():
                    s.app_rules.setdefault(proc, dict(rule))
        except ValueError as e:
            log.error("Не удалось прочитать %s: %s — беру настройки по умолчанию", p, e)
            s = Settings()
            migrate_rdp = False
            _keep_broken(p)
    s.validate()
    if migrate_rdp:
        _migrate_broadcast(s)
    if write:
        save(s)  # дописать новые ключи со значениями по умолчанию
    return s


_SAVE_LOCK = threading.Lock()


def save(s: Settings) -> None:
    p = settings_path()
    data = json.dumps(asdict(s), ensure_ascii=False, indent=2)
    with _SAVE_LOCK:
        tmp = p.with_suffix(".tmp")
        tmp.write_text(data, encoding="utf-8")
        tmp.replace(p)
