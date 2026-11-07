import { AnimatePresence, motion } from "framer-motion";
import {
  AudioWaveform,
  Check,
  ClipboardPaste,
  Clock,
  Copy,
  Eraser,
  FolderOpen,
  Mic,
  MonitorSmartphone,
  NotebookPen,
  Play,
  RefreshCw,
  Search,
  SearchX,
  Sparkles,
  Square,
  Terminal,
  Trash,
  Wand2,
  X,
} from "lucide-react";
import { useEffect, useMemo, useRef, useState, useSyncExternalStore } from "react";
import { AppIcon } from "../components/AppIcon";
import { Button, IconButton, Input, Textarea } from "../components/ui/controls";
import { Badge, Card, EmptyState, KeyCombo, LoadError, PageHeader, Skeleton, Spinner } from "../components/ui/display";
import { Menu } from "../components/ui/menu";
import { Tooltip, useConfirm } from "../components/ui/overlay";
import { Tabs } from "../components/ui/tabs";
import { useToast } from "../components/ui/toast";
import { t, tp, useLocale } from "../i18n";
import { cn } from "../lib/cn";
import { appName, countWords, dayKey, fmtDayLabel, fmtMb, fmtSeconds, fmtTime } from "../lib/format";
import { useDebounced, useEvent } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { navigate } from "../lib/router";
import { useCore, useStyleName } from "../lib/store";
import type { ClearPeriod, HistoryItem, RecordingItem, RetranscribeResult } from "../lib/types";
import { call } from "../lib/ws";

const PAGE = 50;

// ——— Прослушивание записей голоса ———
// Один плеер на всё окно: новая запись останавливает прежнюю. Звук — WAV из ядра
// (recording_audio) в blob: URL; CSP окна разрешает media-src blob:.

let playing: string | null = null;
let player: HTMLAudioElement | null = null;
let playerUrl: string | null = null;
const playListeners = new Set<() => void>();

function setPlaying(id: string | null) {
  playing = id;
  playListeners.forEach((f) => f());
}

function stopPlayback() {
  player?.pause();
  player = null;
  if (playerUrl) URL.revokeObjectURL(playerUrl);
  playerUrl = null;
  setPlaying(null);
}

async function startPlayback(id: string): Promise<void> {
  stopPlayback();
  setPlaying(id);
  try {
    const r = await call("recording_audio", { id });
    if (playing !== id) return; // пока грузилось, нажали другую или «Стоп»
    const bin = atob(r.data);
    const bytes = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) bytes[i] = bin.charCodeAt(i);
    playerUrl = URL.createObjectURL(new Blob([bytes], { type: r.mime }));
    const a = new Audio(playerUrl);
    player = a;
    a.onended = () => player === a && stopPlayback();
    await a.play();
  } catch (e) {
    if (playing === id) stopPlayback();
    throw e;
  }
}

function usePlaying(): string | null {
  return useSyncExternalStore(
    (f) => {
      playListeners.add(f);
      return () => playListeners.delete(f);
    },
    () => playing,
  );
}

/** Кнопка «Прослушать / Остановить» записи голоса. */
function PlayButton({ recId, compact = false }: { recId: string; compact?: boolean }) {
  const toast = useToast();
  const now = usePlaying();
  const on = now === recId;
  const toggle = () => {
    if (on) return stopPlayback();
    startPlayback(recId).catch((e: unknown) =>
      toast({ tone: "error", title: t("history.rec.playError"), description: errorText(e) }),
    );
  };
  const icon = on ? <Square className="size-4" /> : <Play className="size-4" />;
  if (compact) {
    const label = on ? t("history.rec.stop") : t("history.actions.play");
    return (
      <Tooltip content={label} side="right">
        <IconButton label={label} size="sm" onClick={toggle}>
          {icon}
        </IconButton>
      </Tooltip>
    );
  }
  return (
    <Button size="sm" variant="secondary" icon={icon} onClick={toggle}>
      {on ? t("history.rec.stop") : t("history.rec.play")}
    </Button>
  );
}

export function HistoryPage({ tab }: { tab: string | null }) {
  const current = tab === "draft" ? "draft" : tab === "recordings" ? "recordings" : "feed";
  return (
    <div>
      <PageHeader title={t("history.title")} subtitle={t("history.subtitle")} />
      <Tabs
        value={current}
        onChange={(v) => navigate(v === "feed" ? "history" : `history/${v}`)}
        items={[
          { value: "feed", label: t("history.tabs.feed"), icon: <Clock /> },
          { value: "recordings", label: t("history.tabs.recordings"), icon: <AudioWaveform /> },
          { value: "draft", label: t("history.tabs.draft"), icon: <NotebookPen /> },
        ]}
        className="mb-6"
      />
      <AnimatePresence mode="wait" initial={false}>
        <motion.div
          key={current}
          initial={{ opacity: 0, y: 6 }}
          animate={{ opacity: 1, y: 0 }}
          exit={{ opacity: 0, y: -4 }}
          transition={{ duration: 0.18 }}
        >
          {current === "draft" ? <Draft /> : current === "recordings" ? <Recordings /> : <Feed />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

// ——— Лента ———

async function copyText(text: string): Promise<void> {
  await navigator.clipboard.writeText(text);
}

function Feed() {
  const { status, settings } = useCore();
  const toast = useToast();
  const confirm = useConfirm();
  const [query, setQuery] = useState("");
  const [applied, setApplied] = useState("");
  const [items, setItems] = useState<HistoryItem[]>([]);
  const [total, setTotal] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [more, setMore] = useState(false);
  const [moreError, setMoreError] = useState<string | null>(null);
  const reqId = useRef(0);
  const sentinel = useRef<HTMLDivElement>(null);
  // диктовка → её сохранённая запись голоса (для «Прослушать» в ленте)
  const [recByHistory, setRecByHistory] = useState<Map<number, string>>(new Map());
  const [recTick, setRecTick] = useState(0);
  useEffect(() => {
    if (status !== "open") return;
    let alive = true;
    call("recordings_list")
      .then((r) => {
        if (!alive) return;
        setRecByHistory(new Map(r.items.filter((x) => x.history_id !== null).map((x) => [x.history_id as number, x.id])));
      })
      .catch(() => undefined);
    return () => {
      alive = false;
    };
  }, [status, tick, recTick]);
  useEffect(() => () => stopPlayback(), []);

  const applyQuery = useDebounced((q: string) => setApplied(q.trim()), 250);

  // Первая страница (и при смене поиска / переподключении)
  useEffect(() => {
    if (status !== "open") return;
    const id = ++reqId.current;
    setLoading(true);
    setMoreError(null);
    call("history_list", { query: applied || undefined, limit: PAGE, offset: 0 })
      .then((r) => {
        if (id !== reqId.current) return;
        setItems(r.items);
        setTotal(r.total);
        setLoadError(null);
      })
      .catch((e: unknown) => {
        if (id === reqId.current) setLoadError(errorText(e));
      })
      .finally(() => id === reqId.current && setLoading(false));
  }, [applied, status, tick]);

  // Ленивая подгрузка. После ошибки сама не повторяется (иначе IntersectionObserver
  // будет долбить ядро) — только кнопкой «Повторить».
  const loadMore = (force = false) => {
    if (more || loading || items.length >= total || (moreError && !force)) return;
    setMore(true);
    setMoreError(null);
    const id = reqId.current;
    call("history_list", { query: applied || undefined, limit: PAGE, offset: items.length })
      .then((r) => {
        if (id !== reqId.current) return;
        setItems((xs) => {
          const seen = new Set(xs.map((x) => x.id));
          return [...xs, ...r.items.filter((x) => !seen.has(x.id))];
        });
        setTotal(r.total);
      })
      .catch((e: unknown) => {
        if (id === reqId.current) setMoreError(errorText(e));
      })
      .finally(() => setMore(false));
  };
  const loadMoreRef = useRef(loadMore);
  loadMoreRef.current = loadMore;

  useEffect(() => {
    const el = sentinel.current;
    if (!el) return;
    const io = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) loadMoreRef.current();
    }, { rootMargin: "400px" });
    io.observe(el);
    return () => io.disconnect();
  }, [items.length]);

  // Новая диктовка — сразу в ленту
  useEvent("history", (item) => {
    if (applied) return;
    setItems((xs) => (xs.some((x) => x.id === item.id) ? xs : [item, ...xs]));
    setTotal((n) => n + 1);
    setRecTick((n) => n + 1);
  });

  const locale = useLocale();
  const groups = useMemo(() => {
    const out: { key: string; label: string; items: HistoryItem[] }[] = [];
    for (const it of items) {
      const k = dayKey(it.ts);
      const last = out[out.length - 1];
      if (last && last.key === k) last.items.push(it);
      else out.push({ key: k, label: fmtDayLabel(it.ts), items: [it] });
    }
    return out;
  }, [items, locale]); // locale: подписи дней («Сегодня» / "Today")

  const remove = async (it: HistoryItem) => {
    setItems((xs) => xs.filter((x) => x.id !== it.id));
    setTotal((n) => Math.max(0, n - 1));
    try {
      await call("history_delete", { id: it.id });
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
      setItems((xs) => [...xs, it].sort((a, b) => b.ts - a.ts));
    }
  };

  const clear = async (period: ClearPeriod) => {
    const ok = await confirm({
      title: t(`history.clear.confirm.${period}`),
      body: t("history.clear.body"),
      confirmLabel: t("history.clear.action"),
      danger: true,
    });
    if (!ok) return;
    try {
      const r = await call("history_clear", { period });
      toast({ tone: "success", title: tp("history.clear.done", r.deleted) });
      const res = await call("history_list", { query: applied || undefined, limit: PAGE, offset: 0 });
      setItems(res.items);
      setTotal(res.total);
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  const disabledHistory = settings && !settings.history_enabled;

  return (
    <div>
      <div className="mb-5 flex items-center gap-2">
        <Input
          className="flex-1"
          icon={<Search />}
          placeholder={t("history.search")}
          value={query}
          onChange={(e) => {
            setQuery(e.target.value);
            applyQuery.call(e.target.value);
          }}
          right={
            query ? (
              <IconButton
                size="sm"
                label={t("common.clear")}
                onClick={() => {
                  setQuery("");
                  setApplied("");
                }}
              >
                <X className="size-3.5" />
              </IconButton>
            ) : undefined
          }
        />
        <Menu
          label={t("history.clear.button")}
          icon={<Eraser className="size-4" />}
          items={(["day", "week", "month", "all"] as const).map((p) => ({
            label: t(`history.clear.periods.${p}`),
            danger: p === "all",
            onSelect: () => void clear(p),
          }))}
        />
      </div>

      {disabledHistory && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-warn/30 bg-warn/8 px-4 py-3 text-[13px] text-fg">
          <span className="flex-1">{t("history.disabled")}</span>
          <Button size="sm" variant="secondary" onClick={() => navigate("settings/history")}>
            {t("history.disabledAction")}
          </Button>
        </div>
      )}

      {!loading && items.length > 0 && (
        <div className="mb-3 text-[12.5px] text-subtle">
          {applied ? tp("history.found", total) : tp("history.total", total)}
        </div>
      )}

      {loading ? (
        <FeedSkeleton />
      ) : loadError && items.length === 0 ? (
        <LoadError error={loadError} onRetry={() => setTick((x) => x + 1)} />
      ) : items.length === 0 ? (
        applied ? (
          <EmptyState icon={<SearchX />} title={t("history.emptySearch.title")} text={t("history.emptySearch.text", { q: applied })} />
        ) : (
          <EmptyFeed />
        )
      ) : (
        <div className="space-y-6">
          {groups.map((g) => (
            <section key={g.key}>
              <div className="sticky top-0 z-10 -mx-2 mb-2 flex items-center gap-2 bg-layer/85 px-2 py-1.5 backdrop-blur-md">
                <span className="text-[12.5px] font-semibold uppercase tracking-[0.04em] text-muted">{g.label}</span>
                <span className="text-[12px] text-subtle">{tp("units.dictations", g.items.length)}</span>
              </div>
              <Card padded={false} className="overflow-hidden">
                <AnimatePresence initial={false}>
                  {g.items.map((it) => (
                    <HistoryRow key={it.id} item={it} recId={recByHistory.get(it.id)} onDelete={() => void remove(it)} />
                  ))}
                </AnimatePresence>
              </Card>
            </section>
          ))}
          <div ref={sentinel} className="flex min-h-10 items-center justify-center gap-2">
            {more && <Spinner />}
            {!more && moreError && (
              <>
                <span className="text-[12.5px] text-danger">{t("history.moreError")}</span>
                <Button size="sm" variant="ghost" icon={<RefreshCw className="size-4" />} onClick={() => loadMore(true)}>
                  {t("common.retry")}
                </Button>
              </>
            )}
            {!more && !moreError && items.length >= total && items.length > PAGE && (
              <span className="text-[12px] text-subtle">{t("history.end")}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function EmptyFeed() {
  const { state } = useCore();
  return (
    <EmptyState
      icon={<Mic />}
      title={t("history.empty.title")}
      text={
        <span className="inline-flex flex-wrap items-center justify-center gap-1.5">
          {t("history.empty.text")}
          {state && <KeyCombo keys={[state.hotkeys.ptt]} />}
          {t("history.empty.textAfter")}
        </span>
      }
    />
  );
}

function FeedSkeleton() {
  return (
    <div className="space-y-3">
      <Skeleton className="h-4 w-24" />
      <Card padded={false}>
        {[0, 1, 2, 3].map((i) => (
          <div key={i} className="flex gap-3 border-b border-line px-5 py-4 last:border-0">
            <Skeleton className="size-8 shrink-0" />
            <div className="flex-1 space-y-2">
              <Skeleton className="h-3 w-40" />
              <Skeleton className="h-3.5 w-full" />
              <Skeleton className="h-3.5 w-3/4" />
            </div>
          </div>
        ))}
      </Card>
    </div>
  );
}

function HistoryRow({ item, recId, onDelete }: { item: HistoryItem; recId?: string; onDelete: () => void }) {
  const toast = useToast();
  const styleName = useStyleName();
  const [showRaw, setShowRaw] = useState(false);
  const [expanded, setExpanded] = useState(false);
  const [copied, setCopied] = useState(false);
  const differs = item.raw.trim() !== item.text.trim();
  const text = showRaw ? item.raw : item.text;
  const long = text.length > 320 || text.split("\n").length > 5;

  const copy = async () => {
    try {
      await copyText(text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (e) {
      toast({ tone: "error", title: t("errors.copy"), description: errorText(e) });
    }
  };

  const reinsert = async () => {
    try {
      await call("history_reinsert", { id: item.id });
      toast({ tone: "success", title: t("history.reinserted") });
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  return (
    <motion.div
      layout
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
      className="group overflow-hidden border-b border-line last:border-b-0"
    >
      <div className="flex gap-3.5 px-5 py-4 transition-colors duration-150 hover:bg-surface-2/50">
        <AppIcon process={item.app} size={32} className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[12px] text-subtle">
            <span className="font-medium text-muted">{appName(item.app)}</span>
            {item.title && (
              <>
                <span>·</span>
                <span className="min-w-0 max-w-[340px] truncate" title={item.title}>
                  {item.title}
                </span>
              </>
            )}
            <span className="ml-auto shrink-0 tabular">{fmtTime(item.ts)}</span>
          </div>
          <div
            className={cn(
              "selectable mt-1 whitespace-pre-wrap break-words text-[14px] leading-relaxed",
              showRaw ? "text-muted" : "text-fg",
              long && !expanded && "line-clamp-5",
            )}
          >
            {text}
          </div>
          {long && (
            <button
              type="button"
              onClick={() => setExpanded((v) => !v)}
              className="mt-1 text-[12.5px] font-medium text-accent-text hover:underline"
            >
              {expanded ? t("history.collapse") : t("history.expand")}
            </button>
          )}
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            {differs && (
              <div className="mr-1 inline-flex rounded-md bg-surface-2 p-0.5 text-[11.5px] font-medium">
                <button
                  type="button"
                  onClick={() => setShowRaw(false)}
                  className={cn(
                    "rounded px-1.5 py-0.5 transition-colors",
                    !showRaw ? "bg-surface text-fg shadow-card" : "text-subtle hover:text-fg",
                  )}
                >
                  {t("history.processed")}
                </button>
                <button
                  type="button"
                  onClick={() => setShowRaw(true)}
                  className={cn(
                    "rounded px-1.5 py-0.5 transition-colors",
                    showRaw ? "bg-surface text-fg shadow-card" : "text-subtle hover:text-fg",
                  )}
                >
                  {t("history.raw")}
                </button>
              </div>
            )}
            <Badge>{fmtSeconds(item.duration_s)}</Badge>
            <Badge>{tp("units.words", item.words || countWords(item.text))}</Badge>
            {item.ai && (
              <Badge tone="accent" icon={<Sparkles />}>
                {t("history.badges.ai")}
              </Badge>
            )}
            {item.style && item.style !== "normal" && <Badge tone="neutral">{styleName(item.style)}</Badge>}
            {item.target === "rdp_all" && (
              <Badge tone="info" icon={<MonitorSmartphone />}>
                {t("history.badges.rdpAll")}
              </Badge>
            )}
            {item.target === "command" && (
              <Badge tone="warn" icon={<Terminal />}>
                {t("history.badges.command")}
              </Badge>
            )}
            {item.target === "test" && <Badge>{t("history.badges.test")}</Badge>}
          </div>
        </div>
        <div className="flex shrink-0 flex-col gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-within:opacity-100">
          {recId && <PlayButton recId={recId} compact />}
          <Tooltip content={copied ? t("common.copied") : t("history.actions.copy")} side="right">
            <IconButton label={t("history.actions.copy")} size="sm" onClick={() => void copy()}>
              {copied ? <Check className="size-4 text-success" /> : <Copy className="size-4" />}
            </IconButton>
          </Tooltip>
          <Tooltip content={t("history.actions.reinsert")} side="right">
            <IconButton label={t("history.actions.reinsert")} size="sm" onClick={() => void reinsert()}>
              <ClipboardPaste className="size-4" />
            </IconButton>
          </Tooltip>
          <Tooltip content={t("history.actions.delete")} side="right">
            <IconButton label={t("history.actions.delete")} size="sm" tone="danger" onClick={onDelete}>
              <Trash className="size-4" />
            </IconButton>
          </Tooltip>
        </div>
      </div>
    </motion.div>
  );
}

// ——— Записи голоса ———

const MIB = 1024 * 1024;

function Recordings() {
  const { status, settings } = useCore();
  const toast = useToast();
  const confirm = useConfirm();
  const [items, setItems] = useState<RecordingItem[]>([]);
  const [totalBytes, setTotalBytes] = useState(0);
  useEffect(() => () => stopPlayback(), []);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);

  const reload = () => setTick((x) => x + 1);

  useEffect(() => {
    if (status !== "open") return;
    let alive = true;
    call("recordings_list")
      .then((r) => {
        if (!alive) return;
        setItems(r.items);
        setTotalBytes(r.total_bytes);
        setLoadError(null);
      })
      .catch((e: unknown) => alive && setLoadError(errorText(e)))
      .finally(() => alive && setLoading(false));
    return () => {
      alive = false;
    };
  }, [status, tick]);

  // Новая диктовка — новая запись (и, возможно, ротация старых): перечитать список
  useEvent("history", () => reload());
  useEvent("result", () => reload());

  const fail = (e: unknown) => toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });

  const remove = async (it: RecordingItem) => {
    setItems((xs) => xs.filter((x) => x.id !== it.id));
    setTotalBytes((n) => Math.max(0, n - it.bytes));
    try {
      await call("recording_delete", { id: it.id });
    } catch (e) {
      fail(e);
      reload();
    }
  };

  const clearAll = async () => {
    const ok = await confirm({
      title: t("history.rec.clearConfirm"),
      body: t("history.rec.clearBody"),
      confirmLabel: t("history.rec.clear"),
      danger: true,
    });
    if (!ok) return;
    try {
      await call("recordings_clear");
    } catch (e) {
      fail(e);
    }
    reload();
  };

  const keep = settings?.recordings_keep ?? 30;
  const maxMb = settings?.recordings_max_mb ?? 300;

  return (
    <div>
      <p className="mb-4 text-[13px] leading-relaxed text-muted">{t("history.rec.intro")}</p>
      <div className="mb-5 flex flex-wrap items-center gap-2">
        <span className="flex-1 text-[12.5px] text-subtle tabular">
          {keep > 0 &&
            t("history.rec.usage", {
              n: items.length,
              keep,
              size: fmtMb(totalBytes / MIB),
              max: fmtMb(maxMb),
            })}
        </span>
        <Button size="sm" variant="ghost" icon={<FolderOpen className="size-4" />} onClick={() => void call("recording_reveal", {}).catch(fail)}>
          {t("history.rec.folder")}
        </Button>
        <Button size="sm" variant="secondary" onClick={() => navigate("settings/history")}>
          {t("history.rec.settings")}
        </Button>
        {items.length > 0 && (
          <Button size="sm" variant="danger" icon={<Trash className="size-4" />} onClick={() => void clearAll()}>
            {t("history.rec.clear")}
          </Button>
        )}
      </div>

      {keep === 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-warn/30 bg-warn/8 px-4 py-3 text-[13px] text-fg">
          <span className="flex-1">{t("history.rec.off")}</span>
          <Button size="sm" variant="secondary" onClick={() => navigate("settings/history")}>
            {t("history.disabledAction")}
          </Button>
        </div>
      )}

      {loading ? (
        <FeedSkeleton />
      ) : loadError && items.length === 0 ? (
        <LoadError error={loadError} onRetry={reload} />
      ) : items.length === 0 ? (
        <EmptyState icon={<AudioWaveform />} title={t("history.rec.emptyTitle")} text={t("history.rec.emptyText")} />
      ) : (
        <Card padded={false} className="overflow-hidden">
          <AnimatePresence initial={false}>
            {items.map((it) => (
              <RecordingRow key={it.id} item={it} onDelete={() => void remove(it)} onChanged={reload} />
            ))}
          </AnimatePresence>
        </Card>
      )}
    </div>
  );
}

function RecordingRow({ item, onDelete, onChanged }: { item: RecordingItem; onDelete: () => void; onChanged: () => void }) {
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<RetranscribeResult | null>(null);
  const [copied, setCopied] = useState(false);

  const transcribe = async () => {
    setBusy(true);
    try {
      const r = await call("recording_transcribe", { id: item.id });
      setResult(r);
      toast(r.text ? { tone: "success", title: t("history.rec.result") } : { tone: "warn", title: t("history.rec.resultEmpty") });
      onChanged();
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const copy = async () => {
    if (!result?.text) return;
    try {
      await copyText(result.text);
      setCopied(true);
      setTimeout(() => setCopied(false), 1400);
    } catch (e) {
      toast({ tone: "error", title: t("errors.copy"), description: errorText(e) });
    }
  };

  const status =
    item.status === "ok" ? (
      <Badge tone="success">{t("history.rec.statusOk", { n: item.chars })}</Badge>
    ) : item.status === "empty" ? (
      <Badge tone="warn">{t("history.rec.statusEmpty")}</Badge>
    ) : item.status === "error" ? (
      <Badge tone="danger">{t("history.rec.statusError")}</Badge>
    ) : (
      <Badge>{t("history.rec.statusPending")}</Badge>
    );

  return (
    <motion.div
      layout
      initial={{ opacity: 0, height: 0 }}
      animate={{ opacity: 1, height: "auto" }}
      exit={{ opacity: 0, height: 0 }}
      transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
      className="group overflow-hidden border-b border-line last:border-b-0"
    >
      <div className="flex items-start gap-3.5 px-5 py-4 transition-colors duration-150 hover:bg-surface-2/50">
        <AppIcon process={item.app} size={32} className="mt-0.5" />
        <div className="min-w-0 flex-1">
          <div className="flex items-center gap-2 text-[12px] text-subtle">
            <span className="font-medium text-muted">{item.app ? appName(item.app) : "—"}</span>
            <span className="ml-auto shrink-0 tabular">
              {fmtDayLabel(item.ts)}, {fmtTime(item.ts)}
            </span>
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <Badge>{fmtSeconds(item.duration_s)}</Badge>
            <Badge>{fmtMb(item.bytes / MIB)}</Badge>
            {status}
          </div>
          {result?.text && (
            <div className="selectable mt-2.5 whitespace-pre-wrap break-words rounded-lg bg-surface-2/60 px-3 py-2 text-[14px] leading-relaxed text-fg">
              {result.text}
            </div>
          )}
          <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
            <PlayButton recId={item.id} />
            <Button size="sm" variant="soft" icon={<Wand2 className="size-4" />} loading={busy} onClick={() => void transcribe()}>
              {busy ? t("history.rec.transcribing") : t("history.rec.transcribe")}
            </Button>
            {result?.text && (
              <Button size="sm" variant="ghost" icon={copied ? <Check className="size-4 text-success" /> : <Copy className="size-4" />} onClick={() => void copy()}>
                {copied ? t("common.copied") : t("history.actions.copy")}
              </Button>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-col gap-1 opacity-0 transition-opacity duration-150 group-hover:opacity-100 focus-within:opacity-100">
          <Tooltip content={t("history.rec.reveal")} side="right">
            <IconButton
              label={t("history.rec.reveal")}
              size="sm"
              onClick={() =>
                void call("recording_reveal", { id: item.id }).catch((e: unknown) =>
                  toast({ tone: "error", title: t("errors.generic"), description: errorText(e) }),
                )
              }
            >
              <FolderOpen className="size-4" />
            </IconButton>
          </Tooltip>
          <Tooltip content={t("history.rec.delete")} side="right">
            <IconButton label={t("history.rec.delete")} size="sm" tone="danger" onClick={onDelete}>
              <Trash className="size-4" />
            </IconButton>
          </Tooltip>
        </div>
      </div>
    </motion.div>
  );
}

// ——— Черновик ———

type SaveState = "idle" | "saving" | "saved" | "error";

// Текст черновика, который ядро ещё не подтвердило (draft_set в полёте или упал).
// Живёт вне компонента: уход со вкладки размонтирует Draft, а текст терять нельзя —
// при возвращении он станет начальным и уйдёт в ядро повторно.
let unsavedDraft: string | null = null;
let draftSeq = 0;

function Draft() {
  const { status, state } = useCore();
  const toast = useToast();
  const confirm = useConfirm();
  const [text, setText] = useState<string | null>(unsavedDraft);
  const [save, setSave] = useState<SaveState>(unsavedDraft !== null ? "saving" : "idle");
  const [getError, setGetError] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  const [copied, setCopied] = useState(false);
  const areaRef = useRef<HTMLTextAreaElement>(null);

  const saveNow = (v: string) => {
    const seq = ++draftSeq;
    unsavedDraft = v;
    setSave("saving");
    call("draft_set", { text: v })
      .then(() => {
        if (unsavedDraft === v) unsavedDraft = null;
        if (seq === draftSeq) setSave("saved");
      })
      .catch(() => {
        if (seq === draftSeq) setSave("error");
      });
  };
  const saveRef = useRef(saveNow);
  saveRef.current = saveNow;

  useEffect(() => {
    if (status !== "open") return;
    let alive = true;
    call("draft_get")
      .then((r) => {
        if (!alive) return;
        setGetError(null);
        setText((cur) => cur ?? r.text);
        // связь вернулась, а последняя правка так и не сохранилась — отправляем снова
        if (unsavedDraft !== null) saveRef.current(unsavedDraft);
      })
      .catch((e: unknown) => {
        if (alive) setGetError(errorText(e));
      });
    return () => {
      alive = false;
    };
  }, [status, tick]);

  const persist = useDebounced((v: string) => saveNow(v), 700);

  // Уходим со вкладки с несохранённым текстом (прошлая попытка упала) — ещё одна попытка;
  // если и она не удастся, текст останется в unsavedDraft до возвращения.
  useEffect(
    () => () => {
      const v = unsavedDraft;
      if (v === null) return;
      call("draft_set", { text: v })
        .then(() => {
          if (unsavedDraft === v) unsavedDraft = null;
        })
        .catch(() => {});
    },
    [],
  );

  const onChange = (v: string) => {
    setText(v);
    unsavedDraft = v;
    setSave("saving");
    persist.call(v);
  };

  const words = text ? countWords(text) : 0;

  return (
    <div>
      <div className="mb-4 flex items-center gap-3 rounded-xl border border-accent/25 bg-accent-soft px-4 py-3 text-[13px] text-fg">
        <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-accent text-accent-fg">
          <Mic className="size-3.5" />
        </span>
        <span className="flex flex-wrap items-center gap-1.5">
          {t("history.draft.hint")}
          {state && <KeyCombo keys={[state.hotkeys.ptt]} />}
        </span>
      </div>
      <Card padded={false} className="overflow-hidden">
        <div className="flex items-center gap-3 border-b border-line px-4 py-2.5">
          <span className="text-[12.5px] text-muted tabular">
            {tp("units.words", words)} · {tp("units.chars", text?.length ?? 0)}
          </span>
          <AnimatePresence mode="wait">
            <motion.span
              key={save}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
              className={cn(
                "inline-flex items-center gap-1 text-[12px]",
                save === "error" ? "text-danger" : "text-subtle",
              )}
            >
              {save === "saving" && t("history.draft.saving")}
              {save === "saved" && (
                <>
                  <Check className="size-3.5 text-success" />
                  {t("history.draft.saved")}
                </>
              )}
              {save === "error" && (
                <>
                  {t("history.draft.saveError")}
                  <button
                    type="button"
                    onClick={() => text !== null && saveNow(text)}
                    className="ml-1 font-semibold underline-offset-2 hover:underline"
                  >
                    {t("common.retry")}
                  </button>
                </>
              )}
            </motion.span>
          </AnimatePresence>
          <div className="ml-auto flex items-center gap-1.5">
            <Button
              size="sm"
              variant="ghost"
              icon={<Eraser className="size-4" />}
              disabled={!text}
              onClick={async () => {
                const ok = await confirm({
                  title: t("history.draft.clearTitle"),
                  body: t("history.draft.clearBody"),
                  confirmLabel: t("history.draft.clear"),
                  danger: true,
                });
                if (ok) onChange("");
              }}
            >
              {t("history.draft.clear")}
            </Button>
            <Button
              size="sm"
              variant="soft"
              icon={copied ? <Check className="size-4" /> : <Copy className="size-4" />}
              disabled={!text}
              onClick={async () => {
                try {
                  await copyText(text ?? "");
                  setCopied(true);
                  setTimeout(() => setCopied(false), 1400);
                } catch (e) {
                  toast({ tone: "error", title: t("errors.copy"), description: errorText(e) });
                }
              }}
            >
              {copied ? t("common.copied") : t("history.draft.copyAll")}
            </Button>
          </div>
        </div>
        {text === null && getError ? (
          <LoadError compact error={getError} onRetry={() => setTick((x) => x + 1)} />
        ) : text === null ? (
          <div className="space-y-2.5 p-5">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-full" />
            <Skeleton className="h-4 w-5/6" />
          </div>
        ) : (
          <Textarea
            ref={areaRef}
            value={text}
            onChange={(e) => onChange(e.target.value)}
            onBlur={() => persist.flush()}
            placeholder={t("history.draft.placeholder")}
            spellCheck
            className="min-h-[440px] rounded-none border-0 bg-transparent px-5 py-4 text-[15px] leading-[1.7] hover:border-0 focus:bg-transparent focus:shadow-none"
          />
        )}
      </Card>
    </div>
  );
}
