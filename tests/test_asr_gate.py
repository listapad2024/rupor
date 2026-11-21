"""Одна модель на диктовку и встречу (asr.ModelGate) и распознавание кусков встречи
(Recognizer.transcribe_background) — на заглушке модели, без faster-whisper и GPU."""
from __future__ import annotations

import threading
import time
from types import SimpleNamespace
from typing import Any

import numpy as np
import pytest

from rupor import asr

AUDIO = np.zeros(16000, dtype=np.float32)


# ---------- ModelGate ----------

def test_high_lane_is_a_plain_lock() -> None:
    g = asr.ModelGate()
    with g:
        assert g.held
    assert not g.held
    with g, g.background(timeout=0.05) as ok:      # занято — низкий приоритет не берёт
        assert ok is False
    with g.background(timeout=0.5) as ok:
        assert ok is True and g.held
    assert not g.held


def test_background_waits_for_yield_to_and_times_out() -> None:
    g = asr.ModelGate()
    busy = [True]
    t0 = time.monotonic()
    with g.background(lambda: busy[0], timeout=0.2) as ok:
        assert ok is False
    assert 0.15 <= time.monotonic() - t0 < 1.0
    # условие перепроверяется само (раз в GATE_POLL_S): снялось — модель взята
    threading.Timer(0.1, lambda: busy.__setitem__(0, False)).start()
    with g.background(lambda: busy[0], timeout=2.0) as ok:
        assert ok is True


def test_yield_to_error_means_yield() -> None:
    g = asr.ModelGate()

    def boom() -> bool:
        raise RuntimeError("x")

    with g.background(boom, timeout=0.1) as ok:
        assert ok is False


def test_dictation_waits_only_for_the_chunk_in_flight() -> None:
    """Кусок встречи держит модель — диктовка ждёт только его, следующий кусок встречи
    не обгоняет ждущую диктовку."""
    g = asr.ModelGate()
    order: list[str] = []
    started = threading.Event()

    def meeting() -> None:
        for _ in range(3):
            with g.background(timeout=5) as ok:
                assert ok
                order.append("m")
                started.set()
                time.sleep(0.15)

    th = threading.Thread(target=meeting)
    th.start()
    started.wait(2)
    t0 = time.monotonic()
    with g:
        waited = time.monotonic() - t0
        order.append("d")
    th.join(5)
    assert waited < 0.3
    assert order[:2] == ["m", "d"]               # диктовка — сразу после первого куска


def test_background_yields_to_waiting_high_lane() -> None:
    """Модель освободилась, а её ждут и диктовка, и следующий кусок встречи — первой
    берёт диктовка."""
    g = asr.ModelGate()
    order: list[str] = []

    def dictation() -> None:
        with g:
            order.append("d")
            time.sleep(0.05)

    def meeting() -> None:
        with g.background(timeout=3) as ok:
            if ok:
                order.append("m")

    with g.background(timeout=1) as ok:          # кусок встречи в работе
        assert ok
        d = threading.Thread(target=dictation)
        d.start()
        time.sleep(0.05)
        m = threading.Thread(target=meeting)
        m.start()
        time.sleep(0.15)                          # оба ждут
    d.join(3)
    m.join(3)
    assert order == ["d", "m"]


# ---------- transcribe_background ----------

class FakeModel:
    def __init__(self, text: str = " Hello everyone. ", lang: str = "en", prob: float = 0.97,
                 temps: tuple[float, ...] = (0.0,), logprob: float = -0.3) -> None:
        self.text, self.lang, self.prob = text, lang, prob
        self.temps, self.logprob = temps, logprob
        self.calls: list[dict[str, Any]] = []

    def transcribe(self, audio: np.ndarray, **kw: Any) -> tuple[list[Any], Any]:
        self.calls.append(kw)
        segs = [SimpleNamespace(text=self.text, temperature=t, avg_logprob=self.logprob)
                for t in self.temps]
        return segs, SimpleNamespace(language=self.lang, language_probability=self.prob)


def make_rec(model: FakeModel | None) -> asr.Recognizer:
    s = SimpleNamespace(dictation_language="auto", language="ru", en_threshold=0.8,
                        beam_size=5, terms=["ClickHouse"], model="tiny", model_dir="",
                        device="cpu", compute_type="int8")
    rec = asr.Recognizer(s)  # type: ignore[arg-type]
    rec._model = model
    rec.device = "cuda" if model is not None else None

    def no_dictation_prompt() -> str:
        raise AssertionError("подсказка диктовки во встрече не нужна")

    rec.prompt_provider = no_dictation_prompt
    return rec


def test_background_decode_options() -> None:
    m = FakeModel()
    rec = make_rec(m)
    r = rec.transcribe_background(AUDIO, language=None, prompt="Daily standup.",
                                  yield_to=lambda: False, timeout=1)
    assert r == asr.ChunkResult("Hello everyone.", "en", 0.97, 1.0, r.elapsed_ms)
    kw = m.calls[0]
    assert kw["language"] is None and kw["beam_size"] == 1
    assert kw["temperature"] == (0.0, 0.2, 0.4)
    assert kw["vad_filter"] is False and kw["condition_on_previous_text"] is False
    assert kw["without_timestamps"] is True and kw["initial_prompt"] == "Daily standup."
    assert "chunk_length" not in kw              # общий FeatureExtractor не трогаем
    r2 = rec.transcribe_background(AUDIO, language="de", prompt=None, yield_to=None)
    assert r2 is not None and r2.language == "de" and r2.language_prob == 1.0
    assert m.calls[1]["language"] == "de"


def test_background_never_loads_or_falls_back() -> None:
    rec = make_rec(None)
    loads: list[int] = []
    rec.load = lambda: loads.append(1)  # type: ignore[method-assign]
    assert rec.transcribe_background(AUDIO, language=None, prompt=None,
                                     yield_to=None) is None
    assert loads == [] and not rec.loaded

    class Broken(FakeModel):
        def transcribe(self, audio: np.ndarray, **kw: Any) -> tuple[list[Any], Any]:
            raise RuntimeError("CUDA out of memory")

    rec2 = make_rec(Broken())
    with pytest.raises(RuntimeError):
        rec2.transcribe_background(AUDIO, language=None, prompt=None, yield_to=None)
    assert rec2.device == "cuda" and rec2.loaded and not rec2._force_cpu  # диктовка — как была
    assert not rec2._lock.held                                           # замок отпущен


def test_background_yields_to_dictation() -> None:
    m = FakeModel()
    rec = make_rec(m)
    t0 = time.monotonic()
    assert rec.transcribe_background(AUDIO, language=None, prompt=None,
                                     yield_to=lambda: True, timeout=0.2) is None
    assert m.calls == [] and time.monotonic() - t0 >= 0.15
    with rec._lock:                              # диктовка распознаёт — встреча ждёт
        assert rec.transcribe_background(AUDIO, language=None, prompt=None,
                                         yield_to=None, timeout=0.1) is None


@pytest.mark.parametrize("model,why", [
    (FakeModel(text=" Thank you for watching. "), "фраза субтитров"),
    (FakeModel(text=" 你好 "), "чужие символы"),
    (FakeModel(temps=(0.4,), logprob=-2.0), "угадывание"),
])
def test_background_drops_garbage(model: FakeModel, why: str) -> None:
    r = make_rec(model).transcribe_background(AUDIO, language=None, prompt=None,
                                              yield_to=None)
    assert r is not None and r.text == "" and r.audio_s == 1.0, why
    assert len(model.calls) == 1                 # без второго прохода


def test_unsure_but_not_guessing_is_kept() -> None:
    r = make_rec(FakeModel(temps=(0.2,), logprob=-1.2)).transcribe_background(
        AUDIO, language=None, prompt=None, yield_to=None)
    assert r is not None and r.text == "Hello everyone."


def test_background_interim_pass_options() -> None:
    m = FakeModel()
    rec = make_rec(m)
    r = rec.transcribe_background(AUDIO, language="en", prompt=None, yield_to=None,
                                  beam_size=1, temperature=0.0)
    assert r is not None and r.text == "Hello everyone."
    assert m.calls[0]["temperature"] == 0.0 and m.calls[0]["beam_size"] == 1
    # одна температура (промежуточный проход): неуверенный сегмент — угадывание, без повтора
    m2 = FakeModel(temps=(0.0,), logprob=-1.8)
    r2 = make_rec(m2).transcribe_background(AUDIO, language="en", prompt=None, yield_to=None,
                                            temperature=0.0)
    assert r2 is not None and r2.text == "" and len(m2.calls) == 1
    # тот же сегмент, когда повторы до 0.4 ещё возможны, — не угадывание
    r3 = make_rec(FakeModel(temps=(0.0,), logprob=-1.8)).transcribe_background(
        AUDIO, language="en", prompt=None, yield_to=None)
    assert r3 is not None and r3.text == "Hello everyone."
    # последний проход фразы — beam 5 с повторами
    rec.transcribe_background(AUDIO, language="en", prompt=None, yield_to=None, beam_size=5)
    assert m.calls[-1]["beam_size"] == 5 and m.calls[-1]["temperature"] == (0.0, 0.2, 0.4)


def test_background_keeps_own_script_languages() -> None:
    r = make_rec(FakeModel(text=" 你好，大家 ", lang="zh")).transcribe_background(
        AUDIO, language=None, prompt=None, yield_to=None)
    assert r is not None and r.text == "你好，大家" and r.language == "zh"
    assert asr.garbage_reason("你好", "zh") is None and asr.garbage_reason("안녕", "ko") is None
    assert asr.garbage_reason("你好", "en") == "чужие символы"
    assert asr.garbage_reason("你好") == "чужие символы"           # диктовка — как была
    assert asr.garbage_reason("Thank you for watching", "zh") == "фраза субтитров"
