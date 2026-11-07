// Список ключей ИИ по приоритету (Настройки → ИИ): порядок перетаскиванием и стрелками,
// вкл/выкл, подпись, замена и удаление ключа, баланс по каждому ключу. OpenRouter отдаёт
// баланс по API; Anthropic — нет, поэтому для него можно ввести остаток вручную, и ядро
// вычитает из него свой расход через этот ключ (оценка).
import { AnimatePresence, motion, Reorder, useDragControls } from "framer-motion";
import {
  ArrowDown,
  ArrowUp,
  CircleAlert,
  GripVertical,
  KeyRound,
  MoreHorizontal,
  Pencil,
  Plus,
  Power,
  RefreshCw,
  Trash,
  Wallet,
} from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { t } from "../i18n";
import { refreshBalance } from "../lib/aiUsage";
import { cn } from "../lib/cn";
import { fmtAgo, fmtSpend, fmtTime, fmtUsd } from "../lib/format";
import { useLoad, useNow } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { useCore } from "../lib/store";
import type { AiKeyInfo, AiKeyItem, KeyProvider } from "../lib/types";
import { call } from "../lib/ws";
import { Button, IconButton, Input, Segmented, Switch } from "./ui/controls";
import { Badge } from "./ui/display";
import { Menu } from "./ui/menu";
import { useConfirm } from "./ui/overlay";
import { useToast } from "./ui/toast";

const PROVIDER_NAME: Record<KeyProvider, string> = { openrouter: "OpenRouter", anthropic: "Anthropic" };
const LOW_BALANCE = 1;

type Edit = { id: string; kind: "label" | "balance" | "key" } | null;

/** Сумма из поля ввода: «12,5» → 12.5; пусто → null; мусор → undefined. */
function parseUsd(v: string): number | null | undefined {
  const s = v.trim().replace(",", ".").replace(/^\$/, "");
  if (!s) return null;
  const n = Number(s);
  return Number.isFinite(n) && n >= 0 && n <= 1_000_000 ? n : undefined;
}

export function KeyChain() {
  const { settings } = useCore();
  const toast = useToast();
  const list = useLoad(() => call("ai_keys_list"), []);
  const [order, setOrder] = useState<AiKeyItem[]>([]);
  const [info, setInfo] = useState<Record<string, AiKeyInfo | "loading">>({});
  const [edit, setEdit] = useState<Edit>(null);
  const [adding, setAdding] = useState(false);
  const dragging = useRef(false);

  // Список меняется и из других мест (мастер, импорт .env, другое окно) — по событию settings
  const keysSig = JSON.stringify(settings?.ai_keys ?? []);
  useEffect(() => {
    if (list.data) list.reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [keysSig]);
  useEffect(() => {
    if (list.data && !dragging.current) setOrder(list.data);
  }, [list.data]);

  const loadInfo = (id: string, force = false) => {
    setInfo((m) => ({ ...m, [id]: "loading" }));
    call("ai_key_info", { id, force }, { timeoutMs: 30_000 })
      .then((r) => setInfo((m) => ({ ...m, [id]: r })))
      .catch((e: unknown) =>
        setInfo((m) => {
          const prev = m[id];
          const base = prev && prev !== "loading" ? prev : null;
          return {
            ...m,
            [id]: {
              ...(base ?? {
                id,
                provider: "openrouter",
                valid: false,
                balance_usd: null,
                balance_source: null,
                usage_usd: null,
                limit_usd: null,
                spent_usd: 0,
                calls: 0,
                spent_since_usd: null,
                checked_at: null,
              }),
              error: errorText(e),
            },
          };
        }),
      );
  };

  // Баланс — по каждому ключу с секретом (ядро кэширует ответ на 5 минут)
  const ids = (list.data ?? []).filter((k) => k.has_key).map((k) => `${k.id}:${k.balance_usd ?? ""}`).join(",");
  useEffect(() => {
    for (const k of list.data ?? []) if (k.has_key) loadInfo(k.id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ids]);

  const afterChange = () => {
    list.reload();
    void refreshBalance(true);
  };

  const commitOrder = async (next: AiKeyItem[]) => {
    const prev = list.data ?? [];
    if (next.map((k) => k.id).join() === prev.map((k) => k.id).join()) return;
    try {
      const r = await call("ai_keys_reorder", { ids: next.map((k) => k.id) });
      list.setData(() => r);
      void refreshBalance(true);
    } catch (e) {
      setOrder(prev);
      toast({ tone: "error", title: t("aiKeys.saveError"), description: errorText(e) });
    }
  };

  const move = (i: number, d: -1 | 1) => {
    const next = [...order];
    const j = i + d;
    if (j < 0 || j >= next.length) return;
    [next[i], next[j]] = [next[j] as AiKeyItem, next[i] as AiKeyItem];
    setOrder(next);
    void commitOrder(next);
  };

  const onlyProvider = settings?.ai_provider === "openrouter" || settings?.ai_provider === "anthropic" ? settings.ai_provider : null;

  return (
    <div className="space-y-2">
      {list.loading && !list.data ? (
        <div className="h-[58px] rounded-xl skeleton" />
      ) : order.length === 0 ? (
        <div className="flex items-start gap-3 rounded-xl border border-dashed border-line-strong px-4 py-3.5">
          <KeyRound className="mt-0.5 size-4 shrink-0 text-subtle" />
          <div>
            <div className="text-[13.5px] font-medium text-fg">{t("aiKeys.empty")}</div>
            <div className="text-[12.5px] text-muted">{t("aiKeys.emptyHint")}</div>
          </div>
        </div>
      ) : (
        <Reorder.Group axis="y" values={order} onReorder={setOrder} className="space-y-2">
          {order.map((k, i) => (
            <KeyRow
              key={k.id}
              item={k}
              index={i}
              count={order.length}
              info={info[k.id]}
              dimmed={onlyProvider !== null && k.provider !== onlyProvider}
              edit={edit?.id === k.id ? edit.kind : null}
              setEdit={(kind) => setEdit(kind ? { id: k.id, kind } : null)}
              onMove={(d) => move(i, d)}
              onDragStart={() => (dragging.current = true)}
              onDragEnd={() => {
                dragging.current = false;
                void commitOrder(order);
              }}
              onRefresh={() => loadInfo(k.id, true)}
              onChanged={afterChange}
            />
          ))}
        </Reorder.Group>
      )}

      <AnimatePresence initial={false}>
        {adding ? (
          <motion.div
            key="add"
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            className="overflow-hidden"
          >
            <AddKeyForm
              onDone={(added) => {
                setAdding(false);
                if (added) {
                  toast({ tone: "success", title: t("aiKeys.added"), description: added.title });
                  afterChange();
                }
              }}
            />
          </motion.div>
        ) : (
          <motion.div key="addBtn" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}>
            <Button size="sm" variant="secondary" icon={<Plus className="size-4" />} onClick={() => setAdding(true)}>
              {t("aiKeys.add")}
            </Button>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

function KeyRow({
  item,
  index,
  count,
  info,
  dimmed,
  edit,
  setEdit,
  onMove,
  onDragStart,
  onDragEnd,
  onRefresh,
  onChanged,
}: {
  item: AiKeyItem;
  index: number;
  count: number;
  info: AiKeyInfo | "loading" | undefined;
  dimmed: boolean;
  edit: "label" | "balance" | "key" | null;
  setEdit: (kind: "label" | "balance" | "key" | null) => void;
  onMove: (d: -1 | 1) => void;
  onDragStart: () => void;
  onDragEnd: () => void;
  onRefresh: () => void;
  onChanged: () => void;
}) {
  const toast = useToast();
  const confirm = useConfirm();
  const drag = useDragControls();
  const now = useNow(30_000);
  const [busy, setBusy] = useState(false);
  const loaded = info && info !== "loading" ? info : null;
  const paused = item.paused_until !== null && item.paused_until * 1000 > now;

  const update = async (args: { label?: string; enabled?: boolean; balance_usd?: number | null }) => {
    setBusy(true);
    try {
      await call("ai_key_update", { id: item.id, ...args });
      setEdit(null);
      onChanged();
    } catch (e) {
      toast({ tone: "error", title: t("aiKeys.saveError"), description: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    const ok = await confirm({
      title: t("aiKeys.removeTitle", { name: item.title }),
      body: t("aiKeys.removeBody"),
      confirmLabel: t("aiKeys.remove"),
      danger: true,
    });
    if (!ok) return;
    try {
      await call("ai_key_remove", { id: item.id });
      onChanged();
    } catch (e) {
      toast({ tone: "error", title: t("aiKeys.saveError"), description: errorText(e) });
    }
  };

  // Баланс: OpenRouter — по API; Anthropic — оценка по введённому остатку или «недоступно»
  let balance: { value: string; label: string; tone: "ok" | "low" | "muted" } | null = null;
  if (loaded?.balance_usd !== null && loaded?.balance_usd !== undefined) {
    balance = {
      value: fmtUsd(loaded.balance_usd),
      label: loaded.balance_source === "estimate" ? t("aiKeys.balanceEstimate") : t("aiKeys.balanceApi"),
      tone: loaded.balance_usd < LOW_BALANCE ? "low" : "ok",
    };
  } else if (loaded && loaded.valid && item.provider === "anthropic") {
    balance = { value: "—", label: t("aiKeys.balanceUnavailable"), tone: "muted" };
  }
  const error = loaded && !loaded.valid ? (loaded.error ?? t("aiKeys.invalid")) : null;

  return (
    <Reorder.Item
      value={item}
      dragListener={false}
      dragControls={drag}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      className="list-none"
    >
      <div
        className={cn(
          "rounded-xl border bg-surface-2/60 transition-opacity",
          index === 0 && item.enabled && !dimmed ? "border-accent/40" : "border-line",
          (!item.enabled || dimmed) && "opacity-60",
        )}
      >
        <div className="flex items-center gap-2.5 px-2.5 py-2.5">
          <button
            type="button"
            aria-label={t("aiKeys.drag")}
            title={t("aiKeys.drag")}
            onPointerDown={(e) => drag.start(e)}
            className="flex size-7 shrink-0 cursor-grab touch-none items-center justify-center rounded-md text-subtle hover:bg-surface-3 hover:text-fg active:cursor-grabbing"
          >
            <GripVertical className="size-4" />
          </button>
          <span className="w-4 shrink-0 text-center text-[12px] font-semibold tabular text-subtle">{index + 1}</span>
          <div className="min-w-0 flex-1">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="truncate text-[13.5px] font-medium text-fg">{item.title}</span>
              {item.label && <Badge>{PROVIDER_NAME[item.provider]}</Badge>}
              {index === 0 && item.enabled && !dimmed && <Badge tone="accent">{t("aiKeys.first")}</Badge>}
              {!item.enabled && <Badge>{t("aiKeys.off")}</Badge>}
              {!item.has_key && <Badge tone="danger">{t("aiKeys.noSecret")}</Badge>}
              {paused && item.paused_until !== null && (
                <Badge tone="warn">
                  {t("aiKeys.paused", {
                    time: fmtTime(item.paused_until),
                    reason: item.paused_reason ? t(`aiKeys.reasons.${item.paused_reason}` as never) : "",
                  })}
                </Badge>
              )}
            </div>
            <div className="mt-0.5 flex flex-wrap items-center gap-x-2 text-[12px] text-subtle">
              {item.masked && <span className="font-mono">{item.masked}</span>}
              {item.spent_usd > 0 && <span>{t("aiKeys.spent", { v: fmtSpend(item.spent_usd) })}</span>}
              {loaded?.checked_at && <span>{t("aiKeys.checked", { ago: fmtAgo(loaded.checked_at, now) })}</span>}
            </div>
          </div>

          {item.has_key && (
            <div className="min-w-[92px] shrink-0 text-right">
              {info === "loading" && !balance ? (
                <span className="inline-block h-4 w-14 align-middle skeleton" />
              ) : balance ? (
                <>
                  <div
                    className={cn(
                      "text-[15px] font-semibold leading-tight tabular",
                      balance.tone === "ok" && "text-success",
                      balance.tone === "low" && "text-warn",
                      balance.tone === "muted" && "text-subtle",
                    )}
                  >
                    {balance.value}
                  </div>
                  <div className="text-[11px] text-muted">{balance.label}</div>
                </>
              ) : null}
            </div>
          )}

          <div className="flex shrink-0 items-center gap-0.5">
            <IconButton size="sm" label={t("aiKeys.check")} disabled={info === "loading" || !item.has_key} onClick={onRefresh}>
              <RefreshCw className={cn("size-3.5", info === "loading" && "animate-spin")} />
            </IconButton>
            <IconButton size="sm" label={t("aiKeys.moveUp")} disabled={index === 0} onClick={() => onMove(-1)}>
              <ArrowUp className="size-3.5" />
            </IconButton>
            <IconButton size="sm" label={t("aiKeys.moveDown")} disabled={index === count - 1} onClick={() => onMove(1)}>
              <ArrowDown className="size-3.5" />
            </IconButton>
            <Switch size="sm" label={item.enabled ? t("aiKeys.disable") : t("aiKeys.enable")} checked={item.enabled} disabled={busy} onChange={(v) => void update({ enabled: v })} />
            <Menu
              iconOnly
              label={t("aiKeys.actions")}
              icon={<MoreHorizontal className="size-4" />}
              variant="ghost"
              size="sm"
              width={230}
              items={[
                { label: t("aiKeys.rename"), icon: <Pencil />, onSelect: () => setEdit("label") },
                ...(item.provider === "anthropic"
                  ? [
                      {
                        label: item.balance_usd === null ? t("aiKeys.setBalance") : t("aiKeys.editBalance"),
                        icon: <Wallet />,
                        onSelect: () => setEdit("balance"),
                      },
                    ]
                  : []),
                ...(item.balance_usd !== null
                  ? [{ label: t("aiKeys.clearBalance"), icon: <Wallet />, onSelect: () => void update({ balance_usd: null }) }]
                  : []),
                { label: t("aiKeys.replace"), icon: <KeyRound />, onSelect: () => setEdit("key") },
                { label: item.enabled ? t("aiKeys.disable") : t("aiKeys.enable"), icon: <Power />, onSelect: () => void update({ enabled: !item.enabled }) },
                { label: t("aiKeys.remove"), icon: <Trash />, danger: true, onSelect: () => void remove() },
              ]}
            />
          </div>
        </div>

        {error && (
          <div className="mx-2.5 mb-2.5 flex items-start gap-2 rounded-lg bg-danger/8 px-3 py-2 text-[12.5px] text-danger">
            <CircleAlert className="mt-0.5 size-3.5 shrink-0" />
            {error}
          </div>
        )}
        {!error && loaded?.error && loaded.balance_usd === null && item.provider === "anthropic" && item.balance_usd === null && (
          <div className="mx-2.5 mb-2.5 flex items-center justify-between gap-2 rounded-lg bg-surface-3/60 px-3 py-2 text-[12px] text-muted">
            <span>{loaded.error}</span>
            <Button size="sm" variant="soft" icon={<Wallet className="size-3.5" />} onClick={() => setEdit("balance")}>
              {t("aiKeys.setBalance")}
            </Button>
          </div>
        )}
        {item.balance_usd !== null && item.spent_since_usd !== null && item.spent_since_usd > 0 && (
          <div className="mx-2.5 mb-2 text-[11.5px] text-subtle">
            {t("aiKeys.spentSince", { balance: fmtUsd(item.balance_usd), v: fmtSpend(item.spent_since_usd) })}
          </div>
        )}

        <AnimatePresence initial={false}>
          {edit && (
            <motion.div
              key={edit}
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              className="overflow-hidden"
            >
              <InlineEdit
                kind={edit}
                item={item}
                busy={busy}
                onCancel={() => setEdit(null)}
                onLabel={(label) => void update({ label })}
                onBalance={(v) => void update({ balance_usd: v })}
                onKey={async (key) => {
                  setBusy(true);
                  try {
                    await call("ai_key_replace", { id: item.id, key });
                    setEdit(null);
                    onChanged();
                  } catch (e) {
                    toast({ tone: "error", title: t("aiKeys.saveError"), description: errorText(e) });
                  } finally {
                    setBusy(false);
                  }
                }}
              />
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </Reorder.Item>
  );
}

function InlineEdit({
  kind,
  item,
  busy,
  onCancel,
  onLabel,
  onBalance,
  onKey,
}: {
  kind: "label" | "balance" | "key";
  item: AiKeyItem;
  busy: boolean;
  onCancel: () => void;
  onLabel: (v: string) => void;
  onBalance: (v: number | null) => void;
  onKey: (v: string) => void;
}) {
  const [v, setV] = useState(kind === "label" ? item.label : kind === "balance" && item.balance_usd !== null ? String(item.balance_usd) : "");
  const usd = kind === "balance" ? parseUsd(v) : null;
  const valid = kind === "key" ? v.trim().length > 0 : kind === "balance" ? usd !== undefined : true;
  const submit = () => {
    if (!valid || busy) return;
    if (kind === "label") onLabel(v.trim());
    else if (kind === "balance") onBalance(usd ?? null);
    else onKey(v.trim());
  };
  return (
    <div className="border-t border-line px-3 py-2.5">
      <div className="flex gap-2">
        <Input
          className="flex-1"
          type={kind === "key" ? "password" : "text"}
          inputMode={kind === "balance" ? "decimal" : undefined}
          autoFocus
          autoComplete="off"
          spellCheck={false}
          value={v}
          placeholder={
            kind === "label"
              ? t("aiKeys.labelPlaceholder")
              : kind === "balance"
                ? t("aiKeys.balance")
                : item.provider === "openrouter"
                  ? t("key.placeholderOpenrouter")
                  : t("key.placeholderAnthropic")
          }
          onChange={(e) => setV(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") submit();
            if (e.key === "Escape") onCancel();
          }}
        />
        <Button variant="primary" loading={busy} disabled={!valid} onClick={submit}>
          {t("aiKeys.apply")}
        </Button>
        <Button variant="ghost" onClick={onCancel}>
          {t("aiKeys.cancel")}
        </Button>
      </div>
      {kind === "balance" && <div className="mt-1.5 text-[12px] text-subtle">{t("aiKeys.balanceHint")}</div>}
      {kind === "key" && <div className="mt-1.5 text-[12px] text-subtle">{t("key.manualHint")}</div>}
    </div>
  );
}

function AddKeyForm({ onDone }: { onDone: (added: AiKeyItem | null) => void }) {
  const toast = useToast();
  const [provider, setProvider] = useState<KeyProvider>("anthropic");
  const [label, setLabel] = useState("");
  const [key, setKey] = useState("");
  const [bal, setBal] = useState("");
  const [busy, setBusy] = useState(false);
  const usd = parseUsd(bal);
  const valid = key.trim().length > 0 && (provider === "openrouter" || usd !== undefined);

  const submit = async () => {
    if (!valid || busy) return;
    setBusy(true);
    try {
      const r = await call("ai_key_add", {
        provider,
        key: key.trim(),
        label: label.trim(),
        ...(provider === "anthropic" && usd !== null && usd !== undefined ? { balance_usd: usd } : {}),
      });
      setKey("");
      onDone(r);
    } catch (e) {
      toast({ tone: "error", title: t("aiKeys.saveError"), description: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-2.5 rounded-xl border border-line bg-surface-2/60 p-3.5">
      <div className="text-[13.5px] font-semibold text-fg">{t("aiKeys.addTitle")}</div>
      <Segmented
        full
        value={provider}
        onChange={setProvider}
        options={[
          { value: "anthropic", label: "Anthropic" },
          { value: "openrouter", label: "OpenRouter" },
        ]}
      />
      <div className="grid grid-cols-[1fr_1.4fr] gap-2">
        <Input value={label} placeholder={t("aiKeys.labelPlaceholder")} onChange={(e) => setLabel(e.target.value)} aria-label={t("aiKeys.label")} />
        <Input
          type="password"
          autoFocus
          autoComplete="off"
          spellCheck={false}
          value={key}
          aria-label={t("aiKeys.key")}
          placeholder={provider === "openrouter" ? t("key.placeholderOpenrouter") : t("key.placeholderAnthropic")}
          onChange={(e) => setKey(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && void submit()}
        />
      </div>
      {provider === "anthropic" && (
        <div>
          <Input
            inputMode="decimal"
            value={bal}
            aria-label={t("aiKeys.balance")}
            placeholder={`${t("aiKeys.balance")} (${t("aiKeys.balancePlaceholder")})`}
            onChange={(e) => setBal(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && void submit()}
          />
          <div className="mt-1.5 text-[12px] text-subtle">{t("aiKeys.balanceHint")}</div>
        </div>
      )}
      <div className="text-[12px] text-subtle">{t("key.manualHint")}</div>
      <div className="flex justify-end gap-2">
        <Button variant="ghost" onClick={() => onDone(null)}>
          {t("aiKeys.cancel")}
        </Button>
        <Button variant="primary" loading={busy} disabled={!valid} onClick={() => void submit()}>
          {t("aiKeys.save")}
        </Button>
      </div>
    </div>
  );
}
