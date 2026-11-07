# Рупор — архитектура и контракты (этапы 3–6)

Этот документ — **единственный источник правды** для всех, кто пишет код. Если реализация
расходится с документом — прав документ; если документ неполон — его нужно дополнить.

## Процессы

```
rupor.exe (Tauri 2, Rust)  ── spawn ──►  ядро (Python)
  ├─ окно main  (index.html)  ─┐           ├─ хук клавиатуры, микрофон, faster-whisper
  ├─ окно pill  (pill.html)   ─┤ WS        ├─ вставка текста, RDP-рассылка
  └─ трей                      └─────────► ├─ SQLite, ИИ (OpenRouter/Anthropic/Ollama/claude -p)
                                            └─ WebSocket-сервер 127.0.0.1:<port>
```

- Rust выбирает свободный порт (bind `127.0.0.1:0` → взять порт → закрыть) и случайный токен
  (32 hex), запускает ядро с аргументами `--sidecar --ws-port <p> --token <t> --ui-pid <pid>`.
- Как найти ядро (по порядку):
  1. переменная окружения `RUPOR_CORE` (полная командная строка через пробел — только для отладки);
  2. `<resource_dir>/core/rupor-core.exe` (установленная версия, PyInstaller onedir);
  3. только debug-сборка: `<CARGO_MANIFEST_DIR>/../../.venv/Scripts/python.exe -m rupor <args>`
     с рабочей папкой `<CARGO_MANIFEST_DIR>/../../core`.
- Ядро запускается с флагом `CREATE_NO_WINDOW` (0x08000000); stdout/stderr → `Stdio::null()`
  (логи ядро пишет само в `%APPDATA%\Rupor\logs`).
- Для тестов: `--no-hook` (не ставить хук клавиатуры — нажатия на настоящей клавиатуре,
  в том числе правый Ctrl, не должны запускать тестовое ядро) и `--no-mutex` (не проверять единственный
  экземпляр). Режим `--sidecar` без `--ui-pid` не следит за родителем.
- Ядро следит за `--ui-pid`: если процесс UI умер — ядро завершается.
- Rust следит за ядром: если процесс ядра завершился не по команде «Выход» — перезапуск с
  паузой 1 с / 3 с / 10 с (сброс счётчика после 60 с стабильной работы), событие `core-restarted`
  во все окна. Порт и токен при перезапуске новые — окна снова зовут `backend_info`.
- При выходе из приложения (трей «Выход», `quit_app`, перезапуск от администратора,
  `RunEvent::Exit`) Rust даёт ядру завершиться самому (`Supervisor::shutdown`): готовому ядру —
  WS-команда `quit` (свой минимальный клиент: рукопожатие с токеном, кадр команды, ответ
  `{"id":1,"ok":true…}`, кадр закрытия — соединение, не ответившее на закрытие, сервер ядра при
  выходе ждал бы до 3 с), ожидание выхода процесса до 2,5 с и только потом `child.kill()`
  (не готово, не ответило или не вышло). Обычно ядро выходит за ~0,4 с. Ядро по `quit` сразу
  начинает возвращать звук компьютера, приглушённый записью, и ждёт этого до 2 с
  (`Ducker.close()`); после `kill` посреди записи звук вернётся только при следующем запуске
  ядра (`duck.json`). Остальное ядро при `kill` не теряет (SQLite в WAL, настройки пишутся
  сразу). Конец сеанса Windows (выход из системы, выключение) ядро ловит и само — скрытым
  окном (WM_QUERYENDSESSION, см. «Звук компьютера во время записи»).

## WebSocket-протокол

URL: `ws://127.0.0.1:<port>/?token=<token>`. Без верного токена сервер закрывает соединение
с кодом 4401. Все сообщения — JSON-объекты в UTF-8. Сервер принимает соединения только с
`127.0.0.1`. Рукопожатие с заголовком `Origin` не из списка (окна Tauri `http(s)://tauri.localhost`,
`tauri://localhost`, dev-сервер `http://localhost:1420`/`1421`, а также без `Origin` — Rust-проба,
тесты) отклоняется с HTTP 403: страницы в браузере до ядра не достучатся. Дополнительные
адреса — переменная окружения `RUPOR_WS_ORIGINS` (через запятую). Отказы по токену пишутся
в лог не чаще раза в 30 с.

Запрос клиента: `{"id": 17, "cmd": "history_list", "args": {...}}`
Ответ: `{"id": 17, "ok": true, "result": ...}` или `{"id": 17, "ok": false, "error": "текст на языке интерфейса"}`
(язык — `State.ui_language`, см. «Локализация»)
Событие сервера (без id): `{"event": "state", "data": {...}}`

Сразу после подключения сервер сам присылает событие `hello` с тем же содержимым, что `get_state`,
а за ним — события `notice` уровня warn/error, выданные за последние 60 с, пока ни один
клиент не был подключён (откат на CPU при старте, «Микрофон недоступен», «База истории
была повреждена»): ядро стартует раньше, чем подключаются окна.

Команды одного соединения выполняются **по порядку** (например, `mic_monitor` false→true,
быстрые `set_settings` от ползунка). Долгие (`ai_test`, `ai_key_check`, `ai_balance`,
`test_record_stop`, `cuda_check`, `model_recommend`, `model_dir_set` — последняя создаёт папку
и пишет в неё пробный файл, на недоступном сетевом пути это десятки секунд) тоже ждут
предыдущие команды, но следующие команды их не ждут.
Команды, которые читают и меняют настройки (`rdp_toggle`, `app_rule_*`, `title_rule_*`), делают
это атомарно под замком настроек.

### Команды

| cmd | args | result |
|---|---|---|
| `get_state` | — | `State` (см. ниже) |
| `get_settings` | — | объект настроек целиком (как `settings.json`) |
| `set_settings` | `{patch: {...}}` | новый объект настроек. Валидируется; хоткеи, микрофон, режимы применяются сразу без перезапуска. Смена `model`/`device`/`compute_type`/`model_dir` → перезагрузка модели в фоне; если новой модели нет на диске (в папке `model_dir`), прежняя распознаёт, пока новая качается (не скачалась — остаётся прежняя, notice). `model_dir` здесь только меняет папку, без переноса (перенос — `model_dir_set`). Файл не записался — в памяти тоже ничего не меняется (ошибка). Смена хоткеев или микрофона во время записи останавливает её с распознаванием (notice) |
| `pause` | `{on: bool}` | `State` |
| `quit` | — | `null`; ядро завершается через 200 мс |
| `list_devices` | — | `[{index, name, default: bool}]` |
| `mic_monitor` | `{on: bool}` | `null`; пока `on` — события `level` идут постоянно (для мастера) |
| `test_record_start` | — | `null`; запись без вставки (мастер, «проверить распознавание»). Не дольше 60 с; отменяется, если отключились все клиенты. Пока она идёт, диктовка не начинается (notice warn) |
| `test_record_stop` | — | `{text, language, audio_s, asr_ms}` |
| `cuda_check` | — | `{ok, device: "cuda"\|"cpu", gpu_name, vram_total_mb, error}` |
| `setup_status` | — | `{model_ready, cuda_ready, first_run_done, model_dir, model_dir_default}` — `model_ready`: модель `settings.model` целиком лежит в папке моделей; `model_dir` — полный путь папки, где Рупор ищет и куда качает модель сейчас (`settings.model_dir` или, если он пуст, кэш HuggingFace по умолчанию); `model_dir_default` — кэш HuggingFace по умолчанию (`HF_HUB_CACHE`, обычно `%USERPROFILE%\.cache\huggingface\hub`) |
| `setup_download` | `{what: "model"\|"cuda"}` | `null`; прогресс событиями `progress`. Модель качается в папку моделей (`model_dir`) |
| `model_dir_set` | `{path: string, move?: bool=false}` | `{ok: true, moving: bool}` сразу — сменить папку моделей (см. «Папка модели и подбор модели»). `path` проверяется как настройка `model_dir` (`""` — кэш по умолчанию; относительный путь — ошибка «Папка модели: нужен полный путь…»), папка внутри папки самой модели (`models--org--name`) или `.locks` нынешней папки — ошибка «Папку … выбрать нельзя: она внутри служебной папки модели…» (папка не создаётся); папка создаётся и проверяется запись в неё (нельзя — ошибка «Не удалось создать папку…» / «В папку … нельзя записывать…»). Та же запись, что сейчас, — ничего не делает (`moving: false`); та же папка, записанная иначе (явный путь кэша по умолчанию и `""`, другой регистр, junction, символьная ссылка, subst, сетевой диск и его UNC-путь — `fetch.same_dir`), — только сохраняется новая запись, без переноса и удаления (`moving: false`, событие `settings`). `move=true` и модель есть в нынешней папке — `moving: true`, перенос в фоне: события `progress` `what: "model"` (как у загрузки), затем `settings` с новым `model_dir`, перезагрузка модели из новой папки, удаление старой копии и `notice` «Модель перенесена в …» (info, toast) или «Старую копию удалить не удалось: …» (warn); модель из новой папки не загрузилась — старая копия остаётся, `notice` «Модель не загрузилась из новой папки — старая копия оставлена: …» (warn). Ошибка переноса — `progress` с `done: true` и `error`, настройки и модель прежние. Иначе (`move=false` или переносить нечего) — как `set_settings {model_dir}`: модель есть в новой папке — перезагрузка оттуда, нет — качается туда. Во время переноса распознавание работает на загруженной модели; повторный `model_dir_set` — ошибка «Модель сейчас переносится или скачивается…» |
| `model_recommend` | — | `ModelRecommend` — какую модель предложить в мастере первого запуска по мощности компьютера (видеокарта, память, потоки процессора) и модели на выбор с размерами с Hugging Face. Не дольше ≈5 с: размеры, которые не успели прийти (нет сети), — запасные |
| `history_list` | `{query?: str, limit?: int=50, offset?: int=0}` | `{items: [HistoryItem], total: int}` |
| `history_delete` | `{id}` | `null` |
| `history_clear` | `{period: "day"\|"week"\|"month"\|"all"}` | `{deleted: int}` — удалить записи **за** последний период («за день» = последние 24 ч) |
| `history_reinsert` | `{id}` | `null` — активировать последнее «чужое» окно (не Рупора) и вставить `text` |
| `last_insert` | — | `null` — вставить последний результат в последнее чужое окно |
| `dict_list` | — | `[DictItem]` |
| `dict_upsert` | `{id?, term, variants: [str]}` | `DictItem` |
| `dict_delete` | `{id}` | `null` |
| `dict_learn_accept` | `{term, variant}` | `DictItem` — кнопка «Добавить» уведомления `dict_add`: добавить термин и вариант; термин уже есть (без учёта регистра и «ё») — вариант сливается с ним без дублей, написание термина не меняется. `variant` пустой/не задан или равен термину — только термин. Вариант, который уже ведёт к другому термину, — ошибка «Вариант «…» уже относится к термину «…»» |
| `dict_learn_undo` | `{term, variant, id, created}` | `null` — кнопка «Отменить» уведомления `dict_undo` и строки «Недавно добавлено» (`dict_learn_journal`): убрать этот вариант; `created=true` — термин, созданный этой правкой, удаляется целиком, если в нём не осталось других вариантов (выученных позже или добавленных вручную) и его написание не меняли, иначе остаётся без этого варианта. Отмена одноразовая: откатывается только то, что записано в журнале обучения (`kv.learn_journal`, по `term`+`variant`+`created`, при нескольких — с этим `id`), запись журнала стирается; повторная отмена, правка, которая ничего не меняла, термин, удалённый вручную, — ничего не делают и не ошибка. Отменённую правку ядро больше не выучивает. `created` обязателен (true/false) |
| `dict_learn_journal` | `{limit?: int=50}` | `[LearnJournalEntry]` — «Недавно добавлено» на странице «Словарь»: что словарь выучил (журнал обучения `kv.learn_journal`: добавил Рупор сам или пользователь кнопкой «Добавить»), новые первыми, не больше `limit`. Записи, чья запись словаря удалена или переименована (отменять нечего), не показываются. «Отменить» в строке — `dict_learn_undo` с `{term, variant, id, created}` этой записи: запись журнала стирается и при следующем запросе строки нет. `limit` вне 0–300 — ошибка «limit: допустимо от 0 до 300» |
| `snippets_list` | — | `[Snippet]` |
| `snippet_upsert` | `{id?, trigger, text}` | `Snippet` |
| `snippet_delete` | `{id}` | `null` |
| `export_data` | — | `{version: 1, dictionary: [{term, variants}], snippets: [{trigger, text}]}` |
| `import_data` | `{data: <как export_data>, replace: bool}` | `{dictionary: int, snippets: int}` (сколько добавлено/обновлено) |
| `stats_get` | — | `Stats` |
| `draft_get` | — | `{text}` |
| `draft_set` | `{text}` | `null` |
| `styles_list` | — | `[{id, name, description}]` (5 встроенных, см. ИИ); `name` и `description` — на языке интерфейса |
| `app_rules_list` | — | `[AppRule]` |
| `app_rule_upsert` | `AppRule` | `AppRule` |
| `app_rule_delete` | `{process}` | `null` |
| `title_rules_list` | — | `[TitleRule]` — правила по названию окна (по процессу и названию) |
| `title_rule_upsert` | `TitleRule` | `TitleRule` (нормализованное). То же правило (процесс + название без учёта регистра и «ё») заменяется. Название короче 2 символов, неверный режим/стиль/процесс — ошибка |
| `title_rule_delete` | `{process, title}` | `null` (без учёта регистра; нет такого — не ошибка) |
| `recent_apps` | — | `[{process, title, titles: string[]}]` — до 20 последних различных процессов, куда был фокус (новые первыми); `titles` — до 10 последних различных заголовков окон процесса, свежие первыми (только в памяти, на диск не пишутся) |
| `rdp_windows` | — | `[{key, title, process, included: bool}]` — открытые окна RDP-клиентов; `key` — ключ сессии (см. «Рассылка в RDP»), `included` — ключ есть в `broadcast_targets` |
| `rdp_toggle` | `{key, included: bool}` (совместимость: `{title, included}`) | `null` — отметить/снять сессию для рассылки (`broadcast_targets`, без учёта регистра). По `title` ключ берётся у открытого окна с таким заголовком, иначе по правилу mstsc |
| `ai_keys_list` | — | `[AiKeyItem]` — список ключей ИИ по приоритету (см. «Список ключей ИИ»): запись `settings.ai_keys` + `title`, `has_key`, `masked`, расход Рупора через ключ (`spent_usd`, `calls`, `spent_since_usd`), пауза после ошибки (`paused_until`, `paused_reason`). Без сети |
| `ai_key_add` | `{provider, key, label?, balance_usd?}` | `AiKeyItem` — новый ключ в конец списка; секрет — в Credential Manager. Первый ключ провайдера получает цель `Rupor/<провайдер>`, следующие — `Rupor/<провайдер>/<id>` |
| `ai_key_update` | `{id, label?, enabled?, balance_usd?: number\|null}` | `AiKeyItem` — подпись, вкл/выкл, остаток, введённый вручную (`null` — убрать; время ввода — `balance_at`) |
| `ai_key_replace` | `{id, key}` | `AiKeyItem` — заменить секрет ключа |
| `ai_key_remove` | `{id}` | `null` — убрать ключ из списка и его секрет из Credential Manager |
| `ai_keys_reorder` | `{ids: [str]}` | `[AiKeyItem]` — новый порядок (= приоритет); `ids` — все id списка ровно по разу, иначе ошибка |
| `ai_key_info` | `{id, force?: bool}` | `AiKeyInfo` — проверка и баланс ключа: OpenRouter — баланс по API (`balance_source: "api"`), Anthropic — проверка `GET /v1/models`, баланса по API нет: введён остаток — оценка «остаток − расход с момента ввода» (`"estimate"`), иначе `error` «не отдаёт баланс по API». Кэш проверки 5 мин (неудачной — минута) |
| `ai_key_status` | `{provider: "openrouter"\|"anthropic"}` | `{has_key, masked}` — `masked` вида `sk-or-v1-…XYZ` (ключ по прежней цели `Rupor/<провайдер>`) |
| `ai_key_import_env` | `{path?}` | `{imported: bool, masked}` — читает `openrouter_api_key` из `.env` (по умолчанию `settings.env_path`) и кладёт в Credential Manager |
| `ai_key_set` | `{provider, key}` | `{masked}` — ключ по прежней цели `Rupor/<провайдер>`; записи нет в списке ключей — она добавляется в конец (мастер первого запуска, импорт из `.env`) |
| `ai_key_delete` | `{provider}` | `null` — и запись `<провайдер>` из списка ключей |
| `ai_key_check` | — | `{valid, label, balance_usd, usage_usd, limit_usd, error}` — OpenRouter `GET /api/v1/key` + `GET /api/v1/credits` |
| `ai_test` | `{text, style}` | `{text, ms, cost_usd, provider, model}` — прогнать текст через очистку, как в диктовке: словарь → ИИ → снова словарь (вызов пишется в расход ИИ как `test`) |
| `ai_usage_stats` | — | `AiUsageStats` — расход ИИ (см. «Расход ИИ и баланс») |
| `ai_balance` | `{force?: bool}` | `AiBalance` — баланс ключа, который сейчас первым в очереди (очистки, если ИИ включён, иначе командного режима): то же, что `ai_key_info` этого ключа, + `key_id`, `provider`, `label`, `estimate`. Облачные ключи не используются (Ollama, Claude Code) — в сеть не ходит (`error` «Облачные ключи не используются…»); ключей нет — `error` «Ключей ИИ нет…» |
| `vocab_list` | `{process?: str, limit?: int=50}` | `[{word, count, excluded}]` — личный словарь частот: до `limit` слов, которые идут в подсказку (достаточно частые), частые первыми; после них — все исключённые пользователем слова (и за пределом `limit`, и без частот — `count: 0`; для `process` — только встречавшиеся в нём). Падежные формы слиты («Тимуру», «Тимура» → «Тимур»). `process` — только этот процесс (без учёта регистра), `limit` ≤ 500 |
| `vocab_exclude` | `{word, excluded: bool}` | `null` — исключить слово из подсказок или вернуть (без учёта регистра, «ё» и формы слова: «Тимуру» исключает и «Тимура», вернуть можно любой формой) |
| `open_logs` | — | `null` (открыть папку логов в проводнике) |
| `relaunch_admin` | — | не используется UI — перезапуск делает Rust-команда |

### Типы

```ts
type State = {
  state: "loading" | "idle" | "recording" | "hands_free" | "processing" | "ai" | "paused" | "error";
  paused: boolean;
  device: "cuda" | "cpu" | null;     // null — модель ещё не загружена
  model_loaded: boolean;
  insert_mode: "auto" | "type" | "clipboard";
  style: string;                      // стиль по умолчанию (id)
  ai_enabled: boolean;
  first_run_done: boolean;
  version: string;
  elevated: boolean;                  // ядро запущено от администратора
  hotkeys: { ptt: string; broadcast: string; command: string; cancel: string };
  ui_language: "ru" | "en";           // разрешённый язык интерфейса (settings.language,
                                      // "system" → язык Windows), см. «Локализация»
};
type HistoryItem = {
  id: number; ts: number /* unix сек */; app: string; title: string;
  raw: string; text: string; duration_s: number; words: number;
  language: string; style: string; ai: boolean; latency_ms: number; target: "window" | "rdp_all" | "test" | "command";
};
type DictItem = { id: number; term: string; variants: string[] };
type Snippet = { id: number; trigger: string; text: string };
// notice.data у уведомлений обучения словаря (action "dict_undo" / "dict_add")
type LearnNotice = {
  term: string;          // как исправил пользователь: «Metabase»
  variant: string;       // как было распознано: «метабейс»; "" — вариант не нужен (правка регистра/«ё»)
  id: number | null;     // dict_undo — запись словаря; dict_add — null
  created: boolean;      // dict_undo — запись создана этой правкой («Отменить» удалит термин целиком)
};
// dict_learn_journal — запись журнала обучения (что словарь выучил), новые первыми
type LearnJournalEntry = {
  id: number; term: string; variant: string; created: boolean;   // как data у dict_undo
  ts: number | null;                   // когда добавлено (unix сек); null — запись старой версии
  source: "auto" | "accepted" | null;  // auto — добавил Рупор сам, accepted — пользователь
                                       // нажал «Добавить»; null — запись старой версии
};
type AppRule = { process: string; insert_mode: "auto" | "type" | "clipboard" | null; style: string | null };
type TitleRule = {
  process: string;              // имя exe в нижнем регистре или "*" — любое окно
  title: string;                // подстрока заголовка окна (без учёта регистра и «ё»), ≥ 2 символов
  insert_mode: "auto" | "type" | "clipboard" | null;   // null — не переопределять
  style: string | null;                                // null — не переопределять
};
type Usage = { calls: number; tokens_in: number; tokens_out: number; cost_usd: number };
// у today/week/month/total ещё unknown_cost_calls: успешные вызовы с неизвестной ценой
// (claude -p по подписке, модель Anthropic не из таблицы цен) — в cost_usd их нет ($0),
// UI может написать «цена неизвестна», а не «$0»
type UsageSpan = Usage & { unknown_cost_calls: number };
type AiUsageStats = {
  today: UsageSpan; week: UsageSpan /* 7 дней, включая сегодня */; month: UsageSpan /* 30 дней */;
  total: UsageSpan;
  avg_cost_per_call: number;    // средняя стоимость успешного вызова с известной ценой
  daily: ({ date: string /* YYYY-MM-DD */ } & Usage)[];   // ровно 30 дней, дни без вызовов — нули
  by_kind: { kind: "cleanup" | "command" | "test"; calls: number; cost_usd: number }[];  // все три, за всё время
  by_model: { model: string; calls: number; cost_usd: number;
              unknown_cost_calls: number }[];  // за всё время, дорогие первыми
};
type AiBalance = {
  balance_usd: number | null; usage_usd: number | null; limit_usd: number | null;
  checked_at: number | null;    // unix, с; null — проверки не было (нет ключа, OpenRouter не используется)
  error: string | null;         // на языке интерфейса: «Ключ OpenRouter не задан — …»,
                                // «OpenRouter не используется (…) — баланс не проверяется»,
                                // ошибка сети/ключа; при смене языка кэш с ошибкой сбрасывается
};
type ModelRecommend = {
  gpu_name: string | null;      // первая видеокарта NVIDIA (NVML), null — нет
  vram_mb: number | null;       // её видеопамять, МБ; null — не определилась
  cuda_ok: boolean;             // CUDA (CTranslate2) видит видеокарту
  ram_gb: number;               // оперативная память, ГБ (0 — не определилась)
  cpu_threads: number;          // потоков процессора
  recommended: { model: string; device: "cuda" | "cpu"; compute_type: string };
  reason: string;               // почему — на языке интерфейса: «Видеокарта RTX 3060 Ti, 8 ГБ — …»
  options: {                    // tiny, base, small, medium, large-v3-turbo, large-v3 (по порядку)
    model: string; repo: string /* репозиторий Hugging Face */;
    size_mb: number;            // размер файлов модели, МБ по 10⁶ байт (как на Hugging Face)
    quality: 1 | 2 | 3 | 4 | 5; // точность распознавания русского
    speed: 1 | 2 | 3 | 4 | 5;
    recommended: boolean;       // ровно у одной — recommended.model
    note: string;               // пояснение на языке интерфейса
  }[];
};
type Stats = {
  words_today: number; words_week: number; words_total: number;
  wpm: number;              // средняя скорость диктовки: слова / минуты речи (за всё время)
  saved_minutes: number;    // words_total/40 − минуты речи (не меньше 0)
  streak_days: number;      // дней подряд с ≥1 диктовкой, включая сегодня (или вчера, если сегодня ещё не было)
  dictations_total: number;
  daily: { date: string /* YYYY-MM-DD */; words: number }[];  // последние 30 дней, включая дни с 0
};
```

### События

| event | data |
|---|---|
| `hello` | `State` |
| `state` | `State` (присылается при каждой смене состояния/паузы/настроек) |
| `level` | `{rms: number}` — ≤ 30 раз/с, пока идёт запись или `mic_monitor` |
| `recording` | `{started_at: number /* unix мс */, broadcast: bool, command: bool, hands_free: bool, targets: string[]}` — запись подтверждена (после 250 мс удержания); `targets` — ключи RDP-сессий, куда уйдёт текст (отмеченные и открытые), при `broadcast=true`, иначе `[]` |
| `broadcast` | `{on: bool}` |
| `result` | `{text, raw, language, audio_s, asr_ms, latency_ms, ai: bool, style}` |
| `history` | `HistoryItem` — добавлена запись |
| `notice` | `{text /* на языке интерфейса */, level: "info"\|"warn"\|"error", action: null \| "relaunch_admin" \| "open_settings_ai" \| "open_settings_insert" \| "dict_undo" \| "dict_add", toast: bool, data: LearnNotice \| null}` — `data` есть всегда: `null` у всех уведомлений, кроме обучения словаря (см. «Обучение словаря на правках»); `open_settings_insert` — открыть настройки вставки/рассылки («Не выбраны RDP-сессии для рассылки») |
| `ai_usage` | `{kind: "cleanup"\|"command"\|"test", cost_usd, tokens_in, tokens_out, today_cost_usd}` — после каждого вызова ИИ (и неудачного: стоимость 0); неизвестное — 0 |
| `progress` | `{what: "model"\|"cuda", pct: 0..100, text, done: bool, error: string\|null}` — загрузка (`setup_download`) или перенос модели в другую папку (`model_dir_set` с `move`, тоже `what: "model"`, текст «Переношу модель …: N из M МБ»). Размеры в текстах — МБ и ГБ по 10⁶ и 10⁹ байт, как `size_mb` в `model_recommend` и на Hugging Face: у модели в списке мастера и в прогрессе — одно число |
| `settings` | объект настроек (после `set_settings`) |

## Настройки (`%APPDATA%\Rupor\settings.json`)

Существующие ключи из `core/rupor/config.py` сохраняются. Добавляются:

```jsonc
{
  "command_modifier": "ralt",       // ptt + этот модификатор → командный режим
  "app_rules": {"telegram.exe": {"insert_mode": null, "style": "messenger"}},
                                    // старый формат {"x.exe": "type"} читать и конвертировать
  "default_style": "normal",
  "history_enabled": true,
  "history_retention_days": 0,      // 0 — хранить всегда
  "pill_enabled": true,
  "pill_position": null,            // [x, y] физические пиксели или null — снизу по центру
  "theme": "system",                // system | dark | light
  "accent": "#7c5cff",
  "first_run_done": false,
  "ai_enabled": false,
  "ai_provider": "keys",            // keys (список ключей по приоритету) | openrouter | anthropic (только ключи этого провайдера) | ollama
  "ai_keys": [                      // ключи ИИ по приоритету; секреты — в Credential Manager
    {"id": "anthropic", "provider": "anthropic", "label": "", "enabled": true,
     "balance_usd": 80.0, "balance_at": 1762500000.0}   // остаток, введённый вручную, и когда
  ],
  "ai_model": "anthropic/claude-haiku-4.5",
  "anthropic_model": "claude-haiku-4-5",
  "ollama_model": "qwen2.5:7b",
  "ollama_url": "http://127.0.0.1:11434",
  "ai_provider_order": [],          // OpenRouter provider.order, напр. ["amazon-bedrock","anthropic"]
  "ai_mode": "wait",                // wait | raw_first
  "ai_min_words": 6,
  "ai_timeout_s": 5,
  "command_provider": "openrouter", // openrouter | claude_cli
  "env_path": "",                   // путь к .env с openrouter_api_key (необязательно)
  "learn_mode": "auto",             // обучение словаря на правках: auto | suggest | off
  "dict_fuzzy": true,               // словарь: узнавать похожие написания терминов по звучанию
                                    // (латиница/кириллица, слитно/раздельно, падежи; термины
                                    // до 4 звуков — только по вариантам)
  "title_rules": [],                // [TitleRule] — правила по названию окна (важнее app_rules)
  "broadcast_targets": [],          // ключи RDP-сессий для рассылки (белый список)
  "broadcast_excluded": [],         // устарело (до 0.4), см. «Рассылка в RDP»
  "vocab_enabled": true,            // подсказка распознаванию и ИИ: мои частые слова
  "context_enabled": true,          // подсказка распознаванию: текст перед курсором в окне
  "duck_mode": "duck",              // звук других программ во время записи: off | duck | mute
  "duck_level": 15,                 // duck: до скольких процентов исходной громкости (0–100)
  "language": "system",             // язык интерфейса и сообщений: system | ru | en
  "dictation_language": "auto",     // язык распознавания: auto | ru | en (см. «Локализация»)
  "model_dir": ""                   // папка моделей Whisper (формат кэша HuggingFace, как
                                    // download_root у faster-whisper); "" — кэш HuggingFace по
                                    // умолчанию. Полный путь (диск или UNC), %VAR% и ~
                                    // раскрываются: "D:\\Модели\\whisper-models"
}
```

Приоритет стиля и режима вставки для окна: `title_rules` (процесс совпал или `"*"`, `title` —
подстрока заголовка активного окна без учёта регистра и «ё»; из подходящих — самое длинное
`title`, при равной длине правило процесса раньше `"*"`) → `app_rules` → `default_style` /
`insert_mode`. Действует только одно правило по названию — самое длинное; его поле `null`
(«Не менять») — как у приложения: берётся правило приложения, затем настройка (более короткие
правила по названию при этом не участвуют). Дубли `title_rules` (процесс + название без
регистра) запрещены: `set_settings` — ошибка, при загрузке файла лишнее пропускается.

## SQLite (`%APPDATA%\Rupor\rupor.db`, WAL)

```sql
CREATE TABLE history(id INTEGER PRIMARY KEY, ts REAL NOT NULL, app TEXT, title TEXT,
  raw TEXT NOT NULL, text TEXT NOT NULL, duration_s REAL, words INTEGER, language TEXT,
  style TEXT, ai INTEGER DEFAULT 0, latency_ms INTEGER, target TEXT);
CREATE INDEX history_ts ON history(ts);
CREATE TABLE dictionary(id INTEGER PRIMARY KEY, term TEXT NOT NULL UNIQUE COLLATE NOCASE,
  variants TEXT NOT NULL DEFAULT '[]');           -- JSON-массив
CREATE TABLE snippets(id INTEGER PRIMARY KEY, trigger TEXT NOT NULL UNIQUE COLLATE NOCASE,
  text TEXT NOT NULL);
CREATE TABLE stats_daily(date TEXT PRIMARY KEY, words INTEGER NOT NULL DEFAULT 0,
  speech_s REAL NOT NULL DEFAULT 0, dictations INTEGER NOT NULL DEFAULT 0);
CREATE TABLE kv(key TEXT PRIMARY KEY, value TEXT);  -- черновик: key='draft';
  -- обучение словаря: 'learn_counts' — JSON {"вариант→термин": повторов} (предложенные),
  -- 'learn_rejected' — JSON ["вариант→термин", …] (отменены «Отменить»),
  -- 'learn_journal' — JSON [{id, term, variant, created, owns, ts, source}, …] (что изменила
  -- каждая выученная правка — для одноразовой «Отменить» и «Недавно добавлено»; ts — unix, с,
  -- source — "auto" | "accepted", у записей до этих полей их нет); по 300 последних;
  -- 'ai_low_balance_notice' — когда предупреждали о балансе < $1 (unix, с);
  -- 'vocab_version' — версия отбора слов, по которой собран vocab
-- версия 2: расход ИИ — строка на КАЖДЫЙ вызов (ошибка: ok=0, cost_usd=0)
CREATE TABLE ai_usage(id INTEGER PRIMARY KEY, ts REAL NOT NULL, kind TEXT NOT NULL,  -- cleanup|command|test
  provider TEXT, model TEXT, upstream TEXT, tokens_in INTEGER, tokens_out INTEGER,
  cost_usd REAL, ms INTEGER, ok INTEGER NOT NULL DEFAULT 1, error_kind TEXT, app TEXT);
CREATE INDEX ai_usage_ts ON ai_usage(ts);
-- версия 3: личный словарь частот (производная от истории) и исключения
CREATE TABLE vocab(word TEXT NOT NULL, app TEXT NOT NULL DEFAULT '', count INTEGER NOT NULL DEFAULT 0,
  last_ts REAL NOT NULL DEFAULT 0, PRIMARY KEY(word, app));   -- app — history.app в нижнем регистре
CREATE INDEX vocab_app ON vocab(app);
CREATE TABLE vocab_excluded(word TEXT PRIMARY KEY);            -- casefold + «ё»→«е» слова;
                                                               -- действует на все его формы
PRAGMA user_version = 3;
```

Миграции — `storage._MIGRATIONS` (SQL или функция Python на шаг); миграция 2 → 3 только
создаёт таблицы. Если `kv.vocab_version` не совпадает с `vocab.version()` (миграция, правила
отбора или частотные списки изменились), `vocab` пересчитывается по всей истории — иначе
вычитание при удалении истории разошлось бы с тем, что прибавляли. У движка
(`Storage(background=True)`) — в фоне после старта (`vocab_rebuild_async` из потока
`rupor-idle`, после срока хранения): открытие базы и WS-порт её не ждут, до конца пересборки
подсказка — без частых слов. Тексты разбираются порциями без замка базы, в конце одной
транзакцией учитываются записи, добавленные/удалённые за это время. Пустая история — версия
ставится сразу. Сбой пересборки открытию базы не мешает. Битый UTF-8 в базе
(`text_factory`, поиск через `CAST … AS BLOB`) ничего не роняет — байты заменяются «�».

Удаление истории (запись, «Очистить за период», срок хранения) вычитает частоты одним
проходом: тексты разбираются до захвата замка, под замком — `UPDATE` по (слово, процесс) и
один `DELETE FROM vocab WHERE count <= 0`. «Очистить всё» стирает и `vocab`, и
`vocab_excluded`.

Статистика пишется в `stats_daily` **всегда** (даже при `history_enabled=false` и после очистки
истории). Даты — локальные (`time.localtime`).

## Python-модули ядра (контракты)

### `storage.py`
```python
class Storage:
    def __init__(self, path: Path | None = None, *, background: bool = False) -> None
        # None → app_dir()/"rupor.db"; потокобезопасен (lock). background (движок): пересборка
        # частот — по vocab_rebuild_async, кэш частых слов после диктовки — в фоне
    def add_history(self, item: dict) -> dict                # поля HistoryItem без id → с id
    def list_history(self, query: str | None, limit: int, offset: int) -> tuple[list[dict], int]
    def get_history(self, id: int) -> dict | None
    def delete_history(self, id: int) -> None
    def clear_history(self, period: str) -> int
    def purge_older_than(self, days: int) -> int
    def record_stats(self, words: int, speech_s: float, when: float | None = None) -> None
    def stats(self, today: date | None = None) -> dict       # Stats
    def dict_list(self) -> list[dict]; def dict_upsert(self, id: int | None, term: str, variants: list[str]) -> dict
    def dict_delete(self, id: int) -> None
    def snippets_list(self) -> list[dict]; def snippet_upsert(self, id: int | None, trigger: str, text: str) -> dict
    def snippet_delete(self, id: int) -> None
    def export_data(self) -> dict; def import_data(self, data: dict, replace: bool) -> dict
    def kv_get(self, key: str, default: str = "") -> str; def kv_set(self, key: str, value: str) -> None
    def dict_learn_add(self, term: str, variant: str = "", journal: bool = True, source: str = "auto") -> tuple[dict, bool]
        # (DictItem, created): слить вариант с термином (без учёта регистра/«ё»), написание
        # термина не менять; вариант уже у другого термина — ValueError; изменение — в журнал
        # (с ts и source: "auto" — Рупор сам, "accepted" — кнопка «Добавить»)
    def dict_learn_undo(self, term: str, variant: str = "", id: int | None = None, created: bool = False) -> bool
        # одноразовый откат по журналу; True — словарь изменился
    def dict_learn_journal(self, limit: int = 50) -> list[dict]
        # [LearnJournalEntry], новые первыми; без записей удалённых/переименованных терминов
    def add_ai_usage(self, rec: dict) -> dict              # строка ai_usage; ok=False → cost 0
    def ai_usage_stats(self, today: date | None = None) -> dict   # AiUsageStats
    def ai_usage_today_cost(self, now: float | None = None) -> float
    def vocab_list(self, process: str | None = None, limit: int = 50) -> list[dict]
    def vocab_exclude(self, word: str, excluded: bool) -> None
    def vocab_prompt_words(self, process: str | None, limit: int = 60, strong_only: bool = False,
                           reject: Callable[[str], bool] | None = None
                           ) -> tuple[list[str], list[str]]   # (слова процесса, общие)
        # топ считается в SQL (VOCAB_FETCH_ROWS самых частых написаний процесса / всех
        # процессов), формы сливаются в Python; кэш по процессу — общий для подсказки
        # Whisper, терминов ИИ и vocab_list. Новая диктовка кэш не сбрасывает (помечает
        # устаревшим: с background — пересчёт в фоне), удаление истории — сбрасывает
    def vocab_words(self, process: str | None = None) -> list[Word]
    def vocab_rebuild(self) -> int; def vocab_rebuild_async(self) -> bool
    vocab_ready: bool                                      # нет незаконченной пересборки
    # add_history прибавляет слова текста к vocab (target window/rdp_all), delete_history /
    # clear_history / purge_older_than вычитают, «Очистить всё» стирает vocab и vocab_excluded
```

### `textproc.py`
```python
def count_words(text: str) -> int
def apply_dictionary(text: str, items: list[dict]) -> str
    # варианты → термин, без учёта регистра, по границам слов («клик хаус»/«кликхаус» → ClickHouse)
def misrecognition_filter(items: list[dict]) -> Callable[[str], bool] | None
    # «словарь заменил бы это слово другим термином» — ошибка распознавания («Xenoposter» при
    # варианте термина «Zennoposter»); такие частые слова не идут в подсказку Whisper и в
    # термины ИИ. Предикат общий на словарь, ответ по слову запоминается
def expand_snippets(text: str, snippets: list[dict]) -> tuple[str, bool]
    # вся фраза (без пунктуации и регистра) == триггер → текст сниппета, True;
    # иначе вхождения триггера внутри фразы заменяются текстом, флаг True если была замена
def asr_prompt(terms: list[str], items: list[dict], limit_chars: int = 400) -> str | None
    # подсказка для initial_prompt: термины из настроек + термины словаря
```

### `fuzzy.py` (похожие написания терминов словаря)
```python
def sound_key(text: str, reading: str = "en") -> Key   # звучание: (head — первый звук, body — по классам)
    # кириллица → латиница, «кс»/x в начале → «з», английское чтение (немая и «магическая» e,
    # ow/ou → au, -ange/-age → «ейндж/едж», ph → f, ch → «ч», c → k/s…), части по camelCase/
    # пробелу/дефису склеиваются, звонкие = глухие, гласные классами (о/а, е/и), двойные — одна.
    # «Xena браузер» == «ZennoBrowser». Другие прочтения латинского термина (reading):
    # letters — побуквенное («Метабаза», «Геонод»), closed_u — u как «а» («гитхаб»),
    # soft_g — мягкое g («Джемини»), short_a — «а» в начале как «э» («Энтропик»),
    # long_i — «i» перед ld/nd как «ай» («Вайлд Дак»); au — «о» (Claude = «Клод»)
def find(text: str, items: list[dict]) -> list[FuzzyMatch]
    # фрагменты из 1–3 слов без пунктуации между ними. Одно слово: звучание совпадает с
    # термином или вариантом (4 звука — буква в букву, 5–7 — точно, 8–11 — одна правка, от
    # 12 — две; ≤ 3 — не ищем) и слова нет в языке (spell.py: проверка орфографии Windows;
    # «курсор», «редис», cloud не трогаются; нет словаря языка — похожих не ищем; имя,
    # известное лишь с заглавной, — не-слово только для термина от 8 звуков при точном
    # совпадении: «Телеграм» в начале фразы). Несколько слов: совпадают с частями термина по
    # одной (у каждой свой запас правок) или звучат как термин целиком (точно, либо хотя бы
    # одна часть — в точности часть термина: «Zenno Bowser»), и среди них есть не-слово или
    # слово-термин. Первый звук совпадает; окончание после кириллицы: у кириллического термина
    # переносится, у латинского — отбрасывается; отпадающая гласная — только у склоняемых
    # (латиница на -a, кириллица на -а/-я/-и/-ы) и с окончаниями женского рода. Кириллица против
    # кириллического термина — ещё и буква в букву, с «ь» («Ксенька» ≠ «Зенка»). Письменность
    # фрагмента — по первой букве (при равных правках — термин той же письменности); латиница
    # кириллическим термином не заменяется, слово на «ь» — латинским, фамилия на -ов/-ев с
    # заглавной — тоже. Меньше правок важнее, при равенстве — длиннее фрагмент. Не трогаются:
    # уже термины (и «Postgres'е», «Zennoposter-е», слова многословного «Grafana Cloud» и
    # «Grafana Cloud'е»), слова с цифрами, части путей/адресов/имён файлов, слова короче 3 букв
    # в составном фрагменте, фрагменты со своим частым словом пользователя, которого нет в
    # словаре Windows («гео ноды»; user_words — vocab.group_key строчных частых слов). Термин,
    # записанный через дефис или раздельно («Proxy-pool», «Zenno Poster», «Cloud Flare»),
    # пишется как в словаре, если части с заглавной или среди них не-слово («air-flow» — нет).
    # Ещё: короткая часть термина (≤ 4 звуков) — только точно («Зен браузер»); частое имя —
    # настоящее слово («Ксена браузер»); «кс» как «з» у кириллицы — только для терминов от 8
    # звуков («Ксенка» ≠ «Зенка»); термин из двух слов не склеивается из слова-термина и
    # настоящего слова («Claude код»); окончание/частица через дефис или апостроф у
    # исправленного слова сохраняются («Zenobowser'е» → «ZennoBrowser'е»); слова, склеенные
    # ударением или невидимым знаком, и тире между словами — не трогаются
def apply(text, items, fuzzy=True, user_words=frozenset()) -> tuple[str, int]   # точный словарь, затем похожие
def apply_text(text, items, fuzzy=True, user_words=frozenset()) -> str
def misrecognition_filter(items, fuzzy=True) -> Callable[[str], bool] | None
    # как textproc.misrecognition_filter, но с похожими написаниями: «Xenoposter» — ошибка и без
    # варианта, если в словаре есть «Zennoposter»
```

### `spell.py` (есть ли слово в языке — проверка орфографии Windows)
```python
def known_words(words) -> dict[str, bool | None]
    # ISpellChecker (spellcheck.h) через comtypes: кириллица — ru-RU, латиница — en-US; слово из
    # двух письменностей — False; нет словаря языка / API / не ответил за 2 с — None (fuzzy тогда
    # похожих написаний не ищет). Не ответила — минуту её не ждём (BACKOFF_S), опоздавшие
    # ответы не кешируются. Все вызовы COM — в потоке rupor-spell (MTA), ответы кешируются
def ready() -> bool                          # словари подняты и отвечают (fuzzy запоминает ответы)
def warm_up() -> None                        # поднять поток и словари в фоне (старт движка)
def selftest() -> tuple[bool, str]           # «орфография: ok (ru да, en да)» для --uia-selftest
```

### `vocab.py` (личный словарь частот и подсказка распознаванию — чистая логика)
```python
def extract(text: str) -> list[str]          # характерные слова текста (с повторами)
    # латиница в русском тексте, смешанный регистр и аббревиатуры (ClickHouse, API, СДЭК),
    # имена — кириллица с заглавной не в начале предложения, редкие строчные слова (≥ 5 букв,
    # «слабые»); общеупотребительные отсеиваются списками data/common_ru.txt (≈ 3200 форм
    # и инфинитивов, по форме и по основе — стеммер Snowball; личные формы и прошедшее время
    # правильных глаголов достраивает verb_forms, частые формы с приставкой — «подскажи»,
    # «передай» — is_common_ru_prefixed) и data/common_en.txt (≈ 1800, с окончаниями
    # -s/-ed/-ing…) + learn.STOPWORDS/COMMON_WORDS/COMMON_NAMES; адреса, почта, пути, имена
    # файлов и числа пропускаются; «ClickHouse-запрос» → ClickHouse, «Metabase'е» →
    # Metabase, но «Wi-Fi», «GPT-4o», «Санкт-Петербург», «Яндекс.Go» — целиком; буквы с
    # цифрами — только похожие на название (K8s, GPT4, iPhone15, 1С), не «Summer2024».
    # Английская диктовка (нет кириллицы) — строже: частые английские слова не берутся,
    # редкие строчные — слабые: в таблице vocab они хранятся с меткой EN_WEAK_MARK
    # («~deployment»), при выдаче метка снимается. ALGO_VERSION = 3: при смене версии
    # отбора база один раз пересобирает частоты из истории.
def group_key(word) -> str                   # ключ слияния: без регистра/«ё», у кириллицы — основа
def merge(rows) -> list[Word]                # формы одного слова (регистр, «ё», падежи) → одно;
    # показывается именительный падеж, если встречался («Тимур» среди «Тимуру», «Тимура»)
def pick_words(proc, general, excluded, limit=60, strong_only=False, reject=None
               ) -> tuple[list[str], list[str]]
    # reject(слово) → True — пропустить, места в limit не занимает (misrecognition_filter)
    # сильные (термины, имена: STRONG_MIN=2 повтора) раньше слабых (WEAK_MIN=3, не больше 20);
    # исключение действует на все формы слова (excluded_keys)
def context_tail(text, max_chars=300, sentences=2) -> str     # 1–2 последних предложения
def build_prompt(context, terms, process_words, general_words, max_tokens=200,
                 max_words=60, context_chars=300, context_tokens=100,
                 count_tokens=None) -> str | None
    # «<контекст окна> ClickHouse, Metabase, Redis.»: контекст (≤ 300 симв. и ≤ 100 токенов,
    # срез слева по словам) → термины настроек и словаря → частые слова процесса → общие;
    # ≤ 200 токенов. Токены считает count_tokens — движок передаёт токенизатор загруженной
    # модели (Recognizer.token_counter: так faster-whisper кодирует initial_prompt и оставляет
    # последние 223 токена); без него — estimate_tokens (кириллица 1,45 симв./токен,
    # латиница 2,0 — не меньше настоящего числа токенов). Частых слов ≤ 60, дубли без
    # регистра и «ё» убираются, в конце — проверка подсказки целиком
def version() -> str; def selftest() -> tuple[bool, str]
```

### `credman.py`
```python
def read(target: str) -> str | None; def write(target: str, secret: str, user: str = "Rupor") -> None
def delete(target: str) -> None
TARGETS = {"openrouter": "Rupor/openrouter", "anthropic": "Rupor/anthropic"}
def read_env_key(path: str, name: str = "openrouter_api_key") -> str | None
def mask(key: str) -> str      # "sk-or-v1-…XYZ"
```
Ключ нигде не логируется и не попадает в историю/ошибки.

### `ai.py`
```python
STYLES: list[dict]   # [{id, name, description, instructions}] — as_is, normal, business, messenger, code
STYLES_EN: dict[str, dict]    # id → {name, instructions} — английские инструкции тех же стилей
BASE_PROMPT: str     # системный промпт из ТЗ + усиленное правило про паразитов в начале фразы
BASE_PROMPT_EN: str  # тот же смысл для английского текста (um/uh/like/you know/basically…)
COMMAND_PROMPT: str; COMMAND_PROMPT_EN: str
def text_language(text: str, hint: str | None = None) -> str      # "ru" | "en" — язык промпта очистки
def build_system_prompt(style: str, dictionary_terms: list[str], lang: str = "ru") -> str
def styles_public(lang: str | None = None) -> list[dict]          # для styles_list, на языке интерфейса
def command_prompt(lang: str | None = None) -> str                # по умолчанию — i18n.current()
@dataclass class AIResult: text: str; ms: float; cost_usd: float | None; provider: str; model: str
class AIError(Exception)
def cleanup(text: str, style: str, s: Settings, dictionary_terms: list[str], *,
            api_key: str | None = None, language: str | None = None) -> AIResult   # таймаут s.ai_timeout_s
def command(instruction: str, selected: str, s: Settings) -> AIResult                       # командный режим, таймаут 30 с
def check_openrouter_key(key: str) -> dict                                                  # ответ ai_key_check
```
Язык промптов — см. «Локализация». Тексты `AIError` и ответа `check_openrouter_key` — через
`i18n.tr` (ключи `ai.*`).
HTTP — `urllib.request` со своим `User-Agent: Rupor/<version>` (Cloudflare режет UA urllib).
OpenRouter: `temperature=0`, `max_tokens = max(300, 2 * оценка_токенов_входа)`, `provider.order`
если задан, `usage: {include: true}` → `cost_usd` из `usage.cost`.
`claude -p`: `subprocess.run(["claude", "-p", *CLI_FLAGS], input=prompt, timeout=30,
capture_output=True, text=True, encoding="utf-8", creationflags=CREATE_NO_WINDOW)` — промпт
(инструкция + выделенный текст) **только через stdin**, не в командной строке процесса (её
видят аудит создания процессов, Sysmon/EDR); OAuth-токены Claude Code не читать.
Ключ из Credential Manager обрезается по краям (`\r\n` от сторонних инструментов); пробел
или управляющий символ внутри — `AIError("auth")` без текста ключа. Ошибки заголовков
http.client (`ValueError` с «Bearer <ключ>») превращаются в `AIError` без текста исходной
ошибки; ответ WS-команды и лог (`log.ScrubFormatter`) дополнительно вычищаются от
всего, похожего на ключ.
Командный режим, системный промпт: «Ты редактор. Примени инструкцию пользователя к тексту.
Верни только результат…» (`COMMAND_PROMPT`; при английском интерфейсе — `COMMAND_PROMPT_EN`
того же смысла).

### `fetch.py` (загрузки первого запуска)
```python
# cache_dir — папка моделей (settings.model_dir or None; None — кэш HuggingFace по умолчанию)
def model_ready(model: str, cache_dir=None) -> bool
def model_path(model: str, cache_dir=None) -> Path | None   # папка снимка модели, без сети
def download_model(model: str, on_progress: Callable[[float, str], None], cache_dir=None) -> str   # путь
def model_cache_dir(model_dir: str) -> Path   # действующая папка: model_dir или HF_HUB_CACHE
def move_model(model, src_dir, dst_dir, on_progress) -> str   # перенос в другую папку, путь
def remove_model(model, cache_dir) -> bool   # удалить models--org--name и .locks/models--org--name
def same_dir(a, b) -> bool               # одна папка: регистр, вид записи, junction/ссылка/subst/UNC
def check_move_target(model, src_dir, dst_dir) -> None   # dst внутри папки модели или .locks — FetchError
def shares_files(model, old_dir, new_dir) -> bool   # remove_model(old) задел бы копию в new
def check_writable_dir(path) -> Path     # создать папку и проверить запись (FetchError)
def model_sizes(repos, budget_s=4.0) -> dict[str, int]   # размеры с Hub, параллельно, кэш на процесс
def cuda_ready() -> bool                 # DLL cuBLAS найдены (cuDNN CTranslate2 для Whisper не нужен — проверено) (pip-пакеты nvidia-* или %LOCALAPPDATA%\Rupor\cuda)
def download_cuda(on_progress) -> None   # колесо nvidia-cublas-cu12 (≈0,55 ГБ)
                                         # с PyPI (версии как в pyproject), из них извлекаются только nvidia/*/bin/*.dll
def cuda_dll_dirs() -> list[Path]
```

### Папка модели и подбор модели (`config.model_dir`, `fetch.move_model`, `recommend.py`)
- **Где лежит модель.** Настройка `model_dir` — папка в формате кэша HuggingFace
  (`models--<org>--<имя>/blobs|snapshots|refs|trees`, замки — `.locks/models--…`), тот же формат,
  что `download_root` у faster-whisper: папку можно указать и другим программам на
  faster-whisper. `""` — кэш HuggingFace по умолчанию (старые установки работают как раньше).
  Поиск (`engine._load_model`/`_reload_model`, `setup_status`, `asr.Recognizer`) и загрузка
  (`_download`) — с `cache_dir = model_dir or None`.
- **Проверка** (`config.normalize_model_dir`): строка; пробелы и кавычки вокруг («Копировать как
  путь») снимаются; `%VAR%` и `~` раскрываются (незаданная переменная — ошибка); путь только
  полный — с буквой диска (`D:\…`) или UNC (`\\сервер\папка`); символы `< > : " | ? *` —
  ошибка; `ntpath.normpath`. Кириллица и пробелы допустимы (`D:\Модели\whisper-models`).
- **Смена папки без переноса** (`set_settings {model_dir}` или `model_dir_set` с `move=false`):
  `model_dir` входит в `config.MODEL_FIELDS` — перезагрузка модели как при смене модели: есть
  в новой папке — грузится оттуда, нет — качается туда, прежняя распознаёт до готовности.
- **Та же папка под другим путём** (`fetch.same_dir`: регистр и вид записи; номер тома и файла
  `os.stat` — junction, символьная ссылка, subst, сетевой диск и его UNC-путь; папки ещё нет —
  `os.path.realpath`): `model_dir_set` только сохраняет новую запись (например, явный путь кэша
  по умолчанию → `""`), без переноса и удаления — иначе «перенос» нашёл бы модель «уже там»,
  а удаление «старой» копии стёрло бы единственную.
- **Папка внутри папки модели** (`fetch.check_move_target`): новая папка внутри
  `models--org--name` или `.locks` нынешней — ошибка (и в `model_dir_set` до создания папки, и в
  `move_model`): копия легла бы в то, что удаляет `remove_model`.
- **Перенос** (`model_dir_set` с `move=true`, модель есть в нынешней папке): поток
  `rupor-model-move` занимает слот `"model"` в `engine._downloading` (загрузка модели в это
  время не начнётся: `setup_download` запоминает просьбу, `_model_dl_deferred`, и после
  переноса ставит задание `model_downloaded` — модель, выбранная во время переноса, качается в
  действующую папку) → `fetch.move_model`:
  - в новой папке модель уже целиком — все файлы исходного снимка (у копии другой программы
    может не быть `preprocessor_config.json`: тогда она дополняется копией) — копировать нечего;
  - тот же том и папки модели там нет — `os.replace` папки (мгновенно; не вышло — копия);
  - другой том — копия во временную `models--<org>--<имя>.rupor-tmp` рядом и переименование: прерванная
    копия никогда не выглядит полной. Прогресс по байтам (`progress` `what: "model"`).
    Относительные ссылки snapshots (`../../blobs/<хэш>`) пересоздаются такими же; создать
    ссылку нельзя (нет права) — на её месте пишется сам файл, а блоб отдельно не копируется
    (без двойного места). `refs/` и `trees/` копируются (`refs/main` нужен поиску без сети),
    `*.incomplete` — нет. Перед копией — проверка места (размер файлов + 64 МБ), после — что
    `model_path` находит модель в новой папке; любая ошибка — временная копия удаляется,
    `FetchError`. Исходная папка `move_model` не удаляет;
  - затем `model_dir` сохраняется (событие `settings`; не записался — переименованная модель
    возвращается на место, ошибка), задание `reload` перезагружает модель из новой папки,
    следом задание `model_moved` удаляет старую копию (`fetch.remove_model` — только
    `models--org--name` и `.locks/models--org--name`, в фоне) и шлёт `notice`. Удаляет — только
    если перезагрузка из новой папки удалась: модель загружена (фаза `ready`, нет ожидающей
    подмены), `settings.model` и `model_dir` — те, что переносились, и модель находится в новой
    папке; иначе старая копия остаётся (notice warn `eng.model_old_kept`) — она может быть
    единственной рабочей. Старая и новая копии — одни и те же файлы (`fetch.shares_files`:
    junction на `models--org--name`, копия внутри удаляемых папок, жёсткие ссылки) — не
    удаляется ничего, перенос удачен.
  Распознавание всё это время работает на загруженной модели.
- **Подбор модели** (`model_recommend`, `recommend.py`): `asr.gpu_info` (NVML),
  `asr.cuda_device_count` и `asr.cuda_compute_types`
  (`ctranslate2.get_supported_compute_types("cuda")`; cuBLAS ни для того, ни для другого не
  нужен — он грузится только с моделью), память — установленная
  (`GetPhysicallyInstalledSystemMemory`; не узнать — `GlobalMemoryStatusEx`, у 8 ГБ со
  встроенной графикой это 7,3 ГБ), потоки — `os.cpu_count()`. Видеокарта NVIDIA, которую видит
  CUDA: видеопамяти от 4 ГБ — `large-v3-turbo`, `cuda`, `float16`; 2–4 ГБ (и объём не
  определился) — `large-v3-turbo`, `cuda`, `int8_float16`; меньше — `small`, `cuda`,
  `int8_float16`. Точность — только из тех, что CTranslate2 умеет на этой карте (явно заданную
  неподходящую он не подменяет, а не грузит модель): нет float16/int8_float16 (Pascal, GTX 10xx,
  CC 6.1) — `int8` на той же карте; нет и int8 (Maxwell, Kepler) — правила для процессора.
  Карту видит только NVML, а CUDA — нет (драйвер слишком старый для CUDA 12) — тоже правила для
  процессора (причина в тексте: «CUDA её не видит…»). Без видеокарты: от 8 потоков и от 8 ГБ
  памяти — `small`, `cpu`, `int8`, иначе `base`, `cpu`, `int8`. Пороги с допуском (3800 МБ,
  1900 МБ, 7,5 ГБ). Варианты — tiny, base,
  small, medium, large-v3-turbo, large-v3 (distil-* — только английские, не предлагаются);
  размер — по данным Hub (`fetch.model_sizes`: запросы параллельно, все вместе не дольше 4 с,
  удачные — в кэше процесса, неудачные не переспрашиваются 5 минут), иначе запасной
  (`recommend.FALLBACK_MB`: 75, 145, 484, 1530, 1620, 3090).

### Изменения в существующих модулях
- `hotkeys.GestureMachine`: `command_mod` (по умолчанию правый Alt) → `Kind.COMMAND`
  (аналог BROADCAST). Модификаторы broadcast/command, нажатые **во время удержания ptt**,
  «проглатываются» хуком (не доходят до приложений, иначе Alt откроет меню): после
  `key_down`/`key_up` у машины выставлен `swallow: bool`, хук возвращает 1 без CallNextHookEx.
  Отпускание проглоченной клавиши тоже проглатывается.
- `engine.py`: история, статистика, словарь/сниппеты, ИИ (режимы wait/raw_first),
  командный режим, стили и правила приложений, отслеживание последнего «чужого» окна
  (WinEvent foreground; свои окна — pid ядра и `--ui-pid`), `mic_monitor`, `test_record_*`,
  применение настроек на лету.
- `__main__.py`: режим `--sidecar` (WS-сервер, без pystray), старый режим без флага — pystray.

### Поток одной диктовки (engine)
0. Нажата диктовка (START): если `context_enabled` и активное окно — локальное чужое окно
   (не RDP/RemoteApp/Citrix/консоль ВМ, не окно Рупора), в фоне (поток `rupor-context`)
   `uia.caret_context()` читает до 600 символов перед курсором поля с фокусом (поле пароля —
   никогда); результат, пришедший позже 300 мс (`CONTEXT_TIMEOUT_S`) или из другого процесса,
   отбрасывается; от него остаются 1–2 последних предложения (`vocab.context_tail`, ≤ 300
   симв.). Распознавание контекст не ждёт: не успел — подсказка без него. Текст контекста
   живёт только в памяти сессии, в лог — только длина.
1. ASR → `raw`. Подсказка `initial_prompt` собирается перед каждым распознаванием
   (`Engine._asr_prompt` → `vocab.build_prompt`): [контекст окна — не для командного режима и
   рассылки, и только если диктовка закончилась в том же процессе, где её начали] + [`terms` настроек и термины словаря] + [частые слова процесса окна] + [общие
   частые слова] (частые — если `vocab_enabled`, без исключённых), ≤ 200 токенов по
   токенизатору модели (контекст — ≤ 100 из них). Ошибки распознавания из словаря (слова,
   которые словарь заменил бы другим термином, точно или по звучанию — `fuzzy.misrecognition_filter`) в подсказку не идут — ни частым
   словом, ни термином настроек, а контекст окна идёт уже исправленным словарём: иначе
   ошибка, попавшая в историю до словаря, закрепляла бы сама себя.
2. Словарь (`Engine._dict_apply` → `fuzzy.apply`: точные варианты `apply_dictionary`, затем,
   если `dict_fuzzy`, похожие по звучанию написания терминов; число таких замен — в лог) →
   `expand_snippets` (если сниппет сработал — ИИ пропускается). Тот же словарь — для
   проверочной записи, триггеров сниппетов и контекста окна; инструкция командного режима —
   только точным словарём (слово, названное в команде как есть, не переписывается).
3. Стиль = правило по названию окна → правило приложения → `default_style`. ИИ, если
   `ai_enabled`, стиль ≠ `as_is`, слов ≥ `ai_min_words`, есть ключ провайдера. В промпт
   ИИ-очистки идут термины словаря и частые «сильные» слова пользователя (термины, имена;
   до 40), если `vocab_enabled`, — тоже без ошибок распознавания из словаря. Каждый вызов
   ИИ пишется в `ai_usage` (см. ниже). Ответ ИИ (в обоих режимах) ещё раз проходит
   словарь (`_dict_apply`): словарь пользователя — последнее слово.
   - `wait`: состояние `ai`, вызов с таймаутом; ошибка/таймаут → вставить обработанный без ИИ
     текст + `notice` warn «ИИ не ответил — вставлен текст без обработки».
   - `raw_first`: сразу вставить, в фоне ИИ; по приходу — заменить, **только если** с
     **начала** вставки не было нажатий (физических и чужого программного ввода; счётчик
     снимается до вставки — нажатия во время печати тоже считаются), активное окно и окно
     с фокусом ввода (`GetGUIThreadInfo.hwndFocus`) те же: выделить Shift+← (по числу
     символов вставленного текста, `len(str)`), **проверить выделение** (Ctrl+Insert через
     буфер) — оно должно совпасть со вставленным текстом, иначе снять выделение (→) и не
     заменять (щелчок мышью в другое место, автозамена «...»→«…», ячейки Excel, пустое поле
     после «Отправить»); затем вставить очищенный. Длиннее 1500 символов — не заменять.
4. Вставка (как на этапе 2) — **только в окно, где диктовали** (`job.target`): если к
   моменту вставки активно другое окно, текст не вставляется (notice warn, «Вставить
   последнее»); печать и вставка проверяют фокус перед каждым пакетом (`inject.FocusLost`).
   Затем `result`, запись в историю (если включена) и статистику.
5. Обучение словаря: до 60 с следить за полем, куда вставлен текст, и выучить исправления
   ошибок распознавания (см. «Обучение словаря на правках»).

Печать ждёт, пока пользователь отпустит модификаторы, перед **каждым** пакетом; клавишу,
которую пользователь держит сам (ptt следующей диктовки — `GestureMachine.is_held`), программно
не отпускает. Enter/Tab, Ctrl+V, Ctrl+Insert — со скан-кодами (RDP передаёт скан-коды).

Вставка через буфер — отложенным рендерингом (`SetClipboardData(CF_UNICODETEXT, NULL)` от
скрытого окна-владельца): прежний буфер возвращается после того, как окно-получатель прочитало
текст (`WM_RENDERFORMAT`, до 2 с), плюс `clipboard_restore_ms`. Временный текст и возврат
буфера помечаются форматами `ExcludeClipboardContentFromMonitorProcessing`,
`CanIncludeInClipboardHistory=0`, `CanUploadToCloudClipboard=0` (не в журнал Win+V и облако).
Режим «авто» печатает, а не вставляет, и в окна RemoteApp (`RAIL_WINDOW`), Citrix
(`wfica32.exe`, `cdviewer.exe`) и консоли ВМ (`vmconnect.exe`, VMware, VirtualBox).

Жесты: Esc, отменивший запись (удержание или «без рук»), проглатывается. Запись дольше
10 минут останавливается (STOP + notice); повторное «нажатие» ptt, которое выглядит как
автоповтор, но пришло позже 3 с после прошлого, — потерянное отпускание: CANCEL + START.
Удержание ≥ 1 с, а аудио < 30 % длительности — микрофон молчит: notice error и
переоткрытие микрофона (`audio.Recorder.start` сам переоткрывает поток, который молчит > 1 с).

### Обучение словаря на правках (`learn.py`, `uia.py`, `engine._LearnWatch`)

Как в Wispr Flow: после вставки диктовки ядро до 60 с следит за полем ввода, куда вставило
текст, и правки, похожие на ошибку распознавания, добавляет в словарь (с кнопкой «Отменить»)
или предлагает. Каждое пополнение словаря видно: уведомление в пилюле и список «Недавно
добавлено» на странице «Словарь».

- **Когда следим**: вставка удалась, окно локальное (не RDP/RemoteApp/Citrix/консоль ВМ),
  не рассылка, не командный режим, `learn_mode != "off"`, не пауза, окно не Рупора.
  Одновременно одно наблюдение: новая вставка заменяет прежнее, прежнее оценивается.
- **Чтение поля** (`uia.py`): Windows UI Automation через comtypes, все вызовы в одном потоке
  `rupor-uia` (`CoInitializeEx` MTA; comtypes импортируется там же с `sys.coinit_flags=MTA`),
  синхронные обёртки с таймаутом 3 с; `IUIAutomation2.ConnectionTimeout/TransactionTimeout`
  = 1000 мс. `focused_field()` → `Field{runtime_id, pid, control_type, read_text()}` или None:
  нет фокуса, нет ValuePattern/TextPattern (TextPattern без ValuePattern — только у Edit и
  Document), поле только для чтения, **пароль (IsPassword) — не читается никогда**.
  `read_text()`: ValuePattern.CurrentValue, иначе TextPattern.DocumentRange (до 20 000 симв.;
  длиннее — окно ±10 000 вокруг курсора). ValuePattern поля Win32 Edit отдаёт не больше
  4096 символов (`uia.VALUE_CLIP`): значение такой длины и длиннее берётся из TextPattern,
  а без TextPattern поле не читается (иначе фрагмент в конце длинного поля не находится,
  а слово на границе попадает в снимок обрезанным). Ошибки COM — None и `log.debug` без
  текста. Проверено: Блокнот, Telegram Desktop (Qt, `Ui::InputField::Inner`), поля Chromium,
  Word.
- **Наблюдение** (`engine._LearnWatch`, поток `rupor-learn`): основа — поле с фокусом того же
  pid, что окно вставки, в тексте которого есть вставленный фрагмент (ждём до 2 с). Опрос
  каждые 400 мс, а в течение 2 с после изменения поля — каждые 100 мс. Снимки
  (`learn.Snapshot`, последние 64) помнят, когда такой текст прочитан впервые и в последний
  раз; «хороший» — пока в поле есть большая часть фрагмента (`learn.fragment_alive`).
  Конец и оценка: фокус ушёл с поля (другой `runtime_id`), поле очищено (Telegram отправил
  сообщение — берётся прошлый хороший снимок), 60 с (поле менялось меньше 2 с назад —
  ждём ещё, до 15 с), новая диктовка (START), новая вставка — в двух последних случаях поле
  перечитывается ещё раз. Пауза и выход — конец без оценки. «Сначала сырой»: когда ИИ
  заменил текст, основа снимается заново — правки ИИ не считаются правками пользователя.
- **Оценка** — только если после вставки были нажатия (`machine.presses`):
  `learn.held_corrections(основа, фрагмент, снимки, словарь)` — правки последнего хорошего
  снимка (`learn.find_corrections`) и сколько секунд каждая непрерывно держалась в поле.
  Меньше 0,3 с (`LEARN_MIN_HELD_S`: Enter, Alt+Tab или хоткей сразу после буквы — в снимке
  мог оказаться «ClickHou») — не учится.
- **Какие правки** (`learn.find_corrections`) — пословный diff (`SequenceMatcher`); в блоке
  замены старая часть до 3 слов, новая до 2 («клик хаус» → ClickHouse). Учимся, если новое
  написание похоже на термин (латиница, цифры, смешанный регистр, API/СДЭК, заглавная не
  в начале предложения — имя) **и** похоже на старое по звучанию (транслитерация + простая
  фонетика: ≥ 0,6 между письменностями, ≥ 0,75 в одной; первая согласная та же с точностью
  до звонкости: React → Preact, Даше → Маше — нет), либо правка только регистра/«ё»
  термина (тогда вариант не нужен). Не учимся никогда:
  - грамматика: другая падежная форма того же слова («Наташа» → «Наташе», «Кузнецова» →
    «Кузнецовой», «Валере» → «Валерии»), русское окончание у латинского слова
    («Metabase» → «Metabase'е»/«Metabase-е»);
  - правка, которая отличается только числами (RUPOR-12 → RUPOR-13, GPT-4 → GPT-5);
  - перевод обычного русского слова, частого имени или города на латиницу («проект» →
    project, «тесты» → tests, «Москва» → Moscow, «Сергей» → Sergey; `learn.COMMON_WORDS`,
    `learn.COMMON_NAMES`);
  - заглавная у обычного русского слова или прилагательного («Заказчик», «с Новым годом»,
    «картой Мир», «Рабочую»), у слова в начале предложения или пункта (после .!?… «:» «;»,
    маркеров списка «1)», «•», «*», «-», «#», тире, значка в начале строки) и у двух и более
    слов рядом, где поднята только первая буква («Молоко, Хлеб, Сыр», «Project Status
    Update», «Рога и Копыта» — оформление);
  - блок, который не объясняется целиком по звучанию («я написал Наташе» → «мне написала
    Наташа», «клик хаус сегодня» → «ClickHouse вчера»); крайнее слово, удалённое или
    дописанное заодно, в вариант/термин не входит («в этом метабейсе» → «в Metabase» —
    вариант «метабейс», «клик хаус» → «ClickHouse упал» — термин ClickHouse);
  - а также: строчные слова по смыслу, пунктуация, удаление/добавление слов, числа, слова
    короче 3 символов, частые слова (`learn.STOPWORDS`), расширение слова (Java →
    JavaScript), уже известное словарю, вариант чужого термина.
  Падежное окончание варианта отрезается, если оно не звучит в термине («метабейсе» →
  «метабейс», «клик хаусом» → «клик хаус»; для терминов на «a» — «джире» → «джира»;
  «Сергей» → Sergey, «тесты» → tests не режутся), остальные падежи ловит
  `apply_dictionary`. Не больше 3 правок за наблюдение.
- **Уверенность** (`Correction.sure`) на результат больше не влияет — только пишется в лог.
  Без уверенности: имя → похожее имя и вообще правка, где и старое, и новое — «термины»
  (MySQL → MSSQL, Алёну → Алину, Metabays → Metabase); только заглавная у русского слова
  («петя» → «Петя», «маше» → «Маше»); перевод в строчную латиницу («вебхук» → webhook);
  латинское окружение («hi Жора» → Zhora); вариант из одного слова ≤ 4 букв после отрезания
  окончания («слак» → Slack, «зуме» → Zoom — вариант не отбрасывается). Раньше такие правки
  в `auto` только предлагались, и вопрос пропадал незамеченным; теперь они добавляются, как
  все, — пользователь видит каждое пополнение словаря и отменяет лишнее.
- **Результат** по `learn_mode`:
  - `auto` — любая правка, прошедшая фильтры выше (держалась ≥ `LEARN_MIN_HELD_S`, не
    отменялась раньше): `Storage.dict_learn_add(source="auto")` и `notice {level: "info",
    toast: true, action: "dict_undo", text: "В словарь добавлено: «{variant}» → {term}"
    (без варианта — "В словарь добавлено: {term}"), data: {term, variant, id, created}}`;
  - `suggest` — `notice {action: "dict_add", text: "Добавить в словарь: «{variant}» → {term}?"
    (без варианта — "Добавить в словарь: {term}?"), data: {term, variant, id: null,
    created: false}}`; кнопка «Добавить» — `dict_learn_accept` (в журнал с
    `source: "accepted"`); счётчик одинаковых правок — в `kv.learn_counts`; на второй раз
    правка добавляется сама (`source: "auto"`) с уведомлением как в `auto`;
  - правка, отменённая «Отменить» (`dict_learn_undo`), больше не выучивается (`kv.learn_rejected`).
  Пилюля показывает эти уведомления нейтрально, с кнопкой: «Добавить …?» ~6 с, «В словарь
  добавлено … [Отменить]» — 12 с; пока курсор над пилюлей, уведомление не пропадает.
- **Журнал** (`kv.learn_journal`, 300 последних): каждое изменение словаря обучением —
  `{id, term, variant, created, owns, ts, source}`. По нему работает одноразовая «Отменить»,
  и он же — список «Недавно добавлено» на странице «Словарь» (`dict_learn_journal`: новые
  первыми, «добавил Рупор» / «вы подтвердили», время, кнопка «Отменить» = `dict_learn_undo`).
- Подсказка распознаванию (`initial_prompt`) считается перед каждой диктовкой из словаря —
  выученный термин попадает в неё со следующей диктовки.
- **Приватность**: текст поля и слова из него (термины, варианты — это имена собеседников
  и слова переписки) не логируются никогда — только счётчики, длины, причины окончания;
  в лог уведомления о правке — длина термина и есть ли вариант.
- `--uia-selftest` (флаг ядра): инициализировать UIA и записать в лог «Самопроверка UIA:
  UIA: ok, фокус: тип N, поле с текстом: да/нет, длина M, своё поле: ok, частотные списки: ok
  (…), Core Audio: ok, сессий K, устройств вывода D, локализация: ok (ru N, en N),
  орфография: ok (ru да, en да)» (без текста; «орфография» — `spell.selftest()`: проверка
  орфографии Windows работает в сборке, «нет» — словаря языка на машине нет;
  «локализация» — `i18n.selftest()`: оба каталога на месте и с одинаковыми ключами;
  «своё поле» — чтение собственного невидимого окна Edit; Core Audio — `duck.selftest()`: устройства вывода и
  аудиосессии только перечисляются и читаются, без имён; нет устройства вывода — «Core Audio:
  ok, устройства вывода нет»), код выхода 0/1.
- Сборка: comtypes в зависимостях ядра; `packaging/rupor-core.spec` генерирует обёртку
  `comtypes.gen.UIAutomationClient` при сборке и включает её (в замороженном ядре comtypes
  не пишет код в `%TEMP%`). `packaging/verify_core.py` (зовёт `build.cmd` после копирования
  ядра в ресурсы Tauri) запускает `rupor-core.exe --uia-selftest` во временных APPDATA/
  LOCALAPPDATA/TEMP и останавливает сборку, если ядро старое (не знает флага или в
  самопроверке нет «локализация:» или «орфография:»), самопроверка не прошла (в том числе
  нет «локализация: ok» или «орфография: ok»)
  или ядро что-то записало во временную папку.

### Звук компьютера во время записи (`duck.py`, `engine._duck_on` / `_duck_off`)

Как «Mute music while dictating» в Wispr Flow: пока идёт запись, звук других программ
приглушается (`duck_mode: "duck"` — громкость × `duck_level`/100) или выключается (`"mute"`);
`"off"` — не трогать.

- **Что меняется**: уровень аудиосессий Windows (ISimpleAudioVolume каждой сессии, как
  ползунки микшера), а не общий уровень устройства. Core Audio: IMMDeviceEnumerator →
  устройства вывода → IAudioSessionManager2 → IAudioSessionEnumerator →
  IAudioSessionControl2 + ISimpleAudioVolume. Интерфейсы описаны в `duck.py` на comtypes
  (IID и порядок методов — по заголовкам Windows SDK), без pycaw/psutil и без генерации кода.
- **Какие устройства**: перебираются все активные устройства вывода
  (EnumAudioEndpoints(eRender, DEVICE_STATE_ACTIVE) → IMMDeviceCollection), у сессии —
  признак `default`: она на устройстве по умолчанию для роли eConsole, eMultimedia или
  eCommunications (GetDefaultAudioEndpoint). **Приглушаются** только такие; **возвращаются** —
  на любом активном устройстве: если во время записи устройство по умолчанию сменилось
  (подключили наушники), плеер на новом устройстве приглушается при переопросе, а на прежнем
  всё равно вернётся. Приложения, которым в Windows явно назначено другое устройство
  («Параметры → Звук → Громкость приложений»), не приглушаются. Отключённое посреди записи
  устройство — запись ждёт, пока оно снова станет активным (как закрытое приложение, ниже).
- **Не трогаются**: сессии процессов Рупора (`Engine.own_pids`: ядро — его звуки начала и
  конца записи, и окно), сессия системных звуков (IsSystemSoundsSession, pid 0), сессии уже
  без звука или тише цели, не играющие сессии (не AudioSessionStateActive — приглушатся,
  когда заиграют). RDP: звук удалённой машины играет mstsc — обычная сессия, приглушается.
- **Когда** (движок): приглушение — когда запись подтверждена (`_confirm`, после звука начала;
  удержание, «без рук», щелчок по пилюле, кнопка мыши) и проверочная запись мастера
  (`test_record_start`). Возврат — в `_end_session` (стоп, отмена, выброс, лимит, пауза, смена
  микрофона/хоткеев), по окончании/отмене проверочной записи и при выходе. Смена
  `duck_mode`/`duck_level` во время записи: `off` — вернуть; другой режим (или уровень в
  `duck`) — вернуть и приглушить заново от исходной громкости, кроме сессий, которые
  пользователь в этой записи поменял сам; уровень в `mute` ни на что не влияет (ни возврата,
  ни повторного беззвука — звук не «прорывается»); из `off` — приглушить. Приглушение и
  возврат ставятся под `Engine._lock` вместе с проверкой сессии — приглушение не переживает
  запись, закончившуюся в другом потоке.
- **Выход**: команда WS `quit` (`Engine.request_quit`) ставит возврат звука сразу, до ответа, —
  ядро завершается через 0,2 с, `shutdown` ставит возврат первым делом и ждёт его в
  `Ducker.close()` (до 2 с). Оболочка даёт ядру выйти самому (`Supervisor::shutdown`: WS
  `quit`, ожидание выхода процесса до 2,5 с) и только потом `kill` — иначе приглушённое
  посреди записи вернул бы только следующий запуск ядра (по `duck.json`). Конец сеанса Windows (выход из системы,
  перезагрузка, выключение, установщик через Restart Manager): ядро держит скрытое окно
  верхнего уровня (`winutil.SessionEndWatcher`, запускается в `run_sidecar`) — на
  WM_QUERYENDSESSION `Engine.session_ending()` возвращает звук и ждёт этого до 1,5 с
  (Windows рассылает WM_ENDSESSION, в том числе окну Tauri, только после ответов всех
  программ); WM_ENDSESSION(TRUE) — ядро выходит, (FALSE, завершение отменено) — если запись
  идёт, приглушить снова (`session_end_cancelled`).
- **Поток**: все вызовы Core Audio — в потоке `rupor-duck` (CoInitializeEx MTA, comtypes
  импортируется с `sys.coinit_flags=MTA`, как в `uia.py`). `duck(mode, level)` / `restore()`
  только ставят команды в очередь: старт записи и хук их не ждут, зависший вызов Core Audio
  задерживает только этот поток. Во время записи — переопрос сессий раз в 0,5 с (`POLL_S`):
  заигравшие сессии приглушаются. Ошибки COM запись не роняют (лог — только числа и имя
  исключения); не перечислилось ни одно устройство или сломалось устройство по умолчанию —
  объекты Core Audio создаются заново при следующем обращении.
- **Возвращаем только своё**: для каждой изменённой сессии помним исходные громкость и
  беззвук, что поставили и что из этого ещё наше (`vol_ours`, `mute_ours`) — громкость и
  беззвук порознь. Меняем одно: в `duck` — громкость, в `mute` — беззвук. Если в сессии уже
  не поставленное нами (±0,005), пользователь поменял это сам — отпускаем (не возвращаем) и
  до конца записи сессию больше не приглушаем, в том числе после смены настроек. Изменение
  другой части нашу не отпускает: выключил звук приглушённой программы — громкость всё
  равно вернём (беззвук его); подвинул громкость выключенной — беззвук всё равно снимем.
- **Защита от «залипшего» беззвука** — `%APPDATA%\Rupor\duck.json`:
  `{version: 1, entries: [{instance, session, pid, volume, muted, set_volume, set_muted, ts,
  vol_ours, mute_ours}]}` (`instance` — GetSessionInstanceIdentifier, с pid; `session` —
  GetSessionIdentifier: приложение + устройство, без pid; `vol_ours`/`mute_ours` в файлах
  первой версии нет — выводятся из значений). Пишется ДО изменения громкости (атомарно: .tmp →
  replace) и только когда записи изменились (переопросы и ожидание закрытых приложений файл
  не переписывают); пустой — удаляется. При старте ядра (поток `rupor-duck`, запуск не ждёт)
  всё из файла возвращается по тем же правилам (на любом активном устройстве). Windows помнит
  громкость приложения между его запусками по `session`, поэтому запись о сессии, которой нет
  (приложение закрылось во время записи, выключение компьютера), ждёт НОВЫЙ экземпляр того же
  приложения: пришёл с нашими значениями — вернуть исходные (во время записи — считать его
  приглушённым нами), с другими — запись забыть. Пока такие записи есть, сессии проверяются
  раз в 3 с (`PENDING_POLL_S`); старше 7 дней (`PENDING_TTL_S`) — забываются; записей не
  больше 256.

```python
class Ducker:                     # duck.py
    def __init__(self, own_pids: Iterable[int] = (), path: Path | None = None,
                 backend_factory: Callable[[], Backend] | None = None) -> None
    def start(self) -> None       # поток rupor-duck; сначала — возврат из duck.json
    def duck(self, mode: str, level: int) -> None    # не блокирует
    def restore(self) -> None                        # не блокирует
    def flush(self, timeout: float = 5.0) -> bool    # дождаться очереди (конец сеанса, тесты)
    def close(self, timeout: float = 2.0) -> bool    # вернуть звук и остановить поток
def selftest(timeout: float = 10.0) -> tuple[bool, str]
    # «Core Audio: ok, сессий N, устройств вывода D»; перебор устройств не удался — False
```
`Backend.sessions() -> list[AudioSession]` (`instance, session, pid, system, active, default,
volume(), muted(), set_volume(v), set_muted(m)`) — `CoreAudioBackend` или фейк в тестах;
тесты движка по умолчанию получают `NullBackend` (`tests/conftest.py`).

### Расход ИИ и баланс (`engine._ai_call`, `storage.ai_usage`)

- Каждый вызов ИИ — очистка (`cleanup`, и «ждать», и «сначала сырой»), командный режим
  (`command`), кнопка проверки (`test`, WS `ai_test`) — пишется строкой `ai_usage`:
  провайдер, модель, фактический провайдер OpenRouter (`upstream`), токены, `cost_usd`
  (`usage.cost` OpenRouter; Anthropic — по цене семейства модели `ai.anthropic_price`,
  датированный id «claude-haiku-4-5-20251001» — как «claude-haiku-4-5»; модель не из таблицы
  и `claude -p` — NULL, «цена неизвестна»: в `avg_cost_per_call` не входит, считается в
  `unknown_cost_calls`; Ollama — 0),
  время, `ok`, `error_kind` (`AIError.kind` или имя исключения), процесс окна. Ошибка —
  `ok=0`, стоимость 0, модель — из настроек (`ai.provider_model`). «Как есть» (без сети) не
  пишется. После записи — событие `ai_usage` с суммой за сегодня.
- `ai_balance`: баланс ключа, который первым в очереди (см. «Список ключей ИИ»):
  OpenRouter — `ai.check_openrouter_key` (`/key` + `/credits`), Anthropic — оценка по введённому
  остатку. Кэш проверки 5 минут (неудачной — 1 минута), `force` — мимо кэша, смена ключа
  сбрасывает кэш. Ключей нет — `error` «Ключей ИИ нет…»; облачные ключи не используются
  (Ollama, Claude Code) — `error` «Облачные ключи не используются…», сеть не трогается. После
  удачного вызова OpenRouter баланс этого ключа обновляется в фоне, если кэш устарел.
- Баланс первого ключа в очереди < $1 — `notice` warn «На балансе <ключ> меньше $1 —
  пополните его, иначе ИИ-очистка перестанет работать (осталось $0.42)» с
  `action: "open_settings_ai"`, не чаще раза в сутки (`kv.ai_low_balance_notice`).
- Трей: UI формирует строку `ai_line` для `update_tray` из `ai_usage_stats` и `ai_balance`.

### Рассылка в RDP: только отмеченные сессии (`broadcast.py`)

- Ключ сессии — `winutil.rdp_session_key(title, process, cls)`: для mstsc (процесс
  `mstsc.exe` или класс `TscShellContainerClass`) — часть заголовка до « - » (также « – »,
  « — »): «srv-analytics - Подключение к удалённому рабочему столу» и «srv-analytics - Remote
  Desktop Connection» → «srv-analytics», «work - 10.0.0.5 - Remote Desktop Connection» (.rdp-
  файл) → «work»; менеджеры с вкладками — не по выбранному подключению (иначе галочка
  «терялась» бы при переключении вкладки): mRemoteNG («mRemoteNG - confCons.xml - srv-01») →
  «mRemoteNG - confCons.xml» (без файла подключений — «mRemoteNG»), RDCMan → «RDCMan»; прочие
  клиенты (добавленные в `rdp_processes`/`rdp_classes`) — заголовок целиком. Ключи
  сравниваются без учёта регистра.
- `broadcast_targets` — белый список ключей. Рассылка идёт ТОЛЬКО в открытые окна, чьи ключи
  отмечены. Нет ни одной отмеченной открытой сессии — текст не рассылается (остаётся в
  «Вставить последнее» и истории), `notice` warn «Не выбраны RDP-сессии для рассылки» с
  `action: "open_settings_insert"`.
- Миграция (`config.load`): в файле нет `broadcast_targets` (версии до 0.4 — «во все, кроме
  снятых галочек») → отмечаются открытые сейчас RDP-сессии, кроме тех, чей заголовок или
  ключ есть в `broadcast_excluded`; окон нет — пустой список; результат сразу сохраняется.
  `broadcast_excluded` больше не используется.
- Событие `recording.targets` — ключи отмеченных открытых сессий при рассылке.
- История рассылки: `title` — ключи сессий, куда ушёл текст.

### Командный режим
ptt + command_modifier (удержание) → говорим инструкцию → отпускаем. Ядро: сохранить буфер
(буфер занят — notice, копирование не отправляется), **Ctrl+Insert**, прочитать выделение (ждать
смены `GetClipboardSequenceNumber` до 400 мс, в RDP до 1000 мс), восстановить буфер. Ctrl+C —
только запасной путь и только там, где он не прерывает процесс (не терминалы, не IDE, не RDP и
менеджеры подключений, не консоли ВМ — `winutil.ctrl_c_unsafe`). Если буфер сменился уже после
таймаута, прежний всё равно возвращается. Нет выделения → notice «Выделите текст для командного
режима». Иначе `ai.command(instruction, selected)` → перед вставкой проверить: окно то же,
нажатий с момента чтения выделения не было, выделен всё тот же текст (прочитать ещё раз), а при
печати клавишами (RDP) в ответе не больше строк, чем в выделении (иначе лишние переводы строк
сработали бы как Enter). Не выполнено — notice warn и «Вставить последнее». В RDP без буфера
выделение не прочитать → notice.

## Tauri (Rust) — `ui/src-tauri`

- Tauri 2, плагины: `single-instance` (второй запуск → показать main), `autostart`
  (JS API во фронтенде), `dialog` (системный выбор папки моделей — кнопка «Обзор…» в
  `ModelFolder`; разрешение `dialog:allow-open` только окну `main`, `capabilities/main.json`;
  ветка `~2.7` — последняя под tauri 2.11, в `package.json` тоже `~2.7`), `notification`
  (не обязателен).
- Окна (`tauri.conf.json`):
  - `main`: `index.html`, 1080×720, min 900×600, `decorations: true`, `transparent: true`,
    `windowEffects: {effects: ["mica"]}`, `visible: false` (показывается из трея, мастером или при
    первом запуске), закрытие → скрыть.
  - `pill`: `pill.html`, 360×96, `transparent`, `decorations: false`, `alwaysOnTop`,
    `skipTaskbar`, `resizable: false`, `shadow: false`, `focus: false`, `visible: true`.
    После создания Rust ставит `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` — пилюля **никогда не
    забирает фокус** (иначе текст напечатается в неё). Пилюля **не пропадает**, пока Рупор
    работает (`pill.rs`): «поверх всех» подтверждается при каждой смене активного окна
    (`SetWinEventHook(EVENT_SYSTEM_FOREGROUND)`) и в начале записи (`pill_raise` из пилюли) —
    иначе её закрывает окно, которое тоже «поверх всех» и активировано позже (полноэкранный
    RDP); после `WM_DISPLAYCHANGE`/`WM_DPICHANGED`/`SPI_SETWORKAREA` пилюля вне всех мониторов
    возвращается вниз по центру основного (размер и отступ — в масштабе этого монитора);
    сбой процесса WebView2 (`ProcessFailed`, `pill::recovery`) окна `pill` или `main`: упала
    страница (вид 1/3) — перезагрузить окно; зависла страница пилюли (вид 2) — перезагрузить
    после 3 сообщений за минуту (зависание главного окна ждём); GPU и прочие процессы (вид 6 и
    др.) WebView2 поднимает сам — только журнал; автоматических перезагрузок окна — не больше 3
    за 5 минут; умер браузерный процесс (вид 0) — перезапустить оболочку (`--wait-pid`, не раньше
    2 мин после старта; раньше — вручную пунктом трея). Ошибка отрисовки страницы пилюли —
    React-граница `Guard` (`pill/main.tsx`): причина в ui.log (`pill_log`, не чаще раза в минуту),
    пилюля перерисовывается через 1 с; больше 5 раз за минуту — перезагрузка страницы (не
    больше 2 за 10 минут, счёт в `sessionStorage`), дальше перерисовка с нарастающей паузой до
    минуты. Ошибки вне отрисовки (обработчики, промисы) — тоже в ui.log, не чаще раза в 10 с.
- Команды (`invoke`):
  - `backend_info` → `{port, token}` (ждёт готовности ядра до 30 с, иначе ошибка);
  - `update_tray` `{state, paused, insert_mode?, style?, styles?: {id, name}[], ai_line?: string | null, rdp?: {key, title, included}[] | null, labels?: TrayLabels | null}`
    → обновить меню/иконку/подсказку трея. `labels` — подписи трея на языке интерфейса (см.
    «Локализация»). Ключи — snake_case, как в контракте: Rust
    разбирает тело `invoke` целиком (`tauri::ipc::Request` → `tray::TrayUpdate`), а не
    аргументами команды, поэтому отличает «ключа нет» от `null`; тело не объект — ошибка.
    `styles` — подменю «Стиль» (нет или `[]` — прежний список, по умолчанию 5 встроенных).
    `ai_line` — неактивная строка меню, например «ИИ: сегодня $0.03 · баланс $12.34»
    (формирует UI из `ai_usage_stats` + `ai_balance`; ИИ выключен — `null`, строки нет).
    `rdp` — подменю «Рассылка в RDP ▸» с флажками. Для `ai_line` и `rdp` (`tray::Patch`):
    **ключа нет — не менять** (Keep: частичный вызов, например только `{state, paused}`, не
    прячет строку и подменю), **`null` — скрыть** (Clear; `ai_line: ""` — тоже), **значение —
    показать** (Set; для `rdp` пустой список `[]` — неактивная заглушка «Нет открытых
    RDP-сессий»). UI присылает оба поля при каждом вызове; `rdp: null`, пока список сессий
    ещё не загружен. Подпись флажка — `key` (пустой — `title`): без управляющих символов,
    до 60 символов (строка ИИ — до 80), `&` экранируется; повторы ключа без учёта регистра и
    пустые ключи отбрасываются. Пункты подменю пересоздаются только при смене набора сессий,
    смена флажков их не пересоздаёт (меню не мигает). Ключ сессии в журнал не пишется;
  - `relaunch_admin` → ShellExecuteW "runas" на текущий exe с `--elevated`, затем выход;
  - `show_main` `{route?: string}`; `quit_app`;
  - `set_pill_passthrough` `{on: bool}` — не обязателен;
  - `pill_raise` — пилюлю наверх слоя «поверх всех» без активации (скрытую не трогает);
  - `pill_log` `{level: "error" | "warn" | "info", message}` — ошибка страницы пилюли в ui.log
    («Пилюля: …», одна строка, до 500 символов).
- События Rust → окна: `tray-action` `{id}` (id: `pause`, `mode:auto|type|clipboard`,
  `style:<id>`, `last_insert`, `rdp:<key>` — флажок сессии в подменю рассылки, key как
  пришёл; UI зовёт `rdp_toggle` и обновляет трей), `core-restarted` `{}`, `navigate` `{route}`.
- Трей: иконка по состоянию (idle серый, recording красный, processing/ai янтарный,
  paused тёмный, loading синий, error — значок ошибки), подсказка «Рупор — <статус>»
  («Rupor — <status>» по-английски; все подписи — из `labels`, см. «Локализация»); меню:
  - «Рупор: <статус>» (неактивный);
  - [строка расхода ИИ `ai_line`, неактивная — сразу под статусом];
  - —; Пауза (флажок); Режим вставки ▸; Стиль ▸;
  - [Рассылка в RDP ▸ — флажки открытых сессий или «Нет открытых RDP-сессий»];
  - Вставить последнее; Вернуть пилюлю (`pill_restore`: Rust ставит пилюлю вниз по центру
    основного монитора, шлёт `tray-action pill_restore` — UI сохраняет `pill_position: null`,
    — показывает, поднимает наверх и перезагружает страницу; пилюля выключена в настройках —
    открывает настройки; браузерный процесс WebView2 умер — перезапускает оболочку); —;
    Открыть Рупор; Выход.

  Пункты в скобках появляются по `update_tray` (см. выше). Клик по флажку Windows
  переключает сразу — Rust возвращает прежнее состояние и шлёт `tray-action`; новое
  приходит следующим `update_tray`, когда ядро применило действие. Левый клик — открыть main.
- Откуда UI берёт данные для трея (`App.tsx`, `lib/aiUsage.ts`, `lib/rdp.ts`):
  - `ai_usage_stats` — локальная SQLite ядра, перечитывается после каждого события
    `ai_usage` (сумма за сегодня из события подставляется сразу);
  - `ai_balance` — только пока ИИ включён (`ai_enabled`): при включении, затем раз в
    5 мин 5 с (кэш ядра — 5 мин, запрос не попадает в свежий кэш) и через 3 с после
    платного вызова ИИ (`cost_usd > 0`); `force: true` — только кнопка «Обновить» в
    «Настройки → ИИ → Расход». ИИ выключен — баланс не запрашивается, `ai_line: null`.
    Баланс неизвестен или ошибка — строка только с суммой за сегодня;
  - `rdp_windows` — раз в 15 с (окна RDP открываются без событий ядра), после каждого
    `rdp_toggle` и при смене `broadcast_targets` в событии `settings`.

## Фронтенд — `ui/`

Vite + React 18 + TypeScript strict + Tailwind + Framer Motion + lucide-react.
Шрифт — `@fontsource-variable/inter` (латиница и кириллица, без сети). Многостраничная сборка:
`index.html` (main) и `pill.html` (пилюля). Dev-сервер `http://localhost:1420`, сборка в `ui/dist`.

- `src/lib/ws.ts` — клиент: `backend_info` (вне Tauri, т. е. нет `window.__TAURI_INTERNALS__`, —
  из параметров адреса `?port=…&token=…`, чтобы UI можно было проверять в обычном браузере) → подключение, переподключение с паузой, очередь
  запросов, `call<T>(cmd, args)`, `on(event, fn)`; при `core-restarted` — переподключиться.
- `src/i18n/ru.json`, `en.json` — **все** строки интерфейса (одинаковые ключи); `t(key, params?)`,
  `tp(key, n)` — множественное число; выбор языка — см. «Локализация».
- Страницы main (боковое меню): Главная, История (вкладки «Лента» и «Черновик»), Словарь
  (вкладки «Термины» — с переключателем «Узнавать похожие написания», `dict_fuzzy`, и
  списком «Недавно добавлено» — что словарь выучил, с «Отменить» (`dict_learn_journal`,
  `dict_learn_undo`; обновляется по уведомлениям обучения), — и «Частые слова»), Сниппеты,
  Стили, Правила приложений, Настройки,
  Справка. Мастер первого запуска — полноэкранный маршрут, если `first_run_done=false`;
  его первый шаг — выбор языка.
- Модель и её папка:
  - мастер, шаг «Видеокарта и модель»: рекомендация `model_recommend` (кэш 10 мин на язык
    интерфейса — `reason` и `note` приходят на нём) и список моделей с размером, качеством и
    скоростью; выбор шлёт `set_settings {model, device, compute_type}` (`device` и
    `compute_type` — из `recommended`). Рекомендация подставляется сама один раз за проход
    мастера и только на первой установке: `setup_status.first_run_done=false`, модели
    `settings.model` нет на диске и она не качается (уже скачанную мастер молча не меняет; при
    повторном запуске из настроек ядро уже качает или держит модель, выбранную там). «Скачать»
    до прихода рекомендации закрепляет модель из настроек — пришедшая позже её не подменит.
    Не пришла — мастер работает с моделью из настроек. Ниже — `ModelFolder` («Куда скачать
    модель») и строки загрузки CUDA и модели; итог прошлого переноса или загрузки (ошибка,
    «Модель перенесена») строка модели сбрасывает при смене папки, идущий — нет;
  - `src/components/ModelFolder.tsx` — общий для мастера и «Настройки → Диктовка → Папка
    модели»: действующая папка (`setup_status.model_dir`), поле для пути, «Обзор…»
    (`lib/tauri.ts` `pickFolder` → plugin-dialog; вне Tauri кнопки нет), «По умолчанию» (при
    непустом `model_dir`, в подсказке — `model_dir_default`; если там записан явный путь к той
    же папке — сразу `model_dir_set {path: ""}` без окна: переносить нечего, ядро сохраняет
    `""`). Новая папка при скачанной модели — окно «Перенести модель?»: «Перенести»
    (`model_dir_set` `move: true`), «Не переносить» (`move: false`), «Отмена». Кнопки окна
    берут путь из ref, а не из замыкания: окно закрывается с анимацией, и второй щелчок по
    уходящему окну (двойной по «Перенести») ничего не шлёт; пока идёт перенос, начатый здесь,
    второй не отправляется. Прогресс переноса — события `progress` `what: "model"`, полоса
    уходит по `done` или по событию `settings` с новым `model_dir`; тост об успехе — `notice`
    ядра. В настройках та же полоса показывает загрузку модели (смена модели или папки качает
    её сюда), ошибка загрузки остаётся до новой загрузки, смены модели или папки; пошедшая
    загрузка убирает ошибку прошлого переноса. Пока модель качается или переносится, смена
    папки (и выбор модели в мастере) недоступна;
  - демо: `?mock=1&first_run=1&gpu=0` — компьютер без видеокарты NVIDIA (рекомендация `small`
    на процессоре); путь с `full` (`E:\full-disk\models`) — ошибка переноса «не хватает места».
- Маршруты (hash-роутер `src/lib/router.ts`, `#/<страница>/<вкладка>/<якорь>`; их же
  принимают `show_main {route}` и событие `navigate`):
  - `#/history/draft` — вкладка «Черновик»;
  - `#/dictionary/vocab` — вкладка «Частые слова» (личный словарь частот: `vocab_list`,
    `vocab_exclude`); ссылка на неё есть и в «Настройки → Диктовка»;
  - `#/settings/<раздел>` — `general | dictation | insert | ai | history | about`;
    `#/settings/ai` — сюда ведёт `notice.action = "open_settings_ai"` (там же блок «Расход»);
  - `#/settings/insert/sessions` — раздел «Вставка», прокрутка к блоку «Сессии для рассылки»
    с подсветкой (`useHashAnchor`); сюда ведёт `notice.action = "open_settings_insert"`
    («Не выбраны RDP-сессии для рассылки»): тост в main — `navigate`, кнопка в пилюле —
    `show_main {route: "settings/insert/sessions"}`.
- Пилюля: покой — маленькая полупрозрачная капсула; запись — живая волна (из `level`) и таймер;
  `hands_free` — другой цвет; `processing`/`ai` — анимация обработки (с подписью «ИИ» для `ai`);
  ошибка (`notice` level=error/warn) — красная подсветка с текстом 4 с, для
  `action=relaunch_admin` — кнопка «Перезапустить от администратора». Перетаскивание
  (`data-tauri-drag-region`), позиция сохраняется в `pill_position`. `pill_enabled=false` → скрыть.
- Тема: system/dark/light, акцент (CSS-переменная), эффект Mica (полупрозрачный фон).
  Анимации переходов 150–250 мс.

## Локализация (русский и английский)

Два языка — русский и английский — во всех трёх слоях: ядро, оболочка, интерфейс. Название
продукта: по-русски «Рупор», по-английски «Rupor» (в интерфейсе — ключ `app.name`, в трее —
подпись `labels.app`). `productName` в `tauri.conf.json` остаётся «Рупор» и **не меняется**:
от него зависят папка установки (`%LOCALAPPDATA%\Рупор`), ярлыки, ключи реестра установки
(`${UNINSTKEY}`, `${MANUKEY}`), описание процесса в диспетчере задач и «Автозагрузке»
(`FileDescription` в ресурсах exe), имя файла установщика и автозапуск у тех, кто уже поставил
программу. Сам exe называется `rupor.exe` (имя `[[bin]]` в `Cargo.toml`, в NSIS —
`MAINBINARYNAME`). На языке установщика — только подписи: заголовки и страницы мастера
(`LangString ^Name` / `^NameDA` в `windows/hooks.nsh`), сообщения хуков и `DisplayName` /
`Publisher` записи в «Установленных приложениях» (хук `NSIS_HOOK_POSTINSTALL`); тексты шаблона,
собранные из `${PRODUCTNAME}` при компиляции («Рупор <версия> is already installed…»), остаются
«Рупор». Установщик собирается как `Рупор_<версия>_x64-setup.exe`, а в GitHub Releases
выкладывается как `Rupor_<версия>_x64-setup.exe`: GitHub переименовывает файлы релиза с не-ASCII
символами в имени (порядок публикации — `docs/TESTING.md`, «Публикация релиза»).

### Язык интерфейса: настройка и разрешённый язык

- `settings.language`: `"system"` (по умолчанию) | `"ru"` | `"en"`. `"system"` — язык интерфейса
  Windows (`GetUserDefaultUILanguage`): основной язык (`PRIMARYLANGID`) русский `0x19`,
  белорусский `0x23`, украинский `0x22`, казахский `0x3F` → `ru`, любой другой → `en`; вызов
  не удался → `en`. Правило одно и то же в ядре (`i18n.lang_for_langid`), оболочке
  (`src/i18n.rs`) и интерфейсе (до подключения к ядру — по `navigator.language`).
- Разрешённый язык — `State.ui_language: "ru" | "en"` (в `hello`, `get_state`, `state`).
  `set_settings {patch: {language}}` → `Engine._apply_changes` → `i18n.set_language` → как при
  любой смене настроек, события `settings` и `state` (уже с новым `ui_language`). Перезапуск не
  нужен: следующий `tr()` — на новом языке; кэш `ai_balance` с текстом ошибки сбрасывается.
- Неверное значение — ошибка `set_settings` (текст на языке интерфейса); при загрузке файла —
  значение по умолчанию.

### Ядро: `i18n.py`, `locales/*.json`

```python
LANGUAGES = ("system", "ru", "en")            # значения настройки language
UI_LANGS = ("ru", "en")                        # разрешённый язык
def system_language() -> str                   # язык Windows → ru/en (кэш на процесс)
def resolve(setting: str | None) -> str        # system/ru/en → ru/en
def set_language(value: str | None) -> bool    # Engine: при создании и при смене language; True — язык сменился
def current() -> str                           # State.ui_language
def add_listener(fn: Callable[[str], None]) -> None    # fn(новый язык) после смены (трей pystray)
def tr(key: str, /, **params) -> str           # на текущем языке
def tr_in(lang: str, key: str, /, **params) -> str
def tr_or(key: str, fallback: str, /, **params) -> str  # нет ключа в каталоге — fallback
def has(key: str, lang: str | None = None) -> bool
def plural_form(n: float, lang: str | None = None) -> str  # CLDR: ru one/few/many/other, en one/other
def tp(key: str, n: float, /, **params) -> str  # с числом: шаблон <key>.<форма>, {n} — через fmt_num
def tp_in(lang: str, key: str, n: float, /, **params) -> str
class Msg(key, /, **params)                    # отложенный перевод: str(Msg) — на языке в момент показа
def fmt_num(value: float, decimals: int = 0, lang: str | None = None) -> str  # «1 234,5» / «1,234.5»
def selftest() -> tuple[bool, str]             # «локализация: ok (ru N, en N)» для --uia-selftest
```

- Каталоги `core/rupor/locales/ru.json` и `en.json` — плоские объекты `ключ → шаблон`, ключи
  **одинаковые**, кроме форм множественного числа (группы `api.*`, `cfg.*`, `setting.*`, `eng.*`, `ai.*`, `asr.*`, `fetch.*`,
  `cred.*`, `inj.*`, `learn.*`, `rec.*` (подбор модели), `tray.*`, `cli.*`, …). Подстановки — `str.format`
  (`tr("eng.mic_unavailable", error=e)`), фигурные скобки самого текста удвоены (`{{id}}`);
  недостающий параметр остаётся в тексте как `{name}`. Нет ключа в каталоге языка — берётся
  русский, нет и там — сам ключ (и предупреждение в журнал).
- Множественное число (`tp`): плоские ключи `<key>.one/.few/.many` в `ru.json` и
  `<key>.one/.other` в `en.json` (`eng.rdp_sent`, `tray.rdp_hint`…); у таких ключей паритет
  сверяется по базовому ключу, и у каждого языка должны быть ровно его формы
  (`catalog_problems`, его же вызывает `selftest`).
- Тексты, которые ядро запоминает и отдаёт позже (`Recognizer.last_error` — причина отказа
  CUDA, ошибка «нет модели»), хранятся как `Msg` и переводятся при выдаче: после смены языка
  `cuda_check` отвечает уже на новом.
- Служебные записи истории (`target: "test"` — проверка распознавания в мастере, `"rdp_all"` —
  рассылка) пишутся в базу нейтрально (`app = "rupor"`, `title = ""`); подписи на языке
  интерфейса подставляет `engine.history_display()` при выдаче (`history_list`, событие
  `history`). Записи прежних версий с русскими подписями узнаются по каталогам; поиск находит
  такие записи по подписям на обоих языках.
- Клавиши в текстах ошибок — `hotkeys.key_label()`: «ctrl+win» → «Ctrl + Win», «rctrl» →
  «Right Ctrl» / «Правый Ctrl» (ключи `key.*`).
- На языке интерфейса — всё, что видит пользователь: `notice.text`, `{ok: false, error}` ответов
  WS, тексты `ValueError` проверки настроек (названия настроек — `setting.<ключ>`), `progress.text`
  и ошибки загрузок, ошибки ИИ (`AIError`, ответ `ai_key_check`), Credential Manager, вставки,
  UI Automation и Core Audio, уведомления словаря («В словарь добавлено…» / “Added to
  dictionary…”), заголовки служебных записей истории, меню pystray старого режима (подписи
  считаются на лету, `add_listener`), справка CLI, `styles_list` (`engine.styles_localized()` →
  `ai.style.<id>.name` / `.description`).
- **Журналы (`log.*`) — всегда по-русски**, как и тексты самопроверок `uia.py`/`duck.py`,
  которые уходят только в журнал (`verify_core` ищет в них маркеры «своё поле: ok»,
  «Core Audio: ok»).
- Упаковка: `locales/*.json` — в `packaging/rupor-core.spec` и в `package-data` `pyproject.toml`;
  `verify_core.py` требует «локализация: ok» в самопроверке.
- Тесты — `tests/test_i18n.py`: паритет ключей и подстановок ru/en, в `en.json` нет кириллицы,
  каждый ключ, который код передаёт в `tr()`, есть в обоих каталогах, в модулях ядра нет
  русских строковых литералов вне журналов (кроме короткого белого списка), смена языка на
  лету через настоящий WS-сервер. `tests/conftest.py` считает «системный» язык русским на любой
  машине.

### Мусор распознавания (`asr.py`)

Неуверенный сегмент faster-whisper перераспознаёт с нарастающей температурой (до 1.0) — это
угадывание, отсюда иероглифы и наборы букв («隙 隅», «jpuy, jpac»); в тишине и музыке Whisper
«слышит» служебные фразы субтитров («ДИНАМИЧНАЯ МУЗЫКА»). `Recognizer._second_opinion`:

- `garbage_reason(text)` — заведомый мусор: символы чужих письменностей (иероглифы, кана,
  хангыль, арабица, иврит, индийские, тайская), текст целиком — фраза субтитров
  (`_HALLUCINATIONS`), зацикленный повтор (сжимаемость > 2,4 при длине от 40 символов);
- `guessing(stats)` — какой-то сегмент с температурой ≥ 0,6; `low_confidence(stats)` —
  средняя уверенность сегмента < −1,5;
- мусор или угадывание → второй проход без подсказки (её мог отравить прежний мусор из
  истории) и с температурой 0: чистый — он, тоже мусор — пустой текст («Речь не распознана»,
  в историю и частые слова не попадает); просто низкая уверенность — второй проход, берётся
  более уверенный из двух, мусорный повтор не берётся. В журнал — только причина, без текста.

### Язык распознавания (`asr.py`)

`settings.dictation_language`: `"auto"` (по умолчанию) | `"ru"` | `"en"`.

- `ru` / `en` — всегда этот язык: определение языка не запускается (раннее определение по
  первым 2,5 с тоже), `transcribe(language=…)` с заданным языком.
- `auto` — основной язык = язык интерфейса (`asr.primary_language`: `i18n.resolve(s.language)`),
  второй (en при русском интерфейсе, ru при английском) выбирается, только если модель уверена
  в нём не меньше `en_threshold` (0,8; теперь это порог **второго** языка, а не только
  английского). Определение не удалось — язык интерфейса. `asr.choose_language(s, detected,
  prob)`.
- Подсказка `initial_prompt` для английской фразы — без кириллицы (`asr.adapt_prompt`):
  латинские куски контекста окна и термины остаются в прежнем порядке, русские слова
  выбрасываются, дубли убираются. Для русской фразы подсказка не меняется.
- Язык результата — `result.language`, `HistoryItem.language`.

### Язык промптов ИИ (`ai.py`)

- **Очистка** — на языке **текста** (`ai.text_language(text, hint)`): кириллицей написано не
  меньше половины слов → `ru`; кириллицы нет и латинских слов ≥ 2 → `en`; иначе (одно-два
  слова, русская фраза из английских терминов, английская с русским именем) решает `hint` —
  язык распознавания, который движок передаёт как `cleanup(..., language=res.language)`.
  `ai_test` — язык по наличию кириллицы в тексте. Для `en` промпт собирается из
  `BASE_PROMPT_EN`, инструкции `STYLES_EN[style]` и строки «Terms and names from the user's
  dictionary…»; для `ru` — прежние `BASE_PROMPT` и `STYLES` (байт-в-байт как до локализации).
  Язык интерфейса на очистку не влияет.
- **Командный режим** — на языке **интерфейса** (`command_prompt()` → `COMMAND_PROMPT_EN` /
  `COMMAND_PROMPT`, метка инструкции «Instruction:» / «Инструкция:»).
- `PROMPT_VERSION` меняется при каждой правке любого из промптов. Замеры английской очистки
  (`tools/ai_bench.py --cases en`, `tools/data/cleanup_cases_en.json`) — в `docs/BENCH.md`,
  раздел 3.

### Оболочка (`ui/src-tauri/src/i18n.rs`)

- Язык при запуске — `language` из `settings.json` (ровно `ru` | `en`; `system`, нет ключа или
  неверное значение, в том числе `EN` / ` en `, — язык Windows по тому же правилу: так же
  строго, как `config._choice` в ядре). Дальше язык задаёт интерфейс —
  `update_tray.labels.lang` (`State.ui_language`), на лету.
- На этом языке: подписи трея до первого `labels`, заголовки окон при запуске («Рупор» /
  «Rupor», пилюля — «Рупор — пилюля» / «Rupor — indicator»), ошибки `backend_info`, `relaunch_admin`,
  команд пилюли и окно «не удалось запустить» (WebView2). Журнал `ui.log` и ошибки разбора
  аргументов `update_tray` — по-русски.
- `update_tray.labels` (`tray::TrayLabels`): `{lang?, app?, status_loading?, status_idle?,
  status_recording?, status_hands_free?, status_processing?, status_ai?, status_paused?,
  status_error?, status_starting?, status_restarting?, status_failed?,
  status_already_running?, pause?, insert_mode?, mode_auto?, mode_type?, mode_clipboard?,
  style?, rdp?, rdp_empty?, last_insert?, open?, quit?}`. **Ключа `labels` нет** — подписи
  прежние; **`null`** — встроенные на текущем языке оболочки; **объект** — встроенные на языке
  `lang`, поверх — присланные непустые строки (пустая строка или `null` у ключа — встроенная
  подпись, незнакомые ключи пропускаются, не строка — ошибка разбора всего вызова). Строка
  статуса — «{app}: {статус}», подсказка значка — «{app} — {статус}» (до 127 знаков UTF-16).
  Меню не перестраивается: меняется текст только изменившихся пунктов. Встроенные названия
  стилей переподписываются при смене языка, пока интерфейс не прислал свой `styles`.
  Встроенные тексты и таблица ключей — `ui/src-tauri/README.md`, «Язык оболочки».
- Установщик NSIS: `bundle.windows.nsis.languages: ["English", "Russian"]`,
  `displayLanguageSelector: true` — выбор языка при установке; сообщения хуков
  (`windows/hooks.nsh`) — `LangString` на обоих языках (коды 1033 и 1049: хуки подключаются
  раньше `MUI_LANGUAGE`).

### Интерфейс (`ui/src/i18n`)

- `ru.json` и `en.json` — все строки интерфейса, одинаковые ключи. Множественное число — через
  `Intl.PluralRules` (`tp(key, n)`: в русском `one`/`few`/`many`, в английском `one`/`other`);
  даты, время, числа, компактные числа — `Intl` по текущей локали; доллары в обоих языках —
  `$12.34`.
- Какой язык показывать (`syncLocale`): `settings.language`, если выбран явно (`ru`/`en`);
  при `system` — `State.ui_language`; до `hello` — `?lang=en|ru` из адреса (демо и проверки в
  браузере), иначе язык прошлого запуска (`localStorage["rupor.locale"]`), иначе
  `navigator.language` (ru/be/uk/kk → ru, иначе en).
  Смена — на лету: корни окон (`App`, `Pill`) перерисовываются, при новом `ui_language` окно
  перечитывает `styles_list`, заголовки окон — `setTitle(t("app.name"))` / `pill.windowTitle`,
  в `update_tray` уходит `labels` со всеми ключами и `lang`.
- «Настройки → Общие → Язык»: «Язык интерфейса» (Системный / Русский / English — названия
  языков всегда на своём языке) и «Язык диктовки» (Авто / Русский / English). Первый шаг
  мастера — выбор языка: язык Windows сохраняется как `"system"`, другой — явным `ru`/`en`.
- Демо-режим: `?mock=1&lang=en|ru` — «язык Windows» демо-ядра и язык демо-данных
  (`mock-data.en.json` / `mock-data.json`); работает и для `pill.html`.
- `npm run check:i18n`: паритет ключей ru/en, формы множественного числа для каждого языка,
  одинаковые подстановки `{…}`, в `en.json` нет кириллицы, в `src/**/*.ts(x)` вне `src/i18n`
  нет кириллицы в строках и JSX. `check:contract` проверяет смену `language` и
  `dictation_language` на живом ядре.

## Сборка (этап 6)

- `packaging/rupor-core.spec` — PyInstaller onedir `rupor-core` (windowed, без консоли), без DLL
  `nvidia/*` (они качаются при первом запуске в `%LOCALAPPDATA%\Rupor\cuda`) и без модели.
- `build.cmd` → PyInstaller → копия в `ui/src-tauri/resources/core` → проверка
  `packaging/verify_core.py` (ядро в ресурсах свежее и `--uia-selftest` проходит: UIA,
  частотные списки, «Core Audio: ok»; самопроверки без «Core Audio» — старое ядро; иначе
  сборка останавливается — установщик со старым ядром и новым UI не собрать) →
  `npm run tauri build` → `ui/src-tauri/target/release/bundle/nsis/*-setup.exe` (установка
  для текущего пользователя, без прав администратора). Собирать установщик в обход
  `build.cmd` (`npm run tauri build` на ресурсах, оставшихся от прошлой сборки) нельзя.

## Карта модулей

| Область | Файлы |
|---|---|
| Хранение | `core/rupor/storage.py`, `core/rupor/textproc.py`, `tests/test_storage.py`, `tests/test_textproc.py` |
| ИИ | `core/rupor/ai.py`, `core/rupor/credman.py`, `tests/test_ai.py`, `tests/test_credman.py`, `tools/ai_bench.py`, `tools/data/cleanup_cases.json`, `tools/data/cleanup_cases_en.json`, `tools/wer_bench.py`, `tools/data/asr_refs.json` |
| Локализация | `core/rupor/i18n.py`, `core/rupor/locales/ru.json`, `core/rupor/locales/en.json`, `tests/test_i18n.py`, `tests/test_asr_lang.py`; оболочка — `ui/src-tauri/src/i18n.rs`; интерфейс — `ui/src/i18n/*`, `ui/scripts/check-i18n.mjs`, `ui/src/lib/mock-data.en.json` |
| Загрузки и сборка | `core/rupor/fetch.py`, `packaging/**`, `build.cmd` |
| Движок | `core/rupor/engine.py`, `hotkeys.py`, `config.py`, `server.py`, `api.py`, `__main__.py`, `asr.py`, `audio.py`, `tray.py`, `inject.py`, `winutil.py`, `broadcast.py`, `sounds.py`, `learn.py`, `uia.py`, `vocab.py`, `duck.py`, `core/rupor/data/*`, `tests/test_hotkeys.py`, `tests/test_routing.py`, `tests/test_api.py`, `tests/test_learn.py`, `tests/test_learn_engine.py`, `tests/test_learn_storage.py`, `tests/test_uia.py`, `tests/test_packaging_verify.py`, `tests/test_vocab.py`, `tests/test_storage_vocab_usage.py`, `tests/test_title_rules_rdp.py`, `tests/test_api_usage_rules_vocab.py`, `tests/test_duck.py`, `tests/test_duck_engine.py`, `tests/conftest.py`, `tools/ws_smoke.py` |
| Оболочка (Rust) | `ui/src-tauri/**` |
| Интерфейс | `ui/*` (кроме `src-tauri`), `ui/src/**` |

Зависимости ядра (в т. ч. `websockets`, `comtypes`, а в dev — `pytest`, `mypy`, `pyinstaller`) —
в `pyproject.toml`.


## Список ключей ИИ

Ключей ИИ может быть сколько угодно (до 20), в том числе несколько у одного провайдера
(`rupor.keys`). Описание — в `settings.ai_keys` (id, провайдер, подпись, вкл/выкл, остаток,
введённый вручную), порядок списка — приоритет. Секрет — в Credential Manager:
`Rupor/openrouter` и `Rupor/anthropic` для первых ключей провайдера (id = имя провайдера,
так было до списка), `Rupor/<провайдер>/<id>` для остальных.

- **Очередь.** `ai_provider` / `command_provider` = `keys` — все включённые ключи по порядку;
  `openrouter` / `anthropic` — только ключи этого провайдера. Модель — по провайдеру ключа
  (`ai_model` для OpenRouter, `anthropic_model` для Anthropic).
- **Переход на следующий ключ.** Ошибка ключа или счёта (`auth`, `payment`, `forbidden`),
  лимит (`rate_limit`), сбой провайдера (`server`), сеть (`network`), неверный запрос
  (`bad_request`, например модели нет у этого провайдера) — сразу следующий ключ. Таймаут —
  нет: диктовка уже прождала `ai_timeout_s`. Плохой ответ модели (обрезан, отказ) — тоже нет.
  Ключ без секрета пропускается. Все ключи упали — наружу ошибка первого.
- **Пауза.** Упавший ключ уходит в конец очереди: `auth`/`payment`/`forbidden` — на 30 минут,
  `no_key`/`bad_request` — на 10, лимит, сбой, сеть и таймаут — на минуту. Так следующая
  диктовка не ждёт заведомо нерабочий ключ. Замена секрета или вкл/выкл ключа снимают паузу.
- **Расход по ключу.** `ai_usage.key_id` (схема базы 4) — какой ключ ответил;
  `Storage.ai_key_spend(id, since)`.
- **Баланс.** OpenRouter — по API (`/key` + `/credits`). У Anthropic API баланса нет: если
  пользователь ввёл остаток из Console, показывается оценка «остаток − расход Рупора через
  этот ключ с момента ввода» (расход считается по таблице цен `ANTHROPIC_PRICES`, модель не из
  таблицы оценку не уменьшает). Иначе — честное «баланс по API недоступен» и расход Рупора.
- **Переход со старых настроек.** Файл без `ai_keys`: ключи `Rupor/openrouter` и
  `Rupor/anthropic` становятся записями списка (выбранный провайдер первым), `ai_provider` и
  `command_provider` `openrouter`/`anthropic` → `keys`. Записи без секрета движок убирает при
  запуске.
