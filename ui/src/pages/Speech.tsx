// «Речь»: слова-паразиты по сырому тексту распознавания (до словаря и ИИ-очистки).
// Цифры — fillers_stats за выбранный период и предыдущий такой же (тренд), список фраз —
// fillers_config (редактируется здесь), «Найти обороты с ИИ» — fillers_ai_suggest по кнопке.
import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowDownRight,
  ArrowUpRight,
  BarChart3,
  Crown,
  Info,
  ListChecks,
  Minus,
  Percent,
  Plus,
  RefreshCw,
  RotateCcw,
  Sigma,
  Sparkles,
  Speech,
  Trash,
} from "lucide-react";
import { useEffect, useMemo, useState, type ReactNode } from "react";
import { DayBars } from "../components/DayBars";
import { Button, ChipInput, Segmented, Switch } from "../components/ui/controls";
import { Card, EmptyState, LoadError, PageHeader, SectionTitle, Skeleton } from "../components/ui/display";
import { useConfirm } from "../components/ui/overlay";
import { useToast } from "../components/ui/toast";
import { t, tp } from "../i18n";
import { cn } from "../lib/cn";
import { fmtDecimal, fmtLongDate, fmtNum } from "../lib/format";
import { useDebounced, useEvent, useLoad } from "../lib/hooks";
import { errorText } from "../lib/hub";
import type { FillerDay, FillerSuggestion, FillerTop } from "../lib/types";
import { call } from "../lib/ws";

type Period = "7" | "30" | "90" | "0";
const TOP_SHORT = 12;

const fmt100 = (v: number) => fmtDecimal(v, v >= 10 ? 0 : 1);

/** Меньше паразитов — хорошо: падение зелёное, рост красный. */
function Trend({ cur, prev, className }: { cur: number; prev: number | null; className?: string }) {
  if (prev === null) return <span className={cn("text-subtle", className)}>{t("speech.trend.none")}</span>;
  const diff = cur - prev;
  if (Math.abs(diff) < 0.05)
    return (
      <span className={cn("inline-flex items-center gap-1 text-subtle", className)}>
        <Minus className="size-3.5" />
        {t("speech.trend.same")}
      </span>
    );
  const down = diff < 0;
  return (
    <span className={cn("inline-flex items-center gap-1", down ? "text-success" : "text-danger", className)}>
      {down ? <ArrowDownRight className="size-3.5" /> : <ArrowUpRight className="size-3.5" />}
      {t(down ? "speech.trend.down" : "speech.trend.up", { x: fmt100(Math.abs(diff)) })}
    </span>
  );
}

function Kpi({
  icon,
  label,
  value,
  foot,
  loading,
}: {
  icon: ReactNode;
  label: string;
  value: ReactNode;
  foot: ReactNode;
  loading: boolean;
}) {
  return (
    <Card className="min-w-0">
      <div className="flex items-center gap-2 text-[12.5px] font-medium text-muted [&>svg]:size-4 [&>svg]:text-subtle">
        {icon}
        {label}
      </div>
      {loading ? (
        <Skeleton className="mt-3 h-8 w-24" />
      ) : (
        <div className="mt-2 truncate text-[26px] font-semibold tracking-[-0.02em] text-fg tabular">{value}</div>
      )}
      <div className="mt-1 min-h-[18px] truncate text-[12.5px] text-muted">{loading ? "" : foot}</div>
    </Card>
  );
}

function TopRow({ item, max }: { item: FillerTop; max: number }) {
  const w = max > 0 ? Math.max(3, (item.count / max) * 100) : 0;
  const diff = item.prev_per100 === null ? null : item.per100 - item.prev_per100;
  return (
    <div className="grid grid-cols-[minmax(0,10rem)_1fr_auto_auto] items-center gap-3 py-1.5">
      <div className="truncate text-[13.5px] font-medium text-fg" title={item.phrase}>
        {item.phrase}
      </div>
      <div className="h-2 overflow-hidden rounded-full bg-surface-3/70">
        <motion.div
          className="h-full rounded-full bg-accent"
          initial={{ width: 0 }}
          animate={{ width: `${w}%` }}
          transition={{ duration: 0.45, ease: [0.22, 1, 0.36, 1] }}
        />
      </div>
      <div className="w-[4.5rem] text-right text-[12.5px] text-muted tabular">{tp("speech.topList.times", item.count)}</div>
      <div className="flex w-[5.5rem] items-center justify-end gap-1 text-[12.5px] tabular">
        <span className="text-fg">{fmt100(item.per100)}</span>
        {diff !== null && Math.abs(diff) >= 0.05 && (
          <span className={diff < 0 ? "text-success" : "text-danger"}>
            {diff < 0 ? <ArrowDownRight className="size-3.5" /> : <ArrowUpRight className="size-3.5" />}
          </span>
        )}
      </div>
    </div>
  );
}

export function SpeechPage() {
  const toast = useToast();
  const confirm = useConfirm();
  const [period, setPeriod] = useState<Period>("30");
  const stats = useLoad(() => call("fillers_stats", { days: Number(period) }), [period]);
  const cfg = useLoad(() => call("fillers_config"));
  const [draft, setDraft] = useState<string[] | null>(null);
  const [showAll, setShowAll] = useState(false);
  const [busy, setBusy] = useState<"save" | "rebuild" | "ai" | "reset" | null>(null);
  const [ideas, setIdeas] = useState<{ items: FillerSuggestion[]; sampled: number } | null>(null);

  const refresh = useDebounced(() => stats.reload(), 900);
  useEvent("result", () => refresh.call());

  useEffect(() => {
    if (cfg.data && draft === null) setDraft(cfg.data.phrases);
  }, [cfg.data, draft]);

  const s = stats.data;
  const phrases = draft ?? cfg.data?.phrases ?? [];
  const dirty = !!cfg.data && draft !== null && draft.join("\n") !== cfg.data.phrases.join("\n");
  const top = s?.top ?? [];
  const shown = showAll ? top : top.slice(0, TOP_SHORT);
  const maxCount = top[0]?.count ?? 0;
  const hasData = !!s && s.daily.some((d) => d.words > 0);

  const tip = useMemo(
    () => (d: FillerDay) =>
      t("speech.chart.tip", { per100: fmt100(d.per100), fillers: fmtNum(d.fillers), words: tp("units.words", d.words) }),
    [],
  );

  const rebuild = async (quiet = false) => {
    setBusy("rebuild");
    try {
      const r = await call("fillers_rebuild");
      if (!quiet)
        toast({
          tone: "success",
          title: t("speech.list.rebuilt", { days: tp("units.days", r.days), dictations: tp("units.dictations", r.dictations) }),
        });
      stats.reload();
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  const savePhrases = async (next: string[]): Promise<boolean> => {
    setBusy("save");
    try {
      const c = await call("fillers_config_set", { phrases: next });
      cfg.setData(() => c);
      setDraft(c.phrases);
      return true;
    } catch (e) {
      toast({ tone: "error", title: t("errors.save"), description: errorText(e) });
      return false;
    } finally {
      setBusy(null);
    }
  };

  const save = async () => {
    if (!(await savePhrases(phrases))) return;
    toast({
      tone: "success",
      title: t("speech.list.saved"),
      description: t("speech.list.savedRebuild"),
      action: { label: t("speech.list.rebuild"), onClick: () => void rebuild() },
    });
  };

  const restore = async () => {
    try {
      const c = await call("fillers_defaults");
      cfg.setData(() => c);
      setDraft(c.phrases);
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  const toggle = async (on: boolean) => {
    try {
      const c = await call("fillers_config_set", { enabled: on });
      cfg.setData(() => c);
    } catch (e) {
      toast({ tone: "error", title: t("errors.save"), description: errorText(e) });
    }
  };

  const askAi = async () => {
    setBusy("ai");
    setIdeas(null);
    try {
      const r = await call("fillers_ai_suggest");
      setIdeas(r);
      if (!r.items.length) toast({ tone: "info", title: t("speech.ai.none", { n: r.sampled }) });
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  const addIdea = async (p: string) => {
    if (await savePhrases([...phrases, p])) {
      setIdeas((x) => (x ? { ...x, items: x.items.filter((i) => i.phrase !== p) } : x));
      toast({ tone: "success", title: t("speech.ai.added", { phrase: p }) });
      void rebuild(true);
    }
  };

  const reset = async () => {
    const ok = await confirm({
      title: t("speech.reset.title"),
      body: t("speech.reset.body"),
      confirmLabel: t("speech.reset.confirm"),
      danger: true,
    });
    if (!ok) return;
    setBusy("reset");
    try {
      await call("fillers_reset");
      toast({ tone: "info", title: t("speech.reset.done") });
      stats.reload();
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div>
      <PageHeader
        title={t("speech.title")}
        subtitle={t("speech.subtitle")}
        actions={
          <Segmented<Period>
            size="sm"
            value={period}
            onChange={(v) => {
              setPeriod(v);
              setShowAll(false);
            }}
            options={[
              { value: "7", label: t("speech.period.d7") },
              { value: "30", label: t("speech.period.d30") },
              { value: "90", label: t("speech.period.d90") },
              { value: "0", label: t("speech.period.all") },
            ]}
          />
        }
      />

      {stats.error && !s ? (
        <LoadError error={stats.error} onRetry={stats.reload} />
      ) : (
        <>
          <div className="grid grid-cols-3 gap-3">
            <Kpi
              loading={!s}
              icon={<Percent />}
              label={t("speech.kpi.per100")}
              value={s ? fmt100(s.per100) : ""}
              foot={s ? <Trend cur={s.per100} prev={s.prev_per100} /> : ""}
            />
            <Kpi
              loading={!s}
              icon={<Sigma />}
              label={t("speech.kpi.total")}
              value={s ? fmtNum(s.fillers) : ""}
              foot={s ? t("speech.kpi.totalFoot", { words: tp("units.words", s.words) }) : ""}
            />
            <Kpi
              loading={!s}
              icon={<Crown />}
              label={t("speech.kpi.top")}
              value={top[0] ? t("speech.kpi.topValue", { phrase: top[0].phrase }) : t("speech.kpi.topNone")}
              foot={
                top[0]
                  ? t("speech.kpi.topFoot", { count: tp("speech.topList.times", top[0].count), per100: fmt100(top[0].per100) })
                  : ""
              }
            />
          </div>

          <Card className="mt-3 pb-3">
            <div className="mb-4 flex items-start justify-between gap-4">
              <div>
                <div className="flex items-center gap-2 text-[14px] font-semibold text-fg">
                  <BarChart3 className="size-4 text-subtle" />
                  {t("speech.chart.title")}
                </div>
                {s?.since && (
                  <div className="mt-0.5 text-[12.5px] text-muted">{t("speech.since", { date: fmtLongDate(s.since) })}</div>
                )}
              </div>
            </div>
            {!s ? (
              <Skeleton className="h-[188px] w-full" />
            ) : hasData ? (
              <DayBars data={s.daily} value={(d) => d.per100} axisFormat={fmt100} tooltip={tip} emptyMax={10} />
            ) : (
              <EmptyState compact icon={<Speech />} title={t("speech.chart.emptyTitle")} text={t("speech.chart.emptyText")} />
            )}
          </Card>

          <Card className="mt-3">
            <SectionTitle hint={t("speech.topList.hint")}>{t("speech.topList.title")}</SectionTitle>
            {!s ? (
              <div className="space-y-2">
                {[0, 1, 2, 3].map((i) => (
                  <Skeleton key={i} className="h-5 w-full" />
                ))}
              </div>
            ) : top.length === 0 ? (
              <p className="text-[13px] text-muted">{t("speech.topList.empty")}</p>
            ) : (
              <>
                <div className="grid grid-cols-[minmax(0,10rem)_1fr_auto_auto] gap-3 pb-1 text-[11.5px] text-subtle">
                  <span />
                  <span />
                  <span className="w-[4.5rem] text-right">{t("speech.topList.colCount")}</span>
                  <span className="w-[5.5rem] text-right">{t("speech.topList.colPer100")}</span>
                </div>
                <div className="divide-y divide-line/60">
                  <AnimatePresence initial={false}>
                    {shown.map((it) => (
                      <motion.div key={it.phrase} initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
                        <TopRow item={it} max={maxCount} />
                      </motion.div>
                    ))}
                  </AnimatePresence>
                </div>
                {top.length > TOP_SHORT && (
                  <button
                    type="button"
                    onClick={() => setShowAll((v) => !v)}
                    className="mt-2 text-[12.5px] font-medium text-muted transition-colors hover:text-accent-text"
                  >
                    {showAll ? t("speech.topList.showLess") : t("speech.topList.showAll", { n: top.length })}
                  </button>
                )}
              </>
            )}
          </Card>
        </>
      )}

      <Card className="mt-3">
        <SectionTitle
          hint={t("speech.list.hint")}
          right={
            cfg.data && (
              <Switch checked={cfg.data.enabled} onChange={(v) => void toggle(v)} label={t("speech.list.enabled")} />
            )
          }
        >
          <span className="inline-flex items-center gap-2">
            <ListChecks className="size-4 text-subtle" />
            {t("speech.list.title")}
          </span>
        </SectionTitle>
        {cfg.data && !cfg.data.enabled && <p className="mb-3 text-[12.5px] text-warn">{t("speech.list.enabledHint")}</p>}
        {cfg.error && !cfg.data ? (
          <LoadError error={cfg.error} onRetry={cfg.reload} />
        ) : !cfg.data ? (
          <Skeleton className="h-24 w-full" />
        ) : (
          <>
            <ChipInput values={phrases} onChange={setDraft} placeholder={t("speech.list.placeholder")} />
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <Button size="sm" disabled={!dirty} loading={busy === "save"} onClick={() => void save()}>
                {t("speech.list.save")}
              </Button>
              <Button size="sm" variant="secondary" icon={<RefreshCw className="size-3.5" />} loading={busy === "rebuild"} onClick={() => void rebuild()}>
                {t("speech.list.rebuild")}
              </Button>
              <Button size="sm" variant="ghost" icon={<RotateCcw className="size-3.5" />} onClick={() => void restore()}>
                {t("speech.list.defaults")}
              </Button>
              <span className="ml-auto text-[12px] text-subtle">{t("speech.list.count", { n: phrases.length })}</span>
            </div>
          </>
        )}

        <div className="mt-5 rounded-xl border border-line bg-surface-2/60 p-4">
          <div className="flex flex-wrap items-start justify-between gap-3">
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 text-[13.5px] font-medium text-fg">
                <Sparkles className="size-4 text-accent-text" />
                {t("speech.ai.title")}
              </div>
              <p className="mt-1 text-[12.5px] leading-relaxed text-muted">{t("speech.ai.hint")}</p>
            </div>
            <Button size="sm" variant="soft" icon={<Sparkles className="size-3.5" />} loading={busy === "ai"} onClick={() => void askAi()}>
              {t("speech.ai.run")}
            </Button>
          </div>
          {ideas && ideas.items.length > 0 && (
            <div className="mt-3">
              <div className="mb-2 text-[12.5px] text-muted">{t("speech.ai.found", { n: ideas.sampled })}</div>
              <div className="flex flex-wrap gap-2">
                {ideas.items.map((it) => (
                  <button
                    key={it.phrase}
                    type="button"
                    title={it.example || undefined}
                    aria-label={t("speech.ai.add", { phrase: it.phrase })}
                    onClick={() => void addIdea(it.phrase)}
                    className="inline-flex items-center gap-1.5 rounded-lg border border-line-strong bg-surface px-2.5 py-1 text-[12.5px] text-fg transition-colors hover:border-accent hover:text-accent-text"
                  >
                    <Plus className="size-3.5" />
                    {it.phrase}
                    <span className="text-subtle tabular">{tp("speech.topList.times", it.count)}</span>
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
      </Card>

      <div className="mt-4 space-y-1.5 px-1 text-[12px] leading-relaxed text-subtle">
        {(["whisper", "meaning", "rebuild"] as const).map((k) => (
          <p key={k} className="flex gap-2">
            <Info className="mt-[3px] size-3.5 shrink-0" />
            <span>{t(`speech.note.${k}`)}</span>
          </p>
        ))}
      </div>

      <div className="mt-4 flex justify-end">
        <Button size="sm" variant="danger" icon={<Trash className="size-3.5" />} loading={busy === "reset"} onClick={() => void reset()}>
          {t("speech.reset.button")}
        </Button>
      </div>
    </div>
  );
}
