import { AnimatePresence, motion } from "framer-motion";
import {
  BookCheck,
  BookPlus,
  CircleAlert,
  Download,
  FileText,
  FlaskConical,
  LoaderCircle,
  PlugZap,
  RefreshCw,
  WifiOff,
} from "lucide-react";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Logo } from "./components/Logo";
import { Sidebar } from "./components/Sidebar";
import { Button } from "./components/ui/controls";
import { ProgressBar } from "./components/ui/display";
import { ConfirmProvider } from "./components/ui/overlay";
import { ToastProvider, useToast } from "./components/ui/toast";
import { getLocale, t, useLocale } from "./i18n";
import { cn } from "./lib/cn";
import { useEvent, useLoad } from "./lib/hooks";
import { errorText } from "./lib/hub";
import { isLearnNotice, learnNoticeArrived, resolveLearn, type LearnNotice } from "./lib/learn";
import { aiTrayLine, balanceMode, setBalanceMode, useAiUsage } from "./lib/aiUsage";
import { pollRdp, RDP_SESSIONS_ROUTE, sameKey, toggleRdp, useRdpWindows } from "./lib/rdp";
import { useLocation, type Route } from "./lib/router";
import { CoreProvider, useCore } from "./lib/store";
import {
  inTauri,
  onNavigate,
  onTrayAction,
  relaunchAdmin,
  setAutostart,
  setWindowTitle,
  showMain,
  updateTray,
  type TrayLabels,
  type TrayRdpItem,
} from "./lib/tauri";
import { applyTheme, onSystemThemeChange } from "./lib/theme";
import type { InsertMode, ProgressEvent } from "./lib/types";
import { call } from "./lib/ws";
import { AppRulesPage } from "./pages/AppRules";
import { DictionaryPage } from "./pages/Dictionary";
import { HelpPage } from "./pages/Help";
import { HistoryPage } from "./pages/History";
import { HomePage } from "./pages/Home";
import { SettingsPage } from "./pages/Settings";
import { SnippetsPage } from "./pages/Snippets";
import { StylesPage } from "./pages/Styles";
import { Wizard } from "./wizard/Wizard";

export function App() {
  // Смена языка перерисовывает всё дерево: t() читает текущий словарь при рендере
  const locale = useLocale();
  useEffect(() => void setWindowTitle(t("app.name")), [locale]);
  return (
    <ToastProvider>
      <ConfirmProvider>
        <CoreProvider>
          <Shell />
        </CoreProvider>
      </ConfirmProvider>
    </ToastProvider>
  );
}

/** Все строки меню трея на языке интерфейса (update_tray.labels). */
function trayLabels(): TrayLabels {
  return {
    lang: getLocale(),
    app: t("app.name"),
    status_loading: t("tray.status.loading"),
    status_idle: t("tray.status.idle"),
    status_recording: t("tray.status.recording"),
    status_hands_free: t("tray.status.hands_free"),
    status_processing: t("tray.status.processing"),
    status_ai: t("tray.status.ai"),
    status_paused: t("tray.status.paused"),
    status_error: t("tray.status.error"),
    status_starting: t("tray.status.starting"),
    status_restarting: t("tray.status.restarting"),
    status_failed: t("tray.status.failed"),
    status_already_running: t("tray.status.already_running"),
    pause: t("tray.pause"),
    insert_mode: t("tray.insertMode"),
    mode_auto: t("insertModes.auto"),
    mode_type: t("tray.modeType"),
    mode_clipboard: t("tray.modeClipboard"),
    style: t("tray.style"),
    last_insert: t("tray.lastInsert"),
    pill_restore: t("tray.pillRestore"),
    rdp: t("tray.rdp"),
    rdp_empty: t("tray.rdpEmpty"),
    open: t("tray.open"),
    quit: t("tray.quit"),
  };
}

/** Связка окна с ядром и Tauri: тема, трей, уведомления, навигация. */
function Bridge({ navigate }: { navigate: (to: string) => void }) {
  const { state, settings, styles, updateSettings, setState } = useCore();
  const locale = useLocale();
  const toast = useToast();
  const stateRef = useRef(state);
  stateRef.current = state;
  const settingsRef = useRef(settings);
  settingsRef.current = settings;

  // Тема и акцент
  useEffect(() => {
    if (!settings) return;
    applyTheme(settings.theme, settings.accent);
    if (settings.theme !== "system") return;
    return onSystemThemeChange(() => applyTheme(settings.theme, settings.accent));
  }, [settings?.theme, settings?.accent]);

  // Расход ИИ: за балансом OpenRouter ходим, только если Рупор им пользуется (очистка или
  // командный режим через OpenRouter) — с Ollama/Anthropic ключ в OpenRouter не отправляется
  const aiOn = Boolean(settings?.ai_enabled);
  const bMode = balanceMode(settings);
  useEffect(() => setBalanceMode(bMode), [bMode]);
  const ai = useAiUsage();
  const aiLine = aiOn ? aiTrayLine(ai, bMode !== "off") : null;

  // RDP-сессии для подменю трея: окна открываются без событий ядра — перечитываем раз в 15 с
  useEffect(() => pollRdp(), []);
  const rdp = useRdpWindows();
  const rdpRef = useRef(rdp);
  rdpRef.current = rdp;
  const rdpItems: TrayRdpItem[] | null = rdp.windows
    ? rdp.windows.map((w) => ({ key: w.key, title: w.title, included: w.included }))
    : null;
  const rdpSig = JSON.stringify(rdpItems);

  // Трей: состояние → меню и иконка; строка расхода ИИ, подменю рассылки и строки меню на
  // языке интерфейса (labels) — всегда (null в ai_line/rdp — скрыть)
  useEffect(() => {
    if (!state) return;
    void updateTray({
      state: state.state,
      paused: state.paused,
      insert_mode: state.insert_mode,
      style: state.style,
      ...(styles.length ? { styles: styles.map((s) => ({ id: s.id, name: s.name })) } : {}),
      ai_line: aiLine,
      rdp: rdpItems,
      labels: trayLabels(),
    });
    // rdpItems сравниваем по содержимому (rdpSig): новый массив при каждом опросе — не повод
  }, [state?.state, state?.paused, state?.insert_mode, state?.style, styles, aiLine, rdpSig, locale]);

  // Трей: действия из меню
  useEffect(
    () =>
      onTrayAction((id) => {
        const s = stateRef.current;
        const fail = (e: unknown) =>
          toast({ tone: "error", title: t("errors.generic"), description: errorText(e) });
        if (id === "pause") {
          call("pause", { on: !(s?.paused ?? false) }).then(setState, fail);
        } else if (id.startsWith("mode:")) {
          const mode = id.slice(5) as InsertMode;
          void updateSettings({ insert_mode: mode });
        } else if (id.startsWith("style:")) {
          void updateSettings({ default_style: id.slice(6) });
        } else if (id === "last_insert") {
          call("last_insert").catch(fail);
        } else if (id === "pill_restore") {
          // Пилюлю на место ставит Rust; здесь — запомнить это место (позиция по умолчанию)
          void updateSettings({ pill_position: null });
        } else if (id.startsWith("rdp:")) {
          // Флажок сессии в подменю «Рассылка в RDP ▸»: key — как пришёл в update_tray
          const key = id.slice(4);
          const w = rdpRef.current.windows?.find((x) => sameKey(x.key, key));
          const targets = settingsRef.current?.broadcast_targets ?? [];
          const included = w ? !w.included : !targets.some((k) => sameKey(k, key));
          toggleRdp(key, included).catch(fail);
        }
      }),
    [toast, updateSettings, setState],
  );

  useEffect(() => onNavigate((r) => navigate(r)), [navigate]);

  // Первый запуск: Рупор живёт в трее и окно сам не открывает. Мастер показываем, только
  // если без него диктовка не заработает (нет модели или библиотек CUDA при видеокарте NVIDIA).
  const firstRunChecked = useRef(false);
  const firstRunDone = settings?.first_run_done;
  useEffect(() => {
    if (!inTauri() || firstRunDone !== false || firstRunChecked.current) return;
    firstRunChecked.current = true;
    void (async () => {
      try {
        const setup = await call("setup_status");
        let ready = setup.model_ready;
        if (ready && !setup.cuda_ready) ready = !(await call("cuda_check")).gpu_name;
        if (!ready) {
          await showMain("wizard");
          return;
        }
        await setAutostart(true).catch(() => false);
        await updateSettings({ first_run_done: true });
      } catch {
        await showMain("wizard");
      }
    })();
  }, [firstRunDone, updateSettings]);

  // «Словарь учится»: тост с той же кнопкой, что в пилюле. Нажали в одном окне — во втором
  // кнопка уже ничего не отправит в ядро (lib/learn), а покажет то же подтверждение.
  const learnToast = (n: LearnNotice) => {
    learnNoticeArrived(n);
    const add = n.action === "dict_add";
    const { term, variant, created } = n.data;
    const act = () =>
      resolveLearn(n).then(
        () =>
          toast({
            tone: "success",
            duration: 2600,
            title: add
              ? t("learn.addedToast", { term })
              : created
                ? t("learn.undoneTerm", { term })
                : t("learn.undoneVariant", { term, variant }),
          }),
        (e: unknown) =>
          toast({ tone: "error", title: add ? t("learn.addFailed") : t("learn.undoFailed"), description: errorText(e) }),
      );
    toast({
      tone: "info",
      icon: add ? (
        <BookPlus className="size-[18px] text-accent-text" />
      ) : (
        <BookCheck className="size-[18px] text-accent-text" />
      ),
      title: n.text,
      duration: 6000,
      action: { label: add ? t("learn.add") : t("learn.undo"), onClick: () => void act() },
    });
  };

  // Уведомления ядра → тосты
  useEvent("notice", (n) => {
    if (isLearnNotice(n)) {
      learnToast(n);
      return;
    }
    if (!n.toast) return;
    const action =
      n.action === "relaunch_admin"
        ? { label: t("notice.relaunchAdmin"), onClick: () => void relaunchAdmin() }
        : n.action === "open_settings_ai"
          ? { label: t("notice.openAiSettings"), onClick: () => navigate("settings/ai") }
          : n.action === "open_settings_insert"
            ? { label: t("notice.openRdpSettings"), onClick: () => navigate(RDP_SESSIONS_ROUTE) }
            : n.action === "open_recordings"
              ? { label: t("notice.openRecordings"), onClick: () => navigate("history/recordings") }
              : undefined;
    toast({
      tone: n.level === "error" ? "error" : n.level === "warn" ? "warn" : "info",
      title: n.text,
      action,
    });
  });

  return null;
}

const PAGES: Record<Exclude<Route, "wizard">, (p: { sub: string | null }) => ReactNode> = {
  home: () => <HomePage />,
  history: ({ sub }) => <HistoryPage tab={sub} />,
  dictionary: ({ sub }) => <DictionaryPage tab={sub} />,
  snippets: () => <SnippetsPage />,
  styles: () => <StylesPage />,
  apps: () => <AppRulesPage />,
  settings: ({ sub }) => <SettingsPage section={sub} />,
  help: () => <HelpPage />,
};

function Shell() {
  const { status, state, settings } = useCore();
  const { route, sub, navigate } = useLocation();

  if (status === "no-params") return <NoParams />;
  if (!state || !settings) return <Splash reconnecting={status === "reconnecting"} />;

  const wizard = route === "wizard" || !settings.first_run_done;

  return (
    <>
      <Bridge navigate={navigate} />
      <AnimatePresence mode="wait" initial={false}>
        {wizard ? (
          <motion.div
            key="wizard"
            className="h-full"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <Wizard onDone={() => navigate("home")} />
          </motion.div>
        ) : (
          <motion.div
            key="main"
            className="flex h-full"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.2 }}
          >
            <Sidebar route={route} onNavigate={(r) => navigate(r)} />
            <main className="relative mt-2 flex min-w-0 flex-1 flex-col overflow-hidden rounded-tl-[16px] border-l border-t border-line bg-layer">
              <ConnectionBanner />
              <AnimatePresence mode="wait" initial={false}>
                <motion.div
                  key={route}
                  className="min-h-0 flex-1 overflow-y-auto"
                  initial={{ opacity: 0, y: 8 }}
                  animate={{ opacity: 1, y: 0 }}
                  exit={{ opacity: 0, y: -4 }}
                  transition={{ duration: 0.18, ease: [0.22, 1, 0.36, 1] }}
                >
                  <div className="@container mx-auto w-full max-w-[1000px] px-10 pb-16 pt-9">
                    {PAGES[route]({ sub })}
                  </div>
                </motion.div>
              </AnimatePresence>
            </main>
          </motion.div>
        )}
      </AnimatePresence>
    </>
  );
}

/** Последнее событие загрузки модели (null — загрузки нет или она закончилась успешно). */
function useModelProgress(): ProgressEvent | null {
  const [p, setP] = useState<ProgressEvent | null>(null);
  useEvent("progress", (e) => {
    if (e.what === "model") setP(e.done && !e.error ? null : e);
  });
  // новое ядро (перезапуск) — прежняя загрузка умерла вместе со старым
  useEvent("hello", () => setP((cur) => (cur && !cur.done ? null : cur)));
  return p;
}

/** Полоса сверху: нет связи с ядром, загружается модель или модель не готова (state=error). */
function ConnectionBanner() {
  const { status, state } = useCore();
  const progress = useModelProgress();
  const offline = status !== "open";
  const loading = !offline && state?.state === "loading";
  const failed = !offline && state?.state === "error";
  const downloading = progress !== null && !progress.done;
  return (
    <AnimatePresence initial={false}>
      {failed && <ModelErrorBanner key="err" progress={progress} />}
      {(offline || loading) && (
        <motion.div
          key={offline ? "off" : "load"}
          initial={{ height: 0, opacity: 0 }}
          animate={{ height: "auto", opacity: 1 }}
          exit={{ height: 0, opacity: 0 }}
          transition={{ duration: 0.2 }}
          className="shrink-0 overflow-hidden"
        >
          <div
            className={
              offline
                ? "flex items-center gap-2.5 border-b border-line bg-danger/8 px-6 py-2.5 text-[13px] text-danger"
                : "border-b border-line bg-info/8 px-6 py-2.5 text-[13px] text-info"
            }
          >
            {offline ? (
              <>
                <WifiOff className="size-4" />
                {t("conn.lost")}
                <LoaderCircle className="ml-auto size-4 animate-spin" />
              </>
            ) : (
              <div className="flex items-center gap-3">
                <LoaderCircle className="size-4 shrink-0 animate-spin" />
                {downloading && progress ? (
                  <>
                    <span className="min-w-0 truncate">{progress.text || t("conn.modelDownloading")}</span>
                    <ProgressBar value={progress.pct} className="ml-auto max-w-48 shrink-0" />
                    <span className="tabular w-10 shrink-0 text-right">{Math.round(progress.pct)}%</span>
                  </>
                ) : (
                  <>
                    <span className="shrink-0">{t("conn.modelLoading")}</span>
                    <ProgressBar value={0} indeterminate className="max-w-48" />
                  </>
                )}
              </div>
            )}
          </div>
        </motion.div>
      )}
    </AnimatePresence>
  );
}

/**
 * state=error: модель не скачана или не загрузилась. Причина, «Скачать модель» / «Повторить»
 * (setup_download: уже скачанную модель ядро не качает заново, а сразу перезагружает)
 * и прогресс загрузки — без мастера, который спрятан в «О программе».
 */
function ModelErrorBanner({ progress }: { progress: ProgressEvent | null }) {
  const toast = useToast();
  const setup = useLoad(() => call("setup_status"));
  const [starting, setStarting] = useState(false);
  const downloading = progress !== null && !progress.done;

  useEvent("progress", (e) => {
    if (e.what === "model" && e.done) setup.reload();
  });

  const noModel = setup.data?.model_ready === false;
  const reason = progress?.error ?? null;
  const unknown = !setup.data && setup.loading && !downloading;

  const start = async () => {
    setStarting(true);
    try {
      await call("setup_download", { what: "model" });
    } catch (e) {
      toast({ tone: "error", title: t("conn.downloadError"), description: errorText(e) });
    } finally {
      setStarting(false);
    }
  };

  // Пока не знаем, скачана ли модель, не пугаем «не загрузилась» (ответ приходит за миллисекунды).
  if (unknown) return null;

  return (
    <motion.div
      initial={{ height: 0, opacity: 0 }}
      animate={{ height: "auto", opacity: 1 }}
      exit={{ height: 0, opacity: 0 }}
      transition={{ duration: 0.2 }}
      className="shrink-0 overflow-hidden"
    >
      <div
        role="alert"
        className={cn(
          "border-b border-line px-6 py-2.5 text-[13px]",
          downloading ? "bg-info/8 text-info" : noModel && !reason ? "bg-warn/8 text-warn" : "bg-danger/8 text-danger",
        )}
      >
        {downloading && progress ? (
          <div className="flex items-center gap-3">
            <LoaderCircle className="size-4 shrink-0 animate-spin" />
            <span className="min-w-0 truncate">{progress.text || t("conn.modelDownloading")}</span>
            <ProgressBar value={progress.pct} className="ml-auto max-w-48 shrink-0" />
            <span className="tabular w-10 shrink-0 text-right">{Math.round(progress.pct)}%</span>
          </div>
        ) : (
          <div className="flex items-center gap-2.5">
            {noModel ? <Download className="size-4 shrink-0" /> : <CircleAlert className="size-4 shrink-0" />}
            <div className="min-w-0 flex-1">
              <div className="font-medium">{noModel ? t("conn.noModel") : t("conn.modelFailed")}</div>
              <div className="truncate text-[12.5px] opacity-85" title={reason ?? undefined}>
                {reason ?? (noModel ? t("conn.noModelHint") : t("conn.modelFailedHint"))}
              </div>
            </div>
            {!noModel && (
              <Button
                size="sm"
                variant="ghost"
                icon={<FileText className="size-4" />}
                onClick={() => call("open_logs").catch((e: unknown) => toast({ tone: "error", title: t("errors.generic"), description: errorText(e) }))}
              >
                {t("conn.openLogs")}
              </Button>
            )}
            <Button
              size="sm"
              variant="secondary"
              loading={starting}
              icon={noModel ? <Download className="size-4" /> : <RefreshCw className="size-4" />}
              onClick={() => void start()}
            >
              {noModel ? t("conn.downloadModel") : t("common.retry")}
            </Button>
          </div>
        )}
      </div>
    </motion.div>
  );
}

function Splash({ reconnecting }: { reconnecting: boolean }) {
  const [slow, setSlow] = useState(false);
  useEffect(() => {
    const id = setTimeout(() => setSlow(true), 8000);
    return () => clearTimeout(id);
  }, []);
  return (
    <div className="flex h-full flex-col items-center justify-center gap-5">
      <motion.div
        initial={{ scale: 0.9, opacity: 0 }}
        animate={{ scale: 1, opacity: 1 }}
        transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
      >
        <Logo size={56} live />
      </motion.div>
      <div className="text-center">
        <div className="text-[15px] font-semibold text-fg">
          {reconnecting ? t("conn.reconnecting") : t("conn.connecting")}
        </div>
        <AnimatePresence>
          {slow && (
            <motion.div
              initial={{ opacity: 0, y: 4 }}
              animate={{ opacity: 1, y: 0 }}
              className="mt-1.5 max-w-sm text-[13px] text-muted"
            >
              {t("conn.slow")}
            </motion.div>
          )}
        </AnimatePresence>
      </div>
    </div>
  );
}

function NoParams() {
  const openMock = () => {
    const url = new URL(window.location.href);
    url.searchParams.set("mock", "1");
    window.location.href = url.toString();
  };
  return (
    <div className="flex h-full items-center justify-center p-8">
      <motion.div
        initial={{ opacity: 0, y: 8 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-md rounded-2xl border border-line bg-surface p-7 shadow-card"
      >
        <div className="mb-4 flex size-12 items-center justify-center rounded-2xl bg-accent-soft text-accent-text">
          <PlugZap className="size-6" />
        </div>
        <div className="text-[18px] font-semibold tracking-[-0.01em] text-fg">{t("noParams.title")}</div>
        <p className="mt-2 text-[13.5px] leading-relaxed text-muted">{t("noParams.text")}</p>
        <code className="mt-3 block rounded-lg bg-surface-2 px-3 py-2 font-mono text-[12.5px] text-fg selectable">
          ?port=51234&amp;token=…
        </code>
        <div className="mt-6 flex gap-2">
          <Button variant="primary" icon={<FlaskConical className="size-4" />} onClick={openMock}>
            {t("noParams.mock")}
          </Button>
        </div>
      </motion.div>
    </div>
  );
}
