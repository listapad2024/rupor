export type MouseButton = "" | "middle" | "x1" | "x2";

// Типы протокола ядра — по docs/ARCHITECTURE.md (раздел «WebSocket-протокол»).

export type CoreStateName =
  | "loading"
  | "idle"
  | "recording"
  | "hands_free"
  | "processing"
  | "ai"
  | "paused"
  | "error";

export type InsertMode = "auto" | "type" | "clipboard";
export type ThemeMode = "system" | "dark" | "light";
/**
 * keys — облачные ключи из Settings.ai_keys по приоритету (переход на следующий при ошибке);
 * openrouter / anthropic — только ключи этого провайдера из того же списка; ollama — офлайн.
 */
export type AiProvider = "keys" | "openrouter" | "anthropic" | "ollama";
export type KeyProvider = "openrouter" | "anthropic";
export type AiMode = "wait" | "raw_first";
export type CommandProvider = "keys" | "openrouter" | "claude_cli" | "anthropic" | "ollama";

/** Запись списка ключей ИИ (Settings.ai_keys); порядок списка — приоритет. Секрета тут нет. */
export interface AiKeyEntry {
  id: string;
  provider: KeyProvider;
  label: string;
  enabled: boolean;
  /** Остаток, введённый вручную (для провайдеров без API баланса), $; null — не введён. */
  balance_usd: number | null;
  /** Когда введён остаток, unix сек. */
  balance_at: number | null;
}

/** Строка списка ключей в настройках (ai_keys_list): запись + маска и расход, без сети. */
export interface AiKeyItem extends AiKeyEntry {
  /** Подпись для людей: своя или «Anthropic #2». */
  title: string;
  has_key: boolean;
  masked: string | null;
  /** Расход Рупора через ключ: всего и с момента ввода остатка. */
  spent_usd: number;
  calls: number;
  spent_since_usd: number | null;
  /** Ключ после ошибки стоит в конце очереди — до (unix сек) и почему (AIError.kind). */
  paused_until: number | null;
  paused_reason: string | null;
}

/** Проверка и баланс одного ключа (ai_key_info; кэш ядра 5 мин, force — сейчас). */
export interface AiKeyInfo {
  id: string;
  provider: KeyProvider;
  valid: boolean;
  balance_usd: number | null;
  /** api — баланс от провайдера; estimate — введённый остаток минус расход Рупора. */
  balance_source: "api" | "estimate" | null;
  usage_usd: number | null;
  limit_usd: number | null;
  spent_usd: number;
  calls: number;
  spent_since_usd: number | null;
  checked_at: number | null;
  error: string | null;
}
export type ClearPeriod = "day" | "week" | "month" | "all";
/** Словарь учится на правках: auto — сразу добавлять, suggest — спрашивать, off — выключено. */
export type LearnMode = "auto" | "suggest" | "off";
/**
 * Звук компьютера во время записи: off — не трогать, duck — приглушать до duck_level %
 * от исходной громкости, mute — выключать. Ядро меняет громкость аудиосессий других
 * приложений (Core Audio), свои звуки и системные звуки Windows не трогает.
 */
export type DuckMode = "off" | "duck" | "mute";
export type DownloadWhat = "model" | "cuda";
/** Язык интерфейса, разрешённый ядром (State.ui_language). */
export type UiLanguage = "ru" | "en";
/** Настройка языка интерфейса: system — по языку Windows (ru/be/uk/kk → ru, иначе en). */
export type LanguagePref = "system" | "ru" | "en";
/**
 * Язык диктовки: auto — определять по фразе с приоритетом языка интерфейса (другой язык —
 * только при уверенности ≥ en_threshold); ru/en — всегда этот язык, без определения.
 */
export type DictationLanguage = "auto" | "ru" | "en";

export interface CoreHotkeys {
  ptt: string;
  broadcast: string;
  command: string;
  cancel: string;
  /** Сочетание «Вставить последнее» (ctrl+alt+shift+v), "" — выключено. */
  insert_last: string;
  /** Сочетание «Скопировать последнее» (ctrl+alt+shift+c), "" — выключено. */
  copy_last: string;
  /** Сочетание «Сменить раскладку выделенного» (shift+pause), "" — выключено. */
  convert_layout: string;
}

export interface CoreState {
  state: CoreStateName;
  paused: boolean;
  device: "cuda" | "cpu" | null;
  model_loaded: boolean;
  insert_mode: InsertMode;
  style: string;
  ai_enabled: boolean;
  first_run_done: boolean;
  version: string;
  elevated: boolean;
  hotkeys: CoreHotkeys;
  /** Язык интерфейса и сообщений ядра: settings.language, для "system" — по языку Windows. */
  ui_language: UiLanguage;
}

export type HistoryTarget = "window" | "rdp_all" | "test" | "command";

export interface HistoryItem {
  id: number;
  ts: number;
  app: string;
  title: string;
  raw: string;
  text: string;
  duration_s: number;
  words: number;
  language: string;
  style: string;
  ai: boolean;
  latency_ms: number;
  target: HistoryTarget;
}

export interface DictItem {
  id: number;
  term: string;
  variants: string[];
}

/**
 * Кто пополнил словарь: auto — Рупор выучил правку и добавил сам, accepted — вы нажали
 * «Добавить» на предложении.
 */
export type LearnSource = "auto" | "accepted";

/** Запись журнала обучения (dict_learn_journal): что изменила одна выученная правка. */
export interface LearnJournalEntry {
  /** id термина в словаре. */
  id: number;
  /** Термин (как надо). */
  term: string;
  /** Добавленный вариант (как было распознано); "" — правка только создала термин. */
  variant: string;
  /** Термин создан этой правкой (отмена удалит его, если других вариантов не осталось). */
  created: boolean;
  /** Когда, unix сек; null — запись из версии, где время не сохранялось. */
  ts: number | null;
  /** null — запись из версии, где источник не сохранялся. */
  source: LearnSource | null;
}

export interface Snippet {
  id: number;
  trigger: string;
  text: string;
}

export interface AppRule {
  process: string;
  insert_mode: InsertMode | null;
  style: string | null;
}

/**
 * Правило по названию окна: process — имя exe в нижнем регистре или "*" (любое приложение),
 * title — подстрока заголовка окна без учёта регистра (не короче 2 символов). null в
 * insert_mode/style — не переопределять (действует правило приложения или настройка по умолчанию).
 */
export interface TitleRule {
  process: string;
  title: string;
  insert_mode: InsertMode | null;
  style: string | null;
}

export interface DailyWords {
  date: string;
  words: number;
}

export interface Stats {
  words_today: number;
  words_week: number;
  words_total: number;
  wpm: number;
  saved_minutes: number;
  streak_days: number;
  dictations_total: number;
  daily: DailyWords[];
}

export interface AppRuleSetting {
  insert_mode: InsertMode | null;
  style: string | null;
}

/** Объект настроек целиком (как settings.json). */
export interface Settings {
  ptt_key: string;
  broadcast_modifier: string;
  command_modifier: string;
  cancel_key: string;
  tap_max_ms: number;
  double_tap_ms: number;
  /** Сочетания клавиш вида «ctrl+alt+shift+v»; "" — выключено. */
  insert_last_hotkey: string;
  copy_last_hotkey: string;
  convert_layout_hotkey: string;
  /** Кнопка мыши для диктовки: "" — нет, middle — колёсико, x1/x2 — боковые. */
  mouse_button: MouseButton;

  input_device: string | null;
  min_record_ms: number;

  model: string;
  /**
   * Папка моделей Whisper в формате кэша Hugging Face (models--org--name/blobs|snapshots|refs,
   * как download_root у faster-whisper — её можно отдать и другим программам). "" — кэш
   * Hugging Face по умолчанию (SetupStatus.model_dir_default). Путь — абсолютный.
   * Сменить с переносом скачанной модели — команда model_dir_set.
   */
  model_dir: string;
  device: string;
  compute_type: string;
  beam_size: number;
  en_threshold: number;
  terms: string[];
  unload_after_min: number;

  insert_mode: InsertMode;
  type_delay_ms: number;
  rdp_type_delay_ms: number;
  type_chunk: number;
  clipboard_restore_ms: number;
  rdp_classes: string[];
  rdp_processes: string[];
  app_rules: Record<string, AppRuleSetting>;
  /** Правила по названию окна — важнее правил приложений (самое длинное совпадение title). */
  title_rules: TitleRule[];
  /** Ключи RDP-сессий, куда идёт рассылка (белый список, без учёта регистра). */
  broadcast_targets: string[];
  /** Устарело (до broadcast_targets): ядро мигрирует его при загрузке. */
  broadcast_excluded?: string[];
  add_trailing_space: boolean;
  /** Учиться на правках пользователя после вставки (UI Automation, ~60 с). */
  learn_mode: LearnMode;
  /**
   * Словарь: узнавать похожие написания терминов по звучанию (латиница/кириллица, слитно/
   * раздельно, падежи) — варианты нужны только для непохожих ошибок; по умолчанию true.
   */
  dict_fuzzy: boolean;
  /** Подсказывать распознаванию ваши частые слова из истории диктовок. */
  vocab_enabled: boolean;
  /** Подсказывать распознаванию 1–2 предложения перед курсором (UIA; не в RDP и не в паролях). */
  context_enabled: boolean;

  /** Звук других приложений во время записи; по умолчанию "duck". */
  duck_mode: DuckMode;
  /** До какого процента от исходной громкости приглушать (duck_mode="duck"), целое 0–100; по умолчанию 15. */
  duck_level: number;

  sounds: boolean;
  autostart: boolean;

  default_style: string;
  history_enabled: boolean;
  history_retention_days: number;
  /** Сколько последних записей голоса хранить на диске (0 — не сохранять), 0–500; по умолчанию 30. */
  recordings_keep: number;
  /** Сколько места записи голоса могут занять всего, МБ (20–20000); по умолчанию 300. */
  recordings_max_mb: number;
  pill_enabled: boolean;
  pill_position: [number, number] | null;
  theme: ThemeMode;
  accent: string;
  /** Язык интерфейса (по умолчанию "system"); разрешённый — в State.ui_language. */
  language: LanguagePref;
  /** Язык распознавания (по умолчанию "auto"). */
  dictation_language: DictationLanguage;
  first_run_done: boolean;
  /** Служебное: версия схемы хоткеев (миграция настроек в ядре), интерфейс её не меняет. */
  hotkeys_version?: number;

  ai_enabled: boolean;
  ai_provider: AiProvider;
  ai_keys: AiKeyEntry[];
  ai_model: string;
  anthropic_model: string;
  ollama_model: string;
  ollama_url: string;
  ai_provider_order: string[];
  ai_mode: AiMode;
  ai_min_words: number;
  ai_timeout_s: number;
  command_provider: CommandProvider;
  env_path: string;
}

export type SettingsPatch = Partial<Settings>;

export interface AudioDevice {
  index: number;
  name: string;
  default: boolean;
}

export interface TestRecordResult {
  text: string;
  language: string;
  audio_s: number;
  asr_ms: number;
}

export interface CudaCheck {
  ok: boolean;
  device: "cuda" | "cpu";
  gpu_name: string | null;
  vram_total_mb: number | null;
  error: string | null;
}

export interface SetupStatus {
  /** Модель settings.model есть в папке model_dir. */
  model_ready: boolean;
  cuda_ready: boolean;
  first_run_done: boolean;
  /** Папка, где Рупор сейчас ищет и куда качает модели (абсолютный путь). */
  model_dir: string;
  /** Кэш Hugging Face по умолчанию — папка при settings.model_dir = "". */
  model_dir_default: string;
}

/** Оценка 1–5 в списке моделей (качество, скорость). */
export type ModelRating = 1 | 2 | 3 | 4 | 5;

/** Модель Whisper в выборе мастера (model_recommend). */
export interface ModelOption {
  /** Имя для settings.model: tiny, base, small, medium, large-v3-turbo, large-v3. */
  model: string;
  /** Репозиторий на Hugging Face (Systran/faster-whisper-…). */
  repo: string;
  /** Размер файлов модели, МБ. */
  size_mb: number;
  quality: ModelRating;
  speed: ModelRating;
  recommended: boolean;
  /** Пояснение на языке интерфейса. */
  note: string;
}

/** Модель под этот компьютер (мастер первого запуска). */
export interface ModelRecommend {
  gpu_name: string | null;
  vram_mb: number | null;
  /** CUDA работает (или видеокарта NVIDIA с драйвером есть, а библиотеки ещё не скачаны). */
  cuda_ok: boolean;
  ram_gb: number;
  cpu_threads: number;
  recommended: { model: string; device: "cuda" | "cpu"; compute_type: string };
  /** Почему эта модель — на языке интерфейса. */
  reason: string;
  options: ModelOption[];
}

export interface ExportData {
  version: 1;
  dictionary: { term: string; variants: string[] }[];
  snippets: { trigger: string; text: string }[];
}

export interface StyleInfo {
  id: string;
  name: string;
  description: string;
}

export interface RecentApp {
  process: string;
  title: string;
  /** До 10 последних различных заголовков окон этого процесса (только в памяти ядра). */
  titles: string[];
}

export interface RdpWindow {
  /** Ключ сессии: для mstsc — имя сервера из заголовка, для других клиентов — заголовок. */
  key: string;
  title: string;
  process: string;
  /** Ключ есть в broadcast_targets — рассылка идёт в это окно. */
  included: boolean;
}

/** Слово личного словаря частот (vocab_list). */
export interface VocabItem {
  word: string;
  count: number;
  /** Исключено пользователем — в подсказку распознаванию и ИИ не попадает. */
  excluded: boolean;
}

// ——— Расход ИИ ———

export type AiUsageKind = "cleanup" | "command" | "test";

export interface AiUsageTotals {
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
  /** Удачные вызовы с неизвестной ценой модели (cost 0, в avg_cost_per_call не входят). */
  unknown_cost_calls?: number;
}

export interface AiUsageDay {
  /** YYYY-MM-DD, локальная дата. */
  date: string;
  calls: number;
  tokens_in: number;
  tokens_out: number;
  cost_usd: number;
}

export interface AiUsageStats {
  today: AiUsageTotals;
  week: AiUsageTotals;
  month: AiUsageTotals;
  total: AiUsageTotals;
  avg_cost_per_call: number;
  /** Ровно 30 последних дней, включая дни без вызовов. */
  daily: AiUsageDay[];
  by_kind: { kind: AiUsageKind; calls: number; cost_usd: number }[];
  by_model: { model: string; calls: number; cost_usd: number; unknown_cost_calls?: number }[];
}

/** Баланс ключа, который сейчас первым в очереди (кэш ядра 5 мин; force — проверить сейчас). */
export interface AiBalance {
  balance_usd: number | null;
  usage_usd: number | null;
  limit_usd: number | null;
  /** Когда проверено, unix сек; null — ещё не проверялось. */
  checked_at: number | null;
  /** Причина на языке интерфейса (нет ключа, ключ отвергнут, нет сети) или null. */
  error: string | null;
  /** Чей это баланс. */
  key_id?: string | null;
  provider?: KeyProvider | null;
  label?: string | null;
  /** true — оценка (введённый остаток минус расход), не ответ провайдера. */
  estimate?: boolean;
}

/** Событие после каждого вызова ИИ (в том числе неудачного — cost 0). */
export interface AiUsageEvent {
  kind: AiUsageKind;
  cost_usd: number;
  tokens_in: number;
  tokens_out: number;
  today_cost_usd: number;
}

export interface KeyStatus {
  has_key: boolean;
  masked: string | null;
}

export interface KeyCheck {
  valid: boolean;
  label: string | null;
  balance_usd: number | null;
  usage_usd: number | null;
  limit_usd: number | null;
  error: string | null;
}

export interface AiTestResult {
  text: string;
  ms: number;
  cost_usd: number | null;
  provider: string;
  model: string;
}

/** Команды WS: аргументы и результат. */
export interface CommandMap {
  get_state: { args: void; result: CoreState };
  get_settings: { args: void; result: Settings };
  set_settings: { args: { patch: SettingsPatch }; result: Settings };
  pause: { args: { on: boolean }; result: CoreState };
  quit: { args: void; result: null };
  list_devices: { args: void; result: AudioDevice[] };
  mic_monitor: { args: { on: boolean }; result: null };
  test_record_start: { args: void; result: null };
  dictation_toggle: { args: void; result: { started: boolean } };
  test_record_stop: { args: void; result: TestRecordResult };
  cuda_check: { args: void; result: CudaCheck };
  setup_status: { args: void; result: SetupStatus };
  setup_download: { args: { what: DownloadWhat }; result: null };
  /**
   * Сменить папку моделей (path "" — кэш Hugging Face по умолчанию). move — перенести туда
   * скачанную модель: moving=true — перенос идёт в фоне (события progress what="model", по
   * окончании — событие settings с новым model_dir и notice), иначе папка уже сменена, как
   * обычной set_settings (модель перезагрузится оттуда или скачается туда).
   */
  model_dir_set: { args: { path: string; move: boolean }; result: { ok: true; moving: boolean } };
  /** Модель под мощность компьютера (видеокарта, память, процессор) и список на выбор. */
  model_recommend: { args: void; result: ModelRecommend };
  history_list: {
    args: { query?: string; limit?: number; offset?: number };
    result: { items: HistoryItem[]; total: number };
  };
  history_delete: { args: { id: number }; result: null };
  history_clear: { args: { period: ClearPeriod }; result: { deleted: number } };
  history_reinsert: { args: { id: number }; result: null };
  last_insert: { args: void; result: null };
  recordings_list: {
    args: void;
    result: { items: RecordingItem[]; total_bytes: number; keep: number; max_bytes: number };
  };
  /** Распознать сохранённую запись заново: результат — в историю и «Вставить последнее». */
  recording_transcribe: { args: { id: string }; result: RetranscribeResult };
  recording_delete: { args: { id: string }; result: null };
  /** Звук записи для прослушивания: WAV целиком в base64. */
  recording_audio: { args: { id: string }; result: { mime: string; data: string } };
  recordings_clear: { args: void; result: { deleted: number } };
  /** Проводник: с id — выделить файл записи, без id — открыть папку записей. */
  recording_reveal: { args: { id?: string }; result: null };
  dict_list: { args: void; result: DictItem[] };
  dict_upsert: { args: { id?: number; term: string; variants: string[] }; result: DictItem };
  dict_delete: { args: { id: number }; result: null };
  /** Добавить правку в словарь (слить с существующим термином без дублей). */
  dict_learn_accept: { args: { term: string; variant: string }; result: DictItem };
  /** Отменить автодобавление: created — удалить термин целиком, иначе убрать только вариант. */
  dict_learn_undo: { args: LearnData; result: null };
  /**
   * Журнал обучения — что попало в словарь из правок, свежие сверху (limit 0–300, по
   * умолчанию 50). Отмена записи — dict_learn_undo с её term, variant, id, created.
   */
  dict_learn_journal: { args: { limit?: number }; result: LearnJournalEntry[] };
  snippets_list: { args: void; result: Snippet[] };
  snippet_upsert: { args: { id?: number; trigger: string; text: string }; result: Snippet };
  snippet_delete: { args: { id: number }; result: null };
  export_data: { args: void; result: ExportData };
  import_data: {
    args: { data: ExportData; replace: boolean };
    result: { dictionary: number; snippets: number };
  };
  stats_get: { args: void; result: Stats };
  draft_get: { args: void; result: { text: string } };
  draft_set: { args: { text: string }; result: null };
  styles_list: { args: void; result: StyleInfo[] };
  app_rules_list: { args: void; result: AppRule[] };
  app_rule_upsert: { args: AppRule; result: AppRule };
  app_rule_delete: { args: { process: string }; result: null };
  title_rules_list: { args: void; result: TitleRule[] };
  title_rule_upsert: { args: TitleRule; result: TitleRule };
  title_rule_delete: { args: { process: string; title: string }; result: null };
  recent_apps: { args: void; result: RecentApp[] };
  rdp_windows: { args: void; result: RdpWindow[] };
  /** {title, included} ядро тоже принимает (совместимость), интерфейс шлёт key. */
  rdp_toggle: { args: { key: string; included: boolean }; result: null };
  vocab_list: { args: { process?: string; limit?: number }; result: VocabItem[] };
  vocab_exclude: { args: { word: string; excluded: boolean }; result: null };
  ai_usage_stats: { args: void; result: AiUsageStats };
  ai_balance: { args: { force?: boolean }; result: AiBalance };
  ai_key_status: { args: { provider: KeyProvider }; result: KeyStatus };
  ai_key_import_env: { args: { path?: string }; result: { imported: boolean; masked: string | null } };
  ai_key_set: { args: { provider: KeyProvider; key: string }; result: { masked: string } };
  ai_key_delete: { args: { provider: KeyProvider }; result: null };
  ai_key_check: { args: void; result: KeyCheck };
  ai_keys_list: { args: void; result: AiKeyItem[] };
  ai_key_add: {
    args: { provider: KeyProvider; key: string; label?: string; balance_usd?: number | null };
    result: AiKeyItem;
  };
  ai_key_update: {
    args: { id: string; label?: string; enabled?: boolean; balance_usd?: number | null };
    result: AiKeyItem;
  };
  ai_key_replace: { args: { id: string; key: string }; result: AiKeyItem };
  ai_key_remove: { args: { id: string }; result: null };
  ai_keys_reorder: { args: { ids: string[] }; result: AiKeyItem[] };
  ai_key_info: { args: { id: string; force?: boolean }; result: AiKeyInfo };
  ai_test: { args: { text: string; style: string }; result: AiTestResult };
  open_logs: { args: void; result: null };
}

export type CommandName = keyof CommandMap;
export type CommandArgs<C extends CommandName> = CommandMap[C]["args"];
export type CommandResult<C extends CommandName> = CommandMap[C]["result"];

export interface RecordingEvent {
  started_at: number;
  broadcast: boolean;
  command: boolean;
  hands_free: boolean;
  /** Ключи RDP-сессий, куда уйдёт текст (broadcast=true), иначе []. */
  targets: string[];
}

export interface ResultEvent {
  text: string;
  raw: string;
  language: string;
  audio_s: number;
  asr_ms: number;
  latency_ms: number;
  ai: boolean;
  style: string;
}

/** Сохранённая запись голоса (последние N на диске, см. recordings_keep). Текста в ней нет. */
export interface RecordingItem {
  /** Имя файла без расширения: 20261107-183305-581. */
  id: string;
  /** Unix-время начала записи, с. */
  ts: number;
  duration_s: number;
  bytes: number;
  /** Процесс окна, куда диктовали ("" — неизвестно). */
  app: string;
  /** pending — распознаётся (или ядро упало посреди), ok — текст есть, empty — речи нет, error — сбой. */
  status: "pending" | "ok" | "empty" | "error";
  /** Сколько символов распознано. */
  chars: number;
  /** Запись истории с этим текстом (null — нет: история выключена или текста не было). */
  history_id: number | null;
}

export interface RetranscribeResult {
  id: string;
  /** После словаря, сниппетов и ИИ; "" — речи нет. */
  text: string;
  raw: string;
  language: string;
  duration_s: number;
  ai: boolean;
}

export type NoticeLevel = "info" | "warn" | "error";
export type NoticeAction =
  | null
  | "relaunch_admin"
  | "open_settings_ai"
  | "open_settings_insert"
  | "open_recordings"
  | "dict_undo"
  | "dict_add";

/** Правка, которую словарь выучил (dict_undo) или предлагает выучить (dict_add). */
export interface LearnData {
  /** Как надо: исправленное слово (термин словаря). */
  term: string;
  /** Как было распознано (вариант термина). */
  variant: string;
  /** id термина в словаре; null — термина ещё нет (dict_add). */
  id: number | null;
  /** Термин создан этой правкой (отмена удалит его целиком). */
  created: boolean;
}

export interface NoticeEvent {
  text: string;
  level: NoticeLevel;
  action: NoticeAction;
  toast: boolean;
  /** Только для action dict_undo / dict_add. */
  data?: LearnData | null;
}

export interface ProgressEvent {
  what: DownloadWhat;
  pct: number;
  text: string;
  done: boolean;
  error: string | null;
}

/** События сервера (без id). */
export interface EventMap {
  hello: CoreState;
  state: CoreState;
  level: { rms: number };
  recording: RecordingEvent;
  broadcast: { on: boolean };
  result: ResultEvent;
  history: HistoryItem;
  notice: NoticeEvent;
  progress: ProgressEvent;
  settings: Settings;
  ai_usage: AiUsageEvent;
}

export type EventName = keyof EventMap;

export type ConnStatus = "connecting" | "open" | "reconnecting" | "no-params";
