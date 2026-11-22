"""Общие фикстуры: тесты не смотрят в окна пользователя и не трогают его звук.

- ``winutil.list_rdp_windows`` → [] (миграция broadcast_targets в config.load, рассылка и
  событие recording иначе перебирали бы настоящие окна и их заголовки);
- ``uia.caret_context`` → None (контекст окна для подсказки иначе читал бы текст перед
  курсором в настоящем поле с фокусом);
- ``winutil.window_titles`` → {} (источники звука встречи, ``loopback.list_sources``, иначе
  читали бы заголовки окон браузеров);
- ``duck.default_backend`` → сессий нет (движок по умолчанию приглушает звук компьютера во
  время записи — иначе тест с записью приглушил бы музыку или созвон пользователя; и
  источников звука встречи нет — только «весь звук компьютера»);
- ``meeting_detect.read_mic_usage`` → [] (кто держит микрофон: журнал Windows
  CapabilityAccessManager пользователя — иначе тест с «auto» источником встречи или
  предложением встречи видел бы его настоящие звонки; tests/test_meeting_detect.py
  подставляет свой фейковый реестр).
- ``ai._new_conn`` → OSError (постоянные соединения перевода встречи: ``ai.prewarm`` и
  ``ai.translate_stream`` иначе открывали бы настоящие TCP/TLS к провайдеру; тесты потока
  подменяют ``ai._stream_open``), ``ai._urlopen`` → OSError (вызов ИИ без потока, который тест
  не подменил сам, — например заметки к встрече после «Стоп» в тесте движка с ключом, —
  ушёл бы в настоящий OpenRouter).
  Настоящий Core Audio — только в tests/test_duck.py и только на собственной тестовой
  сессии (отдельный процесс, играющий тишину), и в tests/test_loopback.py с
  RUPOR_AUDIO_TESTS=1 (свой процесс, играющий тихий тон);
- язык сообщений ядра — русский: «Системный» язык (``i18n.system_language``) считается
  русским на любой машине (CI с английской Windows), после каждого теста язык снова
  «Системный» — тексты ошибок и уведомлений в тестах сверяются по-русски, если тест сам
  не переключил язык (tests/test_i18n.py).
Тест, которому нужны окна, подменяет их сам (monkeypatch поверх этих)."""
from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import pytest

from rupor import appmute, ai, duck, i18n, keys, meeting_detect, uia, winutil


@pytest.fixture(autouse=True)
def _no_user_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(winutil, "list_rdp_windows", lambda classes, procs: [])
    monkeypatch.setattr(winutil, "window_titles", lambda pids: {})

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
    monkeypatch.setattr(appmute, "default_backend", appmute.NullBackend)
    monkeypatch.setattr(meeting_detect, "read_mic_usage", lambda want=None: [])


@pytest.fixture(autouse=True)
def _russian_messages(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    monkeypatch.setattr(i18n, "system_language", lambda: i18n.RU)
    i18n.set_language(i18n.SYSTEM)
    yield
    i18n.set_language(i18n.SYSTEM)


@pytest.fixture(autouse=True)
def _no_ai_connections(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_a: Any, **_k: Any) -> Any:
        raise OSError("сеть в тестах не открывается")

    monkeypatch.setattr(ai, "_new_conn", refuse)
    monkeypatch.setattr(ai, "_urlopen", refuse)
