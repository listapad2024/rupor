"""Сквозной тест движка без микрофона и без хука: запись подменяется wav-файлом,
жесты подаются напрямую, результат печатается в тестовое окно (tools/target_window.py).

python tools/e2e.py samples/ru1.wav [type|clipboard]

Звук компьютера не трогает: duck_mode "off" и приглушение без Core Audio (NullBackend) с
duck.json во временной папке — %APPDATA%\\Rupor\\duck.json запущенного Рупора не читается
и не переписывается.
"""
from __future__ import annotations

import logging
import sys
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, "core")
sys.path.insert(0, "tools")
from rupor import asr, config, duck, engine  # noqa: E402
from rupor.hotkeys import Action, Kind  # noqa: E402
from try_inject import guard, open_target  # noqa: E402

_real_deliver = engine.deliver


def _guarded_deliver(*a, **kw):  # type: ignore[no-untyped-def]
    guard()
    return _real_deliver(*a, **kw)


engine.deliver = _guarded_deliver

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")


class FakeRecorder:
    """Отдаёт wav «в реальном времени»: snapshot() — сколько «наговорено» к этому моменту."""

    def __init__(self, audio: np.ndarray) -> None:
        self.audio = audio
        self.t0 = 0.0
        self.recording = False

    def open(self) -> None: ...
    def close(self) -> None: ...

    def start(self) -> None:
        self.t0 = time.monotonic()
        self.recording = True

    def _upto(self) -> np.ndarray:
        n = int((time.monotonic() - self.t0) * asr.SAMPLE_RATE)
        return self.audio[:n]

    def snapshot(self) -> np.ndarray:
        return self._upto()

    def stop(self, tail_s: float = 0.0) -> np.ndarray:
        self.recording = False
        return self._upto()


def main() -> int:
    wav = sys.argv[1]
    s = config.Settings(sounds=False, insert_mode=sys.argv[2] if len(sys.argv) > 2 else "type",
                        duck_mode="off")
    audio = asr.load_wav(wav)
    ducker = duck.Ducker(path=Path(tempfile.mkdtemp(prefix="rupor_e2e_")) / duck.STATE_FILE,
                         backend_factory=duck.NullBackend)
    e = engine.Engine(s, ducker=ducker)
    e.recorder = FakeRecorder(audio)  # type: ignore[assignment]
    results: list[dict] = []  # type: ignore[type-arg]
    done = threading.Event()
    e.bus.subscribe(lambda ev, d: (results.append(d), done.set()) if ev == "result" else None)
    ready = threading.Event()
    e.bus.subscribe(lambda ev, d: ready.set() if ev == "ready" else None)
    e.start(hook=False)
    ready.wait(120)

    out = Path(tempfile.gettempdir()) / "rupor_target.txt"
    out.write_text("", encoding="utf-8")
    proc = open_target(out)
    try:
        e._on_actions([Action(Kind.START)])
        time.sleep(len(audio) / asr.SAMPLE_RATE + 0.2)  # «говорим» всю фразу
        e._on_actions([Action(Kind.STOP)])
        done.wait(30)
        time.sleep(1.0)
        got = out.read_text(encoding="utf-8")
        r = results[0]
        print(f"\nзадержка отпускание→вставка: {r['latency_ms']:.0f} мс "
              f"(ASR {r['asr_ms']:.0f} мс, аудио {r['audio_s']:.1f} с, язык {r['language']})")
        print("в окне:", got)
        ok = got.strip() == r["text"].strip()
        print("совпадает с распознанным:", ok)
        return 0 if ok else 2
    finally:
        proc.kill()
        e.shutdown()


if __name__ == "__main__":
    sys.exit(main())
