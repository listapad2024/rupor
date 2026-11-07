"""Захват микрофона: 16 кГц моно float32.

Поток держится открытым постоянно и пишет кольцевой пре-буфер, поэтому первое слово
не обрезается, даже если начать говорить одновременно с нажатием клавиши.
Уровень сигнала (``on_level``) отдаётся во время записи и в режиме ``monitor``
(индикатор микрофона в мастере первого запуска).

Отвалившийся поток: если USB/Bluetooth-гарнитуру переподключили или сменился её профиль,
поток MME перестаёт вызывать callback, но ошибки не даёт. Поэтому время последнего
callback запоминается, и ``start()`` переоткрывает поток, который молчит дольше STALE_S.
"""
from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Any, Callable

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000  # то же, что asr.SAMPLE_RATE (без импорта asr — он тянет faster-whisper)

log = logging.getLogger(__name__)

BLOCK = 512  # 32 мс
STALE_S = 1.0  # callback раз в 32 мс; тишина дольше секунды — поток мёртв
_MME = 0     # индекс host API MME: сам ресемплирует в 16 кГц (WASAPI shared — нет)


def list_input_devices() -> list[tuple[int, str]]:
    out = []
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0 and d["hostapi"] == _MME:
            out.append((i, str(d["name"])))
    return out


def default_input_index() -> int | None:
    """Системный микрофон по умолчанию (в MME)."""
    try:
        idx = int(sd.query_hostapis(_MME)["default_input_device"])
    except Exception:
        return None
    return idx if idx >= 0 else None


def resolve_device(spec: str | None) -> int | None:
    """Имя (подстрока, без учёта регистра) или индекс → индекс устройства; None — системное.
    При поиске по имени сначала смотрим устройства MME."""
    if spec is None or spec == "":
        return None
    if isinstance(spec, int) or str(spec).isdigit():
        return int(spec)
    name = str(spec).lower()
    devs = list(enumerate(sd.query_devices()))
    for mme_first in (True, False):
        for i, d in devs:
            if (d["max_input_channels"] > 0 and (d["hostapi"] == _MME) == mme_first
                    and name in str(d["name"]).lower()):
                return i
    log.warning("Микрофон %r не найден — беру системный по умолчанию", spec)
    return None


class Recorder:
    def __init__(self, device: str | None = None, preroll_ms: int = 250,
                 on_level: Callable[[float], None] | None = None) -> None:
        self._spec = device
        self._pre: collections.deque[np.ndarray] = collections.deque(
            maxlen=max(1, preroll_ms * SAMPLE_RATE // 1000 // BLOCK))
        self._chunks: list[np.ndarray] = []
        self._recording = False
        self._lock = threading.Lock()
        self._on_level = on_level
        self._stream: Any = None
        self._last_cb = 0.0   # monotonic последнего callback (или открытия потока)
        self.monitor = False  # отдавать уровень и без записи

    def open(self) -> None:
        if self._stream is not None:
            return
        dev = resolve_device(self._spec)
        stream = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                                blocksize=BLOCK, device=dev, callback=self._cb)
        self._last_cb = time.monotonic()
        try:
            stream.start()
        except BaseException:
            # не запустился (микрофон выдернули, занят) — закрыть, иначе поток PortAudio
            # останется висеть и держать устройство
            try:
                stream.close()
            except Exception as e:
                log.debug("закрытие незапущенного потока: %s", e)
            raise
        self._stream = stream
        log.info("Микрофон открыт: %s", sd.query_devices(stream.device)["name"])

    def stale(self) -> bool:
        """Поток открыт, но не присылает звук (устройство пропало или переподключено)."""
        st = self._stream
        if st is None:
            return False
        try:
            active = bool(st.active)
        except Exception:
            active = False
        return not active or time.monotonic() - self._last_cb > STALE_S

    def close(self) -> None:
        if self._stream is not None:
            stream, self._stream = self._stream, None
            try:
                stream.stop()
            finally:
                # stop() бросил (устройство пропало) — поток всё равно закрываем
                stream.close()

    @property
    def is_open(self) -> bool:
        return self._stream is not None

    def _cb(self, indata: np.ndarray, frames: int, _t: Any, status: Any) -> None:
        self._last_cb = time.monotonic()
        if status:
            log.debug("audio status: %s", status)
        block = indata[:, 0].copy()
        with self._lock:
            if self._recording:
                self._chunks.append(block)
            else:
                self._pre.append(block)
        if (self._recording or self.monitor) and self._on_level:
            self._on_level(float(np.sqrt(np.mean(block * block))))

    def start(self) -> None:
        if self.stale():
            log.warning("Микрофон молчит %.1f с — переоткрываю поток",
                        time.monotonic() - self._last_cb)
            try:
                self.close()
            except Exception:
                log.exception("Не удалось закрыть молчащий поток микрофона")
                self._stream = None
            with self._lock:
                self._pre.clear()  # пре-буфер устарел
        self.open()
        with self._lock:
            self._chunks = list(self._pre)
            self._pre.clear()
            self._recording = True

    def snapshot(self) -> np.ndarray:
        """Записанное на текущий момент (для раннего определения языка)."""
        with self._lock:
            return np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)

    def stop(self) -> np.ndarray:
        with self._lock:
            self._recording = False
            audio = np.concatenate(self._chunks) if self._chunks else np.zeros(0, np.float32)
            self._chunks = []
        return audio

    @property
    def recording(self) -> bool:
        return self._recording
