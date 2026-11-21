"""Слова-паразиты: подсчёт по сырому тексту распознавания и накопительная статистика по дням.

Считается **сырой** текст Whisper (``raw``) — до словаря и ИИ-очистки: очистка паразитов
как раз убирает. Подсчёт локальный и бесплатный: текст режется на слова (как
``textproc.count_words``), фразы из списка ищутся по последовательностям слов без учёта
регистра и «ё», самая длинная — первой, без пересечений. Тянуть звуки («эээ», «э-э»,
«эм», «мммм», «ааа», «um», «uhh») сводятся к одной форме, чтобы их можно было держать
в списке одной строкой.

Смысла счётчик не понимает: «как бы ты поступил» — тоже «как бы», «вот дом» — тоже «вот».
Поэтому список редактируемый (``kv: fillers_list``), а необязательная кнопка «Найти
обороты с ИИ» (``ai_suggest``) предлагает фразы, которые человек повторяет именно у себя.

Whisper сам часто выкидывает «эээ/ммм» и обрывки слов — они видны в статистике только
тогда, когда модель их записала.

Хранение — две таблицы в той же базе (создаются здесь, вне цепочки миграций ``storage``):

- ``filler_daily(date, phrase, count)`` — сколько раз фраза прозвучала за день;
- ``filler_words(date, words, dictations)`` — сколько слов сказано за день (знаменатель
  «на 100 слов»).

Статистика не зависит от истории: её срок хранения и очистка эти таблицы не трогают.
Пересчёт по истории (``rebuild``) заменяет только те дни, по которым история есть, —
при первом открытии экрана он делается сам, чтобы цифры были с первого дня.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time
import unicodedata
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable

from . import ai, i18n
from .i18n import tr
from .textproc import _WORD_RE, fold

if TYPE_CHECKING:
    from .engine import Engine
    from .storage import Storage

log = logging.getLogger(__name__)

_DATA = Path(__file__).resolve().parent / "data"
KV_LIST = "fillers_list"            # JSON-список фраз (нет ключа — список по умолчанию)
KV_ENABLED = "fillers_enabled"      # "0" — не считать
KV_BACKFILL = "fillers_backfill"    # "1" — первый пересчёт по истории уже был
MAX_PHRASES = 200
MAX_PHRASE_WORDS = 6
MAX_PHRASE_CHARS = 60
PERIODS = (7, 30, 90, 0)            # 0 — за всё время
CHART_DAYS_MAX = 90
AI_SAMPLE_CHARS = 8000              # сколько сырого текста отдать ИИ (свежие диктовки)
AI_SAMPLE_ITEMS = 80
AI_TIMEOUT_S = 40.0
HISTORY_SKIP_TARGETS = ("test",)    # проверка микрофона в настройках — не речь пользователя

_HYPHENS = re.compile(r"[-‐‑'’_]")
# Тянуть звуки: «э», «ээ», «э-э-э», «эм», «эмм»; «ммм»; «аа», «ааа» (одиночное «а» — союз)
_HES_E = re.compile(r"э+м*")
_HES_M = re.compile(r"м{3,}")   # «мм» — ещё и миллиметры
_HES_A = re.compile(r"аа+")
_HES_UM = re.compile(r"u+m+|e+r+m+|e+m+")
_HES_UH = re.compile(r"u+h+|e+r+|a+h+")
_HES_HMM = re.compile(r"h+m+")

_schema_lock = threading.Lock()


# ---------- разбор текста ----------

def _token(w: str) -> str:
    """Нормальная форма слова: без регистра и «ё», тянуть звуки — к одной форме."""
    t = fold(w)
    bare = _HYPHENS.sub("", t)
    if _HES_E.fullmatch(bare):
        return "эээ"
    if _HES_M.fullmatch(bare):
        return "ммм"
    if _HES_A.fullmatch(bare):
        return "ааа"
    if _HES_UM.fullmatch(bare):
        return "um"
    if _HES_UH.fullmatch(bare):
        return "uh"
    if _HES_HMM.fullmatch(bare):
        return "hmm"
    return t


def tokens(text: str) -> list[str]:
    if not text:
        return []
    return [_token(w) for w in _WORD_RE.findall(unicodedata.normalize("NFC", text))]


def normalize(phrase: str) -> str:
    """Фраза списка в нормальной форме («  Как  БЫ, » → «как бы», «Э-э-э» → «эээ»)."""
    return " ".join(tokens(phrase))


def clean_list(items: Iterable[Any]) -> list[str]:
    """Проверенный список: нормальные формы, без пустых и повторов, порядок сохранён."""
    out: list[str] = []
    seen: set[str] = set()
    for it in items:
        if not isinstance(it, str):
            continue
        p = normalize(it)
        if not p or len(p) > MAX_PHRASE_CHARS or len(p.split()) > MAX_PHRASE_WORDS:
            continue
        if p in seen:
            continue
        seen.add(p)
        out.append(p)
        if len(out) >= MAX_PHRASES:
            break
    return out


def _read_list(name: str) -> list[str]:
    try:
        lines = (_DATA / name).read_text(encoding="utf-8").splitlines()
    except OSError:
        log.warning("Нет списка паразитов %s", name)
        return []
    return [ln.strip() for ln in lines if ln.strip() and not ln.lstrip().startswith("#")]


def default_list() -> list[str]:
    return clean_list(_read_list("fillers_ru.txt") + _read_list("fillers_en.txt"))


class Matcher:
    """Поиск фраз списка в тексте: самая длинная фраза с этой позиции — первой."""

    def __init__(self, phrases: Iterable[str]) -> None:
        self.by_first: dict[str, list[tuple[str, ...]]] = {}
        for p in clean_list(phrases):
            seq = tuple(p.split())
            self.by_first.setdefault(seq[0], []).append(seq)
        for seqs in self.by_first.values():
            seqs.sort(key=len, reverse=True)

    def count(self, text: str) -> tuple[Counter[str], int]:
        """(сколько раз каждая фраза, число слов в тексте)."""
        toks = tokens(text)
        found: Counter[str] = Counter()
        i, n = 0, len(toks)
        while i < n:
            step = 1
            for seq in self.by_first.get(toks[i], ()):
                k = len(seq)
                if tuple(toks[i:i + k]) == seq:
                    found[" ".join(seq)] += 1
                    step = k
                    break
            i += step
        return found, n


# ---------- хранение ----------

def _ensure(st: Storage) -> None:
    if getattr(st, "_fillers_ready", False):
        return
    with _schema_lock:
        if getattr(st, "_fillers_ready", False):
            return
        with st._tx() as c:
            c.execute("""CREATE TABLE IF NOT EXISTS filler_daily(date TEXT NOT NULL,
                phrase TEXT NOT NULL, count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY(date, phrase))""")
            c.execute("""CREATE TABLE IF NOT EXISTS filler_words(date TEXT PRIMARY KEY,
                words INTEGER NOT NULL DEFAULT 0, dictations INTEGER NOT NULL DEFAULT 0)""")
        st._fillers_ready = True  # type: ignore[attr-defined]


def _day(ts: float | None) -> str:
    return datetime.fromtimestamp(time.time() if ts is None else ts).date().isoformat()


def is_enabled(st: Storage) -> bool:
    return st.kv_get(KV_ENABLED, "1") != "0"


def get_list(st: Storage) -> list[str]:
    raw = st.kv_get(KV_LIST, "")
    if not raw:
        return default_list()
    try:
        data = json.loads(raw)
    except ValueError:
        log.warning("Список паразитов в базе битый — беру список по умолчанию")
        return default_list()
    return clean_list(data) if isinstance(data, list) else default_list()


_matchers: dict[tuple[str, ...], Matcher] = {}


def _matcher(st: Storage) -> Matcher:
    key = tuple(get_list(st))
    m = _matchers.get(key)
    if m is None:
        _matchers.clear()
        m = _matchers[key] = Matcher(key)
    return m


def record(st: Storage, raw: str, when: float | None = None) -> None:
    """Учесть одну диктовку (сырой текст распознавания) в статистике дня."""
    if not raw or not raw.strip() or not is_enabled(st):
        return
    _ensure(st)
    found, words = _matcher(st).count(raw)
    if words <= 0:
        return
    d = _day(when)
    with st._tx() as c:
        c.execute("INSERT INTO filler_words(date, words, dictations) VALUES(?, ?, 1) "
                  "ON CONFLICT(date) DO UPDATE SET words = words + excluded.words, "
                  "dictations = dictations + 1", (d, words))
        c.executemany("INSERT INTO filler_daily(date, phrase, count) VALUES(?, ?, ?) "
                      "ON CONFLICT(date, phrase) DO UPDATE SET count = count + excluded.count",
                      [(d, p, n) for p, n in found.items()])


def rebuild(st: Storage) -> dict[str, int]:
    """Пересчитать по истории текущим списком: заменяются только дни, по которым есть
    история (более старые дни, чью историю уже удалил срок хранения, остаются как были).
    Одинаковый сырой текст в один день считается один раз — это повторное распознавание
    сохранённой записи, а не новая речь."""
    _ensure(st)
    m = _matcher(st)
    marks = ",".join("?" * len(HISTORY_SKIP_TARGETS))
    with st._lock:
        rows = st._conn.execute(
            f"SELECT ts, raw FROM history WHERE COALESCE(target, '') NOT IN ({marks}) "
            "ORDER BY ts", HISTORY_SKIP_TARGETS).fetchall()
    days: dict[str, tuple[Counter[str], list[int]]] = {}
    seen: set[tuple[str, str]] = set()
    for r in rows:
        raw = str(r["raw"] or "").strip()
        if not raw:
            continue
        d = _day(float(r["ts"]))
        if (d, raw) in seen:
            continue
        seen.add((d, raw))
        found, words = m.count(raw)
        if words <= 0:
            continue
        cnt, tot = days.setdefault(d, (Counter(), [0, 0]))
        cnt.update(found)
        tot[0] += words
        tot[1] += 1
    with st._tx() as c:
        for d, (cnt, tot) in days.items():
            c.execute("DELETE FROM filler_daily WHERE date = ?", (d,))
            c.execute("INSERT INTO filler_words(date, words, dictations) VALUES(?, ?, ?) "
                      "ON CONFLICT(date) DO UPDATE SET words = excluded.words, "
                      "dictations = excluded.dictations", (d, tot[0], tot[1]))
            c.executemany("INSERT INTO filler_daily(date, phrase, count) VALUES(?, ?, ?)",
                          [(d, p, n) for p, n in cnt.items()])
        c.execute("INSERT INTO kv(key, value) VALUES(?, '1') "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (KV_BACKFILL,))
    log.info("Паразиты: пересчёт по истории — дней %d, диктовок %d", len(days),
             sum(t[1] for _, t in days.values()))
    return {"days": len(days), "dictations": sum(t[1] for _, t in days.values())}


def reset(st: Storage) -> None:
    """Обнулить статистику паразитов (список остаётся)."""
    _ensure(st)
    with st._tx() as c:
        c.execute("DELETE FROM filler_daily")
        c.execute("DELETE FROM filler_words")
        c.execute("INSERT INTO kv(key, value) VALUES(?, '1') "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (KV_BACKFILL,))


def set_config(st: Storage, phrases: Any = None, enabled: Any = None) -> dict[str, Any]:
    """Сохранить список (``None`` — не менять, ``[]`` с ``reset`` в UI — пустой) и флаг."""
    if phrases is not None:
        if not isinstance(phrases, list):
            raise ValueError(tr("api.arg_str_list", name="phrases"))
        st.kv_set(KV_LIST, json.dumps(clean_list(phrases), ensure_ascii=False))
    if enabled is not None:
        if not isinstance(enabled, bool):
            raise ValueError(tr("api.arg_bool", name="enabled"))
        st.kv_set(KV_ENABLED, "1" if enabled else "0")
    return config_out(st)


def restore_defaults(st: Storage) -> dict[str, Any]:
    st.kv_set(KV_LIST, json.dumps(default_list(), ensure_ascii=False))
    return config_out(st)


def config_out(st: Storage) -> dict[str, Any]:
    return {"enabled": is_enabled(st), "phrases": get_list(st), "defaults": default_list()}


# ---------- статистика для экрана ----------

def _per100(n: int, words: int) -> float:
    return round(n * 100.0 / words, 2) if words > 0 else 0.0


def stats(st: Storage, days: int = 30, today: date | None = None) -> dict[str, Any]:
    """Статистика за период (``days`` из PERIODS, 0 — всё время) и предыдущий такой же
    период для сравнения. Первый вызов сам пересчитывает историю (``rebuild``)."""
    if days not in PERIODS:
        raise ValueError(tr("api.fillers_period"))
    _ensure(st)
    if st.kv_get(KV_BACKFILL, "") != "1":
        try:
            rebuild(st)
        except Exception:
            log.exception("Паразиты: первый пересчёт по истории не удался")
    today = today or date.today()
    with st._lock:
        wrows = st._conn.execute("SELECT date, words, dictations FROM filler_words").fetchall()
        frows = st._conn.execute("SELECT date, phrase, count FROM filler_daily").fetchall()
    words_by: dict[date, tuple[int, int]] = {}
    for r in wrows:
        try:
            words_by[date.fromisoformat(str(r["date"]))] = (int(r["words"] or 0),
                                                            int(r["dictations"] or 0))
        except ValueError:
            continue
    fill_by: dict[date, Counter[str]] = {}
    for r in frows:
        try:
            d = date.fromisoformat(str(r["date"]))
        except ValueError:
            continue
        fill_by.setdefault(d, Counter())[str(r["phrase"])] += int(r["count"] or 0)

    all_days = sorted(set(words_by) | set(fill_by))
    if days:
        first = today - timedelta(days=days - 1)
        prev_first = first - timedelta(days=days)
    else:
        first = all_days[0] if all_days else today
        prev_first = first

    def span(a: date, b: date) -> tuple[Counter[str], int, int]:
        c: Counter[str] = Counter()
        w = n = 0
        for d in all_days:
            if a <= d <= b:
                c.update(fill_by.get(d, Counter()))
                ww, nn = words_by.get(d, (0, 0))
                w += ww
                n += nn
        return c, w, n

    cur, words, dictations = span(first, today)
    prev, prev_words, _ = span(prev_first, first - timedelta(days=1)) if days else (
        Counter(), 0, 0)
    total = sum(cur.values())
    top = [{"phrase": p, "count": n, "per100": _per100(n, words),
            "prev_per100": _per100(prev.get(p, 0), prev_words) if prev_words else None}
           for p, n in sorted(cur.items(), key=lambda kv: (-kv[1], kv[0])) if n > 0]

    chart_first = max(first, today - timedelta(days=CHART_DAYS_MAX - 1))
    daily = []
    d = chart_first
    while d <= today:
        w = words_by.get(d, (0, 0))[0]
        f = sum(fill_by.get(d, Counter()).values())
        daily.append({"date": d.isoformat(), "words": w, "fillers": f, "per100": _per100(f, w)})
        d += timedelta(days=1)

    return {
        "enabled": is_enabled(st),
        "days": days,
        "words": words,
        "dictations": dictations,
        "fillers": total,
        "per100": _per100(total, words),
        "prev_per100": _per100(sum(prev.values()), prev_words) if prev_words else None,
        "top": top,
        "daily": daily,
        "since": all_days[0].isoformat() if all_days else None,
    }


# ---------- поиск оборотов с ИИ (по кнопке, платно) ----------

_AI_SYSTEM = {
    "ru": (
        "Ты — тренер по речи. Тебе дают сырые расшифровки устной речи одного человека "
        "(без правок, как распознала модель). Найди слова-паразиты и обороты-паразиты, "
        "которые этот человек повторяет по привычке и без смысловой нагрузки: вводные "
        "слова, связки, «заполнители» пауз, личные словесные тики. Не предлагай обычные "
        "служебные слова, если они в тексте нужны по смыслу. Не повторяй фразы из списка "
        "«уже отслеживается». Ответь ТОЛЬКО JSON без пояснений: "
        "{\"items\": [{\"phrase\": \"фраза в нижнем регистре\", \"count\": число, "
        "\"example\": \"короткий кусок текста с ней\"}]} — до 15 фраз, самые частые первыми."
    ),
    "en": (
        "You are a speech coach. You get raw transcripts of one person's speech (unedited, "
        "as the speech model heard it). Find the filler words and filler phrases this "
        "person repeats out of habit with no meaning: discourse markers, pause fillers, "
        "personal verbal tics. Do not suggest ordinary function words when they are needed "
        "for meaning. Do not repeat phrases from the \"already tracked\" list. Reply with "
        "JSON ONLY, no comments: {\"items\": [{\"phrase\": \"lowercase phrase\", "
        "\"count\": number, \"example\": \"short excerpt with it\"}]} — up to 15 phrases, "
        "most frequent first."
    ),
}
_AI_TRACKED = {"ru": "Уже отслеживается", "en": "Already tracked"}
_AI_TEXTS = {"ru": "Расшифровки", "en": "Transcripts"}


def _sample(st: Storage) -> list[str]:
    marks = ",".join("?" * len(HISTORY_SKIP_TARGETS))
    with st._lock:
        rows = st._conn.execute(
            f"SELECT raw FROM history WHERE COALESCE(target, '') NOT IN ({marks}) "
            "ORDER BY ts DESC LIMIT ?", (*HISTORY_SKIP_TARGETS, AI_SAMPLE_ITEMS * 3)).fetchall()
    out: list[str] = []
    seen: set[str] = set()
    size = 0
    for r in rows:
        raw = " ".join(str(r["raw"] or "").split())
        if not raw or raw in seen:
            continue
        seen.add(raw)
        if size + len(raw) > AI_SAMPLE_CHARS and out:
            break
        out.append(raw[:AI_SAMPLE_CHARS])
        size += len(raw)
        if len(out) >= AI_SAMPLE_ITEMS:
            break
    return out


def _parse_ai(text: str) -> list[dict[str, Any]]:
    s = text.strip()
    a, b = s.find("{"), s.rfind("}")
    if a < 0 or b <= a:
        raise ai.AIError(tr("api.fillers_ai_bad"), "bad_response")
    try:
        data = json.loads(s[a:b + 1])
    except ValueError:
        raise ai.AIError(tr("api.fillers_ai_bad"), "bad_response") from None
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise ai.AIError(tr("api.fillers_ai_bad"), "bad_response")
    out: list[dict[str, Any]] = []
    for it in items:
        if not isinstance(it, dict) or not isinstance(it.get("phrase"), str):
            continue
        cnt = it.get("count")
        ex = it.get("example")
        out.append({"phrase": it["phrase"],
                    "count": int(cnt) if isinstance(cnt, (int, float)) and cnt > 0 else 0,
                    "example": ex.strip()[:200] if isinstance(ex, str) else ""})
    return out


def ai_suggest(engine: Engine, lang: str | None = None) -> dict[str, Any]:
    """Попросить ИИ найти личные обороты-паразиты в свежих сырых диктовках. Фразы, которые
    уже в списке, отбрасываются; число вхождений пересчитывается локально — ИИ только
    предлагает, решает человек (кнопка «Добавить»)."""
    st = engine.storage
    texts = _sample(st)
    if not texts:
        raise ValueError(tr("api.fillers_ai_no_history"))
    lang = "en" if (lang or i18n.current()) == "en" else "ru"
    tracked = get_list(st)
    user = (f"{_AI_TRACKED[lang]}: {', '.join(tracked) or '-'}\n\n{_AI_TEXTS[lang]}:\n"
            + "\n".join(f"<t>{t}</t>" for t in texts))
    s = engine.s
    provider = ai._str_setting(s, "ai_provider")
    if provider == "claude_cli":
        provider = "keys"
    timeout = max(AI_TIMEOUT_S, ai._num_setting(s, "ai_timeout_s"))
    res = engine._ai_call("cleanup", "", lambda: ai._call(
        provider, _AI_SYSTEM[lang], user, s, timeout, 1500, None))
    have = set(tracked)
    joined = "\n".join(texts)
    out: list[dict[str, Any]] = []
    for it in _parse_ai(res.text):
        p = normalize(it["phrase"])
        if not p or p in have or len(p.split()) > MAX_PHRASE_WORDS:
            continue
        have.add(p)
        local, _ = Matcher([p]).count(joined)
        n = local.get(p, 0)
        if n == 0:
            continue                    # ИИ придумал фразу, которой в текстах нет
        out.append({"phrase": p, "count": n, "example": it["example"]})
    log.info("Паразиты: ИИ предложил %d фраз по %d диктовкам", len(out), len(texts))
    return {"items": out, "sampled": len(texts)}
