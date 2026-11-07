// Расход ИИ и баланс ключа OpenRouter — один источник на окно: карточка на Главной,
// «Настройки → ИИ → Расход» и строка трея читают одно и то же, запросы не дублируются.
//
// - ai_usage_stats — локальная SQLite ядра, перечитывается после каждого события ai_usage
//   (сразу подставляем сегодняшнюю сумму из события, остальное — через ~0,4 с), при открытии
//   Главной и «Расхода» и после смены даты (иначе в трее до первого вызова за новый день —
//   вчерашнее «сегодня»);
// - ai_balance — только если OpenRouter действительно используется (balanceMode): ядро само
//   кэширует ответ на 5 мин и обновляет его в фоне после платного вызова; окно спрашивает
//   раз в 15 мин и через 3 с после платного вызова. force — кнопка «Обновить».
import { useSyncExternalStore } from "react";
import { t } from "../i18n";
import { fmtSpend, fmtUsd } from "./format";
import { errorText } from "./hub";
import type { AiBalance, AiKeyEntry, AiUsageEvent, AiUsageStats, AiUsageTotals, Settings } from "./types";
import { backend } from "./ws";

export interface AiUsageSnapshot {
  stats: AiUsageStats | null;
  statsError: string | null;
  balance: AiBalance | null;
  balanceLoading: boolean;
  /** Последнее событие ai_usage (живое обновление, подсветка сумм). */
  lastEvent: AiUsageEvent | null;
}

/**
 * Как окно ходит за балансом OpenRouter:
 * - poll — очистка идёт через OpenRouter: при подключении, раз в 15 мин и после платного вызова;
 * - on_use — через OpenRouter только командный режим: без опроса, только после платного вызова
 *   (ответ — из кэша ядра, которое уже обновило баланс после вызова);
 * - off — OpenRouter не используется: в OpenRouter не ходим, баланс не показываем.
 */
export type BalanceMode = "poll" | "on_use" | "off";

// Ядро обновляет баланс само после каждого платного вызова OpenRouter (если кэш старше 5 мин),
// так что опрос нужен только для пополнений и трат с того же ключа в других программах.
const BALANCE_PERIOD_MS = 15 * 60_000;
const STATS_DEBOUNCE_MS = 400;
const BALANCE_AFTER_USAGE_MS = 3_000;
// Смена даты: раз в минуту сравниваем с датой последней загрузки (переживает и сон компьютера)
const DAY_CHECK_MS = 60_000;

let snap: AiUsageSnapshot = {
  stats: null,
  statsError: null,
  balance: null,
  balanceLoading: false,
  lastEvent: null,
};
const listeners = new Set<() => void>();
let started = false;
let mode: BalanceMode = "off";
let balanceTimer: ReturnType<typeof setInterval> | null = null;
let statsDebounce: ReturnType<typeof setTimeout> | null = null;
let balanceDebounce: ReturnType<typeof setTimeout> | null = null;
let statsSeq = 0;
let balanceSeq = 0;
/** Локальная дата, за которую загружены stats (null — ещё не загружались). */
let statsDay: string | null = null;

function set(patch: Partial<AiUsageSnapshot>): void {
  snap = { ...snap, ...patch };
  for (const fn of [...listeners]) fn();
}

function todayKey(): string {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

/** Событие пришло раньше, чем ядро перечитано: сразу поправить «сегодня» и столбик дня. */
function applyEvent(s: AiUsageStats, e: AiUsageEvent): AiUsageStats {
  const add = <X extends AiUsageTotals>(x: X): X => ({
    ...x,
    calls: x.calls + 1,
    tokens_in: x.tokens_in + e.tokens_in,
    tokens_out: x.tokens_out + e.tokens_out,
  });
  const delta = Math.max(0, e.today_cost_usd - s.today.cost_usd);
  const key = todayKey();
  return {
    ...s,
    today: { ...add(s.today), cost_usd: e.today_cost_usd },
    week: { ...add(s.week), cost_usd: s.week.cost_usd + delta },
    month: { ...add(s.month), cost_usd: s.month.cost_usd + delta },
    total: { ...add(s.total), cost_usd: s.total.cost_usd + delta },
    daily: s.daily.map((d) => (d.date === key ? { ...add(d), cost_usd: d.cost_usd + delta } : d)),
  };
}

function onUsage(e: AiUsageEvent): void {
  // Первое событие нового дня, а stats ещё за вчера: не прибавлять к вчерашнему — перечитать
  const stats = snap.stats && statsDay === todayKey() ? applyEvent(snap.stats, e) : snap.stats;
  set({ lastEvent: e, stats });
  scheduleStats(STATS_DEBOUNCE_MS);
  if (mode !== "off" && e.cost_usd > 0) {
    if (balanceDebounce) clearTimeout(balanceDebounce);
    balanceDebounce = setTimeout(() => {
      balanceDebounce = null;
      if (mode !== "off") void refreshBalance(false);
    }, BALANCE_AFTER_USAGE_MS);
  }
}

function start(): void {
  if (started) return;
  started = true;
  backend.on("ai_usage", onUsage);
  backend.onStatus((s) => {
    if (s !== "open") return;
    reloadAiStats();
    if (mode === "poll") void refreshBalance(false);
  });
  if (backend.status() === "open") reloadAiStats();
  // Рупор неделями живёт в трее: после полуночи «сегодня» обнуляется без вызова ИИ
  setInterval(() => {
    if (statsDay !== null && statsDay !== todayKey() && backend.status() === "open") reloadAiStats();
  }, DAY_CHECK_MS);
}

function scheduleStats(ms: number): void {
  if (statsDebounce) clearTimeout(statsDebounce);
  statsDebounce = setTimeout(() => {
    statsDebounce = null;
    reloadAiStats();
  }, ms);
}

/** Перечитать ai_usage_stats (локально, без сети). */
export function reloadAiStats(): void {
  start();
  const seq = ++statsSeq;
  const day = todayKey();
  backend
    .call("ai_usage_stats")
    .then((stats) => {
      if (seq !== statsSeq) return;
      statsDay = day;
      set({ stats, statsError: null });
    })
    .catch((e: unknown) => {
      if (seq === statsSeq) set({ statsError: errorText(e) });
    });
}

/** Баланс ключа: без force — кэш ядра (5 мин), с force — запрос в OpenRouter сейчас. */
export async function refreshBalance(force: boolean): Promise<void> {
  start();
  const seq = ++balanceSeq;
  set({ balanceLoading: true });
  try {
    const balance = await backend.call("ai_balance", force ? { force: true } : {});
    if (seq === balanceSeq) set({ balance, balanceLoading: false });
  } catch (e) {
    if (seq !== balanceSeq) return;
    // Команда не выполнилась (нет связи с ядром) — прежний баланс оставляем, но с причиной.
    set({
      balanceLoading: false,
      balance: {
        balance_usd: snap.balance?.balance_usd ?? null,
        usage_usd: snap.balance?.usage_usd ?? null,
        limit_usd: snap.balance?.limit_usd ?? null,
        checked_at: snap.balance?.checked_at ?? null,
        error: errorText(e),
      },
    });
  }
}

type KeySettings = Pick<Settings, "ai_enabled" | "ai_provider" | "command_provider" | "ai_keys">;

/** Первый включённый ключ в очереди ai_provider / command_provider (как keys.chain в ядре). */
export function firstKey(s: Pick<Settings, "ai_keys">, provider: string): AiKeyEntry | null {
  if (provider !== "keys" && provider !== "openrouter" && provider !== "anthropic") return null;
  return s.ai_keys.find((k) => k.enabled && (provider === "keys" || k.provider === provider)) ?? null;
}

/** Провайдер и модель, которыми сейчас идёт очистка (для подписи на Главной). */
export function activeModel(s: Settings): { provider: string; model: string } {
  if (s.ai_provider === "ollama") return { provider: "ollama", model: s.ollama_model };
  const p = firstKey(s, s.ai_provider)?.provider ?? (s.ai_provider === "anthropic" ? "anthropic" : "openrouter");
  return { provider: p, model: p === "anthropic" ? s.anthropic_model : s.ai_model };
}

/**
 * Нужен ли баланс ключа, который первым в очереди. Очистка через облачные ключи (ИИ включён) —
 * опрос; через них только командный режим — после платных вызовов; иначе (Ollama, Claude Code,
 * ключей нет) — не нужен: ядро не ходит к провайдеру, которым Рупор не пользуется.
 */
export function balanceMode(s: KeySettings | null | undefined): BalanceMode {
  if (!s) return "off";
  if (s.ai_enabled && firstKey(s, s.ai_provider)) return "poll";
  if (firstKey(s, s.command_provider)) return "on_use";
  return "off";
}

/**
 * Режим баланса (balanceMode). poll — сразу спросить и дальше раз в 15 мин; on_use — ждать
 * платного вызова; off — перестать ходить за балансом и забыть его (он не показывается).
 */
export function setBalanceMode(next: BalanceMode): void {
  start();
  if (next === mode) return;
  mode = next;
  if (balanceTimer) clearInterval(balanceTimer);
  balanceTimer = null;
  if (next === "off") {
    if (balanceDebounce) clearTimeout(balanceDebounce);
    balanceDebounce = null;
    balanceSeq++; // ответ запроса «в полёте» больше не нужен
    set({ balance: null, balanceLoading: false });
    return;
  }
  if (next !== "poll") return;
  if (backend.status() === "open") void refreshBalance(false);
  balanceTimer = setInterval(() => {
    if (backend.status() === "open") void refreshBalance(false);
  }, BALANCE_PERIOD_MS);
}

function subscribe(fn: () => void): () => void {
  start();
  listeners.add(fn);
  return () => {
    listeners.delete(fn);
  };
}

function getSnapshot(): AiUsageSnapshot {
  return snap;
}

export function useAiUsage(): AiUsageSnapshot {
  return useSyncExternalStore(subscribe, getSnapshot);
}

/** Баланс известен и ключ в порядке. */
export function balanceValue(b: AiBalance | null): number | null {
  return b && !b.error && b.balance_usd !== null ? b.balance_usd : null;
}

/**
 * Строка трея: «ИИ: сегодня $0.03 · баланс $12.34». Без баланса (не известен или OpenRouter
 * не используется — withBalance=false) — только «ИИ: сегодня $0.03».
 */
export function aiTrayLine(s: AiUsageSnapshot, withBalance: boolean): string | null {
  if (!s.stats) return null;
  const today = fmtSpend(s.stats.today.cost_usd);
  const b = withBalance ? balanceValue(s.balance) : null;
  return b !== null ? t("tray.aiLine", { today, balance: fmtUsd(b) }) : t("tray.aiLineToday", { today });
}

/**
 * Средняя цена запроса — avg_cost_per_call ядра: по удачным вызовам (таймауты и ошибки стоят
 * $0 и среднюю не занижают). by_kind для этого не годится: в нём и неудачные вызовы.
 * cleanupOnly — командного режима не было, значит это средняя цена очистки.
 */
export function avgCost(s: AiUsageStats): { value: number; cleanupOnly: boolean } {
  const commands = s.by_kind.find((k) => k.kind === "command")?.calls ?? 0;
  return { value: s.avg_cost_per_call, cleanupOnly: commands === 0 };
}
