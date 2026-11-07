"""Список ключей ИИ по приоритету: где лежит секрет, в каком порядке пробовать ключи,
какие ключи временно пропускать.

Описание ключей (id, провайдер, подпись, вкл/выкл, введённый вручную баланс) хранится в
настройках (``Settings.ai_keys``), порядок списка — это приоритет. Секрет — в Windows
Credential Manager:

- ``Rupor/openrouter`` и ``Rupor/anthropic`` — ключи, заведённые до списка (id записи
  совпадает с именем провайдера). Цель не меняется, так что ключ, введённый в прежней
  версии или руками через «Диспетчер учётных данных», продолжает работать;
- ``Rupor/<провайдер>/<id>`` — остальные ключи (в том числе второй, третий ключ того же
  провайдера).

Очистка и командный режим идут по ключам сверху вниз (``chain``). Ключ, который упал с
ошибкой «ключ/счёт/лимит/сервер/сеть», уходит на время в конец очереди (``mark_failed``):
следующая диктовка не ждёт заведомо нерабочий ключ. Секреты модуль не логирует.
"""
from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any

from . import credman
from .config import CHAIN_PROVIDERS, KEY_PROVIDERS

log = logging.getLogger(__name__)

PROVIDER_NAMES = {"openrouter": "OpenRouter", "anthropic": "Anthropic"}

# Ошибки, после которых имеет смысл взять следующий ключ (AIError.kind). Таймаут сюда не
# входит: диктовка уже прождала весь ai_timeout_s, второй такой же срок — слишком долго;
# такой ключ только уходит в конец очереди для следующих диктовок.
FALLBACK_KINDS = frozenset({"no_key", "auth", "payment", "forbidden", "rate_limit", "server",
                            "network", "bad_request"})
# Сколько секунд ключ стоит в конце очереди после ошибки
COOLDOWN_S: dict[str, float] = {
    "auth": 1800.0, "payment": 1800.0, "forbidden": 1800.0, "no_key": 600.0,
    "bad_request": 600.0, "rate_limit": 60.0, "server": 60.0, "network": 60.0,
    "timeout": 60.0,
}


def target(entry: dict[str, Any]) -> str:
    """Имя цели в Credential Manager для записи списка."""
    provider, kid = str(entry["provider"]), str(entry["id"])
    if kid == provider:
        return credman.TARGETS[provider]
    return f"{credman.TARGETS[provider]}/{kid}"


def new_id(provider: str, existing: list[dict[str, Any]]) -> str:
    """Новый id записи: первый ключ провайдера — его имя (цель как до списка ключей), если
    эта цель свободна; дальше — «<провайдер>-<6 hex>»."""
    ids = {str(e.get("id")) for e in existing}
    if provider not in ids:
        return provider
    while True:
        kid = f"{provider}-{secrets.token_hex(3)}"
        if kid not in ids:
            return kid


def label_of(entry: dict[str, Any], entries: list[dict[str, Any]]) -> str:
    """Подпись для логов и сообщений: своя или «Anthropic #2» по номеру среди ключей этого
    провайдера."""
    if entry.get("label"):
        return str(entry["label"])
    name = PROVIDER_NAMES.get(str(entry.get("provider")), str(entry.get("provider")))
    same = [e for e in entries if e.get("provider") == entry.get("provider")]
    if len(same) <= 1:
        return name
    n = next((i for i, e in enumerate(same, 1) if e.get("id") == entry.get("id")), 1)
    return f"{name} #{n}"


def find(entries: list[dict[str, Any]], kid: str) -> dict[str, Any] | None:
    return next((e for e in entries if e.get("id") == kid), None)


def drop_missing_legacy(entries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Записи, созданные переходом на список (id = имя провайдера), у которых секрета в
    Credential Manager нет, — убрать (ключа Anthropic у пользователя могло и не быть).
    Ошибка чтения — запись оставляем."""
    out = []
    for e in entries:
        if e.get("id") == e.get("provider"):
            try:
                if credman.read(target(e)) is None:
                    log.info("Список ключей: ключа %s нет — запись убрана", e["provider"])
                    continue
            except OSError:
                pass
        out.append(e)
    return out


# ---------- очередь и пропуск упавших ключей ----------

@dataclass
class Cooldown:
    until: float      # time.time()
    kind: str


_lock = threading.Lock()
_cooldown: dict[str, Cooldown] = {}


def mark_failed(kid: str, kind: str) -> None:
    sec = COOLDOWN_S.get(kind)
    if not sec:
        return
    with _lock:
        _cooldown[kid] = Cooldown(time.time() + sec, kind)


def mark_ok(kid: str) -> None:
    with _lock:
        _cooldown.pop(kid, None)


def cooldown(kid: str) -> Cooldown | None:
    """Ключ сейчас стоит в конце очереди — до какого времени и из-за какой ошибки."""
    now = time.time()
    with _lock:
        c = _cooldown.get(kid)
        if c is not None and c.until <= now:
            _cooldown.pop(kid, None)
            c = None
    return c


def reset(kid: str | None = None) -> None:
    """Забыть ошибки ключа (ключ заменили) или всех ключей."""
    with _lock:
        if kid is None:
            _cooldown.clear()
        else:
            _cooldown.pop(kid, None)


def chain(s: object, provider: str) -> list[dict[str, Any]]:
    """Ключи, которые попробует вызов с ``ai_provider``/``command_provider`` = ``provider``:
    включённые, по порядку списка; ``openrouter``/``anthropic`` — только ключи этого
    провайдера. Ключи «на паузе» после ошибки — в конце (в своём порядке)."""
    if provider not in CHAIN_PROVIDERS:
        return []
    raw = getattr(s, "ai_keys", None)
    if raw is None:      # объект без поля ai_keys (старые настройки, бенчмарки) — прежние цели
        raw = [{"id": p, "provider": p, "enabled": True} for p in KEY_PROVIDERS]
    entries = [e for e in raw if isinstance(e, dict)] if isinstance(raw, list) else []
    picked = [e for e in entries
              if e.get("enabled", True) and e.get("provider") in KEY_PROVIDERS
              and (provider == "keys" or e.get("provider") == provider)]
    fresh = [e for e in picked if cooldown(str(e.get("id"))) is None]
    resting = [e for e in picked if cooldown(str(e.get("id"))) is not None]
    return fresh + resting


def first_provider(s: object, provider: str) -> str | None:
    """Провайдер первого ключа очереди (для модели в учёте расхода, если вызов упал)."""
    c = chain(s, provider)
    return str(c[0]["provider"]) if c else None
