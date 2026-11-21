"""Запуск ядра Рупора.

  python -m rupor                         — старый режим: хук + трей pystray (без UI);
  python -m rupor --sidecar --ws-port P --token T [--ui-pid PID]
                                          — ядро для UI на Tauri: WebSocket-сервер, без трея;
  python -m rupor --file x.wav [--type]   — распознать файл (и напечатать через 3 с);
  python -m rupor --uia-selftest          — проверить сборку: UI Automation, частотные
                                            списки, Core Audio, каталоги сообщений,
                                            орфография, захват звука (результат — в лог, код
                                            выхода 0/1; текст полей и имена не пишутся);
  python -m rupor --loopback <цель> [--parent PID]
                                          — служебный режим (его запускает само ядро для
                                            перевода встречи, rupor.loopback): звук программы
                                            (pid:<pid> | system | exclude:<pid>) — в stdout
                                            (float32, 16 кГц, моно), журнал — в stderr; без
                                            настроек, лога ядра и проверки экземпляра.

Для тестов: --no-hook (не ставить хук клавиатуры) и --no-mutex (не проверять
единственный экземпляр). --cpu — распознавание на CPU, -v — подробный лог.
"""
from __future__ import annotations

import argparse
import ctypes
import logging
import os
import sys
import threading
import time

from . import config, i18n
from . import log as logsetup
from .i18n import tr

log = logging.getLogger("rupor")
_MUTEX_NAME = "Local\\RuporSingleInstance"


def _single_instance() -> bool:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.CreateMutexW.restype = ctypes.c_void_p
    _single_instance.handle = k32.CreateMutexW(None, False, _MUTEX_NAME)  # type: ignore[attr-defined]
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def run_file(path: str, do_type: bool, s: config.Settings) -> int:
    """Распознать файл; с --type — напечатать результат в активное окно через 3 с."""
    from . import asr, engine
    rec = asr.Recognizer(s, on_notice=lambda text, _level: print(text))
    rec.load()
    res = rec.transcribe(asr.load_wav(path))
    print(tr("cli.file_result", lang=res.language, audio=f"{res.audio_s:.1f}",
             ms=f"{res.elapsed_ms:.0f}", device=rec.device))
    print(res.text)
    if do_type:
        print(tr("cli.type_in"))
        time.sleep(3)
        engine.deliver(engine.finalize_text(res.text, s.add_trailing_space), s, False,
                       lambda text, **_kw: print(text))
    return 0


def run_uia_selftest() -> int:
    """Самопроверка сборки. Сборка оконная (stdout нет) — результат в лог: UI Automation
    (тип элемента с фокусом, есть ли у него текст и его длина — без текста), частотные
    списки словаря частот, Core Audio (число аудиосессий — без имён, ничего не меняя),
    каталоги сообщений locales/*.json (на месте и с одинаковыми ключами), проверка
    орфографии Windows (похожие написания словаря), захват звука для перевода встреч
    (comtypes.COMObject, process loopback своего процесса, помощник --loopback)."""
    from . import duck, loopback, spell, uia, vocab
    ok, summary = uia.selftest()
    # Частотные списки словаря частот (data/*.txt) должны попасть в сборку
    v_ok, v_summary = vocab.selftest()
    # Интерфейсы Core Audio (приглушение звука во время записи) работают в сборке
    a_ok, a_summary = duck.selftest()
    # Каталоги сообщений ru/en (locales/*.json) должны попасть в сборку
    l_ok, l_summary = i18n.selftest()
    # Проверка орфографии Windows (spellcheck.h через comtypes) — для похожих написаний
    s_ok, s_summary = spell.selftest()
    # Захват звука для перевода встреч: обработчик активации на comtypes.COMObject и
    # помощник --loopback (ничего не играет и громкость не трогает)
    c_ok, c_summary = loopback.selftest()
    ok = ok and v_ok and a_ok and l_ok and s_ok and c_ok
    summary = f"{summary}, {v_summary}, {a_summary}, {l_summary}, {s_summary}, {c_summary}"
    (log.info if ok else log.error)("Самопроверка UIA: %s", summary)
    return 0 if ok else 1


def run_tray(s: config.Settings, hook: bool) -> int:
    from .engine import Engine
    from .tray import Tray
    eng = Engine(s, auto_download_model=True)
    eng.start(hook=hook)
    log.info("Рупор запущен (без UI). Удерживайте %s и говорите.", s.ptt_key)
    Tray(eng).run()  # блокирует до «Выход»
    eng.storage.close()
    return 0


def run_sidecar(args: argparse.Namespace, s: config.Settings) -> int:
    from . import winutil
    from .api import Api
    from .engine import Engine
    from .server import WsServer

    quit_ev = threading.Event()
    own = (args.ui_pid,) if args.ui_pid else ()
    eng = Engine(s, own_pids=own, auto_download_model=False)
    eng.on_quit = quit_ev.set
    api = Api(eng)

    def on_clients(n: int) -> None:
        if n == 0:
            # UI отключился — индикатор микрофона мастера больше не нужен, а проверочную
            # запись уже некому остановить (иначе диктовка заблокирована навсегда)
            eng.set_mic_monitor(False)
            eng.cancel_test_record()

    srv = WsServer(api, eng.bus, args.ws_port, args.token, on_clients=on_clients)
    try:
        srv.start()
    except OSError as e:
        log.error("Не удалось открыть порт %d: %s", args.ws_port, e)
        return 1
    eng.start(hook=not args.no_hook)

    def session_end(ending: bool) -> None:
        if ending:
            log.info("Windows завершает сеанс — ядро выходит")
            quit_ev.set()
        else:
            eng.session_end_cancelled()

    # Конец сеанса Windows: звук компьютера, приглушённый записью, вернуть сразу — оболочка
    # на Tauri в это время снимает ядро, не дожидаясь его выхода
    winutil.SessionEndWatcher(eng.session_ending, session_end).start()
    if args.ui_pid:
        def ui_gone() -> None:
            log.info("Окно Рупора (pid %d) завершилось — ядро выходит", args.ui_pid)
            quit_ev.set()
        winutil.watch_process(args.ui_pid, ui_gone)
    log.info("Ядро Рупора запущено (sidecar, порт %d%s)", srv.port,
             ", без хука клавиатуры" if args.no_hook else "")
    try:
        while not quit_ev.wait(0.5):
            pass
    except KeyboardInterrupt:
        pass
    log.info("Ядро Рупора завершает работу")
    try:
        eng.shutdown()
        srv.stop()
        eng.storage.close()
    except Exception:
        log.exception("Ошибка при завершении")
    logging.shutdown()
    # Потоки CUDA/PortAudio могут задержать обычный выход интерпретатора
    os._exit(0)


def main() -> int:
    # Справка — на языке Windows: настройки ещё не прочитаны
    ap = argparse.ArgumentParser(prog="rupor", description=tr("cli.description"))
    ap.add_argument("--file", help=tr("cli.help_file"))
    ap.add_argument("--type", action="store_true", help=tr("cli.help_type"))
    ap.add_argument("--cpu", action="store_true", help=tr("cli.help_cpu"))
    ap.add_argument("--sidecar", action="store_true", help=tr("cli.help_sidecar"))
    ap.add_argument("--ws-port", type=int, default=0, help=tr("cli.help_ws_port"))
    ap.add_argument("--token", default="", help=tr("cli.help_token"))
    ap.add_argument("--ui-pid", type=int, default=0, help=tr("cli.help_ui_pid"))
    ap.add_argument("--no-hook", action="store_true", help=tr("cli.help_no_hook"))
    ap.add_argument("--no-mutex", action="store_true", help=tr("cli.help_no_mutex"))
    ap.add_argument("--uia-selftest", action="store_true", help=tr("cli.help_selftest"))
    ap.add_argument("--loopback", metavar="TARGET", help=tr("cli.help_loopback"))
    ap.add_argument("--parent", type=int, default=0, help=tr("cli.help_parent"))
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    if args.loopback is not None:
        # Помощник захвата звука встречи: stdout — только звук, журнал — в stderr (лог ядра
        # с ротацией из второго процесса не пишем); настройки и мьютекс не нужны
        from . import loopback
        loopback.helper_main(args.loopback, args.parent or None)

    for stream in (sys.stdout, sys.stderr):
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    logsetup.setup(args.verbose)
    if args.uia_selftest:
        return run_uia_selftest()
    s = config.load()
    i18n.set_language(s.language)
    if args.cpu:
        s.device, s.compute_type = "cpu", "int8"
    if args.file:
        return run_file(args.file, args.type, s)
    if args.sidecar and (not 0 < args.ws_port < 65536 or not args.token):
        log.error("Режим --sidecar требует --ws-port (1–65535) и --token")
        return 2

    if not args.no_mutex and not _single_instance():
        log.error("Рупор уже запущен")
        return 1
    if args.sidecar:
        return run_sidecar(args, s)
    return run_tray(s, hook=not args.no_hook)


if __name__ == "__main__":
    sys.exit(main())
