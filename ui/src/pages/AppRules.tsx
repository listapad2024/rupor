import { AnimatePresence, motion } from "framer-motion";
import { AppWindow, Check, CodeXml, Mail, Plus, RefreshCw, Send, Trash, WandSparkles } from "lucide-react";
import { useState, type ReactNode } from "react";
import { AppIcon } from "../components/AppIcon";
import { TitleRulesBlock } from "../components/TitleRules";
import { Button, IconButton, Input } from "../components/ui/controls";
import { Card, EmptyState, LoadError, PageHeader, SectionTitle, Skeleton, Spinner } from "../components/ui/display";
import { Modal } from "../components/ui/overlay";
import { Select, type SelectOption } from "../components/ui/select";
import { useToast } from "../components/ui/toast";
import { t } from "../i18n";
import { cn } from "../lib/cn";
import { appName } from "../lib/format";
import { useLoad } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { useCore } from "../lib/store";
import type { AppRule, InsertMode } from "../lib/types";
import { call } from "../lib/ws";
import { STYLE_ICONS } from "./Styles";

interface Preset {
  id: "telegram" | "outlook" | "code";
  icon: ReactNode;
  processes: string[];
  style: string;
}

const PRESETS: Preset[] = [
  { id: "telegram", icon: <Send />, processes: ["telegram.exe"], style: "messenger" },
  { id: "outlook", icon: <Mail />, processes: ["outlook.exe", "olk.exe"], style: "business" },
  { id: "code", icon: <CodeXml />, processes: ["cursor.exe", "code.exe", "code - insiders.exe"], style: "code" },
];

export function AppRulesPage() {
  const { styles } = useCore();
  const toast = useToast();
  const rules = useLoad(() => call("app_rules_list"));
  const [adding, setAdding] = useState(false);

  const upsert = async (rule: AppRule): Promise<boolean> => {
    const prev = rules.data;
    rules.setData((xs) => {
      const arr = xs ?? [];
      const idx = arr.findIndex((r) => r.process === rule.process);
      if (idx < 0) return [...arr, rule];
      const copy = [...arr];
      copy[idx] = rule;
      return copy;
    });
    try {
      await call("app_rule_upsert", rule);
      return true;
    } catch (e) {
      rules.setData(() => prev);
      toast({ tone: "error", title: t("errors.save"), description: errorText(e) });
      return false;
    }
  };

  const remove = async (process: string) => {
    const prev = rules.data;
    rules.setData((xs) => (xs ?? []).filter((r) => r.process !== process));
    try {
      await call("app_rule_delete", { process });
    } catch (e) {
      rules.setData(() => prev);
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  const has = (p: string) => (rules.data ?? []).some((r) => r.process.toLowerCase() === p.toLowerCase());
  const presetApplied = (p: Preset) => p.processes.every((x) => has(x));

  const applyPreset = async (p: Preset) => {
    for (const proc of p.processes) {
      const existing = rules.data?.find((r) => r.process === proc);
      await upsert({ process: proc, insert_mode: existing?.insert_mode ?? null, style: p.style });
    }
  };

  const modeOptions: SelectOption<InsertMode | null>[] = [
    { value: null, label: t("apps.defaultValue") },
    { value: "auto", label: t("insertModes.auto") },
    { value: "type", label: t("insertModes.type") },
    { value: "clipboard", label: t("insertModes.clipboard") },
  ];
  const styleOptions: SelectOption<string | null>[] = [
    { value: null, label: t("apps.defaultValue") },
    ...styles.map((s) => ({ value: s.id, label: s.name, icon: STYLE_ICONS[s.id] })),
  ];

  const list = [...(rules.data ?? [])].sort((a, b) => a.process.localeCompare(b.process));

  return (
    <div>
      <PageHeader
        title={t("apps.title")}
        subtitle={t("apps.subtitle")}
        actions={
          <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
            {t("apps.add")}
          </Button>
        }
      />

      <SectionTitle
        hint={t("apps.presets.hint")}
        right={
          <Button
            size="sm"
            variant="soft"
            icon={<WandSparkles className="size-4" />}
            disabled={!rules.data || PRESETS.every(presetApplied)}
            onClick={async () => {
              for (const p of PRESETS) if (!presetApplied(p)) await applyPreset(p);
              toast({ tone: "success", title: t("apps.presets.appliedAll") });
            }}
          >
            {t("apps.presets.applyAll")}
          </Button>
        }
      >
        {t("apps.presets.title")}
      </SectionTitle>
      <div className="grid grid-cols-3 gap-3">
        {PRESETS.map((p) => {
          const applied = presetApplied(p);
          return (
            <Card key={p.id} className="flex flex-col p-4">
              <div className="flex items-center gap-2.5">
                <span className="flex size-8 items-center justify-center rounded-lg bg-accent-soft text-accent-text [&>svg]:size-4">
                  {p.icon}
                </span>
                <div className="text-[14px] font-semibold text-fg">{t(`apps.presets.${p.id}.title`)}</div>
              </div>
              <div className="mt-2 flex-1 text-[12.5px] leading-relaxed text-muted">{t(`apps.presets.${p.id}.text`)}</div>
              <div className="mt-3">
                <Button
                  size="sm"
                  variant={applied ? "ghost" : "secondary"}
                  disabled={applied || !rules.data}
                  icon={applied ? <Check className="size-4 text-success" /> : <Plus className="size-4" />}
                  onClick={() => void applyPreset(p)}
                >
                  {applied ? t("apps.presets.applied") : t("apps.presets.apply")}
                </Button>
              </div>
            </Card>
          );
        })}
      </div>

      <SectionTitle className="mt-9">{t("apps.rulesTitle")}</SectionTitle>
      {rules.loading && !rules.data ? (
        <Card padded={false}>
          {[0, 1, 2].map((i) => (
            <div key={i} className="flex items-center gap-4 border-b border-line px-5 py-3.5 last:border-0">
              <Skeleton className="size-8" />
              <Skeleton className="h-4 w-40" />
              <Skeleton className="ml-auto h-8 w-44" />
              <Skeleton className="h-8 w-44" />
            </div>
          ))}
        </Card>
      ) : rules.error && !rules.data ? (
        <Card>
          <LoadError compact error={rules.error} onRetry={rules.reload} />
        </Card>
      ) : list.length === 0 ? (
        <Card>
          <EmptyState
            compact
            icon={<AppWindow />}
            title={t("apps.empty.title")}
            text={t("apps.empty.text")}
            action={
              <Button variant="secondary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
                {t("apps.add")}
              </Button>
            }
          />
        </Card>
      ) : (
        <Card padded={false} className="overflow-hidden">
          <div className="grid grid-cols-[1fr_200px_220px_36px] items-center gap-3 border-b border-line bg-surface-2/60 px-5 py-2.5 text-[12px] font-medium text-subtle">
            <span>{t("apps.cols.app")}</span>
            <span>{t("apps.cols.mode")}</span>
            <span>{t("apps.cols.style")}</span>
            <span />
          </div>
          <AnimatePresence initial={false}>
            {list.map((r) => (
              <motion.div
                key={r.process}
                layout="position"
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.18 }}
                className="border-b border-line last:border-b-0"
              >
                <div className="grid grid-cols-[1fr_200px_220px_36px] items-center gap-3 px-5 py-3">
                  <div className="flex min-w-0 items-center gap-3">
                    <AppIcon process={r.process} size={30} />
                    <div className="min-w-0">
                      <div className="truncate text-[13.5px] font-medium text-fg">{appName(r.process)}</div>
                      <div className="truncate font-mono text-[11.5px] text-subtle">{r.process}</div>
                    </div>
                  </div>
                  <Select
                    size="sm"
                    value={r.insert_mode}
                    options={modeOptions}
                    onChange={(v) => void upsert({ ...r, insert_mode: v })}
                  />
                  <Select
                    size="sm"
                    value={r.style}
                    options={styleOptions}
                    onChange={(v) => void upsert({ ...r, style: v })}
                  />
                  <IconButton label={t("common.delete")} size="sm" tone="danger" onClick={() => void remove(r.process)}>
                    <Trash className="size-3.5" />
                  </IconButton>
                </div>
              </motion.div>
            ))}
          </AnimatePresence>
        </Card>
      )}

      <TitleRulesBlock />

      <AddAppDialog
        open={adding}
        exists={has}
        onClose={() => setAdding(false)}
        onPick={async (process) => {
          const ok = await upsert({ process: process.toLowerCase(), insert_mode: null, style: null });
          if (ok) setAdding(false);
        }}
      />
    </div>
  );
}

function AddAppDialog({
  open,
  onClose,
  onPick,
  exists,
}: {
  open: boolean;
  onClose: () => void;
  onPick: (process: string) => void;
  exists: (p: string) => boolean;
}) {
  const recent = useLoad(() => (open ? call("recent_apps") : Promise.resolve([])), [open]);
  const [manual, setManual] = useState("");

  const submitManual = () => {
    let p = manual.trim();
    if (!p) return;
    if (!/\.exe$/i.test(p)) p += ".exe";
    onPick(p);
    setManual("");
  };

  return (
    <Modal open={open} onClose={onClose} title={t("apps.dialog.title")} width={520}>
      <div className="mb-2 flex items-center justify-between">
        <span className="text-[12.5px] font-medium text-muted">{t("apps.dialog.recent")}</span>
        <IconButton size="sm" label={t("common.refresh")} onClick={recent.reload}>
          <RefreshCw className={cn("size-3.5", recent.loading && "animate-spin")} />
        </IconButton>
      </div>
      <div className="max-h-[300px] overflow-y-auto rounded-xl border border-line">
        {recent.loading && !recent.data ? (
          <div className="flex justify-center py-8">
            <Spinner />
          </div>
        ) : (recent.data ?? []).length === 0 ? (
          <div className="px-4 py-8 text-center text-[13px] text-subtle">{t("apps.dialog.noRecent")}</div>
        ) : (
          (recent.data ?? []).map((a) => {
            const added = exists(a.process);
            return (
              <button
                key={a.process}
                type="button"
                disabled={added}
                onClick={() => onPick(a.process)}
                className="flex w-full items-center gap-3 border-b border-line px-3.5 py-2.5 text-left transition-colors last:border-b-0 hover:bg-surface-2 disabled:opacity-50 focus-visible:-outline-offset-2!"
              >
                <AppIcon process={a.process} size={28} />
                <div className="min-w-0 flex-1">
                  <div className="text-[13.5px] font-medium text-fg">
                    {appName(a.process)} <span className="font-mono text-[11.5px] font-normal text-subtle">{a.process}</span>
                  </div>
                  <div className="truncate text-[12px] text-subtle">{a.title}</div>
                </div>
                {added ? (
                  <Check className="size-4 text-success" />
                ) : (
                  <Plus className="size-4 text-subtle" />
                )}
              </button>
            );
          })
        )}
      </div>
      <div className="mt-5 text-[12.5px] font-medium text-muted">{t("apps.dialog.manual")}</div>
      <div className="mt-2 flex gap-2">
        <Input
          className="flex-1"
          value={manual}
          placeholder={t("apps.dialog.manualPlaceholder")}
          onChange={(e) => setManual(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submitManual()}
        />
        <Button variant="primary" disabled={!manual.trim()} onClick={submitManual}>
          {t("common.add")}
        </Button>
      </div>
    </Modal>
  );
}
