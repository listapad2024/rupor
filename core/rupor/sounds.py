"""Тихие звуки начала/конца/отмены записи (генерируются в wav при первом запуске)."""
from __future__ import annotations

import logging
import wave
import winsound
from pathlib import Path

import numpy as np

from .config import app_dir

log = logging.getLogger(__name__)
RATE = 44100
_TONES: dict[str, list[tuple[float, float]]] = {
    "start": [(660, 0.06), (880, 0.08)],
    "stop": [(880, 0.06), (660, 0.08)],
    "cancel": [(440, 0.12)],
    "error": [(330, 0.08), (262, 0.12)],
}


def _render(path: Path, tones: list[tuple[float, float]]) -> None:
    parts = []
    for freq, dur in tones:
        t = np.arange(int(RATE * dur)) / RATE
        env = np.minimum(1, np.minimum(t / 0.008, (dur - t) / 0.02))  # мягкие фронты
        parts.append(0.18 * np.sin(2 * np.pi * freq * t) * env)
    pcm = (np.concatenate(parts) * 32767).astype("<i2")
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1)
        f.setsampwidth(2)
        f.setframerate(RATE)
        f.writeframes(pcm.tobytes())


def _path(name: str) -> Path:
    d = app_dir() / "sounds"
    d.mkdir(exist_ok=True)
    p = d / f"{name}.wav"
    if not p.exists():
        _render(p, _TONES[name])
    return p


def prepare() -> None:
    for n in _TONES:
        _path(n)


def play(name: str) -> None:
    """Звук — не главное: никакая ошибка здесь (диск полон, папка занята, нет устройства
    вывода) не должна сорвать запись или убить поток, который его вызвал."""
    try:
        winsound.PlaySound(str(_path(name)), winsound.SND_FILENAME | winsound.SND_ASYNC
                           | winsound.SND_NODEFAULT)
    except (RuntimeError, OSError, wave.Error) as e:
        log.debug("звук %s: %s", name, e)
