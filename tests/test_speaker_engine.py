"""Теневой счёт «свой / чужой» в движке: фраза уходит на подсчёт только при включённой
настройке и уже после вставки текста; сбой подсчёта диктовку не срывает. Модели и звука
пользователя нет: эмбеддинг подменяется."""
from __future__ import annotations

import json
import logging
import time
from typing import Any

import numpy as np
import pytest

from rupor import speaker
from test_api import Env, env  # noqa: F401 — фикстура env


class FakeShadow:
    def __init__(self, e: Env) -> None:
        self.env = e
        self.calls: list[tuple[int, str | None, float, int]] = []

    def submit(self, audio: np.ndarray, rec_id: str | None, ts: float) -> None:
        # сколько текстов уже вставлено к этому моменту
        self.calls.append((len(audio), rec_id, ts, len(self.env.calls.delivered)))

    def stop(self) -> None:
        pass


def _wait(cond: Any, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        time.sleep(0.02)
    return bool(cond())


def test_shadow_is_off_by_default(env: Env) -> None:
    c = env.client()
    s = c.call("get_settings")
    s = s.get("settings", s)
    assert (s["speaker_shadow"], s["speaker_own_score"], s["speaker_alien_score"]) == (False, 5.5, 3.0)
    fake = FakeShadow(env)
    env.eng.speaker = fake                                   # type: ignore[assignment]
    c.mark()
    env.dictate("раз два")
    c.wait_event("history")
    time.sleep(0.1)
    assert fake.calls == []
    c.close()


def test_phrase_goes_to_shadow_after_insertion(env: Env) -> None:
    c = env.client()
    fake = FakeShadow(env)
    env.eng.speaker = fake                                   # type: ignore[assignment]
    c.call("set_settings", {"patch": {"speaker_shadow": True}})
    c.mark()
    env.dictate("проверка голоса")
    c.wait_event("history")
    assert _wait(lambda: len(fake.calls) == 1)
    n, rec_id, ts, delivered = fake.calls[0]
    assert n == 16000                                        # секунда записи фейкового микрофона
    assert delivered == 1                                    # текст вставлен раньше подсчёта
    [item] = c.call("recordings_list")["items"]
    assert rec_id == item["id"] and ts == pytest.approx(item["ts"], abs=1.0)
    # выключили — следующая фраза на подсчёт не идёт
    c.call("set_settings", {"patch": {"speaker_shadow": False}})
    env.dictate("ещё раз")
    c.wait_event("history")
    time.sleep(0.1)
    assert len(fake.calls) == 1
    c.close()


def test_scores_land_in_journal_next_to_database(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    rng = np.random.default_rng(11)
    own = speaker.unit(rng.standard_normal(192).astype(np.float32))
    root = env.tmp / "speaker"
    root.mkdir()
    (root / speaker.MODEL_FILE).write_bytes(b"stub")
    mine = speaker.unit(rng.standard_normal((40, 192)).astype(np.float32) + own * 12)
    cohort = speaker.unit(rng.standard_normal((400, 192)).astype(np.float32))
    speaker.build_profile([mine], cohort, {"built": "test"}).save(root / speaker.PROFILE_FILE)
    monkeypatch.setattr(speaker, "voice_embedding", lambda audio, emb: (mine[0], 2.5))
    c = env.client()
    c.call("set_settings", {"patch": {"speaker_shadow": True, "speaker_own_score": 4.0}})
    c.mark()
    env.dictate("это мой голос")
    h = c.wait_event("history")
    journal = root / speaker.JOURNAL_FILE
    assert _wait(journal.exists)
    assert _wait(lambda: len(journal.read_text(encoding="utf-8").splitlines()) == 1)
    row = json.loads(journal.read_text(encoding="utf-8"))
    assert row["label"] == "own" and row["score"] > 4.0 and row["speech_s"] == 2.5
    assert len(row["emb"]) == 192 and row["profile"] == "test"
    assert row["rec_id"] == c.call("recordings_list")["items"][0]["id"]
    assert h["text"] == "это мой голос"
    c.close()


def test_scoring_failure_does_not_break_dictation(env: Env, monkeypatch: pytest.MonkeyPatch,
                                                  caplog: pytest.LogCaptureFixture) -> None:
    root = env.tmp / "speaker"
    root.mkdir()
    (root / speaker.MODEL_FILE).write_bytes(b"stub")
    rng = np.random.default_rng(12)
    speaker.build_profile([speaker.unit(rng.standard_normal((5, 192)).astype(np.float32))],
                          speaker.unit(rng.standard_normal((300, 192)).astype(np.float32))
                          ).save(root / speaker.PROFILE_FILE)

    def boom(audio: np.ndarray, emb: speaker.Embedder) -> Any:
        raise RuntimeError("onnx failed")

    monkeypatch.setattr(speaker, "voice_embedding", boom)
    c = env.client()
    c.call("set_settings", {"patch": {"speaker_shadow": True}})
    c.mark()
    with caplog.at_level(logging.ERROR, logger="rupor.speaker"):
        env.dictate("первая")
        assert c.wait_event("history")["text"] == "первая"
        env.dictate("вторая")
        assert c.wait_event("history")["text"] == "вторая"
        assert _wait(lambda: caplog.text.count("счёт для диктовки не посчитан") == 2)
    assert env.calls.delivered[-2:] == [("первая ", False), ("вторая ", False)]
    assert "onnx failed" in caplog.text
    assert not (root / speaker.JOURNAL_FILE).exists()
    c.close()


def test_threshold_settings_are_validated(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"speaker_own_score": 6, "speaker_alien_score": 2.5}})
    s = c.call("get_settings")
    s = s.get("settings", s)
    assert (s["speaker_own_score"], s["speaker_alien_score"]) == (6.0, 2.5)
    assert "Порог счёта «свой голос»" in c.error("set_settings", {"patch": {"speaker_own_score": 99}})
    assert c.error("set_settings", {"patch": {"speaker_shadow": "yes"}})
    c.close()
