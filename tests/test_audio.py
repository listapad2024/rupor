"""Рекордер без настоящего микрофона: поток PortAudio подменён."""
from __future__ import annotations

import types
from typing import Any

import numpy as np
import pytest

from rupor import audio


class FakeStream:
    created: list["FakeStream"] = []

    def __init__(self, **kw: Any) -> None:
        self.callback = kw["callback"]
        self.device = kw.get("device")
        self.active = False
        self.closed = False
        FakeStream.created.append(self)

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def close(self) -> None:
        self.closed = True

    def feed(self, seconds: float) -> None:
        for _ in range(int(seconds * audio.SAMPLE_RATE / audio.BLOCK)):
            self.callback(np.full((audio.BLOCK, 1), 0.1, np.float32), audio.BLOCK, None, None)


@pytest.fixture
def fake_sd(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeStream.created = []
    monkeypatch.setattr(audio.sd, "InputStream", FakeStream)
    monkeypatch.setattr(audio.sd, "query_devices", lambda dev=None: {"name": "Тестовый микрофон"})
    monkeypatch.setattr(audio, "resolve_device", lambda spec: None)


def test_silent_stream_is_reopened_on_start(fake_sd: None,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    """Гарнитуру переподключили: поток больше не зовёт callback — start() открывает новый,
    а не пишет «в пустоту» до перезапуска ядра."""
    clock = {"t": 1000.0}
    monkeypatch.setattr(audio, "time", types.SimpleNamespace(monotonic=lambda: clock["t"]))
    rec = audio.Recorder()
    rec.open()
    first = FakeStream.created[0]
    first.feed(0.5)
    rec.start()
    assert len(FakeStream.created) == 1          # живой поток не трогаем
    first.feed(1.0)
    assert len(rec.stop()) >= audio.SAMPLE_RATE  # пре-буфер + секунда записи
    clock["t"] += 5.0                            # 5 с без callback — поток умер
    assert rec.stale()
    rec.start()
    assert len(FakeStream.created) == 2 and first.closed
    FakeStream.created[1].feed(1.0)
    got = rec.stop()
    assert audio.SAMPLE_RATE * 0.9 <= len(got) <= audio.SAMPLE_RATE * 1.1  # без старого пре-буфера


def test_inactive_stream_is_stale(fake_sd: None) -> None:
    rec = audio.Recorder()
    rec.open()
    assert not rec.stale()
    FakeStream.created[0].active = False          # PortAudio остановил поток сам
    assert rec.stale()


def test_stop_with_tail_records_after_release(fake_sd: None) -> None:
    """Отпустили клавишу на последнем слоге: stop(tail_s) дописывает ещё tail_s секунд,
    которые приходят в callback уже после вызова stop."""
    import threading
    rec = audio.Recorder(preroll_ms=0)
    rec.open()
    stream = FakeStream.created[0]
    rec.start()
    stream.feed(1.0)
    before = len(rec.snapshot())
    feeder = threading.Timer(0.05, stream.feed, (0.5,))
    feeder.start()
    got = rec.stop(tail_s=0.3)
    feeder.join()
    assert len(got) >= before + 0.3 * audio.SAMPLE_RATE
    assert not rec.recording


def test_stop_with_tail_does_not_hang_on_silent_stream(fake_sd: None) -> None:
    """Поток перестал звать callback: stop(tail_s) отдаёт записанное через tail + запас."""
    import time as _time
    rec = audio.Recorder(preroll_ms=0)
    rec.open()
    rec.start()
    FakeStream.created[0].feed(0.5)
    t0 = _time.monotonic()
    got = rec.stop(tail_s=0.1)
    assert _time.monotonic() - t0 < 0.1 + audio.TAIL_GRACE_S + 0.2
    assert len(got) >= 0.4 * audio.SAMPLE_RATE
