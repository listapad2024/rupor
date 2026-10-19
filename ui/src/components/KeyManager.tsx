// Управление ключом ИИ-провайдера: статус-маска, импорт из .env, ручной ввод, удаление, проверка баланса.
import { AnimatePresence, motion } from "framer-motion";
import { CircleAlert, FileDown, KeyRound, Pencil, ShieldCheck, Trash, Wallet } from "lucide-react";
import { useState } from "react";
import { t } from "../i18n";
import { refreshBalance } from "../lib/aiUsage";
import { invalidate } from "../lib/cache";
import { cn } from "../lib/cn";
import { fmtUsd } from "../lib/format";
import { useLoad } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { useCore } from "../lib/store";
import type { KeyCheck, KeyProvider } from "../lib/types";
import { call } from "../lib/ws";
import { CommitInput } from "./fields";
import { Button, Input } from "./ui/controls";
import { Spinner } from "./ui/display";
import { useConfirm } from "./ui/overlay";
import { useToast } from "./ui/toast";

export function KeyManager({ provider, compact }: { provider: KeyProvider; compact?: boolean }) {
  const { settings, updateSettings } = useCore();
  const toast = useToast();
  const confirm = useConfirm();
  const status = useLoad(() => call("ai_key_status", { provider }), [provider]);
  const [manual, setManual] = useState(false);
  const [keyInput, setKeyInput] = useState("");
  const [busy, setBusy] = useState<null | "import" | "save" | "check" | "delete">(null);
  const [check, setCheck] = useState<KeyCheck | null>(null);

  const isOR = provider === "openrouter";
  const has = Boolean(status.data?.has_key);

  const importEnv = async () => {
    setBusy("import");
    try {
      const r = await call("ai_key_import_env", { path: settings?.env_path });
      if (r.imported) {
        toast({ tone: "success", title: t("key.imported"), description: r.masked ?? undefined });
        status.reload();
        invalidate("ai_key_check");
        if (isOR) void refreshBalance(true);
        setCheck(null);
      } else {
        toast({ tone: "warn", title: t("key.notInEnv") });
      }
    } catch (e) {
      toast({ tone: "error", title: t("key.importError"), description: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  const saveManual = async () => {
    const k = keyInput.trim();
    if (!k) return;
    setBusy("save");
    try {
      await call("ai_key_set", { provider, key: k });
      setKeyInput("");
      setManual(false);
      toast({ tone: "success", title: t("key.saved") });
      status.reload();
      invalidate("ai_key_check");
      if (isOR) void refreshBalance(true);
      setCheck(null);
    } catch (e) {
      toast({ tone: "error", title: t("key.saveError"), description: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: t("key.deleteTitle"),
      body: t("key.deleteBody"),
      confirmLabel: t("common.delete"),
      danger: true,
    });
    if (!ok) return;
    setBusy("delete");
    try {
      await call("ai_key_delete", { provider });
      status.reload();
      invalidate("ai_key_check");
      if (isOR) void refreshBalance(true);
      setCheck(null);
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  const doCheck = async () => {
    setBusy("check");
    try {
      invalidate("ai_key_check");
      setCheck(await call("ai_key_check", undefined, { timeoutMs: 30_000 }));
    } catch (e) {
      setCheck({ valid: false, label: null, balance_usd: null, usage_usd: null, limit_usd: null, error: errorText(e) });
    } finally {
      setBusy(null);
    }
  };

  return (
    <div className={cn("space-y-3", compact && "space-y-2.5")}>
      <div className="flex items-center gap-3 rounded-xl border border-line bg-surface-2/60 px-3.5 py-3">
        <span
          className={cn(
            "flex size-8 shrink-0 items-center justify-center rounded-lg",
            has ? "bg-success/12 text-success" : "bg-surface-3 text-subtle",
          )}
        >
          {has ? <ShieldCheck className="size-4" /> : <KeyRound className="size-4" />}
        </span>
        <div className="min-w-0 flex-1">
          <div className="text-[13.5px] font-medium text-fg">
            {status.loading && !status.data ? (
              <span className="inline-block h-3.5 w-28 align-middle skeleton" />
            ) : has ? (
              t("key.stored")
            ) : (
              t("key.missing")
            )}
          </div>
          <div className={cn("truncate text-[12px] text-subtle", has && "font-mono")}>
            {has ? status.data?.masked : t("key.storeHint")}
          </div>
        </div>
        <div className="flex shrink-0 gap-1.5">
          {isOR && has && (
            <Button size="sm" variant="soft" icon={<Wallet className="size-4" />} loading={busy === "check"} onClick={() => void doCheck()}>
              {t("key.check")}
            </Button>
          )}
          <Button size="sm" variant="secondary" icon={<Pencil className="size-3.5" />} onClick={() => setManual((v) => !v)}>
            {has ? t("key.replace") : t("key.enter")}
          </Button>
          {has && (
            <Button size="sm" variant="ghost" icon={<Trash className="size-3.5" />} loading={busy === "delete"} onClick={() => void remove()}>
              {t("common.delete")}
            </Button>
          )}
        </div>
      </div>

      <AnimatePresence initial={false}>
        {check && (
          <motion.div
            key="check"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            {check.valid ? (
              <div className="grid grid-cols-3 gap-2">
                <Stat label={t("key.balance")} value={check.balance_usd !== null ? fmtUsd(check.balance_usd) : "—"} strong />
                <Stat label={t("key.usage")} value={check.usage_usd !== null ? fmtUsd(check.usage_usd) : "—"} />
                <Stat label={t("key.limit")} value={check.limit_usd !== null ? fmtUsd(check.limit_usd) : t("key.noLimit")} />
              </div>
            ) : (
              <div className="flex items-start gap-2 rounded-xl bg-danger/8 px-3.5 py-2.5 text-[13px] text-danger">
                <CircleAlert className="mt-0.5 size-4 shrink-0" />
                {check.error ?? t("key.invalid")}
              </div>
            )}
          </motion.div>
        )}
        {manual && (
          <motion.div
            key="manual"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <div className="flex gap-2 p-1">
              <Input
                className="flex-1"
                type="password"
                autoFocus
                autoComplete="off"
                spellCheck={false}
                value={keyInput}
                placeholder={isOR ? t("key.placeholderOpenrouter") : t("key.placeholderAnthropic")}
                onChange={(e) => setKeyInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && void saveManual()}
              />
              <Button variant="primary" loading={busy === "save"} disabled={!keyInput.trim()} onClick={() => void saveManual()}>
                {t("common.save")}
              </Button>
            </div>
            <div className="mt-1.5 text-[12px] text-subtle">{t("key.manualHint")}</div>
          </motion.div>
        )}
      </AnimatePresence>

      {isOR && settings && (
        <div>
          <div className="mb-1.5 text-[12.5px] font-medium text-muted">{t("key.envTitle")}</div>
          <div className="flex gap-2">
            <CommitInput
              className="flex-1"
              mono
              value={settings.env_path}
              placeholder={t("key.envPlaceholder")}
              onCommit={(v) => void updateSettings({ env_path: v })}
            />
            <Button
              variant="secondary"
              icon={busy === "import" ? <Spinner /> : <FileDown className="size-4" />}
              disabled={busy === "import"}
              onClick={() => void importEnv()}
            >
              {t("key.importEnv")}
            </Button>
          </div>
          <div className="mt-1.5 text-[12px] text-subtle">{t("key.envHint")}</div>
        </div>
      )}
    </div>
  );
}

function Stat({ label, value, strong }: { label: string; value: string; strong?: boolean }) {
  return (
    <div className={cn("rounded-xl border px-3.5 py-2.5", strong ? "border-success/30 bg-success/8" : "border-line bg-surface-2/60")}>
      <div className="text-[11.5px] text-muted">{label}</div>
      <div className={cn("mt-0.5 text-[16px] font-semibold tabular", strong ? "text-success" : "text-fg")}>{value}</div>
    </div>
  );
}
