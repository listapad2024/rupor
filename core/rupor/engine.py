"""Движок: хоткеи → запись → распознавание → словарь/сниппеты → ИИ → вставка → история.

Хук клавиатуры только кладёт действия в очередь; всё тяжёлое идёт в рабочем потоке
(распознавание, ИИ, вставка — строго по очереди, чтобы тексты не перемешались).
Состояние и события публикуются в шину (Bus): её слушают WebSocket-сервер (UI на Tauri)
и трей старого режима. Имена событий и поля — как в docs/ARCHITECTURE.md.

Поток одной диктовки:
  1. ASR → raw;
  2. словарь (_dict_apply: точные варианты, затем, если dict_fuzzy, похожие по звучанию
     написания терминов — fuzzy.py) → expand_snippets (сработал сниппет — ИИ пропускается);
  3. стиль = правило приложения → default_style; ИИ, если включён, стиль ≠ as_is,
     слов ≥ ai_min_words и есть ключ провайдера:
       wait      — состояние "ai", ошибка/таймаут → вставить текст без ИИ + notice;
       raw_first — сразу вставить, ИИ в фоне; по приходу заменить (Shift+← и печать),
                   только если после вставки не было нажатий и окно то же;
     ответ ИИ ещё раз проходит через словарь — словарь пользователя последнее слово, а
     ошибки распознавания (всё, что словарь заменил бы термином) не попадают ни в
     подсказку Whisper, ни в термины ИИ;
  4. вставка, событие result, история (если включена) и статистика (всегда);
  5. обучение словаря: до минуты следим за полем, куда вставили текст (UI Automation),
     и правки, похожие на ошибку распознавания («метабейс» → Metabase), добавляем
     в словарь с кнопкой «Отменить» или предлагаем добавить (learn_mode, см. _LearnWatch
     и learn.py); что добавлено — в журнале обучения (Storage.dict_learn_journal).
Текст никогда не теряется: при любой ошибке вставляется то, что есть, и всё пишется в историю.

Звук компьютера (duck_mode, duck.py): приглушается, когда запись подтверждена (после звука
начала; удержание, «без рук», пилюля, кнопка мыши, проверочная запись мастера), и
возвращается, когда запись кончилась (стоп, отмена, выброс, лимит, пауза, смена микрофона
или хоткеев), при смене duck_mode/duck_level, при выходе (сразу по команде quit — до
завершения ядра) и в конце сеанса Windows (session_ending, WM_QUERYENDSESSION).

Вставка только туда, где диктовали: если к моменту вставки активно другое окно (или фокус
ушёл посреди печати), текст не вставляется, а остаётся в «Вставить последнее» и истории.

Тексты уведомлений и ошибок — на языке интерфейса (i18n.tr, настройка language); смена
языка применяется сразу (State.ui_language, событие state).
"""
from __future__ import annotations

import collections
import copy
import inspect
import json
import logging
import os
import queue
import threading
import time
import dataclasses
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Protocol

import numpy as np

from . import (ai, audio, broadcast, config, credman, duck, fetch, fuzzy, i18n, inject,
               learn, recommend, sounds, spell, textproc, uia, vocab, winutil)
from . import _win
from ._win import VK_RIGHT
from .asr import (SAMPLE_RATE, ModelMissing, Recognizer, Result, cuda_compute_types,
                  cuda_device_count, gpu_info)
from .config import MODE_AUTO, MODE_CLIPBOARD, MODE_TYPE, Settings
from .hotkeys import (Action, Chord, GestureMachine, KeyboardHook, KeyGroup, Kind, key_group,
                      parse_chord, parse_ptt)
from .i18n import tr
from .storage import Storage

log = logging.getLogger(__name__)

LANG_PREFETCH_S = 2.5   # через сколько секунд записи определять язык заранее
CONFIRM_MS = 250        # после этого удержание считается диктовкой (звук, индикатор)
LEVEL_INTERVAL_S = 1 / 30  # события level — не чаще 30 раз в секунду
RAW_FIRST_MAX_CHARS = 1500  # длиннее — «сначала сырой» не заменяет текст
SELECTION_TIMEOUT_MS = 400
SELECTION_TIMEOUT_RDP_MS = 1000  # буфер RDP синхронизируется через rdpclip — дольше
PURGE_INTERVAL_S = 3600
RECENT_APPS = 20
MAX_RECORD_S = 600          # запись дольше 10 минут останавливается (потерянное отпускание,
                            # забытый режим «без рук»)
TEST_RECORD_MAX_S = 60      # проверочная запись мастера — не дольше минуты
MIC_DEAD_HOLD_S = 1.0       # удержание дольше этого, а звука почти нет — микрофон отвалился
MIC_DEAD_RATIO = 0.3        # …если записано меньше этой доли от длительности удержания
LEARN_WATCH_S = 60.0        # обучение словаря: столько следим за полем после вставки
LEARN_POLL_S = 0.4          # …опрашивая его так часто
LEARN_POLL_FAST_S = 0.1     # …а пока поле меняется (печатают) — чаще
LEARN_FAST_FOR_S = 2.0      # «меняется» — столько секунд после последнего изменения
LEARN_MIN_HELD_S = 0.3      # правка держалась в поле меньше — не учим (слово ещё печатали:
                            # Enter, Alt+Tab или хоткей сразу после буквы)
LEARN_SNAPSHOTS = 64        # сколько последних снимков поля помнить (≥ 6 с при частом опросе)
LEARN_BASELINE_S = 2.0      # ждать, пока вставленный текст появится в поле
LEARN_BASELINE_POLL_S = 0.15
LEARN_SETTLE_S = 2.0        # время вышло, а поле менялось только что (печатают) — подождать
LEARN_EXTRA_S = 15.0        # …но не дольше этого сверх LEARN_WATCH_S
LEARN_REPEAT = 2            # режим suggest: на такой по счёту одинаковой правке — добавить самому
LEARN_KV_MAX = 300          # сколько правок помнить (счётчики повторов, отменённые)
LEARN_COUNTS_KEY = "learn_counts"      # kv: {"вариант→термин": сколько раз}
LEARN_REJECTED_KEY = "learn_rejected"  # kv: ["вариант→термин", …] — отменены «Отменить»
RECENT_TITLES = 10          # recent_apps: столько последних заголовков окон процесса (в памяти)
CONTEXT_TIMEOUT_S = 0.3     # контекст окна (текст перед курсором) — не дольше, иначе без него
SESSION_END_WAIT_S = 1.5    # конец сеанса Windows: ждать возврата звука компьютера не дольше
BALANCE_TTL_S = 300.0       # баланс OpenRouter кэшируется на 5 минут (force — мимо кэша)
BALANCE_ERROR_TTL_S = 60.0  # …а неудачная проверка — на минуту
LOW_BALANCE_USD = 1.0       # меньше — предупреждение «пополните»
LOW_BALANCE_EVERY_S = 86400.0   # …не чаще раза в сутки
LOW_BALANCE_KEY = "ai_low_balance_notice"   # kv: когда предупреждали (unix, с)
OPENROUTER = "openrouter"

# Задания, после которых состояние рассылается, даже если не изменилось
_FORCE_STATE_JOBS = frozenset({"transcribe", "load", "reload", "model_downloaded",
                               "cuda_downloaded"})

# Состояния для UI (State.state)
IDLE, LOADING, RECORDING, HANDS_FREE, PROCESSING, AI, PAUSED, ERROR = (
    "idle", "loading", "recording", "hands_free", "processing", "ai", "paused", "error")

# События, которые уходят в UI (остальные — внутренние, например "ready" для трея)
PUBLIC_EVENTS = frozenset({"hello", "state", "level", "recording", "broadcast", "result",
                           "history", "notice", "progress", "settings", "ai_usage"})


def _version() -> str:
    try:
        from importlib.metadata import version
        return version("rupor")
    except Exception:
        return "0"


VERSION = _version()


class Bus:
    """Шина событий: emit(event, data) → все подписчики (из потока, который вызвал emit)."""

    def __init__(self) -> None:
        self._subs: list[Callable[[str, dict[str, Any]], None]] = []
        self._lock = threading.Lock()

    def subscribe(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            self._subs.append(fn)

    def unsubscribe(self, fn: Callable[[str, dict[str, Any]], None]) -> None:
        with self._lock:
            if fn in self._subs:
                self._subs.remove(fn)

    def emit(self, event: str, data: dict[str, Any] | None = None, **kw: Any) -> None:
        payload = dict(data or {}, **kw)
        with self._lock:
            subs = list(self._subs)
        for fn in subs:
            try:
                fn(event, payload)
            except Exception:
                log.exception("Подписчик шины упал на %s", event)


# ---------- маршрутизация и вставка ----------

def _rule(info: winutil.WindowInfo | None, s: Settings) -> dict[str, str | None]:
    if info is None:
        return {}
    return s.app_rules.get(info.process.lower()) or {}


def title_rules_for(info: winutil.WindowInfo | None, s: Settings) -> list[dict[str, str | None]]:
    """Правила по названию окна, подходящие к окну: процесс совпал (или «*»), название —
    подстрока заголовка без учёта регистра и «ё». Порядок: длинное название первым, при
    равной длине — правило процесса раньше «*»."""
    if info is None or not s.title_rules or not info.title:
        return []
    proc = info.process.lower()
    title = config.fold_title(info.title)
    hits = [r for r in s.title_rules
            if r.get("process") in (proc, "*")
            and config.fold_title(str(r.get("title") or "")) in title]
    hits.sort(key=lambda r: (-len(str(r.get("title") or "")), r.get("process") == "*"))
    return hits


def _pick(info: winutil.WindowInfo | None, s: Settings, name: str) -> str | None:
    """Значение поля правила (контракт: «при нескольких — самое длинное title; null — брать
    следующий уровень»): действует только одно правило по названию окна — самое длинное
    подходящее (при равной длине — правило процесса, а не «*»). Его null («Не менять») —
    как у приложения: правило приложения, затем настройки (None). Более короткие правила
    по названию окна при этом не участвуют."""
    hits = title_rules_for(info, s)
    if hits:
        v = hits[0].get(name)
        if v:
            return v
    return _rule(info, s).get(name) or None


def resolve_mode(info: winutil.WindowInfo | None, s: Settings) -> str:
    """Способ вставки: правило по названию окна > правило приложения > режим из настроек."""
    if info is None:
        return MODE_TYPE
    mode = _pick(info, s, "insert_mode") or s.insert_mode
    if mode == MODE_AUTO:
        return MODE_TYPE if _is_remote_info(info, s) else MODE_CLIPBOARD
    return mode


def resolve_style(info: winutil.WindowInfo | None, s: Settings) -> str:
    """Стиль: правило по названию окна (самое длинное) > правило приложения > стиль по
    умолчанию."""
    return _pick(info, s, "style") or s.default_style


def _cleanup_kwargs(language: str | None) -> dict[str, Any]:
    """Язык текста для ai.cleanup (английская диктовка — английский промпт), если ai.cleanup
    его принимает: параметр language (lang) или **kwargs."""
    if not language:
        return {}
    try:
        params = inspect.signature(ai.cleanup).parameters
    except (TypeError, ValueError):
        return {}
    for name in ("language", "lang"):
        if name in params:
            return {name: language}
    if any(p.kind is inspect.Parameter.VAR_KEYWORD for p in params.values()):
        return {"language": language}
    return {}


def styles_localized() -> list[dict[str, str]]:
    """Встроенные стили на языке интерфейса (WS styles_list, трей): [{id, name, description}].
    Тексты — ai.styles_public() (каталог: ai.style.<id>.name/.description); без перевода —
    как в ai.STYLES."""
    out: list[dict[str, str]] = []
    for st in ai.styles_public():
        sid = str(st["id"])
        out.append({"id": sid,
                    "name": i18n.tr_or(f"ai.style.{sid}.name", str(st.get("name") or sid)),
                    "description": i18n.tr_or(f"ai.style.{sid}.description",
                                              str(st.get("description") or ""))})
    return out


# Служебные записи истории (проверка распознавания в мастере, рассылка в RDP без
# адресатов) хранятся с нейтральными app/title, не зависящими от языка, а подписи
# подставляются на языке интерфейса при выдаче (history_list, событие history): после смены
# языка история не становится двуязычной. Прежние версии сохраняли подписи готовым текстом —
# они узнаются по каталогам всех языков и тоже переводятся.
HISTORY_APP_SELF = "rupor"


def _catalog_values(key: str) -> frozenset[str]:
    return frozenset(i18n.tr_in(lang, key).casefold() for lang in i18n.UI_LANGS)


def history_display(item: dict[str, Any]) -> dict[str, Any]:
    """Запись истории для выдачи: служебные app/title — на текущем языке интерфейса."""
    target = item.get("target")
    if target not in ("test", "rdp_all"):
        return item
    out = dict(item)
    app = str(out.get("app") or "")
    title = str(out.get("title") or "")
    if target == "test":
        if not app or app.casefold() in _catalog_values("app.name") | {HISTORY_APP_SELF}:
            out["app"] = tr("app.name")
        if not title or title.casefold() in _catalog_values("hist.test_title"):
            out["title"] = tr("hist.test_title")
    elif not title or title.casefold() in _catalog_values("hist.rdp_title"):
        out["title"] = tr("hist.rdp_title")
    return out


def finalize_text(text: str, trailing_space: bool) -> str:
    text = text.strip()
    if text and trailing_space and not text.endswith(("\n", " ")):
        text += " "
    return text


@dataclass
class Delivery:
    """Итог вставки: куда и как вставлено (нужно для замены в режиме «сначала сырой»)."""
    ok: bool
    hwnd: int = 0
    mode: str = ""
    info: winutil.WindowInfo | None = None
    sent: list[str] = field(default_factory=list)  # рассылка: ключи RDP-сессий


def _is_rdp_info(info: winutil.WindowInfo | None, s: Settings) -> bool:
    return info is not None and winutil.is_rdp(info.cls, info.process, s.rdp_classes,
                                               s.rdp_processes)


def _is_remote_info(info: winutil.WindowInfo | None, s: Settings) -> bool:
    """RDP или другое удалённое окно (RemoteApp, Citrix, консоль ВМ): печать, а не буфер."""
    return info is not None and (_is_rdp_info(info, s) or winutil.is_remote_typing(info))


def deliver(text: str, s: Settings, to_all: bool,
            notice: Callable[..., None]) -> Delivery:
    """Вставить текст в активное окно (или разослать во все RDP-сессии).

    Если активное окно сменится посреди печати, остаток не уходит в чужое окно
    (inject.FocusLost)."""
    if to_all:
        rep = broadcast.send_all(text, s)
        sent, failed = len(rep.sent), len(rep.failed)
        if not sent and not failed:
            notice(tr("eng.rdp_none"), level="warn", action="open_settings_insert")
        elif not sent:
            notice(i18n.tp("eng.rdp_failed", failed), level="warn")
        elif failed:
            notice(i18n.tp("eng.rdp_sent_partial", sent + failed, sent=sent, failed=failed),
                   level="warn")
        else:
            notice(i18n.tp("eng.rdp_sent", sent), toast=True)
        return Delivery(ok=bool(rep.sent), mode=MODE_TYPE, sent=list(rep.sent))
    info = winutil.foreground()
    if info and winutil.blocked_by_uipi(info):
        notice(tr("eng.window_admin", name=info.title or info.process),
               level="warn", action="relaunch_admin")
        return Delivery(ok=False, hwnd=info.hwnd, info=info)
    mode = resolve_mode(info, s)
    hwnd = info.hwnd if info else None
    if mode == MODE_CLIPBOARD:
        inject.paste_text(text, s.clipboard_restore_ms, expect_hwnd=hwnd)
    else:
        inject.type_text(text, delay_ms=s.rdp_type_delay_ms if _is_remote_info(info, s)
                         else s.type_delay_ms, chunk=s.type_chunk, expect_hwnd=hwnd)
    log.info("Вставлено %d симв. в %s (%s)", len(text), info.process if info else "?", mode)
    return Delivery(ok=True, hwnd=info.hwnd if info else 0, mode=mode, info=info)


def _same_text(a: str | None, b: str) -> bool:
    """Выделение (из буфера: переводы строк \\r\\n) совпадает с тем, что вставлено."""
    if a is None:
        return False
    return a.replace("\r\n", "\n").replace("\r", "\n") == b.replace("\r\n", "\n")


def replace_text(n_back: int, text: str, d: Delivery, s: Settings, expected: str) -> bool:
    """Заменить n_back символов слева от курсора на text: выделить Shift+←, убедиться, что
    выделено ровно expected (то, что вставляли), и вставить поверх.

    Выделено не то (щелчок мышью в другое место, автозамена «...»→«…», диапазон ячеек
    Excel, пустое поле после «Отправить») — выделение снимается (→), False: чужой текст
    не затирается."""
    delay = s.rdp_type_delay_ms if _is_remote_info(d.info, s) else 0
    hwnd = d.hwnd or None
    inject.select_left(n_back, delay_ms=delay, chunk=s.type_chunk, expect_hwnd=hwnd)
    timeout = SELECTION_TIMEOUT_RDP_MS if _is_remote_info(d.info, s) else SELECTION_TIMEOUT_MS
    try:
        got = inject.copy_selection(timeout, via=_copy_vias(d.info, s))
    except OSError as e:
        log.warning("Не удалось проверить выделение перед заменой: %s", e)
        got = None
    if not _same_text(got, expected):
        inject.press_key(VK_RIGHT)
        log.info("Замена отменена: выделено не то, что вставлялось (%s)",
                 "не прочитать" if got is None else f"{len(got)} симв.")
        return False
    if d.mode == MODE_CLIPBOARD:
        inject.paste_text(text, s.clipboard_restore_ms, expect_hwnd=hwnd)
    else:
        inject.type_text(text, delay_ms=delay or s.type_delay_ms, chunk=s.type_chunk,
                         expect_hwnd=hwnd)
    log.info("Текст заменён очищенным: %d → %d симв.", n_back, len(text))
    return True


def read_selection(info: winutil.WindowInfo, s: Settings) -> str | None:
    """Выделенный текст активного окна с восстановлением буфера.

    Сначала Ctrl+Insert (копирует в обычных полях, браузерах, Office, IDE и терминалах и не
    шлёт SIGINT). Ctrl+C — только если Ctrl+Insert не сработал и окно не из тех, где Ctrl+C
    может прервать процесс (терминалы, встроенные терминалы IDE, RDP и менеджеры
    подключений, консоли ВМ). Буфер занят — OSError."""
    timeout = SELECTION_TIMEOUT_RDP_MS if _is_remote_info(info, s) else SELECTION_TIMEOUT_MS
    return inject.copy_selection(timeout, via=_copy_vias(info, s))


def _copy_vias(info: winutil.WindowInfo | None, s: Settings) -> tuple[str, ...]:
    """Чем копировать выделение: Ctrl+Insert, а Ctrl+C — запасной путь только там, где он
    не может оказаться прерыванием процесса."""
    if info is None or _is_remote_info(info, s) or winutil.ctrl_c_unsafe(info):
        return (inject.COPY_INSERT,)
    return (inject.COPY_INSERT, inject.COPY_CTRL_C)


# ---------- подменяемые части (тесты, e2e) ----------

class RecorderLike(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def start(self) -> None: ...
    def stop(self) -> np.ndarray: ...
    def snapshot(self) -> np.ndarray: ...


class RecognizerLike(Protocol):
    device: str | None
    last_error: str | i18n.Msg | None
    last_used: float
    prompt_provider: Callable[[], str | None]

    @property
    def loaded(self) -> bool: ...
    def load(self) -> None: ...
    def unload(self) -> None: ...
    def detect_language(self, audio: np.ndarray) -> str: ...
    def transcribe(self, audio: np.ndarray, language: str | None = None) -> Result: ...


RecorderFactory = Callable[[str | None, Callable[[float], None]], RecorderLike]


class FieldLike(Protocol):
    """Поле ввода с фокусом (uia.Field или фейк в тестах)."""
    @property
    def runtime_id(self) -> tuple[int, ...]: ...
    @property
    def pid(self) -> int: ...
    def read_text(self) -> str | None: ...


class DuckerLike(Protocol):
    """Приглушение звука компьютера (duck.Ducker или фейк в тестах). duck/restore не
    блокируют; flush — дождаться выполненного (не дольше timeout); close — вернуть звук при
    выходе (ждёт не дольше timeout)."""
    def start(self) -> None: ...
    def duck(self, mode: str, level: int) -> None: ...
    def restore(self) -> None: ...
    def flush(self, timeout: float = ...) -> bool: ...
    def close(self, timeout: float = ...) -> bool: ...


FieldReader = Callable[[], FieldLike | None]
# Текст перед курсором в поле с фокусом: (pid, текст) или None (uia.caret_context / фейк)
ContextReader = Callable[[], tuple[int, str] | None]


def _default_recorder(device: str | None, on_level: Callable[[float], None]) -> RecorderLike:
    return audio.Recorder(device, on_level=on_level)


def _uia_context() -> tuple[int, str] | None:
    return uia.caret_context(timeout=CONTEXT_TIMEOUT_S)


@dataclass
class _Session:
    id: int
    started: float           # monotonic
    started_wall: float      # unix, с
    broadcast: bool = False
    command: bool = False
    confirmed: bool = False
    hands_free: bool = False
    language: str | None = None
    timers: list[threading.Timer] = field(default_factory=list)
    # Контекст окна (1–2 предложения перед курсором) — читается в фоне при старте записи
    context: str | None = None
    context_pid: int = 0        # процесс окна, из которого прочитан контекст
    context_done: threading.Event = field(default_factory=threading.Event)


@dataclass
class _Job:
    sess: _Session
    audio: np.ndarray
    t_release: float                     # perf_counter в момент отпускания
    target: winutil.WindowInfo | None    # активное окно в момент отпускания


@dataclass
class _RawFirst:
    job: _Job
    res: Result
    raw: str
    text: str            # после словаря/сниппетов (без ИИ)
    out: str             # что реально вставлено (с пробелом в конце)
    style: str
    latency_ms: float
    delivery: Delivery
    presses: int         # счётчик нажатий ДО вставки: нажатия во время печати — тоже «после»
    terms: list[str]
    focus: int | None = None   # окно с фокусом ввода сразу после вставки


class Engine:
    def __init__(self, settings: Settings, storage: Storage | None = None, *,
                 own_pids: tuple[int, ...] | list[int] | set[int] = (),
                 auto_download_model: bool = True,
                 recognizer: RecognizerLike | None = None,
                 recorder_factory: RecorderFactory | None = None,
                 field_reader: FieldReader | None = None,
                 context_reader: ContextReader | None = None,
                 ducker: DuckerLike | None = None) -> None:
        self.s = settings
        # Язык сообщений ядра — из настроек (system → язык Windows); меняется на лету
        i18n.set_language(getattr(settings, "language", i18n.SYSTEM))
        self.bus = Bus()
        # background: пересборка частот слов и обновление их кэша — в фоне (запуск ядра
        # и подсказка распознаванию их не ждут)
        self.storage = storage if storage is not None else Storage(background=True)
        if self.storage.recovered is not None:
            log.warning("База была повреждена и пересоздана: %s", self.storage.recovered)
        self.own_pids = {os.getpid(), *(p for p in own_pids if p)}
        self.auto_download_model = auto_download_model
        self.rec: RecognizerLike = recognizer or Recognizer(settings, on_notice=self._rec_notice)
        self.rec.prompt_provider = self._asr_prompt
        self._make_recorder: RecorderFactory = recorder_factory or _default_recorder
        self.recorder: RecorderLike = self._make_recorder(settings.input_device, self._on_level)
        self.machine = GestureMachine(*self._machine_args())
        self.hook = KeyboardHook(self.machine, self._on_actions, rehook_when=self._is_rdp_hwnd)
        self.focus = winutil.ForegroundWatcher(self._on_foreground)
        self._ctl: queue.Queue[list[Action] | None] = queue.Queue()
        self._jobs: queue.Queue[tuple[str, Any] | None] = queue.Queue()
        self._lock = threading.RLock()        # сессия, тестовая запись, счётчики
        self._state_lock = threading.RLock()  # порядок событий state
        self._settings_lock = threading.RLock()
        self._ctl_lock = threading.RLock()    # обработка жестов (поток хука и настройки)
        self._session: _Session | None = None
        self._seq = 0
        self._test_started: float | None = None   # unix, с — идёт проверочная запись
        self._test_timer: threading.Timer | None = None
        self._paused = False
        self._busy = 0            # диктовок в очереди/в работе
        self._ai_busy = False
        self._model_phase = "loading"   # loading | ready | error | unloaded
        self._model_error: str | i18n.Msg | None = None
        self.state = LOADING
        self.last_text = ""
        self._level_t = 0.0
        self._stop = threading.Event()
        self._last_foreign: winutil.WindowInfo | None = None
        # Недавние приложения: процесс → {process, title, titles} (заголовки — только в памяти)
        self._recent: collections.OrderedDict[str, dict[str, Any]] = collections.OrderedDict()
        self._downloading: set[str] = set()
        # Идёт перенос модели в другую папку (model_dir_set с move): занят слот "model"
        # в _downloading — загрузка модели в это время не начнётся
        self._moving = False
        # Загрузку модели просили, пока слот "model" держал перенос: повторить после него
        self._model_dl_deferred = False
        # Новая модель качается, а распознаёт прежняя: по окончании скачивания — подменить
        self._swap_pending = False
        self._elevated = winutil.self_elevated()
        self._nokey_warned = False
        self._last_purge = 0.0
        self._started = False
        self.on_quit: Callable[[], None] | None = None
        # Обучение словаря: чем читать поле с фокусом (UI Automation), текущее наблюдение
        self._field_reader: FieldReader = field_reader or uia.focused_field
        self._learn: _LearnWatch | None = None
        self._learn_kv_lock = threading.Lock()
        # Подсказка распознаванию: контекст окна (uia) и задание, которое сейчас распознаётся
        self._context_reader: ContextReader = context_reader or _uia_context
        self._prompt_local = threading.local()
        # Баланс OpenRouter: кэш ответа ai_balance и замок на время запроса
        self._balance: dict[str, Any] | None = None
        self._balance_at = 0.0          # monotonic — когда получен кэш
        self._balance_lock = threading.Lock()
        self._balance_fetching = threading.Lock()
        # Звук компьютера во время записи: сессии процессов Рупора (ядро играет звуки начала
        # и конца, own_pids) не трогаются; _ducked — приглушение запрошено (под self._lock)
        self.ducker: DuckerLike = (ducker if ducker is not None
                                   else duck.Ducker(own_pids=self.own_pids))
        self._ducked = False

    # ---------- жизненный цикл ----------

    def _machine_args(self) -> tuple[tuple[KeyGroup, ...], KeyGroup, KeyGroup, int, int,
                                     KeyGroup, Chord | None, Chord | None]:
        s = self.s
        il = parse_chord(s.insert_last_hotkey) if s.insert_last_hotkey else None
        cl = parse_chord(s.copy_last_hotkey) if s.copy_last_hotkey else None
        return (parse_ptt(s.ptt_key), key_group(s.broadcast_modifier), key_group(s.cancel_key),
                s.tap_max_ms, s.double_tap_ms, key_group(s.command_modifier), il, cl)

    def start(self, hook: bool = True, track_focus: bool = True) -> None:
        if self._started:
            return
        self._started = True
        try:
            sounds.prepare()
        except Exception:
            log.exception("Не удалось подготовить звуки")
        threading.Thread(target=self._ctl_loop, name="rupor-ctl", daemon=True).start()
        threading.Thread(target=self._job_loop, name="rupor-asr", daemon=True).start()
        threading.Thread(target=self._idle_loop, name="rupor-idle", daemon=True).start()
        try:
            # поток rupor-duck первым делом возвращает звук, оставшийся приглушённым после
            # падения или выключения (duck.json); запуск ядра этого не ждёт
            self.ducker.start()
        except Exception:
            log.exception("Звук компьютера: приглушение не запустилось")
        if track_focus:
            self.focus.start()
            fg = winutil.foreground()
            if fg:
                self._on_foreground(fg.hwnd)
        self._refresh_state(force=True)
        self._jobs.put(("load", None))
        self._open_recorder()
        if ((self._field_reader is uia.focused_field and self.s.learn_mode != "off")
                or (self._context_reader is _uia_context and self.s.context_enabled)):
            # UI Automation — заранее: первое наблюдение за полем и первый контекст окна
            # не ждут импорта comtypes
            threading.Thread(target=uia.available, name="rupor-uia-warm", daemon=True).start()
        if hook:
            # печать ждёт, пока пользователь держит клавишу диктовки (следующая фраза)
            inject.set_hold_probe(self.machine.is_held)
            self.hook.mouse_button = self.s.mouse_button
            self.hook.start()
        if self.storage.recovered is not None:
            self.notice(tr("eng.db_recovered"), level="warn")

    def shutdown(self) -> None:
        self._stop.set()
        with self._lock:
            # звук — первым делом (поток rupor-duck): остальное завершение его не ждёт
            self._duck_off()
        self._learn_stop("выход", evaluate=False)
        self.hook.stop()
        self.focus.stop()
        try:
            self.ducker.close()     # дождаться возврата (не успел — вернёт следующий запуск)
        except Exception:
            log.exception("Звук компьютера: не удалось вернуть громкость при выходе")
        try:
            self.recorder.close()
        except Exception:
            log.exception("Не удалось закрыть микрофон")
        self._ctl.put(None)
        self._jobs.put(None)

    def request_quit(self, delay_s: float = 0.2) -> None:
        """Команда «Выход» из UI: ответ успевает уйти, затем ядро завершается. Звук
        компьютера, приглушённый записью, начинает возвращаться сразу: оболочка ждёт выхода
        ядра недолго и потом снимает процесс."""
        cb = self.on_quit
        if cb is None:
            log.info("Выход запрошен, но обработчик не задан")
            return
        with self._lock:
            self._duck_off()
        t = threading.Timer(delay_s, cb)
        t.daemon = True
        t.start()

    def session_ending(self, timeout: float = SESSION_END_WAIT_S) -> None:
        """Windows завершает сеанс (выход из системы, перезагрузка, выключение; установщик
        закрывает программу) — WM_QUERYENDSESSION: вернуть звук компьютера сейчас и
        дождаться этого (не дольше timeout): после ответа процесс ядра могут снять в любой
        момент, а оболочка на Tauri — снять его раньше, чем ядро завершится само."""
        with self._lock:
            ducked = self._ducked
            self._duck_off()
        if not ducked:
            return
        try:
            if not self.ducker.flush(timeout):
                log.warning("Звук компьютера: не успел вернуть громкость до конца сеанса")
        except Exception:
            log.exception("Звук компьютера: не удалось вернуть громкость до конца сеанса")

    def session_end_cancelled(self) -> None:
        """Завершение сеанса отменено (WM_ENDSESSION с FALSE), а запись идёт — приглушить
        снова."""
        with self._lock:
            if not self._ducked and not self._stop.is_set() and self._recording_confirmed():
                self._duck_on()

    # ---------- состояние и события ----------

    def _compute_state(self) -> str:
        if self._paused:
            return PAUSED
        sess = self._session
        if self._test_started is not None:
            return RECORDING
        if sess is not None and sess.confirmed:
            return HANDS_FREE if sess.hands_free else RECORDING
        if self._ai_busy:
            return AI
        if self._busy > 0:
            return PROCESSING
        if self._model_phase == "loading":
            return LOADING
        if self._model_phase == "error":
            return ERROR
        return IDLE

    def _refresh_state(self, force: bool = False) -> None:
        with self._state_lock:
            st = self._compute_state()
            if st == self.state and not force:
                return
            self.state = st
            self.bus.emit("state", self.get_state())

    def get_state(self) -> dict[str, Any]:
        s = self.s
        loaded = self.rec.loaded
        return {
            "state": self.state,
            "paused": self._paused,
            "device": self.rec.device if loaded else None,
            "model_loaded": loaded,
            "insert_mode": s.insert_mode,
            "style": s.default_style,
            "ai_enabled": s.ai_enabled,
            "first_run_done": s.first_run_done,
            "version": VERSION,
            "elevated": self._elevated,
            "ui_language": i18n.current(),
            "hotkeys": {"ptt": s.ptt_key, "broadcast": s.broadcast_modifier,
                        "command": s.command_modifier, "cancel": s.cancel_key,
                        "insert_last": s.insert_last_hotkey, "copy_last": s.copy_last_hotkey},
        }

    def notice(self, text: str, level: str = "info", action: str | None = None,
               toast: bool | None = None, data: dict[str, Any] | None = None) -> None:
        """toast — показать всплывающее уведомление (по умолчанию только для warn/error).
        data — данные для кнопки уведомления (обучение словаря: {term, variant, id, created})."""
        if data is None:
            log.log(logging.WARNING if level != "info" else logging.INFO, "Уведомление: %s",
                    text)
        else:  # в тексте и data — слова из переписки пользователя: в лог только длины
            term = data.get("term")
            log.info("Уведомление (%s): правка словаря, термин %d симв., %s", action,
                     len(term) if isinstance(term, str) else 0,
                     "с вариантом" if data.get("variant") else "без варианта")
        self.bus.emit("notice", {"text": text, "level": level, "action": action,
                                 "toast": level != "info" if toast is None else toast,
                                 "data": data})

    def _rec_notice(self, text: str, level: str) -> None:
        self.notice(text, level=level)

    @property
    def paused(self) -> bool:
        return self._paused

    def toggle_dictation(self) -> dict[str, bool]:
        """Клик по пилюле: начать запись «без рук» или остановить её (как в Wispr Flow).
        Пилюля фокус не берёт, поэтому текст вставится в окно, где работал пользователь."""
        if self._paused:
            raise ValueError(tr("eng.paused_toggle"))
        acts = self.machine.external_toggle(int(_win.kernel32.GetTickCount()))
        if acts:
            log.info("Клик по пилюле: %s", "старт" if acts[0].kind is Kind.START else "стоп")
            self._on_actions(acts)
        return {"started": bool(acts) and acts[0].kind is Kind.START}

    def set_paused(self, p: bool) -> None:
        with self._lock:
            self._paused = p
            self.hook.paused = p
            self.machine.reset()
            if p and self._session is not None:
                self._end_session()
                self.recorder.stop()
        if p:
            self._learn_stop("пауза", evaluate=False)
        self._refresh_state(force=True)

    def _is_rdp_hwnd(self, hwnd: int) -> bool:
        info = winutil.window_info(hwnd)
        return winutil.is_rdp(info.cls, info.process, self.s.rdp_classes, self.s.rdp_processes)

    def _on_level(self, rms: float) -> None:
        now = time.monotonic()
        if now - self._level_t >= LEVEL_INTERVAL_S:
            self._level_t = now
            self.bus.emit("level", rms=round(float(rms), 5) if rms == rms else 0.0)

    # ---------- микрофон ----------

    def _open_recorder(self) -> bool:
        try:
            self.recorder.open()
            return True
        except Exception as e:
            log.exception("Микрофон не открылся")
            self.notice(tr("eng.mic_unavailable", error=e), level="error")
            return False

    def _reopen_recorder(self) -> None:
        with self._lock:
            if self._session is not None:
                self._end_session()
            self._test_started = None
            self._duck_off()
            old = self.recorder
            monitor = bool(getattr(old, "monitor", False))
            try:
                old.close()
            except Exception:
                log.exception("Не удалось закрыть старый микрофон")
            self.recorder = self._make_recorder(self.s.input_device, self._on_level)
            if monitor:
                setattr(self.recorder, "monitor", True)
        self._open_recorder()
        self._refresh_state()

    def set_mic_monitor(self, on: bool) -> None:
        """Мастер первого запуска: события level идут и без записи.
        Под тем же замком, что и пересоздание рекордера: флаг не теряется при смене микрофона."""
        with self._lock:
            setattr(self.recorder, "monitor", bool(on))
        if on:
            self._open_recorder()

    def list_devices(self) -> list[dict[str, Any]]:
        default = audio.default_input_index()
        return [{"index": i, "name": n, "default": i == default}
                for i, n in audio.list_input_devices()]

    # ---------- управление (из хука через очередь) ----------

    def _on_actions(self, acts: list[Action]) -> None:
        self._ctl.put(acts)  # поток хука не блокируем

    def _ctl_loop(self) -> None:
        while not self._stop.is_set():
            acts = self._ctl.get()
            if acts is None:
                break
            for a in acts:
                try:
                    with self._ctl_lock:
                        self._handle(a)
                except Exception:
                    log.exception("Ошибка обработки %s", a)

    def _interrupt_session(self, why: str) -> None:
        """Остановить идущую запись снаружи (смена микрофона/хоткеев, лимит длительности):
        записанное распознаётся и вставляется, автомат жестов сбрасывается, пользователь
        видит почему."""
        with self._ctl_lock:
            if self._session is None:
                return
            self.machine.interrupt()
            self.notice(why, toast=True)
            self._handle(Action(Kind.STOP))

    def _record_limit(self, sid: int) -> None:
        sess = self._session
        if sess is not None and sess.id == sid:
            self._interrupt_session(tr("eng.record_too_long", minutes=f"{MAX_RECORD_S / 60:g}"))

    def _handle(self, a: Action) -> None:
        k = a.kind
        if k is Kind.INSERT_LAST:
            try:
                self.insert_last()
            except ValueError as e:
                self.notice(str(e), level="warn")
            return
        if k is Kind.COPY_LAST:
            try:
                self.copy_last()
            except (ValueError, OSError) as e:
                self.notice(str(e), level="warn")
            return
        if k is Kind.START:
            # Новая диктовка: наблюдение за прошлой вставкой заканчивается (с оценкой правок)
            self._learn_stop("новая диктовка")
            with self._lock:
                if self._paused:
                    return
                testing = self._test_started is not None
                if not testing:
                    self._seq += 1
                    self._session = _Session(self._seq, time.monotonic(), time.time())
                new = self._session
            if testing or new is None:
                self.notice(tr("eng.test_in_progress"), level="warn")
                return
            try:
                self.recorder.start()
            except Exception as e:
                log.exception("Не удалось начать запись")
                with self._lock:
                    self._session = None
                self.notice(tr("eng.mic_unavailable", error=e), level="error")
                return
            log.info("Жест: запись началась")
            self._start_context(new)
            t1 = threading.Timer(CONFIRM_MS / 1000, self._confirm, (new.id,))
            t2 = threading.Timer(LANG_PREFETCH_S, self._prefetch_lang, (new.id,))
            t3 = threading.Timer(MAX_RECORD_S, self._record_limit, (new.id,))
            for t in (t1, t2, t3):
                t.daemon = True
            new.timers += [t1, t2, t3]
            for t in (t1, t2, t3):
                t.start()
            return
        sess = self._session
        if sess is None:
            return
        if k is Kind.BROADCAST:
            sess.broadcast = True
            self.bus.emit("broadcast", on=True)
            if sess.confirmed:
                self._emit_recording(sess)
        elif k is Kind.COMMAND:
            sess.command = True
            if sess.confirmed:
                self._emit_recording(sess)
        elif k is Kind.HANDS_FREE:
            log.info("Жест: режим «без рук»")
            sess.hands_free = True
            if sess.confirmed:
                self._emit_recording(sess)
                self._refresh_state()
            else:
                self._confirm(sess.id)
        elif k in (Kind.DISCARD, Kind.CANCEL):
            log.info("Жест: %s", "отмена" if k is Kind.CANCEL
                     else "не диктовка (короткое нажатие или сочетание клавиш)")
            self._end_session()
            self.recorder.stop()
            if sess.broadcast:
                self.bus.emit("broadcast", on=False)
            if k is Kind.CANCEL:
                if self.s.sounds:
                    sounds.play("cancel")
                self.notice(tr("eng.cancelled"))
            self._refresh_state()
        elif k is Kind.STOP:
            self._end_session()
            audio_data = self.recorder.stop()
            t_release = time.perf_counter()
            held_s = time.monotonic() - sess.started
            log.info("Жест: стоп, удержание %.1f с, аудио %.1f с", held_s,
                     len(audio_data) / SAMPLE_RATE)
            if sess.broadcast:
                self.bus.emit("broadcast", on=False)
            if self.s.sounds:
                sounds.play("stop")
            if (held_s >= MIC_DEAD_HOLD_S
                    and len(audio_data) < held_s * SAMPLE_RATE * MIC_DEAD_RATIO):
                # Поток микрофона молчит (гарнитуру переподключили, сменился профиль BT):
                # вместо записи — только старый пре-буфер. Сказать и переоткрыть микрофон.
                log.warning("Микрофон не дал звука: %.1f с удержания → %.2f с аудио", held_s,
                            len(audio_data) / SAMPLE_RATE)
                if self.s.sounds:
                    sounds.play("error")
                self.notice(tr("eng.mic_silent"), level="error")
                self._reopen_recorder()
                return
            if len(audio_data) < self.s.min_record_ms * SAMPLE_RATE / 1000:
                self._refresh_state()
                return
            target = winutil.foreground()
            if target is not None and self._is_foreign(target):
                self._remember_window(target)
            with self._lock:
                self._busy += 1
            self._refresh_state()
            self._jobs.put(("transcribe", _Job(sess, audio_data, t_release, target)))

    def _end_session(self) -> None:
        """Запись кончилась (стоп, отмена, выброс, лимит, пауза, смена микрофона): таймеры
        сессии — прочь, звук компьютера — вернуть."""
        with self._lock:
            if self._session:
                for t in self._session.timers:
                    t.cancel()
            self._session = None
            self._duck_off()

    # ---------- звук компьютера во время записи (duck.py) ----------

    def _duck_on(self) -> None:
        """Запись подтверждена — приглушить звук компьютера (duck_mode, duck_level). Не
        блокирует: Core Audio работает в потоке rupor-duck. Звать под self._lock — тогда
        приглушение не переживёт запись, закончившуюся в другом потоке."""
        mode = self.s.duck_mode
        if mode == duck.OFF:
            return
        try:
            self.ducker.duck(mode, self.s.duck_level)
            self._ducked = True
        except Exception:
            log.exception("Звук компьютера: не удалось приглушить")

    def _duck_off(self) -> None:
        """Вернуть звук компьютера, если приглушали (звать под self._lock)."""
        if not self._ducked:
            return
        self._ducked = False
        try:
            self.ducker.restore()
        except Exception:
            log.exception("Звук компьютера: не удалось вернуть громкость")

    def _recording_confirmed(self) -> bool:
        sess = self._session
        return (sess is not None and sess.confirmed) or self._test_started is not None

    def _emit_recording(self, sess: _Session) -> None:
        targets: list[str] = []
        if sess.broadcast:
            try:
                targets = broadcast.target_keys(self.s)
            except Exception:
                log.exception("Не удалось перечислить RDP-сессии для рассылки")
        self.bus.emit("recording", started_at=int(sess.started_wall * 1000),
                      broadcast=sess.broadcast, command=sess.command, hands_free=sess.hands_free,
                      targets=targets)

    # ---------- контекст окна (подсказка распознаванию) ----------

    def _start_context(self, sess: _Session) -> None:
        """Нажата диктовка: в фоне прочитать 1–2 предложения перед курсором в поле, куда
        будет вставка (uia, не дольше CONTEXT_TIMEOUT_S). Не для RDP и удалённых окон, не
        для окон Рупора; поля паролей uia не читает никогда. Распознавание не ждёт: не
        успело — подсказка без контекста."""
        if not self.s.context_enabled:
            sess.context_done.set()
            return
        try:
            fg = winutil.foreground()
        except Exception:
            fg = None
        if (fg is None or not fg.pid or fg.pid in self.own_pids or not self._is_foreign(fg)
                or _is_remote_info(fg, self.s)):
            sess.context_done.set()
            return
        sess.context_pid = fg.pid
        threading.Thread(target=self._read_context, args=(sess, fg.pid),
                         name="rupor-context", daemon=True).start()

    def _read_context(self, sess: _Session, pid: int) -> None:
        t0 = time.monotonic()
        text = ""
        try:
            got = self._context_reader()
            took = time.monotonic() - t0
            if got is not None and took <= CONTEXT_TIMEOUT_S and got[0] == pid:
                text = vocab.context_tail(got[1])
            elif got is not None:
                log.debug("Контекст окна не взят (%s)", "опоздал" if took > CONTEXT_TIMEOUT_S
                          else "другое окно")
        except Exception as e:
            log.debug("Контекст окна не прочитан (%s)", type(e).__name__)
        sess.context = text or None
        sess.context_done.set()
        # В лог — только длина: это текст переписки пользователя
        log.debug("Контекст окна: %s за %.0f мс", f"{len(text)} симв." if text else "нет",
                  (time.monotonic() - t0) * 1000)

    def _confirm(self, sid: int) -> None:
        sess = self._session
        if sess is None or sess.id != sid or sess.confirmed:
            return
        sess.confirmed = True
        if self.s.sounds:
            sounds.play("start")
        self._emit_recording(sess)
        self._refresh_state()
        with self._lock:
            if self._session is sess:     # запись не кончилась, пока играл звук начала
                self._duck_on()

    def _prefetch_lang(self, sid: int) -> None:
        sess = self._session
        if sess is not None and sess.id == sid and self.rec.loaded:
            self._jobs.put(("detect", (sess, self.recorder.snapshot())))

    # ---------- рабочий поток ----------

    def _job_loop(self) -> None:
        while not self._stop.is_set():
            item = self._jobs.get()
            if item is None:
                break
            kind, payload = item
            try:
                if kind == "load":
                    self._load_model()
                elif kind == "reload":
                    self._reload_model()
                elif kind == "model_downloaded":
                    self._model_downloaded()
                elif kind == "cuda_downloaded":
                    self._cuda_downloaded()
                elif kind == "model_moved":
                    self._model_moved(*payload)
                elif kind == "detect":
                    sess, audio_data = payload
                    try:
                        sess.language = self.rec.detect_language(audio_data)
                        log.debug("Язык определён заранее: %s", sess.language)
                    except Exception:
                        log.debug("Раннее определение языка не удалось", exc_info=True)
                elif kind == "transcribe":
                    self._process(payload)
                elif kind == "insert":
                    self._insert_to(*payload)
                elif kind == "replace":
                    self._do_replace(*payload)
            except ModelMissing as e:
                if self.s.sounds:
                    sounds.play("error")
                self.notice(str(e), level="error")
            except Exception as e:
                log.exception("Ошибка задачи %s", kind)
                if self.s.sounds:
                    sounds.play("error")
                self.notice(tr("eng.error", error=e), level="error")
            finally:
                if kind == "transcribe":
                    with self._lock:
                        self._busy = max(0, self._busy - 1)
                        self._ai_busy = False
                self._refresh_state(force=kind in _FORCE_STATE_JOBS)

    # ---------- модель ----------

    def _cache_dir(self) -> str | None:
        """Папка моделей для fetch: model_dir из настроек, None — кэш HuggingFace по умолчанию."""
        return self.s.model_dir or None

    def _load_model(self) -> None:
        if self.rec.loaded:
            self._model_phase = "ready"
            return
        if not fetch.model_ready(self.s.model, self._cache_dir()):
            if self.auto_download_model or self.s.first_run_done:
                self._model_phase = "loading"
                self._refresh_state()
                if "model" not in self._downloading:
                    self.notice(tr("eng.model_downloading_once", model=self.s.model),
                                toast=True)
                self.setup_download("model")  # по окончании — задание model_downloaded
            else:
                # первый запуск: модель скачает мастер (setup_download)
                self._model_phase = "error"
                self._model_error = i18n.Msg("eng.model_missing", model=self.s.model)
                log.info("%s — жду команды setup_download", self._model_error)
            return
        self._model_phase = "loading"
        self._refresh_state()
        try:
            self.rec.load()
        except Exception as e:
            log.exception("Модель не загрузилась")
            self._model_phase = "error"
            self._model_error = str(e)
            self.notice(tr("eng.model_load_failed", error=e), level="error")
            return
        self._model_phase = "ready"
        self._model_error = None
        self.bus.emit("ready", device=self.rec.device)

    def _reload_model(self) -> None:
        model = self.s.model
        if self.rec.loaded and not fetch.model_ready(model, self._cache_dir()):
            # Новой модели ещё нет на диске: прежняя продолжает распознавать, пока новая
            # качается (по окончании — model_downloaded → перезагрузка).
            self._model_phase = "ready"
            self._swap_pending = True
            if self.auto_download_model or self.s.first_run_done:
                if "model" not in self._downloading:
                    self.notice(tr("eng.model_downloading_swap", model=model), toast=True)
                self.setup_download("model")
            else:
                log.info("Модель %s не скачана — жду setup_download, работает прежняя", model)
            return
        self._swap_pending = False
        self.rec.unload()
        self._model_phase = "loading"
        self._refresh_state(force=True)
        self._load_model()

    def _model_downloaded(self) -> None:
        """Модель скачана: загрузить её (или подменить ею работающую прежнюю)."""
        if self.rec.loaded and self._swap_pending:
            self._reload_model()
        else:
            self._load_model()

    def _cuda_downloaded(self) -> None:
        """Библиотеки CUDA скачаны. Задание идёт в очереди рабочего потока — после загрузки
        модели, которая могла начаться до конца скачивания и уйти на CPU."""
        if self.s.device == "cuda" and self.rec.loaded and self.rec.device == "cpu":
            self.notice(tr("eng.cuda_restart"))
            self._reload_model()

    def cuda_check(self) -> dict[str, Any]:
        missing = fetch.missing_cuda_dlls()
        count = cuda_device_count()
        gi = gpu_info()
        err: str | None = None
        if count == 0 and gi is None:
            err = tr("eng.no_gpu")
        elif missing:
            err = tr("eng.cuda_missing", names=", ".join(missing))
        elif count == 0:
            err = tr("eng.cuda_unavailable")
        elif self.rec.loaded and self.rec.device == "cpu" and self.s.device == "cuda":
            # last_error — i18n.Msg или текст исключения: на языке момента показа
            err = str(self.rec.last_error or "") or tr("eng.model_on_cpu")
        ok = err is None
        if self.rec.loaded and self.rec.device:
            device = self.rec.device
        else:
            device = "cuda" if ok and self.s.device == "cuda" else "cpu"
        return {"ok": ok, "device": device, "gpu_name": gi[0] if gi else None,
                "vram_total_mb": gi[1] if gi else None, "error": err}

    def setup_status(self) -> dict[str, Any]:
        return {"model_ready": fetch.model_ready(self.s.model, self._cache_dir()),
                "cuda_ready": fetch.cuda_ready(),
                "first_run_done": self.s.first_run_done,
                "model_dir": str(fetch.model_cache_dir(self.s.model_dir)),
                "model_dir_default": str(fetch.model_cache_dir(""))}

    def model_recommend(self) -> dict[str, Any]:
        """Модель под мощность компьютера для мастера: видеокарта, память, потоки процессора
        → {recommended, reason, options} (recommend.py). Не дольше ≈5 с: размеры моделей
        с Hugging Face, не успели — запасные."""
        return recommend.recommend(recommend.hardware(gpu_info(), cuda_device_count(),
                                                      cuda_compute_types()))

    def model_dir_set(self, path: str, move: bool) -> dict[str, Any]:
        """Сменить папку моделей (model_dir). path "" — кэш HuggingFace по умолчанию.

        move и модель есть в нынешней папке — перенос в фоне (прогресс — события progress
        what="model"): по окончании настройка сохраняется, модель перезагружается из новой
        папки, старая копия удаляется (notice). Распознавание всё это время работает на
        загруженной модели; ошибка — progress с error, настройки и модель прежние.
        Иначе — как смена настройки: модель есть в новой папке — перезагрузка оттуда, нет —
        скачивается туда (прежняя распознаёт, пока новая не готова).
        Та же папка, записанная иначе (явный путь кэша по умолчанию и "", junction, ссылка,
        subst, сетевой диск и его UNC-путь), — только сохранить новую запись: переносить и
        удалять нечего. Папка внутри папки самой модели или .locks — ошибка.

        Команда сама трогает диск (папка, пробный файл; недоступный сетевой путь — десятки
        секунд), поэтому она в server.LONG_COMMANDS: очередь команд соединения не держит."""
        try:
            new = config.normalize_model_dir(path)
        except ValueError as e:
            raise ValueError(f"{config.label('model_dir')}: {e}") from None
        old = self.s.model_dir
        if new == old:
            return {"ok": True, "moving": False}
        if self._moving:   # сразу, не трогая диск (под замком — ещё раз ниже)
            raise ValueError(tr("eng.model_dir_busy"))
        src, dst = fetch.model_cache_dir(old), fetch.model_cache_dir(new)
        if fetch.same_dir(src, dst):
            # Та же папка под другой записью: перенос «в неё» и удаление «старой» копии
            # стёрли бы единственную. Сохранить запись (например, явный путь по умолчанию → "")
            log.info("Папка моделей %s — та же, что %s: меняю только запись", dst, src)
            self.update_settings({"model_dir": new})
            return {"ok": True, "moving": False}
        model = self.s.model
        # до создания папки: внутри папки модели её создавать незачем
        fetch.check_move_target(model, old or None, new or None)
        fetch.check_writable_dir(dst)
        movable = move and not os.path.isdir(model) and fetch.model_ready(model, old or None)
        with self._lock:
            if self._moving:
                raise ValueError(tr("eng.model_dir_busy"))
            if movable:
                if "model" in self._downloading:
                    raise ValueError(tr("eng.model_dir_busy"))
                self._downloading.add("model")
                self._moving = True
                self._model_dl_deferred = False
        if not movable:
            self.update_settings({"model_dir": new})
            return {"ok": True, "moving": False}
        log.info("Перенос модели %s: %s → %s", model, src, dst)
        threading.Thread(target=self._move_model, args=(model, old, new),
                         name="rupor-model-move", daemon=True).start()
        return {"ok": True, "moving": True}

    def _move_model(self, model: str, old: str, new: str) -> None:
        """Поток переноса: копия → сохранить model_dir → перезагрузить модель из новой папки
        (задание reload) → удалить старую копию (задание model_moved, после перезагрузки)."""
        last = [0.0]

        def cb(pct: float, text: str) -> None:
            last[0] = pct
            self.bus.emit("progress", what="model", pct=round(float(pct), 1), text=text,
                          done=False, error=None)

        moved = False
        try:
            fetch.move_model(model, old or None, new or None, cb)
            try:
                self.update_settings({"model_dir": new})
            except Exception:
                # Настройка не сохранилась (файл занят): модель, переименованную в новую папку,
                # вернуть на место, чтобы прежняя папка осталась рабочей
                if (not fetch.model_ready(model, old or None)
                        and fetch.model_ready(model, new or None)):
                    try:
                        fetch.move_model(model, new or None, old or None, lambda p, t: None)
                    except fetch.FetchError as e:
                        log.error("Модель не вернулась в прежнюю папку: %s", e)
                raise
            moved = True
        except fetch.FetchError as e:
            self._move_failed(str(e), last[0])
            return
        except Exception as e:
            log.exception("Перенос модели упал")
            self._move_failed(tr("eng.model_move_failed", name=type(e).__name__, error=e),
                              last[0])
            return
        finally:
            with self._lock:
                # Удачный перенос держит _moving до удаления старой копии (_model_moved):
                # перенос обратно в эти секунды нашёл бы её ещё целой («уже там»), и потом
                # удалились бы обе копии. Слот загрузки освобождается сразу.
                self._moving = moved
                self._downloading.discard("model")
                # Модель сменили во время переноса: её загрузка не началась — слот «model»
                # был занят (и notice не показан), а повторить некому: перенос, в отличие от
                # загрузки, не ставит model_downloaded. Повторить теперь, когда слот свободен:
                # задание идёт после перезагрузки, которую уже поставила смена model_dir
                # (качается в действующую папку — новую после удачного переноса, прежнюю
                # после неудачного). Уже не нужно — оно ничего не делает.
                deferred, self._model_dl_deferred = self._model_dl_deferred, False
            if deferred:
                self._jobs.put(("model_downloaded", None))
        self.bus.emit("progress", what="model", pct=100.0, text=tr("eng.model_moved_progress"),
                      done=True, error=None)
        # перезагрузка из новой папки уже в очереди (смена model_dir); старую копию — после неё
        self._jobs.put(("model_moved", (model, old, new)))

    def _move_failed(self, error: str, pct: float) -> None:
        log.warning("Перенос модели не удался: %s", error)
        self.bus.emit("progress", what="model", pct=pct, text=error, done=True, error=error)

    def _model_moved(self, model: str, old: str, new: str) -> None:
        """Перезагрузка из новой папки уже прошла (задание reload — раньше в очереди).

        Удалась — модель распознаёт из новой папки: удалить старую копию (в фоне, диктовки не
        ждут) и сообщить. Не удалась (модель не загрузилась, в новой папке её не стало,
        модель или папку за это время сменили) — старую копию оставить: она может оказаться
        единственной рабочей (warn). Старая и новая копии — одни и те же файлы (junction,
        ссылка) — не удалять: это и есть новая. До конца удаления новый model_dir_set —
        «переносится» (_moving)."""
        reloaded = (self.rec.loaded and self._model_phase == "ready" and not self._swap_pending
                    and self.s.model == model and self.s.model_dir == new
                    and fetch.model_ready(model, new or None))
        if not reloaded:
            with self._lock:
                self._moving = False
            folder = self._model_folder_text(model, old)
            log.warning("Модель %s из новой папки %s не загрузилась — старая копия оставлена: %s",
                        model, fetch.model_cache_dir(new), folder)
            self.notice(tr("eng.model_old_kept", path=folder), level="warn")
            return

        def remove() -> None:
            try:
                if fetch.shares_files(model, old or None, new or None):
                    log.warning("Старая и новая копии модели %s — одни и те же файлы (%s и %s): "
                                "не удаляю", model, fetch.model_cache_dir(old),
                                fetch.model_cache_dir(new))
                    ok = True
                else:
                    ok = fetch.remove_model(model, old or None)
            finally:
                with self._lock:
                    self._moving = False
            if ok:
                self.notice(tr("eng.model_moved", path=fetch.model_cache_dir(new)), toast=True)
                return
            self.notice(tr("eng.model_old_not_removed", path=self._model_folder_text(model, old)),
                        level="warn")

        threading.Thread(target=remove, name="rupor-model-rm", daemon=True).start()

    @staticmethod
    def _model_folder_text(model: str, model_dir: str) -> Path:
        """Папка модели в model_dir для сообщений (неизвестная модель — сама папка моделей)."""
        try:
            return fetch.model_folder(model, model_dir or None)
        except fetch.FetchError:
            return fetch.model_cache_dir(model_dir)

    def setup_download(self, what: str) -> None:
        """Скачать модель или библиотеки CUDA в фоне; прогресс — событиями progress."""
        if what not in ("model", "cuda"):
            raise ValueError(tr("err.what_download"))
        with self._lock:
            if what in self._downloading:
                if what == "model" and self._moving:
                    # слот держит перенос модели: загрузка начнётся после него (_move_model)
                    self._model_dl_deferred = True
                return
            self._downloading.add(what)
        threading.Thread(target=self._download, args=(what,), name=f"rupor-dl-{what}",
                         daemon=True).start()

    def _download(self, what: str) -> None:
        last = [0.0]  # последний процент — для события об ошибке

        def cb(pct: float, text: str) -> None:
            last[0] = pct
            self.bus.emit("progress", what=what, pct=round(float(pct), 1), text=text,
                          done=False, error=None)

        try:
            if what == "model":
                fetch.download_model(self.s.model, cb, cache_dir=self._cache_dir())
            else:
                fetch.download_cuda(cb)
        except fetch.FetchError as e:
            self._download_failed(what, str(e), last[0])
            return
        except Exception as e:
            log.exception("Загрузка %s упала", what)
            self._download_failed(what, tr("eng.download_failed", name=type(e).__name__,
                                           error=e), last[0])
            return
        finally:
            with self._lock:
                self._downloading.discard(what)
        done_text = tr("eng.model_downloaded") if what == "model" else tr("fetch.cuda_done")
        self.bus.emit("progress", what=what, pct=100.0, text=done_text, done=True, error=None)
        self._jobs.put(("model_downloaded" if what == "model" else "cuda_downloaded", None))

    def _download_failed(self, what: str, error: str, pct: float) -> None:
        log.warning("Загрузка %s не удалась: %s", what, error)
        self.bus.emit("progress", what=what, pct=pct, text=error, done=True, error=error)
        if what == "model" and not self.rec.loaded:
            self._model_phase = "error"
            self._model_error = error
            self._refresh_state()
        elif what == "model" and self._swap_pending:
            self._swap_pending = False
            self.notice(tr("eng.model_swap_failed", model=self.s.model, error=error),
                        level="warn")

    # ---------- словарь, подсказка ----------

    def _dict_items(self) -> list[dict[str, Any]]:
        try:
            return self.storage.dict_list()
        except Exception:
            log.exception("Не удалось прочитать словарь")
            return []

    def _dict_apply(self, text: str, items: list[dict[str, Any]]) -> str:
        """Словарь целиком: точные варианты и, если dict_fuzzy, похожие по звучанию
        написания терминов (fuzzy.py). Сколько похожих заменено — в лог (без слов)."""
        out, n = fuzzy.apply(text, items, self.s.dict_fuzzy, self._user_words())
        if n:
            log.info("Словарь: похожих написаний исправлено %d", n)
        return out

    def _user_words(self) -> frozenset[str]:
        """Слова, которые пользователь сам часто диктует (строчная кириллица из словаря частот,
        ключи форм vocab.group_key): для похожих написаний они настоящие («гео ноды» не
        станет GeoNode). Словарь частот ещё собирается или недоступен — пусто."""
        if not self.s.dict_fuzzy:
            return frozenset()
        try:
            words = self.storage.vocab_words(None) if self.storage.vocab_ready else []
        except Exception:
            log.debug("Частые слова для похожих написаний недоступны", exc_info=True)
            return frozenset()
        return frozenset(w.key for w in words if not w.strong and vocab.eligible(w))

    def _snippets(self) -> list[dict[str, Any]]:
        try:
            return self.storage.snippets_list()
        except Exception:
            log.exception("Не удалось прочитать сниппеты")
            return []

    def _snippets_for(self, items: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Сниппеты для сопоставления с текстом ПОСЛЕ словаря: триггер, в котором есть слово
        из вариантов словаря («мой имейл» при словаре имейл → email), добавляется ещё и в
        форме после словаря («мой email»), иначе он бы никогда не сработал."""
        snips = self._snippets()
        if not items:
            return snips
        extra: list[dict[str, Any]] = []
        for sn in snips:
            trig = sn.get("trigger")
            if isinstance(trig, str):
                norm = fuzzy.apply_text(trig, items, self.s.dict_fuzzy, self._user_words())
                if norm != trig:
                    extra.append({"trigger": norm, "text": sn.get("text")})
        return snips + extra

    def _asr_prompt(self) -> str | None:
        """initial_prompt: [контекст окна] + [термины настроек и словаря] + [частые слова
        процесса] + [общие частые слова], ≤ 200 токенов (vocab.build_prompt; токены считает
        токенизатор загруженной модели — faster-whisper молча срезает начало подсказки
        длиннее 223 токенов). Собирается перед каждым распознаванием: выученный термин
        попадает в подсказку со следующей же диктовки, новые частые слова — как только
        обновится их кэш (в фоне, сразу после записи истории).

        Ошибки распознавания из словаря (его варианты) в подсказку не попадают: ни частым
        словом (ошибка из истории, записанной до словаря), ни термином настроек, а контекст
        окна идёт уже исправленным словарём — иначе подсказка сама учила бы Whisper ошибке."""
        s = self.s
        job: _Job | None = getattr(self._prompt_local, "job", None)
        items = self._dict_items()
        bad = fuzzy.misrecognition_filter(items, s.dict_fuzzy)
        terms: list[object] = [*(t for t in s.terms if bad is None or not bad(str(t))),
                               *(d.get("term") for d in items)]
        proc_words: list[str] = []
        gen_words: list[str] = []
        if s.vocab_enabled:
            try:
                proc_words, gen_words = self.storage.vocab_prompt_words(self._vocab_app(job),
                                                                        reject=bad)
            except Exception:
                log.exception("Не удалось прочитать частые слова")
        ctx: str | None = None
        if (job is not None and s.context_enabled and not job.sess.command
                and not job.sess.broadcast and job.sess.context_done.is_set()
                and job.target is not None and job.target.pid == job.sess.context_pid):
            ctx = job.sess.context      # окно то же, где нажали диктовку
            if ctx:
                ctx = fuzzy.apply_text(ctx, items, s.dict_fuzzy, self._user_words())
        counter: vocab.TokenCounter | None = None
        get_counter = getattr(self.rec, "token_counter", None)
        if callable(get_counter):
            try:
                counter = get_counter()
            except Exception:
                log.exception("Токенизатор модели недоступен — оценка длины подсказки")
        prompt = vocab.build_prompt(ctx, terms, proc_words, gen_words, count_tokens=counter)
        log.debug("Подсказка распознаванию: %d симв. (контекст %d, частых слов %d, %s)",
                  len(prompt or ""), len(ctx or ""), len(proc_words) + len(gen_words),
                  "токенизатор модели" if counter is not None else "оценка токенов")
        return prompt

    @staticmethod
    def _vocab_app(job: _Job | None) -> str | None:
        """Процесс для частых слов: окно, где диктуют; рассылка — «rdp» (как в истории)."""
        if job is None:
            return None
        if job.sess.broadcast:
            return "rdp"
        return job.target.process.lower() if job.target is not None else None

    def _ai_terms(self, items: list[dict[str, Any]], job: _Job) -> list[str]:
        """Термины для промпта ИИ-очистки: словарь + частые термины и имена пользователя,
        кроме ошибок распознавания из словаря (иначе ИИ «исправлял» бы на них)."""
        terms = [str(d["term"]) for d in items]
        if not self.s.vocab_enabled:
            return terms
        try:
            proc, gen = self.storage.vocab_prompt_words(
                self._vocab_app(job), vocab.MAX_AI_WORDS, strong_only=True,
                reject=fuzzy.misrecognition_filter(items, self.s.dict_fuzzy))
        except Exception:
            log.exception("Не удалось прочитать частые слова")
            return terms
        known = {textproc.fold(t) for t in terms}
        return terms + [w for w in (*proc, *gen) if textproc.fold(w) not in known]

    # ---------- обучение словаря на правках ----------

    def _learn_start(self, inserted: str, job: _Job, d: Delivery) -> None:
        """После вставки диктовки — следить за полем (одно наблюдение: новое заменяет
        старое, старое оценивается). Не следим: рассылка, командный режим, RDP и другие
        удалённые окна, свои окна, режим off, пауза."""
        s = self.s
        info = d.info
        if (s.learn_mode == "off" or self._paused or not d.ok or job.sess.broadcast
                or job.sess.command or info is None or not info.pid
                or info.pid in self.own_pids or _is_remote_info(info, s)
                or not learn.has_words(inserted)):
            return
        w = _LearnWatch(self, inserted, info, self.machine.presses)
        with self._lock:
            old, self._learn = self._learn, w
        if old is not None:
            old.stop("новая вставка")
        w.start()
        log.debug("Обучение словаря: слежу за полем в %s (%d симв.)", info.process,
                  len(inserted))

    def _learn_stop(self, reason: str, evaluate: bool = True) -> None:
        with self._lock:
            w = self._learn
        if w is not None:
            w.stop(reason, evaluate)

    def _learn_rebase(self, hwnd: int, inserted: str) -> None:
        """«Сначала сырой»: ИИ заменил текст в поле — сравнивать дальше с очищенным."""
        with self._lock:
            w = self._learn
        if w is not None and w.target.hwnd == hwnd:
            w.rebase(inserted, self.machine.presses)

    def _learn_finished(self, w: _LearnWatch) -> None:
        with self._lock:
            if self._learn is w:
                self._learn = None

    def _learn_evaluate(self, w: _LearnWatch, before: str, span: tuple[int, int],
                        snaps: list[learn.Snapshot], reason: str, polls: int,
                        changes: int) -> None:
        """Наблюдение закончено: найти правки последнего хорошего снимка и добавить или
        предложить их (_learn_apply). Правка, которая держалась в поле меньше
        LEARN_MIN_HELD_S (слово ещё печатали), не учится. В лог — только счётчики, текст поля
        и слова из него — никогда."""
        pressed = self.machine.presses != w.presses
        log.info("Обучение словаря: наблюдение закончено (%s), опросов %d, изменений поля %d, "
                 "нажатия %s", reason, polls, changes, "были" if pressed else "не было")
        if not pressed or self.s.learn_mode == "off" or self._paused:
            return
        found = learn.held_corrections(before, span, snaps, self._dict_items(),
                                       horizon=LEARN_MIN_HELD_S)
        if not found:
            return
        kept = [(c, held) for c, held in found if held >= LEARN_MIN_HELD_S]
        log.info("Обучение словаря: правок найдено %d, устоялось %d", len(found), len(kept))
        for c, _ in kept:
            try:
                self._learn_apply(c)
            except Exception:
                log.exception("Обучение словаря: не удалось учесть правку")

    def _learn_apply(self, c: learn.Correction) -> None:
        """Режим auto — добавить сразу любую правку, прошедшую фильтры (и неуверенную:
        имя → похожее имя, заглавная у русского слова, короткий вариант), и показать
        «Отменить»: пользователь видит каждое пополнение словаря, а не вопросы, которые
        пропадают. Режим suggest — предложить «Добавить»; та же правка LEARN_REPEAT-й раз —
        добавить самому. Добавленное пишется в журнал обучения (source "auto")."""
        key = learn.pair_key(c.term, c.variant)
        if key in self._kv_list(LEARN_REJECTED_KEY):
            log.info("Обучение словаря: эту правку пользователь уже отменял")
            return
        data: dict[str, Any] = {"term": c.term, "variant": c.variant, "id": None,
                                "created": False}
        if self.s.learn_mode == "suggest" and self._learn_count(key) < LEARN_REPEAT:
            self.notice(learn.suggest_text(c.term, c.variant), action="dict_add", toast=True,
                        data=data)
            return
        try:
            item, created = self.storage.dict_learn_add(c.term, c.variant, journal=True,
                                                        source="auto")
        except ValueError as e:
            log.info("Обучение словаря: правка не добавлена (%s)", type(e).__name__)
            return
        self._learn_forget(key, rejected=False)
        log.info("Обучение словаря: правка добавлена сама (%s, %s)",
                 "новый термин" if created else "к существующему термину",
                 "уверенная" if c.sure else "без уверенности")
        data.update(id=item["id"], created=created)
        self.notice(learn.added_text(c.term, c.variant), action="dict_undo", toast=True,
                    data=data)

    def learn_accept(self, term: str, variant: str) -> dict[str, Any]:
        """Кнопка «Добавить» (режим suggest): добавить термин/вариант, слив с существующим
        (в журнал обучения — source "accepted")."""
        item, created = self.storage.dict_learn_add(term, variant, journal=True,
                                                    source="accepted")
        self._learn_forget(learn.pair_key(term, variant), rejected=True)
        log.info("Обучение словаря: пользователь добавил правку (%s)",
                 "новый термин" if created else "к существующему термину")
        return item

    def learn_undo(self, term: str, variant: str, id: int | None, created: bool) -> None:
        """Кнопка «Отменить»: убрать выученное (один раз — по журналу обучения в storage);
        такую правку больше не учить."""
        done = self.storage.dict_learn_undo(term, variant, id, created)
        key = learn.pair_key(term, variant)
        with self._learn_kv_lock:
            rej = [k for k in self._kv_list(LEARN_REJECTED_KEY) if k != key] + [key]
            self._kv_put(LEARN_REJECTED_KEY, rej[-LEARN_KV_MAX:])
        log.info("Обучение словаря: пользователь отменил правку (%s)",
                 "убрана из словаря" if done else "в словаре уже нечего убирать")

    def _learn_count(self, key: str) -> int:
        """+1 к счётчику одинаковой правки (режим suggest), новое значение."""
        with self._learn_kv_lock:
            counts = self._kv_dict(LEARN_COUNTS_KEY)
            prev = counts.pop(key, 0)
            n = (prev if isinstance(prev, int) and not isinstance(prev, bool) else 0) + 1
            counts[key] = n                          # свежие — в конце
            while len(counts) > LEARN_KV_MAX:
                counts.pop(next(iter(counts)))
            self._kv_put(LEARN_COUNTS_KEY, counts)
        return n

    def _learn_forget(self, key: str, rejected: bool) -> None:
        with self._learn_kv_lock:
            counts = self._kv_dict(LEARN_COUNTS_KEY)
            if counts.pop(key, None) is not None:
                self._kv_put(LEARN_COUNTS_KEY, counts)
            if rejected:
                rej = self._kv_list(LEARN_REJECTED_KEY)
                if key in rej:
                    self._kv_put(LEARN_REJECTED_KEY, [k for k in rej if k != key])

    def _kv_json(self, key: str) -> Any:
        try:
            return json.loads(self.storage.kv_get(key, "") or "null")
        except ValueError:
            return None

    def _kv_dict(self, key: str) -> dict[str, Any]:
        v = self._kv_json(key)
        return {str(k): x for k, x in v.items()} if isinstance(v, dict) else {}

    def _kv_list(self, key: str) -> list[str]:
        v = self._kv_json(key)
        return [str(x) for x in v] if isinstance(v, list) else []

    def _kv_put(self, key: str, value: Any) -> None:
        self.storage.kv_set(key, json.dumps(value, ensure_ascii=False))

    # ---------- ИИ ----------

    def _has_key(self, provider: str) -> bool:
        target = credman.TARGETS.get(provider)
        if target is None:
            return True  # ollama — без ключа
        try:
            return credman.read(target) is not None
        except OSError:
            log.warning("Не удалось прочитать ключ %s из Credential Manager", provider)
            return False

    def _want_ai(self, text: str, style: str, snippet_used: bool) -> bool:
        s = self.s
        if not s.ai_enabled or style == "as_is" or snippet_used:
            return False
        if textproc.count_words(text) < s.ai_min_words:
            return False
        if not self._has_key(s.ai_provider):
            if not self._nokey_warned:
                self._nokey_warned = True
                self.notice(tr("eng.ai_no_key"), level="warn", action="open_settings_ai")
            return False
        return True

    def _ai_failed(self, e: BaseException, inserted_raw: bool = False) -> None:
        tail = tr("eng.ai_tail_kept") if inserted_raw else tr("eng.ai_tail_inserted")
        action: str | None = None
        if isinstance(e, ai.AIError):
            # хвост — отдельное предложение: точка в конце ошибки (текст провайдера) лишняя
            text = (tr("eng.ai_no_answer", tail=tail) if e.kind == "timeout"
                    else tr("eng.ai_error", error=str(e).rstrip(" ."), tail=tail))
            if e.kind in ("no_key", "auth", "payment"):
                action = "open_settings_ai"
        else:
            log.error("ИИ-очистка упала: %s", type(e).__name__, exc_info=e)
            text = tr("eng.ai_no_answer", tail=tail)
        self.notice(text, level="warn", action=action)

    # ---------- расход ИИ и баланс ----------

    def _ai_call(self, kind: str, app: str, fn: Callable[[], ai.AIResult]) -> ai.AIResult:
        """Вызов ИИ с записью расхода (ai_usage) — и удачный, и с ошибкой (стоимость 0)."""
        t0 = time.perf_counter()
        try:
            r = fn()
        except BaseException as e:
            self._track_ai(kind, app, None, (time.perf_counter() - t0) * 1000, e)
            raise
        self._track_ai(kind, app, r, (time.perf_counter() - t0) * 1000, None)
        return r

    def _track_ai(self, kind: str, app: str, r: ai.AIResult | None, ms: float,
                  err: BaseException | None) -> None:
        """Записать вызов в ai_usage, разослать событие ai_usage; после удачного вызова
        OpenRouter — обновить баланс в фоне, если кэш устарел. Ошибки базы вызов не срывают."""
        if r is not None and r.provider == "none":
            return                                   # «Как есть» — в сеть не ходили
        if r is not None:
            provider, model = r.provider, r.model
        else:
            provider, model = ai.provider_model(self.s, command=kind == "command")
        rec: dict[str, Any] = {
            "ts": time.time(), "kind": kind, "provider": provider, "model": model,
            "upstream": r.upstream if r is not None else None,
            "tokens_in": r.tokens_in if r is not None else None,
            "tokens_out": r.tokens_out if r is not None else None,
            "cost_usd": r.cost_usd if r is not None else 0.0,
            "ms": round(r.ms if r is not None and r.ms else ms), "ok": err is None,
            "error_kind": None if err is None else (
                err.kind if isinstance(err, ai.AIError) else type(err).__name__),
            "app": app,
        }
        try:
            saved = self.storage.add_ai_usage(rec)
            today = self.storage.ai_usage_today_cost()
        except Exception:
            log.exception("Не удалось записать расход ИИ")
            return
        self.bus.emit("ai_usage", kind=kind, cost_usd=saved["cost_usd"] or 0.0,
                      tokens_in=saved["tokens_in"] or 0, tokens_out=saved["tokens_out"] or 0,
                      today_cost_usd=today)
        if err is None and provider == "openrouter":
            with self._balance_lock:  # monotonic на Windows — с загрузки: пустой кэш не «свежий»
                stale = (self._balance is None
                         or time.monotonic() - self._balance_at >= BALANCE_TTL_S)
            if stale:
                threading.Thread(target=self._balance_refresh, name="rupor-balance",
                                 daemon=True).start()

    def _balance_refresh(self) -> None:
        try:
            self.ai_balance(force=False)
        except Exception:
            log.exception("Не удалось обновить баланс OpenRouter")

    def ai_test(self, text: str, style: str) -> ai.AIResult:
        """Кнопка «Проверить очистку» (WS ai_test) — тоже вызов ИИ с записью расхода. Язык
        текста (для английского промпта) — по буквам: есть кириллица — ru, иначе en.
        Словарь — как в диктовке: до ИИ и после."""
        items = self._dict_items()
        terms = [str(d["term"]) for d in items]
        kw = _cleanup_kwargs(i18n.text_language(text))
        uw = self._user_words()
        src = fuzzy.apply_text(text, items, self.s.dict_fuzzy, uw)
        r = self._ai_call("test", "", lambda: ai.cleanup(src, style, self.s, terms, **kw))
        return dataclasses.replace(r, text=fuzzy.apply_text(r.text, items, self.s.dict_fuzzy,
                                                            uw))

    def ai_usage_stats(self) -> dict[str, Any]:
        return self.storage.ai_usage_stats()

    def _openrouter_used(self) -> bool:
        """OpenRouter нужен очистке или командному режиму."""
        return OPENROUTER in (self.s.ai_provider, self.s.command_provider)

    def ai_balance(self, force: bool = False) -> dict[str, Any]:
        """Баланс ключа OpenRouter (WS ai_balance): {balance_usd, usage_usd, limit_usd,
        checked_at, error}. Кэш 5 минут (неудачная проверка — минута), force — мимо кэша.
        Баланс меньше $1 — notice warn не чаще раза в сутки. Ни очистка, ни командный
        режим не идут через OpenRouter — в сеть не ходим (error «не используется»); кнопка
        «Обновить» (force) всё же проверяет ключ, но без предупреждения о балансе."""
        if not force and not self._openrouter_used():
            return {"balance_usd": None, "usage_usd": None, "limit_usd": None,
                    "checked_at": None, "error": tr("eng.balance_unused")}
        with self._balance_fetching:        # параллельные запросы не ходят в сеть дважды
            with self._balance_lock:
                cached, at = self._balance, self._balance_at
            ttl = BALANCE_TTL_S if cached is not None and not cached.get("error") \
                else BALANCE_ERROR_TTL_S
            if cached is not None and not force and time.monotonic() - at < ttl:
                return dict(cached)
            try:
                key = credman.read(credman.TARGETS["openrouter"])
            except OSError:
                key = None
                log.warning("Не удалось прочитать ключ OpenRouter из Credential Manager")
            if not key:
                return {"balance_usd": None, "usage_usd": None, "limit_usd": None,
                        "checked_at": None,
                        "error": tr("eng.openrouter_no_key")}
            r = ai.check_openrouter_key(key)
            res: dict[str, Any] = {"balance_usd": r.get("balance_usd"),
                                   "usage_usd": r.get("usage_usd"),
                                   "limit_usd": r.get("limit_usd"),
                                   "checked_at": int(time.time()), "error": r.get("error")}
            with self._balance_lock:
                self._balance, self._balance_at = dict(res), time.monotonic()
        self._check_low_balance(res.get("balance_usd"))
        return res

    def _check_low_balance(self, balance: Any) -> None:
        if (not isinstance(balance, (int, float)) or isinstance(balance, bool)
                or balance >= LOW_BALANCE_USD or not self._openrouter_used()):
            return
        with self._balance_lock:
            try:
                last = float(self.storage.kv_get(LOW_BALANCE_KEY, "0") or 0)
            except ValueError:
                last = 0.0
            now = time.time()
            if now - last < LOW_BALANCE_EVERY_S:
                return
            try:
                self.storage.kv_set(LOW_BALANCE_KEY, str(now))
            except Exception:
                log.exception("Не удалось запомнить предупреждение о балансе")
        self.notice(tr("eng.low_balance", balance=f"{balance:.2f}"), level="warn",
                    action="open_settings_ai")

    # ---------- поток одной диктовки ----------

    def _process(self, job: _Job) -> None:
        self._prompt_local.job = job     # подсказка знает окно и контекст этой диктовки
        try:
            res = self.rec.transcribe(job.audio, language=job.sess.language)
        finally:
            self._prompt_local.job = None
        self._model_phase = "ready"
        raw = res.text.strip()
        if not raw:
            self.notice(tr("eng.no_speech"))
            return
        if job.sess.command:
            self._command_flow(job, res, raw)
            return
        s = self.s
        items = self._dict_items()
        text = self._dict_apply(raw, items)
        text, snippet_used = textproc.expand_snippets(text, self._snippets_for(items))
        style = resolve_style(job.target, s)
        use_ai = self._want_ai(text, style, snippet_used)
        terms = self._ai_terms(items, job) if use_ai else [str(d["term"]) for d in items]
        app = job.target.process if job.target is not None else ""
        lang_kw = _cleanup_kwargs(res.language)      # английская диктовка — английский промпт
        final, ai_used = text, False
        if use_ai and s.ai_mode != "raw_first":
            with self._lock:
                self._ai_busy = True
            self._refresh_state()
            try:
                final = self._ai_call("cleanup", app,
                                      lambda: ai.cleanup(text, style, s, terms, **lang_kw)).text
                ai_used = True
            except Exception as e:
                self._ai_failed(e)
            finally:
                with self._lock:
                    self._ai_busy = False
        if ai_used:
            # Словарь — последнее слово: ИИ мог вернуть написание, которое пользователь
            # занёс в словарь как ошибку
            final = self._dict_apply(final, items)
        out = finalize_text(final, s.add_trailing_space)
        latency = (time.perf_counter() - job.t_release) * 1000
        log.info("От отпускания до вставки: %.0f мс (аудио %.1f с%s)", latency, res.audio_s,
                 ", с ИИ" if ai_used else "")
        self.last_text = final.strip()
        self._emit_result(final, raw, res, latency, ai_used, style)
        presses = self.machine.presses  # до вставки: нажатие во время печати — тоже «после»
        d = self._deliver_job(out, job)
        if use_ai and s.ai_mode == "raw_first":
            ctx = _RawFirst(job, res, raw, text, out, style, latency, d, presses, terms,
                            winutil.focus_state(d.hwnd) if d.ok else None)
            self._learn_start(out, job, d)
            threading.Thread(target=self._raw_first_ai, args=(ctx,), name="rupor-ai-bg",
                             daemon=True).start()
            return
        self._record(job, res, raw, final, style, ai_used, latency, d)
        self._learn_start(out, job, d)

    def _deliver_job(self, text: str, job: _Job) -> Delivery:
        """Вставить результат диктовки туда, где диктовали: если активное окно сменилось
        (переключились, всплыло чужое окно), текст не вставляется в чужое окно."""
        target = job.target
        if job.sess.broadcast:
            try:
                wins = broadcast.targets(self.s)
            except Exception:
                log.exception("Не удалось перечислить RDP-сессии для рассылки")
                wins = []
            if not wins:
                # Текст не теряется: он в «Вставить последнее» и в истории
                self.notice(tr("eng.rdp_none"), level="warn", action="open_settings_insert")
                return Delivery(ok=False)
        if not job.sess.broadcast and target is not None:
            fg = winutil.foreground()
            if fg is None or fg.hwnd != target.hwnd:
                name = target.title or target.process
                self.notice(tr("eng.window_changed_saved", name=name), level="warn")
                return Delivery(ok=False, hwnd=target.hwnd, info=target)
        return self._safe_deliver(text, job.sess.broadcast)

    def _safe_deliver(self, text: str, to_all: bool) -> Delivery:
        """Вставка, которая не роняет конвейер: при сбое текст остаётся в истории
        и в «Вставить последнее»."""
        try:
            return deliver(text, self.s, to_all, self.notice)
        except Exception as e:
            log.exception("Вставка не удалась")
            if self.s.sounds:
                sounds.play("error")
            self.notice(tr("eng.insert_failed", error=e), level="error")
            return Delivery(ok=False)

    def _emit_result(self, text: str, raw: str, res: Result, latency: float, ai_used: bool,
                     style: str) -> None:
        self.bus.emit("result", text=text.strip(), raw=raw, language=res.language,
                      audio_s=round(res.audio_s, 2), asr_ms=round(res.elapsed_ms),
                      latency_ms=round(latency), ai=ai_used, style=style)

    def _record(self, job: _Job, res: Result, raw: str, text: str, style: str, ai_used: bool,
                latency: float, d: Delivery | None, target_kind: str | None = None,
                words_of: str | None = None) -> None:
        """Статистика — всегда; история — если включена. Ошибки базы диктовку не срывают."""
        text = text.strip()
        words = textproc.count_words(words_of if words_of is not None else text)
        try:
            self.storage.record_stats(words, res.audio_s)
        except Exception:
            log.exception("Не удалось записать статистику")
        if not self.s.history_enabled:
            return
        t = job.target
        kind = target_kind or ("rdp_all" if job.sess.broadcast else "window")
        item: dict[str, Any] = {
            "ts": time.time(), "app": t.process if t else "", "title": t.title if t else "",
            "raw": raw, "text": text, "duration_s": round(res.audio_s, 2),
            "words": textproc.count_words(text), "language": res.language, "style": style,
            "ai": ai_used, "latency_ms": round(latency), "target": kind,
        }
        if kind == "rdp_all":
            item["app"] = "RDP"
            # без адресатов — пусто: подпись «Рассылка в RDP» ставит history_display
            item["title"] = ", ".join(d.sent)[:500] if d and d.sent else ""
        try:
            saved = self.storage.add_history(item)
        except Exception:
            log.exception("Не удалось записать историю")
            return
        self.bus.emit("history", history_display(dict(saved)))

    def _raw_first_ai(self, ctx: _RawFirst) -> None:
        app = ctx.job.target.process if ctx.job.target is not None else ""
        kw = _cleanup_kwargs(ctx.res.language)
        try:
            r = self._ai_call("cleanup", app,
                              lambda: ai.cleanup(ctx.text, ctx.style, self.s, ctx.terms, **kw))
        except Exception as e:
            self._ai_failed(e, inserted_raw=True)
            self._record(ctx.job, ctx.res, ctx.raw, ctx.text, ctx.style, False, ctx.latency_ms,
                         ctx.delivery)
            return
        # словарь — последнее слово и после ИИ (см. _process)
        self._jobs.put(("replace", (ctx, self._dict_apply(r.text, self._dict_items()))))

    def _do_replace(self, ctx: _RawFirst, cleaned: str) -> None:
        s = self.s
        cleaned = cleaned.strip()
        reason: str | None = None
        replaced: str | None = None     # что теперь стоит в поле вместо сырого текста
        if cleaned == ctx.text.strip():
            pass  # ИИ ничего не поменял — заменять нечего
        elif ctx.job.sess.broadcast:
            reason = tr("eng.replace_rdp")
        elif not ctx.delivery.ok:
            reason = tr("eng.replace_not_inserted")
        elif len(ctx.out) > RAW_FIRST_MAX_CHARS:
            reason = tr("eng.replace_too_long", max=RAW_FIRST_MAX_CHARS)
        elif self.machine.presses != ctx.presses:
            reason = tr("eng.replace_keys_pressed")
        else:
            fg = winutil.foreground()
            if fg is None or fg.hwnd != ctx.delivery.hwnd:
                reason = tr("eng.replace_window_changed")
            elif ctx.focus is not None and winutil.focus_state(ctx.delivery.hwnd) != ctx.focus:
                reason = tr("eng.replace_focus_changed")
            else:
                try:
                    new_out = finalize_text(cleaned, s.add_trailing_space)
                    if replace_text(len(ctx.out), new_out, ctx.delivery, s, expected=ctx.out):
                        replaced = new_out
                    else:
                        reason = tr("eng.replace_not_ours")
                except inject.FocusLost:
                    reason = tr("eng.replace_window_changed")
                except Exception as e:
                    log.exception("Замена текста не удалась")
                    reason = tr("eng.replace_error", name=type(e).__name__)
        if reason:
            self.notice(tr("eng.replace_skipped", reason=reason), toast=False)
        if replaced is not None:
            # В поле теперь очищенный ИИ текст: правки ИИ — не правки пользователя
            self._learn_rebase(ctx.delivery.hwnd, replaced)
        self.last_text = cleaned
        self._emit_result(cleaned, ctx.raw, ctx.res, ctx.latency_ms, True, ctx.style)
        self._record(ctx.job, ctx.res, ctx.raw, cleaned, ctx.style, True, ctx.latency_ms,
                     ctx.delivery)

    # ---------- командный режим ----------

    def _command_flow(self, job: _Job, res: Result, raw: str) -> None:
        s = self.s
        target = job.target
        # Инструкция — точным словарём: похожие написания могли бы переписать слово,
        # которое пользователь назвал в команде как есть («исправь Xenoposter на …»)
        instruction = textproc.apply_dictionary(raw, self._dict_items())
        if target is None:
            self.notice(tr("eng.cmd_no_window"), level="warn")
            return
        if winutil.is_terminal(target):
            self.notice(tr("eng.cmd_terminal"), level="warn")
            return
        fg = winutil.foreground()
        if fg is None or fg.hwnd != target.hwnd:
            self.notice(tr("eng.cmd_window_changed"), level="warn")
            return
        if winutil.blocked_by_uipi(target):
            self.notice(tr("eng.cmd_admin", name=target.title or target.process),
                        level="warn", action="relaunch_admin")
            return
        try:
            selected = read_selection(target, s)
        except OSError as e:  # буфер занят — копирование даже не отправлялось
            self.notice(tr("eng.cmd_error_retry", error=e), level="warn")
            return
        if selected is None or not selected.strip():
            if selected is None and _is_rdp_info(target, s):
                self.notice(tr("eng.cmd_rdp_selection"), level="warn")
            else:
                self.notice(tr("eng.cmd_select"), level="warn")
            return
        presses = self.machine.presses  # свои Ctrl+Insert/Ctrl+C хук не считает
        with self._lock:
            self._ai_busy = True
        self._refresh_state()
        try:
            r = self._ai_call("command", target.process,
                              lambda: ai.command(instruction, selected, s))
        except ai.AIError as e:
            action = "open_settings_ai" if e.kind in ("no_key", "auth", "payment") else None
            self.notice(tr("eng.cmd_error", error=e), level="error", action=action)
            return
        finally:
            with self._lock:
                self._ai_busy = False
            self._refresh_state()
        out = r.text
        latency = (time.perf_counter() - job.t_release) * 1000
        self.last_text = out.strip()
        self._emit_result(out, instruction, res, latency, True, "")
        d: Delivery | None = None
        reason = self._command_blocker(target, selected, out, presses)
        if reason:
            self.notice(tr("eng.cmd_not_inserted", reason=reason), level="warn")
        else:
            d = self._safe_deliver(out, False)
        self._record(job, res, instruction, out, "", True, latency, d, target_kind="command",
                     words_of=instruction)

    def _command_blocker(self, target: winutil.WindowInfo, selected: str, out: str,
                         presses: int) -> str | None:
        """Почему результат командного режима нельзя вставить поверх выделения (None — можно).

        Ответ ИИ ждём до 30 с: за это время пользователь мог переключиться, выделить другой
        абзац или продолжить печатать — тогда вставка затёрла бы не то."""
        s = self.s
        fg = winutil.foreground()
        if fg is None or fg.hwnd != target.hwnd:
            return tr("eng.cmd_block_window")
        if self.machine.presses != presses:
            return tr("eng.cmd_block_keys")
        typed = resolve_mode(target, s) == MODE_TYPE or _is_remote_info(target, s)
        if typed and out.count("\n") > selected.count("\n"):
            # Печать клавишами: каждый перевод строки — Enter. Лишние строки от модели
            # (в т. ч. внедрённые в выделенный текст) в удалённой консоли выполнились бы.
            return tr("eng.cmd_block_lines")
        try:
            again = read_selection(target, s)
        except OSError:
            again = None
        if not _same_text(again, selected):
            return tr("eng.cmd_block_selection")
        return None

    # ---------- проверочная запись (мастер) ----------

    def test_record_start(self) -> None:
        with self._lock:
            if self._session is not None or self._test_started is not None:
                raise ValueError(tr("eng.recording_busy"))
            started = self._test_started = time.time()
        try:
            self.recorder.start()
        except Exception as e:
            with self._lock:
                self._test_started = None
            raise ValueError(tr("eng.mic_unavailable", error=e)) from None
        # Мастер мог пропасть, не остановив запись (перезагрузка окна, обрыв связи): без
        # лимита диктовка была бы заблокирована навсегда, а аудио копилось бы в памяти.
        t = threading.Timer(TEST_RECORD_MAX_S, self._test_record_expired, (started,))
        t.daemon = True
        with self._lock:
            self._test_timer = t
            if self._test_started == started:
                self._duck_on()
        t.start()
        self.bus.emit("recording", started_at=int(started * 1000), broadcast=False,
                      command=False, hands_free=False, targets=[])
        self._refresh_state()

    def _test_record_expired(self, started: float) -> None:
        if self.cancel_test_record(started):
            self.notice(tr("eng.test_too_long", seconds=TEST_RECORD_MAX_S), level="warn")

    def cancel_test_record(self, started: float | None = None) -> bool:
        """Отменить проверочную запись без распознавания (UI отключился, истёк лимит).
        started — отменять, только если идёт именно эта запись. True — была отменена."""
        with self._lock:
            if self._test_started is None or (started is not None
                                              and self._test_started != started):
                return False
            self._test_started = None
            self._duck_off()
            if self._test_timer is not None:
                self._test_timer.cancel()
                self._test_timer = None
            try:
                self.recorder.stop()
            except Exception:
                log.exception("Не удалось остановить проверочную запись")
        log.info("Проверочная запись отменена")
        self._refresh_state(force=True)
        return True

    def test_record_stop(self) -> dict[str, Any]:
        with self._lock:
            if self._test_started is None:
                raise ValueError(tr("eng.test_not_started"))
            self._test_started = None
            self._duck_off()
            if self._test_timer is not None:
                self._test_timer.cancel()
                self._test_timer = None
            audio_data = self.recorder.stop()
            self._busy += 1
        self._refresh_state()
        try:
            if len(audio_data) < 0.1 * SAMPLE_RATE:
                # распознавать нечего: язык — заданный язык диктовки, иначе язык интерфейса
                lang = self.s.dictation_language
                return {"text": "", "language": lang if lang in i18n.UI_LANGS
                        else i18n.current(),
                        "audio_s": round(len(audio_data) / SAMPLE_RATE, 2), "asr_ms": 0}
            res = self.rec.transcribe(audio_data)
            self._model_phase = "ready"
        finally:
            with self._lock:
                self._busy = max(0, self._busy - 1)
            self._refresh_state(force=True)
        text = self._dict_apply(res.text.strip(), self._dict_items())
        if text and self.s.history_enabled:
            try:
                saved = self.storage.add_history({   # подписи — history_display
                    "ts": time.time(), "app": HISTORY_APP_SELF, "title": "",
                    "raw": res.text.strip(), "text": text, "duration_s": round(res.audio_s, 2),
                    "language": res.language, "style": "as_is", "ai": False,
                    "latency_ms": round(res.elapsed_ms), "target": "test"})
                self.bus.emit("history", history_display(dict(saved)))
            except Exception:
                log.exception("Не удалось записать историю")
        return {"text": text, "language": res.language, "audio_s": round(res.audio_s, 2),
                "asr_ms": round(res.elapsed_ms)}

    # ---------- «чужие» окна, повторная вставка ----------

    def _is_foreign(self, info: winutil.WindowInfo) -> bool:
        return info.pid not in self.own_pids and not winutil.is_shell_window(info)

    def _on_foreground(self, hwnd: int) -> None:
        info = winutil.window_info(hwnd)
        if not self._is_foreign(info):
            return
        self._last_foreign = info
        self._remember_window(info)

    def _remember_window(self, info: winutil.WindowInfo) -> None:
        """Недавние приложения: процесс в начало списка, его заголовок — в начало titles
        (до RECENT_TITLES различных; только в памяти — на диск не пишутся)."""
        if not info.process:
            return
        key = info.process.lower()
        with self._lock:
            prev = self._recent.pop(key, None)
            titles: list[str] = list(prev["titles"]) if prev else []
            if info.title:
                titles = [info.title, *(t for t in titles if t != info.title)][:RECENT_TITLES]
            self._recent[key] = {"process": info.process,
                                 "title": info.title or (prev["title"] if prev else ""),
                                 "titles": titles}
            while len(self._recent) > RECENT_APPS:
                self._recent.popitem(last=False)

    def recent_apps(self) -> list[dict[str, Any]]:
        """[{process, title, titles}] — последние процессы, новые первыми; titles — до 10
        последних различных заголовков окон процесса, свежие первыми."""
        with self._lock:
            return [{"process": v["process"], "title": v["title"], "titles": list(v["titles"])}
                    for v in reversed(self._recent.values())]

    def _insert_target(self) -> winutil.WindowInfo | None:
        fg = winutil.foreground()
        if fg is not None and self._is_foreign(fg):
            return fg
        lf = self._last_foreign
        if lf is not None and winutil.is_window(lf.hwnd):
            if not winutil.is_visible(lf.hwnd):
                raise ValueError(tr("eng.window_hidden_retry", name=lf.title or lf.process))
            return lf
        return None

    def insert_text(self, text: str) -> None:
        """Вставить текст в последнее «чужое» окно (history_reinsert, last_insert)."""
        text = text.strip()
        if not text:
            raise ValueError(tr("eng.nothing_to_insert"))
        target = self._insert_target()
        if target is None:
            raise ValueError(tr("eng.no_target"))
        self._jobs.put(("insert", (text, target)))

    def _last(self) -> str:
        if self.last_text:
            return self.last_text
        try:
            items, _ = self.storage.list_history(None, 1, 0)
        except Exception:
            log.exception("Не удалось прочитать историю")
            return ""
        return str(items[0]["text"]) if items else ""

    def insert_last(self) -> None:
        text = self._last()
        if not text:
            raise ValueError(tr("eng.insert_nothing_yet"))
        self.insert_text(text)

    def copy_last(self) -> None:
        text = self._last()
        if not text:
            raise ValueError(tr("eng.copy_nothing_yet"))
        inject.clipboard_set_text(text)
        self.notice(tr("eng.copied"), toast=False)

    def _insert_to(self, text: str, target: winutil.WindowInfo) -> None:
        fg = winutil.foreground()
        if fg is None or fg.hwnd != target.hwnd:
            name = target.title or target.process
            if not winutil.is_window(target.hwnd):
                self.notice(tr("eng.window_closed", name=name), level="warn")
                return
            if not winutil.is_visible(target.hwnd):
                self.notice(tr("eng.window_hidden", name=name), level="warn")
                return
            if not winutil.activate(target.hwnd):
                self.notice(tr("eng.switch_failed", name=name), level="warn")
                return
            time.sleep(0.15)  # окну нужно время, чтобы принять ввод
        deliver(finalize_text(text, self.s.add_trailing_space), self.s, False, self.notice)

    # ---------- настройки на лету ----------

    def update_settings(self, patch: dict[str, Any]) -> dict[str, Any]:
        """Проверить и применить патч настроек (ValueError на языке интерфейса), сохранить,
        разослать."""
        return self.modify_settings(lambda s: patch)

    def modify_settings(self, make_patch: Callable[[Settings], dict[str, Any] | None]
                        ) -> dict[str, Any]:
        """Прочитать-изменить-записать под замком настроек: make_patch(текущие) → патч.
        Так параллельные команды (галочки RDP, правила приложений) не теряют изменений.

        Патч применяется к копии и сохраняется в файл; только после удачного сохранения
        копия становится настройками движка — ошибка записи (файл занят антивирусом) не
        оставляет в памяти новое значение, которое так и не применилось бы."""
        with self._settings_lock:
            patch = make_patch(self.s)
            changed: list[str] = []
            if patch:
                cand = copy.deepcopy(self.s)
                changed = config.apply_patch(cand, patch)
                if changed:
                    config.save(cand)
                    for k in changed:
                        setattr(self.s, k, getattr(cand, k))
            if changed:
                log.info("Настройки изменены: %s", ", ".join(changed))
                self._apply_changes(changed)
            d = config.to_dict(self.s)
        if changed:
            self.bus.emit("settings", d)
            self._refresh_state(force=True)
        return d

    def _apply_changes(self, changed: list[str]) -> None:
        s = self.s
        if "language" in changed:
            # Сообщения ядра — сразу на новом языке; State.ui_language уйдёт событием state.
            # Закэшированная ошибка баланса написана на прежнем языке — спросить заново
            i18n.set_language(s.language)
            with self._balance_lock:
                if self._balance is not None and self._balance.get("error"):
                    self._balance, self._balance_at = None, 0.0
        hotkeys = any(k in config.HOTKEY_FIELDS for k in changed)
        if (hotkeys or "input_device" in changed) and self._session is not None:
            # Автомат жестов и микрофон меняются — идущую запись завершаем явно
            # (распознаём записанное), а не теряем молча.
            self._interrupt_session(tr("eng.settings_changed_stop"))
        if hotkeys:
            self.machine.configure(*self._machine_args())
            self.hook.set_mouse_button(s.mouse_button)
        if "input_device" in changed:
            self._reopen_recorder()
        if any(k in config.MODEL_FIELDS for k in changed):
            self._jobs.put(("reload", None))
        if "history_retention_days" in changed:
            threading.Thread(target=self._purge, args=(True,), daemon=True).start()
        if "sounds" in changed and s.sounds:
            try:
                sounds.prepare()
            except Exception:
                log.exception("Не удалось подготовить звуки")
        if "ai_enabled" in changed or "ai_provider" in changed:
            self._nokey_warned = False
        if "duck_mode" in changed or "duck_level" in changed:
            # Во время записи: «Не трогать» — вернуть звук; другой режим или уровень — вернуть
            # и приглушить заново (Ducker делает это сам); из «Не трогать» — приглушить
            with self._lock:
                if self._ducked and s.duck_mode == duck.OFF:
                    self._duck_off()
                elif self._ducked or self._recording_confirmed():
                    self._duck_on()
        if ("first_run_done" in changed and s.first_run_done and not self.rec.loaded
                and self._model_phase == "error"):
            self._jobs.put(("load", None))

    def key_changed(self) -> None:
        """Ключ ИИ добавлен/удалён — снова предупредить, если его нет; баланс — заново."""
        self._nokey_warned = False
        with self._balance_lock:
            self._balance = None
            self._balance_at = 0.0

    # ---------- фон: выгрузка модели, срок хранения истории ----------

    def _purge(self, force: bool = False) -> None:
        days = self.s.history_retention_days
        now = time.monotonic()
        if days <= 0 or (not force and now - self._last_purge < PURGE_INTERVAL_S):
            return
        self._last_purge = now
        try:
            n = self.storage.purge_older_than(days)
            if n:
                log.info("Срок хранения истории: удалено %d записей", n)
        except Exception:
            log.exception("Не удалось удалить старую историю")

    def _idle_loop(self) -> None:
        self._purge(force=True)
        if self.s.dict_fuzzy:
            spell.warm_up()                # словари Windows — до первой диктовки
        try:
            # после миграции или новой версии отбора слов — пересобрать частоты по истории
            # (до конца пересборки подсказка распознаванию — без частых слов)
            if not self.storage.vocab_rebuild_async() and self.storage.vocab_ready:
                self.storage.vocab_words(None)     # прогреть кэш частых слов до диктовки
        except Exception:
            log.exception("Не удалось запустить пересборку словаря частот")
        while not self._stop.wait(30):
            n = self.s.unload_after_min
            if (n > 0 and self.rec.loaded and self._session is None and self._busy == 0
                    and self._test_started is None
                    and time.monotonic() - self.rec.last_used > n * 60):
                self.rec.unload()
                self._model_phase = "unloaded"
                self._refresh_state(force=True)
            self._purge()


# ---------- обучение словаря: наблюдение за полем после вставки ----------

class _LearnWatch:
    """Следит за полем, куда вставлена диктовка (UI Automation, поток ``rupor-learn``).

    1. Снимок-основа: поле с фокусом того же процесса, что окно вставки, в тексте которого
       есть вставленный фрагмент (ждём до LEARN_BASELINE_S — приложение принимает текст
       не сразу).
    2. Опрос каждые LEARN_POLL_S (пока поле меняется — LEARN_POLL_FAST_S) до LEARN_WATCH_S.
       Снимки (learn.Snapshot) помнят, когда текст впервые и в последний раз прочитан;
       «хороший» — пока в поле есть большая часть фрагмента (learn.fragment_alive).
    3. Конец и оценка: фокус ушёл с поля (другой runtime_id), поле очищено (Telegram
       отправил сообщение — берём предыдущий хороший снимок), время вышло, новая диктовка
       или новая вставка (поле перечитывается ещё раз). Пауза и выход — конец без оценки.
       Правка, которая держалась в поле меньше LEARN_MIN_HELD_S (Enter сразу после
       последней буквы), не учится — см. Engine._learn_evaluate.
    Оценивается, только если после вставки были нажатия клавиш (machine.presses).
    Текст поля нигде не логируется."""

    def __init__(self, eng: Engine, inserted: str, target: winutil.WindowInfo,
                 presses: int) -> None:
        self.eng = eng
        self.inserted = inserted
        self.target = target
        self.presses = presses
        self._ev = threading.Event()
        self._lock = threading.Lock()
        self._reason = ""
        self._cancel = False
        self._rebase: str | None = None
        self.thread = threading.Thread(target=self._run, name="rupor-learn", daemon=True)

    def start(self) -> None:
        self.thread.start()

    def stop(self, reason: str, evaluate: bool = True) -> None:
        with self._lock:
            if not self._reason:
                self._reason = reason
            if not evaluate:
                self._cancel = True
        self._ev.set()

    def rebase(self, inserted: str, presses: int) -> None:
        with self._lock:
            self._rebase = inserted
            self.presses = presses

    def _run(self) -> None:
        try:
            self._watch()
        except Exception:
            log.exception("Обучение словаря: наблюдение за полем упало")
        finally:
            self.eng._learn_finished(self)

    def _baseline(self, inserted: str) -> tuple[FieldLike, str, tuple[int, int]] | None:
        until = time.monotonic() + LEARN_BASELINE_S
        while True:
            with self._lock:   # ИИ успел заменить текст раньше, чем мы его увидели
                if self._rebase is not None:
                    inserted, self._rebase = self._rebase, None
                    until = time.monotonic() + LEARN_BASELINE_S
            f = self.eng._field_reader()
            if f is not None and f.pid == self.target.pid:
                text = f.read_text()
                if text is not None:
                    text = learn.norm_text(text)
                    span = learn.fragment_span(text, inserted)
                    if span is not None:
                        return f, text, span
            if time.monotonic() >= until or self._ev.wait(LEARN_BASELINE_POLL_S):
                log.debug("Обучение словаря: поле со вставленным текстом не найдено")
                return None

    def _watch(self) -> None:
        base = self._baseline(self.inserted)
        if base is None:
            return
        fld, before, span = base
        now = time.monotonic()
        snaps = [learn.Snapshot(before, now, now)]
        deadline = now + LEARN_WATCH_S
        changed_at = 0.0
        reason = "время вышло"
        polls = changes = 0
        while True:
            busy = time.monotonic() - changed_at < LEARN_FAST_FOR_S
            stopped = self._ev.wait(LEARN_POLL_FAST_S if busy else LEARN_POLL_S)
            if stopped:
                with self._lock:
                    reason, cancel = self._reason, self._cancel
                if cancel:
                    log.debug("Обучение словаря: наблюдение прервано (%s)", reason)
                    return
                # Новая диктовка или вставка: прочитать поле ещё раз — что в нём сейчас
            else:
                with self._lock:
                    rebase, self._rebase = self._rebase, None
                if rebase is not None:
                    base = self._baseline(rebase)
                    if base is None:
                        return
                    fld, before, span = base
                    now = time.monotonic()
                    snaps = [learn.Snapshot(before, now, now)]
                    continue
                now = time.monotonic()
                if now >= deadline and not (now - changed_at < LEARN_SETTLE_S
                                            and now < deadline + LEARN_EXTRA_S):
                    break              # (слово, которое печатают прямо сейчас, не берём)
            polls += 1
            f = self.eng._field_reader()
            if f is None or f.runtime_id != fld.runtime_id:
                reason = reason if stopped else "фокус ушёл с поля"
                break
            text = f.read_text()
            if text is None:
                reason = reason if stopped else "поле недоступно"
                break
            text = learn.norm_text(text)
            now = time.monotonic()
            cur = snaps[-1]
            if text == cur.text:
                cur.last = now
            else:
                changes += 1
                changed_at = now
                empty = not text.strip()
                snaps.append(learn.Snapshot(
                    text, now, now, not empty and learn.fragment_alive(before, span, text)))
                del snaps[:-LEARN_SNAPSHOTS]
                if empty:
                    reason = reason if stopped else "поле очищено"
                    break
            if stopped:
                break
        self.eng._learn_evaluate(self, before, span, snaps, reason, polls, changes)
