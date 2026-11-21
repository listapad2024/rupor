"""Перевод встречи (rupor.meeting и движок): нарезка звука на фразы (StreamVad — на
кодированной громкостью «вероятности» и на настоящей речи из samples/), очередь, строки,
потоковое распознавание (LocalAgreement-2: промежуточные проходы, принятое и хвост,
последний проход), перевод кусками потоком с уточнением всей фразы и правилами ошибок,
задержки, файл расшифровки, команды meeting_* по WebSocket, приоритет диктовки,
приглушение (предложение встречи — tests/test_meeting_detect.py).

Звук не захватывается (процесс захвата — FakeStream), окна и аудиосессии пользователя не
перебираются (источники — фейки), сеть и Credential Manager подменены (test_api.env), файлы
— во временной папке."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import numpy as np
import pytest

from rupor import ai, api, config, inject, meeting
from rupor.asr import BG_TEMPERATURES, ChunkResult
from rupor.hotkeys import Action, Kind
from rupor.meeting import MeetingSession, StreamVad, TailFallback
from test_api import (FAKE_KEY, MEET_SRC, SYSTEM_SRC, Client, Env, FakeStream,  # noqa: F401
                      env, value_vad)

SR = 16000
ROOT = Path(__file__).resolve().parent.parent
SAMPLES = sorted((ROOT / "samples" / "asr").glob("*.wav")) if (ROOT / "samples").is_dir() else []


def tone(sec: float, v: float = 0.9) -> np.ndarray:
    """«Речь» для value_vad: кадры со значением v — вероятность речи v."""
    return np.full(int(sec * SR), v, np.float32)


def quiet(sec: float) -> np.ndarray:
    return np.zeros(int(sec * SR), np.float32)


def feed(vad: StreamVad, audio: np.ndarray, step: int = 800) -> list[meeting.Utterance]:
    out: list[meeting.Utterance] = []
    for i in range(0, len(audio), step):
        out += vad.feed(audio[i:i + step])
    return out


def wait_for(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("не дождался")
        time.sleep(0.01)


# ---------- StreamVad ----------

FRAME_S = meeting.FRAME_S


def test_vad_phrase_with_preroll_and_tail() -> None:
    vad = StreamVad(value_vad)
    utts = feed(vad, np.concatenate([quiet(1.0), tone(1.0), quiet(1.0)]))
    assert len(utts) == 1
    u = utts[0]
    assert u.start_s == pytest.approx(1.0 - 0.2, abs=FRAME_S * 1.5)        # 200 мс до речи
    assert u.dur == pytest.approx(0.2 + 1.0 + 0.2, abs=FRAME_S * 2.5)       # и 200 мс после
    assert u.end_s == pytest.approx(u.start_s + u.dur, abs=1e-6)
    assert u.audio.dtype == np.float32 and not vad.in_speech


def test_vad_short_pause_does_not_end_phrase() -> None:
    audio = np.concatenate([tone(1.0), quiet(0.3), tone(1.0), quiet(0.8)])
    assert len(feed(StreamVad(value_vad), audio)) == 1
    audio = np.concatenate([tone(1.0), quiet(0.7), tone(1.0), quiet(0.8)])
    assert len(feed(StreamVad(value_vad), audio)) == 2


def test_vad_hysteresis_between_thresholds_keeps_speech() -> None:
    # 0.4 — ниже начала (0.5), но выше конца (0.35): не начинает фразу и не заканчивает её
    assert feed(StreamVad(value_vad), np.concatenate([tone(2.0, 0.4), quiet(1.0)])) == []
    utts = feed(StreamVad(value_vad),
                np.concatenate([tone(0.5), tone(1.5, 0.4), tone(0.5), quiet(0.8)]))
    assert len(utts) == 1 and utts[0].dur > 2.4


def test_vad_ignores_clicks() -> None:
    assert feed(StreamVad(value_vad), np.concatenate([tone(0.1), quiet(1.0)])) == []


def test_vad_cuts_long_speech_at_quietest_frame() -> None:
    dip = tone(2 * FRAME_S, 0.4)              # самый «тихий» кадр последних 2 с
    audio = np.concatenate([quiet(1.0), tone(10.5), dip, tone(4.0), quiet(1.0)])
    utts = feed(StreamVad(value_vad), audio)
    assert len(utts) == 2
    first, second = utts
    assert first.start_s == pytest.approx(0.8, abs=FRAME_S * 1.5)
    assert first.end_s == pytest.approx(1.0 + 10.5 + 1.5 * FRAME_S, abs=FRAME_S)
    assert first.dur <= meeting.VAD_MAX_S + 1e-6
    assert second.start_s == pytest.approx(first.end_s, abs=1e-6)       # без потерь
    assert second.end_s == pytest.approx(1.0 + 10.5 + 2 * FRAME_S + 4.0 + 0.2,
                                         abs=FRAME_S * 2.5)


def test_vad_max_length_is_configurable() -> None:
    utts = feed(StreamVad(value_vad, max_s=6.0), np.concatenate([tone(13.0), quiet(1.0)]))
    assert len(utts) == 3 and all(u.dur <= 6.0 + 1e-6 for u in utts)


def test_vad_flush_returns_unfinished_phrase() -> None:
    vad = StreamVad(value_vad)
    assert feed(vad, tone(1.0)) == [] and vad.in_speech
    rest = vad.flush()
    assert len(rest) == 1 and rest[0].dur == pytest.approx(1.0, abs=FRAME_S * 1.5)
    assert vad.flush() == [] and not vad.in_speech


def test_vad_falls_back_when_prob_fn_breaks(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(frames: np.ndarray) -> np.ndarray:
        raise RuntimeError("onnx")

    monkeypatch.setattr(meeting, "TailFallback", lambda: value_vad)
    utts = feed(StreamVad(broken), np.concatenate([tone(1.0), quiet(1.0)]))
    assert len(utts) == 1


def test_tail_fallback_maps_segments_to_frames() -> None:
    seen: list[int] = []

    def ts(audio: np.ndarray) -> list[dict[str, int]]:
        seen.append(len(audio))
        # речь — подряд идущие ненулевые сэмплы
        nz = np.concatenate([[0], (audio != 0).astype(np.int8), [0]])
        edges = np.flatnonzero(np.diff(nz))
        return [{"start": int(a), "end": int(b)} for a, b in zip(edges[::2], edges[1::2])]

    fb = TailFallback(ts)
    frames = np.zeros((4, 512), np.float32)
    frames[1:3] = 0.5
    assert fb(frames).tolist() == [0.0, 1.0, 1.0, 0.0]
    frames2 = np.zeros((2, 512), np.float32)
    frames2[1] = 0.5
    assert fb(frames2).tolist() == [0.0, 1.0]
    assert seen == [4 * 512, 6 * 512]          # второй вызов — с хвостом прежнего звука


def test_merge_utterances() -> None:
    a = meeting.Utterance(tone(1.0), 1.0, 2.0)
    b = meeting.Utterance(tone(2.0), 3.0, 5.0)
    m = meeting.merge_utterances([a, b])
    assert m.start_s == 1.0 and m.end_s == 5.0
    assert m.dur == pytest.approx(3.0 + meeting.MERGE_GAP_S)
    assert meeting.merge_utterances([a]) is a


# ---------- настоящая речь (samples/asr, если есть) ----------

def _clips() -> list[np.ndarray]:
    if len(SAMPLES) < 2:
        pytest.skip("нет samples/asr/*.wav")
    from rupor.asr import load_wav
    return [load_wav(str(p)) for p in SAMPLES[:2]]


@pytest.mark.parametrize("backend", ["silero_stream", "tail_fallback"])
def test_vad_on_real_speech(backend: str) -> None:
    c1, c2 = _clips()
    gap = 1.5
    audio = np.concatenate([quiet(0.5), c1, quiet(gap), c2, quiet(1.0)])
    spans = [(0.5, 0.5 + len(c1) / SR),
             (0.5 + len(c1) / SR + gap, 0.5 + (len(c1) + len(c2)) / SR + gap)]
    prob = meeting.default_prob_fn() if backend == "silero_stream" else TailFallback()
    if backend == "silero_stream":
        assert isinstance(prob, meeting.SileroStream)     # faster-whisper 1.2: потоковый
    vad = StreamVad(prob)
    utts = feed(vad, audio, step=800) + vad.flush()
    assert utts, "речь не нашлась"
    hit = [0, 0]
    for u in utts:
        assert u.dur <= meeting.VAD_MAX_S + 1e-6
        # кусок целиком в одной фразе (с запасом на 200 мс до и после) — паузу не перешагнул
        inside = [i for i, (a, b) in enumerate(spans)
                  if u.start_s >= a - 0.25 and u.end_s <= b + 0.75]
        assert len(inside) == 1, (u.start_s, u.end_s, spans)
        hit[inside[0]] += 1
    assert hit[0] >= 1 and hit[1] >= 1
    covered = sum(u.dur for u in utts)
    assert covered >= 0.6 * (spans[0][1] - spans[0][0] + spans[1][1] - spans[1][0])


def test_silero_stream_matches_batch() -> None:
    c1, _ = _clips()
    from faster_whisper.vad import get_vad_model
    a = c1[: len(c1) // 512 * 512]
    batch = np.asarray(get_vad_model()(a)).reshape(-1)
    prob = meeting.default_prob_fn()
    assert isinstance(prob, meeting.SileroStream)
    frames = a.reshape(-1, 512)
    got: list[np.ndarray] = []
    i, sizes = 0, [1, 3, 7, 2, 10]
    while i < len(frames):                     # кусками разной длины
        n = sizes[len(got) % len(sizes)]
        got.append(prob(frames[i:i + n]))
        i += n
    stream = np.concatenate(got)
    assert stream.shape == batch.shape
    assert float(np.max(np.abs(stream - batch))) < 1e-3


# ---------- сеанс встречи (без движка) ----------

SRC = {"id": "proc:4100", "kind": "process", "pid": 4100, "exe": "brave.exe",
       "name": "Brave", "title": "Meet – abc-defg-hij", "meeting": True, "active": True}


class Rec:
    """Распознавание кусков. Последний проход фразы (повторы с температурой): результаты по
    очереди (строка — английский текст, исключение — сбой). Промежуточный проход (одна
    температура): ``interim(звук, с)`` — гипотеза (по умолчанию пусто). Пока hold()
    истинно — модель «занята» (None, как ModelGate)."""

    def __init__(self) -> None:
        self.loaded = True
        self.results: list[Any] = []
        self.calls: list[dict[str, Any]] = []
        self.interim: Callable[[float], Any] = lambda audio_s: ""
        self.interim_calls: list[dict[str, Any]] = []
        self.hold: Callable[[], bool] = lambda: False

    def transcribe_background(self, audio: np.ndarray, *, language: str | None,
                              prompt: str | None, yield_to: Callable[[], bool] | None,
                              timeout: float | None = None, beam_size: int = 1,
                              temperature: float | tuple[float, ...] = BG_TEMPERATURES
                              ) -> ChunkResult | None:
        if not self.loaded or self.hold() or (yield_to is not None and yield_to()):
            time.sleep(0.01)
            return None
        call = {"language": language, "prompt": prompt, "audio_s": len(audio) / SR,
                "beam_size": beam_size, "temperature": temperature}
        if not isinstance(temperature, tuple):
            self.interim_calls.append(call)
            h = self.interim(len(audio) / SR)
            if isinstance(h, str):
                h = ChunkResult(h, language or "en", 0.95, len(audio) / SR, 5)
            return h
        self.calls.append(call)
        r = self.results.pop(0) if self.results else ""
        if isinstance(r, BaseException):
            raise r
        if isinstance(r, str):
            r = ChunkResult(r, language or "en", 0.95, len(audio) / SR, 5)
        return r


class Box:
    """Сеанс встречи с фейками. interim=False (по умолчанию) — без промежуточных проходов:
    строка — одна на фразу, её текст — последнего прохода (как до потокового распознавания),
    перевод — одним куском; потоковые тесты включают interim=True. sess_kw — другие
    аргументы MeetingSession (название записи: title_fn, clock)."""

    def __init__(self, tmp: Path, *, translate_ok: bool = True, interim: bool = False,
                 sess_kw: dict[str, Any] | None = None, src: dict[str, Any] | None = None,
                 **sets: Any) -> None:
        self.s = config.Settings(meeting_dir=str(tmp), **sets)
        self.rec = Rec()
        self.events: list[tuple[str, dict[str, Any]]] = []
        self.notices: list[dict[str, Any]] = []
        self.tr_calls: list[tuple[str, str, str, list[str]]] = []
        self.reqs: list[meeting.TrRequest] = []
        self.refines: list[meeting.TrRequest] = []
        self.warmed = 0
        self.delta: Callable[[meeting.TrRequest], None] | None = None
        self.tr_impl: Callable[[str, str, str, list[str]], ai.AIResult] = (
            lambda text, src, dst, ctx: ai.AIResult(f"RU:{text}", 5.0, 0.0001, "openrouter", "m"))
        self.refine_impl: Callable[[meeting.TrRequest], ai.AIResult] = (
            lambda req: ai.AIResult(f"WHOLE:{req.text}", 5.0, 0.0001, "openrouter", "m"))
        self.streams: list[FakeStream] = []
        self.start_error: BaseException | None = None
        self.ended: list[MeetingSession] = []
        self.sess = MeetingSession(
            SRC if src is None else src, settings=self.s, rec=self.rec, emit=self._emit,
            notice=self._notice, translate=self._tr, stream_factory=self._stream,
            prob_fn=value_vad, translate_ok=translate_ok, terms=["ClickHouse"],
            on_end=self.ended.append, warm=self._warm, interim=interim, **(sess_kw or {}))

    def _warm(self) -> None:
        self.warmed += 1

    def _emit(self, ev: str, data: dict[str, Any]) -> None:
        self.events.append((ev, data))

    def _notice(self, text: str, level: str = "info", action: str | None = None,
                toast: bool | None = None, data: Any = None) -> None:
        self.notices.append({"text": text, "level": level, "action": action})

    def _tr(self, req: meeting.TrRequest) -> ai.AIResult:
        if req.refine:
            self.refines.append(req)
            return self.refine_impl(req)
        self.reqs.append(req)
        self.tr_calls.append((req.text, req.src, req.dst, list(req.context)))
        if self.delta is not None and req.on_delta is not None:
            self.delta(req)                     # «поток»: часть перевода до ответа
        return self.tr_impl(req.text, req.src, req.dst, req.context)

    def _stream(self, on_audio: Callable[[np.ndarray], None],
                on_exit: Callable[[int, str], None]) -> FakeStream:
        st = FakeStream(on_audio, on_exit)
        if self.start_error is not None:
            err = self.start_error

            def boom(target: str) -> None:
                raise err
            st.start = boom  # type: ignore[method-assign]
        self.streams.append(st)
        return st

    @property
    def stream(self) -> FakeStream:
        return self.streams[-1]

    def audio(self, a: np.ndarray, step: int = 800) -> None:
        for i in range(0, len(a), step):
            self.stream.on_audio(a[i:i + step])

    def say(self, sec: float = 0.6, wait: bool = True) -> None:
        """Фраза и пауза; wait — дождаться, пока её распознают (иначе фразы, набежавшие в
        очередь, склеились бы в один кусок)."""
        n = len(self.rec.calls)
        self.audio(np.concatenate([tone(sec), quiet(0.6)]))
        if wait:
            wait_for(lambda: len(self.rec.calls) > n)

    def lines(self) -> list[dict[str, Any]]:
        return self.sess.lines()

    def wait_lines(self, n: int, final: bool = True, timeout: float = 5.0) -> list[dict[str, Any]]:
        def ok() -> bool:
            ls = self.lines()
            return len(ls) >= n and (not final or all(x["status"] != "pending" for x in ls))
        wait_for(ok, timeout)
        return self.lines()

    def line_events(self) -> list[dict[str, Any]]:
        return [d for ev, d in self.events if ev == "meeting_line"]

    def statuses(self) -> list[dict[str, Any]]:
        return [d for ev, d in self.events if ev == "meeting"]


@pytest.fixture
def box(tmp_path: Path) -> Any:
    boxes: list[Box] = []

    def make(**kw: Any) -> Box:
        b = Box(tmp_path, **kw)
        boxes.append(b)
        return b

    yield make
    for b in boxes:
        b.sess.stop(drain=False)


def test_session_lines_translation_and_file(box: Any, tmp_path: Path) -> None:
    b = box()
    b.sess.start()
    assert b.stream.target == "pid:4100"
    st = b.sess.status()
    assert st["running"] is True and st["state"] == "listening" and st["translate"] is True
    assert st["source"] == SRC and st["target_lang"] == "ru" and st["file"] is None
    # ISO с мс и смещением: окно перевода отличает по нему сеансы, начатые в одну секунду
    assert datetime.fromisoformat(st["started_at"]).utcoffset() is not None
    assert len(st["started_at"].split("T")[1].split(".")[1]) == 3 + 6, st["started_at"]
    b.rec.results = ["Hello everyone", ChunkResult("Всем привет", "ru", 0.99, 1.0, 5),
                     "Let's start with ClickHouse"]
    b.say()
    b.say()
    b.say()
    ls = b.wait_lines(3)
    assert [(x["id"], x["lang"], x["status"]) for x in ls] == [
        (1, "en", "done"), (2, "ru", "original"), (3, "en", "done")]
    assert ls[0]["translation"] == "RU:Hello everyone" and ls[1]["translation"] is None
    assert set(ls[0]) == {"id", "t", "ts", "lang", "text", "partial", "translation", "status",
                          "final", "refined"}
    assert all(x["final"] and x["partial"] == "" and not x["refined"] for x in ls)
    assert ls[0]["t"] == pytest.approx(0.0, abs=0.25) and len(ls[0]["ts"]) == 8
    # своя строка на языке перевода — без ИИ; контекст — прежние исходные строки
    assert [c[0] for c in b.tr_calls] == ["Hello everyone", "Let's start with ClickHouse"]
    assert b.tr_calls[1][1:] == ("en", "ru", ["Hello everyone", "Всем привет"])
    # подсказка Whisper: термины + хвост принятого текста встречи, без кириллицы для
    # английской речи; последний проход фразы — beam 5 с повторами по температуре
    assert b.rec.calls[0]["prompt"] == "ClickHouse." and b.rec.calls[0]["beam_size"] == 5
    assert b.rec.calls[0]["temperature"] == BG_TEMPERATURES
    assert b.rec.calls[1]["prompt"] == "ClickHouse. Hello everyone"
    # прошлая строка русская — подсказка с ней как есть (для английской — без кириллицы)
    assert b.rec.calls[2]["prompt"] == "ClickHouse. Hello everyone Всем привет"
    assert b.rec.calls[0]["language"] is None
    # события строки — обновления той же строки: «переводится», последнее — готовая
    evs = [(d["id"], d["status"]) for d in b.line_events() if d["id"] == 1]
    assert {st for _i, st in evs} <= {"pending", "done"} and evs[-1] == (1, "done")
    # средние задержки — в статусе
    lat = b.sess.status()["latency"]
    assert set(lat) == {"asr_ms", "first_token_ms", "done_ms"}
    assert lat["asr_ms"] is not None and lat["done_ms"] is not None
    assert b.warmed >= 1                  # соединение перевода — заранее
    # файл — уже на диске (каждая строка сбрасывается сразу), строки по порядку
    f = b.sess.file
    # название без ИИ (title_fn нет): заголовок окна «Meet – abc-defg-hij» → «Google Meet»
    assert f is not None and f.parent == tmp_path and f.name.endswith(" — Google Meet.md")
    started = datetime.fromisoformat(st["started_at"])
    assert f.name == f"{started:%Y-%m-%d %H-%M} — Google Meet.md"
    text = f.read_text(encoding="utf-8")
    assert text.startswith("# Google Meet\n\n")
    assert f"- Дата: {started:%Y-%m-%d %H:%M}\n" in text
    assert "- Источник: Brave — Meet – abc-defg-hij\n" in text
    assert "- Языки: речь — определяется автоматически, перевод — ru\n" in text
    assert b.sess.status()["title"] == "Google Meet"
    body = text.split("\n\n", 2)[2]
    # заметок ещё нет — сразу «## Полная расшифровка» и строки (место заметок — над ней)
    assert body == ("## Полная расшифровка\n\n"
                    f"**{ls[0]['ts']}** RU:Hello everyone\n> Hello everyone\n\n"
                    f"**{ls[1]['ts']}** Всем привет\n\n"
                    f"**{ls[2]['ts']}** RU:Let's start with ClickHouse\n"
                    f"> Let's start with ClickHouse\n\n")
    assert b.sess.status()["lines"] == 3 and b.sess.status()["file"] == str(f)
    b.sess.stop()
    st = b.sess.status()
    assert st["running"] is False and st["state"] == "idle" and st["level"] == 0.0
    assert b.stream.stopped and b.ended == [b.sess]
    assert b.statuses()[-1]["running"] is False
    b.sess.stop()                                        # повторно — ничего
    assert b.ended == [b.sess]


def test_clipboard_text(box: Any) -> None:
    b = box()
    b.sess.start()
    b.rec.results = ["Hello", ChunkResult("Привет", "ru", 0.99, 1.0, 5)]
    b.say()
    b.say()
    b.wait_lines(2)
    ts = [x["ts"] for x in b.lines()]
    assert b.sess.transcript_text() == (f"{ts[0]} RU:Hello\n{' ' * 9}Hello\n"
                                        f"{ts[1]} Привет")


def test_language_from_previous_line_when_unsure(box: Any) -> None:
    b = box()
    b.sess.start()
    b.rec.results = [ChunkResult("Hello", "en", 0.9, 1, 5), ChunkResult("Ja okay", "de", 0.3, 1, 5),
                     ChunkResult("Guten Tag", "de", 0.8, 1, 5)]
    for _ in range(3):
        b.say()
    assert [x["lang"] for x in b.wait_lines(3)] == ["en", "en", "de"]
    assert b.tr_calls[2][1] == "de"


def test_forced_source_language_and_translation_off(box: Any) -> None:
    b = box(meeting_source_lang="de", meeting_translate=False)
    b.sess.start()
    assert b.sess.status()["translate"] is False
    b.rec.results = ["Guten Morgen"]
    b.say()
    ls = b.wait_lines(1)
    assert ls[0]["lang"] == "de" and ls[0]["status"] == "original"
    assert b.rec.calls[0]["language"] == "de" and b.tr_calls == []
    # включили перевод посреди встречи — следующие строки переводятся
    b.s.meeting_translate = True
    b.rec.results = ["Wie geht's"]
    b.say()
    ls = b.wait_lines(2)
    assert ls[1]["status"] == "done" and ls[1]["translation"] == "RU:Wie geht's"


def test_auth_error_stops_translation_with_one_notice(box: Any) -> None:
    b = box()
    b.sess.start()

    def fail(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        raise ai.AIError("OpenRouter отклонил ключ (401)", "auth", 401)

    b.tr_impl = fail
    b.rec.results = ["One", "Two", "Three"]
    for _ in range(3):
        b.say()
    ls = b.wait_lines(3)
    assert ls[0]["status"] == "error" and "отклонил ключ" in ls[0]["error"]
    assert [x["status"] for x in ls[1:]] == ["original", "original"]
    assert len(b.tr_calls) == 1
    assert [n["action"] for n in b.notices] == ["open_settings_ai"]
    assert "до конца встречи" in b.notices[0]["text"]
    assert b.sess.status()["translate"] is False
    # ключ задали снова — перевод вернулся
    b.tr_impl = lambda text, src, dst, ctx: ai.AIResult("RU", 1.0, 0.0, "openrouter", "m")
    b.sess.set_translate_available(True)
    b.rec.results = ["Four"]
    b.say()
    assert b.wait_lines(4)[3]["status"] == "done"


def test_transient_errors_pause_after_three(box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meeting, "TR_RETRY_S", 0.02)   # строка: вторая попытка — сразу
    b = box()
    b.sess.start()

    def fail(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        raise ai.AIError("Нет связи с OpenRouter", "network")

    b.tr_impl = fail
    b.rec.results = ["One", "Two", "Three", "Four"]
    for _ in range(4):
        b.say()
    ls = b.wait_lines(4)
    assert [x["status"] for x in ls] == ["error"] * 4
    # у строки две попытки; третья ошибка подряд — пауза: дальше без вызовов
    assert len(b.tr_calls) == 3
    assert "на паузе" in ls[3]["error"] and "Нет связи" in ls[3]["error"]
    assert b.notices == [] and b.sess.status()["translate"] is True   # без всплывашек
    monkeypatch.setattr(b.sess, "_tr_paused_until", 0.0)
    b.tr_impl = lambda text, src, dst, ctx: ai.AIResult("RU", 1.0, 0.0, "openrouter", "m")
    b.rec.results = ["Five"]
    b.say()
    assert b.wait_lines(5)[4]["status"] == "done"


def test_pause_spares_line_translated_in_full(box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """Перевод встал на паузу (ошибки подряд у других строк), пока медленный кусок строки 1
    ещё переводился: строка 1 переведена целиком — готова, а не «перевод на паузе»."""
    monkeypatch.setattr(meeting, "TR_RETRY_S", 0.02)
    b = box()

    def impl(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        if text.startswith("good"):
            wait_for(lambda: b.sess._tr_paused_until > 0)    # ответ пришёл уже в паузе
            return ai.AIResult(f"RU:{text}", 5.0, 0.0001, "openrouter", "m")
        raise ai.AIError("Нет связи с OpenRouter", "network")

    b.tr_impl = impl
    b.rec.results = ["good line one.", "bad line two.", "bad line three."]
    b.sess.start()
    for _ in range(3):
        b.say()
    wait_for(lambda: len(b.lines()) == 3
             and all(x["status"] not in ("pending", "streaming") for x in b.lines()))
    ls = b.lines()
    assert (ls[0]["status"], ls[0]["translation"]) == ("done", "RU:good line one.")
    assert "error" not in ls[0]
    assert [x["status"] for x in ls[1:]] == ["error", "error"]
    assert b.sess.status()["latency"]["done_ms"] is not None


def test_rate_limit_is_retried_once_while_fresh(box: Any) -> None:
    b = box()
    b.sess.start()
    n = [0]

    def flaky(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        n[0] += 1
        if n[0] == 1:
            raise ai.AIError("429", "rate_limit", 429)
        return ai.AIResult("RU", 1.0, 0.0, "openrouter", "m")

    b.tr_impl = flaky
    b.rec.results = ["Hello"]
    b.say()
    assert b.wait_lines(1)[0]["status"] == "done" and n[0] == 2


def test_garbage_chunk_is_dropped_and_resets_prompt(box: Any) -> None:
    b = box()
    b.sess.start()
    b.rec.results = ["Hello there", "", "Next"]
    for _ in range(3):
        b.say()
    ls = b.wait_lines(2)
    assert [x["text"] for x in ls] == ["Hello there", "Next"]
    assert b.rec.calls[1]["prompt"] == "ClickHouse. Hello there"
    assert b.rec.calls[2]["prompt"] == "ClickHouse."         # после мусора — без хвоста


def test_waits_for_model_then_continues(box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meeting, "WAIT_GPU_AFTER_S", 0.1)
    b = box()
    b.sess.start()
    busy = [True]
    b.rec.hold = lambda: busy[0]
    b.rec.results = ["Hello"]
    b.say(wait=False)
    wait_for(lambda: b.sess.status()["state"] == "waiting_gpu")
    assert b.lines() == []
    busy[0] = False
    b.wait_lines(1)
    wait_for(lambda: b.sess.status()["state"] == "listening")
    states = [d["state"] for d in b.statuses()]
    assert "waiting_gpu" in states and states[-1] == "listening"


def test_backlog_is_merged(box: Any) -> None:
    b = box()
    b.sess.start()
    busy = [True]
    b.rec.hold = lambda: busy[0]
    for _ in range(5):
        b.say(0.5, wait=False)
    wait_for(lambda: len(b.sess._queue) >= 4)
    busy[0] = False
    b.rec.results = ["All five phrases"]
    ls = b.wait_lines(1)
    time.sleep(0.2)
    assert len(b.rec.calls) <= 2 and b.rec.calls[0]["audio_s"] > 3.0
    assert ls[0]["text"] == "All five phrases"


def test_no_audio_state(box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meeting, "NO_AUDIO_S", 0.2)
    b = box()
    b.sess.start()
    b.audio(quiet(0.1))
    time.sleep(0.25)
    b.audio(quiet(0.1))
    assert b.sess.status()["state"] == "no_audio"
    assert b.statuses()[-1]["state"] == "no_audio"
    b.audio(tone(0.1, 0.01))
    assert b.sess.status()["state"] == "listening"
    assert 0.0 < b.sess.status()["level"] <= 1.0


def test_level_events_are_throttled(box: Any) -> None:
    b = box()
    b.sess.start()
    n0 = len(b.statuses())
    for i in range(40):                         # 40 кусков по 50 мс за доли секунды
        b.stream.on_audio(tone(0.05, 0.02 + 0.02 * (i % 2)))
    assert len(b.statuses()) - n0 <= 2


def test_lines_cap_and_paging(box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meeting, "LINES_MAX", 3)
    b = box(meeting_translate=False)
    b.sess.start()
    b.rec.results = [f"Line {i}" for i in range(1, 6)]
    for i in range(5):
        b.say()                                 # по одной: очередь не склеивает фразы
        wait_for(lambda: b.sess.status()["lines"] == i + 1)
    assert [x["id"] for x in b.sess.lines()] == [3, 4, 5]
    assert [x["id"] for x in b.sess.lines(limit=2)] == [4, 5]
    assert [x["id"] for x in b.sess.lines(after_id=3, limit=1)] == [4]
    assert b.sess.lines(after_id=5) == []
    f = b.sess.file
    assert f is not None and f.read_text(encoding="utf-8").count("**") == 10   # файл — все 5


def test_capture_target_closed_stops_quietly(box: Any) -> None:
    b = box()
    b.sess.start()
    b.stream.on_exit(3, "")
    wait_for(lambda: not b.sess.running)
    st = b.sess.status()
    assert st["state"] == "idle" and st["error"] is None
    assert b.notices[-1]["level"] == "info" and "Brave" in b.notices[-1]["text"]


def test_capture_failure_is_an_error(box: Any) -> None:
    b = box()
    b.sess.start()
    b.stream.on_exit(2, "AUDCLNT_E_UNSUPPORTED_FORMAT")
    wait_for(lambda: not b.sess.running)
    st = b.sess.status()
    assert st["state"] == "error" and "AUDCLNT_E_UNSUPPORTED_FORMAT" in st["error"]
    assert b.notices[-1]["level"] == "error"


def test_start_failure_raises(box: Any) -> None:
    b = box()
    b.start_error = OSError("помощник не запустился")
    with pytest.raises(ValueError, match="Не удалось захватить звук «Brave»"):
        b.sess.start()
    assert b.sess.running is False


def test_asr_errors_stop_meeting(box: Any) -> None:
    b = box()
    b.sess.start()
    b.rec.results = [RuntimeError("CUDA failed")] * 3
    for _ in range(3):
        b.say()
    wait_for(lambda: not b.sess.running)
    assert b.sess.status()["state"] == "error" and "CUDA failed" in b.sess.status()["error"]


def test_stop_drains_unfinished_phrase(box: Any) -> None:
    b = box()
    b.sess.start()
    b.rec.results = ["Last words"]
    b.audio(tone(1.0))                         # без паузы — фраза ещё не кончилась
    b.sess.stop(drain=True)
    ls = b.wait_lines(1)
    assert ls[0]["text"] == "Last words" and ls[0]["status"] == "done"
    wait_for(lambda: b.sess._finalized)
    f = b.sess.file
    assert f is not None and "RU:Last words" in f.read_text(encoding="utf-8")


def test_stop_without_drain_keeps_pending_lines_in_file(box: Any) -> None:
    b = box()
    b.sess.start()
    gate = threading.Event()

    def slow(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        gate.wait(5)
        return ai.AIResult("RU", 1.0, 0.0, "openrouter", "m")

    b.tr_impl = slow
    b.rec.results = ["Pending line"]
    b.say()
    b.wait_lines(1, final=False)
    b.sess.stop(drain=False)
    gate.set()
    f = b.sess.file
    assert f is not None and "Pending line" in f.read_text(encoding="utf-8")


def test_detach_silences_events(box: Any) -> None:
    b = box()
    b.sess.start()
    b.sess.detach()
    n = len(b.events)
    b.rec.results = ["Hello"]
    b.say()
    b.wait_lines(1)
    assert len(b.events) == n


def test_transcript_file_names(tmp_path: Path) -> None:
    t = datetime(2026, 10, 19, 14, 5)
    w1 = meeting.TranscriptWriter(tmp_path, t, "Brave — Meet", 'Brave: "Meet"/x', "langs")
    w2 = meeting.TranscriptWriter(tmp_path, t, "Brave — Meet", 'Brave: "Meet"/x', "langs")
    line = meeting.MeetingLine(1, 0.0, "14:05:01", "en", "Hi", "Привет", "done")
    w1.write(line)
    w2.write(line)
    w1.close()
    w2.close()
    assert w1.path is not None and w1.path.name == "2026-10-19 14-05 — Brave Meet x.md"
    assert w2.path is not None and w2.path.name == "2026-10-19 14-05 — Brave Meet x (2).md"
    text = w1.path.read_text(encoding="utf-8")
    assert text.startswith('# Brave: "Meet"/x\n\n- Дата: 2026-10-19 14:05\n'
                           "- Источник: Brave — Meet\n- langs\n\n")
    assert text.endswith("**14:05:01** Привет\n> Hi\n\n")
    assert meeting.safe_file_part("  ...  ") == "meeting"


def test_title_matching_and_app_names() -> None:
    pats = config.Settings().meeting_title_patterns
    assert meeting.title_matches("Meet – abc-defg-hij - Brave", pats)
    assert meeting.title_matches("ЯНДЕКС ТЕЛЕМОСТ — встреча", pats)
    assert not meeting.title_matches("Документация — Brave", pats)
    assert not meeting.title_matches("", pats)
    assert meeting.is_browser(r"C:\Program Files\BraveSoftware\brave.exe")
    assert not meeting.is_browser("telegram.exe")
    assert meeting.app_name("msedge.exe") == "Microsoft Edge"
    assert meeting.app_name("someapp.exe") == "Someapp"
    assert meeting.capture_target(SRC) == "pid:4100"
    assert meeting.capture_target(SYSTEM_SRC) == "system"
    with pytest.raises(ValueError):
        meeting.capture_target({"id": "bogus", "kind": "process", "pid": None})


# ---------- настройки ----------

@pytest.mark.parametrize("patch,ok", [
    ({"meeting_target_lang": "de"}, True), ({"meeting_target_lang": "xx"}, False),
    ({"meeting_source_lang": "auto"}, True), ({"meeting_source_lang": "ja"}, True),
    ({"meeting_source_lang": "klingon"}, False),
    ({"meeting_font_size": 12}, True), ({"meeting_font_size": 29}, False),
    ({"meeting_title_patterns": ["Zoom", " zoom ", "Webex"]}, True),
    ({"meeting_title_patterns": [""]}, False), ({"meeting_title_patterns": ["x" * 61]}, False),
    ({"meeting_title_patterns": [f"p{i}" for i in range(31)]}, False),
    ({"meeting_title_patterns": "Zoom"}, False),
    ({"meeting_rect": [10, -20, 420, 900]}, True), ({"meeting_rect": None}, True),
    ({"meeting_rect": [0, 0, 199, 900]}, False), ({"meeting_rect": [0, 0, 420]}, False),
    ({"meeting_rect": [0, 2e6, 420, 900]}, False), ({"meeting_rect": [0, 0, True, 900]}, False),
    ({"meeting_dir": ""}, True), ({"meeting_dir": r"D:\Встречи"}, True),
    ({"meeting_dir": "relative\\dir"}, False),
    ({"translate_model": "google/gemini-flash"}, True), ({"translate_timeout_s": 0.5}, False),
    ({"translate_timeout_s": 30}, True), ({"meeting_protect": "yes"}, False),
])
def test_meeting_settings_validation(patch: dict[str, Any], ok: bool) -> None:
    s = config.Settings()
    if ok:
        config.apply_patch(s, patch)
    else:
        with pytest.raises(ValueError):
            config.apply_patch(s, patch)


def test_meeting_settings_defaults_and_normalization() -> None:
    s = config.Settings()
    assert (s.meeting_target_lang, s.meeting_source_lang, s.meeting_translate,
            s.meeting_show_original, s.meeting_font_size, s.meeting_autosuggest,
            s.meeting_save, s.meeting_dir, s.meeting_protect, s.meeting_rect,
            s.translate_model, s.translate_timeout_s) == (
        "ru", "auto", True, True, 16, True, True, "", True, None, "", 6.0)
    assert "Google Meet" in s.meeting_title_patterns and "Телемост" in s.meeting_title_patterns
    config.apply_patch(s, {"meeting_title_patterns": ["Zoom", " zoom ", "Web  ex"],
                           "meeting_rect": [1.4, 2.6, 420.0, 900]})
    assert s.meeting_title_patterns == ["Zoom", "Web ex"] and s.meeting_rect == [1, 3, 420, 900]
    assert ai.DEFAULTS["translate_timeout_s"] == 6.0 and ai.DEFAULTS["translate_model"] == ""


def test_meeting_dir_default(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    assert config.meeting_dir(config.Settings()) == tmp_path / "Rupor" / "meetings"
    assert config.meeting_dir(config.Settings(meeting_dir=r"D:\M")) == Path(r"D:\M")


# ---------- движок и WebSocket ----------

def _key(env: Env) -> None:
    env.calls.creds["Rupor/openrouter"] = FAKE_KEY
    env.eng.update_settings({"ai_keys": [{"id": "openrouter", "provider": "openrouter"}]})


@pytest.fixture
def translated(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, str, str, list[str]]]:
    seen: list[tuple[str, str, str, list[str]]] = []

    def fake(text: str, src: str, dst: str, s: config.Settings, context_lines: Any = (),
             **kw: Any) -> ai.AIResult:
        seen.append((text, src, dst, list(context_lines)))
        return ai.AIResult(f"RU:{text}", 4.0, 0.0002, "openrouter", "anthropic/claude-haiku-4.5")

    monkeypatch.setattr(ai, "translate", fake)
    monkeypatch.setattr(ai, "translate_stream", fake)    # кусок фразы — потоком
    monkeypatch.setattr(ai, "prewarm", lambda s, connections=1: None)
    return seen


def _say(st: FakeStream, sec: float = 0.6, rec: Any = None) -> None:
    n = len(rec.bg_calls) if rec is not None else 0
    a = np.concatenate([tone(sec), quiet(0.6)])
    for i in range(0, len(a), 800):
        st.on_audio(a[i:i + 800])
    if rec is not None:                        # дождаться распознавания (без склейки)
        wait_for(lambda: len(rec.bg_calls) > n)


def test_meeting_end_to_end_over_ws(env: Env, translated: list[Any],
                                    monkeypatch: pytest.MonkeyPatch) -> None:
    _key(env)
    excluded: list[frozenset[int]] = []
    monkeypatch.setattr(env.eng.ducker, "set_excluded",
                        lambda pids, everything=False: excluded.append(pids))
    clip: list[str] = []
    monkeypatch.setattr(inject, "clipboard_set_text", lambda text, private=False: clip.append(text))
    revealed: list[Path] = []
    monkeypatch.setattr(api, "_reveal_file", revealed.append)
    env.calls.sources = [dict(SYSTEM_SRC), dict(MEET_SRC, meeting=False),
                         dict(MEET_SRC, id="proc:77", pid=77, exe="chrome.exe",
                              name="Google Chrome", meeting=True)]
    c = env.client()
    srcs = c.call("meeting_sources")
    assert [x["id"] for x in srcs] == ["system", "proc:4100", "proc:77"]
    assert set(srcs[0]) == {"id", "kind", "pid", "exe", "name", "title", "meeting", "active",
                            "available"}
    assert all(x["available"] for x in srcs)
    c.mark()
    st = c.call("meeting_start", {})                       # авто: источник со встречей
    assert st["running"] is True and st["source"]["id"] == "proc:77"
    assert env.calls.streams[-1].target == "pid:77" and excluded == [frozenset({77})]
    assert c.wait_event("meeting", lambda d: d["running"])["source"]["pid"] == 77
    assert c.call("get_state")["meeting"]["running"] is True
    env.rec.bg = ["Good morning", "Let's begin"]
    _say(env.calls.streams[-1], rec=env.rec)
    _say(env.calls.streams[-1], rec=env.rec)
    line = c.wait_event("meeting_line", lambda d: d["id"] == 2 and d["status"] == "done")
    assert line["translation"] == "RU:Let's begin" and line["lang"] == "en"
    assert translated[1] == ("Let's begin", "en", "ru", ["Good morning"])
    usage = c.wait_event("ai_usage", lambda d: d["kind"] == "translate", fresh=False)
    assert usage["cost_usd"] == 0.0002
    rows = env.eng.storage.ai_usage_stats()["by_kind"]
    assert {r["kind"]: r["calls"] for r in rows}["translate"] == 2
    assert [x["id"] for x in c.call("meeting_lines")] == [1, 2]
    assert [x["id"] for x in c.call("meeting_lines", {"after_id": 1})] == [2]
    assert c.call("meeting_copy")["chars"] == len(clip[0]) and "RU:Good morning" in clip[0]
    c.call("meeting_open_folder")
    f = env.eng.meeting_file()
    assert f is not None and revealed == [f] and f.parent == env.tmp / "Rupor" / "meetings"
    # диктовка этого не видит: ни result, ни processing, ни _busy
    assert c.events("result") == []
    assert all(d["state"] in ("idle", "loading") for d in c.events("state"))
    assert env.eng._busy == 0 and env.eng._ai_busy is False
    # тот же источник — не перезапускать; другой — перезапуск
    assert c.call("meeting_start", {"source_id": "proc:77"})["started_at"] == st["started_at"]
    n_streams = len(env.calls.streams)
    st2 = c.call("meeting_start", {"source_id": "proc:4100"})
    assert st2["source"]["id"] == "proc:4100" and len(env.calls.streams) == n_streams + 1
    assert env.calls.streams[-2].stopped and excluded[-1] == frozenset({4100})
    st3 = c.call("meeting_stop")
    assert st3["running"] is False and excluded[-1] == frozenset()
    assert env.calls.streams[-1].stopped
    c.close()


def test_whole_sound_meeting_is_never_ducked(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Встреча слушает весь звук компьютера — приглушение диктовки не трогает ничего (иначе
    приглушённая программа встречи пропала бы из захвата); конец встречи — как обычно."""
    calls: list[tuple[frozenset[int], bool]] = []
    monkeypatch.setattr(env.eng.ducker, "set_excluded",
                        lambda pids, everything=False: calls.append((pids, everything)))
    env.calls.sources = [dict(MEET_SRC), dict(SYSTEM_SRC)]
    c = env.client()
    st = c.call("meeting_start", {"source_id": "system"})
    assert st["source"]["id"] == "system" and env.calls.streams[-1].target == "system"
    assert calls == [(frozenset(), True)]
    c.call("meeting_stop")
    assert calls[-1] == (frozenset(), False)
    c.close()


def test_meeting_yields_to_dictation(env: Env, translated: list[Any]) -> None:
    _key(env)
    env.calls.sources = [dict(MEET_SRC)]
    c = env.client()
    c.call("meeting_start", {})
    st = env.calls.streams[-1]
    env.eng._on_actions([Action(Kind.START)])
    wait_for(lambda: env.eng._session is not None)
    env.rec.bg = ["Meeting phrase"]
    _say(st)
    wait_for(lambda: env.rec.bg_yielded > 0)
    assert env.rec.bg_calls == [] and env.eng._meeting_yield() is True
    env.rec.texts.append("диктовка")
    env.eng._on_actions([Action(Kind.STOP)])
    c.wait_event("result", lambda d: d["text"] == "диктовка", fresh=False)
    line = c.wait_event("meeting_line", lambda d: d["status"] == "done", fresh=False)
    assert line["text"] == "Meeting phrase"
    assert env.eng._asr_pending == 0 and env.eng._meeting_yield() is False
    c.close()


def test_dictation_stop_keeps_model_from_meeting_without_gap(
        env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """STOP: место распознавания диктовки занимается в том же замке, где кончается запись, —
    пока задание transcribe ставится (запись, звук «стоп», окно), встреча модель не берёт;
    короткая запись место возвращает."""
    seen: list[bool] = []
    real_stop = env.recorder.stop

    def stop() -> np.ndarray:
        seen.append(env.eng._meeting_yield())
        return real_stop()

    monkeypatch.setattr(env.recorder, "stop", stop)
    env.rec.texts.append("диктовка")
    env.eng._on_actions([Action(Kind.START)])
    wait_for(lambda: env.eng._session is not None)
    env.eng._on_actions([Action(Kind.STOP)])
    wait_for(lambda: len(seen) == 1 and env.eng._busy == 0 and env.eng._asr_pending == 0)
    assert seen == [True] and env.eng._meeting_yield() is False
    env.recorder.seconds = 0.01                   # короткая — без распознавания
    env.eng._on_actions([Action(Kind.START)])
    wait_for(lambda: env.eng._session is not None)
    env.eng._on_actions([Action(Kind.STOP)])
    wait_for(lambda: len(seen) == 2 and env.eng._session is None)
    time.sleep(0.05)
    assert seen == [True, True] and env.eng._asr_pending == 0
    assert env.eng._meeting_yield() is False


def test_meeting_without_key_is_transcript_only(env: Env, translated: list[Any]) -> None:
    env.calls.sources = [dict(MEET_SRC)]
    c = env.client()
    c.mark()
    st = c.call("meeting_start", {})
    assert st["translate"] is False
    n = c.wait_event("notice", lambda d: d["action"] == "open_settings_ai")
    assert "без перевода" in n["text"]
    env.rec.bg = ["Hello"]
    _say(env.calls.streams[-1])
    line = c.wait_event("meeting_line", lambda d: d["id"] == 1)
    assert line["status"] == "original" and translated == []
    # ключ появился — перевод включается сам
    c.call("ai_key_set", {"provider": "openrouter", "key": FAKE_KEY})
    assert c.call("meeting_status")["translate"] is True
    env.rec.bg = ["World"]
    _say(env.calls.streams[-1])
    assert c.wait_event("meeting_line", lambda d: d["id"] == 2 and d["status"] == "done")
    c.close()


def test_old_windows_falls_back_to_whole_sound(env: Env) -> None:
    """Windows старее сборки 20348 (process loopback нет): «auto», источник предложения и
    явный proc: — весь звук компьютера вместо ошибки, уведомление — один раз за запуск;
    процессы в meeting_sources — available: false, MeetingStatus.loopback_available: false."""
    env.eng._loopback_available = lambda: False
    env.calls.sources = [dict(MEET_SRC)]          # «весь звук» не в списке — всё равно есть
    c = env.client()
    assert [(x["id"], x["available"]) for x in c.call("meeting_sources")] == [
        ("proc:4100", False)]
    assert c.call("meeting_status")["loopback_available"] is False
    c.mark()
    st = c.call("meeting_start", {})
    assert st["running"] is True and st["source"]["id"] == "system"
    assert st["loopback_available"] is False and env.calls.streams[-1].target == "system"
    n = c.wait_event("notice", lambda d: "Windows 11" in d["text"])
    assert n["level"] == "info" and "весь звук компьютера" in n["text"]
    # уже идёт «весь звук» — предложение «Начать» (proc:) ничего не перезапускает
    assert c.call("meeting_start", {"source_id": "proc:4100"})["started_at"] == st["started_at"]
    assert len(env.calls.streams) == 1
    c.call("meeting_stop")
    env.calls.sources = [dict(MEET_SRC), dict(SYSTEM_SRC)]
    st2 = env.eng.meeting_start("proc:4100")
    assert st2["source"]["id"] == "system" and env.calls.streams[-1].target == "system"
    env.eng.meeting_stop()
    time.sleep(0.2)
    assert sum("Windows 11" in d["text"] for d in c.events("notice")) == 1
    # Windows 11: процессы доступны
    env.eng._loopback_available = lambda: True
    assert all(x["available"] for x in c.call("meeting_sources"))
    assert meeting.idle_status(env.eng.s, loopback_available=True)["loopback_available"] is True
    c.close()


def test_meeting_start_errors(env: Env) -> None:
    c = env.client()
    env.calls.sources = [dict(MEET_SRC)]
    env.rec._loaded = False
    env.eng._model_phase = "error"
    env.eng._model_error = "Модель tiny не скачана"
    assert "Модель распознавания не готова: Модель tiny не скачана" in c.error("meeting_start")
    assert env.calls.streams == []
    c.close()


def test_meeting_loads_unloaded_model(env: Env, translated: list[Any]) -> None:
    _key(env)
    env.calls.sources = [dict(MEET_SRC)]
    env.rec.unload()
    env.eng._model_phase = "unloaded"
    c = env.client()
    st = c.call("meeting_start", {})
    assert st["running"] is True
    wait_for(lambda: env.rec.loaded)                       # задание load — как перед диктовкой
    env.rec.bg = ["Back again"]
    _say(env.calls.streams[-1])
    assert c.wait_event("meeting_line", lambda d: d["status"] == "done", fresh=False)
    c.close()


def test_idle_unload_waits_for_meeting(env: Env) -> None:
    env.eng.s.unload_after_min = 1
    env.rec.last_used = time.monotonic() - 3600
    assert env.eng._idle_unload_due() is True
    env.calls.sources = [dict(MEET_SRC)]
    env.eng.meeting_start(None)
    assert env.eng._idle_unload_due() is False
    env.eng.meeting_stop()
    assert env.eng._idle_unload_due() is True


def test_shutdown_stops_meeting(env: Env) -> None:
    env.calls.sources = [dict(MEET_SRC)]
    env.eng.meeting_start(None)
    env.eng._meeting_shutdown()
    assert env.eng.meeting_status()["running"] is False and env.calls.streams[-1].stopped


def test_meeting_settings_change_emits_status(env: Env) -> None:
    c = env.client()
    c.mark()
    c.call("set_settings", {"patch": {"meeting_target_lang": "en"}})
    ev = c.wait_event("meeting")
    assert ev["target_lang"] == "en" and ev["running"] is False
    assert "Язык перевода встречи" in c.error("set_settings",
                                              {"patch": {"meeting_target_lang": "xx"}})
    c.close()


# ---------- потоковое распознавание (LocalAgreement-2) и перевод кусками ----------

def test_words_and_agreement_helpers() -> None:
    assert meeting.split_words("Good  morning — everyone, hi") == ["Good", "morning —",
                                                                   "everyone,", "hi"]
    assert meeting.split_words("你好，大家", "zh") == ["你", "好，", "大", "家"]
    assert meeting.join_words(["你", "好，", "大", "家"], "zh") == "你好，大家"
    assert meeting.norm_word("Everyone,") == "everyone" and meeting.norm_word("Ёлка") == "елка"
    n = [meeting.norm_word(w) for w in "Good morning, everyone thanks".split()]
    assert meeting.skip_done(["good", "morning"], n) == 2
    assert meeting.skip_done([], n) == 0
    # модель поправила принятое слово, потеряла или добавила слово — граница по правкам
    assert meeting.skip_done(["good", "morning", "every"], n) == 3
    assert meeting.skip_done(["good", "morning", "everyone", "thanks", "for"], n) == 4
    assert meeting.skip_done(["the", "cat"], ["a", "cat", "sat", "on", "the", "mat"]) == 2
    assert meeting.skip_done(["morning", "all"], ["good", "morning", "all", "of"]) == 3
    assert meeting.agree(["thanks", "for", "join"], ["thanks", "for", "joining", "us"]) == 2
    assert meeting.agree([], ["a"]) == 0
    st = meeting.idle_status(config.Settings())
    assert st["latency"] == {"asr_ms": None, "first_token_ms": None, "done_ms": None}


def _bare(box: Any, **kw: Any) -> Any:
    """Сеанс без потоков: проходы зовутся из теста по одному (детерминированно)."""
    b = box(interim=True, **kw)
    b.sess.started_wall = time.time()
    return b


def test_local_agreement_commits_agreed_prefix(box: Any) -> None:
    b = _bare(box, meeting_translate=False)
    hyps = iter(["Good morning", "Good morning every", "Good morning, everyone thanks",
                 "Good morning everyone, thanks for joining"])
    b.rec.interim = lambda audio_s: next(hyps)
    states = []
    for _ in range(4):
        assert b.sess._interim_pass(1, 0.5, tone(1.0)) is True
        ln = b.sess.lines()[0]
        states.append((ln["text"], ln["partial"], ln["final"], ln["status"]))
    assert states == [("", "Good morning", False, "original"),
                      ("Good morning", "every", False, "original"),
                      ("Good morning", "everyone thanks", False, "original"),
                      ("Good morning everyone, thanks", "for joining", False, "original")]
    # язык: первый проход определяет его, дальше — заданный; beam 1, без повторов
    assert [c["language"] for c in b.rec.interim_calls] == [None, "en", "en", "en"]
    assert all(c["beam_size"] == 1 and c["temperature"] == 0.0 for c in b.rec.interim_calls)
    # конец фразы: последний проход (beam 5, язык фразы) заменяет текст целиком
    b.rec.results = ["Good morning everyone, thanks for joining the weekly sync."]
    u = meeting.Utterance(tone(2.0), 0.5, 2.5, seq=1, speech_end_s=2.3)
    assert b.sess._final_pass(u) is True
    ln = b.sess.lines()[0]
    assert ln["text"] == "Good morning everyone, thanks for joining the weekly sync."
    assert ln["partial"] == "" and ln["final"] is True and ln["status"] == "original"
    assert b.rec.calls[-1]["beam_size"] == 5 and b.rec.calls[-1]["language"] == "en"
    assert {d["id"] for d in b.line_events()} == {1}              # обновления одной строки
    assert b.sess.status()["lines"] == 1
    lat = b.sess.status()["latency"]
    assert lat["asr_ms"] is not None and lat["first_token_ms"] is None


def test_failed_final_pass_closes_live_line(box: Any) -> None:
    """Последний проход фразы со звучащей строкой один раз упал (сбой на GPU): строка
    закрывается принятым — следующие фразы снова идут потоком, файл дописывается дальше."""
    b = box(interim=True, meeting_translate=False)
    b.sess.start()
    b.rec.interim = lambda audio_s: "hello there friends"
    for _ in range(2):                           # две одинаковые гипотезы — всё принято
        b.sess._interim_pass(1, 0.0, tone(1.0))
    assert b.sess._live is not None and b.sess.lines()[0]["text"] == "hello there friends"
    b.rec.results = [RuntimeError("CUDA failed with error out of memory"), "Second phrase."]
    b.sess._enqueue(meeting.Utterance(tone(2.0), 0.0, 2.0, seq=1, speech_end_s=1.9))
    wait_for(lambda: b.sess.lines()[0]["final"])
    ln = b.sess.lines()[0]
    assert (ln["text"], ln["partial"], ln["status"]) == ("hello there friends", "", "original")
    assert b.sess._live is None and b.sess.status()["state"] == "listening"
    # следующая фраза — снова промежуточный проход (своя звучащая строка) и последний
    b.rec.interim = lambda audio_s: "Second"
    assert b.sess._interim_pass(2, 3.0, tone(1.0)) is True
    assert [x["id"] for x in b.sess.lines()] == [1, 2] and b.sess._live is not None
    b.sess._enqueue(meeting.Utterance(tone(1.5), 3.0, 4.5, seq=2, speech_end_s=4.4))
    wait_for(lambda: b.sess.lines()[1]["final"])
    f = b.sess.file
    assert f is not None
    wait_for(lambda: "Second phrase." in f.read_text(encoding="utf-8"))
    assert "hello there friends" in f.read_text(encoding="utf-8")
    assert b.sess.running and b.notices == []


def test_session_follows_actual_model_device(box: Any) -> None:
    """Встречу начали, пока модель не загружена (по настройкам — GPU), а загрузилась она на
    CPU (нет CUDA, откат): режим CPU — без промежуточных проходов, фразы и склейки короче,
    уведомление один раз; модель снова на GPU — потоковое распознавание."""
    b = box(interim=True)
    b.rec.device = None                          # модель ещё не загружена
    b.sess.start()
    assert b.sess._interim is True and b.sess.max_s == meeting.VAD_MAX_S
    b.rec.device = "cpu"
    wait_for(lambda: b.sess._interim is False)
    assert b.sess.max_s == meeting.VAD_MAX_S_CPU
    assert b.sess.merge_max_s == meeting.MERGE_MAX_S_CPU
    vad = b.sess._vad
    assert vad is not None
    assert vad._max_n == round(meeting.VAD_MAX_S_CPU / meeting.FRAME_S)
    assert [n["level"] for n in b.notices] == ["warn"] and "отставать" in b.notices[0]["text"]
    b.rec.device = "cuda"
    wait_for(lambda: b.sess._interim is True)
    assert b.sess.max_s == meeting.VAD_MAX_S and len(b.notices) == 1
    assert vad._max_n == round(meeting.VAD_MAX_S / meeting.FRAME_S)


def test_subtitle_hallucination_is_never_committed(box: Any) -> None:
    b = _bare(box, meeting_translate=False)
    hyps = iter(["Hello.", "Hello.", "Hello. Thank you for watching.",
                 "Hello. Thank you for watching."])
    b.rec.interim = lambda audio_s: next(hyps)
    for _ in range(4):
        b.sess._interim_pass(1, 0.0, tone(1.0))
    ln = b.sess.lines()[0]
    assert ln["text"] == "Hello." and ln["partial"] == "Thank you for watching."


def test_garbage_final_drops_tentative_line_but_keeps_committed(box: Any) -> None:
    b = _bare(box, meeting_translate=False)
    b.rec.interim = lambda audio_s: "Hmm"
    b.sess._interim_pass(1, 0.0, tone(1.0))
    assert b.sess.status()["lines"] == 1
    b.rec.results = [""]
    b.sess._final_pass(meeting.Utterance(tone(1.0), 0.0, 1.0, seq=1))
    ev = b.line_events()[-1]
    assert ev["id"] == 1 and ev["final"] and ev["text"] == "" and ev["partial"] == ""
    assert b.sess.lines() == [] and b.sess.status()["lines"] == 0
    # принятое не теряется: последний проход — мусор, строка остаётся с принятым
    b.rec.interim = lambda audio_s: "Okay then"
    b.sess._interim_pass(2, 2.0, tone(1.0))
    b.sess._interim_pass(2, 2.0, tone(1.2))
    b.rec.results = [""]
    b.sess._final_pass(meeting.Utterance(tone(1.2), 2.0, 3.2, seq=2))
    assert [(x["text"], x["final"]) for x in b.sess.lines()] == [("Okay then", True)]
    # фразу выбросил VAD (щелчок) — её строка уходит
    b.rec.interim = lambda audio_s: "Uh"
    b.sess._interim_pass(3, 4.0, tone(1.0))
    assert len(b.sess.lines()) == 2
    b.sess._discard_seq(3)
    b.sess._check_discarded()
    assert [x["text"] for x in b.sess.lines()] == ["Okay then"]
    assert b.sess.transcript_text().endswith("Okay then")


def test_streaming_translation_chunks_and_refine(box: Any, tmp_path: Path) -> None:
    b = box(interim=True)
    b.tr_impl = lambda text, src, dst, ctx: ai.AIResult(f"<{text}>", 5.0, 0.0001,
                                                        "openrouter", "m")
    b.delta = lambda req: req.on_delta(f"<{req.text[:4]}") if req.on_delta else None
    b.sess.start()
    hyps = iter(["Good morning everyone,", "Good morning everyone, thanks for",
                 "Good morning everyone, thanks for joining the weekly"])
    b.rec.interim = lambda audio_s: next(hyps)
    b.sess._interim_pass(1, 0.0, tone(1.0))
    b.sess._interim_pass(1, 0.0, tone(1.5))   # принято «Good morning everyone,» — граница
    wait_for(lambda: b.sess.lines()[0]["translation"] == "<Good morning everyone,>")
    assert b.sess.lines()[0]["status"] == "streaming"            # фраза ещё звучит
    b.sess._interim_pass(1, 0.0, tone(2.0))   # «thanks for» — два слова без границы: ждут
    time.sleep(0.1)
    assert len(b.reqs) == 1
    b.rec.results = ["Good morning everyone, thanks for joining the weekly sync."]
    b.sess._final_pass(meeting.Utterance(tone(2.5), 0.0, 2.5, seq=1, speech_end_s=2.3))
    wait_for(lambda: b.sess.lines()[0]["refined"])
    r0, r1 = b.reqs
    assert (r0.text, r0.done_src, r0.done_tr) == ("Good morning everyone,", "", "")
    assert r1.text == "thanks for joining the weekly sync."
    assert (r1.done_src, r1.done_tr) == ("Good morning everyone,", "<Good morning everyone,>")
    # перевод показывался по ходу ответа, по кускам; конец — вся фраза разом
    trs = [d["translation"] for d in b.line_events() if d["translation"]]
    assert "<Good" in trs and "<Good morning everyone,> <than" in trs
    assert "<Good morning everyone,> <thanks for joining the weekly sync.>" in trs
    ln = b.sess.lines()[0]
    assert ln["translation"] == "WHOLE:" + ln["text"] and ln["status"] == "done"
    assert [r.text for r in b.refines] == [ln["text"]] and b.refines[0].refine
    lat = b.sess.status()["latency"]
    assert all(lat[k] is not None for k in ("asr_ms", "first_token_ms", "done_ms"))
    f = b.sess.file
    assert f is not None and "WHOLE:Good morning" in f.read_text(encoding="utf-8")


def test_refine_rules(box: Any) -> None:
    b = box(interim=True)
    b.sess.start()
    seq = [0]

    def line(hyp: str, final: str) -> None:
        seq[0] += 1
        b.rec.interim = lambda audio_s: hyp
        b.sess._interim_pass(seq[0], seq[0] * 3.0, tone(1.0))
        b.sess._interim_pass(seq[0], seq[0] * 3.0, tone(1.2))
        n = len(b.reqs)
        wait_for(lambda: len(b.reqs) > n)        # принятое — уже в переводе
        b.rec.results = [final]
        b.sess._final_pass(meeting.Utterance(tone(1.3), seq[0] * 3.0, seq[0] * 3.0 + 1.3,
                                             seq=seq[0]))
        wait_for(lambda: b.sess.lines()[-1]["status"] == "done"
                 and b.sess._by_id[b.sess.lines()[-1]["id"]].tr.settled)

    line("Hello there.", "Hello there.")       # одним куском, текст тот же — без уточнения
    assert b.refines == [] and b.sess.lines()[0]["translation"] == "RU:Hello there."
    line("We fixed two books.", "We fixed two bugs.")   # последний проход поправил слово
    assert [r.text for r in b.refines] == ["We fixed two bugs."]
    ln = b.sess.lines()[1]
    assert ln["refined"] and ln["translation"] == "WHOLE:We fixed two bugs."
    # уточнение выключено — поправленный текст остаётся с прежним переводом, без вызовов
    b.s.meeting_refine = False
    line("Two books left.", "Two bugs left.")
    assert len(b.refines) == 1 and b.sess.lines()[2]["translation"] == "RU:Two books left."
    assert not b.sess.lines()[2]["refined"]


def test_same_language_streams_text_without_ai(box: Any) -> None:
    b = box(interim=True)
    b.sess.start()
    b.rec.interim = lambda audio_s: ChunkResult("Привет всем", "ru", 0.99, audio_s, 5)
    b.sess._interim_pass(1, 0.0, tone(1.0))
    b.sess._interim_pass(1, 0.0, tone(1.2))
    ln = b.sess.lines()[0]
    assert (ln["text"], ln["status"], ln["lang"]) == ("Привет всем", "original", "ru")
    b.rec.results = [ChunkResult("Привет всем, коллеги", "ru", 1.0, 1.3, 5)]
    b.sess._final_pass(meeting.Utterance(tone(1.3), 0.0, 1.3, seq=1))
    ln = b.sess.lines()[0]
    assert ln["text"] == "Привет всем, коллеги" and ln["final"] and ln["status"] == "original"
    time.sleep(0.05)
    assert b.reqs == [] and b.refines == []


def test_two_translation_calls_in_flight_at_most(box: Any) -> None:
    b = box()
    gate = threading.Event()
    lock = threading.Lock()
    inside, peak = [0], [0]

    def slow(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        with lock:
            inside[0] += 1
            peak[0] = max(peak[0], inside[0])
        gate.wait(5)
        with lock:
            inside[0] -= 1
        return ai.AIResult(f"RU:{text}", 1.0, 0.0, "openrouter", "m")

    b.tr_impl = slow
    b.sess.start()
    b.rec.results = ["One.", "Two.", "Three."]
    for _ in range(3):
        b.say()
    wait_for(lambda: len(b.tr_calls) >= 2)
    time.sleep(0.15)
    assert len(b.tr_calls) == 2 and peak[0] == 2         # третья строка ждёт
    gate.set()
    ls = b.wait_lines(3)
    assert [x["status"] for x in ls] == ["done"] * 3 and peak[0] == 2


def test_streaming_end_to_end_with_vad(box: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(meeting, "MEETING_STEP_S", 0.05)
    monkeypatch.setattr(meeting, "INTERIM_MIN_S", 0.3)
    monkeypatch.setattr(meeting, "INTERIM_NEW_S", 0.05)
    b = box(interim=True)
    words = "Good morning everyone, thanks for joining the weekly sync.".split()
    # гипотеза — слова, которые «прозвучали» к концу окна (по слову на 0.25 с после 0.2 с)
    b.rec.interim = lambda audio_s: " ".join(words[:max(0, int((audio_s - 0.2) / 0.25))])
    b.rec.results = [" ".join(words)]
    b.sess.start()
    a = tone(2.4)
    for i in range(0, len(a), 1600):                     # 0.1 с звука за 20 мс
        b.stream.on_audio(a[i:i + 1600])
        time.sleep(0.02)
    b.audio(quiet(0.6))
    wait_for(lambda: bool(b.sess.lines()) and b.sess.lines()[0]["final"]
             and b.sess.lines()[0]["status"] == "done")
    evs = [d for d in b.line_events() if d["id"] == 1]
    assert len(b.rec.interim_calls) >= 3 and len(b.rec.calls) == 1
    # принятое только растёт; хвост был; конец — текст последнего прохода целиком
    texts = [d["text"] for d in evs]
    assert all(y.startswith(x) for x, y in zip(texts, texts[1:])), texts
    assert any(d["partial"] for d in evs) and evs[-1]["partial"] == ""
    assert evs[-1]["text"] == " ".join(words) and evs[-1]["translation"]
    assert any(not d["final"] and d["text"] for d in evs)     # принятое — до конца фразы
    assert b.sess.status()["latency"]["asr_ms"] is not None


def test_engine_translator_streams_chunks_and_refines_whole(env: Env,
                                                            monkeypatch: pytest.MonkeyPatch
                                                            ) -> None:
    calls: list[tuple[Any, ...]] = []

    def stream(text: str, src: str, dst: str, s: config.Settings, context_lines: Any = (),
               **kw: Any) -> ai.AIResult:
        calls.append(("stream", text, list(context_lines), kw["done_src"], kw["done_tr"],
                      kw["on_delta"] is not None, kw["terms"]))
        return ai.AIResult("кусок", 1.0, 0.0001, "openrouter", "m")

    def whole(text: str, src: str, dst: str, s: config.Settings, context_lines: Any = (),
              **kw: Any) -> ai.AIResult:
        calls.append(("whole", text, list(context_lines)))
        return ai.AIResult("целиком", 1.0, 0.0001, "openrouter", "m")

    monkeypatch.setattr(ai, "translate_stream", stream)
    monkeypatch.setattr(ai, "translate", whole)
    fn = env.eng._meeting_translator("brave.exe", ["ClickHouse"])
    assert fn(meeting.TrRequest("b c", "en", "ru", ["a"], "a0", "А0", False,
                                lambda t: None)).text == "кусок"
    assert fn(meeting.TrRequest("a0 b c", "en", "ru", ["a"], refine=True)).text == "целиком"
    assert calls == [("stream", "b c", ["a"], "a0", "А0", True, ["ClickHouse"]),
                     ("whole", "a0 b c", ["a"])]
    rows = env.eng.storage.ai_usage_stats()["by_kind"]
    assert {r["kind"]: r["calls"] for r in rows}["translate"] == 2   # строка на вызов


def test_language_needs_confidence_then_meeting_language(box: Any) -> None:
    b = _bare(box, meeting_translate=False)

    def run(seq: int, hyps: list[Any], final: Any, start: float) -> dict[str, Any]:
        it = iter(hyps)
        b.rec.interim = lambda audio_s: next(it)
        for _ in hyps:
            b.sess._interim_pass(seq, start, tone(1.0))
        b.rec.results = [final]
        b.sess._final_pass(meeting.Utterance(tone(1.5), start, start + 1.5, seq=seq))
        return b.sess.lines()[-1]

    calls = b.rec.interim_calls
    # язык короткого начала неуверенный — не фиксируется, слова в нём не принимаются
    ln = run(1, [ChunkResult("Гуд морнинг", "ru", 0.52, 0.8, 5),
                 ChunkResult("Good morning every", "en", 0.93, 1.6, 5), "Good morning everyone"],
             "Good morning everyone.", 0.0)
    assert [c["language"] for c in calls] == [None, None, "en"]
    assert [d["text"] for d in b.line_events() if d["id"] == 1][:3] == ["", "", "Good morning"]
    assert b.rec.calls[-1]["language"] == "en"           # уверенный язык — и последнему проходу
    assert ln["lang"] == "en"
    # три прохода без уверенности — последний определённый, «слабо»: последний проход
    # определяет язык по всей фразе
    n = len(calls)
    ln = run(2, [ChunkResult("Let's", "ru", 0.3, 0.8, 5),
                 ChunkResult("Let's start", "ru", 0.3, 1.6, 5),
                 ChunkResult("Let's start with", "en", 0.4, 2.4, 5)],
             "Let's start with the release.", 3.0)
    assert [c["language"] for c in calls[n:]] == [None, None, None]
    assert b.rec.calls[-1]["language"] is None and ln["lang"] == "en"
    # две строки подряд на одном языке (язык встречи): первый проход фразы всё равно
    # определяет язык; неуверенно (акцент) — дальше с языком встречи, «слабо», и последний
    # проход проверяет: фраза оказалась русской
    n = len(calls)
    ln = run(3, [ChunkResult("Thanks", "en", 0.6, 0.8, 5), "Thanks all"],
             ChunkResult("Спасибо всем", "ru", 0.97, 1.5, 5), 6.0)
    assert [c["language"] for c in calls[n:]] == [None, "en"]
    assert b.rec.calls[-1]["language"] is None
    assert (ln["lang"], ln["text"]) == ("ru", "Спасибо всем")
    # первый проход уверенно узнал другой язык — сразу он; последний проход — с ним же
    b.sess._final_langs.extend(["en", "en"])
    n = len(calls)
    ln = run(4, [ChunkResult("Привет", "ru", 0.99, 0.8, 5), "Привет всем"],
             "Привет всем, коллеги.", 9.0)
    assert [c["language"] for c in calls[n:]] == [None, "ru"]
    assert b.rec.calls[-1]["language"] == "ru" and ln["lang"] == "ru"
    # уверенно язык встречи — последний проход без повторного определения (−0.2 с)
    b.sess._final_langs.extend(["en", "en"])
    n = len(calls)
    ln = run(5, [ChunkResult("Next week", "en", 0.99, 0.8, 5), "Next week we"],
             "Next week we ship.", 12.0)
    assert [c["language"] for c in calls[n:]] == [None, "en"]
    assert b.rec.calls[-1]["language"] == "en" and ln["lang"] == "en"


# ---------- язык фразы: письменность прохода, смена языка на последнем проходе ----------

def test_script_mismatch_helper() -> None:
    assert meeting.script_mismatch("Colleegi, я коротко добавлю", "en") == "ru"
    assert meeting.script_mismatch("Good morning everyone", "ru") == "en"
    assert meeting.script_mismatch("Good morning everyone", "en") is None
    assert meeting.script_mismatch("Деплой в Kubernetes кластер", "ru") is None   # термины
    assert meeting.script_mismatch("Ok", "ru") is None                           # мало букв
    assert meeting.script_mismatch("我们开始吧 meeting", "zh") is None             # не проверяется


def test_meeting_language_on_other_language_speech_is_redetected(box: Any) -> None:
    """Две строки по-английски (язык встречи en), затем русская фраза: проход с en пишет
    кириллицей — язык фразы определяется заново, перевод не начинается, строка — original."""
    b = box(interim=True)
    b.sess.start()
    b.sess._final_langs.extend(["en", "en"])

    def hyp(audio_s: float) -> Any:
        forced = b.rec.interim_calls[-1]["language"]
        if forced == "en":                      # Whisper с en на русской речи
            return ChunkResult("Colleegi, я коротко добавлю,", "en", 1.0, audio_s, 5)
        if audio_s < 0.9:                       # самое начало — язык неуверенно
            return ChunkResult("Коллеги, я", "ru", 0.6, audio_s, 5)
        words = "Коллеги, я коротко добавлю, демонстрацию переносим".split()
        n = 4 if audio_s < 1.3 else 6
        return ChunkResult(" ".join(words[:n]), "ru", 0.99, audio_s, 5)

    b.rec.interim = hyp
    b.sess._interim_pass(1, 0.0, tone(0.8))     # определение неуверенно — язык не выбран
    b.sess._interim_pass(1, 0.0, tone(1.0))     # дальше — язык встречи: не та письменность
    ln = b.sess.lines()[0]
    assert (ln["lang"], ln["text"], ln["status"]) == ("ru", "", "original")
    assert ln["partial"] == "Colleegi, я коротко добавлю,"
    b.sess._interim_pass(1, 0.0, tone(1.2))     # язык определяется снова: ru, уверенно
    b.sess._interim_pass(1, 0.0, tone(1.4))     # дальше — с ним; совпавшее принято
    assert [c["language"] for c in b.rec.interim_calls] == [None, "en", None, "ru"]
    ln = b.sess.lines()[0]
    assert ln["text"] == "Коллеги, я коротко добавлю," and ln["status"] == "original"
    b.rec.results = [ChunkResult("Коллеги, я коротко добавлю: демонстрацию переносим.", "ru",
                                 1.0, 1.5, 5)]
    b.sess._final_pass(meeting.Utterance(tone(1.5), 0.0, 1.5, seq=1))
    time.sleep(0.1)
    ln = b.sess.lines()[0]
    assert (ln["lang"], ln["status"], ln["translation"]) == ("ru", "original", None)
    assert b.reqs == [] and b.refines == []
    # следующая фраза — снова с языком встречи (другая фраза, проверка не мешает)
    b.sess._final_langs.extend(["en", "en"])
    b.rec.interim = lambda audio_s: ChunkResult("Thanks", "en", 0.7, audio_s, 5)
    b.sess._interim_pass(2, 3.0, tone(0.8))
    b.rec.interim = lambda audio_s: "Thanks everyone"
    b.sess._interim_pass(2, 3.0, tone(1.0))
    assert [c["language"] for c in b.rec.interim_calls[-2:]] == [None, "en"]


def test_language_change_at_final_pass_discards_chunk_in_flight(box: Any) -> None:
    """Язык встречи (слабо) en, а фраза русская и Whisper писал по-английски: кусок ушёл в
    перевод; последний проход определил ru — перевод строки сброшен сразу, а ответ куска,
    пришедший позже, строке не показывается."""
    b = box(interim=True)
    gate = threading.Event()

    def slow(text: str, src: str, dst: str, ctx: list[str]) -> ai.AIResult:
        gate.wait(5)
        return ai.AIResult(f"RU:{text}", 1.0, 0.0, "openrouter", "m")

    b.tr_impl = slow
    b.delta = lambda req: req.on_delta("RU:Coll") if req.on_delta else None
    b.sess.start()
    b.sess._final_langs.extend(["en", "en"])
    # первый проход: определение неуверенно; дальше — язык встречи (Whisper пишет перевод)
    b.rec.interim = lambda audio_s: ChunkResult("Colleagues, I will add briefly.", "en", 0.7,
                                                audio_s, 5)
    b.sess._interim_pass(1, 0.0, tone(1.0))
    b.rec.interim = lambda audio_s: "Colleagues, I will add briefly."
    b.sess._interim_pass(1, 0.0, tone(1.2))
    assert [c["language"] for c in b.rec.interim_calls] == [None, "en"]
    wait_for(lambda: len(b.reqs) == 1)
    wait_for(lambda: b.sess.lines()[0]["translation"] == "RU:Coll")
    b.rec.results = [ChunkResult("Коллеги, я коротко добавлю.", "ru", 0.99, 1.5, 5)]
    b.sess._final_pass(meeting.Utterance(tone(1.5), 0.0, 1.5, seq=1))
    assert b.rec.calls[-1]["language"] is None          # слабый язык — определить заново
    ln = b.sess.lines()[0]
    assert (ln["lang"], ln["status"], ln["translation"]) == ("ru", "original", None)
    gate.set()
    time.sleep(0.2)
    ln = b.sess.lines()[0]
    assert (ln["status"], ln["translation"], ln["refined"]) == ("original", None, False)
    assert len(b.reqs) == 1 and b.refines == []
    assert b.line_events()[-1]["translation"] is None
