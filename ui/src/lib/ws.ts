// Клиент WebSocket-протокола ядра (docs/ARCHITECTURE.md → «WebSocket-протокол»).
//
// В Tauri порт и токен берутся командой backend_info, в обычном браузере — из адреса
// (?port=…&token=…). ?mock=1 — фейковый бэкенд из mock.ts (UI без ядра).
import { t, urlLocale } from "../i18n";
import { BackendError, EventHub, type ArgsTuple, type Backend, type CallOptions } from "./hub";
import { MockBackend } from "./mock";
import { backendInfo, inTauri, onCoreRestarted, type BackendInfo } from "./tauri";
import type {
  CommandName,
  CommandResult,
  ConnStatus,
  EventMap,
  EventName,
  LearnMode,
} from "./types";

const DEFAULT_TIMEOUT_MS = 45_000;
const RETRY_DELAYS_MS = [300, 700, 1500, 3000, 5000];

interface Pending {
  id: number;
  cmd: string;
  payload: string;
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

function isRecord(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null;
}

function paramsFromUrl(): BackendInfo | null {
  const q = new URLSearchParams(window.location.search);
  const port = Number(q.get("port"));
  const token = q.get("token");
  if (!port || !token) return null;
  return { port, token };
}

class WsBackend implements Backend {
  readonly mock = false;
  private ws: WebSocket | null = null;
  private st: ConnStatus = "connecting";
  private everOpened = false;
  private nextId = 1;
  private inflight = new Map<number, Pending>();
  private queue: Pending[] = [];
  private hub = new EventHub<EventMap>();
  private statusFns = new Set<(s: ConnStatus) => void>();
  private retry = 0;
  private retryTimer: ReturnType<typeof setTimeout> | null = null;
  private gen = 0;
  private started = false;

  start(): void {
    if (this.started) return;
    this.started = true;
    if (inTauri()) {
      // Ядро перезапущено Rust-ом: порт и токен новые — подключаемся заново.
      onCoreRestarted(() => this.restart());
    }
    void this.connect();
  }

  status(): ConnStatus {
    return this.st;
  }

  onStatus(fn: (s: ConnStatus) => void): () => void {
    this.statusFns.add(fn);
    return () => {
      this.statusFns.delete(fn);
    };
  }

  on<E extends EventName>(event: E, fn: (data: EventMap[E]) => void): () => void {
    return this.hub.on(event, fn);
  }

  call<C extends CommandName>(cmd: C, ...rest: ArgsTuple<C>): Promise<CommandResult<C>> {
    const [args, opts] = rest as [unknown, CallOptions | undefined];
    if (this.st === "no-params") {
      return Promise.reject(new BackendError(t("errors.noParams"), cmd));
    }
    return new Promise<CommandResult<C>>((resolve, reject) => {
      const id = this.nextId++;
      const msg: Record<string, unknown> = { id, cmd };
      if (args !== undefined) msg.args = args;
      const p: Pending = {
        id,
        cmd,
        payload: JSON.stringify(msg),
        resolve: resolve as (v: unknown) => void,
        reject,
        timer: setTimeout(() => {
          this.inflight.delete(id);
          this.queue = this.queue.filter((q) => q.id !== id);
          reject(new BackendError(t("errors.timeout"), cmd));
        }, opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS),
      };
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        this.send(p);
      } else {
        this.queue.push(p);
      }
    });
  }

  private send(p: Pending): void {
    if (!this.ws) {
      this.queue.push(p);
      return;
    }
    this.inflight.set(p.id, p);
    try {
      this.ws.send(p.payload);
    } catch {
      this.inflight.delete(p.id);
      this.queue.push(p);
    }
  }

  private setStatus(s: ConnStatus): void {
    if (this.st === s) return;
    this.st = s;
    for (const fn of [...this.statusFns]) fn(s);
  }

  private restart(): void {
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = null;
    this.retry = 0;
    this.gen++;
    const old = this.ws;
    this.ws = null;
    if (old) old.close();
    this.failInflight();
    void this.connect();
  }

  private failInflight(): void {
    for (const p of this.inflight.values()) {
      clearTimeout(p.timer);
      p.reject(new BackendError(t("errors.connectionLost"), p.cmd));
    }
    this.inflight.clear();
  }

  private async connect(): Promise<void> {
    const gen = ++this.gen;
    this.setStatus(this.everOpened ? "reconnecting" : "connecting");

    let info: BackendInfo | null;
    if (inTauri()) {
      try {
        info = await backendInfo();
      } catch (e) {
        console.warn("backend_info", e);
        if (gen === this.gen) this.scheduleRetry();
        return;
      }
    } else {
      info = paramsFromUrl();
      if (!info) {
        this.setStatus("no-params");
        for (const p of this.queue) {
          clearTimeout(p.timer);
          p.reject(new BackendError(t("errors.noParams"), p.cmd));
        }
        this.queue = [];
        return;
      }
    }
    if (gen !== this.gen) return;

    let ws: WebSocket;
    try {
      ws = new WebSocket(`ws://127.0.0.1:${info.port}/?token=${encodeURIComponent(info.token)}`);
    } catch (e) {
      console.warn("ws", e);
      this.scheduleRetry();
      return;
    }
    this.ws = ws;

    ws.onopen = () => {
      if (gen !== this.gen) {
        ws.close();
        return;
      }
      this.retry = 0;
      this.everOpened = true;
      this.setStatus("open");
      const q = this.queue;
      this.queue = [];
      for (const p of q) this.send(p);
    };

    ws.onmessage = (ev: MessageEvent) => {
      if (typeof ev.data === "string") this.handleMessage(ev.data);
    };

    ws.onclose = () => {
      if (this.ws === ws) this.ws = null;
      if (gen !== this.gen) return;
      this.failInflight();
      this.scheduleRetry();
    };

    ws.onerror = () => {
      // за ошибкой всегда следует close — там и переподключимся
    };
  }

  private scheduleRetry(): void {
    this.setStatus(this.everOpened ? "reconnecting" : "connecting");
    const delay = RETRY_DELAYS_MS[Math.min(this.retry, RETRY_DELAYS_MS.length - 1)] ?? 5000;
    this.retry++;
    if (this.retryTimer) clearTimeout(this.retryTimer);
    this.retryTimer = setTimeout(() => {
      this.retryTimer = null;
      void this.connect();
    }, delay);
  }

  private handleMessage(raw: string): void {
    let msg: unknown;
    try {
      msg = JSON.parse(raw);
    } catch {
      return;
    }
    if (!isRecord(msg)) return;
    if (typeof msg.id === "number") {
      const p = this.inflight.get(msg.id);
      if (!p) return;
      this.inflight.delete(msg.id);
      clearTimeout(p.timer);
      if (msg.ok === true) {
        p.resolve(msg.result ?? null);
      } else {
        const text = typeof msg.error === "string" && msg.error ? msg.error : t("errors.unknown");
        p.reject(new BackendError(text, p.cmd));
      }
      return;
    }
    if (typeof msg.event === "string") {
      this.hub.emit(msg.event as EventName, msg.data as never);
    }
  }
}

/** ?learn= в демо-режиме: 1/auto, suggest, off. */
function parseLearn(v: string | null): LearnMode | null {
  if (v === "1" || v === "auto") return "auto";
  if (v === "suggest" || v === "off") return v;
  return null;
}

function createBackend(): Backend {
  const q = new URLSearchParams(window.location.search);
  if (q.get("mock") === "1") {
    return new MockBackend({
      demo: q.get("demo") !== "0",
      firstRun: q.get("first_run") === "1",
      noModel: q.get("no_model") === "1",
      learn: parseLearn(q.get("learn")),
      pill: window.location.pathname.endsWith("pill.html"),
      meeting: window.location.pathname.endsWith("meeting.html"),
      suggest: q.get("suggest") === "1" || q.get("suggest") === "banner",
      aiOff: q.get("ai") === "0",
      rdp: q.get("rdp") === "0" ? "0" : q.get("rdp") === "none" ? "none" : null,
      lowBalance: q.get("low_balance") === "1",
      win10: q.get("win10") === "1",
      noGpu: q.get("gpu") === "0",
      scene: q.get("scene"),
      lang: urlLocale(),
    });
  }
  return new WsBackend();
}

/** Единственный клиент ядра на окно. */
export const backend: Backend = createBackend();

export function call<C extends CommandName>(
  cmd: C,
  ...rest: ArgsTuple<C>
): Promise<CommandResult<C>> {
  return backend.call(cmd, ...rest);
}

export function on<E extends EventName>(event: E, fn: (data: EventMap[E]) => void): () => void {
  return backend.on(event, fn);
}

export { BackendError };
