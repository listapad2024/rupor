"""Название записи встречи: ai.meeting_title (промпт, тело запроса, очистка ответа), название
без ИИ (заголовок окна без хвоста браузера, служба встреч, первые слова, никогда — имя exe),
имя файла (запрещённые символы, имена устройств Windows, «(2)»), переименование файла, пока
строки дописываются, когда сеанс зовёт ИИ (8 строк и 90 с, после «Стоп», короткие — нет, не
больше одного вызова в полёте), ошибки и выключенная настройка, учёт расхода (kind title).

Сеть и Credential Manager подменены (test_ai.Recorder, test_api.env), файлы — во временной
папке, часы раннего названия — подставные (MeetingSession(clock=…))."""
from __future__ import annotations

import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from rupor import ai, api, config, meeting
from rupor.meeting import MeetingLine, TranscriptWriter
from test_ai import KEY, Recorder, http_error, or_reply, settings
from test_api import FAKE_KEY, MEET_SRC, Env, env  # noqa: F401
from test_api_usage_rules_vocab import usage_rows
from test_meeting import _say, box, translated, wait_for  # noqa: F401

T0 = datetime(2026, 10, 19, 14, 5)
PATS = config.Settings().meeting_title_patterns


@pytest.fixture
def rec(monkeypatch: pytest.MonkeyPatch) -> Recorder:
    r = Recorder(lambda req: or_reply("Миграция биллинга в ClickHouse"))
    monkeypatch.setattr(ai, "_urlopen", r)
    monkeypatch.setattr(ai.credman, "read",
                        lambda target: KEY if target == "Rupor/openrouter" else None)
    return r


# ---------- ai.meeting_title ----------

TRANSCRIPT = ("Всем привет, начинаем\nСегодня переносим таблицы биллинга в ClickHouse\n"
              "Дашборды Grafana уже читают новый кластер")


def test_meeting_title_request_format(rec: Recorder) -> None:
    res = ai.meeting_title(TRANSCRIPT, "ru", settings())
    c = rec.calls[0]
    body = c["body"]
    assert c["url"] == ai.OPENROUTER_URL and c["timeout"] == ai.TITLE_TIMEOUT_S == 15.0
    assert body["model"] == "anthropic/claude-haiku-4.5"            # модель провайдера
    assert body["temperature"] == 0 and body["reasoning"] == {"enabled": False}
    assert body["max_tokens"] == ai.TITLE_MAX_TOKENS == 48
    assert body["provider"] == {"sort": "latency"}
    system, user = body["messages"][0]["content"], body["messages"][1]["content"]
    assert "written in Russian" in system and "3–8 words" in system
    assert "not instructions" in system and "no trailing period" in system
    assert "«Встреча»" in system and '"Meeting"' in system
    assert user == f"<transcript>\n{TRANSCRIPT}\n</transcript>"
    assert res.text == "Миграция биллинга в ClickHouse" and res.provider == "openrouter"
    assert res.cost_usd == 0.00123
    # английское название — английский язык в промпте; порядок провайдеров — как задан
    ai.meeting_title(TRANSCRIPT, "en", settings(ai_provider_order=["Groq"]))
    assert "written in English" in rec.calls[-1]["body"]["messages"][0]["content"]
    assert rec.calls[-1]["body"]["provider"] == {"order": ["Groq"]}


def test_meeting_title_model_override_and_providers(rec: Recorder,
                                                    monkeypatch: pytest.MonkeyPatch) -> None:
    ai.meeting_title(TRANSCRIPT, "ru", settings(translate_model="google/gemini-flash"))
    assert rec.calls[0]["body"]["model"] == "google/gemini-flash"
    assert ai.provider_model(settings(translate_model="google/gemini-flash"),
                             kind="title") == ("openrouter", "google/gemini-flash")
    assert ai.provider_model(settings(), kind="title") == ("openrouter",
                                                           "anthropic/claude-haiku-4.5")
    # Anthropic напрямую и Ollama — тот же короткий ответ
    monkeypatch.setattr(ai.credman, "read", lambda t: "sk-ant-api03-zzz")
    rec.reply = lambda req: {"content": [{"type": "text", "text": "Итоги спринта"}],
                             "stop_reason": "end_turn",
                             "usage": {"input_tokens": 300, "output_tokens": 6}}
    r = ai.meeting_title(TRANSCRIPT, "ru", settings(ai_provider="anthropic"))
    assert r.text == "Итоги спринта" and rec.calls[-1]["body"]["max_tokens"] == 48
    assert rec.calls[-1]["body"]["model"] == "claude-haiku-4-5"
    rec.reply = lambda req: {"message": {"content": "Итоги спринта"}, "done_reason": "stop"}
    ai.meeting_title(TRANSCRIPT, "ru", settings(ai_provider="ollama"))
    assert rec.calls[-1]["body"]["options"] == {"temperature": 0, "num_predict": 48}


def test_meeting_title_long_transcript_is_head_and_tail(rec: Recorder) -> None:
    lines = [f"Строка {i:04d} про релиз и бюджет" for i in range(600)]
    ai.meeting_title("\n".join(lines), "ru", settings())
    user = rec.calls[0]["body"]["messages"][1]["content"]
    inner = user.removeprefix("<transcript>\n").removesuffix("\n</transcript>")
    assert len(inner) <= ai.TITLE_INPUT_CHARS + 10
    head, tail = inner.split("\n…\n")
    assert head.startswith("Строка 0000") and tail.endswith("Строка 0599 про релиз и бюджет")
    assert len(head) <= ai.TITLE_HEAD_CHARS and len(tail) <= ai.TITLE_TAIL_CHARS
    assert all(x.startswith("Строка ") for x in head.split("\n") + tail.split("\n"))
    assert ai.title_excerpt("a\n\n  b  \n") == "a\nb"


@pytest.mark.parametrize("out,expected", [
    ("Миграция биллинга в ClickHouse", "Миграция биллинга в ClickHouse"),
    ("«Миграция биллинга в ClickHouse».", "Миграция биллинга в ClickHouse"),
    ('"Q4 roadmap for Rupor"', "Q4 roadmap for Rupor"),
    ("'Phoenix launch plan'", "Phoenix launch plan"),
    ("`Phoenix launch plan`", "Phoenix launch plan"),
    ("Let's ship Phoenix", "Let's ship Phoenix"),           # апостроф внутри слова — не кавычка
    ("Название: Перенос дашбордов Grafana", "Перенос дашбордов Grafana"),
    ("Название встречи: «Перенос дашбордов»", "Перенос дашбордов"),
    ("Title: Budget review for 2027", "Budget review for 2027"),
    ("**Title:** Budget review", "Budget review"),
    ("# Бюджет на 2027 год", "Бюджет на 2027 год"),
    ("**Бюджет** на 2027 год", "Бюджет на 2027 год"),
    ("Title:\nDesign review of the pill", "Design review of the pill"),
    ("\n\n  Итоги   спринта  \nПояснение: про спринт", "Итоги спринта"),
    ("```\nИтоги спринта\n```", "Итоги спринта"),
    ("<title>Итоги спринта</title>", "Итоги спринта"),
    ("Обсуждение " + "очень длинного названия " * 4,
     "Обсуждение очень длинного названия очень длинного названия очень длинного"),
    ("Встреча", ""), ("Meeting.", ""), ("Созвон", ""),
    ("I'm sorry, I can't title this", ""), ("Извините, расшифровка пуста", ""),
    ("2027-10-19", ""), ("", ""), ("   \n  ", ""),
    ("x " * 20, ""),                                       # больше TITLE_MAX_WORDS слов
    # невидимые и направляющие символы, обрезанный эмодзи (одиночный суррогат) — прочь
    (chr(0x202E) + "Итоги" + chr(0x200B) + " спринта" + chr(0xD83D), "Итоги спринта"),
    ("Итоги" + chr(0x7) + "спринта" + chr(0xFEFF), "Итоги спринта"),
])
def test_clean_title(out: str, expected: str) -> None:
    got = ai.clean_title(out)
    assert got == expected
    assert len(got) <= ai.TITLE_MAX_CHARS


def test_visible_text_keeps_joiners() -> None:
    # ZWNJ (персидский) и ZWJ (эмодзи) — часть написания; направляющие, BOM, суррогат — прочь
    fa = "می" + chr(0x200C) + "خواهم"
    assert ai.visible_text(fa) == fa
    dev = chr(0x1F468) + chr(0x200D) + chr(0x1F4BB)
    assert ai.visible_text(dev) == dev
    assert ai.visible_text(chr(0x202B) + "a" + chr(0x2066) + "b" + chr(0xFEFF) + chr(0xDC00)
                           + "\tc") == "ab c"


def test_meeting_title_rejects_garbage_and_keeps_error_kinds(rec: Recorder,
                                                             monkeypatch: pytest.MonkeyPatch
                                                             ) -> None:
    rec.reply = lambda req: or_reply("Встреча")
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_title(TRANSCRIPT, "ru", settings())
    assert ei.value.kind == "bad_response" and "название" in str(ei.value)
    rec.reply = lambda req: or_reply("Бюджет", finish="length")
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_title(TRANSCRIPT, "ru", settings())
    assert ei.value.kind == "truncated"
    rec.reply = lambda req: http_error(402, {"error": {"message": "Insufficient credits"}})
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_title(TRANSCRIPT, "ru", settings())
    assert ei.value.kind == "payment"
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_title("  \n ", "ru", settings())
    assert ei.value.kind == "bad_request"
    monkeypatch.setattr(ai.credman, "read", lambda t: None)
    with pytest.raises(ai.AIError) as ei:
        ai.meeting_title(TRANSCRIPT, "ru", settings())
    assert ei.value.kind == "no_key"


# ---------- имя файла ----------

@pytest.mark.parametrize("name,expected", [
    ("CON", "CON_"), ("nul", "nul_"), ("Aux", "Aux_"), ("PRN", "PRN_"), ("COM1", "COM1_"),
    ("lpt9", "lpt9_"), ("COM¹", "COM¹_"), ("nul.txt", "nul_.txt"), ("con .md", "con_ .md"),
    ("Console", "Console"), ("COM10", "COM10"), ("Contoso", "Contoso"),
    ("Q3: план?*", "Q3 план"), ('a<b>c"d/e\\f|g', "a b c d e f g"),
    ("Итоги. . .", "Итоги"), ("  точки в конце ...  ", "точки в конце"),
    ("...", "meeting"), ("", "meeting"),
    # путь — не путь: разделители и «..» остаются частью имени в той же папке
    ("../../evil", ".. .. evil"), ("..", "meeting"), (r"C:\Windows\x", "C Windows x"),
    # U+202E перевернул бы имя в проводнике («abc<U+202E>exe.dm.md» → «abcdm.md.exe»), U+200E
    # невидим, одиночный суррогат в UTF-8 не записать, DEL — управляющий
    ("abc" + chr(0x202E) + "exe.dm", "abcexe.dm"), ("Daily" + chr(0x200E) + " standup",
                                                    "Daily standup"),
    ("Итоги " + chr(0xD83D), "Итоги"), ("a" + chr(0x7F) + "b", "a b"),
])
def test_safe_file_part(name: str, expected: str) -> None:
    assert meeting.safe_file_part(name) == expected


def test_safe_file_part_length_at_word_boundary() -> None:
    s = meeting.safe_file_part("слово " * 30)
    assert len(s) <= meeting.TITLE_PART_MAX and s.endswith("слово") and "  " not in s
    assert meeting.safe_file_part("x" * 200) == "x" * meeting.TITLE_PART_MAX


# ---------- название без ИИ ----------

@pytest.mark.parametrize("title,expected", [
    ("Meet – abc-defg-hij - Google Chrome", "Google Meet"),
    ("Meet - abc-defg-hij – Brave", "Google Meet"),
    ("Meet – Weekly sync - Google Chrome", "Weekly sync"),
    ("Google Meet - Vivaldi", "Google Meet"),
    ("Daily standup - Personal - Microsoft" + chr(0x200b) + " Edge", "Daily standup"),
    ("Daily standup and 3 more pages - Work - Microsoft Edge", "Daily standup"),
    ("Планёрка — Яндекс Браузер", "Планёрка"),
    ("Планёрка - Opera", "Планёрка"),
    ("(3) Ретро команды - Mozilla Firefox", "Ретро команды"),
    ("Zoom Meeting", "Zoom"), ("Zoom Workplace", "Zoom"), ("Zoom", "Zoom"),
    ("Planning | Microsoft Teams", "Planning"),
    # невидимая метка направления и обрезанный пополам эмодзи в заголовке — прочь
    ("Planning" + chr(0x200F) + " " + chr(0xD83D) + "| Microsoft Teams", "Planning"),
    ("Chat | Microsoft Teams", "Microsoft Teams"),
    ("Calendar | Microsoft Teams (work or school)", "Microsoft Teams"),
    ("Видеовстреча — Телемост", "Телемост"),
    ("Синк по релизу — Телемост — Яндекс Браузер", "Синк по релизу"),
    ("Lo-fi beats – YouTube - Google Chrome", "Lo-fi beats – YouTube"),
    ("New Tab - Google Chrome", ""), ("Новая вкладка — Яндекс Браузер", ""),
    ("Google Chrome", ""), ("Brave", ""),
    (r"C:\Python313\python.exe", ""), ("", ""), ("   ", ""), ("12:30", ""),
])
def test_clean_window_title(title: str, expected: str) -> None:
    assert meeting.clean_window_title(title, PATS) == expected


def _src(**kw: Any) -> dict[str, Any]:
    return {"id": "proc:9", "kind": "process", "pid": 9, "exe": "brave.exe", "name": "Brave",
            "title": "", "meeting": False, "active": True, **kw}


def test_fallback_title_order_and_never_exe_stem() -> None:
    fb = meeting.fallback_title
    # заголовок окна встречи — первым, даже когда первая строка есть
    assert fb(_src(title="Meet – abc-defg-hij - Brave"), PATS, "Всем привет") == "Google Meet"
    # без заголовка — первые слова первой строки
    assert fb(_src(), PATS, "Ну что, коллеги, давайте начнём наш еженедельный синк") == (
        "Встреча — Ну что, коллеги, давайте начнём наш")
    assert fb(_src(), PATS, "大家好，我们开始今天的会议吧，先看一下进度", "zh") == "Встреча — 大家好，我们开始今天的会"
    # строки ещё нет — понятное имя программы, иначе «Встреча»
    assert fb(_src(), PATS) == "Brave"
    assert fb(_src(title="Telegram", exe="telegram.exe", name="Telegram Desktop"), PATS,
              "Привет всем") == "Встреча — Привет всем"
    assert fb(_src(title="Telegram", exe="telegram.exe", name="Telegram"), PATS) == "Telegram"
    # имя exe — никогда: «Python», «rupor-core», путь в заголовке консоли
    for exe, name in (("python.exe", "Python"), ("pythonw.exe", "Python"),
                      ("rupor-core.exe", "rupor-core"), ("node.exe", "Node.js"),
                      ("someapp.exe", "Someapp"), ("someapp.exe", "")):
        src = _src(exe=exe, name=name, title=r"C:\Python313\python.exe")
        got = fb(src, PATS)
        assert got not in ("Python", "rupor-core", "Someapp", "someapp", "python") and (
            got in ("Встреча", "Node.js")), (exe, name, got)
    assert fb(_src(exe="python.exe", name="Python", title="Python"), PATS) == "Встреча"
    # неизвестная программа с описанием — её описание
    assert fb(_src(exe="vlc.exe", name="VLC media player"), PATS) == "VLC media player"
    # весь звук компьютера — не «Весь звук компьютера»
    system = {"id": "system", "kind": "system", "pid": None, "exe": "",
              "name": "Весь звук компьютера", "title": "", "meeting": False, "active": False}
    assert fb(system, PATS) == "Встреча"
    assert fb(system, PATS, "Hello everyone, let's talk about the budget") == (
        "Встреча — Hello everyone, let's talk about the")
    assert fb(None) == "Встреча"


# ---------- TranscriptWriter: имя, заголовок, переименование ----------

def _line(i: int, text: str | None = None) -> MeetingLine:
    return MeetingLine(i, float(i), f"14:{i // 60 % 60:02d}:{i % 60:02d}", "en",
                       text or f"line {i}", None, "original")


def _body(path: Path) -> list[str]:
    return [x for x in path.read_text(encoding="utf-8").splitlines() if x.startswith("**")]


def test_rename_while_lines_keep_arriving(tmp_path: Path) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave — Meet", "Google Meet", "Языки: en")
    n = 400
    started = threading.Event()

    def writer() -> None:
        for i in range(1, n + 1):
            w.write(_line(i))
            if i == 5:
                started.set()

    th = threading.Thread(target=writer)
    th.start()
    assert started.wait(5)
    titles = ["Перенос биллинга", "Второе: название?", "Итоги спринта по биллингу"]
    renamed = []
    for t in titles:
        renamed.append(w.rename(t))
        time.sleep(0.01)
    th.join(10)
    assert renamed == [True, True, True]
    w.write(_line(n + 1, "after rename"))          # файл снова открыт на дозапись
    w.close()
    files = sorted(tmp_path.iterdir())
    assert files == [w.path], files                 # ни старых, ни временных файлов
    assert w.path is not None and w.path.name == "2026-10-19 14-05 — Итоги спринта по биллингу.md"
    text = w.path.read_text(encoding="utf-8")
    assert text.startswith("# Итоги спринта по биллингу\n\n- Дата: 2026-10-19 14:05\n"
                           "- Источник: Brave — Meet\n- Языки: en\n\n")
    assert _body(w.path) == [meeting.line_markdown(_line(i)).split("\n")[0]
                             for i in range(1, n + 1)] + [
        meeting.line_markdown(_line(n + 1, "after rename")).split("\n")[0]]
    assert [x for x in text.splitlines() if x.startswith("# ")] == ["# Итоги спринта по биллингу"]
    assert text.count("\n## Полная расшифровка\n") == 1 and w.title == "Итоги спринта по биллингу"


def test_rename_before_file_and_after_close(tmp_path: Path) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    assert w.rename("Ранний синк") is True and w.path is None    # файла ещё нет
    assert w.rename("Ранний синк") is False                      # то же название
    w.write(_line(1))
    assert w.path is not None and w.path.name == "2026-10-19 14-05 — Ранний синк.md"
    w.close()
    assert w.rename("Итоги после конца") is True                 # после close — тоже
    assert w.path.name == "2026-10-19 14-05 — Итоги после конца.md" and w._f is None
    # строка опоздала к close (stop(drain=False): последний проход ещё шёл) — дописана, а
    # файл снова закрыт
    w.write(_line(2))
    assert w._f is None
    assert _body(w.path) == ["**14:00:01** line 1", "**14:00:02** line 2"]
    assert w.path.read_text(encoding="utf-8").startswith("# Итоги после конца\n")
    assert sorted(tmp_path.iterdir()) == [w.path]


def test_rename_collision_and_same_file_name(tmp_path: Path) -> None:
    taken = tmp_path / "2026-10-19 14-05 — Итоги.md"
    taken.write_text("чужой файл", encoding="utf-8")
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    w.write(_line(1))
    assert w.rename("Итоги") is True
    assert w.path is not None and w.path.name == "2026-10-19 14-05 — Итоги (2).md"
    assert taken.read_text(encoding="utf-8") == "чужой файл"
    # название другое, а имя файла то же (разница — в запрещённых символах): заголовок
    # меняется на месте
    old = w.path
    assert w.rename("Итоги?") is True and w.path == old
    assert old.read_text(encoding="utf-8").startswith("# Итоги?\n")
    w.write(_line(2))
    w.close()
    assert _body(old) == ["**14:00:01** line 1", "**14:00:02** line 2"]
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "2026-10-19 14-05 — Итоги (2).md", "2026-10-19 14-05 — Итоги.md"]


def test_rename_failure_keeps_old_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    w.write(_line(1))
    old = w.path
    assert old is not None

    def boom(src: Any, dst: Any) -> None:
        raise OSError(13, "занято")

    monkeypatch.setattr(meeting.os, "replace", boom)
    assert w.rename("Новое название") is False
    monkeypatch.undo()
    assert w.path == old and w.title == "Google Meet"
    w.write(_line(2))                                  # дописывается в прежний файл
    assert sorted(tmp_path.iterdir()) == [old]         # временный и занятый — прочь
    assert old.read_text(encoding="utf-8").startswith("# Google Meet\n")
    assert _body(old) == ["**14:00:01** line 1", "**14:00:02** line 2"]

    # старый файл не удалить (открыт в другой программе) — новый прочь, старый остаётся
    def no_remove(path: Any) -> None:
        raise PermissionError(13, "файл открыт")

    monkeypatch.setattr(meeting.os, "remove", no_remove)
    assert w.rename("Новое название") is False
    monkeypatch.undo()
    w.write(_line(3))
    w.close()
    assert sorted(tmp_path.iterdir()) == [old]
    assert _body(old) == ["**14:00:01** line 1", "**14:00:02** line 2", "**14:00:03** line 3"]


def test_rename_stress_two_writers_no_lost_or_duplicated_lines(tmp_path: Path) -> None:
    """Два потока дописывают строки, третий без остановки переименовывает: в занятые чужими
    файлами имена, назад, только регистром, «путями» и слишком длинными названиями. Строки —
    все и по разу, в файле один заголовок, чужие файлы целы, лишних файлов нет."""
    theirs = {"2026-10-19 14-05 — Итоги.md": "чужой", "2026-10-19 14-05 — B.md": "чужой B"}
    for name, text in theirs.items():
        (tmp_path / name).write_text(text, encoding="utf-8")
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    per_thread = 300
    ids = iter(range(1, 2 * per_thread + 1))
    ids_lock = threading.Lock()

    def writer() -> None:
        for _ in range(per_thread):
            with ids_lock:
                i = next(ids)
            w.write(_line(i, f"line {i}"))

    ths = [threading.Thread(target=writer) for _ in range(2)]
    for th in ths:
        th.start()
    titles = ["A", "B", "Итоги", "a", "Итоги?", "Итоги", "CON", "x" * 200, "../../evil",
              r"C:\Windows\x", "A"]
    renames = 0
    while any(th.is_alive() for th in ths) or renames < len(titles):
        w.rename(titles[renames % len(titles)])
        renames += 1
    for th in ths:
        th.join(10)
    w.close()
    assert w.rename("Итог после конца") is True
    assert w.path is not None and w.path.parent == tmp_path
    nums = [int(x.rsplit(" ", 1)[1]) for x in _body(w.path)]
    assert sorted(nums) == list(range(1, 2 * per_thread + 1))       # ни потерь, ни повторов
    text = w.path.read_text(encoding="utf-8")
    assert text.startswith("# Итог после конца\n") and text.count("\n# ") == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([*theirs, w.path.name])
    for name, body in theirs.items():
        assert (tmp_path / name).read_text(encoding="utf-8") == body


def test_rename_case_only_stays_in_place(tmp_path: Path) -> None:
    # раннее и последнее названия различаются только регистром — то же имя, не «(2)»
    w = TranscriptWriter(tmp_path, T0, "Brave", "Обсуждение API", "langs")
    w.write(_line(1))
    assert w.rename("обсуждение API") is True
    w.write(_line(2))
    assert w.path is not None and w.path.name == "2026-10-19 14-05 — обсуждение API.md"
    assert [p.name for p in tmp_path.iterdir()] == [w.path.name]
    assert w.path.read_text(encoding="utf-8").startswith("# обсуждение API\n")
    # «(2)» (имя без него занято чужим файлом) остаётся «(2)»
    (tmp_path / "2026-10-19 14-05 — Итоги.md").write_text("чужой", encoding="utf-8")
    assert w.rename("Итоги") is True and w.path.name == "2026-10-19 14-05 — Итоги (2).md"
    assert w.rename("ИТОГИ") is True and w.path.name == "2026-10-19 14-05 — ИТОГИ (2).md"
    w.write(_line(3))
    w.close()
    assert sorted(p.name for p in tmp_path.iterdir()) == sorted([
        "2026-10-19 14-05 — Итоги.md", "2026-10-19 14-05 — ИТОГИ (2).md"])
    assert (tmp_path / "2026-10-19 14-05 — Итоги.md").read_text(encoding="utf-8") == "чужой"
    assert _body(w.path) == ["**14:00:01** line 1", "**14:00:02** line 2", "**14:00:03** line 3"]


def test_writer_survives_text_utf8_cannot_hold(tmp_path: Path) -> None:
    # заголовок окна с обрезанным пополам эмодзи (одиночный суррогат): раньше —
    # UnicodeEncodeError на каждой строке (поток распознавания считал его сбоем и через три
    # останавливал встречу), а рядом копились пустые «… (2)», «… (3)»
    w = TranscriptWriter(tmp_path, T0, "Chat " + chr(0xD83D) + " - Brave", "A", "langs")
    w.write(_line(1))
    w.write(_line(2))
    assert w.rename("Итоги " + chr(0xD83D) + "спринта") is True
    w.write(_line(3))
    w.close()
    assert w.path is not None and [p.name for p in tmp_path.iterdir()] == [w.path.name]
    assert w.path.name == "2026-10-19 14-05 — Итоги спринта.md"
    text = w.path.read_text(encoding="utf-8")
    assert text.startswith("# Итоги ?спринта\n") and "- Источник: Chat ? - Brave\n" in text
    assert _body(w.path) == ["**14:00:01** line 1", "**14:00:02** line 2", "**14:00:03** line 3"]


def test_header_failure_leaves_no_empty_file(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")

    def no_space(self: TranscriptWriter, title: str) -> str:
        raise OSError(28, "нет места на диске")

    monkeypatch.setattr(TranscriptWriter, "_header", no_space)
    for i in (1, 2):
        with pytest.raises(OSError):
            w.write(_line(i))
    assert list(tmp_path.iterdir()) == [] and w.path is None     # пустых «(2)» нет
    monkeypatch.undo()
    w.write(_line(3))
    w.close()
    assert [p.name for p in tmp_path.iterdir()] == ["2026-10-19 14-05 — Google Meet.md"]


def test_freeze_waits_for_rename_in_flight_and_blocks_new_ones(tmp_path: Path) -> None:
    # выход ядра (os._exit сразу после): переименование, которое уже идёт, доделывается,
    # новое не начинается — оборванное посередине оставило бы пустой или второй файл
    w = TranscriptWriter(tmp_path, T0, "Brave", "Google Meet", "langs")
    w.write(_line(1))
    held, release, frozen = threading.Event(), threading.Event(), threading.Event()

    def in_flight() -> None:                     # «переименование» держит замок писателя
        with w._lock:
            held.set()
            assert release.wait(5)

    th = threading.Thread(target=in_flight)
    th.start()
    assert held.wait(5)
    fz = threading.Thread(target=lambda: (w.freeze(), frozen.set()))
    fz.start()
    time.sleep(0.05)
    assert not frozen.is_set()                   # ждёт идущего
    release.set()
    th.join(5)
    assert frozen.wait(5)
    fz.join(5)
    assert w.rename("Итоги спринта") is False and w.title == "Google Meet"
    w.write(_line(2))                            # строки — как прежде
    w.close()
    assert [p.name for p in tmp_path.iterdir()] == ["2026-10-19 14-05 — Google Meet.md"]
    assert _body(tmp_path / "2026-10-19 14-05 — Google Meet.md") == [
        "**14:00:01** line 1", "**14:00:02** line 2"]


# ---------- когда сеанс зовёт ИИ ----------

class Titles:
    """title_fn сеанса: ответы по очереди (строка — название, исключение — сбой); gate —
    вызов ждёт, пока его не отпустят; active/max_active — вызовов в полёте."""

    def __init__(self, *replies: Any) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []
        self.gate: threading.Event | None = None
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def __call__(self, text: str, lang: str) -> ai.AIResult:
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.calls.append((text, lang))
        try:
            if self.gate is not None:
                assert self.gate.wait(10)
            r = self.replies.pop(0) if self.replies else "Миграция биллинга в ClickHouse"
            if isinstance(r, BaseException):
                raise r
            return ai.AIResult(r, 5.0, 0.0001, "openrouter", "m")
        finally:
            with self._lock:
                self.active -= 1


def _titled(box: Any, *replies: Any, **kw: Any) -> tuple[Any, Titles, list[float]]:
    ti = Titles(*replies)
    now = [1000.0]
    b = box(sess_kw={"title_fn": ti, "clock": lambda: now[0]}, **kw)
    return b, ti, now


def _lines(b: Any, n: int) -> None:
    have = len(b.lines())
    b.rec.results = [f"Phrase {i} about the billing migration" for i in range(have, have + n)]
    for _ in range(n):
        b.say()
    b.wait_lines(have + n)
    wait_for(lambda: b.sess._next_write > have + n)     # и уже в файле


def _done(b: Any) -> None:
    th = b.sess._title_thread
    assert th is not None
    th.join(5)
    assert not th.is_alive()


def test_early_title_after_8_lines_and_90_s_then_final(box: Any, tmp_path: Path) -> None:
    b, ti, now = _titled(box)
    b.sess.start()
    assert b.sess.status()["title"] == "Google Meet"            # без ИИ — сразу
    _lines(b, 8)
    first = b.sess.file
    assert first is not None and first.name.endswith(" — Google Meet.md")
    now[0] += 60                                                # 8 строк, но 60 с — рано
    b.sess._title_kick()
    time.sleep(0.15)
    assert ti.calls == []
    now[0] += 31
    b.sess._title_kick()
    # статус с названием — после переименования файла
    wait_for(lambda: any(d.get("title") == "Миграция биллинга в ClickHouse" for d in b.statuses()))
    text, lang = ti.calls[0]
    # расшифровка — переводы строк (без перевода — оригинал), название — на языке перевода
    assert lang == "ru" and text.splitlines() == [
        f"RU:Phrase {i} about the billing migration" for i in range(8)]
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Миграция биллинга в ClickHouse.md")
    assert not first.exists() and sorted(tmp_path.glob("*.md")) == [f]
    assert f.read_text(encoding="utf-8").startswith("# Миграция биллинга в ClickHouse\n")
    assert len(_body(f)) == 8
    ev = [d for d in b.statuses() if d.get("title") == "Миграция биллинга в ClickHouse"]
    assert ev and ev[-1]["file"] == str(f)
    # дальше — без ранних вызовов; строки пишутся в новый файл
    _lines(b, 2)
    time.sleep(0.1)
    assert len(ti.calls) == 1 and len(_body(f)) == 10
    # «Стоп» — ещё раз по всей расшифровке; другое название — другое имя файла
    ti.replies = ["Перенос биллинга и дашборды Grafana"]
    b.sess.stop()
    _done(b)
    assert len(ti.calls) == 2 and len(ti.calls[1][0].splitlines()) == 10
    g = b.sess.file
    assert g is not None and g.name.endswith(" — Перенос биллинга и дашборды Grafana.md")
    assert sorted(tmp_path.glob("*.md")) == [g] and len(_body(g)) == 10
    assert b.sess.status()["title"] == "Перенос биллинга и дашборды Grafana"
    assert ti.max_active == 1


def test_final_title_same_as_current_keeps_file(box: Any, tmp_path: Path) -> None:
    b, ti, now = _titled(box, "Итоги спринта", "Итоги спринта")
    b.sess.start()
    _lines(b, 8)
    now[0] += 91
    b.sess._title_kick()
    # статус с названием — после переименования файла
    wait_for(lambda: any(d.get("title") == "Итоги спринта" for d in b.statuses()))
    f = b.sess.file
    _lines(b, 1)
    b.sess.stop()
    _done(b)
    assert len(ti.calls) == 2 and b.sess.file == f and f is not None and f.exists()


def test_failed_rename_is_retried_after_stop(box: Any, tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    """Раннее название не дошло до файла (старый файл держала другая программа): после «Стоп»
    файл переименовывается ещё раз — и когда новых строк нет (ИИ не зовётся), и когда ИИ
    ответил тем же названием."""
    real_remove = meeting.os.remove
    blocked = [True]

    def remove(path: Any) -> None:
        if blocked[0]:
            raise PermissionError(13, "файл открыт в другой программе")
        real_remove(path)

    monkeypatch.setattr(meeting.os, "remove", remove)
    for more in (0, 2):
        for p in tmp_path.glob("*.md"):
            p.unlink()
        blocked[0] = True
        b, ti, now = _titled(box, "Итоги спринта", "Итоги спринта")
        b.sess.start()
        _lines(b, 8)
        first = b.sess.file
        now[0] += 91
        b.sess._title_kick()
        wait_for(lambda: any(d.get("title") == "Итоги спринта" for d in b.statuses()))
        assert b.sess.file == first and sorted(tmp_path.glob("*.md")) == [first]
        assert first is not None and first.read_text(encoding="utf-8").startswith("# Google Meet\n")
        blocked[0] = False                      # программа файл отпустила
        if more:
            _lines(b, more)
        b.sess.stop()
        _done(b)
        assert len(ti.calls) == (2 if more else 1)
        f = b.sess.file
        assert f is not None and f.name.endswith(" — Итоги спринта.md"), f
        assert sorted(tmp_path.glob("*.md")) == [f] and not first.exists()
        assert f.read_text(encoding="utf-8").startswith("# Итоги спринта\n")
        assert len(_body(f)) == 8 + more
        assert b.statuses()[-1]["file"] == str(f)       # статус — с новым файлом


def test_short_meetings_get_no_ai_call(box: Any, tmp_path: Path) -> None:
    b, ti, now = _titled(box)
    b.sess.start()
    _lines(b, 2)
    now[0] += 600
    b.sess.stop()
    _done(b)
    assert ti.calls == [] and b.sess.status()["title"] == "Google Meet"
    # три строки — одно название после «Стоп» (раннего не было: строк меньше 8)
    b2, ti2, now2 = _titled(box)
    b2.sess.start()
    _lines(b2, 3)
    now2[0] += 600
    b2.sess._title_kick()
    time.sleep(0.1)
    assert ti2.calls == []
    b2.sess.stop()
    _done(b2)
    assert len(ti2.calls) == 1 and len(ti2.calls[0][0].splitlines()) == 3
    assert b2.sess.status()["title"] == "Миграция биллинга в ClickHouse"


def test_one_title_call_in_flight(box: Any) -> None:
    b, ti, now = _titled(box, "Раннее название", "Последнее название")
    ti.gate = threading.Event()
    b.sess.start()
    _lines(b, 8)
    now[0] += 91
    b.sess._title_kick()
    wait_for(lambda: len(ti.calls) == 1)
    _lines(b, 1)
    b.sess.stop()                                  # не ждёт вызова в полёте
    assert b.sess.running is False
    wait_for(lambda: b.sess._finalized)
    time.sleep(0.15)
    assert len(ti.calls) == 1 and ti.active == 1   # последнее ждёт, пока не кончится раннее
    ti.gate.set()
    _done(b)
    assert len(ti.calls) == 2 and ti.max_active == 1
    assert b.sess.status()["title"] == "Последнее название"


def test_disabled_setting_keeps_name_without_ai(box: Any, tmp_path: Path) -> None:
    b, ti, now = _titled(box, meeting_ai_title=False)
    b.sess.start()
    _lines(b, 8)
    now[0] += 120
    b.sess._title_kick()
    b.sess.stop()
    _done(b)
    assert ti.calls == [] and b.sess.status()["title"] == "Google Meet"
    f = b.sess.file
    assert f is not None and f.name.endswith(" — Google Meet.md")


def test_ai_errors_keep_name_and_no_key_stops_titles(box: Any) -> None:
    # сеть подвела в раннем вызове — название без ИИ остаётся, после «Стоп» — ещё попытка
    b, ti, now = _titled(box, ai.AIError("нет сети", "network"), "Итоги спринта")
    b.sess.start()
    _lines(b, 8)
    now[0] += 91
    b.sess._title_kick()
    wait_for(lambda: len(ti.calls) == 1)
    time.sleep(0.05)
    assert b.sess.status()["title"] == "Google Meet"
    b.sess.stop()                       # новых строк нет, но ответа на них не было — ещё раз
    _done(b)
    assert len(ti.calls) == 2 and ti.calls[0] == ti.calls[1]
    assert b.sess.status()["title"] == "Итоги спринта"
    # нет ключа — больше не звать до конца встречи
    b2, ti2, now2 = _titled(box, ai.AIError("нет ключа", "no_key"))
    b2.sess.start()
    _lines(b2, 8)
    now2[0] += 91
    b2.sess._title_kick()
    wait_for(lambda: len(ti2.calls) == 1)
    _lines(b2, 1)
    b2.sess.stop()
    _done(b2)
    assert len(ti2.calls) == 1 and b2.sess.status()["title"] == "Google Meet"
    # …пока ключ не появился (движок: set_translate_available после ai_key_set)
    b4, ti4, now4 = _titled(box, ai.AIError("нет ключа", "no_key"), "Ретро команды")
    b4.sess.start()
    _lines(b4, 8)
    now4[0] += 91
    b4.sess._title_kick()
    wait_for(lambda: len(ti4.calls) == 1)
    b4.sess.set_translate_available(True)
    b4.sess.stop()
    _done(b4)
    assert len(ti4.calls) == 2 and b4.sess.status()["title"] == "Ретро команды"
    # мусор вместо названия — тоже прежнее
    b3, ti3, _ = _titled(box, "Встреча")
    b3.sess.start()
    _lines(b3, 3)
    b3.sess.stop()
    _done(b3)
    assert len(ti3.calls) == 1 and b3.sess.status()["title"] == "Google Meet"


def test_core_exit_makes_no_title_call(box: Any) -> None:
    b, ti, now = _titled(box)
    b.sess.start()
    _lines(b, 3)
    b.sess.abort()
    _done(b)
    assert ti.calls == []


def test_first_words_name_without_window_title(box: Any, tmp_path: Path) -> None:
    src = {"id": "system", "kind": "system", "pid": None, "exe": "",
           "name": "Весь звук компьютера", "title": "", "meeting": False, "active": False}
    b, ti, now = _titled(box, src=src, meeting_translate=False)
    b.sess.start()
    assert b.sess.status()["title"] == "Встреча"
    b.rec.results = ["Hello everyone, let's talk about the budget for next year"]
    b.say()
    b.wait_lines(1)
    want = "Встреча — Hello everyone, let's talk about the"
    wait_for(lambda: b.sess.status()["title"] == want)
    wait_for(lambda: any(d.get("title") == want for d in b.statuses()))     # событие meeting
    f = b.sess.file
    assert f is not None and f.name.endswith(f" — {want}.md")
    assert f.read_text(encoding="utf-8").startswith(f"# {want}\n")
    # перевода нет — название на языке строк
    _lines(b, 2)
    b.sess.stop()
    _done(b)
    assert ti.calls and ti.calls[0][1] == "en"
    first = ti.calls[0][0].splitlines()[0]
    assert first == "Hello everyone, let's talk about the budget for next year"


def test_status_title_idle_is_none() -> None:
    assert meeting.idle_status(config.Settings())["title"] is None
    s = config.Settings()
    assert s.meeting_ai_title is True
    config.apply_patch(s, {"meeting_ai_title": False})
    assert s.meeting_ai_title is False
    with pytest.raises(ValueError):
        config.apply_patch(s, {"meeting_ai_title": "yes"})


# ---------- движок: учёт расхода и ключ ----------

def test_engine_titles_meeting_with_usage_row(env: Env, translated: list[Any],
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    env.calls.creds["Rupor/openrouter"] = FAKE_KEY
    env.eng.update_settings({"ai_keys": [{"id": "openrouter", "provider": "openrouter"}]})
    seen: list[tuple[str, str]] = []

    def fake_title(transcript: str, lang: str, s: config.Settings, **kw: Any) -> ai.AIResult:
        seen.append((transcript, lang))
        return ai.AIResult("Утренний синк по релизу", 5.0, 0.0003, "openrouter",
                           "anthropic/claude-haiku-4.5", tokens_in=300, tokens_out=8)

    monkeypatch.setattr(ai, "meeting_title", fake_title)
    revealed: list[Path] = []
    monkeypatch.setattr(api, "_reveal_file", revealed.append)
    env.calls.sources = [dict(MEET_SRC)]
    c = env.client()
    st = c.call("meeting_start", {})
    assert st["title"] == "Google Meet"
    env.rec.bg = ["Good morning", "Let's begin", "Release first"]
    for _ in range(3):
        _say(env.calls.streams[-1], rec=env.rec)
    c.wait_event("meeting_line", lambda d: d["id"] == 3 and d["status"] == "done", fresh=False)
    c.call("meeting_stop")
    ev = c.wait_event("meeting", lambda d: d.get("title") == "Утренний синк по релизу",
                      fresh=False)
    assert ev["file"].endswith(" — Утренний синк по релизу.md")
    usage = c.wait_event("ai_usage", lambda d: d["kind"] == "title", fresh=False)
    assert usage["cost_usd"] == 0.0003
    rows = {r["kind"]: r["calls"] for r in env.eng.storage.ai_usage_stats()["by_kind"]}
    assert rows["title"] == 1 and rows["translate"] >= 3
    title_rows = [r for r in usage_rows(env) if r["kind"] == "title"]
    assert [(r["app"], r["model"], r["cost_usd"], bool(r["ok"])) for r in title_rows] == [
        ("brave.exe", "anthropic/claude-haiku-4.5", 0.0003, True)]
    assert seen[0][1] == "ru" and seen[0][0].splitlines() == [
        "RU:Good morning", "RU:Let's begin", "RU:Release first"]
    assert env.eng._ai_busy is False
    c.call("meeting_open_folder")                  # выделен уже переименованный файл
    f = env.eng.meeting_file()
    assert f is not None and revealed == [f] and f.name.endswith(" — Утренний синк по релизу.md")
    assert c.call("meeting_status")["title"] == "Утренний синк по релизу"
    c.close()


def test_engine_without_key_makes_no_title_call(env: Env, translated: list[Any],
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    called: list[str] = []
    monkeypatch.setattr(ai, "meeting_title", lambda *a, **k: called.append("x"))
    env.calls.sources = [dict(MEET_SRC)]
    c = env.client()
    c.call("meeting_start", {})
    env.rec.bg = ["One", "Two", "Three"]
    for _ in range(3):
        _say(env.calls.streams[-1], rec=env.rec)
    c.wait_event("meeting_line", lambda d: d["id"] == 3 and d["final"], fresh=False)
    c.call("meeting_stop")
    m = env.eng._meeting
    assert m is not None and m._title_thread is not None
    m._title_thread.join(5)
    assert called == [] and c.call("meeting_status")["title"] == "Google Meet"
    rows = {r["kind"]: r["calls"] for r in env.eng.storage.ai_usage_stats()["by_kind"]}
    assert rows["title"] == 0
    c.close()


def test_core_exit_stops_replaced_meeting_too(env: Env) -> None:
    """Встречу сменила новая (другой источник), а прежняя ещё дораспознаёт и позовёт
    название: выход ядра останавливает обе (abort) — прежняя дописывает готовое и не
    переименовывает файл посреди os._exit."""
    env.calls.sources = [dict(MEET_SRC), dict(MEET_SRC, id="proc:77", pid=77, exe="chrome.exe",
                                              name="Google Chrome")]
    env.eng.meeting_start("proc:4100")
    old = env.eng._meeting
    env.eng.meeting_start("proc:77")
    new = env.eng._meeting
    assert old is not None and new is not None and new is not old
    assert env.eng._meeting_prev == [old]
    env.eng._meeting_shutdown()
    assert old._shutdown and new._shutdown and old._finalized
    assert old._writer is not None and old._writer._frozen
    assert env.eng._meeting_prev == [] and new.running is False
