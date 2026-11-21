"""Встреча замечается сама (rupor.meeting_detect и движок): журнал микрофона Windows (фейковый
winreg), сопоставление записи с процессами (фейковая таблица процессов: браузер, новый Teams из
Store, зависшие записи), правило «микрофон ≥ 10 с + играющий звук в дереве», одно предложение
на разговор, ignore-список и свои exe, название встречи по заголовкам, выбор источника «auto»
по микрофону, подсказка «встреча, похоже, закончилась»; по WebSocket — событие
meeting_suggest и команда meeting_suggest_answer.

Настоящий реестр, процессы, окна и аудиосессии пользователя не читаются (tests/conftest.py
подменяет read_mic_usage; здесь всё — фейки), звук не захватывается (FakeStream)."""
from __future__ import annotations

import sys
from collections import Counter
from typing import Any, Callable

import pytest

from rupor import config, meeting_detect
from rupor.meeting_detect import (HINT_ENDED, MeetingDetector, MicUse, eligible, map_use,
                                  meeting_title, service_of)
from rupor.meeting_detect import read_mic_usage as real_read_mic_usage
from test_api import Env, env  # noqa: F401

S = 10_000_000                       # FILETIME: тиков в секунде
FT0 = 134_000_000_000_000_000        # «давно» (запуск программ)
BRAVE = r"C:\Program Files\BraveSoftware\Brave-Browser\Application\brave.exe"
CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
ZOOM = r"C:\Users\Demo\AppData\Roaming\Zoom\bin\Zoom.exe"
TEAMS_PFN = "MSTeams_8wekyb3d8bbwe"
TEAMS = r"C:\Program Files\WindowsApps\MSTeams_25001.1_x64__8wekyb3d8bbwe\ms-teams.exe"
WEBVIEW = r"C:\Program Files (x86)\Microsoft\EdgeWebView\Application\141.0\msedgewebview2.exe"


def key_of(path: str) -> str:
    return path.replace("\\", "#")


def src(pid: int, exe: str, name: str, *, active: bool = True, meeting: bool = False,
        title: str = "") -> dict[str, Any]:
    return {"id": f"proc:{pid}", "kind": "process", "pid": pid, "exe": exe, "name": name,
            "title": title, "meeting": meeting, "active": active}


SYSTEM = {"id": "system", "kind": "system", "pid": None, "exe": "", "name": "Весь звук",
          "title": "", "meeting": False, "active": True}


class World:
    """Фейковая машина: журнал микрофона, процессы, окна, аудиосессии, часы."""

    def __init__(self) -> None:
        self.now = 1000.0                                  # monotonic
        self.ft = FT0 + 3600 * S                           # «сейчас» как FILETIME
        self.uses: list[MicUse] = []
        # brave: корень 100, дети 101 (GPU), 102 (звук), 103 (вкладка); chrome: 200, 202 (звук)
        self.table: dict[int, tuple[int, str]] = {
            1: (0, "explorer.exe"),
            100: (1, "brave.exe"), 101: (100, "brave.exe"), 102: (100, "brave.exe"),
            103: (100, "brave.exe"),
            200: (1, "chrome.exe"), 202: (200, "chrome.exe"),
            300: (1, "ms-teams.exe"), 310: (300, "msedgewebview2.exe"),
            311: (310, "msedgewebview2.exe"),
            400: (1, "Zoom.exe"),
            500: (1, "rupor-core.exe"),
        }
        self.paths: dict[int, str] = {
            **{p: BRAVE for p in (100, 101, 102, 103)}, 200: CHROME, 202: CHROME,
            300: TEAMS, 310: WEBVIEW, 311: WEBVIEW, 400: ZOOM,
            500: r"C:\Users\Demo\AppData\Local\Rupor\core\rupor-core.exe"}
        self.created: dict[int, int] = {p: FT0 for p in self.table}
        self.pfn: dict[int, str] = {300: TEAMS_PFN, 310: TEAMS_PFN, 311: TEAMS_PFN}
        self.sources: list[dict[str, Any]] = [
            src(100, "brave.exe", "Brave Browser", title="Meet - abc-defg-hij - Brave"),
            src(200, "chrome.exe", "Google Chrome", title="Music - YouTube - Google Chrome"),
            dict(SYSTEM)]
        self.sessions: list[tuple[int, bool]] | None = None   # None — из sources
        self.titles: dict[int, list[str]] = {100: ["Meet - abc-defg-hij - Brave"]}
        self.calls: Counter[str] = Counter()

    # ---- журнал микрофона ----

    def mic(self, path_or_pfn: str, *, age_s: float = 12.0, packaged: bool = False,
            stop: int = 0) -> MicUse:
        """Программа держит микрофон age_s секунд (stop ≠ 0 — уже отпустила)."""
        key = path_or_pfn if packaged else key_of(path_or_pfn)
        self.uses = [u for u in self.uses if u.key != key]
        u = MicUse(key, packaged, self.ft - int(age_s * S), stop)
        self.uses.append(u)
        return u

    def release(self, path_or_pfn: str, packaged: bool = False) -> None:
        key = path_or_pfn if packaged else key_of(path_or_pfn)
        self.uses = [MicUse(u.key, u.packaged, u.start, self.ft) if u.key == key else u
                     for u in self.uses]

    def tick(self, sec: float) -> None:
        self.now += sec
        self.ft += int(sec * S)

    # ---- подмены ----

    def reader(self, want: Callable[[str], bool] | None) -> list[MicUse]:
        self.calls["reader"] += 1
        return [u for u in self.uses if u.packaged or want is None or want(u.exe)]

    def get_table(self) -> dict[int, tuple[int, str]]:
        self.calls["table"] += 1
        return dict(self.table)

    def get_sources(self) -> list[dict[str, Any]]:
        self.calls["sources"] += 1
        return [dict(x) for x in self.sources]

    def detector(self, sources: Callable[[], list[dict[str, Any]]] | None = None,
                 own: tuple[int, ...] = ()) -> MeetingDetector:
        def sessions() -> list[tuple[int, bool]]:
            self.calls["sessions"] += 1
            return list(self.sessions or [])

        def package_of(pid: int) -> str | None:
            self.calls["pfn"] += 1
            return self.pfn.get(pid, "")

        return MeetingDetector(
            sources=sources or self.get_sources, own_pids=own,
            sessions=sessions if self.sessions is not None else None,
            reader=self.reader, table=self.get_table, path_of=lambda p: self.paths.get(p, ""),
            created=lambda p: self.created.get(p), package_of=package_of,
            titles_of=lambda pids: {p: t for p, t in self.titles.items() if p in pids},
            describe=lambda path: {BRAVE: "Brave Browser", CHROME: "Google Chrome",
                                   TEAMS: "Microsoft Teams", ZOOM: "Zoom Workplace"}.get(path, ""),
            clock=lambda: self.now, now_filetime=lambda: self.ft)


@pytest.fixture
def w() -> World:
    return World()


# ---------- журнал микрофона (фейковый winreg) ----------

class FakeKey:
    def __init__(self, subkeys: dict[str, FakeKey] | None = None,
                 values: dict[str, Any] | None = None) -> None:
        self.subkeys = subkeys or {}
        self.values = values or {}

    def __enter__(self) -> FakeKey:
        return self

    def __exit__(self, *exc: object) -> None:
        return None


class FakeWinreg:
    HKEY_CURRENT_USER = object()

    def __init__(self, root: FakeKey) -> None:
        self.root = root
        self.opened: list[str] = []

    def OpenKey(self, parent: Any, name: str) -> FakeKey:
        node = self.root if parent is self.HKEY_CURRENT_USER else parent
        for part in name.split("\\"):
            if part not in node.subkeys:
                raise FileNotFoundError(name)
            node = node.subkeys[part]
        self.opened.append(name)
        return node

    def EnumKey(self, key: FakeKey, i: int) -> str:
        names = list(key.subkeys)
        if i >= len(names):
            raise OSError("no more items")
        return names[i]

    def QueryValueEx(self, key: FakeKey, name: str) -> tuple[Any, int]:
        if name not in key.values:
            raise FileNotFoundError(name)
        return key.values[name], 11


def _registry(store: dict[str, FakeKey]) -> FakeKey:
    node = FakeKey(store)
    for part in reversed(meeting_detect.MIC_KEY.split("\\")):
        node = FakeKey({part: node})
    return node


def test_read_mic_usage_parses_both_branches(monkeypatch: pytest.MonkeyPatch) -> None:
    used = {"LastUsedTimeStart": FT0, "LastUsedTimeStop": 0}
    reg = FakeWinreg(_registry({
        "NonPackaged": FakeKey({
            key_of(BRAVE): FakeKey(values=dict(used)),
            key_of(ZOOM): FakeKey(values={"LastUsedTimeStart": FT0, "LastUsedTimeStop": FT0 + S}),
            key_of(r"C:\Tools\obs64.exe"): FakeKey(values=dict(used)),
        }),
        TEAMS_PFN: FakeKey(values=dict(used)),
        "Microsoft.WindowsCamera_8wekyb3d8bbwe": FakeKey(values={"Value": "Allow"}),
    }))
    monkeypatch.setitem(sys.modules, "winreg", reg)
    out = {u.key: u for u in real_read_mic_usage(lambda exe: exe != "obs64.exe")}
    assert set(out) == {key_of(BRAVE), key_of(ZOOM), TEAMS_PFN,
                        "Microsoft.WindowsCamera_8wekyb3d8bbwe"}
    b = out[key_of(BRAVE)]
    assert b.in_use and not b.packaged and b.path == BRAVE and b.exe == "brave.exe"
    assert b.ident == (key_of(BRAVE).casefold(), FT0)
    assert not out[key_of(ZOOM)].in_use                     # отпустил
    t = out[TEAMS_PFN]
    assert t.in_use and t.packaged and t.path == "" and t.exe == ""
    cam = out["Microsoft.WindowsCamera_8wekyb3d8bbwe"]
    assert (cam.start, cam.stop, cam.in_use) == (0, -1, False)   # ни разу не пользовалась
    # подключ отсеянного exe даже не открывался
    assert not any("obs64" in k for k in reg.opened)


def test_read_mic_usage_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "winreg", FakeWinreg(FakeKey()))
    assert real_read_mic_usage() == []


# ---------- запись → процессы ----------

def _map(w: World, u: MicUse, own: tuple[int, ...] = ()) -> tuple[str, Any]:
    return map_use(u, w.table, path_of=lambda p: w.paths.get(p, ""),
                   created=lambda p: w.created.get(p), package_of=lambda p: w.pfn.get(p, ""),
                   own_pids=own)


def test_map_browser_to_root_and_tree(w: World) -> None:
    verdict, app = _map(w, w.mic(BRAVE))
    assert verdict == "ok" and app.exe == "brave.exe" and app.root == 100
    assert app.roots == frozenset({100}) and app.tree == frozenset({100, 101, 102, 103})
    assert app.path == BRAVE


def test_map_stale_and_gone_records(w: World) -> None:
    # старый путь программы (переустановлена): процессов с этим путём нет
    old = r"C:\Users\Demo\AppData\Local\Yandex\YandexBrowser\Application\browser.exe"
    assert _map(w, w.mic(old, age_s=3600 * 24 * 200)) == ("gone", None)
    # путь тот же, но все процессы запущены позже начала записи — зависла после падения
    w.created = {p: w.ft for p in w.table}
    assert _map(w, w.mic(BRAVE, age_s=600)) == ("stale", None)
    # свой процесс
    w.created = {p: FT0 for p in w.table}
    assert _map(w, w.mic(BRAVE), own=(101,)) == ("own", None)


def test_map_packaged_app_by_package_family(w: World) -> None:
    verdict, app = _map(w, w.mic(TEAMS_PFN, packaged=True))
    assert verdict == "ok" and app.exe == "ms-teams.exe" and app.root == 300
    assert app.roots == frozenset({300, 310}) and app.tree == frozenset({300, 310, 311})


def test_eligibility() -> None:
    s = config.Settings()
    assert eligible("brave.exe", s) and eligible("Browser.exe", s) and eligible("comet.exe", s)
    assert eligible("zoom.exe", s) and eligible("ms-teams.exe", s)
    assert not eligible("rupor-core.exe", s) and not eligible("rupor.exe", s)
    assert not eligible("obs64.exe", s) and not eligible("python.exe", s)
    assert not eligible("notepad.exe", s) and not eligible("", s)
    s.meeting_detect_desktop_apps = False
    assert not eligible("zoom.exe", s) and eligible("msedge.exe", s)
    s.meeting_ignore_apps = ["msedge.exe"]
    assert not eligible("MSEdge.exe", s)


@pytest.mark.parametrize("titles,want", [
    (["Meet - abc-defg-hij - Brave"], "Google Meet"),
    (["Meet – Daily standup – Google Chrome"], "Google Meet"),
    (["Zoom Meeting"], "Zoom"),
    (["Chat | Microsoft Teams"], "Microsoft Teams"),
    (["Steamship - Wikipedia"], ""),                        # «teams» не словом
    (["Docs", "Пятничный созвон — Контур.Толк - Brave"], "Контур.Толк"),
    (["Телемост — Яндекс Браузер"], "Телемост"),
    (["New tab - Brave"], ""),
])
def test_meeting_title(titles: list[str], want: str) -> None:
    assert meeting_title(titles, config.DEFAULT_MEETING_PATTERNS) == want


def test_service_of_patterns() -> None:
    assert service_of("Meet –") == "Google Meet" and service_of("Teams") == "Microsoft Teams"
    assert service_of("Контур.Толк") == ""


# ---------- правило и одно предложение на разговор ----------

def test_suggests_once_after_ten_seconds_with_audio(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    w.mic(BRAVE, age_s=5)
    assert d.poll(s) is None and w.calls["table"] == 0      # < 10 с — даже процессы не смотрим
    w.tick(5.5)
    sugg = d.poll(s)
    assert sugg is not None
    p = sugg.payload()
    assert set(p) == {"id", "source", "app", "title", "detected_at"}
    assert p["source"]["id"] == "proc:100" and p["app"] == "Brave Browser"
    assert p["title"] == "Google Meet" and "T" in p["detected_at"] and len(p["id"]) == 12
    assert sugg.exe == "brave.exe" and d.pending() == [sugg]
    # тот же разговор — больше не предлагается, и дорогих вызовов нет
    calls = dict(w.calls)
    for _ in range(3):
        w.tick(2)
        assert d.poll(s) is None
    assert w.calls["table"] == calls["table"] and w.calls["sources"] == calls["sources"]
    # новый разговор (микрофон отпущен и снова занят) — новое предложение
    w.release(BRAVE)
    w.tick(2)
    assert d.poll(s) is None
    w.mic(BRAVE, age_s=11)
    again = d.poll(s)
    assert again is not None and again.id != sugg.id


def test_needs_playing_audio_in_the_same_tree(w: World) -> None:
    s = config.Settings()
    w.sessions = [(202, True), (102, False)]       # играет только chrome (YouTube)
    d = w.detector()
    w.mic(BRAVE, age_s=30)
    assert d.poll(s) is None and w.calls["sources"] == 0
    w.tick(1)
    assert d.poll(s) is None and w.calls["sessions"] == 1   # повтор — не раньше чем через 2 с
    w.tick(1.5)
    w.sessions = [(202, True), (102, True)]        # собеседники зазвучали в brave
    sugg = d.poll(s)
    assert sugg is not None and sugg.source["pid"] == 100


def test_audio_retry_slows_down(w: World) -> None:
    s = config.Settings()
    w.sessions = []
    d = w.detector()
    w.mic(BRAVE, age_s=11)
    for _ in range(40):                            # 80 с микрофона без звука
        d.poll(s)
        w.tick(2)
    n = w.calls["sessions"]
    for _ in range(5):                             # дальше — раз в 10 с
        d.poll(s)
        w.tick(2)
    assert w.calls["sessions"] - n == 1


@pytest.mark.parametrize("setup", ["ignored", "own_exe", "own_pid", "desktop_off", "off",
                                   "stale", "not_meeting_app"])
def test_no_suggestion(w: World, setup: str) -> None:
    s = config.Settings()
    own: tuple[int, ...] = ()
    if setup == "ignored":
        s.meeting_ignore_apps = ["brave.exe"]
        w.mic(BRAVE)
    elif setup == "own_exe":
        w.mic(w.paths[500])
        w.sources.append(src(500, "rupor-core.exe", "Rupor"))
    elif setup == "own_pid":
        own = (100,)
        w.mic(BRAVE)
    elif setup == "desktop_off":
        s.meeting_detect_desktop_apps = False
        w.mic(ZOOM)
        w.sources.append(src(400, "Zoom.exe", "Zoom Workplace"))
    elif setup == "off":
        s.meeting_autosuggest = False
        w.mic(BRAVE)
    elif setup == "stale":
        w.created = {p: w.ft for p in w.table}
        w.mic(BRAVE, age_s=3600)
    elif setup == "not_meeting_app":
        w.table[600] = (1, "Wispr Flow.exe")
        w.paths[600] = r"C:\Tools\Wispr Flow.exe"
        w.mic(w.paths[600])
        w.sources.append(src(600, "Wispr Flow.exe", "Wispr Flow"))
    d = w.detector(own=own)
    for _ in range(3):
        assert d.poll(s) is None
        w.tick(2)
    if setup == "off":
        assert w.calls["reader"] == 0
    if setup == "stale":
        assert w.calls["table"] == 1                  # зависшая запись разобрана один раз


def test_desktop_app_suggested(w: World) -> None:
    w.mic(ZOOM)
    w.sources.append(src(400, "Zoom.exe", "Zoom Workplace", title="Zoom Meeting"))
    w.titles[400] = ["Zoom Meeting"]
    sugg = w.detector().poll(config.Settings())
    assert sugg is not None and sugg.app == "Zoom Workplace" and sugg.title == "Zoom"
    assert sugg.exe == "zoom.exe" and sugg.source["id"] == "proc:400"


def test_packaged_teams_with_webview_audio(w: World) -> None:
    w.mic(TEAMS_PFN, packaged=True)
    w.sources.append(src(310, "msedgewebview2.exe", "Microsoft Edge WebView2"))
    w.titles[300] = ["Chat | Microsoft Teams"]
    d = w.detector()
    sugg = d.poll(config.Settings())
    assert sugg is not None and sugg.source["id"] == "proc:310" and sugg.exe == "ms-teams.exe"
    assert sugg.app == "Microsoft Teams" and sugg.title == "Microsoft Teams"
    # сеанс по этому источнику назван программой, а не WebView2
    assert d.decorate(sugg.source)["name"] == "Microsoft Teams"
    assert d.decorate(w.sources[0])["name"] == "Brave Browser"       # без изменений
    # pid источника достался другой программе — имя не подменяется
    assert d.decorate(src(310, "chrome.exe", "Google Chrome"))["name"] == "Google Chrome"
    # пакеты процессов перебираются один раз (кэш), не каждый опрос
    n = w.calls["pfn"]
    w.mic(TEAMS_PFN, packaged=True, age_s=20)      # новый разговор
    w.tick(1)
    assert d.poll(config.Settings()) is not None and w.calls["pfn"] == n


def test_title_from_foreground_hint(w: World) -> None:
    w.titles = {100: ["Inbox - Brave"]}           # сейчас активна другая вкладка
    d = w.detector()
    d.title_hint(100, "Пятничный созвон — Контур.Толк - Brave", config.DEFAULT_MEETING_PATTERNS)
    d.title_hint(100, "Inbox - Brave", config.DEFAULT_MEETING_PATTERNS)   # не встреча — мимо
    w.mic(BRAVE)
    sugg = d.poll(config.Settings())
    assert sugg is not None and sugg.title == "Контур.Толк"


def test_no_title_when_unknown(w: World) -> None:
    """Браузер без заголовка встречи — предложение без названия, но только когда микрофон
    занят дольше минуты (голосовое сообщение, диктовка в вкладке — не встреча)."""
    w.titles = {100: ["Inbox - Brave"]}
    w.mic(BRAVE)
    d = w.detector()
    assert d.poll(config.Settings()) is None
    w.tick(50)
    sugg = d.poll(config.Settings())
    assert sugg is not None and sugg.title == "" and sugg.app == "Brave Browser"


def test_unnamed_browser_needs_a_minute_and_later_snoozes(w: World) -> None:
    """Музыка в одной вкладке, голосовое сообщение или диктовка в другой — звук играет то же
    дерево процессов. Без заголовка встречи — только после минуты микрофона; «Не сейчас» —
    такие предложения этого браузера 30 мин не приходят, а встреча с заголовком — приходит."""
    s = config.Settings()
    w.titles = {200: ["Lofi - YouTube - Google Chrome"]}
    d = w.detector()
    w.mic(CHROME, age_s=12)                        # голосовое сообщение под музыку
    assert d.poll(s) is None
    w.tick(30)
    assert d.poll(s) is None
    w.tick(20)                                     # микрофон занят дольше минуты
    sugg = d.poll(s)
    assert sugg is not None and sugg.title == "" and sugg.source["id"] == "proc:200"
    taken = d.take(sugg.id)
    assert taken is not None
    d.snooze(taken)
    w.release(CHROME)
    w.tick(2)
    d.poll(s)
    w.mic(CHROME, age_s=12)                        # следующая длинная диктовка
    w.tick(60)
    assert d.poll(s) is None
    w.titles[200] = ["Meet - abc-defg-hij - Google Chrome"]   # а это уже встреча
    w.tick(10)
    again = d.poll(s)
    assert again is not None and again.title == "Google Meet"
    d.snooze(again)                                # с заголовком — «Не сейчас» как раньше
    assert d.take(again.id) is again
    w.release(CHROME)
    w.tick(2)
    d.poll(s)
    w.titles[200] = ["Lofi - YouTube - Google Chrome"]
    w.tick(meeting_detect.LATER_SNOOZE_S)          # пауза кончилась
    w.mic(CHROME, age_s=61)
    assert d.poll(s) is not None


def test_long_silent_mic_hold_is_not_a_meeting(w: World) -> None:
    """Браузер долго держит микрофон без звука (забытая вкладка, голосовой помощник):
    заигравшая потом вкладка без заголовка встречи — не встреча, встреча с заголовком — да.
    Микрофон занят часами — без перепроверок (ни процессов, ни аудиосессий)."""
    s = config.Settings()
    w.titles = {200: ["Music - YouTube - Google Chrome"]}
    w.sources[1] = dict(w.sources[1], active=False)          # chrome молчит
    d = w.detector()
    w.mic(CHROME, age_s=30)
    for _ in range(100):                                      # больше 15 мин тишины
        assert d.poll(s) is None
        w.tick(10)
    w.sources[1]["active"] = True                             # заиграла музыка
    for _ in range(3):
        w.tick(10)
        assert d.poll(s) is None
    w.titles[200] = ["Meet - abc-defg-hij - Google Chrome"]
    w.tick(10)
    assert d.poll(s) is not None
    w2 = World()
    d2 = w2.detector()
    w2.mic(BRAVE, age_s=4 * 3600)
    for _ in range(3):
        assert d2.poll(s) is None
        w2.tick(10)
    assert w2.calls["table"] == 0 and w2.calls["sources"] == 0


def test_after_session_ongoing_call_is_not_suggested(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    w.mic(BRAVE, age_s=60)
    d.session_over()                               # сеанс (начатый вручную) только что кончился
    assert d.poll(s) is None
    w.tick(2)
    assert d.poll(s) is None
    w.release(BRAVE)
    w.tick(2)
    d.poll(s)
    w.mic(BRAVE, age_s=10)                         # следующий разговор — предлагается
    assert d.poll(s) is not None


def test_take_once_and_expiry(w: World) -> None:
    w.mic(BRAVE)
    d = w.detector()
    sugg = d.poll(config.Settings())
    assert sugg is not None
    assert d.take("nope") is None
    assert d.take(sugg.id) is sugg and d.take(sugg.id) is None
    w.mic(BRAVE, age_s=11)
    w.tick(1)
    late = d.poll(config.Settings())
    assert late is not None
    w.tick(meeting_detect.PENDING_TTL_S + 1)
    assert d.take(late.id) is None


# ---------- источник «auto» ----------

def test_auto_prefers_app_holding_the_mic(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    srcs = [src(200, "chrome.exe", "Google Chrome", meeting=False),     # YouTube играет
            src(100, "brave.exe", "Brave Browser"), dict(SYSTEM)]
    assert d.auto_sources(s, srcs) == []                # микрофон никто не держит
    w.mic(BRAVE, age_s=1)                               # без порога в 10 с
    assert [x["id"] for x in d.auto_sources(s, srcs)] == ["proc:100"]
    # звук держащего микрофон не играет (забытая вкладка, голосовой помощник) — не встреча:
    # только запасной выбор
    srcs[1] = src(100, "brave.exe", "Brave Browser", active=False)
    assert d.auto_sources(s, srcs) == []
    assert [x["id"] for x in d.auto_sources(s, srcs, active_only=False)] == ["proc:100"]
    s.meeting_ignore_apps = ["brave.exe"]
    assert d.auto_sources(s, srcs, active_only=False) == []


# ---------- помощники и пути ----------

MAX_UI = r"C:\Program Files\MAX\max.exe"
MAX_SVC = r"C:\Program Files\MAX\max-service\max-service.exe"
CPTHOST = r"C:\Users\Demo\AppData\Roaming\Zoom\bin\CptHost.exe"


@pytest.mark.parametrize("child", [True, False])
def test_max_call_is_held_by_max_service(w: World, child: bool) -> None:
    """MAX: микрофон держит max-service.exe (звонок, WebRTC), а не max.exe — предлагается
    MAX; помощник — потомок max.exe или запущен отдельно из папки MAX."""
    s = config.Settings()
    w.table[600] = (1, "max.exe")
    w.table[601] = (600 if child else 1, "max-service.exe")
    w.paths.update({600: MAX_UI, 601: MAX_SVC})
    w.created.update({600: FT0, 601: FT0})
    w.sources.append(src(601, "max-service.exe", "max-service"))
    d = w.detector()
    w.mic(MAX_SVC)
    sugg = d.poll(s)
    assert sugg is not None and sugg.app == "MAX" and sugg.source["id"] == "proc:601"
    assert sugg.exe == "max-service.exe" and d.decorate(sugg.source)["name"] == "MAX"
    assert [x["id"] for x in d.auto_sources(s, w.sources)] == ["proc:601"]
    verdict, app = _map(w, w.mic(MAX_SVC))
    assert verdict == "ok" and app.root == 600 and app.roots == frozenset({600, 601})
    assert {600, 601} <= app.tree
    # «Не предлагать для MAX» — exe в ignore-списке: запись больше не читается
    s.meeting_ignore_apps = ["max-service.exe"]
    assert not meeting_detect.wanted("max-service.exe", s)


def test_other_max_exe_is_not_max(w: World) -> None:
    """Чужая программа с именем Max.exe (Cycling '74 Max) с микрофоном и звуком — не MAX."""
    w.table[700] = (1, "Max.exe")
    w.paths[700] = r"C:\Program Files\Cycling '74\Max 8\Max.exe"
    w.sources.append(src(700, "Max.exe", "Max"))
    w.mic(w.paths[700])
    assert w.detector().poll(config.Settings()) is None


def test_zoom_screen_share_host_counts_as_zoom(w: World) -> None:
    """Микрофон держит CptHost.exe (демонстрация экрана Zoom со звуком) — это Zoom; конец
    встречи следится по записи CptHost (её exe — не zoom.exe)."""
    s = config.Settings()
    w.table[410] = (400, "CptHost.exe")
    w.paths[410] = CPTHOST
    w.created[410] = FT0
    w.sources.append(src(400, "Zoom.exe", "Zoom Workplace", title="Zoom Meeting"))
    w.titles[400] = ["Zoom Meeting"]
    d = w.detector()
    w.mic(CPTHOST)
    sugg = d.poll(s)
    assert sugg is not None and sugg.exe == "zoom.exe" and sugg.app == "Zoom Workplace"
    assert sugg.source["id"] == "proc:400" and sugg.title == "Zoom"
    sess_src = sugg.source
    assert d.session_tick(s, sess_src, "T5") is None
    w.release(CPTHOST)
    w.tick(5)
    assert d.session_tick(s, sess_src, "T5") is None
    w.sources[-1] = dict(sess_src, active=False)
    w.tick(31)
    assert d.session_tick(s, sess_src, "T5") == HINT_ENDED
    s.meeting_ignore_apps = ["zoom.exe"]                  # «Не предлагать для Zoom»
    assert not meeting_detect.wanted("cpthost.exe", s)


def test_webview_mic_of_meeting_app(w: World) -> None:
    """Микрофон держит WebView2 программы встреч (запись по пути msedgewebview2.exe, не по
    пакету): программа — её предок в дереве процессов; WebView2 других программ — нет."""
    s = config.Settings()
    w.pfn = {}
    w.sources.append(src(310, "msedgewebview2.exe", "Microsoft Edge WebView2"))
    w.titles[300] = ["Chat | Microsoft Teams"]
    w.mic(WEBVIEW)
    d = w.detector()
    sugg = d.poll(s)
    assert sugg is not None and sugg.exe == "ms-teams.exe" and sugg.source["id"] == "proc:310"
    assert sugg.app == "Microsoft Teams" and d.decorate(sugg.source)["name"] == "Microsoft Teams"
    w.table[300] = (1, "SearchHost.exe")                  # WebView2 поиска Windows
    w.mic(WEBVIEW, age_s=20)
    w.tick(1)
    assert w.detector().poll(s) is None


def test_map_path_with_hash_sign(w: World) -> None:
    """«#» в самом пути (папка пользователя «Dev#1»): запись сверяется с процессами по ключу,
    а не по пути, собранному из ключа обратно."""
    tg = r"C:\Users\Dev#1\AppData\Roaming\Telegram Desktop\Telegram.exe"
    w.table[800] = (1, "Telegram.exe")
    w.paths[800] = tg
    w.created[800] = FT0
    verdict, app = _map(w, w.mic(tg))
    assert verdict == "ok" and app.root == 800 and app.exe == "telegram.exe"
    assert app.path == tg


# ---------- идущий сеанс: «встреча, похоже, закончилась» ----------

def test_session_end_hint(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    w.mic(BRAVE)
    sess_src = w.sources[0]
    assert d.session_tick(s, sess_src, "T1") is None              # микрофон занят
    w.release(BRAVE)
    for _ in range(7):                                            # 30 с без микрофона…
        w.tick(5)
        assert d.session_tick(s, sess_src, "T1") is None          # …звук ещё играет — нет
    w.sources[0] = dict(sess_src, active=False)
    w.tick(5)
    assert d.session_tick(s, sess_src, "T1") == HINT_ENDED
    w.tick(1)
    assert d.session_tick(s, sess_src, "T1") == HINT_ENDED        # проверка — раз в 5 с
    n = w.calls["reader"]
    w.tick(1)
    d.session_tick(s, sess_src, "T1")
    assert w.calls["reader"] == n
    w.mic(BRAVE, age_s=0)                                         # снова говорит
    w.tick(5)
    assert d.session_tick(s, sess_src, "T1") is None


def test_session_hint_needs_mic_seen(w: World) -> None:
    s = config.Settings()
    d = w.detector()
    yt = src(200, "chrome.exe", "Google Chrome", active=False)    # YouTube без микрофона
    for _ in range(10):
        w.tick(5)
        assert d.session_tick(s, yt, "T2") is None
    # «весь звук компьютера» — без подсказки
    for _ in range(3):
        w.tick(5)
        assert d.session_tick(s, dict(SYSTEM), "T3") is None


def test_session_watch_by_path_of_manual_source(w: World) -> None:
    """Сеанс начат вручную (без предложения): запись микрофона — по пути exe источника."""
    s = config.Settings()
    d = w.detector()
    w.mic(ZOOM)
    zoom = src(400, "Zoom.exe", "Zoom Workplace", active=False)
    w.sources.append(zoom)
    assert d.session_tick(s, zoom, "T4") is None
    w.release(ZOOM)
    w.tick(5)
    d.session_tick(s, zoom, "T4")
    w.tick(31)
    assert d.session_tick(s, zoom, "T4") == HINT_ENDED


# ---------- настройки ----------

@pytest.mark.parametrize("patch,ok", [
    ({"meeting_ignore_apps": []}, True), ({"meeting_ignore_apps": ["Zoom.exe"]}, True),
    ({"meeting_ignore_apps": "zoom.exe"}, False), ({"meeting_ignore_apps": [""]}, False),
    ({"meeting_ignore_apps": [1]}, False), ({"meeting_ignore_apps": ["x" * 101]}, False),
    ({"meeting_ignore_apps": [f"a{i}.exe" for i in range(101)]}, False),
    ({"meeting_detect_desktop_apps": False}, True), ({"meeting_detect_desktop_apps": 1}, False),
])
def test_detect_settings_validation(patch: dict[str, Any], ok: bool) -> None:
    s = config.Settings()
    if ok:
        config.apply_patch(s, patch)
    else:
        with pytest.raises(ValueError):
            config.apply_patch(s, patch)


def test_detect_settings_defaults_and_normalization() -> None:
    s = config.Settings()
    assert s.meeting_detect_desktop_apps is True
    assert s.meeting_ignore_apps == ["obs64.exe", "natspeak.exe", "ffmpeg.exe", "python.exe",
                                     "pythonw.exe"]
    config.apply_patch(s, {"meeting_ignore_apps": [r"C:\Program Files\OBS\OBS64.EXE", "zoom",
                                                   "obs64.exe", " Teams.exe "]})
    assert s.meeting_ignore_apps == ["obs64.exe", "zoom.exe", "teams.exe"]


# ---------- движок и WebSocket ----------

@pytest.fixture
def det(env: Env, w: World) -> MeetingDetector:  # noqa: F811
    """Детектор движка на фейковой машине; источники звука — как у движка (env.calls)."""
    d = w.detector(sources=env.eng.meeting_sources)
    env.eng._detector = d
    env.calls.sources = [dict(x) for x in w.sources]
    return d


def test_engine_suggest_and_start(env: Env, w: World, det: MeetingDetector) -> None:  # noqa: F811
    c = env.client()
    c.call("meeting_status")
    c.mark()
    w.mic(BRAVE)
    env.eng._meeting_detect_tick()
    ev = c.wait_event("meeting_suggest")
    assert set(ev) == {"id", "source", "app", "title", "detected_at"}
    assert ev["source"]["id"] == "proc:100" and ev["title"] == "Google Meet"
    assert set(ev["source"]) == {"id", "kind", "pid", "exe", "name", "title", "meeting",
                                 "active", "available"}
    st = c.call("meeting_suggest_answer", {"id": ev["id"], "answer": "start"})
    assert st["running"] is True and st["source"]["id"] == "proc:100" and st["hint"] is None
    assert env.calls.streams[-1].target == "pid:100"
    # ответ — один раз; пока сеанс идёт, новых предложений нет
    assert "неактуально" in c.error("meeting_suggest_answer",
                                    {"id": ev["id"], "answer": "start"})
    w.mic(BRAVE, age_s=30)                              # «новый разговор» во время сеанса
    env.eng._meeting_detect_tick()
    c.call("meeting_stop")
    env.eng._meeting_detect_tick()                      # после «Стоп» идущий разговор — тихо
    w.tick(3)
    env.eng._meeting_detect_tick()
    c.call("meeting_status")                            # всё, что разослано, уже пришло
    assert c.events("meeting_suggest") == [ev]
    c.close()


def test_engine_suggest_never_and_later(env: Env, w: World,  # noqa: F811
                                        det: MeetingDetector) -> None:
    c = env.client()
    c.call("meeting_status")
    c.mark()
    w.mic(BRAVE)
    env.eng._meeting_detect_tick()
    ev = c.wait_event("meeting_suggest")
    assert c.call("meeting_suggest_answer", {"id": ev["id"], "answer": "never"}) is None
    sett = c.wait_event("settings", lambda d: "brave.exe" in d["meeting_ignore_apps"])
    assert det._st.snoozed == {}                       # «никогда» — не пауза
    assert sett["meeting_ignore_apps"][-1] == "brave.exe"
    assert env.eng.s.meeting_ignore_apps.count("brave.exe") == 1
    w.mic(BRAVE, age_s=20)                              # следующий разговор — уже не предлагаем
    env.eng._meeting_detect_tick()
    # «Не сейчас» по устаревшему id — молча; неверный ответ — ошибка
    assert c.call("meeting_suggest_answer", {"id": "gone", "answer": "later"}) is None
    assert "start, later или never" in c.error("meeting_suggest_answer",
                                               {"id": "x", "answer": "maybe"})
    assert "id" in c.error("meeting_suggest_answer", {"answer": "start"})
    c.call("meeting_status")
    assert c.events("meeting_suggest") == [ev]
    # выключено — ни опроса, ни предложений
    c.call("set_settings", {"patch": {"meeting_autosuggest": False, "meeting_ignore_apps": []}})
    n = w.calls["reader"]
    env.eng._meeting_detect_tick()
    assert w.calls["reader"] == n
    c.close()


def test_engine_no_suggestion_before_first_run(env: Env, w: World,  # noqa: F811
                                               det: MeetingDetector) -> None:
    env.eng.s.first_run_done = False
    w.mic(BRAVE)
    env.eng._meeting_detect_tick()
    assert w.calls["reader"] == 0 and det.pending() == []
    env.eng.s.first_run_done = True
    env.eng._meeting_detect_tick()
    assert len(det.pending()) == 1


def test_engine_auto_source_prefers_mic(env: Env, w: World,  # noqa: F811
                                        det: MeetingDetector) -> None:
    # chrome играет музыку и «похож на встречу» по заголовку, brave держит микрофон (звонок:
    # поток вывода открыт)
    env.calls.sources = [src(200, "chrome.exe", "Google Chrome", meeting=True),
                         src(100, "brave.exe", "Brave Browser"), dict(SYSTEM)]
    w.mic(BRAVE, age_s=2)
    st = env.eng.meeting_start(None)
    assert st["source"]["id"] == "proc:100"
    env.eng.meeting_stop()
    # звук brave не играет (вкладка молча держит микрофон) — играющий chrome
    env.calls.sources[1]["active"] = False
    env.calls.sources[0]["meeting"] = False
    st = env.eng.meeting_start("auto")
    assert st["source"]["id"] == "proc:200"
    env.eng.meeting_stop()
    # больше ничего нет — всё-таки brave с микрофоном
    env.calls.sources = [src(100, "brave.exe", "Brave Browser", active=False), dict(SYSTEM)]
    st = env.eng.meeting_start("auto")
    assert st["source"]["id"] == "proc:100"
    env.eng.meeting_stop()
    env.calls.sources = [src(200, "chrome.exe", "Google Chrome", meeting=True),
                         src(100, "brave.exe", "Brave Browser", active=False), dict(SYSTEM)]
    w.release(BRAVE)
    w.tick(3)
    st = env.eng.meeting_start("auto")                  # микрофон свободен — как раньше
    assert st["source"]["id"] == "proc:200"
    env.eng.meeting_stop()


def test_engine_end_hint(env: Env, w: World, det: MeetingDetector) -> None:  # noqa: F811
    c = env.client()
    w.mic(BRAVE)
    c.call("meeting_start", {"source_id": "proc:100"})
    c.mark()
    env.eng._meeting_detect_tick()                      # микрофон занят — подсказки нет
    w.release(BRAVE)
    env.calls.sources[0]["active"] = False              # звук встречи пропал
    for _ in range(8):
        w.tick(5)
        env.eng._meeting_detect_tick()
    ev = c.wait_event("meeting", lambda d: d["hint"] == HINT_ENDED)
    assert ev["running"] is True                        # сам не останавливается
    assert c.call("meeting_status")["hint"] == HINT_ENDED
    w.mic(BRAVE, age_s=0)
    w.tick(5)
    env.eng._meeting_detect_tick()
    assert c.wait_event("meeting", lambda d: d["hint"] is None)["running"] is True
    c.call("meeting_stop")
    assert c.call("meeting_status")["hint"] is None
    c.close()
