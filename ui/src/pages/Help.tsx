import { AnimatePresence, motion } from "framer-motion";
import {
  BookCheck,
  ChevronDown,
  Clipboard,
  ClipboardPaste,
  Copy,
  Cpu,
  Hand,
  Keyboard,
  Languages,
  Maximize,
  Mic,
  MicOff,
  MonitorSmartphone,
  MousePointerClick,
  ShieldAlert,
  Sparkles,
  Terminal,
  Type,
  Volume1,
  Wand,
  X,
} from "lucide-react";
import { useState, type ReactNode } from "react";
import { Card, KeyCombo, PageHeader, SectionTitle } from "../components/ui/display";
import { t, type TKey } from "../i18n";
import { cn } from "../lib/cn";
import { useCore } from "../lib/store";

export function HelpPage() {
  const { state } = useCore();
  const hk = state?.hotkeys ?? {
    ptt: "rctrl",
    broadcast: "rshift",
    command: "ralt",
    cancel: "esc",
    insert_last: "ctrl+alt+shift+v",
    copy_last: "ctrl+alt+shift+c",
    convert_layout: "shift+pause",
  };

  const steps: { icon: ReactNode; title: TKey; text: TKey; keys?: string[] }[] = [
    { icon: <MousePointerClick />, title: "help.steps.s1", text: "help.steps.s1Text" },
    { icon: <Mic />, title: "help.steps.s2", text: "help.steps.s2Text", keys: [hk.ptt] },
    { icon: <Type />, title: "help.steps.s3", text: "help.steps.s3Text" },
  ];

  const hotkeys: { icon: ReactNode; title: TKey; text: TKey; keys: string[]; suffix?: string }[] = [
    { icon: <Mic />, title: "help.hotkeys.hold", text: "help.hotkeys.holdText", keys: [hk.ptt] },
    { icon: <Hand />, title: "help.hotkeys.handsFree", text: "help.hotkeys.handsFreeText", keys: [hk.ptt], suffix: "×2" },
    { icon: <MonitorSmartphone />, title: "help.hotkeys.broadcast", text: "help.hotkeys.broadcastText", keys: [hk.ptt, hk.broadcast] },
    { icon: <Terminal />, title: "help.hotkeys.command", text: "help.hotkeys.commandText", keys: [hk.ptt, hk.command] },
    { icon: <X />, title: "help.hotkeys.cancel", text: "help.hotkeys.cancelText", keys: [hk.cancel] },
    ...(hk.insert_last
      ? [{ icon: <ClipboardPaste />, title: "help.hotkeys.insertLast" as TKey, text: "help.hotkeys.insertLastText" as TKey, keys: [hk.insert_last] }]
      : []),
    ...(hk.copy_last
      ? [{ icon: <Copy />, title: "help.hotkeys.copyLast" as TKey, text: "help.hotkeys.copyLastText" as TKey, keys: [hk.copy_last] }]
      : []),
    ...(hk.convert_layout
      ? [
          {
            icon: <Languages />,
            title: "help.hotkeys.convertLayout" as TKey,
            text: "help.hotkeys.convertLayoutText" as TKey,
            keys: [hk.convert_layout],
          },
        ]
      : []),
  ];

  const modes: { icon: ReactNode; title: TKey; text: TKey }[] = [
    { icon: <Wand />, title: "insertModes.auto", text: "help.modes.auto" },
    { icon: <Type />, title: "insertModes.type", text: "help.modes.type" },
    { icon: <Clipboard />, title: "insertModes.clipboard", text: "help.modes.clipboard" },
    { icon: <MonitorSmartphone />, title: "help.modes.broadcastTitle", text: "help.modes.broadcast" },
  ];

  const faq: { icon: ReactNode; q: TKey; a: TKey }[] = [
    { icon: <ShieldAlert />, q: "help.faq.adminQ", a: "help.faq.adminA" },
    { icon: <Maximize />, q: "help.faq.rdpQ", a: "help.faq.rdpA" },
    { icon: <MonitorSmartphone />, q: "help.faq.rdpTargetsQ", a: "help.faq.rdpTargetsA" },
    { icon: <Cpu />, q: "help.faq.cudaQ", a: "help.faq.cudaA" },
    { icon: <MicOff />, q: "help.faq.micQ", a: "help.faq.micA" },
    { icon: <Sparkles />, q: "help.faq.aiQ", a: "help.faq.aiA" },
    { icon: <Keyboard />, q: "help.faq.ctrlQ", a: "help.faq.ctrlA" },
    { icon: <BookCheck />, q: "help.faq.learnQ", a: "help.faq.learnA" },
    { icon: <Volume1 />, q: "help.faq.duckQ", a: "help.faq.duckA" },
  ];

  return (
    <div>
      <PageHeader title={t("help.title")} subtitle={t("help.subtitle")} />

      <SectionTitle>{t("help.howTitle")}</SectionTitle>
      <div className="grid grid-cols-3 gap-3">
        {steps.map((s, i) => (
          <Card key={s.title} className="relative overflow-hidden p-5">
            <span className="absolute right-4 top-3 text-[40px] font-bold leading-none tracking-[-0.04em] text-fg/[0.05]">
              {i + 1}
            </span>
            <span className="flex size-9 items-center justify-center rounded-xl bg-accent-soft text-accent-text [&>svg]:size-[18px]">
              {s.icon}
            </span>
            <div className="mt-3 text-[14px] font-semibold text-fg">{t(s.title)}</div>
            <div className="mt-1 text-[12.5px] leading-relaxed text-muted">{t(s.text)}</div>
            {s.keys && <KeyCombo keys={s.keys} className="mt-3" />}
          </Card>
        ))}
      </div>

      <SectionTitle className="mt-9" hint={t("help.hotkeysHint")}>
        {t("help.hotkeysTitle")}
      </SectionTitle>
      <Card padded={false}>
        {hotkeys.map((h) => (
          <div key={h.title} className="flex items-center gap-4 border-b border-line px-5 py-3.5 last:border-b-0">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted [&>svg]:size-4">
              {h.icon}
            </span>
            <div className="min-w-0 flex-1">
              <div className="text-[13.5px] font-medium text-fg">{t(h.title)}</div>
              <div className="text-[12.5px] text-muted">{t(h.text)}</div>
            </div>
            <span className="flex shrink-0 items-center gap-1.5">
              <KeyCombo keys={h.keys} />
              {h.suffix && <span className="text-[12px] font-medium text-subtle">{h.suffix}</span>}
            </span>
          </div>
        ))}
      </Card>

      <SectionTitle className="mt-9">{t("help.modesTitle")}</SectionTitle>
      <div className="grid grid-cols-2 gap-3">
        {modes.map((m) => (
          <Card key={m.title} className="flex gap-3 p-4">
            <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-muted [&>svg]:size-4">
              {m.icon}
            </span>
            <div>
              <div className="text-[13.5px] font-semibold text-fg">{t(m.title)}</div>
              <div className="mt-0.5 text-[12.5px] leading-relaxed text-muted">{t(m.text)}</div>
            </div>
          </Card>
        ))}
      </div>

      <SectionTitle className="mt-9">{t("help.commandTitle")}</SectionTitle>
      <Card>
        <p className="text-[13.5px] leading-relaxed text-muted">
          {t("help.commandText")}
        </p>
        <div className="mt-3 flex items-center gap-2">
          <KeyCombo keys={[hk.ptt, hk.command]} />
          <span className="text-[12.5px] text-subtle">{t("help.commandHold")}</span>
        </div>
        <div className="mt-4 flex flex-wrap gap-2">
          {(["e1", "e2", "e3", "e4", "e5"] as const).map((k) => (
            <span key={k} className="rounded-full border border-line bg-surface-2 px-3 py-1 text-[12.5px] text-fg">
              {t("help.quoted", { text: t(`help.commandExamples.${k}`) })}
            </span>
          ))}
        </div>
      </Card>

      <SectionTitle className="mt-9">{t("help.faqTitle")}</SectionTitle>
      <Card padded={false}>
        {faq.map((f) => (
          <Faq key={f.q} icon={f.icon} q={t(f.q)} a={t(f.a)} />
        ))}
      </Card>
    </div>
  );
}

function Faq({ icon, q, a }: { icon: ReactNode; q: string; a: string }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="border-b border-line last:border-b-0">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-5 py-3.5 text-left transition-colors hover:bg-surface-2/50"
      >
        <span className="flex text-subtle [&>svg]:size-4">{icon}</span>
        <span className="flex-1 text-[13.5px] font-medium text-fg">{q}</span>
        <ChevronDown className={cn("size-4 text-subtle transition-transform duration-200", open && "rotate-180")} />
      </button>
      <AnimatePresence initial={false}>
        {open && (
          <motion.div
            initial={{ height: 0, opacity: 0 }}
            animate={{ height: "auto", opacity: 1 }}
            exit={{ height: 0, opacity: 0 }}
            transition={{ duration: 0.2, ease: [0.22, 1, 0.36, 1] }}
            className="overflow-hidden"
          >
            <div className="whitespace-pre-line px-5 pb-4 pl-12 text-[13px] leading-relaxed text-muted">{a}</div>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}
