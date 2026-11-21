"""Последние записи голоса на диске: ``%APPDATA%\\Rupor\\recordings``.

Каждая диктовка сохраняется до распознавания (WAV 16 кГц, 16 бит, моно — 32 КБ на секунду,
10-минутная запись ≈ 19 МБ). Если распознавание или вставка сорвались, запись не пропадает:
её можно распознать заново (``Engine.recording_transcribe``) или забрать файлом.

Рядом с ``<id>.wav`` лежит ``<id>.json`` — время, длительность, приложение и чем кончилось
распознавание (``status``: pending | ok | empty | error, число символов, id в истории).
Текста диктовки в нём нет — текст живёт только в истории и подчиняется её настройкам.

Ротация (``prune``): хранится не больше ``keep`` последних записей и не больше ``max_bytes``
в сумме; самая новая запись хранится всегда, даже если одна больше лимита. ``keep = 0`` —
записи не сохраняются, а уже сохранённые удаляются.

Записей могут быть тысячи, а ротация идёт на каждой диктовке, поэтому список не открывает
каждый файл: папка читается одним проходом (имена, размеры, время изменения), а ``<id>.json``
перечитывается только у новых и изменившихся записей (``Recordings._metas``).
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
import time
import wave
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

SAMPLE_RATE = 16000
BYTES_PER_S = SAMPLE_RATE * 2
STATUSES = ("pending", "ok", "empty", "error")
_ID_RE = re.compile(r"^\d{8}-\d{6}-\d{3}(?:-\d+)?$")

log = logging.getLogger(__name__)


@dataclass
class Limits:
    keep: int          # сколько последних записей хранить (0 — не сохранять)
    max_bytes: int     # сколько всего места они могут занять


def valid_id(rid: str) -> bool:
    """id — только имя вида 20261107-183305-581 (никаких путей)."""
    return isinstance(rid, str) and bool(_ID_RE.match(rid))


def to_pcm16(audio: np.ndarray) -> bytes:
    a = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
    return (a * 32767.0).astype("<i2").tobytes()


def read_wav(path: Path) -> np.ndarray:
    """WAV, записанный ``save`` → float32 16 кГц моно."""
    with wave.open(str(path), "rb") as w:
        if w.getnchannels() != 1 or w.getsampwidth() != 2 or w.getframerate() != SAMPLE_RATE:
            raise ValueError("unexpected wav format")
        data = w.readframes(w.getnframes())
    return np.frombuffer(data, dtype="<i2").astype(np.float32) / 32767.0


class Recordings:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)
        self._lock = threading.RLock()
        # id → (отметка <id>.json в папке, метаданные): прочитанное в прошлый раз. Отметка —
        # (время изменения, размер) или None, если файла метаданных нет.
        self._metas: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}

    # ---------- пути ----------

    def _wav(self, rid: str) -> Path:
        return self.root / f"{rid}.wav"

    def _meta_path(self, rid: str) -> Path:
        return self.root / f"{rid}.json"

    def _new_id(self, ts: float) -> str:
        base = time.strftime("%Y%m%d-%H%M%S", time.localtime(ts)) + f"-{int(ts * 1000) % 1000:03d}"
        rid, n = base, 1
        while self._wav(rid).exists() or self._meta_path(rid).exists():
            n += 1
            rid = f"{base}-{n}"
        return rid

    # ---------- запись ----------

    def save(self, audio: np.ndarray, limits: Limits, *, ts: float | None = None,
             app: str = "") -> str | None:
        """Сохранить запись и сразу применить ротацию. None — сохранение выключено
        (keep = 0) или запись пустая. Ошибки диска не роняют диктовку — только лог."""
        if limits.keep <= 0 or len(audio) == 0:
            return None
        ts = time.time() if ts is None else ts
        try:
            with self._lock:
                self.root.mkdir(parents=True, exist_ok=True)
                rid = self._new_id(ts)
                tmp = self.root / f"{rid}.wav.part"
                with wave.open(str(tmp), "wb") as w:
                    w.setnchannels(1)
                    w.setsampwidth(2)
                    w.setframerate(SAMPLE_RATE)
                    w.writeframes(to_pcm16(audio))
                tmp.replace(self._wav(rid))
                self._write_meta(rid, {
                    "id": rid, "ts": ts, "duration_s": round(len(audio) / SAMPLE_RATE, 2),
                    "app": app, "status": "pending", "chars": 0, "history_id": None,
                })
                self.prune(limits)
            return rid
        except OSError:
            log.exception("Не удалось сохранить запись голоса")
            return None

    def _write_meta(self, rid: str, meta: dict[str, Any]) -> None:
        tmp = self.root / f"{rid}.json.part"
        tmp.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self._meta_path(rid))
        self._metas.pop(rid, None)      # прочитанное раньше устарело

    def update(self, rid: str | None, **fields: Any) -> None:
        """Дописать в метаданные (status, chars, history_id). Записи уже нет — ничего."""
        if not rid or not valid_id(rid):
            return
        try:
            with self._lock:
                meta = self._read_meta(rid)
                if meta is None:
                    return
                meta.update(fields)
                self._write_meta(rid, meta)
        except OSError:
            log.exception("Не удалось обновить сведения о записи")

    # ---------- чтение ----------

    def _read_meta(self, rid: str) -> dict[str, Any] | None:
        wav = self._wav(rid)
        if not wav.exists():
            return None
        try:
            meta = json.loads(self._meta_path(rid).read_text(encoding="utf-8"))
            if not isinstance(meta, dict):
                raise ValueError
        except (OSError, ValueError):
            # Метаданных нет или битые (выключили компьютер) — восстановить из самого файла
            try:
                size = wav.stat().st_size
                mtime = wav.stat().st_mtime
            except OSError:
                return None
            meta = {"ts": mtime, "duration_s": round(max(0, size - 44) / BYTES_PER_S, 2),
                    "app": "", "status": "pending", "chars": 0, "history_id": None}
        meta["id"] = rid
        return meta

    def _scan(self) -> tuple[dict[str, int], dict[str, tuple[int, int]]]:
        """Папка одним проходом: размеры записей (id → байты WAV) и отметки их метаданных
        (id → время изменения и размер ``<id>.json``). В Windows это данные самого
        перечисления папки — к каждому файлу отдельно система не обращается."""
        sizes: dict[str, int] = {}
        stamps: dict[str, tuple[int, int]] = {}
        try:
            with os.scandir(self.root) as entries:
                for e in entries:
                    rid, ext = os.path.splitext(e.name)
                    ext = ext.lower()
                    if ext not in (".wav", ".json") or not valid_id(rid):
                        continue
                    try:
                        st = e.stat()
                    except OSError:
                        continue
                    if ext == ".wav":
                        sizes[rid] = st.st_size
                    else:
                        stamps[rid] = (st.st_mtime_ns, st.st_size)
        except OSError:
            pass                        # папки ещё нет
        return sizes, stamps

    def _ids(self) -> list[str]:
        return list(self._scan()[0])

    def items(self) -> list[dict[str, Any]]:
        """Записи, новые сверху, с размером файла (bytes)."""
        out: list[dict[str, Any]] = []
        with self._lock:
            sizes, stamps = self._scan()
            seen: dict[str, tuple[tuple[int, int] | None, dict[str, Any]]] = {}
            for rid, size in sizes.items():
                stamp = stamps.get(rid)
                known = self._metas.get(rid)
                if known is not None and known[0] == stamp:
                    meta = known[1]
                else:
                    read = self._read_meta(rid)
                    if read is None:
                        continue
                    meta = read
                seen[rid] = (stamp, meta)
                out.append({**meta, "bytes": size})
            self._metas = seen          # удалённые записи отсюда уходят сами
        out.sort(key=lambda m: (float(m.get("ts") or 0), str(m["id"])), reverse=True)
        return out

    def total_bytes(self) -> int:
        return sum(int(m["bytes"]) for m in self.items())

    def audio(self, rid: str) -> np.ndarray | None:
        if not valid_id(rid):
            return None
        path = self._wav(rid)
        if not path.exists():
            return None
        return read_wav(path)

    def path(self, rid: str) -> Path | None:
        if not valid_id(rid):
            return None
        p = self._wav(rid)
        return p if p.exists() else None

    # ---------- удаление ----------

    def delete(self, rid: str) -> bool:
        if not valid_id(rid):
            return False
        with self._lock:
            existed = self._wav(rid).exists()
            for p in (self._wav(rid), self._meta_path(rid)):
                try:
                    p.unlink(missing_ok=True)
                except OSError:
                    log.warning("Не удалось удалить %s", p.name)
                    return False
        return existed

    def clear(self) -> int:
        n = 0
        with self._lock:
            for rid in self._ids():
                n += int(self.delete(rid))
            # недописанные файлы (выключили компьютер посреди записи)
            if self.root.is_dir():
                for p in self.root.glob("*.part"):
                    p.unlink(missing_ok=True)
        return n

    def prune(self, limits: Limits) -> int:
        """Ротация: оставить не больше keep последних и не больше max_bytes в сумме (самая
        новая — всегда). Возвращает, сколько записей удалено."""
        with self._lock:
            items = self.items()
            if limits.keep <= 0:
                return self.clear()
            removed = 0
            total = 0
            for i, m in enumerate(items):
                b = int(m["bytes"])
                if i == 0 or (i < limits.keep and total + b <= limits.max_bytes):
                    total += b
                else:
                    removed += int(self.delete(str(m["id"])))
            if removed:
                log.info("Записи голоса: ротация удалила %d", removed)
            return removed
