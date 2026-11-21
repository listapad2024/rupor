"""Заметки к встрече: ai.meeting_notes (модель по провайдеру, тело запроса, промпт, очистка
ответа, длинная встреча частями и сведением, поток), когда сеанс их пишет (сам после «Стоп» по
порогам, по команде — одни в полёте, название из заметок вместо отдельного вызова, тот — если
заметок нет или они не удались), файл (заметки между заголовком и «## Полная расшифровка»,
замена, пока строки дописываются, длительность), ошибки (нет ключа — одно уведомление), учёт
расхода (kind notes, оплаченный негодный ответ — с ценой) и WS-команды meeting_notes,
meeting_notes_get, meeting_copy {what}.

Здесь же — четыре мелочи названия записи: оплаченный ответ, который не название, пишется в
расход с ценой; строка, опоздавшая к закрытию файла, дописывается; название без ИИ из
заголовка окна — не длиннее 80 символов; временный файл переписывания — с коротким именем.

Сеть и Credential Manager подменены (test_ai.Recorder/StreamRecorder, test_api.env), файлы —
во временной папке."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from rupor import ai, api, config, inject, meeting
from rupor.meeting import MeetingLine, NotesRequest, TranscriptWriter
from test_ai import (CYR, KEY, FakeStreamResp, Recorder, StreamRecorder,  # noqa: F401
                     http_error, or_delta, or_end, or_reply, settings, sse, srec)
from test_api import FAKE_KEY, MEET_SRC, Env, env  # noqa: F401
from test_api_usage_rules_vocab import usage_rows
from test_meeting import _say, box, translated, wait_for  # noqa: F401
from test_meeting_title import Titles, _body, _done, _lines

T0 = datetime(2026, 10, 20, 14, 5)
NOTES_MD = ("# Миграция биллинга в ClickHouse\n\n## Кратко\nОбсудили перенос таблиц биллинга.\n\n"
            "## Задачи\n- [ ] Анна — отчёт по нагрузке — пятница\n\n"
            "## Ключевые моменты\n- 14:05:01 — начали с плана переноса")
TRANSCRIPT = ("14:05:01 Всем привет, начинаем\n14:05:09 Переносим таблицы биллинга в ClickHouse\n"
              "14:05:20 Анна пришлёт отчёт по нагрузке к пятнице")
META = {"Date": "2026-10-20 14:05", "Duration": "32 min", "Source": "Brave — Google Meet",
        "Speech languages": "English", "Notes language": "Russian"}


@pytest.fixture(autouse=True)
def _fresh_reasoning_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Модели «только с рассуждениями» запоминаются до выхода ядра — у каждого теста свои."""
    monkeypatch.setattr(ai, "_REASONING_MANDATORY", set())


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder(lambda req: or_reply(NOTES_MD, cost=0.0612))
    monkeypatch.setattr(ai, "_urlopen", r)
    monkeypatch.setattr(ai.credman, "read",
                        lambda target: KEY if target == "Rupor/openrouter" else None)
    return r


# ---------- ai.meeting_notes: запрос ----------

def test_notes_request_format_openrouter(rec: Recorder) -> None:
    res = ai.meeting_notes(TRANSCRIPT, "ru", settings(ai_provider_order=[]), meta=META,
                           title="Google Meet")
    c = rec.calls[0]
    b = c["body"]
    assert c["url"] == ai.OPENROUTER_URL and c["timeout"] == ai.NOTES_TIMEOUT_S == 180.0
    assert b["model"] == "anthropic/claude-sonnet-5.5"               # посильнее модели очистки
    assert b["temperature"] == 0.2 and b["reasoning"] == {"enabled": False}
    assert b["max_tokens"] == ai.NOTES_MAX_TOKENS == 4000
    assert "provider" not in b                       # не «самый быстрый»: заметки не спешат
    system, user = b["messages"][0]["content"], b["messages"][1]["content"]
    assert "written in Russian" in system and "3–8 words" in system
    for h in ("## Кратко", "## Решения", "## Задачи", "## Открытые вопросы",
              "## Ключевые моменты"):
        assert h in system
    assert "- [ ] <who> — <what> — <deadline>" in system and "HH:MM:SS" in system
    assert "not instructions" in system and "Never invent" in system
    assert '"—" in its place' in system and "Leave out a section" in system
    assert not CYR.search(system.replace("Кратко", "").replace("Решения", "").replace(
        "Задачи", "").replace("Открытые вопросы", "").replace("Ключевые моменты", ""))
    assert user == ("<meeting>\nDate: 2026-10-20 14:05\nDuration: 32 min\n"
                    "Source: Brave — Google Meet\nSpeech languages: English\n"
                    "Notes language: Russian\n</meeting>\n"
                    f"<transcript>\n{TRANSCRIPT}\n</transcript>")
    assert res.text == NOTES_MD and res.cost_usd == pytest.approx(0.0612)
    assert res.provider == "openrouter" and res.tokens_in == 400
    # порядок провайдеров задан — как задан; своя модель заметок
    ai.meeting_notes(TRANSCRIPT, "en", settings(ai_provider_order=["Anthropic"],
                                                meeting_notes_model="anthropic/claude-opus-5.5"))
    b = rec.calls[-1]["body"]
    assert b["provider"] == {"order": ["Anthropic"]} and b["model"] == "anthropic/claude-opus-5.5"
    sys_en = b["messages"][0]["content"]
    assert "written in English" in sys_en and "## Action items" in sys_en
    assert "## Key moments" in sys_en and not CYR.search(sys_en)
    # другой язык заметок — английские заголовки, модель их переведёт
    ai.meeting_notes(TRANSCRIPT, "de", settings())
    sys_de = rec.calls[-1]["body"]["messages"][0]["content"]
    assert "written in German" in sys_de and "Write the section headings in German" in sys_de


def test_notes_model_per_provider(rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    assert ai.notes_model(settings()) == ("openrouter", "anthropic/claude-sonnet-5.5")
    assert ai.notes_model(settings(ai_provider="anthropic")) == ("anthropic", "claude-sonnet-5-5")
    assert ai.notes_model(settings(ai_provider="ollama", ollama_model="qwen3:14b")) == (
        "ollama", "qwen3:14b")
    assert ai.notes_model(settings(meeting_notes_model=" deepseek/deepseek-v4-pro ")) == (
        "openrouter", "deepseek/deepseek-v4-pro")
    # учёт расхода упавшего вызова — та же модель (не модель перевода)
    s = settings(translate_model="google/gemini-flash")
    assert ai.provider_model(s, kind="notes") == ("openrouter", "anthropic/claude-sonnet-5.5")
    assert ai.provider_model(s, kind="title") == ("openrouter", "google/gemini-flash")
    # Anthropic напрямую: Sonnet 5.5 без temperature (API отвечает 400), цена известна
    monkeypatch.setattr(ai.credman, "read", lambda t: "sk-ant-api03-zzz")
    rec.reply = lambda req: {"content": [{"type": "text", "text": NOTES_MD}],
                             "stop_reason": "end_turn",
                             "usage": {"input_tokens": 20_000, "output_tokens": 2_000}}
    r = ai.meeting_notes(TRANSCRIPT, "ru", settings(ai_provider="anthropic"))
    b = rec.calls[-1]["body"]
    assert b["model"] == "claude-sonnet-5-5" and "temperature" not in b
    # размышления Sonnet 5.5 не выключить — усилие low и запас лимита под них
    assert b["max_tokens"] == 4000 + ai.THINKING_HEADROOM_TOKENS
    assert b["output_config"] == {"effort": "low"} and rec.calls[-1]["timeout"] == 180.0
    assert r.cost_usd == pytest.approx((20_000 * 2 + 2_000 * 10) / 1e6)
    assert ai.anthropic_price("claude-opus-5-5") == (4.0, 20.0)
    assert ai.anthropic_price("claude-haiku-5-5-20261001") == (0.1, 0.5)
    ai.meeting_notes(TRANSCRIPT, "ru", settings(ai_provider="anthropic",
                                                meeting_notes_model="claude-haiku-5-5"))
    assert "temperature" not in rec.calls[-1]["body"]   # Haiku 5.5 его тоже не принимает
    ai.meeting_notes(TRANSCRIPT, "ru", settings(ai_provider="anthropic",
                                                meeting_notes_model="claude-haiku-4-5"))
    assert rec.calls[-1]["body"]["temperature"] == 0.2
    # Ollama — модель Ollama, та же температура и лимит; окно контекста — не меньше 8К
    rec.reply = lambda req: {"message": {"content": NOTES_MD}, "done_reason": "stop"}
    r = ai.meeting_notes(TRANSCRIPT, "ru", settings(ai_provider="ollama"))
    b = rec.calls[-1]["body"]
    assert b["model"] == "qwen2.5:7b" and b["options"] == {"temperature": 0.2,
                                                           "num_predict": 4000,
                                                           "num_ctx": 8192}
    assert r.cost_usd == 0.0 and rec.calls[-1]["local"] is True


# ---------- ai.meeting_notes: ответ ----------

@pytest.mark.parametrize("out,expected", [
    (NOTES_MD, NOTES_MD),
    # ```-блок вокруг и вступление до первого заголовка
    ("```markdown\n" + NOTES_MD + "\n```", NOTES_MD),
    ("Вот заметки к встрече:\n\n" + NOTES_MD, NOTES_MD),
    # вступление перед ```-строкой и приписка после закрывающей: закрывающая — тоже прочь
    ("Вот заметки к встрече:\n```markdown\n" + NOTES_MD + "\n```", NOTES_MD),
    ("```markdown\n" + NOTES_MD + "\n```\n\nНадеюсь, это поможет!", NOTES_MD),
    # названия нет или оно не название — название записи
    ("## Кратко\nОбсудили перенос.", "# Google Meet\n\n## Кратко\nОбсудили перенос."),
    ("# Встреча\n\n## Кратко\nОбсудили перенос.", "# Google Meet\n\n## Кратко\nОбсудили перенос."),
    # «Название:», кавычки и точка в названии — прочь; второй «# …» — раздел
    ("# Название: «Итоги спринта».\n\n## Кратко\nA.\n\n# Решения\n- B",
     "# Итоги спринта\n\n## Кратко\nA.\n\n## Решения\n- B"),
    # лишние пустые строки, пробелы в конце, невидимые символы
    ("# Итоги" + chr(0x200B) + " спринта\n\n\n\n## Кратко   \nA." + chr(0x202E),
     "# Итоги спринта\n\n## Кратко\nA."),
    # не заметки: пусто, без букв, отказ
    ("", ""), ("```\n```", ""), ("# 2026-10-20\n\n---", ""),
    ("I'm sorry, I can't help with that.", ""),
])
def test_clean_notes(out: str, expected: str) -> None:
    assert ai.clean_notes(out, "Google Meet") == expected


def test_clean_notes_caps_length_and_parts_have_no_title() -> None:
    long = "# Итоги\n\n## Кратко\n" + "\n".join(f"- пункт {i} " + "x" * 60 for i in range(600))
    out = ai.clean_notes(long, "T")
    assert len(out) <= ai.NOTES_MAX_CHARS + 20 and out.startswith("# Итоги\n\n## Кратко\n- пункт 0")
    assert out.endswith("x")                               # по строке, а не посреди слова
    assert ai.clean_notes(NOTES_MD, "T", part=True).startswith("## Кратко\n")
    assert ai.notes_title(NOTES_MD) == "Миграция биллинга в ClickHouse"
    assert ai.notes_title("## Кратко\nA") == ""
    assert ai.notes_display("```markdown\n# Ит") == "# Ит" and ai.notes_display("```mark") == ""


def test_unusable_notes_are_an_error_with_their_paid_cost(rec: Recorder) -> None:
    rec.reply = lambda req: or_reply("Извините, я не могу помочь с этим.", cost=0.031)
    tracked: list[Any] = []

    def track(call: Any) -> ai.AIResult:
        try:
            r = call()
        except ai.AIError as e:
            tracked.append(e)
            raise
        tracked.append(r)
        return r

    with pytest.raises(ai.AIError) as ei:
        ai.meeting_notes(TRANSCRIPT, "ru", settings(), track=track)
    e = ei.value
    assert e.kind == "bad_response" and "заметки" in str(e)
    assert e.result is not None and e.result.cost_usd == pytest.approx(0.031)
    assert tracked == [e]                                  # учёт видит ошибку и её цену
    # обрезанный по лимиту — тоже оплачен
    rec.reply = lambda req: or_reply(NOTES_MD, cost=0.07, finish="length")
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_notes(TRANSCRIPT, "ru", settings())
    assert ei.value.kind == "truncated" and ei.value.result is not None
    assert ei.value.result.cost_usd == pytest.approx(0.07)
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_notes("  \n ", "ru", settings())
    assert ei.value.kind == "bad_request" and ei.value.result is None


def test_long_meeting_is_summarised_in_parts_then_merged(rec: Recorder,
                                                         monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "NOTES_SINGLE_CHARS", 1000)
    monkeypatch.setattr(ai, "NOTES_CHUNK_CHARS", 400)
    monkeypatch.setattr(ai, "NOTES_MAX_CHUNKS", 3)
    lines = [f"14:{i // 60:02d}:{i % 60:02d} строка {i:03d} про биллинг" for i in range(100)]

    def reply(req: Any) -> Any:
        import json
        system = json.loads(req.data)["messages"][0]["content"]
        if system.startswith("You merge"):
            return or_reply(NOTES_MD, cost=0.02)
        n = system.split("notes for part ", 1)[1].split(" ", 1)[0]
        return or_reply(f"# Не нужно\n\n## Кратко\nЧасть {n}.", cost=0.01)

    rec.reply = reply
    calls: list[str] = []

    def track(call: Any) -> ai.AIResult:
        r: ai.AIResult = call()
        calls.append(r.text)
        return r

    res = ai.meeting_notes("\n".join(lines), "ru", settings(), meta=META, track=track)
    assert len(rec.calls) == 4 and len(calls) == 4         # три части и сведение
    parts = [c["body"] for c in rec.calls[:3]]
    for i, b in enumerate(parts, 1):
        system, user = b["messages"][0]["content"], b["messages"][1]["content"]
        assert f"notes for part {i} of 3" in system and "without a title" in system
        assert b["max_tokens"] == ai.NOTES_PART_MAX_TOKENS
        assert user.startswith("<meeting>\nDate: 2026-10-20 14:05\n")
    users = [b["messages"][1]["content"] for b in parts]
    got = [x for u in users for x in u.split("<transcript>\n")[1].split("\n</transcript>")[0]
           .splitlines()]
    assert got == lines                                    # каждая строка — ровно в одной части
    merge = rec.calls[3]["body"]
    assert merge["messages"][0]["content"].startswith("You merge") and merge["max_tokens"] == 4000
    mu = merge["messages"][1]["content"]
    assert '<part n="1">\n## Кратко\nЧасть 1.\n</part>' in mu and '<part n="3">' in mu
    assert "# Не нужно" not in mu                          # у частей названия нет
    assert calls[:3] == ["## Кратко\nЧасть 1.", "## Кратко\nЧасть 2.", "## Кратко\nЧасть 3."]
    assert res.text == NOTES_MD and res.cost_usd == pytest.approx(0.05)
    assert res.tokens_in == 1600 and res.tokens_out == 240
    # частей не больше NOTES_MAX_CHUNKS, сколько бы ни было строк: вызовов не больше 4
    many = [f"14:00:00 {'слово ' * 20}{i}" for i in range(2000)]
    chunks = ai.notes_chunks(many)
    assert len(chunks) <= 3 and [x for c in chunks for x in c] == many
    assert ai.notes_chunks(lines[:5]) == [lines[:5]]


MANDATORY = {"error": {"code": 400, "message": "Reasoning is mandatory for this endpoint and "
                                               "cannot be disabled."}}


def test_model_that_needs_reasoning_is_asked_again_with_low_effort(rec: Recorder) -> None:
    """Claude Sonnet 5.5 в OpenRouter без рассуждений не отвечает (400, «Reasoning is
    mandatory») — сразу ещё раз с наименьшими и запасом токенов на них; дальше — сразу так.
    Другие 400 — ошибка без повтора."""
    replies: list[Any] = [http_error(400, MANDATORY), or_reply(NOTES_MD, cost=0.011)]
    rec.reply = lambda req: replies.pop(0)
    r = ai.meeting_notes(TRANSCRIPT, "ru", settings())
    assert r.text == NOTES_MD and r.cost_usd == pytest.approx(0.011) and len(rec.calls) == 2
    first, second = rec.calls[0]["body"], rec.calls[1]["body"]
    assert first["reasoning"] == {"enabled": False} and first["max_tokens"] == 4000
    assert second["reasoning"] == {"effort": "low"}
    assert second["max_tokens"] == ai.NOTES_MAX_TOKENS + ai.NOTES_REASONING_TOKENS == 8000
    rec.reply = lambda req: or_reply(NOTES_MD)
    ai.meeting_notes(TRANSCRIPT, "ru", settings())
    assert len(rec.calls) == 3 and rec.calls[2]["body"]["reasoning"] == {"effort": "low"}
    # другая модель — снова без рассуждений; ошибка не про рассуждения — без повтора
    rec.reply = lambda req: http_error(400, {"error": {"message": "Unknown model"}})
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_notes(TRANSCRIPT, "ru", settings(meeting_notes_model="x/unknown"))
    assert ei.value.kind == "bad_request" and len(rec.calls) == 4
    assert rec.calls[3]["body"]["reasoning"] == {"enabled": False}
    assert ei.value.detail == "Unknown model" and not ai.reasoning_mandatory(ei.value)
    # очистка и перевод так не повторяются: рассуждения им не нужны
    assert ai._openrouter_body("s", "u", settings(), "m", 10)["reasoning"] == {"enabled": False}


def test_stream_needing_reasoning_is_asked_again(srec: StreamRecorder) -> None:
    import json
    replies: list[Any] = [FakeStreamResp([], status=400, body=json.dumps(MANDATORY).encode()),
                          FakeStreamResp(sse(or_delta(NOTES_MD), or_end(cost=0.02)))]
    srec.reply = lambda body: replies.pop(0)
    seen: list[str] = []
    r = ai.meeting_notes(TRANSCRIPT, "ru", settings(), on_delta=seen.append)
    assert r.text == NOTES_MD and seen and len(srec.calls) == 2
    assert srec.calls[0]["body"]["reasoning"] == {"enabled": False}
    assert srec.calls[1]["body"]["reasoning"] == {"effort": "low"}
    assert srec.calls[1]["body"]["max_tokens"] == 8000 and srec.calls[1]["body"]["stream"] is True


def test_notes_stream_shows_text_as_it_comes(srec: StreamRecorder) -> None:
    srec.reply = lambda body: FakeStreamResp(sse(
        or_delta("```markdown\n# Миграция"), or_delta(" биллинга\n\n## Кратко\nОбсудили."),
        or_end(cost=0.04)))
    seen: list[str] = []
    res = ai.meeting_notes(TRANSCRIPT, "ru", settings(), meta=META, on_delta=seen.append)
    b = srec.calls[0]["body"]
    assert b["stream"] is True and b["temperature"] == 0.2 and "provider" not in b
    assert b["model"] == "anthropic/claude-sonnet-5.5" and srec.calls[0]["timeout"] == 180.0
    assert seen == ["# Миграция", "# Миграция биллинга\n\n## Кратко\nОбсудили."]
    assert res.text == "# Миграция биллинга\n\n## Кратко\nОбсудили."
    assert res.cost_usd == pytest.approx(0.04)


def test_rejected_title_and_truncated_answers_keep_their_paid_result(rec: Recorder) -> None:
    rec.reply = lambda req: or_reply("Встреча", cost=0.0004)
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_title(TRANSCRIPT, "ru", settings())
    assert ei.value.kind == "bad_response" and ei.value.result is not None
    assert ei.value.result.cost_usd == pytest.approx(0.0004)
    rec.reply = lambda req: or_reply("Итоги", cost=0.0005, finish="length")
    with pytest.raises(ai.AIError) as ei:
        ai.cleanup("ну короче надо обновить дашборд", "normal", settings(), [])
    assert ei.value.kind == "truncated" and ei.value.result is not None
    assert ei.value.result.cost_usd == pytest.approx(0.0005)
    assert ai.cap_title("слово " * 30, 20) == "слово слово слово"
    assert ai.cap_title("  коротко  ") == "коротко"


# ---------- файл: заметки между заголовком и расшифровкой ----------

def _line(i: int, text: str | None = None) -> MeetingLine:
    return MeetingLine(i, float(i), f"14:{i // 60 % 60:02d}:{i % 60:02d}", "en",
                       text or f"line {i}", None, "original")


HEAD = ("# Google Meet\n\n- Дата: 2026-10-20 14:05\n- Источник: Brave — Meet\n"
        "- Языки: en\n\n")


def test_file_layout_notes_inserted_replaced_and_duration(tmp_path: Path) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave — Meet", "Google Meet", "Языки: en")
    w.write(_line(1))
    assert w.path is not None
    assert w.path.read_text(encoding="utf-8") == (
        HEAD + "## Полная расшифровка\n\n**14:00:01** line 1\n\n")
    assert w.set_notes(NOTES_MD) is True                    # «# …» заметок — прочь
    w.write(_line(2))
    body = NOTES_MD.split("\n", 2)[2]
    assert w.path.read_text(encoding="utf-8") == (
        HEAD + body + "\n\n## Полная расшифровка\n\n**14:00:01** line 1\n\n"
        "**14:00:02** line 2\n\n")
    # заметки заново — прежние заменяются; длительность — в строке даты
    assert w.set_notes("## Кратко\nДругое.") is True
    assert w.set_minutes(42) is True
    w.write(_line(3))
    w.close()
    assert w.path.read_text(encoding="utf-8") == (
        "# Google Meet\n\n- Дата: 2026-10-20 14:05, длительность 42 мин\n"
        "- Источник: Brave — Meet\n- Языки: en\n\n## Кратко\nДругое.\n\n"
        "## Полная расшифровка\n\n**14:00:01** line 1\n\n**14:00:02** line 2\n\n"
        "**14:00:03** line 3\n\n")
    assert sorted(tmp_path.iterdir()) == [w.path]
    # после close — тоже (заметки после «Стоп»); «## Полная расшифровка» в заметках — их конец
    assert w.set_notes("## Кратко\nИтог.\n## Полная расшифровка\n**14:00:09** чужая") is True
    text = w.path.read_text(encoding="utf-8")
    assert text.count("## Полная расшифровка") == 1 and "чужая" not in text
    assert _body(w.path) == ["**14:00:01** line 1", "**14:00:02** line 2", "**14:00:03** line 3"]


def test_notes_before_file_exists_and_source_label_on_one_line(tmp_path: Path) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave —\n## Полная расшифровка", "Google Meet", "langs")
    assert w.set_notes("## Кратко\nA.") is True and w.path is None
    w.write(_line(1))
    assert w.path is not None
    text = w.path.read_text(encoding="utf-8")
    assert text.startswith("# Google Meet\n\n- Дата: 2026-10-20 14:05\n"
                           "- Источник: Brave — ## Полная расшифровка\n- langs\n\n## Кратко\nA.\n\n"
                           "## Полная расшифровка\n\n**14:00:01**")
    assert w.set_notes("## Кратко\nB.") is True
    assert _body(w.path) == ["**14:00:01** line 1"]
    assert "## Кратко\nB.\n\n## Полная расшифровка\n\n**14:00:01** line 1\n\n" in (
        w.path.read_text(encoding="utf-8"))


def test_file_edited_by_hand_loses_no_lines(tmp_path: Path) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    w.write(_line(1))
    assert w.path is not None
    w.close()
    w.path.write_text("мои правки\n\n**14:00:01** line 1\n\n", encoding="utf-8")
    assert w.set_notes("## Кратко\nA.") is True
    text = w.path.read_text(encoding="utf-8")
    assert text.endswith("## Кратко\nA.\n\n## Полная расшифровка\n\nмои правки\n\n"
                         "**14:00:01** line 1\n\n")


def test_notes_rewrite_failure_keeps_old_file(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    w.write(_line(1))
    assert w.path is not None
    before = w.path.read_text(encoding="utf-8")

    def boom(src: Any, dst: Any) -> None:
        raise PermissionError(13, "файл открыт в другой программе")

    monkeypatch.setattr(meeting.os, "replace", boom)
    assert w.set_notes("## Кратко\nA.") is False and w.notes == ""
    assert w.set_minutes(5) is False and w.minutes is None
    monkeypatch.undo()
    assert w.path.read_text(encoding="utf-8") == before
    assert sorted(tmp_path.iterdir()) == [w.path]          # временных файлов нет
    w.write(_line(2))
    w.close()
    assert _body(w.path) == ["**14:00:01** line 1", "**14:00:02** line 2"]


def test_notes_and_renames_while_two_writers_append(tmp_path: Path) -> None:
    """Два потока дописывают строки, третий без остановки меняет заметки, название и
    длительность: строки — все и по разу, заголовок и «## Полная расшифровка» — по одному,
    заметки — последние, лишних файлов нет."""
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    per_thread = 300
    ids = iter(range(1, 2 * per_thread + 1))
    ids_lock = threading.Lock()

    def writer() -> None:
        for _ in range(per_thread):
            with ids_lock:
                i = next(ids)
            w.write(_line(i))

    ths = [threading.Thread(target=writer) for _ in range(2)]
    for th in ths:
        th.start()
    k = 0
    while any(th.is_alive() for th in ths) or k < 12:
        k += 1
        w.set_notes(f"## Кратко\nВерсия {k}.\n\n## Задачи\n- [ ] Анна — отчёт — {k}")
        if k % 3 == 0:
            w.rename(f"Итоги {k % 2}")
        if k % 5 == 0:
            w.set_minutes(k)
    for th in ths:
        th.join(10)
    w.close()
    assert w.path is not None
    text = w.path.read_text(encoding="utf-8")
    nums = sorted(int(x.rsplit(" ", 1)[1]) for x in _body(w.path))
    assert nums == list(range(1, 2 * per_thread + 1))
    assert [x for x in text.splitlines() if x.startswith("# ")] == [f"# {w.title}"]
    assert text.count("## Полная расшифровка") == 1
    assert f"## Кратко\nВерсия {k}.\n\n## Задачи\n- [ ] Анна — отчёт — {k}\n\n" \
           "## Полная расшифровка\n\n" in text
    assert text.count("## Кратко") == 1
    assert sorted(tmp_path.iterdir()) == [w.path]


def test_rewrite_temp_file_has_a_short_name_in_the_same_folder(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Временный файл переписывания не длиннее файла записи (путь у самого MAX_PATH раньше
    переполнялся именно на нём: «<имя записи>.<pid>.<id потока>.tmp»)."""
    opened: list[Path] = []
    real = meeting._open_text

    def spy(path: Path, mode: str) -> Any:
        if mode == "w":
            opened.append(Path(path))
        return real(path, mode)

    monkeypatch.setattr(meeting, "_open_text", spy)
    w = TranscriptWriter(tmp_path, T0, "Brave", "x" * 70, "langs")
    w.write(_line(1))
    assert w.rename("y" * 80) and w.set_notes("## Кратко\nA.") and w.set_minutes(3)
    w.close()
    assert w.path is not None and len(opened) == 3
    for p in opened:
        assert p.parent == tmp_path and p.name.startswith(".~rp") and p.suffix == ".tmp"
        assert len(p.name) < len("2026-10-20 14-05 — x.md") < len(w.path.name)
    assert len({p.name for p in opened}) == 3 and sorted(tmp_path.iterdir()) == [w.path]


def test_late_line_after_close_lands_in_the_file(tmp_path: Path) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    w.close()
    w.write(_line(1))                    # файла ещё не было — создан и снова закрыт
    assert w.path is not None and w._f is None
    w.write(_line(2))
    assert _body(w.path) == ["**14:00:01** line 1", "**14:00:02** line 2"]


# ---------- сеанс: когда заметки пишутся ----------

class Notes:
    """notes_fn сеанса: ответы по очереди (строка — заметки, исключение — сбой); gate — вызов
    ждёт, пока его не отпустят; deltas — начало заметок по ходу (on_delta) до ответа."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[NotesRequest] = []
        self.gate: threading.Event | None = None
        self.deltas: list[str] = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, req: NotesRequest) -> ai.AIResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append(req)
        try:
            for d in self.deltas:
                assert req.on_delta is not None
                req.on_delta(d)
                time.sleep(0.01)
            if self.gate is not None:
                assert self.gate.wait(10)
            r = self.replies.pop(0) if self.replies else NOTES_MD
            if isinstance(r, BaseException):
                raise r
            return ai.AIResult(r, 9000.0, 0.0612, "openrouter", "anthropic/claude-sonnet-5.5")
        finally:
            with self._lock:
                self.active -= 1


def _notes_box(box: Any, *replies: Any, title: bool = True, **kw: Any) -> tuple[Any, Notes, Titles]:
    no, ti = Notes(*replies), Titles()
    sess_kw: dict[str, Any] = {"notes_fn": no}
    if title:
        sess_kw.update(title_fn=ti, clock=lambda: 1000.0)
    b = box(sess_kw=sess_kw, **kw)
    return b, no, ti


def _notes_events(b: Any) -> list[dict[str, Any]]:
    return [d for ev, d in b.events if ev == "meeting_notes"]


def _wait_notes(b: Any, status: str) -> dict[str, Any]:
    wait_for(lambda: (b.sess.notes() or {}).get("status") == status
             and not b.sess._notes_busy)
    n = b.sess.notes()
    assert n is not None
    return n


def test_notes_by_themselves_after_stop(box: Any, tmp_path: Path) -> None:
    b, no, ti = _notes_box(box)
    no.gate = threading.Event()
    no.deltas = ["# Мигра", "# Миграция биллинга\n\n## Кратко\nОбсу"]
    b.sess.start()
    _lines(b, 6)
    t0 = time.monotonic()
    b.sess.stop()                                       # не ждёт заметок
    assert time.monotonic() - t0 < 1.0
    wait_for(lambda: len(no.calls) == 1)
    st = b.sess.status()
    assert st["notes"] is not None and st["notes"]["status"] == "running"
    assert "markdown" not in st["notes"]                # в статусе — без текста заметок
    wait_for(lambda: any(d["status"] == "running" and d["markdown"] for d in _notes_events(b)))
    no.gate.set()
    n = _wait_notes(b, "done")
    _done(b)
    assert ti.calls == [] and no.max_active == 1         # название — из заметок
    req = no.calls[0]
    ts = [x["ts"] for x in b.lines()]
    assert req.lang == "ru" and req.transcript.splitlines() == [
        f"{ts[i]} RU:Phrase {i} about the billing migration" for i in range(6)]
    assert req.title == "Google Meet"
    assert req.meta["Duration"] == "1 min" and req.meta["Notes language"] == "Russian"
    assert req.meta["Speech languages"] == "English"
    assert req.meta["Source"] == "Brave — Meet – abc-defg-hij"
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Миграция биллинга в ClickHouse.md")
    assert sorted(tmp_path.glob("*.md")) == [f]
    text = f.read_text(encoding="utf-8")
    head, rest = text.split("\n## Полная расшифровка\n\n", 1)
    assert head.startswith("# Миграция биллинга в ClickHouse\n\n- Дата: ")
    assert ", длительность 1 мин\n- Источник: Brave — Meet – abc-defg-hij\n" in head
    assert head.endswith("## Кратко\nОбсудили перенос таблиц биллинга.\n\n## Задачи\n"
                         "- [ ] Анна — отчёт по нагрузке — пятница\n\n## Ключевые моменты\n"
                         "- 14:05:01 — начали с плана переноса\n")
    assert [x for x in text.splitlines() if x.startswith("# ")] == [
        "# Миграция биллинга в ClickHouse"]
    assert len(_body(f)) == 6
    assert set(n) == {"status", "markdown", "generated_at", "model", "cost_usd", "error", "auto"}
    assert n["markdown"] == NOTES_MD and n["auto"] is True and n["error"] is None
    assert n["model"] == "anthropic/claude-sonnet-5.5" and n["cost_usd"] == 0.0612
    assert datetime.fromisoformat(n["generated_at"]).utcoffset() is not None
    evs = _notes_events(b)
    assert evs[0]["status"] == "running" and evs[-1] == n
    assert b.sess.status()["title"] == "Миграция биллинга в ClickHouse"
    assert b.sess.status()["notes"] == {k: v for k, v in n.items() if k != "markdown"}
    assert b.sess.notes_text() == NOTES_MD
    assert not b.sess.busy


def test_short_meeting_gets_title_call_but_no_notes(box: Any, tmp_path: Path) -> None:
    b, no, ti = _notes_box(box)
    b.sess.start()
    _lines(b, 3)
    b.sess.stop()
    _done(b)
    wait_for(lambda: not b.sess.busy)
    assert no.calls == [] and len(ti.calls) == 1 and b.sess.notes() is None
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Миграция биллинга в ClickHouse.md")
    text = f.read_text(encoding="utf-8")
    assert "\n- Языки: речь — определяется автоматически, перевод — ru\n\n## Полная расшифровка\n\n" \
           in text
    # выключено — не пишутся и после длинной встречи; название — отдельным вызовом
    b2, no2, ti2 = _notes_box(box, meeting_notes=False)
    b2.sess.start()
    _lines(b2, 6)
    b2.sess.stop()
    _done(b2)
    assert no2.calls == [] and len(ti2.calls) == 1


def test_speech_threshold_and_unchanged_transcript(box: Any) -> None:
    b, no, _ti = _notes_box(box, title=False)
    b.sess.start()
    _lines(b, 2)
    assert b.sess._notes_auto_due() is False
    b.sess._speech_s = meeting.NOTES_MIN_SPEECH_S + 1        # две строки, но две минуты речи
    assert b.sess._notes_auto_due() is True
    b.sess.request_notes()
    _wait_notes(b, "done")
    assert b.sess._notes_auto_due() is False                # расшифровка та же — не заново
    b.sess.stop()
    wait_for(lambda: b.sess._finalized and not b.sess.busy)
    assert len(no.calls) == 1


def test_failed_notes_fall_back_to_title_call_and_notice_once(box: Any, tmp_path: Path) -> None:
    b, no, ti = _notes_box(box, ai.AIError("Нет ключа OpenRouter — добавьте его", "no_key"),
                           ai.AIError("Нет ключа OpenRouter — добавьте его", "no_key"))
    b.sess.start()
    _lines(b, 5)
    b.sess.stop()
    n = _wait_notes(b, "error")
    _done(b)
    assert len(ti.calls) == 1 and b.sess.status()["title"] == "Миграция биллинга в ClickHouse"
    assert n["error"] == "Нет ключа OpenRouter — добавьте его" and n["markdown"] is None
    assert n["auto"] is True and n["model"] is None
    f = b.sess.file
    assert f is not None
    text = f.read_text(encoding="utf-8")
    assert "\n- Языки: речь — определяется автоматически, перевод — ru\n\n## Полная расшифровка\n\n" \
           in text and "## Кратко" not in text                # файл без заметок
    notes = [x for x in b.notices if "Заметки к встрече не получились" in x["text"]]
    assert notes == [{"text": "Заметки к встрече не получились: Нет ключа OpenRouter — "
                              "добавьте его. Расшифровка сохранена",
                      "level": "warn", "action": "open_settings_ai"}]
    # по кнопке — снова нет ключа: ошибка в окне, второго уведомления нет
    assert b.sess.request_notes()["status"] in ("running", "error")   # ответ — сразу
    _wait_notes(b, "error")
    assert len(no.calls) == 2
    assert len([x for x in b.notices if "Заметки к встрече" in x["text"]]) == 1


def test_network_error_by_button_shows_in_window_only(box: Any) -> None:
    b, no, _ti = _notes_box(box, ai.AIError("Нет сети", "network"), title=False)
    b.sess.start()
    _lines(b, 2)
    b.sess.request_notes()
    n = _wait_notes(b, "error")
    assert n["error"] == "Нет сети" and n["auto"] is False
    assert [x for x in b.notices if "Заметки" in x["text"]] == []


def test_notes_on_demand_during_meeting_one_in_flight_and_replaced(box: Any,
                                                                 tmp_path: Path) -> None:
    b, no, _ti = _notes_box(box, "# Первые заметки\n\n## Кратко\nНачало встречи.",
                            "# Вторые заметки\n\n## Кратко\nВся встреча.", title=False)
    no.gate = threading.Event()
    b.sess.start()
    with pytest.raises(ValueError, match="заметки делать не из чего"):
        b.sess.request_notes()                         # строк ещё нет
    _lines(b, 2)
    r1 = b.sess.request_notes()
    assert r1["status"] == "running" and r1["auto"] is False and r1["markdown"] is None
    assert b.sess.request_notes()["status"] == "running"   # уже пишутся — одни в полёте
    wait_for(lambda: len(no.calls) == 1)
    time.sleep(0.05)
    assert len(no.calls) == 1
    no.gate.set()
    _wait_notes(b, "done")
    assert no.max_active == 1
    # название из заметок — сразу (раннее название по содержанию уже не нужно)
    assert b.sess.status()["title"] == "Первые заметки" and b.sess._title_early is True
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Первые заметки.md")
    _lines(b, 1)                                       # строки идут дальше
    no.gate = None
    b.sess.request_notes()
    n = _wait_notes(b, "done")
    assert n["markdown"] == "# Вторые заметки\n\n## Кратко\nВся встреча."
    assert len(no.calls[1].transcript.splitlines()) == 3
    f = b.sess.file
    assert f is not None and sorted(tmp_path.glob("*.md")) == [f]
    text = f.read_text(encoding="utf-8")
    assert "Вся встреча." in text and "Начало встречи." not in text
    assert len(_body(f)) == 3
    b.sess.stop()
    wait_for(lambda: b.sess._finalized and not b.sess.busy)
    assert len(no.calls) == 2                           # 3 строки — после «Стоп» сами не пишутся


def test_stop_while_notes_are_written_writes_them_again(box: Any) -> None:
    """Заметки по кнопке ещё пишутся, а встреча кончилась: в них нет последних строк — сразу
    после них ещё раз (сами), их название — последнее."""
    b, no, ti = _notes_box(box, "# Ранние заметки\n\n## Кратко\nA.",
                           "# Итоги встречи\n\n## Кратко\nB.")
    no.gate = threading.Event()
    b.sess.start()
    _lines(b, 5)
    b.sess.request_notes()
    wait_for(lambda: len(no.calls) == 1)
    _lines(b, 1)
    b.sess.stop()
    wait_for(lambda: b.sess._finalized)
    time.sleep(0.05)
    assert len(no.calls) == 1 and b.sess._notes_again is True
    no.gate.set()
    wait_for(lambda: len(no.calls) == 2)
    n = _wait_notes(b, "done")
    _done(b)
    assert n["auto"] is True and n["markdown"].startswith("# Итоги встречи\n")
    assert len(no.calls[1].transcript.splitlines()) == 6
    assert ti.calls == [] and b.sess.status()["title"] == "Итоги встречи"


def test_notes_title_not_from_model_or_ai_title_off(box: Any) -> None:
    # «# …» — название записи (ИИ названия не дал) — последнее название отдельным вызовом
    b, no, ti = _notes_box(box, "# Google Meet\n\n## Кратко\nОбсудили.")
    b.sess.start()
    _lines(b, 5)
    b.sess.stop()
    _wait_notes(b, "done")
    _done(b)
    assert len(ti.calls) == 1 and b.sess.status()["title"] == "Миграция биллинга в ClickHouse"
    n = b.sess.notes()
    assert n is not None and n["markdown"] == "# Миграция биллинга в ClickHouse\n\n## Кратко\nОбсудили."
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Миграция биллинга в ClickHouse.md")
    # называть по содержанию выключено: «# …» заметок — не название записи
    b2, no2, ti2 = _notes_box(box, meeting_ai_title=False)
    b2.sess.start()
    _lines(b2, 5)
    b2.sess.stop()
    n2 = _wait_notes(b2, "done")
    _done(b2)
    assert ti2.calls == [] and b2.sess.status()["title"] == "Google Meet"
    assert n2["markdown"].startswith("# Google Meet\n\n## Кратко\n")
    f2 = b2.sess.file
    assert f2 is not None and f2.name.endswith(" — Google Meet.md")
    assert f2.read_text(encoding="utf-8").startswith("# Google Meet\n")


QUOTED_SRC = {"id": "proc:4100", "kind": "process", "pid": 4100, "exe": "brave.exe",
              "name": "Brave", "title": "Обсуждение «Альфы» - Brave", "meeting": True,
              "active": True}


def _untitled_notes(no: Notes) -> Any:
    """notes_fn, как ai.meeting_notes, когда модель названия не дала: «# …» — название записи
    из запроса после clean_notes (подставлено)."""
    def fn(req: NotesRequest) -> ai.AIResult:
        no(req)
        return ai.AIResult(ai.clean_notes("## Кратко\nОбсудили.", req.title), 9000.0, 0.06,
                           "openrouter", "anthropic/claude-sonnet-5.5")
    return fn


def test_substituted_title_with_quotes_is_not_taken_for_the_models(box: Any) -> None:
    """Модель названия не дала — clean_notes подставил название записи без ИИ («Обсуждение
    «Альфы»»); после clean_title оно без кавычек, но это всё равно не название модели:
    последнее название — отдельным вызовом, а не искажённое название без ИИ."""
    no, ti = Notes(), Titles()
    no.gate = threading.Event()
    b = box(src=QUOTED_SRC, sess_kw={"notes_fn": _untitled_notes(no), "title_fn": ti,
                                     "clock": lambda: 1000.0})
    b.sess.start()
    assert b.sess.status()["title"] == "Обсуждение «Альфы»"
    _lines(b, 5)
    b.sess.stop()
    wait_for(lambda: len(no.calls) == 1 and b.sess._title_closed)
    no.gate.set()
    _wait_notes(b, "done")
    _done(b)
    assert len(no.calls) == 1 and no.calls[0].title_ai is False
    assert len(ti.calls) == 1 and b.sess.status()["title"] == "Миграция биллинга в ClickHouse"


def test_title_by_content_during_notes_is_not_undone_by_substituted_one(box: Any) -> None:
    """Пока заметки пишутся, запись получила название по содержанию; заметки без своего
    названия (подставлено прежнее, без ИИ) его не возвращают."""
    no = Notes()
    no.gate = threading.Event()
    b = box(src=QUOTED_SRC, sess_kw={"notes_fn": _untitled_notes(no)})
    b.sess.start()
    _lines(b, 2)
    b.sess.request_notes()
    wait_for(lambda: len(no.calls) == 1)
    b.sess._set_title("Альфа и её релиз")              # раннее название по содержанию
    no.gate.set()
    n = _wait_notes(b, "done")
    assert b.sess.status()["title"] == "Альфа и её релиз"
    assert n["markdown"] == "# Альфа и её релиз\n\n## Кратко\nОбсудили."
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Альфа и её релиз.md")


def test_notes_done_before_finalize_signals_the_title_thread(box: Any) -> None:
    """Заметки после «Стоп» успели раньше, чем поток названия узнал о конце встречи: их
    название — всё равно последнее, отдельного вызова нет."""
    b, no, ti = _notes_box(box, "# Итоги миграции\n\n## Кратко\nA.")
    real = b.sess._start_notes

    def start_and_finish(auto: bool, again: bool = False) -> bool:
        ok = real(auto, again)
        assert b.sess._notes_idle.wait(5)           # заметки готовы до _title_closed
        return ok

    b.sess._start_notes = start_and_finish  # type: ignore[method-assign]
    b.sess.start()
    _lines(b, 5)
    b.sess.stop()
    _wait_notes(b, "done")
    _done(b)
    assert len(no.calls) == 1 and ti.calls == []
    assert b.sess.status()["title"] == "Итоги миграции"


def test_notes_by_button_over_whole_meeting_give_the_last_title(box: Any) -> None:
    """Заметки по кнопке написаны по всей расшифровке, потом «Стоп»: сами заново не пишутся
    (расшифровка та же), и их название остаётся последним — отдельный вызов его не заменяет."""
    b, no, ti = _notes_box(box, "# Итоги по кнопке\n\n## Кратко\nA.")
    b.sess.start()
    _lines(b, 5)
    b.sess.request_notes()
    _wait_notes(b, "done")
    assert b.sess.status()["title"] == "Итоги по кнопке"
    b.sess.stop()
    _done(b)
    wait_for(lambda: not b.sess.busy)
    assert len(no.calls) == 1 and ti.calls == []
    assert b.sess.status()["title"] == "Итоги по кнопке"
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Итоги по кнопке.md")


def test_early_title_answer_after_notes_title_does_not_replace_it(box: Any) -> None:
    """Раннее название ещё в полёте, а заметки по кнопке уже дали своё: ответ раннего вызова
    его не заменяет, и после «Стоп» (расшифровка та же) последнее название — из заметок."""
    no, ti = Notes("# Итоги по заметкам\n\n## Кратко\nA."), Titles()
    ti.gate = threading.Event()
    now = [1000.0]
    b = box(sess_kw={"notes_fn": no, "title_fn": ti, "clock": lambda: now[0]})
    b.sess.start()
    _lines(b, 8)
    now[0] += meeting.TITLE_EARLY_S + 1
    b.sess._title_kick()
    wait_for(lambda: len(ti.calls) == 1)                # раннее название — ждёт ответа
    b.sess.request_notes()
    _wait_notes(b, "done")
    assert b.sess.status()["title"] == "Итоги по заметкам"
    ti.gate.set()
    b.sess.stop()
    _done(b)
    assert len(ti.calls) == 1 and len(no.calls) == 1
    assert b.sess.status()["title"] == "Итоги по заметкам"
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Итоги по заметкам.md")


def test_failed_notes_notice_without_file_does_not_claim_it_is_saved(box: Any) -> None:
    b, _no, _ti = _notes_box(box, ai.AIError("Нет ключа OpenRouter — добавьте его", "no_key"),
                             title=False, meeting_save=False)
    b.sess.start()
    _lines(b, 5)
    b.sess.stop()
    _wait_notes(b, "error")
    assert b.sess.file is None
    notes = [x for x in b.notices if "Заметки к встрече" in x["text"]]
    assert notes == [{"text": "Заметки к встрече не получились: Нет ключа OpenRouter — "
                              "добавьте его", "level": "warn", "action": "open_settings_ai"}]


def test_transcript_section_in_notes_is_cut_in_window_and_clipboard_too(box: Any) -> None:
    """Модель дописала «## Полная расшифровка» — в файле заметки кончаются перед ней; окно
    и «Копировать всё» показывают те же заметки (второго такого раздела нет)."""
    b, _no, _ti = _notes_box(box, "# Итоги\n\n## Кратко\nA.\n\n## Полная расшифровка\n"
                                  "**00:00:01** выдумано", title=False)
    b.sess.start()
    _lines(b, 2)
    b.sess.request_notes()
    n = _wait_notes(b, "done")
    assert n["markdown"] == "# Итоги\n\n## Кратко\nA." and b.sess.notes_text() == n["markdown"]
    f = b.sess.file
    assert f is not None
    text = f.read_text(encoding="utf-8")
    assert text.count("## Полная расшифровка") == 1 and "выдумано" not in text
    # без файла (meeting_save выключено) — то же
    b2, _no2, _ti2 = _notes_box(box, "# Итоги\n\n## Кратко\nB.\n## Полная расшифровка\nx",
                                title=False, meeting_save=False)
    b2.sess.start()
    _lines(b2, 2)
    b2.sess.request_notes()
    assert _wait_notes(b2, "done")["markdown"] == "# Итоги\n\n## Кратко\nB."


def test_core_exit_makes_no_notes(box: Any) -> None:
    b, no, _ti = _notes_box(box)
    b.sess.start()
    _lines(b, 6)
    b.sess.abort()
    wait_for(lambda: b.sess._finalized)
    time.sleep(0.05)
    assert no.calls == [] and b.sess.notes() is None


def test_late_final_line_after_stop_without_drain_lands_in_file(box: Any) -> None:
    """stop(drain=False) (сбой захвата) закрывает файл сразу, а последний проход фразы ещё
    шёл: его строка дописывается в закрытый файл, а не пропадает."""
    b = box()
    b.sess.start()
    held, release = threading.Event(), threading.Event()
    real = b.rec.transcribe_background

    def slow(audio: Any, **kw: Any) -> Any:
        if isinstance(kw.get("temperature"), tuple):
            held.set()
            assert release.wait(5)
        return real(audio, **kw)

    b.rec.transcribe_background = slow  # type: ignore[method-assign]
    b.rec.results = ["Late words"]
    b.say(wait=False)
    assert held.wait(5)
    b.sess.stop(drain=False)
    assert b.sess._finalized and b.sess.file is None     # строк ещё не было — файла нет
    release.set()
    wait_for(lambda: b.sess.file is not None)
    f = b.sess.file
    assert f is not None
    wait_for(lambda: "Late words" in f.read_text(encoding="utf-8"))
    assert _body(f) == [f"**{b.lines()[0]['ts']}** Late words"]
    assert b.sess._writer is not None and b.sess._writer._f is None   # снова закрыт


def test_long_window_title_is_capped(box: Any, tmp_path: Path) -> None:
    long = "Еженедельная синхронизация команды платформы данных по миграции биллинга " * 3
    b = box(src={"id": "proc:9", "kind": "process", "pid": 9, "exe": "brave.exe",
                 "name": "Brave", "title": long + "- Brave", "meeting": True, "active": True})
    b.sess.start()
    t = b.sess.status()["title"]
    assert t is not None and len(t) <= meeting.TITLE_PART_MAX and long.startswith(t)
    assert t.endswith("биллинга")                       # по границе слова
    _lines(b, 1)
    f = b.sess.file
    assert f is not None and f.read_text(encoding="utf-8").startswith(f"# {t}\n")
    assert meeting.fallback_title({"kind": "process", "exe": "brave.exe", "name": "B",
                                   "title": "x" * 300}) == "x" * meeting.TITLE_PART_MAX


def test_notes_settings_validation() -> None:
    s = config.Settings()
    assert s.meeting_notes is True and s.meeting_notes_model == ""
    config.apply_patch(s, {"meeting_notes": False, "meeting_notes_model": "anthropic/claude-opus-5.5"})
    assert s.meeting_notes is False and s.meeting_notes_model == "anthropic/claude-opus-5.5"
    for bad in ({"meeting_notes": "yes"}, {"meeting_notes_model": "x" * 201},
                {"meeting_notes_model": 5}):
        with pytest.raises(ValueError):
            config.apply_patch(s, bad)
    assert meeting.idle_status(config.Settings())["notes"] is None


# ---------- движок и WebSocket ----------

def _meeting_with_lines(env: Env, c: Any, n: int) -> None:
    env.calls.sources = [dict(MEET_SRC)]
    c.call("meeting_start", {})
    env.rec.bg = [f"Phrase {i} about billing" for i in range(n)]
    for _ in range(n):
        _say(env.calls.streams[-1], rec=env.rec)
    c.wait_event("meeting_line", lambda d: d["id"] == n and d["final"], fresh=False)


def test_ws_notes_commands_without_meeting(env: Env) -> None:
    c = env.client()
    assert c.call("meeting_notes_get") is None
    assert "Встречи ещё не было" in c.error("meeting_notes")
    assert "transcript, notes или all" in c.error("meeting_copy", {"what": "bogus"})
    assert "копировать нечего" in c.error("meeting_copy", {"what": "notes"})
    c.close()


def test_engine_notes_after_stop_with_usage_rows(env: Env, translated: list[Any],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    env.calls.creds["Rupor/openrouter"] = FAKE_KEY
    env.eng.update_settings({"ai_keys": [{"id": "openrouter", "provider": "openrouter"}]})
    seen: list[dict[str, Any]] = []
    titled: list[str] = []
    monkeypatch.setattr(ai, "meeting_title", lambda *a, **k: titled.append("x"))

    def unusable() -> ai.AIResult:
        raise ai.AIError("не заметки", "bad_response", result=ai.AIResult(
            "x", 900.0, 0.011, "openrouter", "anthropic/claude-sonnet-5.5"))

    def fake_notes(transcript: str, lang: str, s: config.Settings, **kw: Any) -> ai.AIResult:
        seen.append({"transcript": transcript, "lang": lang, **kw})
        track = kw["track"]
        # длинная встреча: часть (оплачена, но не подошла) и итог — каждый вызов в расход
        with pytest.raises(ai.AIError):
            track(unusable)
        kw["on_delta"]("# Утренний")
        return track(lambda: ai.AIResult(NOTES_MD.replace("Миграция биллинга в ClickHouse",
                                                          "Утренний синк по биллингу"),
                                         8000.0, 0.05, "openrouter",
                                         "anthropic/claude-sonnet-5.5", tokens_in=20_000,
                                         tokens_out=1500))

    monkeypatch.setattr(ai, "meeting_notes", fake_notes)
    clip: list[str] = []
    monkeypatch.setattr(inject, "clipboard_set_text",
                        lambda text, private=False: clip.append(text))
    c = env.client()
    _meeting_with_lines(env, c, 5)
    c.call("meeting_stop")
    ev = c.wait_event("meeting_notes", lambda d: d["status"] == "done", fresh=False)
    assert ev["markdown"].startswith("# Утренний синк по биллингу\n\n## Кратко\n")
    assert ev["auto"] is True and ev["cost_usd"] == 0.05
    st = c.wait_event("meeting", lambda d: d.get("title") == "Утренний синк по биллингу",
                      fresh=False)
    assert st["file"].endswith(" — Утренний синк по биллингу.md")
    m = env.eng._meeting
    assert m is not None and m._title_thread is not None
    m._title_thread.join(5)
    assert titled == []                                 # отдельного названия нет
    assert seen[0]["lang"] == "ru" and seen[0]["title"] == "Google Meet"
    assert seen[0]["transcript"].splitlines()[0].endswith(" RU:Phrase 0 about billing")
    assert set(seen[0]["meta"]) == {"Date", "Duration", "Status", "Source",
                                    "Speech languages", "Notes language"}
    assert seen[0]["meta"]["Status"] == "ended" and seen[0]["in_progress"] is False
    assert any(d["status"] == "running" and d["markdown"] == "# Утренний"
               for d in c.events("meeting_notes"))
    rows = [r for r in usage_rows(env) if r["kind"] == "notes"]
    assert [(r["app"], r["cost_usd"], bool(r["ok"]), r["error_kind"]) for r in rows] == [
        ("brave.exe", 0.011, False, "bad_response"), ("brave.exe", 0.05, True, None)]
    assert rows[1]["tokens_in"] == 20_000 and rows[1]["model"] == "anthropic/claude-sonnet-5.5"
    kinds = [d["kind"] for d in c.events("ai_usage")]
    assert kinds.count("notes") == 2
    by_kind = {r["kind"]: r["calls"] for r in c.call("ai_usage_stats")["by_kind"]}
    assert by_kind["notes"] == 2 and by_kind["title"] == 0
    got = c.call("meeting_notes_get")
    assert got is not None and got["markdown"] == ev["markdown"]
    status = c.call("meeting_status")
    assert status["notes"]["status"] == "done" and "markdown" not in status["notes"]
    f = env.eng.meeting_file()
    assert f is not None and "## Кратко\nОбсудили перенос таблиц биллинга." in f.read_text(
        encoding="utf-8")
    # буфер обмена: заметки, всё, расшифровка (по умолчанию)
    assert c.call("meeting_copy", {"what": "notes"})["chars"] == len(ev["markdown"])
    assert clip[-1] == ev["markdown"]
    c.call("meeting_copy", {"what": "all"})
    assert clip[-1].startswith(ev["markdown"] + "\n\n## Полная расшифровка\n\n")
    assert "RU:Phrase 4 about billing" in clip[-1]
    c.call("meeting_copy")
    assert not clip[-1].startswith("#") and "RU:Phrase 0 about billing" in clip[-1]
    assert env.eng._ai_busy is False
    c.close()


def test_engine_notes_without_key_notice_once(env: Env, translated: list[Any],
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(ai, "meeting_notes", lambda *a, **k: called.append("x"))
    c = env.client()
    _meeting_with_lines(env, c, 5)
    c.mark()
    c.call("meeting_stop")
    ev = c.wait_event("meeting_notes", lambda d: d["status"] == "error", fresh=False)
    assert "Нет ключа OpenRouter" in ev["error"] and ev["markdown"] is None
    n = c.wait_event("notice", lambda d: "Заметки к встрече не получились" in d["text"],
                     fresh=False)
    assert n["action"] == "open_settings_ai" and n["level"] == "warn"
    # по кнопке — ошибка снова, без второго уведомления; ни вызова, ни строки расхода
    r = c.call("meeting_notes")
    assert r["status"] in ("running", "error")
    m = env.eng._meeting
    assert m is not None
    wait_for(lambda: not m._notes_busy)
    assert c.call("meeting_notes_get")["status"] == "error"
    time.sleep(0.1)
    assert len([d for d in c.events("notice") if "Заметки к встрече" in d["text"]]) == 1
    assert called == [] and [r for r in usage_rows(env) if r["kind"] == "notes"] == []
    c.close()


def test_engine_real_notes_call_per_part_is_recorded(env: Env, translated: list[Any],
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    """ai.meeting_notes по-настоящему (сеть подменена): длинная встреча — части и сведение,
    у каждого вызова — своя строка расхода вида notes."""
    import json
    env.calls.creds["Rupor/openrouter"] = FAKE_KEY
    env.eng.update_settings({"ai_keys": [{"id": "openrouter", "provider": "openrouter"}]})
    monkeypatch.setattr(ai, "NOTES_SINGLE_CHARS", 60)
    monkeypatch.setattr(ai, "NOTES_CHUNK_CHARS", 40)
    monkeypatch.setattr(ai, "NOTES_MAX_CHUNKS", 2)

    def reply(req: Any) -> Any:
        system = json.loads(req.data)["messages"][0]["content"]
        if system.startswith("You merge"):
            return or_reply(NOTES_MD, cost=0.02)
        if system.startswith("You name meetings"):
            return or_reply("Лишнее название", cost=0.0001)
        return or_reply("## Кратко\nЧасть.", cost=0.01)

    monkeypatch.setattr(ai, "_urlopen", Recorder(reply))
    # итоговый вызов (сведение) идёт потоком: окно перевода показывает заметки по ходу
    monkeypatch.setattr(ai, "_stream_open", StreamRecorder(
        lambda body: FakeStreamResp(sse(or_delta(NOTES_MD), or_end(cost=0.02)))))
    c = env.client()
    _meeting_with_lines(env, c, 5)
    c.call("meeting_stop")
    c.wait_event("meeting_notes", lambda d: d["status"] == "done", fresh=False)
    m = env.eng._meeting
    assert m is not None and m._title_thread is not None
    m._title_thread.join(5)
    rows = [(r["kind"], r["cost_usd"], bool(r["ok"])) for r in usage_rows(env)
            if r["kind"] in ("notes", "title")]
    assert rows == [("notes", 0.01, True), ("notes", 0.01, True), ("notes", 0.02, True)]
    assert c.call("meeting_status")["title"] == "Миграция биллинга в ClickHouse"
    c.close()


def test_engine_rejected_title_is_recorded_with_its_cost(env: Env, translated: list[Any],
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    env.calls.creds["Rupor/openrouter"] = FAKE_KEY
    env.eng.update_settings({"ai_keys": [{"id": "openrouter", "provider": "openrouter"}]})

    def bad_title(transcript: str, lang: str, s: config.Settings, **kw: Any) -> ai.AIResult:
        raise ai.AIError("ИИ не придумал название встречи", "bad_response", result=ai.AIResult(
            "Встреча", 700.0, 0.0004, "openrouter", "anthropic/claude-haiku-4.5", tokens_in=900,
            tokens_out=3))

    monkeypatch.setattr(ai, "meeting_title", bad_title)
    c = env.client()
    _meeting_with_lines(env, c, 3)
    c.call("meeting_stop")
    m = env.eng._meeting
    assert m is not None and m._title_thread is not None
    m._title_thread.join(5)
    rows = [r for r in usage_rows(env) if r["kind"] == "title"]
    assert [(r["cost_usd"], bool(r["ok"]), r["error_kind"], r["tokens_in"], r["model"])
            for r in rows] == [(0.0004, False, "bad_response", 900, "anthropic/claude-haiku-4.5")]
    assert c.call("meeting_status")["title"] == "Google Meet"
    stats = c.call("ai_usage_stats")
    assert {r["kind"]: r["cost_usd"] for r in stats["by_kind"]}["title"] == 0.0004
    revealed: list[Path] = []
    monkeypatch.setattr(api, "_reveal_file", revealed.append)
    c.call("meeting_open_folder")
    assert revealed and revealed[0].name.endswith(" — Google Meet.md")
    c.close()


# ---------- разбор заметок: недоделанные строки, идущая встреча, окно Ollama, порядок ----------

def _ml(i: int, text: str, *, final: bool = True, translation: str | None = None,
        status: str = "original", partial: str = "", refined: bool = False,
        done_words: int | None = None, busy: bool = False) -> MeetingLine:
    """Строка расшифровки в нужном состоянии: done_words — сколько её слов уже переведено
    (None — все), busy — вызов перевода в полёте."""
    x = MeetingLine(i, float(i), f"14:05:{i:02d}", "en", text, translation, status,
                    partial=partial, final=final, refined=refined)
    x.words = text.split()
    n = len(x.words) if done_words is None else done_words
    x.tr.src = [meeting.norm_word(w) for w in x.words[:n]]
    x.tr.on = translation is not None
    x.tr.busy = busy
    return x


def test_notes_input_takes_only_final_lines_and_whole_translations(box: Any) -> None:
    """Заметки — как название: только готовые строки; у готовой строки с недоделанным
    переводом (идёт, оборван, вызов в полёте) — оригинал, не полуперевод."""
    b, _no, _ti = _notes_box(box, title=False)
    rows = [
        _ml(1, "We move billing", translation="Переносим биллинг", status="done"),
        _ml(2, "Anna sends the report", translation="Анна пришлёт", status="streaming",
            done_words=2),
        _ml(3, "On Friday then", translation="В пятницу", status="done", busy=True),
        _ml(4, "Whole phrase here", translation="Вся фраза целиком", status="done",
            refined=True, done_words=0),
        _ml(5, "Native line", status="original"),
        _ml(6, "Failed line", translation="Не", status="error", done_words=1),
        _ml(7, "Still being", final=False, partial="recognised now",
            translation="Ещё", status="streaming"),
    ]
    b.sess._lines.extend(rows)
    text, langs = b.sess._notes_input()
    assert text.splitlines() == [
        "14:05:01 Переносим биллинг", "14:05:02 Anna sends the report",
        "14:05:03 On Friday then", "14:05:04 Вся фраза целиком", "14:05:05 Native line",
        "14:05:06 Failed line"]
    assert langs == {"en": 6}
    # живая фраза кончилась и переведена целиком — теперь в заметках, переводом
    live = rows[6]
    live.final, live.text, live.partial = True, "Still being recognised now", ""
    live.words = live.text.split()
    live.tr.src = [meeting.norm_word(w) for w in live.words]
    live.translation, live.status = "Ещё распознаётся", "done"
    assert b.sess._notes_input()[0].splitlines()[-1] == "14:05:07 Ещё распознаётся"


def test_notes_during_meeting_say_it_is_in_progress(box: Any) -> None:
    """Заметки по кнопке посреди встречи: в <meeting> — «Status: in progress», в промпте —
    правило «объявленное, но не обсуждённое — не открытый вопрос»; после конца — ended."""
    b, no, _ti = _notes_box(box, title=False)
    b.sess.start()
    _lines(b, 5)
    b.sess.request_notes()
    _wait_notes(b, "done")
    req = no.calls[0]
    assert req.in_progress is True and req.meta["Status"] == "in progress"
    assert req.meta["Duration"].endswith(" min so far")
    _lines(b, 1)
    b.sess.stop()
    wait_for(lambda: len(no.calls) == 2)
    _wait_notes(b, "done")
    req = no.calls[1]
    assert req.in_progress is False and req.meta["Status"] == "ended"
    assert not req.meta["Duration"].endswith("so far")


def test_notes_prompt_in_progress_rule(rec: Recorder) -> None:
    assert "in progress" not in ai.notes_prompt("ru")
    p = ai.notes_prompt("ru", in_progress=True)
    assert '"Status: in progress"' in p and '"Открытые вопросы"' in p
    assert "not discussed yet are not open questions" in p
    assert '"Open questions"' in ai.notes_prompt("en", in_progress=True)
    ai.meeting_notes(TRANSCRIPT, "ru", settings(),
                     meta={**META, "Status": "in progress"}, in_progress=True)
    system, user = (rec.calls[-1]["body"]["messages"][i]["content"] for i in (0, 1))
    assert "not discussed yet" in system and "Status: in progress\n" in user
    ai.meeting_notes(TRANSCRIPT, "ru", settings(), meta=META)
    assert "not discussed yet" not in rec.calls[-1]["body"]["messages"][0]["content"]


def test_long_meeting_parts_and_merge_know_it_is_in_progress(
        rec: Recorder, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "NOTES_SINGLE_CHARS", 100)
    monkeypatch.setattr(ai, "NOTES_CHUNK_CHARS", 60)
    rec.reply = lambda req: or_reply("## Кратко\nЧасть.", cost=0.01)
    lines = "\n".join(f"14:05:{i:02d} фраза номер {i} о переносе" for i in range(8))
    ai.meeting_notes(lines, "ru", settings(), in_progress=True)
    systems = [c["body"]["messages"][0]["content"] for c in rec.calls]
    assert len(systems) > 2 and all("not discussed yet" in x for x in systems)


def test_ollama_notes_context_window_fits_the_transcript(rec: Recorder) -> None:
    assert ai.notes_ctx("s", "u", 4000) == ai.NOTES_CTX_MIN == 8192
    big = ai.notes_ctx("", "x" * 100_000, 4000)            # 40 000 + 4 000 токенов
    assert big % ai.NOTES_CTX_STEP == 0 and 44_000 <= big < 44_000 + ai.NOTES_CTX_STEP
    assert ai.notes_ctx("", "x" * 10_000_000, 4000) == ai.NOTES_CTX_MAX
    rec.reply = lambda req: {"message": {"content": NOTES_MD}, "done_reason": "stop"}
    long_tr = "\n".join(f"14:{i // 60 % 60:02d}:{i % 60:02d} " + "слово " * 30
                        for i in range(400))
    ai.meeting_notes(long_tr, "ru", settings(ai_provider="ollama"))
    b = rec.calls[-1]["body"]
    need = (len(b["messages"][0]["content"]) + len(b["messages"][1]["content"])) / 2.5 + 4000
    assert b["options"]["num_ctx"] >= need and b["options"]["num_ctx"] > 8192
    # прочие вызовы Ollama (очистка, перевод) — окно по умолчанию, как было
    rec.reply = lambda req: {"message": {"content": "Привет."}, "done_reason": "stop"}
    ai.cleanup("привет", "clean", settings(ai_provider="ollama"), [])
    assert "num_ctx" not in rec.calls[-1]["body"]["options"]


def test_ollama_notes_stream_sets_context_window(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[dict[str, Any]] = []

    def fake_stream(system: str, user: str, s: object, timeout: float, max_tokens: int,
                    model: str | None, on_text: Any, temperature: float = 0,
                    num_ctx: int | None = None) -> ai.AIResult:
        seen.append({"num_ctx": num_ctx, "max_tokens": max_tokens})
        on_text(NOTES_MD)
        return ai.AIResult(NOTES_MD, 10.0, 0.0, "ollama", model or "")

    monkeypatch.setattr(ai, "_ollama_stream", fake_stream)
    shown: list[str] = []
    ai.meeting_notes(TRANSCRIPT, "ru", settings(ai_provider="ollama"), on_delta=shown.append)
    assert seen == [{"num_ctx": 8192, "max_tokens": 4000}] and shown


def test_notes_events_cannot_overtake_each_other(box: Any) -> None:
    """Снимок заметок и его отправка — под одним замком: пока одно событие meeting_notes
    уходит, другой поток не снимает новый снимок (иначе старый ушёл бы после нового)."""
    b, _no, _ti = _notes_box(box, title=False)
    sess = b.sess
    sess._notes = {"status": "done", "markdown": None, "generated_at": None, "model": None,
                   "cost_usd": None, "error": None, "auto": False}
    sess._notes_body = "## Кратко\nA."
    gate, inside = threading.Event(), threading.Event()
    sent: list[str] = []
    snaps: list[str] = []
    real_notes = sess.notes

    def notes() -> dict[str, Any] | None:
        n = real_notes()
        snaps.append(threading.current_thread().name)
        return n

    def emit(ev: str, data: dict[str, Any]) -> None:
        if ev != "meeting_notes":
            return
        name = threading.current_thread().name
        if name == "first":
            inside.set()
            assert gate.wait(5)
        sent.append(name)

    sess.notes = notes  # type: ignore[method-assign]
    sess._emit_fn = emit
    t1 = threading.Thread(target=sess._emit_notes, name="first")
    t1.start()
    assert inside.wait(5)
    t2 = threading.Thread(target=sess._emit_notes, name="second")
    t2.start()
    time.sleep(0.1)
    assert snaps == ["first"]                    # второй ждёт, пока первый не отправит
    gate.set()
    t1.join(5)
    t2.join(5)
    assert snaps == ["first", "second"] and sent == ["first", "second"]


def test_notes_thread_survives_a_failing_error_handler(box: Any,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """Разбор неудачи заметок сам упал: заметки не «пишутся» навсегда (_notes_busy снят,
    поток названия не ждёт), в окне — ошибка, и по кнопке их можно заказать снова."""
    b, _no, _ti = _notes_box(box, RuntimeError("boom"), "# Снова\n\n## Кратко\nB.",
                             title=False)
    sess = b.sess

    def broken(e: BaseException, auto: bool) -> None:
        raise ValueError("handler broke")

    monkeypatch.setattr(sess, "_notes_failed", broken)
    sess.start()
    _lines(b, 2)
    sess.request_notes()
    wait_for(lambda: not sess._notes_busy)
    assert sess._notes_idle.is_set()
    n = sess.notes()
    assert n is not None and n["status"] == "error" and n["error"]
    sess.request_notes()
    n = _wait_notes(b, "done")
    assert n["markdown"] is not None and "B." in n["markdown"]


def test_notes_thread_unsticks_when_emit_between_calls_fails(box: Any) -> None:
    """Событие между двумя вызовами (конец встречи, пока заметки писались) упало: поток
    заметок кончается, но _notes_busy не залипает, и прежние удачные заметки остаются."""
    b, no, _ti = _notes_box(box, "# Первые\n\n## Кратко\nA.", title=False)
    sess = b.sess
    no.gate = threading.Event()
    sess.start()
    _lines(b, 2)
    sess.request_notes()
    wait_for(lambda: len(no.calls) == 1)
    sess._notes_again = True                     # «встреча кончилась, пока писались»
    real = sess._emit_status

    def boom(force: bool = False) -> None:
        if threading.current_thread().name == "rupor-meeting-notes" and sess._notes_again is False \
                and (sess._notes or {}).get("status") == "running":
            raise RuntimeError("emit broke")
        real(force)

    sess._emit_status = boom  # type: ignore[method-assign]
    no.gate.set()
    wait_for(lambda: not sess._notes_busy)
    assert sess._notes_idle.is_set()
    n = sess.notes()
    assert n is not None and n["status"] == "done" and "A." in (n["markdown"] or "")
