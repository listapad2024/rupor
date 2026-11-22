"""Автоостановка встречи (meeting_autostop): сигналы конца (a — программа отпустила микрофон и
речи нет, b — источник закрылся, c — цифровая тишина у источника без записи микрофона),
отсчёт 10 с (hint "ending", ending_in_s), «Не останавливать» (meeting_autostop_cancel) и
отмена сама (речь вернулась, микрофон взяли снова), заглушённый микрофон — не конец,
остановка — обычный «Стоп» с заметками и уведомлением; запись микрофона сеанса
(MeetingDetector.session_mic) и опрос движка.

Звук, распознавание и реестр подменены (test_meeting.Box, test_meeting_detect.World)."""
from __future__ import annotations

import time
from typing import Any

import pytest

from rupor import config
from rupor.meeting import (AUTOSTOP_GRACE_S, AUTOSTOP_MIC_OFF_S, AUTOSTOP_QUIET_S,
                           AUTOSTOP_SILENCE_S, AutoStop)
from rupor.meeting_detect import SessionMic
from test_api import Env, env  # noqa: F401
from test_meeting import box, wait_for  # noqa: F401
from test_meeting_detect import BRAVE, SYSTEM, World, w  # noqa: F401
from test_meeting_notes import _meeting_with_lines, _notes_box, _wait_notes
from test_meeting_title import _lines


# ---------- AutoStop: переходы ----------

def _tick(a: AutoStop, now: float, *, tracked: bool = True, free: float | None = 20.0,
          in_use: bool = False, quiet: float = 40.0, silence: float = 0.0) -> str:
    sig = AutoStop.signal(mic_tracked=tracked, mic_free_s=free, quiet_s=quiet, silence_s=silence)
    return a.tick(now, sig, mic_in_use=in_use, quiet_s=quiet, silence_s=silence)


def test_thresholds() -> None:
    assert (AUTOSTOP_MIC_OFF_S, AUTOSTOP_QUIET_S, AUTOSTOP_SILENCE_S, AUTOSTOP_GRACE_S) == (
        15.0, 30.0, 180.0, 10.0)
    sig = AutoStop.signal
    assert sig(mic_tracked=True, mic_free_s=15.0, quiet_s=30.0, silence_s=0) == "mic"
    assert sig(mic_tracked=True, mic_free_s=14.0, quiet_s=300.0, silence_s=999) == ""
    assert sig(mic_tracked=True, mic_free_s=60.0, quiet_s=29.0, silence_s=999) == ""
    # заглушённый микрофон в Meet остаётся занятым: free None — не конец, даже в тишине
    assert sig(mic_tracked=True, mic_free_s=None, quiet_s=900.0, silence_s=900.0) == ""
    # источник без записи микрофона («весь звук компьютера») — только цифровая тишина
    assert sig(mic_tracked=False, mic_free_s=None, quiet_s=900.0, silence_s=179.0) == ""
    assert sig(mic_tracked=False, mic_free_s=None, quiet_s=0.0, silence_s=180.0) == "silence"


def test_countdown_then_stop() -> None:
    a = AutoStop()
    assert _tick(a, 100.0, free=10.0) == ""
    assert _tick(a, 102.0) == "ending" and a.left_s(102.0) == 10
    assert _tick(a, 106.5) == "ending" and a.left_s(106.5) == 6
    assert _tick(a, 112.0) == "stop" and a.left_s(112.0) == 0


def test_countdown_cancelled_by_speech_or_mic() -> None:
    a = AutoStop()
    assert _tick(a, 0.0) == "ending"
    assert _tick(a, 4.0, quiet=1.0) == "resumed" and a.deadline is None   # заговорили
    assert _tick(a, 6.0, quiet=40.0) == "ending"
    assert _tick(a, 8.0, free=None, in_use=True) == "resumed"             # микрофон снова занят
    assert a.left_s(8.0) is None


def test_user_cancel_holds_until_mic_or_speech_returns() -> None:
    a = AutoStop()
    assert _tick(a, 0.0) == "ending"
    a.cancel(3.0)
    assert a.deadline is None
    for t in (5.0, 30.0, 300.0):                       # сигнал есть, но «Не останавливать»
        assert _tick(a, t, quiet=40.0 + t) == ""
    # речь после отмены — снова следим (и сигнал пропал, пока говорят)
    assert _tick(a, 310.0, quiet=2.0) == ""
    assert a.cancelled_at is None
    assert _tick(a, 350.0, quiet=40.0) == "ending"
    # отмена, потом микрофон взяли снова — следим
    a.cancel(351.0)
    assert _tick(a, 360.0, free=None, in_use=True) == ""
    assert a.cancelled_at is None


def test_silence_fallback_and_its_cancel() -> None:
    a = AutoStop()
    sys_tick = dict(tracked=False, free=None, quiet=999.0)
    assert _tick(a, 0.0, silence=100.0, **sys_tick) == ""
    assert _tick(a, 80.0, silence=180.0, **sys_tick) == "ending" and a.reason == "silence"
    assert _tick(a, 85.0, silence=1.0, **sys_tick) == "resumed"           # звук вернулся
    assert _tick(a, 300.0, silence=200.0, **sys_tick) == "ending"
    a.cancel(301.0)
    assert _tick(a, 400.0, silence=299.0, **sys_tick) == ""               # тишина та же
    assert _tick(a, 500.0, silence=50.0, **sys_tick) == ""                # звук после отмены
    assert a.cancelled_at is None


# ---------- сеанс ----------

def _quiet(b: Any, now: float, quiet_s: float = 40.0) -> None:
    b.sess._last_speech = now - quiet_s


def test_session_countdown_status_and_stop_with_notes(box: Any) -> None:
    b, no, _ti = _notes_box(box)
    sess = b.sess
    sess.start()
    _lines(b, 5)
    now = time.monotonic()
    _quiet(b, now)
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=20.0, mic_in_use=False,
                              now=now) == "ending"
    st = sess.status()
    assert st["hint"] == "ending" and st["ending_in_s"] in (9, 10) and st["running"]
    assert any(ev == "meeting" and d["hint"] == "ending" for ev, d in b.events)
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=22.0, mic_in_use=False,
                              now=now + 2) == "ending"
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=31.0, mic_in_use=False,
                              now=now + AUTOSTOP_GRACE_S + 0.1) == "stop"
    wait_for(lambda: not sess.running)
    st = sess.status()
    assert st["state"] == "idle" and st["error"] is None and st["hint"] is None
    assert st["ending_in_s"] is None
    assert b.notices[-1] == {"text": "Встреча закончилась — расшифровка сохранена, заметки "
                                     "готовятся", "level": "info", "action": None}
    wait_for(lambda: len(no.calls) == 1)                   # обычный «Стоп»: заметки сами
    assert _wait_notes(b, "done")["auto"] is True
    assert no.calls[0].in_progress is False


def test_session_resumed_speech_cancels_and_cancel_command(box: Any) -> None:
    b = box()
    sess = b.sess
    sess.start()
    now = time.monotonic()
    _quiet(b, now)
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=20.0, mic_in_use=False,
                              now=now) == "ending"
    sess._last_speech = now + 3                           # заговорили
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=23.0, mic_in_use=False,
                              now=now + 4) == "resumed"
    assert sess.status()["hint"] is None
    _quiet(b, now + 40)
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=60.0, mic_in_use=False,
                              now=now + 40) == "ending"
    sess.autostop_cancel()                                # «Не останавливать»
    st = sess.status()
    assert st["hint"] is None and st["ending_in_s"] is None and st["running"]
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=90.0, mic_in_use=False,
                              now=now + 80) == ""
    assert sess.running


def test_session_muted_mic_does_not_stop(box: Any) -> None:
    b = box()
    sess = b.sess
    sess.start()
    now = time.monotonic()
    _quiet(b, now, 900.0)
    sess._last_sound = now - 900.0
    for i in range(10):
        assert sess.autostop_tick(mic_tracked=True, mic_free_s=None, mic_in_use=True,
                                  now=now + i * 60) == ""
    assert sess.running and sess.status()["hint"] is None


def test_session_system_source_silence_fallback(box: Any) -> None:
    b = box(src=dict(SYSTEM))
    sess = b.sess
    sess.start()
    now = time.monotonic()
    sess._last_sound = now - 100.0
    assert sess.autostop_tick(mic_tracked=False, mic_free_s=None, mic_in_use=False,
                              now=now) == ""
    sess._last_sound = now - 180.0
    assert sess.autostop_tick(mic_tracked=False, mic_free_s=None, mic_in_use=False,
                              now=now) == "ending"
    assert sess.autostop_tick(mic_tracked=False, mic_free_s=None, mic_in_use=False,
                              now=now + 11) == "stop"
    wait_for(lambda: not sess.running)
    assert b.notices[-1]["text"] == "Встреча закончилась — перевод остановлен"   # строк нет


def test_session_autostop_off_clears_countdown(box: Any) -> None:
    b = box()
    sess = b.sess
    sess.start()
    now = time.monotonic()
    _quiet(b, now)
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=20.0, mic_in_use=False,
                              now=now) == "ending"
    b.s.meeting_autostop = False
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=40.0, mic_in_use=False,
                              now=now + 20) == "resumed"
    assert sess.autostop_tick(mic_tracked=True, mic_free_s=60.0, mic_in_use=False,
                              now=now + 40) == ""
    assert sess.running and sess.status()["hint"] is None


def test_source_closed_is_a_clean_stop_with_notes(box: Any) -> None:
    """(b) Процесс захвата вышел с кодом 3 (программа встречи закрылась): «Стоп» сразу, без
    ошибки и без отсчёта — заметки сами, как после кнопки."""
    b, no, _ti = _notes_box(box)
    b.sess.start()
    _lines(b, 5)
    b.stream.on_exit(3, "")
    wait_for(lambda: not b.sess.running)
    st = b.sess.status()
    assert st["state"] == "idle" and st["error"] is None
    assert b.notices[-1]["level"] == "info"
    wait_for(lambda: len(no.calls) == 1)
    _wait_notes(b, "done")


# ---------- запись микрофона сеанса ----------

def test_session_mic_follows_the_session_watch(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    sess_src = w.sources[0]
    assert d.session_mic("T1") == SessionMic()                   # ещё не проверяли
    w.mic(BRAVE)
    d.session_tick(s, sess_src, "T1")
    assert d.session_mic("T1") == SessionMic(tracked=True, in_use=True)
    w.release(BRAVE)
    w.tick(5)
    d.session_tick(s, sess_src, "T1")
    w.tick(12)
    mic = d.session_mic("T1")
    assert mic.tracked and not mic.in_use and mic.free_s == pytest.approx(12.0)
    assert d.session_mic("T2") == SessionMic()                   # другой сеанс


def test_session_mic_not_tracked_without_mic_use(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    sess_src = w.sources[0]
    for _ in range(4):
        w.tick(5)
        d.session_tick(s, sess_src, "T3")
    assert d.session_mic("T3") == SessionMic()


# ---------- движок и WS ----------

def test_ws_autostop_cancel_without_meeting(env: Env) -> None:
    c = env.client()
    st = c.call("meeting_autostop_cancel")
    assert st["running"] is False and st["ending_in_s"] is None and st["hint"] is None
    c.close()


def test_engine_tick_counts_down_and_cancel_by_ws(env: Env,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    c = env.client()
    _meeting_with_lines(env, c, 2)
    m = env.eng._meeting
    assert m is not None and m.running
    det = env.eng._detector
    monkeypatch.setattr(det, "session_tick", lambda s, src, started: None)
    monkeypatch.setattr(det, "session_mic",
                        lambda started: SessionMic(tracked=True, free_s=20.0))
    m._last_speech = time.monotonic() - 60
    env.eng._meeting_detect_tick()
    st = c.wait_event("meeting", lambda d: d["hint"] == "ending", fresh=False)
    assert st["ending_in_s"] in (9, 10)
    st = c.call("meeting_autostop_cancel")
    assert st["hint"] is None and st["ending_in_s"] is None and st["running"] is True
    env.eng._meeting_detect_tick()
    assert m.status()["hint"] is None and m.running
    # выключено в настройках — опрос автоостановку не начинает
    m.autostop_tick(mic_tracked=True, mic_free_s=None, mic_in_use=True)   # микрофон снова занят
    c.call("set_settings", {"patch": {"meeting_autostop": False}})
    env.eng._meeting_detect_tick()
    assert m.status()["hint"] is None
    c.call("meeting_stop")
    c.close()


def test_autostop_settings_validation() -> None:
    s = config.Settings()
    assert s.meeting_autostop is True and s.meeting_mini_pos is None
    assert config._VALIDATORS["meeting_autostop"](False) is False
    assert config._VALIDATORS["meeting_mini_pos"]([1500.4, -20]) == [1500, -20]
    assert config._VALIDATORS["meeting_mini_pos"](None) is None
    for bad in ("yes", [1], [1, 2, 3], ["a", 2], [True, 1]):
        with pytest.raises(ValueError):
            config._VALIDATORS["meeting_mini_pos" if not isinstance(bad, str) else
                              "meeting_autostop"](bad)
