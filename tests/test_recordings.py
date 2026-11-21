"""Последние записи голоса на диске (rupor.recordings): сохранение, ротация по числу и по
размеру, метаданные, чтение обратно. Только временная папка — без Windows и без модели."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from rupor.recordings import BYTES_PER_S, SAMPLE_RATE, Limits, Recordings, read_wav, valid_id

BIG = Limits(keep=30, max_bytes=10**9)


def _audio(seconds: float, value: float = 0.25) -> np.ndarray:
    return np.full(int(SAMPLE_RATE * seconds), value, np.float32)


def test_save_roundtrip_and_meta(tmp_path: Path) -> None:
    r = Recordings(tmp_path / "rec")
    rid = r.save(_audio(1.5), BIG, ts=1_762_530_000.123, app="claude.exe")
    assert rid is not None and valid_id(rid)
    back = r.audio(rid)
    assert back is not None and len(back) == int(SAMPLE_RATE * 1.5)
    assert np.allclose(back, 0.25, atol=1e-4)
    [m] = r.items()
    assert m["id"] == rid and m["app"] == "claude.exe" and m["status"] == "pending"
    assert m["duration_s"] == 1.5 and m["bytes"] == 44 + int(1.5 * BYTES_PER_S)
    r.update(rid, status="ok", chars=42, history_id=7)
    [m] = r.items()
    assert (m["status"], m["chars"], m["history_id"]) == ("ok", 42, 7)
    assert not list((tmp_path / "rec").glob("*.part"))


def test_clipping_and_empty(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    assert r.save(np.zeros(0, np.float32), BIG) is None
    rid = r.save(np.array([2.0, -2.0, 0.5], np.float32), BIG)
    assert rid is not None
    back = read_wav(tmp_path / f"{rid}.wav")
    assert back[0] == pytest.approx(1.0) and back[1] == pytest.approx(-1.0)


def test_rotation_by_count_keeps_newest(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    ids = [r.save(_audio(0.1), Limits(3, 10**9), ts=1000.0 + i) for i in range(5)]
    left = [m["id"] for m in r.items()]
    assert left == [ids[4], ids[3], ids[2]]
    assert not (tmp_path / f"{ids[0]}.json").exists()


def test_rotation_by_size(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    one = 44 + BYTES_PER_S              # секунда записи
    lim = Limits(30, int(one * 2.5))    # помещаются две
    ids = [r.save(_audio(1.0), lim, ts=2000.0 + i) for i in range(4)]
    assert [m["id"] for m in r.items()] == [ids[3], ids[2]]
    assert r.total_bytes() <= lim.max_bytes


def test_newest_kept_even_if_bigger_than_limit(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    r.save(_audio(0.2), BIG, ts=1.0)
    rid = r.save(_audio(2.0), Limits(30, 1000), ts=2.0)
    assert [m["id"] for m in r.items()] == [rid]


def test_keep_zero_disables_and_clears(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    r.save(_audio(0.1), BIG, ts=1.0)
    assert r.save(_audio(0.1), Limits(0, 10**9)) is None
    assert len(r.items()) == 1           # save с keep=0 ничего не трогает…
    assert r.prune(Limits(0, 10**9)) == 1  # …а ротация с keep=0 удаляет всё
    assert r.items() == []


def test_same_second_ids_are_unique(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    a = r.save(_audio(0.1), BIG, ts=5000.5)
    b = r.save(_audio(0.1), BIG, ts=5000.5)
    assert a and b and a != b and valid_id(b)


def test_broken_meta_is_rebuilt_from_wav(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    rid = r.save(_audio(2.0), BIG, ts=3000.0)
    assert rid is not None
    (tmp_path / f"{rid}.json").write_text("{не json", encoding="utf-8")
    [m] = r.items()
    assert m["id"] == rid and m["duration_s"] == 2.0 and m["status"] == "pending"
    r.update(rid, status="empty")
    assert json.loads((tmp_path / f"{rid}.json").read_text(encoding="utf-8"))["status"] == "empty"


def test_list_reads_meta_once_and_sees_changes(tmp_path: Path,
                                               monkeypatch: pytest.MonkeyPatch) -> None:
    """Список и ротация не открывают метаданные каждой записи заново: читаются только новые
    и изменившиеся; удалённое мимо программы из списка пропадает."""
    r = Recordings(tmp_path)
    ids = [r.save(_audio(0.1), BIG, ts=1000.0 + i) for i in range(5)]
    assert all(ids)
    r.items()
    reads: list[str] = []
    real = Recordings._read_meta

    def counting(self: Recordings, rid: str) -> dict[str, object] | None:
        reads.append(rid)
        return real(self, rid)

    monkeypatch.setattr(Recordings, "_read_meta", counting)
    assert [m["id"] for m in r.items()] == ids[::-1] and reads == []
    assert r.prune(BIG) == 0 and reads == []
    # новая запись — читается только она
    new = r.save(_audio(0.1), BIG, ts=2000.0)
    assert [m["id"] for m in r.items()][0] == new and reads == [new]
    reads.clear()
    # изменившиеся метаданные видны сразу
    r.update(ids[0], status="ok", chars=7)
    assert r.items()[-1]["status"] == "ok" and r.items()[-1]["chars"] == 7
    assert set(reads) == {ids[0]}
    reads.clear()
    # метаданные поправили мимо программы — перечитываются по отметке файла
    (tmp_path / f"{ids[1]}.json").write_text(json.dumps(
        {"ts": 1001.0, "duration_s": 0.1, "app": "x.exe", "status": "error", "chars": 0,
         "history_id": None}), encoding="utf-8")
    assert [m for m in r.items() if m["id"] == ids[1]][0]["app"] == "x.exe"
    assert reads == [ids[1]]
    # файл записи удалили в проводнике
    (tmp_path / f"{ids[2]}.wav").unlink()
    assert ids[2] not in [m["id"] for m in r.items()]
    # возвращённый список — копия: правка в нём не портит следующий
    r.items()[0]["status"] = "broken"
    assert r.items()[0]["status"] == "pending"


def test_rotation_with_thousands_keeps_all_within_limit(tmp_path: Path) -> None:
    """Лимит в тысячи записей: лишние не удаляются, сверх лимита — уходят самые старые."""
    r = Recordings(tmp_path)
    tmp_path.mkdir(exist_ok=True)
    wav = b"RIFF" + bytes(40) + bytes(320)
    for i in range(2100):
        rid = f"20261101-{i // 3600:02d}{i // 60 % 60:02d}{i % 60:02d}-000"
        (tmp_path / f"{rid}.wav").write_bytes(wav)
        (tmp_path / f"{rid}.json").write_text(json.dumps(
            {"id": rid, "ts": 5000.0 + i, "duration_s": 0.01, "app": "", "status": "ok",
             "chars": 1, "history_id": None}), encoding="utf-8")
    assert r.prune(Limits(10000, 10**9)) == 0 and len(r.items()) == 2100
    assert r.prune(Limits(2000, 10**9)) == 100
    left = r.items()
    assert len(left) == 2000 and left[0]["ts"] == 5000.0 + 2099 and left[-1]["ts"] == 5100.0
    assert len(list(tmp_path.glob("*.json"))) == 2000


@pytest.mark.parametrize("bad", ["", "..", "../rupor", "20261107-183305-581/../x", "abc",
                                 "C:\\Windows\\win", "20261107-183305"])
def test_ids_are_never_paths(tmp_path: Path, bad: str) -> None:
    r = Recordings(tmp_path)
    assert not valid_id(bad)
    assert r.audio(bad) is None and r.path(bad) is None and r.delete(bad) is False


def test_delete_and_clear(tmp_path: Path) -> None:
    r = Recordings(tmp_path)
    a = r.save(_audio(0.1), BIG, ts=1.0)
    r.save(_audio(0.1), BIG, ts=2.0)
    (tmp_path / "x.wav.part").write_bytes(b"0")
    assert a is not None and r.delete(a) is True and r.delete(a) is False
    assert r.clear() == 1 and r.items() == [] and not list(tmp_path.glob("*.part"))
