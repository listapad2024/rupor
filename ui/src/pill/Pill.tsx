// Плавающая пилюля: покой / запись с волной и таймером / обработка / ИИ / ошибка.
import { AnimatePresence, motion } from "framer-motion";
import {
  BookCheck,
  BookPlus,
  Check,
  CircleAlert,
  Download,
  Hand,
  Info,
  LoaderCircle,
  MonitorSmartphone,
  Pause,
  Plus,
  ShieldAlert,
  Sparkles,
  Terminal,
  TriangleAlert,
  Undo2,
  Unplug,
} from "lucide-react";
import { useEffect, useRef, useState, type PointerEvent, type ReactNode } from "react";
import { LearnText } from "../components/Learn";
import { LiveWave } from "../components/Level";
import { syncLocale, t, useLocale } from "../i18n";
import { cn } from "../lib/cn";
import { fmtClock } from "../lib/format";
import { useConnStatus, useEvent, useNow } from "../lib/hooks";
import { errorText } from "../lib/hub";
import { keyLabel } from "../lib/keys";
import {
  isLearnNotice,
  isResolved,
  learnKey,
  learnNoticeArrived,
  onLearnResolved,
  resolveLearn,
  sameCorrection,
  type LearnNotice,
} from "../lib/learn";
import { RDP_SESSIONS_ROUTE } from "../lib/rdp";
import { pillRaise, relaunchAdmin, setWindowTitle, showMain } from "../lib/tauri";
import { normalizeAccent, accentForeground } from "../lib/theme";
import type { CoreState, NoticeEvent, RecordingEvent, Settings } from "../lib/types";
import { backend, call } from "../lib/ws";
import { applyPillPosition, setPillVisible, startPassthrough, startPillDrag, watchPillMoves } from "./window";

type Mode =
  | "idle"
  | "offline"
  | "paused"
  | "loading"
  | "error"
  | "setup"
  | "rec"
  | "processing"
  | "ai"
  | "done"
  | "notice"
  | "learn";

/** Уведомление «словарь учится»: вопрос/отмена → нажали (busy) → «Добавлено»/«Отменено» или ошибка. */
type LearnPhase = "ask" | "busy" | "ok" | "fail";
interface LearnItem {
  key: string;
  n: LearnNotice;
  phase: LearnPhase;
  error: string | null;
}

const SPRING = { type: "spring", stiffness: 520, damping: 40, mass: 0.8 } as const;

export function Pill() {
  const conn = useConnStatus();
  const [core, setCore] = useState<CoreState | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [rec, setRec] = useState<RecordingEvent | null>(null);
  const [broadcast, setBroadcast] = useState(false);
  // Куда уйдёт текст рассылки (ключи RDP-сессий); null — ещё не знаем (рассылку включили
  // модификатором уже во время записи — спросим rdp_windows).
  const [targets, setTargets] = useState<string[] | null>(null);
  const targetsRef = useRef<string[] | null>(null);
  targetsRef.current = targets;
  const [notice, setNotice] = useState<NoticeEvent | null>(null);
  // Очередь уведомлений «словарь учится»: исправили два слова подряд — покажем оба по очереди
  const [learnQ, setLearnQ] = useState<LearnItem[]>([]);
  const [done, setDone] = useState(false);
  const [hover, setHover] = useState(false);
  // state=error из-за нескачанной модели (первый запуск, мастер) — это настройка, а не ошибка
  const [modelReady, setModelReady] = useState<boolean | null>(null);
  const capsuleRef = useRef<HTMLDivElement>(null);
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const hoverRef = useRef(false);
  hoverRef.current = hover;

  // Подключение и начальные данные
  useEffect(() => {
    backend.start();
  }, []);
  useEffect(() => {
    if (conn !== "open") return;
    call("get_state").then(setCore).catch(() => {});
    call("get_settings").then(setSettings).catch(() => {});
  }, [conn]);

  useEvent("hello", setCore);
  useEvent("state", setCore);
  useEvent("settings", setSettings);

  // Язык: как в главном окне — settings.language, для "system" — State.ui_language.
  // useLocale перерисовывает пилюлю при смене (тексты уведомлений ядра приходят уже на новом).
  const locale = useLocale();
  const langPref = settings?.language;
  const uiLanguage = core?.ui_language;
  useEffect(() => syncLocale(langPref, uiLanguage), [langPref, uiLanguage]);
  useEffect(() => void setWindowTitle(t("pill.windowTitle")), [locale]);
  const fetchTargets = () => {
    call("rdp_windows")
      .then((ws) => setTargets(ws.filter((w) => w.included).map((w) => w.key)))
      .catch(() => setTargets(null));
  };
  useEvent("recording", (r) => {
    setRec(r);
    setBroadcast(r.broadcast);
    setNotice(null);
    if (!r.broadcast) setTargets(null);
    else if (Array.isArray(r.targets)) setTargets(r.targets);
    else {
      // ядро без поля targets — отмеченные открытые сессии спросим сами
      setTargets(null);
      fetchTargets();
    }
  });
  useEvent("broadcast", (b) => {
    setBroadcast(b.on);
    if (b.on && targetsRef.current === null) fetchTargets();
  });
  useEvent("result", () => {
    setDone(true);
    setTimeout(() => setDone(false), 1100);
  });
  useEvent("notice", (n) => {
    if (isLearnNotice(n)) {
      pushLearn(n);
      return;
    }
    if (n.level === "info" && !n.toast) return;
    setNotice(n);
  });
  useEvent("progress", (p) => {
    if (p.what === "model" && p.done) setModelReady(p.error ? null : true);
  });

  const isError = core?.state === "error";
  useEffect(() => {
    if (!isError || conn !== "open") {
      setModelReady(null);
      return;
    }
    let alive = true;
    call("setup_status")
      .then((st) => {
        if (alive) setModelReady(st.model_ready);
      })
      .catch(() => {});
    return () => {
      alive = false;
    };
  }, [isError, conn]);

  const pushLearn = (n: LearnNotice) => {
    learnNoticeArrived(n);
    if (isResolved(n)) return;
    const key = learnKey(n);
    setLearnQ((q) => {
      if (q.some((x) => x.key === key)) return q;
      // Та же правка второй раз (режим suggest: Рупор добавил сам) — прежний вопрос не нужен.
      const rest = q.filter((x) => !(x.phase === "ask" && sameCorrection(x.n, n)));
      return [...rest, { key, n, phase: "ask" as const, error: null }].slice(-4);
    });
  };
  const setLearnPhase = (key: string, phase: LearnPhase, error: string | null = null) =>
    setLearnQ((q) => q.map((x) => (x.key === key ? { ...x, phase, error } : x)));
  const actLearn = (item: LearnItem) => {
    if (item.phase !== "ask") return;
    setLearnPhase(item.key, "busy");
    resolveLearn(item.n).then(
      () => setLearnPhase(item.key, "ok"),
      (e: unknown) => setLearnPhase(item.key, "fail", errorText(e)),
    );
  };
  // Кнопку нажали в главном окне — здесь показываем «Отменено»/«Добавлено» (или убираем из очереди).
  useEffect(
    () =>
      onLearnResolved((key) => {
        setLearnQ((q) =>
          q.flatMap((x, i) => {
            if (x.key !== key || x.phase === "ok" || x.phase === "fail") return [x];
            return i === 0 ? [{ ...x, phase: "ok" as const, error: null }] : [];
          }),
        );
      }),
    [],
  );

  const armNoticeTimer = (ms: number) => {
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = setTimeout(() => {
      if (hoverRef.current) armNoticeTimer(1500);
      else setNotice(null);
    }, ms);
  };

  // Запись без события recording (на всякий случай) — показать через 400 мс
  const recording = core?.state === "recording" || core?.state === "hands_free";
  useEffect(() => {
    if (!recording) {
      setRec(null);
      setBroadcast(false);
      setTargets(null);
      return;
    }
    const id = setTimeout(() => {
      setRec(
        (r) =>
          r ?? {
            started_at: Date.now() - 400,
            broadcast: false,
            command: false,
            hands_free: core?.state === "hands_free",
            targets: [],
          },
      );
    }, 400);
    return () => clearTimeout(id);
  }, [recording]);

  // Настройки окна: акцент, позиция (видимость — ниже: она зависит ещё и от уведомлений)
  useEffect(() => {
    if (!settings) return;
    const accent = normalizeAccent(settings.accent);
    document.documentElement.style.setProperty("--c-accent", accent);
    document.documentElement.style.setProperty("--c-accent-fg", accentForeground(accent));
    void applyPillPosition(settings.pill_position);
  }, [settings]);

  useEffect(() => {
    let off: (() => void) | null = null;
    let alive = true;
    void watchPillMoves((pos) => {
      call("set_settings", { patch: { pill_position: pos } }).catch(() => {});
    }).then((f) => {
      if (alive) off = f;
      else f();
    });
    let offPass: (() => void) | null = null;
    void startPassthrough(() => capsuleRef.current?.getBoundingClientRect() ?? null).then((f) => {
      if (alive) offPass = f;
      else f();
    });
    return () => {
      alive = false;
      off?.();
      offPass?.();
    };
  }, []);

  // Перетаскивание: сдвиг больше 4 px — это перенос, иначе клик
  const down = useRef<{ x: number; y: number; dragging: boolean } | null>(null);
  const onPointerDown = (e: PointerEvent) => {
    if (e.button !== 0) return;
    // Кнопки уведомлений («Отменить», «Добавить»…) — ни перетаскивания, ни диктовки по клику.
    if ((e.target as HTMLElement).closest("button")) {
      down.current = null;
      return;
    }
    down.current = { x: e.screenX, y: e.screenY, dragging: false };
  };
  const onPointerMove = (e: PointerEvent) => {
    const d = down.current;
    if (!d || d.dragging || !(e.buttons & 1)) return;
    if (Math.hypot(e.screenX - d.x, e.screenY - d.y) > 4) {
      d.dragging = true;
      void startPillDrag();
    }
  };
  const onPointerUp = () => {
    const d = down.current;
    down.current = null;
    if (!d || d.dragging) return;
    if (mode === "error" || mode === "setup") {
      void showMain();
      return;
    }
    // Клик — диктовка «без рук», как в Wispr Flow: старт, повторный клик — стоп и вставка.
    // Пилюля фокус не берёт, поэтому текст уйдёт в окно, где вы работали.
    if (mode === "idle" || (mode === "rec" && hf)) void call("dictation_toggle").catch(() => undefined);
  };
  // Правый клик — открыть окно Рупора
  const onContextMenu = (e: { preventDefault: () => void }) => {
    e.preventDefault();
    void showMain();
  };

  const hf = core?.state === "hands_free" || Boolean(rec?.hands_free);
  // warn/error важнее всего, кроме записи; info («Отправлено в 3 RDP-сессий») не перекрывает
  // «Распознаю» и «Вставлено» — покажется после них.
  const urgent = notice !== null && notice.level !== "info";
  const learnHead = learnQ[0] ?? null;
  let mode: Mode;
  if (recording && rec) mode = "rec";
  else if (urgent) mode = "notice";
  else if (core?.state === "processing") mode = "processing";
  else if (core?.state === "ai") mode = "ai";
  else if (done) mode = "done";
  else if (notice) mode = "notice";
  else if (learnHead && conn === "open") mode = "learn";
  else if (conn !== "open" || !core) mode = "offline";
  else if (core.state === "loading") mode = "loading";
  else if (core.state === "paused") mode = "paused";
  else if (core.state === "error")
    mode = modelReady === false || (modelReady === null && !core.first_run_done) ? "setup" : "error";
  else mode = "idle";

  // Началась запись — пилюлю наверх: её могло закрыть окно, которое тоже «поверх всех»
  // (полноэкранный RDP). При смене активного окна это делает и сама оболочка.
  useEffect(() => {
    if (mode === "rec") void pillRaise();
  }, [mode]);

  // Таймер уведомления идёт, пока его видно; спрятанное (за «Вставлено») ждёт не дольше 10 с.
  const noticeVisible = mode === "notice";
  useEffect(() => {
    if (!notice) return;
    armNoticeTimer(noticeVisible ? (notice.action ? 8000 : 4000) : 10_000);
  }, [notice, noticeVisible]);
  useEffect(
    () => () => {
      if (noticeTimer.current) clearTimeout(noticeTimer.current);
    },
    [],
  );

  // Уведомление «словарь учится» висит: «В словарь добавлено … [Отменить]» — 12 с, чтобы
  // успеть прочитать и отменить, вопрос «Добавить …?» — ~6 с (пока курсор над пилюлей —
  // дольше). Спрятанное за записью ждёт до 2 мин: после «Вставлено» всё равно покажем,
  // что попало в словарь.
  const learnVisible = mode === "learn";
  const learnKeyNow = learnHead?.key;
  const learnPhase = learnHead?.phase;
  const learnUndo = learnHead?.n.action === "dict_undo";
  useEffect(() => {
    if (!learnKeyNow || !learnPhase || learnPhase === "busy") return;
    const shown = learnUndo ? 12_000 : 6000;
    const ms = learnPhase === "ok" ? 1300 : learnPhase === "fail" ? 4500 : learnVisible ? shown : 120_000;
    let timer: ReturnType<typeof setTimeout> | null = null;
    const arm = (d: number) => {
      timer = setTimeout(() => {
        if (learnPhase === "ask" && learnVisible && hoverRef.current) arm(1500);
        else setLearnQ((q) => (q[0]?.key === learnKeyNow ? q.slice(1) : q));
      }, d);
    };
    arm(ms);
    return () => {
      if (timer) clearTimeout(timer);
    };
  }, [learnKeyNow, learnPhase, learnVisible, learnUndo]);

  // Пилюля выключена, но пришло warn/error (например, «Перезапустить от администратора»):
  // главное окно обычно скрыто в трее, поэтому на время уведомления пилюлю всё же показываем
  // (без активации — это держит Rust), потом снова прячем. Так же — «В словарь добавлено»:
  // пользователь должен видеть, что Рупор выучил, и успеть отменить.
  const forced = Boolean(settings && !settings.pill_enabled && ((noticeVisible && urgent) || learnVisible));
  const pillEnabled = settings?.pill_enabled;
  useEffect(() => {
    if (pillEnabled === undefined) return;
    void setPillVisible(pillEnabled || forced);
  }, [pillEnabled, forced]);

  const glow =
    mode === "notice" && notice
      ? notice.level === "error"
        ? "err"
        : notice.level === "warn"
          ? "warn"
          : null
      : mode === "error"
        ? "err"
        : mode === "rec"
          ? hf
            ? "accent"
            : "rec"
          : mode === "ai"
            ? "accent"
            : null;

  const compact = mode === "idle" || mode === "offline" || mode === "paused";

  return (
    <div className="pill-stage">
      <motion.div
        ref={capsuleRef}
        layout
        transition={SPRING}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onContextMenu={onContextMenu}
        onMouseEnter={() => setHover(true)}
        onMouseLeave={() => setHover(false)}
        className={cn("pill-capsule", compact && !hover && "pill-compact", glow && `pill-glow-${glow}`)}
        style={{
          // Уведомление в несколько строк в «таблетке» 999 срезало бы углы текста; при высоте
          // в одну строку (40 px) радиус 20 — та же таблетка
          borderRadius:
            mode === "notice" ||
            (mode === "learn" && (learnHead?.phase === "ask" || learnHead?.phase === "busy")) ||
            (mode === "rec" && broadcast)
              ? 20
              : 999,
        }}
        data-mode={mode}
      >
        <AnimatePresence mode="popLayout" initial={false}>
          <motion.div
            key={mode + (mode === "idle" && hover ? "-h" : "") + (mode === "learn" ? learnViewKey(learnHead) : "")}
            layout="position"
            initial={{ opacity: 0, scale: 0.92, filter: "blur(4px)" }}
            animate={{ opacity: 1, scale: 1, filter: "blur(0px)" }}
            exit={{ opacity: 0, scale: 0.92, filter: "blur(4px)" }}
            transition={{ duration: 0.16 }}
            className="flex items-center"
          >
            {renderContent()}
          </motion.div>
        </AnimatePresence>
      </motion.div>
    </div>
  );

  function renderContent(): ReactNode {
    switch (mode) {
      case "idle":
        return hover ? (
          <span className="flex items-center gap-2 px-3.5 py-[7px] text-[12px] font-medium text-white/85">
            <span className="size-1.5 rounded-full bg-[var(--c-accent)]" />
            {t("pill.hint", { key: keyLabel(core?.hotkeys.ptt ?? "rctrl") })}
          </span>
        ) : (
          <IdleDots />
        );
      case "offline":
        return hover ? (
          <span className="flex items-center gap-2 px-3.5 py-[7px] text-[12px] font-medium text-white/70">
            <Unplug className="size-3.5" />
            {t("pill.offline")}
          </span>
        ) : (
          <IdleDots dim />
        );
      case "paused":
        return (
          <span className="flex items-center gap-1.5 px-3 py-[5px] text-[11.5px] font-medium text-white/70">
            <Pause className="size-3 fill-current" />
            {hover ? t("pill.pausedHint") : t("pill.paused")}
          </span>
        );
      case "loading":
        return (
          <span className="flex items-center gap-2 px-4 py-2 text-[12px] font-medium text-white/80">
            <LoaderCircle className="size-3.5 animate-spin" />
            <span className="pill-shimmer">{t("pill.loading")}</span>
          </span>
        );
      case "error":
        return (
          <span className="flex items-center gap-2 px-4 py-2 text-[12px] font-medium text-[#ff8a8e]">
            <CircleAlert className="size-3.5" />
            {hover ? t("pill.errorHint") : t("pill.error")}
          </span>
        );
      case "setup":
        return (
          <span className="flex items-center gap-2 px-4 py-2 text-[12px] font-medium text-white/80">
            <Download className="size-3.5" />
            {hover ? t("pill.noModelHint") : t("pill.noModel")}
          </span>
        );
      case "rec":
        return (
          <Recording rec={rec} handsFree={hf} broadcast={broadcast} targets={targets} command={Boolean(rec?.command)} />
        );
      case "processing":
        return (
          <span className="flex items-center gap-2.5 px-4 py-2.5 text-[12.5px] font-medium text-white/85">
            <BouncingDots />
            <span className="pill-shimmer">{t("pill.processing")}</span>
          </span>
        );
      case "ai":
        return (
          <span className="flex items-center gap-2 px-4 py-2.5 text-[12.5px] font-semibold">
            <motion.span
              animate={{ rotate: [0, 18, -12, 0], scale: [1, 1.15, 1] }}
              transition={{ duration: 1.4, repeat: Infinity }}
              className="flex text-[var(--c-accent)]"
            >
              <Sparkles className="size-4" />
            </motion.span>
            <span className="pill-ai-text">{t("pill.ai")}</span>
          </span>
        );
      case "done":
        return (
          <span className="flex items-center gap-2 px-3.5 py-2 text-[12px] font-medium text-white/90">
            <span className="flex size-4 items-center justify-center rounded-full bg-[#3dbb7d]">
              <Check className="size-3 text-white" strokeWidth={3} />
            </span>
            {t("pill.done")}
          </span>
        );
      case "notice":
        return notice ? <Notice n={notice} /> : null;
      case "learn":
        return learnHead ? <Learn item={learnHead} onAct={() => actLearn(learnHead)} /> : null;
    }
  }
}

/** Ключ анимации: другая правка или «вопрос → Отменено» — плавная смена содержимого. */
function learnViewKey(item: LearnItem | null): string {
  if (!item) return "";
  return `:${item.key}:${item.phase === "ok" || item.phase === "fail" ? item.phase : "ask"}`;
}

/**
 * «В словарь добавлено: «метабейс» → Metabase [Отменить]» (dict_undo) или
 * «Добавить в словарь: …? [Добавить]» (dict_add). Нейтрально, без красной подсветки.
 * Кнопка не берёт фокус и не запускает ни диктовку, ни перетаскивание (см. onPointerDown).
 */
function Learn({ item, onAct }: { item: LearnItem; onAct: () => void }) {
  const add = item.n.action === "dict_add";
  if (item.phase === "ok") {
    return (
      <span className="flex items-center gap-2 px-3.5 py-2 text-[12px] font-medium text-white/90">
        <span className="flex size-4 items-center justify-center rounded-full bg-[#3dbb7d]">
          <Check className="size-3 text-white" strokeWidth={3} />
        </span>
        {add ? t("learn.added") : t("learn.undone")}
      </span>
    );
  }
  if (item.phase === "fail") {
    return (
      <span className="flex max-w-[336px] items-center gap-2 px-3.5 py-2 text-[12px] font-medium text-[#ff8a8e]">
        <CircleAlert className="size-3.5 shrink-0" />
        <span className="line-clamp-2 min-w-0">{t("learn.failed", { error: item.error ?? "" })}</span>
      </span>
    );
  }
  const busy = item.phase === "busy";
  return (
    <span className="flex max-w-[344px] items-center gap-2.5 py-[7px] pl-2 pr-[7px]">
      <span className="flex size-6 shrink-0 items-center justify-center rounded-full bg-[color-mix(in_oklab,var(--c-accent)_34%,transparent)] text-white">
        {add ? <BookPlus className="size-3.5" /> : <BookCheck className="size-3.5" />}
      </span>
      <span className="line-clamp-2 min-w-0 flex-1 text-[12px] font-medium leading-snug text-white/75">
        <LearnText
          text={item.n.text}
          term={item.n.data.term}
          variant={item.n.data.variant}
          termClass="font-semibold text-white"
          variantClass="text-white/55"
        />
      </span>
      <button
        type="button"
        tabIndex={-1}
        disabled={busy}
        onMouseDown={(e) => e.preventDefault()}
        onClick={onAct}
        className={cn(
          "flex h-7 shrink-0 items-center gap-1 rounded-full px-2.5 text-[11.5px] font-semibold transition-[transform,background-color] duration-150 hover:scale-[1.03] active:scale-95 disabled:pointer-events-none",
          add ? "bg-white text-[#15151a]" : "bg-white/12 text-white hover:bg-white/20",
        )}
      >
        {busy ? (
          <LoaderCircle className="size-3 animate-spin" />
        ) : add ? (
          <Plus className="size-3" strokeWidth={2.6} />
        ) : (
          <Undo2 className="size-3" strokeWidth={2.4} />
        )}
        {add ? t("learn.add") : t("learn.undo")}
      </button>
    </span>
  );
}

function IdleDots({ dim }: { dim?: boolean }) {
  return (
    <span className={cn("flex items-center gap-[3px] px-3 py-[4px]", dim && "opacity-50")}>
      {[0, 1, 2, 3, 4].map((i) => (
        <span
          key={i}
          className="w-[2px] rounded-full bg-white/70"
          style={{ height: [3, 5, 7, 5, 3][i] }}
        />
      ))}
    </span>
  );
}

function BouncingDots() {
  return (
    <span className="flex items-center gap-[3px]">
      {[0, 1, 2].map((i) => (
        <motion.span
          key={i}
          className="size-[5px] rounded-full bg-white/85"
          animate={{ y: [0, -4, 0], opacity: [0.5, 1, 0.5] }}
          transition={{ duration: 0.8, repeat: Infinity, delay: i * 0.13, ease: "easeInOut" }}
        />
      ))}
    </span>
  );
}

function Recording({
  rec,
  handsFree,
  broadcast,
  targets,
  command,
}: {
  rec: RecordingEvent | null;
  handsFree: boolean;
  broadcast: boolean;
  targets: string[] | null;
  command: boolean;
}) {
  return (
    <span className="flex flex-col">
      <RecordingRow rec={rec} handsFree={handsFree} broadcast={broadcast} command={command} />
      <AnimatePresence initial={false}>
        {broadcast && (
          <motion.span
            initial={{ opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: "auto" }}
            exit={{ opacity: 0, height: 0 }}
            transition={{ duration: 0.16 }}
            className="flex overflow-hidden"
          >
            <Targets targets={targets} />
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}

/** «→ srv-analytics, dev-box и ещё 2» — куда уйдёт текст рассылки. */
function Targets({ targets }: { targets: string[] | null }) {
  if (targets !== null && targets.length === 0) {
    return (
      <span className="flex items-center gap-1.5 pb-2 pl-[15px] pr-3.5 text-[11.5px] font-medium text-[#ffc56b]">
        <TriangleAlert className="size-3 shrink-0" />
        {t("pill.noTargets")}
      </span>
    );
  }
  const shown = (targets ?? []).slice(0, 2);
  const more = (targets?.length ?? 0) - shown.length;
  return (
    <span className="flex max-w-[330px] items-center gap-1.5 pb-2 pl-[15px] pr-3.5 text-[11.5px] font-medium text-[#b9d0ff]">
      <span className="shrink-0 text-white/45">→</span>
      {targets === null ? (
        <LoaderCircle className="size-3 animate-spin text-white/45" />
      ) : (
        <>
          <span className="min-w-0 truncate">{shown.join(", ")}</span>
          {more > 0 && <span className="shrink-0 text-white/50">{t("pill.targetsMore", { n: more })}</span>}
        </>
      )}
    </span>
  );
}

function RecordingRow({
  rec,
  handsFree,
  broadcast,
  command,
}: {
  rec: RecordingEvent | null;
  handsFree: boolean;
  broadcast: boolean;
  command: boolean;
}) {
  const now = useNow(200);
  const started = rec?.started_at ?? now;
  return (
    <span className="flex items-center gap-3 py-2 pl-3.5 pr-3">
      <span className="flex items-center gap-2">
        <span className="relative flex size-2.5">
          <motion.span
            className={cn("absolute inset-0 rounded-full", handsFree ? "bg-[var(--c-accent)]" : "bg-[#ff4d55]")}
            animate={{ scale: [1, 2.2], opacity: [0.6, 0] }}
            transition={{ duration: 1.2, repeat: Infinity, ease: "easeOut" }}
          />
          <span className={cn("relative size-2.5 rounded-full", handsFree ? "bg-[var(--c-accent)]" : "bg-[#ff4d55]")} />
        </span>
        <span className="tabular w-[38px] text-[12.5px] font-semibold text-white/90">{fmtClock(now - started)}</span>
      </span>
      <span className="w-[118px]">
        <LiveWave
          active
          bars={21}
          barWidth={3}
          gap={2.6}
          height={24}
          color={handsFree ? undefined : "rgba(255,255,255,0.95)"}
        />
      </span>
      <AnimatePresence initial={false}>
        {(handsFree || broadcast || command) && (
          <motion.span
            initial={{ opacity: 0, width: 0 }}
            animate={{ opacity: 1, width: "auto" }}
            exit={{ opacity: 0, width: 0 }}
            className="flex items-center gap-1 overflow-hidden"
          >
            {handsFree && <PillBadge icon={<Hand />} tone="accent">{t("pill.handsFree")}</PillBadge>}
            {broadcast && <PillBadge icon={<MonitorSmartphone />} tone="blue">{t("pill.broadcast")}</PillBadge>}
            {command && <PillBadge icon={<Terminal />} tone="amber">{t("pill.command")}</PillBadge>}
          </motion.span>
        )}
      </AnimatePresence>
    </span>
  );
}

function PillBadge({ icon, children, tone }: { icon: ReactNode; children: ReactNode; tone: "accent" | "blue" | "amber" }) {
  return (
    <motion.span
      layout
      initial={{ opacity: 0, scale: 0.8 }}
      animate={{ opacity: 1, scale: 1 }}
      className={cn(
        "flex h-5 items-center gap-1 whitespace-nowrap rounded-full px-2 text-[10.5px] font-semibold [&>svg]:size-3",
        tone === "accent" && "bg-[color-mix(in_oklab,var(--c-accent)_30%,transparent)] text-white",
        tone === "blue" && "bg-[#3e7bfa]/30 text-[#b9d0ff]",
        tone === "amber" && "bg-[#f5a524]/25 text-[#ffd48a]",
      )}
    >
      {icon}
      {children}
    </motion.span>
  );
}

function Notice({ n }: { n: NoticeEvent }) {
  const err = n.level === "error";
  const info = n.level === "info";
  const action =
    n.action === "relaunch_admin" ? (
      <button
        type="button"
        onClick={() => void relaunchAdmin()}
        className="flex h-6 shrink-0 items-center gap-1 rounded-full bg-white px-2.5 text-[11px] font-semibold text-[#15151a] transition-transform hover:scale-[1.03] active:scale-95"
      >
        <ShieldAlert className="size-3" />
        {t("pill.relaunchAdmin")}
      </button>
    ) : n.action === "open_settings_ai" ? (
      <button
        type="button"
        onClick={() => void showMain("settings/ai")}
        className="flex h-6 shrink-0 items-center rounded-full bg-white px-2.5 text-[11px] font-semibold text-[#15151a] transition-transform hover:scale-[1.03] active:scale-95"
      >
        {t("pill.openSettings")}
      </button>
    ) : n.action === "open_settings_insert" ? (
      <button
        type="button"
        onClick={() => void showMain(RDP_SESSIONS_ROUTE)}
        className="flex h-6 shrink-0 items-center gap-1 rounded-full bg-white px-2.5 text-[11px] font-semibold text-[#15151a] transition-transform hover:scale-[1.03] active:scale-95"
      >
        <MonitorSmartphone className="size-3" />
        {t("pill.chooseSessions")}
      </button>
    ) : n.action === "open_recordings" ? (
      <button
        type="button"
        onClick={() => void showMain("history/recordings")}
        className="flex h-6 shrink-0 items-center rounded-full bg-white px-2.5 text-[11px] font-semibold text-[#15151a] transition-transform hover:scale-[1.03] active:scale-95"
      >
        {t("pill.openRecordings")}
      </button>
    ) : null;
  const icon = (
    <span
      className={cn(
        "flex size-6 shrink-0 items-center justify-center rounded-full",
        err ? "bg-[#ff4d55]/20 text-[#ff8a8e]" : info ? "bg-white/12 text-white/85" : "bg-[#f5a524]/20 text-[#ffc56b]",
      )}
    >
      {err ? <CircleAlert className="size-3.5" /> : info ? <Info className="size-3.5" /> : <TriangleAlert className="size-3.5" />}
    </span>
  );
  // Тексты ядра бывают длинными (заголовок окна + что делать, ~160 символов), а окно
  // пилюли — 360×96. С кнопкой текст идёт на всю ширину (3 строки), значок — у кнопки;
  // без кнопки — 4 строки рядом со значком. Не влезло — полный текст во всплывающей подсказке.
  return action ? (
    <span className="flex w-[336px] flex-col gap-1 px-3 pb-1.5 pt-1.5" title={n.text}>
      <span className="line-clamp-3 text-[11.5px] font-medium leading-[15px] text-white/90">{n.text}</span>
      <span className="flex items-center justify-between gap-2">
        {icon}
        {action}
      </span>
    </span>
  ) : (
    <span className="flex max-w-[336px] items-center gap-2.5 px-3 py-2" title={n.text}>
      {icon}
      <span className="line-clamp-4 min-w-0 flex-1 text-[12px] font-medium leading-snug text-white/90">{n.text}</span>
    </span>
  );
}
