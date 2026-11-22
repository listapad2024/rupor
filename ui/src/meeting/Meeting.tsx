// Окно перевода встречи: шапка (статус, источник звука, старт/стоп, настройки), живая
// расшифровка с переводом и подвал (копировать, открыть запись, задержка). Окно без фокуса
// (как пилюля): клики и колесо мыши работают, клавиатуры нет — поэтому здесь нет полей ввода.
//
// Второй вид того же окна — карточка «Обнаружена новая встреча» (оболочка ставит окно в
// правый верхний угол, когда ядро прислало meeting_suggest, а панель была скрыта). Видимая
// панель показывает предложение полоской под шапкой.
//
// Переключатель «Расшифровка | Заметки» под шапкой: заметки к встрече (meeting_notes) — после
// «Стоп» сами или по кнопке; время строк в них — кнопка к строке в расшифровке. Заметки пришли,
// а смотрят расшифровку, — отметка на переключателе и «Заметки готовы».
//
// Справа в шапке — «Свернуть» (окно становится полоской поверх всех: последняя строка бегущей
// строкой, развернуть, стоп, скрыть в трей; двойной щелчок — снова панель) и «Закрыть»: без
// сеанса окно просто скрывается, во время перевода — короткий вопрос под шапкой «Свернуть» /
// «Остановить и закрыть». Встреча кончилась (meeting_autostop) — «останавливаю через N с» с
// кнопкой «Не останавливать» и в панели, и в полоске.
import { AnimatePresence, motion } from "framer-motion";
import {
  AppWindow,
  ArrowDown,
  AudioLines,
  CheckCheck,
  ChevronsRight,
  CircleAlert,
  Copy,
  Cpu,
  FileText,
  Languages,
  LoaderCircle,
  Maximize2,
  Minus,
  NotebookPen,
  PhoneOff,
  Play,
  Plus,
  RefreshCw,
  Settings2,
  Sparkles,
  Speaker,
  Square,
  Timer,
  TriangleAlert,
  Unplug,
  Video,
  VolumeX,
  X,
} from "lucide-react";
import {
  memo,
  useCallback,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type PointerEvent as ReactPointerEvent,
  type ReactNode,
} from "react";
import { Switch } from "../components/ui/controls";
import { Select, type SelectOption } from "../components/ui/select";
import { syncLocale, t, tp, useLocale } from "../i18n";
import { cn } from "../lib/cn";
import { fmtDecimal } from "../lib/format";
import { useConnStatus, useEvent } from "../lib/hooks";
import { errorText } from "../lib/hub";
import {
  IDLE_MEETING,
  isDroppedLine,
  lineAt,
  loopbackOff,
  MEETING_LANGS,
  MEETING_SETTINGS_ROUTE,
  SUGGEST_TTL_MS,
  meetingLangInSentence,
  meetingLangName,
  mergeFetchedLines,
  needsSpace,
  pickSource,
  sessionKey,
  sourceUsable,
  splitGrowth,
  startSuggested,
  upsertLine,
  wheelUnpins,
} from "../lib/meeting";
import {
  inTauri,
  meetingSuggestDone,
  meetingWindowHide,
  meetingWindowMinimize,
  meetingWindowMode,
  meetingWindowReset,
  meetingWindowRestore,
  onMeetingWindowMode,
  setWindowTitle,
  showMain,
  watchWindowShown,
  type MeetingWindowMode,
} from "../lib/tauri";
import { applyTheme, onSystemThemeChange } from "../lib/theme";
import type {
  AudioSource,
  CoreState,
  MeetingLang,
  MeetingLine,
  MeetingNotes,
  MeetingState,
  MeetingStatus,
  MeetingSuggest,
  Settings,
  SettingsPatch,
} from "../lib/types";
import { backend, call } from "../lib/ws";
import { NotesView } from "./Notes";
import { setMeetingProtected, startMeetingDrag, watchMeetingMiniPos, watchMeetingRect } from "./window";

/** Сколько строк рисовать сразу; «Показать ранние строки» добавляет ещё столько же. */
const PAGE = 300;
/** Пачка meeting_lines (максимум ядра) и предел строк в памяти (как у ядра). */
const BATCH = 500;
const KEEP = 5000;
/** Ближе стольких px к низу — «внизу»: новые строки прокручиваются сами. */
const BOTTOM_SLACK = 40;
/** meeting_start ждёт загрузки модели, если её ещё нет. */
const START_TIMEOUT_MS = 120_000;
/** Прокрутка за этим временем после жеста пользователя (колесо, полоса прокрутки) — его. */
const USER_SCROLL_MS = 800;
/** Без оболочки (демо в браузере) окно — всегда панель (демо полоски — meeting.html?mock=1&mini=1). */
const FULL_MODE: MeetingWindowMode = { mode: "full", suggestion: null, hide_in_ms: null };
const MINI_MODE: MeetingWindowMode = { mode: "mini", suggestion: null, hide_in_ms: null };
/** Полоска после конца сеанса скрывается сама через столько (заметки пришли — ещё столько же). */
const MINI_HIDE_MS = 20_000;
/** Бегущая строка полоски — хвост строки не длиннее стольких символов (новые слова видны). */
const TICKER_CHARS = 120;
/** Строка, к которой перешли из заметок, подсвечивается столько. */
const JUMP_FLASH_MS = 1800;

type Tone = "ok" | "warn" | "muted" | "err" | "idle";
type Flash = { tone: "ok" | "err"; text: string } | null;
type View = "transcript" | "notes";
/** Переход к строке из заметок: id строки и номер перехода (тот же id ещё раз — снова). */
type Jump = { id: number; n: number } | null;

/** Вид окна (расшифровка или заметки) — помнит само окно; хранилища нет — расшифровка. */
function savedView(): View {
  try {
    return window.localStorage.getItem("rupor.meeting.view") === "notes" ? "notes" : "transcript";
  } catch {
    return "transcript";
  }
}

function saveView(v: View): void {
  try {
    window.localStorage.setItem("rupor.meeting.view", v);
  } catch {
    // окно без хранилища — вид не запоминается
  }
}

/** Заметки в статусе и в окне — одни и те же (иначе окно перечитает их у ядра). */
function sameNotes(info: MeetingStatus["notes"], n: MeetingNotes | null): boolean {
  if (!info || !n) return !info && !n;
  return info.status === n.status && info.generated_at === n.generated_at && info.error === n.error;
}

function sourceLabel(s: AudioSource): string {
  return s.title && s.kind === "process" ? `${s.name} — ${s.title}` : s.name;
}

function reducedMotion(): boolean {
  return typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/** Демо в браузере: &mini=1 — сразу полоска. */
function demoStartMode(): MeetingWindowMode {
  if (inTauri()) return FULL_MODE;
  return new URLSearchParams(window.location.search).get("mini") === "1" ? MINI_MODE : FULL_MODE;
}

/** Хвост текста для бегущей строки: последние слова видны, начало — «…». */
function tickerTail(text: string): string {
  const s = text.replace(/\s+/g, " ").trim();
  if (s.length <= TICKER_CHARS) return s;
  const cut = s.slice(-TICKER_CHARS);
  const sp = cut.indexOf(" ");
  return `…${sp > 0 && sp < 30 ? cut.slice(sp + 1) : cut}`;
}

/**
 * Обратный отсчёт по секундам от ядра (MeetingStatus.ending_in_s): между событиями meeting
 * считает сам, каждое новое значение — заново. null — отсчёта нет.
 */
function useCountdown(seconds: number | null): number | null {
  const [deadline, setDeadline] = useState<number | null>(null);
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t0 = Date.now();
    setNow(t0);
    setDeadline(seconds === null ? null : t0 + seconds * 1000);
  }, [seconds]);
  useEffect(() => {
    if (deadline === null) return;
    const id = setInterval(() => setNow(Date.now()), 250);
    return () => clearInterval(id);
  }, [deadline]);
  if (deadline === null || seconds === null) return null;
  return Math.max(0, Math.min(seconds, Math.ceil((deadline - now) / 1000)));
}

/** Задержка в секундах с одним знаком: «0,9 с». */
function fmtLatency(ms: number | null): string | null {
  return ms === null ? null : t("time.sec", { n: fmtDecimal(Math.max(0, ms) / 1000, 1) });
}

export function Meeting() {
  const conn = useConnStatus();
  const locale = useLocale();
  const [core, setCore] = useState<CoreState | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [status, setStatus] = useState<MeetingStatus>(IDLE_MEETING);
  const [lines, setLines] = useState<MeetingLine[]>([]);
  // выброшенные ядром строки (шум) — в списке, но не на экране и не в счётчике
  const shownLines = useMemo(() => lines.filter((l) => !isDroppedLine(l)), [lines]);
  const [sources, setSources] = useState<AudioSource[] | null>(null);
  const [picked, setPicked] = useState("auto");
  const [busy, setBusy] = useState<"start" | "stop" | null>(null);
  const [flash, setFlash] = useState<Flash>(null);
  const [gear, setGear] = useState(false);
  const [shown, setShown] = useState(true);
  const [winMode, setWinMode] = useState<MeetingWindowMode>(demoStartMode);
  /** «Закрыть» во время перевода — вопрос под шапкой: свернуть или остановить и закрыть. */
  const [confirmClose, setConfirmClose] = useState(false);
  /** Предложения, на которые уже ответили здесь (оболочка снимет их чуть позже). */
  const [answered, setAnswered] = useState<ReadonlySet<string>>(() => new Set());
  const [view, setViewState] = useState<View>(savedView);
  const [notes, setNotes] = useState<MeetingNotes | null>(null);
  /** Заметки готовы, а смотрят расшифровку — отметка на переключателе. */
  const [notesUnread, setNotesUnread] = useState(false);
  const [notesAsked, setNotesAsked] = useState(false);
  const [jump, setJump] = useState<Jump>(null);
  const viewRef = useRef<View>(view);
  const sessionRef = useRef("");
  const flashTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    backend.start();
  }, []);

  const showFlash = useCallback((tone: "ok" | "err", text: string) => {
    if (flashTimer.current) clearTimeout(flashTimer.current);
    setFlash({ tone, text });
    flashTimer.current = setTimeout(() => setFlash(null), tone === "err" ? 7000 : 3500);
  }, []);
  useEffect(
    () => () => {
      if (flashTimer.current) clearTimeout(flashTimer.current);
    },
    [],
  );

  // Вид окна (панель или карточка предложения) — от оболочки: при загрузке и по событию
  useEffect(() => {
    if (!inTauri()) return;
    let alive = true;
    meetingWindowMode()
      .then((m) => alive && setWinMode(m))
      .catch((e: unknown) => console.warn("meeting_window_mode", e));
    const off = onMeetingWindowMode(setWinMode);
    return () => {
      alive = false;
      off();
    };
  }, []);

  // Строки сеанса — пачками по BATCH (после подключения, переподключения и нового сеанса).
  // События meeting_line, пришедшие тем временем, не теряются: слияние по id.
  const loadLines = useCallback(async (key: string) => {
    let after = 0;
    let all: MeetingLine[] = [];
    try {
      for (let i = 0; i < Math.ceil(KEEP / BATCH) + 1; i++) {
        const batch = await call("meeting_lines", { after_id: after, limit: BATCH });
        if (sessionRef.current !== key) return;
        all = all.concat(batch);
        const last = batch[batch.length - 1];
        if (batch.length < BATCH || !last) break;
        after = last.id;
      }
    } catch (e) {
      console.warn("meeting_lines", e);
    }
    if (sessionRef.current !== key) return;
    setLines((prev) => mergeFetchedLines(prev, all).slice(-KEEP));
  }, []);

  // Заметки сеанса: при подключении, новом сеансе и когда статус говорит о других заметках
  // (событие meeting_notes могло не дойти — окно было закрыто, ядро перезапускалось)
  const notesRef = useRef<MeetingNotes | null>(null);
  const loadNotes = useCallback(async (key: string) => {
    try {
      const n = await call("meeting_notes_get");
      if (sessionRef.current !== key) return;
      notesRef.current = n;
      setNotes(n);
    } catch (e) {
      console.warn("meeting_notes_get", e);
    }
  }, []);

  /** Новый статус; другой сеанс (новый started_at) — строки и заметки заново. */
  const applyStatus = useCallback(
    (s: MeetingStatus, reload = false) => {
      setStatus(s);
      const key = sessionKey(s);
      if (!key) return;
      if (key !== sessionRef.current) {
        sessionRef.current = key;
        setLines([]);
        notesRef.current = null;
        setNotes(null);
        setNotesUnread(false);
        void loadLines(key);
        if (s.notes) void loadNotes(key);
      } else if (reload) {
        void loadLines(key);
        void loadNotes(key);
      }
    },
    [loadLines, loadNotes],
  );
  const notesInfo = status.notes ?? null;
  useEffect(() => {
    const key = sessionRef.current;
    if (key && !sameNotes(notesInfo, notesRef.current)) void loadNotes(key);
  }, [notesInfo, loadNotes]);

  const refreshSources = useCallback(() => {
    call("meeting_sources")
      .then(setSources)
      .catch((e: unknown) => console.warn("meeting_sources", e));
  }, []);

  // Подключение и переподключение (новое ядро — строки и источники заново)
  useEffect(() => {
    if (conn !== "open") return;
    call("get_state").then(setCore).catch(() => {});
    call("get_settings").then(setSettings).catch(() => {});
    call("meeting_status")
      .then((s) => applyStatus(s, true))
      .catch((e: unknown) => console.warn("meeting_status", e));
    refreshSources();
  }, [conn, applyStatus, refreshSources]);

  useEvent("hello", (s) => {
    setCore(s);
    if (s.meeting) applyStatus(s.meeting);
  });
  useEvent("state", (s) => {
    setCore(s);
    if (s.meeting) applyStatus(s.meeting);
  });
  useEvent("settings", setSettings);
  useEvent("meeting", (s) => applyStatus(s));
  useEvent("meeting_line", (l) => setLines((prev) => upsertLine(prev, l).slice(-KEEP)));
  useEvent("meeting_notes", (n) => {
    const prev = notesRef.current;
    notesRef.current = n;
    setNotes(n);
    // новые заметки готовы, а смотрят расшифровку — отметка на переключателе
    const fresh = n.status === "done" && (prev?.status !== "done" || prev.generated_at !== n.generated_at);
    if (fresh && viewRef.current !== "notes") {
      setNotesUnread(true);
      showFlash("ok", t("meeting.notes.ready"));
    }
  });
  // В Tauri предложение приносит оболочка (главное окно → meeting_suggest_show → вид окна);
  // без неё (демо meeting.html?mock=1&suggest=1, полоской в панели — &suggest=banner) окно
  // показывает его само.
  useEvent("meeting_suggest", (sg) => {
    if (inTauri()) return;
    const banner = status.running || new URLSearchParams(window.location.search).get("suggest") === "banner";
    setWinMode({ mode: banner ? "full" : "suggest", suggestion: sg, hide_in_ms: SUGGEST_TTL_MS });
  });
  const demoHideIn = !inTauri() && winMode.suggestion ? winMode.hide_in_ms : null;
  useEffect(() => {
    if (demoHideIn === null) return;
    const id = setTimeout(() => setWinMode(FULL_MODE), demoHideIn);
    return () => clearTimeout(id);
  }, [demoHideIn, winMode.suggestion]);

  // Окно показали — список источников свежий; пока сеанса нет и окно видно — раз в 5 с
  // (вкладку со встречей могли открыть только что).
  useEffect(
    () =>
      watchWindowShown((v) => {
        setShown(v);
        if (v && backend.status() === "open") refreshSources();
      }),
    [refreshSources],
  );
  const card = winMode.mode === "suggest";
  const mini = winMode.mode === "mini";
  useEffect(() => {
    if (!shown || card || mini || status.running || conn !== "open") return;
    const id = setInterval(refreshSources, 5000);
    return () => clearInterval(id);
  }, [shown, card, mini, status.running, conn, refreshSources]);

  // Язык, заголовок, тема и акцент — как в главном окне
  const langPref = settings?.language;
  const uiLanguage = core?.ui_language;
  useEffect(() => syncLocale(langPref, uiLanguage), [langPref, uiLanguage]);
  useEffect(() => void setWindowTitle(t("meeting.windowTitle")), [locale]);
  const theme = settings?.theme;
  const accent = settings?.accent;
  useEffect(() => {
    if (!theme || !accent) return;
    applyTheme(theme, accent);
    if (theme !== "system") return;
    return onSystemThemeChange(() => applyTheme(theme, accent));
  }, [theme, accent]);
  const protect = settings?.meeting_protect;
  useEffect(() => {
    if (protect !== undefined) void setMeetingProtected(protect);
  }, [protect]);

  // Место и размер окна — в meeting_rect, место полоски — в meeting_mini_pos (присылает Rust
  // после перетаскивания)
  useEffect(
    () =>
      watchMeetingRect((rect) => {
        call("set_settings", { patch: { meeting_rect: rect } }).catch((e: unknown) => console.warn("meeting_rect", e));
      }),
    [],
  );
  useEffect(
    () =>
      watchMeetingMiniPos((pos) => {
        call("set_settings", { patch: { meeting_mini_pos: pos } }).catch((e: unknown) =>
          console.warn("meeting_mini_pos", e),
        );
      }),
    [],
  );

  const patch = useCallback(
    (p: SettingsPatch) => {
      setSettings((s) => (s ? { ...s, ...p } : s));
      call("set_settings", { patch: p })
        .then(setSettings)
        .catch((e: unknown) => {
          showFlash("err", errorText(e));
          call("get_settings").then(setSettings).catch(() => {});
        });
    },
    [showFlash],
  );

  // Звук одной программы захватить нельзя (Windows 10): программы в выборе неактивны, по
  // умолчанию — весь звук компьютера, предложение встречи начинает с него же
  const off = loopbackOff(status, sources);

  const start = async (id: string) => {
    if (busy) return;
    setBusy("start");
    try {
      applyStatus(await call("meeting_start", id === "auto" ? {} : { source_id: id }, { timeoutMs: START_TIMEOUT_MS }));
    } catch (e) {
      showFlash("err", `${t("meeting.startFailed")}: ${errorText(e)}`);
      refreshSources();
    } finally {
      setBusy(null);
    }
  };
  const stop = async () => {
    if (busy) return;
    setBusy("stop");
    try {
      applyStatus(await call("meeting_stop"));
    } catch (e) {
      showFlash("err", errorText(e));
    } finally {
      setBusy(null);
    }
  };
  /** «Не останавливать»: снять отсчёт автоостановки (до нового микрофона или речи). */
  const keepGoing = () => {
    call("meeting_autostop_cancel").then(
      (s) => applyStatus(s),
      (e: unknown) => showFlash("err", errorText(e)),
    );
  };

  // ——— Свернуть, закрыть, полоска ———

  /** «Свернуть»: окно — полоской поверх всех (в демо — тот же вид в рамке). */
  const minimize = () => {
    setConfirmClose(false);
    setGear(false);
    if (inTauri()) meetingWindowMinimize().catch((e: unknown) => showFlash("err", errorText(e)));
    else setWinMode(MINI_MODE);
  };
  /** Полоску — снова панелью на её месте (двойной щелчок по полоске, «Развернуть»). */
  const expand = () => {
    if (inTauri()) meetingWindowRestore().catch((e: unknown) => console.warn("meeting_window_restore", e));
    else setWinMode(FULL_MODE);
  };
  const hideWindow = () => {
    setConfirmClose(false);
    meetingWindowHide().catch((e: unknown) => console.warn("meeting_window_hide", e));
  };
  /** «Закрыть»: без сеанса — скрыть; во время перевода — спросить (свернуть или остановить). */
  const askClose = () => {
    if (status.running) setConfirmClose((v) => !v);
    else hideWindow();
  };
  const stopAndClose = async () => {
    setConfirmClose(false);
    await stop();
    hideWindow();
  };
  // сеанс кончился, пока висел вопрос, — вопрос больше не нужен
  useEffect(() => {
    if (!status.running) setConfirmClose(false);
  }, [status.running]);

  const copy = () => {
    call("meeting_copy", { what: "transcript" }).then(
      (r) => showFlash("ok", r.chars > 0 ? t("meeting.copied") : t("meeting.copyEmpty")),
      (e: unknown) => showFlash("err", errorText(e)),
    );
  };

  // ——— Заметки ———

  const setView = useCallback((v: View) => {
    viewRef.current = v;
    setViewState(v);
    saveView(v);
    if (v === "notes") setNotesUnread(false);
  }, []);
  /**
   * «Сделать заметки сейчас» / «Обновить» / «Повторить». Что с ними — событиями meeting_notes
   * (ответ команды может прийти позже события о готовых заметках — его не применяем).
   */
  const makeNotes = async () => {
    if (notesAsked) return;
    setNotesAsked(true);
    try {
      await call("meeting_notes");
    } catch (e) {
      showFlash("err", errorText(e));
    } finally {
      setNotesAsked(false);
    }
  };
  const copyNotes = () => {
    call("meeting_copy", { what: "notes" }).then(
      () => showFlash("ok", t("meeting.notes.copied")),
      (e: unknown) => showFlash("err", errorText(e)),
    );
  };
  // время строки в заметках — к этой строке в расшифровке
  const jumpN = useRef(0);
  const onJump = (ts: string) => {
    const line = lineAt(shownLines, ts);
    if (!line) return;
    jumpN.current += 1;
    setJump({ id: line.id, n: jumpN.current });
    setView("transcript");
  };
  const openFile = () => {
    call("meeting_open_folder").catch((e: unknown) => showFlash("err", errorText(e)));
  };
  const dock = () => {
    patch({ meeting_rect: null });
    meetingWindowReset().catch((e: unknown) => showFlash("err", errorText(e)));
  };

  // ——— Предложение встречи ———

  /** Ответили здесь: карточку скрывает (полоску снимает) оболочка, в демо — само окно. */
  const settle = (sg: MeetingSuggest) => {
    setAnswered((prev) => new Set(prev).add(sg.id));
    if (inTauri()) void meetingSuggestDone(sg.id);
    else setWinMode(FULL_MODE);
  };
  const suggestStart = async (sg: MeetingSuggest) => {
    if (busy) return;
    setAnswered((prev) => new Set(prev).add(sg.id));
    // без оболочки окно — уже панель: только вид
    if (!inTauri()) setWinMode(FULL_MODE);
    setBusy("start");
    try {
      applyStatus(await startSuggested(sg, { showWindow: inTauri(), systemOnly: off }));
    } catch (e) {
      showFlash("err", `${t("meeting.startFailed")}: ${errorText(e)}`);
      refreshSources();
    } finally {
      setBusy(null);
      settle(sg);
    }
  };
  const suggestLater = (sg: MeetingSuggest) => {
    call("meeting_suggest_answer", { id: sg.id, answer: "later" }).catch((e: unknown) =>
      console.warn("meeting_suggest_answer", e),
    );
    settle(sg);
  };
  const suggestNever = (sg: MeetingSuggest) => {
    const inPanel = !card;
    const ignored = settings?.meeting_ignore_apps ?? [];
    const exe = sg.source.exe;
    call("meeting_suggest_answer", { id: sg.id, answer: "never" })
      .catch(async (e: unknown) => {
        // предложение ядро уже забыло (устарело, ядро перезапускалось) — то же настройкой
        if (!exe) throw e;
        if (!ignored.some((x) => x.toLowerCase() === exe.toLowerCase())) {
          await call("set_settings", { patch: { meeting_ignore_apps: [...ignored, exe] } });
        }
        return null;
      })
      .then(
        () => inPanel && showFlash("ok", t("meeting.suggest.neverDone", { app: sg.app })),
        (e: unknown) => showFlash("err", errorText(e)),
      );
    settle(sg);
  };
  const suggestion = winMode.suggestion && !answered.has(winMode.suggestion.id) ? winMode.suggestion : null;

  // Перетаскивание за шапку: сдвиг больше 4 px — перенос (щелчок по шапке окно не двигает)
  const down = useRef<{ x: number; y: number; dragging: boolean } | null>(null);
  const onHeadDown = (e: ReactPointerEvent) => {
    if (e.button !== 0 || (e.target as HTMLElement).closest("button, [role=listbox], [data-no-drag]")) {
      down.current = null;
      return;
    }
    down.current = { x: e.screenX, y: e.screenY, dragging: false };
  };
  const onHeadMove = (e: ReactPointerEvent) => {
    const d = down.current;
    if (!d || d.dragging || !(e.buttons & 1)) return;
    if (Math.hypot(e.screenX - d.x, e.screenY - d.y) > 4) {
      d.dragging = true;
      void startMeetingDrag();
    }
  };

  const offline = conn !== "open";
  const running = status.running;
  const state: MeetingState | "offline" = offline ? "offline" : status.state;
  const tone: Tone =
    state === "listening"
      ? "ok"
      : state === "waiting_gpu"
        ? "warn"
        : state === "no_audio"
          ? "muted"
          : state === "error" || state === "offline"
            ? "err"
            : "idle";
  const stateText = state === "offline" ? t("meeting.state.offline") : t(`meeting.state.${state}`);
  const fontSize = settings?.meeting_font_size ?? 16;
  const showOriginal = settings?.meeting_show_original ?? true;

  // Источник: идёт сеанс — его источник, иначе выбранный (по умолчанию — «Автоматически», а
  // если программы захватить нельзя — «Весь звук компьютера»: «Автоматически» тогда не
  // предлагается, программы видны неактивными)
  const list = [...(sources ?? [])];
  if (status.source && !list.some((s) => s.id === status.source?.id)) list.unshift(status.source);
  const pick = pickSource(picked, list, off);
  const value = running && status.source ? status.source.id : pick;
  const options: SelectOption<string>[] = [
    ...(off
      ? []
      : [{ value: "auto", label: t("meeting.source.auto"), hint: t("meeting.source.autoHint"), icon: <Sparkles /> }]),
    ...list.map((s) => {
      const usable = sourceUsable(s, off) || (running && s.id === status.source?.id);
      return {
        value: s.id,
        label: sourceLabel(s),
        hint:
          s.kind === "system"
            ? t("meeting.source.systemHint")
            : !usable
              ? t("meeting.source.needsWin11")
              : s.meeting
                ? t("meeting.source.meeting")
                : s.active
                  ? t("meeting.source.playing")
                  : t("meeting.source.silent"),
        icon: s.kind === "system" ? <Speaker /> : s.meeting ? <Video /> : <AppWindow />,
        disabled: !usable,
      };
    }),
  ];
  const onPick = (v: string) => {
    setPicked(v);
    if (running && v !== status.source?.id) void start(v);
  };

  // «Идёт диктовка» — подсказкой, только если ждём дольше 1,5 с: короткая диктовка не дёргает ленту
  const [gpuHint, setGpuHint] = useState(false);
  useEffect(() => {
    if (state !== "waiting_gpu") {
      setGpuHint(false);
      return;
    }
    const id = setTimeout(() => setGpuHint(true), 1500);
    return () => clearTimeout(id);
  }, [state]);

  // Встреча кончилась (meeting_autostop): «останавливаю через N с» и «Не останавливать»;
  // «Встреча, похоже, закончилась» (микрофон свободен > 30 с и звука нет) — с кнопкой «Стоп»
  const ending = running && !offline && status.hint === "ending";
  const endingLeft = useCountdown(ending ? (status.ending_in_s ?? 0) : null);
  const endingText = ending ? t("meeting.stateHint.ending", { n: endingLeft ?? status.ending_in_s ?? 0 }) : "";
  const ended = running && !offline && status.hint === "ended";
  type Hint = { id: string; tone: "warn" | "muted" | "err"; icon: ReactNode; text: string; stop?: boolean; keep?: boolean };
  const hint: Hint | null = ending
    ? { id: "ending", tone: "warn", icon: <PhoneOff />, text: endingText, keep: true }
    : ended
      ? { id: "ended", tone: "muted", icon: <PhoneOff />, text: t("meeting.stateHint.ended"), stop: true }
      : state === "waiting_gpu" && gpuHint
        ? { id: "gpu", tone: "warn", icon: <Cpu />, text: t("meeting.stateHint.waiting_gpu") }
        : state === "no_audio"
          ? { id: "no_audio", tone: "muted", icon: <VolumeX />, text: t("meeting.stateHint.no_audio") }
          : state === "error" && status.error
            ? { id: `err:${status.error}`, tone: "err", icon: <CircleAlert />, text: status.error }
            : null;

  // Полоска после конца сеанса — «Перевод остановлен» (и «Заметки готовы»), потом скрывается
  // сама; заметки пришли — ещё MINI_HIDE_MS, чтобы их отметку успели увидеть
  const notesDone = notes?.status === "done" ? (notes.generated_at ?? "done") : null;
  const notesRunning = notes?.status === "running";
  useEffect(() => {
    if (!mini || running || notesRunning || !inTauri()) return;
    const id = setTimeout(() => void meetingWindowHide().catch(() => undefined), MINI_HIDE_MS);
    return () => clearTimeout(id);
  }, [mini, running, notesRunning, notesDone]);

  const translateOn = settings?.meeting_translate ?? status.translate;
  const targetLang = settings?.meeting_target_lang ?? status.target_lang;

  // Полоска свёрнутого перевода — окно целиком (в демо — рамкой окна в углу вкладки)
  if (mini) {
    const last = shownLines[shownLines.length - 1];
    const lineText = last ? last.translation?.trim() || [last.text, last.partial].filter(Boolean).join(" ") : "";
    const bar = (
      <MiniBar
        tone={tone}
        level={running && !offline ? status.level : 0}
        running={running}
        offline={offline}
        stateText={stateText}
        source={running && status.source && !offline ? sourceLabel(status.source) : ""}
        ticker={running ? tickerTail(lineText) || t("meeting.mini.listening") : t("meeting.mini.stopped")}
        endingIn={ending ? (endingLeft ?? status.ending_in_s ?? 0) : null}
        notesReady={!running && notes?.status === "done"}
        busy={busy !== null}
        onExpand={expand}
        onStop={() => void stop()}
        onHide={hideWindow}
        onKeep={keepGoing}
        onPointerDown={onHeadDown}
        onPointerMove={onHeadMove}
      />
    );
    return inTauri() ? (
      bar
    ) : (
      <div className="meeting-card-demo">
        <div className="meeting-mini-frame">{bar}</div>
      </div>
    );
  }

  // Карточка «Обнаружена новая встреча» — окно целиком (в демо — рамкой окна в углу вкладки)
  if (card) {
    const body = suggestion ? (
      <SuggestCard
        key={suggestion.id}
        s={suggestion}
        translate={translateOn}
        target={targetLang}
        hideInMs={winMode.hide_in_ms}
        busy={busy === "start"}
        disabled={offline}
        onStart={() => void suggestStart(suggestion)}
        onLater={() => suggestLater(suggestion)}
        onNever={() => suggestNever(suggestion)}
      />
    ) : null;
    return inTauri() ? (
      <div className="meeting-card-shell">{body}</div>
    ) : (
      <div className="meeting-card-demo">
        <div className="meeting-card-frame">{body}</div>
      </div>
    );
  }

  const lat = status.latency ?? null;
  const asrLat = fmtLatency(lat?.asr_ms ?? null);
  const trLat = status.translate ? fmtLatency(lat?.first_token_ms ?? null) : null;
  const doneLat = status.translate ? fmtLatency(lat?.done_ms ?? null) : null;
  const latTitle = t("meeting.latency.hint", { asr: asrLat ?? "—", first: trLat ?? "—", done: doneLat ?? "—" });
  // Название записи (по содержанию или без ИИ) — вместо «Перевод встречи»; подсказка — оно
  // целиком и путь к файлу (имя файла меняется вместе с названием)
  const recordTitle = status.title ?? null;
  const recordTip = recordTitle ? [recordTitle, status.file].filter(Boolean).join("\n") : undefined;

  return (
    <div className="meeting-shell" style={{ ["--mf" as string]: `${fontSize}px` }}>
      <header className="meeting-head" onPointerDown={onHeadDown} onPointerMove={onHeadMove}>
        <div className="flex items-center gap-3 px-4 pb-2.5 pt-3">
          <StatusMark tone={tone} level={running && !offline ? status.level : 0} />
          <div className="min-w-0 flex-1">
            <div
              className="truncate text-[13.5px] font-semibold tracking-[-0.01em] text-fg"
              title={recordTip}
            >
              {recordTitle ?? t("meeting.title")}
            </div>
            <div className={cn("flex min-w-0 items-center gap-1.5 text-[12px]", `meeting-tone-${tone}`)}>
              {offline && <Unplug className="size-3 shrink-0" />}
              <span className="shrink-0 font-medium">{stateText}</span>
              {running && status.source && !offline && (
                <span className="min-w-0 truncate text-subtle" title={sourceLabel(status.source)}>
                  · {sourceLabel(status.source)}
                </span>
              )}
            </div>
          </div>
          <HeadButton label={t("meeting.settings")} active={gear} gearToggle onClick={() => setGear((g) => !g)}>
            <Settings2 />
          </HeadButton>
          <span className="meeting-head-sep" aria-hidden />
          <HeadButton label={t("meeting.minimize")} onClick={minimize}>
            <Minus />
          </HeadButton>
          <HeadButton label={t("meeting.close")} active={confirmClose} onClick={askClose} danger>
            <X />
          </HeadButton>
        </div>
        <div className="flex items-center gap-2 px-4 pb-3" data-no-drag>
          <div className="min-w-0 flex-1">
            <Select
              value={value}
              options={options}
              onChange={onPick}
              size="sm"
              className="w-full"
              minWidth={260}
              disabled={offline || busy !== null}
            />
          </div>
          <HeadButton label={t("meeting.source.refresh")} onClick={refreshSources} disabled={offline}>
            <RefreshCw />
          </HeadButton>
          {running ? (
            <button
              type="button"
              tabIndex={-1}
              disabled={offline || busy !== null}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => void stop()}
              className="meeting-btn meeting-btn-stop"
            >
              {busy === "stop" ? <LoaderCircle className="animate-spin" /> : <Square className="fill-current" />}
              {t("meeting.stop")}
            </button>
          ) : (
            <button
              type="button"
              tabIndex={-1}
              disabled={offline || busy !== null}
              onMouseDown={(e) => e.preventDefault()}
              onClick={() => void start(pick)}
              className="meeting-btn meeting-btn-start"
            >
              {busy === "start" ? <LoaderCircle className="animate-spin" /> : <Play className="fill-current" />}
              {busy === "start" ? t("meeting.starting") : t("meeting.start")}
            </button>
          )}
        </div>
        <div className="meeting-tabs" role="tablist" data-no-drag>
          <ViewTab on={view === "transcript"} onClick={() => setView("transcript")} icon={<AudioLines />}>
            {t("meeting.view.transcript")}
          </ViewTab>
          <ViewTab on={view === "notes"} onClick={() => setView("notes")} icon={<NotebookPen />}>
            {t("meeting.view.notes")}
            {notes?.status === "running" && (
              <LoaderCircle className="meeting-tab-spin animate-spin" aria-label={t("meeting.notes.running")} />
            )}
            {notesUnread && notes?.status !== "running" && (
              <span className="meeting-tab-badge" role="img" aria-label={t("meeting.notes.ready")} title={t("meeting.notes.ready")} />
            )}
          </ViewTab>
        </div>
        <AnimatePresence initial={false}>
          {suggestion && !running && (
            <motion.div
              key={`s:${suggestion.id}`}
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
              data-no-drag
            >
              <SuggestCard
                banner
                s={suggestion}
                translate={translateOn}
                target={targetLang}
                hideInMs={winMode.hide_in_ms}
                busy={busy === "start"}
                disabled={offline || busy !== null}
                onStart={() => void suggestStart(suggestion)}
                onLater={() => suggestLater(suggestion)}
                onNever={() => suggestNever(suggestion)}
              />
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence initial={false}>
          {confirmClose && running && (
            <motion.div
              key="close"
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.16 }}
              className="overflow-hidden"
              data-no-drag
            >
              <div className="meeting-confirm" role="alertdialog" aria-label={t("meeting.closeConfirm.running")}>
                <span className="meeting-confirm-text">{t("meeting.closeConfirm.running")}</span>
                <span className="meeting-confirm-actions">
                  <button
                    type="button"
                    tabIndex={-1}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={minimize}
                    className="meeting-btn meeting-btn-start meeting-btn-sm"
                  >
                    <Minus />
                    {t("meeting.closeConfirm.minimize")}
                  </button>
                  <button
                    type="button"
                    tabIndex={-1}
                    disabled={busy !== null}
                    onMouseDown={(e) => e.preventDefault()}
                    onClick={() => void stopAndClose()}
                    className="meeting-btn meeting-btn-quiet meeting-btn-sm"
                  >
                    {t("meeting.closeConfirm.stopClose")}
                  </button>
                </span>
              </div>
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence initial={false}>
          {(hint || flash) && (
            <motion.div
              key={flash ? `f:${flash.text}` : `h:${hint?.id ?? ""}`}
              initial={{ opacity: 0, height: 0 }}
              animate={{ opacity: 1, height: "auto" }}
              exit={{ opacity: 0, height: 0 }}
              transition={{ duration: 0.18 }}
              className="overflow-hidden"
            >
              {flash ? (
                <div className={cn("meeting-hint", flash.tone === "err" ? "meeting-hint-err" : "meeting-hint-ok")}>
                  {flash.tone === "err" ? <TriangleAlert /> : <Copy />}
                  <span className="min-w-0 flex-1">{flash.text}</span>
                </div>
              ) : hint ? (
                <div className={cn("meeting-hint", `meeting-hint-${hint.tone}`)} data-no-drag>
                  {hint.icon}
                  <span className="min-w-0 flex-1">{hint.text}</span>
                  {hint.stop && (
                    <button
                      type="button"
                      tabIndex={-1}
                      disabled={busy !== null}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={() => void stop()}
                      className="meeting-hint-btn"
                    >
                      <Square className="fill-current" />
                      {t("meeting.stop")}
                    </button>
                  )}
                  {hint.keep && (
                    <button
                      type="button"
                      tabIndex={-1}
                      onMouseDown={(e) => e.preventDefault()}
                      onClick={keepGoing}
                      className="meeting-hint-btn meeting-hint-btn-keep"
                    >
                      <Play className="fill-current" />
                      {t("meeting.keepGoing")}
                    </button>
                  )}
                </div>
              ) : null}
            </motion.div>
          )}
        </AnimatePresence>
        <AnimatePresence>
          {gear && settings && (
            <GearPanel
              s={settings}
              patch={patch}
              onDock={dock}
              onClose={() => setGear(false)}
              onAllSettings={() => {
                setGear(false);
                void showMain(MEETING_SETTINGS_ROUTE);
              }}
            />
          )}
        </AnimatePresence>
      </header>

      <Transcript
        lines={shownLines}
        showOriginal={showOriginal}
        running={running}
        sourceName={status.source ? sourceLabel(status.source) : ""}
        session={sessionKey(status)}
        systemOnly={off}
        inactive={view !== "transcript"}
        jump={jump}
      />
      {view === "notes" && (
        <div className="relative min-h-0 flex-1">
          <div className="meeting-body">
            <NotesView
              notes={notes}
              hasLines={shownLines.length > 0}
              running={running}
              autoOn={settings?.meeting_notes ?? true}
              offline={offline}
              starting={notesAsked}
              onMake={() => void makeNotes()}
              onJump={onJump}
            />
          </div>
        </div>
      )}

      <footer className="meeting-foot">
        <span className="flex min-w-0 items-center gap-2 overflow-hidden text-[12px] text-subtle">
          <span className="tabular shrink-0">{tp("units.lines", shownLines.length)}</span>
          {running && asrLat && (
            <span className="meeting-lat tabular" title={latTitle} aria-label={latTitle}>
              <Timer />
              {asrLat}
              {trLat && <span className="meeting-lat-tr">→ {trLat}</span>}
            </span>
          )}
          {running && !status.translate && (
            <span className="meeting-chip min-w-0 truncate" title={t("meeting.transcriptOnly")}>
              {t("meeting.transcriptOnly")}
            </span>
          )}
          {!running && shownLines.length > 0 && <span className="truncate">· {t("meeting.stopped")}</span>}
        </span>
        <span className="flex shrink-0 items-center gap-1">
          {view === "notes" ? (
            <FootButton
              onClick={copyNotes}
              disabled={offline || !notes?.markdown || notes.status === "running"}
              icon={<Copy />}
            >
              {t("meeting.notes.copy")}
            </FootButton>
          ) : (
            <FootButton onClick={copy} disabled={offline || shownLines.length === 0} icon={<Copy />}>
              {t("meeting.copy")}
            </FootButton>
          )}
          <FootButton onClick={openFile} disabled={offline} icon={<FileText />}>
            {t("meeting.openFile")}
          </FootButton>
        </span>
      </footer>
    </div>
  );
}

/**
 * Предложение начать расшифровку: карточкой (окно целиком) или полоской в панели (banner).
 * Говорит, будет ли перевод: язык перевода или «только расшифровка».
 */
function SuggestCard({
  s,
  translate,
  target,
  hideInMs,
  busy,
  disabled,
  banner,
  onStart,
  onLater,
  onNever,
}: {
  s: MeetingSuggest;
  translate: boolean;
  target: MeetingLang;
  hideInMs: number | null;
  busy: boolean;
  disabled?: boolean;
  banner?: boolean;
  onStart: () => void;
  onLater: () => void;
  onNever: () => void;
}) {
  const where = s.title && s.title !== s.app ? `${s.app} — ${s.title}` : s.app;
  return (
    <section className={banner ? "meeting-suggest meeting-suggest-banner" : "meeting-suggest meeting-suggest-card"}>
      <div className="flex min-w-0 items-start gap-3">
        <span className="meeting-suggest-icon" aria-hidden>
          <Video />
        </span>
        <div className="min-w-0 flex-1">
          <div className="meeting-suggest-title">{t("meeting.suggest.title")}</div>
          <div className="meeting-suggest-where" title={where}>
            {where}
          </div>
          <div className="meeting-suggest-mode">
            {translate ? <Languages /> : <FileText />}
            <span className="min-w-0 truncate">
              {translate
                ? t("meeting.suggest.translate", { lang: meetingLangInSentence(target) })
                : t("meeting.suggest.transcribe")}
            </span>
          </div>
        </div>
      </div>
      <div className="meeting-suggest-actions">
        <button
          type="button"
          tabIndex={-1}
          disabled={disabled || busy}
          onMouseDown={(e) => e.preventDefault()}
          onClick={onStart}
          className="meeting-btn meeting-btn-start"
        >
          {busy ? <LoaderCircle className="animate-spin" /> : <Play className="fill-current" />}
          {t("meeting.suggest.start")}
        </button>
        <button
          type="button"
          tabIndex={-1}
          disabled={busy}
          onMouseDown={(e) => e.preventDefault()}
          onClick={onLater}
          className="meeting-btn meeting-btn-quiet"
        >
          {t("meeting.suggest.later")}
        </button>
      </div>
      <button
        type="button"
        tabIndex={-1}
        disabled={busy}
        onMouseDown={(e) => e.preventDefault()}
        onClick={onNever}
        className="meeting-suggest-never"
      >
        {t("meeting.suggest.never", { app: s.app })}
      </button>
      {hideInMs !== null && hideInMs > 0 && (
        <span className="meeting-suggest-timer" style={{ animationDuration: `${hideInMs}ms` }} aria-hidden />
      )}
    </section>
  );
}

/**
 * Полоска свёрнутого перевода: состояние и уровень звука, сверху — состояние и источник (или
 * «останавливаю через N с», или «Перевод остановлен» с отметкой заметок), снизу — последняя
 * строка (перевод, без него — оригинал) хвостом. Кнопки: развернуть, стоп, скрыть в трей; во
 * время отсчёта — «Не останавливать». Перетаскивается за себя, двойной щелчок — снова панель.
 */
function MiniBar({
  tone,
  level,
  running,
  offline,
  stateText,
  source,
  ticker,
  endingIn,
  notesReady,
  busy,
  onExpand,
  onStop,
  onHide,
  onKeep,
  onPointerDown,
  onPointerMove,
}: {
  tone: Tone;
  level: number;
  running: boolean;
  offline: boolean;
  stateText: string;
  source: string;
  ticker: string;
  /** Секунд до автоостановки или null — отсчёта нет. */
  endingIn: number | null;
  notesReady: boolean;
  busy: boolean;
  onExpand: () => void;
  onStop: () => void;
  onHide: () => void;
  onKeep: () => void;
  onPointerDown: (e: ReactPointerEvent) => void;
  onPointerMove: (e: ReactPointerEvent) => void;
}) {
  const ending = endingIn !== null;
  return (
    <div
      className={cn("meeting-mini", ending && "meeting-mini-ending")}
      title={t("meeting.mini.bar")}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onDoubleClick={(e) => {
        if (!(e.target as HTMLElement).closest("button")) onExpand();
      }}
    >
      {!ending && <StatusMark tone={tone} level={level} />}
      <div className="meeting-mini-body">
        {ending ? (
          <>
            <div className="meeting-mini-ending-text">{t("meeting.mini.endingTitle")}</div>
            <div className="meeting-mini-ending-in tabular">{t("meeting.mini.endingIn", { n: endingIn })}</div>
          </>
        ) : (
          <>
            <div className={cn("meeting-mini-state", `meeting-tone-${tone}`)}>
              {offline && <Unplug />}
              <span className="shrink-0 font-medium">{running || offline ? stateText : t("meeting.mini.stopped")}</span>
              {source && <span className="min-w-0 truncate text-subtle">· {source}</span>}
              {notesReady && (
                <span className="meeting-mini-notes">
                  <NotebookPen />
                  {t("meeting.mini.notesReady")}
                </span>
              )}
            </div>
            {/* новые слова — справа: длинная строка обрезается слева (rtl-контейнер, текст — ltr) */}
            <div className="meeting-mini-ticker" title={running ? ticker : undefined}>
              <span dir="auto">{running ? ticker : ""}</span>
            </div>
          </>
        )}
      </div>
      {ending ? (
        <button
          type="button"
          tabIndex={-1}
          onMouseDown={(e) => e.preventDefault()}
          onClick={onKeep}
          className="meeting-hint-btn meeting-hint-btn-keep"
        >
          <Play className="fill-current" />
          {t("meeting.keepGoing")}
        </button>
      ) : (
        <>
          <HeadButton label={t("meeting.mini.expand")} onClick={onExpand}>
            <Maximize2 />
          </HeadButton>
          {running && (
            <HeadButton label={t("meeting.mini.stop")} onClick={onStop} disabled={busy || offline} danger>
              <Square className="fill-current" />
            </HeadButton>
          )}
        </>
      )}
      <HeadButton label={t("meeting.mini.hide")} onClick={onHide}>
        <X />
      </HeadButton>
    </div>
  );
}

/** Точка состояния и крошечный индикатор звука (уровень приходит 4 раза в секунду). */
function StatusMark({ tone, level }: { tone: Tone; level: number }) {
  const k = [1.5, 2.3, 1.8, 1.25];
  return (
    <span className={cn("meeting-mark", `meeting-mark-${tone}`)} aria-hidden>
      <span className="meeting-dot" />
      <span className="meeting-level">
        {k.map((m, i) => (
          <i key={i} style={{ transform: `scaleY(${Math.max(0.16, Math.min(1, level * m))})` }} />
        ))}
      </span>
    </span>
  );
}

function HeadButton({
  label,
  onClick,
  children,
  active,
  disabled,
  gearToggle,
  danger,
}: {
  label: string;
  onClick: () => void;
  children: ReactNode;
  active?: boolean;
  disabled?: boolean;
  /** Кнопка панели настроек: щелчок по ней панель не закрывает «снаружи», а переключает. */
  gearToggle?: boolean;
  /** «Закрыть», «Стоп»: при наведении — красным. */
  danger?: boolean;
}) {
  return (
    <button
      type="button"
      tabIndex={-1}
      data-gear-toggle={gearToggle ? "" : undefined}
      title={label}
      aria-label={label}
      disabled={disabled}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={cn("meeting-icon-btn", active && "meeting-icon-btn-on", danger && "meeting-icon-btn-danger")}
    >
      {children}
    </button>
  );
}

/** Половина переключателя «Расшифровка | Заметки». */
function ViewTab({
  on,
  onClick,
  icon,
  children,
}: {
  on: boolean;
  onClick: () => void;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      role="tab"
      tabIndex={-1}
      aria-selected={on}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className={cn("meeting-tab", on && "meeting-tab-on")}
    >
      {icon}
      {children}
    </button>
  );
}

function FootButton({
  onClick,
  disabled,
  icon,
  children,
}: {
  onClick: () => void;
  disabled?: boolean;
  icon: ReactNode;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      tabIndex={-1}
      disabled={disabled}
      onMouseDown={(e) => e.preventDefault()}
      onClick={onClick}
      className="meeting-foot-btn"
    >
      {icon}
      {children}
    </button>
  );
}

/** Настройки прямо в окне: язык перевода и собеседников, перевод, оригинал, размер текста. */
function GearPanel({
  s,
  patch,
  onDock,
  onClose,
  onAllSettings,
}: {
  s: Settings;
  patch: (p: SettingsPatch) => void;
  onDock: () => void;
  onClose: () => void;
  onAllSettings: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      const el = e.target as HTMLElement;
      // выпадающий список Select — порталом в body, он «внутри» панели
      if (ref.current?.contains(el) || el.closest("[role=listbox]") || el.closest("[data-gear-toggle]")) return;
      onClose();
    };
    window.addEventListener("mousedown", onDown);
    return () => window.removeEventListener("mousedown", onDown);
  }, [onClose]);

  const langs: SelectOption<MeetingLang>[] = MEETING_LANGS.map((l) => ({ value: l, label: meetingLangName(l) }));
  const sourceLangs: SelectOption<"auto" | MeetingLang>[] = [
    { value: "auto", label: t("meeting.langAuto") },
    ...langs,
  ];
  const size = s.meeting_font_size ?? 16;
  const setSize = (v: number) => patch({ meeting_font_size: Math.max(12, Math.min(28, v)) });

  return (
    <motion.div
      ref={ref}
      data-no-drag
      initial={{ opacity: 0, y: -6, scale: 0.98 }}
      animate={{ opacity: 1, y: 0, scale: 1 }}
      exit={{ opacity: 0, y: -4, scale: 0.98 }}
      transition={{ duration: 0.14, ease: [0.22, 1, 0.36, 1] }}
      className="meeting-gear"
    >
      <GearRow label={t("meeting.translate")}>
        <Switch
          size="sm"
          label={t("meeting.translate")}
          checked={s.meeting_translate}
          onChange={(v) => patch({ meeting_translate: v })}
        />
      </GearRow>
      <GearRow label={t("meeting.targetLang")}>
        <Select<MeetingLang>
          size="sm"
          value={s.meeting_target_lang}
          options={langs}
          onChange={(v) => patch({ meeting_target_lang: v })}
          className="w-[160px]"
          minWidth={180}
          align="right"
          disabled={!s.meeting_translate}
        />
      </GearRow>
      <GearRow label={t("meeting.sourceLang")}>
        <Select<"auto" | MeetingLang>
          size="sm"
          value={s.meeting_source_lang}
          options={sourceLangs}
          onChange={(v) => patch({ meeting_source_lang: v })}
          className="w-[160px]"
          minWidth={200}
          align="right"
        />
      </GearRow>
      <GearRow label={t("meeting.showOriginal")}>
        <Switch
          size="sm"
          label={t("meeting.showOriginal")}
          checked={s.meeting_show_original}
          onChange={(v) => patch({ meeting_show_original: v })}
        />
      </GearRow>
      <GearRow label={t("meeting.fontSize")}>
        <span className="meeting-stepper">
          <button type="button" tabIndex={-1} disabled={size <= 12} onMouseDown={(e) => e.preventDefault()} onClick={() => setSize(size - 1)}>
            <Minus />
          </button>
          <span className="tabular">{size}</span>
          <button type="button" tabIndex={-1} disabled={size >= 28} onMouseDown={(e) => e.preventDefault()} onClick={() => setSize(size + 1)}>
            <Plus />
          </button>
        </span>
      </GearRow>
      <div className="mt-1.5 flex flex-col gap-0.5 border-t border-line pt-1.5">
        <button type="button" tabIndex={-1} onMouseDown={(e) => e.preventDefault()} onClick={onDock} className="meeting-gear-link">
          <ChevronsRight />
          {t("meeting.dock")}
        </button>
        <button type="button" tabIndex={-1} onMouseDown={(e) => e.preventDefault()} onClick={onAllSettings} className="meeting-gear-link">
          <Settings2 />
          {t("meeting.allSettings")}
        </button>
      </div>
    </motion.div>
  );
}

function GearRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex min-h-9 items-center justify-between gap-3 px-1">
      <span className="min-w-0 truncate text-[12.5px] text-muted" title={label}>
        {label}
      </span>
      {children}
    </div>
  );
}

/**
 * Лента строк. Прокрутка: внизу — лента сама плавно едет за новыми словами и строками
 * (догоняет низ по кадрам, без рывков на каждое слово); пользователь прокрутил вверх (колесо,
 * полоса прокрутки) — лента стоит, а снизу появляется «Новые строки». Строки, которые
 * обновились выше (уточнённый перевод), не сдвигают читаемое: браузер держит якорь прокрутки
 * (overflow-anchor). Рисуются последние PAGE строк, ранние — по кнопке (лента сеанса бывает в
 * тысячи строк): строка, которая была вверху ленты, остаётся на своём месте.
 */
const Transcript = memo(function Transcript({
  lines,
  showOriginal,
  running,
  sourceName,
  session,
  systemOnly,
  inactive,
  jump,
}: {
  lines: MeetingLine[];
  showOriginal: boolean;
  running: boolean;
  sourceName: string;
  session: string;
  /** Захватывается только весь звук компьютера (Windows 10) — подсказка в пустой ленте. */
  systemOnly: boolean;
  /** Открыты заметки: лента не рисуется, но остаётся (прокрутка и «внизу» — как были). */
  inactive: boolean;
  /** Переход из заметок: строка — в середину ленты и подсвечена. */
  jump: Jump;
}) {
  const scroller = useRef<HTMLDivElement>(null);
  const content = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);
  const raf = useRef(0);
  const userAt = useRef(0);
  const [unseen, setUnseen] = useState(false);
  const [shownCount, setShownCount] = useState(PAGE);
  /** «Показать ранние строки»: строка вверху ленты и её отступ от верха — до показа. */
  const prepend = useRef<{ id: string; offset: number } | null>(null);
  const lastId = useRef(0);

  /** Догнать низ: плавно (по кадрам, ~1/5 оставшегося за кадр) или сразу. */
  const follow = useCallback((instant = false) => {
    const el = scroller.current;
    if (!el) return;
    if (instant || reducedMotion()) {
      if (raf.current) cancelAnimationFrame(raf.current);
      raf.current = 0;
      el.scrollTop = el.scrollHeight;
      return;
    }
    if (raf.current) return;
    const step = () => {
      raf.current = 0;
      const box = scroller.current;
      if (!box || !pinned.current) return;
      const target = box.scrollHeight - box.clientHeight;
      const gap = target - box.scrollTop;
      if (gap <= 1) return;
      // далеко (вернулись к окну, пачка строк) — сразу, близко — плавно
      box.scrollTop = gap > box.clientHeight * 1.5 ? target : box.scrollTop + Math.max(1, Math.ceil(gap * 0.2));
      raf.current = requestAnimationFrame(step);
    };
    raf.current = requestAnimationFrame(step);
  }, []);
  useEffect(
    () => () => {
      if (raf.current) cancelAnimationFrame(raf.current);
    },
    [],
  );

  // новый сеанс — снова с низа и с одной страницы
  useEffect(() => {
    pinned.current = true;
    lastId.current = 0;
    setUnseen(false);
    setShownCount(PAGE);
  }, [session]);

  const visible = lines.length > shownCount ? lines.slice(-shownCount) : lines;
  const hidden = lines.length - visible.length;

  /** «Показать ранние строки»: запомнить верхнюю видимую строку, показать ещё страницу. */
  const showEarlier = () => {
    const el = scroller.current;
    prepend.current = null;
    if (el) {
      const top = el.getBoundingClientRect().top;
      for (const row of el.querySelectorAll<HTMLElement>("[data-line-id]")) {
        const r = row.getBoundingClientRect();
        if (r.bottom > top) {
          prepend.current = { id: row.dataset.lineId ?? "", offset: r.top - top };
          break;
        }
      }
    }
    // читают начало — лента за новыми строками не едет
    pinned.current = false;
    setShownCount((n) => n + PAGE);
  };

  // Показали ранние строки — строка, что была вверху, остаётся на месте (сдвиг считается от
  // неё самой: что бы ни сделал якорь прокрутки браузера, поправка не удваивается)
  useLayoutEffect(() => {
    const el = scroller.current;
    const a = prepend.current;
    prepend.current = null;
    if (!el || !a) return;
    const row = el.querySelector<HTMLElement>(`[data-line-id="${a.id}"]`);
    if (row) el.scrollTop += row.getBoundingClientRect().top - el.getBoundingClientRect().top - a.offset;
  }, [shownCount]);

  useLayoutEffect(() => {
    const el = scroller.current;
    if (!el) return;
    const newest = lines[lines.length - 1]?.id ?? 0;
    const grew = newest > lastId.current;
    const first = lastId.current === 0;
    lastId.current = newest;
    if (pinned.current) follow(first);
    else if (grew) setUnseen(true);
  }, [lines, showOriginal, follow]);

  // Высота ленты меняется и без новых строк (выросли слова или перевод, другой размер текста,
  // ширина окна) — внизу и остаёмся.
  useEffect(() => {
    const el = content.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(() => {
      if (pinned.current) follow();
    });
    ro.observe(el);
    // и сама лента стала ниже (подсказка под шапкой, размер окна)
    if (scroller.current) ro.observe(scroller.current);
    return () => ro.disconnect();
  }, [follow]);

  // Переход из заметок: ранние строки — показать, если строка среди них; лента больше не едет
  // за новыми строками; строка — в середину и подсвечена
  const jumpTo = useRef<number | null>(null);
  const [flashId, setFlashId] = useState<number | null>(null);
  useEffect(() => {
    if (!jump) return;
    const idx = lines.findIndex((l) => l.id === jump.id);
    if (idx < 0) return;
    const need = lines.length - idx;
    setShownCount((n) => (need > n ? Math.ceil(need / PAGE) * PAGE : n));
    pinned.current = false;
    jumpTo.current = jump.id;
    setFlashId(jump.id);
    const tm = setTimeout(() => setFlashId(null), JUMP_FLASH_MS);
    return () => clearTimeout(tm);
    // строки меняются постоянно — переход только по новому jump (строки — на его момент)
  }, [jump]);
  useLayoutEffect(() => {
    const id = jumpTo.current;
    const el = scroller.current;
    if (id === null || !el || inactive) return;
    const center = () => {
      const row = el.querySelector<HTMLElement>(`[data-line-id="${id}"]`);
      if (!row) return false;
      const r = row.getBoundingClientRect();
      const b = el.getBoundingClientRect();
      el.scrollTop += r.top - b.top - Math.max(0, (b.height - r.height) / 2);
      return true;
    };
    if (!center()) return;
    jumpTo.current = null;
    // строки вне экрана раскладываются лениво (content-visibility) — ещё раз по настоящим высотам
    const raf1 = requestAnimationFrame(() => void center());
    return () => cancelAnimationFrame(raf1);
  });

  const onScroll = () => {
    const el = scroller.current;
    if (!el) return;
    const gap = el.scrollHeight - el.scrollTop - el.clientHeight;
    if (gap <= BOTTOM_SLACK) {
      pinned.current = true;
      setUnseen(false);
    } else if (Date.now() - userAt.current < USER_SCROLL_MS) {
      // отпускает низ только сам пользователь: своя плавная прокрутка тоже бывает «не внизу»
      pinned.current = false;
    }
  };
  const onWheel = (e: { deltaY: number }) => {
    userAt.current = Date.now();
    // короткую ленту (прокрутки нет — не будет и scroll, который вернул бы «внизу») колесо не
    // отпускает: иначе, когда строк станет больше, она так и стояла бы на первых
    const el = scroller.current;
    if (el && wheelUnpins(e.deltaY, el.scrollHeight, el.clientHeight, BOTTOM_SLACK)) pinned.current = false;
  };
  // полоса прокрутки (щелчок по самой ленте, не по строке) и касание
  const onPointerDown = (e: ReactPointerEvent) => {
    if (e.target === scroller.current || e.pointerType === "touch") userAt.current = Date.now();
  };
  const onPointerMove = (e: ReactPointerEvent) => {
    if (e.buttons & 1 && Date.now() - userAt.current < USER_SCROLL_MS) userAt.current = Date.now();
  };

  return (
    <div className={cn("relative min-h-0 flex-1", inactive && "hidden")}>
      <div
        ref={scroller}
        className="meeting-body"
        onScroll={onScroll}
        onWheel={onWheel}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
      >
        <div ref={content} className="meeting-lines">
          {hidden > 0 && (
            <button
              type="button"
              tabIndex={-1}
              onMouseDown={(e) => e.preventDefault()}
              onClick={showEarlier}
              className="meeting-earlier"
            >
              {t("meeting.earlier")} · {tp("units.lines", hidden)}
            </button>
          )}
          {visible.map((l) => (
            <Line key={l.id} line={l} showOriginal={showOriginal} flash={l.id === flashId} />
          ))}
          {lines.length === 0 && <Empty running={running} sourceName={sourceName} systemOnly={systemOnly} />}
        </div>
      </div>
      <AnimatePresence>
        {unseen && (
          <motion.button
            type="button"
            tabIndex={-1}
            initial={{ opacity: 0, y: 8 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: 8 }}
            transition={{ duration: 0.16 }}
            onMouseDown={(e) => e.preventDefault()}
            onClick={() => {
              pinned.current = true;
              setUnseen(false);
              follow();
            }}
            className="meeting-new"
          >
            <ArrowDown className="size-3.5" />
            {t("meeting.newLines")}
          </motion.button>
        )}
      </AnimatePresence>
    </div>
  );
});

/**
 * Текст, который растёт по словам: дописанное проявляется, исправленное (уточнённый перевод,
 * финальное распознавание) — тоже, с первого изменившегося слова. Разбивка пересчитывается
 * только при смене текста: перерисовка строки по другой причине анимацию не обрывает.
 */
function Grow({ text }: { text: string }) {
  const prev = useRef(text);
  const [base, fresh] = useMemo(() => splitGrowth(prev.current, text), [text]);
  useEffect(() => {
    prev.current = text;
  }, [text]);
  if (!fresh) return <>{text}</>;
  return (
    <>
      {base}
      <span key={base.length} className="meeting-fresh">
        {fresh}
      </span>
    </>
  );
}

/** Исходный текст: подтверждённое — обычным, неподтверждённый хвост — тише и курсивом. */
function SourceText({ text, partial }: { text: string; partial: string }) {
  const sep = text && partial && needsSpace(text, partial) ? " " : "";
  return (
    <>
      <Grow text={text} />
      {partial && (
        <span className="meeting-partial">
          {sep}
          {partial}
        </span>
      )}
    </>
  );
}

/**
 * Одна фраза: перевод крупно (растёт по словам, пока приходит), оригинал мельче и тише;
 * время и язык — совсем мелко. Пока перевода нет — на его месте три точки высотой в строку
 * (первые слова встают туда же, лента не прыгает); без оригинала — сам текст, приглушённо.
 */
const Line = memo(function Line({
  line,
  showOriginal,
  flash = false,
}: {
  line: MeetingLine;
  showOriginal: boolean;
  /** К строке перешли из заметок — подсвечена. */
  flash?: boolean;
}) {
  const tr = line.translation ?? "";
  const original = line.status === "original";
  const waiting = line.status === "pending" || line.status === "streaming";
  const hasSource = Boolean(line.text || line.partial);
  const source = hasSource ? <SourceText text={line.text} partial={line.partial} /> : null;
  // слов ещё нет (строка только открылась) или перевод ещё не начался — точки высотой в строку
  const dots = (
    <span className="meeting-typing" role="img" aria-label={t(original ? "meeting.live" : "meeting.pending")}>
      <i />
      <i />
      <i />
    </span>
  );
  let main: ReactNode;
  let orig: ReactNode = null;
  let mainWait = false;
  if (original) {
    main = source ?? dots;
  } else if (tr) {
    main = (
      <>
        <Grow text={tr} />
        {line.status === "streaming" && <span className="meeting-caret" aria-hidden />}
      </>
    );
    if (showOriginal) orig = source;
  } else if ((waiting && showOriginal) || !source) {
    main = dots;
    orig = source;
  } else {
    main = source;
    mainWait = waiting;
  }
  return (
    <article
      data-line-id={line.id}
      className={cn(
        "meeting-line",
        `meeting-line-${line.status}`,
        !line.final && "meeting-line-live",
        flash && "meeting-line-flash",
      )}
    >
      <div className="meeting-meta">
        <time className="tabular">{line.ts}</time>
        <span className="meeting-lang">{line.lang.toUpperCase()}</span>
        {!line.final && <span className="meeting-live" title={t("meeting.live")} aria-label={t("meeting.live")} />}
        {line.status === "pending" && !tr && !showOriginal && <span className="meeting-wait">{t("meeting.pending")}</span>}
        {line.refined && (
          <span className="meeting-refined" title={t("meeting.refined")} aria-label={t("meeting.refined")}>
            <CheckCheck />
          </span>
        )}
        {line.status === "error" && (
          <span className="meeting-err" title={line.error ?? undefined}>
            <TriangleAlert />
            {t("meeting.lineError")}
          </span>
        )}
      </div>
      <p className={cn("meeting-main", mainWait && "meeting-main-wait")}>{main}</p>
      {orig && <p className="meeting-orig">{orig}</p>}
    </article>
  );
});

function Empty({ running, sourceName, systemOnly }: { running: boolean; sourceName: string; systemOnly: boolean }) {
  return (
    <div className="meeting-empty">
      <span className="meeting-empty-icon">{running ? <AudioLines /> : <Languages />}</span>
      <div className="text-[14px] font-semibold text-fg">{t("meeting.emptyTitle")}</div>
      <p className="max-w-[300px] text-[12.5px] leading-relaxed text-muted">
        {running
          ? t("meeting.emptyListening", { source: sourceName })
          : systemOnly
            ? t("meeting.emptyIdleSystem")
            : t("meeting.emptyIdle")}
      </p>
    </div>
  );
}
