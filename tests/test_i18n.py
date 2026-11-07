"""Язык сообщений ядра (rupor.i18n, core/rupor/locales/{ru,en}.json).

- каталоги: одинаковые ключи, одинаковые подстановки, шаблоны форматируются, в английском
  нет кириллицы, все ключи, которые код передаёт в tr(), есть в обоих каталогах;
- в модулях ядра нет русских строк мимо каталога (кроме журналов и лингвистических данных);
- «Системный» язык: ru для русского, белорусского, украинского и казахского Windows, иначе en;
- настройки language / dictation_language: значения по умолчанию и проверка;
- смена языка на лету: ошибки WS-команд, notice, стили и State.ui_language — сразу на новом
  языке, без перезапуска ядра (настоящий WS-сервер, фикстура env из test_api)."""
from __future__ import annotations

import ast
import json
import re
import string
import time
from dataclasses import fields
from pathlib import Path
from typing import Any

import pytest

from rupor import ai, config, engine, i18n, learn
from rupor.asr import Result
from rupor.hotkeys import Action, Kind
from test_api import Env, _enable_ai, env  # noqa: F401 — фикстура env

ROOT = Path(__file__).resolve().parent.parent
CORE = ROOT / "core" / "rupor"
CYR = re.compile(r"[А-Яа-яЁё]")


def _catalog(lang: str) -> dict[str, str]:
    raw = json.loads((CORE / "locales" / f"{lang}.json").read_text(encoding="utf-8"))
    assert isinstance(raw, dict)
    return {k: v for k, v in raw.items() if not k.startswith("_")}


def _fields(template: str) -> set[str]:
    return {f for _, f, _, _ in string.Formatter().parse(template) if f}


# ---------- каталоги ----------

def _plain(cat: dict[str, str]) -> set[str]:
    return {k for k in cat if i18n.plural_base(k) is None}


def _plural(cat: dict[str, str]) -> dict[str, dict[str, str]]:
    out: dict[str, dict[str, str]] = {}
    for k, v in cat.items():
        base = i18n.plural_base(k)
        if base is not None:
            out.setdefault(base, {})[k.rsplit(".", 1)[1]] = v
    return out


def test_catalogs_have_same_keys() -> None:
    ru, en = _catalog("ru"), _catalog("en")
    assert ru and en
    assert sorted(_plain(ru) - _plain(en)) == [], "нет перевода на английский"
    assert sorted(_plain(en) - _plain(ru)) == [], "нет русского текста"
    assert sorted(_plural(ru)) == sorted(_plural(en)), "множественное число только в одном языке"
    assert i18n.catalog_problems({"ru": ru, "en": en}) == []
    for lang, cat in (("ru", ru), ("en", en)):
        empty = [k for k, v in cat.items() if not isinstance(v, str) or not v.strip()]
        assert empty == [], f"{lang}: пустые сообщения"


def test_plural_forms_match_language() -> None:
    """Формы множественного числа — все категории языка и только они (как check:i18n)."""
    ru, en = _plural(_catalog("ru")), _plural(_catalog("en"))
    assert ru, "в каталоге нет сообщений с числом"
    for key in ru:
        assert set(ru[key]) == {"one", "few", "many", "other"}, key
        assert set(en[key]) == {"one", "other"}, key
    broken = i18n.catalog_problems({"ru": {"x.one": "a", "x.few": "b", "x.many": "c"},
                                    "en": {"x.one": "a", "x.other": "b", "x.few": "c"}})
    assert len(broken) == 2                                 # у ru нет other, у en лишняя few


def test_placeholders_match_and_templates_format() -> None:
    ru, en = _catalog("ru"), _catalog("en")
    for key in _plain(ru):
        assert _fields(ru[key]) == _fields(en[key]), key
        for template in (ru[key], en[key]):
            template.format(**{f: "X" for f in _fields(template)})   # скобки текста удвоены
    ru_p, en_p = _plural(ru), _plural(en)
    for key, forms in ru_p.items():
        ref = _fields(forms["other"])
        for template in [*forms.values(), *en_p[key].values()]:
            assert _fields(template) == ref, key
            template.format(**{f: "X" for f in ref})


def test_english_catalog_has_no_cyrillic() -> None:
    bad = {k: v for k, v in _catalog("en").items() if CYR.search(v)}
    assert bad == {}


def test_app_name() -> None:
    assert i18n.tr_in("ru", "app.name") == "Рупор"
    assert i18n.tr_in("en", "app.name") == "Rupor"


def _literal_keys(tree: ast.AST, plural: bool = False) -> set[str]:
    """Ключи из вызовов tr("…"), tr_or("…"), i18n.tr/tr_or/has/Msg("…"), i18n.tr_in(lang, "…");
    plural — ключи сообщений с числом: tp("…", n), tp_in(lang, "…", n)."""
    keys: set[str] = set()
    one, two = (("tp",), ("tp_in",)) if plural else (("tr", "tr_or", "has", "Msg"), ("tr_in",))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        f = node.func
        name = f.id if isinstance(f, ast.Name) else f.attr if isinstance(f, ast.Attribute) else ""
        if name in one:
            arg = node.args[0]
        elif name in two and len(node.args) > 1:
            arg = node.args[1]
        else:
            continue
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str) and "." in arg.value:
            keys.add(arg.value)
    return keys


def test_every_key_used_by_core_exists() -> None:
    from rupor import hotkeys, tray
    used: set[str] = set()
    used_plural: set[str] = set()
    for path in sorted(CORE.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        used |= _literal_keys(tree)
        used_plural |= _literal_keys(tree, plural=True)
    assert {"eng.rdp_sent", "eng.rdp_sent_partial", "eng.rdp_failed",
            "tray.rdp_hint"} <= used_plural
    for lang in i18n.UI_LANGS:
        forms = _plural(_catalog(lang))
        missing = sorted(k for k in used_plural if "other" not in forms.get(k, {}))
        assert missing == [], f"{lang}: нет форм множественного числа"
    # ключи, которые код собирает из частей
    used |= {f"setting.{f.name}" for f in fields(config.Settings)}
    used |= {f"tray.status_{st}" for st in tray._STATES}
    used |= {f"tray.mode_{m}" for m in tray._MODES}
    used |= {f"cred.action_{a}" for a in ("read", "write", "delete")}
    used |= {f"ai.style.{sid}.{part}" for sid in ai.STYLE_IDS for part in ("name", "description")}
    used |= {f"key.{k}" for k in hotkeys._LOCALIZED_KEYS}
    assert len(used) > 200
    for lang in i18n.UI_LANGS:
        missing = sorted(used - set(_catalog(lang)))
        assert missing == [], f"{lang}: нет ключей"


# Русские строки в коде допустимы только в журналах (log.*), докстрингах и здесь:
_ALLOWED: dict[str, set[str]] = {
    # причины конца наблюдения за полем (обучение словаря) — только для журнала
    "engine.py": {"выход", "пауза", "новая диктовка", "смена раскладки", "новая вставка",
                  "время вышло", "фокус ушёл с поля", "поле недоступно", "поле очищено"},
    "config.py": {"ё", "е"},                 # сравнение названий окон без «ё»
    "i18n.py": {"[А-Яа-яЁё]", "локализация: ", "ok", "нет"},
    "__main__.py": {"Самопроверка UIA: %s"},
}
_CHECKED = ("__main__.py", "api.py", "config.py", "credman.py", "engine.py", "fetch.py",
            "hotkeys.py", "i18n.py", "inject.py", "server.py", "storage.py", "tray.py",
            "winutil.py", "broadcast.py", "audio.py", "sounds.py")


def _is_log_call(node: ast.Call) -> bool:
    f = node.func
    if isinstance(f, ast.IfExp):          # (log.info if ok else log.error)(…)
        return all(isinstance(x, ast.Attribute) and isinstance(x.value, ast.Name)
                   and x.value.id == "log" for x in (f.body, f.orelse))
    return isinstance(f, ast.Attribute) and isinstance(f.value, ast.Name) and f.value.id == "log"


@pytest.mark.parametrize("name", _CHECKED)
def test_no_russian_strings_outside_catalog(name: str) -> None:
    tree = ast.parse((CORE / name).read_text(encoding="utf-8"))
    skip: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_log_call(node):
            skip |= {id(n) for n in ast.walk(node)}
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                skip.add(id(body[0].value))
    bad = [f"{node.lineno}: {node.value[:60]}" for node in ast.walk(tree)
           if isinstance(node, ast.Constant) and isinstance(node.value, str)
           and id(node) not in skip and CYR.search(node.value)
           and node.value not in _ALLOWED.get(name, set())]
    assert bad == [], "русский текст мимо i18n.tr"


# ---------- язык интерфейса ----------

@pytest.mark.parametrize("langid,lang", [
    (0x0419, "ru"), (0x0819, "ru"),      # русский (Россия, Молдова)
    (0x0423, "ru"), (0x0422, "ru"), (0x043F, "ru"),   # белорусский, украинский, казахский
    (0x0409, "en"), (0x0809, "en"), (0x0407, "en"), (0x040C, "en"), (0x0404, "en"),
])
def test_system_language_from_langid(langid: int, lang: str) -> None:
    assert i18n.lang_for_langid(langid) == lang


def test_set_language_resolves_and_notifies(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    i18n.add_listener(seen.append)
    try:
        assert i18n.current() == "ru"
        assert i18n.set_language("en") is True
        assert i18n.current() == "en" and i18n.setting() == "en"
        assert i18n.tr("app.name") == "Rupor"
        assert i18n.set_language("en") is False              # не сменился — без события
        monkeypatch.setattr(i18n, "system_language", lambda: i18n.EN)
        i18n.set_language("system")
        assert i18n.current() == "en" and i18n.setting() == "system"
        i18n.set_language("ru")
        assert i18n.tr("app.name") == "Рупор"
        i18n.set_language("klingon")                          # неизвестное — «Системный»
        assert i18n.setting() == "system" and i18n.current() == "en"
    finally:
        i18n.remove_listener(seen.append)
    assert seen == ["en", "ru", "en"]


def test_tr_missing_param_or_key_does_not_raise() -> None:
    assert i18n.tr("eng.mic_unavailable") == "Микрофон недоступен: {error}"
    assert i18n.tr("eng.mic_unavailable", error="нет устройства") == \
        "Микрофон недоступен: нет устройства"
    assert i18n.tr("no.such.key") == "no.such.key"
    assert i18n.tr("server.bad_message").endswith("{id, cmd, args}")   # {{…}} → {…}
    assert i18n.tr_or("no.such.key", "запасной") == "запасной"


def test_fmt_num() -> None:
    assert i18n.fmt_num(1234.5, 1, "ru") == "1 234,5"
    assert i18n.fmt_num(1234.5, 1, "en") == "1,234.5"
    assert i18n.fmt_num(0.55 * 1, 2, "ru") == "0,55"


def test_text_language() -> None:
    assert i18n.text_language("Привет, как дела") == "ru"
    assert i18n.text_language("Hello there") == "en"
    assert i18n.text_language("") == "en"


def test_selftest() -> None:
    ok, summary = i18n.selftest()
    assert ok, summary
    assert summary.startswith("локализация: ok (ru ") and ", en " in summary


def test_learn_notice_texts_follow_language() -> None:
    assert learn.added_text("Metabase", "метабейс") == "В словарь добавлено: «метабейс» → Metabase"
    i18n.set_language("en")
    assert learn.added_text("Metabase", "метабейс") == "Added to dictionary: “метабейс” → Metabase"
    assert learn.added_text("Metabase", "") == "Added to dictionary: Metabase"
    assert learn.suggest_text("Metabase", "") == "Add Metabase to the dictionary?"


# ---------- настройки language / dictation_language ----------

def test_language_settings_defaults_and_validation() -> None:
    s = config.Settings()
    assert s.language == "system" and s.dictation_language == "auto"
    assert config.apply_patch(s, {"language": "en", "dictation_language": "ru"}) == [
        "language", "dictation_language"]
    assert (s.language, s.dictation_language) == ("en", "ru")
    with pytest.raises(ValueError, match="Язык интерфейса: допустимо: system, ru, en"):
        config.apply_patch(s, {"language": "de"})
    with pytest.raises(ValueError, match="Язык диктовки: допустимо: auto, ru, en"):
        config.apply_patch(s, {"dictation_language": "de"})
    i18n.set_language("en")
    with pytest.raises(ValueError, match="Interface language: must be one of system, ru, en"):
        config.apply_patch(s, {"language": 5})
    with pytest.raises(ValueError, match="Unknown setting “languages”"):
        config.apply_patch(s, {"languages": "en"})
    assert (s.language, s.dictation_language) == ("en", "ru")


def test_language_settings_load_and_fallback(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = tmp_path / "Rupor" / "settings.json"
    p.parent.mkdir()
    p.write_text(json.dumps({"language": "en", "dictation_language": "en"}), encoding="utf-8")
    s = config.load()
    assert (s.language, s.dictation_language) == ("en", "en")
    p.write_text(json.dumps({"language": "fr", "dictation_language": 1}), encoding="utf-8")
    s = config.load()
    assert (s.language, s.dictation_language) == ("system", "auto")   # битое — по умолчанию
    saved = json.loads(p.read_text(encoding="utf-8"))
    assert saved["language"] == "system" and saved["dictation_language"] == "auto"


def test_engine_applies_language_from_settings(tmp_path: Path) -> None:
    from rupor.storage import Storage
    from test_api import FakeRecognizer, FakeRecorder
    st = Storage(tmp_path / "rupor.db")
    try:
        eng = engine.Engine(config.Settings(language="en", sounds=False), st,
                            recognizer=FakeRecognizer(),
                            recorder_factory=lambda dev, cb: FakeRecorder())
        assert i18n.current() == "en"
        assert eng.get_state()["ui_language"] == "en"
        assert [x["name"] for x in engine.styles_localized()][:2] == ["As is", "Standard"]
    finally:
        st.close()


# ---------- смена языка на лету через WebSocket ----------

def test_ws_switch_language_on_the_fly(env: Env) -> None:
    c = env.client()
    hello = c.events("hello")[0]
    assert hello["ui_language"] == "ru"
    assert c.error("no_such_cmd") == "Неизвестная команда «no_such_cmd»"
    assert c.error("set_settings", {"patch": {"tap_max_ms": 5}}).startswith("Порог тапа:")
    assert c.call("styles_list")[1]["name"] == "Обычный"

    c.mark()
    d = c.call("set_settings", {"patch": {"language": "en"}})
    assert d["language"] == "en"
    assert c.wait_event("state", lambda x: x["ui_language"] == "en")["ui_language"] == "en"
    assert c.wait_event("settings", fresh=False)["language"] == "en"
    assert c.call("get_state")["ui_language"] == "en"
    # ошибки команд — сразу по-английски
    assert c.error("no_such_cmd") == "Unknown command “no_such_cmd”"
    assert c.error("history_list", {"limit": "x"}) == "Argument “limit” must be an integer"
    assert c.error("set_settings", {"patch": {"tap_max_ms": 5}}) == \
        "Tap threshold: must be between 100 and 1000"
    assert c.error("dict_upsert", {"term": " ", "variants": []}) == "The term can't be empty"
    styles = {x["id"]: x for x in c.call("styles_list")}
    assert styles["normal"]["name"] == "Standard" and styles["messenger"]["name"] == "Chat message"
    assert not any(CYR.search(x["name"] + x["description"]) for x in styles.values())
    # уведомления — тоже
    c.mark()
    env.eng._on_actions([Action(Kind.START), Action(Kind.COMMAND)])
    c.wait_event("recording")
    env.eng._on_actions([Action(Kind.CANCEL)])
    assert c.wait_event("notice")["text"] == "Recording canceled"
    # и обратно
    c.mark()
    c.call("set_settings", {"patch": {"language": "ru"}})
    c.wait_event("state", lambda x: x["ui_language"] == "ru")
    assert c.error("no_such_cmd") == "Неизвестная команда «no_such_cmd»"
    c.mark()
    env.eng._on_actions([Action(Kind.START)])
    c.wait_event("recording")
    env.eng._on_actions([Action(Kind.CANCEL)])
    assert c.wait_event("notice")["text"] == "Запись отменена"
    c.close()


def test_ws_system_language_follows_windows(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    c = env.client()
    monkeypatch.setattr(i18n, "system_language", lambda: i18n.EN)
    c.call("set_settings", {"patch": {"language": "ru"}})
    assert c.call("get_state")["ui_language"] == "ru"
    c.mark()
    c.call("set_settings", {"patch": {"language": "system"}})
    c.wait_event("state", lambda x: x["ui_language"] == "en")
    assert c.error("draft_set", {"text": 5}) == "Argument “text” must be a string"
    c.close()


def test_dictation_notices_in_english(env: Env) -> None:
    c = env.client()
    c.call("set_settings", {"patch": {"language": "en"}})
    c.mark()
    env.dictate("")                                    # ничего не распознано
    assert c.wait_event("notice")["text"] == "No speech detected"
    env.calls.fg = None                                # окна нет — нечего и некуда вставлять
    assert c.error("last_insert") == "Nothing to paste yet. Dictate something first"
    c.close()


def test_english_dictation_uses_english_cleanup(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    """Язык распознавания уходит в ai.cleanup (английский промпт); ai_test — по буквам."""
    got: list[tuple[str, str | None]] = []

    def cleanup(text: str, style: str, s: config.Settings, terms: list[str], *,
                api_key: str | None = None, language: str | None = None) -> ai.AIResult:
        got.append((text, language))
        return ai.AIResult(text, 5.0, 0.0, "openrouter", "test")

    monkeypatch.setattr(ai, "cleanup", cleanup)
    real = env.rec.transcribe

    def transcribe_en(audio: Any, language: str | None = None) -> Result:
        r = real(audio, language)
        return Result(r.text, "en", r.audio_s, r.elapsed_ms)

    monkeypatch.setattr(env.rec, "transcribe", transcribe_en)
    c = env.client()
    _enable_ai(c)
    c.mark()
    env.dictate("so um basically we should ship it today")
    c.wait_event("history")
    assert got == [("so um basically we should ship it today", "en")]
    c.call("ai_test", {"text": "Ну короче созвонимся завтра", "style": "normal"})
    c.call("ai_test", {"text": "um so call me tomorrow", "style": "normal"})
    assert [lang for _, lang in got[1:]] == ["ru", "en"]
    c.close()


def test_history_titles_follow_language(env: Env) -> None:
    """Проверочная запись мастера и рассылка без адресатов хранятся с нейтральными
    app/title, а подписи — на языке, который действует при показе (и у записей, которые
    прежние версии сохранили по-русски готовым текстом)."""
    c = env.client()
    c.call("set_settings", {"patch": {"language": "en"}})
    env.rec.texts.append("hello there")
    c.call("test_record_start")
    time.sleep(0.05)
    c.call("test_record_stop")
    item = c.wait_event("history", fresh=False)
    assert (item["app"], item["title"]) == ("Rupor", "Recognition test")
    raw = env.eng.storage.get_history(item["id"])
    assert raw is not None and (raw["app"], raw["title"]) == (engine.HISTORY_APP_SELF, "")
    old_test = env.eng.storage.add_history({"app": "Рупор", "title": "Проверка распознавания",
                                            "text": "старая проверка", "target": "test"})
    old_rdp = env.eng.storage.add_history({"app": "RDP", "title": "Рассылка в RDP",
                                           "text": "старая рассылка", "target": "rdp_all"})
    sent = env.eng.storage.add_history({"app": "RDP", "title": "srv1, srv2",
                                        "text": "рассылка", "target": "rdp_all"})
    window = env.eng.storage.add_history({"app": "Rupor.exe", "title": "Проверка распознавания",
                                          "text": "обычное окно", "target": "window"})

    def shown() -> dict[int, tuple[str, str]]:
        items = c.call("history_list", {"limit": 10})["items"]
        return {it["id"]: (it["app"], it["title"]) for it in items}

    got = shown()
    assert got[item["id"]] == got[old_test["id"]] == ("Rupor", "Recognition test")
    assert got[old_rdp["id"]] == ("RDP", "RDP broadcast")
    assert got[sent["id"]] == ("RDP", "srv1, srv2")                        # адресаты — как есть
    assert got[window["id"]] == ("Rupor.exe", "Проверка распознавания")    # чужое окно не трогаем
    c.call("set_settings", {"patch": {"language": "ru"}})
    got = shown()
    assert got[item["id"]] == got[old_test["id"]] == ("Рупор", "Проверка распознавания")
    assert got[old_rdp["id"]] == ("RDP", "Рассылка в RDP")
    assert c.call("history_list", {"query": "hello"})["items"][0]["app"] == "Рупор"
    # поиск находит служебные записи по подписям интерфейса на любом языке — и новые
    # (в базе «rupor» / ""), и сохранённые прежними версиями по-русски
    for q in ("Rupor", "рупор", "Проверка распознавания", "recognition test"):
        ids = {it["id"] for it in c.call("history_list", {"query": q, "limit": 20})["items"]}
        assert {item["id"], old_test["id"]} <= ids, q
    for q in ("RDP broadcast", "рассылка"):
        ids = {it["id"] for it in c.call("history_list", {"query": q, "limit": 20})["items"]}
        assert {old_rdp["id"], sent["id"]} <= ids and item["id"] not in ids, q
    assert c.call("history_list", {"query": "Rupor O'Brien"})["total"] == 0   # кавычка в SQL
    c.close()


def test_tray_status_labels_follow_language() -> None:
    from rupor import tray
    assert tray._status("recording") == "Идёт запись"
    i18n.set_language("en")
    assert tray._status("recording") == "Recording"
    assert tray._label("tray.quit")(None) == "Quit"
    assert tray._style_name("messenger")(None) == "Chat message"
    assert tray.Tray._title("paused") == "Rupor — Paused"


# ---------- ревью английской версии: подписи, числа, склейки фраз ----------

# Название настройки в ошибках ядра — как в интерфейсе (ui/src/i18n/*.json)
UI_SETTING_NAMES = {
    "ptt_key": "settings.dictation.ptt",
    "broadcast_modifier": "settings.dictation.broadcast",
    "command_modifier": "settings.dictation.command",
    "cancel_key": "settings.dictation.cancel",
    "double_tap_ms": "settings.dictation.doubleTap",
    "duck_level": "settings.dictation.duck.level",
    "learn_mode": "settings.dictation.learn",
    "model_dir": "settings.dictation.modelDir",
    "insert_mode": "settings.insert.mode",
    "add_trailing_space": "settings.insert.trailingSpace",
    "broadcast_targets": "settings.insert.sessions",
    "autostart": "settings.general.autostart",
    "pill_position": "settings.general.pillPosition",
    "language": "settings.general.uiLanguage",
    "dictation_language": "settings.general.dictationLanguage",
}


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_setting_names_match_ui(lang: str) -> None:
    path = ROOT / "ui" / "src" / "i18n" / f"{lang}.json"
    if not path.exists():
        pytest.skip("нет каталога интерфейса")
    ui = json.loads(path.read_text(encoding="utf-8"))
    for setting, dotted in UI_SETTING_NAMES.items():
        node: Any = ui
        for part in dotted.split("."):
            node = node[part]
        assert i18n.tr_in(lang, f"setting.{setting}") == node, setting


def test_hotkey_conflict_uses_ui_names_and_key_labels() -> None:
    i18n.set_language("en")
    s = config.Settings()
    with pytest.raises(ValueError) as e:
        config.apply_patch(s, {"ptt_key": "ctrl+win", "broadcast_modifier": "ctrl"})
    assert str(e.value) == \
        "“Broadcast to RDP sessions” uses the same key as “Hold to talk” (Ctrl)"
    with pytest.raises(ValueError) as e:
        config.apply_patch(s, {"ptt_key": "rctrl", "cancel_key": "rctrl"})
    assert str(e.value) == "“Cancel recording” uses the same key as “Hold to talk” (Right Ctrl)"
    with pytest.raises(ValueError, match="^Insert method: "):
        config.apply_patch(s, {"insert_mode": "fast"})
    i18n.set_language("ru")
    with pytest.raises(ValueError) as e:
        config.apply_patch(s, {"ptt_key": "ctrl+win", "broadcast_modifier": "ctrl"})
    assert str(e.value) == "«Рассылка в RDP-сессии» совпадает с «Удерживай и говори» (Ctrl)"
    assert s.broadcast_modifier == "shift"                    # патч с ошибкой не применён


def test_key_label() -> None:
    from rupor.hotkeys import key_label
    i18n.set_language("en")
    assert key_label("ctrl+win") == "Ctrl + Win"
    assert key_label("rctrl") == "Right Ctrl"
    assert key_label("ctrl+alt+shift+v") == "Ctrl + Alt + Shift + V"
    assert key_label("F13") == "F13" and key_label("pagedown") == "Page Down"
    assert key_label("vk:0x41") == "Key 0x41"
    i18n.set_language("ru")
    assert key_label("lshift+space") == "Левый Shift + Пробел"


def test_plural_rules() -> None:
    ru = [0, 1, 2, 4, 5, 11, 12, 14, 21, 22, 25, 111, 1.5]
    assert [i18n.plural_form(n, "ru") for n in ru] == [
        "many", "one", "few", "few", "many", "many", "many", "many", "one", "few", "many",
        "many", "other"]
    assert [i18n.plural_form(n, "en") for n in (0, 1, 2, 1.5)] == ["other", "one", "other",
                                                                   "other"]
    assert i18n.tp_in("en", "eng.rdp_sent", 1) == "Sent to 1 RDP session"
    assert i18n.tp_in("en", "eng.rdp_sent", 1200) == "Sent to 1,200 RDP sessions"
    assert i18n.tp_in("ru", "eng.rdp_sent", 1) == "Отправлено в 1 RDP-сессию"
    assert i18n.tp_in("ru", "eng.rdp_sent", 3) == "Отправлено в 3 RDP-сессии"
    assert i18n.tp_in("ru", "eng.rdp_sent", 5) == "Отправлено в 5 RDP-сессий"
    assert i18n.tp_in("ru", "eng.rdp_sent", 21) == "Отправлено в 21 RDP-сессию"
    assert i18n.tp_in("en", "no.such.key", 2) == "no.such.key"


@pytest.mark.parametrize("sent, failed, lang, text, level", [
    (["a"], [], "en", "Sent to 1 RDP session", "info"),
    (["a", "b"], [], "en", "Sent to 2 RDP sessions", "info"),
    (["a"], ["b"], "en", "Sent to 1 of 2 RDP sessions; 1 failed", "warn"),
    ([], ["b"], "en", "Couldn't send the text to 1 RDP session", "warn"),
    ([], ["b", "c"], "en", "Couldn't send the text to 2 RDP sessions", "warn"),
    (["a", "b"], [], "ru", "Отправлено в 2 RDP-сессии", "info"),
    (["a"], ["b", "c", "d", "e"], "ru", "Отправлено в 1 из 5 RDP-сессий, ошибок: 4", "warn"),
])
def test_broadcast_notices_use_plural(monkeypatch: pytest.MonkeyPatch, sent: list[str],
                                      failed: list[str], lang: str, text: str,
                                      level: str) -> None:
    from rupor import broadcast
    monkeypatch.setattr(broadcast, "send_all",
                        lambda t, s: broadcast.BroadcastReport(list(sent), list(failed)))
    i18n.set_language(lang)
    got: list[tuple[str, dict[str, Any]]] = []
    engine.deliver("hi", config.Settings(), True, lambda t, **kw: got.append((t, kw)))
    assert [t for t, _ in got] == [text]
    assert got[0][1].get("level", "info") == level


def test_tray_rdp_hint_plural_and_key_names(monkeypatch: pytest.MonkeyPatch) -> None:
    from types import SimpleNamespace

    from rupor import broadcast, tray, winutil
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda classes, procs: [object()])
    monkeypatch.setattr(broadcast, "session_key", lambda wi: "srv1")
    monkeypatch.setattr(broadcast, "targets", lambda s: ["srv1", "srv2"])
    fake = SimpleNamespace(s=config.Settings(ptt_key="ctrl+win", broadcast_modifier="shift"),
                           _toggle_rdp=lambda key: None)
    i18n.set_language("en")
    assert tray.Tray._rdp_items(fake)[-1].text == \
        "Hold Ctrl + Win + Shift to send text to 2 sessions"
    i18n.set_language("ru")
    assert tray.Tray._rdp_items(fake)[-1].text == \
        "Удерживайте Ctrl + Win + Shift — текст уйдёт в 2 сессии"


def _bare_engine(tmp_path: Path) -> tuple[engine.Engine, Any]:
    from rupor.storage import Storage
    from test_api import FakeRecognizer, FakeRecorder
    st = Storage(tmp_path / "rupor.db")
    eng = engine.Engine(config.Settings(sounds=False), st, recognizer=FakeRecognizer(),
                        recorder_factory=lambda dev, cb: FakeRecorder())
    return eng, st


@pytest.mark.parametrize("error, inserted_raw, lang, text", [
    (ai.AIError("Your OpenRouter balance has run out (402). Top it up", "payment", 402), False,
     "en", "Your OpenRouter balance has run out (402). Top it up. "
           "The original text was inserted instead"),
    (ai.AIError("Can't reach OpenRouter. Check your internet connection (timed out)",
                "network"), True,
     "en", "Can't reach OpenRouter. Check your internet connection (timed out). "
           "The original text was kept"),
    (ai.AIError("OpenRouter: Provider returned error.", "server"), False,
     "en", "OpenRouter: Provider returned error. The original text was inserted instead"),
    (ai.AIError("slow", "timeout"), False,
     "en", "The AI didn't respond. The original text was inserted instead"),
    (ai.AIError("На счёте OpenRouter закончились деньги (402) — пополните баланс", "payment"),
     False, "ru", "На счёте OpenRouter закончились деньги (402) — пополните баланс — "
                  "вставлен текст без обработки"),
])
def test_ai_failure_notice_reads_naturally(tmp_path: Path, error: ai.AIError,
                                           inserted_raw: bool, lang: str, text: str) -> None:
    eng, st = _bare_engine(tmp_path)
    try:
        i18n.set_language(lang)
        seen: list[str] = []
        eng.bus.subscribe(lambda ev, d: seen.append(d["text"]) if ev == "notice" else None)
        eng._ai_failed(error, inserted_raw=inserted_raw)
        assert seen == [text]
    finally:
        st.close()


def test_command_not_inserted_reads_naturally() -> None:
    reason = i18n.tr_in("en", "eng.cmd_block_window")
    assert i18n.tr_in("en", "eng.cmd_not_inserted", reason=reason) == \
        "The window changed. The result wasn't inserted — use “Paste last” to insert it"


def test_english_messages_call_the_process_engine() -> None:
    """В интерфейсе процесс Python — «engine» (Engine offline, Starting the engine…)."""
    en = _catalog("en")
    bad = {k: v for k, v in en.items()            # «Core Audio» — название API Windows
           if re.search(r"\bcore\b(?! Audio)", v, re.IGNORECASE)}
    assert bad == {}
    assert "engine" in en["api.internal_error"] and "engine" in en["api.relaunch_by_ui"]


def test_en_threshold_label_is_about_second_language() -> None:
    assert i18n.tr_in("ru", "setting.en_threshold") == "Порог второго языка"


def test_cuda_check_error_follows_language(tmp_path: Path,
                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """Сохранённая причина работы на CPU (Recognizer.last_error) — на языке момента показа."""
    from rupor import fetch
    eng, st = _bare_engine(tmp_path)
    try:
        monkeypatch.setattr(fetch, "missing_cuda_dlls", lambda dirs=None: [])
        monkeypatch.setattr(engine, "cuda_device_count", lambda: 1)
        monkeypatch.setattr(engine, "gpu_info", lambda: ("GPU", 8192))
        eng.s.device = "cuda"
        eng.rec.device = "cpu"
        eng.rec.last_error = i18n.Msg("eng.cuda_missing", names="cublas64_12.dll")
        assert eng.cuda_check()["error"] == "Не найдены библиотеки CUDA: cublas64_12.dll"
        i18n.set_language("en")
        assert eng.cuda_check()["error"] == "CUDA libraries not found: cublas64_12.dll"
        eng.rec.last_error = "RuntimeError: out of memory"            # текст исключения
        assert eng.cuda_check()["error"] == "RuntimeError: out of memory"
        eng.rec.last_error = None
        assert eng.cuda_check()["error"] == "The model is running on the CPU"
    finally:
        st.close()


def test_recognizer_cpu_fallback_error_is_translated_on_display(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from rupor import asr, fetch
    monkeypatch.setattr(fetch, "cuda_ready", lambda dirs=None: False)
    monkeypatch.setattr(fetch, "missing_cuda_dlls", lambda dirs=None: ["cublas64_12.dll"])
    rec = asr.Recognizer(config.Settings(device="cuda"))
    monkeypatch.setattr(rec, "_create", lambda device, ctype: object())
    monkeypatch.setattr(rec, "_warmup", lambda: None)
    rec.load()
    assert rec.device == "cpu" and isinstance(rec.last_error, i18n.Msg)
    assert str(rec.last_error) == "Не найдены библиотеки CUDA: cublas64_12.dll"
    i18n.set_language("en")
    assert str(rec.last_error) == "CUDA libraries not found: cublas64_12.dll"
