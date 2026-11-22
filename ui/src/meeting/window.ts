// Окно перевода встречи в Tauri: перетаскивание за шапку, сохранение места и размера
// (meeting_rect), скрытие от захвата экрана. Место и показ без фокуса держит Rust
// (meeting.rs); вне Tauri все функции — пустышки.
import { inTauri, onMeetingMiniPos, onMeetingWindowRect } from "../lib/tauri";

type TauriWindowModule = typeof import("@tauri-apps/api/window");

let mod: TauriWindowModule | null = null;
async function win(): Promise<TauriWindowModule | null> {
  if (!inTauri()) return null;
  if (!mod) mod = await import("@tauri-apps/api/window");
  return mod;
}

/** Начать перетаскивание окна (из pointermove после сдвига мыши). */
export async function startMeetingDrag(): Promise<void> {
  const m = await win();
  if (!m) return;
  try {
    await m.getCurrentWindow().startDragging();
  } catch (e) {
    console.warn("startDragging", e);
  }
}

/** meeting_protect: прятать окно от захвата экрана (при создании это уже сделал Rust). */
export async function setMeetingProtected(on: boolean): Promise<void> {
  const m = await win();
  if (!m) return;
  try {
    await m.getCurrentWindow().setContentProtected(on);
  } catch (e) {
    console.warn("setContentProtected", e);
  }
}

/**
 * Пользователь передвинул окно или поменял размер — сохранить прямоугольник (последний за
 * delayMs). Rust присылает его, только если окно правда сдвинулось.
 */
export function watchMeetingRect(save: (rect: [number, number, number, number]) => void, delayMs = 300): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const off = onMeetingWindowRect((rect) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => save(rect), delayMs);
  });
  return () => {
    if (timer) clearTimeout(timer);
    off();
  };
}

/** Полоску передвинули — сохранить её место (последнее за delayMs), как watchMeetingRect. */
export function watchMeetingMiniPos(save: (pos: [number, number]) => void, delayMs = 300): () => void {
  let timer: ReturnType<typeof setTimeout> | null = null;
  const off = onMeetingMiniPos((pos) => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => save(pos), delayMs);
  });
  return () => {
    if (timer) clearTimeout(timer);
    off();
  };
}
