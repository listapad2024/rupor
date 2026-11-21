// Мок-бэкенд (?mock=1): полноценная имитация ядра со всеми командами и событиями
// протокола, чтобы интерфейс можно было оценить в обычном браузере без Python.
// Данные — в mock-data.json (русские) и mock-data.en.json (английские, ?lang=en или
// английский язык системы): набор данных — по языку при старте, а сообщения «ядра»
// (notice, ошибки, стили) — на текущем языке интерфейса, как у настоящего ядра.
import dataRu from "./mock-data.json";
import dataEn from "./mock-data.en.json";
import { fillersMock } from "./mockFillers";
import { navigatorLocale, type Locale } from "../i18n";
import { fmtDecimal, titleRuleKey } from "./format";
import { BackendError, EventHub, type ArgsTuple, type Backend } from "./hub";
import { notesModelId } from "./notesModels";
import type {
  AiBalance,
  AiKeyEntry,
  AiKeyInfo,
  AiKeyItem,
  AiUsageKind,
  AiUsageStats,
  AiUsageTotals,
  AppRule,
  AudioDevice,
  AudioSource,
  ClearPeriod,
  CommandArgs,
  CommandName,
  CommandResult,
  ConnStatus,
  CoreState,
  CoreStateName,
  DictItem,
  EventMap,
  EventName,
  ExportData,
  HistoryItem,
  HistoryTarget,
  KeyProvider,
  LanguagePref,
  LearnJournalEntry,
  LearnMode,
  LearnSource,
  MeetingLine,
  MeetingNotes,
  MeetingStatus,
  MeetingSuggest,
  ModelOption,
  ModelRecommend,
  NoticeEvent,
  RdpWindow,
  RecordingItem,
  Settings,
  SettingsPatch,
  Snippet,
  Stats,
  TitleRule,
  VocabItem,
} from "./types";

export interface MockOptions {
  demo: boolean;
  firstRun: boolean;
  pill: boolean;
  /**
   * Окно перевода встречи (meeting.html?mock=1): с demo — сеанс уже идёт (Brave — Meet:
   * Daily standup), английские фразы с русским переводом идут по ходу речи — слова
   * появляются по мере распознавания (хвост курсивом), перевод растёт по словам, у части
   * фраз финальное распознавание исправляет слово, а пословный перевод заменяется переводом
   * всей фразы (refined).
   */
  meeting?: boolean;
  /**
   * ?suggest=1 — через ~1 с после подключения ядро «замечает встречу» (meeting_suggest:
   * Brave — Google Meet). На meeting.html сеанс тогда не идёт, а окно показывает карточку
   * «Обнаружена новая встреча»; ?suggest=banner — то же предложение полоской в панели (так
   * его показывает уже открытое окно перевода).
   */
  suggest?: boolean;
  /** Модель не скачана при пройденном мастере (?no_model=1) — баннер state=error. */
  noModel?: boolean;
  /**
   * ?learn=1|auto|suggest|off — режим learn_mode при старте; кроме off, через ~1 с после
   * подключения приходит уведомление «словарь учится» (для проверки пилюли и тостов).
   */
  learn?: LearnMode | null;
  /** ?ai=0 — ИИ выключен (карточка «Включить» на Главной, строки трея нет). */
  aiOff?: boolean;
  /** ?rdp=0 — нет открытых RDP-окон; ?rdp=none — окна есть, но ни одно не отмечено. */
  rdp?: "0" | "none" | null;
  /** ?low_balance=1 — баланс $0.62 и предупреждение «меньше $1». */
  lowBalance?: boolean;
  /**
   * ?gpu=0 — видеокарты NVIDIA нет: cuda_check без GPU, model_recommend советует small на
   * процессоре (для мастера: ?mock=1&first_run=1&gpu=0).
   */
  noGpu?: boolean;
  /**
   * ?scene=broadcast|broadcast_none — один раз показать запись с рассылкой (с адресатами /
   * без отмеченных сессий, после неё — уведомление «Не выбраны RDP-сессии»).
   * ?scene=notice_admin|notice_insert|notice_ai — одно уведомление ядра с его
   * настоящим текстом (самые длинные: проверка, что пилюля и тосты их не обрезают).
   * meeting.html?mock=1&scene=meeting_ended — через ~2,5 с встреча «закончилась»
   * (MeetingStatus.hint = "ended": подсказка с кнопкой «Стоп» в окне перевода).
   * meeting.html?mock=1&scene=meeting_ending — через ~2,5 с автоостановка (hint "ending",
   * отсчёт ending_in_s 10 с, «Не останавливать» — meeting_autostop_cancel), потом «Стоп» и
   * уведомление, как у ядра (с &mini=1 — то же в полоске).
   * meeting.html?mock=1&scene=meeting_notes — встреча целиком прозвучала и остановлена: заметки
   * пишутся сами (текст по ходу), потом готовы, их название — название записи.
   */
  scene?: string | null;
  /**
   * ?win10=1 — Windows 10: звук одной программы захватить нельзя (loopback_available=false,
   * у программ available=false), перевод встречи с программой — как у ядра, весь звук
   * компьютера (meeting.html?mock=1&win10=1, с &suggest=1 — карточка предложения).
   */
  win10?: boolean;
  /**
   * ?lang=en|ru — «язык Windows» демо-ядра: при language="system" интерфейс на нём, и
   * демо-данные (история, словарь, сниппеты) на нём же. null — по navigator.language.
   */
  lang?: Locale | null;
}

interface LearnSample {
  term: string;
  variant: string;
}

interface Phrase {
  app: string;
  title: string;
  style: string;
  ai: boolean;
  raw: string;
  text: string;
  target?: string;
}

interface DayStat {
  words: number;
  speech_s: number;
  dictations: number;
}

interface UsageRow {
  ts: number;
  kind: AiUsageKind;
  model: string;
  tokens_in: number;
  tokens_out: number;
  cost: number;
  ok: boolean;
}

interface VocabWord {
  word: string;
  count: number;
  apps: Partial<Record<string, number>>;
}

interface RdpBase {
  key: string;
  title: string;
  process: string;
}

/** Фраза сценария встречи (mock-data*.json → meeting.lines). */
interface MeetingPhrase {
  lang: string;
  /** Как фразу распознаёт финальный проход (beam 5). */
  text: string;
  /** Как её слышат промежуточные проходы (beam 1), если иначе; финальный исправляет на text. */
  heard?: string;
  /** Перевод всей фразы (уточнённый, если есть draft). */
  translation: string | null;
  /** Пословный перевод по ходу речи; конец фразы — его заменяет translation (refined). */
  draft?: string;
  /** Перевод «не успевает» — строка с ошибкой. */
  error?: boolean;
}

/** Предложение встречи в демо-данных: процесс источника, приложение, название встречи. */
interface MeetingSuggestData {
  pid: number;
  app: string;
  title: string;
}

/** Источник звука в демо-данных (id и kind достраивает мок). */
type MeetingSourceData = Omit<AudioSource, "id" | "kind">;

/** Идущий сеанс перевода встречи демо-ядра. */
interface MeetingRun {
  lineTimer: ReturnType<typeof setTimeout> | null;
  levelTimer: ReturnType<typeof setInterval>;
  /** Таймеры обновлений живых строк (сняты при остановке). */
  streamTimers: Set<ReturnType<typeof setTimeout>>;
  /** Следующая фраза сценария. */
  next: number;
  startedMs: number;
  /** Демо «идёт диктовка — распознавание ждёт видеокарту» до этого времени. */
  gpuUntil: number;
  levelPhase: number;
  /** Задержки строк сеанса, мс от конца фразы (MeetingStatus.latency — средние). */
  lat: { asr: number[]; first: number[]; done: number[] };
  /** Готовых строк (для названия по содержанию) и было ли уже название от «ИИ». */
  finals: number;
  titled: boolean;
}

/** Заметки «пишутся»: таймеры показа по ходу и сеанс, к которому они. */
interface NotesRun {
  timers: ReturnType<typeof setTimeout>[];
  startedAt: string | null;
}

// Темп демо-речи и распознавания по ходу фразы (как у ядра: проход раз в ~0,85 с, LocalAgreement-2)
const WORD_MS = 360;
const STEP_MS = 850;
const DECODE_MS = 250;
/** Пауза, после которой фраза считается законченной, и финальный проход (beam 5). */
const VAD_END_MS = 380;
const FINAL_DECODE_MS = 320;
/** Кусок перевода — с этого числа подтверждённых слов (или конец фразы). */
const CHUNK_WORDS = 5;
/** Первые слова перевода через столько после запроса; дальше — по 2 слова раз в 100 мс. */
const TTFT_MS = 480;
/** Не чаще раза в столько мс на строку (как ядро: ≤ 10 обновлений в секунду). */
const LINE_UPDATE_MS = 100;

// Цена как у anthropic/claude-haiku-4.5 в OpenRouter: $1 / 1M входных, $5 / 1M выходных токенов.
const PRICE: Record<string, [number, number]> = {
  "anthropic/claude-haiku-4.5": [1e-6, 5e-6],
  "anthropic/claude-sonnet-4.5": [3e-6, 15e-6],
  "google/gemini-2.5-flash": [0.3e-6, 2.5e-6],
  // заметки к встрече — по умолчанию модель посильнее
  "anthropic/claude-sonnet-5.5": [2e-6, 10e-6],
  "anthropic/claude-opus-5.5": [4e-6, 20e-6],
};
/** Заметки «пишутся» столько (текст по ходу — кусками). */
const NOTES_WRITE_MS = 3200;
const NOTES_STEPS = 12;
/** Заметки сами после «Стоп» — если готовых строк столько (как у ядра). */
const NOTES_MIN_LINES = 5;
// Расход до окна в 30 дней (попадает только во «всё время»)
const USAGE_BEFORE: AiUsageTotals = { calls: 1412, tokens_in: 689_000, tokens_out: 91_300, cost_usd: 1.146 };
const BALANCE_START = 12.34;
const BALANCE_USED = 5.67;
const BALANCE_CACHE_MS = 5 * 60_000;
// Журнал обучения, как у ядра: хранится не больше 300 записей, отдаётся по 50.
const JOURNAL_MAX = 300;
// Перевод встречи: строки в памяти, как у ядра, — до 5000.
const MEETING_LINES_MAX = 5000;
const MEETING_DIR = "C:\\Users\\Demo\\AppData\\Roaming\\Rupor\\meetings";
const MEETING_LANG_CODES: string[] = ["ru", "en", "de", "fr", "es", "it", "pt", "pl", "uk", "zh", "ja", "ko", "tr"];

type MockData = typeof dataRu;
const DATA: Record<Locale, MockData> = { ru: dataRu, en: dataEn as MockData };
const DAY = 86_400;
const WORDS_PER_SEC = 2.3;

// Папка моделей: кэш Hugging Face по умолчанию (settings.model_dir = "") и модели Whisper,
// как их отдаёт ядро в model_recommend (размеры — запасная таблица ядра, МБ).
const HF_CACHE = "C:\\Users\\Demo\\.cache\\huggingface\\hub";
const MODEL_TABLE: Omit<ModelOption, "recommended" | "note">[] = [
  { model: "tiny", repo: "Systran/faster-whisper-tiny", size_mb: 75, quality: 1, speed: 5 },
  { model: "base", repo: "Systran/faster-whisper-base", size_mb: 145, quality: 2, speed: 5 },
  { model: "small", repo: "Systran/faster-whisper-small", size_mb: 484, quality: 3, speed: 4 },
  { model: "medium", repo: "Systran/faster-whisper-medium", size_mb: 1530, quality: 4, speed: 3 },
  { model: "large-v3-turbo", repo: "mobiuslabsgmbh/faster-whisper-large-v3-turbo", size_mb: 1620, quality: 5, speed: 4 },
  { model: "large-v3", repo: "Systran/faster-whisper-large-v3", size_mb: 3090, quality: 5, speed: 2 },
];

/** Ключ папки: регистр, «/» и конечный «\» не важны (как пути Windows). */
function dirKey(p: string): string {
  return p.trim().replace(/\//g, "\\").replace(/\\+$/, "").toLowerCase();
}

/** МБ → ГБ с одним знаком, как в сообщениях ядра о месте на диске («1,6»). */
function fmtGb(mb: number): string {
  return fmtDecimal(mb / 1024, 1);
}

/** Демо-звук записи: тихий тон, WAV 16 кГц моно 16 бит в base64. */
function demoWav(seconds: number): string {
  const rate = 16000;
  const n = Math.max(1, Math.round(rate * seconds));
  const buf = new DataView(new ArrayBuffer(44 + n * 2));
  const str = (o: number, s: string) => [...s].forEach((ch, i) => buf.setUint8(o + i, ch.charCodeAt(0)));
  str(0, "RIFF");
  buf.setUint32(4, 36 + n * 2, true);
  str(8, "WAVEfmt ");
  buf.setUint32(16, 16, true);
  buf.setUint16(20, 1, true);
  buf.setUint16(22, 1, true);
  buf.setUint32(24, rate, true);
  buf.setUint32(28, rate * 2, true);
  buf.setUint16(32, 2, true);
  buf.setUint16(34, 16, true);
  str(36, "data");
  buf.setUint32(40, n * 2, true);
  for (let i = 0; i < n; i++) buf.setInt16(44 + i * 2, Math.round(1500 * Math.sin((2 * Math.PI * 220 * i) / rate)), true);
  let bin = "";
  const bytes = new Uint8Array(buf.buffer);
  for (let i = 0; i < bytes.length; i++) bin += String.fromCharCode(bytes[i]);
  return btoa(bin);
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

function mulberry32(seed: number): () => number {
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6d2b79f5) >>> 0;
    let x = a;
    x = Math.imul(x ^ (x >>> 15), x | 1);
    x ^= x + Math.imul(x ^ (x >>> 7), x | 61);
    return ((x ^ (x >>> 14)) >>> 0) / 4294967296;
  };
}

function countWords(s: string): number {
  return s.split(/\s+/).filter((w) => /[\p{L}\p{N}]/u.test(w)).length;
}

function dateKey(d: Date): string {
  const y = d.getFullYear();
  const m = String(d.getMonth() + 1).padStart(2, "0");
  const dd = String(d.getDate()).padStart(2, "0");
  return `${y}-${m}-${dd}`;
}

function mask(key: string): string {
  if (key.includes("…")) return key;
  return `${key.slice(0, 9)}…${key.slice(-3)}`;
}

function fill(s: string, p: Record<string, string | number>): string {
  return s.replace(/\{(\w+)\}/g, (m, k: string) => (k in p ? String(p[k]) : m));
}

/** Форма множественного числа, как у ядра (tr_n): ключи one/few/many/other по Intl.PluralRules. */
function pluralForm(forms: string | Partial<Record<Intl.LDMLPluralRule, string>>, n: number, lang: Locale): string {
  if (typeof forms === "string") return forms;
  const cat = new Intl.PluralRules(lang === "ru" ? "ru-RU" : "en-US").select(n);
  return forms[cat] ?? forms.other ?? "";
}

/**
 * Уведомления ядра с его настоящими текстами (mock-data.*.json сверяет с core/rupor/locales
 * npm run check:i18n) и подстановками, как их делает engine.py: длинный заголовок окна,
 * ошибка вставки. (eng.replace_skipped — info без toast: его не показывают ни пилюля, ни тосты.)
 */
function coreNotices(t: MockData["texts"]): Record<string, Omit<NoticeEvent, "data">> {
  return {
    admin: { text: fill(t.noticeAdmin, { name: t.adminWindow }), level: "warn", action: "relaunch_admin", toast: true },
    meeting: { text: fill(t.meetingSuggest, { name: "Brave" }), level: "info", action: "meeting_start", toast: true },
    ai: { text: fill(t.noticeAi, { tail: t.aiTailInserted }), level: "warn", action: null, toast: true },
    insert: { text: fill(t.insertFailed, { error: t.insertError }), level: "error", action: null, toast: true },
  };
}

const DEFAULT_SETTINGS: Settings = {
  ptt_key: "ctrl+win",
  broadcast_modifier: "shift",
  command_modifier: "alt",
  cancel_key: "esc",
  tap_max_ms: 250,
  double_tap_ms: 400,
  insert_last_hotkey: "ctrl+alt+shift+v",
  copy_last_hotkey: "ctrl+alt+shift+c",
  convert_layout_hotkey: "shift+pause",
  mouse_button: "",
  input_device: null,
  min_record_ms: 300,
  model: "large-v3-turbo",
  model_dir: "",
  device: "cuda",
  compute_type: "float16",
  beam_size: 5,
  en_threshold: 0.8,
  terms: ["ClickHouse", "Metabase", "Python", "API", "SQL", "RDP", "GitHub", "Grafana"],
  unload_after_min: 0,
  insert_mode: "auto",
  type_delay_ms: 0,
  rdp_type_delay_ms: 5,
  type_chunk: 32,
  clipboard_restore_ms: 300,
  rdp_classes: ["TscShellContainerClass"],
  rdp_processes: ["mRemoteNG.exe", "RDCMan.exe"],
  app_rules: {
    "telegram.exe": { insert_mode: null, style: "messenger" },
    "outlook.exe": { insert_mode: null, style: "business" },
    "mstsc.exe": { insert_mode: "type", style: null },
  },
  title_rules: (DATA.ru.titleRules as TitleRule[]).map((r) => ({ ...r })),
  broadcast_targets: [...DATA.ru.broadcastTargets],
  add_trailing_space: true,
  learn_mode: "auto",
  dict_fuzzy: true,
  vocab_enabled: true,
  context_enabled: true,
  duck_mode: "duck",
  duck_level: 15,
  app_mute_enabled: true,
  app_mute_apps: ["codex.exe", "chatgpt.exe"],
  sounds: true,
  autostart: true,
  default_style: "normal",
  history_enabled: true,
  history_retention_days: 0,
  recordings_keep: 30,
  recordings_max_mb: 300,
  pill_enabled: true,
  pill_position: null,
  theme: "system",
  accent: "#7c5cff",
  language: "system",
  dictation_language: "auto",
  first_run_done: true,
  ai_enabled: true,
  ai_provider: "keys",
  ai_keys: [
    { id: "anthropic", provider: "anthropic", label: "", enabled: true, balance_usd: 80, balance_at: Math.floor(Date.now() / 1000) - 3 * 86400 },
    { id: "openrouter", provider: "openrouter", label: "", enabled: true, balance_usd: null, balance_at: null },
  ],
  ai_model: "anthropic/claude-haiku-4.5",
  anthropic_model: "claude-haiku-4-5",
  ollama_model: "qwen2.5:7b",
  ollama_url: "http://127.0.0.1:11434",
  ai_provider_order: ["amazon-bedrock", "anthropic"],
  ai_mode: "wait",
  ai_min_words: 6,
  ai_timeout_s: 5,
  command_provider: "keys",
  env_path: "",
  meeting_target_lang: "ru",
  meeting_source_lang: "auto",
  meeting_translate: true,
  meeting_show_original: true,
  meeting_font_size: 16,
  meeting_autosuggest: true,
  meeting_autostop: true,
  meeting_title_patterns: [...DATA.ru.meetingPatterns],
  meeting_detect_desktop_apps: true,
  meeting_ignore_apps: ["obs64.exe", "natspeak.exe", "ffmpeg.exe", "python.exe", "pythonw.exe"],
  meeting_refine: true,
  meeting_save: true,
  meeting_ai_title: true,
  meeting_notes: true,
  meeting_notes_model: "",
  meeting_dir: "",
  meeting_protect: true,
  meeting_rect: null,
  meeting_mini_pos: null,
  translate_model: "",
  translate_timeout_s: 6,
};

function pad2(n: number): string {
  return String(n).padStart(2, "0");
}

/** Местное время ISO с мс, без зоны: «2026-10-19T11:05:00.123» (как started_at ядра — ключ сеанса). */
function localIso(ms: number): string {
  const d = new Date(ms);
  const msPart = String(d.getMilliseconds()).padStart(3, "0");
  return `${dateKey(d)}T${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}.${msPart}`;
}

/** «ЧЧ:ММ:СС» — как MeetingLine.ts у ядра. */
function clockOf(ms: number): string {
  const d = new Date(ms);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

/** Часть имени файла, как safe_file_part ядра: без запрещённых символов и точек в конце, ≤ 80. */
function safeFilePart(name: string): string {
  let s = name.replace(/[<>:"/\\|?*\x00-\x1f]+/g, " ").split(/\s+/).filter(Boolean).join(" ");
  if (s.length > 80) {
    const cut = s.slice(0, 81);
    const sp = cut.lastIndexOf(" ");
    s = sp >= 40 ? cut.slice(0, sp) : s.slice(0, 80);
  }
  s = s.replace(/[ .]+$/, "");
  return s || "meeting";
}

/**
 * Название записи без ИИ (коротко, как meeting.fallback_title ядра): заголовок окна без хвоста
 * браузера, код Google Meet — «Google Meet»; заголовка нет — имя программы, у всего звука
 * компьютера — generic («Встреча»).
 */
function fallbackTitle(src: AudioSource, generic: string): string {
  const t =
    src.kind === "system"
      ? ""
      : src.title.replace(/\s*[-–—|]\s*(?:google chrome|brave|microsoft\s*edge|mozilla firefox|firefox|opera|vivaldi)\s*$/i, "").trim();
  if (/^meet\s*[-–—]\s*[a-z]{3,4}-[a-z]{3,4}-[a-z]{3,4}$/i.test(t)) return "Google Meet";
  if (t && t.toLowerCase() !== src.name.toLowerCase()) return t;
  return src.kind === "system" ? generic : src.name || generic;
}

function idleMeeting(target: Settings["meeting_target_lang"], translate: boolean, loopback: boolean): MeetingStatus {
  return {
    running: false,
    source: null,
    started_at: null,
    lines: 0,
    level: 0,
    state: "idle",
    error: null,
    translate,
    target_lang: target,
    file: null,
    title: null,
    latency: { asr_ms: null, first_token_ms: null, done_ms: null },
    hint: null,
    ending_in_s: null,
    loopback_available: loopback,
    notes: null,
  };
}

/** MeetingStatus.notes: заметки без текста. */
function notesInfo(n: MeetingNotes | null): MeetingStatus["notes"] {
  if (!n) return null;
  return { status: n.status, generated_at: n.generated_at, model: n.model, cost_usd: n.cost_usd, error: n.error, auto: n.auto };
}

/**
 * Программа для meeting_ignore_apps, как у ядра (config._exe_names): имя файла без пути, в
 * нижнем регистре, без расширения — с «.exe».
 */
function normalizeExe(x: string): string {
  const name = (x.trim().replace(/\//g, "\\").split("\\").pop() ?? "").trim().toLowerCase();
  return name && !name.includes(".") ? `${name}.exe` : name;
}

/** Среднее, мс; пусто — null. */
function avgMs(xs: number[]): number | null {
  return xs.length ? Math.round(xs.reduce((a, b) => a + b, 0) / xs.length) : null;
}

interface Scenario {
  kind: "dictation" | "notice" | "learn";
  /** Рассылка сразу при старте записи (recording.broadcast=true с targets). */
  broadcastAtStart?: boolean;
  hands_free?: boolean;
  broadcast?: boolean;
  command?: boolean;
  ai?: boolean;
  dur?: number;
  notice?: NoticeEvent;
}

export class MockBackend implements Backend {
  readonly mock = true;
  private hub = new EventHub<EventMap>();
  private st: ConnStatus = "connecting";
  private statusFns = new Set<(s: ConnStatus) => void>();
  private started = false;
  private opts: MockOptions;

  private settings: Settings;
  private state: CoreState;
  private history: HistoryItem[] = [];
  /** Демо «История → Записи»: записи голоса строятся из первых диктовок истории. */
  private recordings: RecordingItem[] | null = null;
  private daily = new Map<string, DayStat>();
  private baseWords = 48_214;
  private baseSpeech = 48_214 / WORDS_PER_SEC;
  private dict: DictItem[];
  private snippets: Snippet[];
  private nextId = 1;
  /** Демо-данные на языке «системы» при старте; сообщения — this.tx (язык интерфейса). */
  private data: MockData;
  private sysLang: Locale;
  private draft: string;
  /** Секреты ключей по id записи списка (маска); id = провайдер — прежняя цель Rupor/<провайдер>. */
  private secrets: Record<string, string | null> = {
    openrouter: "sk-or-v1-…XYZ",
    anthropic: "sk-ant-api03-…k9Q",
  };
  /** Расход демо-ключей: всего и с момента ввода остатка (Anthropic «уже потратил» $3.10). */
  private keySpent: Record<string, number> = { anthropic: 3.1 };
  private rdpOpen: RdpBase[];
  private usage: UsageRow[] = [];
  private balanceCache: { at: number; value: AiBalance } | null = null;
  private balanceSpent = 0;
  private vocabExcluded: Set<string>;
  private cudaReady = true;
  /** Какие модели лежат в какой папке (ключ — dirKey). */
  private stored = new Map<string, Set<string>>();
  /** Слот «model» ядра: пока модель качается или переносится, второе не начнётся. */
  private modelJob: "download" | "move" | null = null;
  private micTimer: ReturnType<typeof setInterval> | null = null;
  private testRec: { started: number; timer: ReturnType<typeof setInterval> } | null = null;
  private busy = false;
  private phraseIdx = 0;
  private learnIdx = 0;
  /** suggest: сколько раз пользователь сделал ту же правку (вариант → термин). */
  private learnSeen = new Map<string, number>();
  /** Журнал обучения (dict_learn_journal): что изменила каждая выученная правка, старые первыми. */
  private journal: LearnJournalEntry[] = [];
  /** Строки последнего сеанса перевода встречи (остаются и после остановки). */
  private meetingLines: MeetingLine[] = [];
  private meetingRun: MeetingRun | null = null;
  /** Последнее предложение встречи без ответа (?suggest=1). */
  private meetingSuggest: MeetingSuggest | null = null;
  /** ?win10=1: уведомление «слушаю весь звук компьютера» уже было (как у ядра — раз за запуск). */
  private loopbackFallbackTold = false;
  /** Заметки последнего сеанса (MeetingNotes) и их разделы без «# название». */
  private meetingNotes: MeetingNotes | null = null;
  private notesBody: string | null = null;
  private notesRun: NotesRun | null = null;

  constructor(opts: MockOptions) {
    this.opts = opts;
    this.sysLang = opts.lang ?? navigatorLocale();
    this.data = DATA[this.sysLang];
    this.draft = this.data.draft;
    this.vocabExcluded = new Set<string>((this.data.vocabExcluded as string[]).map((w) => w.toLowerCase()));
    this.settings = structuredClone(DEFAULT_SETTINGS);
    // Демо-встреча английского набора — на немецком, перевод — на английский
    if (this.sysLang === "en") this.settings.meeting_target_lang = "en";
    if (opts.firstRun) {
      this.settings.first_run_done = false;
      this.settings.ai_enabled = false;
      this.cudaReady = false;
      this.secrets = {};
      this.settings.ai_keys = [];
    }
    if (opts.noGpu && !opts.firstRun) {
      this.settings.device = "cpu";
      this.settings.compute_type = "int8";
    }
    // Демо: в кэше Hugging Face лежат все модели; на чистой машине (мастер, ?no_model=1) — ни одной
    const haveModels = !(opts.firstRun || opts.noModel);
    this.stored.set(dirKey(HF_CACHE), new Set(haveModels ? MODEL_TABLE.map((m) => m.model) : []));
    this.state = {
      state: "idle",
      paused: false,
      device: "cuda",
      model_loaded: true,
      insert_mode: this.settings.insert_mode,
      style: this.settings.default_style,
      ai_enabled: this.settings.ai_enabled,
      first_run_done: this.settings.first_run_done,
      version: __APP_VERSION__, // демо-ядро той же версии, что и интерфейс
      elevated: false,
      hotkeys: this.hotkeys(),
      ui_language: this.uiLanguage(),
      meeting: idleMeeting(this.settings.meeting_target_lang, this.settings.meeting_translate, !opts.win10),
    };
    if (opts.noGpu) this.state.device = "cpu";
    if (opts.firstRun || opts.noModel) {
      // как настоящее ядро на чистой машине: модели нет, ждём setup_download → state=error
      this.state = { ...this.state, state: "error", device: null, model_loaded: false };
    }
    if (opts.learn) this.settings.learn_mode = opts.learn;
    this.dict = this.initialDict();
    if (!opts.firstRun) this.journal = this.initialJournal();
    this.snippets = this.data.snippets.map((s) => ({ id: this.nextId++, trigger: s.trigger, text: s.text }));
    if (opts.aiOff) this.settings.ai_enabled = false;
    this.state.ai_enabled = this.settings.ai_enabled;
    this.rdpOpen = opts.rdp === "0" ? [] : (this.data.rdpWindows as RdpBase[]).map((w) => ({ ...w }));
    if (opts.rdp === "none") this.settings.broadcast_targets = [];
    this.generateHistory();
    this.generateUsage();
  }

  // ——— Backend ———

  start(): void {
    if (this.started) return;
    this.started = true;
    setTimeout(() => {
      this.setStatus("open");
      this.hub.emit("hello", this.snapshot());
      if (this.opts.pill && this.opts.demo) void this.pillDemo();
      if (this.opts.meeting && this.opts.demo && !this.opts.suggest) this.meetingDemo();
      if (this.opts.suggest) setTimeout(() => this.emitMeetingSuggest(), 1100);
      if (this.opts.learn && this.opts.learn !== "off") setTimeout(() => this.emitLearn(true), 600);
      if (this.opts.lowBalance && this.settings.ai_enabled) {
        setTimeout(
          () =>
            this.hub.emit("notice", {
              text: fill(this.tx.balanceLow, { name: "OpenRouter", balance: "0.62" }),
              level: "warn",
              action: null,
              toast: true,
              data: null,
            }),
          900,
        );
      }
      const scene = this.opts.scene ?? "";
      if (scene === "broadcast" || scene === "broadcast_none" || scene === "broadcast_all") {
        if (scene === "broadcast_none") this.settings.broadcast_targets = [];
        if (scene === "broadcast_all") this.settings.broadcast_targets = this.rdpOpen.map((w) => w.key);
        // с адресатами — долгая запись (снимок пилюли), без — короткая, затем notice
        const dur = scene === "broadcast_none" ? 3500 : 60_000;
        setTimeout(() => void this.runScenario({ kind: "dictation", dur, broadcast: true, broadcastAtStart: true }), 500);
      }
      const demo = scene.startsWith("notice_") ? coreNotices(this.tx)[scene.slice("notice_".length)] : undefined;
      if (demo) setTimeout(() => this.hub.emit("notice", { ...demo, data: null }), 500);
    }, 350);
  }

  status(): ConnStatus {
    return this.st;
  }

  onStatus(fn: (s: ConnStatus) => void): () => void {
    this.statusFns.add(fn);
    return () => {
      this.statusFns.delete(fn);
    };
  }

  on<E extends EventName>(event: E, fn: (data: EventMap[E]) => void): () => void {
    return this.hub.on(event, fn);
  }

  call<C extends CommandName>(cmd: C, ...rest: ArgsTuple<C>): Promise<CommandResult<C>> {
    const args = rest[0] as CommandArgs<C>;
    const delay = 60 + Math.random() * 120;
    return new Promise<CommandResult<C>>((resolve, reject) => {
      setTimeout(() => {
        Promise.resolve()
          .then(() => this.handle(cmd, args))
          .then((r) => resolve(r as CommandResult<C>))
          .catch((e: unknown) =>
            reject(e instanceof Error ? e : new BackendError(String(e), cmd)),
          );
      }, delay);
    });
  }

  simulateDictation = (): void => {
    if (this.busy) return;
    const ai = this.settings.ai_enabled;
    // Через ~3 с после вставки «пользователь исправил слово» — словарь учится (learn_mode).
    void this.runScenario({ kind: "dictation", ai, dur: 2600 + Math.random() * 1800 }).then(() =>
      setTimeout(() => this.emitLearn(false), 3000),
    );
  };

  // ——— Внутреннее ———

  private setStatus(s: ConnStatus): void {
    this.st = s;
    for (const fn of [...this.statusFns]) fn(s);
  }

  /** Как ядро: language="system" → язык Windows (здесь — ?lang или navigator.language). */
  private uiLanguage(pref: LanguagePref = this.settings.language): Locale {
    return pref === "ru" || pref === "en" ? pref : this.sysLang;
  }

  /** Демо-записи голоса: по одной на первые диктовки истории, последняя — «речь не распознана». */
  private recordingsDemo(): RecordingItem[] {
    if (this.recordings === null) {
      const src = this.history.slice(0, 7);
      this.recordings = src.map((h, i) => {
        const empty = i === 2;
        const d = empty ? 153.2 : h.duration_s;
        const ts = h.ts;
        const dt = new Date(ts * 1000);
        const p2 = (n: number) => String(n).padStart(2, "0");
        const id =
          `${dt.getFullYear()}${p2(dt.getMonth() + 1)}${p2(dt.getDate())}-` +
          `${p2(dt.getHours())}${p2(dt.getMinutes())}${p2(dt.getSeconds())}-${String(i).padStart(3, "0")}`;
        return {
          id, ts, duration_s: d, bytes: Math.round(d * 32000) + 44, app: h.app,
          status: empty ? "empty" : "ok", chars: empty ? 0 : h.text.length,
          history_id: empty ? null : h.id,
        };
      });
    }
    return this.recordings;
  }

  /** Тексты «ядра» на текущем языке интерфейса. */
  private get tx(): MockData["texts"] {
    return DATA[this.state.ui_language].texts;
  }

  private get phrases(): Phrase[] {
    return this.data.phrases as Phrase[];
  }

  private hotkeys(): CoreState["hotkeys"] {
    return {
      ptt: this.settings.ptt_key,
      broadcast: this.settings.broadcast_modifier,
      command: this.settings.command_modifier,
      cancel: this.settings.cancel_key,
      insert_last: this.settings.insert_last_hotkey,
      copy_last: this.settings.copy_last_hotkey,
      convert_layout: this.settings.convert_layout_hotkey,
    };
  }

  private snapshot(): CoreState {
    return { ...this.state, hotkeys: { ...this.state.hotkeys }, meeting: structuredClone(this.state.meeting) };
  }

  private setState(name: CoreStateName): void {
    this.state = { ...this.state, state: name };
    this.hub.emit("state", this.snapshot());
  }

  private syncState(): void {
    this.state = {
      ...this.state,
      insert_mode: this.settings.insert_mode,
      style: this.settings.default_style,
      ai_enabled: this.settings.ai_enabled,
      first_run_done: this.settings.first_run_done,
      hotkeys: this.hotkeys(),
      ui_language: this.uiLanguage(),
    };
    this.hub.emit("state", this.snapshot());
  }

  private addDay(ts: number, words: number, speech: number): void {
    const key = dateKey(new Date(ts * 1000));
    const d = this.daily.get(key) ?? { words: 0, speech_s: 0, dictations: 0 };
    d.words += words;
    d.speech_s += speech;
    d.dictations += 1;
    this.daily.set(key, d);
  }

  private makeItem(p: Phrase, ts: number, rnd: () => number): HistoryItem {
    const words = countWords(p.text);
    const duration = Math.max(1.2, words / WORDS_PER_SEC + rnd() * 1.5);
    const target: HistoryTarget =
      p.target === "rdp_all" || p.target === "command" || p.target === "window"
        ? p.target
        : "window";
    return {
      id: this.nextId++,
      ts,
      app: p.app,
      title: p.title,
      raw: p.raw,
      text: p.text,
      duration_s: Math.round(duration * 10) / 10,
      words,
      language: this.sysLang,
      style: p.style,
      ai: p.ai,
      latency_ms: Math.round(p.ai ? 1400 + rnd() * 900 : 380 + rnd() * 260),
      target,
    };
  }

  private generateHistory(): void {
    const rnd = mulberry32(20260926);
    const now = new Date();
    const items: HistoryItem[] = [];
    for (let ago = 29; ago >= 0; ago--) {
      const day = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ago);
      const weekend = day.getDay() === 0 || day.getDay() === 6;
      if (ago > 11 && rnd() < (weekend ? 0.55 : 0.12)) continue;
      let count = Math.round((weekend ? 6 : 24) + rnd() * (weekend ? 12 : 40));
      const dayStart = day.getTime() / 1000;
      const limit = ago === 0 ? Math.max(0, Date.now() / 1000 - dayStart - 9 * 3600) : 10 * 3600;
      if (ago === 0) count = Math.max(3, Math.round((count * limit) / (10 * 3600)));
      for (let i = 0; i < count; i++) {
        const p = this.phrases[Math.floor(rnd() * this.phrases.length)] ?? this.phrases[0];
        if (!p) continue;
        const ts = dayStart + 9 * 3600 + rnd() * Math.max(600, limit);
        if (ts > Date.now() / 1000) continue;
        const item = this.makeItem(p, ts, rnd);
        items.push(item);
        this.addDay(ts, item.words, item.duration_s);
      }
    }
    items.sort((a, b) => b.ts - a.ts);
    this.history = items;
  }

  private stats(): Stats {
    const now = new Date();
    const today = dateKey(now);
    const daily: Stats["daily"] = [];
    for (let ago = 29; ago >= 0; ago--) {
      const d = new Date(now.getFullYear(), now.getMonth(), now.getDate() - ago);
      const k = dateKey(d);
      daily.push({ date: k, words: this.daily.get(k)?.words ?? 0 });
    }
    let words = this.baseWords;
    let speech = this.baseSpeech;
    let dictations = 3140;
    for (const d of this.daily.values()) {
      words += d.words;
      speech += d.speech_s;
      dictations += d.dictations;
    }
    const week = daily.slice(-7).reduce((s, d) => s + d.words, 0);
    let streak = 0;
    let i = daily.length - 1;
    if ((daily[i]?.words ?? 0) === 0) i--;
    for (; i >= 0 && (daily[i]?.words ?? 0) > 0; i--) streak++;
    const minutes = speech / 60;
    return {
      words_today: this.daily.get(today)?.words ?? 0,
      words_week: week,
      words_total: words,
      wpm: Math.round(words / Math.max(1, minutes)),
      saved_minutes: Math.max(0, Math.round(words / 40 - minutes)),
      streak_days: streak,
      dictations_total: dictations,
      daily,
    };
  }

  private levelLoop(onTick?: (rms: number) => void): ReturnType<typeof setInterval> {
    const t0 = performance.now();
    const seed = Math.random() * 10;
    return setInterval(() => {
      const t = (performance.now() - t0) / 1000 + seed;
      const syll = Math.abs(Math.sin(t * 8.7)) * 0.75 + Math.random() * 0.35;
      const phrase = Math.sin(t * 1.3) + Math.sin(t * 0.61) > -0.6 ? 1 : 0.08;
      const rms = 0.0025 + 0.11 * syll * phrase * (0.7 + Math.random() * 0.3);
      this.hub.emit("level", { rms });
      onTick?.(rms);
    }, 33);
  }

  private nextPhrase(): Phrase {
    const p = this.phrases[this.phraseIdx % this.phrases.length] ?? this.phrases[0];
    this.phraseIdx++;
    if (!p) throw new Error("no phrases");
    return p;
  }

  private async runScenario(s: Scenario): Promise<void> {
    if (s.kind === "notice") {
      if (s.notice) this.hub.emit("notice", s.notice);
      await sleep(4600);
      return;
    }
    if (s.kind === "learn") {
      this.emitLearn(true);
      await sleep(6800);
      return;
    }
    if (this.state.paused) return;
    this.busy = true;
    const startedAt = Date.now();
    this.setState(s.hands_free ? "hands_free" : "recording");
    const atStart = Boolean(s.broadcast && s.broadcastAtStart);
    this.hub.emit("recording", {
      started_at: startedAt,
      broadcast: atStart,
      command: Boolean(s.command),
      hands_free: Boolean(s.hands_free),
      targets: atStart ? this.targetKeys() : [],
    });
    const timer = this.levelLoop();
    if (atStart) {
      await sleep(s.dur ?? 3000);
    } else if (s.broadcast) {
      await sleep(900);
      this.hub.emit("broadcast", { on: true });
      await sleep((s.dur ?? 3000) - 900);
    } else {
      await sleep(s.dur ?? 3000);
    }
    clearInterval(timer);
    this.setState("processing");
    await sleep(420);
    const useAi = Boolean(s.ai) && this.settings.ai_enabled;
    if (useAi) {
      this.setState("ai");
      await sleep(1100 + Math.random() * 600);
    }
    const p = this.nextPhrase();
    const audio = (Date.now() - startedAt) / 1000;
    if (useAi) this.recordUsage(s.command ? "command" : "cleanup", countWords(p.text));
    this.hub.emit("result", {
      text: p.text,
      raw: p.raw,
      language: this.sysLang,
      audio_s: audio,
      asr_ms: 310,
      latency_ms: useAi ? 1650 : 460,
      ai: useAi,
      style: p.style,
    });
    const target: HistoryTarget = s.broadcast ? "rdp_all" : s.command ? "command" : "window";
    const item: HistoryItem = {
      ...this.makeItem(p, Date.now() / 1000, Math.random),
      ai: useAi,
      target,
      duration_s: Math.round(audio * 10) / 10,
    };
    this.addDay(item.ts, item.words, item.duration_s);
    if (this.settings.history_enabled) {
      this.history.unshift(item);
      this.hub.emit("history", item);
    }
    this.setState("idle");
    this.busy = false;
    if (s.broadcast) {
      const n = this.targetKeys().length;
      this.hub.emit(
        "notice",
        n === 0
          ? { text: this.tx.noTargets, level: "warn", action: "open_settings_insert", toast: true, data: null }
          : { text: fill(pluralForm(this.tx.sentRdp, n, this.state.ui_language), { n }), level: "info", action: null, toast: false, data: null },
      );
    }
  }

  // ——— RDP: белый список рассылки ———

  private sameKey(a: string, b: string): boolean {
    return a.toLowerCase() === b.toLowerCase();
  }

  private rdpWindows(): RdpWindow[] {
    const targets = this.settings.broadcast_targets;
    return this.rdpOpen.map((w) => ({ ...w, included: targets.some((k) => this.sameKey(k, w.key)) }));
  }

  /** Куда уйдёт рассылка: открытые окна, чьи ключи отмечены. */
  private targetKeys(): string[] {
    return this.rdpWindows()
      .filter((w) => w.included)
      .map((w) => w.key);
  }

  // ——— Расход ИИ ———

  private usageRow(ts: number, kind: AiUsageKind, model: string, words: number, rnd: () => number, ok = true): UsageRow {
    const [pin, pout] = PRICE[model] ?? [1e-6, 5e-6];
    // название записи встречи читает расшифровку целиком (до ~8000 символов), заметки — тоже,
    // а пишут страницу
    const base =
      kind === "command" ? 520 : kind === "translate" ? 230 : kind === "title" ? 2400 : kind === "notes" ? 1400 : 430;
    const outPerWord = kind === "command" ? 3.4 : kind === "translate" ? 2.6 : 2.1;
    const tokens_in = Math.round(base + words * 2.2 + rnd() * 90);
    const tokens_out = ok ? Math.round((kind === "notes" ? 1600 : words * outPerWord) + 8 + rnd() * 12) : 0;
    const cost = ok ? tokens_in * pin + tokens_out * pout : 0;
    return { ts, kind, model, tokens_in: ok ? tokens_in : Math.round(tokens_in * 0.1), tokens_out, cost, ok };
  }

  /** 30 дней расхода: очистка по диктовкам с ИИ, немного команд, проверок и других моделей. */
  private generateUsage(): void {
    const rnd = mulberry32(20260927);
    const main = this.data.aiModels.main;
    for (const h of this.history) {
      if (!h.ai) continue;
      const kind: AiUsageKind = h.target === "command" ? "command" : "cleanup";
      const day = Math.floor((Date.now() / 1000 - h.ts) / DAY);
      // неделю назад пробовали другие модели
      const model = day >= 8 && day <= 11 ? (this.data.aiModels.other[day % 2] ?? main) : main;
      this.usage.push(this.usageRow(h.ts, kind, model, h.words, rnd, rnd() > 0.02));
      if (rnd() < 0.03) this.usage.push(this.usageRow(h.ts + 30, "test", main, 40, rnd));
    }
    // Три созвона с переводом за месяц: фраза раз в несколько секунд, около получаса
    const now = Date.now() / 1000;
    for (const ago of [2, 6, 13]) {
      const start = Math.floor(now / DAY - ago) * DAY + 10 * 3600 + rnd() * 3600;
      for (let i = 0; i < 260; i++) {
        this.usage.push(this.usageRow(start + i * 7, "translate", main, 6 + Math.round(rnd() * 14), rnd, rnd() > 0.01));
      }
      // название записи: раннее (8 строк, 90 с) и после «Стоп»
      this.usage.push(this.usageRow(start + 95, "title", main, 9, rnd));
      // после «Стоп» — заметки (их название и стало последним названием записи)
      this.usage.push(this.usageRow(start + 260 * 7 + 40, "notes", "anthropic/claude-sonnet-5.5", 2600, rnd));
    }
  }

  private recordUsage(kind: AiUsageKind, words: number): void {
    const model =
      kind === "notes"
        ? notesModelId(this.settings)
        : this.settings.ai_provider === "anthropic"
          ? this.settings.anthropic_model
          : this.settings.ai_provider === "ollama"
            ? this.settings.ollama_model
            : this.settings.ai_model;
    const row = this.usageRow(Date.now() / 1000, kind, model, words, Math.random);
    if (this.settings.ai_provider === "ollama") row.cost = 0;
    this.usage.push(row);
    this.balanceSpent += row.cost;
    const first = this.chain()[0];
    if (first && this.settings.ai_provider !== "ollama") {
      this.keySpent[first.id] = (this.keySpent[first.id] ?? 0) + row.cost;
    }
    const today = this.aiStats().today.cost_usd;
    this.hub.emit("ai_usage", {
      kind,
      cost_usd: row.cost,
      tokens_in: row.tokens_in,
      tokens_out: row.tokens_out,
      today_cost_usd: today,
    });
  }

  private aiStats(): AiUsageStats {
    const now = new Date();
    const keys: string[] = [];
    for (let ago = 29; ago >= 0; ago--) keys.push(dateKey(new Date(now.getFullYear(), now.getMonth(), now.getDate() - ago)));
    const zero = (): AiUsageTotals => ({ calls: 0, tokens_in: 0, tokens_out: 0, cost_usd: 0 });
    const byDay = new Map(keys.map((k) => [k, zero()]));
    const add = (t: AiUsageTotals, r: UsageRow) => {
      t.calls += 1;
      t.tokens_in += r.tokens_in;
      t.tokens_out += r.tokens_out;
      t.cost_usd += r.cost;
    };
    const week = new Set(keys.slice(-7));
    const today = keys[keys.length - 1];
    const tot = { today: zero(), week: zero(), month: zero(), total: { ...USAGE_BEFORE } };
    const kinds = new Map<AiUsageKind, { calls: number; cost_usd: number }>();
    const models = new Map<string, { calls: number; cost_usd: number }>();
    // Средняя цена — как у ядра: по УДАЧНЫМ вызовам (таймауты и ошибки стоят $0 и не в счёт);
    // расход до окна в 30 дней (USAGE_BEFORE) — удачные вызовы
    let okCalls = USAGE_BEFORE.calls;
    let okCost = USAGE_BEFORE.cost_usd;
    for (const r of this.usage) {
      if (r.ok) {
        okCalls += 1;
        okCost += r.cost;
      }
      const k = dateKey(new Date(r.ts * 1000));
      const d = byDay.get(k);
      add(tot.total, r);
      if (!d) continue;
      add(d, r);
      add(tot.month, r);
      if (week.has(k)) add(tot.week, r);
      if (k === today) add(tot.today, r);
      const kk = kinds.get(r.kind) ?? { calls: 0, cost_usd: 0 };
      kk.calls += 1;
      kk.cost_usd += r.cost;
      kinds.set(r.kind, kk);
      const mm = models.get(r.model) ?? { calls: 0, cost_usd: 0 };
      mm.calls += 1;
      mm.cost_usd += r.cost;
      models.set(r.model, mm);
    }
    return {
      ...tot,
      avg_cost_per_call: okCalls ? okCost / okCalls : 0,
      daily: keys.map((date) => ({ date, ...(byDay.get(date) ?? zero()) })),
      by_kind: [...kinds.entries()].map(([kind, v]) => ({ kind, ...v })),
      by_model: [...models.entries()].map(([model, v]) => ({ model, ...v })),
    };
  }

  /** Очередь ключей, как keys.chain в ядре (без пауз после ошибок). */
  private chain(provider: string = this.settings.ai_provider): AiKeyEntry[] {
    if (provider !== "keys" && provider !== "openrouter" && provider !== "anthropic") return [];
    return this.settings.ai_keys.filter((e) => e.enabled && (provider === "keys" || e.provider === provider));
  }

  private keyTitle(e: AiKeyEntry): string {
    if (e.label) return e.label;
    const name = e.provider === "openrouter" ? "OpenRouter" : "Anthropic";
    const same = this.settings.ai_keys.filter((x) => x.provider === e.provider);
    return same.length <= 1 ? name : `${name} #${same.findIndex((x) => x.id === e.id) + 1}`;
  }

  private keyItem(e: AiKeyEntry): AiKeyItem {
    const secret = this.secrets[e.id] ?? null;
    const spent = Math.round((this.keySpent[e.id] ?? 0) * 10_000) / 10_000;
    return {
      ...structuredClone(e),
      title: this.keyTitle(e),
      has_key: Boolean(secret),
      masked: secret,
      spent_usd: spent,
      calls: Math.round(spent * 900),
      spent_since_usd: e.balance_at !== null ? spent : null,
      paused_until: null,
      paused_reason: null,
    };
  }

  private keyInfo(id: string, cmd: string): AiKeyInfo {
    const e = this.settings.ai_keys.find((x) => x.id === id);
    if (!e) throw new BackendError(this.tx.keyUnknown, cmd);
    const item = this.keyItem(e);
    const base = {
      id,
      provider: e.provider,
      spent_usd: item.spent_usd,
      calls: item.calls,
      spent_since_usd: item.spent_since_usd,
      checked_at: Math.floor(Date.now() / 1000),
    };
    if (!item.has_key) {
      return { ...base, valid: false, balance_usd: null, balance_source: null, usage_usd: null, limit_usd: null, checked_at: null, error: this.tx.noKey };
    }
    if (e.provider === "openrouter") {
      return {
        ...base,
        valid: true,
        balance_usd: Math.round(((this.opts.lowBalance ? 0.62 : BALANCE_START) - this.balanceSpent) * 10_000) / 10_000,
        balance_source: "api",
        usage_usd: Math.round((BALANCE_USED + this.balanceSpent) * 10_000) / 10_000,
        limit_usd: null,
        error: null,
      };
    }
    if (e.balance_usd !== null) {
      return {
        ...base,
        valid: true,
        balance_usd: Math.max(0, Math.round((e.balance_usd - item.spent_usd) * 10_000) / 10_000),
        balance_source: "estimate",
        usage_usd: null,
        limit_usd: null,
        error: null,
      };
    }
    return {
      ...base,
      valid: true,
      balance_usd: null,
      balance_source: null,
      usage_usd: null,
      limit_usd: null,
      error: fill(this.tx.balanceNoApi, { name: "Anthropic" }),
    };
  }

  private aiBalance(force: boolean): AiBalance {
    const hit = this.balanceCache;
    if (!force && hit && Date.now() - hit.at < BALANCE_CACHE_MS) return structuredClone(hit.value);
    const first = (this.settings.ai_enabled ? this.chain() : [])[0] ?? this.chain(this.settings.command_provider)[0];
    let value: AiBalance;
    if (!first) {
      value = { balance_usd: null, usage_usd: null, limit_usd: null, checked_at: null, error: this.tx.noKeys, key_id: null, provider: null, label: null, estimate: false };
    } else {
      const info = this.keyInfo(first.id, "ai_balance");
      value = {
        balance_usd: info.balance_usd,
        usage_usd: info.usage_usd,
        limit_usd: info.limit_usd,
        checked_at: info.checked_at,
        error: info.balance_usd !== null ? null : info.error,
        key_id: first.id,
        provider: first.provider,
        label: this.keyTitle(first),
        estimate: info.balance_source === "estimate",
      };
    }
    this.balanceCache = { at: Date.now(), value };
    return structuredClone(value);
  }

  private ensureLegacyKey(provider: KeyProvider): void {
    if (this.settings.ai_keys.some((e) => e.id === provider)) return;
    this.applySettings({
      ai_keys: [...this.settings.ai_keys, { id: provider, provider, label: "", enabled: true, balance_usd: null, balance_at: null }],
    });
  }

  // ——— Правила по названию окна ———

  private checkRule(r: TitleRule, cmd: string): TitleRule {
    const process = String(r.process ?? "").trim().toLowerCase();
    const title = String(r.title ?? "").trim();
    if (!process) throw new BackendError(this.tx.titleProcess, cmd);
    if (title.length < 2) throw new BackendError(this.tx.titleShort, cmd);
    if (r.style !== null && !DATA.ru.styles.some((st) => st.id === r.style)) {
      throw new BackendError(fill(this.tx.unknownStyle, { style: String(r.style) }), cmd);
    }
    if (r.insert_mode !== null && !["auto", "type", "clipboard"].includes(r.insert_mode)) {
      throw new BackendError(fill(this.tx.badMode, { mode: String(r.insert_mode) }), cmd);
    }
    return { process, title, insert_mode: r.insert_mode, style: r.style };
  }

  // ——— Частые слова ———

  private vocabList(process: string | undefined, limit: number): VocabItem[] {
    const p = process?.trim().toLowerCase();
    const rows = (this.data.vocab as unknown as VocabWord[]).map((w) => ({ word: w.word, count: p ? (w.apps[p] ?? 0) : w.count }))
      .filter((w) => w.count > 0)
      .sort((a, b) => b.count - a.count);
    return rows.slice(0, limit).map((w) => ({ ...w, excluded: this.vocabExcluded.has(w.word.toLowerCase()) }));
  }

  // ——— Перевод встречи ———

  private get meetingData(): {
    sources: MeetingSourceData[];
    lines: MeetingPhrase[];
    suggest: MeetingSuggestData;
    title: string;
  } {
    return this.data.meeting as unknown as {
      title: string;
      sources: MeetingSourceData[];
      lines: MeetingPhrase[];
      suggest: MeetingSuggestData;
    };
  }

  /**
   * Как list_sources ядра: встречи, потом играющие, потом по имени; в конце — весь звук. В
   * ?win10=1 звук программ захватить нельзя (available=false).
   */
  private meetingSources(): AudioSource[] {
    const procs: AudioSource[] = this.meetingData.sources.map((s) => ({
      ...s,
      id: `proc:${s.pid}`,
      kind: "process",
      available: !this.opts.win10,
    }));
    const system: AudioSource = {
      id: "system",
      kind: "system",
      pid: null,
      exe: "",
      name: this.tx.meetingSystem,
      title: "",
      meeting: false,
      active: true,
      available: true,
    };
    const rank = (s: AudioSource) => (s.meeting ? 0 : s.active ? 1 : 2);
    return [...procs.sort((a, b) => rank(a) - rank(b) || a.name.localeCompare(b.name)), system];
  }

  private setMeeting(patch: Partial<MeetingStatus>): void {
    this.state = { ...this.state, meeting: { ...this.state.meeting, ...patch } };
    this.hub.emit("meeting", structuredClone(this.state.meeting));
  }

  /** Перевод нужен: включён и фраза не на языке перевода. */
  private needsTranslation(lang: string): boolean {
    return this.settings.meeting_translate && lang !== this.settings.meeting_target_lang;
  }

  private meetingStart(sourceId: string | undefined, cmd: string, backdateMs = 0): MeetingStatus {
    const sources = this.meetingSources();
    const id = sourceId && sourceId !== "auto" ? sourceId : undefined;
    let src = id
      ? sources.find((s) => s.id === id)
      : (sources.find((s) => s.meeting) ?? sources.find((s) => s.kind === "process" && s.active));
    if (!src) {
      throw new BackendError(id ? fill(this.tx.meetingUnknownSource, { id }) : this.tx.meetingNoSource, cmd);
    }
    // звук программы захватить нельзя (?win10=1) — как ядро: весь звук компьютера и один раз
    // за запуск — уведомление
    let fallback = false;
    if (src.kind === "process" && src.available === false) {
      src = sources.find((s) => s.kind === "system") ?? src;
      fallback = true;
    }
    if (this.meetingRun && this.state.meeting.source?.id === src.id) return structuredClone(this.state.meeting);
    this.meetingHalt();
    this.notesHalt();
    this.meetingNotes = null;
    this.notesBody = null;
    const startedMs = Date.now() - backdateMs;
    const title = fallbackTitle(src, this.tx.meetingTitleGeneric);
    this.meetingLines = [];
    this.meetingSuggest = null;
    this.meetingRun = {
      lineTimer: null,
      levelTimer: setInterval(() => this.meetingLevelTick(), 250),
      streamTimers: new Set(),
      next: 0,
      startedMs,
      gpuUntil: 0,
      levelPhase: Math.random() * 10,
      lat: { asr: [], first: [], done: [] },
      finals: 0,
      titled: false,
    };
    this.setMeeting({
      running: true,
      source: src,
      started_at: localIso(startedMs),
      lines: 0,
      level: 0,
      state: "listening",
      error: null,
      translate: this.settings.meeting_translate,
      target_lang: this.settings.meeting_target_lang,
      file: this.meetingFile(startedMs, title),
      title,
      latency: { asr_ms: null, first_token_ms: null, done_ms: null },
      hint: null,
      ending_in_s: null,
      notes: null,
    });
    this.scheduleMeetingLine(1200);
    if (fallback && !this.loopbackFallbackTold) {
      this.loopbackFallbackTold = true;
      // info без toast — как notice ядра по умолчанию
      this.hub.emit("notice", { text: this.tx.meetingLoopbackFallback, level: "info", action: null, toast: false, data: null });
    }
    return structuredClone(this.state.meeting);
  }

  /** Файл записи, как у ядра: «<папка>\YYYY-MM-DD HH-MM — <название>.md» (meeting_save). */
  private meetingFile(startedMs: number, title: string): string | null {
    if (!this.settings.meeting_save) return null;
    const stamp = `${dateKey(new Date(startedMs))} ${clockOf(startedMs).slice(0, 5).replace(":", "-")}`;
    return `${this.settings.meeting_dir || MEETING_DIR}\\${stamp} — ${safeFilePart(title)}.md`;
  }

  /**
   * Название по содержанию (meeting_ai_title), как у ядра: когда готовых строк 8 и с начала
   * прошло 90 с, а нет — после «Стоп», если строк хотя бы 3. «Ответ ИИ» — через 0,7 с: расход
   * вида title, новое название и имя файла (событие meeting).
   */
  private meetingTitleTick(run: MeetingRun, final: boolean): void {
    if (!this.settings.meeting_ai_title || run.titled) return;
    const early = run.finals >= 8 && Date.now() - run.startedMs >= 90_000;
    if (!early && !(final && run.finals >= 3)) return;
    run.titled = true;
    const startedAt = this.state.meeting.started_at;
    const title = this.meetingData.title;
    setTimeout(() => {
      if (this.state.meeting.started_at !== startedAt) return;
      this.recordUsage("title", 9);
      if (title === this.state.meeting.title) return;
      this.setMeeting({ title, file: this.state.meeting.file ? this.meetingFile(run.startedMs, title) : null });
    }, 700);
  }

  /** Остановить таймеры сеанса (строки остаются — их можно скопировать и после остановки). */
  private meetingHalt(): void {
    const run = this.meetingRun;
    if (!run) return;
    if (run.lineTimer) clearTimeout(run.lineTimer);
    clearInterval(run.levelTimer);
    for (const tm of run.streamTimers) clearTimeout(tm);
    run.streamTimers.clear();
    this.meetingRun = null;
  }

  private meetingStop(): MeetingStatus {
    const run = this.meetingRun;
    // заметки сами (их название — последнее название записи), иначе — последнее название отдельно
    const notesDue = Boolean(run) && this.settings.meeting_notes && (run?.finals ?? 0) >= NOTES_MIN_LINES;
    if (run && !notesDue) this.meetingTitleTick(run, true);
    this.meetingHalt();
    // недоговорённая фраза — как у ядра после «Стоп»: дораспознана, хвост подтверждён
    for (const l of this.meetingLines) {
      if (l.final) continue;
      const done: MeetingLine = { ...l, text: [l.text, l.partial].filter(Boolean).join(" "), partial: "", final: true };
      if (l.status === "pending" || l.status === "streaming") {
        if (l.translation) done.status = "done";
        else Object.assign(done, { status: "error", error: this.meetingLineError() });
      }
      this.meetingLines = this.meetingLines.map((x) => (x.id === l.id ? done : x));
      this.hub.emit("meeting_line", structuredClone(done));
    }
    this.meetingEndingStop();
    this.setMeeting({ running: false, state: "idle", level: 0, error: null, hint: null, ending_in_s: null });
    if (notesDue) this.startNotes(true);
    return structuredClone(this.state.meeting);
  }

  // ——— Заметки к встрече ———

  /** Заметки демо-встречи: время ключевых моментов — время её строк ({t3} — строка №3 сценария). */
  private notesMarkdown(): string {
    const md = (this.meetingData as unknown as { notes: string }).notes;
    const last = this.meetingLines[this.meetingLines.length - 1]?.ts ?? "00:00:00";
    return md.replace(/\{t(\d+)\}/g, (_m, n: string) => this.meetingLines[Number(n)]?.ts ?? last);
  }

  private setNotes(n: MeetingNotes | null): void {
    this.meetingNotes = n;
    if (n) this.hub.emit("meeting_notes", structuredClone(n));
    this.setMeeting({ notes: notesInfo(n) });
  }

  /** Заметки с «# <название записи>» (название могло смениться и после них — как у ядра). */
  private notesView(): MeetingNotes | null {
    const n = this.meetingNotes;
    if (!n) return null;
    if (n.status === "running" || this.notesBody === null) return structuredClone(n);
    return { ...n, markdown: `# ${this.state.meeting.title ?? ""}\n\n${this.notesBody}` };
  }

  private notesHalt(): void {
    for (const tm of this.notesRun?.timers ?? []) clearTimeout(tm);
    this.notesRun = null;
  }

  /**
   * «Модель пишет заметки»: текст по ходу кусками (NOTES_STEPS за NOTES_WRITE_MS), потом готовы:
   * их название — название записи (meeting_ai_title), расход вида notes. Нет ключа — ошибка
   * с текстом ядра (сами после «Стоп» — и уведомление). Уже пишутся — как есть.
   */
  private startNotes(auto: boolean): MeetingNotes {
    if (this.notesRun && this.meetingNotes?.status === "running") return structuredClone(this.meetingNotes);
    const startedAt = this.state.meeting.started_at;
    const good = this.notesBody !== null ? this.meetingNotes : null;
    const base: MeetingNotes = {
      status: "running",
      markdown: null,
      generated_at: null,
      model: null,
      cost_usd: null,
      error: null,
      auto,
    };
    this.setNotes(base);
    const run: NotesRun = { timers: [], startedAt };
    this.notesRun = run;
    const alive = () => this.notesRun === run && this.state.meeting.started_at === startedAt;
    // как ai.meeting_provider в ядре: «по списку ключей» — провайдер первого ключа
    const first = this.chain()[0];
    const provider = this.settings.ai_provider === "keys" ? (first?.provider ?? "openrouter") : this.settings.ai_provider;
    if (provider !== "ollama" && !this.chain(provider).some((e) => this.secrets[e.id])) {
      const error = fill(this.tx.aiNoKey, { name: provider === "anthropic" ? "Anthropic" : "OpenRouter" });
      run.timers.push(
        setTimeout(() => {
          if (!alive()) return;
          this.notesRun = null;
          this.setNotes({
            ...base,
            status: "error",
            generated_at: good?.generated_at ?? null,
            model: good?.model ?? null,
            cost_usd: good?.cost_usd ?? null,
            error,
          });
          if (auto) {
            this.hub.emit("notice", {
              text: fill(this.tx.meetingNotesFailed, { error: error.replace(/[ .]+$/, "") }),
              level: "warn",
              action: "open_settings_ai",
              toast: true,
              data: null,
            });
          }
        }, 500),
      );
      return structuredClone(base);
    }
    const md = this.notesMarkdown();
    for (let i = 1; i <= NOTES_STEPS; i++) {
      run.timers.push(
        setTimeout(
          () => {
            if (!alive() || !this.meetingNotes) return;
            const part = md.slice(0, Math.round((md.length * i) / NOTES_STEPS));
            this.meetingNotes = { ...this.meetingNotes, markdown: part };
            this.hub.emit("meeting_notes", structuredClone(this.meetingNotes));
          },
          600 + ((NOTES_WRITE_MS - 600) * i) / NOTES_STEPS,
        ),
      );
    }
    run.timers.push(
      setTimeout(() => {
        if (!alive()) return;
        this.notesRun = null;
        const [first = "", ...rest] = md.split("\n");
        const title = first.replace(/^#\s*/, "").trim();
        this.notesBody = rest.join("\n").replace(/^\n+/, "");
        if (title && this.settings.meeting_ai_title && title !== this.state.meeting.title) {
          const startedMs = this.meetingRun?.startedMs ?? (Date.parse(this.state.meeting.started_at ?? "") || Date.now());
          this.setMeeting({ title, file: this.state.meeting.file ? this.meetingFile(startedMs, title) : null });
        }
        this.recordUsage("notes", countWords(this.meetingLines.map((l) => l.translation ?? l.text).join(" ")));
        const row = this.usage[this.usage.length - 1];
        const done: MeetingNotes = {
          status: "done",
          markdown: null,
          generated_at: new Date().toISOString(),
          model: notesModelId(this.settings),
          cost_usd: row ? Math.round(row.cost * 1e6) / 1e6 : null,
          error: null,
          auto,
        };
        this.meetingNotes = done;
        const view = this.notesView();
        if (view) this.hub.emit("meeting_notes", view);
        this.setMeeting({ notes: notesInfo(done) });
      }, NOTES_WRITE_MS + 200),
    );
    return structuredClone(base);
  }

  /** Уровень звука 4 раза в секунду: речь фразами с паузами; «диктовка» — ждём видеокарту. */
  private meetingLevelTick(): void {
    const run = this.meetingRun;
    if (!run) return;
    run.levelPhase += 0.25;
    const t = run.levelPhase;
    const speaking = Math.sin(t * 0.9) + Math.sin(t * 0.37) > -0.7;
    const level = speaking
      ? Math.min(1, 0.28 + Math.abs(Math.sin(t * 5.3)) * 0.45 + Math.random() * 0.15)
      : Math.random() * 0.04;
    const waiting = this.busy || Date.now() < run.gpuUntil;
    this.setMeeting({ level: Math.round(level * 100) / 100, state: waiting ? "waiting_gpu" : "listening" });
  }

  /** Следующая фраза — через ms; она звучит, сколько звучит, потом пауза 0,7–1,5 с. */
  private scheduleMeetingLine(ms: number): void {
    const run = this.meetingRun;
    if (!run) return;
    run.lineTimer = setTimeout(() => {
      const speakMs = this.streamMeetingLine(Date.now());
      // раз в ~15 фраз — «диктовка» на 3 с: распознавание встречи ждёт видеокарту
      if (run.next % 15 === 9) run.gpuUntil = Date.now() + 3000;
      this.scheduleMeetingLine(speakMs + 700 + Math.random() * 800);
    }, ms);
  }

  /** Перевод «не успел» — текст ошибки ядра. */
  private meetingLineError(): string {
    return fill(this.tx.meetingTranslateError, { seconds: this.settings.translate_timeout_s });
  }

  /**
   * Фраза по ходу речи, как у ядра. Распознавание (LocalAgreement-2): проход раз в STEP_MS по
   * уже сказанному — гипотеза; подтверждено (text) то, в чём сошлись два прохода подряд,
   * остальное — хвост (partial). Пауза после фразы — финальный проход: text из сценария
   * (heard исправлен), final. Перевод — кусками по мере подтверждения (≥ CHUNK_WORDS слов или
   * конец фразы): первые слова через ~0,5 с после запроса, дальше по 2 слова раз в 100 мс
   * (status streaming), между кусками — pending, всё — done. draft — пословный перевод; если
   * meeting_refine и фраза длиннее 6 слов, его заменяет перевод целиком (refined). Обновления
   * одной строки — не чаще раза в LINE_UPDATE_MS. Возвращает, сколько мс звучит фраза.
   */
  private streamMeetingLine(atMs: number): number {
    const run = this.meetingRun;
    const script = this.meetingData.lines;
    if (!run || !script.length) return 0;
    const p = script[run.next % script.length];
    run.next++;
    if (!p) return 0;
    const id = (this.meetingLines[this.meetingLines.length - 1]?.id ?? 0) + 1;
    const heard = (p.heard ?? p.text).split(/\s+/).filter(Boolean);
    const n = heard.length;
    const speakMs = n * WORD_MS;
    const translate = this.needsTranslation(p.lang);
    const events: { at: number; patch: Partial<MeetingLine> }[] = [];

    // распознавание по ходу речи
    let prevHyp = 0;
    let committed = 0;
    let asrAt: number | null = null;
    const commits: { at: number; words: number }[] = [];
    for (let at = STEP_MS; at < speakMs + VAD_END_MS; at += STEP_MS) {
      const hyp = Math.min(n, Math.floor(Math.max(0, at - DECODE_MS) / WORD_MS));
      const c = Math.max(committed, Math.min(prevHyp, hyp));
      prevHyp = hyp;
      if (c > committed) commits.push({ at, words: c });
      committed = c;
      if (hyp === n && asrAt === null) asrAt = at;
      if (hyp > 0) events.push({ at, patch: { text: heard.slice(0, c).join(" "), partial: heard.slice(c, hyp).join(" ") } });
    }
    const finalAt = speakMs + VAD_END_MS + FINAL_DECODE_MS;
    events.push({ at: finalAt, patch: { text: p.text, partial: "", final: true } });
    commits.push({ at: finalAt, words: n });
    run.lat.asr.push((asrAt ?? finalAt) - speakMs);

    // перевод кусками
    let doneAt = finalAt;
    if (translate && p.error) {
      events.push({ at: finalAt + 1400, patch: { status: "error", error: this.meetingLineError() } });
    } else if (translate) {
      const draft = (p.draft ?? p.translation ?? p.text).split(/\s+/).filter(Boolean);
      const m = draft.length;
      let srcDone = 0;
      let dstDone = 0;
      let free = 0;
      let chunks = 0;
      let lastFirst = finalAt;
      for (const c of commits) {
        const last = c.words >= n;
        // кусок — на границе фразы или предложения (знак препинания) или с CHUNK_WORDS слов
        const boundary = /[.?!;:,—]$/.test(heard[c.words - 1] ?? "");
        if (!last && !boundary && c.words - srcDone < CHUNK_WORDS) continue;
        const to = last ? m : Math.min(m - 1, Math.round((m * c.words) / n));
        srcDone = c.words;
        if (to <= dstDone) continue;
        let t = Math.max(c.at, free) + TTFT_MS + Math.random() * 250;
        lastFirst = t;
        chunks++;
        for (let w = dstDone + 2; ; w += 2) {
          const k = Math.min(w, to);
          events.push({ at: t, patch: { translation: draft.slice(0, k).join(" "), status: "streaming" } });
          if (k >= to) break;
          t += LINE_UPDATE_MS;
        }
        dstDone = to;
        free = t + 60;
        if (!last) events.push({ at: free, patch: { status: "pending" } });
      }
      doneAt = Math.max(free, finalAt);
      events.push({ at: doneAt, patch: { status: "done" } });
      run.lat.first.push(lastFirst - speakMs);
      run.lat.done.push(doneAt - speakMs);
      if (this.settings.meeting_refine && p.draft && n > 6 && chunks > 1) {
        events.push({ at: doneAt + 500 + Math.random() * 400, patch: { translation: p.translation ?? p.text, refined: true } });
      }
    }

    // не чаще раза в LINE_UPDATE_MS: близкие обновления — одним
    events.sort((a, b) => a.at - b.at);
    const groups: { at: number; patch: Partial<MeetingLine> }[] = [];
    for (const e of events) {
      const g = groups[groups.length - 1];
      if (g && e.at - g.at < LINE_UPDATE_MS) g.patch = { ...g.patch, ...e.patch };
      else groups.push({ at: e.at, patch: { ...e.patch } });
    }

    const base: MeetingLine = {
      id,
      t: Math.round((atMs - run.startedMs) / 100) / 10,
      ts: clockOf(atMs),
      lang: p.lang,
      text: "",
      partial: "",
      final: false,
      refined: false,
      translation: null,
      status: translate ? "pending" : "original",
    };
    for (const g of groups) {
      const tm = setTimeout(() => {
        run.streamTimers.delete(tm);
        if (this.meetingRun !== run) return;
        const cur = this.meetingLines.find((l) => l.id === id);
        const next: MeetingLine = { ...(cur ?? base), ...g.patch };
        if (cur) {
          this.meetingLines = this.meetingLines.map((l) => (l.id === id ? next : l));
        } else {
          this.meetingLines = [...this.meetingLines, next].slice(-MEETING_LINES_MAX);
        }
        this.hub.emit("meeting_line", structuredClone(next));
        if (!cur) this.setMeeting({ lines: this.meetingLines.length });
        if (g.patch.status === "done" || g.patch.refined) {
          this.recordUsage("translate", countWords(p.text));
        }
        if (g.patch.final && !cur?.final) {
          run.finals++;
          this.meetingTitleTick(run, false);
        }
        if (g.patch.final || g.patch.status === "done") {
          this.setMeeting({
            latency: { asr_ms: avgMs(run.lat.asr), first_token_ms: avgMs(run.lat.first), done_ms: avgMs(run.lat.done) },
          });
        }
      }, g.at);
      run.streamTimers.add(tm);
    }
    return speakMs;
  }

  /**
   * Уже прозвучавшая фраза (начало демо-сеанса): подтверждена, переведена (длинная с draft —
   * уточнена), одна — с ошибкой перевода.
   */
  private emitMeetingLineDone(atMs: number): void {
    const run = this.meetingRun;
    const script = this.meetingData.lines;
    if (!run || !script.length) return;
    const p = script[run.next % script.length];
    run.next++;
    if (!p) return;
    const id = (this.meetingLines[this.meetingLines.length - 1]?.id ?? 0) + 1;
    const translate = this.needsTranslation(p.lang);
    const refined = translate && !p.error && this.settings.meeting_refine && Boolean(p.draft) && countWords(p.text) > 6;
    const line: MeetingLine = {
      id,
      t: Math.round((atMs - run.startedMs) / 100) / 10,
      ts: clockOf(atMs),
      lang: p.lang,
      text: p.text,
      partial: "",
      final: true,
      refined,
      translation: translate && !p.error ? (p.translation ?? p.text) : null,
      status: !translate ? "original" : p.error ? "error" : "done",
      ...(translate && p.error ? { error: this.meetingLineError() } : {}),
    };
    this.meetingLines = [...this.meetingLines, line].slice(-MEETING_LINES_MAX);
    this.hub.emit("meeting_line", structuredClone(line));
    run.finals++;
    this.meetingTitleTick(run, false);
    // задержки «прошлых» фраз — как у живых
    run.lat.asr.push(650 + Math.round(Math.random() * 300));
    if (translate && !p.error) {
      run.lat.first.push(1350 + Math.round(Math.random() * 400));
      run.lat.done.push(2050 + Math.round(Math.random() * 600));
    }
    this.setMeeting({
      lines: this.meetingLines.length,
      latency: { asr_ms: avgMs(run.lat.asr), first_token_ms: avgMs(run.lat.first), done_ms: avgMs(run.lat.done) },
    });
  }

  /** Демо окна перевода: сеанс идёт уже пару минут, первые фразы уже есть. */
  private meetingDemo(): void {
    try {
      this.meetingStart(undefined, "meeting_start", 130_000);
    } catch {
      return;
    }
    const start = this.meetingRun?.startedMs ?? Date.now();
    // ?scene=meeting_notes — прозвучала вся встреча, «Стоп»: заметки пишутся сами
    const all = this.opts.scene === "meeting_notes";
    const n = all ? this.meetingData.lines.length : 7;
    for (let i = 0; i < n; i++) this.emitMeetingLineDone(start + 6000 + i * (all ? 7_500 : 17_000));
    if (all) {
      setTimeout(() => {
        if (this.meetingRun) this.meetingStop();
      }, 700);
      return;
    }
    // ?scene=meeting_ending — автоостановка: отсчёт 10 с, потом «Стоп» и уведомление
    if (this.opts.scene === "meeting_ending") {
      const run = this.meetingRun;
      setTimeout(() => {
        if (this.meetingRun !== run || !run) return;
        if (run.lineTimer) clearTimeout(run.lineTimer);
        run.lineTimer = null;
        this.meetingEndingStart(10);
      }, 2500);
    }
    // ?scene=meeting_ended — встреча «закончилась»: микрофон свободен, звука нет (MeetingStatus.hint)
    if (this.opts.scene === "meeting_ended") {
      const run = this.meetingRun;
      setTimeout(() => {
        if (this.meetingRun !== run || !run) return;
        if (run.lineTimer) clearTimeout(run.lineTimer);
        run.lineTimer = null;
        this.setMeeting({ hint: "ended" });
      }, 2500);
    }
  }

  /** Отсчёт автоостановки (как MeetingSession.autostop_tick): статус раз в секунду, на нуле — «Стоп». */
  private meetingEndingTimer: ReturnType<typeof setInterval> | null = null;
  private meetingEndingStart(seconds: number): void {
    this.meetingEndingStop();
    const until = Date.now() + seconds * 1000;
    const tick = () => {
      const left = Math.max(0, Math.ceil((until - Date.now()) / 1000));
      if (left > 0) {
        this.setMeeting({ hint: "ending", ending_in_s: left });
        return;
      }
      this.meetingEndingStop();
      const run = this.meetingRun;
      const notes = Boolean(run) && this.settings.meeting_notes && (run?.finals ?? 0) >= NOTES_MIN_LINES;
      this.meetingStop();
      this.hub.emit("notice", {
        text: notes ? this.tx.meetingAutostoppedNotes : this.tx.meetingAutostopped,
        level: "info",
        action: null,
        toast: true,
        data: null,
      });
    };
    tick();
    this.meetingEndingTimer = setInterval(tick, 1000);
  }

  private meetingEndingStop(): void {
    if (this.meetingEndingTimer) clearInterval(this.meetingEndingTimer);
    this.meetingEndingTimer = null;
  }

  /** Демо «ядро заметило встречу» (?suggest=1): Brave держит микрофон и играет звук. */
  private emitMeetingSuggest(): void {
    if (this.meetingRun || !this.settings.meeting_autosuggest) return;
    const d = this.meetingData.suggest;
    const source = this.meetingSources().find((s) => s.pid === d.pid);
    if (!source) return;
    if (this.settings.meeting_ignore_apps.some((x) => x.toLowerCase() === source.exe.toLowerCase())) return;
    const now = Date.now();
    this.meetingSuggest = { id: `sg-${now.toString(36)}`, source, app: d.app, title: d.title, detected_at: localIso(now) };
    this.hub.emit("meeting_suggest", structuredClone(this.meetingSuggest));
  }

  /**
   * Ответ на предложение, как у ядра: start — перевод начинается с источником предложения
   * (MeetingStatus); later — до конца разговора не предлагать (null); never — exe в
   * meeting_ignore_apps (null). Предложение неизвестно (уже ответили, ядро перезапускалось) —
   * ошибка «уже неактуально», для later — просто null.
   */
  private meetingSuggestAnswer(
    a: CommandArgs<"meeting_suggest_answer">,
    cmd: string,
  ): MeetingStatus | null | Promise<MeetingStatus> {
    if (!["start", "later", "never"].includes(a.answer)) throw new BackendError(this.tx.suggestAnswer, cmd);
    const sg = this.meetingSuggest;
    if (!sg || sg.id !== a.id) {
      if (a.answer === "later") return null;
      throw new BackendError(this.tx.meetingSuggestExpired, cmd);
    }
    this.meetingSuggest = null;
    if (a.answer === "start") return sleep(350).then(() => this.meetingStart(sg.source.id, cmd));
    if (a.answer === "never" && sg.source.exe) {
      const exe = normalizeExe(sg.source.exe);
      if (!this.settings.meeting_ignore_apps.includes(exe)) {
        this.applySettings({ meeting_ignore_apps: [...this.settings.meeting_ignore_apps, exe] });
      }
    }
    return null;
  }

  private async pillDemo(): Promise<void> {
    // Сценарии — заново на каждом шаге: тексты уведомлений на текущем языке интерфейса
    const scenarios = (t: MockData["texts"]): Scenario[] => {
      const n = coreNotices(t);
      return [
        { kind: "dictation", dur: 3400, ai: false },
        { kind: "learn" },
        { kind: "dictation", dur: 4200, ai: true, hands_free: true },
        { kind: "dictation", dur: 3000, broadcast: true, broadcastAtStart: true },
        { kind: "notice", notice: n.admin },
        // подсказку о встрече ядро больше не шлёт (её заменило meeting_suggest) — только ?scene=notice_meeting
        { kind: "dictation", dur: 2400, ai: true, command: true },
        { kind: "notice", notice: n.ai },
        { kind: "notice", notice: n.insert },
      ];
    };
    await sleep(1800);
    for (let i = 0; ; i++) {
      const all = scenarios(this.tx);
      const sc = all[i % all.length];
      if (sc) await this.runScenario(sc);
      await sleep(2400);
    }
  }

  private initialDict(): DictItem[] {
    return this.data.dictionary.map((d) => ({ id: this.nextId++, term: d.term, variants: [...d.variants] }));
  }

  /**
   * Демо-журнал обучения: будто последние варианты нескольких терминов словарь выучил сам
   * или вы подтвердили их кнопкой «Добавить» (раздел «Недавно добавлено» на странице «Словарь»).
   */
  private initialJournal(): LearnJournalEntry[] {
    const now = Date.now() / 1000;
    const plan: [term: string, created: boolean, source: LearnSource, agoSec: number][] = [
      ["S3", true, "auto", 26 * 3600],
      ["Airflow", false, "accepted", 3 * 3600 + 600],
      ["Metabase", false, "auto", 7 * 60],
    ];
    const out: LearnJournalEntry[] = [];
    for (const [term, created, source, ago] of plan) {
      const d = this.dict.find((x) => x.term === term);
      const variant = d?.variants[d.variants.length - 1];
      if (d && variant) out.push({ id: d.id, term: d.term, variant, created, ts: now - ago, source });
    }
    return out;
  }

  private journalPush(e: LearnJournalEntry): void {
    this.journal = [...this.journal, e].slice(-JOURNAL_MAX);
  }

  /** Как _journal_drop ядра: термин удалён вручную (id=null — весь словарь) — его правки не откатываются. */
  private journalDrop(id: number | null): void {
    this.journal = id === null ? [] : this.journal.filter((e) => e.id !== id);
  }

  /** Убрать из журнала запись отменяемой правки (при нескольких — с этим id, иначе последнюю). */
  private journalPop(term: string, variant: string, created: boolean, id: number | null): void {
    const tm = term.trim().toLowerCase();
    let v = variant.trim().toLowerCase();
    if (v === tm) v = "";
    const match = this.journal
      .map((e, i) => ({ e, i }))
      .filter(({ e }) => e.term.toLowerCase() === tm && e.variant.toLowerCase() === v && e.created === created);
    const exact = match.filter(({ e }) => id !== null && e.id === id);
    const hit = (exact.length ? exact : match).at(-1);
    if (hit) this.journal = this.journal.filter((_, i) => i !== hit.i);
  }

  private learnKnown(s: LearnSample): boolean {
    const d = this.dict.find((x) => x.term.toLowerCase() === s.term.toLowerCase());
    if (!d) return false;
    if (s.variant.toLowerCase() === s.term.toLowerCase()) return true;
    return d.variants.some((v) => v.toLowerCase() === s.variant.toLowerCase());
  }

  private learnText(kind: "auto" | "suggest", s: LearnSample): string {
    const caseOnly = s.variant.toLowerCase() === s.term.toLowerCase();
    const tx = this.tx;
    const tpl =
      kind === "auto"
        ? caseOnly
          ? tx.learnAddedTerm
          : tx.learnAdded
        : caseOnly
          ? tx.learnSuggestTerm
          : tx.learnSuggest;
    return fill(tpl, { term: s.term, variant: s.variant });
  }

  /**
   * Как dict_learn_accept ядра: слить с существующим термином без дублей. Изменение словаря
   * записывается в журнал обучения с источником source (auto — выучено само, accepted —
   * кнопка «Добавить»).
   */
  private learnAdd(term: string, variant: string, source: LearnSource): { item: DictItem; created: boolean } {
    const tm = term.trim();
    const v = variant.trim();
    if (!tm) throw new BackendError(this.tx.learnEmpty, "dict_learn_accept");
    const caseOnly = v.toLowerCase() === tm.toLowerCase();
    const ts = Date.now() / 1000;
    const existing = this.dict.find((d) => d.term.toLowerCase() === tm.toLowerCase());
    if (existing) {
      if (v && !caseOnly && !existing.variants.some((x) => x.toLowerCase() === v.toLowerCase())) {
        existing.variants.push(v);
        this.journalPush({ id: existing.id, term: existing.term, variant: v, created: false, ts, source });
      }
      return { item: structuredClone(existing), created: false };
    }
    const item: DictItem = { id: this.nextId++, term: tm, variants: v && !caseOnly ? [v] : [] };
    this.dict.push(item);
    this.journalPush({ id: item.id, term: tm, variant: caseOnly ? "" : v, created: true, ts, source });
    return { item: structuredClone(item), created: true };
  }

  /**
   * Пользователь исправил слово после вставки. auto — сразу в словарь + notice dict_undo;
   * suggest — сначала notice dict_add, та же правка второй раз — добавить самому (dict_undo).
   * force — всё из примеров уже выучено: вернуть словарь к исходному, чтобы демо шло дальше.
   */
  private emitLearn(force: boolean): void {
    const mode = this.settings.learn_mode;
    if (mode === "off" || this.state.paused) return;
    const pick = (): LearnSample | null => {
      for (let i = 0; i < (this.data.learnSamples as LearnSample[]).length; i++) {
        const idx = (this.learnIdx + i) % (this.data.learnSamples as LearnSample[]).length;
        const smp = (this.data.learnSamples as LearnSample[])[idx];
        if (smp && !this.learnKnown(smp)) {
          this.learnIdx = idx;
          return smp;
        }
      }
      return null;
    };
    let smp = pick();
    if (!smp && force) {
      this.dict = this.initialDict();
      this.journal = this.initialJournal();
      this.learnSeen.clear();
      smp = pick();
    }
    if (!smp) return;
    const key = `${smp.term.toLowerCase()}\u0001${smp.variant.toLowerCase()}`;
    const seen = (this.learnSeen.get(key) ?? 0) + 1;
    this.learnSeen.set(key, seen);
    if (mode === "suggest" && seen < 2) {
      // learnIdx не двигаем: следующая «диктовка» повторит ту же правку
      const existing = this.dict.find((d) => d.term.toLowerCase() === smp.term.toLowerCase());
      this.hub.emit("notice", {
        text: this.learnText("suggest", smp),
        level: "info",
        action: "dict_add",
        toast: true,
        data: { term: smp.term, variant: smp.variant, id: existing?.id ?? null, created: false },
      });
      return;
    }
    const r = this.learnAdd(smp.term, smp.variant, "auto");
    this.learnIdx++;
    this.hub.emit("notice", {
      text: this.learnText("auto", smp),
      level: "info",
      action: "dict_undo",
      toast: true,
      data: { term: r.item.term, variant: smp.variant, id: r.item.id, created: r.created },
    });
  }

  /** Папка, где «ядро» ищет модели сейчас. */
  private modelDir(): string {
    return this.settings.model_dir || HF_CACHE;
  }

  private modelReady(): boolean {
    return this.stored.get(dirKey(this.modelDir()))?.has(this.settings.model) ?? false;
  }

  private storeModel(dir: string, model: string, present: boolean): void {
    const set = this.stored.get(dirKey(dir)) ?? new Set<string>();
    if (present) set.add(model);
    else set.delete(model);
    this.stored.set(dirKey(dir), set);
  }

  /** Как валидатор ядра: строка, полный путь (буква диска или UNC), без «\» в конце. */
  private checkModelDir(raw: unknown, cmd: string): string {
    if (typeof raw !== "string") {
      throw new BackendError(fill(this.tx.badSetting, { key: "model_dir", value: String(raw) }), cmd);
    }
    let p = raw.trim().replace(/\//g, "\\");
    if (!p) return "";
    if (!/^[A-Za-z]:\\/.test(p) && !/^\\\\[^\\]+\\[^\\]+/.test(p)) {
      // как ядро: «Папка модели: нужен полный путь…»
      throw new BackendError(`${this.tx.modelDirLabel}: ${this.tx.modelDirRelative}`, cmd);
    }
    p = p.replace(/\\+$/, "");
    return /^[A-Za-z]:$/.test(p) ? `${p}\\` : p;
  }

  /** Модель с диска заново (ядро: задание reload) — прежняя работает до готовности новой. */
  private reloadModel(delayMs: number): void {
    this.state = { ...this.state, model_loaded: false };
    this.setState("loading");
    setTimeout(() => {
      this.state = {
        ...this.state,
        model_loaded: true,
        device: this.settings.device === "cpu" ? "cpu" : "cuda",
      };
      this.setState(this.state.paused ? "paused" : "idle");
    }, delayMs);
  }

  /**
   * Перенос модели в другую папку (model_dir_set move=true): копия с прогрессом, на том же
   * диске — мгновенно. По окончании, как у ядра, — новый model_dir (событие settings),
   * progress done, перезагрузка модели оттуда и notice. В папке с «full» в имени демо-ядру
   * «не хватает места» — ошибка переноса до начала копирования (ядро проверяет место заранее).
   */
  private moveModel(path: string, from: string, to: string): void {
    this.modelJob = "move";
    const model = this.settings.model;
    const total = MODEL_TABLE.find((m) => m.model === model)?.size_mb ?? 1620;
    const sameDisk = from.slice(0, 2).toLowerCase() === to.slice(0, 2).toLowerCase();
    const finish = () => {
      this.modelJob = null;
      this.storeModel(from, model, false);
      this.storeModel(to, model, true);
      this.applySettings({ model_dir: path });
      this.hub.emit("progress", { what: "model", pct: 100, text: this.tx.progressDone, done: true, error: null });
      this.hub.emit("notice", { text: fill(this.tx.modelMoved, { path: to }), level: "info", action: null, toast: true, data: null });
    };
    if (/full/i.test(to)) {
      setTimeout(() => {
        this.modelJob = null;
        const need = fmtGb(total + 64);
        const error = fill(this.tx.moveNoSpace, { model, need, free: fmtGb(total / 3), path: to });
        this.hub.emit("progress", { what: "model", pct: 0, text: error, done: true, error });
      }, 400);
      return;
    }
    if (sameDisk) {
      setTimeout(finish, 300);
      return;
    }
    let pct = 0;
    const timer = setInterval(() => {
      pct = Math.min(100, pct + 3 + Math.random() * 6);
      if (pct >= 100) {
        clearInterval(timer);
        finish();
        return;
      }
      this.hub.emit("progress", {
        what: "model",
        pct: Math.round(pct),
        text: fill(this.tx.progressMove, { model, done: Math.round((total * pct) / 100), total }),
        done: false,
        error: null,
      });
    }, 140);
  }

  private recommend(): ModelRecommend {
    const gpu = !this.opts.noGpu;
    const rec: ModelRecommend["recommended"] = gpu
      ? { model: "large-v3-turbo", device: "cuda", compute_type: "float16" }
      : { model: "small", device: "cpu", compute_type: "int8" };
    const threads = gpu ? 16 : 8;
    const ram = gpu ? 31.9 : 15.9;
    const tx = this.tx;
    const notes: Record<string, string> = {
      tiny: tx.noteTiny,
      base: tx.noteBase,
      small: tx.noteSmall,
      medium: tx.noteMedium,
      "large-v3-turbo": tx.noteTurbo,
      "large-v3": tx.noteLarge,
    };
    const lang = this.state.ui_language;
    return {
      gpu_name: gpu ? "NVIDIA GeForce RTX 3060 Ti" : null,
      vram_mb: gpu ? 8192 : null,
      cuda_ok: gpu,
      ram_gb: ram,
      cpu_threads: threads,
      recommended: rec,
      reason: gpu
        ? fill(this.tx.recommendGpu, { gpu: "RTX 3060 Ti", vram: 8 })
        : fill(tx.recommendCpu, {
            threads: fill(pluralForm(tx.recThreads, threads, lang), { n: threads }),
            ram: fill(tx.recRam, { gb: Math.round(ram) }),
          }),
      options: MODEL_TABLE.map((m) => ({ ...m, recommended: m.model === rec.model, note: notes[m.model] ?? "" })),
    };
  }

  private progress(what: "model" | "cuda"): void {
    if (what === "model") {
      // как ядро: слот «model» занят — вторая загрузка не начинается
      if (this.modelJob) return;
      this.modelJob = "download";
    }
    const model = this.settings.model;
    const dir = this.modelDir();
    const total = what === "model" ? (MODEL_TABLE.find((m) => m.model === model)?.size_mb ?? 1620) : 1180;
    const tpl = what === "model" ? this.tx.progressModel : this.tx.progressCuda;
    let pct = 0;
    const timer = setInterval(() => {
      pct = Math.min(100, pct + 2 + Math.random() * 5);
      const done = pct >= 100;
      this.hub.emit("progress", {
        what,
        pct: Math.round(pct),
        text: done
          ? this.tx.progressDone
          : fill(tpl, { done: Math.round((total * pct) / 100), total }),
        done,
        error: null,
      });
      if (done) {
        clearInterval(timer);
        if (what === "model") {
          this.modelJob = null;
          this.storeModel(dir, model, true);
          if (!this.state.model_loaded) {
            // модель скачана — ядро загружает её и становится готовым
            this.setState("loading");
            setTimeout(() => {
              this.state = { ...this.state, device: this.settings.device === "cpu" ? "cpu" : "cuda", model_loaded: true };
              this.setState(this.state.paused ? "paused" : "idle");
            }, 900);
          } else if (this.settings.first_run_done) {
            // скачана в другую папку, пока работала прежняя, — подменить её
            this.reloadModel(900);
          }
        } else this.cudaReady = true;
      }
    }, 140);
  }

  private cleanup(text: string, style: string): string {
    // пример из «Проверить на своём тексте» — на языке интерфейса, а он мог смениться
    const sample = [DATA.ru, DATA.en].map((d) => d.cleanupSample).find((x) => x.raw === text.trim());
    if (sample) return sample.text;
    if (style === "as_is") return text;
    let s = ` ${text.trim()} `;
    for (const f of [...DATA.ru.fillers, ...DATA.en.fillers]) {
      s = s.replace(new RegExp(`\\s${f}(?=[\\s,.!?])`, "giu"), " ");
    }
    for (const d of this.dict) {
      for (const v of d.variants) {
        s = s.replace(new RegExp(`(?<!\\p{L})${v}(?!\\p{L})`, "giu"), d.term);
      }
    }
    s = s.replace(/\s+/g, " ").trim();
    if (!s) return s;
    if (style === "messenger") return s.replace(/[.]$/, "");
    s = s.charAt(0).toUpperCase() + s.slice(1);
    if (!/[.!?…]$/.test(s)) s += ".";
    return s;
  }

  private periodSeconds(p: ClearPeriod): number {
    switch (p) {
      case "day":
        return DAY;
      case "week":
        return 7 * DAY;
      case "month":
        return 30 * DAY;
      case "all":
        return Number.POSITIVE_INFINITY;
    }
  }

  private applySettings(patch: SettingsPatch): Settings {
    const prev = this.settings;
    const next: Settings = { ...prev, ...patch };
    const dirChanged = dirKey(prev.model_dir || HF_CACHE) !== dirKey(next.model_dir || HF_CACHE);
    this.settings = next;
    this.hub.emit("settings", structuredClone(next));
    this.syncState();
    const modelChanged =
      patch.model !== undefined && patch.model !== prev.model ||
      patch.device !== undefined && patch.device !== prev.device ||
      patch.compute_type !== undefined && patch.compute_type !== prev.compute_type ||
      dirChanged;
    // Как ядро: модель на месте — перезагрузить, нет — до первого запуска ждать мастера
    // (setup_download), после — качать сюда, пока работает прежняя
    if (modelChanged && this.state.model_loaded) {
      if (this.modelReady()) this.reloadModel(1800);
      else if (next.first_run_done) this.progress("model");
    }
    return structuredClone(next);
  }

  private handle(cmd: CommandName, rawArgs: unknown): unknown {
    switch (cmd) {
      case "get_state":
        return this.snapshot();
      case "get_settings":
        return structuredClone(this.settings);
      case "set_settings": {
        const a = rawArgs as CommandArgs<"set_settings">;
        const bad = (key: string, value: unknown) =>
          new BackendError(fill(this.tx.badSetting, { key, value: String(value) }), cmd);
        if (a.patch.language !== undefined && !["system", "ru", "en"].includes(a.patch.language)) {
          throw bad("language", a.patch.language);
        }
        if (a.patch.dictation_language !== undefined && !["auto", "ru", "en"].includes(a.patch.dictation_language)) {
          throw bad("dictation_language", a.patch.dictation_language);
        }
        let p = a.patch;
        if (p.meeting_target_lang !== undefined && !MEETING_LANG_CODES.includes(p.meeting_target_lang)) {
          throw bad("meeting_target_lang", p.meeting_target_lang);
        }
        if (p.meeting_source_lang !== undefined && p.meeting_source_lang !== "auto" && !MEETING_LANG_CODES.includes(p.meeting_source_lang)) {
          throw bad("meeting_source_lang", p.meeting_source_lang);
        }
        if (p.meeting_font_size !== undefined && !(Number.isInteger(p.meeting_font_size) && p.meeting_font_size >= 12 && p.meeting_font_size <= 28)) {
          throw bad("meeting_font_size", p.meeting_font_size);
        }
        if (p.translate_timeout_s !== undefined && !(p.translate_timeout_s >= 1 && p.translate_timeout_s <= 30)) {
          throw bad("translate_timeout_s", p.translate_timeout_s);
        }
        // как ядро: имена exe (путь — до имени, нижний регистр, «.exe»), без повторов, ≤ 100 по ≤ 100 символов
        const ignore = p.meeting_ignore_apps;
        if (ignore !== undefined) {
          const names = Array.isArray(ignore) && ignore.every((x) => typeof x === "string") ? ignore.map(normalizeExe) : null;
          if (!names || names.some((x) => !x || x.length > 100) || new Set(names).size > 100) {
            throw bad("meeting_ignore_apps", JSON.stringify(ignore));
          }
          p = { ...p, meeting_ignore_apps: [...new Set(names)] };
        }
        const mp = p.meeting_mini_pos;
        if (mp && !(Array.isArray(mp) && mp.length === 2 && mp.every((v) => Number.isFinite(v) && Math.abs(v) < 1e6))) {
          throw bad("meeting_mini_pos", JSON.stringify(mp));
        }
        if (p.meeting_autostop !== undefined && typeof p.meeting_autostop !== "boolean") {
          throw bad("meeting_autostop", String(p.meeting_autostop));
        }
        const r = p.meeting_rect;
        if (r && !(r.length === 4 && r.every((v) => Number.isInteger(v) && Math.abs(v) < 1e6) && r[2] >= 200 && r[3] >= 200)) {
          throw bad("meeting_rect", JSON.stringify(r));
        }
        const next =
          p.model_dir !== undefined ? this.applySettings({ ...p, model_dir: this.checkModelDir(p.model_dir, cmd) }) : this.applySettings(p);
        if (p.meeting_translate !== undefined || p.meeting_target_lang !== undefined) {
          this.setMeeting({ translate: this.settings.meeting_translate, target_lang: this.settings.meeting_target_lang });
        }
        return next;
      }
      case "pause": {
        const a = rawArgs as CommandArgs<"pause">;
        this.state = { ...this.state, paused: a.on };
        this.setState(a.on ? "paused" : "idle");
        return this.snapshot();
      }
      case "quit":
        return null;
      case "list_devices":
        return this.data.devices.map<AudioDevice>((name, index) => ({ index, name, default: index === 0 }));
      case "mic_monitor": {
        const a = rawArgs as CommandArgs<"mic_monitor">;
        if (this.micTimer) clearInterval(this.micTimer);
        this.micTimer = a.on ? this.levelLoop() : null;
        return null;
      }
      case "dictation_toggle": {
        const started = !this.busy;
        this.simulateDictation();
        return { started };
      }
      case "test_record_start": {
        if (this.testRec) clearInterval(this.testRec.timer);
        this.testRec = { started: Date.now(), timer: this.levelLoop() };
        return null;
      }
      case "test_record_stop": {
        const rec = this.testRec;
        this.testRec = null;
        if (rec) clearInterval(rec.timer);
        const audio = rec ? (Date.now() - rec.started) / 1000 : 0;
        return sleep(650).then(() => ({
          text: this.data.testResult,
          language: this.sysLang,
          audio_s: Math.round(audio * 10) / 10,
          asr_ms: 280 + Math.round(Math.random() * 120),
        }));
      }
      case "cuda_check":
        return sleep(500).then(() =>
          this.opts.noGpu
            ? { ok: false, device: "cpu", gpu_name: null, vram_total_mb: null, error: this.tx.noGpu }
            : { ok: true, device: "cuda", gpu_name: "NVIDIA GeForce RTX 3060 Ti", vram_total_mb: 8192, error: null },
        );
      case "setup_status":
        return {
          model_ready: this.modelReady(),
          cuda_ready: this.cudaReady,
          first_run_done: this.settings.first_run_done,
          model_dir: this.modelDir(),
          model_dir_default: HF_CACHE,
        };
      case "setup_download": {
        const a = rawArgs as CommandArgs<"setup_download">;
        this.progress(a.what);
        return null;
      }
      case "model_dir_set": {
        const a = rawArgs as CommandArgs<"model_dir_set">;
        const path = this.checkModelDir(a.path, cmd);
        const from = this.modelDir();
        const to = path || HF_CACHE;
        if (dirKey(from) === dirKey(to)) {
          // как ядро: та же папка в другой записи (явный путь кэша по умолчанию → "") —
          // сохранить без переноса и перезагрузки
          if (path !== this.settings.model_dir) this.applySettings({ model_dir: path });
          return { ok: true, moving: false };
        }
        if (this.modelJob) throw new BackendError(this.tx.modelBusy, cmd);
        if (a.move && this.modelReady()) {
          this.moveModel(path, from, to);
          return { ok: true, moving: true };
        }
        this.applySettings({ model_dir: path });
        return { ok: true, moving: false };
      }
      case "model_recommend":
        return sleep(700).then(() => this.recommend());
      case "history_list": {
        const a = (rawArgs ?? {}) as CommandArgs<"history_list">;
        const q = (a.query ?? "").trim().toLowerCase();
        const all = q
          ? this.history.filter(
              (h) =>
                h.text.toLowerCase().includes(q) ||
                h.raw.toLowerCase().includes(q) ||
                h.app.toLowerCase().includes(q) ||
                h.title.toLowerCase().includes(q),
            )
          : this.history;
        const offset = a.offset ?? 0;
        const limit = a.limit ?? 50;
        return sleep(q ? 120 : 240).then(() => ({
          items: all.slice(offset, offset + limit),
          total: all.length,
        }));
      }
      case "history_delete": {
        const a = rawArgs as CommandArgs<"history_delete">;
        this.history = this.history.filter((h) => h.id !== a.id);
        return null;
      }
      case "history_clear": {
        const a = rawArgs as CommandArgs<"history_clear">;
        const border = Date.now() / 1000 - this.periodSeconds(a.period);
        const before = this.history.length;
        this.history = this.history.filter((h) => h.ts < border);
        return { deleted: before - this.history.length };
      }
      case "history_reinsert":
      case "last_insert":
        return null;
      case "dict_list":
        return structuredClone(this.dict);
      case "dict_upsert": {
        const a = rawArgs as CommandArgs<"dict_upsert">;
        const term = a.term.trim();
        const dup = this.dict.find(
          (d) => d.term.toLowerCase() === term.toLowerCase() && d.id !== a.id,
        );
        const variants = [...new Set(a.variants.map((v) => v.trim()).filter(Boolean))];
        if (dup) {
          dup.variants = [...new Set([...dup.variants, ...variants])];
          if (a.id !== undefined) this.dict = this.dict.filter((d) => d.id !== a.id);
          return structuredClone(dup);
        }
        const existing = a.id !== undefined ? this.dict.find((d) => d.id === a.id) : undefined;
        if (existing) {
          existing.term = term;
          existing.variants = variants;
          return structuredClone(existing);
        }
        const item: DictItem = { id: this.nextId++, term, variants };
        this.dict.push(item);
        return structuredClone(item);
      }
      case "dict_delete": {
        const a = rawArgs as CommandArgs<"dict_delete">;
        this.dict = this.dict.filter((d) => d.id !== a.id);
        this.journalDrop(a.id);
        return null;
      }
      case "dict_learn_accept": {
        const a = rawArgs as CommandArgs<"dict_learn_accept">;
        return this.learnAdd(a.term, a.variant, "accepted").item;
      }
      case "dict_learn_undo": {
        const a = rawArgs as CommandArgs<"dict_learn_undo">;
        const term = a.term.trim().toLowerCase();
        const d =
          (a.id !== null ? this.dict.find((x) => x.id === a.id) : undefined) ??
          this.dict.find((x) => x.term.toLowerCase() === term);
        if (!d) throw new BackendError(fill(this.tx.learnNotFound, { term: a.term }), cmd);
        this.journalPop(a.term, a.variant, a.created, a.id);
        // Как ядро: вариант убирается, а созданный правкой термин удаляется, только если
        // других вариантов в нём не осталось.
        const v = a.variant.trim().toLowerCase();
        d.variants = d.variants.filter((x) => x.toLowerCase() !== v);
        if (a.created && d.variants.length === 0) this.dict = this.dict.filter((x) => x !== d);
        return null;
      }
      case "dict_learn_journal": {
        const a = (rawArgs ?? {}) as CommandArgs<"dict_learn_journal">;
        const limit = Math.max(0, Math.min(JOURNAL_MAX, Math.trunc(a.limit ?? 50)));
        // Записи терминов, которых уже нет (или их переименовали), не показываем — отменять нечего.
        return this.journal
          .filter((e) => this.dict.some((d) => d.id === e.id && d.term.toLowerCase() === e.term.toLowerCase()))
          .reverse()
          .slice(0, limit)
          .map((e) => ({ ...e }));
      }
      case "snippets_list":
        return structuredClone(this.snippets);
      case "snippet_upsert": {
        const a = rawArgs as CommandArgs<"snippet_upsert">;
        const existing =
          (a.id !== undefined ? this.snippets.find((s) => s.id === a.id) : undefined) ??
          this.snippets.find((s) => s.trigger.toLowerCase() === a.trigger.trim().toLowerCase());
        if (existing) {
          existing.trigger = a.trigger.trim();
          existing.text = a.text;
          return structuredClone(existing);
        }
        const item: Snippet = { id: this.nextId++, trigger: a.trigger.trim(), text: a.text };
        this.snippets.push(item);
        return structuredClone(item);
      }
      case "snippet_delete": {
        const a = rawArgs as CommandArgs<"snippet_delete">;
        this.snippets = this.snippets.filter((s) => s.id !== a.id);
        return null;
      }
      case "export_data": {
        const out: ExportData = {
          version: 1,
          dictionary: this.dict.map((d) => ({ term: d.term, variants: [...d.variants] })),
          snippets: this.snippets.map((s) => ({ trigger: s.trigger, text: s.text })),
        };
        return out;
      }
      case "import_data": {
        const a = rawArgs as CommandArgs<"import_data">;
        if (a.replace) {
          this.dict = [];
          this.journalDrop(null);
          this.snippets = [];
        }
        let nd = 0;
        let ns = 0;
        for (const d of a.data.dictionary ?? []) {
          this.handle("dict_upsert", { term: d.term, variants: d.variants });
          nd++;
        }
        for (const s of a.data.snippets ?? []) {
          this.handle("snippet_upsert", { trigger: s.trigger, text: s.text });
          ns++;
        }
        return { dictionary: nd, snippets: ns };
      }
      case "fillers_stats":
      case "fillers_config":
      case "fillers_config_set":
      case "fillers_defaults":
      case "fillers_rebuild":
      case "fillers_reset":
      case "fillers_ai_suggest":
        return sleep(cmd === "fillers_ai_suggest" ? 900 : 150).then(() =>
          fillersMock(cmd, rawArgs, this.history, [...DATA.ru.fillers, ...DATA.en.fillers]),
        );
      case "stats_get":
        return sleep(180).then(() => this.stats());
      case "draft_get":
        return { text: this.draft };
      case "draft_set": {
        const a = rawArgs as CommandArgs<"draft_set">;
        this.draft = a.text;
        return null;
      }
      case "styles_list":
        return structuredClone(DATA[this.state.ui_language].styles);
      case "app_rules_list":
        return Object.entries(this.settings.app_rules).map<AppRule>(([process, r]) => ({
          process,
          insert_mode: r.insert_mode,
          style: r.style,
        }));
      case "app_rule_upsert": {
        const a = rawArgs as CommandArgs<"app_rule_upsert">;
        const process = a.process.trim().toLowerCase();
        const rules = { ...this.settings.app_rules, [process]: { insert_mode: a.insert_mode, style: a.style } };
        this.applySettings({ app_rules: rules });
        return { process, insert_mode: a.insert_mode, style: a.style };
      }
      case "app_rule_delete": {
        const a = rawArgs as CommandArgs<"app_rule_delete">;
        const rules = { ...this.settings.app_rules };
        delete rules[a.process.toLowerCase()];
        this.applySettings({ app_rules: rules });
        return null;
      }
      case "recent_apps":
        return structuredClone(this.data.recentApps);
      case "rdp_windows":
        return sleep(200).then(() => this.rdpWindows());
      case "rdp_toggle": {
        // {key, included}; для совместимости — {title, included}
        const a = rawArgs as { key?: string; title?: string; included: boolean };
        const key =
          a.key ?? this.rdpOpen.find((w) => a.title !== undefined && w.title === a.title)?.key ?? a.title ?? "";
        const rest = this.settings.broadcast_targets.filter((k) => !this.sameKey(k, key));
        this.applySettings({ broadcast_targets: a.included ? [...rest, key] : rest });
        return null;
      }
      case "ai_key_status": {
        const a = rawArgs as CommandArgs<"ai_key_status">;
        const k = this.secrets[a.provider];
        return { has_key: Boolean(k), masked: k ? mask(k) : null };
      }
      case "ai_key_import_env": {
        const a = (rawArgs ?? {}) as CommandArgs<"ai_key_import_env">;
        const path = (a.path ?? this.settings.env_path).trim();
        return sleep(300).then(() => {
          if (!path) throw new BackendError(this.tx.envEmpty, cmd);
          if (!path.toLowerCase().endsWith(".env")) {
            throw new BackendError(this.tx.envNotFound, cmd);
          }
          this.secrets.openrouter = "sk-or-v1-…XYZ";
          this.ensureLegacyKey("openrouter");
          this.balanceCache = null;
          return { imported: true, masked: this.secrets.openrouter };
        });
      }
      case "ai_key_set": {
        const a = rawArgs as CommandArgs<"ai_key_set">;
        if (a.key.trim().length < 12) throw new BackendError(this.tx.badKey, cmd);
        const masked = mask(a.key.trim());
        this.secrets[a.provider] = masked;
        this.ensureLegacyKey(a.provider);
        this.balanceCache = null;
        return { masked };
      }
      case "ai_key_delete": {
        const a = rawArgs as CommandArgs<"ai_key_delete">;
        this.secrets[a.provider] = null;
        this.applySettings({ ai_keys: this.settings.ai_keys.filter((e) => e.id !== a.provider) });
        this.balanceCache = null;
        return null;
      }
      case "ai_key_check":
        return sleep(700).then(() =>
          this.secrets[this.settings.ai_keys.find((e) => e.provider === "openrouter")?.id ?? "openrouter"]
            ? {
                valid: true,
                label: this.tx.keyLabel,
                balance_usd: 12.34,
                usage_usd: 0.5,
                limit_usd: null,
                error: null,
              }
            : {
                valid: false,
                label: null,
                balance_usd: null,
                usage_usd: null,
                limit_usd: null,
                error: this.tx.noKey,
              },
        );
      case "ai_test": {
        const a = rawArgs as CommandArgs<"ai_test">;
        const ms = 900 + Math.round(Math.random() * 1300);
        return sleep(ms).then(() => {
          if (a.style !== "as_is") this.recordUsage("test", countWords(a.text));
          return {
          text: this.cleanup(a.text, a.style),
          ms,
          cost_usd: a.style === "as_is" ? 0 : (this.usage[this.usage.length - 1]?.cost ?? 0),
          provider: this.settings.ai_provider === "openrouter" ? "Amazon Bedrock" : this.settings.ai_provider,
          model:
            this.settings.ai_provider === "anthropic"
              ? this.settings.anthropic_model
              : this.settings.ai_provider === "ollama"
                ? this.settings.ollama_model
                : this.settings.ai_model,
          };
        });
      }
      case "recordings_list": {
        const items = this.recordingsDemo();
        return sleep(160).then(() => ({
          items: structuredClone(items),
          total_bytes: items.reduce((n, r) => n + r.bytes, 0),
          keep: this.settings.recordings_keep,
          max_bytes: this.settings.recordings_max_mb * 1024 * 1024,
        }));
      }
      case "recording_audio": {
        const a = rawArgs as CommandArgs<"recording_audio">;
        const rec = this.recordingsDemo().find((r) => r.id === a.id);
        if (!rec) throw new BackendError(this.tx.recordingNotFound, cmd);
        return sleep(60).then(() => ({ mime: "audio/wav", data: demoWav(Math.min(rec.duration_s, 3)) }));
      }
      case "recording_transcribe": {
        const a = rawArgs as CommandArgs<"recording_transcribe">;
        const rec = this.recordingsDemo().find((r) => r.id === a.id);
        if (!rec) throw new BackendError(this.tx.recordingNotFound, cmd);
        const h = this.history.find((x) => x.id === rec.history_id) ?? this.history[0];
        const text = h?.text ?? "";
        rec.status = text ? "ok" : "empty";
        rec.chars = text.length;
        return sleep(900).then(() => ({
          id: rec.id, text, raw: h?.raw ?? "", language: h?.language ?? "ru",
          duration_s: rec.duration_s, ai: Boolean(h?.ai),
        }));
      }
      case "recording_delete": {
        const a = rawArgs as CommandArgs<"recording_delete">;
        this.recordings = this.recordingsDemo().filter((r) => r.id !== a.id);
        return null;
      }
      case "recordings_clear": {
        const n = this.recordingsDemo().length;
        this.recordings = [];
        return { deleted: n };
      }
      case "recording_reveal":
        return null;
      case "open_logs":
        return null;
      case "title_rules_list":
        return structuredClone(this.settings.title_rules);
      case "title_rule_upsert": {
        // То же правило — как у ядра: процесс + название без учёта регистра и «ё» (замена на месте)
        const r = this.checkRule(rawArgs as TitleRule, cmd);
        const k = titleRuleKey(r);
        const rules = [...this.settings.title_rules];
        const i = rules.findIndex((x) => titleRuleKey(x) === k);
        if (i >= 0) rules[i] = r;
        else rules.push(r);
        this.applySettings({ title_rules: rules });
        return structuredClone(r);
      }
      case "title_rule_delete": {
        const a = rawArgs as CommandArgs<"title_rule_delete">;
        const k = titleRuleKey(a);
        this.applySettings({ title_rules: this.settings.title_rules.filter((x) => titleRuleKey(x) !== k) });
        return null;
      }
      case "vocab_list": {
        const a = (rawArgs ?? {}) as CommandArgs<"vocab_list">;
        return sleep(120).then(() => this.vocabList(a.process, a.limit ?? 50));
      }
      case "vocab_exclude": {
        const a = rawArgs as CommandArgs<"vocab_exclude">;
        const w = String(a.word ?? "").trim().toLowerCase();
        if (!w) throw new BackendError(this.tx.emptyWord, cmd);
        if (a.excluded) this.vocabExcluded.add(w);
        else this.vocabExcluded.delete(w);
        return null;
      }
      case "ai_usage_stats":
        return sleep(90).then(() => this.aiStats());
      case "ai_balance": {
        const a = (rawArgs ?? {}) as CommandArgs<"ai_balance">;
        return sleep(a.force ? 650 : 60).then(() => this.aiBalance(Boolean(a.force)));
      }
      case "meeting_sources":
        return sleep(80).then(() => this.meetingSources());
      case "meeting_start": {
        const a = (rawArgs ?? {}) as CommandArgs<"meeting_start">;
        // модель уже загружена — старт почти мгновенный (иначе ядро сначала её загружает)
        return sleep(350).then(() => this.meetingStart(a.source_id, cmd));
      }
      case "meeting_stop":
        return this.meetingStop();
      case "meeting_autostop_cancel":
        // как ядро: отсчёт снят, сеанс идёт; встречи нет — просто статус
        if (this.meetingEndingTimer) {
          this.meetingEndingStop();
          this.setMeeting({ hint: null, ending_in_s: null });
        }
        return structuredClone(this.state.meeting);
      case "meeting_status":
        return structuredClone(this.state.meeting);
      case "meeting_lines": {
        const a = (rawArgs ?? {}) as CommandArgs<"meeting_lines">;
        const limit = a.limit ?? 200;
        if (!Number.isInteger(limit) || limit < 1 || limit > 500) throw new BackendError(this.tx.meetingBadLimit, cmd);
        // как ядро: after_id — следующие за ним по порядку, без него — последние limit
        const pick =
          a.after_id === undefined
            ? this.meetingLines.slice(-limit)
            : this.meetingLines.filter((l) => l.id > (a.after_id ?? 0)).slice(0, limit);
        return pick.map((l) => ({ ...l }));
      }
      case "meeting_copy": {
        const a = (rawArgs ?? {}) as CommandArgs<"meeting_copy">;
        const what = a.what ?? "transcript";
        if (!["transcript", "notes", "all"].includes(what)) throw new BackendError(this.tx.copyWhat, cmd);
        // как ядро: время, перевод и под ним оригинал; заметки — Markdown
        const lines = this.meetingLines
          .map((l) => (l.translation ? `${l.ts} ${l.translation}\n         ${l.text}` : `${l.ts} ${l.text}`))
          .join("\n");
        const notes = this.notesBody !== null ? (this.notesView()?.markdown ?? "") : "";
        if (what === "notes" && !notes) throw new BackendError(this.tx.meetingNotesNothing, cmd);
        const text = what === "notes" ? notes : what === "all" && notes ? `${notes}\n\n${lines}` : lines;
        if (!text.trim()) throw new BackendError(this.tx.meetingNothingToCopy, cmd);
        // настоящее ядро кладёт текст в буфер само; в браузере — если страница разрешает
        void navigator.clipboard?.writeText(text).catch(() => undefined);
        return { chars: text.length };
      }
      case "meeting_open_folder":
        return null;
      case "meeting_suggest_answer":
        return this.meetingSuggestAnswer((rawArgs ?? {}) as CommandArgs<"meeting_suggest_answer">, cmd);
      case "meeting_notes": {
        if (!this.state.meeting.started_at) throw new BackendError(this.tx.meetingNotesNoMeeting, cmd);
        if (!this.meetingLines.some((l) => l.text || l.partial)) throw new BackendError(this.tx.meetingNotesEmpty, cmd);
        return this.startNotes(false);
      }
      case "meeting_notes_get":
        return this.notesView();
      case "ai_keys_list":
        return this.settings.ai_keys.map((e) => this.keyItem(e));
      case "ai_key_add": {
        const a = rawArgs as CommandArgs<"ai_key_add">;
        if (a.key.trim().length < 12) throw new BackendError(this.tx.badKey, cmd);
        const ids = new Set(this.settings.ai_keys.map((e) => e.id));
        let id: string = a.provider;
        while (ids.has(id)) id = `${a.provider}-${Math.random().toString(16).slice(2, 8)}`;
        const hasBal = a.balance_usd !== undefined && a.balance_usd !== null;
        const entry: AiKeyEntry = {
          id,
          provider: a.provider,
          label: (a.label ?? "").trim(),
          enabled: true,
          balance_usd: hasBal ? (a.balance_usd ?? null) : null,
          balance_at: hasBal ? Math.floor(Date.now() / 1000) : null,
        };
        this.secrets[id] = mask(a.key.trim());
        this.applySettings({ ai_keys: [...this.settings.ai_keys, entry] });
        this.balanceCache = null;
        return sleep(150).then(() => this.keyItem(entry));
      }
      case "ai_key_update": {
        const a = rawArgs as CommandArgs<"ai_key_update">;
        const e = this.settings.ai_keys.find((x) => x.id === a.id);
        if (!e) throw new BackendError(this.tx.keyUnknown, cmd);
        const next: AiKeyEntry = { ...e };
        if (a.label !== undefined) next.label = a.label.trim();
        if (a.enabled !== undefined) next.enabled = a.enabled;
        if (a.balance_usd !== undefined) {
          next.balance_usd = a.balance_usd;
          next.balance_at = a.balance_usd !== null ? Math.floor(Date.now() / 1000) : null;
          this.keySpent[e.id] = 0;
        }
        this.applySettings({ ai_keys: this.settings.ai_keys.map((x) => (x.id === a.id ? next : x)) });
        this.balanceCache = null;
        return this.keyItem(next);
      }
      case "ai_key_replace": {
        const a = rawArgs as CommandArgs<"ai_key_replace">;
        const e = this.settings.ai_keys.find((x) => x.id === a.id);
        if (!e) throw new BackendError(this.tx.keyUnknown, cmd);
        if (a.key.trim().length < 12) throw new BackendError(this.tx.badKey, cmd);
        this.secrets[e.id] = mask(a.key.trim());
        this.balanceCache = null;
        return this.keyItem(e);
      }
      case "ai_key_remove": {
        const a = rawArgs as CommandArgs<"ai_key_remove">;
        if (!this.settings.ai_keys.some((x) => x.id === a.id)) throw new BackendError(this.tx.keyUnknown, cmd);
        this.secrets[a.id] = null;
        this.applySettings({ ai_keys: this.settings.ai_keys.filter((x) => x.id !== a.id) });
        this.balanceCache = null;
        return null;
      }
      case "ai_keys_reorder": {
        const a = rawArgs as CommandArgs<"ai_keys_reorder">;
        const byId = new Map(this.settings.ai_keys.map((e) => [e.id, e]));
        if (a.ids.length !== byId.size || a.ids.some((id) => !byId.has(id))) throw new BackendError(this.tx.keyUnknown, cmd);
        this.applySettings({ ai_keys: a.ids.map((id) => byId.get(id) as AiKeyEntry) });
        this.balanceCache = null;
        return this.settings.ai_keys.map((e) => this.keyItem(e));
      }
      case "ai_key_info": {
        const a = rawArgs as CommandArgs<"ai_key_info">;
        return sleep(a.force ? 600 : 120).then(() => this.keyInfo(a.id, cmd));
      }
    }
  }
}
