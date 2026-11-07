"""Хранилище Рупора: SQLite (история, словарь, сниппеты, статистика, черновик).

Файл ``%APPDATA%\\Rupor\\rupor.db`` в режиме WAL: процесс ядра можно убить в любой момент
(так Rust закрывает приложение) — закоммиченное не теряется. Одно соединение на объект
``Storage`` (``check_same_thread=False``), каждое обращение — под ``threading.RLock``,
поэтому объект можно звать из любых потоков: рабочий поток движка, WebSocket-сервер, трей.

Схема — в ``docs/ARCHITECTURE.md``; версия — ``PRAGMA user_version``, миграции — список
``_MIGRATIONS`` (элемент i переводит базу с версии i на i+1).

Поиск по истории и уникальность терминов/триггеров — через свою SQL-функцию ``py_fold``
(Python ``casefold`` + «ё» → «е»): встроенные ``LIKE``/``NOCASE`` SQLite понимают регистр
только у латиницы. Статистика хранится отдельно от истории (``stats_daily``) и не зависит
от её очистки и от ``history_enabled``.
"""
from __future__ import annotations

import json
import logging
import math
import sqlite3
import threading
import time
from collections import Counter, OrderedDict
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from . import textproc, vocab
from .config import app_dir
from . import i18n
from .i18n import tr

log = logging.getLogger(__name__)

# Типы ответов (поля — как в docs/ARCHITECTURE.md, раздел «Типы»)
HistoryItem = dict[str, Any]
DictItem = dict[str, Any]
Snippet = dict[str, Any]
Stats = dict[str, Any]
Migration = str | Callable[[sqlite3.Connection], None]

DAY_S = 86400
PERIOD_DAYS = {"day": 1, "week": 7, "month": 30}   # «за день» = последние 24 ч
TYPING_WPM = 40          # база для «сэкономленного времени» (ТЗ)
DAILY_DAYS = 30          # точек в графике на главной
WEEK_DAYS = 7            # «за неделю» = последние 7 дней, включая сегодня
MAX_PAGE = 1000
BUSY_TIMEOUT_S = 10.0    # ждать чужую блокировку записи (второй экземпляр, внешний просмотрщик)
LEARN_JOURNAL_KEY = "learn_journal"   # kv: журнал выученных правок (для «Отменить»)
LEARN_JOURNAL_MAX = 300
LEARN_SOURCES = ("auto", "accepted")  # кто добавил правку: Рупор сам / пользователь «Добавить»
MONTH_DAYS = 30          # расход ИИ «за месяц» = последние 30 дней, включая сегодня
AI_KINDS = ("cleanup", "command", "test")
VOCAB_VERSION_KEY = "vocab_version"   # kv: версия отбора слов, по которой собран vocab
VOCAB_LIST_MAX = 500
VOCAB_FETCH_ROWS = 2000     # строк vocab на процесс (самые частые) — из них слияние форм и топ
VOCAB_CACHE_APPS = 16       # сколько процессов держать в кэше частых слов
VOCAB_REBUILD_CHUNK = 500   # записей истории за один заход пересборки (замок — только на чтение)
_ALL_APPS = "\x00all"       # ключ кэша «все процессы»


_MIGRATIONS: tuple[tuple[Migration, ...], ...] = (
    (  # 0 → 1: исходная схема
        """CREATE TABLE IF NOT EXISTS history(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            app TEXT, title TEXT, raw TEXT NOT NULL, text TEXT NOT NULL, duration_s REAL,
            words INTEGER, language TEXT, style TEXT, ai INTEGER DEFAULT 0,
            latency_ms INTEGER, target TEXT)""",
        "CREATE INDEX IF NOT EXISTS history_ts ON history(ts)",
        """CREATE TABLE IF NOT EXISTS dictionary(id INTEGER PRIMARY KEY,
            term TEXT NOT NULL UNIQUE COLLATE NOCASE, variants TEXT NOT NULL DEFAULT '[]')""",
        """CREATE TABLE IF NOT EXISTS snippets(id INTEGER PRIMARY KEY,
            trigger TEXT NOT NULL UNIQUE COLLATE NOCASE, text TEXT NOT NULL)""",
        """CREATE TABLE IF NOT EXISTS stats_daily(date TEXT PRIMARY KEY,
            words INTEGER NOT NULL DEFAULT 0, speech_s REAL NOT NULL DEFAULT 0,
            dictations INTEGER NOT NULL DEFAULT 0)""",
        "CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY, value TEXT)",
    ),
    (  # 1 → 2: расход ИИ — запись на каждый вызов (очистка, команда, проверка)
        """CREATE TABLE IF NOT EXISTS ai_usage(id INTEGER PRIMARY KEY, ts REAL NOT NULL,
            kind TEXT NOT NULL, provider TEXT, model TEXT, upstream TEXT, tokens_in INTEGER,
            tokens_out INTEGER, cost_usd REAL, ms INTEGER, ok INTEGER NOT NULL DEFAULT 1,
            error_kind TEXT, app TEXT)""",
        "CREATE INDEX IF NOT EXISTS ai_usage_ts ON ai_usage(ts)",
    ),
    (  # 2 → 3: личный словарь частот (по процессам) и исключённые из него слова
        """CREATE TABLE IF NOT EXISTS vocab(word TEXT NOT NULL, app TEXT NOT NULL DEFAULT '',
            count INTEGER NOT NULL DEFAULT 0, last_ts REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(word, app))""",
        "CREATE INDEX IF NOT EXISTS vocab_app ON vocab(app)",
        "CREATE TABLE IF NOT EXISTS vocab_excluded(word TEXT PRIMARY KEY)",
        # частоты по истории считает пересборка после миграции (_vocab_check_version):
        # у движка — в фоне, чтобы большая история не задерживала запуск ядра
    ),
    (  # 3 → 4: какой ключ из списка ключей ИИ ответил (расход и оценка баланса по ключу)
        "ALTER TABLE ai_usage ADD COLUMN key_id TEXT",
        "CREATE INDEX IF NOT EXISTS ai_usage_key ON ai_usage(key_id, ts)",
    ),
)
SCHEMA_VERSION = len(_MIGRATIONS)

_HISTORY_COLS = ("ts", "app", "title", "raw", "text", "duration_s", "words", "language",
                 "style", "ai", "latency_ms", "target")


# ---------- приведение типов (данные приходят из WS/JSON — доверять нельзя) ----------

def _num(v: Any, default: float = 0.0) -> float:
    if isinstance(v, bool):
        return float(v)
    if isinstance(v, (int, float)):
        f = float(v)
    elif isinstance(v, str):
        try:
            f = float(v.strip().replace(",", "."))
        except ValueError:
            return default
    else:
        return default
    return f if math.isfinite(f) else default


def _int(v: Any, default: int = 0) -> int:
    return int(round(_num(v, float(default))))


def _str(v: Any, default: str = "") -> str:
    if v is None:
        return default
    return v if isinstance(v, str) else str(v)


def _clean_term(s: str) -> str:
    return " ".join(s.split())


def _clean_variants(variants: Any) -> list[str]:
    """Строки без пробелов по краям, без пустых и без дублей (регистр и «ё» не важны)."""
    if isinstance(variants, str):
        variants = [variants]
    if not isinstance(variants, (list, tuple)):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for v in variants:
        if not isinstance(v, str):
            continue
        v = _clean_term(v)
        k = textproc.fold(v)
        if v and k not in seen:
            seen.add(k)
            out.append(v)
    return out


def _load_variants(raw: Any) -> list[str]:
    try:
        v = json.loads(raw) if isinstance(raw, str) else []
    except ValueError:
        log.warning("Битый JSON вариантов в словаре — считаю пустым")
        return []
    return _clean_variants(v)


def _local_date(ts: float | None) -> date:
    return datetime.fromtimestamp(time.time() if ts is None else ts).date()


def _rollback(conn: sqlite3.Connection) -> None:
    """Откатить, если транзакция ещё открыта, — чтобы соединение не застряло в ней."""
    if conn.in_transaction:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.Error as e:
            log.error("ROLLBACK не удался: %s", e)


def _py_fold(v: Any) -> str | None:
    if v is None:
        return None
    if isinstance(v, bytes):
        v = v.decode("utf-8", "replace")
    return textproc.fold(v if isinstance(v, str) else str(v))


def _text(b: bytes) -> str:
    """text_factory: битый UTF-8 в базе (запись снаружи, сбой диска) не должен ронять ни
    выдачу истории, ни пересборку частот — такие байты заменяются «\ufffd»."""
    return b.decode("utf-8", "replace")


class Storage:
    """SQLite-хранилище. Все методы потокобезопасны; ошибки валидации — ``ValueError``
    с текстом по-русски (его можно показывать пользователю как есть).

    ``background=True`` (так открывает базу движок): пересборка частот слов после миграции
    или новой версии отбора — не при открытии, а по ``vocab_rebuild_async`` (пока она не
    закончилась, подсказка идёт без частых слов); кэш частых слов после новой диктовки
    обновляется в фоне (до того — прежний, без последней диктовки). Без него (тесты,
    инструменты) то же самое делается сразу, в вызывающем потоке."""

    def __init__(self, path: Path | None = None, *, background: bool = False) -> None:
        self.path = Path(path) if path is not None else app_dir() / "rupor.db"
        self._lock = threading.RLock()
        # Путь, куда отложена повреждённая база, если при открытии пришлось начать заново
        # (движок может показать об этом уведомление); иначе None.
        self.recovered: Path | None = None
        self._background = background
        # Частые слова: топ по процессу (и по всем процессам) после слияния форм — кэш
        # по ключу процесса. Новая диктовка кэш не сбрасывает, а помечает устаревшим
        # (пересчёт — в фоне или при следующем обращении); удаление истории, исключения
        # и пересборка сбрасывают его сразу.
        self._vocab_top: OrderedDict[str, list[vocab.Word]] = OrderedDict()
        self._vocab_stale: set[str] = set()
        self._vocab_ex: frozenset[str] | None = None      # ключи исключений (кэш)
        self._vocab_refreshing = False
        # Частоты не соответствуют истории (миграция, новая версия отбора, сбой пересборки):
        # подсказка — без частых слов, пока vocab_rebuild не закончится
        self._vocab_pending = False
        self._rebuild_lock = threading.Lock()
        self._closed = False
        self._conn = self._open()

    # ---------- подключение и миграции ----------

    def _connect(self) -> sqlite3.Connection:
        """Соединение + настройки + миграции. Битый файл даёт здесь ``DatabaseError``."""
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(self.path), check_same_thread=False, isolation_level=None,
                               timeout=BUSY_TIMEOUT_S)
        try:
            conn.row_factory = sqlite3.Row
            conn.text_factory = _text
            conn.create_function("py_fold", 1, _py_fold, deterministic=True)
            conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")
            mode = conn.execute("PRAGMA journal_mode = WAL").fetchone()[0]
            if str(mode).lower() != "wal" and str(self.path) != ":memory:":
                log.warning("SQLite не перешёл в WAL (режим %s)", mode)
            conn.execute("PRAGMA synchronous = NORMAL")  # в WAL не теряет данные при падении процесса
            # Удалённые строки затираются в основном файле базы. В -wal старые версии
            # страниц остаются до чекпоинта — поэтому после любого удаления истории
            # _checkpoint(TRUNCATE), а journal_size_limit обрезает -wal после чекпоинтов.
            conn.execute("PRAGMA secure_delete = ON")
            conn.execute("PRAGMA journal_size_limit = 0")
            self._migrate(conn)
        except BaseException:
            conn.close()
            raise
        return conn

    def _open(self) -> sqlite3.Connection:
        conn = self._open_conn()
        if self._vocab_pending and not self._background:
            self._conn = conn
            try:
                self.vocab_rebuild()
            except Exception:
                log.exception("Не удалось пересобрать словарь частот — подсказка без "
                              "частых слов")
        return conn

    def _open_conn(self) -> sqlite3.Connection:
        try:
            return self._connect()
        except sqlite3.OperationalError:
            raise                                        # занято/нет доступа — не порча
        except sqlite3.DatabaseError as e:               # «file is not a database», «malformed»
            try:
                broken = self._quarantine()
            except OSError as oe:
                log.error("База %s повреждена (%s), отложить её в сторону не вышло: %s",
                          self.path, e, oe)
                raise e from oe
            log.error("База %s повреждена (%s) — сохранена как %s, создаю новую",
                      self.path, e, broken)
            self.recovered = broken
            return self._connect()

    def _quarantine(self) -> Path:
        """Переименовать повреждённую базу (и её -wal/-shm) в ``rupor.db.broken-<время>``."""
        stamp = time.strftime("%Y%m%d-%H%M%S")
        target = self.path.with_name(f"{self.path.name}.broken-{stamp}")
        for suffix in ("", "-wal", "-shm"):
            src = Path(str(self.path) + suffix)
            if src.exists():
                src.replace(Path(str(target) + suffix))
        return target

    def _migrate(self, conn: sqlite3.Connection) -> None:
        ver = int(conn.execute("PRAGMA user_version").fetchone()[0])
        if ver > SCHEMA_VERSION:
            log.warning("База новее программы (версия схемы %d > %d) — работаю как есть",
                        ver, SCHEMA_VERSION)
            return
        for target in range(ver + 1, SCHEMA_VERSION + 1):
            conn.execute("BEGIN IMMEDIATE")
            try:
                for step in _MIGRATIONS[target - 1]:
                    if isinstance(step, str):
                        conn.execute(step)
                    else:
                        step(conn)
                conn.execute(f"PRAGMA user_version = {target}")
                conn.execute("COMMIT")
            except BaseException:
                _rollback(conn)
                raise
            log.info("Схема базы обновлена до версии %d", target)
        self._vocab_check_version(conn)

    def _vocab_check_version(self, conn: sqlite3.Connection) -> None:
        """Частоты собраны не по текущей версии отбора слов (миграция на схему 3, новая
        версия программы) — их надо пересобрать по истории, иначе вычитание при удалении
        истории разойдётся с тем, что когда-то прибавили. Здесь только отмечаем это:
        пересборка — после открытия базы (``_open``: сразу, а с ``background`` — когда
        движок позовёт ``vocab_rebuild_async``). Её сбой открытию базы не мешает."""
        self._vocab_pending = False
        try:
            r = conn.execute("SELECT value FROM kv WHERE key = ?",
                             (VOCAB_VERSION_KEY,)).fetchone()
            conn.execute("SELECT 1 FROM vocab LIMIT 1").fetchall()
        except sqlite3.OperationalError as e:      # нет таблиц (база новее программы)
            log.warning("Словарь частот не проверен: %s", e)
            return
        if r is not None and r[0] == vocab.version():
            return
        if conn.execute("SELECT 1 FROM history LIMIT 1").fetchone() is None:
            # истории нет (новая база) — пустые частоты ей и так соответствуют
            conn.execute("DELETE FROM vocab")
            conn.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                         "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                         (VOCAB_VERSION_KEY, vocab.version()))
            return
        self._vocab_pending = True

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """Транзакция на запись (BEGIN IMMEDIATE — сразу берём блокировку записи)."""
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
                self._conn.execute("COMMIT")   # упал и COMMIT (диск полон) — тоже откат
            except BaseException:
                _rollback(self._conn)
                raise

    def _checkpoint(self) -> None:
        """После массового удаления — перенести WAL в базу и обрезать его, чтобы старый
        текст не лежал в ``-wal``. Не ждём чужих читателей дольше 0,2 с: не вышло —
        не страшно, SQLite сделает это сам позже."""
        with self._lock:
            try:
                self._conn.execute("PRAGMA busy_timeout = 200")
                self._conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchall()
            except sqlite3.Error as e:
                log.debug("wal_checkpoint не удался: %s", e)
            finally:
                self._conn.execute(f"PRAGMA busy_timeout = {int(BUSY_TIMEOUT_S * 1000)}")

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._conn.close()

    def __enter__(self) -> Storage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ---------- история ----------

    @staticmethod
    def _history_out(r: sqlite3.Row) -> HistoryItem:
        return {
            "id": int(r["id"]),
            "ts": float(r["ts"]),
            "app": r["app"] or "",
            "title": r["title"] or "",
            "raw": r["raw"] or "",
            "text": r["text"] or "",
            "duration_s": float(r["duration_s"] or 0.0),
            "words": int(r["words"] or 0),
            "language": r["language"] or "",
            "style": r["style"] or "",
            "ai": bool(r["ai"]),
            "latency_ms": int(r["latency_ms"] or 0),
            "target": r["target"] or "window",
        }

    def add_history(self, item: dict[str, Any]) -> HistoryItem:
        """Добавить запись (поля ``HistoryItem`` без ``id``) → запись с ``id``.

        Недостающее заполняется: ``ts`` — сейчас, ``words`` — по ``text``, ``text``/``raw`` —
        друг другом, ``target`` — "window". Лишние ключи и ``id`` игнорируются."""
        raw_v, text_v = item.get("raw"), item.get("text")
        raw = _str(raw_v) if raw_v is not None else _str(text_v)
        text = _str(text_v) if text_v is not None else raw
        ts = _num(item.get("ts"), 0.0) or time.time()
        words = item.get("words")
        row: HistoryItem = {
            "ts": ts,
            "app": _str(item.get("app")),
            "title": _str(item.get("title")),
            "raw": raw,
            "text": text,
            "duration_s": max(0.0, _num(item.get("duration_s"))),
            "words": max(0, _int(words)) if words is not None else textproc.count_words(text),
            "language": _str(item.get("language")),
            "style": _str(item.get("style")),
            "ai": bool(item.get("ai")),
            "latency_ms": max(0, _int(item.get("latency_ms"))),
            "target": _str(item.get("target")) or "window",
        }
        values = [int(row[c]) if c == "ai" else row[c] for c in _HISTORY_COLS]
        cnt = _vocab_counts(row["text"], row["target"])      # разбор — до замка базы
        app = _vocab_app(row["app"])
        with self._tx() as c:
            cur = c.execute(f"INSERT INTO history({', '.join(_HISTORY_COLS)}) "
                            f"VALUES({', '.join('?' * len(_HISTORY_COLS))})", values)
            row_id = int(cur.lastrowid or 0)
            if cnt:
                c.executemany(
                    "INSERT INTO vocab(word, app, count, last_ts) VALUES(?, ?, ?, ?) "
                    "ON CONFLICT(word, app) DO UPDATE SET count = count + excluded.count, "
                    "last_ts = max(last_ts, excluded.last_ts)",
                    [(w, app, n, float(ts)) for w, n in cnt.items()])
        if cnt:
            self._vocab_touched(app)
        return {"id": row_id, **row}

    @staticmethod
    def _search_where(query: str | None) -> tuple[str, list[Any]]:
        """Все слова запроса должны встретиться (в тексте, сыром тексте, приложении или
        заголовке окна), без учёта регистра и «ё». Спецсимволы LIKE не нужны — ищем instr.
        Служебные записи (проверка распознавания, рассылка в RDP) находятся и по подписям,
        которые показывает интерфейс, на любом языке: «Рупор», «Rupor», «Recognition test»
        (в базе у них нейтральные app/title — engine.history_display)."""
        tokens = textproc.fold(query or "").split()
        if not tokens:
            return "", []
        # CAST AS BLOB: запись с битым UTF-8 не роняет поиск (py_fold декодирует с заменой)
        hay = ("py_fold(CAST(coalesce(text, '') || char(10) || coalesce(raw, '') || char(10) || "
               "coalesce(app, '') || char(10) || coalesce(title, '') || char(10) || "
               f"{_history_labels_sql()} AS BLOB))")
        return " WHERE " + " AND ".join(f"instr({hay}, ?) > 0" for _ in tokens), list(tokens)

    def list_history(self, query: str | None = None, limit: int = 50,
                     offset: int = 0) -> tuple[list[HistoryItem], int]:
        """Страница истории (новые сверху) и общее число подходящих под запрос записей."""
        limit = max(0, min(_int(limit, 50), MAX_PAGE))
        offset = max(0, _int(offset))
        where, params = self._search_where(query)
        with self._lock:
            total = int(self._conn.execute(f"SELECT count(*) FROM history{where}",
                                           params).fetchone()[0])
            rows = self._conn.execute(
                f"SELECT * FROM history{where} ORDER BY ts DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        return [self._history_out(r) for r in rows], total

    def get_history(self, id: int) -> HistoryItem | None:
        with self._lock:
            r = self._conn.execute("SELECT * FROM history WHERE id = ?", (_int(id),)).fetchone()
        return self._history_out(r) if r is not None else None

    def delete_history(self, id: int) -> None:
        """Удалить запись (например, случайно надиктованный пароль): текст затирается
        и в базе, и в -wal (чекпоинт), даже если ядро потом убьют без закрытия базы."""
        self._delete_where(" WHERE id = ?", [_int(id)])

    def _delete_where(self, where: str, params: list[Any]) -> int:
        """Удалить записи истории и вычесть их слова из частот (vocab).

        Разбор текстов — до захвата замка на запись (на большой истории он долгий, а под
        замком стояли бы диктовка и WS-команды); под замком — одно вычитание по (слово,
        процесс) и одно удаление обнулившихся строк. Записи, появившиеся между чтением и
        удалением, разбираются уже под замком (их единицы)."""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT id, ts, app, target, text FROM history{where}", params).fetchall()
        parsed: dict[int, tuple[float, str, Counter[str]]] = {
            int(r[0]): (float(r[1] or 0.0), _vocab_app(r[2]), _vocab_counts(r[4], r[3]))
            for r in rows}
        del rows
        with self._lock:
            with self._tx() as c:
                now = [(int(r[0]), float(r[1] or 0.0)) for r in
                       c.execute(f"SELECT id, ts FROM history{where}", params).fetchall()]
                todo = [i for i, ts in now if i not in parsed or parsed[i][0] != ts]
                _parse_rows(c, todo, parsed)
                agg: Counter[tuple[str, str]] = Counter()
                for i, _ts in now:
                    _t, app, cnt = parsed[i]
                    for w, k in cnt.items():
                        agg[(w, app)] += k
                if agg:
                    c.executemany("UPDATE vocab SET count = count - ? WHERE word = ? AND app = ?",
                                  [(k, w, a) for (w, a), k in agg.items()])
                    # один просмотр таблицы быстрее тысяч удалений по ключу
                    c.execute("DELETE FROM vocab WHERE count <= 0")
                n = c.execute(f"DELETE FROM history{where}", params).rowcount
            if agg:
                self._vocab_reset_cache()
            if n:
                self._checkpoint()
        return int(n)

    def clear_history(self, period: str, now: float | None = None) -> int:
        """Удалить записи **за** последний период: day — 24 ч, week — 7 дней, month — 30 дней,
        all — все. Возвращает число удалённых."""
        if period == "all":
            n = self._clear_all()
        elif period in PERIOD_DAYS:
            cutoff = (time.time() if now is None else now) - PERIOD_DAYS[period] * DAY_S
            n = self._delete_where(" WHERE ts >= ?", [cutoff])
        else:
            raise ValueError(tr("st.period_unknown", period=period))
        log.info("Очистка истории (%s): удалено %d", period, n)
        return int(n)

    def _clear_all(self) -> int:
        """«Очистить всё»: история, частоты слов (производные от неё) и исключённые из них
        слова — это тоже слова из диктовок (часто имена), а после очистки их не видно ни
        в одном списке."""
        with self._lock:
            with self._tx() as c:
                c.execute("DELETE FROM vocab")
                c.execute("DELETE FROM vocab_excluded")
                n = c.execute("DELETE FROM history").rowcount
                # пустые частоты соответствуют пустой истории при любой версии отбора
                c.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                          "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                          (VOCAB_VERSION_KEY, vocab.version()))
            self._vocab_pending = False
            self._vocab_reset_cache()
            self._checkpoint()
        self._remove_broken_copies()
        return int(n)

    def _remove_broken_copies(self) -> None:
        """«Очистить всё» — это и копии повреждённой базы (rupor.db.broken-*): в них та же
        история, и срок хранения на них не действует."""
        if str(self.path) == ":memory:":
            return
        for p in self.path.parent.glob(f"{self.path.name}.broken-*"):
            try:
                p.unlink()
                log.info("Удалена копия повреждённой базы %s", p.name)
            except OSError as e:
                log.warning("Не удалось удалить %s: %s", p.name, e)

    def purge_older_than(self, days: int, now: float | None = None) -> int:
        """Срок хранения: удалить записи старше ``days`` суток (0 и меньше — хранить всегда)."""
        days = _int(days)
        if days <= 0:
            return 0
        cutoff = (time.time() if now is None else now) - days * DAY_S
        n = self._delete_where(" WHERE ts < ?", [cutoff])
        if n:
            log.info("Срок хранения %d дн.: удалено записей истории %d", days, n)
        return int(n)

    # ---------- статистика ----------

    def record_stats(self, words: int, speech_s: float, when: float | None = None) -> None:
        """Учесть одну диктовку в статистике дня (дата — локальная, по ``when`` или сейчас)."""
        d = _local_date(when).isoformat()
        w = max(0, _int(words))
        s = max(0.0, _num(speech_s))
        with self._tx() as c:
            c.execute(
                "INSERT INTO stats_daily(date, words, speech_s, dictations) VALUES(?, ?, ?, 1) "
                "ON CONFLICT(date) DO UPDATE SET words = words + excluded.words, "
                "speech_s = speech_s + excluded.speech_s, dictations = dictations + 1",
                (d, w, s))

    def stats(self, today: date | None = None) -> Stats:
        """Статистика для главного экрана (тип ``Stats``).

        - words_today / words_week (последние 7 дней, включая сегодня) / words_total;
        - wpm — слова / минуты речи за всё время (0, если речи не было);
        - saved_minutes — max(0, words_total/40 − минуты речи);
        - streak_days — дни подряд с ≥1 диктовкой, заканчивая сегодня, а если сегодня
          диктовок ещё не было — вчера;
        - daily — ровно 30 дней (от сегодня−29 до сегодня), дни без диктовок с нулём."""
        today = today or date.today()
        with self._lock:
            rows = self._conn.execute(
                "SELECT date, words, speech_s, dictations FROM stats_daily").fetchall()
        words_total = 0
        speech_total = 0.0
        dictations_total = 0
        by_day: dict[date, int] = {}
        active: set[date] = set()
        for r in rows:
            w, s, n = int(r["words"] or 0), float(r["speech_s"] or 0.0), int(r["dictations"] or 0)
            words_total += w
            speech_total += s
            dictations_total += n
            try:
                d = date.fromisoformat(str(r["date"]))
            except ValueError:
                continue
            by_day[d] = by_day.get(d, 0) + w
            if n > 0:
                active.add(d)
        week = sum(by_day.get(today - timedelta(days=i), 0) for i in range(WEEK_DAYS))
        minutes = speech_total / 60
        streak = 0
        day = today if today in active else today - timedelta(days=1)
        while day in active:
            streak += 1
            day -= timedelta(days=1)
        first = today - timedelta(days=DAILY_DAYS - 1)
        days = [first + timedelta(days=i) for i in range(DAILY_DAYS)]
        daily = [{"date": d.isoformat(), "words": by_day.get(d, 0)} for d in days]
        return {
            "words_today": by_day.get(today, 0),
            "words_week": week,
            "words_total": words_total,
            "wpm": round(words_total / minutes, 1) if minutes > 0 else 0.0,
            "saved_minutes": round(max(0.0, words_total / TYPING_WPM - minutes), 1),
            "streak_days": streak,
            "dictations_total": dictations_total,
            "daily": daily,
        }

    # ---------- словарь ----------

    @staticmethod
    def _dict_out(r: sqlite3.Row) -> DictItem:
        return {"id": int(r["id"]), "term": r["term"], "variants": _load_variants(r["variants"])}

    def dict_list(self) -> list[DictItem]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM dictionary").fetchall()
        items = [self._dict_out(r) for r in rows]
        items.sort(key=lambda it: (textproc.fold(it["term"]), it["id"]))
        return items

    def _dict_find(self, c: sqlite3.Connection, term: str) -> sqlite3.Row | None:
        row: sqlite3.Row | None = c.execute(
            "SELECT * FROM dictionary WHERE py_fold(term) = ? ORDER BY id LIMIT 1",
            (textproc.fold(term),)).fetchone()
        return row

    def _dict_upsert_locked(self, c: sqlite3.Connection, id: int | None, term: str,
                            variants: list[str]) -> DictItem:
        same = self._dict_find(c, term)
        if id is None and same is not None:
            # Такой термин уже есть — дополняем его варианты (ничего не теряется).
            id = int(same["id"])
            variants = _clean_variants([*_load_variants(same["variants"]), *variants])
        elif id is not None and same is not None and int(same["id"]) != id:
            raise ValueError(tr("st.term_exists", term=same["term"]))
        payload = json.dumps(variants, ensure_ascii=False)
        if id is not None:
            cur = c.execute("UPDATE dictionary SET term = ?, variants = ? WHERE id = ?",
                            (term, payload, id))
            if cur.rowcount:
                return {"id": id, "term": term, "variants": variants}
        cur = c.execute("INSERT INTO dictionary(term, variants) VALUES(?, ?)", (term, payload))
        return {"id": int(cur.lastrowid or 0), "term": term, "variants": variants}

    def dict_upsert(self, id: int | None, term: str, variants: list[str]) -> DictItem:
        """Создать или изменить запись словаря.

        - ``id`` задан — меняем эту запись (если её уже нет — создаётся новая);
        - ``id`` не задан, а термин уже есть (без учёта регистра и «ё», в т. ч. кириллица) —
          варианты добавляются к существующим, написание термина берётся новое
          («сбербанк» после «Сбербанк» → одна запись «сбербанк»);
        - варианты чистятся: пробелы по краям, пустые и дубли убираются."""
        term = _clean_term(_str(term))
        if not term:
            raise ValueError(tr("st.term_empty"))
        clean = _clean_variants(variants)
        try:
            with self._tx() as c:
                return self._dict_upsert_locked(c, None if id is None else _int(id), term, clean)
        except sqlite3.IntegrityError as e:
            raise ValueError(tr("st.term_exists", term=term)) from e

    def dict_delete(self, id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM dictionary WHERE id = ?", (_int(id),))
            self._journal_drop(c, _int(id))

    # ---------- словарь: обучение на правках (engine → learn) ----------
    #
    # Журнал обучения (kv ``learn_journal``): что именно изменила каждая выученная правка —
    # {id, term, variant, created, owns, ts, source}. «Отменить» откатывает только по записи
    # журнала и стирает её: повторное нажатие, id, заново выданный SQLite ручному термину,
    # и правки, сделанные после, не страдают. owns — запись словаря создана обучением и ещё
    # «принадлежит» этой правке (при отмене переходит к следующей правке того же термина).
    # ts — когда (unix, с), source — кто добавил: "auto" — Рупор сам, "accepted" — пользователь
    # нажал «Добавить» (у записей из версий без этих полей — None). Журнал же показывается
    # на странице «Словарь» («Недавно добавлено», dict_learn_journal).

    @staticmethod
    def _journal_load(c: sqlite3.Connection) -> list[dict[str, Any]]:
        r = c.execute("SELECT value FROM kv WHERE key = ?", (LEARN_JOURNAL_KEY,)).fetchone()
        try:
            data = json.loads(r["value"]) if r is not None and r["value"] else []
        except ValueError:
            data = []
        out: list[dict[str, Any]] = []
        for e in data if isinstance(data, list) else []:
            if (isinstance(e, dict) and isinstance(e.get("id"), int)
                    and isinstance(e.get("term"), str) and isinstance(e.get("variant"), str)):
                ts = e.get("ts")
                source = e.get("source")
                out.append({"id": e["id"], "term": e["term"], "variant": e["variant"],
                            "created": e.get("created") is True, "owns": e.get("owns") is True,
                            "ts": float(ts) if (isinstance(ts, (int, float))
                                                and not isinstance(ts, bool)
                                                and math.isfinite(ts)) else None,
                            "source": source if source in LEARN_SOURCES else None})
        return out

    @staticmethod
    def _journal_save(c: sqlite3.Connection, entries: list[dict[str, Any]]) -> None:
        c.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                  "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                  (LEARN_JOURNAL_KEY,
                   json.dumps(entries[-LEARN_JOURNAL_MAX:], ensure_ascii=False)))

    def _journal_drop(self, c: sqlite3.Connection, id: int | None) -> None:
        """Запись словаря удалена вручную (id=None — весь словарь): её правки не откатываются."""
        entries = self._journal_load(c)
        rest = [] if id is None else [e for e in entries if e["id"] != id]
        if len(rest) != len(entries):
            self._journal_save(c, rest)

    def dict_learn_add(self, term: str, variant: str = "", journal: bool = True,
                       source: str = "auto") -> tuple[DictItem, bool]:
        """Выученная правка «вариант → термин». Термин уже есть (без учёта регистра и «ё») —
        вариант добавляется к нему без дублей, написание термина не меняется; нет — создаётся
        новая запись. Возвращает (запись, создана ли она). Вариант, который уже ведёт
        к другому термину, — ``ValueError``: одна замена не может давать два результата.
        journal — записать изменение в журнал обучения (для «Отменить» и «Недавно
        добавлено») с временем и source ("auto" — добавил Рупор, "accepted" — пользователь
        нажал «Добавить»); если словарь не изменился (такой вариант уже был), записывать
        нечего."""
        stamp = {"ts": time.time(), "source": source}
        term = _clean_term(_str(term))
        variant = _clean_term(_str(variant))
        if not term:
            raise ValueError(tr("st.term_empty"))
        fv = textproc.fold(variant)
        if fv == textproc.fold(term):
            variant, fv = "", ""
        with self._tx() as c:
            if fv:
                for r in c.execute("SELECT * FROM dictionary").fetchall():
                    if (textproc.fold(r["term"]) != textproc.fold(term)
                            and fv in {textproc.fold(v) for v in _load_variants(r["variants"])}):
                        raise ValueError(tr("st.variant_taken", variant=variant,
                                            term=r["term"]))
            same = self._dict_find(c, term)
            if same is None:
                variants = [variant] if variant else []
                cur = c.execute("INSERT INTO dictionary(term, variants) VALUES(?, ?)",
                                (term, json.dumps(variants, ensure_ascii=False)))
                item: DictItem = {"id": int(cur.lastrowid or 0), "term": term,
                                  "variants": variants}
                if journal:
                    self._journal_save(c, [*self._journal_load(c), {
                        "id": item["id"], "term": term, "variant": variant, "created": True,
                        "owns": True, **stamp}])
                return item, True
            variants = _load_variants(same["variants"])
            if variant and fv not in {textproc.fold(v) for v in variants}:
                variants.append(variant)
                c.execute("UPDATE dictionary SET variants = ? WHERE id = ?",
                          (json.dumps(variants, ensure_ascii=False), int(same["id"])))
                if journal:
                    self._journal_save(c, [*self._journal_load(c), {
                        "id": int(same["id"]), "term": same["term"], "variant": variant,
                        "created": False, "owns": False, **stamp}])
            return {"id": int(same["id"]), "term": same["term"], "variants": variants}, False

    def dict_learn_journal(self, limit: int = 50) -> list[dict[str, Any]]:
        """«Недавно добавлено» на странице «Словарь»: последние записи журнала обучения,
        новые первыми — {id, term, variant, created, ts, source} (как аргументы
        dict_learn_undo: «Отменить» в строке = та же отмена, запись журнала стирается).
        Записи, чья запись словаря удалена или переименована (отмена там ничего не сделает),
        не показываются. limit — не больше LEARN_JOURNAL_MAX."""
        limit = max(0, min(int(limit), LEARN_JOURNAL_MAX))
        out: list[dict[str, Any]] = []
        if not limit:
            return out
        with self._lock:
            entries = self._journal_load(self._conn)
            rows = {int(r["id"]): textproc.fold(r["term"]) for r in
                    self._conn.execute("SELECT id, term FROM dictionary").fetchall()}
        for e in reversed(entries):
            if rows.get(e["id"]) != textproc.fold(e["term"]):
                continue
            out.append({"id": e["id"], "term": e["term"], "variant": e["variant"],
                        "created": e["created"], "ts": e["ts"], "source": e["source"]})
            if len(out) >= limit:
                break
        return out

    def dict_learn_undo(self, term: str, variant: str = "", id: int | None = None,
                        created: bool = False) -> bool:
        """Отменить выученную правку — один раз, по записи журнала обучения (term, variant,
        created; при нескольких — с этим id). Записи нет (уже отменено, правка ничего не
        меняла, термин удалён вручную) — ничего не делаем. Иначе:

        - убирается этот вариант;
        - термин, созданный этой правкой (created), удаляется целиком, только если в нём не
          осталось других вариантов (выученных позже или добавленных вручную) и его
          написание не меняли; иначе «владение» переходит к следующей правке этого термина.
        Возвращает, изменилось ли что-нибудь в словаре."""
        ft = textproc.fold(_clean_term(_str(term)))
        fv = textproc.fold(_clean_term(_str(variant)))
        with self._tx() as c:
            entries = self._journal_load(c)
            match = [i for i, e in enumerate(entries)
                     if textproc.fold(e["term"]) == ft and textproc.fold(e["variant"]) == fv
                     and e["created"] == bool(created)]
            if not match:
                return False
            exact = [i for i in match if id is not None and entries[i]["id"] == id]
            e = entries.pop((exact or match)[-1])
            row = c.execute("SELECT * FROM dictionary WHERE id = ?", (e["id"],)).fetchone()
            if row is None or textproc.fold(row["term"]) != textproc.fold(e["term"]):
                self._journal_save(c, entries)
                return False
            variants = _load_variants(row["variants"])
            rest = [v for v in variants if textproc.fold(v) != fv] if fv else variants
            others = [x for x in entries if x["id"] == e["id"]]
            if e["owns"] and not rest and not others and row["term"] == e["term"]:
                c.execute("DELETE FROM dictionary WHERE id = ?", (e["id"],))
                self._journal_save(c, entries)
                return True
            if e["owns"] and others:
                others[-1]["owns"] = True
            if len(rest) != len(variants):
                c.execute("UPDATE dictionary SET variants = ? WHERE id = ?",
                          (json.dumps(rest, ensure_ascii=False), e["id"]))
            self._journal_save(c, entries)
            return len(rest) != len(variants)

    # ---------- сниппеты ----------

    @staticmethod
    def _snippet_out(r: sqlite3.Row) -> Snippet:
        return {"id": int(r["id"]), "trigger": r["trigger"], "text": r["text"]}

    def snippets_list(self) -> list[Snippet]:
        with self._lock:
            rows = self._conn.execute("SELECT * FROM snippets").fetchall()
        items = [self._snippet_out(r) for r in rows]
        items.sort(key=lambda it: (textproc.fold(it["trigger"]), it["id"]))
        return items

    def _snippet_find(self, c: sqlite3.Connection, trigger: str) -> sqlite3.Row | None:
        key = textproc.normalize_phrase(trigger)
        for r in c.execute("SELECT * FROM snippets").fetchall():
            if textproc.normalize_phrase(r["trigger"]) == key:
                row: sqlite3.Row = r
                return row
        return None

    def _snippet_upsert_locked(self, c: sqlite3.Connection, id: int | None, trigger: str,
                               text: str) -> Snippet:
        same = self._snippet_find(c, trigger)
        if id is None and same is not None:
            id = int(same["id"])                      # тот же триггер — меняем текст
        elif id is not None and same is not None and int(same["id"]) != id:
            raise ValueError(tr("st.snippet_exists", trigger=same["trigger"]))
        if id is not None:
            cur = c.execute("UPDATE snippets SET trigger = ?, text = ? WHERE id = ?",
                            (trigger, text, id))
            if cur.rowcount:
                return {"id": id, "trigger": trigger, "text": text}
        cur = c.execute("INSERT INTO snippets(trigger, text) VALUES(?, ?)", (trigger, text))
        return {"id": int(cur.lastrowid or 0), "trigger": trigger, "text": text}

    def snippet_upsert(self, id: int | None, trigger: str, text: str) -> Snippet:
        """Создать или изменить сниппет. Триггеры уникальны по нормальной форме
        (регистр, «ё», пунктуация по краям, лишние пробелы не важны)."""
        trigger = _clean_term(_str(trigger))
        text = _str(text)
        if not textproc.normalize_phrase(trigger):
            raise ValueError(tr("st.trigger_empty"))
        if not text.strip():
            raise ValueError(tr("st.snippet_text_empty"))
        try:
            with self._tx() as c:
                return self._snippet_upsert_locked(c, None if id is None else _int(id),
                                                   trigger, text)
        except sqlite3.IntegrityError as e:
            raise ValueError(tr("st.snippet_exists", trigger=trigger)) from e

    def snippet_delete(self, id: int) -> None:
        with self._tx() as c:
            c.execute("DELETE FROM snippets WHERE id = ?", (_int(id),))

    # ---------- импорт / экспорт ----------

    def export_data(self) -> dict[str, Any]:
        return {
            "version": 1,
            "dictionary": [{"term": d["term"], "variants": d["variants"]} for d in self.dict_list()],
            "snippets": [{"trigger": s["trigger"], "text": s["text"]} for s in self.snippets_list()],
        }

    @staticmethod
    def _parse_import(data: Any) -> tuple[list[tuple[str, list[str]]], list[tuple[str, str]], int]:
        """Проверить данные импорта. Битые записи пропускаются (их число — третий элемент);
        данные целиком не того вида — ``ValueError``."""
        if isinstance(data, (str, bytes)):
            try:
                data = json.loads(data)
            except ValueError as e:
                raise ValueError(tr("st.import_not_json")) from e
        if not isinstance(data, dict):
            raise ValueError(tr("st.import_bad_format"))
        if "dictionary" not in data and "snippets" not in data:
            raise ValueError(tr("st.import_empty"))
        version = data.get("version", 1)
        if version != 1:
            log.warning("Импорт: версия формата %r, ожидалась 1 — пробую прочитать", version)

        def as_list(v: Any, a: str, b: str) -> list[Any]:
            if isinstance(v, dict):                   # {термин: [варианты]} / {триггер: текст}
                return [{a: k, b: x} for k, x in v.items()]
            return v if isinstance(v, list) else []

        skipped = 0
        dict_raw = data.get("dictionary") or []
        snip_raw = data.get("snippets") or []
        if not isinstance(dict_raw, (list, dict)):
            skipped += 1
        if not isinstance(snip_raw, (list, dict)):
            skipped += 1
        dictionary: list[tuple[str, list[str]]] = []
        for it in as_list(dict_raw, "term", "variants"):
            term = it.get("term") if isinstance(it, dict) else None
            vars_raw = it.get("variants", []) if isinstance(it, dict) else None
            if (not isinstance(term, str) or not _clean_term(term)
                    or not isinstance(vars_raw, (list, tuple, str, type(None)))):
                skipped += 1
                continue
            dictionary.append((_clean_term(term), _clean_variants(vars_raw or [])))
        snippets: list[tuple[str, str]] = []
        for it in as_list(snip_raw, "trigger", "text"):
            trig = it.get("trigger") if isinstance(it, dict) else None
            body = it.get("text") if isinstance(it, dict) else None
            if (not isinstance(trig, str) or not textproc.normalize_phrase(trig)
                    or not isinstance(body, str) or not body.strip()):
                skipped += 1
                continue
            snippets.append((_clean_term(trig), body))
        return dictionary, snippets, skipped

    def import_data(self, data: dict[str, Any], replace: bool) -> dict[str, int]:
        """Импорт словаря и сниппетов (формат ``export_data``) одной транзакцией.

        ``replace=True`` — сначала очистить словарь и сниппеты. Иначе слияние: варианты
        существующих терминов дополняются, текст существующих сниппетов заменяется.
        Битые записи пропускаются. Результат — сколько терминов и сниппетов добавлено
        или обновлено."""
        dictionary, snippets, skipped = self._parse_import(data)
        d_keys: set[str] = set()
        s_keys: set[str] = set()
        with self._tx() as c:
            if replace:
                c.execute("DELETE FROM dictionary")
                c.execute("DELETE FROM snippets")
                self._journal_drop(c, None)
            for term, variants in dictionary:
                self._dict_upsert_locked(c, None, term, variants)
                d_keys.add(textproc.fold(term))
            for trig, body in snippets:
                self._snippet_upsert_locked(c, None, trig, body)
                s_keys.add(textproc.normalize_phrase(trig))
        if skipped:
            log.warning("Импорт: пропущено битых записей: %d", skipped)
        log.info("Импорт (%s): терминов %d, сниппетов %d",
                 "замена" if replace else "слияние", len(d_keys), len(s_keys))
        return {"dictionary": len(d_keys), "snippets": len(s_keys)}

    # ---------- ключ-значение (черновик и пр.) ----------

    def kv_get(self, key: str, default: str = "") -> str:
        with self._lock:
            r = self._conn.execute("SELECT value FROM kv WHERE key = ?", (key,)).fetchone()
        return default if r is None or r["value"] is None else str(r["value"])

    def kv_set(self, key: str, value: str) -> None:
        with self._tx() as c:
            c.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                      "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, _str(value)))

    # ---------- расход ИИ ----------

    def add_ai_usage(self, rec: dict[str, Any]) -> dict[str, Any]:
        """Записать один вызов ИИ (ai_usage). Ошибка вызова (ok=false) — стоимость 0.
        Неизвестные токены/стоимость (claude -p) — NULL, в суммах считаются нулём."""
        kind = _str(rec.get("kind"))
        if kind not in AI_KINDS:
            raise ValueError(tr("st.ai_kind_unknown", kind=kind, kinds=", ".join(AI_KINDS)))
        ok = bool(rec.get("ok", True))
        cost = rec.get("cost_usd")
        row: dict[str, Any] = {
            "ts": _num(rec.get("ts"), 0.0) or time.time(),
            "kind": kind,
            "provider": _str(rec.get("provider")),
            "model": _str(rec.get("model")),
            "upstream": _str(rec.get("upstream")) or None,
            "tokens_in": _opt_int(rec.get("tokens_in")),
            "tokens_out": _opt_int(rec.get("tokens_out")),
            "cost_usd": (max(0.0, _num(cost)) if cost is not None else None) if ok else 0.0,
            "ms": max(0, _int(rec.get("ms"))),
            "ok": ok,
            "error_kind": None if ok else (_str(rec.get("error_kind")) or "error"),
            "app": _str(rec.get("app")),
            "key_id": _str(rec.get("key_id")) or None,
        }
        cols = ("ts", "kind", "provider", "model", "upstream", "tokens_in", "tokens_out",
                "cost_usd", "ms", "ok", "error_kind", "app", "key_id")
        with self._tx() as c:
            cur = c.execute(f"INSERT INTO ai_usage({', '.join(cols)}) "
                            f"VALUES({', '.join('?' * len(cols))})",
                            [int(row[k]) if k == "ok" else row[k] for k in cols])
            row_id = int(cur.lastrowid or 0)
        return {"id": row_id, **row}

    def ai_key_spend(self, key_id: str, since: float = 0.0) -> dict[str, Any]:
        """Расход через ключ из списка ключей ИИ с момента ``since`` (unix, с):
        {cost_usd, calls} — calls только удачные."""
        with self._lock:
            r = self._conn.execute("SELECT total(cost_usd), total(ok) FROM ai_usage "
                                   "WHERE key_id = ? AND ts >= ?", (key_id, since)).fetchone()
        return {"cost_usd": round(float(r[0] or 0.0), 6), "calls": int(r[1] or 0)}

    def ai_usage_today_cost(self, now: float | None = None) -> float:
        """Сколько стоили вызовы ИИ сегодня (локальная дата)."""
        day = _local_date(now)
        start = datetime.combine(day, datetime.min.time()).timestamp()
        end = datetime.combine(day + timedelta(days=1), datetime.min.time()).timestamp()
        with self._lock:
            r = self._conn.execute("SELECT total(cost_usd) FROM ai_usage WHERE ts >= ? "
                                   "AND ts < ?", (start, end)).fetchone()
        return round(float(r[0] or 0.0), 6)

    def ai_usage_stats(self, today: date | None = None) -> dict[str, Any]:
        """Сводка расхода ИИ (WS ai_usage_stats): today / week (7 дней) / month (30 дней) /
        total — {calls, tokens_in, tokens_out, cost_usd, unknown_cost_calls}; avg_cost_per_call
        — средняя стоимость успешного вызова с известной ценой; daily — ровно 30 дней
        с нулями; by_kind — все виды (cleanup, command, test) за всё время; by_model —
        модели за всё время, дорогие первыми (+ unknown_cost_calls).

        unknown_cost_calls — успешные вызовы, цена которых неизвестна (claude -p по
        подписке, модель Anthropic не из таблицы цен): их стоимость в cost_usd не входит
        (там $0), интерфейс может написать «цена неизвестна», а не «$0»."""
        today = today or date.today()
        with self._lock:
            days = self._conn.execute(
                "SELECT date(ts, 'unixepoch', 'localtime') AS d, count(*) AS n, "
                "total(tokens_in) AS ti, total(tokens_out) AS tout, total(cost_usd) AS c, "
                "total(ok = 1 AND cost_usd IS NULL) AS u FROM ai_usage GROUP BY d").fetchall()
            kinds = self._conn.execute(
                "SELECT kind, count(*) AS n, total(cost_usd) AS c FROM ai_usage "
                "GROUP BY kind").fetchall()
            models = self._conn.execute(
                "SELECT coalesce(model, '') AS m, count(*) AS n, total(cost_usd) AS c, "
                "total(ok = 1 AND cost_usd IS NULL) AS u "
                "FROM ai_usage GROUP BY m ORDER BY c DESC, n DESC, m").fetchall()
            ok_row = self._conn.execute(
                "SELECT count(*), total(cost_usd) FROM ai_usage "
                "WHERE ok = 1 AND cost_usd IS NOT NULL").fetchone()
        zero: _Sum4 = (0, 0, 0, 0.0)
        by_day: dict[date, _Sum4] = {}
        unknown: dict[date, int] = {}
        total = zero
        unknown_total = 0
        for r in days:
            vals: _Sum4 = (int(r["n"]), int(r["ti"] or 0), int(r["tout"] or 0),
                           float(r["c"] or 0.0))
            total = _add4(total, vals)
            unknown_total += int(r["u"] or 0)
            try:
                d = date.fromisoformat(str(r["d"]))
            except ValueError:
                continue
            by_day[d] = _add4(by_day.get(d, zero), vals)
            unknown[d] = unknown.get(d, 0) + int(r["u"] or 0)

        def span(n_days: int) -> _Sum4:
            acc = zero
            for i in range(n_days):
                acc = _add4(acc, by_day.get(today - timedelta(days=i), zero))
            return acc

        def span_unknown(n_days: int) -> int:
            return sum(unknown.get(today - timedelta(days=i), 0) for i in range(n_days))

        first = today - timedelta(days=DAILY_DAYS - 1)
        daily = [{"date": d.isoformat(), **_pack4(by_day.get(d, zero))}
                 for d in (first + timedelta(days=i) for i in range(DAILY_DAYS))]
        kind_map = {str(r["kind"]): (int(r["n"]), float(r["c"] or 0.0)) for r in kinds}
        order = [*AI_KINDS, *sorted(k for k in kind_map if k not in AI_KINDS)]
        ok_n, ok_cost = int(ok_row[0] or 0), float(ok_row[1] or 0.0)
        return {
            "today": {**_pack4(by_day.get(today, zero)),
                      "unknown_cost_calls": span_unknown(1)},
            "week": {**_pack4(span(WEEK_DAYS)), "unknown_cost_calls": span_unknown(WEEK_DAYS)},
            "month": {**_pack4(span(MONTH_DAYS)),
                      "unknown_cost_calls": span_unknown(MONTH_DAYS)},
            "total": {**_pack4(total), "unknown_cost_calls": unknown_total},
            "avg_cost_per_call": round(ok_cost / ok_n, 6) if ok_n else 0.0,
            "daily": daily,
            "by_kind": [{"kind": k, "calls": kind_map.get(k, (0, 0.0))[0],
                         "cost_usd": round(kind_map.get(k, (0, 0.0))[1], 6)} for k in order],
            "by_model": [{"model": str(r["m"]), "calls": int(r["n"]),
                          "cost_usd": round(float(r["c"] or 0.0), 6),
                          "unknown_cost_calls": int(r["u"] or 0)} for r in models],
        }

    # ---------- личный словарь частот (vocab) ----------
    #
    # Производная от истории: при записи в историю частоты прибавляются, при удалении
    # записей (вручную, очистка, срок хранения) — вычитаются; «Очистить всё» стирает их.
    # Слова — только «характерные» (vocab.extract), по процессам (history.app в нижнем
    # регистре). Исключённые пользователем — в vocab_excluded (fold слова; действует на все
    # формы слова — vocab.excluded_keys).
    #
    # Частые слова для подсказки и списка считаются в SQL: самые частые VOCAB_FETCH_ROWS
    # строк процесса (или всех процессов), слияние форм — в Python; результат кэшируется
    # по процессу (см. __init__).

    def _vocab_reset_cache(self) -> None:
        with self._lock:
            self._vocab_top.clear()
            self._vocab_stale.clear()
            self._vocab_ex = None

    def _vocab_touched(self, app: str) -> None:
        """Новая диктовка: кэш частых слов этого процесса и общий устарели. С background —
        пересчитать в фоне (подсказка следующей диктовки не ждёт); иначе — при обращении."""
        start = False
        with self._lock:
            for k in (app, _ALL_APPS):
                if k in self._vocab_top:
                    self._vocab_stale.add(k)
            if self._background and self._vocab_stale and not self._vocab_refreshing:
                self._vocab_refreshing = start = True
        if start:
            threading.Thread(target=self._vocab_refresh_bg, name="rupor-vocab-cache",
                             daemon=True).start()

    def _vocab_refresh_bg(self) -> None:
        try:
            while True:
                with self._lock:
                    if self._closed or not self._vocab_stale:
                        self._vocab_refreshing = False
                        return
                    key = self._vocab_stale.pop()
                    if key in self._vocab_top:
                        self._vocab_top[key] = self._vocab_fetch(key)
        except Exception as e:
            with self._lock:
                self._vocab_refreshing = False
                self._vocab_top.clear()
                self._vocab_stale.clear()
                closed = self._closed
            if not closed:
                log.error("Не удалось обновить частые слова: %s", e)

    def _vocab_fetch(self, key: str) -> list[vocab.Word]:
        """Самые частые строки vocab процесса (key) или всех процессов → слова (формы
        слиты, частые первыми). Под self._lock."""
        if key == _ALL_APPS:
            # NOT INDEXED: просмотр таблицы с сортировкой быстрее обхода индекса (word, app)
            # с чтением каждой строки (≈ 50 мс против 180 мс на 95 тыс. строк)
            rows = self._conn.execute(
                "SELECT word, sum(count) AS n, max(last_ts) FROM vocab NOT INDEXED "
                "GROUP BY word ORDER BY n DESC LIMIT ?", (VOCAB_FETCH_ROWS,)).fetchall()
        else:
            rows = self._conn.execute(
                "SELECT word, sum(count) AS n, max(last_ts) FROM vocab WHERE app = ? "
                "GROUP BY word ORDER BY n DESC LIMIT ?", (key, VOCAB_FETCH_ROWS)).fetchall()
        return vocab.merge((str(r[0]), int(r[1] or 0), float(r[2] or 0.0)) for r in rows)

    def _vocab_words_locked(self, key: str) -> list[vocab.Word]:
        hit = self._vocab_top.get(key)
        if hit is not None and (self._background or key not in self._vocab_stale):
            self._vocab_top.move_to_end(key)
            return hit                       # с background устаревшее обновляется в фоне
        words = self._vocab_fetch(key)
        self._vocab_top[key] = words
        self._vocab_top.move_to_end(key)
        self._vocab_stale.discard(key)
        while len(self._vocab_top) > VOCAB_CACHE_APPS:
            old, _ = self._vocab_top.popitem(last=False)
            self._vocab_stale.discard(old)
        return words

    def _vocab_excluded_locked(self) -> frozenset[str]:
        if self._vocab_ex is None:
            self._vocab_ex = frozenset(
                str(r[0]) for r in self._conn.execute("SELECT word FROM vocab_excluded"))
        return self._vocab_ex

    def vocab_rebuild(self) -> int:
        """Пересчитать частоты по всей истории (миграция, новая версия отбора слов).

        Тексты читаются порциями и разбираются без замка базы: диктовка, запись истории и
        WS-команды в это время не ждут. В конце под замком одной транзакцией: учесть
        записи, добавленные или удалённые за время пересборки, и заменить таблицу vocab."""
        t0 = time.perf_counter()
        with self._rebuild_lock:
            parsed: dict[int, tuple[float, str, Counter[str]]] = {}
            last = 0
            while True:
                with self._lock:
                    if self._closed:
                        raise sqlite3.ProgrammingError("database is closed")
                    rows = self._conn.execute(
                        "SELECT id, ts, app, target, text FROM history WHERE id > ? "
                        "ORDER BY id LIMIT ?", (last, VOCAB_REBUILD_CHUNK)).fetchall()
                if not rows:
                    break
                for r in rows:
                    last = int(r[0])
                    parsed[last] = (float(r[1] or 0.0), _vocab_app(r[2]),
                                    _vocab_counts(r[4], r[3]))
            with self._lock:
                with self._tx() as c:
                    now = [(int(r[0]), float(r[1] or 0.0))
                           for r in c.execute("SELECT id, ts FROM history").fetchall()]
                    todo = [i for i, ts in now if i not in parsed or parsed[i][0] != ts]
                    _parse_rows(c, todo, parsed)
                    agg: dict[tuple[str, str], list[float]] = {}
                    for i, _ts in now:
                        ts, app, cnt = parsed[i]
                        for w, k in cnt.items():
                            cur = agg.setdefault((w, app), [0.0, 0.0])
                            cur[0] += k
                            cur[1] = max(cur[1], ts)
                    c.execute("DELETE FROM vocab")
                    c.executemany("INSERT INTO vocab(word, app, count, last_ts) VALUES(?, ?, ?, ?)",
                                  [(w, a, int(v[0]), v[1]) for (w, a), v in agg.items()])
                    c.execute("INSERT INTO kv(key, value) VALUES(?, ?) "
                              "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                              (VOCAB_VERSION_KEY, vocab.version()))
                self._vocab_pending = False
                self._vocab_reset_cache()
        log.info("Словарь частот пересчитан по истории: записей %d, слов %d, %.0f мс",
                 len(now), len(agg), (time.perf_counter() - t0) * 1000)
        return len(agg)

    def vocab_rebuild_async(self) -> bool:
        """Частоты надо пересобрать (см. background) — пересобрать в фоновом потоке.
        Возвращает, запущена ли пересборка."""
        if not self._vocab_pending or self._rebuild_lock.locked():
            return False
        threading.Thread(target=self._vocab_rebuild_bg, name="rupor-vocab", daemon=True).start()
        return True

    def _vocab_rebuild_bg(self) -> None:
        try:
            self.vocab_rebuild()
        except sqlite3.ProgrammingError:
            log.debug("Пересборка словаря частот прервана: база закрыта")
        except Exception:
            log.exception("Не удалось пересобрать словарь частот — подсказка без частых слов")

    @property
    def vocab_ready(self) -> bool:
        """Частоты соответствуют истории (нет незаконченной пересборки)."""
        return not self._vocab_pending

    def vocab_words(self, process: str | None = None) -> list[vocab.Word]:
        """Частые слова (все процессы или один): формы слиты, частые первыми; не больше
        VOCAB_FETCH_ROWS самых частых написаний."""
        key = _vocab_app(process) if process else _ALL_APPS
        with self._lock:
            return list(self._vocab_words_locked(key))

    def vocab_excluded(self) -> set[str]:
        with self._lock:
            return set(self._vocab_excluded_locked())

    def vocab_list(self, process: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        """WS vocab_list: [{word, count, excluded}] — до limit слов, которые идут в подсказку
        (достаточно частые), частые первыми, и после них — все исключённые пользователем
        слова (для процесса — встречавшиеся в нём): иначе исключение, чьи частоты малы
        или стёрты, нельзя было бы снять."""
        limit = max(0, min(_int(limit, 50), VOCAB_LIST_MAX))
        key = _vocab_app(process) if process else _ALL_APPS
        with self._lock:
            words = list(self._vocab_words_locked(key))
            raw = set(self._vocab_excluded_locked())
        keys = vocab.excluded_keys(raw)
        out: list[dict[str, Any]] = []
        shown: set[str] = set()
        for w in words:
            if len(out) >= limit:
                break
            ex = vocab.is_excluded(w, keys)
            if ex or vocab.eligible(w):
                out.append({"word": w.word, "count": w.count, "excluded": ex})
                shown |= {w.key, textproc.fold(w.word)}
        found = {w.key: w for w in words}
        for e in sorted(raw):
            gk = vocab.group_key(e)
            if e in shown or gk in shown:
                continue
            hit = found.get(gk) or found.get(e)
            if hit is None and process:
                continue
            shown |= {e, gk}
            out.append({"word": hit.word if hit is not None else e,
                        "count": hit.count if hit is not None else 0, "excluded": True})
        return out

    def vocab_exclude(self, word: str, excluded: bool) -> None:
        """Исключить слово из подсказок (или вернуть) — без учёта регистра, «ё» и формы
        слова: «Тимуру» исключает и «Тимура», возврат любой формы снимает исключение."""
        w = _clean_term(_str(word))
        if not w:
            raise ValueError(tr("st.word_empty"))
        if len(w) > 100:
            raise ValueError(tr("st.word_too_long", max=100))
        k = textproc.fold(w)
        gk = vocab.group_key(w)
        with self._tx() as c:
            if excluded:
                c.execute("INSERT OR IGNORE INTO vocab_excluded(word) VALUES(?)", (k,))
            else:
                raw = [str(r[0]) for r in c.execute("SELECT word FROM vocab_excluded")]
                gone = [e for e in raw if e in (k, gk) or vocab.group_key(e) == gk]
                c.executemany("DELETE FROM vocab_excluded WHERE word = ?", [(e,) for e in gone])
            self._vocab_ex = None

    def vocab_prompt_words(self, process: str | None, limit: int = vocab.MAX_FREQ_WORDS,
                           strong_only: bool = False,
                           reject: Callable[[str], bool] | None = None
                           ) -> tuple[list[str], list[str]]:
        """Частые слова для подсказки: (слова процесса, общие) без исключённых, только
        достаточно частые, всего не больше limit. strong_only — только термины и имена
        (для промпта ИИ-очистки) — из того же кэша, без второго подсчёта. reject — без
        ошибок распознавания из словаря (vocab.pick_words). Пока частоты пересобираются
        (vocab_ready = False) — пусто."""
        if self._vocab_pending:
            return [], []
        with self._lock:
            proc = self._vocab_words_locked(_vocab_app(process)) if process else []
            gen = self._vocab_words_locked(_ALL_APPS)
            ex = self._vocab_excluded_locked()
        return vocab.pick_words(proc, gen, ex, limit, strong_only, reject)


_Sum4 = tuple[int, int, int, float]


def _sql_literal(s: str) -> str:
    return "'" + s.replace("'", "''") + "'"


def _history_labels_sql() -> str:
    """Подписи служебных записей истории на всех языках интерфейса — для поиска (SQL CASE
    по target; текст — из каталогов сообщений, кавычки экранированы)."""
    def labels(*keys: str) -> str:
        return " ".join(i18n.tr_in(lang, k) for lang in i18n.UI_LANGS for k in keys)
    return (f"CASE target WHEN 'test' THEN {_sql_literal(labels('app.name', 'hist.test_title'))} "
            f"WHEN 'rdp_all' THEN {_sql_literal(labels('hist.rdp_title'))} ELSE '' END")


def _vocab_app(app: Any) -> str:
    return _str(app).strip().lower()


def _vocab_counts(text: Any, target: Any) -> Counter[str]:
    """Характерные слова записи истории; рассылка в RDP и команды не считаются."""
    if _str(target) not in vocab.VOCAB_TARGETS:
        return Counter()
    return vocab.counts(_str(text))


def _parse_rows(c: sqlite3.Connection, ids: list[int],
                into: dict[int, tuple[float, str, Counter[str]]]) -> None:
    """Разобрать записи истории по id (под замком — их единицы: появились за время
    разбора без замка)."""
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        for r in c.execute(f"SELECT id, ts, app, target, text FROM history WHERE id IN "
                           f"({', '.join('?' * len(chunk))})", chunk).fetchall():
            into[int(r[0])] = (float(r[1] or 0.0), _vocab_app(r[2]), _vocab_counts(r[4], r[3]))


def _opt_int(v: Any) -> int | None:
    if v is None or isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    if not math.isfinite(v):
        return None
    return max(0, int(v))


def _add4(a: _Sum4, b: _Sum4) -> _Sum4:
    return a[0] + b[0], a[1] + b[1], a[2] + b[2], a[3] + b[3]


def _pack4(v: _Sum4) -> dict[str, Any]:
    return {"calls": v[0], "tokens_in": v[1], "tokens_out": v[2], "cost_usd": round(v[3], 6)}
