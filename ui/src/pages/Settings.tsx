import { AnimatePresence, motion } from "framer-motion";
import {
  Bot,
  Check,
  Clipboard,
  Cpu,
  FolderOpen,
  Info,
  Keyboard,
  Languages,
  Laptop,
  LogOut,
  Mic,
  Monitor,
  MonitorOff,
  MonitorSmartphone,
  Moon,
  Palette,
  RefreshCw,
  Rocket,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Sun,
  Terminal,
  TriangleAlert,
  Type,
  Wand,
  Zap,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { DuckSection } from "../components/Duck";
import { CommitInput, Stepper } from "../components/fields";
import { ChordCapture, HotkeyCapture } from "../components/HotkeyCapture";
import { KeyManager } from "../components/KeyManager";
import { LearnModePicker } from "../components/Learn";
import { LevelBar } from "../components/Level";
import { Logo } from "../components/Logo";
import { ModelFolder } from "../components/ModelFolder";
import { Button, Checkbox, ChipInput, IconButton, Segmented, Slider, Switch } from "../components/ui/controls";
import { AiSpendSection } from "../components/AiSpend";
import { Badge, Card, LoadError, PageHeader, SectionTitle, SettingRow, Skeleton } from "../components/ui/display";
import { useConfirm } from "../components/ui/overlay";
import { Select, type SelectOption } from "../components/ui/select";
import type { MouseButton } from "../lib/types";

const MOUSE_OPTIONS: MouseButton[] = ["", "middle", "x1", "x2"];
import { Tabs } from "../components/ui/tabs";
import { useToast } from "../components/ui/toast";
import { languageName, NATIVE_NAMES, systemLocale, t, tp, type TKey } from "../i18n";
import { cn } from "../lib/cn";
import { fmtMb } from "../lib/format";
import { useLoad, useMicMonitor } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { comboParts, keyLabel } from "../lib/keys";
import { reloadRdp, sameKey, toggleRdp, useRdpWindows } from "../lib/rdp";
import { navigate, useHashAnchor } from "../lib/router";
import { useCore } from "../lib/store";
import { autostartEnabled, inTauri, quitApp, relaunchAdmin, setAutostart } from "../lib/tauri";
import { ACCENT_PRESETS, normalizeAccent } from "../lib/theme";
import type { DictationLanguage, InsertMode, LanguagePref, Settings, SettingsPatch, ThemeMode } from "../lib/types";
import { backend, call } from "../lib/ws";
import { AiModePicker } from "./Styles";

const SECTIONS = ["general", "dictation", "insert", "ai", "history", "about"] as const;
type Section = (typeof SECTIONS)[number];

export function SettingsPage({ section }: { section: string | null }) {
  const { settings, updateSettings } = useCore();
  const current: Section = (SECTIONS as readonly string[]).includes(section ?? "") ? (section as Section) : "general";

  return (
    <div>
      <PageHeader title={t("settings.title")} subtitle={t("settings.subtitle")} />
      <Tabs
        value={current}
        onChange={(v) => navigate(`settings/${v}`)}
        items={SECTIONS.map((s) => ({ value: s, label: t(`settings.tabs.${s}`) }))}
        className="mb-7"
      />
      {!settings ? (
        <Skeleton className="h-64 w-full rounded-2xl" />
      ) : (
        <AnimatePresence mode="wait" initial={false}>
          <motion.div
            key={current}
            initial={{ opacity: 0, y: 6 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -4 }}
            transition={{ duration: 0.18 }}
            className="space-y-8"
          >
            {current === "general" && <General s={settings} set={updateSettings} />}
            {current === "dictation" && <Dictation s={settings} set={updateSettings} />}
            {current === "insert" && <Insert s={settings} set={updateSettings} />}
            {current === "ai" && <Ai s={settings} set={updateSettings} />}
            {current === "history" && <HistorySection s={settings} set={updateSettings} />}
            {current === "about" && <About />}
          </motion.div>
        </AnimatePresence>
      )}
    </div>
  );
}

interface SectionProps {
  s: Settings;
  set: (p: SettingsPatch) => Promise<boolean>;
}

// ——— Общие ———

function General({ s, set }: SectionProps) {
  const toast = useToast();
  const { state } = useCore();
  const [auto, setAuto] = useState<boolean | null>(null);
  const [autoBusy, setAutoBusy] = useState(false);
  const tauri = inTauri();

  useEffect(() => {
    void autostartEnabled().then(setAuto);
  }, []);

  const accent = normalizeAccent(s.accent);
  const isPreset = ACCENT_PRESETS.some((p) => p.color === accent);
  const dictLang: DictationLanguage = s.dictation_language ?? "auto";

  return (
    <>
      <section data-testid="language">
        <SectionTitle>{t("settings.general.language")}</SectionTitle>
        <Card padded={false}>
          <SettingRow
            title={t("settings.general.uiLanguage")}
            description={t("settings.general.uiLanguageHint", { lang: languageName(systemLocale(s.language, state?.ui_language)) })}
          >
            {/* Названия языков — всегда на своём языке: English найдут и те, кто не читает по-русски */}
            <Segmented<LanguagePref>
              value={s.language ?? "system"}
              onChange={(v) => void set({ language: v })}
              options={[
                { value: "system", label: t("settings.general.uiLanguageSystem"), icon: <Laptop className="size-3.5" /> },
                { value: "ru", label: NATIVE_NAMES.ru },
                { value: "en", label: NATIVE_NAMES.en },
              ]}
            />
          </SettingRow>
          <SettingRow
            title={t("settings.general.dictationLanguage")}
            description={t(`settings.general.dictationLanguageHints.${dictLang}`)}
          >
            <Segmented<DictationLanguage>
              value={dictLang}
              onChange={(v) => void set({ dictation_language: v })}
              options={[
                { value: "auto", label: t("settings.general.dictationLanguageAuto"), icon: <Languages className="size-3.5" /> },
                { value: "ru", label: NATIVE_NAMES.ru },
                { value: "en", label: NATIVE_NAMES.en },
              ]}
            />
          </SettingRow>
        </Card>
      </section>

      <section>
        <SectionTitle>{t("settings.general.startup")}</SectionTitle>
        <Card padded={false}>
          <SettingRow
            title={t("settings.general.autostart")}
            description={tauri ? t("settings.general.autostartHint") : t("settings.general.onlyInApp")}
          >
            <Switch
              label={t("settings.general.autostart")}
              checked={Boolean(auto)}
              disabled={!tauri || auto === null || autoBusy}
              onChange={async (v) => {
                setAutoBusy(true);
                try {
                  setAuto(await setAutostart(v));
                } catch (e) {
                  toast({ tone: "error", title: t("errors.autostart"), description: errorText(e) });
                } finally {
                  setAutoBusy(false);
                }
              }}
            />
          </SettingRow>
        </Card>
      </section>

      <section>
        <SectionTitle>{t("settings.general.appearance")}</SectionTitle>
        <Card padded={false}>
          <SettingRow title={t("settings.general.theme")} description={t("settings.general.themeHint")}>
            <Segmented<ThemeMode>
              value={s.theme}
              onChange={(v) => void set({ theme: v })}
              options={[
                { value: "system", label: t("settings.general.themes.system"), icon: <Laptop className="size-3.5" /> },
                { value: "light", label: t("settings.general.themes.light"), icon: <Sun className="size-3.5" /> },
                { value: "dark", label: t("settings.general.themes.dark"), icon: <Moon className="size-3.5" /> },
              ]}
            />
          </SettingRow>
          <SettingRow title={t("settings.general.accent")} description={t("settings.general.accentHint")}>
            <div className="flex items-center gap-2">
              {ACCENT_PRESETS.map((p) => (
                <button
                  key={p.id}
                  type="button"
                  title={t(`settings.accents.${p.id}`)}
                  aria-label={t(`settings.accents.${p.id}`)}
                  onClick={() => void set({ accent: p.color })}
                  className="relative flex size-7 items-center justify-center rounded-full transition-transform duration-150 hover:scale-110"
                  style={{ background: p.color }}
                >
                  {accent === p.color && (
                    <motion.span
                      layoutId="accent-ring"
                      className="absolute -inset-[3px] rounded-full border-2"
                      style={{ borderColor: p.color }}
                      transition={{ type: "spring", stiffness: 520, damping: 38 }}
                    />
                  )}
                  {accent === p.color && <Check className="size-3.5 text-white" />}
                </button>
              ))}
              <label
                title={t("settings.general.customColor")}
                className={cn(
                  "relative ml-1 flex size-7 cursor-default items-center justify-center overflow-hidden rounded-full border border-line-strong",
                  !isPreset && "ring-2 ring-offset-2 ring-offset-surface",
                )}
                style={{
                  background: isPreset
                    ? "conic-gradient(from 0deg, #ff5f6d, #ffc371, #7cf29c, #4fc3f7, #7c5cff, #ff5f6d)"
                    : accent,
                  ["--tw-ring-color" as string]: accent,
                }}
              >
                <input
                  type="color"
                  value={accent}
                  onChange={(e) => void set({ accent: e.target.value })}
                  className="absolute inset-0 cursor-default opacity-0"
                />
                <Palette className="pointer-events-none size-3.5 text-white drop-shadow" />
              </label>
            </div>
          </SettingRow>
        </Card>
      </section>

      <section>
        <SectionTitle>{t("settings.general.feedback")}</SectionTitle>
        <Card padded={false}>
          <SettingRow title={t("settings.general.pill")} description={t("settings.general.pillHint")}>
            <Switch label={t("settings.general.pill")} checked={s.pill_enabled} onChange={(v) => void set({ pill_enabled: v })} />
          </SettingRow>
          {s.pill_position && (
            <SettingRow title={t("settings.general.pillPosition")} description={t("settings.general.pillPositionHint")}>
              <Button size="sm" variant="secondary" onClick={() => void set({ pill_position: null })}>
                {t("settings.general.pillReset")}
              </Button>
            </SettingRow>
          )}
          <SettingRow title={t("settings.general.sounds")} description={t("settings.general.soundsHint")}>
            <Switch label={t("settings.general.sounds")} checked={s.sounds} onChange={(v) => void set({ sounds: v })} />
          </SettingRow>
        </Card>
      </section>
    </>
  );
}

// ——— Диктовка ———

const MODELS: { value: string; hint: TKey }[] = [
  { value: "large-v3-turbo", hint: "settings.dictation.models.large_v3_turbo" },
  { value: "large-v3", hint: "settings.dictation.models.large_v3" },
  { value: "medium", hint: "settings.dictation.models.medium" },
  { value: "small", hint: "settings.dictation.models.small" },
  { value: "base", hint: "settings.dictation.models.base" },
  { value: "tiny", hint: "settings.dictation.models.tiny" },
];

function Dictation({ s, set }: SectionProps) {
  const toast = useToast();
  const devices = useLoad(() => call("list_devices"));

  // Живой уровень микрофона, пока открыта вкладка и окно видно
  useMicMonitor(true, s.input_device);

  const keys: { k: keyof Settings & string; title: string; hint: string }[] = [
    { k: "ptt_key", title: t("settings.dictation.ptt"), hint: t("settings.dictation.pttHint") },
    { k: "broadcast_modifier", title: t("settings.dictation.broadcast"), hint: t("settings.dictation.broadcastHint") },
    { k: "command_modifier", title: t("settings.dictation.command"), hint: t("settings.dictation.commandHint") },
    { k: "cancel_key", title: t("settings.dictation.cancel"), hint: t("settings.dictation.cancelHint") },
  ];
  // «Вставить последнее» / «Скопировать последнее» — сочетания с модификаторами (ТЗ)
  const chords: { k: "insert_last_hotkey" | "copy_last_hotkey"; title: string; hint: string }[] = [
    { k: "insert_last_hotkey", title: t("settings.dictation.insertLast"), hint: t("settings.dictation.insertLastHint") },
    { k: "copy_last_hotkey", title: t("settings.dictation.copyLast"), hint: t("settings.dictation.copyLastHint") },
  ];
  const values = keys.map((x) => String(s[x.k] ?? ""));
  const dup = values.find((v, i) => v && values.indexOf(v) !== i);

  const deviceOptions: SelectOption<string | null>[] = [
    { value: null, label: t("settings.dictation.micDefault"), icon: <Mic /> },
    ...(devices.data ?? []).map((d) => ({
      value: d.name,
      label: d.name,
      hint: d.default ? t("settings.dictation.micSystem") : undefined,
    })),
  ];
  if (s.input_device && !deviceOptions.some((o) => o.value === s.input_device)) {
    deviceOptions.push({ value: s.input_device, label: s.input_device, hint: t("settings.dictation.micMissing") });
  }

  const models: SelectOption<string>[] = MODELS.map((m) => ({ value: m.value, label: m.value, hint: t(m.hint) }));
  if (!models.some((m) => m.value === s.model)) models.push({ value: s.model, label: s.model });

  return (
    <>
      <section>
        <SectionTitle hint={t("settings.dictation.hotkeysHint")}>{t("settings.dictation.hotkeys")}</SectionTitle>
        <Card padded={false}>
          {keys.map((x) => (
            <SettingRow key={x.k} title={x.title} description={x.hint}>
              <HotkeyCapture
                value={String(s[x.k] ?? "")}
                combo={x.k === "ptt_key"}
                onChange={(v) => {
                  void set({ [x.k]: v } as SettingsPatch).then(
                    (ok) => ok && toast({ tone: "success", title: t("settings.dictation.keySaved", { key: keyLabel(v) }) }),
                  );
                }}
              />
            </SettingRow>
          ))}
          <SettingRow title={t("settings.dictation.doubleTap")} description={t("settings.dictation.doubleTapHint")}>
            <DoubleTap s={s} set={set} />
          </SettingRow>
          <SettingRow title={t("settings.dictation.mouse")} description={t("settings.dictation.mouseHint")}>
            <Select
              value={s.mouse_button}
              options={MOUSE_OPTIONS.map((v) => ({ value: v, label: t(`settings.dictation.mouseOptions.${v || "off"}`) }))}
              onChange={(v) => void set({ mouse_button: v })}
              className="w-[240px]"
              align="right"
            />
          </SettingRow>
          {chords.map((x) => (
            <SettingRow key={x.k} title={x.title} description={x.hint}>
              <ChordCapture
                value={s[x.k] ?? ""}
                onChange={(v) => {
                  void set({ [x.k]: v } as SettingsPatch).then(
                    (ok) =>
                      ok &&
                      toast({
                        tone: "success",
                        title: v
                          ? t("settings.dictation.chordSaved", { key: comboParts(v).map(keyLabel).join(" + ") })
                          : t("settings.dictation.chordOff"),
                      }),
                  );
                }}
              />
            </SettingRow>
          ))}
          <AnimatePresence>
            {dup && (
              <motion.div
                initial={{ opacity: 0, height: 0 }}
                animate={{ opacity: 1, height: "auto" }}
                exit={{ opacity: 0, height: 0 }}
                className="overflow-hidden"
              >
                <div className="flex items-center gap-2 border-t border-line bg-warn/8 px-5 py-3 text-[13px] text-warn">
                  <TriangleAlert className="size-4" />
                  {t("settings.dictation.conflict", { key: keyLabel(dup) })}
                </div>
              </motion.div>
            )}
          </AnimatePresence>
        </Card>
      </section>

      <section>
        <SectionTitle>{t("settings.dictation.mic")}</SectionTitle>
        <Card padded={false}>
          <SettingRow title={t("settings.dictation.micDevice")} description={t("settings.dictation.micHint")}>
            <div className="flex items-center gap-1.5">
              <Select
                value={s.input_device}
                options={deviceOptions}
                onChange={(v) => void set({ input_device: v })}
                className="w-[300px]"
                minWidth={300}
                align="right"
              />
              <IconButton label={t("common.refresh")} onClick={devices.reload}>
                <RefreshCw className={cn("size-4", devices.loading && "animate-spin")} />
              </IconButton>
            </div>
          </SettingRow>
          <SettingRow title={t("settings.dictation.level")} description={t("settings.dictation.levelHint")}>
            <div className="w-[300px]">
              <LevelBar active />
            </div>
          </SettingRow>
        </Card>
      </section>

      <DuckSection s={s} set={set} />

      <section>
        <SectionTitle hint={t("settings.dictation.asrHint")}>{t("settings.dictation.asr")}</SectionTitle>
        <Card padded={false}>
          <SettingRow title={t("settings.dictation.model")} description={t("settings.dictation.modelHint")}>
            <Select value={s.model} options={models} onChange={(v) => void set({ model: v })} className="w-[240px]" minWidth={340} align="right" />
          </SettingRow>
          <SettingRow stacked title={t("settings.dictation.modelDir")} description={t("settings.dictation.modelDirHint")}>
            <ModelFolder />
          </SettingRow>
          <SettingRow title={t("settings.dictation.device")} description={t("settings.dictation.deviceHint")}>
            <Segmented
              value={s.device === "cpu" ? "cpu" : "cuda"}
              onChange={(v) => void set(v === "cpu" ? { device: "cpu", compute_type: "int8" } : { device: "cuda", compute_type: "float16" })}
              options={[
                { value: "cuda", label: t("settings.dictation.gpu"), icon: <Zap className="size-3.5" /> },
                { value: "cpu", label: t("settings.dictation.cpu"), icon: <Cpu className="size-3.5" /> },
              ]}
            />
          </SettingRow>
          <SettingRow title={t("settings.dictation.precision")} description={t("settings.dictation.precisionHint")}>
            <Select
              value={s.compute_type}
              onChange={(v) => void set({ compute_type: v })}
              className="w-[240px]"
              minWidth={260}
              align="right"
              options={[
                { value: "float16", label: "float16", hint: t("settings.dictation.precisions.float16") },
                { value: "int8_float16", label: "int8_float16", hint: t("settings.dictation.precisions.int8_float16") },
                { value: "int8", label: "int8", hint: t("settings.dictation.precisions.int8") },
              ]}
            />
          </SettingRow>
          <SettingRow title={t("settings.dictation.beam")} description={t("settings.dictation.beamHint")}>
            <Segmented
              value={s.beam_size <= 1 ? "1" : "5"}
              onChange={(v) => void set({ beam_size: Number(v) })}
              options={[
                { value: "5", label: t("settings.dictation.beamAccurate") },
                { value: "1", label: t("settings.dictation.beamFast") },
              ]}
            />
          </SettingRow>
          <SettingRow title={t("settings.dictation.unload")} description={t("settings.dictation.unloadHint")}>
            <Select
              value={s.unload_after_min}
              onChange={(v) => void set({ unload_after_min: v })}
              className="w-[200px]"
              align="right"
              options={[0, 5, 15, 30, 60].map((m) => ({
                value: m,
                label: m === 0 ? t("settings.dictation.unloadNever") : t("settings.dictation.unloadAfter", { n: m }),
              }))}
            />
          </SettingRow>
          <SettingRow stacked title={t("settings.dictation.terms")} description={t("settings.dictation.termsHint")}>
            <ChipInput values={s.terms} onChange={(v) => void set({ terms: v })} placeholder={t("settings.dictation.termsPlaceholder")} />
          </SettingRow>
          <SettingRow
            title={t("settings.dictation.learn")}
            description={
              <>
                {t("settings.dictation.learnHint")}{" "}
                <button
                  type="button"
                  onClick={() => navigate("dictionary")}
                  className="font-medium text-accent-text hover:underline"
                >
                  {t("settings.dictation.learnOpen")}
                </button>
              </>
            }
          >
            <LearnModePicker value={s.learn_mode ?? "auto"} size="sm" onChange={(v) => void set({ learn_mode: v })} />
          </SettingRow>
          <SettingRow
            title={t("settings.dictation.hints")}
            description={
              <>
                {t("settings.dictation.hintsHint")}{" "}
                <button
                  type="button"
                  onClick={() => navigate("dictionary/vocab")}
                  className="font-medium text-accent-text hover:underline"
                >
                  {t("settings.dictation.hintsOpen")}
                </button>
              </>
            }
          >
            <div className="flex flex-col items-end gap-2">
              <div className="flex items-center gap-2.5 text-[12.5px] text-muted">
                {t("vocab.useVocab")}
                <Switch
                  size="sm"
                  label={t("vocab.useVocab")}
                  checked={s.vocab_enabled ?? true}
                  onChange={(v) => void set({ vocab_enabled: v })}
                />
              </div>
              <div className="flex items-center gap-2.5 text-[12.5px] text-muted">
                {t("vocab.useContext")}
                <Switch
                  size="sm"
                  label={t("vocab.useContext")}
                  checked={s.context_enabled ?? true}
                  onChange={(v) => void set({ context_enabled: v })}
                />
              </div>
            </div>
          </SettingRow>
        </Card>
      </section>
    </>
  );
}

function DoubleTap({ s, set }: SectionProps) {
  const [v, setV] = useState(s.double_tap_ms);
  useEffect(() => setV(s.double_tap_ms), [s.double_tap_ms]);
  return (
    <Slider
      className="w-[260px]"
      value={v}
      min={200}
      max={800}
      step={25}
      onChange={setV}
      onCommit={(x) => x !== s.double_tap_ms && void set({ double_tap_ms: x })}
      format={(x) => t("time.ms", { n: x })}
    />
  );
}

// ——— Вставка ———

function Insert({ s, set }: SectionProps) {
  const [typeDelay, setTypeDelay] = useState(s.type_delay_ms);
  const [rdpDelay, setRdpDelay] = useState(s.rdp_type_delay_ms);
  useEffect(() => setTypeDelay(s.type_delay_ms), [s.type_delay_ms]);
  useEffect(() => setRdpDelay(s.rdp_type_delay_ms), [s.rdp_type_delay_ms]);

  const modes: { v: InsertMode; icon: ReactNode }[] = [
    { v: "auto", icon: <Wand /> },
    { v: "type", icon: <Type /> },
    { v: "clipboard", icon: <Clipboard /> },
  ];

  return (
    <>
      <section>
        <SectionTitle hint={t("settings.insert.modeHint")}>{t("settings.insert.mode")}</SectionTitle>
        <div className="grid grid-cols-3 gap-3">
          {modes.map((m) => {
            const active = s.insert_mode === m.v;
            return (
              <button
                key={m.v}
                type="button"
                onClick={() => void set({ insert_mode: m.v })}
                className={cn(
                  "flex flex-col items-start rounded-2xl border p-4 text-left transition-[border-color,box-shadow] duration-200",
                  active ? "border-accent/60 bg-surface shadow-glow" : "border-line bg-surface shadow-card hover:border-line-strong",
                )}
              >
                <div className="flex w-full items-center justify-between">
                  <span
                    className={cn(
                      "flex size-8 items-center justify-center rounded-lg [&>svg]:size-4",
                      active ? "bg-accent text-accent-fg" : "bg-surface-2 text-muted",
                    )}
                  >
                    {m.icon}
                  </span>
                  {m.v === "auto" && <Badge tone="accent">{t("settings.insert.recommended")}</Badge>}
                </div>
                <div className="mt-3 text-[14px] font-semibold text-fg">{t(`insertModes.${m.v}`)}</div>
                <div className="mt-1 text-[12.5px] leading-relaxed text-muted">{t(`settings.insert.modes.${m.v}`)}</div>
              </button>
            );
          })}
        </div>
      </section>

      <section>
        <SectionTitle>{t("settings.insert.typing")}</SectionTitle>
        <Card padded={false}>
          <SettingRow title={t("settings.insert.typeDelay")} description={t("settings.insert.typeDelayHint")}>
            <Slider
              className="w-[260px]"
              value={typeDelay}
              min={0}
              max={30}
              onChange={setTypeDelay}
              onCommit={(v) => v !== s.type_delay_ms && void set({ type_delay_ms: v })}
              format={(v) => t("time.ms", { n: v })}
            />
          </SettingRow>
          <SettingRow title={t("settings.insert.rdpDelay")} description={t("settings.insert.rdpDelayHint")}>
            <Slider
              className="w-[260px]"
              value={rdpDelay}
              min={0}
              max={30}
              onChange={setRdpDelay}
              onCommit={(v) => v !== s.rdp_type_delay_ms && void set({ rdp_type_delay_ms: v })}
              format={(v) => t("time.ms", { n: v })}
            />
          </SettingRow>
          <SettingRow title={t("settings.insert.trailingSpace")} description={t("settings.insert.trailingSpaceHint")}>
            <Switch
              label={t("settings.insert.trailingSpace")}
              checked={s.add_trailing_space}
              onChange={(v) => void set({ add_trailing_space: v })}
            />
          </SettingRow>
        </Card>
      </section>

      <BroadcastSessions s={s} />

      <section>
        <SectionTitle hint={t("settings.insert.rdpDetectHint")}>{t("settings.insert.rdpDetect")}</SectionTitle>
        <Card padded={false}>
          <SettingRow stacked title={t("settings.insert.rdpClasses")} description={t("settings.insert.rdpClassesHint")}>
            <ChipInput mono values={s.rdp_classes} onChange={(v) => void set({ rdp_classes: v })} placeholder="TscShellContainerClass" />
          </SettingRow>
          <SettingRow stacked title={t("settings.insert.rdpProcesses")} description={t("settings.insert.rdpProcessesHint")}>
            <ChipInput mono values={s.rdp_processes} onChange={(v) => void set({ rdp_processes: v })} placeholder="mRemoteNG.exe" />
          </SettingRow>
        </Card>
      </section>
    </>
  );
}

/**
 * Белый список рассылки (broadcast_targets): текст рассылки уходит только в открытые
 * и отмеченные сессии. Ключ сессии — имя сервера (mstsc) или заголовок окна (другие клиенты).
 * Отмеченные, но закрытые сейчас сессии тоже видны — их можно снять.
 */
function BroadcastSessions({ s }: { s: Settings }) {
  const toast = useToast();
  const rdp = useRdpWindows();
  const ref = useRef<HTMLElement>(null);
  const flash = useHashAnchor("sessions", ref);
  const open = rdp.windows ?? [];
  const closed = (s.broadcast_targets ?? []).filter((k) => !open.some((w) => sameKey(w.key, k)));
  const checked = open.filter((w) => w.included).length;
  const keys = `${keyLabel(s.ptt_key)} + ${keyLabel(s.broadcast_modifier)}`;

  const toggle = async (key: string, included: boolean) => {
    try {
      await toggleRdp(key, included);
    } catch (e) {
      toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
    }
  };
  const checkAll = async () => {
    for (const w of open) if (!w.included) await toggle(w.key, true);
  };

  return (
    <section ref={ref} className="scroll-mt-4" data-testid="rdp-sessions">
      <SectionTitle
        hint={t("settings.insert.sessionsHint", { key: keys })}
        right={
          <div className="flex shrink-0 items-center gap-2">
            {open.length > 0 && (
              <span className="whitespace-nowrap text-[12px] text-subtle tabular">
                {t("settings.insert.sessionsChecked", { n: checked, total: open.length })}
              </span>
            )}
            <IconButton label={t("common.refresh")} onClick={reloadRdp}>
              <RefreshCw className={cn("size-4", rdp.loading && "animate-spin")} />
            </IconButton>
          </div>
        }
      >
        {t("settings.insert.sessions")}
      </SectionTitle>
      <Card padded={false} className={cn("overflow-hidden transition-shadow duration-500", flash && "border-accent/50 shadow-glow")}>
        {rdp.windows === null && rdp.error ? (
          <LoadError compact error={rdp.error} onRetry={reloadRdp} />
        ) : rdp.windows === null ? (
          <div className="space-y-3 p-5">
            <Skeleton className="h-4 w-2/3" />
            <Skeleton className="h-4 w-1/2" />
          </div>
        ) : open.length === 0 && closed.length === 0 ? (
          <div className="flex items-start gap-3 px-5 py-5">
            <MonitorSmartphone className="mt-0.5 size-5 shrink-0 text-subtle" />
            <div>
              <div className="text-[13.5px] font-medium text-fg">{t("settings.insert.noSessions")}</div>
              <div className="mt-0.5 text-[12.5px] text-muted">{t("settings.insert.noSessionsHint")}</div>
            </div>
          </div>
        ) : (
          <>
            {open.map((w) => (
              <label
                key={w.key}
                className="flex cursor-default items-center gap-3.5 border-b border-line px-5 py-3 transition-colors last:border-b-0 hover:bg-surface-2/50"
              >
                <Checkbox checked={w.included} onChange={(v) => void toggle(w.key, v)} label={w.key} />
                <span
                  className={cn(
                    "flex size-8 shrink-0 items-center justify-center rounded-lg transition-colors [&>svg]:size-4",
                    w.included ? "bg-accent-soft text-accent-text" : "bg-surface-2 text-subtle",
                  )}
                >
                  <Monitor />
                </span>
                <div className="min-w-0 flex-1">
                  <div className={cn("truncate text-[14px] font-semibold", w.included ? "text-fg" : "text-muted")}>{w.key}</div>
                  <div className="truncate text-[12px] text-subtle" title={w.title}>
                    <span className="font-mono text-[11.5px]">{w.process}</span>
                    {w.title !== w.key && <> · {w.title}</>}
                  </div>
                </div>
                {w.included && <Badge tone="accent">{t("settings.insert.included")}</Badge>}
              </label>
            ))}
            {closed.map((k) => (
              <label
                key={`closed:${k}`}
                className="flex cursor-default items-center gap-3.5 border-b border-line px-5 py-3 transition-colors last:border-b-0 hover:bg-surface-2/50"
              >
                <Checkbox checked onChange={(v) => void toggle(k, v)} label={k} />
                <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-surface-2 text-subtle [&>svg]:size-4">
                  <MonitorOff />
                </span>
                <div className="min-w-0 flex-1">
                  <div className="truncate text-[14px] font-semibold text-muted">{k}</div>
                  <div className="truncate text-[12px] text-subtle">{t("settings.insert.closedHint")}</div>
                </div>
                <Badge>{t("settings.insert.closed")}</Badge>
              </label>
            ))}
            <AnimatePresence initial={false}>
              {open.length > 0 && checked === 0 && (
                <motion.div
                  initial={{ opacity: 0, height: 0 }}
                  animate={{ opacity: 1, height: "auto" }}
                  exit={{ opacity: 0, height: 0 }}
                  className="overflow-hidden"
                >
                  <div className="flex items-center gap-2.5 border-t border-line bg-warn/8 px-5 py-3 text-[13px] text-warn">
                    <TriangleAlert className="size-4 shrink-0" />
                    <span className="flex-1">{t("settings.insert.noneChecked")}</span>
                    <Button size="sm" variant="secondary" onClick={() => void checkAll()}>
                      {t("settings.insert.checkAll")}
                    </Button>
                  </div>
                </motion.div>
              )}
            </AnimatePresence>
          </>
        )}
      </Card>
    </section>
  );
}

// ——— ИИ ———

const PROVIDER_ORDER_SUGGESTIONS = ["amazon-bedrock", "anthropic", "google-vertex"];

function Ai({ s, set }: SectionProps) {
  const [timeout, setTimeoutV] = useState(s.ai_timeout_s);
  useEffect(() => setTimeoutV(s.ai_timeout_s), [s.ai_timeout_s]);

  return (
    <>
      <Card padded={false} className={cn("overflow-hidden", s.ai_enabled && "border-accent/40")}>
        <div className="flex items-center gap-4 px-5 py-4">
          <span
            className={cn(
              "flex size-10 shrink-0 items-center justify-center rounded-xl transition-colors",
              s.ai_enabled ? "bg-accent text-accent-fg" : "bg-surface-2 text-muted",
            )}
          >
            <Sparkles className="size-5" />
          </span>
          <div className="flex-1">
            <div className="text-[14.5px] font-semibold text-fg">{t("settings.ai.enable")}</div>
            <div className="text-[12.5px] text-muted">{t("settings.ai.enableHint")}</div>
          </div>
          <Switch label={t("settings.ai.enable")} checked={s.ai_enabled} onChange={(v) => void set({ ai_enabled: v })} />
        </div>
      </Card>

      <AiSpendSection />

      <section>
        <SectionTitle hint={t("settings.ai.providerHint")}>{t("settings.ai.provider")}</SectionTitle>
        <Card padded={false}>
          <div className="border-b border-line px-5 py-4">
            <Segmented
              full
              value={s.ai_provider}
              onChange={(v) => void set({ ai_provider: v })}
              options={[
                { value: "openrouter", label: t("settings.ai.providers.openrouter") },
                { value: "anthropic", label: t("settings.ai.providers.anthropic") },
                { value: "ollama", label: t("settings.ai.providers.ollama") },
              ]}
            />
          </div>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div
              key={s.ai_provider}
              initial={{ opacity: 0 }}
              animate={{ opacity: 1 }}
              exit={{ opacity: 0 }}
              transition={{ duration: 0.15 }}
            >
              {s.ai_provider === "openrouter" && (
                <>
                  <SettingRow stacked title={t("settings.ai.key")} description={t("settings.ai.keyHint")}>
                    <KeyManager provider="openrouter" />
                  </SettingRow>
                  <SettingRow title={t("settings.ai.model")} description={t("settings.ai.modelHint")}>
                    <CommitInput mono className="w-[300px]" value={s.ai_model} onCommit={(v) => void set({ ai_model: v })} />
                  </SettingRow>
                  <SettingRow stacked title={t("settings.ai.order")} description={t("settings.ai.orderHint")}>
                    <ChipInput
                      mono
                      values={s.ai_provider_order}
                      onChange={(v) => void set({ ai_provider_order: v })}
                      placeholder={t("settings.ai.orderPlaceholder")}
                    />
                    <div className="mt-2 flex flex-wrap items-center gap-1.5">
                      <span className="text-[12px] text-subtle">{t("settings.ai.orderSuggest")}</span>
                      {PROVIDER_ORDER_SUGGESTIONS.filter((p) => !s.ai_provider_order.includes(p)).map((p) => (
                        <button
                          key={p}
                          type="button"
                          onClick={() => void set({ ai_provider_order: [...s.ai_provider_order, p] })}
                          className="rounded-md border border-dashed border-line-strong px-2 py-0.5 font-mono text-[11.5px] text-muted transition-colors hover:border-accent/60 hover:text-accent-text"
                        >
                          + {p}
                        </button>
                      ))}
                    </div>
                  </SettingRow>
                </>
              )}
              {s.ai_provider === "anthropic" && (
                <>
                  <SettingRow stacked title={t("settings.ai.key")} description={t("settings.ai.keyHintAnthropic")}>
                    <KeyManager provider="anthropic" />
                  </SettingRow>
                  <SettingRow title={t("settings.ai.model")} description={t("settings.ai.anthropicModelHint")}>
                    <CommitInput mono className="w-[300px]" value={s.anthropic_model} onCommit={(v) => void set({ anthropic_model: v })} />
                  </SettingRow>
                </>
              )}
              {s.ai_provider === "ollama" && (
                <>
                  <SettingRow title={t("settings.ai.ollamaUrl")} description={t("settings.ai.ollamaUrlHint")}>
                    <CommitInput mono className="w-[300px]" value={s.ollama_url} onCommit={(v) => void set({ ollama_url: v })} />
                  </SettingRow>
                  <SettingRow title={t("settings.ai.model")} description={t("settings.ai.ollamaModelHint")}>
                    <CommitInput mono className="w-[300px]" value={s.ollama_model} onCommit={(v) => void set({ ollama_model: v })} />
                  </SettingRow>
                </>
              )}
            </motion.div>
          </AnimatePresence>
        </Card>
      </section>

      <section>
        <SectionTitle hint={t("settings.ai.behaviorHint")}>{t("settings.ai.behavior")}</SectionTitle>
        <AiModePicker value={s.ai_mode} onChange={(v) => void set({ ai_mode: v })} />
        <Card padded={false} className="mt-3">
          <SettingRow title={t("settings.ai.minWords")} description={t("settings.ai.minWordsHint")}>
            <Stepper value={s.ai_min_words} min={1} max={50} onChange={(v) => void set({ ai_min_words: v })} />
          </SettingRow>
          <SettingRow title={t("settings.ai.timeout")} description={t("settings.ai.timeoutHint")}>
            <Slider
              className="w-[260px]"
              value={timeout}
              min={2}
              max={30}
              onChange={setTimeoutV}
              onCommit={(v) => v !== s.ai_timeout_s && void set({ ai_timeout_s: v })}
              format={(v) => t("time.sec", { n: v })}
            />
          </SettingRow>
        </Card>
      </section>

      <section>
        <SectionTitle hint={t("settings.ai.commandHint", { keys: `${keyLabel(s.ptt_key)} + ${keyLabel(s.command_modifier)}` })}>
          {t("settings.ai.command")}
        </SectionTitle>
        <div className="grid grid-cols-2 gap-3">
          {(
            [
              { v: "openrouter", icon: <Bot />, title: t("settings.ai.commandOpenrouter"), text: t("settings.ai.commandOpenrouterText") },
              { v: "claude_cli", icon: <Terminal />, title: t("settings.ai.commandCli"), text: t("settings.ai.commandCliText") },
            ] as const
          ).map((o) => {
            const active = s.command_provider === o.v;
            return (
              <button
                key={o.v}
                type="button"
                onClick={() => void set({ command_provider: o.v })}
                className={cn(
                  "flex items-start gap-3 rounded-2xl border p-4 text-left transition-[border-color,box-shadow] duration-200",
                  active ? "border-accent/60 bg-surface shadow-glow" : "border-line bg-surface shadow-card hover:border-line-strong",
                )}
              >
                <span
                  className={cn(
                    "flex size-8 shrink-0 items-center justify-center rounded-lg [&>svg]:size-4",
                    active ? "bg-accent text-accent-fg" : "bg-surface-2 text-muted",
                  )}
                >
                  {o.icon}
                </span>
                <span>
                  <span className="block text-[14px] font-semibold text-fg">{o.title}</span>
                  <span className="mt-1 block text-[12.5px] leading-relaxed text-muted">{o.text}</span>
                </span>
              </button>
            );
          })}
        </div>
      </section>
    </>
  );
}

// ——— История ———

function HistorySection({ s, set }: SectionProps) {
  const days = [0, 7, 30, 90, 180, 365];
  const keeps = [0, 10, 30, 50, 100];
  const limits = [100, 300, 500, 1000, 2000];
  return (
    <section>
      <SectionTitle>{t("settings.history.title")}</SectionTitle>
      <Card padded={false}>
        <SettingRow title={t("settings.history.enabled")} description={t("settings.history.enabledHint")}>
          <Switch
            label={t("settings.history.enabled")}
            checked={s.history_enabled}
            onChange={(v) => void set({ history_enabled: v })}
          />
        </SettingRow>
        <SettingRow title={t("settings.history.retention")} description={t("settings.history.retentionHint")}>
          <Select
            value={days.includes(s.history_retention_days) ? s.history_retention_days : 0}
            onChange={(v) => void set({ history_retention_days: v })}
            className="w-[200px]"
            align="right"
            disabled={!s.history_enabled}
            options={days.map((d) => ({
              value: d,
              label: d === 0 ? t("settings.history.forever") : tp("units.days", d),
            }))}
          />
        </SettingRow>
        <SettingRow title={t("settings.history.recKeep")} description={t("settings.history.recKeepHint")}>
          <Select
            value={keeps.includes(s.recordings_keep) ? s.recordings_keep : 30}
            onChange={(v) => void set({ recordings_keep: v })}
            className="w-[200px]"
            align="right"
            options={keeps.map((n) => ({ value: n, label: n === 0 ? t("settings.history.recOff") : String(n) }))}
          />
        </SettingRow>
        <SettingRow title={t("settings.history.recLimit")} description={t("settings.history.recLimitHint")}>
          <Select
            value={limits.includes(s.recordings_max_mb) ? s.recordings_max_mb : 300}
            onChange={(v) => void set({ recordings_max_mb: v })}
            className="w-[200px]"
            align="right"
            disabled={s.recordings_keep === 0}
            options={limits.map((mb) => ({ value: mb, label: fmtMb(mb) }))}
          />
        </SettingRow>
        <SettingRow title={t("settings.history.clear")} description={t("settings.history.clearHint")}>
          <Button size="sm" variant="secondary" onClick={() => navigate("history")}>
            {t("settings.history.openHistory")}
          </Button>
        </SettingRow>
      </Card>
    </section>
  );
}

// ——— О программе ———

function About() {
  const { state } = useCore();
  const toast = useToast();
  const confirm = useConfirm();
  const tauri = inTauri();

  return (
    <>
      <Card className="flex items-center gap-5 p-6">
        <Logo size={56} />
        <div className="flex-1">
          <div className="text-[20px] font-semibold tracking-[-0.02em] text-fg">{t("app.name")}</div>
          <div className="text-[13px] text-muted">{t("settings.about.tagline")}</div>
          <div className="mt-2 flex flex-wrap gap-1.5">
            <Badge tone="accent">{t("settings.about.coreVersion", { v: state?.version ?? "—" })}</Badge>
            <Badge>{t("settings.about.uiVersion", { v: __APP_VERSION__ })}</Badge>
            {backend.mock && <Badge tone="warn">{t("mock.badge")}</Badge>}
          </div>
        </div>
      </Card>

      <Card padded={false}>
        <SettingRow
          title={t("settings.about.rights")}
          description={state?.elevated ? t("settings.about.elevated") : t("settings.about.notElevated")}
        >
          {state?.elevated ? (
            <Badge tone="success" icon={<ShieldCheck />}>
              {t("status.admin")}
            </Badge>
          ) : (
            <Button
              size="sm"
              variant="secondary"
              icon={<ShieldAlert className="size-4" />}
              disabled={!tauri}
              onClick={() => relaunchAdmin().catch((e: unknown) => toast({ tone: "error", title: t("errors.generic"), description: errorText(e) }))}
            >
              {t("notice.relaunchAdmin")}
            </Button>
          )}
        </SettingRow>
        <SettingRow title={t("settings.about.logs")} description={t("settings.about.logsHint")}>
          <Button
            size="sm"
            variant="secondary"
            icon={<FolderOpen className="size-4" />}
            onClick={() => call("open_logs").catch((e: unknown) => toast({ tone: "error", title: t("errors.generic"), description: errorText(e) }))}
          >
            {t("settings.about.openLogs")}
          </Button>
        </SettingRow>
        <SettingRow title={t("settings.about.wizard")} description={t("settings.about.wizardHint")}>
          <Button size="sm" variant="secondary" icon={<Rocket className="size-4" />} onClick={() => navigate("wizard")}>
            {t("settings.about.runWizard")}
          </Button>
        </SettingRow>
        <SettingRow title={t("settings.about.privacy")} description={t("settings.about.privacyHint")}>
          <Info className="size-4 text-subtle" />
        </SettingRow>
        <SettingRow title={t("settings.about.quit")} description={t("settings.about.quitHint")}>
          <Button
            size="sm"
            variant="danger"
            icon={<LogOut className="size-4" />}
            onClick={async () => {
              const ok = await confirm({
                title: t("settings.about.quitTitle"),
                body: t("settings.about.quitBody"),
                confirmLabel: t("settings.about.quit"),
                danger: true,
              });
              if (!ok) return;
              if (tauri) await quitApp();
              else await call("quit").catch(() => {});
            }}
          >
            {t("settings.about.quit")}
          </Button>
        </SettingRow>
      </Card>

      <div className="flex items-center justify-center gap-1.5 text-[12px] text-subtle">
        <Keyboard className="size-3.5" />
        {t("settings.about.footer")}
      </div>
    </>
  );
}
