# -*- mode: python ; coding: utf-8 -*-
r"""PyInstaller: ядро Рупора, onedir → dist/rupor-core/rupor-core.exe.

Сборка (из корня проекта):
    .venv\Scripts\python -m PyInstaller --noconfirm packaging\rupor-core.spec
Консольная сборка для отладки (видны print и логи): задать RUPOR_CONSOLE=1 перед сборкой.

В сборку НЕ входят:
  * DLL CUDA из pip-пакетов nvidia-* — ядро качает при первом запуске только cuBLAS
    (колесо ≈0,55 ГБ, на диске ≈0,75 ГБ) в %LOCALAPPDATA%\Rupor\cuda
    (rupor.fetch.download_cuda); cuDNN и NVRTC CTranslate2 на GPU не нужны;
  * модель распознавания — кэш HuggingFace (rupor.fetch.download_model);
  * PyAV (FFmpeg и кодеки): faster-whisper нужен он только для чтения аудиофайлов, вместо него
    заглушка packaging/rthook_no_av.py — `rupor-core --file` в сборке недоступен;
  * PortAudio с ASIO и direct_url.json из метаданных (путь к проекту на машине сборки).

comtypes входит (UI Automation для обучения словаря, rupor.uia). Обёртка UIAutomationCore
(comtypes.gen.UIAutomationClient и модуль _944DE083_…) генерируется здесь, до анализа, и
кладётся в сборку как обычные модули: в замороженном ядре comtypes не пишет код в %TEMP%
и не нуждается в своём генераторе. Приглушение звука во время записи (rupor.duck) тоже на
comtypes, но интерфейсы Core Audio описаны в самом модуле — генерировать нечего, pycaw/psutil
не нужны (rupor.duck входит через collect_submodules("rupor")). Захват звука встречи
(rupor.loopback) — тоже comtypes: обработчик активации — comtypes.COMObject (comtypes._comobject
входит вместе с comtypes, генерировать нечего), а процесс захвата — этот же rupor-core.exe
в режиме --loopback (отдельного exe нет). Проверка после сборки:
    rupor-core.exe --uia-selftest   → строка «Самопроверка UIA: …, Core Audio: ok, сессий N,
                                      устройств вывода D, локализация: ok (ru K, en K), …,
                                      захват звука: ok (программы: да, весь звук: да,
                                      помощник: да)» в %APPDATA%\Rupor\logs\rupor.log
"""
import os
from pathlib import Path

from PyInstaller.utils.hooks import (collect_data_files, collect_dynamic_libs,
                                     collect_submodules, copy_metadata)

ROOT = Path(SPECPATH).resolve().parent


def _uia_wrapper_modules():
    """Сгенерировать (если ещё нет) обёртку UIAutomationCore для comtypes и вернуть имена
    её модулей — они уходят в hiddenimports, остальные модули comtypes.gen подтянутся
    по импортам."""
    import comtypes.client
    mod = comtypes.client.GetModule("UIAutomationCore.dll")
    names = ["comtypes.gen", "comtypes.gen.UIAutomationClient", mod.__name__]
    wrapper = getattr(mod, "__wrapper_module__", None)
    if wrapper is not None:
        names.append(wrapper.__name__)
    print(f"[rupor-core.spec] обёртка UI Automation: {', '.join(sorted(set(names)))}")
    return sorted(set(names))


CONSOLE = os.environ.get("RUPOR_CONSOLE") == "1"
ICON = ROOT / "ui" / "src-tauri" / "icons" / "icon.ico"

datas = collect_data_files("faster_whisper")  # assets/silero_vad_v6.onnx (VAD)
# частотные списки личного словаря частот (rupor.vocab): rupor/data/common_*.txt;
# каталоги сообщений ядра на русском и английском (rupor.i18n): rupor/locales/*.json
datas += collect_data_files("rupor", includes=["data/*.txt", "locales/*.json"])
# huggingface_hub узнаёт о hf_xet (быстрые загрузки) через importlib.metadata
for dist in ("hf_xet", "huggingface_hub", "tqdm", "rupor"):
    try:
        datas += copy_metadata(dist)
    except Exception as e:  # noqa: BLE001 — метаданные желательны, но не обязательны
        print(f"[rupor-core.spec] нет метаданных {dist}: {e}")

binaries = []
binaries += collect_dynamic_libs("ctranslate2")  # ctranslate2.dll, libiomp5md.dll, cudnn64_9.dll (шим)
binaries += collect_dynamic_libs("onnxruntime")

hiddenimports = []
hiddenimports += collect_submodules("rupor")       # модули ядра грузятся и лениво
hiddenimports += collect_submodules("websockets")  # websockets импортирует подмодули лениво
hiddenimports += ["pystray._win32", "PIL.Image", "PIL.ImageDraw", "sounddevice", "hf_xet"]
# UI Automation (rupor.uia): comtypes + готовая обёртка UIAutomationCore; comtypes.client —
# запасной путь (генерация обёртки на лету, если модуля вдруг нет)
hiddenimports += ["comtypes", "comtypes.client"] + _uia_wrapper_modules()

excludes = [
    "tkinter", "_tkinter", "pytest", "_pytest", "mypy", "mypyc", "comtypes.test", "IPython",
    "matplotlib", "pandas", "scipy", "torch", "PyInstaller", "pip",
    # PyAV (FFmpeg и кодеки, в т.ч. libx264/libx265 под GPL): faster-whisper нужен он только для
    # чтения аудиофайлов; вместо него — заглушка из runtime-хука packaging/rthook_no_av.py
    "av",
]

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT / "core")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[str(ROOT / "packaging" / "rthook_no_av.py")],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)


def _is_nvidia(dest, src):
    """DLL из pip-пакетов nvidia-* (cuBLAS, cuDNN, NVRTC) — в сборку не берём."""
    d = (dest or "").replace("\\", "/").lower()
    s = (src or "").replace("\\", "/").lower()
    return d.startswith("nvidia/") or "/site-packages/nvidia/" in s


def _foreign_portaudio(dest):
    """PortAudio для 32 бит, ARM64 и macOS не нужен (sounddevice на x64 берёт *64bit*.dll);
    вариант с ASIO (код Steinberg ASIO SDK) sounddevice грузит только при SD_ENABLE_ASIO."""
    d = (dest or "").replace("\\", "/").lower()
    if "portaudio-binaries/" not in d or d.endswith(".md"):
        return False
    return "64bit" not in d or "asio" in d


def _local_metadata(dest):
    """direct_url.json из *.dist-info: у editable-установки Рупора там абсолютный путь к
    папке проекта на машине сборки — в сборке он не нужен."""
    return (dest or "").replace("\\", "/").lower().endswith(".dist-info/direct_url.json")


def _drop(t):
    return _is_nvidia(t[0], t[1]) or _foreign_portaudio(t[0]) or _local_metadata(t[0])


_dropped = [t for t in a.binaries + a.datas if _is_nvidia(t[0], t[1])]
a.binaries = [t for t in a.binaries if not _drop(t)]
a.datas = [t for t in a.datas if not _drop(t)]
print(f"[rupor-core.spec] исключено файлов nvidia-*: {len(_dropped)}")

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="rupor-core",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,  # UPX ломает DLL CUDA/CTranslate2 и злит антивирусы
    console=CONSOLE,
    disable_windowed_traceback=True,  # без окна с трассировкой: см. packaging/entry.py
    icon=str(ICON) if ICON.is_file() else None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name="rupor-core",
)
