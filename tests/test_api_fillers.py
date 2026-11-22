"""Слова-паразиты через WS: считаются по сырому тексту распознавания (до ИИ-очистки),
список редактируется командами, неверный период — ошибка на языке интерфейса."""
from __future__ import annotations

from test_api import Env, _enable_ai, env  # noqa: F401 — фикстура env


def test_fillers_counted_from_raw_before_cleanup(env: Env) -> None:
    c = env.client()
    _enable_ai(c)
    env.calls.cleanup_impl = lambda t: "Надо обновить витрину сегодня."
    c.mark()
    env.dictate("Ну, короче, эээ, надо как бы обновить витрину сегодня")
    c.wait_event("history")
    st = c.call("fillers_stats", {"days": 7})
    top = {t["phrase"]: t["count"] for t in st["top"]}
    assert top == {"ну": 1, "короче": 1, "эээ": 1, "как бы": 1}
    assert st["words"] == 9 and st["fillers"] == 4 and st["per100"] == 44.44
    c.close()


def test_fillers_config_commands(env: Env) -> None:
    c = env.client()
    cfg = c.call("fillers_config")
    assert cfg["enabled"] is True and "короче" in cfg["phrases"]
    assert cfg["phrases"] == cfg["defaults"]
    out = c.call("fillers_config_set", {"phrases": ["По факту", "Э-э-э"], "enabled": False})
    assert out["phrases"] == ["по факту", "эээ"] and out["enabled"] is False
    assert c.call("fillers_defaults")["phrases"] == cfg["defaults"]
    assert c.call("fillers_rebuild") == {"days": 0, "dictations": 0}
    assert c.call("fillers_reset") is None
    assert c.error("fillers_stats", {"days": 5})
    assert c.error("fillers_config_set", {"phrases": "ну"})
    assert c.error("fillers_ai_suggest")             # истории нет — ИИ разбирать нечего
    c.close()
