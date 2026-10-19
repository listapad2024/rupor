import { motion } from "framer-motion";
import {
  ArrowRight,
  BarChart3,
  CalendarDays,
  Cpu,
  Flame,
  Gauge,
  Hourglass,
  Keyboard,
  Mic,
  Sigma,
  Sparkles,
  Zap,
} from "lucide-react";
import type { ReactNode } from "react";
import { AiHomeCard } from "../components/AiSpend";
import { AppIcon } from "../components/AppIcon";
import { CountUp } from "../components/CountUp";
import { Card, EmptyState, KeyCombo, Skeleton } from "../components/ui/display";
import { WordsChart } from "../components/WordsChart";
import { t, tp } from "../i18n";
import { cached } from "../lib/cache";
import { cn } from "../lib/cn";
import { appName, fmtDecimal, fmtMinutes, fmtNum, fmtTime } from "../lib/format";
import { useDebounced, useEvent, useLoad } from "../lib/hooks";
import { navigate } from "../lib/router";
import { keyLabel } from "../lib/keys";
import { useCore } from "../lib/store";
import { call } from "../lib/ws";

function greeting(): string {
  const h = new Date().getHours();
  if (h >= 5 && h < 12) return t("home.greeting.morning");
  if (h >= 12 && h < 17) return t("home.greeting.day");
  if (h >= 17 && h < 23) return t("home.greeting.evening");
  return t("home.greeting.night");
}

export function HomePage() {
  const { state } = useCore();
  const stats = useLoad(() => call("stats_get"));
  const recent = useLoad(() => call("history_list", { limit: 5, offset: 0 }));
  const refresh = useDebounced(() => {
    stats.reload();
    recent.reload();
  }, 600);
  useEvent("history", () => refresh.call());

  const s = stats.data;
  const typingMinutes = s ? s.words_today / 40 : 0;
  const hk = state?.hotkeys;

  return (
    <div>
      <div className="mb-7">
        <motion.h1
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.25 }}
          className="text-[26px] font-semibold tracking-[-0.02em] text-fg"
        >
          {greeting()}
        </motion.h1>
        <p className="mt-1 text-[14px] text-muted">
          {!s ? (
            <span className="inline-block h-4 w-72 align-middle skeleton" />
          ) : s.words_today > 0 ? (
            t("home.todaySummary", {
              words: tp("units.words", s.words_today),
              minutes: fmtMinutes(typingMinutes),
            })
          ) : (
            t("home.todayEmpty", { key: hk ? keyLabel(hk.ptt) : "" })
          )}
        </p>
      </div>

      {/* KPI */}
      <div className="grid grid-cols-3 gap-3">
        <Kpi
          loading={!s}
          icon={<Mic />}
          label={t("home.kpi.today")}
          value={s?.words_today ?? 0}
          format={fmtNum}
          foot={s ? tp("units.dictationsTotal", s.dictations_total) : ""}
          delay={0}
        />
        <Kpi
          loading={!s}
          icon={<CalendarDays />}
          label={t("home.kpi.week")}
          value={s?.words_week ?? 0}
          format={fmtNum}
          foot={s ? t("home.kpi.weekFoot", { avg: fmtNum(s.words_week / 7) }) : ""}
          delay={0.03}
        />
        <Kpi
          loading={!s}
          icon={<Sigma />}
          label={t("home.kpi.total")}
          value={s?.words_total ?? 0}
          format={fmtNum}
          foot={s ? t("home.kpi.totalFoot", { pages: fmtNum(s.words_total / 250) }) : ""}
          delay={0.06}
        />
        <Kpi
          loading={!s}
          icon={<Gauge />}
          label={t("home.kpi.wpm")}
          value={s?.wpm ?? 0}
          format={fmtNum}
          suffix={t("home.kpi.wpmUnit")}
          foot={s && s.wpm > 0 ? t("home.kpi.wpmFoot", { x: fmtDecimal(s.wpm / 40) }) : ""}
          delay={0.09}
        />
        <Kpi
          loading={!s}
          icon={<Hourglass />}
          label={t("home.kpi.saved")}
          value={s?.saved_minutes ?? 0}
          format={fmtMinutes}
          foot={t("home.kpi.savedFoot")}
          delay={0.12}
        />
        <Kpi
          loading={!s}
          icon={<Flame />}
          label={t("home.kpi.streak")}
          value={s?.streak_days ?? 0}
          format={(v) => tp("units.days", Math.round(v))}
          foot={s && s.streak_days > 0 ? t("home.kpi.streakFoot") : t("home.kpi.streakZero")}
          highlight={Boolean(s && s.streak_days >= 3)}
          delay={0.15}
        />
      </div>

      {/* График */}
      <Card className="mt-3 pb-3">
        <div className="mb-4 flex items-start justify-between gap-4">
          <div>
            <div className="flex items-center gap-2 text-[14px] font-semibold text-fg">
              <BarChart3 className="size-4 text-subtle" />
              {t("home.chart.title")}
            </div>
            {s && (
              <div className="mt-0.5 text-[12.5px] text-muted">
                {t("home.chart.summary", {
                  total: fmtNum(s.daily.reduce((a, d) => a + d.words, 0)),
                  avg: fmtNum(
                    s.daily.reduce((a, d) => a + d.words, 0) / Math.max(1, s.daily.filter((d) => d.words > 0).length),
                  ),
                })}
              </div>
            )}
          </div>
        </div>
        {s ? (
          s.daily.some((d) => d.words > 0) ? (
            <WordsChart data={s.daily} />
          ) : (
            <EmptyState compact icon={<BarChart3 />} title={t("home.chart.emptyTitle")} text={t("home.chart.emptyText")} />
          )
        ) : (
          <Skeleton className="h-[188px] w-full" />
        )}
      </Card>

      {/* ИИ: расход и баланс */}
      <AiHomeCard />

      <div className="mt-3 grid grid-cols-[1.55fr_1fr] gap-3">
        {/* Последние диктовки */}
        <Card padded={false} className="overflow-hidden">
          <div className="flex items-center justify-between px-5 pb-2 pt-4">
            <div className="text-[14px] font-semibold text-fg">{t("home.recent.title")}</div>
            <button
              type="button"
              onClick={() => navigate("history")}
              className="inline-flex shrink-0 items-center gap-1 whitespace-nowrap text-[12.5px] font-medium text-muted transition-colors hover:text-accent-text"
            >
              {t("home.recent.all")}
              <ArrowRight className="size-3.5" />
            </button>
          </div>
          {!recent.data ? (
            <div className="space-y-3 px-5 pb-5 pt-2">
              {[0, 1, 2].map((i) => (
                <div key={i} className="flex gap-3">
                  <Skeleton className="size-7 shrink-0" />
                  <div className="flex-1 space-y-1.5">
                    <Skeleton className="h-3.5 w-full" />
                    <Skeleton className="h-3.5 w-2/3" />
                  </div>
                </div>
              ))}
            </div>
          ) : recent.data.items.length === 0 ? (
            <EmptyState compact icon={<Mic />} title={t("home.recent.emptyTitle")} text={t("home.recent.emptyText")} />
          ) : (
            <ul className="pb-2">
              {recent.data.items.map((it, i) => (
                <motion.li
                  key={it.id}
                  initial={{ opacity: 0, y: 4 }}
                  animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.2, delay: i * 0.03 }}
                >
                  <button
                    type="button"
                    onClick={() => navigate("history")}
                    className="flex w-full gap-3 px-5 py-2.5 text-left transition-colors hover:bg-surface-2/70 focus-visible:-outline-offset-2!"
                  >
                    <AppIcon process={it.app} size={28} />
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2 text-[12px] text-subtle">
                        <span className="font-medium text-muted">{appName(it.app)}</span>
                        <span>·</span>
                        <span className="tabular">{fmtTime(it.ts)}</span>
                        {it.ai && <Sparkles className="size-3 text-accent-text" />}
                      </div>
                      <div className="mt-0.5 line-clamp-2 text-[13.5px] leading-snug text-fg">{it.text}</div>
                    </div>
                  </button>
                </motion.li>
              ))}
            </ul>
          )}
        </Card>

        <div className="flex flex-col gap-3">
          {/* Хоткеи */}
          <Card>
            <div className="mb-3 flex items-center gap-2 text-[14px] font-semibold text-fg">
              <Keyboard className="size-4 text-subtle" />
              {t("home.hotkeys.title")}
            </div>
            {hk ? (
              <div className="space-y-2.5">
                <HotkeyRow label={t("home.hotkeys.hold")} keys={[hk.ptt]} />
                <HotkeyRow label={t("home.hotkeys.handsFree")} keys={[hk.ptt]} times={2} />
                <HotkeyRow label={t("home.hotkeys.broadcast")} keys={[hk.ptt, hk.broadcast]} />
                <HotkeyRow label={t("home.hotkeys.command")} keys={[hk.ptt, hk.command]} />
                <HotkeyRow label={t("home.hotkeys.cancel")} keys={[hk.cancel]} />
                {hk.insert_last && <HotkeyRow label={t("home.hotkeys.insertLast")} keys={[hk.insert_last]} />}
                {hk.copy_last && <HotkeyRow label={t("home.hotkeys.copyLast")} keys={[hk.copy_last]} />}
              </div>
            ) : (
              <Skeleton className="h-32 w-full" />
            )}
          </Card>
          <SystemCard />
        </div>
      </div>
    </div>
  );
}

function HotkeyRow({ label, keys, times }: { label: string; keys: string[]; times?: number }) {
  return (
    <div className="flex items-center justify-between gap-3">
      <span className="min-w-0 truncate text-[13px] text-muted">{label}</span>
      <span className="flex shrink-0 items-center gap-1.5">
        <KeyCombo keys={keys} wrap={false} />
        {times && <span className="text-[12px] font-medium text-subtle">×{times}</span>}
      </span>
    </div>
  );
}

function Kpi({
  icon,
  label,
  value,
  format,
  foot,
  suffix,
  loading,
  highlight,
  delay,
}: {
  icon: ReactNode;
  label: string;
  value: number;
  format: (v: number) => string;
  foot?: string;
  suffix?: string;
  loading: boolean;
  highlight?: boolean;
  delay: number;
}) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 8 }}
      animate={{ opacity: 1, y: 0 }}
      transition={{ duration: 0.25, delay, ease: [0.22, 1, 0.36, 1] }}
      whileHover={{ y: -2 }}
      className="group relative overflow-hidden rounded-2xl border border-line bg-surface p-4 shadow-card transition-shadow duration-200 hover:shadow-pop"
    >
      <div className="flex items-center gap-2">
        <span
          className={cn(
            "flex size-7 items-center justify-center rounded-lg [&>svg]:size-[15px]",
            highlight ? "bg-warn/14 text-warn" : "bg-accent-soft text-accent-text",
          )}
        >
          {icon}
        </span>
        <span className="text-[12.5px] font-medium text-muted">{label}</span>
      </div>
      <div className="mt-3 flex items-baseline gap-1.5">
        {loading ? (
          <Skeleton className="h-8 w-24" />
        ) : (
          <>
            <span className="text-[28px] font-semibold leading-none tracking-[-0.03em] text-fg">
              <CountUp value={value} format={format} />
            </span>
            {suffix && <span className="text-[13px] font-medium text-subtle">{suffix}</span>}
          </>
        )}
      </div>
      <div className="mt-2 h-4 truncate text-[12px] text-subtle">{loading ? "" : foot}</div>
    </motion.div>
  );
}

function SystemCard() {
  const { state, settings } = useCore();
  const cuda = useLoad(() => cached("cuda_check", 10 * 60_000, () => call("cuda_check", undefined, { timeoutMs: 60_000 })));
  const aiOn = Boolean(settings?.ai_enabled);
  const provider = settings?.ai_provider ?? "openrouter";

  // Баланс OpenRouter — в карточке «ИИ» выше (lib/aiUsage), здесь только что включено.
  const rows: { icon: ReactNode; label: string; value: ReactNode; tone?: string }[] = [
    {
      icon: <Cpu />,
      label: t("home.system.model"),
      value: settings?.model ?? "—",
    },
    {
      icon: <Zap />,
      label: t("home.system.device"),
      value:
        state?.device === "cuda"
          ? cuda.data?.gpu_name ?? t("status.gpu")
          : state?.device === "cpu"
            ? t("home.system.cpu")
            : t("home.system.notLoaded"),
      tone: state?.device === "cuda" ? "text-success" : state?.device === "cpu" ? "text-warn" : undefined,
    },
    {
      icon: <Sparkles />,
      label: t("home.system.ai"),
      value: aiOn ? t(`settings.ai.providers.${provider}`) : t("home.system.aiOff"),
      tone: aiOn ? "text-accent-text" : undefined,
    },
  ];

  return (
    <Card>
      <div className="mb-3 text-[14px] font-semibold text-fg">{t("home.system.title")}</div>
      <div className="space-y-2.5">
        {rows.map((r) => (
          <div key={r.label} className="flex items-center justify-between gap-3">
            <span className="flex items-center gap-2 text-[13px] text-muted [&>svg]:size-3.5 [&>svg]:text-subtle">
              {r.icon}
              {r.label}
            </span>
            <span className={cn("truncate text-right text-[13px] font-medium text-fg", r.tone)}>{r.value}</span>
          </div>
        ))}
      </div>
    </Card>
  );
}
