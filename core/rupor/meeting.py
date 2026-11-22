"""Перевод встречи: звук браузера → фразы (VAD) → распознавание → перевод → окно сбоку.

Конвейер одной встречи (``MeetingSession``, владелец — движок):

1. Звук приходит из отдельного процесса захвата (``loopback.LoopbackStream``: float32 моно
   16 кГц, тишина тоже приходит — нулями) в поток чтения → ``on_audio``: уровень звука
   (``level``) и ``StreamVad``.
2. ``StreamVad`` — Silero VAD потоково (сессия ONNX из faster-whisper, состояние h/c
   переносится между вызовами): фраза начинается при вероятности ≥ 0.5, кончается после
   400 мс < 0.35, 200 мс звука до начала берутся с собой; фраза длиннее 12 с режется по
   самому «тихому» кадру последних 2 с. Внутренностей faster-whisper нет — запасной путь
   ``get_speech_timestamps`` по хвосту буфера.
3. Поток ``rupor-meeting-asr`` (низкий приоритет ``ModelGate``: диктовка всегда первая,
   ``yield_to`` = ``Engine._meeting_yield``; модель занята или не загружена — ждать,
   состояние ``waiting_gpu``): пока фраза звучит — промежуточные проходы (beam 1, не чаще
   ``MEETING_STEP_S``) по звуку с её начала, LocalAgreement-2: совпавшее с прошлой гипотезой
   начало принимается (``MeetingLine.text``), остальное — хвост (``partial``); фраза
   кончилась — последний проход (beam 5) всей фразы, его текст — окончательный. Готовых
   фраз ждут больше 3 — склеиваются в окна до 28 с.
4. Потоки ``rupor-meeting-tr1``/``tr2`` (не больше двух вызовов в полёте): принятые слова
   переводятся кусками потоком («продолжи перевод» уже показанного), фраза кончилась и
   переводилась кусками — перевод всей фразы разом (``meeting_refine``). Язык строки = язык
   перевода или перевод выключен — без ИИ (status ``original``). Нет ключа, ключ отклонён,
   нет денег — до конца встречи только расшифровка и одно уведомление; сетевые ошибки —
   кусок ещё раз, вторая неудача — строка с ошибкой, 3 подряд — пауза 20 с.
5. Расшифровка (``meeting_save``) — Markdown в ``meeting_dir``: каждая готовая строка
   дописывается и сбрасывается на диск сразу, по порядку строк. Файл называется
   «YYYY-MM-DD HH-MM — <название>.md»: сначала — без ИИ (``fallback_title``: заголовок окна
   встречи без хвоста браузера, «Встреча — <первые слова>», имя программы), потом — по
   содержанию (``meeting_ai_title``, поток ``rupor-meeting-title``: ``ai.meeting_title``
   после 8 готовых строк и 90 с встречи и ещё раз после «Стоп»); переименование не теряет
   строк (``TranscriptWriter.rename``). В файле: заголовок (название, дата и — после «Стоп» —
   длительность, источник, языки), заметки (если есть) и «## Полная расшифровка» со строками.
6. Заметки к встрече (``meeting_notes``, поток ``rupor-meeting-notes``, не больше одного
   вызова в полёте): сами после «Стоп», если готовых строк хотя бы NOTES_MIN_LINES или речи
   NOTES_MIN_SPEECH_S, и по команде (WS ``meeting_notes``) во время встречи и после неё.
   ``ai.meeting_notes`` (модель посильнее) пишет «# название» и разделы; название из заметок
   — последнее название записи (отдельный последний вызов ``ai.meeting_title`` — только если
   заметок нет или они не удались), разделы — в файл между заголовком и расшифровкой
   (``TranscriptWriter.set_notes``: то же безопасное переписывание, что у переименования).
7. Автоостановка (``meeting_autostop``, ``AutoStop``; движок зовёт ``autostop_tick`` раз в
   2 с с записью микрофона программы встречи от ``meeting_detect``): программа отпустила
   микрофон ≥ AUTOSTOP_MIC_OFF_S и речи в захвате нет ≥ AUTOSTOP_QUIET_S, а у источника без
   записи микрофона («весь звук компьютера») — цифровая тишина ≥ AUTOSTOP_SILENCE_S →
   ``hint: "ending"`` с отсчётом ``ending_in_s`` (AUTOSTOP_GRACE_S) и, если за это время
   речь не вернулась, микрофон не взяли снова и не нажали «Не останавливать»
   (``autostop_cancel``), — обычный «Стоп» (дораспознать, последнее название, заметки) и
   уведомление. Источник закрылся (процесс захвата вышел с кодом 3) — «Стоп» сразу.

Ничего из этого не трогает очередь заданий диктовки, ``_busy``/``_ai_busy`` и событие
``result``: пилюля и состояние диктовки встречу не видят.
"""
from __future__ import annotations

import collections
import itertools
import logging
import math
import os
import re
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

import numpy as np

from . import ai, config
from .asr import BG_TEMPERATURES, SAMPLE_RATE, ChunkResult, adapt_prompt, garbage_reason
from .i18n import tr

log = logging.getLogger(__name__)

# ---------- VAD ----------

FRAME = 512                         # кадр Silero: 32 мс при 16 кГц
FRAME_S = FRAME / SAMPLE_RATE
VAD_START_P = 0.5                   # вероятность речи: начало фразы
VAD_END_P = 0.35                    # …ниже — тишина
VAD_END_MS = 400                    # столько тишины — конец фразы (последний проход)
VAD_PREROLL_MS = 200                # звук до начала фразы — с собой
VAD_TAIL_MS = 200                   # тишины после фразы оставить в куске
VAD_MAX_S = 12.0                    # длиннее — резать
VAD_MAX_S_CPU = 6.0                 # …на процессоре — короче (кусок держит модель дольше)
VAD_CUT_WINDOW_S = 2.0              # резать по самому тихому кадру последних 2 с
VAD_MIN_SPEECH_MS = 250             # речи меньше — щелчок, а не фраза
FALLBACK_CONTEXT_S = 2.0            # запасной VAD: столько прежнего звука для разгона модели

# ---------- очередь и распознавание ----------

QUEUE_MERGE_AFTER = 3               # ждут больше — склеивать
MERGE_MAX_S = 28.0                  # склеенный кусок не длиннее (окно Whisper — 30 с)
MERGE_MAX_S_CPU = 12.0
MERGE_GAP_S = 0.3                   # тишина между склеенными фразами
QUEUE_MAX_S = 600.0                 # звука в очереди не больше (дальше — старое прочь)
ASR_GATE_TIMEOUT_S = 1.0            # ждать модель за раз (потом — проверить остановку)
WAIT_GPU_AFTER_S = 1.0              # фраза ждёт модель дольше — состояние waiting_gpu
ASR_ERRORS_MAX = 3                  # столько сбоев распознавания подряд — встреча стоп
LANG_MIN_PROB = 0.5                 # язык неуверенный — язык прошлой строки
PROMPT_PREV_CHARS = 200             # подсказка Whisper: хвост принятого текста встречи
PROMPT_TERMS_MAX = 20
HISTORY_KEEP = 400                  # принятого текста встречи помнить (для подсказки)
DRAIN_S = 8.0                       # «Стоп»: дораспознать очередь не дольше

# ---------- потоковое распознавание (LocalAgreement-2) ----------
# Замер (RTX 3060 Ti, large-v3-turbo fp16, окно 1.5–15 с, карта занята и другими): проход
# beam 1 — 0.21–0.27 с почти независимо от длины окна (энкодер всегда считает 30 с); язык не
# задан — ещё +0.21 с (второй проход энкодера); word_timestamps — +0.2…0.6 с; beam 5 — +0.01 с.
MEETING_STEP_S = 0.8                # промежуточный проход не чаще: модель занята встречей ≈ 30 %
STEP_DUTY = 2.5                     # …и не чаще 2.5 × время прохода (медленная карта — реже)
INTERIM_MIN_S = 0.8                 # первый проход — когда у фразы 0.8 с звука (≈ 0.6 с речи)
INTERIM_NEW_S = 0.25                # новый проход — если пришло хотя бы 0.25 с нового звука
INTERIM_MAX_S = 15.0                # фраза длиннее (VAD режет на 12 с) — без проходов
INTERIM_GATE_TIMEOUT_S = 0.25       # модель занята — не ждать: следующий проход возьмёт свежий звук
INTERIM_BEAM = 1                    # промежуточный проход — быстро
FINAL_BEAM = 5                      # последний проход фразы — точнее (и почти не дороже)
# Язык фразы по первым 1–2 с речи Whisper угадывает плохо (замер, английский с русским
# акцентом: 0.8 с — ru 0.26–0.67, 1.6 с — ru до 0.86; вся фраза — en 0.31–0.90): язык
# фиксируется только уверенный, иначе определяется снова; LANG_TRIES проходов без
# уверенности — последний (по самому длинному звуку), но «слабо»: последний проход фразы
# определит язык по всей фразе. Две прошлые строки на одном языке (язык встречи) — первый
# проход фразы всё равно определяет язык (+0.2 с один раз: другой язык посреди встречи —
# русская фраза в английской — по 0.8 с узнаётся с 0.98–1.00, а с заданным чужим языком
# Whisper пишет перевод или не той письменностью); уверенно — язык фразы сразу и без
# повторного определения в последнем проходе (−0.2 с в конце фразы), неуверенно (акцент:
# не выше 0.89) — дальше с языком встречи, слабо: последний проход проверит.
LANG_FIX_PROB = 0.9                 # язык фразы по промежуточному проходу — с такой уверенности
LANG_TRIES = 3                      # столько проходов без уверенности — «слабый» выбор
END_SLACK_S = 0.05                  # проход «дошёл до конца речи» с таким допуском

# ---------- перевод ----------

TR_FAILS_PAUSE = 3                  # ошибок подряд — пауза
TR_PAUSE_S = 20.0
TR_RETRY_MAX_AGE_S = 2.0            # 429/5xx: повторить сразу, если кусок ждёт меньше 2 с
TR_MAX_INFLIGHT = 2                 # вызовов перевода в полёте (куски одной строки — по очереди)
TR_CHUNK_WORDS = 6                  # столько непереведённых принятых слов — кусок в перевод
TR_CHUNK_WAIT_S = 1.2               # …или столько они ждут
TR_CLAUSE_MIN_WORDS = 3             # запятая, двоеточие, тире — граница куска от 3 слов
TR_RETRY_S = 1.0                    # кусок не перевёлся — ещё раз не раньше чем через 1 с
TR_LINE_FAILS = 2                   # столько неудач у строки — строка с ошибкой
REFINE_SKIP_WORDS = 6               # строка не длиннее — перевод всей фразой не нужен
LINE_EMIT_EVERY_S = 0.1             # событие meeting_line одной строки — не чаще 10 раз в секунду
LATENCY_WINDOW = 20                 # MeetingStatus.latency — среднее последних 20 строк
SENT_END = (".", "?", "!", "…", "。", "？", "！")
CLAUSE_END = (",", ";", ":", "—", "–", "，", "；", "：", "、")
TR_AI_WAIT_S = 1.0                  # идёт ИИ-очистка диктовки — подождать до 1 с
TR_STOP_KINDS = frozenset({"no_key", "auth", "payment", "forbidden"})
TR_RETRY_KINDS = frozenset({"rate_limit", "server"})

# ---------- состояние ----------

LINES_MAX = 5000                    # строк в памяти (файл — все)
LEVEL_EVERY_S = 0.25                # событие meeting ради level — не чаще 4 раз в секунду
LEVEL_STEP = 0.02
NO_AUDIO_S = 20.0                   # цифровой тишины дольше — состояние no_audio
SILENCE_PEAK = 1e-5                 # пик ниже — цифровая тишина
STATES = ("idle", "listening", "waiting_gpu", "no_audio", "error")

# ---------- автоостановка: встреча кончилась (meeting_autostop) ----------

AUTOSTOP_MIC_OFF_S = 15.0           # (a) программа встречи отпустила микрофон столько…
AUTOSTOP_QUIET_S = 30.0             # …и речи (VAD) в захвате нет столько
AUTOSTOP_SILENCE_S = 180.0          # (c) цифровая тишина (источник без записи микрофона)
AUTOSTOP_GRACE_S = 10.0             # «Встреча закончилась — останавливаю через N с»
HINT_ENDING = "ending"              # MeetingStatus.hint: идёт отсчёт автоостановки

# ---------- название записи ----------

TITLE_EARLY_LINES = 8               # название по содержанию (ИИ): столько готовых строк…
TITLE_EARLY_S = 90.0                # …и столько секунд от начала встречи
TITLE_FINAL_LINES = 3               # после «Стоп» — заново по всей расшифровке, если строк столько
TITLE_WAIT_S = 1.0                  # поток названия проверяет сроки не реже
TITLE_PART_MAX = 80                 # название в имени файла — не длиннее
TITLE_FIRST_WORDS = 6               # «Встреча — <первые слова первой строки>»
TITLE_FIRST_CHARS = 12              # …у языков без пробелов — символов
FILE_SEP = " — "                    # «YYYY-MM-DD HH-MM — название.md»
RENAME_TRIES = 1000                 # «(2)», «(3)»… — не дальше

# ---------- заметки к встрече ----------

NOTES_MIN_LINES = 5                 # сами после «Стоп» — если готовых непустых строк столько…
NOTES_MIN_SPEECH_S = 120.0          # …или речи (готовых строк) столько секунд
NOTES_EMIT_EVERY_S = 0.5            # событие meeting_notes, пока модель пишет, — не чаще

# Браузеры (имя exe): только у них заголовок окна говорит о встрече в вкладке
BROWSERS: dict[str, str] = {
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox",
    "brave.exe": "Brave", "opera.exe": "Opera", "vivaldi.exe": "Vivaldi",
    "browser.exe": "Yandex Browser", "yandex.exe": "Yandex Browser", "arc.exe": "Arc",
    "chromium.exe": "Chromium", "thorium.exe": "Thorium", "librewolf.exe": "LibreWolf",
    "waterfox.exe": "Waterfox", "floorp.exe": "Floorp", "zen.exe": "Zen",
    "opera_gx.exe": "Opera GX", "iexplore.exe": "Internet Explorer",
}
CYRILLIC_LANGS = frozenset({"ru", "uk", "be", "bg", "sr", "kk", "mk"})


def exe_name(exe: str) -> str:
    return os.path.basename(exe or "").lower()


def is_browser(exe: str) -> bool:
    return exe_name(exe) in BROWSERS


def app_name(exe: str) -> str:
    """Имя программы для уведомлений: браузеры — по таблице, прочие — имя exe без .exe."""
    name = exe_name(exe)
    if name in BROWSERS:
        return BROWSERS[name]
    stem = name[:-4] if name.endswith(".exe") else name
    return stem[:1].upper() + stem[1:] if stem else exe


def title_matches(title: str, patterns: Sequence[str]) -> bool:
    """Заголовок окна похож на встречу: есть подстрока из patterns (без учёта регистра и «ё»)."""
    t = config.fold_title(title or "")
    return bool(t) and any(p and config.fold_title(p) in t for p in patterns)


# ---------- вероятности речи ----------

ProbFn = Callable[[np.ndarray], np.ndarray]   # кадры [k, 512] → вероятности [k]


class SileroStream:
    """Silero VAD потоково: сессия ONNX faster-whisper (входы input[N, 576], h, c), каждой
    строке входа — 64 последних сэмпла прошлого кадра + кадр; h/c переносятся между
    вызовами. Вероятности совпадают с пакетным ``SileroVADModel.__call__``."""

    def __init__(self, session: Any) -> None:
        self._sess = session
        self._h = np.zeros((1, 1, 128), np.float32)
        self._c = np.zeros((1, 1, 128), np.float32)
        self._ctx = np.zeros(64, np.float32)

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        if not len(frames):
            return np.zeros(0, np.float32)
        frames = np.asarray(frames, np.float32).reshape(-1, FRAME)
        ctx = np.empty((len(frames), 64), np.float32)
        ctx[0] = self._ctx
        ctx[1:] = frames[:-1, -64:]
        x = np.concatenate([ctx, frames], axis=1)
        out, self._h, self._c = self._sess.run(None, {"input": x, "h": self._h, "c": self._c})
        self._ctx = frames[-1, -64:].copy()
        return np.asarray(out, np.float32).reshape(-1)[:len(frames)]


class TailFallback:
    """Запасные вероятности речи без внутренностей faster-whisper: публичный
    ``get_speech_timestamps`` по хвосту буфера (FALLBACK_CONTEXT_S прежнего звука +
    новые кадры, порог 0.5 / 0.35 без пауз и отступов) — кадр внутри речи 1.0, иначе 0.0."""

    def __init__(self, timestamps: Callable[..., list[dict[str, int]]] | None = None) -> None:
        if timestamps is None:
            from faster_whisper.vad import VadOptions, get_speech_timestamps
            opts = VadOptions(threshold=VAD_START_P, neg_threshold=VAD_END_P,
                              min_speech_duration_ms=0, min_silence_duration_ms=0,
                              speech_pad_ms=0)

            def timestamps(audio: np.ndarray) -> list[dict[str, int]]:
                return list(get_speech_timestamps(audio, opts))
        self._ts = timestamps
        self._ctx = np.zeros(0, np.float32)
        self._ctx_max = int(FALLBACK_CONTEXT_S * SAMPLE_RATE) // FRAME * FRAME

    def __call__(self, frames: np.ndarray) -> np.ndarray:
        frames = np.asarray(frames, np.float32).reshape(-1, FRAME)
        k = len(frames)
        if not k:
            return np.zeros(0, np.float32)
        new = frames.reshape(-1)
        audio = np.concatenate([self._ctx, new])
        off = len(self._ctx)
        probs = np.zeros(k, np.float32)
        for seg in self._ts(audio):
            a = max(0, (int(seg["start"]) - off) // FRAME)
            b = min(k, -(-(int(seg["end"]) - off) // FRAME))
            if b > a:
                probs[a:b] = 1.0
        self._ctx = audio[-self._ctx_max:] if self._ctx_max else np.zeros(0, np.float32)
        return probs


def default_prob_fn() -> ProbFn:
    """Потоковый Silero из faster-whisper, если его внутренности те, что ожидаются
    (faster-whisper 1.2: ``get_vad_model().session`` с входами input/h/c), иначе запасной
    ``TailFallback``."""
    try:
        from faster_whisper.vad import get_vad_model
        sess = getattr(get_vad_model(), "session", None)
        names = {i.name for i in sess.get_inputs()} if sess is not None else set()
        if {"input", "h", "c"} <= names:
            stream = SileroStream(sess)
            probe = stream(np.zeros((1, FRAME), np.float32))
            if probe.shape == (1,):
                return SileroStream(sess)
        log.warning("Silero VAD faster-whisper не тот (%s) — запасной VAD", sorted(names))
    except Exception:
        log.exception("Потоковый Silero VAD недоступен — запасной VAD")
    return TailFallback()


# ---------- фразы ----------

@dataclass
class Utterance:
    audio: np.ndarray
    start_s: float          # от начала потока звука
    end_s: float
    seq: int = -1           # номер фразы StreamVad (-1 — склеенная очередь)
    speech_end_s: float | None = None   # конец речи (без хвоста тишины)

    @property
    def dur(self) -> float:
        return len(self.audio) / SAMPLE_RATE


def merge_utterances(parts: Sequence[Utterance], gap_s: float = MERGE_GAP_S) -> Utterance:
    """Склеить фразы в один кусок (между ними — gap_s тишины)."""
    if len(parts) == 1:
        return parts[0]
    gap = np.zeros(int(gap_s * SAMPLE_RATE), np.float32)
    chunks: list[np.ndarray] = []
    for i, u in enumerate(parts):
        if i:
            chunks.append(gap)
        chunks.append(u.audio)
    last = parts[-1]
    return Utterance(np.concatenate(chunks), parts[0].start_s, last.end_s, -1,
                     last.speech_end_s if last.speech_end_s is not None else last.end_s)


class StreamVad:
    """Нарезка непрерывного звука на фразы по вероятностям речи (кадры 32 мс).

    ``feed(audio)`` → готовые фразы; ``flush()`` — конец звука (недоговорённая фраза
    отдаётся, если в ней есть речь). ``prob_fn`` — вероятности речи кадров (по умолчанию
    ``default_prob_fn()``); сбой — переход на ``TailFallback`` (один раз в журнал).
    Звучащая фраза — ``snapshot()`` (номер ``seq``, начало, звук с начала); у каждой фразы
    свой номер (продолжение после нарезки длинной — тоже новый); фраза, выброшенная как
    щелчок, — ``on_discard(seq)``."""

    def __init__(self, prob_fn: ProbFn | None = None, *, max_s: float = VAD_MAX_S,
                 start_p: float = VAD_START_P, end_p: float = VAD_END_P,
                 end_ms: int = VAD_END_MS, preroll_ms: int = VAD_PREROLL_MS,
                 tail_ms: int = VAD_TAIL_MS, cut_window_s: float = VAD_CUT_WINDOW_S,
                 min_speech_ms: int = VAD_MIN_SPEECH_MS,
                 on_discard: Callable[[int], None] | None = None) -> None:
        self._prob: ProbFn = prob_fn if prob_fn is not None else default_prob_fn()
        self._on_discard = on_discard
        self._seq = 0                                   # номер текущей (последней) фразы
        self.start_p, self.end_p = start_p, end_p
        self._end_n = max(1, round(end_ms / 1000 / FRAME_S))
        self._pre_n = max(0, round(preroll_ms / 1000 / FRAME_S))
        self._tail_n = max(0, min(self._end_n, round(tail_ms / 1000 / FRAME_S)))
        self._max_n = max(4, round(max_s / FRAME_S))
        self._cut_n = max(1, min(self._max_n - 1, round(cut_window_s / FRAME_S)))
        self._min_speech_n = max(1, round(min_speech_ms / 1000 / FRAME_S))
        self._pending = np.zeros(0, np.float32)
        self._pos = 0                                   # кадров обработано
        self._pre: collections.deque[np.ndarray] = collections.deque(maxlen=self._pre_n or 1)
        self._frames: list[np.ndarray] = []
        self._probs: list[float] = []
        self._start = 0                                 # номер первого кадра фразы
        self._silence = 0
        self._speech = False
        self._failed = False

    def set_max_s(self, max_s: float) -> None:
        """Сменить наибольшую длину фразы (модель оказалась на другом устройстве); звучащая
        фраза режется уже по новой."""
        self._max_n = max(4, round(max_s / FRAME_S))
        self._cut_n = max(1, min(self._max_n - 1, round(VAD_CUT_WINDOW_S / FRAME_S)))

    @property
    def in_speech(self) -> bool:
        return self._speech

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def speech_s(self) -> float:
        """Длина звучащей фразы (с 200 мс до начала), 0 — тишина."""
        return len(self._frames) * FRAME_S if self._speech else 0.0

    def snapshot(self) -> tuple[int, float, np.ndarray] | None:
        """Звучащая фраза: (seq, начало от начала потока, звук с начала); тишина — None."""
        if not self._speech or not self._frames:
            return None
        return (self._seq, self._start * FRAME_S,
                np.concatenate(self._frames).astype(np.float32, copy=False))

    def feed(self, audio: np.ndarray) -> list[Utterance]:
        a = np.asarray(audio, np.float32).reshape(-1)
        if self._pending.size:
            a = np.concatenate([self._pending, a])
        k = len(a) // FRAME
        self._pending = a[k * FRAME:].copy()
        if not k:
            return []
        frames = a[:k * FRAME].reshape(k, FRAME)
        probs = self._probs_of(frames)
        out: list[Utterance] = []
        for frame, p in zip(frames, probs):
            u = self._step(frame, float(p))
            if u is not None:
                out.append(u)
        return out

    def flush(self) -> list[Utterance]:
        out: list[Utterance] = []
        if self._speech and self._frames:
            u = self._emit(len(self._frames), len(self._frames) - self._silence)
            if u is not None:
                out.append(u)
        self._reset_speech()
        self._pending = np.zeros(0, np.float32)
        return out

    # --- внутреннее ---

    def _probs_of(self, frames: np.ndarray) -> np.ndarray:
        try:
            p = np.asarray(self._prob(frames), np.float32).reshape(-1)
            if len(p) != len(frames):
                raise ValueError(f"VAD вернул {len(p)} вероятностей на {len(frames)} кадров")
            return p
        except Exception:
            if self._failed:
                raise
            self._failed = True
            log.exception("VAD упал — перехожу на запасной")
            self._prob = TailFallback()
            return np.asarray(self._prob(frames), np.float32).reshape(-1)

    def _step(self, frame: np.ndarray, p: float) -> Utterance | None:
        idx = self._pos
        self._pos += 1
        if not self._speech:
            if p >= self.start_p:
                pre = list(self._pre) if self._pre_n else []
                self._speech = True
                self._seq += 1
                self._frames = [*pre, frame]
                self._probs = [0.0] * len(pre) + [p]
                self._start = idx - len(pre)
                self._silence = 0
                self._pre.clear()
            elif self._pre_n:
                self._pre.append(frame)
            return None
        self._frames.append(frame)
        self._probs.append(p)
        self._silence = self._silence + 1 if p < self.end_p else 0
        if self._silence >= self._end_n:
            keep = len(self._frames) - self._silence + self._tail_n
            rest = self._frames[keep:]
            u = self._emit(keep, len(self._frames) - self._silence)
            self._reset_speech()
            for f in rest[-self._pre_n:] if self._pre_n else []:
                self._pre.append(f)
            return u
        if len(self._frames) >= self._max_n:
            window = self._probs[-self._cut_n:]
            low = int(np.argmin(np.asarray(window)))
            cut = len(self._frames) - self._cut_n + low + 1
            u = self._emit(cut)
            self._seq += 1                              # продолжение — новая фраза
            self._frames = self._frames[cut:]
            self._probs = self._probs[cut:]
            self._start += cut
            silence = 0
            for q in reversed(self._probs):
                if q >= self.end_p:
                    break
                silence += 1
            self._silence = silence
            return u
        return None

    def _emit(self, n: int, speech_n: int | None = None) -> Utterance | None:
        frames, probs = self._frames[:n], self._probs[:n]
        if not frames or sum(1 for q in probs if q >= self.start_p) < self._min_speech_n:
            if self._on_discard is not None:
                try:
                    self._on_discard(self._seq)
                except Exception:
                    log.exception("VAD: обработчик выброшенной фразы упал")
            return None
        audio = np.concatenate(frames).astype(np.float32, copy=False)
        start = self._start * FRAME_S
        speech = min(len(frames), speech_n if speech_n is not None else len(frames))
        return Utterance(audio, start, start + len(frames) * FRAME_S, self._seq,
                         start + max(1, speech) * FRAME_S)

    def _reset_speech(self) -> None:
        self._speech = False
        self._frames = []
        self._probs = []
        self._silence = 0


# ---------- строки и расшифровка ----------

@dataclass
class LineTr:
    """Перевод строки по кускам (внутреннее, наружу не идёт)."""
    on: bool = False                # строку переводим (язык известен и ≠ языку перевода)
    src: list[str] = field(default_factory=list)    # переведённые исходные слова (norm_word)
    parts: list[str] = field(default_factory=list)  # переведённые исходные куски (текст)
    shown: str = ""                 # их перевод (показан)
    calls: int = 0                  # удачных вызовов по кускам
    busy: bool = False              # вызов в полёте
    since: float = 0.0              # monotonic: непереведённые принятые слова ждут с тех пор
    retry_at: float = 0.0           # кусок не перевёлся — следующая попытка не раньше
    fails: int = 0
    failed: str | None = None       # перевод строки не удался (текст ошибки)
    refine: str = ""                # "" (не решено) | wait | run | done | skip
    last_first: float | None = None     # последний кусок: первый токен (monotonic)
    last_done: float | None = None      # …и конец
    settled: bool = False           # работа перевода строки закончена


@dataclass(eq=False)              # сравнение и хэш — по объекту (строки в списках)
class MeetingLine:
    id: int
    t: float                        # секунды от начала встречи
    ts: str                         # местное время HH:MM:SS
    lang: str
    text: str                       # принятый текст фразы (после конца — последнего прохода)
    translation: str | None = None  # растёт по мере перевода кусков
    status: str = "pending"         # pending | streaming | done | original | error
    error: str | None = None
    created: float = 0.0            # monotonic — когда появилась (не наружу)
    partial: str = ""               # неуверенный хвост гипотезы (ещё может измениться)
    final: bool = False             # фраза кончилась
    refined: bool = False           # перевод — всей фразы разом (заменил кусочный)
    # --- потоковое распознавание (не наружу) ---
    seq: int = -1                   # номер фразы StreamVad
    words: list[str] = field(default_factory=list)  # принятые слова
    tail: list[str] = field(default_factory=list)   # хвост прошлой гипотезы
    lang_ok: bool = False           # язык фразы определён (дальше проходы — с ним)
    lang_weak: bool = False         # …без уверенности: последний проход определит заново
    dropped: bool = False           # фраза оказалась мусором — строку окну не показывать
    passes: list[tuple[float, float]] = field(default_factory=list)  # (конец окна, показано)
    end_s: float | None = None      # конец речи (секунды потока)
    end_mono: float | None = None   # …когда он пришёл (monotonic)
    shown_end: float | None = None  # когда на экране был текст до конца речи
    lat_done: bool = False
    last_emit: float = 0.0
    tr: LineTr = field(default_factory=LineTr)

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {"id": self.id, "t": round(self.t, 2), "ts": self.ts,
                             "lang": self.lang, "text": self.text, "partial": self.partial,
                             "translation": self.translation, "status": self.status,
                             "final": self.final, "refined": self.refined}
        if self.error is not None:
            d["error"] = self.error
        return d


def line_source(line: MeetingLine) -> str:
    """Исходный текст строки целиком: принятое и (если фраза не кончилась) хвост."""
    return " ".join(f"{line.text} {line.partial}".split())


# ---------- слова гипотез и LocalAgreement-2 ----------

_NOT_WORD_RE = re.compile(r"[\W_]+")


def norm_word(w: str) -> str:
    """Слово для сравнения гипотез: без регистра, «ё» и знаков препинания."""
    return _NOT_WORD_RE.sub("", w.casefold().replace("ё", "е"))


def split_words(text: str, lang: str = "") -> list[str]:
    """Слова гипотезы как распознаны (со знаками). Знак отдельно («—») — к прошлому слову.
    Языки без пробелов (zh, ja, …) — по символу."""
    nospace = lang in ai.NO_SPACE_LANGS
    raw = [c for c in text if not c.isspace()] if nospace else text.split()
    out: list[str] = []
    for tok in raw:
        if out and not norm_word(tok):
            out[-1] += tok if nospace else " " + tok
        else:
            out.append(tok)
    return out


def join_words(words: Sequence[str], lang: str = "") -> str:
    return ("" if lang in ai.NO_SPACE_LANGS else " ").join(words)


# Языки Whisper на латинице (для проверки письменности прохода с заданным языком)
LATIN_LANGS = frozenset({
    "en", "de", "fr", "es", "it", "pt", "pl", "tr", "nl", "sv", "da", "no", "nn", "fi", "cs",
    "sk", "sl", "hr", "bs", "ro", "hu", "lt", "lv", "et", "is", "ga", "cy", "eu", "ca", "gl",
    "af", "sw", "id", "ms", "tl", "vi", "az", "uz", "sq", "mt", "lb", "oc", "br", "la", "tk",
    "fo", "jw", "su", "mi", "haw", "ln", "yo", "ha", "so", "sn", "ht",
})
_CYR_RE = re.compile(r"[\u0400-\u04ff]")
_LAT_RE = re.compile(r"[A-Za-z\u00c0-\u024f]")
SCRIPT_MIN_LETTERS = 4              # букв меньше — письменность не проверять


def script_mismatch(text: str, lang: str) -> str | None:
    """Проход с заданным языком написал текст не той письменностью: Whisper с языком встречи
    на речи другого языка (en на русской речи — «Colleegi, я коротко добавлю»). Кириллицы
    больше латиницы у языка на латинице — "ru", латиницы больше у языка на кириллице — "en"
    (догадка до следующего прохода, он определит язык сам); иначе None. Остальные
    письменности (zh, ja, ar, el…) не проверяются."""
    cyr, lat = len(_CYR_RE.findall(text)), len(_LAT_RE.findall(text))
    if cyr + lat < SCRIPT_MIN_LETTERS:
        return None
    if lang in LATIN_LANGS and cyr > lat:
        return "ru"
    if lang in CYRILLIC_LANGS and lat > cyr:
        return "en"
    return None


def skip_done(done: Sequence[str], hyp: Sequence[str]) -> int:
    """Где в гипотезе ``hyp`` кончаются уже принятые слова ``done`` (слова — norm_word).

    Гипотеза каждый раз — с начала фразы, поэтому обычно начинается с принятых слов. Модель
    поправила принятое слово, добавила или потеряла слово — граница по наименьшему числу
    правок между ``done`` и началом ``hyp`` (при равенстве — ближе к len(done))."""
    n, m = len(done), len(hyp)
    if not n:
        return 0
    if list(hyp[:n]) == list(done):
        return n
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        a = done[i - 1]
        for j in range(1, m + 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (a != hyp[j - 1]))
        prev = cur
    return min(range(m + 1), key=lambda j: (prev[j], abs(j - n)))


def agree(prev: Sequence[str], new: Sequence[str]) -> int:
    """LocalAgreement-2: сколько первых слов новой гипотезы совпало с прошлой."""
    k = 0
    for a, b in zip(prev, new):
        if a != b:
            break
        k += 1
    return k


_BAD_NAME_RE = re.compile(r'[<>:"/\\|?*\x00-\x1f]+')
# Имена устройств Windows: файл с таким именем (и с любым расширением) не создать
_RESERVED_NAMES = frozenset({
    "con", "prn", "aux", "nul", "conin$", "conout$",
    *(f"{p}{n}" for p in ("com", "lpt") for n in (*"0123456789", "¹", "²", "³")),
})


def safe_file_part(name: str, limit: int = TITLE_PART_MAX) -> str:
    """Часть имени файла из названия: без запрещённых, невидимых и направляющих символов
    (ai.visible_text: U+202E перевернул бы имя в проводнике), пробелов и точек в конце, не
    длиннее limit (по границе слова, если она не слишком рано); имя устройства Windows (CON,
    NUL, COM1, LPT1… и с расширением: «nul.txt») — с «_» после него; пусто — «meeting»."""
    s = " ".join(_BAD_NAME_RE.sub(" ", ai.visible_text(name or "")).split())
    if len(s) > limit:
        cut = s[:limit + 1]
        sp = cut.rfind(" ")
        s = cut[:sp] if sp >= limit // 2 else s[:limit]
    s = s.rstrip(" .")
    if not s:
        return "meeting"
    stem = s.split(".", 1)[0].rstrip(" ")
    if stem.casefold() in _RESERVED_NAMES:
        s = stem + "_" + s[len(stem):]
    return s


# ---------- название записи без ИИ ----------

# Хвост заголовка окна браузера: «… - Google Chrome», «… – Brave», «… — Яндекс Браузер»;
# заголовок — только имя браузера: о встрече он ничего не говорит
_BROWSER_NAMES = (r"(?:google chrome|chromium|brave(?:\s+browser)?|microsoft\s*edge|"
                  r"mozilla firefox|firefox|opera(?:\s+gx)?|vivaldi|яндекс[\s.]*браузер|"
                  r"yandex(?:\s+browser)?|arc|thorium|librewolf|waterfox|floorp|"
                  r"zen(?:\s+browser)?|comet|maxthon|chatgpt atlas|dia|sidekick|pale moon|"
                  r"internet explorer|chrome|edge)")
_BROWSER_SUFFIX_RE = re.compile(r"\s*[-–—|]\s*" + _BROWSER_NAMES + r"\s*$", re.IGNORECASE)
_BROWSER_ONLY_RE = re.compile(r"^" + _BROWSER_NAMES + r"$", re.IGNORECASE)
_PAREN_FILLER_RE = re.compile(r"\((?:work or school|free|personal|бесплатно)\)", re.IGNORECASE)
# …у Edge перед ним бывают «и ещё 3 страницы» и профиль («- Personal», «- Рабочий»)
_EDGE_PAGES_RE = re.compile(r"\s+(?:and\s+\d+\s+more\s+pages?|и\s+ещё\s+\d+\s+\w+)\s*$",
                            re.IGNORECASE)
_PROFILE_RE = re.compile(r"\s*[-–—]\s*(?:personal|work|profile\s*\d+|личный|рабочий|"
                         r"профиль\s*\d+)\s*$", re.IGNORECASE)
_COUNTER_RE = re.compile(r"^\s*(?:\(\d+\+?\)|\[\d+\]|[●•*])\s*")
_MEET_CODE_RE = re.compile(r"^[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}$")
_PATH_RE = re.compile(r"^[a-z]:[\\/]|^[\\/]{2}|\.exe$", re.IGNORECASE)
_SEPS = " -–—|:·•"
_SEPS_PAREN = _SEPS + "()[]"
# Слова вокруг имени службы, которые сами по себе не название встречи («Zoom Meeting»,
# «Chat | Microsoft Teams», «Видеовстреча — Телемост»)
_SERVICE_FILLER = frozenset({
    "meeting", "meetings", "call", "calls", "chat", "calendar", "activity", "home",
    "workplace", "cloud meetings", "web client", "video call", "meet", "google", "microsoft",
    "work or school", "free", "встреча", "встречи", "видеовстреча", "звонок", "созвон",
    "собрание", "чат", "календарь", "яндекс", "yandex", "контур",
})
# Заголовки, которые ничего не говорят о встрече
_TITLE_JUNK = frozenset({"new tab", "новая вкладка", "untitled", "без названия", "loading",
                         "загрузка", "about:blank", "start page", "начальная страница"})
# Имена программ, которые не годятся в название встречи (описание exe — само имя exe)
_TECH_NAMES = frozenset({"python", "pythonw", "rupor", "rupor-core", "node", "java", "javaw",
                         "cmd", "powershell", "pwsh", "conhost", "electron", "ffmpeg",
                         "audiodg", "svchost", "rundll32", "msedgewebview2",
                         "microsoft edge webview2"})


def _has_letters(text: str) -> bool:
    return bool(re.search(r"[^\W\d_]", text))


def clean_window_title(title: str, patterns: Sequence[str] = ()) -> str:
    """Название встречи по заголовку окна: без хвоста браузера («… - Google Chrome»), профиля
    Edge и счётчика «(3)». Служба встреч — её имя, если кроме него в заголовке только код
    встречи или общие слова («Meet – abc-defg-hij» → «Google Meet», «Zoom Meeting» → «Zoom»),
    иначе — что осталось («Планёрка | Microsoft Teams» → «Планёрка»); служба из
    meeting_title_patterns («Телемост») — так же. Пусто, путь к файлу, «Новая вкладка» — ""."""
    from .meeting_detect import SERVICES, has_word, service_of   # тот модуль берёт BROWSERS отсюда
    # невидимые символы (U+200B у Edge, U+200E…) и обрезанный пополам эмодзи — прочь
    t = " ".join(ai.visible_text(title or "").split())
    for _ in range(4):
        before = t
        t = _BROWSER_SUFFIX_RE.sub("", t)
        if t != before:             # Edge: «<заголовок> and 3 more pages - <профиль> - Edge»
            t = _EDGE_PAGES_RE.sub("", _PROFILE_RE.sub("", t))
        t = _COUNTER_RE.sub("", t)
        if t == before:
            break
    t = t.strip(_SEPS)
    if (not t or _PATH_RE.search(t) or t.casefold() in _TITLE_JUNK or not _has_letters(t)
            or _BROWSER_ONLY_RE.match(t)):
        return ""
    svc, rest = service_of(t), t
    if svc:
        for rx, name in SERVICES:
            if name == svc:
                rest = rx.sub(" ", rest)
    else:
        ft = config.fold_title(t)
        for p in patterns:
            p = (p or "").strip()
            if p and _has_letters(p) and has_word(ft, config.fold_title(p)):
                svc = service_of(p) or p.strip(_SEPS)
                rest = re.sub(re.escape(p), " ", rest, flags=re.IGNORECASE)
                break
    if not svc:
        return t
    rest = " ".join(_PAREN_FILLER_RE.sub(" ", rest).split()).strip(_SEPS_PAREN)
    words = [w.strip(_SEPS_PAREN) for w in re.split(r"[\s|·•,]+", rest.casefold())]
    filler = all(not w or w in _SERVICE_FILLER for w in words) or rest.casefold() in _SERVICE_FILLER
    if (not rest or filler or _MEET_CODE_RE.match(rest.casefold())
            or not _has_letters(rest)):
        return svc
    return rest


def friendly_app_name(source: dict[str, Any]) -> str:
    """Понятное имя программы источника: описание exe («Brave», «Zoom») или имя из таблиц
    браузеров и программ встреч. Имя exe без .exe («Python», «Rupor-core»), служебные
    программы и «весь звук компьютера» — "" (это не название встречи)."""
    if source.get("kind") == "system" or str(source.get("id") or "") == "system":
        return ""
    from .meeting_detect import BROWSERS as ALL_BROWSERS, DESKTOP_APPS, OWN_EXES
    exe = exe_name(str(source.get("exe") or ""))
    if exe in OWN_EXES:
        return ""
    stem = exe[:-4] if exe.endswith(".exe") else exe
    known = ALL_BROWSERS.get(exe) or DESKTOP_APPS.get(exe) or ""
    name = " ".join(str(source.get("name") or "").split())
    low = name.casefold()
    if (name and low not in (stem.casefold(), exe.casefold()) and low not in _TECH_NAMES
            and not _PATH_RE.search(name)):
        return name
    return known


def first_words(text: str, lang: str = "") -> str:
    """Начало строки для названия: TITLE_FIRST_WORDS слов (у языков без пробелов —
    TITLE_FIRST_CHARS символов) без знаков препинания по краям."""
    t = " ".join((text or "").split())
    if lang in ai.NO_SPACE_LANGS:
        t = t[:TITLE_FIRST_CHARS]
    else:
        t = " ".join(t.split(" ")[:TITLE_FIRST_WORDS])
    t = t.strip(" ,.;:!?…—–-\"'«»“”„()[]")
    return t if _has_letters(t) else ""


def fallback_title(source: dict[str, Any] | None, patterns: Sequence[str] = (),
                   first_text: str = "", lang: str = "") -> str:
    """Название записи без ИИ: заголовок окна встречи (clean_window_title), иначе «Встреча —
    <первые слова первой строки>», иначе понятное имя программы (friendly_app_name), иначе
    «Встреча». Имя exe («Python», «rupor-core») — никогда. Не длиннее TITLE_PART_MAX символов
    (по границе слова: заголовок окна бывает длинным, а это «# …» файла и MeetingStatus.title)."""
    src = source or {}
    exe = exe_name(str(src.get("exe") or ""))
    stem = (exe[:-4] if exe.endswith(".exe") else exe).casefold()
    app = friendly_app_name(src)
    t = ("" if src.get("kind") == "system"
         else clean_window_title(str(src.get("title") or ""), patterns))
    if t and (t.casefold() in (stem, exe) or t.casefold() in _TECH_NAMES):
        t = ""
    # заголовок окна — само имя программы («Telegram»): первые слова скажут больше
    if t and t.casefold() != app.casefold():
        return ai.cap_title(t, TITLE_PART_MAX)
    words = first_words(first_text, lang)
    if words:
        return ai.cap_title(tr("meeting.title_words", words=words), TITLE_PART_MAX)
    return ai.cap_title(app or t or tr("meeting.title_generic"), TITLE_PART_MAX)


def line_markdown(line: MeetingLine) -> str:
    """Строка расшифровки: **HH:MM:SS** перевод + «> оригинал» (без перевода — текст)."""
    text = line_source(line)
    if line.translation:
        return f"**{line.ts}** {' '.join(line.translation.split())}\n> {text}\n\n"
    return f"**{line.ts}** {text}\n\n"


def line_plain(line: MeetingLine) -> str:
    """Строка для буфера обмена: время, перевод, под ним оригинал."""
    text = line_source(line)
    if line.translation:
        pad = " " * (len(line.ts) + 1)
        return f"{line.ts} {' '.join(line.translation.split())}\n{pad}{text}"
    return f"{line.ts} {text}"


def notes_body(notes: str, marker: str) -> str:
    """Разделы заметок для файла, окна и буфера обмена: «# …» в начале — прочь (у файла один
    заголовок — название записи), строка ``marker`` («## Полная расшифровка») и всё после неё
    — тоже: переписывание файла приняло бы её за начало расшифровки, а в окне и в «Копировать
    всё» этот раздел был бы дважды."""
    lines = (notes or "").replace("\r\n", "\n").split("\n")
    while lines and not lines[0].strip():
        lines.pop(0)
    if lines and re.match(r"^\s*#(?!#)", lines[0]):
        lines.pop(0)
    stop = " ".join(marker.split()).casefold()
    out: list[str] = []
    for x in lines:
        if " ".join(x.split()).casefold() == stop:
            break
        out.append(x.rstrip())
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip("\n")


_TMP_SEQ = itertools.count(1)


def _tmp_name(folder: Path) -> Path:
    """Временный файл переписывания: в той же папке (os.replace — в пределах тома) и с
    коротким именем — путь к нему короче пути самого файла (тот не короче «YYYY-MM-DD HH-MM —
    x.md»), так что MAX_PATH не мешает временному там, где не мешает файлу."""
    return folder / f".~rp{os.getpid() & 0xFFFF:x}{next(_TMP_SEQ):x}.tmp"


def _fsync(f: Any) -> None:
    f.flush()
    try:
        os.fsync(f.fileno())
    except OSError:
        pass


def _recase(old: Path, target: Path) -> Path:
    """Имя файла другим регистром (заголовок в нём уже новый) → путь. Где регистр
    различается (папка с учётом регистра) и такое имя занято другим файлом или переименовать
    не вышло — прежнее имя: содержимое уже на месте, теряется только регистр."""
    try:
        if target.exists() and not os.path.samefile(old, target):
            return old
        os.rename(old, target)
        return target
    except OSError:
        log.exception("Расшифровка встречи: регистр имени не сменился")
        return old


def _open_text(path: Path, mode: str) -> Any:
    """Файл расшифровки на запись: UTF-8, LF; символ, которого в UTF-8 нет (одиночный
    суррогат из заголовка окна), — «?», а не UnicodeEncodeError (строки и встреча из-за него
    не теряются)."""
    return open(path, mode, encoding="utf-8", errors="replace", newline="\n")


class TranscriptWriter:
    """Файл расшифровки ``<папка>\\YYYY-MM-DD HH-MM — <название>.md``: заголовок
    (``# <название>``, дата и — после «Стоп» — длительность, источник, языки), заметки к
    встрече (если есть), «## Полная расшифровка» и строки — каждая сбрасывается на диск сразу
    (flush + fsync), так что сбой ядра не теряет уже записанное. Файл создаётся с первой
    строкой; имя занято — «… (2)», «… (3)»…

    Начало файла меняется переписыванием (``_rewrite``): ``rename(название)`` — запись
    получила другое название (ИИ), ``set_notes(заметки)`` — заметки вставлены или заменены,
    ``set_minutes(мин)`` — длительность. Всё под тем же замком, что ``write`` (строки, которые
    в это время дописывает поток распознавания или перевода, ждут и попадают уже в новый
    файл): файл сбрасывается и закрывается, новое начало и прежняя расшифровка (со строки
    «## Полная расшифровка» до конца, байт в байт) пишутся во временный файл той же папки с
    коротким именем, тот — на новое имя (``os.replace`` на занятое за собой имя), и только
    потом старый файл удаляется (имя то же или другое только регистром — временный файл на
    место старого, без «(2)»); встреча идёт — новый файл открывается на дозапись. Сбой на
    любом шаге — остаётся старый файл (новый и временный — прочь), строки не теряются.
    Работает и после ``close()``; строка, которая опоздала к ``close()`` (``stop(drain=False)``:
    последний проход фразы или её перевод ещё шли), дописывается в файл, и он снова
    закрывается."""

    def __init__(self, folder: Path, started: datetime, source_label: str, title: str,
                 langs_label: str, heading: str | None = None) -> None:
        self.folder = folder
        self.started = started
        # одной строкой: перевод строки в заголовке окна сбил бы разметку файла
        self.source_label = " ".join((source_label or "").split())
        self.title = " ".join((title or "").split()) or "meeting"
        self.langs_label = " ".join((langs_label or "").split())
        # «Полная расшифровка» — на языке интерфейса при начале встречи: по этой строке
        # переписывание находит начало расшифровки (язык интерфейса потом может смениться)
        self.heading = " ".join((heading or tr("meeting.file_transcript")).split())
        self.minutes: int | None = None     # длительность (после «Стоп»)
        self.notes = ""                     # заметки (Markdown без «# название»)
        self.path: Path | None = None
        self._f: Any = None
        self._closed = False
        self._frozen = False                # выход ядра: переписываний больше нет (freeze)
        self._lock = threading.RLock()

    @property
    def marker(self) -> str:
        """Строка, с которой начинается расшифровка: «## Полная расшифровка»."""
        return f"## {self.heading}"

    def _base(self, title: str) -> str:
        return f"{self.started:%Y-%m-%d %H-%M}{FILE_SEP}{safe_file_part(title)}"

    def _reserve(self, title: str) -> tuple[Path, Any]:
        """Свободное имя для названия («… (2)», «… (3)»…): файл создаётся сразу (режим "x"),
        так что это имя уже никто не займёт → (путь, открытый файл)."""
        self.folder.mkdir(parents=True, exist_ok=True)
        base = self._base(title)
        for n in range(1, RENAME_TRIES + 1):
            path = self.folder / (f"{base}.md" if n == 1 else f"{base} ({n}).md")
            try:
                return path, _open_text(path, "x")
            except FileExistsError:
                continue
        raise FileExistsError(str(self.folder / f"{base}.md"))

    def _head(self, title: str) -> str:
        """Начало файла до расшифровки: «# название», дата (и длительность), источник, языки,
        заметки."""
        date, at = f"{self.started:%Y-%m-%d}", f"{self.started:%H:%M}"
        when = (tr("meeting.file_date_duration", date=date, time=at, minutes=self.minutes)
                if self.minutes is not None else tr("meeting.file_date", date=date, time=at))
        head = (f"# {title}\n\n- {when}\n"
                f"- {tr('meeting.file_source', name=self.source_label)}\n"
                f"- {self.langs_label}\n\n")
        return head + (f"{self.notes}\n\n" if self.notes else "")

    def _header(self, title: str) -> str:
        """Новый файл: начало и строка «## Полная расшифровка» (дальше — строки)."""
        return f"{self._head(title)}{self.marker}\n\n"

    def _body_of(self, content: str) -> str:
        """Расшифровка из содержимого файла: со строки «## Полная расшифровка» до конца. Её нет
        (файл правили руками) — всё прежнее содержимое под этой строкой: строки не теряются."""
        i = content.find(f"\n{self.marker}\n")
        if i >= 0:
            return content[i + 1:]
        if content.startswith(f"{self.marker}\n"):
            return content
        log.warning("Расшифровка встречи: нет строки начала расшифровки — файл целиком под ней")
        return f"{self.marker}\n\n{content}"

    def _open(self) -> None:
        path, f = self._reserve(self.title)
        try:
            f.write(self._header(self.title))
            _fsync(f)
        except BaseException:
            # заголовок не записался — занятое имя (пустой файл) не оставлять: следующая
            # строка попробует снова, и рядом копились бы пустые «… (2)», «… (3)»
            try:
                f.close()
            finally:
                try:
                    path.unlink(missing_ok=True)
                except OSError:
                    log.exception("Расшифровка встречи: пустой файл не удалился")
            raise
        self._f, self.path = f, path

    def write(self, line: MeetingLine) -> None:
        with self._lock:
            if self._f is None:
                if self.path is None:
                    self._open()
                else:               # закрыт (опоздавшая строка) или переписывание не открыло
                    self._f = _open_text(self.path, "a")
            f = self._f
            assert f is not None
            try:
                f.write(line_markdown(line))
                _fsync(f)
            finally:
                if self._closed:
                    # строка опоздала: встреча уже закрыта — дописана, файл снова закрыт
                    self._f = None
                    try:
                        f.close()
                    except OSError:
                        log.exception("Расшифровка встречи: файл не закрылся")

    def rename(self, title: str) -> bool:
        """Другое название записи (см. описание класса). Файла ещё нет — он создастся уже с
        этим названием. → True — название сменилось (и имя файла, если он есть)."""
        title = " ".join((title or "").split())
        with self._lock:
            if not title or title == self.title:
                return False
            if self.path is None:
                self.title = title
                return True
            if self._frozen:
                return False                # выход ядра: файл уже не трогать
            return self._rewrite(title)

    def set_notes(self, notes: str) -> bool:
        """Заметки между заголовком и расшифровкой (вместо прежних; "" — убрать). «# …» в
        начале заметок — прочь (у файла один заголовок — название записи), строка «## Полная
        расшифровка» в них — конец заметок (иначе переписывание приняло бы её за начало
        расшифровки). Файла ещё нет — он создастся уже с ними. → False — файл не переписался
        (остался прежним, с прежними заметками) или выход ядра."""
        body = self._clean_notes(notes)
        with self._lock:
            if self._frozen:
                return False
            old = self.notes
            if body == old:
                return True
            self.notes = body
            if self.path is None or self._rewrite(self.title):
                return True
            self.notes = old
            return False

    def set_minutes(self, minutes: int) -> bool:
        """Длительность встречи в строке даты (после «Стоп»). → False — файл не переписался."""
        with self._lock:
            if self._frozen:
                return False
            old = self.minutes
            if minutes == old:
                return True
            self.minutes = minutes
            if self.path is None or self._rewrite(self.title):
                return True
            self.minutes = old
            return False

    def _clean_notes(self, notes: str) -> str:
        return notes_body(notes, self.marker)

    def _rewrite(self, title: str) -> bool:
        """Переписать файл (под замком, файл есть): новое начало (_head: название,
        длительность, заметки) и прежняя расшифровка; название другое — и имя файла."""
        old = self.path
        assert old is not None
        f, self._f = self._f, None
        if f is not None:
            try:
                _fsync(f)
                f.close()
            except OSError:
                log.exception("Расшифровка встречи: файл не закрылся перед переписыванием")
        new: Path | None = None
        tmp: Path | None = None
        placed = False
        ok = False
        try:
            # как есть (newline=""): расшифровка — байт в байт
            with open(old, encoding="utf-8", newline="") as rf:
                content = self._head(title) + self._body_of(rf.read())
            base_old, base_new = self._base(self.title), self._base(title)
            recase: Path | None = None
            if base_new.casefold() == base_old.casefold():
                # имя то же (разница — в запрещённых символах) или только регистр: на месте,
                # а не «… (2)» (на Windows имя другим регистром — это занятый собой файл)
                new = old
                if base_new != base_old and old.name.startswith(base_old):
                    recase = old.with_name(base_new + old.name[len(base_old):])
            else:
                new, ph = self._reserve(title)
                placed = True
                ph.close()
            tmp = _tmp_name(new.parent)
            with _open_text(tmp, "w") as tf:
                tf.write(content)
                _fsync(tf)
            os.replace(tmp, new)
            tmp = None
            if new != old:
                os.remove(old)          # только когда новый файл уже на месте
            elif recase is not None:
                new = _recase(old, recase)
            self.path, self.title = new, title
            ok = True
        except Exception:
            log.exception("Расшифровка встречи: не переписалась — остаётся прежний файл")
            for p in (tmp, new if placed and new != old else None):
                if p is not None:
                    try:
                        p.unlink(missing_ok=True)
                    except OSError:
                        log.exception("Расшифровка встречи: лишний файл не удалился")
        finally:
            if not self._closed:
                try:
                    self._f = _open_text(self.path or old, "a")
                except OSError:
                    # следующая строка (write) попробует открыть файл снова
                    log.exception("Расшифровка встречи: файл не открылся на дозапись")
                    self._f = None
        return ok

    def freeze(self) -> None:
        """Выход ядра (процесс сейчас кончится, os._exit): идущее переписывание —
        дождаться (тот же замок), новых не будет — оборванное посередине оставило бы рядом
        пустой или второй файл. Строки дописываются как прежде."""
        with self._lock:
            self._frozen = True

    def close(self) -> None:
        with self._lock:
            self._closed = True
            f, self._f = self._f, None
            if f is not None:
                try:
                    f.close()
                except OSError:
                    log.exception("Расшифровка встречи: файл не закрылся")


# ---------- сеанс ----------

class BackgroundRecognizer(Protocol):
    @property
    def loaded(self) -> bool: ...
    def transcribe_background(self, audio: np.ndarray, *, language: str | None,
                              prompt: str | None, yield_to: Callable[[], bool] | None,
                              timeout: float | None = None, beam_size: int = 1,
                              temperature: float | tuple[float, ...] = BG_TEMPERATURES
                              ) -> ChunkResult | None: ...


class StreamLike(Protocol):
    """Захват звука (loopback.LoopbackStream или фейк в тестах)."""
    def start(self, target: str) -> None: ...
    def stop(self) -> object: ...
    @property
    def alive(self) -> bool: ...


AudioFn = Callable[[np.ndarray], None]
ExitFn = Callable[[int, str], None]
StreamFactory = Callable[[AudioFn, ExitFn], StreamLike]


@dataclass
class TrRequest:
    """Вызов перевода встречи: кусок фразы потоком (продолжение уже показанного перевода
    ``done_tr`` её начала ``done_src``; ``on_delta`` — показуемый текст куска по мере ответа)
    или вся фраза разом (``refine`` — уточнение после конца фразы, без потока)."""
    text: str
    src: str
    dst: str
    context: list[str]              # прежние строки (исходный текст) — для справки
    done_src: str = ""
    done_tr: str = ""
    refine: bool = False
    on_delta: Callable[[str], None] | None = None


TranslateFn = Callable[[TrRequest], ai.AIResult]
# Название записи по содержанию: (расшифровка, язык названия) → ответ ИИ (ai.meeting_title)
TitleFn = Callable[[str, str], ai.AIResult]


@dataclass
class NotesRequest:
    """Вызов заметок к встрече (ai.meeting_notes): расшифровка строками «HH:MM:SS текст»
    (перевод, а без него — оригинал), язык заметок, сведения о встрече для <meeting>,
    название записи («# …», если модель названия не дала; title_ai — оно уже по содержанию),
    показ начала заметок по ходу и in_progress — встреча ещё идёт (заметки по кнопке)."""
    transcript: str
    lang: str
    meta: dict[str, str]
    title: str
    on_delta: Callable[[str], None] | None = None
    title_ai: bool = False
    in_progress: bool = False


NotesFn = Callable[[NotesRequest], ai.AIResult]


class AutoStop:
    """Автоостановка: чистые переходы, время (monotonic) — снаружи. Сигнал (``signal``):
    "mic" — программа встречи отпустила микрофон и речи нет, "silence" — цифровая тишина
    (только у источника без записи микрофона: у программы с записью заглушённый микрофон
    остаётся занятым, а тишина бывает и посреди разговора), "" — нет. ``tick`` → "" (ничего),
    "ending" (отсчёт идёт), "resumed" (отсчёт снят сам: речь, звук или микрофон вернулись),
    "stop" (пора остановить). ``cancel`` — «Не останавливать»: до нового использования
    микрофона или речи (у тишины — до звука) сигнал не действует."""

    def __init__(self) -> None:
        self.deadline: float | None = None      # отсчёт идёт: остановить в это время
        self.reason = ""                        # сигнал отсчёта
        self.cancelled_at: float | None = None  # «Не останавливать» — с тех пор
        self.cancel_reason = ""

    @staticmethod
    def signal(*, mic_tracked: bool, mic_free_s: float | None, quiet_s: float,
               silence_s: float) -> str:
        if mic_tracked:
            free = mic_free_s is not None and mic_free_s >= AUTOSTOP_MIC_OFF_S
            return "mic" if free and quiet_s >= AUTOSTOP_QUIET_S else ""
        return "silence" if silence_s >= AUTOSTOP_SILENCE_S else ""

    def tick(self, now: float, sig: str, *, mic_in_use: bool, quiet_s: float,
             silence_s: float) -> str:
        if self.cancelled_at is not None:
            since = now - self.cancelled_at
            if self.cancel_reason == "silence":
                back = silence_s < since or mic_in_use
            else:
                back = mic_in_use or quiet_s < since
            if not back:
                return ""
            self.cancelled_at, self.cancel_reason = None, ""     # снова следим
        if self.deadline is not None:
            if not sig:
                self.deadline, self.reason = None, ""
                return "resumed"
            return "stop" if now >= self.deadline else "ending"
        if sig:
            self.deadline, self.reason = now + AUTOSTOP_GRACE_S, sig
            return "ending"
        return ""

    def cancel(self, now: float) -> None:
        self.cancelled_at = now
        self.cancel_reason = self.reason or "mic"
        self.deadline, self.reason = None, ""

    def left_s(self, now: float) -> int | None:
        """Секунд до остановки (вверх, не меньше 0) или None — отсчёта нет."""
        if self.deadline is None:
            return None
        return max(0, math.ceil(self.deadline - now))
NoticeFn = Callable[..., None]
EmitFn = Callable[[str, dict[str, Any]], None]


def default_stream(on_audio: AudioFn, on_exit: ExitFn) -> StreamLike:
    """Процесс захвата звука (loopback.LoopbackStream; модуль грузится только здесь)."""
    from . import loopback
    return loopback.LoopbackStream(on_audio, on_exit)


def capture_target(source: dict[str, Any]) -> str:
    """Цель процесса захвата: proc:<pid> → pid:<pid>, system → system."""
    sid = str(source.get("id") or "")
    if sid == "system" or source.get("kind") == "system":
        return "system"
    pid = source.get("pid")
    if isinstance(pid, int) and pid > 0:
        return f"pid:{pid}"
    if sid.startswith("proc:") and sid[5:].isdigit():
        return f"pid:{sid[5:]}"
    raise ValueError(tr("meeting.source_not_found", id=sid))


def source_label(source: dict[str, Any] | None) -> str:
    if not source:
        return ""
    name, title = str(source.get("name") or ""), str(source.get("title") or "")
    return f"{name} — {title}" if title and title != name else name


def idle_status(s: config.Settings, loopback_available: bool = True) -> dict[str, Any]:
    """MeetingStatus, когда встречи ещё не было."""
    return {"running": False, "source": None, "started_at": None, "lines": 0, "level": 0.0,
            "state": "idle", "error": None, "translate": bool(s.meeting_translate),
            "target_lang": s.meeting_target_lang, "file": None, "title": None, "hint": None,
            "ending_in_s": None, "latency": dict(NO_LATENCY), "loopback_available": bool(loopback_available),
            "notes": None}


NO_LATENCY: dict[str, float | None] = {"asr_ms": None, "first_token_ms": None, "done_ms": None}


class MeetingSession:
    """Одна встреча: захват → VAD → потоковое распознавание → перевод кусками → строки,
    события и файл.

    Потоки: чтение звука (процесса захвата, ``on_audio``), ``rupor-meeting-asr`` (проходы
    распознавания), ``rupor-meeting-tr1``/``tr2`` (переводы, не больше двух в полёте),
    ``rupor-meeting-title`` (название записи по содержанию, не больше одного вызова ИИ в
    полёте; ни распознавание, ни перевод, ни ``stop()`` его не ждут).
    Всё состояние строк — под ``self._lock``; события строк уходят по одному
    (``_emit_lock``), чтобы обновления одной строки не обгоняли друг друга.
    ``stop(drain=True)`` (кнопка «Стоп») — захват прочь, недоговорённая фраза и очередь
    дораспознаются (до DRAIN_S), переводы доделываются, файл закрывается; ``drain=False``
    (сбой) — сразу: недоделанные строки пишутся в файл как есть; ``abort()`` (выход ядра) —
    так же и без названия по содержанию. ``detach()`` — сеанс сменился новым: события
    старого больше не рассылаются (номера строк нового начнутся с 1).

    Название записи (``MeetingStatus.title``, имя файла и «# заголовок» в нём): сначала —
    без ИИ (``fallback_title``; с первой строкой — «Встреча — <первые слова>», если заголовка
    окна встречи нет), потом — ``title_fn`` (``meeting_ai_title``): один раз, когда готовых
    непустых строк TITLE_EARLY_LINES и с начала прошло TITLE_EARLY_S (часы — ``clock``), и
    ещё раз после конца (файл закрыт) по всей расшифровке, если строк хотя бы
    TITLE_FINAL_LINES; другое название — файл переименовывается. Нет ключа, ключ отклонён,
    нет денег — до конца встречи ИИ для названия не зовётся; любая неудача — название без
    ИИ остаётся.

    Заметки (``notes_fn``, поток ``rupor-meeting-notes``, одни в полёте): ``request_notes()``
    — по команде; сами — после конца (``_finalize``), если ``meeting_notes``, готовых строк
    хотя бы NOTES_MIN_LINES или речи NOTES_MIN_SPEECH_S и расшифровка изменилась с прошлых
    заметок. Их «# название» — последнее название записи (``meeting_ai_title``): поток
    названия после конца ждёт заметки и зовёт ``title_fn`` сам, только если они названия не
    дали. Неудача — файл не трогается, MeetingNotes.status error, одно уведомление за
    встречу (ключ, деньги — с кнопкой в настройки ИИ)."""

    def __init__(self, source: dict[str, Any], *, settings: config.Settings,
                 rec: BackgroundRecognizer, emit: EmitFn, notice: NoticeFn,
                 translate: TranslateFn | None, yield_to: Callable[[], bool] | None = None,
                 ai_busy: Callable[[], bool] | None = None,
                 stream_factory: StreamFactory | None = None,
                 prob_fn: ProbFn | None = None, translate_ok: bool = True,
                 cpu: bool = False, terms: Sequence[str] = (),
                 on_end: Callable[[MeetingSession], None] | None = None,
                 warm: Callable[[], None] | None = None, interim: bool = True,
                 loopback_available: bool = True, title_fn: TitleFn | None = None,
                 clock: Callable[[], float] | None = None,
                 notes_fn: NotesFn | None = None) -> None:
        self.source = dict(source)
        self._loopback_available = bool(loopback_available)   # MeetingStatus (окну перевода)
        self.s = settings
        self._rec = rec
        self._emit_fn = emit
        self._notice = notice
        self._translate = translate
        self._yield_to = yield_to
        self._ai_busy = ai_busy
        self._stream_factory = stream_factory or default_stream
        self._prob_fn = prob_fn
        self._on_end = on_end
        self._warm = warm
        self._terms = [t for t in terms if isinstance(t, str) and t.strip()]
        self._cpu = cpu
        # на процессоре проход держит модель секунды — только последний проход фразы
        self._interim_wanted = interim
        self._interim = interim and not cpu
        self._vad: StreamVad | None = None
        self._vad_lock = threading.Lock()
        self._stream: StreamLike | None = None
        self._lock = threading.RLock()
        self._trcond = threading.Condition(self._lock)
        self._emit_lock = threading.RLock()
        self._qcond = threading.Condition(threading.Lock())
        self._queue: collections.deque[Utterance] = collections.deque()
        self._lines: collections.deque[MeetingLine] = collections.deque()
        self._by_id: dict[int, MeetingLine] = {}
        self._tr_lines: list[MeetingLine] = []      # строки, у которых ещё есть работа перевода
        self._later: list[MeetingLine] = []         # разослать после замка
        self._live: MeetingLine | None = None       # строка звучащей фразы
        self._discarded: set[int] = set()           # фразы, которые VAD выбросил (щелчки)
        self._pass_seq = -1                         # последний промежуточный проход: фраза,
        self._pass_start = 0.0                      # …когда начат
        self._pass_audio = 0.0                      # …сколько звука взял
        self._pass_tries = 0                        # …проходов без уверенного языка
        self._pass_lang: tuple[str, bool, bool] | None = None   # (язык, выбран, слабо)
        self._pass_langs: list[tuple[str, float]] = []   # …что они определили
        self._final_langs: collections.deque[str] = collections.deque(maxlen=2)
        self._prior_off = -1                        # фраза, которой язык встречи не подошёл
        self._pass_ms: float | None = None          # время прохода (скользящее среднее)
        self._history = ""                          # принятый текст встречи (подсказка)
        self._audio_pos = 0.0                       # секунд звука пришло
        self._audio_mono = 0.0                      # …к этому моменту (monotonic)
        self._lat: collections.deque[tuple[float | None, float | None, float | None]] = (
            collections.deque(maxlen=LATENCY_WINDOW))
        self._next_id = 1
        self._next_write = 1
        self._count = 0
        self._dropped = 0
        self.running = False
        self._closing = threading.Event()     # «Стоп»: звук больше не идёт, дораспознать
        self._abort = threading.Event()       # выход: бросить всё сразу
        self._asr_done = False
        self._tr_alive = 0
        self._detached = False
        self._ended = False
        self._error: str | None = None
        # подсказка окну перевода (meeting_detect): "ended" — встреча, похоже, закончилась
        self.hint: str | None = None
        self._autostop = AutoStop()             # meeting_autostop (под self._lock)
        self._last_speech = 0.0                 # monotonic: последняя речь (VAD) в захвате
        self._waiting = False
        self._waiting_since = 0.0
        self._level = 0.0
        self._last_sound = 0.0
        self._last_emit = 0.0
        self._last_emitted: tuple[str, float] | None = None
        self._translate_ok = translate_ok
        self._tr_notice_sent = not translate_ok    # «нет ключа» уже сказали при старте
        self._tr_fails = 0
        self._tr_paused_until = 0.0
        self._tr_last_error = ""
        self._prev_lang = ""
        self._writer: TranscriptWriter | None = None
        self._writer_failed = False
        self._finalized = False
        self.started_wall = 0.0
        self.started_mono = 0.0
        self.started_at: str | None = None
        self._asr_thread: threading.Thread | None = None
        self._tr_threads: list[threading.Thread] = []
        # название записи (поток rupor-meeting-title; _title_cond — свой замок, внутри него
        # других замков не берут)
        self._title_fn = title_fn
        self._clock: Callable[[], float] = clock or time.monotonic
        self._title_cond = threading.Condition(threading.Lock())
        self._title: str | None = None              # название по содержанию (ИИ)
        self._fallback = ""                         # название без ИИ
        self._named_first = False                   # «первые слова» уже пробовали
        self._title_dirty = False                   # название сменилось — разослать статус
        self._title_t0 = 0.0
        self._title_early = False                   # раннее название уже звали
        self._title_closed = False                  # файл закрыт — пора последнее название
        self._title_off = False                     # нет ключа и т. п. — ИИ больше не звать
        self._title_src = ""                        # расшифровка прошлого вызова
        self._title_calls = 0
        self._final_lines = 0                       # готовых непустых строк
        self._speech_s = 0.0                        # …и секунд речи в них
        self._shutdown = False                      # выход ядра: названия по содержанию нет
        self._title_thread: threading.Thread | None = None
        self.ended_wall = 0.0                       # когда остановлена (длительность)
        # заметки к встрече (поток rupor-meeting-notes; _notes_lock — замок-лист: внутри
        # него других замков не берут и событий не шлют)
        self._notes_fn = notes_fn
        self._notes_lock = threading.Lock()
        self._notes_emit_lock = threading.RLock()   # снимок и отправка meeting_notes
        self._notes: dict[str, Any] | None = None   # MeetingNotes (с markdown)
        self._notes_good: dict[str, Any] | None = None   # последние удачные (MeetingNotes)
        self._notes_body: str | None = None         # …их разделы (без «# название»)
        self._notes_busy = False                    # вызов в полёте
        self._notes_again = False                   # конец встречи, а заметки ещё пишутся
        self._notes_idle = threading.Event()        # заметки не пишутся (поток названия ждёт)
        self._notes_idle.set()
        self._notes_planned = False                 # после конца заметки пошли сами
        self._notes_titled = False                  # …и дали последнее название записи
        self._notes_src = ""                        # расшифровка последних удачных заметок
        self._notes_title_src = ""                  # …тех, чьё «# …» стало названием записи
        self._notes_title_n = 0                     # …сколько раз заметки давали название
        self._notes_notice_sent = False
        self._notes_last_emit = 0.0
        self._notes_thread: threading.Thread | None = None
        self.max_s = VAD_MAX_S_CPU if cpu else VAD_MAX_S
        self.merge_max_s = MERGE_MAX_S_CPU if cpu else MERGE_MAX_S

    # ---------- запуск и остановка ----------

    def start(self) -> None:
        """Запустить захват и потоки. Не запустился захват — ValueError с текстом."""
        target = capture_target(self.source)
        self.started_wall = time.time()
        self.started_mono = time.monotonic()
        self._last_sound = self._last_speech = self.started_mono
        started = datetime.fromtimestamp(self.started_wall)
        # с миллисекундами: окно перевода отличает сеансы по started_at (два старта подряд
        # в одну секунду — разные сеансы, строки не смешиваются)
        self.started_at = started.astimezone().isoformat(timespec="milliseconds")
        self._title_t0 = self._clock()
        self._fallback = fallback_title(self.source, self.s.meeting_title_patterns)
        if self.s.meeting_save:
            self._writer = TranscriptWriter(
                config.meeting_dir(self.s), started, source_label(self.source),
                self._fallback, self._langs_label())
        self._vad = StreamVad(self._prob_fn, max_s=self.max_s, on_discard=self._discard_seq)
        self.running = True
        self._asr_thread = threading.Thread(target=self._asr_loop, name="rupor-meeting-asr",
                                            daemon=True)
        self._tr_alive = TR_MAX_INFLIGHT
        self._tr_threads = [threading.Thread(target=self._tr_loop, name=f"rupor-meeting-tr{i}",
                                             daemon=True) for i in range(1, TR_MAX_INFLIGHT + 1)]
        self._title_thread = threading.Thread(target=self._title_loop,
                                              name="rupor-meeting-title", daemon=True)
        self._asr_thread.start()
        for th in self._tr_threads:
            th.start()
        self._title_thread.start()
        try:
            stream = self._stream_factory(self._on_audio, self._on_exit)
            self._stream = stream
            stream.start(target)
        except Exception as e:
            log.exception("Захват звука встречи не запустился")
            self.running = False
            self._abort.set()
            self._wake()
            raise ValueError(tr("meeting.capture_failed", name=self._name(),
                                reason=_reason(e))) from None
        log.info("Встреча: захват %s (%s), перевод %s → %s, потоково: %s", target,
                 exe_name(str(self.source.get("exe") or "")) or "system",
                 self.s.meeting_source_lang,
                 self.s.meeting_target_lang if self.translating else "нет",
                 "да" if self._interim else "нет")
        self._warm_tr()
        self._emit_status(force=True)

    def stop(self, drain: bool = True) -> None:
        """Остановить встречу (повторно — ничего не делает)."""
        with self._lock:
            if self._ended:
                return
            self._ended = True
            self.running = False
            self._waiting = False
            self.ended_wall = time.time()
        self._stop_stream()
        if drain:
            vad = self._vad
            if vad is not None:
                with self._vad_lock:
                    rest = vad.flush()
                    with self._qcond:
                        self._queue.extend(rest)
            self._closing.set()
        else:
            self._abort.set()
            self._finalize()
        self._wake()
        log.info("Встреча остановлена: строк %d, выброшено кусков %d", self._count,
                 self._dropped)
        self._emit_status(force=True)
        self._ended_cb()

    def detach(self) -> None:
        """Сеанс заменён новым: события больше не рассылать (файл дописывается)."""
        self._detached = True

    def abort(self) -> None:
        """Выход ядра: идущую встречу — stop(drain=False); остановленную, которая ещё
        дораспознаёт, — бросить сразу, дописав готовое в файл. Названия по содержанию уже
        не будет (вызов в полёте доживёт сам, но файл не переименует; переименование, которое
        уже идёт, — дождаться: TranscriptWriter.freeze)."""
        self._shutdown = True
        self._notes_idle.set()      # поток названия больше не ждёт заметок (их в файл не будет)
        self._title_kick()
        w = self._writer
        if w is not None:
            w.freeze()          # переименование в полёте доделается, новое не начнётся
        if self.running:
            self.stop(drain=False)
            return
        self._abort.set()
        self._wake()
        self._finalize()

    def _stop_stream(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            try:
                stream.stop()
            except Exception:
                log.exception("Захват звука встречи не остановился")

    def _ended_cb(self) -> None:
        cb = self._on_end
        if cb is not None:
            try:
                cb(self)
            except Exception:
                log.exception("Конец встречи: обработчик упал")

    def _wake(self) -> None:
        with self._qcond:
            self._qcond.notify_all()
        with self._lock:
            self._trcond.notify_all()

    def _fail(self, error: str, level: str = "error") -> None:
        """Встреча не может продолжаться: состояние error и уведомление."""
        with self._lock:
            if self._ended:
                return
            self._error = error
        log.warning("Встреча остановлена с ошибкой: %s", error)
        self._notice(error, level=level)
        self.stop(drain=False)

    def _on_exit(self, code: int, reason: str) -> None:
        """Процесс захвата завершился сам. Колбэк приходит из потока захвата, а остановка
        ждёт этот поток — поэтому она в своём потоке."""
        if self._ended:
            return
        threading.Thread(target=self._exited, args=(code, reason), name="rupor-meeting-end",
                         daemon=True).start()

    def _exited(self, code: int, reason: str) -> None:
        if self._ended:
            return
        if code == 3:
            log.info("Встреча: источник звука закрылся")
            self._notice(tr("meeting.source_closed", name=self._name()), level="info",
                         toast=True)
            self.stop(drain=True)
            return
        why = reason or tr("meeting.helper_exit", code=code)
        key = "meeting.capture_failed" if code == 2 else "meeting.capture_stopped"
        self._fail(tr(key, name=self._name(), reason=why))

    # ---------- состояние ----------

    @property
    def translating(self) -> bool:
        return bool(self.s.meeting_translate) and self._translate_ok

    @property
    def file(self) -> Path | None:
        w = self._writer
        return w.path if w is not None else None

    @property
    def title(self) -> str | None:
        """Название записи: по содержанию (ИИ), иначе без ИИ; сеанс не начат — None."""
        return self._title or self._fallback or None

    @property
    def busy(self) -> bool:
        """Сеанс ещё не кончился совсем: идёт, дораспознаёт (файл не закрыт), зовёт
        название по содержанию или пишет заметки (файл ещё может переписаться)."""
        th = self._title_thread
        return (not self._finalized or (th is not None and th.is_alive())
                or self._notes_busy)

    def _state(self) -> str:
        if self._error:
            return "error"
        if not self.running:
            return "idle"
        if self._waiting:
            return "waiting_gpu"
        if time.monotonic() - self._last_sound >= NO_AUDIO_S:
            return "no_audio"
        return "listening"

    def latency(self) -> dict[str, float | None]:
        """Средние задержки последних строк, мс: конец речи → текст на экране (asr_ms), →
        первый токен перевода (first_token_ms), → перевод готов (done_ms); нет — None."""
        with self._lock:
            rows = list(self._lat)
        out: dict[str, float | None] = {}
        for i, key in enumerate(("asr_ms", "first_token_ms", "done_ms")):
            xs = [v for r in rows if (v := r[i]) is not None]
            out[key] = float(round(sum(xs) / len(xs))) if xs else None
        return out

    def status(self) -> dict[str, Any]:
        lat = self.latency()
        notes = self._notes_brief()
        with self._lock:
            f = self.file
            return {"running": self.running, "source": dict(self.source),
                    "started_at": self.started_at, "lines": self._count,
                    "level": round(self._level, 3) if self.running else 0.0,
                    "state": self._state(), "error": self._error,
                    "translate": self.translating, "target_lang": self.s.meeting_target_lang,
                    "file": str(f) if f is not None else None,
                    "title": self.title,
                    "hint": self._hint() if self.running else None,
                    "ending_in_s": (self._autostop.left_s(time.monotonic())
                                    if self.running else None),
                    "latency": lat, "loopback_available": self._loopback_available,
                    "notes": notes}

    def _hint(self) -> str | None:
        """MeetingStatus.hint (под self._lock): отсчёт автоостановки — "ending", иначе
        подсказка meeting_detect ("ended" или None)."""
        return HINT_ENDING if self._autostop.deadline is not None else self.hint

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        if self._detached:
            return
        try:
            self._emit_fn(event, data)
        except Exception:
            log.exception("Событие встречи %s не ушло", event)

    def _emit_status(self, force: bool = False) -> None:
        """Событие meeting: смена состояния — сразу, уровень звука — не чаще LEVEL_EVERY_S и
        только если заметно изменился."""
        now = time.monotonic()
        key = (self._state(), round(self._level, 3) if self.running else 0.0)
        if not force:
            last = self._last_emitted
            if last is not None and last[0] == key[0]:
                if now - self._last_emit < LEVEL_EVERY_S or abs(last[1] - key[1]) < LEVEL_STEP:
                    return
        self._last_emit, self._last_emitted = now, key
        self._emit("meeting", self.status())

    def _emit_line(self, line: MeetingLine, force: bool = False) -> None:
        """Событие meeting_line (обновление строки по id): не чаще LINE_EMIT_EVERY_S, кроме
        force (новый текст распознавания, конец куска перевода). Звать без self._lock."""
        if self._detached:
            return
        with self._emit_lock:
            with self._lock:
                now = time.monotonic()
                if not force and now - line.last_emit < LINE_EMIT_EVERY_S:
                    return
                line.last_emit = now
                data = line.to_dict()
            self._emit("meeting_line", data)

    def _emit_later(self) -> None:
        with self._lock:
            later, self._later = self._later, []
            dirty, self._title_dirty = self._title_dirty, False
        for line in dict.fromkeys(later):
            self._emit_line(line, force=True)
        if dirty:
            self._emit_status(force=True)         # название без ИИ — по первой строке

    def set_hint(self, hint: str | None) -> None:
        """Подсказка окну перевода (движок, meeting_detect): "ended" — микрофон программы
        встречи давно свободен и звука нет; None — снять. Остановка — не здесь, а
        автоостановкой (autostop_tick)."""
        with self._lock:
            if hint == self.hint:
                return
            self.hint = hint
        self._emit_status(force=True)

    # ---------- автоостановка ----------

    def autostop_tick(self, *, mic_tracked: bool, mic_free_s: float | None,
                      mic_in_use: bool, now: float | None = None) -> str:
        """Раз в 2 с (движок): запись микрофона программы встречи (mic_tracked — она есть и
        микрофон в сеансе был занят; mic_free_s — сколько он свободен; mic_in_use — занят
        сейчас), речь и звук захвата → AutoStop. Отсчёт начался или снят — статус; пора —
        «Стоп» (как кнопкой) и уведомление. meeting_autostop выключено — отсчёт снимается.
        → действие AutoStop.tick ("" | ending | resumed | stop)."""
        if not self.running:
            return ""
        now = time.monotonic() if now is None else now
        on = bool(getattr(self.s, "meeting_autostop", True))
        with self._lock:
            st = self._autostop
            if not on:
                was = st.deadline is not None
                st.deadline, st.reason = None, ""
                act = "resumed" if was else ""
            else:
                quiet, silence = now - self._last_speech, now - self._last_sound
                sig = AutoStop.signal(mic_tracked=mic_tracked, mic_free_s=mic_free_s,
                                      quiet_s=quiet, silence_s=silence)
                started = st.deadline is None
                act = st.tick(now, sig, mic_in_use=mic_in_use, quiet_s=quiet,
                              silence_s=silence)
                if act == "ending" and started:
                    log.info("Встреча, похоже, кончилась (%s) — остановка через %.0f с",
                             "микрофон свободен, речи нет" if sig == "mic" else "тишина",
                             AUTOSTOP_GRACE_S)
                elif act == "resumed":
                    log.info("Автоостановка встречи снята: речь или микрофон вернулись")
        if act == "stop":
            self._auto_stop()
        elif act == "resumed" or (act == "ending" and started):
            self._emit_status(force=True)
        return act

    def autostop_cancel(self) -> None:
        """«Не останавливать» (WS meeting_autostop_cancel): отсчёт снять; автоостановка — не
        раньше, чем микрофон возьмут снова или вернётся речь."""
        with self._lock:
            self._autostop.cancel(time.monotonic())
        log.info("Автоостановка встречи отменена пользователем")
        self._emit_status(force=True)

    def _auto_stop(self) -> None:
        """Отсчёт кончился: «Стоп» (дораспознать, название, заметки) и уведомление."""
        notes = (self._notes_fn is not None and bool(getattr(self.s, "meeting_notes", True))
                 and (self._final_lines >= NOTES_MIN_LINES
                      or self._speech_s >= NOTES_MIN_SPEECH_S))
        saved = self.file is not None and not self._writer_failed
        with self._lock:
            self._autostop.deadline = None
        log.info("Встреча кончилась — автоостановка")
        self.stop(drain=True)
        key = ("meeting.autostopped_notes" if saved and notes
               else "meeting.autostopped_saved" if saved else "meeting.autostopped")
        self._notice(tr(key), level="info", toast=True)

    def settings_changed(self) -> None:
        """Сменились настройки (язык перевода, перевод вкл/выкл) — разослать статус."""
        self._wake()
        self._emit_status(force=True)

    def set_translate_available(self, ok: bool) -> None:
        """Ключ ИИ появился (или провайдер сменился): снова переводить; ok=False — нет.
        Ключ есть — и название по содержанию снова можно звать (после «нет ключа»)."""
        with self._lock:
            if ok:
                self._title_off = False
            if ok == self._translate_ok:
                return
            self._translate_ok = ok
            if ok:
                self._tr_notice_sent = False
                self._tr_fails = 0
                self._tr_paused_until = 0.0
            self._trcond.notify_all()
        log.info("Встреча: перевод %s", "снова включён" if ok else "выключен")
        if ok:
            self._warm_tr()
        self._emit_status(force=True)

    def _warm_tr(self) -> None:
        """Соединение с провайдером перевода — заранее (в начале встречи и каждой фразы)."""
        fn = self._warm
        if fn is None or not self.translating:
            return
        try:
            fn()
        except Exception:
            log.debug("Соединение перевода заранее не открылось", exc_info=True)

    # ---------- звук ----------

    def _on_audio(self, chunk: np.ndarray) -> None:
        """Поток чтения процесса захвата: уровень, VAD, готовые фразы — в очередь."""
        if not self.running:
            return
        a = np.asarray(chunk, np.float32).reshape(-1)
        if not a.size:
            return
        now = time.monotonic()
        peak = float(np.max(np.abs(a)))
        if peak >= SILENCE_PEAK:
            self._last_sound = now
        rms = float(np.sqrt(np.mean(a * a)))
        lvl = min(1.0, max(0.0, (20 * math.log10(max(rms, 1e-6)) + 60) / 60))
        # быстро вверх, медленно вниз — индикатор «звук есть», а не осциллограф
        self._level = lvl if lvl >= self._level else self._level * 0.8 + lvl * 0.2
        vad = self._vad
        if vad is not None:
            started = False
            try:
                # под одним замком с очередью: кто видит новую фразу (snapshot), тот видит и
                # прежнюю в очереди — строки не перепутаются местами
                with self._vad_lock:
                    seq0, speech0 = vad.seq, vad.in_speech
                    utts = vad.feed(a) if self.running else []
                    self._audio_pos += len(a) / SAMPLE_RATE
                    self._audio_mono = now
                    started = vad.in_speech and (not speech0 or vad.seq != seq0)
                    if vad.in_speech or utts:
                        self._last_speech = now       # автоостановка: речь ещё идёт
                    for u in utts:
                        self._enqueue(u)
            except Exception:
                log.exception("VAD встречи упал — кусок звука пропущен")
            if started:
                with self._qcond:
                    self._qcond.notify_all()      # первый проход фразы — не опоздать
                self._warm_tr()
        self._emit_status()

    def _discard_seq(self, seq: int) -> None:
        """VAD выбросил фразу (щелчок) — её строку (если уже появилась) убрать (под _vad_lock)."""
        self._discarded.add(seq)

    def _arrival(self, pos_s: float) -> float:
        """Когда пришёл звук на секунде pos_s потока (monotonic)."""
        if not self._audio_mono:
            return time.monotonic()
        return self._audio_mono - max(0.0, self._audio_pos - pos_s)

    def _enqueue(self, u: Utterance) -> None:
        with self._qcond:
            self._queue.append(u)
            total = sum(x.dur for x in self._queue)
            dropped = 0
            while total > QUEUE_MAX_S and len(self._queue) > 1:
                total -= self._queue.popleft().dur
                dropped += 1
            self._qcond.notify_all()
        if dropped:
            self._dropped += dropped
            log.warning("Встреча: очередь распознавания переполнена — выброшено фраз %d",
                        dropped)

    def _take(self) -> Utterance | None:
        """Готовая фраза из очереди (не ждёт). Ждут больше QUEUE_MERGE_AFTER — склеить в
        один кусок (кроме фразы, у которой уже есть строка: её текст обновляется)."""
        live = self._live
        live_seq = live.seq if live is not None else None
        with self._qcond:
            if not self._queue:
                return None
            if len(self._queue) <= QUEUE_MERGE_AFTER or self._queue[0].seq == live_seq:
                return self._queue.popleft()
            parts = [self._queue.popleft()]
            dur = parts[0].dur
            while (self._queue and self._queue[0].seq != live_seq
                   and dur + MERGE_GAP_S + self._queue[0].dur <= self.merge_max_s):
                u = self._queue.popleft()
                parts.append(u)
                dur += MERGE_GAP_S + u.dur
        if len(parts) > 1:
            log.info("Встреча: очередь — склеено фраз %d в кусок %.1f с", len(parts), dur)
        return merge_utterances(parts)

    def _put_back(self, u: Utterance) -> None:
        with self._qcond:
            self._queue.appendleft(u)

    # ---------- распознавание ----------

    def _forced_lang(self) -> str | None:
        v = self.s.meeting_source_lang
        return None if not v or v == "auto" else v

    def _prompt(self, lang: str | None) -> str | None:
        """Подсказка Whisper самой встречи: термины и хвост принятого текста встречи (до
        PROMPT_PREV_CHARS; после мусора — без него). Для языков не на кириллице — без
        кириллицы (adapt_prompt)."""
        parts: list[str] = []
        terms = self._terms[:PROMPT_TERMS_MAX]
        if terms:
            parts.append(", ".join(terms) + ".")
        prev = self._history
        if prev:
            if len(prev) > PROMPT_PREV_CHARS:
                prev = prev[-PROMPT_PREV_CHARS:]
                prev = prev[prev.find(" ") + 1:] if " " in prev else prev
            parts.append(prev)
        prompt = " ".join(parts).strip() or None
        lang = self._forced_lang() or lang or self._prev_lang
        if prompt and lang not in CYRILLIC_LANGS:
            prompt = adapt_prompt(prompt, "en")
        return prompt

    def _set_waiting(self, on: bool) -> None:
        with self._lock:
            if self._waiting == on:
                return
            self._waiting = on and self.running
        self._emit_status(force=True)

    def _gpu_wait(self) -> None:
        """Проход не взял модель (диктовка, загрузка) — дольше WAIT_GPU_AFTER_S: waiting_gpu."""
        now = time.monotonic()
        if not self._waiting_since:
            self._waiting_since = now
        elif now - self._waiting_since >= WAIT_GPU_AFTER_S:
            self._set_waiting(True)
        if not self._rec.loaded:
            self._abort.wait(0.2)            # модель не загружена — не крутиться

    def _gpu_ok(self) -> None:
        self._waiting_since = 0.0
        self._set_waiting(False)

    def _asr_loop(self) -> None:
        """Сначала — готовые фразы (последний проход, по порядку), потом — промежуточный
        проход звучащей фразы, когда он положен."""
        errors = 0
        deadline: float | None = None
        try:
            while not self._abort.is_set():
                self._sync_device()
                if self._closing.is_set():
                    if deadline is None:
                        deadline = time.monotonic() + DRAIN_S
                    elif time.monotonic() > deadline:
                        with self._qcond:
                            left = len(self._queue)
                        if left:
                            log.warning("Встреча: не успел дораспознать фраз %d", left)
                        break
                self._check_discarded()
                u = self._take()
                snap: tuple[int, float, np.ndarray] | None = None
                wait = 0.3
                if u is None:
                    if self._closing.is_set():
                        break
                    snap, wait = self._interim_due()
                    if snap is None:
                        with self._qcond:
                            if not self._queue and not self._abort.is_set():
                                self._qcond.wait(max(0.005, min(wait, 0.3)))
                        continue
                try:
                    if u is not None:
                        ok = self._final_pass(u)
                    else:
                        assert snap is not None
                        ok = self._interim_pass(*snap)
                except Exception as e:
                    errors += 1
                    log.exception("Встреча: распознавание упало (%d подряд)", errors)
                    if u is not None:
                        self._abandon(u)
                    if errors >= ASR_ERRORS_MAX:
                        self._fail(tr("meeting.asr_failed", error=_reason(e)))
                        break
                    continue
                if ok:
                    errors = 0
        except Exception:
            log.exception("Поток распознавания встречи упал")
        finally:
            try:
                self._close_live()
            finally:
                with self._lock:
                    self._asr_done = True
                    self._trcond.notify_all()

    def _sync_device(self) -> None:
        """Устройство модели известно (загрузилась после старта встречи, откатилась с GPU на
        CPU, сменилось в настройках) — режим сеанса по нему, а не по догадке при старте: на
        CPU без промежуточных проходов, фразы и склейки короче, уведомление «будет
        отставать». Модель не загружена (device None) — как есть."""
        dev = getattr(self._rec, "device", None)
        if not isinstance(dev, str) or (dev == "cpu") == self._cpu:
            return
        cpu = dev == "cpu"
        self._cpu = cpu
        self._interim = self._interim_wanted and not cpu
        self.max_s = VAD_MAX_S_CPU if cpu else VAD_MAX_S
        self.merge_max_s = MERGE_MAX_S_CPU if cpu else MERGE_MAX_S
        with self._vad_lock:
            if self._vad is not None:
                self._vad.set_max_s(self.max_s)
        log.info("Встреча: модель на %s — %s", dev,
                 "без промежуточных проходов" if cpu else "потоковое распознавание")
        if cpu and self.running:
            self._notice(tr("meeting.cpu_slow"), level="warn")

    def _abandon(self, u: Utterance) -> None:
        """Последний проход фразы упал (сбой на GPU): фраза пропала, а её строку (принятое
        промежуточными проходами) закрыть как есть, пустую — убрать. Иначе строка осталась бы
        звучащей навсегда: у следующих фраз ни промежуточных проходов, ни перевода кусками,
        а файл не дописывался бы дальше неё."""
        self._dropped += 1
        with self._lock:
            line = self._live
            if line is None or u.seq < 0 or line.seq != u.seq:
                return
            if line.words or line.tail:
                self._close_line(line)
            else:
                self._drop_line(line)
        self._emit_line(line, force=True)
        self._emit_status(force=True)
        self._emit_later()

    def _check_discarded(self) -> None:
        with self._vad_lock:
            gone, self._discarded = self._discarded, set()
        if not gone:
            return
        with self._lock:
            line = self._live
            if line is None or line.seq not in gone:
                return
            if line.words:                       # что-то уже принято — оставить как есть
                self._close_line(line)
            else:
                self._drop_line(line)
        self._emit_line(line, force=True)
        self._emit_status(force=True)

    def _interim_due(self) -> tuple[tuple[int, float, np.ndarray] | None, float]:
        """Звучащая фраза, если ей положен промежуточный проход, и сколько ждать иначе."""
        vad = self._vad
        if not self._interim or not self.running or vad is None:
            return None, 0.3
        with self._vad_lock:
            if not vad.in_speech:
                return None, 0.3
            seq, dur = vad.seq, vad.speech_s
        if dur < INTERIM_MIN_S:
            return None, INTERIM_MIN_S - dur + 0.005
        if dur > INTERIM_MAX_S:
            return None, 0.3
        live = self._live
        if live is not None and live.seq != seq:
            return None, 0.02                    # прошлая фраза ещё не дораспознана
        if self._pass_seq == seq and self._pass_start:
            step = MEETING_STEP_S
            if self._pass_ms is not None:
                step = max(step, STEP_DUTY * self._pass_ms / 1000)
            left = self._pass_start + step - time.monotonic()
            if left > 0:
                return None, left
            fresh = dur - self._pass_audio
            if fresh < INTERIM_NEW_S:
                return None, INTERIM_NEW_S - fresh
        with self._vad_lock:
            with self._qcond:
                if self._queue:
                    return None, 0.0             # сначала — готовая фраза
            snap = vad.snapshot()
        if snap is None or snap[0] != seq:
            return None, 0.02
        return snap, 0.0

    def _interim_pass(self, seq: int, start_s: float, audio: np.ndarray) -> bool:
        """Промежуточный проход звучащей фразы (с её начала): LocalAgreement-2 — совпавшее
        с прошлой гипотезой начало принимается, остальное — хвост."""
        dur = len(audio) / SAMPLE_RATE
        if self._pass_seq != seq:
            self._pass_seq, self._pass_tries, self._pass_lang = seq, 0, None
            self._pass_langs = []
        self._pass_start, self._pass_audio = time.monotonic(), dur
        with self._lock:
            live = self._live if self._live is not None and self._live.seq == seq else None
        lang = self._forced_lang()
        weak = False
        if lang is None and live is not None and live.lang_ok:
            lang, weak = live.lang, live.lang_weak
        if lang is None and self._pass_lang is not None and self._pass_lang[1]:
            lang, weak = self._pass_lang[0], self._pass_lang[2]
        if lang is None and self._prior_off != seq and self._pass_tries > 0:
            # язык встречи — только после прохода с определением: оно было неуверенным
            prior = self._session_lang()
            if prior:
                lang, weak = prior, True         # язык встречи; последний проход проверит
        res = self._rec.transcribe_background(
            audio, language=lang, prompt=self._prompt(lang), yield_to=self._yield_to,
            timeout=INTERIM_GATE_TIMEOUT_S, beam_size=INTERIM_BEAM, temperature=0.0)
        if res is None:
            self._pass_start = 0.0               # модель освободится — сразу свежий звук
            self._gpu_wait()
            return False
        self._gpu_ok()
        ms = float(res.elapsed_ms)
        self._pass_ms = ms if self._pass_ms is None else 0.7 * self._pass_ms + 0.3 * ms
        shown = time.monotonic()
        text = res.text.strip()
        # Язык прохода задан (язык встречи, язык фразы), а текст — другой письменностью:
        # говорят на другом языке (русская фраза посреди английской встречи). Язык фразы —
        # заново: следующий проход определит его сам, принятое и начатый перевод — прочь.
        guess = (script_mismatch(text, lang) if lang is not None and self._forced_lang() is None
                 else None)
        if guess is not None:
            log.info("Встреча: проход с языком %s написал текст не той письменностью — язык "
                     "фразы определяю заново", lang)
            self._prior_off = seq
            self._pass_tries, self._pass_langs = 0, []
            plang, fixed, weak = guess, False, False
        elif lang is not None:
            plang, fixed = lang, True
        else:
            plang, fixed, weak = self._pass_language(res.language, res.language_prob)
        self._pass_lang = None if guess is not None else (plang, fixed, weak)
        words = split_words(text, plang) if text else []
        if not words:
            return True                          # мусор или тишина — ждать следующего
        with self._lock:
            if self._live is not None and self._live.seq != seq:
                return True
            line = self._live
            created = line is None
            if line is None:
                line = self._new_line(start_s, plang, seq)
                self._live = line
            if guess is not None and line.lang_ok:
                line.lang_ok = line.lang_weak = False
                line.words, line.tail = [], []   # приняты не на том языке
                self._reset_tr(line)
            if not line.lang_ok:
                line.lang, line.lang_ok, line.lang_weak = plang, fixed, weak
            hyp = [norm_word(w) for w in words]
            i = skip_done([norm_word(w) for w in line.words], hyp)
            tail = words[i:]
            k = agree([norm_word(w) for w in line.tail], hyp[i:])
            commit = tail[:k]
            if commit and not line.lang_ok:
                commit, k = [], 0               # язык не ясен — только хвост
            if commit and garbage_reason(join_words(commit, line.lang), line.lang):
                log.info("Встреча: не принимаю %d слов (мусор)", len(commit))
                commit, k = [], 0
            if commit:
                line.words.extend(commit)
                if not line.tr.since:
                    line.tr.since = shown
            line.tail = tail[k:]
            line.text = join_words(line.words, line.lang)
            line.partial = join_words(line.tail, line.lang)
            line.passes.append((start_s + dur, shown))
            self._tr_setup(line)
            self._update_status(line)
            if commit or not line.tail:
                self._trcond.notify_all()
        self._emit_line(line, force=True)
        if created:
            self._emit_status(force=True)
        return True

    def _pass_language(self, detected: str, prob: float) -> tuple[str, bool, bool]:
        """Язык звучащей фразы по определению промежуточного прохода: (язык, выбран ли,
        «слабо» ли). Выбран уверенно — вероятность ≥ LANG_FIX_PROB; LANG_TRIES проходов без
        уверенности — последний определённый (звука больше всего), слабо."""
        self._pass_tries += 1
        if detected:
            self._pass_langs.append((detected, prob))
        if detected and prob >= LANG_FIX_PROB:
            return detected, True, False
        if self._pass_tries >= LANG_TRIES:
            last = self._pass_langs[-1][0] if self._pass_langs else ""
            return last or self._prev_lang or "en", True, True
        return detected or self._prev_lang or "en", False, False

    def _session_lang(self) -> str | None:
        """Язык встречи: две последние строки — на одном языке."""
        langs = list(self._final_langs)
        return langs[0] if len(langs) == 2 and langs[0] == langs[1] else None

    def _final_pass(self, u: Utterance) -> bool:
        """Последний проход готовой фразы (beam 5, повторы с температурой): его текст —
        текст строки; строки ещё не было (короткая фраза, очередь) — новая. Язык фразы
        выбран уверенно — с ним; не выбран или выбран слабо — определить по всей фразе
        (неуверенно и тут — слабый выбор строки, иначе язык прошлой строки)."""
        with self._lock:
            live = (self._live if self._live is not None and u.seq >= 0
                    and self._live.seq == u.seq else None)
        lang = self._forced_lang()
        if lang is None and live is not None and live.lang_ok and not live.lang_weak:
            lang = live.lang
        res = self._rec.transcribe_background(
            u.audio, language=lang, prompt=self._prompt(lang), yield_to=self._yield_to,
            timeout=ASR_GATE_TIMEOUT_S, beam_size=FINAL_BEAM, temperature=BG_TEMPERATURES)
        if res is None:
            self._put_back(u)
            self._gpu_wait()
            return False
        self._gpu_ok()
        now = time.monotonic()
        text = res.text.strip()
        if lang is not None:
            flang = lang
        else:
            flang = res.language or self._prev_lang or "en"
            if res.language_prob < LANG_MIN_PROB:
                if live is not None and live.lang_ok:
                    flang = live.lang
                elif self._prev_lang:
                    flang = self._prev_lang
        with self._lock:
            if not text:
                self._history = ""                # мусор мог отравить подсказку
            if not text and (live is None or not live.words):
                self._dropped += 1
                if live is None:
                    return True
                self._drop_line(live)
                line = live
            else:
                line = live if live is not None else self._new_line(u.start_s, flang, u.seq)
                if text:
                    if line.lang != flang:
                        # язык оказался другим: перевод кусков (с чужого языка) — заново, в
                        # том числе кусок, который ещё переводится (его ответ не нужен)
                        self._reset_tr(line)
                    line.lang, line.lang_ok, line.lang_weak = flang, True, False
                    line.words = split_words(text, flang)
                    if self._forced_lang() is None:
                        self._final_langs.append(flang)
                line.tail = []
                line.end_s = u.speech_end_s if u.speech_end_s is not None else u.end_s
                line.end_mono = self._arrival(line.end_s)
                line.shown_end = next((t for e, t in line.passes
                                       if e >= line.end_s - END_SLACK_S), now)
                self._close_line(line)
                if text:
                    self._history = (self._history + " " + line.text).strip()[-HISTORY_KEEP:]
                self._prev_lang = line.lang
        self._emit_line(line, force=True)
        self._emit_status(force=True)
        self._emit_later()
        return True

    def _new_line(self, start_s: float, lang: str, seq: int) -> MeetingLine:
        """Новая строка (под self._lock)."""
        wall = datetime.fromtimestamp(self.started_wall + start_s)
        line = MeetingLine(self._next_id, start_s, f"{wall:%H:%M:%S}", lang, "", None,
                           "pending", created=time.monotonic(), seq=seq)
        self._next_id += 1
        self._count += 1
        self._lines.append(line)
        self._by_id[line.id] = line
        self._tr_lines.append(line)
        while len(self._lines) > LINES_MAX:
            old = self._lines.popleft()
            self._by_id.pop(old.id, None)
        return line

    def _close_line(self, line: MeetingLine) -> None:
        """Фраза кончилась (под self._lock): хвост — в принятое, строка final."""
        if line.tail:
            line.words.extend(line.tail)
            line.tail = []
        line.text = join_words(line.words, line.lang)
        line.partial = ""
        line.final = True
        line.lang_ok = True
        if self._live is line:
            self._live = None
        self._tr_setup(line)
        self._update_status(line)
        if not line.tr.on:
            self._settle(line)
        self._trcond.notify_all()
        if line.text and not line.dropped:
            self._final_lines += 1
            if line.end_s is not None:
                self._speech_s += max(0.0, line.end_s - line.t)
            if self._final_lines >= TITLE_EARLY_LINES:
                self._title_kick()                # _title_cond — замок-лист: здесь можно

    def _drop_line(self, line: MeetingLine) -> None:
        """Фраза оказалась мусором, а строка уже показана (под self._lock): пустая final —
        окну перевода её не показывать; в meeting_lines и файл она не попадает."""
        line.dropped = True
        line.words, line.tail = [], []
        line.text = line.partial = ""
        line.translation = None
        line.final = True
        line.tr.on = False
        if self._live is line:
            self._live = None
        self._count -= 1
        self._update_status(line)
        self._settle(line)

    def _close_live(self) -> None:
        """Распознавание кончилось («Стоп» дольше DRAIN_S, сбой): звучащую строку закрыть
        тем, что есть."""
        with self._lock:
            line = self._live
            if line is None:
                return
            if line.words or line.tail:
                self._close_line(line)
            else:
                self._drop_line(line)
        self._emit_line(line, force=True)

    # ---------- перевод ----------

    def _wants(self, line: MeetingLine) -> bool:
        return (self.translating and self._translate is not None and line.lang_ok
                and line.lang != self.s.meeting_target_lang and not self._abort.is_set())

    def _may_translate(self, line: MeetingLine) -> bool:
        return (self.translating and self._translate is not None
                and line.lang != self.s.meeting_target_lang)

    def _tr_setup(self, line: MeetingLine) -> None:
        """Язык строки стал известен и её надо переводить — перевод включается (под замком)."""
        st = line.tr
        if (not st.on and not st.settled and st.failed is None and not line.dropped
                and self._wants(line)):
            st.on = True

    def _reset_tr(self, line: MeetingLine) -> None:
        """Язык строки сменился — её перевод (с прежнего языка) заново (под замком). Вызов в
        полёте держит прежний LineTr: его ответ строке больше не показывается (_run_chunk и
        _tr_error сверяют line.tr)."""
        st = line.tr
        if st.on or st.busy or st.calls or st.failed is not None or line.translation:
            line.tr = LineTr()
            line.translation = None
            line.refined = False

    def _untranslated(self, line: MeetingLine) -> tuple[int, list[str]]:
        """Принятые слова, которых ещё нет в переводе: (с какого слова, слова)."""
        i = skip_done(line.tr.src, [norm_word(w) for w in line.words])
        return i, line.words[i:]

    def _update_status(self, line: MeetingLine) -> None:
        """status/error строки по её состоянию (под self._lock)."""
        st = line.tr
        err = None
        if line.dropped:
            status = "original"
        elif st.failed is not None:
            status, err = "error", st.failed
        elif st.on:
            complete = line.final and not st.busy and not self._untranslated(line)[1]
            if complete or st.settled:
                status = "done" if (complete or line.translation) else "original"
            else:
                status = "streaming" if line.translation else "pending"
        elif not line.final and not line.lang_ok and self._may_translate(line):
            status = "pending"
        else:
            status = "original"
        line.status, line.error = status, err

    def _settle(self, line: MeetingLine) -> None:
        """Работа перевода строки закончена (под self._lock): статус, задержки, файл."""
        st = line.tr
        if st.settled:
            return
        st.settled = True
        if line in self._tr_lines:
            self._tr_lines.remove(line)
        self._update_status(line)
        self._record_latency(line)
        self._flush_ready()
        self._later.append(line)

    def _record_latency(self, line: MeetingLine) -> None:
        if line.lat_done or line.dropped or line.end_mono is None:
            return
        line.lat_done = True
        end = line.end_mono
        asr_ms = max(0.0, (line.shown_end - end) * 1000) if line.shown_end else None
        first = done = None
        st = line.tr
        if (st.on and st.failed is None and st.last_done is not None
                and not self._untranslated(line)[1]):
            done = max(0.0, (st.last_done - end) * 1000)
            if st.last_first is not None:
                first = max(0.0, (st.last_first - end) * 1000)
        self._lat.append((asr_ms, first, done))
        log.info("Встреча: строка %d (%s, слов %d): текст через %s мс после конца речи, "
                 "перевод — первый токен %s мс, готов %s мс (кусков %d, проходов %d)",
                 line.id, line.lang, len(line.words), _ms(asr_ms), _ms(first), _ms(done),
                 st.calls, len(line.passes))

    def _context_for(self, line: MeetingLine) -> list[str]:
        """Исходный текст прежних строк (для справки модели перевода; под self._lock)."""
        ctx: list[str] = []
        i = line.id - 1
        while i > 0 and len(ctx) < ai.TRANSLATE_CONTEXT_MAX:
            prev = self._by_id.get(i)
            if prev is None:
                break
            if not prev.dropped and prev.text:
                ctx.append(prev.text)
            i -= 1
        return list(reversed(ctx))

    def _chunk_due(self, line: MeetingLine, rest: list[str],
                   now: float) -> tuple[bool, float | None]:
        """Пора ли отдать непереведённые принятые слова в перевод (и сколько ждать иначе):
        конец фразы, граница предложения или части, пауза (гипотеза принята целиком),
        TR_CHUNK_WORDS слов или TR_CHUNK_WAIT_S ожидания."""
        if line.final:
            return True, None
        if not line.lang_ok:
            return False, None
        last = rest[-1].rstrip("\"'»”)]")
        if (len(rest) >= TR_CHUNK_WORDS or last.endswith(SENT_END) or not line.tail
                or (len(rest) >= TR_CLAUSE_MIN_WORDS and last.endswith(CLAUSE_END))):
            return True, None
        since = line.tr.since
        if since:
            age = now - since
            if age >= TR_CHUNK_WAIT_S:
                return True, None
            return False, TR_CHUNK_WAIT_S - age
        return False, None

    def _refine_needed(self, line: MeetingLine) -> bool:
        """Перевести фразу ещё раз целиком: включено, перевод был, и либо последний проход
        поправил переведённый текст, либо кусков было больше одного у строки длиннее
        REFINE_SKIP_WORDS слов."""
        if not getattr(self.s, "meeting_refine", True):
            return False
        st = line.tr
        if not st.calls or not line.words:
            return False
        if st.src != [norm_word(w) for w in line.words]:
            return True
        return st.calls >= 2 and len(line.words) > REFINE_SKIP_WORDS

    def _pick_job(self) -> tuple[tuple[str, MeetingLine, list[str]] | None, float | None, bool]:
        """Следующий вызов перевода (под self._lock): куски — раньше уточнений, строки — по
        порядку. → (работа или None, сколько ждать (None — до события), есть ли ещё работа)."""
        now = time.monotonic()
        wait: float | None = None
        more = False
        refine: MeetingLine | None = None

        def soon(w: float | None) -> None:
            nonlocal wait
            if w is not None:
                wait = w if wait is None else min(wait, w)

        for line in list(self._tr_lines):
            st = line.tr
            if st.settled:
                if line in self._tr_lines:
                    self._tr_lines.remove(line)
                continue
            if st.busy:
                more = True
                continue
            if line.dropped:
                self._settle(line)
                continue
            self._tr_setup(line)
            if not (st.on and st.failed is None and self.translating):
                if line.final:
                    self._settle(line)
                else:
                    more = True
                continue
            _i, rest = self._untranslated(line)
            if now < self._tr_paused_until:
                # пауза после ошибок подряд: ошибка — только строке, которой перевода не
                # хватает; переведённая целиком готова как есть (без уточнения всей фразой)
                if line.final and not rest:
                    if st.refine != "done":
                        st.refine = "skip"
                    self._settle(line)
                elif line.final:
                    st.failed = tr("meeting.translate_paused", error=self._tr_last_error)
                    self._settle(line)
                else:
                    more = True
                    soon(self._tr_paused_until - now)
                continue
            if rest:
                more = True
                if now < st.retry_at:
                    soon(st.retry_at - now)
                    continue
                due, w = self._chunk_due(line, rest, now)
                if due:
                    st.busy, st.since = True, 0.0
                    return ("chunk", line, list(rest)), 0.0, True
                soon(w)
                continue
            if not line.final:
                more = True
                continue
            if not st.refine:
                st.refine = "wait" if self._refine_needed(line) else "skip"
            if st.refine == "wait":
                more = True
                if refine is None:
                    refine = line
                continue
            self._settle(line)
        if refine is not None:
            refine.tr.busy, refine.tr.refine = True, "run"
            return ("refine", refine, []), 0.0, True
        return None, wait, more

    def _tr_loop(self) -> None:
        try:
            while not self._abort.is_set():
                with self._lock:
                    job, wait, more = self._pick_job()
                    later = bool(self._later)
                    if job is None and not later:
                        if self._asr_done and not more:
                            return
                        self._trcond.wait(0.5 if wait is None else max(0.01, min(wait, 0.5)))
                        continue
                if later:
                    self._emit_later()
                if job is None:
                    continue
                kind, line, words = job
                try:
                    if kind == "chunk":
                        self._run_chunk(line, words)
                    else:
                        self._run_refine(line)
                except Exception:
                    log.exception("Перевод строки встречи упал")
                    with self._lock:
                        line.tr.busy = False
                        line.tr.failed = line.tr.failed or tr("meeting.translate_failed")
                        self._update_status(line)
                    self._emit_line(line, force=True)
        except Exception:
            log.exception("Поток перевода встречи упал")
        finally:
            with self._lock:
                self._tr_alive -= 1
                last = self._tr_alive <= 0
                self._trcond.notify_all()
            if last:
                self._finalize()

    def _wait_ai_busy(self) -> None:
        busy = self._ai_busy
        if busy is None:
            return
        until = time.monotonic() + TR_AI_WAIT_S   # ИИ-очистка диктовки — первая (до 1 с)
        while busy() and time.monotonic() < until and not self._abort.is_set():
            time.sleep(0.05)

    def _call_tr(self, req: TrRequest) -> ai.AIResult:
        """Вызов перевода; 429/5xx — один повтор сразу, если кусок ждёт меньше 2 с."""
        fn = self._translate
        if fn is None:
            raise ai.AIError(tr("meeting.translate_failed"), "config")
        t0 = time.monotonic()
        retried = False
        while True:
            try:
                return fn(req)
            except ai.AIError as e:
                if (e.kind in TR_RETRY_KINDS and not retried and not self._abort.is_set()
                        and time.monotonic() - t0 < TR_RETRY_MAX_AGE_S):
                    retried = True
                    continue
                raise

    def _run_chunk(self, line: MeetingLine, words: list[str]) -> None:
        """Кусок фразы — потоком, продолжением уже показанного перевода."""
        st = line.tr
        with self._lock:
            lang, target = line.lang, self.s.meeting_target_lang
            text = join_words(words, lang)
            done_src, done_tr = " ".join(st.parts), st.shown
            ctx = self._context_for(line)
        self._wait_ai_busy()
        first: list[float] = []

        def on_delta(part: str) -> None:
            with self._lock:
                fresh = not first
                if fresh:
                    first.append(time.monotonic())
                if st.failed is not None or line.dropped or line.tr is not st:
                    return
                line.translation = ai.join_translation(done_tr, part, target)
                self._update_status(line)
            self._emit_line(line, force=fresh)   # первые слова куска — сразу, дальше ≤ 10/с

        req = TrRequest(text, lang, target, ctx, done_src, done_tr, False, on_delta)
        try:
            r = self._call_tr(req)
        except Exception as e:
            self._tr_error(line, e, refine=False, st=st)
            return
        out = (r.text or "").strip()
        now = time.monotonic()
        with self._lock:
            st.busy = False
            if line.tr is not st:
                # язык строки сменился, пока кусок переводился (_reset_tr): ответ не нужен
                self._trcond.notify_all()
                return
            st.calls += 1
            st.fails = 0
            self._tr_fails = 0
            if out and r.provider != "none":
                st.shown = ai.join_translation(st.shown, out, target)
            st.parts.append(text)
            st.src.extend(norm_word(w) for w in words)
            st.last_first = first[0] if first else now
            st.last_done = now
            line.translation = st.shown or None
            self._update_status(line)
            self._trcond.notify_all()
        self._emit_line(line, force=True)

    def _run_refine(self, line: MeetingLine) -> None:
        """Фраза кончилась — перевести её целиком и заменить кусочный перевод (refined)."""
        st = line.tr
        with self._lock:
            req = TrRequest(line.text, line.lang, self.s.meeting_target_lang,
                            self._context_for(line), refine=True)
        self._wait_ai_busy()
        try:
            r = self._call_tr(req)
        except Exception as e:
            self._tr_error(line, e, refine=True, st=st)
            return
        out = (r.text or "").strip()
        with self._lock:
            st.busy = False
            st.refine = "done"
            if line.tr is not st:
                self._trcond.notify_all()
                return
            if out and r.provider != "none":
                line.translation = out
                line.refined = True
            self._update_status(line)
            self._trcond.notify_all()
        self._emit_line(line, force=True)

    def _tr_error(self, line: MeetingLine, e: BaseException, refine: bool,
                  st: LineTr | None = None) -> None:
        """Вызов перевода не удался. ``st`` — LineTr, с которым шёл вызов (язык строки мог
        смениться, пока он шёл: тогда строке уже ничего не нужно, кроме «нет ключа»)."""
        msg = str(e).strip() if isinstance(e, ai.AIError) else _reason(e)
        kind = e.kind if isinstance(e, ai.AIError) else ""
        if not isinstance(e, ai.AIError):
            log.error("Перевод строки встречи упал: %s", msg, exc_info=e)
        if st is None:
            st = line.tr
        with self._lock:
            st.busy = False
            stale = line.tr is not st
            if refine:
                st.refine = "skip"              # кусочный перевод остаётся
            elif not stale:
                line.translation = st.shown or None
        if stale:
            if kind in TR_STOP_KINDS:
                self._translation_stopped(e, None)
            with self._lock:
                self._trcond.notify_all()
            return
        if kind in TR_STOP_KINDS:
            self._translation_stopped(e, None if refine else line)
        elif refine:
            log.info("Встреча: перевод фразы целиком не удался (%s) — остаётся по кускам",
                     kind or msg)
        else:
            self._tr_failed(msg)
            with self._lock:
                st.fails += 1
                if st.fails >= TR_LINE_FAILS:
                    st.failed = msg
                else:
                    st.retry_at = time.monotonic() + TR_RETRY_S
        with self._lock:
            self._update_status(line)
            self._trcond.notify_all()
        self._emit_line(line, force=True)
        self._emit_later()

    def _tr_failed(self, error: str) -> None:
        with self._lock:
            self._tr_fails += 1
            self._tr_last_error = error
            if self._tr_fails >= TR_FAILS_PAUSE:
                self._tr_fails = 0
                self._tr_paused_until = time.monotonic() + TR_PAUSE_S
                log.warning("Встреча: перевод на паузе %.0f с после ошибок подряд", TR_PAUSE_S)

    def _translation_stopped(self, e: BaseException, line: MeetingLine | None) -> None:
        """Нет ключа / ключ отклонён / нет денег / доступ запрещён — до конца встречи только
        расшифровка, одно уведомление с кнопкой в настройки ИИ."""
        kind = e.kind if isinstance(e, ai.AIError) else ""
        with self._lock:
            self._translate_ok = False
            send = not self._tr_notice_sent
            self._tr_notice_sent = True
            if line is not None:
                line.tr.failed = str(e)
            for other in list(self._tr_lines):
                st = other.tr
                if other is line or st.settled or st.busy:
                    continue
                if st.on and st.shown and st.failed is None:
                    st.failed = str(e)          # начало переведено, конец — уже нет
                else:
                    st.on = False
                self._update_status(other)
                self._later.append(other)
            self._trcond.notify_all()
        log.warning("Встреча: перевод выключен до конца встречи (%s)", kind)
        if send:
            self._notice(tr("meeting.translate_stopped", error=str(e).rstrip(" .")),
                         level="warn", action="open_settings_ai")
        self._emit_status(force=True)

    # ---------- файл ----------

    def _langs_label(self) -> str:
        src = self.s.meeting_source_lang
        src_label = tr("meeting.lang_auto") if src == "auto" else src
        target = self.s.meeting_target_lang if self.translating else tr(
            "meeting.file_no_translation")
        return tr("meeting.file_langs", source=src_label, target=target)

    def _flush_ready(self) -> None:
        """Дописать в файл готовые строки — по порядку номеров (звать под self._lock).
        Готова — фраза кончилась и перевод (с уточнением) закончен; выход — все как есть."""
        w = self._writer
        while self._next_write < self._next_id:
            line = self._by_id.get(self._next_write)
            if (line is not None and not line.dropped and not line.tr.settled
                    and not self._finalized):
                break
            self._next_write += 1
            if line is None or line.dropped or not line_source(line):
                continue
            if not self._named_first:
                self._name_from_first(line, w)
            if w is None or self._writer_failed:
                continue
            try:
                w.write(line)
            except OSError as e:
                self._writer_failed = True
                log.exception("Расшифровка встречи: файл не записался")
                self._notice(tr("meeting.file_failed", error=e.strerror or _reason(e)),
                             level="warn")

    def _finalize(self) -> None:
        """Конец: недоделанные строки — в файл как есть, файл закрыть, длительность — в
        заголовок; заметки — сами (если положены); поток названия — последнее название по
        всей расшифровке (или из заметок)."""
        with self._lock:
            if self._finalized:
                return
            self._finalized = True
            self._flush_ready()
            if self._writer is not None:
                self._writer.close()
            dirty, self._title_dirty = self._title_dirty, False
        w = self._writer
        if w is not None and not self._writer_failed and not self._shutdown:
            w.set_minutes(self._minutes())
        f = self.file
        if f is not None:
            log.info("Расшифровка встречи сохранена: %d строк", self._count)
        # заметки — до того, как поток названия узнает о конце: он их подождёт
        if self._notes_auto_due():
            self._notes_planned = True
            self._start_notes(auto=True, again=True)
        elif self._notes_title_src and self._notes_title_src == self._notes_input()[0]:
            # заметки по кнопке уже написаны по всей расшифровке, и их «# …» — название
            # записи: оно и последнее, отдельный вызов названия его бы только заменил
            self._notes_titled = True
        with self._title_cond:
            self._title_closed = True
            self._title_cond.notify_all()
        if dirty:
            self._emit_status(force=True)

    def _minutes(self) -> int:
        """Длительность встречи, минут (не меньше 1): до «Стоп» или до сейчас."""
        end = self.ended_wall or time.time()
        return max(1, round((end - self.started_wall) / 60))

    # ---------- название записи ----------

    def _name_from_first(self, line: MeetingLine, w: TranscriptWriter | None) -> None:
        """Первая строка расшифровки (под self._lock, до её записи): название без ИИ — с её
        первыми словами, если заголовка окна встречи нет (файл создастся уже с ним)."""
        self._named_first = True
        if self._title is not None:
            return
        text = line.translation or line_source(line)
        lang = self.s.meeting_target_lang if line.translation else line.lang
        name = fallback_title(self.source, self.s.meeting_title_patterns, text, lang)
        if name == self._fallback:
            return
        self._fallback = name
        self._title_dirty = True
        if w is not None and w.path is None:
            w.rename(name)                      # файла ещё нет: только название

    def _title_kick(self) -> None:
        with self._title_cond:
            self._title_cond.notify_all()

    def _title_wanted(self) -> bool:
        return (self._title_fn is not None and bool(getattr(self.s, "meeting_ai_title", True))
                and not self._title_off and not self._shutdown)

    def _title_next(self) -> str | None:
        """Следующее название по содержанию: "early" (TITLE_EARLY_LINES готовых строк и
        TITLE_EARLY_S от начала), "final" (файл закрыт, строк хотя бы TITLE_FINAL_LINES) или
        None — больше не будет (поток кончается). Ждёт, пока не пора."""
        with self._title_cond:
            while True:
                if self._shutdown:
                    return None
                if self._title_closed:
                    ok = self._title_wanted() and self._final_lines >= TITLE_FINAL_LINES
                    return "final" if ok else None
                wait = TITLE_WAIT_S
                if not self._title_early and self.running and self._title_wanted():
                    if self._final_lines >= TITLE_EARLY_LINES:
                        left = self._title_t0 + TITLE_EARLY_S - self._clock()
                        if left <= 0:
                            self._title_early = True
                            return "early"
                        wait = min(wait, left)
                self._title_cond.wait(max(0.01, wait))

    def _title_loop(self) -> None:
        try:
            while True:
                job = self._title_next()
                if job is None:
                    return
                if job == "final":
                    # заметки после конца встречи: их «# название» и есть последнее название —
                    # отдельный вызов, только если они его не дали (не удались, не название)
                    if self._notes_planned:
                        self._notes_idle.wait()
                    if self._notes_titled or self._shutdown:
                        return
                self._run_title(job == "final")
                if job == "final":
                    # раннее название не дошло до файла (переименование не удалось), а
                    # последнее — то же или ИИ не ответил: переименовать ещё раз
                    if self._title is not None:
                        self._set_title(self._title)
                    return
        except Exception:
            log.exception("Название встречи: поток упал")

    def _title_input(self) -> tuple[str, str]:
        """Расшифровка для названия (строка — перевод, а без него — оригинал) и язык
        названия: язык перевода, если встреча переводится, иначе язык большинства строк."""
        with self._lock:
            rows = [x for x in self._lines if x.final and not x.dropped and line_source(x)]
            text = "\n".join(" ".join((x.translation or line_source(x)).split()) for x in rows)
            if self.translating:
                lang = self.s.meeting_target_lang
            else:
                counts = collections.Counter(x.lang for x in rows if x.lang)
                lang = counts.most_common(1)[0][0] if counts else self.s.meeting_target_lang
        return text, lang

    def _run_title(self, final: bool) -> None:
        """Один вызов ИИ за название (поток rupor-meeting-title); другое название — файл
        переименовывается. Неудача — остаётся прежнее (в журнал, без уведомления)."""
        fn = self._title_fn
        text, lang = self._title_input()
        if fn is None or not text:
            return
        if final and text == self._title_src:
            return                              # с раннего названия строк не прибавилось
        self._wait_ai_busy()                    # ИИ-очистка диктовки — первая (до 1 с)
        if self._shutdown:
            return
        self._title_calls += 1
        by_notes = self._notes_title_n
        try:
            r = fn(text, lang)
        except Exception as e:
            kind = e.kind if isinstance(e, ai.AIError) else type(e).__name__
            if kind in TR_STOP_KINDS:
                self._title_off = True
            log.info("Название встречи: ИИ не помог (%s) — остаётся «%s»", kind,
                     "своё" if self._title else "без ИИ")
            return
        self._title_src = text                  # ответ на эту расшифровку уже есть
        title = ai.clean_title(r.text)
        if not title:
            log.info("Название встречи: ответ ИИ — не название, остаётся прежнее")
            return
        if self._notes_title_n != by_notes:
            # пока ИИ отвечал, название дали заметки (модель посильнее) — оно и остаётся
            log.info("Название встречи: его уже дали заметки — ответ ИИ не нужен")
            return
        self._set_title(title)

    def _set_title(self, title: str) -> None:
        """Название по содержанию → статус и файл. Название то же, а файл называется иначе
        (прошлое переименование не удалось: файл держала другая программа) — ещё раз."""
        if self._shutdown:
            return
        with self._lock:
            changed = title != self._title
            self._title = title
            w = self._writer
        if (w is not None and not self._writer_failed
                and w.title != " ".join(title.split())):
            if w.rename(title):
                log.info("Расшифровка встречи переименована по содержанию")
                changed = True
        if changed:
            self._emit_status(force=True)
            if self._notes_body is not None and not self._notes_busy:
                self._emit_notes()              # «# название» заметок — новое название записи

    # ---------- заметки к встрече ----------

    def notes(self) -> dict[str, Any] | None:
        """MeetingNotes или None — заметок ещё не было. markdown: пока пишутся — начало
        ответа модели (или None), потом — последние удачные заметки с «# <название записи>»
        (название могло смениться и после них — файл и заметки называются одинаково)."""
        with self._notes_lock:
            if self._notes is None:
                return None
            n = dict(self._notes)
            body = self._notes_body
        if n.get("status") != "running":
            n["markdown"] = self._notes_markdown(body)
        return n

    def _notes_markdown(self, body: str | None) -> str | None:
        if body is None:
            return None
        title = self.title or ""
        return f"# {title}\n\n{body}" if body else f"# {title}"

    def _notes_brief(self) -> dict[str, Any] | None:
        """MeetingStatus.notes: MeetingNotes без markdown (события meeting частые)."""
        n = self.notes()
        if n is not None:
            n.pop("markdown", None)
        return n

    def notes_text(self) -> str:
        """Последние удачные заметки (Markdown) — для буфера обмена; нет — ""."""
        with self._notes_lock:
            body = self._notes_body
        return self._notes_markdown(body) or ""

    def request_notes(self) -> dict[str, Any]:
        """Заметки сейчас (WS meeting_notes) — во время встречи и после неё. Уже пишутся —
        ничего нового (одни в полёте). Расшифровка пуста — ValueError. → MeetingNotes."""
        if self._notes_fn is None or not self._notes_input()[0]:
            raise ValueError(tr("meeting.notes_empty"))
        self._start_notes(auto=False)
        n = self.notes()
        assert n is not None
        return n

    def _notes_auto_due(self) -> bool:
        """Заметки сами после конца: включены, не выход ядра, готовых строк NOTES_MIN_LINES
        или речи NOTES_MIN_SPEECH_S, и расшифровка не та, по которой они уже написаны."""
        if (self._notes_fn is None or self._shutdown
                or not bool(getattr(self.s, "meeting_notes", True))):
            return False
        if self._final_lines < NOTES_MIN_LINES and self._speech_s < NOTES_MIN_SPEECH_S:
            return False
        text = self._notes_input()[0]
        return bool(text) and text != self._notes_src

    def _notes_input(self) -> tuple[str, collections.Counter[str]]:
        """Расшифровка для заметок: готовые строки «HH:MM:SS текст» (как у названия: живая
        фраза, которая ещё распознаётся, — нет; выброшенные — нет) и языки речи строк. Текст —
        перевод, если он законченный (_notes_text), иначе оригинал."""
        with self._lock:
            rows = [x for x in self._lines if x.final and not x.dropped and line_source(x)]
            text = "\n".join(f"{x.ts} {' '.join(self._notes_text(x).split())}" for x in rows)
            langs = collections.Counter(x.lang for x in rows if x.lang)
        return text, langs

    def _notes_text(self, line: MeetingLine) -> str:
        """Текст готовой строки для заметок (под self._lock): перевод — если он всей фразы
        (уточнён целиком или все куски переведены и вызовов в полёте нет); перевод ещё
        идёт, оборван или не удался — оригинал (полуперевод исказил бы заметки)."""
        if line.translation and (line.refined or (
                line.status == "done" and not line.tr.busy
                and not self._untranslated(line)[1])):
            return line.translation
        return line_source(line)

    def _notes_request(self) -> NotesRequest:
        text, langs = self._notes_input()
        started = datetime.fromtimestamp(self.started_wall)
        lang = self.s.meeting_target_lang
        speech = ", ".join(ai.lang_name(code) for code, _n in langs.most_common(4))
        # заметки посреди встречи: модель должна знать, что расшифровка — только начало
        going = self.running and not self._finalized
        meta = {"Date": f"{started:%Y-%m-%d %H:%M}",
                "Duration": f"{self._minutes()} min" + (" so far" if going else ""),
                "Status": "in progress" if going else "ended",
                "Source": source_label(self.source) or "—", "Speech languages": speech or "—",
                "Notes language": ai.lang_name(lang)}
        return NotesRequest(text, lang, meta, self.title or "",
                            title_ai=self._title is not None, in_progress=going)

    def _notes_running(self, auto: bool) -> dict[str, Any]:
        return {"status": "running", "markdown": None, "generated_at": None, "model": None,
                "cost_usd": None, "error": None, "auto": auto}

    def _start_notes(self, auto: bool, again: bool = False) -> bool:
        """Начать заметки (поток rupor-meeting-notes). Уже пишутся — False; again — написать
        их ещё раз сразу после (конец встречи: в идущих нет её последних строк)."""
        with self._notes_lock:
            if self._notes_busy:
                if again:
                    self._notes_again = True
                return False
            self._notes_busy = True
            self._notes_idle.clear()
            self._notes = self._notes_running(auto)
        self._emit_notes()
        self._emit_status(force=True)
        th = threading.Thread(target=self._notes_loop, args=(auto,),
                              name="rupor-meeting-notes", daemon=True)
        self._notes_thread = th
        th.start()
        return True

    def _notes_loop(self, auto: bool) -> None:
        """Поток заметок: вызов (и ещё один, если встреча кончилась, пока он шёл). Что бы ни
        упало (и сам разбор неудачи), _notes_busy снимается: иначе заметок больше не будет, а
        поток названия ждал бы их до выхода ядра."""
        try:
            while True:
                try:
                    self._run_notes(auto)
                except Exception as e:
                    log.exception("Заметки встречи: поток упал")
                    try:
                        self._notes_failed(e, auto)
                    except Exception:
                        log.exception("Заметки встречи: неудача не разобралась")
                        with self._notes_lock:
                            self._notes = {"status": "error", "markdown": None,
                                           "generated_at": None, "model": None,
                                           "cost_usd": None, "error": _reason(e), "auto": auto}
                with self._notes_lock:
                    again = self._notes_again and not self._shutdown
                    self._notes_again = False
                    if again:
                        auto = True
                        self._notes = self._notes_running(True)
                    else:
                        self._notes_busy = False
                        self._notes_idle.set()
                if not again:
                    break
                self._emit_notes()
                self._emit_status(force=True)
        except Exception:
            # упало событие или статус между вызовами: заметки больше не в полёте
            log.exception("Заметки встречи: поток упал")
            with self._notes_lock:
                self._notes_busy = False
                self._notes_again = False
                if self._notes is not None and self._notes.get("status") == "running":
                    self._notes = dict(self._notes_good) if self._notes_good else None
                self._notes_idle.set()
        finally:
            self._title_kick()

    def _run_notes(self, auto: bool) -> None:
        """Один вызов заметок (поток rupor-meeting-notes). Сами после конца, а расшифровка та
        же, что у прошлых заметок, — без вызова (остаются прежние)."""
        fn = self._notes_fn
        req = self._notes_request()
        if fn is None or not req.transcript or (auto and req.transcript == self._notes_src):
            with self._notes_lock:
                self._notes = dict(self._notes_good) if self._notes_good else None
            self._emit_notes()
            self._emit_status(force=True)
            return
        self._wait_ai_busy()                    # ИИ-очистка диктовки — первая (до 1 с)
        if self._shutdown:
            return
        req.on_delta = self._notes_delta
        try:
            r = fn(req)
        except Exception as e:
            if not isinstance(e, ai.AIError):
                log.error("Заметки встречи: вызов упал: %s", _reason(e), exc_info=e)
            self._notes_failed(e, auto)
            return
        if self._shutdown:
            return                              # выход ядра: файл уже не трогать
        self._notes_done(req, r, auto)

    def _notes_done(self, req: NotesRequest, r: ai.AIResult, auto: bool) -> None:
        """Заметки готовы: «# название» — название записи (meeting_ai_title; модель его дала,
        а не подставлено название записи), разделы — в файл, MeetingNotes — done."""
        md = (r.text or "").strip("\n")
        first, _nl, rest = md.partition("\n")
        has_h1 = first.startswith("#") and not first.startswith("##")
        w = self._writer
        # разделы — те же, что лягут в файл: окно и «Копировать всё» без второй «## Полная
        # расшифровка»
        marker = w.marker if w is not None else f"## {tr('meeting.file_transcript')}"
        body = notes_body(rest if has_h1 else md, marker)
        h1 = ai.clean_title(first) if has_h1 else ""
        # «# …», которое ai.clean_notes ставит сам, если модель названия не дала: название
        # записи после той же чистки (без кавычек, markdown, точки в конце). С ним совпало —
        # подставлено или модель согласилась; различимо, только если название записи уже было
        # по содержанию (тогда оно и остаётся)
        sub = ai.clean_title(f"# {ai.cap_title(req.title)}")
        from_model = bool(h1) and (h1 != sub or req.title_ai)
        if from_model and bool(getattr(self.s, "meeting_ai_title", True)):
            with self._title_cond:
                if not self._title_closed:
                    self._title_early = True    # раннее название уже не нужно
            # раньше, чем название: вызов названия, который сейчас в полёте, его не заменит
            self._notes_title_n += 1
            self._set_title(h1)
            self._notes_title_src = req.transcript
            # встреча кончилась и заметки — по всей расшифровке: это название последнее
            # (_finalize ставит _finalized раньше, чем заметки начинаются)
            if self._finalized and req.transcript == self._notes_input()[0]:
                self._notes_titled = True
        if w is not None and not self._writer_failed and not w.set_notes(body):
            log.warning("Заметки встречи: в файл не записались — файл остался прежним")
        self._notes_src = req.transcript
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        cost = round(r.cost_usd, 6) if r.cost_usd is not None else None
        with self._notes_lock:
            # markdown — по запросу (notes): «# <название записи>» и разделы
            self._notes = {"status": "done", "markdown": None, "generated_at": now,
                           "model": r.model or None, "cost_usd": cost, "error": None,
                           "auto": auto}
            self._notes_good = dict(self._notes)
            self._notes_body = body
        log.info("Заметки встречи готовы (%s): %d симв., $%s", "сами" if auto else "по кнопке",
                 len(body), f"{cost:.4f}" if cost is not None else "?")
        self._emit_notes()
        self._emit_status(force=True)

    def _notes_failed(self, e: BaseException, auto: bool) -> None:
        """Заметки не получились: файл не трогается, прежние удачные (если были) остаются,
        MeetingNotes — error. Уведомление — одно за встречу: нет ключа, ключ отклонён, нет денег
        — всегда, с кнопкой в настройки ИИ; прочее — если заметки шли сами (по кнопке ошибку
        видно в окне перевода)."""
        msg = (str(e).strip() if isinstance(e, ai.AIError) else _reason(e)) or type(e).__name__
        kind = e.kind if isinstance(e, ai.AIError) else type(e).__name__
        key = kind in TR_STOP_KINDS
        with self._notes_lock:
            good = self._notes_good or {}           # markdown (notes) — прежних удачных
            self._notes = {"status": "error", "markdown": None,
                           "generated_at": good.get("generated_at"), "model": good.get("model"),
                           "cost_usd": good.get("cost_usd"), "error": msg, "auto": auto}
            send = (key or auto) and not self._notes_notice_sent
            if send:
                self._notes_notice_sent = True
        log.warning("Заметки встречи не получились (%s)", kind)
        if send:
            err = msg.rstrip(" .")
            # «Расшифровка сохранена» — только если файл и правда есть
            if self.file is not None and not self._writer_failed:
                text = tr("meeting.notes_failed", error=err)
            else:
                text = tr("meeting.notes_failed_unsaved", error=err)
            self._notice(text, level="warn", action="open_settings_ai" if key else None)
        self._emit_notes()
        self._emit_status(force=True)

    def _notes_delta(self, text: str) -> None:
        """Модель пишет заметки: начало — в событие meeting_notes (не чаще NOTES_EMIT_EVERY_S)."""
        now = time.monotonic()
        with self._notes_lock:
            n = self._notes
            if n is None or n.get("status") != "running":
                return
            n["markdown"] = text[:ai.NOTES_MAX_CHARS]
            if now - self._notes_last_emit < NOTES_EMIT_EVERY_S:
                return
            self._notes_last_emit = now
        self._emit_notes()

    def _emit_notes(self) -> None:
        """Событие meeting_notes. Снимок и отправка — под _notes_emit_lock: потоки заметок и
        названия (и распознавания — через _set_title) шлют его вперемешку, и более старый
        снимок не должен уйти после нового."""
        with self._notes_emit_lock:
            n = self.notes()
            if n is not None:
                self._emit("meeting_notes", n)

    # ---------- строки ----------

    def lines(self, after_id: int | None = None, limit: int = 200) -> list[dict[str, Any]]:
        """Строки: after_id задан — следующие за ним по порядку (не больше limit), иначе —
        последние limit. Выброшенные (мусор) — не отдаются."""
        with self._lock:
            shown = [x for x in self._lines if not x.dropped]
            if after_id is None:
                items = shown[-limit:] if limit > 0 else []
            else:
                items = [x for x in shown if x.id > after_id][:limit]
            return [x.to_dict() for x in items]

    def transcript_text(self) -> str:
        """Вся расшифровка (что в памяти) для буфера обмена: перевод и под ним оригинал."""
        with self._lock:
            return "\n".join(line_plain(x) for x in self._lines
                             if not x.dropped and line_source(x))

    def _name(self) -> str:
        return str(self.source.get("name") or self.source.get("exe") or "")


def _ms(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}"


def _reason(e: BaseException) -> str:
    text = str(e).strip()
    return text if text else type(e).__name__
