import { AnimatePresence, motion } from "framer-motion";
import {
  ArrowRight,
  BookA,
  BookCheck,
  BookPlus,
  Check,
  Ear,
  Pencil,
  Plus,
  Search,
  SearchX,
  ShieldCheck,
  Sigma,
  Trash,
  Undo2,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState, type KeyboardEvent } from "react";
import { ImportExport } from "../components/ImportExport";
import { LearnModePicker, LearnText } from "../components/Learn";
import { JOURNAL_LIMIT, LearnJournal } from "../components/LearnJournal";
import { Button, ChipInput, IconButton, Input, Switch } from "../components/ui/controls";
import { Card, EmptyState, LoadError, PageHeader, SettingRow, Skeleton } from "../components/ui/display";
import { Tabs } from "../components/ui/tabs";
import { VocabPanel } from "../components/Vocab";
import { useToast } from "../components/ui/toast";
import { intlTag, t, tp, useLocale } from "../i18n";
import { useEvent, useLoad, type Loadable } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { isLearnNotice, onDictChanged } from "../lib/learn";
import { navigate } from "../lib/router";
import { useCore } from "../lib/store";
import type { DictItem, LearnJournalEntry, LearnMode } from "../lib/types";
import { call } from "../lib/ws";

type DictTab = "terms" | "vocab";

export function DictionaryPage({ tab }: { tab?: string | null }) {
  const current: DictTab = tab === "vocab" ? "vocab" : "terms";
  const list = useLoad(() => call("dict_list"));
  // Журнал обучения («Недавно добавлено»): импорт с заменой очищает и его — перечитать оба.
  const journal = useLoad(() => call("dict_learn_journal", { limit: JOURNAL_LIMIT }));
  const { reload: reloadList } = list;
  const { reload: reloadJournal } = journal;
  const onImported = useCallback(() => {
    reloadList();
    reloadJournal();
  }, [reloadList, reloadJournal]);
  const count = list.data?.length ?? 0;
  return (
    <div>
      <PageHeader
        title={t("dictionary.title")}
        subtitle={current === "vocab" ? t("vocab.subtitle") : t("dictionary.subtitle")}
        actions={current === "terms" ? <TermsActions onImported={onImported} exportDisabled={count === 0} /> : undefined}
      />
      <Tabs
        value={current}
        onChange={(v) => navigate(v === "vocab" ? "dictionary/vocab" : "dictionary")}
        items={[
          { value: "terms", label: t("dictionary.tabs.terms"), icon: <BookA />, ...(list.data ? { count } : {}) },
          { value: "vocab", label: t("dictionary.tabs.vocab"), icon: <Sigma /> },
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
          {current === "vocab" ? <VocabPanel /> : <TermsTab list={list} journal={journal} />}
        </motion.div>
      </AnimatePresence>
    </div>
  );
}

/** Кнопки над списком терминов: импорт/экспорт и «Добавить термин» (открывает редактор). */
function TermsActions({ onImported, exportDisabled }: { onImported: () => void; exportDisabled: boolean }) {
  return (
    <>
      <ImportExport onImported={onImported} exportDisabled={exportDisabled} />
      <Button
        variant="primary"
        icon={<Plus className="size-4" />}
        onClick={() => window.dispatchEvent(new CustomEvent(NEW_TERM_EVENT))}
      >
        {t("dictionary.add")}
      </Button>
    </>
  );
}

const NEW_TERM_EVENT = "rupor:dict-new-term";

function TermsTab({ list, journal }: { list: Loadable<DictItem[]>; journal: Loadable<LearnJournalEntry[]> }) {
  const toast = useToast();
  const { settings } = useCore();
  const fuzzy = settings?.dict_fuzzy ?? true;
  const [query, setQuery] = useState("");
  const [editing, setEditing] = useState<number | "new" | null>(null);
  useEffect(() => {
    const on = () => setEditing("new");
    window.addEventListener(NEW_TERM_EVENT, on);
    return () => window.removeEventListener(NEW_TERM_EVENT, on);
  }, []);

  // Рупор выучил правку (dict_undo — уже в словаре) или её отменили/добавили кнопкой
  // уведомления (в пилюле или тосте) — перечитать словарь и «Недавно добавлено».
  const { reload: reloadList } = list;
  const { reload: reloadJournal } = journal;
  useEvent("notice", (n) => {
    if (!isLearnNotice(n)) return;
    reloadJournal();
    if (n.action === "dict_undo") reloadList();
  });
  useEffect(
    () =>
      onDictChanged(() => {
        reloadList();
        reloadJournal();
      }),
    [reloadList, reloadJournal],
  );

  const locale = useLocale();
  const items = useMemo(() => {
    const all = [...(list.data ?? [])].sort((a, b) => a.term.localeCompare(b.term, intlTag()));
    const q = query.trim().toLowerCase();
    if (!q) return all;
    return all.filter(
      (d) => d.term.toLowerCase().includes(q) || d.variants.some((v) => v.toLowerCase().includes(q)),
    );
  }, [list.data, query, locale]);

  const save = async (id: number | undefined, term: string, variants: string[]): Promise<boolean> => {
    if (!term.trim()) {
      toast({ tone: "warn", title: t("dictionary.termRequired") });
      return false;
    }
    try {
      const saved = await call("dict_upsert", { ...(id !== undefined ? { id } : {}), term: term.trim(), variants });
      list.setData((xs) => {
        const rest = (xs ?? []).filter((x) => x.id !== saved.id && x.id !== id);
        return [...rest, saved];
      });
      reloadJournal();
      setEditing(null);
      return true;
    } catch (e) {
      toast({ tone: "error", title: t("errors.save"), description: errorText(e) });
      return false;
    }
  };

  const remove = async (d: DictItem) => {
    list.setData((xs) => (xs ?? []).filter((x) => x.id !== d.id));
    try {
      await call("dict_delete", { id: d.id });
      reloadJournal();
      toast({
        tone: "info",
        title: t("dictionary.deleted", { term: d.term }),
        action: {
          label: t("common.undo"),
          onClick: () => void save(undefined, d.term, d.variants),
        },
      });
    } catch (e) {
      list.reload();
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };

  /**
   * «Отменить» в «Недавно добавлено» — как кнопка уведомления (dict_learn_undo): строка
   * исчезает сразу, словарь перечитывается, тост говорит, что именно убрано.
   */
  const undoLearned = async (e: LearnJournalEntry) => {
    journal.setData((xs) => (xs ?? []).filter((x) => x !== e));
    try {
      await call("dict_learn_undo", { term: e.term, variant: e.variant, id: e.id, created: e.created });
      const after = await call("dict_list");
      list.setData(() => after);
      const gone = !after.some((d) => d.id === e.id);
      toast({
        tone: "success",
        duration: 2600,
        title: gone
          ? t("learn.undoneTerm", { term: e.term })
          : e.variant
            ? t("learn.undoneVariant", { term: e.term, variant: e.variant })
            : t("learn.undone"),
      });
    } catch (err) {
      reloadList();
      toast({ tone: "error", title: t("learn.undoFailed"), description: errorText(err) });
    }
    reloadJournal();
  };

  const count = list.data?.length ?? 0;

  return (
    <div>
      <FuzzyCard />
      <LearnCard />
      <LearnJournal entries={journal.data ?? []} onUndo={(e) => void undoLearned(e)} />

      {count > 0 && (
        <div className="mb-4 flex items-center gap-3">
          <Input
            className="flex-1"
            icon={<Search />}
            placeholder={t("dictionary.search")}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <span className="shrink-0 text-[12.5px] text-subtle">{tp("units.terms", count)}</span>
        </div>
      )}

      {list.loading && !list.data ? (
        <Card padded={false}>
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="flex items-center gap-6 border-b border-line px-5 py-4 last:border-0">
              <Skeleton className="h-4 w-32" />
              <div className="flex gap-1.5">
                <Skeleton className="h-6 w-20" />
                <Skeleton className="h-6 w-24" />
              </div>
            </div>
          ))}
        </Card>
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
                  <DictEditor fuzzy={fuzzy} onSave={(term, v) => save(undefined, term, v)} onCancel={() => setEditing(null)} />
                </Card>
              </motion.div>
            )}
          </AnimatePresence>

          {count === 0 && editing !== "new" ? (
            <EmptyState
              icon={<BookA />}
              title={t("dictionary.empty.title")}
              text={t("dictionary.empty.text")}
              action={
                <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setEditing("new")}>
                  {t("dictionary.add")}
                </Button>
              }
            />
          ) : items.length === 0 && query ? (
            <EmptyState compact icon={<SearchX />} title={t("common.nothingFound")} text={t("dictionary.notFound", { q: query })} />
          ) : (
            items.length > 0 && (
              <Card padded={false} className="overflow-hidden">
                <AnimatePresence initial={false}>
                  {items.map((d) => (
                    <motion.div
                      key={d.id}
                      layout="position"
                      initial={{ opacity: 0 }}
                      animate={{ opacity: 1 }}
                      exit={{ opacity: 0, height: 0 }}
                      transition={{ duration: 0.18 }}
                      className="border-b border-line last:border-b-0"
                    >
                      {editing === d.id ? (
                        <DictEditor
                          initial={d}
                          fuzzy={fuzzy}
                          onSave={(term, v) => save(d.id, term, v)}
                          onCancel={() => setEditing(null)}
                        />
                      ) : (
                        <DictRow item={d} fuzzy={fuzzy} onEdit={() => setEditing(d.id)} onDelete={() => void remove(d)} />
                      )}
                    </motion.div>
                  ))}
                </AnimatePresence>
              </Card>
            )
          )}
        </>
      )}
    </div>
  );
}

/**
 * «Узнавать похожие написания» (dict_fuzzy): ядро само находит слова, похожие на термин по
 * звучанию, — варианты нужны только для совсем непохожих ошибок и очень коротких терминов.
 */
function FuzzyCard() {
  const { settings, updateSettings } = useCore();
  if (!settings) return null;
  const on = settings.dict_fuzzy ?? true;
  return (
    <Card padded={false} className="mb-4 overflow-hidden" data-testid="dict-fuzzy">
      <SettingRow
        title={
          <span className="flex items-center gap-2">
            <Ear className="size-4 text-subtle" />
            {t("dictionary.fuzzy.title")}
          </span>
        }
        description={t("dictionary.fuzzy.hint")}
      >
        <Switch label={t("dictionary.fuzzy.title")} checked={on} onChange={(v) => void updateSettings({ dict_fuzzy: v })} />
      </SettingRow>
    </Card>
  );
}

/** «Учиться на ваших правках»: режим learn_mode, что будет видно и что Рупор читает. */
function LearnCard() {
  const { settings, updateSettings } = useCore();
  if (!settings) return null;
  const mode: LearnMode = settings.learn_mode ?? "auto";
  return (
    <Card padded={false} className="mb-6 overflow-hidden">
      <div className="flex flex-wrap items-start justify-between gap-x-6 gap-y-3 px-5 pb-4 pt-[18px]">
        <div className="flex min-w-0 flex-1 basis-[320px] gap-3.5">
          <div className="flex size-9 shrink-0 items-center justify-center rounded-xl bg-accent-soft text-accent-text">
            <BookCheck className="size-[18px]" />
          </div>
          <div className="min-w-0">
            <div className="text-[14px] font-semibold text-fg">{t("dictionary.learn.title")}</div>
            <p className="mt-0.5 max-w-xl text-[12.5px] leading-relaxed text-muted">{t("dictionary.learn.text")}</p>
          </div>
        </div>
        <LearnModePicker value={mode} size="sm" onChange={(v) => void updateSettings({ learn_mode: v })} />
      </div>
      <div className="flex flex-wrap items-center justify-between gap-x-8 gap-y-3 border-t border-line bg-surface-2/40 px-5 py-3.5">
        <div className="min-w-0 flex-1 basis-[300px]">
          <AnimatePresence mode="wait" initial={false}>
            <motion.p
              key={mode}
              initial={{ opacity: 0, y: 3 }}
              animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -3 }}
              transition={{ duration: 0.14 }}
              className="text-[12.5px] leading-relaxed text-fg/85"
            >
              {t(`dictionary.learn.hints.${mode}`)}
            </motion.p>
          </AnimatePresence>
          <div className="mt-1.5 flex items-start gap-1.5 text-[12px] leading-relaxed text-subtle">
            <ShieldCheck className="mt-[2px] size-3.5 shrink-0" />
            <span>{t("dictionary.learn.privacy")}</span>
          </div>
        </div>
        <AnimatePresence mode="wait" initial={false}>
          {mode !== "off" && (
            <motion.div
              key={mode}
              initial={{ opacity: 0, scale: 0.97 }}
              animate={{ opacity: 1, scale: 1 }}
              exit={{ opacity: 0, scale: 0.97 }}
              transition={{ duration: 0.16 }}
              className="flex shrink-0 flex-col items-center gap-1.5"
            >
              <PillPreview add={mode === "suggest"} />
              <span className="text-[11px] text-subtle">{t("dictionary.learn.example")}</span>
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </Card>
  );
}

/** Как уведомление выглядит в пилюле (всегда тёмное стекло, как сама пилюля). */
function PillPreview({ add }: { add: boolean }) {
  const term = t("dictionary.learn.exampleTerm");
  const variant = t("dictionary.learn.exampleVariant");
  const text = add
    ? t("dictionary.learn.exampleSuggest", { term, variant })
    : t("dictionary.learn.exampleAuto", { term, variant });
  return (
    <div
      aria-hidden
      className="flex items-center gap-2.5 rounded-full border border-white/10 bg-[rgb(18_18_22/0.92)] py-[6px] pl-[7px] pr-[6px] shadow-[0_6px_18px_-6px_rgb(0_0_0/0.5),inset_0_1px_0_rgb(255_255_255/0.06)]"
    >
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[color-mix(in_oklab,var(--c-accent)_34%,transparent)] text-white">
        {add ? <BookPlus className="size-3.5" /> : <BookCheck className="size-3.5" />}
      </span>
      <span className="whitespace-nowrap text-[12px] font-medium text-white/75">
        <LearnText text={text} term={term} variant={variant} termClass="font-semibold text-white" variantClass="text-white/55" />
      </span>
      <span
        className={
          add
            ? "flex h-7 items-center gap-1 rounded-full bg-white px-2.5 text-[11.5px] font-semibold text-[#15151a]"
            : "flex h-7 items-center gap-1 rounded-full bg-white/12 px-2.5 text-[11.5px] font-semibold text-white"
        }
      >
        {add ? <Plus className="size-3" strokeWidth={2.6} /> : <Undo2 className="size-3" strokeWidth={2.4} />}
        {add ? t("learn.add") : t("learn.undo")}
      </span>
    </div>
  );
}

/** Похожие написания ядро ищет у терминов от 4 звуков без цифр (fuzzy.py); здесь —
 * примерно: буквы, двойные — одна. */
function soundsLongEnough(term: string): boolean {
  if (/\d/.test(term)) return false;
  const letters = [...term.toLowerCase()].filter((ch) => /\p{L}/u.test(ch));
  return letters.filter((ch, i) => ch !== letters[i - 1]).length >= 4;
}

function DictRow({
  item,
  fuzzy,
  onEdit,
  onDelete,
}: {
  item: DictItem;
  /** dict_fuzzy: термин без вариантов всё равно узнаётся по звучанию (если не слишком короткий). */
  fuzzy: boolean;
  onEdit: () => void;
  onDelete: () => void;
}) {
  return (
    <div
      className="group flex items-center gap-5 px-5 py-3.5 transition-colors hover:bg-surface-2/50"
      onDoubleClick={onEdit}
    >
      <div className="w-52 shrink-0 truncate text-[14px] font-semibold text-fg selectable">{item.term}</div>
      <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
        {item.variants.length === 0 ? (
          <span className="text-[12.5px] text-subtle">
            {t(fuzzy && soundsLongEnough(item.term) ? "dictionary.noVariantsFuzzy" : "dictionary.noVariants")}
          </span>
        ) : (
          <>
            {item.variants.map((v) => (
              <span
                key={v}
                className="inline-flex h-6 items-center rounded-md bg-surface-2 px-2 text-[12.5px] text-muted"
              >
                {v}
              </span>
            ))}
            <ArrowRight className="mx-0.5 size-3.5 text-subtle" />
            <span className="inline-flex h-6 items-center rounded-md bg-accent-soft px-2 text-[12.5px] font-medium text-accent-text">
              {item.term}
            </span>
          </>
        )}
      </div>
      <div className="flex shrink-0 gap-0.5 opacity-0 transition-opacity group-hover:opacity-100 focus-within:opacity-100">
        <IconButton label={t("common.edit")} size="sm" onClick={onEdit}>
          <Pencil className="size-3.5" />
        </IconButton>
        <IconButton label={t("common.delete")} size="sm" tone="danger" onClick={onDelete}>
          <Trash className="size-3.5" />
        </IconButton>
      </div>
    </div>
  );
}

function DictEditor({
  initial,
  fuzzy,
  onSave,
  onCancel,
}: {
  initial?: DictItem;
  /** dict_fuzzy: варианты необязательны — подсказка в поле вариантов об этом. */
  fuzzy: boolean;
  onSave: (term: string, variants: string[]) => Promise<boolean>;
  onCancel: () => void;
}) {
  const [term, setTerm] = useState(initial?.term ?? "");
  const [variants, setVariantsState] = useState<string[]>(initial?.variants ?? []);
  const [busy, setBusy] = useState(false);
  // Варианты читаем из ref: Ctrl+Enter в поле вариантов сначала добавляет недописанный
  // вариант (ChipInput, тот же keydown), и submit должен увидеть его сразу, до перерисовки.
  const variantsRef = useRef(variants);
  const setVariants = (v: string[]) => {
    variantsRef.current = v;
    setVariantsState(v);
  };

  const submit = async () => {
    setBusy(true);
    const ok = await onSave(term, variantsRef.current);
    if (!ok) setBusy(false);
  };

  const onKey = (e: KeyboardEvent<HTMLDivElement>) => {
    if (e.key === "Escape") {
      onCancel();
      return;
    }
    if (e.key !== "Enter" || e.shiftKey) return;
    const el = e.target as HTMLElement;
    // Enter в поле: сохранить. Исключение — ChipInput только что добавил вариант из
    // черновика (он делает preventDefault): тогда Enter означает «добавить», а не «сохранить».
    // Ctrl+Enter сохраняет всегда (вариант из черновика уже добавлен — см. variantsRef).
    if (e.ctrlKey || (el.tagName === "INPUT" && !e.defaultPrevented)) {
      e.preventDefault();
      void submit();
    }
  };

  return (
    <div className="flex flex-col gap-3 px-5 py-4" onKeyDown={onKey}>
      <div className="grid grid-cols-[13rem_1fr] gap-3">
        <div>
          <div className="mb-1.5 text-[12px] font-medium text-muted">{t("dictionary.term")}</div>
          <Input
            autoFocus
            data-role="term"
            value={term}
            placeholder={t("dictionary.termPlaceholder")}
            onChange={(e) => setTerm(e.target.value)}
          />
        </div>
        <div>
          <div className="mb-1.5 text-[12px] font-medium text-muted">{t("dictionary.variants")}</div>
          <ChipInput
            values={variants}
            onChange={setVariants}
            placeholder={t(fuzzy ? "dictionary.variantsPlaceholderFuzzy" : "dictionary.variantsPlaceholder")}
          />
        </div>
      </div>
      <div className="flex items-center justify-between">
        <span className="text-[12px] text-subtle">{t("dictionary.editorHint")}</span>
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
