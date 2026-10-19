import { AnimatePresence, motion } from "framer-motion";
import { Check, CornerDownRight, Mic, Pencil, Plus, Search, SearchX, TextQuote, Trash, X } from "lucide-react";
import { useMemo, useState, type KeyboardEvent } from "react";
import { ImportExport } from "../components/ImportExport";
import { Button, IconButton, Input, Textarea } from "../components/ui/controls";
import { Card, EmptyState, LoadError, PageHeader, Skeleton } from "../components/ui/display";
import { useToast } from "../components/ui/toast";
import { intlTag, t, tp, useLocale } from "../i18n";
import { useLoad } from "../lib/hooks";
import { errorText } from "../lib/hub";
import type { Snippet } from "../lib/types";
import { call } from "../lib/ws";

export function SnippetsPage() {
  const toast = useToast();
  const list = useLoad(() => call("snippets_list"));
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);

  const locale = useLocale();
  const items = useMemo(() => {
    const all = [...(list.data ?? [])].sort((a, b) => a.trigger.localeCompare(b.trigger, intlTag()));
    const q = query.trim().toLowerCase();
    if (!q) return all;
    return all.filter((s) => s.trigger.toLowerCase().includes(q) || s.text.toLowerCase().includes(q));
  }, [list.data, query, locale]);

  const save = async (id: number | undefined, trigger: string, text: string): Promise<boolean> => {
    if (!trigger.trim() || !text.trim()) {
      toast({ tone: "warn", title: t("snippets.required") });
      return false;
    }
    try {
      const saved = await call("snippet_upsert", { ...(id !== undefined ? { id } : {}), trigger: trigger.trim(), text });
      list.setData((xs) => [...(xs ?? []).filter((x) => x.id !== saved.id && x.id !== id), saved]);
      setEditing(null);
      return true;
    } catch (e) {
      toast({ tone: "error", title: t("errors.save"), description: errorText(e) });
      return false;
    }
  };

  const remove = async (s: Snippet) => {
    list.setData((xs) => (xs ?? []).filter((x) => x.id !== s.id));
    try {
      await call("snippet_delete", { id: s.id });
      toast({
        tone: "info",
        title: t("snippets.deleted", { trigger: s.trigger }),
        action: { label: t("common.undo"), onClick: () => void save(undefined, s.trigger, s.text) },
      });
    } catch (e) {
      list.reload();
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  const count = list.data?.length ?? 0;
  const example = list.data?.[0]?.trigger ?? t("snippets.exampleTrigger");

  return (
    <div>
      <PageHeader
        title={t("snippets.title")}
        subtitle={t("snippets.subtitle")}
        actions={
          <>
            <ImportExport onImported={list.reload} exportDisabled={count === 0} />
            <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
              {t("snippets.add")}
            </Button>
          </>
        }
      />

      {count > 0 && (
        <div className="mb-4 flex items-center gap-3 rounded-xl border border-line bg-surface px-4 py-3 text-[13px] text-muted shadow-card">
          <span className="flex size-7 shrink-0 items-center justify-center rounded-lg bg-accent-soft text-accent-text">
            <Mic className="size-3.5" />
          </span>
          <span>{t("snippets.howto", { trigger: example })}</span>
        </div>
      )}

      {count > 3 && (
        <div className="mb-4 flex items-center gap-3">
          <Input
            className="flex-1"
            icon={<Search />}
            placeholder={t("snippets.search")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="shrink-0 text-[12.5px] text-subtle">{tp("units.snippets", count)}</span>
        </div>
      )}

      {list.loading && !list.data ? (
        <div className="grid gap-3">
          {[0, 1, 2].map((i) => (
            <Card key={i}>
              <Skeleton className="h-4 w-36" />
              <Skeleton className="mt-3 h-3.5 w-full" />
              <Skeleton className="mt-2 h-3.5 w-2/3" />
            </Card>
          ))}
        </div>
      ) : list.error && !list.data ? (
        <LoadError error={list.error} onRetry={list.reload} />
      ) : (
        <>
          <AnimatePresence initial={false}>
            {editing === "new" && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                transition={{ duration: 0.2 }}
                className="overflow-hidden"
              >
                <Card padded={false} className="mb-3">
                  <SnippetEditor onSave={(tr, tx) => save(undefined, tr, tx)} onCancel={() => setEditing(null)} />
                </Card>
              </motion.div>
            )}
          </AnimatePresence>

          {count === 0 && editing !== "new" ? (
            <EmptyState
              icon={<TextQuote />}
              title={t("snippets.empty.title")}
              text={t("snippets.empty.text")}
              action={
                <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
                  {t("snippets.add")}
                </Button>
              }
            />
          ) : items.length === 0 && query ? (
            <EmptyState compact icon={<SearchX />} title={t("common.nothingFound")} text={t("snippets.notFound", { q: query })} />
          ) : (
            <div className="grid gap-3">
              <AnimatePresence initial={false}>
                {items.map((s) => (
                  <motion.div
                    key={s.id}
                    layout="position"
                    initial={{ opacity: 0, y: 4 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0, scale: 0.98 }}
                    transition={{ duration: 0.18 }}
                  >
                    <Card padded={false} className="group overflow-hidden transition-shadow hover:shadow-pop">
                      {editing === s.id ? (
                        <SnippetEditor
                          initial={s}
                          onSave={(tr, tx) => save(s.id, tr, tx)}
                          onCancel={() => setEditing(null)}
                        />
                      ) : (
                        <div className="flex gap-4 px-5 py-4" onDoubleClick={() => setEditing(s.id)}>
                          <div className="min-w-0 flex-1">
                            <div className="flex items-center gap-2">
                              <span className="inline-flex h-7 items-center gap-1.5 rounded-lg bg-accent-soft px-2.5 text-[13px] font-semibold text-accent-text">
                                <Mic className="size-3.5" />
                                {s.trigger}
                              </span>
                            </div>
                            <div className="mt-2.5 flex gap-2">
                              <CornerDownRight className="mt-0.5 size-4 shrink-0 text-subtle" />
                              <div className="line-clamp-4 whitespace-pre-wrap text-[13.5px] leading-relaxed text-fg selectable">
                                {s.text}
                              </div>
                            </div>
                          </div>
                          <div className="flex shrink-0 gap-0.5 self-start opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
                            <IconButton label={t("common.edit")} size="sm" onClick={() => setEditing(s.id)}>
                              <Pencil className="size-3.5" />
                            </IconButton>
                            <IconButton label={t("common.delete")} size="sm" tone="danger" onClick={() => void remove(s)}>
                              <Trash className="size-3.5" />
                            </IconButton>
                          </div>
                        </div>
                      )}
                    </Card>
                  </motion.div>
                ))}
              </AnimatePresence>
            </div>
          )}
        </>
      )}
    </div>
  );
}

function SnippetEditor({
  initial,
  onSave,
  onCancel,
}: {
  initial?: Snippet;
  onSave: (trigger: string, text: string) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [trigger, setTrigger] = useState(initial?.trigger ?? "");
  const [text, setText] = useState(initial?.text ?? "");
  const [busy, setBusy] = useState(false);

  const submit = async () => {
    setBusy(true);
    const ok = await onSave(trigger, text);
    if (!ok) setBusy(false);
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") onCancel();
    if (e.key === "Enter" && e.ctrlKey) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <div className="flex flex-col gap-3 px-5 py-4" onKeyDown={onKey}>
      <div>
        <div className="mb-1.5 text-[12px] font-medium text-muted">{t("snippets.trigger")}</div>
        <Input
          autoFocus
          value={trigger}
          placeholder={t("snippets.triggerPlaceholder")}
          onChange={(e) => setTrigger(e.target.value)}
          icon={<Mic />}
        />
      </div>
      <div>
        <div className="mb-1.5 text-[12px] font-medium text-muted">{t("snippets.text")}</div>
        <Textarea
          rows={4}
          value={text}
          placeholder={t("snippets.textPlaceholder")}
          onChange={(e) => setText(e.target.value)}
        />
      </div>
      <div className="flex items-center justify-between">
        <span className="text-[12px] text-subtle">{t("snippets.editorHint")}</span>
        <div className="flex gap-2">
          <Button size="sm" variant="ghost" icon={<X className="size-4" />} onClick={onCancel}>
            {t("common.cancel")}
          </Button>
          <Button size="sm" variant="primary" icon={<Check className="size-4" />} loading={busy} onClick={() => void submit()}>
            {t("common.save")}
          </Button>
        </div>
      </div>
    </div>
  );
}
