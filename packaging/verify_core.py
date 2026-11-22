r"""Проверка собранного ядра перед упаковкой установщика.

UI и ядро выпускаются вместе: если в ui\src-tauri\resources\core осталось старое ядро (без
обучения словаря — модулей rupor.learn, rupor.uia и comtypes), переключатель learn_mode
и кнопки «Добавить»/«Отменить» получат ошибку «неизвестная команда», а обучение не работает.

Запуск (из корня проекта):
    .venv\Scripts\python packaging\verify_core.py [путь\к\rupor-core.exe]
По умолчанию проверяется ui\src-tauri\resources\core\rupor-core.exe.

Ядро запускается с ``--uia-selftest`` — без хука клавиатуры, без окна и сети, выходит сразу —
во ВРЕМЕННЫХ APPDATA / LOCALAPPDATA / TEMP: настройки, база и логи пользователя не трогаются.
Проверяется: код выхода 0 (старое ядро не знает этого флага — код 2), в логе строка
«Самопроверка UIA: … своё поле: ok, частотные списки: ok (…), Core Audio: ok, сессий N,
устройств вывода D, локализация: ok (ru K, en K)» (частотные списки личного словаря частот —
rupor/data/*.txt — попали в сборку; интерфейсы Core Audio для приглушения звука во время
записи работают — устройства вывода и сессии только перечисляются, ничего не меняется;
каталоги сообщений rupor/locales/{ru,en}.json на месте и с одинаковыми ключами; проверка
орфографии Windows работает; захват звука для перевода встреч работает — «захват звука: ok
(…)»: обработчик активации на comtypes.COMObject, process loopback своего процесса, помощник
``rupor-core.exe --loopback`` отдаёт звук по каналу и выходит на закрытый stdin; на Windows
старее сборки 20348 годится и «захват звука: недоступен (…)»; ничего не играет и громкость
не трогает; ядро без любой из этих частей — старое), во временном TEMP ничего не
записано (обёртка UIAutomationCore для comtypes должна быть внутри сборки). Код выхода
скрипта: 0 — годится, 1 — нет. В вывод идёт только строка самопроверки (тип элемента с
фокусом и длина его текста — без самого текста; число аудиосессий — без имён)."""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_EXE = ROOT / "ui" / "src-tauri" / "resources" / "core" / "rupor-core.exe"
TIMEOUT_S = 120


def verify(exe: Path, runner: tuple[str, ...] = ()) -> tuple[bool, str]:
    """(годится ли ядро, пояснение по-русски). runner — чем запускать exe (тесты: python)."""
    if not exe.is_file():
        return False, f"нет файла {exe}"
    with tempfile.TemporaryDirectory(prefix="rupor-verify-") as tmp:
        dirs = {name: Path(tmp) / name for name in ("appdata", "local", "temp")}
        for d in dirs.values():
            d.mkdir()
        env = dict(os.environ, APPDATA=str(dirs["appdata"]), LOCALAPPDATA=str(dirs["local"]),
                   TEMP=str(dirs["temp"]), TMP=str(dirs["temp"]))
        try:
            r = subprocess.run([*runner, str(exe), "--uia-selftest"], env=env, cwd=tmp,
                               capture_output=True, timeout=TIMEOUT_S)
        except subprocess.TimeoutExpired:
            return False, f"ядро не завершилось за {TIMEOUT_S} с"
        except OSError as e:
            return False, f"ядро не запустилось: {e}"
        log = dirs["appdata"] / "Rupor" / "logs" / "rupor.log"
        text = log.read_text(encoding="utf-8", errors="replace") if log.is_file() else ""
        line = next((ln for ln in text.splitlines() if "Самопроверка UIA" in ln), "")
        summary = line.split("Самопроверка UIA:", 1)[-1].strip() if line else ""
        if r.returncode == 2 and not line:
            return False, ("ядро не знает --uia-selftest — это старая сборка без обучения "
                           "словаря; пересоберите: build.cmd core")
        if line and "частотные списки:" not in summary:
            return False, ("ядро без личного словаря частот (в самопроверке нет «частотные "
                           "списки») — это старая сборка; пересоберите: build.cmd core")
        if line and "Core Audio:" not in summary:
            return False, ("ядро без приглушения звука во время записи (в самопроверке нет "
                           "«Core Audio») — это старая сборка; пересоберите: build.cmd core")
        if line and "локализация:" not in summary:
            return False, ("ядро без английского языка (в самопроверке нет «локализация») — "
                           "это старая сборка; пересоберите: build.cmd core")
        if line and "орфография:" not in summary:
            return False, ("ядро без похожих написаний словаря (в самопроверке нет "
                           "«орфография») — это старая сборка; пересоберите: build.cmd core")
        if line and "захват звука:" not in summary:
            return False, ("ядро без перевода встреч (в самопроверке нет «захват звука») — "
                           "это старая сборка; пересоберите: build.cmd core")
        if r.returncode != 0 or "своё поле: ok" not in summary:
            return False, f"самопроверка не прошла (код {r.returncode}): {summary or 'нет строки в логе'}"
        if "частотные списки: ok" not in summary:
            return False, ("в сборке нет частотных списков rupor/data/*.txt "
                           f"(packaging\\rupor-core.spec): {summary}")
        if "Core Audio: ok" not in summary:
            return False, f"Core Audio в сборке не работает (comtypes, rupor.duck): {summary}"
        if "локализация: ok" not in summary:
            return False, ("в сборке нет каталогов сообщений rupor/locales/*.json или их ключи "
                           f"разошлись (packaging\\rupor-core.spec): {summary}")
        if "орфография: ok" not in summary:
            return False, ("проверка орфографии Windows в сборке не работает (comtypes, "
                           f"rupor.spell): {summary}")
        if "захват звука: ok" not in summary and "захват звука: недоступен (" not in summary:
            return False, ("захват звука для перевода встреч в сборке не работает "
                           f"(comtypes.COMObject, rupor.loopback, --loopback): {summary}")
        leftovers =[p for p in dirs["temp"].rglob("*") if p.is_file()]
        if leftovers:
            return False, (f"ядро записало {len(leftovers)} файл(ов) во временную папку — "
                           "похоже, обёртки comtypes нет в сборке (packaging\\rupor-core.spec)")
        return True, summary


def main(argv: list[str]) -> int:
    exe = Path(argv[1]) if len(argv) > 1 else DEFAULT_EXE
    ok, why = verify(exe)
    print(("[ок] ядро годится: " if ok else "[ошибка] ") + why)
    return 0 if ok else 1


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main(sys.argv))
