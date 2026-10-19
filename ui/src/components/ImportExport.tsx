// Импорт/экспорт словаря и сниппетов (общий формат export_data, JSON-файл).
import { motion } from "framer-motion";
import { Download, FileUp, Upload } from "lucide-react";
import { useRef, useState } from "react";
import { t, tp } from "../i18n";
import { cn } from "../lib/cn";
import { todayIso } from "../lib/format";
import { errorText } from "../lib/hub";
import type { ExportData } from "../lib/types";
import { call } from "../lib/ws";
import { Button } from "./ui/controls";
import { Modal } from "./ui/overlay";
import { useToast } from "./ui/toast";

function isObj(v: unknown): v is Record<string, unknown> {
  return typeof v === "object" && v !== null && !Array.isArray(v);
}

/** Разбор файла: принимает полный формат и «голые» массивы словаря/сниппетов. */
export function parseImport(text: string): ExportData {
  let raw: unknown;
  try {
    raw = JSON.parse(text);
  } catch {
    throw new Error(t("transfer.invalidJson"));
  }
  const out: ExportData = { version: 1, dictionary: [], snippets: [] };
  const takeDict = (arr: unknown) => {
    if (!Array.isArray(arr)) return;
    for (const d of arr) {
      if (isObj(d) && typeof d.term === "string" && d.term.trim()) {
        const variants = Array.isArray(d.variants) ? d.variants.filter((v): v is string => typeof v === "string") : [];
        out.dictionary.push({ term: d.term.trim(), variants });
      }
    }
  };
  const takeSnippets = (arr: unknown) => {
    if (!Array.isArray(arr)) return;
    for (const s of arr) {
      if (isObj(s) && typeof s.trigger === "string" && typeof s.text === "string" && s.trigger.trim()) {
        out.snippets.push({ trigger: s.trigger.trim(), text: s.text });
      }
    }
  };
  if (isObj(raw)) {
    takeDict(raw.dictionary);
    takeSnippets(raw.snippets);
  } else if (Array.isArray(raw)) {
    takeDict(raw);
    takeSnippets(raw);
  }
  if (!out.dictionary.length && !out.snippets.length) throw new Error(t("transfer.empty"));
  return out;
}

export async function downloadExport(): Promise<void> {
  const data = await call("export_data");
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url;
  a.download = `rupor-export-${todayIso()}.json`;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 2000);
}

export function ImportExport({ onImported, exportDisabled = false }: { onImported: () => void; exportDisabled?: boolean }) {
  const toast = useToast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [pending, setPending] = useState<{ name: string; data: ExportData } | null>(null);
  const [replace, setReplace] = useState(false);
  const [busy, setBusy] = useState(false);

  const onFile = async (f: File | undefined) => {
    if (!f) return;
    try {
      const data = parseImport(await f.text());
      setReplace(false);
      setPending({ name: f.name, data });
    } catch (e) {
      toast({ tone: "error", title: t("transfer.readError"), description: errorText(e) });
    } finally {
      if (fileRef.current) fileRef.current.value = "";
    }
  };

  const doImport = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      const r = await call("import_data", { data: pending.data, replace });
      toast({
        tone: "success",
        title: t("transfer.done"),
        description: t("transfer.doneText", {
          dict: tp("units.terms", r.dictionary),
          snippets: tp("units.snippets", r.snippets),
        }),
      });
      setPending(null);
      onImported();
    } catch (e) {
      toast({ tone: "error", title: t("transfer.importError"), description: errorText(e) });
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <input
        ref={fileRef}
        type="file"
        accept=".json,application/json"
        className="hidden"
        onChange={(e) => void onFile(e.target.files?.[0])}
      />
      <Button variant="ghost" icon={<Upload className="size-4" />} onClick={() => fileRef.current?.click()}>
        {t("transfer.import")}
      </Button>
      <Button
        variant="ghost"
        icon={<Download className="size-4" />}
        disabled={exportDisabled}
        onClick={() =>
          downloadExport().then(
            () => toast({ tone: "success", title: t("transfer.exported") }),
            (e: unknown) => toast({ tone: "error", title: t("transfer.exportError"), description: errorText(e) }),
          )
        }
      >
        {t("transfer.export")}
      </Button>
      <Modal
        open={pending !== null}
        onClose={() => !busy && setPending(null)}
        title={t("transfer.title")}
        footer={
          <>
            <Button variant="ghost" onClick={() => setPending(null)} disabled={busy}>
              {t("common.cancel")}
            </Button>
            <Button variant={replace ? "danger-solid" : "primary"} loading={busy} onClick={() => void doImport()}>
              {t("transfer.confirm")}
            </Button>
          </>
        }
      >
        {pending && (
          <div>
            <div className="flex items-center gap-3 rounded-xl bg-surface-2 p-3">
              <span className="flex size-9 items-center justify-center rounded-lg bg-accent-soft text-accent-text">
                <FileUp className="size-4" />
              </span>
              <div className="min-w-0">
                <div className="truncate text-[13.5px] font-medium text-fg">{pending.name}</div>
                <div className="text-[12.5px] text-muted">
                  {tp("units.terms", pending.data.dictionary.length)} · {tp("units.snippets", pending.data.snippets.length)}
                </div>
              </div>
            </div>
            <div className="mt-4 space-y-2">
              {([false, true] as const).map((v) => (
                <button
                  key={String(v)}
                  type="button"
                  onClick={() => setReplace(v)}
                  className={cn(
                    "relative flex w-full items-start gap-3 rounded-xl border p-3 text-left transition-colors",
                    replace === v ? "border-accent/60 bg-accent-soft" : "border-line hover:bg-surface-2",
                  )}
                >
                  <span
                    className={cn(
                      "mt-0.5 flex size-4 shrink-0 items-center justify-center rounded-full border-2",
                      replace === v ? "border-accent" : "border-line-strong",
                    )}
                  >
                    {replace === v && (
                      <motion.span layoutId="import-radio" className="size-1.5 rounded-full bg-accent" />
                    )}
                  </span>
                  <span>
                    <span className="block text-[13.5px] font-medium text-fg">
                      {v ? t("transfer.replace") : t("transfer.merge")}
                    </span>
                    <span className="block text-[12.5px] text-muted">
                      {v ? t("transfer.replaceHint") : t("transfer.mergeHint")}
                    </span>
                  </span>
                </button>
              ))}
            </div>
          </div>
        )}
      </Modal>
    </>
  );
}
