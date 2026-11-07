"""Общие фикстуры: тесты не смотрят в окна пользователя и не трогают его звук.

- ``winutil.list_rdp_windows`` → [] (миграция broadcast_targets в config.load, рассылка и
  событие recording иначе перебирали бы настоящие окна и их заголовки);
- ``uia.caret_context`` → None (контекст окна для подсказки иначе читал бы текст перед
  курсором в настоящем поле с фокусом);
- ``duck.default_backend`` → сессий нет (движок по умолчанию приглушает звук компьютера во
  время записи — иначе тест с записью приглушил бы музыку или созвон пользователя).
  Настоящий Core Audio — только в tests/test_duck.py и только на собственной тестовой
  сессии (отдельный процесс, играющий тишину);
- язык сообщений ядра — русский: «Системный» язык (``i18n.system_language``) считается
  русским на любой машине (CI с английской Windows), после каждого теста язык снова
  «Системный» — тексты ошибок и уведомлений в тестах сверяются по-русски, если тест сам
  не переключил язык (tests/test_i18n.py).
Тест, которому нужны окна, подменяет их сам (monkeypatch поверх этих)."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from rupor import duck, i18n, keys, uia, winutil


@pytest.fixture(autouse=True)
def _no_user_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda classes, procs: [])

    def no_context(*_a: Any, **_k: Any) -> None:
        return None

    monkeypatch.setattr(uia, "caret_context", no_context)


@pytest.fixture(autouse=True)
def _fresh_key_queue() -> Iterator[None]:
    """Паузы ключей ИИ после ошибок (rupor.keys) — у каждого теста свои."""
    keys.reset()
    yield
    keys.reset()


@pytest.fixture(autouse=True)
def _no_user_audio(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duck, "default_backend", duck.NullBackend)


@pytest.fixture(autouse=True)
def _russian_messages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(i18n, "system_language", lambda: i18n.RU)
    i18n.set_language(i18n.SYSTEM)
    yield
    i18n.set_language(i18n.SYSTEM)
