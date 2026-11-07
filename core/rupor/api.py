"""Диспетчер WebSocket-команд (docs/ARCHITECTURE.md → «Команды»).

``Api.respond(id, cmd, args)`` → готовый ответ ``{id, ok: true, result}`` или
``{id, ok: false, error: "текст на языке интерфейса"}`` (``i18n.tr``: язык меняется на лету,
следующая ошибка — уже на новом). Команды выполняются в пуле потоков
сервера, поэтому долгие (ai_test, test_record_stop) не мешают остальным.
Ключи API наружу не отдаются никогда — только маска ``sk-or-v1-…XYZ``; текст любой
ошибки дополнительно чистится от всего, похожего на ключ (``_scrub``).
"""
from __future__ import annotations

import copy
import logging
import math
import os
import time
from typing import Any, Callable

from . import ai, broadcast, config, credman, fetch, keys, recordings, winutil
from .config import app_dir
from .engine import Engine, history_display, styles_localized
from .i18n import tr
from .storage import LEARN_JOURNAL_MAX

log = logging.getLogger(__name__)

KEY_PROVIDERS = tuple(credman.TARGETS)  # openrouter, anthropic
_PROVIDER_NAMES = {"openrouter": "OpenRouter", "anthropic": "Anthropic"}


class ApiError(Exception):
    """Ошибка команды: текст (на языке интерфейса) уходит в поле error как есть."""


# ---------- разбор аргументов ----------

def _args(a: Any) -> dict[str, Any]:
    if a is None:
        return {}
    if not isinstance(a, dict):
        raise ApiError(tr("api.args_not_object"))
    return a


def _req(a: dict[str, Any], name: str) -> Any:
    if name not in a or a[name] is None:
        raise ApiError(tr("api.arg_missing", name=name))
    return a[name]


def _int(a: dict[str, Any], name: str, default: int | None = None) -> int:
    v = a.get(name, default)
    if v is None:
        raise ApiError(tr("api.arg_missing", name=name))
    if isinstance(v, bool) or not isinstance(v, (int, float)) or v != int(v):
        raise ApiError(tr("api.arg_int", name=name))
    return int(v)


def _opt_int(a: dict[str, Any], name: str) -> int | None:
    return None if a.get(name) is None else _int(a, name)


def _str(a: dict[str, Any], name: str, default: str | None = None) -> str:
    v = a.get(name, default)
    if v is None:
        raise ApiError(tr("api.arg_missing", name=name))
    if not isinstance(v, str):
        raise ApiError(tr("api.arg_str", name=name))
    return v


def _bool(a: dict[str, Any], name: str) -> bool:
    v = _req(a, name)
    if not isinstance(v, bool):
        raise ApiError(tr("api.arg_bool", name=name))
    return v


def _rec_id(a: dict[str, Any]) -> str:
    rid = _str(a, "id")
    if not recordings.valid_id(rid):
        raise ApiError(tr("api.recording_not_found"))
    return rid


def _provider(a: dict[str, Any]) -> str:
    p = _str(a, "provider")
    if p not in KEY_PROVIDERS:
        raise ApiError(tr("api.unknown_provider", provider=p, allowed=", ".join(KEY_PROVIDERS)))
    return p


def _str_list(a: dict[str, Any], name: str) -> list[str]:
    v = a.get(name, [])
    if v is None:
        return []
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise ApiError(tr("api.arg_str_list", name=name))
    return list(v)


class Api:
    def __init__(self, engine: Engine) -> None:
        self.e = engine
        self.storage = engine.storage
        self._handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "get_state": lambda a: self.e.get_state(),
            "get_settings": lambda a: config.to_dict(self.e.s),
            "set_settings": self._set_settings,
            "pause": self._pause,
            "quit": self._quit,
            "list_devices": lambda a: self.e.list_devices(),
            "mic_monitor": self._mic_monitor,
            "test_record_start": self._test_record_start,
            "dictation_toggle": lambda a: self.e.toggle_dictation(),
            "test_record_stop": lambda a: self.e.test_record_stop(),
            "cuda_check": lambda a: self.e.cuda_check(),
            "setup_status": lambda a: self.e.setup_status(),
            "setup_download": self._setup_download,
            "model_dir_set": self._model_dir_set,
            "model_recommend": lambda a: self.e.model_recommend(),
            "history_list": self._history_list,
            "history_delete": self._history_delete,
            "history_clear": self._history_clear,
            "history_reinsert": self._history_reinsert,
            "last_insert": self._last_insert,
            "recordings_list": lambda a: self.e.recordings_list(),
            "recording_transcribe": lambda a: self.e.recording_transcribe(_rec_id(a)),
            "recording_audio": lambda a: self.e.recording_audio(_rec_id(a)),
            "recording_delete": self._recording_delete,
            "recordings_clear": lambda a: {"deleted": self.e.recordings_clear()},
            "recording_reveal": self._recording_reveal,
            "dict_list": lambda a: self.storage.dict_list(),
            "dict_upsert": self._dict_upsert,
            "dict_delete": self._dict_delete,
            "dict_learn_accept": self._dict_learn_accept,
            "dict_learn_undo": self._dict_learn_undo,
            "dict_learn_journal": self._dict_learn_journal,
            "snippets_list": lambda a: self.storage.snippets_list(),
            "snippet_upsert": self._snippet_upsert,
            "snippet_delete": self._snippet_delete,
            "export_data": lambda a: self.storage.export_data(),
            "import_data": self._import_data,
            "stats_get": lambda a: self.storage.stats(),
            "draft_get": lambda a: {"text": self.storage.kv_get("draft", "")},
            "draft_set": self._draft_set,
            "styles_list": lambda a: styles_localized(),
            "app_rules_list": lambda a: self._app_rules_list(),
            "app_rule_upsert": self._app_rule_upsert,
            "app_rule_delete": self._app_rule_delete,
            "title_rules_list": lambda a: self._title_rules_list(),
            "title_rule_upsert": self._title_rule_upsert,
            "title_rule_delete": self._title_rule_delete,
            "recent_apps": lambda a: self.e.recent_apps(),
            "rdp_windows": lambda a: self._rdp_windows(),
            "rdp_toggle": self._rdp_toggle,
            "ai_key_status": self._ai_key_status,
            "ai_key_import_env": self._ai_key_import_env,
            "ai_key_set": self._ai_key_set,
            "ai_key_delete": self._ai_key_delete,
            "ai_key_check": lambda a: self._ai_key_check(),
            "ai_keys_list": lambda a: self.e.ai_keys_list(),
            "ai_key_add": self._ai_key_add,
            "ai_key_update": self._ai_key_update,
            "ai_key_replace": self._ai_key_replace,
            "ai_key_remove": self._ai_key_remove,
            "ai_keys_reorder": self._ai_keys_reorder,
            "ai_key_info": self._ai_key_info,
            "ai_test": self._ai_test,
            "ai_usage_stats": lambda a: self.e.ai_usage_stats(),
            "ai_balance": self._ai_balance,
            "vocab_list": self._vocab_list,
            "vocab_exclude": self._vocab_exclude,
            "open_logs": self._open_logs,
            "relaunch_admin": self._relaunch_admin,
        }

    @property
    def commands(self) -> list[str]:
        return list(self._handlers)

    def hello(self) -> dict[str, Any]:
        return self.e.get_state()

    def call(self, cmd: Any, args: Any = None) -> Any:
        """Выполнить команду. Ошибка — ApiError/ValueError с текстом на языке интерфейса."""
        if not isinstance(cmd, str) or cmd not in self._handlers:
            raise ApiError(tr("api.unknown_command", cmd=cmd))
        return self._handlers[cmd](_args(args))

    def respond(self, req_id: Any, cmd: Any, args: Any = None) -> dict[str, Any]:
        """Ответ по протоколу; исключения превращаются в {ok: false, error}."""
        try:
            return {"id": req_id, "ok": True, "result": self.call(cmd, args)}
        except (ApiError, ValueError) as e:
            err = str(e) or tr("api.bad_request")
        except (ai.AIError, fetch.FetchError) as e:
            err = str(e)
        except OSError as e:
            log.warning("Команда %s: %s", cmd, e)
            err = tr("api.system_error", error=e.strerror or type(e).__name__)
        except Exception as e:
            log.exception("Команда %s упала", cmd)
            err = tr("api.internal_error", name=type(e).__name__)
        # Страховка для любой команды: ключ мог оказаться в тексте исключения (например,
        # ValueError http.client о недопустимом заголовке «Bearer <ключ>»)
        return {"id": req_id, "ok": False, "error": _scrub(err)}

    # ---------- состояние, настройки ----------

    def _set_settings(self, a: dict[str, Any]) -> dict[str, Any]:
        patch = _req(a, "patch")
        if not isinstance(patch, dict):
            raise ApiError(tr("api.patch_not_object"))
        return self.e.update_settings(patch)

    def _pause(self, a: dict[str, Any]) -> dict[str, Any]:
        self.e.set_paused(_bool(a, "on"))
        return self.e.get_state()

    def _quit(self, a: dict[str, Any]) -> None:
        log.info("Команда «Выход» из интерфейса")
        self.e.request_quit()
        return None

    def _mic_monitor(self, a: dict[str, Any]) -> None:
        self.e.set_mic_monitor(_bool(a, "on"))
        return None

    def _test_record_start(self, a: dict[str, Any]) -> None:
        self.e.test_record_start()
        return None

    def _setup_download(self, a: dict[str, Any]) -> None:
        what = _str(a, "what")
        if what not in ("model", "cuda"):
            raise ApiError(tr("err.what_download"))
        self.e.setup_download(what)
        return None

    def _model_dir_set(self, a: dict[str, Any]) -> dict[str, Any]:
        path = _str(a, "path")
        move = a.get("move", False)
        if not isinstance(move, bool):
            raise ApiError(tr("api.arg_bool", name="move"))
        return self.e.model_dir_set(path, move)

    # ---------- история ----------

    def _history_list(self, a: dict[str, Any]) -> dict[str, Any]:
        q = a.get("query")
        if q is not None and not isinstance(q, str):
            raise ApiError(tr("api.arg_str", name="query"))
        limit = _int(a, "limit", 50)
        offset = _int(a, "offset", 0)
        if limit < 0 or offset < 0:
            raise ApiError(tr("api.limit_offset_negative"))
        items, total = self.storage.list_history(q, limit, offset)
        return {"items": [history_display(dict(it)) for it in items], "total": total}

    def _history_delete(self, a: dict[str, Any]) -> None:
        self.storage.delete_history(_int(a, "id"))
        return None

    def _history_clear(self, a: dict[str, Any]) -> dict[str, int]:
        period = _str(a, "period")
        if period not in ("day", "week", "month", "all"):
            raise ApiError(tr("api.history_period"))
        return {"deleted": self.storage.clear_history(period)}

    def _history_reinsert(self, a: dict[str, Any]) -> None:
        item = self.storage.get_history(_int(a, "id"))
        if item is None:
            raise ApiError(tr("api.history_not_found"))
        self.e.insert_text(str(item["text"]))
        return None

    def _last_insert(self, a: dict[str, Any]) -> None:
        self.e.insert_last()
        return None

    # ---------- сохранённые записи голоса ----------

    def _recording_delete(self, a: dict[str, Any]) -> None:
        self.e.recording_delete(_rec_id(a))
        return None

    def _recording_reveal(self, a: dict[str, Any]) -> None:
        """Проводник: с id — выделить файл записи, без id — открыть папку записей."""
        rid = a.get("id")
        if rid is None:
            os.startfile(self.e.recording_path(None))
            return None
        path = self.e.recording_path(_rec_id(a))
        import subprocess
        subprocess.Popen(["explorer.exe", "/select,", str(path)])
        return None

    # ---------- словарь, сниппеты, импорт ----------

    def _dict_upsert(self, a: dict[str, Any]) -> dict[str, Any]:
        return self.storage.dict_upsert(_opt_int(a, "id"), _str(a, "term"),
                                        _str_list(a, "variants"))

    def _dict_delete(self, a: dict[str, Any]) -> None:
        self.storage.dict_delete(_int(a, "id"))
        return None

    # Обучение словаря на правках: кнопки уведомлений «Добавить» (suggest) и «Отменить» (auto),
    # журнал выученного — «Недавно добавлено» на странице «Словарь»

    def _learn_args(self, a: dict[str, Any]) -> tuple[str, str]:
        term = _str(a, "term").strip()
        if not term:
            raise ApiError(tr("st.term_empty"))
        variant = a.get("variant")
        if variant is not None and not isinstance(variant, str):
            raise ApiError(tr("api.arg_str", name="variant"))
        return term, (variant or "").strip()

    def _dict_learn_accept(self, a: dict[str, Any]) -> dict[str, Any]:
        term, variant = self._learn_args(a)
        return self.e.learn_accept(term, variant)

    def _dict_learn_undo(self, a: dict[str, Any]) -> None:
        term, variant = self._learn_args(a)
        self.e.learn_undo(term, variant, _opt_int(a, "id"), _bool(a, "created"))
        return None

    def _dict_learn_journal(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        limit = _int(a, "limit", 50)
        if not 0 <= limit <= LEARN_JOURNAL_MAX:
            raise ApiError(tr("api.limit_range", lo=0, hi=LEARN_JOURNAL_MAX))
        return self.storage.dict_learn_journal(limit)

    def _snippet_upsert(self, a: dict[str, Any]) -> dict[str, Any]:
        return self.storage.snippet_upsert(_opt_int(a, "id"), _str(a, "trigger"),
                                           _str(a, "text"))

    def _snippet_delete(self, a: dict[str, Any]) -> None:
        self.storage.snippet_delete(_int(a, "id"))
        return None

    def _import_data(self, a: dict[str, Any]) -> dict[str, int]:
        data = _req(a, "data")
        replace = a.get("replace", False)
        if not isinstance(replace, bool):
            raise ApiError(tr("api.arg_bool", name="replace"))
        return self.storage.import_data(data, replace)

    def _draft_set(self, a: dict[str, Any]) -> None:
        text = _str(a, "text")
        if len(text) > 5_000_000:
            raise ApiError(tr("api.draft_too_big"))
        self.storage.kv_set("draft", text)
        return None

    # ---------- правила приложений, RDP ----------

    def _app_rules_list(self) -> list[dict[str, Any]]:
        rules = self.e.s.app_rules
        return [{"process": p, "insert_mode": r.get("insert_mode"), "style": r.get("style")}
                for p, r in sorted(rules.items())]

    # Прочитать-изменить-записать — под замком настроек движка (modify_settings):
    # параллельные команды не теряют изменений друг друга.

    def _app_rule_upsert(self, a: dict[str, Any]) -> dict[str, Any]:
        r = config.app_rule(_req(a, "process"), a.get("insert_mode"), a.get("style"))

        def patch(s: config.Settings) -> dict[str, Any]:
            rules = copy.deepcopy(s.app_rules)
            rules[str(r["process"])] = {"insert_mode": r["insert_mode"], "style": r["style"]}
            return {"app_rules": rules}

        self.e.modify_settings(patch)
        return r

    def _app_rule_delete(self, a: dict[str, Any]) -> None:
        p = config.normalize_process(_req(a, "process"))

        def patch(s: config.Settings) -> dict[str, Any] | None:
            if p not in s.app_rules:
                return None
            return {"app_rules": {k: copy.deepcopy(v) for k, v in s.app_rules.items()
                                  if k != p}}

        self.e.modify_settings(patch)
        return None

    # Правила по названию окна: список, ключ — процесс + название без учёта регистра

    def _title_rules_list(self) -> list[dict[str, Any]]:
        return [dict(r) for r in sorted(
            self.e.s.title_rules,
            key=lambda r: (str(r["process"]), config.fold_title(str(r["title"]))))]

    def _title_rule_upsert(self, a: dict[str, Any]) -> dict[str, Any]:
        r = config.title_rule(_req(a, "process"), _req(a, "title"), a.get("insert_mode"),
                              a.get("style"))
        key = config.title_rule_key(str(r["process"]), str(r["title"]))

        def patch(s: config.Settings) -> dict[str, Any]:
            rules = [copy.deepcopy(x) for x in s.title_rules]
            for i, x in enumerate(rules):
                if config.title_rule_key(str(x["process"]), str(x["title"])) == key:
                    rules[i] = dict(r)           # то же правило (другой регистр) — заменить
                    break
            else:
                rules.append(dict(r))
            return {"title_rules": rules}

        self.e.modify_settings(patch)
        return r

    def _title_rule_delete(self, a: dict[str, Any]) -> None:
        proc = config.title_rule_process(_req(a, "process"))
        key = config.title_rule_key(proc, _str(a, "title"))

        def patch(s: config.Settings) -> dict[str, Any] | None:
            rest = [copy.deepcopy(x) for x in s.title_rules
                    if config.title_rule_key(str(x["process"]), str(x["title"])) != key]
            return None if len(rest) == len(s.title_rules) else {"title_rules": rest}

        self.e.modify_settings(patch)
        return None

    # Рассылка: только в отмеченные сессии (broadcast_targets — ключи сессий)

    def _rdp_windows(self) -> list[dict[str, Any]]:
        s = self.e.s
        chosen = broadcast.selected(s)
        out: list[dict[str, Any]] = []
        for wi in winutil.list_rdp_windows(s.rdp_classes, s.rdp_processes):
            key = broadcast.session_key(wi)
            out.append({"key": key, "title": wi.title, "process": wi.process,
                        "included": key.casefold() in chosen})
        return out

    def _rdp_key(self, a: dict[str, Any]) -> str:
        """Ключ сессии из {key} или (совместимость) из {title}: заголовок открытого окна →
        его ключ; окна нет — ключ по правилу mstsc, если заголовок похож, иначе заголовок."""
        if a.get("key") is not None:
            key = " ".join(_str(a, "key").split())
        else:
            title = _str(a, "title")
            s = self.e.s
            key = ""
            for wi in winutil.list_rdp_windows(s.rdp_classes, s.rdp_processes):
                if wi.title == title:
                    key = broadcast.session_key(wi)
                    break
            key = key or winutil.rdp_session_key(title, winutil.MSTSC_PROCESS)
        if not key:
            raise ApiError(tr("api.rdp_key_missing"))
        return key

    def _rdp_toggle(self, a: dict[str, Any]) -> None:
        key = self._rdp_key(a)
        included = _bool(a, "included")

        def patch(s: config.Settings) -> dict[str, Any] | None:
            rest = [k for k in s.broadcast_targets if k.casefold() != key.casefold()]
            new = [*rest, key] if included else rest
            return None if new == s.broadcast_targets else {"broadcast_targets": new}

        self.e.modify_settings(patch)
        return None

    # ---------- ключи ИИ ----------

    def _ensure_legacy_entry(self, provider: str) -> None:
        """Ключ записан по прежней цели Rupor/<провайдер> (мастер первого запуска, импорт из
        .env, старые команды) — он должен быть и в списке ключей (в конце, если его там нет)."""
        def patch(s: config.Settings) -> dict[str, Any] | None:
            if keys.find(s.ai_keys, provider) is not None:
                return None
            if len(s.ai_keys) >= config.AI_KEYS_MAX:
                raise ApiError(tr("api.keys_too_many", max=config.AI_KEYS_MAX))
            return {"ai_keys": [*s.ai_keys, {"id": provider, "provider": provider}]}
        self.e.modify_settings(patch)

    def _drop_entry(self, kid: str) -> None:
        def patch(s: config.Settings) -> dict[str, Any] | None:
            rest = [e for e in s.ai_keys if e["id"] != kid]
            return None if len(rest) == len(s.ai_keys) else {"ai_keys": rest}
        self.e.modify_settings(patch)

    def _entry(self, a: dict[str, Any]) -> dict[str, Any]:
        kid = _str(a, "id")
        e = keys.find(self.e.s.ai_keys, kid)
        if e is None:
            raise ApiError(tr("eng.key_unknown"))
        return e

    def _list_item(self, kid: str) -> dict[str, Any]:
        return next(x for x in self.e.ai_keys_list() if x["id"] == kid)

    def _ai_key_status(self, a: dict[str, Any]) -> dict[str, Any]:
        key = credman.read(credman.TARGETS[_provider(a)])
        return {"has_key": bool(key), "masked": credman.mask(key) if key else None}

    def _ai_key_import_env(self, a: dict[str, Any]) -> dict[str, Any]:
        path = a.get("path")
        if path is not None and not isinstance(path, str):
            raise ApiError(tr("api.arg_str", name="path"))
        path = (path or "").strip() or self.e.s.env_path
        if not path:
            raise ApiError(tr("api.env_path_missing"))
        key = credman.read_env_key(path)
        if not key:
            log.info("Ключ openrouter_api_key в %s не найден", path)
            return {"imported": False, "masked": None}
        credman.write(credman.TARGETS["openrouter"], key)
        self._ensure_legacy_entry("openrouter")
        self.e.key_changed("openrouter")
        log.info("Ключ OpenRouter импортирован из .env в Credential Manager")
        return {"imported": True, "masked": credman.mask(key)}

    def _ai_key_set(self, a: dict[str, Any]) -> dict[str, Any]:
        provider = _provider(a)
        key = _str(a, "key").strip()
        if not key:
            raise ApiError(tr("api.key_empty"))
        if any(c.isspace() for c in key):
            raise ApiError(tr("api.key_whitespace"))
        credman.write(credman.TARGETS[provider], key)
        self._ensure_legacy_entry(provider)
        self.e.key_changed(provider)
        return {"masked": credman.mask(key)}

    def _ai_key_delete(self, a: dict[str, Any]) -> None:
        provider = _provider(a)
        credman.delete(credman.TARGETS[provider])
        self._drop_entry(provider)
        self.e.key_changed(provider)
        return None

    def _ai_key_check(self) -> dict[str, Any]:
        # первый ключ OpenRouter из списка; списка нет — прежняя цель Rupor/openrouter
        first = next((e for e in self.e.s.ai_keys if e["provider"] == "openrouter"), None)
        key = credman.read(keys.target(first) if first else credman.TARGETS["openrouter"])
        if not key:
            return {"valid": False, "label": None, "balance_usd": None, "usage_usd": None,
                    "limit_usd": None, "error": tr("api.openrouter_key_missing")}
        return ai.check_openrouter_key(key)

    # ---------- список ключей ИИ по приоритету ----------

    @staticmethod
    def _secret(a: dict[str, Any]) -> str:
        key = _str(a, "key").strip()
        if not key:
            raise ApiError(tr("api.key_empty"))
        if any(c.isspace() for c in key):
            raise ApiError(tr("api.key_whitespace"))
        return key

    @staticmethod
    def _balance_arg(a: dict[str, Any]) -> float | None:
        v = a.get("balance_usd")
        if v is None:
            return None
        if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) \
                or not 0 <= v <= 1_000_000:
            raise ApiError(tr("api.arg_balance", name="balance_usd"))
        return round(float(v), 4)

    def _ai_key_add(self, a: dict[str, Any]) -> dict[str, Any]:
        """Новый ключ — в конец списка (приоритет меняется перетаскиванием)."""
        provider = _provider(a)
        key = self._secret(a)
        label = _str(a, "label", "")
        balance = self._balance_arg(a)
        if len(self.e.s.ai_keys) >= config.AI_KEYS_MAX:
            raise ApiError(tr("api.keys_too_many", max=config.AI_KEYS_MAX))
        kid = keys.new_id(provider, self.e.s.ai_keys)
        entry = config.ai_key_entry({
            "id": kid, "provider": provider, "label": label, "enabled": True,
            "balance_usd": balance, "balance_at": time.time() if balance is not None else None})
        credman.write(keys.target(entry), key)

        def patch(s: config.Settings) -> dict[str, Any]:
            if len(s.ai_keys) >= config.AI_KEYS_MAX:
                raise ApiError(tr("api.keys_too_many", max=config.AI_KEYS_MAX))
            return {"ai_keys": [*s.ai_keys, entry]}
        try:
            self.e.modify_settings(patch)
        except Exception:
            credman.delete(keys.target(entry))     # запись не попала в список — секрет убрать
            raise
        self.e.key_changed(kid)
        log.info("Ключ ИИ добавлен в список: %s (%s)", kid, provider)
        return self._list_item(kid)

    def _ai_key_update(self, a: dict[str, Any]) -> dict[str, Any]:
        """Подпись, вкл/выкл, введённый вручную баланс (null — убрать); остальное не трогаем."""
        kid = str(self._entry(a)["id"])
        changes: dict[str, Any] = {}
        if "label" in a:
            changes["label"] = _str(a, "label", "")
        if "enabled" in a:
            changes["enabled"] = _bool(a, "enabled")
        if "balance_usd" in a:
            bal = self._balance_arg(a)
            changes["balance_usd"] = bal
            changes["balance_at"] = time.time() if bal is not None else None

        def patch(s: config.Settings) -> dict[str, Any] | None:
            new = [{**e, **changes} if e["id"] == kid else e for e in s.ai_keys]
            return None if new == s.ai_keys else {"ai_keys": new}
        self.e.modify_settings(patch)
        if "enabled" in changes:
            self.e.key_changed(kid)
        return self._list_item(kid)

    def _ai_key_replace(self, a: dict[str, Any]) -> dict[str, Any]:
        e = self._entry(a)
        credman.write(keys.target(e), self._secret(a))
        self.e.key_changed(str(e["id"]))
        return self._list_item(str(e["id"]))

    def _ai_key_remove(self, a: dict[str, Any]) -> None:
        e = self._entry(a)
        credman.delete(keys.target(e))
        self._drop_entry(str(e["id"]))
        self.e.key_changed(str(e["id"]))
        log.info("Ключ ИИ убран из списка: %s", e["id"])
        return None

    def _ai_keys_reorder(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        """Новый порядок (= приоритет): ids — все id списка ровно по разу."""
        ids = _str_list(a, "ids")

        def patch(s: config.Settings) -> dict[str, Any] | None:
            by_id = {e["id"]: e for e in s.ai_keys}
            if len(ids) != len(by_id) or set(ids) != set(by_id):
                raise ApiError(tr("api.keys_reorder_mismatch"))
            new = [by_id[i] for i in ids]
            return None if new == s.ai_keys else {"ai_keys": new}
        self.e.modify_settings(patch)
        return self.e.ai_keys_list()

    def _ai_key_info(self, a: dict[str, Any]) -> dict[str, Any]:
        kid = str(self._entry(a)["id"])
        force = a.get("force", False)
        if not isinstance(force, bool):
            raise ApiError(tr("api.arg_bool", name="force"))
        return self.e.ai_key_info(kid, force)

    def _ai_test(self, a: dict[str, Any]) -> dict[str, Any]:
        text = _str(a, "text")
        if not text.strip():
            raise ApiError(tr("api.ai_test_empty"))
        style = _str(a, "style", self.e.s.default_style)
        if style not in ai.STYLE_IDS:
            raise ApiError(tr("api.unknown_style", style=style))
        r = self.e.ai_test(text, style)
        return {"text": r.text, "ms": round(r.ms), "cost_usd": r.cost_usd,
                "provider": r.provider, "model": r.model}

    def _ai_balance(self, a: dict[str, Any]) -> dict[str, Any]:
        force = a.get("force", False)
        if not isinstance(force, bool):
            raise ApiError(tr("api.arg_bool", name="force"))
        return self.e.ai_balance(force)

    # ---------- личный словарь частот ----------

    def _vocab_list(self, a: dict[str, Any]) -> list[dict[str, Any]]:
        proc = a.get("process")
        if proc is not None and not isinstance(proc, str):
            raise ApiError(tr("api.arg_str", name="process"))
        limit = _int(a, "limit", 50)
        if limit < 0:
            raise ApiError(tr("api.limit_negative"))
        return self.storage.vocab_list((proc or "").strip() or None, limit)

    def _vocab_exclude(self, a: dict[str, Any]) -> None:
        self.storage.vocab_exclude(_str(a, "word"), _bool(a, "excluded"))
        return None

    # ---------- прочее ----------

    def _open_logs(self, a: dict[str, Any]) -> None:
        d = app_dir() / "logs"
        d.mkdir(parents=True, exist_ok=True)
        os.startfile(d)
        return None

    def _relaunch_admin(self, a: dict[str, Any]) -> None:
        raise ApiError(tr("api.relaunch_by_ui"))


def _scrub(text: str) -> str:
    """Страховка: из текста ошибки убрать всё, похожее на ключ."""
    from .log import scrub_secrets
    return scrub_secrets(text)
