"""GPU не успевает (видеопамять заняли другие программы, 22.11.2026): диктовка, не уложившаяся
в срок, распознаётся заново на запасной модели на CPU, а пока опоздавший проход GPU не
кончился, следующие диктовки сразу идут на CPU. На заглушках, без faster-whisper и GPU."""
from __future__ import annotations

import threading
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from rupor import asr

AUDIO = np.zeros(16000, dtype=np.float32)


class Model:
    def __init__(self, text: str, gate: threading.Event | None = None) -> None:
        self.text, self.gate = text, gate
        self.calls = 0

    def transcribe(self, audio: np.ndarray, **kw: Any) -> tuple[list[Any], Any]:
        self.calls += 1
        if self.gate is not None:
            self.gate.wait(10)
        return [SimpleNamespace(text=self.text, temperature=0.0, avg_logprob=-0.2)], None

    def detect_language(self, audio: np.ndarray) -> tuple[str, float, list[Any]]:
        if self.gate is not None:
            self.gate.wait(10)
        return "ru", 0.99, []


def make(gpu: Model, cpu: Model, monkeypatch: pytest.MonkeyPatch
         ) -> tuple[asr.Recognizer, list[str]]:
    s = SimpleNamespace(dictation_language="auto", language="ru", en_threshold=0.8,
                        beam_size=5, terms=[], model="tiny", model_dir="", device="cuda",
                        compute_type="float16")
    notices: list[str] = []
    rec = asr.Recognizer(s, lambda text, level: notices.append(level))  # type: ignore[arg-type]
    rec._model, rec.device = gpu, "cuda"
    rec.prompt_provider = lambda: "подсказка"

    def spare_load(self: asr.Recognizer) -> None:
        if self._model is None:
            self._model, self.device = cpu, "cpu"
    monkeypatch.setattr(asr.Recognizer, "load", spare_load)
    monkeypatch.setattr(asr, "video_memory_note", lambda: "видеопамять: тест")
    return rec, notices


def test_fast_gpu_stays_on_gpu(monkeypatch: pytest.MonkeyPatch) -> None:
    gpu, cpu = Model("на видеокарте"), Model("на процессоре")
    rec, notices = make(gpu, cpu, monkeypatch)
    assert rec.transcribe(AUDIO, language="ru").text == "на видеокарте"
    assert cpu.calls == 0 and notices == [] and not rec.gpu_stuck


def test_slow_gpu_falls_to_cpu_until_it_finishes(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    gpu, cpu = Model("на видеокарте", release), Model("на процессоре")
    rec, notices = make(gpu, cpu, monkeypatch)
    monkeypatch.setattr(asr, "GPU_DEADLINE_BASE_S", 0.05)
    monkeypatch.setattr(asr, "GPU_DEADLINE_PER_AUDIO", 0.0)

    assert rec.transcribe(AUDIO, language="ru").text == "на процессоре"
    assert notices == ["warn"] and rec.gpu_stuck
    # опоздавший проход ещё идёт — следующая диктовка сразу на CPU, без нового прохода GPU
    assert rec.transcribe(AUDIO, language="ru").text == "на процессоре"
    assert gpu.calls == 1 and cpu.calls == 2 and notices == ["warn"]
    with pytest.raises(asr.GpuSlow):
        rec.detect_language(AUDIO)

    release.set()
    for _ in range(200):
        if not rec.gpu_stuck:
            break
        threading.Event().wait(0.01)
    assert not rec.gpu_stuck
    assert rec.transcribe(AUDIO, language="ru").text == "на видеокарте"
    assert rec._spare is None                    # GPU снова успевает — запасная не нужна


def test_slow_language_detection_gives_up(monkeypatch: pytest.MonkeyPatch) -> None:
    release = threading.Event()
    gpu, cpu = Model("на видеокарте", release), Model("на процессоре")
    rec, notices = make(gpu, cpu, monkeypatch)
    monkeypatch.setattr(asr, "DETECT_DEADLINE_S", 0.05)
    with pytest.raises(asr.GpuSlow):
        rec.detect_language(AUDIO)
    assert notices == ["warn"] and rec.gpu_stuck
    assert rec.transcribe(AUDIO).text == "на процессоре"   # язык определит запасная
    release.set()


def test_gpu_error_in_time_is_raised_to_the_old_fallback(monkeypatch: pytest.MonkeyPatch
                                                          ) -> None:
    class Broken(Model):
        def transcribe(self, audio: np.ndarray, **kw: Any) -> tuple[list[Any], Any]:
            raise RuntimeError("CUDA failed with error out of memory")
    rec, notices = make(Broken("x"), Model("на процессоре"), monkeypatch)
    unloaded: list[bool] = []
    monkeypatch.setattr(rec, "unload", lambda: (unloaded.append(True),
                                                setattr(rec, "_model", None)))
    assert rec.transcribe(AUDIO, language="ru").text == "на процессоре"
    assert unloaded == [True] and rec._force_cpu and notices == ["warn"]


def test_deadline_is_generous() -> None:
    # худший обычный проход в журнале — 5,3 с на 59,5 с аудио, 3 с на 2,7 с
    assert asr.gpu_deadline(59.5) > 3 * 5.3
    assert asr.gpu_deadline(2.7) > 1.5 * 3.0
