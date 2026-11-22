"""Приглушение звука компьютера во время записи (rupor.duck).

Логика — на фейковых сессиях (FakeBackend): приглушить и вернуть, режим «выключать»,
пользователь поменял сам — не трогаем, новая сессия во время записи, свои и системные
сессии, восстановление из duck.json после «падения», ошибки COM.

Настоящий Core Audio — только на СОБСТВЕННОЙ тестовой сессии: отдельный процесс играет
тишину (sounddevice), а Ducker видит только сессии этого процесса (OnlyPid). Чужие сессии
здесь не меняются никогда — только перечисляются (duck.selftest: число, без имён). Нет
устройства вывода — тест пропускается."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator, TypeVar

import pytest

from rupor import duck
from rupor.duck import Ducker

OWN_UI = 70_000          # pid окна Рупора (own_pids)
T = TypeVar("T")


# ---------- фейки ----------

class FakeSession:
    def __init__(self, app: str, pid: int, vol: float = 1.0, muted: bool = False,
                 active: bool = True, system: bool = False, dev: str = "dev",
                 default: bool = True) -> None:
        # идентификаторы как у Windows: устройство + приложение (+ pid у экземпляра)
        self.session = f"{{{dev}}}|\\Device\\HarddiskVolume3\\{app}.exe%b{{00000000}}"
        self.instance = f"{self.session}|1%b{pid}"
        self.pid, self.active, self.system, self.default = pid, active, system, default
        self.vol, self.mute = duck._f32(vol), muted
        self.fail_read = False
        self.fail_set = False
        self.sets = 0
        self.on_set: Callable[[], None] = lambda: None

    def volume(self) -> float:
        if self.fail_read:
            raise OSError("COM: сессия недоступна")
        return self.vol

    def muted(self) -> bool:
        if self.fail_read:
            raise OSError("COM: сессия недоступна")
        return self.mute

    def set_volume(self, v: float) -> None:
        self.on_set()
        if self.fail_set:
            raise OSError("COM: E_FAIL")
        self.sets += 1
        self.vol = duck._f32(v)

    def set_muted(self, m: bool) -> None:
        self.on_set()
        if self.fail_set:
            raise OSError("COM: E_FAIL")
        self.sets += 1
        self.mute = m


class FakeBackend:
    def __init__(self, *sessions: FakeSession) -> None:
        self.list = list(sessions)
        self.fail = False
        self.block: threading.Event | None = None

    def sessions(self) -> list[Any]:
        if self.block is not None:
            self.block.wait(10)
        if self.fail:
            raise OSError("COM: служба звука недоступна")
        return list(self.list)


@pytest.fixture
def fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(duck, "POLL_S", 0.02)
    monkeypatch.setattr(duck, "PENDING_POLL_S", 0.02)


def make(tmp_path: Path, backend: Any, **kw: Any) -> Ducker:
    d = Ducker(own_pids=(OWN_UI,), path=tmp_path / "duck.json",
               backend_factory=lambda: backend, **kw)
    d.start()
    assert d.flush()
    return d


def entries(tmp_path: Path) -> list[dict[str, Any]]:
    p = tmp_path / "duck.json"
    if not p.exists():
        return []
    data: dict[str, Any] = json.loads(p.read_text(encoding="utf-8"))
    return list(data["entries"])


def crash(d: Ducker) -> None:
    """«Падение» ядра: поток останавливается, ничего не возвращая (duck.json остаётся)."""
    d._restore = lambda: None       # type: ignore[method-assign]
    assert d.close(5)


def wait_until(pred: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not pred():
        if time.monotonic() > deadline:
            raise TimeoutError("условие не выполнилось")
        time.sleep(0.01)


def approx(a: float, b: float) -> bool:
    return abs(a - b) < 0.002


# ---------- приглушить и вернуть ----------

def test_duck_and_restore(tmp_path: Path) -> None:
    music = FakeSession("spotify", 101, vol=1.0)
    call = FakeSession("chrome", 102, vol=0.6)
    d = make(tmp_path, FakeBackend(music, call))
    d.duck("duck", 15)
    assert d.flush()
    assert approx(music.vol, 0.15) and approx(call.vol, 0.09)
    assert not music.mute and not call.mute
    saved = entries(tmp_path)
    assert {e["pid"] for e in saved} == {101, 102}
    e = next(x for x in saved if x["pid"] == 101)
    assert e["volume"] == 1.0 and e["muted"] is False and approx(e["set_volume"], 0.15)
    assert e["instance"] == music.instance and e["session"] == music.session
    d.restore()
    assert d.flush()
    assert music.vol == 1.0 and approx(call.vol, 0.6)
    assert not (tmp_path / "duck.json").exists()
    assert d.close()


def test_mute_mode_mutes_and_unmutes(tmp_path: Path) -> None:
    music = FakeSession("spotify", 101, vol=0.7)
    d = make(tmp_path, FakeBackend(music))
    d.duck("mute", 15)
    assert d.flush()
    assert music.mute is True and approx(music.vol, 0.7)   # громкость не трогаем
    d.restore()
    assert d.flush()
    assert music.mute is False and approx(music.vol, 0.7)
    d.close()


def test_level_zero_and_hundred(tmp_path: Path) -> None:
    a = FakeSession("a", 101, vol=0.8)
    d = make(tmp_path, FakeBackend(a))
    d.duck("duck", 0)
    assert d.flush()
    assert a.vol == 0.0 and a.mute is False       # 0 % — громкость 0, не беззвук
    d.restore()
    d.duck("duck", 100)                           # 100 % — трогать нечего
    assert d.flush()
    assert approx(a.vol, 0.8) and a.sets == 2
    d.restore()
    assert d.flush() and not (tmp_path / "duck.json").exists()
    d.close()


def test_off_mode_restores(tmp_path: Path) -> None:
    a = FakeSession("a", 101)
    d = make(tmp_path, FakeBackend(a))
    d.duck("duck", 15)
    d.duck("off", 15)
    assert d.flush()
    assert a.vol == 1.0
    d.close()


def test_settings_change_during_recording_reducks_from_original(tmp_path: Path) -> None:
    a = FakeSession("a", 101, vol=1.0)
    d = make(tmp_path, FakeBackend(a))
    d.duck("duck", 15)
    d.duck("duck", 40)            # ползунок во время записи: 40 % от ИСХОДНОЙ, не от 15 %
    assert d.flush()
    assert approx(a.vol, 0.4)
    d.duck("mute", 40)
    assert d.flush()
    assert a.mute is True and approx(a.vol, 1.0)
    d.restore()
    assert d.flush()
    assert a.vol == 1.0 and a.mute is False
    d.close()


def test_settings_change_keeps_sessions_user_changed(tmp_path: Path) -> None:
    """Пользователь сам поднял музыку во время записи, потом подвинул ползунок уровня или
    сменил режим — его музыку снова не приглушаем (остальное — от исходной громкости)."""
    music = FakeSession("music", 101, vol=1.0)
    call = FakeSession("teams", 102, vol=0.8)
    d = make(tmp_path, FakeBackend(music, call))
    d.duck("duck", 15)
    assert d.flush()
    music.vol = duck._f32(0.7)
    d.duck("duck", 30)
    assert d.flush()
    assert approx(music.vol, 0.7) and approx(call.vol, 0.24)
    d.duck("mute", 30)
    assert d.flush()
    assert approx(music.vol, 0.7) and music.mute is False
    assert call.mute is True and approx(call.vol, 0.8)
    d.restore()
    assert d.flush()
    assert approx(music.vol, 0.7) and music.mute is False
    assert approx(call.vol, 0.8) and call.mute is False
    d.duck("duck", 15)                             # следующая запись — снова всё
    assert d.flush()
    assert approx(music.vol, 0.105)
    d.close()
    assert approx(music.vol, 0.7)


def test_level_change_in_mute_mode_changes_nothing(tmp_path: Path) -> None:
    a = FakeSession("a", 101, vol=0.9)
    d = make(tmp_path, FakeBackend(a))
    d.duck("mute", 15)
    assert d.flush() and a.mute is True
    sets = a.sets
    d.duck("mute", 40)             # уровень в режиме «выключать» ни при чём
    assert d.flush()
    assert a.sets == sets and a.mute is True       # звук не «прорывается» на миг
    d.restore()
    assert d.flush()
    assert a.mute is False and approx(a.vol, 0.9)
    d.close()


def test_level_raised_from_hundred_during_recording_ducks(tmp_path: Path) -> None:
    """При 100 % трогать нечего; ползунок сдвинули во время записи — теперь приглушаем."""
    a = FakeSession("a", 101, vol=1.0)
    d = make(tmp_path, FakeBackend(a))
    d.duck("duck", 100)
    assert d.flush() and a.sets == 0
    d.duck("duck", 40)
    assert d.flush()
    assert approx(a.vol, 0.4)
    d.restore()
    assert d.flush() and a.vol == 1.0
    d.close()


# ---------- что не трогаем ----------

def test_own_system_muted_and_quiet_sessions_untouched(tmp_path: Path) -> None:
    core = FakeSession("rupor-core", os.getpid())            # звуки начала/конца записи
    ui = FakeSession("rupor", OWN_UI)
    system = FakeSession("system", 0, system=True)
    system2 = FakeSession("systemsounds", 55, system=True)   # IsSystemSoundsSession
    muted = FakeSession("muted", 103, vol=0.9, muted=True)
    silent = FakeSession("silent", 104, vol=0.0)
    idle = FakeSession("paused", 105, active=False)          # не играет
    music = FakeSession("music", 106)
    all_ = [core, ui, system, system2, muted, silent, idle, music]
    d = make(tmp_path, FakeBackend(*all_))
    d.duck("duck", 15)
    assert d.flush()
    assert approx(music.vol, 0.15)
    for s in all_[:-1]:
        assert s.sets == 0, s.pid
    d.duck("mute", 15)
    assert d.flush()
    assert muted.sets == 0 and core.sets == 0 and system.sets == 0 and idle.sets == 0
    d.restore()
    assert d.flush()
    assert [e for e in entries(tmp_path)] == []
    d.close()


# ---------- исключённое дерево процессов (перевод встречи) ----------

# brave(200) → brave(201, служба звука) → brave(202, вкладка); spotify(300) — сам по себе
PARENTS = {201: 200, 202: 201, 200: 10, 300: 10, 10: 4}


def test_excluded_tree_is_not_ducked(tmp_path: Path) -> None:
    meet = FakeSession("brave", 201)             # звук браузера играет дочерний процесс
    tab = FakeSession("brave", 202, vol=0.8)     # внук корня — тоже его дерево
    music = FakeSession("spotify", 300)
    d = make(tmp_path, FakeBackend(meet, tab, music), parent_of=PARENTS.get)
    d.set_excluded(frozenset({200}))
    d.duck("duck", 15)
    assert d.flush()
    assert meet.vol == 1.0 and approx(tab.vol, 0.8) and meet.sets == tab.sets == 0
    assert approx(music.vol, 0.15)
    assert [e["pid"] for e in entries(tmp_path)] == [300]
    d.restore()
    assert d.flush()
    assert music.vol == 1.0 and meet.sets == 0
    d.close()


def test_exclusion_during_recording_restores_meeting_at_once(tmp_path: Path) -> None:
    meet = FakeSession("brave", 201, vol=0.9)
    music = FakeSession("spotify", 300)
    d = make(tmp_path, FakeBackend(meet, music), parent_of=PARENTS.get)
    d.duck("duck", 15)
    assert d.flush()
    assert approx(meet.vol, 0.135) and approx(music.vol, 0.15)
    d.set_excluded({200, 0})                     # встреча началась посреди диктовки
    assert d.flush()
    assert approx(meet.vol, 0.9) and approx(music.vol, 0.15)
    assert [e["pid"] for e in entries(tmp_path)] == [300] and d.changed == 1
    d.duck("duck", 15)                           # переопрос: встречу снова не приглушаем
    assert d.flush()
    assert approx(meet.vol, 0.9)
    d.set_excluded(frozenset())                  # встреча закончилась — как обычно
    d.duck("duck", 15)
    assert d.flush()
    assert approx(meet.vol, 0.135)
    d.restore()
    assert d.flush()
    assert approx(meet.vol, 0.9) and music.vol == 1.0
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_exclude_everything_for_whole_sound_meeting(tmp_path: Path) -> None:
    """Встреча слушает весь звук компьютера: во время диктовки не приглушается ничего, а
    уже приглушённое (встреча началась посреди диктовки) возвращается сразу."""
    meet = FakeSession("zoom", 400, vol=0.9)
    music = FakeSession("spotify", 300)
    d = make(tmp_path, FakeBackend(meet, music), parent_of=PARENTS.get)
    d.duck("mute", 15)
    assert d.flush()
    assert meet.mute and music.mute
    d.set_excluded(frozenset(), everything=True)    # встреча «весь звук» началась
    assert d.flush()
    assert not meet.mute and not music.mute and approx(meet.vol, 0.9)
    assert entries(tmp_path) == [] and d.changed == 0
    d.duck("duck", 15)                              # следующая диктовка — ничего не трогать
    assert d.flush()
    assert approx(meet.vol, 0.9) and music.vol == 1.0 and not music.mute
    d.restore()
    d.set_excluded(frozenset())                     # встреча кончилась — как обычно
    d.duck("duck", 15)
    assert d.flush()
    assert approx(music.vol, 0.15) and approx(meet.vol, 0.135)
    d.restore()
    assert d.flush()
    assert music.vol == 1.0 and approx(meet.vol, 0.9)
    d.close()


def test_exclusion_leaves_what_user_changed(tmp_path: Path) -> None:
    meet = FakeSession("brave", 201)
    d = make(tmp_path, FakeBackend(meet), parent_of=PARENTS.get)
    d.duck("duck", 15)
    assert d.flush()
    meet.vol = duck._f32(0.4)                    # пользователь сам подвинул ползунок
    d.set_excluded(frozenset({200}))
    assert d.flush()
    assert approx(meet.vol, 0.4) and entries(tmp_path) == []
    d.restore()
    assert d.flush()
    assert approx(meet.vol, 0.4)
    d.close()


def test_exclusion_parent_lookup_errors_and_cache(tmp_path: Path,
                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[int] = []

    def parent_of(pid: int) -> int | None:
        calls.append(pid)
        if pid == 201:
            raise OSError("нет доступа к процессу")
        return PARENTS.get(pid)

    tab = FakeSession("brave", 202)              # цепочка обрывается на 201 — не исключён
    music = FakeSession("spotify", 300)
    d = make(tmp_path, FakeBackend(tab, music), parent_of=parent_of)
    d.set_excluded(frozenset({200}))
    d.duck("duck", 15)
    assert d.flush()
    assert approx(tab.vol, 0.15) and approx(music.vol, 0.15)
    d.restore()
    assert d.flush()
    n = len(calls)
    d.duck("duck", 15)
    assert d.flush()
    assert len(calls) == n                       # решения по pid помнятся
    monkeypatch.setattr(duck, "EXCLUDE_TTL_S", 0.0)
    d.restore()
    d.duck("duck", 15)
    assert d.flush()
    assert len(calls) > n
    d.restore()
    assert d.flush()
    assert tab.vol == 1.0 and music.vol == 1.0
    d.close()


def test_exclusion_default_parent_lookup_is_real() -> None:
    d = Ducker(path=Path(os.devnull))
    d._excluded = frozenset({os.getppid()})
    assert d._is_excluded(os.getpid())           # этот процесс — потомок своего родителя
    assert not d._is_excluded(0xFFFFFFF0)        # такого процесса нет
    d._excluded = frozenset()
    assert not d._is_excluded(os.getpid())


def test_user_changed_session_during_recording_is_left_alone(tmp_path: Path) -> None:
    music = FakeSession("music", 101, vol=1.0)
    call = FakeSession("teams", 102, vol=0.8)
    d = make(tmp_path, FakeBackend(music, call))
    d.duck("duck", 15)
    assert d.flush()
    music.vol = duck._f32(0.5)        # пользователь сам поднял музыку во время записи
    d.duck("duck", 15)                # переопрос (как по таймеру)
    assert d.flush()
    assert approx(music.vol, 0.5)     # снова не приглушаем
    assert [e["pid"] for e in entries(tmp_path)] == [102]
    d.restore()
    assert d.flush()
    assert approx(music.vol, 0.5) and approx(call.vol, 0.8)
    d.close()


def test_user_change_just_before_stop_is_not_reverted(tmp_path: Path) -> None:
    music = FakeSession("music", 101, vol=1.0)
    d = make(tmp_path, FakeBackend(music))
    d.duck("mute", 15)
    assert d.flush()
    music.mute = False                # снял беззвук сам, переопроса ещё не было
    music.vol = duck._f32(0.3)
    d.restore()
    assert d.flush()
    assert music.mute is False and approx(music.vol, 0.3)
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_mute_mode_user_moved_volume_is_still_unmuted(tmp_path: Path) -> None:
    """«Выключать»: пользователь во время записи подвинул громкость выключенного плеера, а
    беззвук не снимал — беззвук наш, его всё равно снимаем; громкость — его."""
    vlc = FakeSession("vlc", 101, vol=0.8)
    d = make(tmp_path, FakeBackend(vlc))
    d.duck("mute", 15)
    assert d.flush() and vlc.mute is True
    vlc.vol = duck._f32(0.6)
    d.duck("mute", 15)                  # переопрос (как по таймеру)
    assert d.flush()
    [e] = entries(tmp_path)
    assert e["mute_ours"] is True and e["vol_ours"] is False
    d.restore()
    assert d.flush()
    assert vlc.mute is False and approx(vlc.vol, 0.6)
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_duck_mode_user_muted_volume_is_still_restored(tmp_path: Path) -> None:
    """«Приглушать»: пользователь во время записи выключил звук приглушённой программы —
    беззвук его (не снимаем), а громкость наша — возвращаем, иначе после включения звука
    программа играла бы на 15 % (и Windows это запомнила бы)."""
    spotify = FakeSession("spotify", 101, vol=1.0)
    d = make(tmp_path, FakeBackend(spotify))
    d.duck("duck", 15)
    assert d.flush()
    spotify.mute = True
    d.duck("duck", 15)                  # переопрос
    assert d.flush()
    d.restore()
    assert d.flush()
    assert spotify.mute is True and spotify.vol == 1.0
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_new_session_during_recording_is_ducked(tmp_path: Path, fast: None) -> None:
    music = FakeSession("music", 101)
    be = FakeBackend(music)
    d = make(tmp_path, be)
    d.duck("duck", 15)
    assert d.flush()
    video = FakeSession("chrome", 102, vol=0.5)
    be.list.append(video)                          # заиграло во время записи
    wait_until(lambda: approx(video.vol, 0.075))   # переопрос раз в POLL_S
    paused = FakeSession("player", 103, active=False)
    be.list.append(paused)
    time.sleep(0.1)
    assert paused.sets == 0                        # не играет — не трогаем
    paused.active = True                           # нажали «играть»
    wait_until(lambda: approx(paused.vol, 0.15))
    d.restore()
    assert d.flush()
    assert music.vol == 1.0 and approx(video.vol, 0.5) and paused.vol == 1.0
    d.close()


def test_rdp_session_is_ducked_like_any_other(tmp_path: Path) -> None:
    mstsc = FakeSession("mstsc", 4321, vol=1.0)
    d = make(tmp_path, FakeBackend(mstsc))
    d.duck("duck", 20)
    assert d.flush()
    assert approx(mstsc.vol, 0.2)
    d.close()                                      # выход тоже возвращает звук
    assert mstsc.vol == 1.0


def test_default_device_changed_during_recording(tmp_path: Path) -> None:
    """Во время записи подключили наушники, и Windows сделала их устройством по умолчанию:
    плеер на новом устройстве приглушается, а на колонках (уже не по умолчанию) — всё равно
    возвращается. Приложение, которому явно назначено другое устройство, не трогаем."""
    speakers = FakeSession("spotify", 101, vol=0.8, dev="speakers")
    discord = FakeSession("discord", 102, vol=1.0, dev="headset", default=False)
    be = FakeBackend(speakers, discord)
    d = make(tmp_path, be)
    d.duck("duck", 15)
    assert d.flush()
    assert approx(speakers.vol, 0.12) and discord.sets == 0
    speakers.default = False
    phones = FakeSession("spotify", 101, vol=1.0, dev="phones")   # тот же процесс
    be.list.append(phones)
    d.duck("duck", 15)                             # переопрос
    assert d.flush()
    assert approx(phones.vol, 0.15)
    d.restore()
    assert d.flush()
    assert approx(speakers.vol, 0.8) and phones.vol == 1.0 and discord.sets == 0
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_restore_after_crash_on_non_default_device(tmp_path: Path) -> None:
    """Упали посреди записи, а к следующему запуску устройство по умолчанию другое: сессия
    на прежнем (активном) устройстве всё равно возвращается."""
    speakers = FakeSession("spotify", 101, vol=0.8, dev="speakers")
    be = FakeBackend(speakers)
    d1 = make(tmp_path, be)
    d1.duck("mute", 15)
    assert d1.flush()
    crash(d1)
    speakers.default = False
    d2 = make(tmp_path, be)
    assert speakers.mute is False
    assert not (tmp_path / "duck.json").exists()
    d2.close()


def test_file_is_not_rewritten_without_changes(tmp_path: Path, fast: None) -> None:
    """Переопрос во время записи и ожидание закрывшегося приложения (раз в PENDING_POLL_S,
    до недели) не переписывают duck.json, пока записи те же."""
    music = FakeSession("spotify", 101, vol=0.8)
    be = FakeBackend(music)
    d = make(tmp_path, be)
    d.duck("duck", 15)
    assert d.flush()
    n = d.file_writes
    time.sleep(0.2)                                # ~10 переопросов записи
    assert d.flush() and d.file_writes == n
    be.list = []                                   # закрыли плеер во время записи
    d.restore()
    assert d.flush()
    assert len(entries(tmp_path)) == 1
    n = d.file_writes
    stamp = (tmp_path / "duck.json").stat().st_mtime_ns
    time.sleep(0.3)                                # ~15 проверок ожидающей записи
    assert d.flush() and d.file_writes == n
    assert (tmp_path / "duck.json").stat().st_mtime_ns == stamp
    d.close()
    assert d.file_writes == n and len(entries(tmp_path)) == 1   # ждёт следующий запуск


def test_file_is_written_before_volume_changes(tmp_path: Path) -> None:
    music = FakeSession("music", 101)
    seen: list[int] = []
    music.on_set = lambda: seen.append(len(entries(tmp_path)))
    d = make(tmp_path, FakeBackend(music))
    d.duck("duck", 15)
    assert d.flush()
    assert seen == [1]             # падение сразу после изменения — запись уже в файле
    d.close()


# ---------- восстановление после падения ----------

def test_restore_after_crash_on_next_start(tmp_path: Path) -> None:
    music = FakeSession("music", 101, vol=0.9)
    call = FakeSession("zoom", 102, vol=1.0)
    be = FakeBackend(music, call)
    d1 = make(tmp_path, be)
    d1.duck("mute", 15)
    assert d1.flush()
    crash(d1)
    assert music.mute and call.mute and len(entries(tmp_path)) == 2
    d2 = make(tmp_path, be)                         # новый запуск ядра
    assert music.mute is False and call.mute is False
    assert approx(music.vol, 0.9)
    assert not (tmp_path / "duck.json").exists()
    d2.close()


def test_restore_after_crash_skips_sessions_user_changed(tmp_path: Path) -> None:
    music = FakeSession("music", 101, vol=1.0)
    call = FakeSession("zoom", 102, vol=1.0)
    be = FakeBackend(music, call)
    d1 = make(tmp_path, be)
    d1.duck("duck", 15)
    assert d1.flush()
    crash(d1)
    music.vol = duck._f32(0.6)       # заметил тихую музыку и поднял сам
    d2 = make(tmp_path, be)
    assert approx(music.vol, 0.6) and call.vol == 1.0
    assert not (tmp_path / "duck.json").exists()
    d2.duck("duck", 15)              # следующая запись приглушает и её (от 0,6)
    assert d2.flush()
    assert approx(music.vol, 0.09) and approx(call.vol, 0.15)
    d2.close()
    assert approx(music.vol, 0.6) and call.vol == 1.0


def test_restore_after_crash_returns_our_part_only(tmp_path: Path) -> None:
    """«Выключать», пользователь подвинул громкость выключенного плеера, ядро упало: при
    следующем запуске беззвук снимается, громкость остаётся пользовательской."""
    vlc = FakeSession("vlc", 101, vol=0.9)
    be = FakeBackend(vlc)
    d1 = make(tmp_path, be)
    d1.duck("mute", 15)
    assert d1.flush()
    crash(d1)
    vlc.vol = duck._f32(0.5)
    d2 = make(tmp_path, be)
    assert vlc.mute is False and approx(vlc.vol, 0.5)
    assert not (tmp_path / "duck.json").exists()
    d2.close()


def test_file_of_first_version_is_restored(tmp_path: Path) -> None:
    """duck.json без vol_ours/mute_ours (первая версия): наше — то, что меняли."""
    music = FakeSession("music", 101, vol=0.15)
    vlc = FakeSession("vlc", 102, vol=0.7, muted=True)
    old = [{"instance": music.instance, "session": music.session, "pid": 101, "volume": 1.0,
            "muted": False, "set_volume": 0.15, "set_muted": False, "ts": time.time()},
           {"instance": vlc.instance, "session": vlc.session, "pid": 102, "volume": 0.7,
            "muted": False, "set_volume": 0.7, "set_muted": True, "ts": time.time()},
           {"instance": "x|1%b5", "session": "x", "pid": 5, "volume": 0.5, "muted": False,
            "set_volume": 0.5, "set_muted": False, "ts": time.time()}]  # менять нечего
    (tmp_path / "duck.json").write_text(json.dumps({"version": 1, "entries": old}),
                                        encoding="utf-8")
    d = make(tmp_path, FakeBackend(music, vlc))
    assert music.vol == 1.0 and vlc.mute is False and approx(vlc.vol, 0.7)
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_app_restarted_after_crash_gets_its_volume_back(tmp_path: Path, fast: None) -> None:
    """Выключение компьютера во время записи: после перезагрузки процессы новые, но Windows
    отдаёт приложению запомненную (приглушённую) громкость — её надо вернуть, когда оно
    запустится, даже если позже ядра."""
    music = FakeSession("spotify", 101, vol=0.8)
    video = FakeSession("chrome", 102, vol=1.0)
    other = FakeSession("vlc", 103, vol=1.0)
    be = FakeBackend(music, video, other)
    d1 = make(tmp_path, be)
    d1.duck("duck", 15)
    assert d1.flush()
    crash(d1)
    # перезагрузка: spotify уже запущен (новый pid, запомненная громкость 12 %),
    # chrome и vlc — ещё нет
    music2 = FakeSession("spotify", 201, vol=music.vol)
    be.list = [music2]
    d2 = make(tmp_path, be)
    assert approx(music2.vol, 0.8)
    assert sorted(e["pid"] for e in entries(tmp_path)) == [102, 103]   # ждут своё приложение
    video2 = FakeSession("chrome", 202, vol=video.vol)   # запустили chrome — громкость 15 %
    other2 = FakeSession("vlc", 203, vol=0.42)           # Windows не запомнила — не наше
    be.list += [video2, other2]
    wait_until(lambda: not (tmp_path / "duck.json").exists())
    assert video2.vol == 1.0 and approx(other2.vol, 0.42)
    d2.close()


def test_app_closed_during_recording_is_restored_when_it_starts_again(
        tmp_path: Path, fast: None) -> None:
    music = FakeSession("spotify", 101, vol=0.8)
    be = FakeBackend(music)
    d = make(tmp_path, be)
    d.duck("duck", 15)
    assert d.flush()
    be.list = []                                   # закрыли плеер, пока шла запись
    d.restore()
    assert d.flush()
    assert [e["pid"] for e in entries(tmp_path)] == [101]
    music2 = FakeSession("spotify", 301, vol=music.vol)   # открыли снова — с 12 %
    be.list = [music2]
    wait_until(lambda: approx(music2.vol, 0.8))
    wait_until(lambda: not (tmp_path / "duck.json").exists())
    d.close()


def test_app_restarted_during_recording_stays_ours(tmp_path: Path) -> None:
    music = FakeSession("spotify", 101, vol=1.0)
    be = FakeBackend(music)
    d = make(tmp_path, be)
    d.duck("duck", 15)
    assert d.flush()
    music2 = FakeSession("spotify", 102, vol=music.vol)   # перезапустился с нашей громкостью
    be.list = [music2]
    d.duck("duck", 15)
    assert d.flush()
    assert approx(music2.vol, 0.15) and music2.sets == 0   # не приглушён повторно (0,15 × 0,15)
    d.restore()
    assert d.flush()
    assert music2.vol == 1.0
    assert not (tmp_path / "duck.json").exists()
    d.close()


def test_old_pending_entries_expire(tmp_path: Path) -> None:
    old = {"instance": "x|1%b5", "session": "x", "pid": 5, "volume": 1.0, "muted": False,
           "set_volume": 0.15, "set_muted": False, "ts": time.time() - duck.PENDING_TTL_S - 5}
    (tmp_path / "duck.json").write_text(json.dumps({"version": 1, "entries": [old]}),
                                        encoding="utf-8")
    d = make(tmp_path, FakeBackend())
    assert not (tmp_path / "duck.json").exists()
    d.close()


@pytest.mark.parametrize("content", ["не json", "[]", '{"entries": 5}',
                                     '{"entries": [{"instance": 1}, 7, null]}'])
def test_broken_file_is_ignored(tmp_path: Path, content: str) -> None:
    (tmp_path / "duck.json").write_text(content, encoding="utf-8")
    a = FakeSession("a", 101)
    d = make(tmp_path, FakeBackend(a))
    assert not (tmp_path / "duck.json").exists()      # битый файл убран
    d.duck("duck", 15)
    assert d.flush()
    assert approx(a.vol, 0.15)
    d.close()
    assert a.vol == 1.0


# ---------- ошибки Core Audio не мешают записи ----------

def test_com_errors_do_not_break_anything(tmp_path: Path) -> None:
    good = FakeSession("good", 101)
    broken = FakeSession("broken", 102)
    broken.fail_set = True
    unreadable = FakeSession("gone", 103)
    unreadable.fail_read = True
    be = FakeBackend(good, broken, unreadable)
    d = make(tmp_path, be)
    d.duck("duck", 15)
    assert d.flush()
    assert approx(good.vol, 0.15) and broken.vol == 1.0
    assert [e["pid"] for e in entries(tmp_path)] == [101]
    be.fail = True                   # служба звука перезапускается
    d.duck("duck", 15)
    d.restore()
    assert d.flush()
    assert approx(good.vol, 0.15)    # не смогли — запись осталась, вернём, когда сможем
    assert [e["pid"] for e in entries(tmp_path)] == [101]
    be.fail = False
    d.restore()
    assert d.flush()
    assert good.vol == 1.0 and not (tmp_path / "duck.json").exists()
    d.close()


def test_backend_unavailable(tmp_path: Path) -> None:
    def broken() -> duck.Backend:
        raise OSError("CoCreateInstance: класс не зарегистрирован")

    d = Ducker(path=tmp_path / "duck.json", backend_factory=broken)
    d.start()
    d.duck("duck", 15)
    d.restore()
    assert d.flush() and d.close()


def test_core_audio_recreated_after_failure(tmp_path: Path) -> None:
    """Служба звука перезапустилась: старые COM-объекты мертвы — создаём Core Audio заново."""
    a = FakeSession("a", 101)
    dead = FakeBackend(a)
    fresh = FakeBackend(a)
    made: list[FakeBackend] = []
    ok = [False]

    def factory() -> duck.Backend:
        if not ok[0]:
            raise OSError("служба звука не запущена")
        b = dead if not made else fresh
        made.append(b)
        return b

    d = Ducker(path=tmp_path / "duck.json", backend_factory=factory)
    d.start()
    d.duck("duck", 15)                  # Core Audio ещё нет — запись идёт без приглушения
    assert d.flush() and a.vol == 1.0
    d.restore()
    ok[0] = True
    d.duck("duck", 15)                  # служба поднялась — создаём
    assert d.flush() and approx(a.vol, 0.15) and made == [dead]
    dead.fail = True                    # объекты умерли
    d.duck("duck", 15)
    d.restore()                         # перебор не удался → при следующем — заново
    d.restore()
    assert d.flush()
    assert made == [dead, fresh] and a.vol == 1.0
    d.close()


def test_hung_core_audio_does_not_block_callers(tmp_path: Path) -> None:
    be = FakeBackend(FakeSession("a", 101))
    d = make(tmp_path, be)
    be.block = threading.Event()        # вызов Core Audio завис
    t0 = time.monotonic()
    d.duck("duck", 15)
    d.restore()
    d.duck("mute", 15)
    assert time.monotonic() - t0 < 0.1  # движок (старт записи, хук) не ждёт
    assert d.close(0.2) is False        # выход не ждёт дольше таймаута
    be.block.set()


# ---------- настоящий Core Audio: только своя тестовая сессия ----------

def in_com_thread(fn: Callable[[], T], timeout: float = 15.0) -> T:
    """Выполнить в отдельном потоке (COM MTA там, а не в главном потоке pytest)."""
    box: dict[str, Any] = {}

    def run() -> None:
        try:
            box["r"] = fn()
        except BaseException as e:  # noqa: BLE001
            box["e"] = e

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout)
    if "e" in box:
        raise box["e"]
    if "r" not in box:
        raise TimeoutError("Core Audio не ответил")
    result: T = box["r"]
    return result


class OnlyPid:
    """Настоящий Core Audio, но Ducker видит ТОЛЬКО сессии тестового процесса."""

    def __init__(self, pid: int) -> None:
        self.pid = pid
        self.real = duck.CoreAudioBackend()

    def sessions(self) -> list[Any]:
        got = [s for s in self.real.sessions() if s.pid == self.pid]
        assert all(s.pid == self.pid for s in got)
        return got


def own_state(pid: int) -> tuple[float, bool] | None:
    def read() -> tuple[float, bool] | None:
        ss = [s for s in OnlyPid(pid).sessions() if s.active]
        return (ss[0].volume(), ss[0].muted()) if ss else None
    return in_com_thread(read)


def set_own(pid: int, vol: float, muted: bool) -> None:
    def write() -> None:
        for s in OnlyPid(pid).sessions():
            s.set_volume(vol)
            s.set_muted(muted)
    in_com_thread(write)


SILENCE = r'''
import os, sys
import sounddevice as sd

def cb(outdata, frames, t, status):
    outdata.fill(0)

with sd.OutputStream(samplerate=48000, channels=1, dtype="float32", callback=cb):
    print(os.getpid(), flush=True)
    sys.stdin.read()
'''


@pytest.fixture
def own_player() -> Iterator[int]:
    """Свой процесс, играющий тишину (своя аудиосессия); pid — самого интерпретатора
    (в .venv python.exe — запускатель, звук играет его дочерний процесс)."""
    try:
        import sounddevice as sd
        sd.query_devices(kind="output")
    except Exception as e:
        pytest.skip(f"нет устройства вывода: {type(e).__name__}")
    ok, summary = duck.selftest()
    if not ok or "устройства вывода нет" in summary:
        pytest.skip(f"Core Audio без устройства вывода ({summary})")
    proc = subprocess.Popen([sys.executable, "-c", SILENCE], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        assert proc.stdout is not None
        line = in_com_thread(proc.stdout.readline, 20)
        if not line.strip().isdigit():
            pytest.skip("тестовый процесс не открыл вывод звука")
        pid = int(line)
        deadline = time.monotonic() + 10
        while own_state(pid) is None:
            if time.monotonic() > deadline:
                pytest.skip("сессия тестового процесса не появилась")
            time.sleep(0.1)
        on_default = in_com_thread(
            lambda: any(s.default for s in OnlyPid(pid).sessions() if s.active))
        if not on_default:
            pytest.skip("тестовый процесс играет не на устройстве вывода по умолчанию")
        yield pid
    finally:
        try:
            assert proc.stdin is not None
            proc.stdin.close()
            proc.wait(10)
        except Exception:
            proc.kill()


def test_real_core_audio_on_own_session(tmp_path: Path, own_player: int) -> None:
    pid = own_player
    start = own_state(pid)
    assert start is not None
    vol0, mute0 = start
    if mute0 or vol0 < 0.2:
        pytest.skip("у тестовой сессии и так тихо — проверять нечего")

    def ducker() -> Ducker:
        d = Ducker(path=tmp_path / "duck.json", backend_factory=lambda: OnlyPid(pid))
        d.start()
        assert d.flush(15)
        return d

    try:
        d = ducker()
        d.duck("duck", 15)
        assert d.flush(15)
        st = own_state(pid)
        assert st is not None and abs(st[0] - vol0 * 0.15) < 0.01 and st[1] == mute0
        assert [e["pid"] for e in entries(tmp_path)] == [pid]
        d.restore()
        assert d.flush(15)
        st = own_state(pid)
        assert st is not None and abs(st[0] - vol0) < 0.01
        assert not (tmp_path / "duck.json").exists()

        d.duck("mute", 15)
        assert d.flush(15)
        st = own_state(pid)
        assert st is not None and st[1] is True and abs(st[0] - vol0) < 0.01
        d.restore()
        assert d.flush(15)
        assert own_state(pid) == (pytest.approx(vol0, abs=0.01), False)

        # «пользователь» поменял громкость во время записи — не возвращаем
        d.duck("duck", 15)
        assert d.flush(15)
        set_own(pid, vol0 * 0.5, False)
        d.restore()
        assert d.flush(15)
        st = own_state(pid)
        assert st is not None and abs(st[0] - vol0 * 0.5) < 0.01
        set_own(pid, vol0, mute0)

        # «падение» ядра во время записи → следующий запуск возвращает громкость
        d.duck("duck", 15)
        assert d.flush(15)
        crash(d)
        st = own_state(pid)
        assert st is not None and abs(st[0] - vol0 * 0.15) < 0.01
        d2 = ducker()
        st = own_state(pid)
        assert st is not None and abs(st[0] - vol0) < 0.01
        assert not (tmp_path / "duck.json").exists()
        d2.close()
    finally:
        set_own(pid, vol0, mute0)     # запомненная Windows громкость python.exe — как была


def test_selftest_counts_sessions_without_names() -> None:
    ok, summary = duck.selftest()       # только перечисляет и читает, ничего не меняет
    assert summary.startswith("Core Audio: ")
    if ok:
        assert summary.startswith(("Core Audio: ok, сессий ", "Core Audio: ok, устройства"))
        assert ".exe" not in summary and "\\" not in summary
