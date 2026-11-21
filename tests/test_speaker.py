"""Счёт «свой / чужой» по голосу (rupor.speaker): признаки, профиль и AS-norm, метки, журнал
теневого режима, сборка профиля. Без модели и без звука пользователя: эмбеддинги подменяются."""
from __future__ import annotations

import json
import logging
import threading
import time
import wave
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from rupor import speaker
from rupor.speaker import SAMPLE_RATE, Profile, Shadow

DIM = 192


def _vecs(rng: np.random.Generator, n: int, around: np.ndarray | None = None,
          spread: float = 1.0) -> np.ndarray:
    v = rng.standard_normal((n, DIM)).astype(np.float32) * spread
    return speaker.unit(v if around is None else v + around)


def _profile(rng: np.random.Generator, own: np.ndarray) -> Profile:
    return speaker.build_profile([_vecs(rng, 40, own * 12)], _vecs(rng, 400),
                                 {"built": "2026-11-21 18:00:00"})


# ---------- признаки ----------

def test_fbank_layout_and_reference_values() -> None:
    """Кадры и значения закреплены: расчёт сверен с kaldi-native-fbank из sherpa-onnx
    (косинус эмбеддингов 1,000000), любое расхождение сдвинет счета."""
    x = (0.1 * np.random.default_rng(0).standard_normal(SAMPLE_RATE)).astype(np.float32)
    f = speaker.fbank(x)
    assert f.shape == (100, 80) and f.dtype == np.float32
    assert float(f.mean()) == pytest.approx(-0.2766, abs=2e-3)
    assert [float(f[0, 0]), float(f[50, 40]), float(f[99, 79])] == pytest.approx(
        [-7.3403, -0.2868, 3.6084], abs=2e-3)
    # число кадров — как при snip_edges=False: (n + 80) // 160, края отражаются
    assert speaker.fbank(np.ones(1601, np.float32)).shape == (10, 80)
    assert speaker.fbank(np.full(239, 0.1, np.float32)).shape == (1, 80)
    assert speaker.fbank(np.zeros(0, np.float32)).shape == (0, 80)


def test_fbank_sine_and_silence() -> None:
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    f = speaker.fbank((0.5 * np.sin(2 * np.pi * 1000 * t)).astype(np.float32))
    assert int(f[50].argmax()) == 27                     # полоса вокруг 1 кГц
    silence = speaker.fbank(np.zeros(1600, np.float32))
    assert float(silence.max()) == pytest.approx(float(np.log(np.finfo(np.float32).eps)))


def test_voice_embedding_takes_speech_from_the_start(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, int] = {"take": SAMPLE_RATE * 40}

    def fake_speech(a: np.ndarray) -> np.ndarray:
        seen["vad"] = len(a)
        return a[:seen["take"]]

    class FakeEmbedder:
        def embed(self, speech: np.ndarray) -> np.ndarray:
            seen["embedded"] = len(speech)
            return np.ones(DIM, np.float32)

    monkeypatch.setattr(speaker, "speech_only", fake_speech)
    audio = np.zeros(SAMPLE_RATE * 90, np.float32)
    emb, speech_s = speaker.voice_embedding(audio, FakeEmbedder())       # type: ignore[arg-type]
    # VAD видит первую минуту записи, в модель идут первые 15 с речи
    assert seen["vad"] == SAMPLE_RATE * 60 and seen["embedded"] == SAMPLE_RATE * 15
    assert emb is not None and speech_s == 15.0
    seen["take"] = SAMPLE_RATE // 2                                      # полсекунды речи
    del seen["embedded"]
    emb, speech_s = speaker.voice_embedding(audio, FakeEmbedder())       # type: ignore[arg-type]
    assert emb is None and speech_s == 0.5 and "embedded" not in seen


# ---------- профиль и счёт ----------

def test_score_is_centered_cosine_with_as_norm() -> None:
    rng = np.random.default_rng(1)
    own = _vecs(rng, 1)[0]
    cohort_raw = _vecs(rng, 400)
    src = _vecs(rng, 30, own * 12)
    p = speaker.build_profile([src], cohort_raw)
    x_raw = _vecs(rng, 1, own * 12)[0]
    cos, score = p.score(x_raw)
    # та же формула, что в замере (analyze.Scorer)
    center = cohort_raw.mean(0)
    cohort = speaker.unit(cohort_raw - center)
    prof = speaker.unit(speaker.unit(src - center).mean(0))
    x = speaker.unit(x_raw - center)
    s = float(x @ prof)

    def stats(v: np.ndarray) -> tuple[float, float]:
        top = np.sort(cohort @ v)[-300:]
        return float(top.mean()), float(top.std())

    (mp, sp), (mx, sx) = stats(prof), stats(x)
    assert cos == pytest.approx(s, abs=1e-5)
    assert score == pytest.approx(0.5 * ((s - mp) / sp + (s - mx) / sx), rel=1e-4)
    # свой голос — высоко, случайный чужой — около нуля
    alien = p.score(_vecs(rng, 1)[0])[1]
    assert score > 5.5 and alien < 3.0


def test_profile_sources_have_equal_weight() -> None:
    """Телефон и микрофон компьютера входят в профиль поровну, сколько бы файлов в каждом
    ни было (как в замере: сумма нормированных центроидов)."""
    rng = np.random.default_rng(2)
    a, b = _vecs(rng, 1)[0], _vecs(rng, 1)[0]
    cohort = _vecs(rng, 400)
    many, few = _vecs(rng, 500, a * 20), _vecs(rng, 5, b * 20)
    p = speaker.build_profile([many, few], cohort)
    center = cohort.mean(0)
    ca = speaker.unit(speaker.unit(many - center).mean(0))
    cb = speaker.unit(speaker.unit(few - center).mean(0))
    assert float(p.voice @ ca) == pytest.approx(float(p.voice @ cb), abs=1e-3)
    assert np.allclose(p.voice, speaker.unit(ca + cb), atol=1e-5)


def test_build_profile_needs_data() -> None:
    rng = np.random.default_rng(3)
    with pytest.raises(ValueError, match="cohort too small"):
        speaker.build_profile([_vecs(rng, 5)], _vecs(rng, 299))
    with pytest.raises(ValueError, match="no voice recordings"):
        speaker.build_profile([np.zeros((0, DIM), np.float32)], _vecs(rng, 300))


def test_profile_roundtrip(tmp_path: Path) -> None:
    rng = np.random.default_rng(4)
    p = _profile(rng, _vecs(rng, 1)[0])
    p.save(tmp_path / "speaker" / "profile.npz")
    q = Profile.load(tmp_path / "speaker" / "profile.npz")
    assert q.meta == {"built": "2026-11-21 18:00:00"}
    x = _vecs(rng, 1)[0]
    assert q.score(x) == pytest.approx(p.score(x))
    assert not list((tmp_path / "speaker").glob("*.part"))


@pytest.mark.parametrize(("score", "speech_s", "want"), [
    (9.0, 4.0, "own"), (5.5, 4.0, "own"), (5.49, 4.0, "unsure"), (3.0, 4.0, "unsure"),
    (2.99, 4.0, "alien"), (-1.0, 4.0, "alien"), (9.0, 1.49, "short"), (None, 0.4, "short"),
])
def test_label(score: float | None, speech_s: float, want: str) -> None:
    assert speaker.label(score, speech_s, 5.5, 3.0) == want
    assert want in speaker.LABELS


# ---------- теневой подсчёт ----------

class _Setup:
    def __init__(self, root: Path, rng: np.random.Generator) -> None:
        self.root = root
        self.own = _vecs(rng, 1)[0]
        self.rng = rng
        root.mkdir(parents=True, exist_ok=True)
        (root / speaker.MODEL_FILE).write_bytes(b"not a real model")
        _profile(rng, self.own).save(root / speaker.PROFILE_FILE)
        self.next: tuple[np.ndarray | None, float] = (_vecs(rng, 1, self.own * 12)[0], 4.2)

    def embed(self, audio: np.ndarray, embedder: speaker.Embedder) -> tuple[np.ndarray | None, float]:
        return self.next

    def rows(self) -> list[dict[str, Any]]:
        p = self.root / speaker.JOURNAL_FILE
        if not p.exists():
            return []
        return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines()]


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> _Setup:
    st = _Setup(tmp_path / "speaker", np.random.default_rng(5))
    monkeypatch.setattr(speaker, "voice_embedding", st.embed)
    return st


def test_shadow_writes_journal_row(setup: _Setup) -> None:
    sh = Shadow(setup.root, lambda: (5.5, 3.0))
    audio = np.zeros(SAMPLE_RATE * 6, np.float32)
    row = sh.score(audio, "20261121-180000-001", 1_763_000_000.0)
    assert row is not None and row["label"] == "own" and row["score"] > 5.5
    assert set(row) == {"ts", "time", "rec_id", "audio_s", "speech_s", "cos", "score", "label",
                        "ms", "profile", "emb"}
    assert row["rec_id"] == "20261121-180000-001" and row["audio_s"] == 6.0
    assert row["speech_s"] == 4.2 and row["profile"] == "2026-11-21 18:00:00"
    assert len(row["emb"]) == DIM
    # чужой голос и короткая фраза — тоже строки журнала
    setup.next = (_vecs(setup.rng, 1)[0], 3.0)
    assert sh.score(audio, None, 1_763_000_010.0)["label"] == "alien"        # type: ignore[index]
    setup.next = (None, 0.4)
    short = sh.score(audio, None, 1_763_000_020.0)
    assert short is not None and short["label"] == "short"
    assert short["score"] is None and short["cos"] is None and short["emb"] is None
    rows = setup.rows()
    assert [r["label"] for r in rows] == ["own", "alien", "short"]
    # по эмбеддингу из журнала счёт пересчитывается без звука
    p = Profile.load(setup.root / speaker.PROFILE_FILE)
    assert p.score(np.array(rows[0]["emb"], np.float32))[1] == pytest.approx(rows[0]["score"], abs=0.01)


def test_shadow_thresholds_are_read_per_phrase(setup: _Setup) -> None:
    limits = [5.5, 3.0]
    sh = Shadow(setup.root, lambda: (limits[0], limits[1]))
    audio = np.zeros(SAMPLE_RATE * 3, np.float32)
    assert sh.score(audio, None, 1.0)["label"] == "own"                       # type: ignore[index]
    limits[0] = 40.0
    assert sh.score(audio, None, 2.0)["label"] == "unsure"                    # type: ignore[index]


def test_shadow_without_profile_logs_once_and_recovers(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    root = tmp_path / "speaker"
    sh = Shadow(root, lambda: (5.5, 3.0))
    audio = np.zeros(SAMPLE_RATE * 3, np.float32)
    with caplog.at_level(logging.WARNING, logger="rupor.speaker"):
        assert sh.score(audio, None, 1.0) is None and sh.score(audio, None, 2.0) is None
    assert [r.levelname for r in caplog.records] == ["WARNING"]
    assert "нет модели или профиля" in caplog.text
    assert not (root / speaker.JOURNAL_FILE).exists()
    # профиль собрали — следующая фраза считается без перезапуска
    st = _Setup(root, np.random.default_rng(6))
    monkeypatch.setattr(speaker, "voice_embedding", st.embed)
    assert sh.score(audio, None, 3.0)["label"] == "own"                       # type: ignore[index]


def test_shadow_reloads_changed_profile(setup: _Setup) -> None:
    sh = Shadow(setup.root, lambda: (5.5, 3.0))
    audio = np.zeros(SAMPLE_RATE * 3, np.float32)
    assert sh.score(audio, None, 1.0)["label"] == "own"                       # type: ignore[index]
    other = _vecs(setup.rng, 1)[0]
    speaker.build_profile([_vecs(setup.rng, 40, other * 12)], _vecs(setup.rng, 410),
                          {"built": "later"}).save(setup.root / speaker.PROFILE_FILE)
    row = sh.score(audio, None, 2.0)
    assert row is not None and row["profile"] == "later" and row["label"] == "alien"


def test_shadow_runs_in_background_and_failures_are_logged(
        setup: _Setup, monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture) -> None:
    gate, calls = threading.Event(), []

    def slow(audio: np.ndarray, embedder: speaker.Embedder) -> tuple[np.ndarray | None, float]:
        calls.append(len(audio))
        gate.wait(5)
        if len(audio) == 7:
            raise RuntimeError("model exploded")
        return setup.next

    monkeypatch.setattr(speaker, "voice_embedding", slow)
    sh = Shadow(setup.root, lambda: (5.5, 3.0))
    t0 = time.perf_counter()
    sh.submit(np.zeros(7, np.float32), "a", 1.0)          # эта фраза сорвётся
    sh.submit(np.zeros(SAMPLE_RATE * 3, np.float32), "b", 2.0)
    assert time.perf_counter() - t0 < 0.5                 # submit не ждёт подсчёта
    assert setup.rows() == []
    with caplog.at_level(logging.ERROR, logger="rupor.speaker"):
        gate.set()
        deadline = time.monotonic() + 5
        while len(setup.rows()) < 1 and time.monotonic() < deadline:
            time.sleep(0.02)
        sh.stop()
    assert [r["rec_id"] for r in setup.rows()] == ["b"]   # сбой одной фразы не мешает следующей
    assert "счёт для диктовки не посчитан" in caplog.text and "model exploded" in caplog.text


def test_shadow_full_queue_drops_with_warning(setup: _Setup, monkeypatch: pytest.MonkeyPatch,
                                              caplog: pytest.LogCaptureFixture) -> None:
    gate = threading.Event()

    def stuck(audio: np.ndarray, embedder: speaker.Embedder) -> tuple[np.ndarray | None, float]:
        gate.wait(5)
        return setup.next

    monkeypatch.setattr(speaker, "voice_embedding", stuck)
    sh = Shadow(setup.root, lambda: (5.5, 3.0))
    with caplog.at_level(logging.WARNING, logger="rupor.speaker"):
        for i in range(Shadow.QUEUE + 3):
            sh.submit(np.zeros(SAMPLE_RATE, np.float32), str(i), float(i))
    assert "очередь подсчёта полна" in caplog.text
    gate.set()
    sh.stop()


# ---------- сборка профиля ----------

def _write_wav(path: Path, pcm: np.ndarray, rate: int = SAMPLE_RATE) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm.astype("<i2").tobytes())


def test_decode_reads_rupor_wav_without_pyav(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Записи самого Рупора (WAV 16 кГц моно 16 бит) читаются без PyAV — его нет в
    установщике; берётся первая минута."""
    import sys

    monkeypatch.setitem(sys.modules, "av", None)          # import av → ImportError
    pcm = np.array([0, 16384, -32768, 32767], np.int16)
    _write_wav(tmp_path / "a.wav", pcm)
    got = speaker.decode(tmp_path / "a.wav")
    assert got.dtype == np.float32
    assert got.tolist() == pytest.approx([0.0, 0.5, -1.0, 32767 / 32768])
    _write_wav(tmp_path / "long.wav", np.zeros(SAMPLE_RATE * 61, np.int16))
    assert len(speaker.decode(tmp_path / "long.wav")) == SAMPLE_RATE * 60
    # другая частота — уже работа PyAV, а его здесь нет
    _write_wav(tmp_path / "8k.wav", pcm, rate=8000)
    with pytest.raises(ImportError):
        speaker.decode(tmp_path / "8k.wav")


def test_enroll_fails_fast_without_own_voice(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    voices, cohort = tmp_path / "voices", tmp_path / "cohort"
    voices.mkdir()
    cohort.mkdir()
    (voices / "a.ogg").write_bytes(b"x")
    (cohort / "c.wav").write_bytes(b"x")
    (tmp_path / "m.onnx").write_bytes(b"m")
    seen: list[str] = []

    def unreadable(path: Path) -> np.ndarray:
        seen.append(path.name)
        raise OSError("cannot decode")

    monkeypatch.setattr(speaker, "decode", unreadable)
    with pytest.raises(ValueError, match="no voice recordings"):
        speaker.enroll(tmp_path / "root", cohort, voices, model=tmp_path / "m.onnx",
                       progress=lambda s: None)
    assert seen == ["a.ogg"]                              # до опорной выборки дело не дошло

def test_enroll_builds_profile(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(7)
    own = _vecs(rng, 1)[0]
    voices, cohort, recs = tmp_path / "voices", tmp_path / "cohort" / "sub", tmp_path / "recs"
    for d in (voices, cohort, recs):
        d.mkdir(parents=True)
    for i in range(12):
        (voices / f"v{i:02d}.ogg").write_bytes(b"x")
    (voices / "notes.txt").write_text("не звук", encoding="utf-8")
    (voices / "silent.ogg").write_bytes(b"x")
    (voices / "broken.ogg").write_bytes(b"x")
    for i in range(340):
        (cohort / f"c{i:03d}.wav").write_bytes(b"x")
    rec_files = [recs / f"r{i}.wav" for i in range(4)]
    for p in rec_files:
        p.write_bytes(b"x")
    model = tmp_path / "campplus.onnx"
    model.write_bytes(b"model bytes")
    seen: list[str] = []
    lock = threading.Lock()

    def fake_decode(path: Path) -> Any:
        if path.stem == "broken":
            raise OSError("bad file")
        return path                      # вместо звука — сам путь: его получит подмена ниже

    def fake_embedding(path: Any, embedder: speaker.Embedder) -> tuple[np.ndarray | None, float]:
        with lock:                       # файлы считаются в несколько потоков
            seen.append(path.name)
            if path.stem == "silent":
                return None, 0.3
            mine = path.suffix == ".ogg" or path.stem.startswith("r")
            return _vecs(rng, 1, own * 12 if mine else None)[0], 3.0

    monkeypatch.setattr(speaker, "decode", fake_decode)
    monkeypatch.setattr(speaker, "voice_embedding", fake_embedding)
    root = tmp_path / "data" / "speaker"
    said: list[str] = []
    prof = speaker.enroll(root, tmp_path / "cohort", voices, rec_files, model,
                          exclude=["v03", "v07.ogg"], cohort_limit=320, progress=said.append)
    assert (root / speaker.MODEL_FILE).read_bytes() == b"model bytes"
    assert prof.meta["counts"] == {"voices": 10, "recordings": 4, "cohort": 320}
    assert prof.meta["top_n"] == 300 and len(prof.meta["model_sha256"]) == 16
    assert "v03.ogg" not in seen and "v07.ogg" not in seen and "notes.txt" not in seen
    assert "broken.ogg" not in seen and "silent.ogg" in seen
    loaded = Profile.load(root / speaker.PROFILE_FILE)
    assert loaded.cohort.shape == (320, DIM) and loaded.meta == prof.meta
    assert loaded.score(_vecs(rng, 1, own * 12)[0])[1] > 5.5
    assert loaded.score(_vecs(rng, 1)[0])[1] < 3.0
    assert any("профиль собран" in s for s in said)
    # модель уже на месте — второй раз её можно не передавать; без неё — понятная ошибка
    speaker.enroll(root, tmp_path / "cohort", voices, None, None, cohort_limit=320,
                   progress=said.append)
    with pytest.raises(FileNotFoundError, match="speaker model not found"):
        speaker.enroll(tmp_path / "empty", tmp_path / "cohort", voices, progress=said.append)
