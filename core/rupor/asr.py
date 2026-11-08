"""Распознавание речи: faster-whisper (CTranslate2) на CUDA с откатом на CPU.

Модель грузится только из локального кэша HuggingFace (``fetch.model_path``) в папке
``model_dir`` из настроек (пусто — кэш по умолчанию) — сеть здесь не трогается; скачивание —
отдельной командой (``fetch.download_model``) с прогрессом.
DLL CUDA ищет ``fetch`` (pip-пакеты nvidia-* или %LOCALAPPDATA%\\Rupor\\cuda).

Язык распознавания (``choose_language``): настройка ``dictation_language`` — ``ru``/``en``
жёстко, без определения; ``auto`` — приоритет у языка интерфейса (``i18n``): второй язык
(en при русском интерфейсе, ru при английском) — только если модель в нём уверена
(вероятность ≥ ``en_threshold``). Подсказка ``initial_prompt`` для английской диктовки —
без кириллицы (``adapt_prompt``): русский контекст окна и русские частые слова тянули бы
Whisper к русскому, остаются латинские термины и фразы.
"""
from __future__ import annotations

import ctypes
import logging
import re
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from . import fetch, i18n
from .config import Settings

fetch.add_cuda_dll_dirs()  # до импорта faster-whisper/ctranslate2

from faster_whisper import WhisperModel  # noqa: E402

log = logging.getLogger(__name__)

SAMPLE_RATE = 16000

NoticeFn = Callable[[str, str], None]  # (текст, уровень info|warn|error)

LANGS = ("ru", "en")
DICTATION_LANGUAGES = ("auto", "ru", "en")

_CYR_RE = re.compile(r"[А-Яа-яЁё]")
# Границы кусков подсказки: после запятой/точки/?!…; или перевод строки
_SEG_SPLIT_RE = re.compile(r"(?<=[,;.!?…])\s+|\n+")
# Латинское слово или термин: ClickHouse, C++, C#, Node.js, GPT-4o, don't, snake_case
_LATIN_TOKEN_RE = re.compile(
    r"(?<![^\W\d_])[A-Za-z][A-Za-z0-9+#]*(?:[._'’-][A-Za-z0-9+#]+)*")


# ---------- мусор распознавания ----------
#
# Неуверенный сегмент faster-whisper перераспознаёт со всё большей «температурой» (до 1.0) —
# это уже угадывание: иероглифы, «jpuy, jpac», «зап測,測,測». А в тишине и музыке Whisper,
# обученный на субтитрах, «слышит» их служебные фразы («ДИНАМИЧНАЯ МУЗЫКА»). Такое не
# вставляем: распознаём ещё раз без подсказки (её мог отравить прежний мусор из истории)
# и без угадывания; снова мусор — пустой текст («Речь не распознана»).

# Письменности, которых в русской и английской диктовке не бывает: иврит, арабица,
# индийские, тайская, хангыль, кана, иероглифы
_FOREIGN_RANGES = ((0x0590, 0x08FF), (0x0900, 0x0DFF), (0x0E00, 0x0EFF), (0x1100, 0x11FF),
                   (0x3040, 0x30FF), (0x3100, 0x318F), (0x3400, 0x4DBF), (0x4E00, 0x9FFF),
                   (0xAC00, 0xD7AF), (0xF900, 0xFAFF))
# Целиком — служебная фраза субтитров (сравнение без регистра, «ё» и знаков препинания)
_HALLUCINATIONS = frozenset((
    "динамичная музыка", "спокойная музыка", "веселая музыка", "грустная музыка",
    "напряженная музыка", "тревожная музыка", "торжественная музыка", "музыка",
    "аплодисменты", "субтитры сделал dimatorzok", "субтитры создавал dimatorzok",
    "субтитры делал dimatorzok", "продолжение следует", "спасибо за просмотр",
    "редактор субтитров а синецкая корректор а егорова", "thank you for watching",
    "thanks for watching", "subtitles by the amara org community",
))
GUESS_TEMPERATURE = 0.6    # сегмент с такой температурой и выше — угадывание
LOW_LOGPROB = -1.5         # средняя уверенность сегмента ниже — повторить без подсказки
REPEAT_RATIO = 2.4         # сжимаемость текста выше (как у Whisper) — зацикленный повтор
REPEAT_MIN_CHARS = 40
# Сжимаемость считается окнами: у Whisper порог 2.4 — для одного сегмента (≤ 30 с), а
# обычный русский текст в UTF-8 длиннее ~2000 символов (2,5 минуты речи) сжимается сильнее
# 2.4 и без всякого повтора — так длинная диктовка целиком уходила в «мусор».
REPEAT_WINDOW = 400
SALVAGE_MIN_WORDS = 4      # оба прохода — мусор, но после чистки осталось столько слов — вставить
# Короткое зацикливание посреди нормальной фразы («типа какого интерфа, какого интерфа,
# какого интерфа, интерфа, интерфа интерф» — 08.11.2026): сжимаемость всего текста ещё
# обычная, а Whisper (часто отравленный подсказкой) уже крутит одно и то же до конца
# записи вместо сказанного. Признак — фраза из нескольких слов подряд LOOP_REPEATS раз
# или одно слово подряд LOOP_WORD_REPEATS раз; такое распознаётся ещё раз без подсказки.
LOOP_MAX_UNIT = 6          # самая длинная повторяющаяся фраза, слов
LOOP_REPEATS = 3           # фраза из 2+ слов подряд столько раз — зацикливание
LOOP_WORD_REPEATS = 4      # одно слово (от 3 букв) подряд столько раз — зацикливание
# Оба прохода зациклены: повтор, которого человек не скажет, сворачивается до одного раза
COLLAPSE_REPEATS = 4       # фраза из 2+ слов подряд столько раз и больше
COLLAPSE_WORD_REPEATS = 6  # одно слово подряд столько раз и больше
_WORD_RE = re.compile(r"[^\W_]+")


def _loop_runs(text: str) -> list[tuple[int, int, int]]:
    """Повторы подряд в тексте: (первое слово, длина фразы в словах, сколько раз подряд),
    самые длинные по числу повторённых слов, без пересечений. Сравнение — без регистра и
    «ё». Повтор одного слова короче 3 букв («да да да») не считается."""
    words = [m.group().casefold().replace("ё", "е") for m in _WORD_RE.finditer(text)]
    runs: list[tuple[int, int, int]] = []
    i = 0
    while i < len(words):
        best: tuple[int, int] | None = None   # (длина фразы, повторов)
        for n in range(1, LOOP_MAX_UNIT + 1):
            if i + 2 * n > len(words):
                break
            unit = words[i:i + n]
            if (n == 1 and len(unit[0]) < 3) or (n > 1 and len(set(unit)) == 1):
                continue    # «да да да»; фраза из одного слова — это повтор слова (n = 1)
            k = 1
            while words[i + k * n:i + (k + 1) * n] == unit:
                k += 1
            if k >= 2 and (best is None or k * n > best[0] * best[1]):
                best = (n, k)
        if best is None:
            i += 1
            continue
        n, k = best
        runs.append((i, n, k))
        i += n * k
    return runs


def _is_loop(n: int, k: int) -> bool:
    return k >= (LOOP_WORD_REPEATS if n == 1 else LOOP_REPEATS)


def looping(text: str) -> bool:
    """Whisper зациклился: фраза из 2+ слов LOOP_REPEATS раз подряд или одно слово
    LOOP_WORD_REPEATS раз подряд."""
    return any(_is_loop(n, k) for _i, n, k in _loop_runs(text))


def loop_words(text: str) -> int:
    """Сколько слов текста — лишние повторы зацикливания (меньше — текст лучше)."""
    return sum(n * (k - 1) for _i, n, k in _loop_runs(text) if _is_loop(n, k))


def collapse_loops(text: str) -> str:
    """Свернуть повторы, которых человек не говорит (фраза из 2+ слов COLLAPSE_REPEATS раз
    подряд, одно слово COLLAPSE_WORD_REPEATS раз), до одного раза. Остальной текст —
    как был, с исходной пунктуацией и регистром."""
    spans = [m.span() for m in _WORD_RE.finditer(text)]
    cut: list[tuple[int, int]] = []
    for i, n, k in _loop_runs(text):
        if k >= (COLLAPSE_WORD_REPEATS if n == 1 else COLLAPSE_REPEATS):
            cut.append((spans[i + n - 1][1], spans[i + n * k - 1][1]))
    if not cut:
        return text
    out, pos = [], 0
    for a, b in cut:
        out.append(text[pos:a])
        pos = b
    out.append(text[pos:])
    res = "".join(out)
    res = re.sub(r"\s+([,.!?;:])", r"\1", res)
    return re.sub(r"\s{2,}", " ", res).strip()


def _foreign(text: str) -> bool:
    return any(lo <= ord(ch) <= hi for ch in text for lo, hi in _FOREIGN_RANGES)


def _norm_phrase(text: str) -> str:
    words = re.findall(r"[^\W_]+", text.casefold().replace("ё", "е"))
    return " ".join(words)


def _ratio(text: str) -> float:
    import zlib
    data = text.encode("utf-8")
    return len(data) / max(1, len(zlib.compress(data)))


def _repetitive(text: str) -> bool:
    """Зацикленный повтор где-то в тексте: сжимаемость окна REPEAT_WINDOW символов (шаг —
    половина окна) выше REPEAT_RATIO. Короткий текст — целиком."""
    if len(text) < REPEAT_MIN_CHARS:
        return False
    if len(text) <= REPEAT_WINDOW:
        return _ratio(text) > REPEAT_RATIO
    step = REPEAT_WINDOW // 2
    starts = list(range(0, len(text) - REPEAT_WINDOW + 1, step))
    if starts[-1] + REPEAT_WINDOW < len(text):
        starts.append(len(text) - REPEAT_WINDOW)
    return any(_ratio(text[i:i + REPEAT_WINDOW]) > REPEAT_RATIO for i in starts)


def salvage(text: str) -> str:
    """Что можно спасти из мусорного текста: убрать символы чужих алфавитов (Whisper вставляет
    иероглиф посреди нормальной длинной фразы). Осталось меньше SALVAGE_MIN_WORDS слов или
    остаток — всё равно мусор → пустая строка."""
    if not text:
        return ""
    out = "".join(" " if any(lo <= ord(ch) <= hi for lo, hi in _FOREIGN_RANGES) else ch
                  for ch in text)
    out = re.sub(r"\s+([,.!?;:])", r"\1", out)
    out = re.sub(r"([,;:])(?:\s*[,;:])+", r"\1", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" ,;:")
    if len(re.findall(r"[^\W\d_]{2,}", out)) < SALVAGE_MIN_WORDS or garbage_reason(out):
        return ""
    return out


def garbage_reason(text: str) -> str | None:
    """Почему текст — заведомый мусор (не вставлять), None — текст как текст."""
    if not text:
        return None
    if _foreign(text):
        return "чужие символы"
    if _norm_phrase(text) in _HALLUCINATIONS:
        return "фраза субтитров"
    if _repetitive(text):
        return "зацикленный повтор"
    return None


def _mean(stats: list[tuple[float | None, float]]) -> float:
    return sum(lp for _t, lp in stats) / len(stats) if stats else 0.0


def guessing(stats: list[tuple[float | None, float]]) -> bool:
    """Какой-то сегмент распознан с повышенной температурой — это уже угадывание."""
    return any(t is not None and t >= GUESS_TEMPERATURE for t, _lp in stats)


def low_confidence(stats: list[tuple[float | None, float]]) -> bool:
    """Какой-то сегмент с низкой средней уверенностью — стоит переспросить без подсказки."""
    return any(lp < LOW_LOGPROB for _t, lp in stats)


def dictation_mode(s: object) -> str:
    """Настройка ``dictation_language``: auto | ru | en (неизвестное — auto)."""
    v = getattr(s, "dictation_language", "auto")
    return v if v in DICTATION_LANGUAGES else "auto"


def primary_language(s: object) -> str:
    """Основной язык диктовки в режиме auto — язык интерфейса (ru/en): из настройки
    ``language`` (system → язык Windows), без неё — текущий язык ``i18n``."""
    setting = getattr(s, "language", None)
    return i18n.resolve(setting) if setting is not None else i18n.current()


def choose_language(s: object, detected: str | None, prob: float) -> str:
    """Язык распознавания по настройкам и результату определения языка моделью.

    ``ru``/``en`` в ``dictation_language`` — всегда этот язык. ``auto`` — язык интерфейса,
    а второй язык — только при уверенности модели ≥ ``en_threshold`` (порог второго
    языка: при русском интерфейсе — английского, при английском — русского)."""
    mode = dictation_mode(s)
    if mode != "auto":
        return mode
    primary = primary_language(s)
    second = "en" if primary == "ru" else "ru"
    threshold = getattr(s, "en_threshold", 0.8)
    if detected == second and prob >= threshold:
        return second
    return primary


def adapt_prompt(prompt: str | None, language: str) -> str | None:
    """Подсказка ``initial_prompt`` под язык распознавания.

    Русская диктовка — как есть. Английская — без кириллицы: куски подсказки (контекст
    окна, термины через запятую) целиком на латинице остаются как были и в том же порядке,
    из кусков с кириллицей берутся только латинские слова («надо проверить ClickHouse» →
    «ClickHouse»), русские слова и имена выбрасываются. Ничего не осталось — None."""
    if not prompt or language != "en" or not _CYR_RE.search(prompt):
        return prompt
    parts: list[str] = []
    seen: set[str] = set()
    for seg in _SEG_SPLIT_RE.split(prompt.strip()):
        seg = seg.strip()
        if not seg:
            continue
        if not _CYR_RE.search(seg):
            if seg.rstrip(",;:.!?…").strip().lower() in seen:
                continue            # термин списка, уже взятый из контекста
            parts.append(seg)
            seen.update(t.lower() for t in _LATIN_TOKEN_RE.findall(seg))
            continue
        for tok in _LATIN_TOKEN_RE.findall(seg):
            if len(tok) >= 2 and tok.lower() not in seen:
                seen.add(tok.lower())
                parts.append(tok + ",")
    out = re.sub(r"[\s,;:]+$", "", " ".join(parts))
    if not out:
        return None
    return out if out[-1] in ".!?…" else out + "."


class ModelMissing(RuntimeError):
    """Модели нет в локальном кэше — её нужно скачать (setup_download)."""


@dataclass
class Result:
    text: str
    language: str
    audio_s: float
    elapsed_ms: float


def _no_notice(_text: str, _level: str) -> None:
    pass


class Recognizer:
    def __init__(self, settings: Settings, on_notice: NoticeFn | None = None) -> None:
        self.s = settings
        self._on_notice: NoticeFn = on_notice or _no_notice
        self._model: Any = None
        self._lock = threading.Lock()
        self.device: str | None = None       # фактическое устройство загруженной модели
        # почему не вышло на GPU (для cuda_check): i18n.Msg — переводится при показе, после
        # смены языка — уже на новом; текст исключения — как есть
        self.last_error: str | i18n.Msg | None = None
        self.last_used = time.monotonic()
        self._force_cpu = False  # GPU упал посреди работы — до выгрузки модели только CPU
        # Подсказка распознаванию (initial_prompt); движок подставляет термины словаря
        self.prompt_provider: Callable[[], str | None] = self.default_prompt

    # ---------- загрузка ----------

    def _create(self, device: str, compute_type: str) -> Any:
        path = fetch.model_path(self.s.model, self.s.model_dir or None)
        if path is None:
            raise ModelMissing(i18n.tr("asr.model_missing", model=self.s.model))
        return WhisperModel(str(path), device=device, compute_type=compute_type)

    def load(self) -> None:
        with self._lock:
            if self._model is not None:
                return
            t0 = time.perf_counter()
            device, ctype = self.s.device, self.s.compute_type
            if device == "cuda" and self._force_cpu:
                device, ctype = "cpu", "int8"
            elif device == "cuda" and not fetch.cuda_ready():
                missing = fetch.missing_cuda_dlls()
                self.last_error = i18n.Msg("eng.cuda_missing", names=", ".join(missing))
                log.warning("Не найдены библиотеки CUDA: %s — загружаю модель на CPU",
                            ", ".join(missing))
                self._on_notice(i18n.tr("asr.cuda_missing_cpu"), "warn")
                device, ctype = "cpu", "int8"
            try:
                self._model = self._create(device, ctype)
                self.device = device
                self._warmup()
                if device == "cuda":
                    self.last_error = None
            except ModelMissing:
                self._model = None
                raise
            except Exception as e:  # CUDA недоступна, не хватает VRAM, нет DLL…
                self._model = None
                if device == "cpu":
                    raise
                self.last_error = f"{type(e).__name__}: {e}"
                log.exception("Не удалось запустить модель на GPU — перехожу на CPU")
                self._on_notice(i18n.tr("asr.gpu_unavailable", error=type(e).__name__),
                                "warn")
                self._model = self._create("cpu", "int8")
                self.device = "cpu"
                self._warmup()
            self.last_used = time.monotonic()
            log.info("Модель %s загружена на %s за %.1f с", self.s.model, self.device,
                     time.perf_counter() - t0)

    def _warmup(self) -> None:
        silence = np.zeros(SAMPLE_RATE, dtype=np.float32)
        segs, _ = self._model.transcribe(silence, language="ru", beam_size=1)
        list(segs)

    def unload(self) -> None:
        """Выгрузить модель. Следующая загрузка снова попробует GPU."""
        with self._lock:
            self._force_cpu = False
            if self._model is not None:
                self._model = None
                self.device = None
                log.info("Модель выгружена из памяти")

    @property
    def loaded(self) -> bool:
        return self._model is not None

    # ---------- распознавание ----------

    def detect_language(self, audio: np.ndarray) -> str:
        """Язык диктовки (``choose_language``): жёсткий из настроек — сразу, без модели;
        auto — приоритет у языка интерфейса, второй язык — только при уверенности модели.

        Движок зовёт это заранее, ещё во время записи (по первым секундам речи),
        чтобы после отпускания клавиши оставалось только распознавание."""
        fixed = dictation_mode(self.s)
        if fixed != "auto":
            return fixed
        self.load()
        with self._lock:
            return self._pick_language(audio)

    def _pick_language(self, audio: np.ndarray) -> str:
        fixed = dictation_mode(self.s)
        if fixed != "auto":
            return fixed
        if self._model is None:
            raise ModelMissing(i18n.tr("asr.model_unloaded"))
        try:
            lang, prob, _ = self._model.detect_language(audio)
        except Exception:
            primary = primary_language(self.s)
            log.exception("detect_language упал — считаю, что язык %s", primary)
            return primary
        return choose_language(self.s, lang, float(prob))

    def token_counter(self) -> Callable[[str], int] | None:
        """Счётчик токенов подсказки — так, как faster-whisper кодирует initial_prompt
        (" " + текст без пробелов по краям, без служебных токенов): он оставляет только
        последние 223 токена, лишнее начало молча срезается. None — модель не загружена
        или без токенизатора (тогда vocab оценивает длину сам)."""
        model = self._model
        tok = getattr(model, "hf_tokenizer", None) if model is not None else None
        if tok is None:
            return None

        def count(text: str) -> int:
            return len(tok.encode(" " + text.strip(), add_special_tokens=False).ids)
        return count

    def default_prompt(self) -> str | None:
        terms = [t for t in self.s.terms if t.strip()]
        return ", ".join(terms) + "." if terms else None

    def transcribe(self, audio: np.ndarray, language: str | None = None) -> Result:
        """Распознать. Сбой на GPU посреди работы (память, драйвер) → откат на CPU int8
        с уведомлением и повтор той же записи — диктовка не теряется."""
        try:
            return self._transcribe(audio, language)
        except ModelMissing:
            raise
        except Exception as e:
            if self.device != "cuda":
                raise
            log.exception("Распознавание на GPU упало — перехожу на CPU")
            self.unload()
            self.last_error = f"{type(e).__name__}: {e}"
            self._force_cpu = True
            self._on_notice(i18n.tr("asr.gpu_failed", error=type(e).__name__), "warn")
            return self._transcribe(audio, language)

    def _transcribe(self, audio: np.ndarray, language: str | None) -> Result:
        self.load()
        self.last_used = time.monotonic()
        try:
            prompt = self.prompt_provider()
        except Exception:
            log.exception("Не удалось собрать подсказку для распознавания")
            prompt = self.default_prompt()
        t0 = time.perf_counter()
        with self._lock:
            if self._model is None:  # выгрузили между load() и захватом замка
                raise ModelMissing(i18n.tr("asr.model_unloaded_retry"))
            lang = language or self._pick_language(audio)
            prompt = adapt_prompt(prompt, lang)
            text, stats = self._decode(audio, lang, prompt, guess=True)
            text = self._second_opinion(audio, lang, text, stats)
        ms = (time.perf_counter() - t0) * 1000
        r = Result(text, lang, len(audio) / SAMPLE_RATE, ms)
        log.info("ASR %.1f с аудио → %.0f мс, язык %s, %d симв.", r.audio_s, ms, lang, len(text))
        return r

    def _second_opinion(self, audio: np.ndarray, lang: str, text: str,
                        stats: list[tuple[float | None, float]]) -> str:
        """Первый проход — мусор или угадывание: распознать ещё раз без подсказки и без
        угадывания; повтор чистый — он, повтор тоже мусор — пустой текст. Первый проход
        просто неуверенный — из двух берётся более уверенный, мусорный повтор не берётся."""
        bad = garbage_reason(text)
        guessed = guessing(stats)
        loop = bad is None and looping(text)
        if not (bad or guessed or loop or low_confidence(stats)):
            return text
        log.warning("Распознавание сомнительное (%s) — повторяю без подсказки",
                    bad or ("угадывание" if guessed else "зацикливание" if loop
                            else "низкая уверенность"))
        text2, stats2 = self._decode(audio, lang, None, guess=False)
        again = garbage_reason(text2)
        if loop and not again and not guessed:
            # Без подсказки тоже повтор — скорее всего его и правда сказали (или звук такой):
            # берётся проход с меньшим повтором, нечеловеческие повторы сворачиваются
            if looping(text2):
                best = text2 if loop_words(text2) <= loop_words(text) else text
                log.warning("Повтор без подсказки тоже зациклен — сворачиваю повторы")
                return collapse_loops(best)
            return text2
        if again:
            if bad or guessed:
                # Длинная диктовка с одним «иероглифом» — не мусор целиком: спасти текст
                for cand in (text2, text):
                    saved = salvage(cand)
                    if saved:
                        log.warning("Повтор тоже мусор (%s) — вставляю очищенный текст "
                                    "(%d из %d симв.)", again, len(saved), len(cand))
                        return saved
                log.warning("Повтор тоже мусор (%s) — текст не вставляю", again)
                return ""
            return collapse_loops(text)
        if bad or guessed or _mean(stats2) >= _mean(stats):
            return collapse_loops(text2)
        return text

    def _decode(self, audio: np.ndarray, lang: str, prompt: str | None, guess: bool
                ) -> tuple[str, list[tuple[float | None, float]]]:
        """Один проход faster-whisper под замком: текст и (температура, средняя уверенность)
        сегментов. guess=False — без повторов с повышенной температурой (только 0)."""
        assert self._model is not None
        kw: dict[str, Any] = {} if guess else {"temperature": 0.0}
        segs, _info = self._model.transcribe(
            audio,
            language=lang,
            beam_size=self.s.beam_size,
            vad_filter=True,
            vad_parameters={"min_silence_duration_ms": 500},
            condition_on_previous_text=False,
            without_timestamps=True,
            initial_prompt=prompt,
            **kw,
        )
        segs = list(segs)
        text = " ".join(s.text.strip() for s in segs).strip()
        return text, [(getattr(s, "temperature", None), float(getattr(s, "avg_logprob", 0.0)))
                      for s in segs]


def load_wav(path: str) -> np.ndarray:
    """wav → float32 16 кГц моно (через декодер faster-whisper, умеет любые форматы)."""
    from faster_whisper import decode_audio
    return np.asarray(decode_audio(path, sampling_rate=SAMPLE_RATE), dtype=np.float32)


# ---------- сведения о GPU (для cuda_check) ----------

def cuda_device_count() -> int:
    try:
        import ctranslate2
        return int(ctranslate2.get_cuda_device_count())
    except Exception:
        log.debug("get_cuda_device_count", exc_info=True)
        return 0


def cuda_compute_types() -> frozenset[str] | None:
    """Точности, которые CTranslate2 умеет на первой видеокарте CUDA (float16 и int8_float16 —
    от Volta, CC 7.0; int8 — от CC 6.1); None — CUDA карты не видит или узнать не вышло.
    cuBLAS для этого не нужен (он грузится только с моделью) — работает и до его загрузки."""
    try:
        import ctranslate2
        if ctranslate2.get_cuda_device_count() < 1:
            return None
        return frozenset(str(t) for t in ctranslate2.get_supported_compute_types("cuda"))
    except Exception:
        log.debug("get_supported_compute_types", exc_info=True)
        return None


class _NvmlMemory(ctypes.Structure):
    _fields_ = [("total", ctypes.c_ulonglong), ("free", ctypes.c_ulonglong),
                ("used", ctypes.c_ulonglong)]


def gpu_info() -> tuple[str, int] | None:
    """(имя GPU, VRAM в МБ) первой видеокарты NVIDIA через NVML (идёт с драйвером); None — нет."""
    try:
        nv = ctypes.WinDLL("nvml.dll")
    except OSError:
        return None
    try:
        if nv.nvmlInit_v2() != 0:
            return None
        try:
            h = ctypes.c_void_p()
            if nv.nvmlDeviceGetHandleByIndex_v2(0, ctypes.byref(h)) != 0:
                return None
            name = ctypes.create_string_buffer(96)
            if nv.nvmlDeviceGetName(h, name, 96) != 0:
                return None
            mem = _NvmlMemory()
            total = 0
            if nv.nvmlDeviceGetMemoryInfo(h, ctypes.byref(mem)) == 0:
                total = int(mem.total // (1024 * 1024))
            return name.value.decode("utf-8", "replace"), total
        finally:
            nv.nvmlShutdown()
    except (OSError, AttributeError):
        log.debug("NVML", exc_info=True)
        return None
