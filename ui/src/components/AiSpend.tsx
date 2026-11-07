// Расход ИИ: карточка на Главной (сегодня / месяц / баланс + мини-график) и блок
// «Настройки → ИИ → Расход» (итоги, график по дням, разбивка по типам и моделям, баланс).
// Данные — lib/aiUsage (живое обновление по событию ai_usage).
import { AnimatePresence, motion } from "framer-motion";
import { ArrowRight, BarChart3, CircleAlert, RefreshCw, Sparkles, Wallet } from "lucide-react";
import { useEffect, useState, type ReactNode } from "react";
import { t, tp } from "../i18n";
import { activeModel, avgCost, balanceMode, balanceValue, firstKey, refreshBalance, reloadAiStats, useAiUsage } from "../lib/aiUsage";
import { cn } from "../lib/cn";
import { fmtAgo, fmtSpend, fmtTokens, fmtUsd } from "../lib/format";
import { useNow } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { navigate } from "../lib/router";
import { useCore } from "../lib/store";
import type { AiBalance, AiUsageDay, AiUsageKind, AiUsageTotals } from "../lib/types";
import { call } from "../lib/ws";
import { CountUp } from "./CountUp";
import { DayBars } from "./DayBars";
import { Button, IconButton } from "./ui/controls";
import { Card, EmptyState, LoadError, SectionTitle, Skeleton } from "./ui/display";
import { useToast } from "./ui/toast";

const costOf = (d: AiUsageDay) => d.cost_usd;
/** Как в предупреждении ядра: меньше $1 — пора пополнить. */
const LOW_BALANCE = 1;

function DayTip({ d }: { d: AiUsageDay }) {
  return (
    <span className="flex flex-col">
      <span>{fmtSpend(d.cost_usd)}</span>
      <span className="text-[11.5px] font-normal text-muted">
        {d.calls > 0
          ? t("aiUsage.dayTip", { calls: tp("units.calls", d.calls), tokens: fmtTokens(d.tokens_in + d.tokens_out) })
          : t("aiUsage.dayTipEmpty")}
      </span>
    </span>
  );
}
const dayTip = (d: AiUsageDay) => <DayTip d={d} />;

/** Кнопка «Обновить баланс» (force: запрос в OpenRouter сейчас, мимо 5-минутного кэша ядра). */
function RefreshBalance({ size = "sm" }: { size?: "sm" | "md" }) {
  const { balanceLoading } = useAiUsage();
  return (
    <IconButton label={t("aiUsage.refresh")} size={size} disabled={balanceLoading} onClick={() => void refreshBalance(true)}>
      <RefreshCw className={cn(size === "sm" ? "size-3.5" : "size-4", balanceLoading && "animate-spin")} />
    </IconButton>
  );
}

function balanceFoot(b: AiBalance | null, now: number): { text: string; error: boolean } {
  if (!b) return { text: t("aiUsage.balance.notChecked"), error: false };
  if (b.error) return { text: b.error, error: true };
  if (b.checked_at === null) return { text: t("aiUsage.balance.notChecked"), error: false };
  return { text: t("aiUsage.balance.checked", { ago: fmtAgo(b.checked_at, now) }), error: false };
}

// ——— Главная ———

export function AiHomeCard() {
  const { settings } = useCore();
  const ai = useAiUsage();
  const now = useNow(30_000);
  // «Сегодня» и 30 дней — свежие при каждом открытии Главной (дата могла смениться)
  useEffect(() => reloadAiStats(), []);
  if (!settings) return null;
  if (!settings.ai_enabled) return <AiOffCard />;

  const s = ai.stats;
  // Баланс первого ключа в очереди — когда очистка идёт через облачные ключи; если через них
  // только команды — когда он уже известен (после платной команды). Ollama — без баланса.
  // Anthropic баланс по API не отдаёт: показываем, только если введён остаток (оценка).
  const bm = balanceMode(settings);
  const bal = balanceValue(ai.balance);
  const showBalance = (bm === "poll" || (bm === "on_use" && ai.balance !== null)) && !(ai.balance?.provider === "anthropic" && bal === null);
  const foot = balanceFoot(ai.balance, now);
  const { provider: activeProvider, model } = activeModel(settings);

  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25, delay: 0.05 }}>
      <Card padded={false} className="mt-3" data-testid="ai-home-card">
        <div className="grid grid-cols-[minmax(0,1fr)_auto_minmax(150px,0.8fr)] items-center gap-x-6 px-5 py-4">
          <div className="flex min-w-0 items-center gap-3">
            <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent-text">
              <Sparkles className="size-[18px]" />
            </span>
            <div className="min-w-0">
              <div className="text-[14px] font-semibold text-fg">{t("aiUsage.home.title")}</div>
              <div className="truncate text-[12px] text-muted" title={model}>
                {t(`settings.ai.providers.${activeProvider as "openrouter" | "anthropic" | "ollama"}`)} ·{" "}
                <span className="font-mono text-[11.5px]">{model.split("/").pop() || model}</span>
              </div>
              <button
                type="button"
                onClick={() => navigate("settings/ai")}
                title={t("aiUsage.home.details")}
                className="mt-0.5 inline-flex max-w-full items-center gap-1 whitespace-nowrap text-[12px] font-medium text-muted transition-colors hover:text-accent-text"
              >
                <span className="truncate">{t("aiUsage.home.details")}</span>
                <ArrowRight className="size-3 shrink-0" />
              </button>
            </div>
          </div>

          <div className="flex items-stretch gap-6">
            <Metric
              label={t("aiUsage.periods.today")}
              loading={!s}
              value={s?.today.cost_usd ?? 0}
              foot={s ? tp("units.calls", s.today.calls) : ""}
            />
            <Metric
              label={t("aiUsage.periods.month")}
              loading={!s}
              value={s?.month.cost_usd ?? 0}
              foot={s ? tp("units.calls", s.month.calls) : ""}
            />
            {showBalance && (
              <div className="min-w-[96px]">
                <div className="flex items-center gap-1 text-[12px] font-medium text-muted">
                  <Wallet className="size-3.5 text-subtle" />
                  {t("aiUsage.home.balance")}
                  <RefreshBalance />
                </div>
                <div
                  className={cn(
                    "-mt-0.5 text-[22px] font-semibold leading-tight tracking-[-0.02em] tabular",
                    bal !== null && bal < LOW_BALANCE ? "text-warn" : "text-fg",
                  )}
                >
                  {ai.balance === null && ai.balanceLoading ? (
                    <Skeleton className="mt-1 h-6 w-16" />
                  ) : bal !== null ? (
                    fmtUsd(bal)
                  ) : (
                    "—"
                  )}
                </div>
                <div
                  className={cn("mt-0.5 max-w-[150px] truncate text-[11.5px]", foot.error ? "text-danger" : "text-subtle")}
                  title={foot.text}
                >
                  {foot.text}
                </div>
              </div>
            )}
          </div>

          <div className="min-w-0">
            {s ? (
              <DayBars compact data={s.daily} value={costOf} tooltip={dayTip} height={50} emptyMax={0.01} />
            ) : (
              <Skeleton className="h-[50px] w-full" />
            )}
            <div className="mt-1.5 flex justify-between text-[11px] text-subtle">
              <span>{t("aiUsage.home.chart")}</span>
              {s && <span className="tabular">{fmtSpend(s.daily.reduce((a, d) => a + d.cost_usd, 0))}</span>}
            </div>
          </div>
        </div>
      </Card>
    </motion.div>
  );
}

function Metric({ label, value, foot, loading }: { label: string; value: number; foot: string; loading: boolean }) {
  return (
    <div className="min-w-[84px]">
      <div className="text-[12px] font-medium text-muted">{label}</div>
      <div className="mt-0.5 text-[22px] font-semibold leading-tight tracking-[-0.02em] text-fg">
        {loading ? <Skeleton className="mt-1 h-6 w-14" /> : <CountUp value={value} format={fmtSpend} />}
      </div>
      <div className="mt-0.5 truncate text-[11.5px] text-subtle">{foot}</div>
    </div>
  );
}

/** ИИ выключен: что он даёт и кнопка «Включить» (без ключа — в настройки ИИ). */
function AiOffCard() {
  const { settings, updateSettings } = useCore();
  const toast = useToast();
  const [busy, setBusy] = useState(false);

  const enable = async () => {
    if (!settings) return;
    setBusy(true);
    try {
      const provider = settings.ai_provider;
      if (provider !== "ollama") {
        const keys = firstKey(settings, provider) ? await call("ai_keys_list") : [];
        const usable = keys.some((k) => k.enabled && k.has_key && (provider === "keys" || k.provider === provider));
        if (!usable) {
          navigate("settings/ai");
          toast({ tone: "info", title: t("aiUsage.home.needKey") });
          return;
        }
      }
      if (await updateSettings({ ai_enabled: true })) toast({ tone: "success", title: t("aiUsage.home.enabled") });
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <motion.div initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.25, delay: 0.05 }}>
      <Card padded={false} className="mt-3 overflow-hidden" data-testid="ai-home-off">
        <div className="flex items-center gap-4 bg-gradient-to-r from-accent/[0.07] to-transparent px-5 py-4">
          <span className="flex size-10 shrink-0 items-center justify-center rounded-xl bg-surface-2 text-muted">
            <Sparkles className="size-5" />
          </span>
          <div className="min-w-0 flex-1">
            <div className="text-[14px] font-semibold text-fg">{t("aiUsage.home.offTitle")}</div>
            <div className="mt-0.5 max-w-2xl text-[12.5px] leading-relaxed text-muted">{t("aiUsage.home.offText")}</div>
          </div>
          <Button variant="ghost" size="sm" onClick={() => navigate("settings/ai")}>
            {t("aiUsage.home.configure")}
          </Button>
          <Button variant="primary" size="sm" loading={busy} icon={<Sparkles className="size-4" />} onClick={() => void enable()}>
            {t("aiUsage.home.enable")}
          </Button>
        </div>
      </Card>
    </motion.div>
  );
}

// ——— Настройки → ИИ → Расход ———

const PERIODS = ["today", "week", "month", "total"] as const;
const KINDS: AiUsageKind[] = ["cleanup", "command", "test"];

export function AiSpendSection() {
  const { settings } = useCore();
  const ai = useAiUsage();
  const now = useNow(30_000);
  useEffect(() => reloadAiStats(), []);
  const s = ai.stats;
  // Баланс OpenRouter — только если Рупор ходит в OpenRouter (очисткой или командами)
  const showBalance = balanceMode(settings) !== "off";
  const avg = s ? avgCost(s) : null;

  return (
    <section data-testid="ai-spend">
      <SectionTitle hint={t("aiUsage.hint")}>{t("aiUsage.title")}</SectionTitle>
      <div className="space-y-3">
        {showBalance && <BalanceCard balance={ai.balance} loading={ai.balanceLoading} now={now} />}

        {!s && ai.statsError ? (
          <Card>
            <LoadError compact error={ai.statsError} onRetry={reloadAiStats} />
          </Card>
        ) : (
          <>
            <Card padded={false} className="grid grid-cols-4 overflow-hidden">
              {PERIODS.map((p, i) => (
                <PeriodTile key={p} label={t(`aiUsage.periods.${p}`)} v={s ? s[p] : null} first={i === 0} />
              ))}
            </Card>

            <Card className="pb-3">
              <div className="mb-4 flex flex-wrap items-start justify-between gap-4">
                <div>
                  <div className="flex items-center gap-2 text-[14px] font-semibold text-fg">
                    <BarChart3 className="size-4 text-subtle" />
                    {t("aiUsage.chart.title")}
                  </div>
                  {s && (
                    <div className="mt-0.5 text-[12.5px] text-muted">
                      {t("aiUsage.chart.summary", {
                        total: fmtSpend(s.daily.reduce((a, d) => a + d.cost_usd, 0)),
                        calls: tp("units.calls", s.daily.reduce((a, d) => a + d.calls, 0)),
                      })}
                    </div>
                  )}
                </div>
                {s && avg && s.total.calls > 0 && (
                  <div className="text-right" title={t("aiUsage.avgHint")}>
                    <div className="text-[12px] text-muted">{avg.cleanupOnly ? t("aiUsage.avgCleanup") : t("aiUsage.avgCall")}</div>
                    <div className="text-[15px] font-semibold text-fg tabular">{fmtSpend(avg.value)}</div>
                  </div>
                )}
              </div>
              {!s ? (
                <Skeleton className="h-[188px] w-full" />
              ) : s.daily.some((d) => d.calls > 0) ? (
                <DayBars data={s.daily} value={costOf} axisFormat={fmtSpend} tooltip={dayTip} padLeft={50} emptyMax={0.01} />
              ) : (
                <EmptyState compact icon={<BarChart3 />} title={t("aiUsage.empty.title")} text={t("aiUsage.empty.text")} />
              )}
            </Card>

            {s && s.total.calls > 0 && (
              <div className="grid grid-cols-2 gap-3">
                <Breakdown
                  title={t("aiUsage.byKind")}
                  rows={KINDS.map((k) => {
                    const r = s.by_kind.find((x) => x.kind === k);
                    return { id: k, label: t(`aiUsage.kinds.${k}`), calls: r?.calls ?? 0, cost: r?.cost_usd ?? 0 };
                  })}
                />
                <Breakdown
                  title={t("aiUsage.byModel")}
                  mono
                  rows={[...s.by_model]
                    .sort((a, b) => b.cost_usd - a.cost_usd || b.calls - a.calls)
                    .map((m) => ({ id: m.model, label: m.model || t("aiUsage.unknownModel"), calls: m.calls, cost: m.cost_usd }))}
                />
              </div>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function PeriodTile({
  label,
  v,
  first,
}: {
  label: string;
  v: AiUsageTotals | null;
  first: boolean;
}) {
  return (
    <div className={cn("px-5 py-4", !first && "border-l border-line")}>
      <div className="text-[12.5px] font-medium text-muted">{label}</div>
      <div className="mt-1 text-[24px] font-semibold leading-tight tracking-[-0.02em] text-fg">
        {v ? <CountUp value={v.cost_usd} format={fmtSpend} /> : <Skeleton className="mt-1 h-7 w-16" />}
      </div>
      <div className="mt-1.5 space-y-0.5 text-[12px] text-subtle">
        {v ? (
          <>
            <div className="tabular">{tp("units.calls", v.calls)}</div>
            <div className="tabular" title={t("aiUsage.tokensTitle")}>
              {t("aiUsage.tokens", { in: fmtTokens(v.tokens_in), out: fmtTokens(v.tokens_out) })}
            </div>
          </>
        ) : (
          <Skeleton className="h-3.5 w-20" />
        )}
      </div>
    </div>
  );
}

function Breakdown({
  title,
  rows,
  mono,
}: {
  title: string;
  rows: { id: string; label: string; calls: number; cost: number }[];
  mono?: boolean;
}) {
  const total = rows.reduce((a, r) => a + r.cost, 0);
  const totalCalls = rows.reduce((a, r) => a + r.calls, 0);
  return (
    <Card padded={false}>
      <div className="flex items-baseline justify-between gap-3 border-b border-line px-5 py-3">
        <span className="text-[13.5px] font-semibold text-fg">{title}</span>
        <span className="text-[11.5px] text-subtle">{t("aiUsage.allTime")}</span>
      </div>
      {rows.length === 0 ? (
        <div className="px-5 py-4 text-[12.5px] text-subtle">{t("aiUsage.empty.title")}</div>
      ) : (
        <ul>
          {rows.map((r) => {
            const share = total > 0 ? r.cost / total : totalCalls > 0 ? r.calls / totalCalls : 0;
            return (
              <li key={r.id} className="border-b border-line px-5 py-2.5 last:border-b-0">
                <div className="flex items-baseline justify-between gap-3">
                  <span className={cn("min-w-0 truncate text-[13px] text-fg", mono && "font-mono text-[12px]")} title={r.label}>
                    {r.label}
                  </span>
                  <span className="shrink-0 text-[13px] font-semibold text-fg tabular">{fmtSpend(r.cost)}</span>
                </div>
                <div className="mt-1.5 flex items-center gap-3">
                  <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-surface-3">
                    <motion.div
                      className="h-full rounded-full bg-accent"
                      initial={{ width: 0 }}
                      animate={{ width: `${Math.round(share * 100)}%` }}
                      transition={{ duration: 0.5, ease: [0.22, 1, 0.36, 1] }}
                    />
                  </div>
                  <span className="w-[86px] shrink-0 text-right text-[11.5px] text-subtle tabular">{tp("units.calls", r.calls)}</span>
                </div>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}

function BalanceCard({ balance, loading, now }: { balance: AiBalance | null; loading: boolean; now: number }) {
  const b = balanceValue(balance);
  const foot = balanceFoot(balance, now);
  const details: ReactNode[] = [];
  if (balance && !balance.error && balance.usage_usd !== null) {
    details.push(t("aiUsage.balance.usage", { v: fmtUsd(balance.usage_usd) }));
  }
  if (balance && !balance.error && balance.checked_at !== null && !balance.estimate) {
    details.push(balance.limit_usd !== null ? t("aiUsage.balance.limit", { v: fmtUsd(balance.limit_usd) }) : t("aiUsage.balance.noLimit"));
  }
  return (
    <Card padded={false}>
      <div className="flex items-center gap-4 px-5 py-4">
        <span className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent-text">
          <Wallet className="size-[18px]" />
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium text-fg">
            {balance?.label ? t("aiUsage.balance.titleKey", { name: balance.label }) : t("aiUsage.balance.title")}
            {balance?.estimate && <span className="ml-1.5 text-[12px] font-normal text-muted">{t("aiUsage.balance.estimate")}</span>}
          </div>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={foot.text}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.14 }}
              className={cn("mt-0.5 flex items-center gap-1.5 text-[12.5px]", foot.error ? "text-danger" : "text-muted")}
            >
              {foot.error && <CircleAlert className="size-3.5 shrink-0" />}
              <span className="min-w-0 truncate" title={foot.text}>
                {[foot.text, ...details].join(" · ")}
              </span>
            </motion.div>
          </AnimatePresence>
        </div>
        <div className={cn("text-[24px] font-semibold tracking-[-0.02em] tabular", b !== null && b < LOW_BALANCE ? "text-warn" : "text-fg")}>
          {balance === null && loading ? <Skeleton className="h-7 w-20" /> : b !== null ? fmtUsd(b) : "—"}
        </div>
        <Button
          size="sm"
          variant="secondary"
          loading={loading}
          icon={<RefreshCw className="size-4" />}
          onClick={() => void refreshBalance(true)}
        >
          {t("common.refresh")}
        </Button>
      </div>
    </Card>
  );
}
