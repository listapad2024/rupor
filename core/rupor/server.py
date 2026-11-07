"""WebSocket-сервер ядра: ws://127.0.0.1:<port>/?token=<token> (docs/ARCHITECTURE.md).

- asyncio-цикл живёт в своём потоке; команды выполняются в пуле потоков (Api.respond),
  так что долгие команды не задерживают остальные и события;
- команды одного соединения выполняются по порядку (mic_monitor false→true, быстрые
  set_settings от ползунка); долгие (LONG_COMMANDS) тоже ждут предыдущие, но следующие
  команды их не ждут;
- без верного токена соединение закрывается с кодом 4401, не с 127.0.0.1 — 4403;
  рукопожатие со страницы в браузере (заголовок Origin не окна Рупора) отклоняется (403);
- сразу после подключения — событие hello (= get_state), затем предупреждения и ошибки,
  которые ядро успело выдать, пока UI ещё не подключился (откат на CPU, нет микрофона);
- события шины движка рассылаются всем клиентам потокобезопасно
  (loop.call_soon_threadsafe → websockets.broadcast); JSON в UTF-8 без \\u-экранирования.
"""
from __future__ import annotations

import asyncio
import collections
import hmac
import json
import logging
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

from websockets.asyncio.server import Server, ServerConnection, broadcast, serve
from websockets.exceptions import ConnectionClosed
from websockets.typing import Origin

from .api import Api
from .engine import PUBLIC_EVENTS, Bus
from .i18n import tr

log = logging.getLogger(__name__)
# Библиотека websockets пишет строку на каждое соединение, а Rust проверяет готовность ядра
# голым TCP-подключением каждые 150 мс — в лог ядра пускаем только предупреждения и ошибки.
_ws_log = logging.getLogger("rupor.ws")
_ws_log.setLevel(logging.WARNING)

HOST = "127.0.0.1"
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
MAX_MESSAGE = 32 * 1024 * 1024  # импорт словаря может быть большим

# Команды, которые ждут сеть, модель, NVML или диск (model_dir_set создаёт папку и пишет
# пробный файл — на недоступном сетевом пути это десятки секунд): идут параллельно, не держат
# очередь соединения
LONG_COMMANDS = frozenset({"ai_test", "ai_key_check", "ai_balance", "ai_key_info", "test_record_stop",
                           "cuda_check", "model_recommend", "model_dir_set"})

# Окна Tauri (Windows: http(s)://tauri.localhost; macOS/Linux: tauri://localhost), dev-сервер
# Vite (npm run dev / preview) и клиенты без Origin (Rust-проба, тесты, tools/ws_smoke.py).
# Прочие страницы браузера до ядра не достучатся. Дополнительные — через RUPOR_WS_ORIGINS.
ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://tauri.localhost", "https://tauri.localhost", "tauri://localhost",
    "http://localhost:1420", "http://127.0.0.1:1420",
    "http://localhost:1421", "http://127.0.0.1:1421",
)

PENDING_NOTICE_MAX_AGE_S = 60.0  # предупреждения без клиентов досылаются новым клиентам
PENDING_NOTICE_MAX = 20
REJECT_LOG_INTERVAL_S = 30.0     # отказы по токену — не чаще раза в 30 с


def _origins() -> list[Origin | None]:
    extra = [o.strip() for o in os.environ.get("RUPOR_WS_ORIGINS", "").split(",") if o.strip()]
    return [None, *(Origin(o) for o in (*ALLOWED_ORIGINS, *extra))]


def _default(o: Any) -> Any:
    if hasattr(o, "item") and callable(o.item):  # numpy-скаляры
        return o.item()
    if isinstance(o, Path):
        return str(o)
    if isinstance(o, (set, frozenset, tuple)):
        return list(o)
    if is_dataclass(o) and not isinstance(o, type):
        return asdict(o)
    return str(o)


def dumps(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False, separators=(",", ":"), default=_default)


class WsServer:
    def __init__(self, api: Api, bus: Bus, port: int, token: str, host: str = HOST,
                 on_clients: Callable[[int], None] | None = None, workers: int = 8) -> None:
        if not token:
            raise ValueError(tr("server.no_token"))
        self.api = api
        self.bus = bus
        self.host = host
        self.port = port
        self._token = token
        self._on_clients = on_clients
        self._pool = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="rupor-api")
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: Server | None = None
        self._clients: set[ServerConnection] = set()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._error: BaseException | None = None
        self._stopped: asyncio.Event | None = None
        self._pending: collections.deque[tuple[float, str]] = collections.deque(
            maxlen=PENDING_NOTICE_MAX)
        self._pending_lock = threading.Lock()
        self._rejected = 0
        self._reject_logged = 0.0

    # ---------- запуск и остановка ----------

    def start(self, timeout: float = 10.0) -> int:
        """Запустить сервер; вернуть фактический порт. Ошибка bind — OSError."""
        self._thread = threading.Thread(target=self._run, name="rupor-ws", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout):
            raise TimeoutError(tr("server.not_started"))
        if self._error is not None:
            raise self._error
        self.bus.subscribe(self.publish)
        log.info("WebSocket-сервер слушает %s:%d", self.host, self.port)
        return self.port

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(self._main())
        except BaseException as e:  # noqa: BLE001 — отдаём в start()
            if not self._ready.is_set():
                self._error = e
                self._ready.set()
            else:
                log.exception("WebSocket-сервер упал")
        finally:
            loop.close()

    async def _main(self) -> None:
        self._stopped = asyncio.Event()
        async with serve(self._handler, self.host, self.port, max_size=MAX_MESSAGE,
                         logger=_ws_log, compression=None, origins=_origins()) as server:
            self._server = server
            sock = next(iter(server.sockets))
            self.port = int(sock.getsockname()[1])
            self._ready.set()
            await self._stopped.wait()
            server.close()
            await server.wait_closed()

    def stop(self) -> None:
        self.bus.unsubscribe(self.publish)
        loop, stopped = self._loop, self._stopped
        if loop is not None and stopped is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(stopped.set)
            except RuntimeError:
                pass
        if self._thread is not None:
            self._thread.join(3)
        self._pool.shutdown(wait=False, cancel_futures=True)

    @property
    def clients(self) -> int:
        return len(self._clients)

    # ---------- события → клиентам ----------

    def publish(self, event: str, data: dict[str, Any]) -> None:
        """Подписчик шины: вызывается из любого потока.

        Предупреждения и ошибки (notice warn/error), выданные, пока ни один клиент не
        подключён (старт ядра опережает окна Tauri), запоминаются и досылаются каждому
        новому клиенту в течение PENDING_NOTICE_MAX_AGE_S."""
        if event not in PUBLIC_EVENTS:
            return
        pending = (event == "notice" and not self._clients
                   and data.get("level") in ("warn", "error"))
        if not self._clients and not pending:
            return
        try:
            text = dumps({"event": event, "data": data})
        except (TypeError, ValueError):
            log.exception("Событие %s не сериализуется", event)
            return
        if pending:
            with self._pending_lock:
                self._pending.append((time.monotonic(), text))
            return
        loop = self._loop
        if loop is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(self._broadcast, text)
        except RuntimeError:
            pass  # цикл уже остановлен

    def _broadcast(self, text: str) -> None:
        if self._clients:
            broadcast(self._clients, text)

    # ---------- соединение ----------

    def _authorized(self, ws: ServerConnection) -> bool:
        try:
            query = parse_qs(urlsplit(ws.request.path).query) if ws.request else {}
        except ValueError:
            return False
        token = (query.get("token") or [""])[0]
        return hmac.compare_digest(token.encode(), self._token.encode())

    async def _handler(self, ws: ServerConnection) -> None:
        remote = ws.remote_address[0] if ws.remote_address else ""
        if remote not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            await ws.close(CLOSE_FORBIDDEN, "forbidden")
            return
        if not self._authorized(ws):
            self._log_rejected()
            await ws.close(CLOSE_UNAUTHORIZED, "unauthorized")
            return
        loop = asyncio.get_running_loop()
        try:
            hello = await loop.run_in_executor(self._pool, self.api.hello)
            await ws.send(dumps({"event": "hello", "data": hello}))
            for text in self._pending_notices():
                await ws.send(text)
        except ConnectionClosed:
            return
        self._clients.add(ws)
        self._clients_changed()
        tasks: set[asyncio.Task[None]] = set()
        prev: asyncio.Task[None] | None = None  # последняя «обычная» команда соединения
        try:
            async for msg in ws:
                req_id, cmd, args, error = _parse(msg)
                # Каждая команда ждёт предыдущие «обычные» (test_record_stop не обгонит
                # test_record_start); долгая сама очередь не держит — следующие её не ждут.
                t = asyncio.create_task(self._serve(ws, req_id, cmd, args, error, prev))
                if not (isinstance(cmd, str) and cmd in LONG_COMMANDS):
                    prev = t
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        except ConnectionClosed:
            pass
        finally:
            self._clients.discard(ws)
            self._clients_changed()

    def _pending_notices(self) -> list[str]:
        now = time.monotonic()
        with self._pending_lock:
            while self._pending and now - self._pending[0][0] > PENDING_NOTICE_MAX_AGE_S:
                self._pending.popleft()
            return [text for _, text in self._pending]

    def _log_rejected(self) -> None:
        """Отказы по токену — в лог не чаще раза в REJECT_LOG_INTERVAL_S: перебор портов
        со страницы не вытеснит из ротируемого лога настоящие записи."""
        self._rejected += 1
        now = time.monotonic()
        if now - self._reject_logged >= REJECT_LOG_INTERVAL_S:
            log.warning("Отклонено подключений без верного токена: %d", self._rejected)
            self._rejected = 0
            self._reject_logged = now

    def _clients_changed(self) -> None:
        if self._on_clients is not None:
            try:
                self._on_clients(len(self._clients))
            except Exception:
                log.exception("on_clients")

    async def _serve(self, ws: ServerConnection, req_id: Any, cmd: Any, args: Any,
                     error: str | None, after: asyncio.Task[None] | None) -> None:
        """Выполнить команду; after — дождаться предыдущей команды этого соединения."""
        if after is not None and not after.done():
            await asyncio.wait({after})
        resp: dict[str, Any]
        if error is not None:
            resp = {"id": req_id, "ok": False, "error": error}
        else:
            loop = asyncio.get_running_loop()
            try:
                resp = await loop.run_in_executor(self._pool, self.api.respond, req_id, cmd, args)
            except RuntimeError:  # пул остановлен — ядро завершается
                return
        try:
            await ws.send(dumps(resp))
        except ConnectionClosed:
            pass
        except (TypeError, ValueError):
            log.exception("Ответ на %s не сериализуется", req_id)
            await ws.send(dumps({"id": req_id, "ok": False,
                                 "error": tr("server.not_serializable")}))


def _parse(msg: str | bytes) -> tuple[Any, Any, Any, str | None]:
    """Сообщение клиента → (id, cmd, args, ошибка формата или None)."""
    req_id: Any = None
    try:
        req = json.loads(msg)
        if not isinstance(req, dict):
            raise ValueError
        req_id = req.get("id")
        return req_id, req.get("cmd"), req.get("args"), None
    except (ValueError, UnicodeDecodeError):
        return req_id, None, None, tr("server.bad_message")
