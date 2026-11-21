"""Чей голос: счёт «свой / чужой» для диктовки по голосовому профилю — теневой режим.

Рупор по этому счёту ничего не решает и ничего не блокирует: после вставки текста фраза в
фоне сравнивается с профилем голоса, и результат дописывается в журнал. По журналу потом
подбираются пороги.

Путь одной фразы (тем же кодом считаются профиль и опорная выборка — иначе счета несравнимы):
  звук 16 кГц → только речь (VAD silero из faster-whisper), первые 15 с; меньше секунды речи
  — не считаем → признаки fbank 80 (как в Kaldi) минус среднее по времени → модель CAM++
  (ONNX, процессор) → вектор 192 длины 1.
Счёт:
  вектор минус среднее опорной выборки, длина снова 1 → косинус с профилем (``cos``) →
  AS-norm по 300 ближайшим из опорной выборки: s' = ½·((s − μp)/σp + (s − μx)/σx).

Файлы лежат в данных пользователя, ``%APPDATA%\\Rupor\\speaker``: ``model.onnx`` (CAM++
``3dspeaker_speech_campplus_sv_zh_en_16k-common_advanced`` из выпуска
``speaker-recongition-models`` репозитория k2-fsa/sherpa-onnx), ``profile.npz`` (профиль и
опорная выборка), ``journal.jsonl`` (журнал, не ротируется). Профиль собирает
``python -m rupor --speaker-enroll …`` (``enroll``). В репозитории и в сборке этих файлов нет.
"""
from __future__ import annotations

import functools
import hashlib
import json
import logging
import queue
import random
import shutil
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable

import numpy as np

SAMPLE_RATE = 16000
MAX_SPEECH_S = 15.0         # в эмбеддинг идут первые столько секунд речи
MIN_SPEECH_S = 1.0          # речи меньше — эмбеддинг не считается
SHORT_SPEECH_S = 1.5        # речи меньше — метка «коротко», какой бы ни был счёт
MAX_DECODE_S = 60.0         # из файла при сборке профиля берётся начало
TOP_N = 300                 # AS-norm: сколько ближайших из опорной выборки
MODEL_FILE = "model.onnx"
PROFILE_FILE = "profile.npz"
JOURNAL_FILE = "journal.jsonl"
AUDIO_EXT = {".ogg", ".oga", ".opus", ".wav", ".mp3", ".m4a", ".flac"}
LABELS = ("own", "alien", "unsure", "short")

log = logging.getLogger(__name__)


def unit(v: np.ndarray) -> np.ndarray:
    return np.asarray(v / (np.linalg.norm(v, axis=-1, keepdims=True) + 1e-9), dtype=np.float32)


# ---------- признаки: fbank как в Kaldi ----------

_FRAME, _SHIFT, _FFT, _MELS = 400, 160, 512, 80      # 25 мс / 10 мс при 16 кГц
_PREEMPH = 0.97
_LOW_HZ, _HIGH_HZ = 20.0, SAMPLE_RATE / 2 - 400.0
_FLOOR = float(np.finfo(np.float32).eps)


@functools.cache
def _window() -> np.ndarray:
    i = np.arange(_FRAME)
    return np.asarray((0.5 - 0.5 * np.cos(2 * np.pi * i / (_FRAME - 1))) ** 0.85)   # povey


@functools.cache
def _mel_banks() -> np.ndarray:
    """Треугольные полосы на шкале мел, как MelBanks в Kaldi: [80, 256]."""
    def mel(f: np.ndarray | float) -> Any:
        return 1127.0 * np.log(1.0 + np.asarray(f, dtype=np.float64) / 700.0)

    lo, hi = float(mel(_LOW_HZ)), float(mel(_HIGH_HZ))
    step = (hi - lo) / (_MELS + 1)
    m = mel(np.arange(_FFT // 2) * (SAMPLE_RATE / _FFT))[None, :]
    left = lo + np.arange(_MELS)[:, None] * step
    center, right = left + step, left + 2 * step
    up, down = (m - left) / step, (right - m) / step
    return np.asarray(np.where((m > left) & (m <= center), up,
                               np.where((m > center) & (m < right), down, 0.0)))


def fbank(audio: np.ndarray) -> np.ndarray:
    """Признаки [кадры, 80] для звука 16 кГц в [-1, 1] — те же, что считает
    kaldi-native-fbank в sherpa-onnx для моделей диктора: окно povey 25 мс, шаг 10 мс,
    80 полос 20–7600 Гц, snip_edges=False (края — зеркальным отражением), без dither."""
    x = np.asarray(audio, dtype=np.float64).reshape(-1)
    n = len(x)
    frames = (n + _SHIFT // 2) // _SHIFT
    if n < 2 or frames < 1:
        return np.zeros((0, _MELS), np.float32)
    idx = (np.arange(frames)[:, None] * _SHIFT + (_SHIFT // 2 - _FRAME // 2)
           + np.arange(_FRAME)[None, :])
    while True:
        neg, over = idx < 0, idx >= n
        if not (neg.any() or over.any()):
            break
        idx = np.where(neg, -idx - 1, idx)
        idx = np.where(idx >= n, 2 * n - 1 - idx, idx)
    f = x[idx]
    f = f - f.mean(axis=1, keepdims=True)                           # remove_dc_offset
    f = np.concatenate([f[:, :1] * (1.0 - _PREEMPH), f[:, 1:] - _PREEMPH * f[:, :-1]], axis=1)
    f = f * _window()
    power = np.abs(np.fft.rfft(f, _FFT, axis=1)[:, :_FFT // 2]) ** 2
    return np.asarray(np.log(np.maximum(power @ _mel_banks().T, _FLOOR)), dtype=np.float32)


# ---------- речь и эмбеддинг ----------

def speech_only(audio: np.ndarray) -> np.ndarray:
    """Только речь: участки голоса по silero VAD (тот же, что у распознавания), подряд."""
    from faster_whisper.vad import VadOptions, get_speech_timestamps

    a = np.asarray(audio, dtype=np.float32).reshape(-1)
    spans = get_speech_timestamps(a, VadOptions(
        min_speech_duration_ms=250, min_silence_duration_ms=250, speech_pad_ms=30))
    if not spans:
        return np.zeros(0, np.float32)
    return np.concatenate([a[int(s["start"]):int(s["end"])] for s in spans])


class Embedder:
    """Модель диктора (ONNX, процессор). Сессия создаётся при первом расчёте."""

    def __init__(self, model: Path, threads: int = 1) -> None:
        self.model = Path(model)
        self._threads = threads
        self._lock = threading.Lock()
        self._sess: Any = None
        self._input = ""

    def _session(self) -> Any:
        with self._lock:
            if self._sess is None:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = self._threads
                opts.inter_op_num_threads = 1
                opts.log_severity_level = 3
                # байтами, а не путём: путь к данным пользователя бывает не латиницей
                self._sess = ort.InferenceSession(self.model.read_bytes(), opts,
                                                  providers=["CPUExecutionProvider"])
                self._input = self._sess.get_inputs()[0].name
            return self._sess

    def embed(self, speech: np.ndarray) -> np.ndarray:
        """Вектор диктора длины 1 для звука, в котором только речь."""
        feats = fbank(speech)
        if not len(feats):
            raise ValueError("no audio to embed")
        feats = feats - feats.mean(axis=0, keepdims=True)           # global-mean из метаданных
        out = self._session().run(None, {self._input: feats[None, :, :]})[0]
        return unit(np.asarray(out, dtype=np.float32).reshape(-1))


def voice_embedding(audio: np.ndarray, embedder: Embedder) -> tuple[np.ndarray | None, float]:
    """(вектор диктора или None, секунд речи) для записи 16 кГц: речь по VAD из начала
    записи (MAX_DECODE_S), первые MAX_SPEECH_S секунд; речи меньше MIN_SPEECH_S — вектор не
    считается."""
    head = np.asarray(audio).reshape(-1)[:int(MAX_DECODE_S * SAMPLE_RATE)]
    speech = speech_only(head)[:int(MAX_SPEECH_S * SAMPLE_RATE)]
    speech_s = len(speech) / SAMPLE_RATE
    if speech_s < MIN_SPEECH_S:
        return None, speech_s
    return embedder.embed(speech), speech_s


# ---------- профиль и счёт ----------

@dataclass
class Profile:
    center: np.ndarray                   # среднее опорной выборки [192]
    voice: np.ndarray                    # профиль голоса [192]: центрирован, длина 1
    cohort: np.ndarray                   # опорная выборка [N, 192]: центрирована, длина 1
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self._mu_p, self._sigma_p = self._top_stats(self.voice)

    def _top_stats(self, x: np.ndarray) -> tuple[float, float]:
        top = np.sort(self.cohort @ x)[-TOP_N:]
        return float(top.mean()), float(top.std())

    def score(self, emb: np.ndarray) -> tuple[float, float]:
        """(косинус с профилем после центрирования, счёт AS-norm)."""
        x = unit(np.asarray(emb, dtype=np.float32) - self.center)
        s = float(x @ self.voice)
        mu_x, sigma_x = self._top_stats(x)
        return s, 0.5 * ((s - self._mu_p) / self._sigma_p + (s - mu_x) / sigma_x)

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".part")
        with tmp.open("wb") as f:
            np.savez(f, center=self.center, voice=self.voice, cohort=self.cohort,
                     meta=np.array(json.dumps(self.meta, ensure_ascii=False)))
        tmp.replace(path)

    @classmethod
    def load(cls, path: Path) -> Profile:
        with np.load(path, allow_pickle=False) as z:
            return cls(center=z["center"], voice=z["voice"], cohort=z["cohort"],
                       meta=json.loads(str(z["meta"])))


def build_profile(sources: list[np.ndarray], cohort: np.ndarray,
                  meta: dict[str, Any] | None = None) -> Profile:
    """Профиль из эмбеддингов своего голоса и опорной выборки чужих. ``sources`` — по
    массиву [n, 192] на источник (голосовые, записи Рупора); у источников равный вес, сколько
    бы записей в каждом ни было: телефон и микрофон компьютера звучат по-разному."""
    cohort = np.asarray(cohort, dtype=np.float32)
    sources = [np.asarray(s, dtype=np.float32) for s in sources if len(s)]
    if len(cohort) < TOP_N:
        raise ValueError(f"cohort too small: {len(cohort)} < {TOP_N}")
    if not sources:
        raise ValueError("no voice recordings for the profile")
    center = cohort.mean(axis=0)
    voice = unit(np.sum([unit(unit(s - center).mean(axis=0)) for s in sources], axis=0))
    return Profile(center=center, voice=voice, cohort=unit(cohort - center), meta=meta or {})


def label(score: float | None, speech_s: float, own_min: float, alien_max: float) -> str:
    """Предварительная метка по счёту AS-norm: own / alien / unsure / short."""
    if score is None or speech_s < SHORT_SPEECH_S:
        return "short"
    if score >= own_min:
        return "own"
    return "alien" if score < alien_max else "unsure"


# ---------- теневой подсчёт для диктовок ----------

class Shadow:
    """Очередь фраз и один фоновый поток: счёт против профиля и строка в журнал. Диктовку
    не задерживает и не срывает; сбой подсчёта — в лог."""

    QUEUE = 8                            # фраз в ожидании; сверх — пропуск с записью в лог

    def __init__(self, root: Path, thresholds: Callable[[], tuple[float, float]]) -> None:
        self.root = Path(root)
        self._thresholds = thresholds
        self._q: queue.Queue[tuple[np.ndarray, str | None, float] | None] = queue.Queue(self.QUEUE)
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._embedder: Embedder | None = None
        self._profile: Profile | None = None
        self._stamp: tuple[tuple[int, int], tuple[int, int]] | None = None
        self._missing_logged = False

    # --- вход ---

    def submit(self, audio: np.ndarray, rec_id: str | None, ts: float) -> None:
        """Отдать фразу на подсчёт. Возвращается сразу."""
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(target=self._loop, name="rupor-speaker",
                                                daemon=True)
                self._thread.start()
        try:
            self._q.put_nowait((audio, rec_id, ts))
        except queue.Full:
            log.warning("Голос: очередь подсчёта полна — фраза пропущена")

    def stop(self) -> None:
        """Остановить поток; фразы из очереди досчитываются, но выход ждёт не дольше секунды."""
        with self._lock:
            t, self._thread = self._thread, None
        if t is None:
            return
        try:
            self._q.put_nowait(None)
        except queue.Full:
            return                       # поток фоновый — уйдёт вместе с процессом
        t.join(1.0)

    # --- поток ---

    def _loop(self) -> None:
        while True:
            item = self._q.get()
            if item is None:
                return
            try:
                self.score(*item)
            except Exception:
                log.exception("Голос: счёт для диктовки не посчитан")

    def _load(self) -> bool:
        """Модель и профиль с диска; перечитываются, когда файлы сменились. False — их нет."""
        files = (self.root / MODEL_FILE, self.root / PROFILE_FILE)
        try:
            st = [p.stat() for p in files]
        except OSError:
            if not self._missing_logged:
                self._missing_logged = True
                log.warning("Голос: нет модели или профиля в %s — счёт не считается "
                            "(профиль собирает --speaker-enroll)", self.root)
            self._embedder = self._profile = self._stamp = None
            return False
        stamp = ((st[0].st_mtime_ns, st[0].st_size), (st[1].st_mtime_ns, st[1].st_size))
        if stamp != self._stamp:
            self._embedder = Embedder(files[0])
            self._profile = Profile.load(files[1])
            self._stamp = stamp
            self._missing_logged = False
            log.info("Голос: профиль загружен (%s)", self._profile.meta.get("built", "?"))
        return True

    def score(self, audio: np.ndarray, rec_id: str | None, ts: float) -> dict[str, Any] | None:
        """Посчитать одну фразу и дописать строку в журнал. None — нет модели или профиля."""
        if not self._load():
            return None
        assert self._embedder is not None and self._profile is not None
        t0 = time.perf_counter()
        emb, speech_s = voice_embedding(audio, self._embedder)
        cos = score = None
        if emb is not None:
            cos, score = self._profile.score(emb)
        own_min, alien_max = self._thresholds()
        row: dict[str, Any] = {
            "ts": round(ts, 3),
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "rec_id": rec_id,
            "audio_s": round(len(audio) / SAMPLE_RATE, 2),
            "speech_s": round(speech_s, 2),
            "cos": None if cos is None else round(cos, 4),
            "score": None if score is None else round(score, 3),
            "label": label(score, speech_s, own_min, alien_max),
            "ms": round((time.perf_counter() - t0) * 1000),
            "profile": self._profile.meta.get("built"),
            "emb": None if emb is None else [round(float(v), 5) for v in emb],
        }
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / JOURNAL_FILE).open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
        log.info("Голос: %s, счёт %s, речи %.1f с, %d мс", row["label"],
                 "—" if score is None else f"{score:+.2f}", speech_s, row["ms"])
        return row


# ---------- сборка профиля ----------

def decode(path: Path) -> np.ndarray:
    """Начало звукового файла (MAX_DECODE_S) → 16 кГц моно float32.

    WAV 16 кГц моно 16 бит (записи самого Рупора) читается напрямую. Остальное — через PyAV:
    в установщик он не входит (см. packaging/rupor-core.spec), так что профиль из голосовых
    в ogg собирается из окружения разработки, а в собранном ядре — только из таких WAV."""
    if path.suffix.lower() == ".wav":
        try:
            with wave.open(str(path), "rb") as w:
                if (w.getnchannels(), w.getsampwidth(), w.getframerate()) == (1, 2, SAMPLE_RATE):
                    data = w.readframes(int(MAX_DECODE_S * SAMPLE_RATE))
                    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32768.0
        except wave.Error:
            pass                         # не PCM — пусть разбирает PyAV
    import av

    parts: list[np.ndarray] = []
    n = 0
    with av.open(str(path)) as c:
        rs = av.AudioResampler(format="flt", layout="mono", rate=SAMPLE_RATE)
        for frame in c.decode(c.streams.audio[0]):
            for f in rs.resample(frame):
                a = f.to_ndarray().reshape(-1)
                parts.append(a)
                n += a.size
            if n >= MAX_DECODE_S * SAMPLE_RATE:
                break
    return np.concatenate(parts).astype(np.float32) if parts else np.zeros(0, np.float32)


def audio_files(folder: Path, recursive: bool = False) -> list[Path]:
    it = folder.rglob("*") if recursive else folder.iterdir()
    return sorted(p for p in it if p.suffix.lower() in AUDIO_EXT and p.is_file())


def embed_files(files: list[Path], embedder: Embedder, what: str,
                progress: Callable[[str], None], workers: int = 6) -> np.ndarray:
    """Эмбеддинги файлов [n, 192]; файлы без секунды речи и нечитаемые пропускаются
    (нечитаемые — с записью в лог)."""
    def one(p: Path) -> np.ndarray | None:
        try:
            return voice_embedding(decode(p), embedder)[0]
        except Exception as e:
            log.warning("Голос: файл не прочитан (%s): %s", p.name, e)
            return None

    out: list[np.ndarray] = []
    t0 = time.monotonic()
    with ThreadPoolExecutor(workers) as pool:
        for i, e in enumerate(pool.map(one, files), 1):
            if e is not None:
                out.append(e)
            if i % 250 == 0 or i == len(files):
                progress(f"{what}: {i}/{len(files)}, с речью {len(out)}, "
                         f"{time.monotonic() - t0:.0f} с")
    return np.array(out, np.float32) if out else np.zeros((0, 0), np.float32)


def enroll(root: Path, cohort_dir: Path, voices_dir: Path | None = None,
           recordings: list[Path] | None = None, model: Path | None = None,
           exclude: Iterable[str] = (), cohort_limit: int = 1500,
           progress: Callable[[str], None] = print) -> Profile:
    """Собрать профиль голоса в ``root`` (``%APPDATA%\\Rupor\\speaker``).

    ``voices_dir`` — папка голосовых сообщений хозяина; ``recordings`` — файлы записей Рупора
    со статусом ok; ``cohort_dir`` — чужие голоса (опорная выборка, с подпапками; больше
    ``cohort_limit`` — берётся случайная часть); ``model`` — файл модели, копируется в
    ``root`` (не задан — уже лежит там); ``exclude`` — имена файлов голосовых, которые не
    голос хозяина (с расширением или без)."""
    root.mkdir(parents=True, exist_ok=True)
    target = root / MODEL_FILE
    if model is not None and Path(model).resolve() != target.resolve():
        shutil.copyfile(model, target)
    if not target.is_file():
        raise FileNotFoundError(f"speaker model not found: {target}")
    embedder = Embedder(target)
    skip = {Path(x).stem for x in exclude}
    sources: list[np.ndarray] = []
    counts: dict[str, int] = {}
    if voices_dir is not None:
        files = [p for p in audio_files(Path(voices_dir)) if p.stem not in skip]
        e = embed_files(files, embedder, "голосовые", progress)
        sources.append(e)
        counts["voices"] = len(e)
    if recordings:
        e = embed_files(sorted(recordings), embedder, "записи Рупора", progress)
        sources.append(e)
        counts["recordings"] = len(e)
    if not any(len(s) for s in sources):
        # до опорной выборки: её расчёт — самая долгая часть
        raise ValueError("no voice recordings for the profile")
    files = audio_files(Path(cohort_dir), recursive=True)
    if len(files) > cohort_limit:
        random.Random(0).shuffle(files)
        files = sorted(files[:cohort_limit])
    cohort = embed_files(files, embedder, "опорная выборка", progress)
    counts["cohort"] = len(cohort)
    profile = build_profile(sources, cohort, {
        "built": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": counts,
        "model_sha256": hashlib.sha256(target.read_bytes()).hexdigest()[:16],
        "top_n": TOP_N,
    })
    profile.save(root / PROFILE_FILE)
    progress(f"профиль собран: {counts}")
    return profile
