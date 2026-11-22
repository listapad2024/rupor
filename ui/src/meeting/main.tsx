import "@fontsource-variable/inter";
import "../styles/app.css";
import "./meeting.css";
import { Component, Fragment, StrictMode, type ErrorInfo, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { meetingWindowLog } from "../lib/tauri";
import { applyCachedTheme } from "../lib/theme";
import { Meeting } from "./Meeting";

// Тема из прошлого запуска (общий кэш окон) — до ответа ядра окно не мигает чужим фоном.
applyCachedTheme();
document.documentElement.classList.add("meeting-root");

/** Не больше стольких перерисовок за минуту — дальше страница перезагружается целиком. */
const MAX_RESTARTS = 5;

function describe(e: unknown): string {
  if (e instanceof Error) return `${e.name}: ${e.message} ${e.stack ?? ""}`;
  return String(e);
}

// Ошибки вне отрисовки (обработчики, промисы) — в журнал оболочки, не чаще раза в 10 с.
// Текст журнала — технический, по-английски (кириллица — только в каталогах i18n).
let lastLogged = 0;
function logOnce(prefix: string, e: unknown): void {
  const now = Date.now();
  if (now - lastLogged < 10_000) return;
  lastLogged = now;
  void meetingWindowLog("warn", `${prefix}: ${describe(e)}`);
}
window.addEventListener("error", (ev) => logOnce("error", ev.error ?? ev.message));
window.addEventListener("unhandledrejection", (ev) => logOnce("unhandled rejection", ev.reason));

/** Перезагрузок страницы — не больше стольких за RELOAD_WINDOW_MS (счёт переживает reload). */
const MAX_RELOADS = 2;
const RELOAD_WINDOW_MS = 10 * 60_000;
const RELOADS_KEY = "rupor.meeting.reloads";

function recentReloads(now: number): number[] {
  try {
    const raw = JSON.parse(window.sessionStorage.getItem(RELOADS_KEY) ?? "[]") as unknown;
    return Array.isArray(raw) ? raw.filter((t): t is number => typeof t === "number" && now - t < RELOAD_WINDOW_MS) : [];
  } catch {
    return [];
  }
}

/** Перезагрузить страницу, если лимит не исчерпан; false — исчерпан. */
function reloadPage(now: number): boolean {
  const list = recentReloads(now);
  if (list.length >= MAX_RELOADS) return false;
  try {
    window.sessionStorage.setItem(RELOADS_KEY, JSON.stringify([...list, now]));
  } catch {
    return false; // без хранилища счёт не сохранить — не рискуем бесконечной перезагрузкой
  }
  window.setTimeout(() => window.location.reload(), 1000);
  return true;
}

/**
 * Ошибка при отрисовке окна перевода не должна оставлять пустую панель посреди встречи (как и
 * у пилюли, pill/main.tsx): причина — в журнал оболочки (не чаще раза в минуту), окно
 * перерисовывается через секунду (строки снова приходят из ядра, meeting_lines); падает снова
 * и снова (больше MAX_RESTARTS за минуту) — страница перезагружается (не больше MAX_RELOADS за
 * 10 минут), а дальше перерисовка с нарастающей паузой (до минуты).
 */
class Guard extends Component<{ children: ReactNode }, { failed: boolean; generation: number }> {
  override state = { failed: false, generation: 0 };
  private restarts: number[] = [];
  private lastLog = 0;
  private backoff = 1000;

  static getDerivedStateFromError(): { failed: boolean } {
    return { failed: true };
  }

  override componentDidCatch(error: unknown, info: ErrorInfo): void {
    const now = Date.now();
    if (now - this.lastLog >= 60_000) {
      this.lastLog = now;
      void meetingWindowLog("error", `render failed: ${describe(error)} ${info.componentStack ?? ""}`);
    }
    this.restarts = this.restarts.filter((t) => now - t < 60_000);
    this.restarts.push(now);
    let delay = 1000;
    if (this.restarts.length > MAX_RESTARTS) {
      if (reloadPage(now)) return;
      delay = this.backoff;
      this.backoff = Math.min(this.backoff * 2, 60_000);
    }
    window.setTimeout(
      () => this.setState((s) => ({ failed: false, generation: s.generation + 1 })),
      delay,
    );
  }

  override render(): ReactNode {
    if (this.state.failed) return null;
    // новый key — дерево окна монтируется заново, с чистым состоянием
    return <Fragment key={this.state.generation}>{this.props.children}</Fragment>;
  }
}

const root = document.getElementById("meeting-root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Guard>
        <Meeting />
      </Guard>
    </StrictMode>,
  );
}
