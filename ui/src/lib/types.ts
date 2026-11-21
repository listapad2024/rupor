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
  /** Перевод встречи: идёт ли сеанс и что с ним (как событие meeting). */
  meeting: MeetingStatus;
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

/** Слова-паразиты: одна фраза за период (fillers_stats.top). */
export interface FillerTop {
  phrase: string;
  count: number;
  per100: number;
  /** Та же фраза на 100 слов в предыдущем таком же периоде; null — сравнивать не с чем. */
  prev_per100: number | null;
}

export interface FillerDay {
  date: string;
  words: number;
  fillers: number;
  per100: number;
}

/** Статистика паразитов по сырому тексту распознавания (до словаря и ИИ-очистки). */
export interface FillerStats {
  enabled: boolean;
  /** 7, 30, 90 или 0 — всё время. */
  days: number;
  words: number;
  dictations: number;
  fillers: number;
  per100: number;
  prev_per100: number | null;
  top: FillerTop[];
  /** По дням (не больше 90 последних дней периода). */
  daily: FillerDay[];
  /** Первый день со статистикой (ISO) или null. */
  since: string | null;
}

export interface FillerConfig {
  enabled: boolean;
  phrases: string[];
  defaults: string[];
}

export interface FillerSuggestion {
  phrase: string;
  /** Сколько раз фраза нашлась в разобранных диктовках (посчитано локально). */
  count: number;
  example: string;
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
  /** Голосовые чаты (live chat Codex, ChatGPT) не слышат диктовку: на время записи Рупор выключает им микрофон их же кнопкой; по умолчанию true. */
  app_mute_enabled: boolean;
  /** Приложения (имена exe); по умолчанию ["codex.exe", "chatgpt.exe"] (десктопный Codex — ChatGPT.exe). */
  app_mute_apps: string[];

  sounds: boolean;
  autostart: boolean;

  default_style: string;
  history_enabled: boolean;
  history_retention_days: number;
  /** Сколько последних записей голоса хранить на диске (0 — не сохранять), 0–10000; по умолчанию 30. */
  recordings_keep: number;
  /** Сколько места записи голоса могут занять всего, МБ (20–20000); по умолчанию 300. */
  recordings_max_mb: number;
  /**
   * Теневой счёт «свой / чужой» по голосу: после каждой диктовки ядро сравнивает голос с
   * профилем и пишет счёт в журнал (%APPDATA%\Rupor\speaker\journal.jsonl); на диктовку не влияет.
   */
  speaker_shadow: boolean;
  /** Счёт (шкала AS-norm), от которого метка журнала — «свой»; −50…50, по умолчанию 5,5. */
  speaker_own_score: number;
  /** Счёт, ниже которого метка журнала — «чужой»; −50…50, по умолчанию 3. */
  speaker_alien_score: number;
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

  // ——— Перевод встречи ———
  /** На какой язык переводить речь собеседников (по умолчанию "ru"). */
  meeting_target_lang: MeetingLang;
  /** Язык собеседников: auto — определять по каждой фразе, иначе — всегда этот. */
  meeting_source_lang: "auto" | MeetingLang;
  /** false — только расшифровка: перевод ИИ не вызывается (название записи — meeting_ai_title). */
  meeting_translate: boolean;
  /** Показывать оригинал под переводом. */
  meeting_show_original: boolean;
  /** Размер текста в окне перевода, 12–28. */
  meeting_font_size: number;
  /**
   * Замечать встречу и предлагать перевод (событие meeting_suggest): приложение держит
   * микрофон ≥ 10 с и играет звук. false — не следить вовсе.
   */
  meeting_autosuggest: boolean;
  /**
   * Встреча кончилась (программа отпустила микрофон ≥ 15 с и речи нет ≥ 30 с; у «всего звука
   * компьютера» — 3 минуты тишины) — через 10 с остановить перевод самому (MeetingStatus.hint
   * "ending" с отсчётом ending_in_s; «Не останавливать» — meeting_autostop_cancel).
   */
  meeting_autostop: boolean;
  /** Подстроки заголовков окон встреч (без учёта регистра), до 30 штук по ≤ 60 символов — для названия встречи. */
  meeting_title_patterns: string[];
  /** Предлагать и для настольных приложений встреч (Zoom, Teams, Telegram, Discord…), не только браузеров. */
  meeting_detect_desktop_apps: boolean;
  /** Не предлагать для этих exe (имя файла, без учёта регистра); «Не предлагать для …» добавляет сюда. */
  meeting_ignore_apps: string[];
  /** Фраза кончилась — перевести её целиком ещё раз и заменить пословный перевод (MeetingLine.refined). */
  meeting_refine: boolean;
  /** Сохранять расшифровку каждого сеанса (Markdown). */
  meeting_save: boolean;
  /**
   * Называть запись по содержанию: короткое название по расшифровке придумывает ИИ (провайдер
   * ai_provider, расход — ai_usage вида title) после 8 строк и 90 с встречи и ещё раз после
   * «Стоп». false, нет ключа или ошибка — название без ИИ (окно встречи, первые слова).
   */
  meeting_ai_title: boolean;
  /**
   * Заметки к встрече: после «Стоп» (встреча от 5 строк или 2 минут речи) модель посильнее пишет
   * «Кратко», решения, задачи, открытые вопросы и ключевые моменты; их название — название
   * записи. Расход — ai_usage вида notes. Сделать заметки по кнопке можно и при false.
   */
  meeting_notes: boolean;
  /**
   * Модель заметок у провайдера ai_provider; "" — сильная по умолчанию: OpenRouter —
   * anthropic/claude-sonnet-5.5, Anthropic — claude-sonnet-5-5, Ollama — ollama_model.
   */
  meeting_notes_model: string;
  /** Папка расшифровок; "" — %APPDATA%\Rupor\meetings. Путь — абсолютный. */
  meeting_dir: string;
  /** Прятать окно перевода от захвата экрана (демонстрация экрана на встрече). */
  meeting_protect: boolean;
  /**
   * Окно перевода [x, y, ширина, высота] в физических пикселях (пишут оболочка и окно);
   * null — у правого края рабочей области, на всю высоту, ширина 420 логических px.
   */
  meeting_rect: [number, number, number, number] | null;
  /**
   * Компактная полоска окна перевода («Свернуть»): левый верхний угол её видимой части [x, y]
   * в физических пикселях (пишет окно после переноса); null — справа внизу рабочей области.
   */
  meeting_mini_pos: [number, number] | null;
  /**
   * Модель для перевода (у провайдера ai_provider); "" — та же, что для очистки у этого
   * провайдера (ai_model, anthropic_model или ollama_model).
   */
  translate_model: string;
  /** Сколько ждать перевод одной фразы, с (1–30). */
  translate_timeout_s: number;
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

/**
 * translate — перевод встречи, title — название записи встречи (meeting_ai_title), notes —
 * заметки к ней (meeting_notes; у длинной встречи — несколько вызовов: части и сведение).
 */
export type AiUsageKind = "cleanup" | "command" | "test" | "translate" | "title" | "notes";

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
  fillers_stats: { args: { days?: number }; result: FillerStats };
  fillers_config: { args: void; result: FillerConfig };
  /** phrases — весь список целиком (нормализуется ядром), enabled — считать ли. */
  fillers_config_set: { args: { phrases?: string[]; enabled?: boolean }; result: FillerConfig };
  fillers_defaults: { args: void; result: FillerConfig };
  /** Пересчитать по истории текущим списком (дни без истории не трогаются). */
  fillers_rebuild: { args: void; result: { days: number; dictations: number } };
  fillers_reset: { args: void; result: null };
  /** ИИ ищет личные обороты в свежих диктовках (платно, по кнопке). */
  fillers_ai_suggest: { args: void; result: { items: FillerSuggestion[]; sampled: number } };
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
  /** Откуда можно слушать встречу: процессы со звуком (по корневому процессу) и весь звук. */
  meeting_sources: { args: void; result: AudioSource[] };
  /**
   * Начать перевод встречи. Без source_id (или "auto") — лучший источник встречи, иначе
   * активный браузер, иначе ошибка. Уже идёт с другим источником — перезапуск на новом.
   * Модель не загружена — загружается, как перед диктовкой (долгая команда).
   */
  meeting_start: { args: { source_id?: string }; result: MeetingStatus };
  meeting_stop: { args: void; result: MeetingStatus };
  /**
   * «Не останавливать»: снять отсчёт автоостановки идущей встречи (до нового использования
   * микрофона или речи). Встречи нет — просто статус.
   */
  meeting_autostop_cancel: { args: void; result: MeetingStatus };
  meeting_status: { args: void; result: MeetingStatus };
  /** Строки текущего (или последнего) сеанса с id > after_id, по возрастанию id; limit 1–500 (200). */
  meeting_lines: { args: { after_id?: number; limit?: number }; result: MeetingLine[] };
  /**
   * В буфер обмена силами ядра (окно перевода без фокуса): transcript (по умолчанию) — вся
   * расшифровка (перевод и оригинал), notes — заметки (Markdown), all — заметки и расшифровка.
   * Копировать нечего — ошибка.
   */
  meeting_copy: { args: { what?: MeetingCopyWhat }; result: { chars: number } };
  /**
   * Сделать заметки к встрече сейчас (идущей или последней): ответ сразу (status running,
   * уже пишутся — тоже их), готовые и ошибка — событием meeting_notes. Встречи не было или
   * расшифровка пуста — ошибка.
   */
  meeting_notes: { args: void; result: MeetingNotes };
  /** Заметки идущей (или последней) встречи; null — их не было. */
  meeting_notes_get: { args: void; result: MeetingNotes | null };
  /** Открыть папку расшифровок (или выделить файл текущего сеанса) в проводнике. */
  meeting_open_folder: { args: void; result: null };
  /**
   * Ответ на предложение meeting_suggest: start — «Начать расшифровку» (ядро начинает перевод
   * с источником предложения → MeetingStatus, долгая команда), later — «Не сейчас» (до конца
   * этого разговора не предлагать) → null, never — «Не предлагать для {app}» (exe — в
   * meeting_ignore_apps) → null. Предложение устарело или неизвестно — ошибка (later — null);
   * тогда интерфейс сам зовёт meeting_start с источником (или дописывает exe в настройку).
   */
  meeting_suggest_answer: {
    args: { id: string; answer: MeetingSuggestAnswer };
    result: MeetingStatus | null;
  };
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
  | "dict_add"
  /** «Похоже, идёт встреча…» — кнопка «Включить»: meeting_start {} и показать окно перевода. */
  | "meeting_start";

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
  /** Состояние перевода встречи — при каждой смене; level — не чаще 4 раз в секунду. */
  meeting: MeetingStatus;
  /**
   * Новая строка расшифровки и каждое её обновление по тому же id (выросли слова или
   * перевод, фраза кончилась, уточнён перевод, ошибка) — не чаще 10 раз в секунду на строку.
   */
  meeting_line: MeetingLine;
  /** Замечена встреча (приложение держит микрофон и играет звук) — предложить расшифровку. */
  meeting_suggest: MeetingSuggest;
  /**
   * Заметки к встрече: начали писаться, пишутся (markdown — начало ответа модели, не чаще
   * 2 раз в секунду), готовы, не удались; и когда название записи сменилось после них.
   */
  meeting_notes: MeetingNotes;
}

export type EventName = keyof EventMap;

// ——— Перевод встречи ———

/** Языки перевода встречи (meeting_target_lang / meeting_source_lang). */
export type MeetingLang =
  | "ru"
  | "en"
  | "de"
  | "fr"
  | "es"
  | "it"
  | "pt"
  | "pl"
  | "uk"
  | "zh"
  | "ja"
  | "ko"
  | "tr";

/**
 * Источник звука встречи. process — дерево процессов (браузер: звук играет дочерний процесс,
 * захват — по корневому); system — весь звук компьютера (устройство вывода по умолчанию).
 */
export interface AudioSource {
  /** "proc:<корневой pid>" или "system". */
  id: string;
  kind: "process" | "system";
  pid: number | null;
  exe: string;
  /** Понятное имя приложения (описание exe), для system — «Весь звук компьютера». */
  name: string;
  /** Заголовок самого подходящего окна (совпадающий с шаблонами встреч — в приоритете). */
  title: string;
  /** Заголовок совпал с meeting_title_patterns. */
  meeting: boolean;
  /** Звук сейчас играет. */
  active: boolean;
  /**
   * Звук этого источника можно захватить. false — у программы в Windows старше Windows 11 /
   * Server 2022 (сборка 20348): там захватывается только весь звук компьютера, окно перевода
   * показывает программу неактивной. Ядро присылает поле всегда (check-contract это сверяет);
   * интерфейс без него считает, что можно.
   */
  available?: boolean;
}

/**
 * idle — сеанса нет; listening — слушаем; waiting_gpu — распознавание ждёт видеокарту (идёт
 * диктовка); no_audio — 20 с полной тишины в источнике; error — сеанс упал (error — причина).
 */
export type MeetingState = "idle" | "listening" | "waiting_gpu" | "no_audio" | "error";

export interface MeetingStatus {
  running: boolean;
  source: AudioSource | null;
  /** Начало сеанса (локальное время, ISO) или null. */
  started_at: string | null;
  /** Строк в сеансе. */
  lines: number;
  /** Уровень звука 0–1 (сглаженный) — «звук есть». */
  level: number;
  state: MeetingState;
  error: string | null;
  /** Переводит ли сеанс (выключено, нет ключа ИИ — только расшифровка). */
  translate: boolean;
  target_lang: MeetingLang;
  /**
   * Файл расшифровки текущего сеанса или null (meeting_save выключено, строк ещё нет):
   * «YYYY-MM-DD HH-MM — <title>.md»; новое название — файл переименован (событие meeting).
   */
  file: string | null;
  /**
   * Название записи: по содержанию (ИИ, meeting_ai_title) или без ИИ — заголовок окна встречи
   * («Google Meet»), «Встреча — <первые слова>», имя программы. null — сеанса не было.
   */
  title: string | null;
  /** Средние задержки сеанса (окно перевода показывает их в подвале). */
  latency: MeetingLatency;
  /**
   * Подсказка по идущему сеансу: ended — встреча, похоже, закончилась (приложение больше 30 с
   * не держит микрофон и не играет звук; кнопка «Стоп»); ending — встреча кончилась и сеанс
   * остановится сам через ending_in_s (meeting_autostop; «Не останавливать» —
   * meeting_autostop_cancel). null — нет.
   */
  hint: MeetingHint | null;
  /** Секунд до автоостановки (hint "ending"), иначе null. */
  ending_in_s: number | null;
  /**
   * Можно захватить звук одной программы (Windows 11 / Server 2022, сборка 20348). false —
   * только весь звук компьютера: окно перевода берёт его по умолчанию, программы в выборе
   * источника неактивны, а встреча с программой (автовыбор, предложение) идёт по всему звуку.
   * Ядро присылает поле всегда (check-contract это сверяет); интерфейс без него считает, что
   * можно.
   */
  loopback_available?: boolean;
  /** Заметки к встрече (без текста — он в событии meeting_notes и meeting_notes_get); null — не было. */
  notes: MeetingNotesInfo | null;
}

export type MeetingHint = "ended" | "ending";

/** running — модель пишет заметки; done — готовы; error — не удались (error — причина). */
export type MeetingNotesStatus = "running" | "done" | "error";

/** Что копировать (meeting_copy). */
export type MeetingCopyWhat = "transcript" | "notes" | "all";

/** Заметки к встрече без текста (MeetingStatus.notes). */
export interface MeetingNotesInfo {
  status: MeetingNotesStatus;
  /** Когда готовы последние удачные (ISO со смещением) или null. */
  generated_at: string | null;
  /** Модель последних удачных или null. */
  model: string | null;
  /** Сколько они стоили (все вызовы), $; null — неизвестно или их нет. */
  cost_usd: number | null;
  /** Почему не удались (status error) или null. */
  error: string | null;
  /** Написаны сами после «Стоп» (true) или по кнопке. */
  auto: boolean;
}

/**
 * Заметки к встрече (событие meeting_notes, meeting_notes_get, meeting_notes). markdown:
 * пока пишутся — начало ответа модели (или null), потом — последние удачные заметки: первая
 * строка «# <название записи>», дальше разделы «## …» (кратко, решения, задачи «- [ ] кто —
 * что — срок», открытые вопросы, ключевые моменты со временем строк ЧЧ:ММ:СС); null — их нет.
 */
export interface MeetingNotes {
  status: MeetingNotesStatus;
  markdown: string | null;
  generated_at: string | null;
  model: string | null;
  cost_usd: number | null;
  error: string | null;
  auto: boolean;
}

/**
 * Средние задержки строк сеанса, мс от конца сказанного: asr_ms — текст на экране,
 * first_token_ms — первые слова перевода, done_ms — перевод целиком. null — ещё не измерено
 * (нет строк или перевода).
 */
export interface MeetingLatency {
  asr_ms: number | null;
  first_token_ms: number | null;
  done_ms: number | null;
}

/**
 * pending — перевод ещё не начался; streaming — перевод приходит по словам; done — переведено;
 * original — перевод не нужен (речь уже на языке перевода или перевод выключен); error —
 * перевести не удалось (error — причина).
 */
export type MeetingLineStatus = "pending" | "streaming" | "done" | "original" | "error";

/**
 * Строка (фраза) расшифровки. Пока говорят, одна и та же строка (тот же id) обновляется:
 * text растёт подтверждёнными словами, partial — хвост, который ещё может поменяться,
 * translation растёт по словам; фраза кончилась — final; переведена целиком заново — refined.
 */
export interface MeetingLine {
  /** 1, 2, … внутри сеанса. */
  id: number;
  /** Секунды от начала сеанса. */
  t: number;
  /** Местное время «ЧЧ:ММ:СС». */
  ts: string;
  /** Язык фразы (код Whisper). */
  lang: string;
  /** Подтверждённый текст фразы (дальше только растёт; в конце фразы его может уточнить финальное распознавание). */
  text: string;
  /** Неподтверждённый хвост после text (ещё может поменяться), "" — нет. */
  partial: string;
  /** Фраза кончилась (пауза в речи): text окончательный, partial пустой. */
  final: boolean;
  /** Перевод заменён переводом всей фразы целиком (meeting_refine). */
  refined: boolean;
  translation: string | null;
  status: MeetingLineStatus;
  error?: string;
}

export type MeetingSuggestAnswer = "start" | "later" | "never";

/** Предложение начать расшифровку: замеченная встреча. */
export interface MeetingSuggest {
  /** id предложения — для meeting_suggest_answer. */
  id: string;
  /** Откуда слушать (meeting_start {source_id: source.id}). */
  source: AudioSource;
  /** Приложение: «Brave», «Zoom»… */
  app: string;
  /** Название встречи: «Google Meet», заголовок окна… ("" — неизвестно). */
  title: string;
  /** Когда замечена (локальное время, ISO). */
  detected_at: string;
}

export type ConnStatus = "connecting" | "open" | "reconnecting" | "no-params";
