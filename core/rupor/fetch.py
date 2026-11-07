"""Загрузки первого запуска: модель распознавания и библиотеки CUDA.

* Модель — в кэш HuggingFace, там же её ищет faster-whisper (``WhisperModel(...,
  local_files_only=True)``). Скачиваются только нужные файлы, как в
  ``faster_whisper.utils.download_model``. Папка кэша — настройка ``model_dir`` (``cache_dir``
  функций; ``None`` — кэш HuggingFace по умолчанию), тот же формат, что ``download_root``
  у faster-whisper. ``move_model`` переносит скачанную модель в другую папку,
  ``remove_model`` удаляет её из прежней.
* Библиотеки CUDA — колесо ``nvidia-cublas-cu12`` с PyPI; из него извлекаются только
  ``nvidia/*/bin/*.dll`` в ``%LOCALAPPDATA%\\Rupor\\cuda``. CTranslate2 (faster-whisper) на GPU
  нужен только cuBLAS: cuDNN и NVRTC он не загружает (проверено на RTX 3060 Ti, все
  compute_type). Отдельный CUDA Toolkit не нужен.

TLS: и PyPI (urllib), и HuggingFace (httpx внутри huggingface_hub) проверяют сертификаты по
корням certifi *и* хранилища Windows (``ssl_context``). Антивирусы с проверкой HTTPS
(Kaspersky, ESET…) и корпоративные прокси подменяют сертификат своим корнем, который лежит
только в хранилище Windows, — одного certifi (так httpx делает по умолчанию) им мало.

Сеть трогают только ``download_model``, ``download_cuda`` и ``model_sizes`` (размеры моделей
для мастера); ``model_ready``, ``cuda_ready``, ``move_model`` и ``remove_model`` работают офлайн. Ошибки загрузки — ``FetchError`` с текстом по-русски (его можно показать
пользователю как есть).
"""
from __future__ import annotations

import errno
import fnmatch
import functools
import hashlib
import http.client
import json
import logging
import os
import re
import shutil
import ssl
import stat
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from collections.abc import Callable, Iterable, Iterator, Sequence, Sized
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, cast

from . import i18n
from .i18n import tr

log = logging.getLogger(__name__)

ProgressFn = Callable[[float, str], None]


class FetchError(Exception):
    """Загрузка не удалась. Текст — по-русски, пригоден для показа пользователю."""


def _version() -> str:
    pkg = sys.modules.get(__package__ or "")
    v = getattr(pkg, "__version__", None)
    if isinstance(v, str) and v:
        return v
    try:
        from importlib.metadata import version
        return version("rupor")
    except Exception:
        return "0"


def user_agent() -> str:
    # Свой User-Agent: Cloudflare и часть CDN режут стандартный «Python-urllib».
    return f"Rupor/{_version()}"


# Размеры в текстах — мегабайты и гигабайты по 10⁶ и 10⁹ байт, как на сайте Hugging Face и
# в списке моделей мастера (recommend.options → size_mb, fmtMb в интерфейсе): у одной и той
# же модели в списке и в прогрессе загрузки или переноса — одно и то же число.

def _mb(n: float) -> str:
    """Байты → мегабайты (10⁶ байт) по правилам языка интерфейса: «1 234» / «1,234»."""
    return i18n.fmt_num(n / 1_000_000)


def _gb(n: float) -> str:
    """Байты → гигабайты (10⁹ байт) с одним знаком: «1,5» / «1.5»."""
    return i18n.fmt_num(n / 1_000_000_000, 1)


# ---------------------------------------------------------------------------
# TLS и понятные причины ошибок
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def ssl_context() -> ssl.SSLContext:
    """Контекст TLS для всех загрузок: корни certifi + хранилище Windows (ROOT и CA).

    Только certifi (выбор httpx по умолчанию) не знает корня антивируса или корпоративного
    прокси, которые перехватывают HTTPS. Только хранилище Windows может не содержать корня,
    который Windows докачивает по требованию лишь для своих программ. Поэтому — оба набора."""
    try:
        import certifi
        ctx = ssl.create_default_context(cafile=certifi.where())
    except (ImportError, OSError, ssl.SSLError) as e:
        log.warning("Корни certifi недоступны (%s) — только хранилище системы", e)
        return ssl.create_default_context()
    try:
        ctx.load_default_certs()  # на Windows — системные хранилища ROOT и CA
    except (OSError, ssl.SSLError) as e:
        log.warning("Не удалось прочитать хранилище сертификатов системы: %s", e)
    return ctx


def _hf_client_factory() -> Any:
    """httpx.Client для huggingface_hub: как его default_client_factory (те же хуки, таймаут,
    редиректы), но с нашим ssl_context() вместо одного certifi."""
    import httpx
    from huggingface_hub.utils import _http as hf_http
    base = hf_http.default_client_factory()
    try:
        return httpx.Client(verify=ssl_context(), event_hooks=base.event_hooks,
                            follow_redirects=base.follow_redirects, timeout=base.timeout,
                            trust_env=base.trust_env)
    finally:
        base.close()


_HF_TLS_LOCK = threading.Lock()
_hf_tls_ready = False


def _use_system_certs() -> None:
    """Один раз на процесс подменить фабрику HTTP-клиента huggingface_hub (см. ssl_context).

    Загрузку больших файлов через Xet (hf_xet) это не касается: он и так проверяет
    сертификаты через хранилище Windows."""
    global _hf_tls_ready
    with _HF_TLS_LOCK:
        if _hf_tls_ready:
            return
        try:
            import huggingface_hub
            from huggingface_hub.utils import _http as hf_http
            if not callable(getattr(hf_http, "default_client_factory", None)):
                raise AttributeError("no default_client_factory")
            huggingface_hub.set_client_factory(_hf_client_factory)
        except (ImportError, AttributeError) as e:  # старый huggingface_hub на requests
            log.warning("Не удалось подключить хранилище сертификатов Windows "
                        "к huggingface_hub: %s", e)
        _hf_tls_ready = True


def _causes(e: BaseException) -> Iterator[BaseException]:
    """e и все его причины: __cause__, __context__, URLError.reason (без зацикливания)."""
    seen: set[int] = set()
    stack: list[BaseException] = [e]
    while stack:
        cur = stack.pop()
        if id(cur) in seen:
            continue
        seen.add(id(cur))
        yield cur
        reason = cur.reason if isinstance(cur, urllib.error.URLError) else None
        for nxt in (reason, cur.__cause__, cur.__context__):
            if isinstance(nxt, BaseException):
                stack.append(nxt)


# ssl/httpx и rustls (hf_xet присылает ошибки текстом)
_CERT_MARKERS = ("CERTIFICATE_VERIFY_FAILED", "invalid peer certificate", "UnknownIssuer")


def _is_cert_error(e: BaseException) -> bool:
    """Сертификат сервера не прошёл проверку (в т.ч. обёрнутый httpx/huggingface_hub/hf_xet)."""
    return any(isinstance(c, ssl.SSLCertVerificationError)
               or any(m in str(c) for m in _CERT_MARKERS) for c in _causes(e))


_DISK_FULL_WINERRORS = (39, 112)  # ERROR_HANDLE_DISK_FULL, ERROR_DISK_FULL
_DISK_FULL_MARKERS = ("os error 112", "os error 39", "no space left on device",
                      "not enough space on the disk")


def _is_disk_full(e: BaseException) -> bool:
    for c in _causes(e):
        if isinstance(c, OSError) and (c.errno == errno.ENOSPC
                                       or getattr(c, "winerror", None) in _DISK_FULL_WINERRORS):
            return True
        text = str(c).lower()  # ошибки hf_xet (Rust) приходят текстом: «… (os error 112)»
        if any(m in text for m in _DISK_FULL_MARKERS):
            return True
    return False


def _reason(e: BaseException) -> str:
    if _is_cert_error(e):
        return tr("fetch.reason_cert")
    if _is_disk_full(e):
        return tr("fetch.reason_disk")
    if isinstance(e, urllib.error.HTTPError):
        return tr("fetch.reason_http", code=e.code)
    if isinstance(e, urllib.error.URLError):
        return tr("fetch.reason_net", reason=e.reason)
    if isinstance(e, TimeoutError):
        return tr("fetch.reason_timeout")
    return str(e) or type(e).__name__


def _advice(e: BaseException, default: str) -> str:
    """Что делать пользователю: особый совет для сертификата и места на диске, иначе default
    (все — на языке интерфейса)."""
    if _is_cert_error(e):
        return tr("fetch.advice_cert")
    if _is_disk_full(e):
        return tr("fetch.advice_disk")
    return default


PROGRESS_INTERVAL_S = 0.25  # не чаще стольких секунд между событиями прогресса


class _Reporter:
    """Прокладка к on_progress: не чаще раза в PROGRESS_INTERVAL_S, pct в [0, 100]."""

    def __init__(self, on_progress: ProgressFn) -> None:
        self._cb = on_progress
        self._min = PROGRESS_INTERVAL_S
        self._last = 0.0
        self._lock = threading.Lock()

    def emit(self, pct: float, text: str, force: bool = False) -> None:
        pct = max(0.0, min(100.0, pct))
        with self._lock:
            now = time.monotonic()
            if not force and now - self._last < self._min:
                return
            self._last = now
            try:
                self._cb(round(pct, 1), text)
            except Exception:
                log.exception("Обработчик прогресса упал")


# ---------------------------------------------------------------------------
# Модель
# ---------------------------------------------------------------------------

# Как в faster_whisper.utils.download_model: только то, что нужно CTranslate2.
MODEL_FILES: tuple[str, ...] = (
    "config.json", "preprocessor_config.json", "model.bin", "tokenizer.json", "vocabulary.*",
)
_REQUIRED_MODEL_FILES = ("config.json", "model.bin", "tokenizer.json")

# Запасной словарь на случай, если в faster_whisper переименуют приватный _MODELS.
_FALLBACK_MODELS = {
    "large-v3-turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "turbo": "mobiuslabsgmbh/faster-whisper-large-v3-turbo",
    "large-v3": "Systran/faster-whisper-large-v3",
    "medium": "Systran/faster-whisper-medium",
    "small": "Systran/faster-whisper-small",
    "base": "Systran/faster-whisper-base",
    "tiny": "Systran/faster-whisper-tiny",
}


def _known_models() -> dict[str, str]:
    try:
        from faster_whisper.utils import _MODELS
        return dict(_MODELS)
    except (ImportError, AttributeError):
        return dict(_FALLBACK_MODELS)


def repo_id(model: str) -> str:
    """Имя модели faster-whisper («large-v3-turbo») → репозиторий HuggingFace."""
    if "/" in model:
        return model
    repo = _known_models().get(model)
    if repo is None:
        raise FetchError(tr("fetch.unknown_model", model=model))
    return repo


def _model_complete(path: Path) -> bool:
    try:
        if not all((path / f).is_file() for f in _REQUIRED_MODEL_FILES):
            return False
        return (path / "model.bin").stat().st_size > 0 and any(path.glob("vocabulary.*"))
    except OSError:
        return False


def model_path(model: str, cache_dir: str | Path | None = None) -> Path | None:
    """Папка полностью скачанной модели или None. Без сети (только локальный кэш)."""
    if os.path.isdir(model):  # faster-whisper принимает и путь к папке с моделью
        p = Path(model)
        return p if _model_complete(p) else None
    try:
        repo = repo_id(model)
    except FetchError:
        return None
    try:
        from huggingface_hub import snapshot_download
        found = snapshot_download(repo, allow_patterns=list(MODEL_FILES), local_files_only=True,
                                  cache_dir=cache_dir)
    except Exception as e:  # LocalEntryNotFoundError, неполный снимок, битый кэш…
        log.debug("Модели %s нет в локальном кэше: %s", model, e)
        return None
    p = Path(found)
    return p if _model_complete(p) else None


def model_ready(model: str, cache_dir: str | Path | None = None) -> bool:
    """Модель целиком лежит в локальном кэше (сеть не трогается)."""
    return model_path(model, cache_dir) is not None


# Размеры моделей по данным Hub (байты нужных файлов): удачные ответы — на весь процесс,
# неудачные — не переспрашивать SIZE_RETRY_S (мастер без сети не ждёт каждый раз)
_SIZES: dict[str, int] = {}
_SIZES_FAILED: dict[str, float] = {}
_SIZE_THREADS: dict[str, threading.Thread] = {}
_SIZES_LOCK = threading.Lock()
SIZE_RETRY_S = 300.0


def _expected_model_size(repo: str, timeout: float = 10) -> int:
    """Суммарный размер нужных файлов по данным Hub (для честного процента); 0 — не знаем."""
    with _SIZES_LOCK:
        cached = _SIZES.get(repo)
    if cached:
        return cached
    _use_system_certs()
    try:
        from huggingface_hub import HfApi
        info = HfApi(library_name="rupor", library_version=_version()).model_info(
            repo, files_metadata=True, timeout=timeout)
        size = sum(int(s.size or 0) for s in (info.siblings or [])
                   if any(fnmatch.fnmatch(s.rfilename, pat) for pat in MODEL_FILES))
    except Exception as e:
        log.debug("Размер модели %s не узнать заранее: %s", repo, e)
        return 0
    if size > 0:
        with _SIZES_LOCK:
            _SIZES[repo] = size
    return size


def _fetch_size(repo: str, timeout: float) -> None:
    size = _expected_model_size(repo, timeout=timeout)
    with _SIZES_LOCK:
        if size > 0:
            _SIZES[repo] = size
            _SIZES_FAILED.pop(repo, None)
        else:
            _SIZES_FAILED[repo] = time.monotonic()


def model_sizes(repos: Sequence[str], budget_s: float = 4.0) -> dict[str, int]:
    """Размеры моделей по данным Hub (байты файлов MODEL_FILES): запросы параллельно, ждём
    не дольше budget_s на все. Не успели, нет сети или Hub ответил ошибкой — репозитория
    в ответе нет (берите запасной размер). Удачные ответы кэшируются на процесс."""
    out: dict[str, int] = {}
    waits: list[threading.Thread] = []
    now = time.monotonic()
    with _SIZES_LOCK:
        for repo in dict.fromkeys(repos):
            if repo in _SIZES:
                out[repo] = _SIZES[repo]
                continue
            t = _SIZE_THREADS.get(repo)
            if t is None or not t.is_alive():
                failed = _SIZES_FAILED.get(repo)
                if failed is not None and now - failed < SIZE_RETRY_S:
                    continue
                t = threading.Thread(target=_fetch_size, args=(repo, budget_s),
                                     name="rupor-model-size", daemon=True)
                _SIZE_THREADS[repo] = t
                t.start()
            waits.append(t)
    deadline = time.monotonic() + budget_s
    for t in waits:
        t.join(max(0.0, deadline - time.monotonic()))
    with _SIZES_LOCK:
        for repo in repos:
            if repo in _SIZES:
                out[repo] = _SIZES[repo]
    return out


class _BarGroup:
    """Общее состояние всех «прогресс-баров», которые huggingface_hub создаёт за одну загрузку."""

    def __init__(self, on_change: Callable[[int, int, str], None]) -> None:
        self.bars: list[_SilentBar] = []
        self.lock = threading.RLock()
        self._on_change = on_change

    def changed(self) -> None:
        with self.lock:
            byte_bars = [b for b in self.bars if b.unit == "B"]
            # huggingface_hub ≥1.x ведёт два байтовых бара: сеть («Downloading bytes») и запись
            # на диск. У Xet-файлов запись на диск догоняет только в конце, а сеть растёт плавно
            # (но её итог «плавает» из-за сжатия). Поэтому готово — самый продвинутый из счётчиков,
            # а итог — сумма размеров файлов (бар записи на диск).
            if byte_bars:
                done = max(int(b.n) for b in byte_bars)
                sizes = [int(b.total or 0) for b in byte_bars if not b.is_transfer]
                total = sum(sizes) if sizes else max(int(b.total or 0) for b in byte_bars)
                unit = "B"
            else:
                done = sum(int(b.n) for b in self.bars)
                total = sum(int(b.total or 0) for b in self.bars)
                unit = "files"
            self._on_change(done, total, unit)


class _SilentBar:
    """Замена tqdm для huggingface_hub: ничего не рисует, только считает и сообщает группе.

    Реализует то подмножество API tqdm, которое зовут snapshot_download/hf_thread_map
    (и tqdm.contrib.concurrent.thread_map в старых версиях huggingface_hub)."""

    group: _BarGroup  # задаётся в подклассе, см. _bar_class()

    def __init__(self, iterable: Iterable[Any] | None = None, *args: Any, **kwargs: Any) -> None:
        self.iterable: Iterable[Any] | None = (
            iterable if iterable is not None else kwargs.get("iterable"))
        self.desc = str(kwargs.get("desc") or "")
        self.unit = str(kwargs.get("unit") or "it")
        self.is_transfer = self.desc.lower().startswith("downloading bytes")
        total = kwargs.get("total")
        if total is None and isinstance(self.iterable, Sized):
            total = len(self.iterable)
        self.total: float | None = total
        self.n: float = kwargs.get("initial") or 0
        self.disable = False
        self.postfix = ""
        with self.group.lock:
            self.group.bars.append(self)
        self.group.changed()

    # --- потоки: tqdm.contrib.concurrent / hf_thread_map раздают общий замок воркерам ---
    @classmethod
    def get_lock(cls) -> Any:
        lock = getattr(cls, "_lock", None)
        if lock is None:
            lock = threading.RLock()
            setattr(cls, "_lock", lock)
        return lock

    @classmethod
    def set_lock(cls, lock: Any) -> None:
        setattr(cls, "_lock", lock)

    # --- API tqdm ---
    def update(self, n: float | None = 1) -> None:
        with self.group.lock:
            self.n += n or 0
        self.group.changed()

    def __iter__(self) -> Iterator[Any]:
        for item in self.iterable or ():
            yield item
            self.update(1)

    def __enter__(self) -> _SilentBar:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def refresh(self, *args: Any, **kwargs: Any) -> None:
        self.group.changed()

    def reset(self, total: float | None = None) -> None:
        with self.group.lock:
            self.n = 0
            if total is not None:
                self.total = total
        self.group.changed()

    def close(self) -> None:
        pass

    def clear(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_description(self, desc: str | None = None, refresh: bool = True) -> None:
        self.desc = desc or ""

    def set_description_str(self, desc: str | None = None, refresh: bool = True) -> None:
        self.desc = desc or ""

    def set_postfix(self, *args: Any, **kwargs: Any) -> None:
        pass

    def set_postfix_str(self, s: str = "", refresh: bool = True) -> None:
        self.postfix = s

    @property
    def format_dict(self) -> dict[str, Any]:
        return {"n": self.n, "total": self.total, "rate": None, "unit": self.unit}


def _bar_class(group: _BarGroup) -> type[_SilentBar]:
    # Свой подкласс на каждую загрузку: у каждого свой group и свой замок.
    return cast("type[_SilentBar]", type("_RuporBar", (_SilentBar,), {"group": group}))


_MODEL_LOCK = threading.Lock()
_MODEL_SPACE_MARGIN = 64 << 20  # запас сверх размера файлов модели


def _hf_cache_dir(cache_dir: str | Path | None) -> Path:
    if cache_dir is not None:
        return Path(cache_dir)
    from huggingface_hub import constants
    return Path(constants.HF_HUB_CACHE)


def _check_model_space(model: str, repo: str, expected: int,
                       cache_dir: str | Path | None) -> None:
    """Хватит ли места в кэше HuggingFace (сам huggingface_hub только пишет предупреждение
    в лог и падает на середине с ENOSPC). Уже скачанное (blobs, в т.ч. *.incomplete)
    вычитается. expected == 0 (размер неизвестен) — не проверяем."""
    if expected <= 0:
        return
    cache = _hf_cache_dir(cache_dir)
    have = 0
    try:
        for p in (cache / ("models--" + repo.replace("/", "--")) / "blobs").iterdir():
            if p.is_file():
                have += p.stat().st_size
    except OSError:
        pass
    probe = cache
    while not probe.exists() and probe.parent != probe:  # кэша может ещё не быть
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError as e:
        log.debug("Свободное место для %s не узнать: %s", cache, e)
        return
    need = max(expected - have, 0) + _MODEL_SPACE_MARGIN
    if free < need:
        raise FetchError(tr("fetch.model_no_space", model=model, need=_gb(need),
                            free=_gb(free), path=cache))


def download_model(model: str, on_progress: ProgressFn,
                   cache_dir: str | Path | None = None) -> str:
    """Скачать модель в кэш HuggingFace и вернуть путь к её папке.

    Уже скачанную модель сеть не трогает. Прерванная загрузка при повторном вызове
    докачивается (это умеет huggingface_hub). Ошибка — FetchError."""
    rep = _Reporter(on_progress)
    with _MODEL_LOCK:
        ready = model_path(model, cache_dir)
        if ready is not None:
            rep.emit(100.0, tr("fetch.model_ready", model=model), force=True)
            return str(ready)
        if os.path.isdir(model):
            raise FetchError(tr("fetch.model_dir_empty", model=model))
        repo = repo_id(model)
        rep.emit(0.0, tr("fetch.model_downloading", model=model), force=True)
        _use_system_certs()
        expected = _expected_model_size(repo)
        _check_model_space(model, repo, expected, cache_dir)

        def on_change(done: int, total: int, unit: str) -> None:
            if unit == "B":
                total = max(total, expected)
                done = min(done, total)
                text = tr("fetch.model_progress_mb", model=model, done=_mb(done),
                          total=_mb(total))
            else:
                text = tr("fetch.model_progress_files", model=model, done=done, total=total)
            pct = done * 100.0 / total if total else 0.0
            rep.emit(min(pct, 99.9), text)

        from huggingface_hub import snapshot_download
        log.info("Скачиваю модель %s из %s", model, repo)
        t0 = time.monotonic()
        try:
            found = snapshot_download(
                repo, allow_patterns=list(MODEL_FILES), cache_dir=cache_dir,
                tqdm_class=cast(Any, _bar_class(_BarGroup(on_change))),
                library_name="rupor", library_version=_version())
        except Exception as e:
            log.warning("Не удалось скачать модель %s: %s", model, e)
            advice = _advice(e, tr("fetch.advice_net"))
            raise FetchError(tr("fetch.model_failed", model=model, reason=_reason(e),
                                advice=advice)) from e
        p = Path(found)
        if not _model_complete(p):
            raise FetchError(tr("fetch.model_incomplete", model=model))
        log.info("Модель %s скачана за %.0f с: %s", model, time.monotonic() - t0, p)
        rep.emit(100.0, tr("fetch.model_done", model=model), force=True)
        return str(p)


# ---------------------------------------------------------------------------
# Папка моделей (настройка model_dir): перенос и удаление
# ---------------------------------------------------------------------------
#
# Папка — в формате кэша HuggingFace, как download_root у faster-whisper:
#   models--<org>--<имя>/blobs/<хэш>          — файлы;
#   models--<org>--<имя>/snapshots/<ревизия>/  — имена файлов (на Windows — относительные
#                                                символьные ссылки на ../../blobs/<хэш>, если
#                                                их можно создавать, иначе сами файлы);
#   models--<org>--<имя>/refs/main             — ревизия (без неё local_files_only не найдёт);
#   models--<org>--<имя>/trees/                — список файлов ревизии (проверка полноты);
#   .locks/models--<org>--<имя>/               — замки загрузки.

_REPO_RE = re.compile(r"[\w.-]+(?:/[\w.-]+)?")
_LOCKS_DIR = ".locks"
_TMP_SUFFIX = ".rupor-tmp"
_WRITE_PROBE = ".rupor-write-test"


def model_cache_dir(model_dir: str | Path | None) -> Path:
    """Папка, где Рупор ищет модели и куда их качает: model_dir из настроек, а пустая
    строка — кэш HuggingFace по умолчанию (HF_HUB_CACHE, обычно
    %USERPROFILE%\\.cache\\huggingface\\hub)."""
    return Path(os.path.abspath(_hf_cache_dir(model_dir or None)))


def _real(path: str | Path) -> str:
    """Настоящий путь для сравнения: ссылки, junction, subst и сетевой диск раскрыты
    (os.path.realpath; несуществующий хвост пути остаётся как есть), регистр не важен."""
    p = os.fspath(path)
    try:
        return os.path.normcase(os.path.realpath(p))
    except (OSError, ValueError):
        return os.path.normcase(os.path.abspath(p))


def _same_file(a: str | Path, b: str | Path) -> bool | None:
    """Один и тот же файл или папка на диске (номер тома и файла, ссылки раскрываются);
    None — не узнать: пути нет или файловая система не даёт номеров файлов (0)."""
    try:
        sa, sb = os.stat(a), os.stat(b)
    except (OSError, ValueError):
        return None
    if not sa.st_ino or not sb.st_ino:
        return None
    return (sa.st_dev, sa.st_ino) == (sb.st_dev, sb.st_ino)


def same_dir(a: str | Path, b: str | Path) -> bool:
    """Одна и та же папка: без учёта регистра и вида записи пути, а также та же папка под
    другим путём — junction, символьная ссылка, subst, сетевой диск и его UNC-путь (иначе
    «перенос» в неё и удаление «старой» копии стёрли бы единственную)."""
    a, b = os.fspath(a), os.fspath(b)
    if os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b)):
        return True
    same = _same_file(a, b)
    if same is not None:
        return same
    return _real(a) == _real(b)       # одной из папок ещё нет — по настоящему пути


def _within(path: str | Path, root: str | Path) -> bool:
    """path — сама папка root или что-то внутри неё (по настоящим путям, через ссылки)."""
    p, r = _real(path), _real(root)
    try:
        if os.path.commonpath([p, r]) == r:
            return True
    except ValueError:  # другой диск или UNC — сверим по номерам папок ниже
        pass
    # Та же папка под путём, который realpath не сводит к локальному (\\localhost\C$\…,
    # сетевой диск на эту же машину): сама папка или её родитель — root по номеру тома и файла.
    cur = os.path.abspath(os.fspath(path))
    while True:
        if os.path.exists(cur) and _same_file(cur, root):
            return True
        parent = os.path.dirname(cur)
        if parent == cur:
            return False
        cur = parent


def check_move_target(model: str, src_dir: str | Path | None,
                      dst_dir: str | Path | None) -> None:
    """Новая папка dst_dir — не внутри папки самой модели в src_dir (models--org--name) и не
    внутри папки замков .locks: копия легла бы в то, что потом удаляет remove_model, и
    пропали бы обе. Нельзя — FetchError. Модель, заданная своей папкой, и неизвестная модель
    (её папки в кэше нет) не проверяются."""
    if os.path.isdir(model):
        return
    try:
        name = _repo_folder(repo_id(model))
    except FetchError:
        return
    src, dst = _hf_cache_dir(src_dir), _hf_cache_dir(dst_dir)
    for root in (src / name, src / _LOCKS_DIR):
        if _within(dst, root):
            raise FetchError(tr("fetch.move_into_model", path=dst, folder=root))


def check_writable_dir(path: str | Path) -> Path:
    """Создать папку (со всеми родительскими) и проверить, что в неё можно писать: создать и
    удалить пробный файл. Ошибка — FetchError с понятным текстом."""
    p = Path(path)
    try:
        p.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise FetchError(tr("fetch.mkdir_failed", path=p, error=_os_error(e))) from e
    probe = p / f"{_WRITE_PROBE}-{os.getpid()}"
    try:
        with open(probe, "wb") as f:
            f.write(b"ok")
    except OSError as e:
        raise FetchError(tr("fetch.dir_not_writable", path=p, error=_os_error(e))) from e
    finally:
        try:
            probe.unlink(missing_ok=True)
        except OSError:
            pass
    return p


def _os_error(e: OSError) -> str:
    return e.strerror or str(e) or type(e).__name__


def _repo_folder(repo: str) -> str:
    """«org/name» → «models--org--name» — папка репозитория в кэше HuggingFace. Имя проверяется:
    из него не должно выйти пути за пределы кэша."""
    if not _REPO_RE.fullmatch(repo) or ".." in repo:
        raise FetchError(tr("fetch.unknown_model", model=repo))
    return "models--" + repo.replace("/", "--")


def model_folder(model: str, cache_dir: str | Path | None) -> Path:
    """Папка модели в кэше: <кэш>/models--org--name (модель задана своей папкой — она сама).
    Неизвестная модель — FetchError."""
    if os.path.isdir(model):
        return Path(model)
    return _hf_cache_dir(cache_dir) / _repo_folder(repo_id(model))


def _same_volume(a: Path, b: Path) -> bool:
    """a и b на одном томе — папку модели можно переименовать, а не копировать."""
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _rmtree(path: Path) -> bool:
    """Удалить папку (или файл, ссылку) со всем содержимым; атрибут «только чтение» снимается.
    True — по пути больше ничего нет. Ошибки — в лог."""
    if not os.path.lexists(path):
        return True

    def onexc(func: Callable[..., Any], p: str, exc: BaseException) -> None:
        if isinstance(exc, PermissionError):
            try:
                os.chmod(p, stat.S_IWRITE)
                func(p)
                return
            except OSError:
                pass
        raise exc

    try:
        if os.path.islink(path) or not path.is_dir():
            path.unlink()
        else:
            shutil.rmtree(path, onexc=onexc)
    except OSError as e:
        log.warning("Не удалось удалить %s: %s", path, e)
    return not os.path.lexists(path)


@dataclass
class _CopyItem:
    rel: str            # путь внутри папки модели
    src: Path           # что читать (для ссылки — файл, на который она ведёт)
    size: int
    link: str = ""      # относительная ссылка (как в исходной папке); "" — обычный файл
    blob: str = ""      # ссылка ведёт на blobs/<хэш> этой же модели — имя блоба


def _plan_copy(folder: Path) -> tuple[list[_CopyItem], list[_CopyItem], list[_CopyItem]]:
    """Что копировать из папки модели: (ссылки snapshots, блобы, остальные файлы).
    Недокачанные блобы (*.incomplete) не берутся. Ссылка наружу папки модели или по
    абсолютному пути переносится как обычный файл (содержимое цели)."""
    links: list[_CopyItem] = []
    blobs: list[_CopyItem] = []
    other: list[_CopyItem] = []
    root = os.path.abspath(folder)
    blobs_dir = os.path.join(root, "blobs")
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for d in dirnames:
            if os.path.islink(os.path.join(dirpath, d)):
                log.warning("Перенос модели: ссылка на папку пропущена: %s",
                            os.path.join(dirpath, d))
        for name in filenames:
            p = os.path.join(dirpath, name)
            rel = os.path.relpath(p, root)
            if name.endswith(".incomplete"):
                continue
            if os.path.islink(p):
                target = os.readlink(p)
                real = os.path.realpath(p)
                if not os.path.isfile(real):
                    log.warning("Перенос модели: ссылка %s ведёт в никуда — пропущена", rel)
                    continue
                size = os.path.getsize(real)
                inside = os.path.normcase(os.path.normpath(os.path.join(dirpath, target)))
                try:
                    within = (os.path.commonpath([inside, os.path.normcase(root)])
                              == os.path.normcase(root))
                except ValueError:  # другой диск
                    within = False
                if not os.path.isabs(target) and within:
                    blob = ""
                    if os.path.dirname(inside) == os.path.normcase(blobs_dir):
                        blob = os.path.basename(inside)
                    links.append(_CopyItem(rel, Path(real), size, os.path.normpath(target),
                                           blob))
                else:
                    other.append(_CopyItem(rel, Path(real), size))
                continue
            item = _CopyItem(rel, Path(p), os.path.getsize(p))
            if os.path.normcase(dirpath) == os.path.normcase(blobs_dir):
                blobs.append(item)
            else:
                other.append(item)
    return links, blobs, other


def _copy_file(src: Path, dst: Path, report: Callable[[int], None]) -> None:
    """Скопировать файл потоково, сообщая о каждом записанном куске (байты)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    with open(src, "rb") as fi, open(dst, "wb") as fo:
        while True:
            chunk = fi.read(_CHUNK)
            if not chunk:
                break
            fo.write(chunk)
            report(len(chunk))


def _copy_model_folder(src: Path, tmp: Path, report: Callable[[int], None]) -> None:
    """Скопировать папку модели src в tmp. Ссылки snapshots пересоздаются такими же
    относительными; не вышло (нет права создавать ссылки) — на месте ссылки пишется сам файл,
    а его блоб отдельно не копируется (место на диске не удваивается)."""
    links, blobs, other = _plan_copy(src)
    linked: set[str] = set()       # блобы, на которые осталась ссылка, — их копировать
    inlined: set[str] = set()      # блобы, записанные вместо ссылки
    for it in links:
        dst = tmp / it.rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(it.link, dst)
        except OSError as e:
            log.debug("Ссылку %s не создать (%s) — копирую файл", it.rel, e)
            _copy_file(it.src, dst, report)
            if it.blob:
                inlined.add(it.blob)
            continue
        if it.blob:  # ссылка не на блоб (внутри папки) — её цель и так копируется как файл
            linked.add(it.blob)
    for it in blobs:
        name = os.path.normcase(os.path.basename(it.rel))   # как имена блобов в ссылках
        if name in inlined and name not in linked:
            continue
        _copy_file(it.src, tmp / it.rel, report)
    for it in other:
        _copy_file(it.src, tmp / it.rel, report)


def _copy_size(src: Path) -> int:
    """Сколько байт займёт копия папки модели (с запасом на случай, когда ссылки создать
    нельзя и файл, на который ведут две ссылки, запишется дважды)."""
    links, blobs, other = _plan_copy(src)
    rest = sum(it.size for it in other)
    with_links = sum(it.size for it in blobs) + rest
    referenced = {it.blob for it in links if it.blob}
    without_links = (sum(it.size for it in links)
                     + sum(it.size for it in blobs
                           if os.path.normcase(os.path.basename(it.rel)) not in referenced)
                     + rest)
    return max(with_links, without_links)


def _merge_into(tmp: Path, dst: Path) -> None:
    """Переложить готовую копию tmp в уже существующую (неполную) папку модели dst: сначала
    блобы и списки файлов, потом snapshots, последним refs — неполная копия не выглядит
    полной ни в какой момент. Блоб с тем же именем (тот же хэш) уже на месте — оставить."""
    order = ["blobs", "trees"]
    order += [p.name for p in sorted(tmp.iterdir()) if p.name not in ("blobs", "trees",
                                                                      "snapshots", "refs")]
    order += ["snapshots", "refs"]
    for sub in order:
        base = tmp / sub
        if not os.path.lexists(base):
            continue
        for dirpath, _dirnames, filenames in os.walk(base, followlinks=False):
            for name in filenames:
                p = Path(dirpath) / name
                target = dst / p.relative_to(tmp)
                if (sub == "blobs" and target.is_file() and not target.is_symlink()
                        and target.stat().st_size == p.lstat().st_size):
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                if os.path.lexists(target) and (target.is_symlink() or not target.is_dir()):
                    target.unlink()
                _replace(p, target)


def _move_locks(src: Path, dst: Path, name: str, rename: bool) -> None:
    """Папку замков модели (.locks/models--…) — следом за моделью; не вышло — не беда."""
    s, d = src / _LOCKS_DIR / name, dst / _LOCKS_DIR / name
    if not s.is_dir() or os.path.lexists(d):
        return
    try:
        d.parent.mkdir(parents=True, exist_ok=True)
        if rename:
            os.replace(s, d)
        else:
            shutil.copytree(s, d, symlinks=True)
    except OSError as e:
        log.debug("Замки модели не перенесены (%s): %s", name, e)


def _has_files_of(snap: Path, other: Path) -> bool:
    """В снимке snap есть все файлы снимка other (по именам)."""
    try:
        return all((snap / f.name).is_file() for f in other.iterdir() if f.is_file())
    except OSError:
        return False


def move_model(model: str, src_dir: str | Path | None, dst_dir: str | Path | None,
               on_progress: ProgressFn) -> str:
    """Перенести модель из кэша src_dir в кэш dst_dir (None — кэш HuggingFace по умолчанию)
    и вернуть путь к её папке в dst_dir. Исходная копия не удаляется (это remove_model).

    * dst_dir внутри папки самой модели в src_dir или внутри .locks — FetchError
      (check_move_target);
    * в dst_dir модель уже целиком (все файлы исходного снимка) — ничего не копируется;
    * тот же том, а папки модели в dst_dir нет — мгновенное переименование;
    * другой том — копия во временную папку «models--<org>--<имя>.rupor-tmp» и переименование (прерванная
      копия никогда не выглядит полной); прогресс — по байтам, как у загрузки. Относительные
      ссылки snapshots сохраняются; если создавать ссылки нельзя — вместо ссылки сам файл,
      без второй копии блоба. refs/ и trees/ копируются (refs/main нужен поиску без сети).
    Перед копированием проверяется место на диске, после — что модель находится в dst_dir.
    Ошибка — FetchError; временная копия при этом удаляется."""
    rep = _Reporter(on_progress)
    with _MODEL_LOCK:
        if os.path.isdir(model):  # модель задана своей папкой: она не в кэше, переносить нечего
            found = model_path(model)
            if found is None:
                raise FetchError(tr("fetch.model_dir_empty", model=model))
            return str(found)
        name = _repo_folder(repo_id(model))
        src, dst = _hf_cache_dir(src_dir), _hf_cache_dir(dst_dir)
        check_move_target(model, src, dst)
        ready = model_path(model, dst)
        have = model_path(model, src)
        if ready is not None and (have is None or _has_files_of(ready, have)):
            rep.emit(100.0, tr("fetch.move_already", model=model, path=dst), force=True)
            return str(ready)
        # Неполная копия в новой папке (нет файла, который есть в исходной: старый
        # huggingface_hub или другая программа не докачали preprocessor_config.json) — не
        # «уже там»: дополняется копией ниже, иначе удалилась бы полная исходная
        if same_dir(src, dst) or have is None:
            raise FetchError(tr("fetch.move_no_model", model=model, path=src))
        check_writable_dir(dst)
        src_folder, dst_folder = src / name, dst / name
        rep.emit(0.0, tr("fetch.move_start", model=model), force=True)
        log.info("Переношу модель %s: %s → %s", model, src, dst)
        t0 = time.monotonic()

        if not os.path.lexists(dst_folder) and _same_volume(src_folder, dst):
            try:
                os.replace(src_folder, dst_folder)
            except OSError as e:  # файл модели занят, том на самом деле другой — копируем
                log.info("Переименовать папку модели не вышло (%s) — копирую", e)
            else:
                found = model_path(model, dst)
                if found is None:
                    try:
                        os.replace(dst_folder, src_folder)
                    except OSError as e:
                        log.error("Не удалось вернуть модель %s на место: %s", model, e)
                    raise FetchError(tr("fetch.move_verify_failed", model=model, path=dst))
                _move_locks(src, dst, name, rename=True)
                log.info("Модель %s перенесена переименованием: %s", model, found)
                rep.emit(100.0, tr("fetch.move_done", model=model), force=True)
                return str(found)

        tmp = dst / (name + _TMP_SUFFIX)
        _rmtree(tmp)  # след прерванной прошлой попытки
        created = False
        done = [0]
        try:
            total = _copy_size(src_folder)
            _check_move_space(model, dst, total)

            def report(n: int) -> None:
                done[0] += n
                shown = max(total, done[0])
                rep.emit(min(done[0] * 100.0 / shown if shown else 0.0, 99.9),
                         tr("fetch.move_progress_mb", model=model, done=_mb(done[0]),
                            total=_mb(shown)))

            _copy_model_folder(src_folder, tmp, report)
            if os.path.lexists(dst_folder):
                _merge_into(tmp, dst_folder)
                _rmtree(tmp)
            else:
                _replace(tmp, dst_folder)
                created = True
        except FetchError:
            _rmtree(tmp)
            raise
        except OSError as e:
            _rmtree(tmp)
            log.warning("Не удалось перенести модель %s в %s: %s", model, dst, e)
            raise FetchError(tr("fetch.move_failed", model=model, path=dst, reason=_reason(e),
                                advice=_advice(e, tr("fetch.advice_retry")))) from e
        found = model_path(model, dst)
        if found is None:
            if created:
                _rmtree(dst_folder)
            raise FetchError(tr("fetch.move_verify_failed", model=model, path=dst))
        _move_locks(src, dst, name, rename=False)
        log.info("Модель %s скопирована за %.0f с (%s МБ): %s", model, time.monotonic() - t0,
                 _mb(done[0]), found)
        rep.emit(100.0, tr("fetch.move_done", model=model), force=True)
        return str(found)


def _check_move_space(model: str, dst: Path, need_files: int) -> None:
    try:
        free = shutil.disk_usage(dst).free
    except OSError as e:
        log.debug("Свободное место в %s не узнать: %s", dst, e)
        return
    need = need_files + _MODEL_SPACE_MARGIN
    if free < need:
        raise FetchError(tr("fetch.move_no_space", model=model, need=_gb(need), free=_gb(free),
                            path=dst))


def remove_model(model: str, cache_dir: str | Path | None) -> bool:
    """Удалить модель из кэша cache_dir (None — кэш по умолчанию): только её папку
    models--org--name и .locks/models--org--name, больше ничего. True — удалено (или её там
    и не было); ошибки — в лог. Модель, заданная своей папкой, не удаляется никогда."""
    if os.path.isdir(model):
        log.warning("Модель задана папкой (%s) — не удаляю", model)
        return False
    try:
        name = _repo_folder(repo_id(model))
    except FetchError as e:
        log.warning("Удалить модель %s нельзя: %s", model, e)
        return False
    cache = _hf_cache_dir(cache_dir)
    ok = True
    for p in (cache / name, cache / _LOCKS_DIR / name):
        if not _rmtree(p):
            ok = False
    if ok:
        log.info("Модель %s удалена из %s", model, cache)
    return ok


def shares_files(model: str, old_dir: str | Path | None, new_dir: str | Path | None) -> bool:
    """remove_model(model, old_dir) задело бы копию модели в new_dir: это одна и та же папка
    под разными путями (junction или ссылка на кэш или на саму models--org--name), копия
    лежит внутри удаляемых папок или её файлы — те же файлы, что в old_dir (жёсткие ссылки,
    ссылки по абсолютному пути на старые блобы). Тогда удалять старую копию нельзя: это и есть
    новая. Копии в new_dir нет — False; не узнать (ошибка чтения) — True."""
    if os.path.isdir(model):
        return True      # модель задана своей папкой: её не удаляют никогда
    try:
        name = _repo_folder(repo_id(model))
    except FetchError:
        return True
    old, new = _hf_cache_dir(old_dir), _hf_cache_dir(new_dir)
    doomed = (old / name, old / _LOCKS_DIR / name)
    try:
        if same_dir(old, new) or same_dir(old / name, new / name):
            return True
        snap = model_path(model, new)
        if snap is None:
            return False
        if any(_within(snap, d) for d in doomed):
            return True
        old_snap = model_path(model, old)
        for f in snap.iterdir():
            if any(_within(f, d) for d in doomed):     # ссылка ведёт в старую папку
                return True
            if old_snap is not None and _same_file(f, old_snap / f.name):
                return True
    except OSError as e:
        log.warning("Не узнать, общие ли файлы у копий модели %s (%s и %s): %s", model, old,
                    new, e)
        return True
    return False


# ---------------------------------------------------------------------------
# CUDA
# ---------------------------------------------------------------------------

# Версия — как в dev-окружении (.venv), с ней проверена работа CTranslate2.
# Только cuBLAS: CTranslate2 4.x на GPU грузит лишь cublas64_12 и cublasLt64_12 (плюс свой
# cudnn64_9.dll из пакета ctranslate2, он входит в сборку). cuDNN (≈712 МБ) и NVRTC (≈73 МБ)
# не нужны — проверено на RTX 3060 Ti во всех compute_type.
CUDA_PACKAGES: tuple[tuple[str, str], ...] = (
    ("nvidia-cublas-cu12", "12.9.2.10"),
)
_FRIENDLY = {
    "nvidia-cublas-cu12": "cuBLAS",
    "nvidia-cuda-nvrtc-cu12": "NVRTC",
    "nvidia-cudnn-cu12": "cuDNN",
}
# Что должно найтись, чтобы CTranslate2 заработал на GPU (маски имён, без учёта регистра).
REQUIRED_DLLS: tuple[str, ...] = (
    "cublas64_12.dll", "cublasLt64_12.dll",
)
ENV_CUDA_DIRS = "RUPOR_CUDA_DIRS"  # доп. папки с DLL через os.pathsep (для тестов и отладки)
PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"
_CHUNK = 1 << 20
# Паузы между повторами загрузки колеса; сдаёмся после len+1 попыток подряд без прироста .part
_RETRY_PAUSES_S: tuple[float, ...] = (2.0, 5.0, 15.0)
_RETRIES = len(_RETRY_PAUSES_S) + 1
# os.replace свежего файла: антивирус держит его открытым на время проверки (WinError 32)
_REPLACE_PAUSES_S: tuple[float, ...] = (0.2, 0.4, 0.6, 0.8, 1.0, 1.0, 1.0)
_TIMEOUT_S = 30.0


def cuda_home() -> Path:
    """%LOCALAPPDATA%\\Rupor\\cuda — куда download_cuda кладёт DLL."""
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "Rupor" / "cuda"


def _nvidia_roots() -> list[Path]:
    roots = [Path(p) / "nvidia" for p in sys.path
             if p and p.rstrip("\\/").lower().endswith("site-packages")]
    if getattr(sys, "frozen", False):  # на случай сборки, куда DLL всё-таки положили
        roots.append(Path(getattr(sys, "_MEIPASS", ".")) / "nvidia")
    return roots


def _has_dlls(d: Path) -> bool:
    try:
        return d.is_dir() and any(d.glob("*.dll"))
    except OSError:
        return False


def cuda_dll_dirs() -> list[Path]:
    """Папки с DLL CUDA по приоритету: RUPOR_CUDA_DIRS → site-packages/nvidia/*/bin (dev)
    → %LOCALAPPDATA%\\Rupor\\cuda. Только существующие и содержащие *.dll, без повторов."""
    cands = [Path(p) for p in os.environ.get(ENV_CUDA_DIRS, "").split(os.pathsep) if p.strip()]
    for root in _nvidia_roots():
        try:
            if root.is_dir():
                cands += sorted(sub / "bin" for sub in root.iterdir())
        except OSError:
            continue
    cands.append(cuda_home())
    out: list[Path] = []
    seen: set[str] = set()
    for d in cands:
        key = os.path.normcase(os.path.abspath(d))
        if key in seen or not _has_dlls(d):
            continue
        seen.add(key)
        out.append(d)
    return out


def missing_cuda_dlls(dirs: Sequence[Path] | None = None) -> list[str]:
    """Какие из REQUIRED_DLLS не нашлись (пустой список — всё на месте)."""
    names: set[str] = set()
    for d in cuda_dll_dirs() if dirs is None else dirs:
        try:
            names.update(p.name.lower() for p in Path(d).glob("*.dll"))
        except OSError:
            continue
    return [pat for pat in REQUIRED_DLLS
            if not any(fnmatch.fnmatchcase(n, pat.lower()) for n in names)]


def cuda_ready(dirs: Sequence[Path] | None = None) -> bool:
    """Все DLL из REQUIRED_DLLS (cuBLAS) на месте (сеть не трогается)."""
    return not missing_cuda_dlls(dirs)


_ADDED_DIRS: set[str] = set()
_DLL_COOKIES: list[Any] = []
_ADD_LOCK = threading.Lock()


def add_cuda_dll_dirs() -> list[Path]:
    """Подключить папки cuda_dll_dirs() к поиску DLL процесса: os.add_dll_directory + PATH
    (PATH — для DLL, которые зависимости грузят обычным LoadLibrary, как cuDNN свои
    под-библиотеки в dev-окружении).
    Повторный вызов добавляет только новые папки. Возвращает добавленные."""
    with _ADD_LOCK:
        new: list[Path] = []
        for d in cuda_dll_dirs():
            key = os.path.normcase(os.path.abspath(d))
            if key in _ADDED_DIRS:
                continue
            _ADDED_DIRS.add(key)
            new.append(d)
            if sys.platform == "win32":
                try:
                    _DLL_COOKIES.append(os.add_dll_directory(str(d)))
                except OSError as e:
                    log.warning("Не удалось добавить папку DLL %s: %s", d, e)
        if new:
            prefix = os.pathsep.join(str(d) for d in new)
            os.environ["PATH"] = prefix + os.pathsep + os.environ.get("PATH", "")
            log.info("Папки DLL CUDA: %s", "; ".join(str(d) for d in new))
        return new


@dataclass(frozen=True)
class _Wheel:
    name: str
    version: str
    filename: str
    url: str
    size: int
    sha256: str

    @property
    def friendly(self) -> str:
        return _FRIENDLY.get(self.name, self.name)


def _urlopen(url: str, headers: dict[str, str] | None = None,
             timeout: float = _TIMEOUT_S) -> Any:
    """HTTP GET со своим User-Agent и ssl_context(). Отдельной функцией — чтобы тесты
    подменяли сеть."""
    req = urllib.request.Request(url, headers={"User-Agent": user_agent(), **(headers or {})})
    return urllib.request.urlopen(req, timeout=timeout, context=ssl_context())


def _wheel_info(name: str, version: str) -> _Wheel:
    url = PYPI_JSON.format(name=urllib.parse.quote(name), version=urllib.parse.quote(version))
    try:
        with _urlopen(url) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, http.client.HTTPException, OSError, ValueError) as e:
        advice = _advice(e, "")
        raise FetchError(tr("fetch.pypi_info_failed", name=name, version=version,
                            reason=_reason(e)) + (f". {advice}" if advice else "")) from e
    for f in data.get("urls") or []:
        fn = str(f.get("filename") or "")
        if f.get("packagetype") != "bdist_wheel" or not fn.endswith("win_amd64.whl"):
            continue
        sha = str((f.get("digests") or {}).get("sha256") or "").lower()
        if not sha or PurePosixPath(fn).name != fn or "\\" in fn:
            break
        return _Wheel(name, version, fn, str(f["url"]), int(f.get("size") or 0), sha)
    raise FetchError(tr("fetch.pypi_no_wheel", name=name, version=version))


def _marker(dest: Path, name: str, version: str) -> Path:
    return dest / f"{name}-{version}.done.json"


def _installed(dest: Path, name: str, version: str) -> bool:
    """Пакет уже распакован: есть отметка и все DLL из неё с нужными размерами."""
    try:
        data = json.loads(_marker(dest, name, version).read_text(encoding="utf-8"))
        files = data["files"]
        return bool(files) and all(
            (dest / f["name"]).stat().st_size == int(f["size"]) for f in files)
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _is_bin_dll(member: str) -> bool:
    """nvidia/<пакет>/bin/<имя>.dll — только такие файлы берём из колеса."""
    p = PurePosixPath(member)
    return (len(p.parts) >= 4 and p.parts[0] == "nvidia" and p.parts[-2] == "bin"
            and p.suffix.lower() == ".dll" and ".." not in p.parts)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _fetch_into(w: _Wheel, part: Path, have: int, report: Callable[[int], None]) -> None:
    """Одна попытка: докачать w в part начиная с байта have."""
    headers = {"Range": f"bytes={have}-"} if have else {}
    with _urlopen(w.url, headers) as resp:
        status = int(getattr(resp, "status", 200) or 200)
        if have and status == 206:
            cr = str(resp.headers.get("Content-Range") or "")
            if cr and not cr.startswith(f"bytes {have}-"):
                raise ConnectionError(tr("fetch.wrong_chunk", range=cr))
            mode = "ab"
            log.info("Докачиваю %s с %s МБ", w.filename, _mb(have))
        else:  # сервер не умеет докачку (200) — начинаем заново
            if have:
                log.info("Сервер не поддерживает докачку — %s качается заново", w.filename)
            have, mode = 0, "wb"
        got = have
        with open(part, mode) as out:
            src = cast(BinaryIO, resp)
            while True:
                chunk = src.read(_CHUNK)
                if not chunk:
                    break
                out.write(chunk)
                got += len(chunk)
                report(got)
    if got < w.size:
        raise ConnectionError(tr("fetch.connection_dropped", got=_mb(got), total=_mb(w.size)))


def _part_size(part: Path) -> int:
    try:
        return part.stat().st_size
    except OSError:
        return 0


def _download_wheel(w: _Wheel, part: Path, report: Callable[[int], None]) -> None:
    """Скачать колесо в part с докачкой и повторами; по выходу размер part == w.size.

    Сдаётся только после _RETRIES обрывов подряд без прогресса. Попытка, которая докачала
    .part дальше прежнего максимума, обнуляет счётчик: на нестабильной сети обрыв раз
    в 100–200 МБ не повод бросать загрузку. Паузы между повторами нарастают (_RETRY_PAUSES_S)."""
    failures = 0
    best = _part_size(part)  # дальше этого .part ещё не докачивался
    while True:
        have = _part_size(part)
        if w.size and have > w.size:
            part.unlink()
            have = 0
        if w.size and have == w.size:
            report(have)
            return
        err: BaseException
        try:
            _fetch_into(w, part, have, report)
            if not w.size or part.stat().st_size == w.size:
                return
            err = ConnectionError(tr("fetch.size_mismatch"))
        except urllib.error.HTTPError as e:
            if e.code == 416:  # докачивать нечего или .part испорчен — заново
                part.unlink(missing_ok=True)
            elif e.code < 500 and e.code not in (408, 429):
                raise FetchError(tr("fetch.download_failed", what=w.friendly,
                                    reason=_reason(e))) from e
            err = e
        except (urllib.error.URLError, http.client.HTTPException, TimeoutError,
                ConnectionError, ssl.SSLError) as e:
            # ssl.SSLError посреди тела (обрыв TLS) — тот же обрыв связи, а не ошибка записи
            err = e
        now = _part_size(part)
        if now > best:  # попытка продвинула загрузку — обрыв не в счёт, считаем заново
            best, failures = now, 0
        else:
            failures += 1
        if failures >= _RETRIES or _is_cert_error(err):  # сертификат повтор не исправит
            raise FetchError(tr("fetch.download_failed", what=w.friendly,
                                reason=_reason(err)) + ". "
                             + _advice(err, tr("fetch.advice_resume"))) from err
        pause = _RETRY_PAUSES_S[max(min(failures, len(_RETRY_PAUSES_S)), 1) - 1]
        log.warning("Загрузка %s прервалась на %s МБ (%s), повтор через %.0f с "
                    "(неудач подряд: %d из %d)", w.filename, _mb(now), _reason(err), pause,
                    failures, _RETRIES)
        time.sleep(pause)


def _replace(src: Path, dst: Path) -> None:
    """os.replace с повторами на PermissionError (WinError 32/5), как в pip: антивирус держит
    свежезаписанный файл открытым, пока его проверяет, — обычно доли секунды."""
    for pause in _REPLACE_PAUSES_S:
        try:
            os.replace(src, dst)
            return
        except PermissionError as e:
            log.debug("%s → %s: файл занят (%s), жду %.1f с", src.name, dst.name, e, pause)
            time.sleep(pause)
    os.replace(src, dst)


def _unlink_quietly(path: Path) -> None:
    """Удалить файл, если получится (занятый антивирусом файл — не повод падать)."""
    for pause in (*_REPLACE_PAUSES_S, None):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError as e:
            if pause is None:
                log.warning("Не удалось удалить %s: %s", path, e)
                return
            time.sleep(pause)
        except OSError as e:
            log.warning("Не удалось удалить %s: %s", path, e)
            return


def _extract_dlls(wheel: Path, dest: Path) -> list[dict[str, Any]]:
    """Извлечь nvidia/*/bin/*.dll из колеса в dest (плоско). Атомарно: .tmp → замена."""
    files: list[dict[str, Any]] = []
    with zipfile.ZipFile(wheel) as zf:
        members = [m for m in zf.infolist() if not m.is_dir() and _is_bin_dll(m.filename)]
        if not members:
            raise FetchError(tr("fetch.no_dlls", name=wheel.name))
        for m in members:
            name = PurePosixPath(m.filename).name
            dst, tmp = dest / name, dest / (name + ".tmp")
            with zf.open(m) as src, open(tmp, "wb") as out:  # CRC проверяет zipfile
                shutil.copyfileobj(src, out, _CHUNK)
            try:
                _replace(tmp, dst)
            except PermissionError as e:  # всё ещё занят: антивирус или DLL загружена процессом
                _unlink_quietly(tmp)
                if not (dst.is_file() and dst.stat().st_size == m.file_size):
                    raise FetchError(tr("fetch.file_locked", path=dst, dir=dest)) from e
            files.append({"name": name, "size": m.file_size})
    return files


def _check_space(dest: Path, wheels: Sequence[_Wheel]) -> None:
    sizes = [w.size for w in wheels]
    # колёса + распакованные DLL (≈1,5 размера колеса), пик — пока лежит самое большое колесо
    need = sum(sizes) * 16 // 10 + max(sizes, default=0)
    have = 0
    for w in wheels:
        for p in (dest / (w.filename + ".part"), dest / w.filename):
            if p.exists():
                have += p.stat().st_size
    free = shutil.disk_usage(dest).free
    if free + have < need:
        raise FetchError(tr("fetch.cuda_no_space", need=_gb(need), free=_gb(free), path=dest))


def _install_wheel(w: _Wheel, target: Path, rep: _Reporter, report: Callable[[int], None],
                   pct_after: float) -> None:
    """Скачать (или докачать) колесо, сверить sha256, извлечь DLL, записать отметку."""
    part, wheel = target / (w.filename + ".part"), target / w.filename
    if not (wheel.exists() and wheel.stat().st_size == w.size):
        _download_wheel(w, part, report)
        _replace(part, wheel)
    rep.emit(min(pct_after, 99.0), tr("fetch.checking", what=w.friendly), force=True)
    if _sha256(wheel) != w.sha256:
        wheel.unlink(missing_ok=True)
        raise FetchError(tr("fetch.checksum_failed", name=w.filename))
    rep.emit(min(pct_after, 99.0), tr("fetch.unpacking", what=w.friendly), force=True)
    files = _extract_dlls(wheel, target)
    m = _marker(target, w.name, w.version)
    tmp = m.with_suffix(".tmp")
    tmp.write_text(json.dumps({"name": w.name, "version": w.version, "wheel": w.filename,
                               "sha256": w.sha256, "files": files},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    _replace(tmp, m)
    _unlink_quietly(wheel)  # DLL уже на месте; не удалось стереть колесо — не ошибка
    log.info("%s %s: извлечено DLL — %d", w.name, w.version, len(files))


_CUDA_LOCK = threading.Lock()


def download_cuda(on_progress: ProgressFn, dest: str | Path | None = None,
                  packages: Sequence[tuple[str, str]] | None = None) -> None:
    """Скачать колёса NVIDIA с PyPI и извлечь из них DLL в dest (по умолчанию cuda_home()).

    Проверяет sha256 из PyPI, пишет потоково в .part (повторный запуск докачивает),
    уже распакованные пакеты пропускает, колёса после распаковки удаляет. Прогресс —
    по байтам суммарно. packages/dest — для проверок на маленьком колесе во временной папке.
    При установке по умолчанию (dest=None) новые DLL сразу подключаются к процессу
    (add_cuda_dll_dirs). Ошибка — FetchError."""
    target = Path(dest) if dest is not None else cuda_home()
    pkgs = list(CUDA_PACKAGES if packages is None else packages)
    rep = _Reporter(on_progress)
    with _CUDA_LOCK:
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise FetchError(tr("fetch.mkdir_failed", path=target, error=e)) from e
        for stale in target.glob("*.tmp"):
            _unlink_quietly(stale)
        rep.emit(0.0, tr("fetch.cuda_checking"), force=True)
        pending = [(n, v) for n, v in pkgs if not _installed(target, n, v)]
        if not pending:
            rep.emit(100.0, tr("fetch.cuda_ready"), force=True)
            return
        wheels = [_wheel_info(n, v) for n, v in pending]
        _check_space(target, wheels)
        total = sum(w.size for w in wheels) or 1
        base = 0
        t0 = time.monotonic()
        log.info("Скачиваю библиотеки CUDA в %s: %s (%s МБ)", target,
                 ", ".join(w.filename for w in wheels), _mb(total))
        for w in wheels:
            def report(got: int, w: _Wheel = w, base: int = base) -> None:
                done = base + got
                rep.emit(min(done * 100.0 / total, 99.0),
                         tr("fetch.cuda_progress", what=w.friendly, done=_mb(done),
                            total=_mb(total)))

            try:
                _install_wheel(w, target, rep, report, (base + w.size) * 100.0 / total)
            except FetchError:
                raise
            except (OSError, zipfile.BadZipFile) as e:  # диск полон, нет прав, битый архив…
                log.exception("Не удалось установить %s", w.filename)
                advice = _advice(e, tr("fetch.advice_retry"))
                raise FetchError(tr("fetch.save_failed", what=w.friendly, path=target,
                                    reason=_reason(e), advice=advice)) from e
            base += w.size
        if packages is None:
            missing = missing_cuda_dlls([target])
            if missing:
                raise FetchError(tr("fetch.cuda_missing", names=", ".join(missing)))
        if dest is None:
            add_cuda_dll_dirs()
        log.info("Библиотеки CUDA скачаны за %.0f с", time.monotonic() - t0)
        rep.emit(100.0, tr("fetch.cuda_done"), force=True)
