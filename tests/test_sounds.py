"""Звуки начала и конца записи: ошибка звука не срывает запись и не роняет поток."""
from __future__ import annotations

from pathlib import Path

import pytest

from rupor import sounds


@pytest.mark.parametrize("exc", [OSError(28, "на диске нет места"), RuntimeError("нет звука")])
def test_play_swallows_errors(monkeypatch: pytest.MonkeyPatch, exc: Exception) -> None:
    def broken(name: str) -> Path:
        raise exc

    monkeypatch.setattr(sounds, "_path", broken)
    sounds.play("stop")                          # не бросает
