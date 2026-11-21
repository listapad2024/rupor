"""packaging/verify_core.py: проверка ядра в ресурсах Tauri перед сборкой установщика.

Настоящее rupor-core.exe здесь не запускается: вместо него — крошечные скрипты-«ядра»
(старое — не знает --uia-selftest; свежее — пишет строку самопроверки в лог; неряшливое —
пишет во временную папку)."""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parent.parent


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("verify_core",
                                                  ROOT / "packaging" / "verify_core.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


vc = _load()

GOOD = r'''
import os, sys, pathlib
assert sys.argv[1:] == ["--uia-selftest"]
d = pathlib.Path(os.environ["APPDATA"]) / "Rupor" / "logs"
d.mkdir(parents=True)
(d / "rupor.log").write_text(
    "2026 INFO rupor: Самопроверка UIA: UIA: ok, таймауты 1 с, фокус: тип 50004, "
    "поле с текстом: да, длина 3, своё поле: ok"
    + (", частотные списки: нет (ru 0, en 0)" if "NOLISTS" in os.environ.get("FAKE", "")
       else "" if "OLDSELFTEST" in os.environ.get("FAKE", "")
       else ", частотные списки: ok (ru 2500, en 1800)")
    + ("" if "NOAUDIO" in os.environ.get("FAKE", "") or "OLDSELFTEST" in os.environ.get("FAKE", "")
       else ", Core Audio: ошибка COMError" if "BADAUDIO" in os.environ.get("FAKE", "")
       else ", Core Audio: ok, сессий 7")
    + ("" if os.environ.get("FAKE", "") in ("NOAUDIO", "OLDSELFTEST", "NOLOCALES")
       else ", локализация: нет (ru 370, en 0)" if "BADLOCALES" in os.environ.get("FAKE", "")
       else ", локализация: ok (ru 370, en 370)")
    + ("" if os.environ.get("FAKE", "") in ("NOAUDIO", "OLDSELFTEST", "NOLOCALES", "NOSPELL")
       else ", орфография: ошибка (API недоступно)" if "BADSPELL" in os.environ.get("FAKE", "")
       else ", орфография: ok (ru да, en да)")
    + ("" if os.environ.get("FAKE", "") in ("NOAUDIO", "OLDSELFTEST", "NOLOCALES", "NOSPELL",
                                             "NOCAPTURE")
       else ", захват звука: ошибка (GetActivateResult: 0x8000000E E_ILLEGAL_METHOD_CALL)"
       if "BADCAPTURE" in os.environ.get("FAKE", "")
       else ", захват звука: недоступен (программы: нет (сборка Windows 19045, нужна 20348), "
            "весь звук: да, помощник: да)" if "OLDWINCAPTURE" in os.environ.get("FAKE", "")
       else ", захват звука: ok (программы: да, весь звук: да, помощник: да)") + "\n",
    encoding="utf-8")
if os.environ.get("FAKE", "") in ("BADAUDIO", "BADLOCALES", "BADSPELL", "BADCAPTURE"):
    sys.exit(1)
if "LITTER" in os.environ.get("FAKE", ""):
    (pathlib.Path(os.environ["TEMP"]) / "comtypes_gen.py").write_text("x")
'''
OLD = "import sys; sys.exit(2)   # argparse: unrecognized arguments: --uia-selftest\n"


def _fake(tmp_path: Path, body: str) -> Path:
    p = tmp_path / "fake_core.py"
    p.write_text(body, encoding="utf-8")
    return p


def test_fresh_core_passes(tmp_path: Path) -> None:
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert ok, why
    assert "своё поле: ok" in why and "Core Audio: ok, сессий 7" in why
    assert "локализация: ok (ru 370, en 370)" in why
    assert "орфография: ok (ru да, en да)" in why
    assert "захват звука: ok (программы: да, весь звук: да, помощник: да)" in why


def test_core_without_capture_is_rejected_as_old(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "NOCAPTURE")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "старая сборка" in why and "захват звука" in why


def test_core_with_broken_capture_is_rejected(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "BADCAPTURE")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "захват звука: ошибка" in why


def test_capture_unavailable_on_old_windows_is_accepted(tmp_path: Path,
                                                        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "OLDWINCAPTURE")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert ok, why
    assert "захват звука: недоступен (" in why


def test_core_without_spell_is_rejected_as_old(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "NOSPELL")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "старая сборка" in why and "орфография" in why


def test_core_with_broken_spell_is_rejected(tmp_path: Path,
                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "BADSPELL")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "орфография: ошибка" in why


def test_old_core_is_rejected(tmp_path: Path) -> None:
    ok, why = vc.verify(_fake(tmp_path, OLD), runner=(sys.executable,))
    assert not ok and "старая сборка" in why


def test_core_writing_to_temp_is_rejected(tmp_path: Path,
                                          monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "LITTER")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "временную папку" in why


def test_missing_exe(tmp_path: Path) -> None:
    ok, why = vc.verify(tmp_path / "нет.exe")
    assert not ok and "нет файла" in why


def test_core_without_vocab_lists_is_rejected(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "NOLISTS")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "частотных списков" in why


def test_core_before_vocab_is_rejected_as_old(tmp_path: Path,
                                             monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "OLDSELFTEST")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "старая сборка" in why


def test_core_without_audio_ducking_is_rejected_as_old(tmp_path: Path,
                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "NOAUDIO")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "старая сборка" in why and "Core Audio" in why


def test_core_with_broken_core_audio_is_rejected(tmp_path: Path,
                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "BADAUDIO")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "Core Audio: ошибка" in why


def test_core_without_locales_is_rejected_as_old(tmp_path: Path,
                                                monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "NOLOCALES")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "старая сборка" in why and "локализация" in why


def test_core_with_broken_locales_is_rejected(tmp_path: Path,
                                              monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FAKE", "BADLOCALES")
    ok, why = vc.verify(_fake(tmp_path, GOOD), runner=(sys.executable,))
    assert not ok and "локализация: нет" in why
