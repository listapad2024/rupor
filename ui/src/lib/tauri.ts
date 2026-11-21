// Обёртки над Tauri API. Вне Tauri (обычный браузер) все функции безопасны:
// команды тихо ничего не делают, подписки возвращают пустую отписку.
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import type { CoreStateName, InsertMode, MeetingSuggest, UiLanguage } from "./types";

export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

export interface BackendInfo {
  port: number;
  token: string;
}

export async function backendInfo(): Promise<BackendInfo> {
  const info = await invoke<BackendInfo>("backend_info");
  return { port: Number(info.port), token: String(info.token) };
}

export interface TrayInfo {
  state: CoreStateName;
  paused: boolean;
  insert_mode: InsertMode;
  style: string;
  /** Подменю «Стиль» в трее: [{id, name}] из styles_list. */
  styles?: { id: string; name: string }[];
  /**
   * Неактивная строка меню «ИИ: сегодня $0.03 · баланс $12.34»; null — ИИ выключен,
   * строки нет. Интерфейс присылает поле всегда.
   */
  ai_line?: string | null;
  /**
   * Подменю «Рассылка в RDP ▸» с флажками; [] — пункт «Нет открытых RDP-сессий».
   * Клик → tray-action "rdp:<key>" (key как пришёл). null — список ещё не загружен.
   */
  rdp?: TrayRdpItem[] | null;
  /**
   * Все строки меню трея и подсказки значка на языке интерфейса. Пока UI не прислал
   * labels, оболочка берёт встроенные строки по языку Windows.
   */
  labels?: TrayLabels;
  /** Идёт перевод встречи: пункт «Перевод встречи (идёт)» и подсказка значка. */
  meeting_running?: boolean;
}

/**
 * Строки трея (ключи — snake_case, как их разбирает Rust, tray::TrayLabels). Пустой или
 * недостающий ключ — встроенная подпись оболочки на языке lang.
 */
export interface TrayLabels {
  /** Язык интерфейса: на нём встроенные подписи и сообщения самой оболочки. */
  lang: UiLanguage;
  /** Название программы: «Рупор: готов», подсказка «Рупор — запись». */
  app: string;
  status_loading: string;
  status_idle: string;
  status_recording: string;
  status_hands_free: string;
  status_processing: string;
  status_ai: string;
  status_paused: string;
  status_error: string;
  /** Состояния, известные только оболочке (ядро ещё не подключилось или упало). */
  status_starting: string;
  status_restarting: string;
  status_failed: string;
  status_already_running: string;
  /** Подсказка значка, пока идёт перевод встречи, а диктовки нет. */
  status_meeting: string;
  pause: string;
  insert_mode: string;
  mode_auto: string;
  mode_type: string;
  mode_clipboard: string;
  style: string;
  last_insert: string;
  pill_restore: string;
  /** «Перевод встречи» — показать/скрыть окно перевода (выполняет сама оболочка). */
  meeting: string;
  /** …он же, пока перевод встречи идёт: «Перевод встречи (идёт)». */
  meeting_running: string;
  rdp: string;
  rdp_empty: string;
  open: string;
  quit: string;
}

export interface TrayRdpItem {
  key: string;
  title: string;
  included: boolean;
}

/**
 * Вне Tauri (демо ?mock=1, проверки в браузере) трея нет: последнее содержимое меню лежит в
 * window.__ruporTray, а window.__ruporTrayAction(id) имитирует пункт меню (tray-action).
 */
interface TrayDebug {
  __ruporTray?: TrayInfo;
  __ruporTrayAction?: (id: string) => void;
}

export async function updateTray(info: TrayInfo): Promise<void> {
  if (!inTauri()) {
    (window as unknown as TrayDebug).__ruporTray = info;
    return;
  }
  try {
    // Rust разбирает тело вызова целиком (tray::TrayUpdate), ключи — snake_case, как в
    // контракте. Нет ключа ai_line/rdp — пункт не меняется, null — скрывается.
    await invoke("update_tray", { ...info });
  } catch (e) {
    console.warn("update_tray", e);
  }
}

export async function relaunchAdmin(): Promise<void> {
  if (!inTauri()) return;
  await invoke("relaunch_admin");
}

export async function showMain(route?: string): Promise<void> {
  if (!inTauri()) return;
  try {
    await invoke("show_main", route ? { route } : {});
  } catch (e) {
    console.warn("show_main", e);
  }
}

/** Вернуть пилюлю вниз по центру основного монитора (Rust). */
export async function resetPillPosition(): Promise<void> {
  if (!inTauri()) return;
  await invoke("reset_pill_position");
}

/** Пилюлю — наверх слоя «поверх всех» (её могло закрыть, например, полноэкранное RDP). */
export async function pillRaise(): Promise<void> {
  if (!inTauri()) return;
  try {
    await invoke("pill_raise");
  } catch (e) {
    console.warn("pill_raise", e);
  }
}

/** Ошибка страницы пилюли — в журнал оболочки (ui.log), чтобы пропажа не была молчаливой. */
export async function pillLog(level: "error" | "warn" | "info", message: string): Promise<void> {
  if (!inTauri()) {
    console.warn("pill", level, message);
    return;
  }
  try {
    await invoke("pill_log", { level, message });
  } catch (e) {
    console.warn("pill_log", e);
  }
}

// ——— Окно перевода встречи (meeting.html) ———

/**
 * Вне Tauri (демо ?mock=1, проверка в браузере) окна оболочки нет: окно перевода открывается
 * вкладкой браузера с теми же параметрами адреса (mock, lang, port/token).
 */
function openMeetingTab(): void {
  window.open(`meeting.html${window.location.search}`, "rupor-meeting", "popup,width=440,height=820");
}

/** Показать окно перевода встречи (при первом открытии оболочка его создаёт), без фокуса. */
export async function meetingWindowShow(): Promise<void> {
  if (!inTauri()) {
    openMeetingTab();
    return;
  }
  await invoke("meeting_window_show");
}

/** Скрыть окно перевода (сеанс идёт дальше). Вне Tauri — закрыть вкладку окна. */
export async function meetingWindowHide(): Promise<void> {
  if (!inTauri()) {
    window.close();
    return;
  }
  await invoke("meeting_window_hide");
}

/** Свернуть окно перевода в компактную полоску поверх всех (вне Tauri — ничего: демо &mini=1). */
export async function meetingWindowMinimize(): Promise<void> {
  if (!inTauri()) return;
  await invoke("meeting_window_minimize");
}

/** Полоску (или скрытое окно) — снова панелью на её месте. */
export async function meetingWindowRestore(): Promise<void> {
  if (!inTauri()) return;
  await invoke("meeting_window_restore");
}

/** Показать или скрыть окно перевода; true — теперь видно. */
export async function meetingWindowToggle(): Promise<boolean> {
  if (!inTauri()) {
    openMeetingTab();
    return true;
  }
  return invoke<boolean>("meeting_window_toggle");
}

/** Окно перевода — снова к правому краю экрана (meeting_rect: null сохраняет вызывающий). */
export async function meetingWindowReset(): Promise<void> {
  if (!inTauri()) return;
  await invoke("meeting_window_reset");
}

/** Ошибка страницы окна перевода — в журнал оболочки (ui.log), как pillLog. */
export async function meetingWindowLog(level: "error" | "warn" | "info", message: string): Promise<void> {
  if (!inTauri()) {
    console.warn("meeting", level, message);
    return;
  }
  try {
    await invoke("meeting_window_log", { level, message });
  } catch (e) {
    console.warn("meeting_window_log", e);
  }
}

/**
 * Вид окна перевода: full — панель; mini — компактная полоска («Свернуть»); suggest — карточка «Обнаружена новая встреча» в правом
 * верхнем углу (окно было скрыто, когда ядро предложило встречу). suggestion — предложение,
 * пока на него не ответили (в панели — полоской над лентой); hide_in_ms — через сколько оно
 * пропадёт само.
 */
export interface MeetingWindowMode {
  mode: "full" | "suggest" | "mini";
  suggestion: MeetingSuggest | null;
  hide_in_ms: number | null;
}

/** Вне Tauri оболочки нет: окно — всегда панель без предложения (демо задаёт вид само). */
const FULL_MODE: MeetingWindowMode = { mode: "full", suggestion: null, hide_in_ms: null };

/** Что сейчас показывает окно перевода (спросить при загрузке страницы окна). */
export async function meetingWindowMode(): Promise<MeetingWindowMode> {
  if (!inTauri()) return FULL_MODE;
  return invoke<MeetingWindowMode>("meeting_window_mode");
}

/** Оболочка сменила вид окна перевода или предложение. */
export function onMeetingWindowMode(fn: (m: MeetingWindowMode) => void): () => void {
  return onTauriEvent<MeetingWindowMode>("meeting-window-mode", (m) => {
    if (m && (m.mode === "full" || m.mode === "suggest" || m.mode === "mini")) fn(m);
  });
}

/**
 * Ядро предложило встречу (событие meeting_suggest) — показать предложение: скрытое окно
 * перевода становится карточкой в правом верхнем углу, видимая панель — полоской. Зовёт
 * главное окно (его клиент ядра живёт всегда). Вне Tauri — ничего (демо: meeting.html?mock=1&suggest=1).
 */
export async function meetingSuggestShow(suggestion: MeetingSuggest): Promise<void> {
  if (!inTauri()) return;
  await invoke("meeting_suggest_show", { suggestion });
}

/** На предложение ответили или его закрыли: карточка скрывается, полоска пропадает. */
export async function meetingSuggestDone(id: string): Promise<void> {
  if (!inTauri()) return;
  try {
    await invoke("meeting_suggest_done", { id });
  } catch (e) {
    console.warn("meeting_suggest_done", e);
  }
}

/**
 * Пользователь передвинул окно перевода или поменял его размер (оболочка: WM_EXITSIZEMOVE) —
 * новый прямоугольник [x, y, w, h] в физических пикселях, его сохраняют в meeting_rect.
 */
export function onMeetingWindowRect(fn: (rect: [number, number, number, number]) => void): () => void {
  return onTauriEvent<[number, number, number, number]>("meeting-window-rect", (r) => {
    if (Array.isArray(r) && r.length === 4 && r.every((v) => Number.isFinite(v))) fn(r);
  });
}

/** Пользователь передвинул полоску окна перевода: [x, y] видимой части (meeting_mini_pos). */
export function onMeetingMiniPos(fn: (pos: [number, number]) => void): () => void {
  return onTauriEvent<[number, number]>("meeting-window-mini-pos", (p) => {
    if (Array.isArray(p) && p.length === 2 && p.every((v) => Number.isFinite(v))) fn(p);
  });
}

export async function quitApp(): Promise<void> {
  if (!inTauri()) {
    window.close();
    return;
  }
  await invoke("quit_app");
}

/** Подписка на событие Tauri с синхронной отпиской (listen асинхронный). */
export function onTauriEvent<T>(name: string, fn: (payload: T) => void): () => void {
  if (!inTauri()) return () => {};
  let disposed = false;
  let unlisten: (() => void) | null = null;
  listen<T>(name, (ev) => fn(ev.payload))
    .then((u) => {
      if (disposed) u();
      else unlisten = u;
    })
    .catch((e: unknown) => console.warn("listen", name, e));
  return () => {
    disposed = true;
    if (unlisten) unlisten();
  };
}

/**
 * Видно ли окно пользователю. Закрытие главного окна в Tauri — это window.hide() (в трей),
 * а WebView2 при этом остаётся «видимым»: document.visibilityState не меняется и
 * visibilitychange не приходит. Поэтому в Tauri раз в periodMs спрашиваем у окна
 * isVisible / isMinimized. Вне Tauri хватает visibilitychange.
 * fn вызывается при каждом изменении (первый раз — сразу после проверки).
 */
export function watchWindowShown(fn: (shown: boolean) => void, periodMs = 1000): () => void {
  const docShown = () => document.visibilityState === "visible";
  if (!inTauri()) {
    const sync = () => fn(docShown());
    sync();
    document.addEventListener("visibilitychange", sync);
    return () => document.removeEventListener("visibilitychange", sync);
  }
  let stopped = false;
  let last: boolean | null = null;
  let timer: ReturnType<typeof setTimeout> | null = null;
  const emit = (v: boolean) => {
    if (stopped || v === last) return;
    last = v;
    fn(v);
  };
  const tick = async () => {
    let shown = docShown();
    if (shown) {
      try {
        const { getCurrentWindow } = await import("@tauri-apps/api/window");
        const w = getCurrentWindow();
        shown = (await w.isVisible()) && !(await w.isMinimized());
      } catch {
        // нет прав на API окна — остаётся только document.visibilityState
      }
    }
    if (stopped) return;
    emit(shown);
    timer = setTimeout(() => void tick(), periodMs);
  };
  const onVis = () => {
    if (!docShown()) emit(false);
  };
  document.addEventListener("visibilitychange", onVis);
  void tick();
  return () => {
    stopped = true;
    if (timer) clearTimeout(timer);
    document.removeEventListener("visibilitychange", onVis);
  };
}

export function onTrayAction(fn: (id: string) => void): () => void {
  if (!inTauri()) {
    const w = window as unknown as TrayDebug;
    w.__ruporTrayAction = fn;
    return () => {
      if (w.__ruporTrayAction === fn) delete w.__ruporTrayAction;
    };
  }
  return onTauriEvent<{ id: string } | string>("tray-action", (p) => {
    fn(typeof p === "string" ? p : p.id);
  });
}

export function onCoreRestarted(fn: () => void): () => void {
  return onTauriEvent<unknown>("core-restarted", () => fn());
}

export function onNavigate(fn: (route: string) => void): () => void {
  return onTauriEvent<{ route: string } | string>("navigate", (p) => {
    fn(typeof p === "string" ? p : p.route);
  });
}

/** Автозапуск (plugin-autostart). Вне Tauri — null (недоступно). */
export async function autostartEnabled(): Promise<boolean | null> {
  if (!inTauri()) return null;
  try {
    const m = await import("@tauri-apps/plugin-autostart");
    return await m.isEnabled();
  } catch (e) {
    console.warn("autostart", e);
    return null;
  }
}

export async function setAutostart(on: boolean): Promise<boolean> {
  if (!inTauri()) return false;
  const m = await import("@tauri-apps/plugin-autostart");
  if (on) await m.enable();
  else await m.disable();
  // Выбор запоминаем: при старте оболочка восстанавливает автозапуск, только если он включён
  const { call } = await import("./ws");
  await call("set_settings", { patch: { autostart: on } }).catch(() => undefined);
  return m.isEnabled();
}

/**
 * Заголовок окна («Рупор» / "Rupor") — на языке интерфейса. Вне Tauri — только
 * document.title. Нет разрешения на setTitle (старая оболочка) — тихо ничего.
 */
export async function setWindowTitle(title: string): Promise<void> {
  if (typeof document !== "undefined") document.title = title;
  if (!inTauri()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().setTitle(title);
  } catch {
    // заголовок не главное: окно пилюли без рамки, а у main он уже стоит из tauri.conf.json
  }
}

/**
 * Выбрать папку системным диалогом (plugin-dialog, только окно main). null — отменили, или
 * не Tauri (демо ?mock=1, браузер): там путь вводят в поле.
 */
export async function pickFolder(defaultPath?: string, title?: string): Promise<string | null> {
  if (!inTauri()) return null;
  const { open } = await import("@tauri-apps/plugin-dialog");
  const picked = await open({
    directory: true,
    multiple: false,
    defaultPath: defaultPath || undefined,
    title,
  });
  return typeof picked === "string" && picked ? picked : null;
}

/** Тема окна (влияет на оттенок Mica). null — как в системе. */
export async function setWindowTheme(theme: "light" | "dark" | null): Promise<void> {
  if (!inTauri()) return;
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().setTheme(theme);
  } catch (e) {
    console.warn("setTheme", e);
  }
}
