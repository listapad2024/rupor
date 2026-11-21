// Модели заметок к встрече: модель по умолчанию у провайдера (как ai.notes_model ядра), готовые
// модели для «Настройки → Встречи» и примерная цена часа встречи. Без зависимостей (кроме
// типов): берут и интерфейс, и демо-ядро (mock.ts).
import type { AiProvider, Settings } from "./types";

/** Модель заметок по умолчанию (meeting_notes_model = ""), как ai.NOTES_MODELS ядра. */
export const NOTES_DEFAULT_MODEL: Partial<Record<AiProvider, string>> = {
  openrouter: "anthropic/claude-sonnet-5.5",
  anthropic: "claude-sonnet-5-5",
};

/** Готовая модель заметок: id у провайдера, подпись (settings.meeting.notesPresets.*), цена $ за 1M токенов. */
export interface NotesPreset {
  id: string;
  label: "sonnet" | "opus" | "haiku" | "deepseek" | "gemini";
  price: [number, number];
}

// Цены — каталог OpenRouter 20.10.2026 (вход / выход, $ за 1M токенов); у Anthropic напрямую — те же.
export const NOTES_PRESETS: Partial<Record<AiProvider, NotesPreset[]>> = {
  openrouter: [
    { id: "anthropic/claude-sonnet-5.5", label: "sonnet", price: [2, 10] },
    { id: "anthropic/claude-opus-5.5", label: "opus", price: [4, 20] },
    { id: "anthropic/claude-haiku-5.5", label: "haiku", price: [0.1, 0.5] },
    { id: "deepseek/deepseek-v4-pro", label: "deepseek", price: [0.96, 1.91] },
    // Gemini 2.5 Pro в каталоге — до 20.10.2026 (expiration_date), поэтому 3.1 Pro
    { id: "google/gemini-3.1-pro-preview", label: "gemini", price: [2, 12] },
  ],
  anthropic: [
    { id: "claude-sonnet-5-5", label: "sonnet", price: [2, 10] },
    { id: "claude-opus-5-5", label: "opus", price: [4, 20] },
    { id: "claude-haiku-5-5", label: "haiku", price: [0.1, 0.5] },
  ],
};

/**
 * Токенов на час встречи (для оценки цены): расшифровка — около 9 тыс. слов (~18 тыс. токенов)
 * с промптом, ответ — заметки на пару страниц.
 */
export const NOTES_TOKENS_PER_HOUR = { input: 20_000, output: 2_500 };

/** Примерная цена заметок к часу встречи, $. */
export function notesCostPerHour(price: readonly [number, number]): number {
  return (NOTES_TOKENS_PER_HOUR.input * price[0] + NOTES_TOKENS_PER_HOUR.output * price[1]) / 1_000_000;
}

/**
 * Провайдер встречи (перевод, название, заметки), как ai.meeting_provider ядра: «по списку
 * ключей» — провайдер первого включённого ключа, без ключей — OpenRouter.
 */
export function meetingProvider(s: Pick<Settings, "ai_provider" | "ai_keys">): AiProvider {
  if (s.ai_provider !== "keys") return s.ai_provider;
  return s.ai_keys.find((k) => k.enabled)?.provider ?? "openrouter";
}

/** Модель, которой пишутся заметки при этих настройках (как ai.notes_model ядра). */
export function notesModelId(
  s: Pick<Settings, "ai_provider" | "ai_keys" | "meeting_notes_model" | "ollama_model" | "ai_model">,
): string {
  const own = s.meeting_notes_model.trim();
  if (own) return own;
  const provider = meetingProvider(s);
  if (provider === "ollama") return s.ollama_model;
  return NOTES_DEFAULT_MODEL[provider] ?? s.ai_model;
}
