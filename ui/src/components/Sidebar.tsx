import { motion } from "framer-motion";
import {
  AppWindow,
  BookA,
  CircleQuestionMark,
  Clock,
  Cpu,
  House,
  Pause,
  Play,
  Settings,
  ShieldCheck,
  Sparkles,
  Speech,
  TextQuote,
  WandSparkles,
  Zap,
} from "lucide-react";
import type { ReactNode } from "react";
import { t, type TKey } from "../i18n";
import { cn } from "../lib/cn";
import { errorText } from "../lib/hub";
import type { Route } from "../lib/router";
import { useCore } from "../lib/store";
import type { ConnStatus, CoreState } from "../lib/types";
import { backend, call } from "../lib/ws";
import { Logo } from "./Logo";
import { Badge } from "./ui/display";
import { Tooltip } from "./ui/overlay";
import { useToast } from "./ui/toast";

const NAV: { route: Route; label: TKey; icon: ReactNode }[] = [
  { route: "home", label: "nav.home", icon: <House /> },
  { route: "history", label: "nav.history", icon: <Clock /> },
  { route: "speech", label: "nav.speech", icon: <Speech /> },
  { route: "dictionary", label: "nav.dictionary", icon: <BookA /> },
  { route: "snippets", label: "nav.snippets", icon: <TextQuote /> },
  { route: "styles", label: "nav.styles", icon: <WandSparkles /> },
  { route: "apps", label: "nav.apps", icon: <AppWindow /> },
  { route: "settings", label: "nav.settings", icon: <Settings /> },
  { route: "help", label: "nav.help", icon: <CircleQuestionMark /> },
];

export type StatusTone = "ok" | "rec" | "hands" | "busy" | "paused" | "loading" | "error" | "offline";

export function statusOf(conn: ConnStatus, state: CoreState | null): { tone: StatusTone; label: string } {
  if (conn !== "open" || !state) return { tone: "offline", label: t("state.offline") };
  switch (state.state) {
    case "idle":
      return { tone: "ok", label: t("state.idle") };
    case "recording":
      return { tone: "rec", label: t("state.recording") };
    case "hands_free":
      return { tone: "hands", label: t("state.hands_free") };
    case "processing":
      return { tone: "busy", label: t("state.processing") };
    case "ai":
      return { tone: "busy", label: t("state.ai") };
    case "paused":
      return { tone: "paused", label: t("state.paused") };
    case "loading":
      return { tone: "loading", label: t("state.loading") };
    case "error":
      return { tone: "error", label: t("state.error") };
  }
}

const DOT: Record<StatusTone, string> = {
  ok: "bg-success",
  rec: "bg-danger",
  hands: "bg-accent",
  busy: "bg-warn",
  paused: "bg-subtle",
  loading: "bg-info",
  error: "bg-danger",
  offline: "bg-subtle",
};

export function StatusDot({ tone, className }: { tone: StatusTone; className?: string }) {
  const pulse = tone === "rec" || tone === "hands" || tone === "busy" || tone === "loading";
  return (
    <span className={cn("relative inline-flex size-2", className)}>
      {pulse && (
        <motion.span
          className={cn("absolute inset-0 rounded-full", DOT[tone])}
          animate={{ scale: [1, 2.4], opacity: [0.55, 0] }}
          transition={{ duration: 1.3, repeat: Infinity, ease: "easeOut" }}
        />
      )}
      <span className={cn("relative inline-flex size-2 rounded-full", DOT[tone])} />
    </span>
  );
}

export function Sidebar({ route, onNavigate }: { route: Route; onNavigate: (r: Route) => void }) {
  const { status, state, setState } = useCore();
  const toast = useToast();
  const st = statusOf(status, state);
  const live = state?.state === "recording" || state?.state === "hands_free";

  const togglePause = async () => {
    if (!state) return;
    try {
      const s = await call("pause", { on: !state.paused });
      setState(s);
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  return (
    <aside className="flex w-[236px] shrink-0 flex-col px-3 pb-3 pt-4">
      <div className="flex items-center gap-2.5 px-2 pb-6">
        <Logo live={live} />
        <div className="leading-tight">
          <div className="text-[15px] font-semibold tracking-[-0.01em] text-fg">{t("app.name")}</div>
          <div className="text-[11.5px] text-subtle">{t("app.tagline")}</div>
        </div>
      </div>

      <nav className="flex flex-col gap-0.5">
        {NAV.map((item) => {
          const active = item.route === route;
          return (
            <button
              key={item.route}
              type="button"
              onClick={() => onNavigate(item.route)}
              className={cn(
                "group relative flex h-9 items-center gap-3 rounded-[10px] px-2.5 text-left text-[13.5px] font-medium transition-colors duration-150",
                active ? "text-fg" : "text-muted hover:text-fg",
              )}
            >
              {active && (
                <motion.span
                  layoutId="nav-active"
                  className="absolute inset-0 rounded-[10px] border border-line bg-surface shadow-card"
                  transition={{ type: "spring", stiffness: 520, damping: 42 }}
                />
              )}
              {!active && (
                <span className="absolute inset-0 rounded-[10px] bg-surface-2/0 transition-colors duration-150 group-hover:bg-fg/[0.04]" />
              )}
              <span
                className={cn(
                  "relative flex [&>svg]:size-[18px] [&>svg]:stroke-[1.8]",
                  active ? "text-accent-text" : "text-subtle group-hover:text-muted",
                )}
              >
                {item.icon}
              </span>
              <span className="relative">{t(item.label)}</span>
            </button>
          );
        })}
      </nav>

      <div className="mt-auto space-y-2">
        {backend.mock && (
          <button
            type="button"
            onClick={() => backend.simulateDictation?.()}
            className="flex w-full items-center gap-2 rounded-[10px] border border-dashed border-line-strong px-3 py-2 text-left text-[12px] text-muted transition-colors hover:border-accent/50 hover:text-accent-text"
          >
            <Sparkles className="size-3.5 shrink-0" />
            <span className="truncate">{t("mock.simulate")}</span>
          </button>
        )}
        <div className="rounded-xl border border-line bg-surface/70 p-3 shadow-card">
          <div className="flex items-center justify-between gap-2">
            <div className="flex min-w-0 items-center gap-2.5">
              <StatusDot tone={st.tone} />
              <span className="truncate text-[13px] font-medium text-fg">{st.label}</span>
            </div>
            {state && (
              <Tooltip content={state.paused ? t("status.resume") : t("status.pause")}>
                <button
                  type="button"
                  onClick={togglePause}
                  aria-label={state.paused ? t("status.resume") : t("status.pause")}
                  className={cn(
                    "flex size-7 items-center justify-center rounded-lg transition-colors",
                    state.paused ? "bg-accent text-accent-fg" : "text-muted hover:bg-surface-2 hover:text-fg",
                  )}
                >
                  {state.paused ? <Play className="size-3.5" /> : <Pause className="size-3.5" />}
                </button>
              </Tooltip>
            )}
          </div>
          {state && (
            <div className="mt-2.5 flex flex-wrap gap-1.5">
              <Badge
                tone={state.device === "cuda" ? "success" : state.device === "cpu" ? "warn" : "neutral"}
                icon={state.device === "cuda" ? <Zap /> : <Cpu />}
              >
                {state.device === "cuda" ? t("status.gpu") : state.device === "cpu" ? t("status.cpu") : t("status.noModel")}
              </Badge>
              <Badge tone={state.ai_enabled ? "accent" : "neutral"} icon={<Sparkles />}>
                {state.ai_enabled ? t("status.aiOn") : t("status.aiOff")}
              </Badge>
              {state.elevated && (
                <Badge tone="info" icon={<ShieldCheck />}>
                  {t("status.admin")}
                </Badge>
              )}
            </div>
          )}
        </div>
      </div>
    </aside>
  );
}
