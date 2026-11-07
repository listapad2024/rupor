"""Список ключей ИИ по приоритету: переход со старых настроек, очередь ключей с переходом
на следующий при ошибке, WS-команды списка и баланс по ключу.

Сеть не нужна: ai._urlopen подменяется, Credential Manager — словарь (фикстура env из
test_api и monkeypatch здесь)."""
from __future__ import annotations

import io
import json
import time
import urllib.error
import urllib.request
from typing import Any, Callable

import pytest

from rupor import ai, config, i18n, keys
from rupor.config import Settings
from test_api import FAKE_KEY, Env, env  # noqa: F401 — фикстура env

OR_KEY = "sk-or-v1-" + "a" * 60
ANT1 = "sk-ant-api03-" + "b" * 40
ANT2 = "sk-ant-api03-" + "c" * 40


@pytest.fixture(autouse=True)
def ui_ru() -> Any:
    prev = i18n.setting()
    i18n.set_language("ru")
    yield
    i18n.set_language(prev)


def entry(kid: str, provider: str, **kw: Any) -> dict[str, Any]:
    return {"id": kid, "provider": provider, "label": "", "enabled": True,
            "balance_usd": None, "balance_at": None, **kw}


def settings(entries: list[dict[str, Any]], **kw: Any) -> Settings:
    s = Settings(ai_keys=entries, **kw)
    return s


class Net:
    """Подмена ai._urlopen: ответ — по ключу из заголовка авторизации."""

    def __init__(self) -> None:
        self.by_key: dict[str, Callable[[], Any]] = {}
        self.calls: list[str] = []

    def __call__(self, req: urllib.request.Request, timeout: float, local: bool = False) -> Any:
        h = {k.lower(): v for k, v in req.header_items()}
        key = h.get("x-api-key") or h.get("authorization", "").removeprefix("Bearer ")
        self.calls.append(key)
        r = self.by_key[key]()
        if isinstance(r, BaseException):
            raise r
        return _Resp(r)


class _Resp:
    def __init__(self, data: Any) -> None:
        self._b = json.dumps(data).encode("utf-8")

    def read(self, n: int = -1) -> bytes:
        return self._b

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *a: object) -> None:
        return None


def http(code: int) -> urllib.error.HTTPError:
    return urllib.error.HTTPError("https://x", code, "x", {},  # type: ignore[arg-type]
                                  io.BytesIO(b'{"error":{"message":"nope"}}'))


def ant_ok(text: str = "Готово, всё обновили сегодня.") -> dict[str, Any]:
    return {"content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
            "usage": {"input_tokens": 100, "output_tokens": 20}}


def or_ok(text: str = "Готово, всё обновили сегодня.") -> dict[str, Any]:
    return {"model": "m", "choices": [{"message": {"content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "cost": 0.001}}


RAW = "ну короче готово всё обновили сегодня вот"
CREDS = {"Rupor/openrouter": OR_KEY, "Rupor/anthropic": ANT1,
         "Rupor/anthropic/anthropic-2": ANT2}


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Net:
    n = Net()
    monkeypatch.setattr(ai, "_urlopen", n)
    monkeypatch.setattr(ai.credman, "read", lambda t: CREDS.get(t))
    return n


# ---------- где лежит секрет ----------

def test_target_keeps_old_names_for_first_keys() -> None:
    assert keys.target(entry("openrouter", "openrouter")) == "Rupor/openrouter"
    assert keys.target(entry("anthropic-2", "anthropic")) == "Rupor/anthropic/anthropic-2"
    ids = [entry("anthropic", "anthropic")]
    assert keys.new_id("openrouter", ids) == "openrouter"
    kid = keys.new_id("anthropic", ids)
    assert kid.startswith("anthropic-") and kid != "anthropic"


def test_labels_number_keys_of_one_provider() -> None:
    es = [entry("anthropic", "anthropic"), entry("openrouter", "openrouter"),
          entry("anthropic-2", "anthropic"), entry("x", "anthropic", label="Рабочий")]
    assert [keys.label_of(e, es) for e in es] == ["Anthropic #1", "OpenRouter",
                                                  "Anthropic #2", "Рабочий"]


# ---------- очередь ключей ----------

def test_chain_order_filter_and_disabled() -> None:
    es = [entry("anthropic", "anthropic"), entry("openrouter", "openrouter"),
          entry("anthropic-2", "anthropic", enabled=False)]
    s = settings(es)
    assert [e["id"] for e in keys.chain(s, "keys")] == ["anthropic", "openrouter"]
    assert [e["id"] for e in keys.chain(s, "openrouter")] == ["openrouter"]
    assert keys.chain(s, "ollama") == [] and keys.chain(s, "claude_cli") == []


def test_falls_back_to_next_key_on_payment_and_rests_the_failed_one(net: Net) -> None:
    net.by_key = {ANT1: lambda: http(402), ANT2: lambda: ant_ok()}
    s = settings([entry("anthropic", "anthropic"), entry("anthropic-2", "anthropic")],
                 ai_provider="keys")
    res = ai.cleanup(RAW, "normal", s, [])
    assert res.key_id == "anthropic-2" and net.calls == [ANT1, ANT2]
    # следующая диктовка начинает со здорового ключа — упавший в конце очереди
    assert [e["id"] for e in keys.chain(s, "keys")] == ["anthropic-2", "anthropic"]
    assert keys.cooldown("anthropic") is not None
    assert keys.cooldown("anthropic").kind == "payment"  # type: ignore[union-attr]
    net.calls.clear()
    ai.cleanup(RAW, "normal", s, [])
    assert net.calls == [ANT2]


def test_mixed_providers_in_priority_order(net: Net) -> None:
    net.by_key = {ANT1: lambda: http(529), OR_KEY: lambda: or_ok()}
    s = settings([entry("anthropic", "anthropic"), entry("openrouter", "openrouter")])
    res = ai.cleanup(RAW, "normal", s, [])
    assert res.provider == "openrouter" and res.key_id == "openrouter"


def test_timeout_does_not_wait_for_next_key(net: Net) -> None:
    net.by_key = {ANT1: lambda: TimeoutError(), ANT2: lambda: ant_ok()}
    s = settings([entry("anthropic", "anthropic"), entry("anthropic-2", "anthropic")])
    with pytest.raises(ai.AIError) as ei:
        ai.cleanup(RAW, "normal", s, [])
    assert ei.value.kind == "timeout" and net.calls == [ANT1]
    # …но следующая диктовка уже пойдёт через второй ключ
    assert ai.cleanup(RAW, "normal", s, []).key_id == "anthropic-2"


def test_all_keys_fail_reports_first_error(net: Net) -> None:
    net.by_key = {ANT1: lambda: http(401), OR_KEY: lambda: http(429)}
    s = settings([entry("anthropic", "anthropic"), entry("openrouter", "openrouter")])
    with pytest.raises(ai.AIError) as ei:
        ai.cleanup(RAW, "normal", s, [])
    assert ei.value.kind == "auth" and "Anthropic" in str(ei.value)


def test_key_without_secret_is_skipped(net: Net) -> None:
    net.by_key = {OR_KEY: lambda: or_ok()}
    s = settings([entry("gone", "anthropic"), entry("openrouter", "openrouter")])
    assert ai.cleanup(RAW, "normal", s, []).key_id == "openrouter"
    s = settings([entry("gone", "anthropic")])
    with pytest.raises(ai.AIError) as ei:
        ai.cleanup(RAW, "normal", s, [])
    assert ei.value.kind == "no_key"


def test_empty_list_says_no_keys(net: Net) -> None:
    with pytest.raises(ai.AIError) as ei:
        ai.cleanup(RAW, "normal", settings([]), [])
    assert ei.value.kind == "no_key" and "ни одного ключа" in str(ei.value)


def test_bad_model_answer_is_not_retried_with_another_key(net: Net) -> None:
    trunc = {"content": [{"type": "text", "text": "Гот"}], "stop_reason": "max_tokens"}
    net.by_key = {ANT1: lambda: trunc, ANT2: lambda: ant_ok()}
    s = settings([entry("anthropic", "anthropic"), entry("anthropic-2", "anthropic")])
    with pytest.raises(ai.AIError) as ei:
        ai.cleanup(RAW, "normal", s, [])
    assert ei.value.kind == "truncated" and net.calls == [ANT1]


def test_command_mode_uses_the_same_chain(net: Net) -> None:
    net.by_key = {ANT1: lambda: http(402), OR_KEY: lambda: or_ok("Коротко.")}
    s = settings([entry("anthropic", "anthropic"), entry("openrouter", "openrouter")],
                 command_provider="keys")
    assert ai.command("сократи", "длинный текст", s).key_id == "openrouter"


# ---------- переход со старых настроек ----------

def test_migration_from_single_provider(tmp_path: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("APPDATA", str(tmp_path))
    p = config.settings_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"ai_provider": "anthropic", "command_provider": "openrouter",
                             "broadcast_targets": [], "default_style": "normal"}),
                 encoding="utf-8")
    s = config.load()
    assert s.ai_provider == "keys" and s.command_provider == "keys"
    assert [e["id"] for e in s.ai_keys] == ["anthropic", "openrouter"]
    # дальше — без повторного перехода: порядок пользователя сохраняется
    s2 = config.load()
    assert s2.ai_keys == s.ai_keys
    # ollama не трогаем
    p.write_text(json.dumps({"ai_provider": "ollama", "command_provider": "claude_cli",
                             "broadcast_targets": []}), encoding="utf-8")
    s3 = config.load()
    assert s3.ai_provider == "ollama" and s3.command_provider == "claude_cli"


def test_broken_entries_are_dropped_on_load() -> None:
    s = Settings(ai_keys=[entry("ok", "anthropic"), {"id": "BAD ID", "provider": "x"},
                          entry("ok", "openrouter"), "мусор"])  # type: ignore[list-item]
    s.validate()
    assert [e["id"] for e in s.ai_keys] == ["ok"]
    with pytest.raises(ValueError):
        config.apply_patch(Settings(), {"ai_keys": [{"id": "a", "provider": "google"}]})


# ---------- WS-команды ----------

def test_ws_add_reorder_update_info_remove(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ai, "check_anthropic_key", lambda k: {
        "valid": True, "label": None, "balance_usd": None, "usage_usd": None,
        "limit_usd": None, "error": None})
    c = env.client()
    assert c.call("ai_keys_list") == []
    a1 = c.call("ai_key_add", {"provider": "anthropic", "key": ANT1})
    a2 = c.call("ai_key_add", {"provider": "anthropic", "key": ANT2, "label": "Новый",
                               "balance_usd": 50})
    o = c.call("ai_key_add", {"provider": "openrouter", "key": FAKE_KEY})
    assert a1["id"] == "anthropic" and a2["id"].startswith("anthropic-")
    assert env.calls.creds["Rupor/anthropic"] == ANT1
    assert env.calls.creds[f"Rupor/anthropic/{a2['id']}"] == ANT2
    assert a2["title"] == "Новый" and a2["has_key"] and a2["masked"].endswith(ANT2[-3:])
    assert a1["title"] == "Anthropic"            # пока он один
    titles = {x["id"]: x["title"] for x in c.call("ai_keys_list")}
    assert titles["anthropic"] == "Anthropic #1" and titles["openrouter"] == "OpenRouter"
    # порядок = приоритет
    lst = c.call("ai_keys_reorder", {"ids": [a2["id"], o["id"], a1["id"]]})
    assert [x["id"] for x in lst] == [a2["id"], "openrouter", "anthropic"]
    assert "обновите" in c.error("ai_keys_reorder", {"ids": [a2["id"]]})
    # баланс Anthropic: API его не даёт — оценка «введено − расход с того момента»
    env.eng.storage.add_ai_usage({"kind": "cleanup", "provider": "anthropic", "model": "m",
                                  "cost_usd": 1.25, "ok": True, "key_id": a2["id"]})
    info = c.call("ai_key_info", {"id": a2["id"]})
    assert info["balance_usd"] == 48.75 and info["balance_source"] == "estimate"
    assert info["spent_usd"] == 1.25 and info["valid"] is True
    info1 = c.call("ai_key_info", {"id": "anthropic"})
    assert info1["balance_usd"] is None and "не отдаёт баланс" in info1["error"]
    # активный ключ (первый) — его баланс на Главной
    b = c.call("ai_balance")
    assert b["key_id"] == a2["id"] and b["estimate"] is True and b["balance_usd"] == 48.75
    # OpenRouter — по API
    io_ = c.call("ai_key_info", {"id": "openrouter"})
    assert io_["balance_usd"] == 12.34 and io_["balance_source"] == "api"
    # выключить, переименовать, убрать баланс
    u = c.call("ai_key_update", {"id": a2["id"], "enabled": False, "label": "",
                                 "balance_usd": None})
    assert u["enabled"] is False and u["balance_usd"] is None and u["title"] == "Anthropic #1"   # номер — по месту в списке
    assert env.eng.s.ai_keys[0]["enabled"] is False
    # удалить — и секрет тоже
    c.call("ai_key_remove", {"id": a2["id"]})
    assert f"Rupor/anthropic/{a2['id']}" not in env.calls.creds
    assert [x["id"] for x in c.call("ai_keys_list")] == ["openrouter", "anthropic"]
    assert "нет" in c.error("ai_key_remove", {"id": "nope"})
    raw = "".join(c.raw)
    assert ANT1 not in raw and ANT2 not in raw and FAKE_KEY not in raw
    c.close()


def test_legacy_key_set_joins_the_list(env: Env) -> None:
    c = env.client()
    c.call("ai_key_set", {"provider": "anthropic", "key": ANT1})
    assert [e["id"] for e in env.eng.s.ai_keys] == ["anthropic"]
    c.call("ai_key_set", {"provider": "anthropic", "key": ANT2})   # замена — без дубля
    assert len(env.eng.s.ai_keys) == 1
    c.call("ai_key_delete", {"provider": "anthropic"})
    assert env.eng.s.ai_keys == [] and env.calls.creds == {}
    c.close()


def test_spend_by_key_is_recorded(env: Env) -> None:
    st = env.eng.storage
    t0 = time.time()
    st.add_ai_usage({"kind": "cleanup", "provider": "anthropic", "model": "m", "ts": t0 - 100,
                     "cost_usd": 2.0, "ok": True, "key_id": "k"})
    st.add_ai_usage({"kind": "cleanup", "provider": "anthropic", "model": "m", "ts": t0,
                     "cost_usd": 0.5, "ok": True, "key_id": "k"})
    st.add_ai_usage({"kind": "cleanup", "provider": "anthropic", "model": "m", "ts": t0,
                     "ok": False, "error_kind": "auth", "key_id": "k"})
    assert st.ai_key_spend("k") == {"cost_usd": 2.5, "calls": 2}
    assert st.ai_key_spend("k", t0 - 1) == {"cost_usd": 0.5, "calls": 1}
    assert st.ai_key_spend("other") == {"cost_usd": 0.0, "calls": 0}
