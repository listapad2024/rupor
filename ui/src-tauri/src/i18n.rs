//! Язык оболочки: подписи трея, пока интерфейс их не прислал, заголовки окон по умолчанию и
//! сообщения самой оболочки (ошибки `backend_info`, `relaunch_admin`, команд пилюли и окна
//! перевода встречи, окно ошибки запуска).
//!
//! Язык при запуске — `language` из settings.json (`ru` | `en`); `system` (по умолчанию) —
//! по языку интерфейса Windows (GetUserDefaultUILanguage): русский для русского, белорусского,
//! украинского и казахского, иначе английский — так же, как у ядра (core/rupor/i18n.py).
//! Дальше язык задаёт интерфейс: `update_tray.labels.lang` (разрешённый язык из `State`),
//! смена — на лету.
//!
//! Тексты — шаблоны с подстановками `{name}` ([`fill`]); у русского и английского набора одни
//! и те же поля (проверяет компилятор) и одни и те же подстановки (тест `same_placeholders`).
//! Журнал оболочки (`ui.log`) остаётся русским.

use std::fmt::Display;
use std::sync::atomic::{AtomicU8, Ordering};

use serde::Serialize;
use windows_sys::Win32::Globalization::GetUserDefaultUILanguage;

use crate::tray::TrayDefaults;

/// Разрешённый язык интерфейса.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Lang {
    Ru,
    En,
}

impl Lang {
    /// Ровно `ru` / `en`; прочее (в том числе `EN`, ` en `) — `None`. Строго, как ядро
    /// (`config._choice`): неверное значение `language` в settings.json ядро заменяет на
    /// `system`, и оболочка должна прийти к тому же языку, а не к своему.
    pub fn parse(s: &str) -> Option<Lang> {
        match s {
            "ru" => Some(Lang::Ru),
            "en" => Some(Lang::En),
            _ => None,
        }
    }

    pub fn code(self) -> &'static str {
        match self {
            Lang::Ru => "ru",
            Lang::En => "en",
        }
    }

    pub fn texts(self) -> &'static Texts {
        match self {
            Lang::Ru => &RU,
            Lang::En => &EN,
        }
    }

    fn to_u8(self) -> u8 {
        match self {
            Lang::Ru => 1,
            Lang::En => 2,
        }
    }

    fn from_u8(v: u8) -> Option<Lang> {
        match v {
            1 => Some(Lang::Ru),
            2 => Some(Lang::En),
            _ => None,
        }
    }
}

/// Текущий язык оболочки; 0 — ещё не определён (определится по Windows при первом запросе).
/// В тестах — русский: эталонные тексты в тестах русские и не зависят от языка Windows.
static CURRENT: AtomicU8 = AtomicU8::new(if cfg!(test) { 1 } else { 0 });

/// Текущий язык оболочки.
pub fn current() -> Lang {
    if let Some(lang) = Lang::from_u8(CURRENT.load(Ordering::Relaxed)) {
        return lang;
    }
    let lang = system();
    CURRENT.store(lang.to_u8(), Ordering::Relaxed);
    lang
}

/// Сменить язык оболочки. `true` — язык действительно сменился.
pub fn set_current(lang: Lang) -> bool {
    CURRENT.swap(lang.to_u8(), Ordering::Relaxed) != lang.to_u8()
}

/// Тексты на текущем языке.
pub fn t() -> &'static Texts {
    current().texts()
}

/// Значение настройки `language` (`system` | `ru` | `en`, нет ключа — `system`) → язык.
pub fn resolve(setting: Option<&str>) -> Lang {
    setting.and_then(Lang::parse).unwrap_or_else(system)
}

/// Язык интерфейса Windows, приведённый к ru/en.
pub fn system() -> Lang {
    // SAFETY: функция без аргументов, только читает настройку пользователя.
    from_langid(unsafe { GetUserDefaultUILanguage() })
}

/// LANGID → язык: основной язык (PRIMARYLANGID) русский, белорусский, украинский или
/// казахский — русский, иначе английский.
pub fn from_langid(langid: u16) -> Lang {
    const RU_FAMILY: [u16; 4] = [0x19, 0x23, 0x22, 0x3F];
    if RU_FAMILY.contains(&(langid & 0x3FF)) {
        Lang::Ru
    } else {
        Lang::En
    }
}

/// Подставить параметры в шаблон: `{name}` → значение. Неизвестные `{…}` остаются как есть.
pub fn fill(template: &str, params: &[(&str, &dyn Display)]) -> String {
    let mut out = template.to_string();
    for (name, value) in params {
        out = out.replace(&format!("{{{name}}}"), &value.to_string());
    }
    out
}

/// Все тексты оболочки на одном языке.
#[derive(Debug)]
pub struct Texts {
    /// Заголовок окна пилюли (виден разве что экранному диктору и в списках окон).
    pub pill_title: &'static str,
    /// Заголовок окна перевода встречи (до того как страница поставит свой).
    pub meeting_title: &'static str,
    /// Подписи трея по умолчанию; `tray.app` — название программы (заголовок главного окна).
    pub tray: TrayDefaults,
    /// Встроенные стили в трее до `update_tray.styles` — по порядку [`crate::tray::BUILTIN_STYLE_IDS`].
    pub styles: [&'static str; 5],

    /// Окно ошибки, если интерфейс не поднялся. `{error}`.
    pub startup_failed: &'static str,
    /// Внутренняя ошибка (упала фоновая задача). `{error}`.
    pub internal_error: &'static str,
    pub pill_not_found: &'static str,
    /// Окно перевода встречи не создалось. `{error}`.
    pub meeting_window_failed: &'static str,

    pub relaunch_pending: &'static str,
    pub relaunch_cancelled: &'static str,
    /// `{code}` — код ShellExecute.
    pub relaunch_failed: &'static str,
    /// `{error}`.
    pub exe_path_failed: &'static str,

    pub core_quitting: &'static str,
    /// `{seconds}`, `{reason}`.
    pub core_not_ready: &'static str,
    pub core_no_response: &'static str,
    pub core_already_running: &'static str,
    /// `{code}`.
    pub core_exited: &'static str,
    /// `{code}`.
    pub core_exited_at_start: &'static str,
    /// `{code}`, `{reason}` — последняя ошибка из журнала ядра.
    pub core_exited_at_start_reason: &'static str,
    pub core_not_found: &'static str,
    /// `{path}`, `{error}`.
    pub core_spawn_failed: &'static str,
    /// `{error}`.
    pub core_port_failed: &'static str,
    /// `{error}`.
    pub core_token_failed: &'static str,
    /// `{error}`.
    pub core_supervisor_failed: &'static str,
}

pub const RU: Texts = Texts {
    pill_title: "Рупор — пилюля",
    meeting_title: "Рупор — перевод встречи",
    tray: TrayDefaults {
        app: "Рупор",
        status_loading: "загрузка модели…",
        status_idle: "готов",
        status_recording: "запись",
        status_hands_free: "запись без рук",
        status_processing: "распознавание…",
        status_ai: "обработка ИИ…",
        status_paused: "пауза",
        status_error: "ошибка",
        status_starting: "запуск ядра…",
        status_restarting: "перезапуск ядра…",
        status_failed: "ядро не запускается",
        status_already_running: "уже запущен другой Рупор",
        status_meeting: "перевод встречи идёт",
        pause: "Пауза",
        insert_mode: "Режим вставки",
        mode_auto: "Авто",
        mode_type: "Печать клавишами",
        mode_clipboard: "Через буфер",
        style: "Стиль",
        rdp: "Рассылка в RDP",
        rdp_empty: "Нет открытых RDP-сессий",
        last_insert: "Вставить последнее",
        pill_restore: "Вернуть пилюлю",
        meeting: "Перевод встречи",
        meeting_running: "Перевод встречи (идёт)",
        open: "Открыть Рупор",
        quit: "Выход",
    },
    styles: ["Как есть", "Обычный", "Деловой", "Сообщение в мессенджер", "Для кода/промпта"],

    startup_failed: "Не удалось запустить Рупор: {error}\n\nПроверьте, что установлен \
                     Microsoft Edge WebView2 Runtime. Подробности — в \
                     %APPDATA%\\Rupor\\logs\\ui.log.",
    internal_error: "Внутренняя ошибка: {error}",
    pill_not_found: "Окно пилюли не найдено",
    meeting_window_failed: "Не удалось открыть окно перевода встречи: {error}",

    relaunch_pending: "Перезапуск от имени администратора уже запрошен — ответьте на запрос \
                       Windows.",
    relaunch_cancelled: "Перезапуск от имени администратора отменён.",
    relaunch_failed: "Не удалось перезапустить Рупор от имени администратора (код {code}).",
    exe_path_failed: "Не найден путь к Рупору: {error}",

    core_quitting: "Рупор завершает работу.",
    core_not_ready: "Ядро Рупора не запустилось за {seconds} с: {reason}. Подробности — в \
                     %APPDATA%\\Rupor\\logs.",
    core_no_response: "ядро не отвечает",
    core_already_running: "уже запущен другой Рупор (возможно, старая версия из автозагрузки) — \
                           закройте его, и этот Рупор подключится сам",
    core_exited: "ядро завершилось с кодом {code}",
    core_exited_at_start: "ядро завершилось при запуске с кодом {code}",
    core_exited_at_start_reason: "ядро завершилось при запуске с кодом {code}: {reason}",
    core_not_found: "Не найдено ядро Рупора (core\\rupor-core.exe рядом с программой). \
                     Переустановите Рупор.",
    core_spawn_failed: "Не удалось запустить ядро {path}: {error}",
    core_port_failed: "Не удалось выбрать порт для ядра: {error}",
    core_token_failed: "Не удалось создать токен ядра: {error}",
    core_supervisor_failed: "Не удалось запустить ядро: {error}",
};

pub const EN: Texts = Texts {
    pill_title: "Rupor — indicator",
    meeting_title: "Rupor — meeting translation",
    tray: TrayDefaults {
        app: "Rupor",
        status_loading: "Loading model…",
        status_idle: "Ready",
        status_recording: "Recording",
        status_hands_free: "Recording hands-free",
        status_processing: "Transcribing…",
        status_ai: "AI cleanup…",
        status_paused: "Paused",
        status_error: "Error",
        status_starting: "Starting engine…",
        status_restarting: "Restarting engine…",
        status_failed: "Engine won't start",
        status_already_running: "Another Rupor is already running",
        status_meeting: "Meeting translation running",
        pause: "Pause",
        insert_mode: "Insert method",
        mode_auto: "Auto",
        mode_type: "Type as keystrokes",
        mode_clipboard: "Paste via clipboard",
        style: "Style",
        rdp: "Broadcast to RDP",
        rdp_empty: "No open RDP sessions",
        last_insert: "Paste last",
        pill_restore: "Bring back the indicator",
        meeting: "Meeting translation",
        meeting_running: "Meeting translation (running)",
        open: "Open Rupor",
        quit: "Quit",
    },
    styles: ["As is", "Standard", "Business", "Chat message", "Code / prompt"],

    startup_failed: "Rupor couldn't start: {error}\n\nMake sure Microsoft Edge WebView2 Runtime \
                     is installed. Details are in %APPDATA%\\Rupor\\logs\\ui.log.",
    internal_error: "Internal error: {error}",
    pill_not_found: "The pill window wasn't found",
    meeting_window_failed: "Couldn't open the meeting translation window: {error}",

    relaunch_pending: "A restart as administrator has already been requested — respond to the \
                       Windows prompt.",
    relaunch_cancelled: "Restart as administrator was canceled.",
    relaunch_failed: "Couldn't restart Rupor as administrator (code {code}).",
    exe_path_failed: "Couldn't find the Rupor executable: {error}",

    core_quitting: "Rupor is shutting down.",
    core_not_ready: "Rupor's engine didn't start within {seconds} s: {reason}. Details are in \
                     %APPDATA%\\Rupor\\logs.",
    core_no_response: "the engine isn't responding",
    core_already_running: "another Rupor is already running (maybe an older version started \
                           with Windows) — close it, and this one will connect on its own",
    core_exited: "the engine exited with code {code}",
    core_exited_at_start: "the engine exited on startup with code {code}",
    core_exited_at_start_reason: "the engine exited on startup with code {code}: {reason}",
    core_not_found: "Rupor's engine is missing (core\\rupor-core.exe next to the program). \
                     Reinstall Rupor.",
    core_spawn_failed: "Couldn't start the engine {path}: {error}",
    core_port_failed: "Couldn't pick a port for the engine: {error}",
    core_token_failed: "Couldn't create the engine token: {error}",
    core_supervisor_failed: "Couldn't start the engine: {error}",
};

#[cfg(test)]
impl Texts {
    /// Все тексты с именами — для проверок паритета.
    fn pairs(&self) -> Vec<(String, &'static str)> {
        let mut v: Vec<(String, &'static str)> = vec![
            ("pill_title".into(), self.pill_title),
            ("meeting_title".into(), self.meeting_title),
            ("startup_failed".into(), self.startup_failed),
            ("internal_error".into(), self.internal_error),
            ("pill_not_found".into(), self.pill_not_found),
            ("meeting_window_failed".into(), self.meeting_window_failed),
            ("relaunch_pending".into(), self.relaunch_pending),
            ("relaunch_cancelled".into(), self.relaunch_cancelled),
            ("relaunch_failed".into(), self.relaunch_failed),
            ("exe_path_failed".into(), self.exe_path_failed),
            ("core_quitting".into(), self.core_quitting),
            ("core_not_ready".into(), self.core_not_ready),
            ("core_no_response".into(), self.core_no_response),
            ("core_already_running".into(), self.core_already_running),
            ("core_exited".into(), self.core_exited),
            ("core_exited_at_start".into(), self.core_exited_at_start),
            ("core_exited_at_start_reason".into(), self.core_exited_at_start_reason),
            ("core_not_found".into(), self.core_not_found),
            ("core_spawn_failed".into(), self.core_spawn_failed),
            ("core_port_failed".into(), self.core_port_failed),
            ("core_token_failed".into(), self.core_token_failed),
            ("core_supervisor_failed".into(), self.core_supervisor_failed),
        ];
        v.extend(self.tray.pairs().into_iter().map(|(k, s)| (format!("tray.{k}"), s)));
        v.extend(self.styles.iter().enumerate().map(|(i, s)| (format!("styles.{i}"), *s)));
        v
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::collections::BTreeSet;

    fn placeholders(s: &str) -> BTreeSet<String> {
        let mut out = BTreeSet::new();
        let mut rest = s;
        while let Some(i) = rest.find('{') {
            let tail = &rest[i + 1..];
            let Some(j) = tail.find('}') else { break };
            out.insert(tail[..j].to_string());
            rest = &tail[j + 1..];
        }
        out
    }

    #[test]
    fn langid_families() {
        assert_eq!(from_langid(0x0419), Lang::Ru); // ru-RU
        assert_eq!(from_langid(0x0423), Lang::Ru); // be-BY
        assert_eq!(from_langid(0x0422), Lang::Ru); // uk-UA
        assert_eq!(from_langid(0x043F), Lang::Ru); // kk-KZ
        assert_eq!(from_langid(0x0819), Lang::Ru); // ru-MD: другой подъязык, тот же основной
        assert_eq!(from_langid(0x0409), Lang::En); // en-US
        assert_eq!(from_langid(0x0809), Lang::En); // en-GB
        assert_eq!(from_langid(0x0407), Lang::En); // de-DE
        assert_eq!(from_langid(0x0415), Lang::En); // pl-PL
        assert_eq!(from_langid(0), Lang::En);
    }

    #[test]
    fn setting_values() {
        assert_eq!(resolve(Some("ru")), Lang::Ru);
        assert_eq!(resolve(Some("en")), Lang::En);
        // system, пусто и неизвестное — по Windows; регистр и пробелы не прощаем, как ядро
        // (config._choice): иначе на русской Windows при "EN" ядро и интерфейс были бы
        // русскими, а трей и окна — английскими
        assert_eq!(resolve(Some("system")), system());
        assert_eq!(resolve(Some("de")), system());
        assert_eq!(resolve(Some("EN")), system());
        assert_eq!(resolve(Some(" en ")), system());
        assert_eq!(resolve(Some("Ru")), system());
        assert_eq!(resolve(Some("")), system());
        assert_eq!(Lang::parse("EN"), None);
        assert_eq!(Lang::parse(" en "), None);
        assert_eq!(resolve(None), system());
        assert_eq!(Lang::parse("ru").map(Lang::code), Some("ru"));
        assert_eq!(serde_json::to_value(Lang::En).unwrap(), serde_json::json!("en"));
    }

    #[test]
    fn fill_params() {
        assert_eq!(
            fill(EN.relaunch_failed, &[("code", &42)]),
            "Couldn't restart Rupor as administrator (code 42)."
        );
        assert_eq!(
            fill(RU.core_not_ready, &[("seconds", &30), ("reason", &RU.core_no_response)]),
            "Ядро Рупора не запустилось за 30 с: ядро не отвечает. Подробности — в \
             %APPDATA%\\Rupor\\logs."
        );
        // неизвестный параметр не мешает, недостающий остаётся как есть
        assert_eq!(fill("{a} {b}", &[("a", &1), ("c", &3)]), "1 {b}");
    }

    /// Одинаковые подстановки в русском и английском тексте, никаких пустых строк,
    /// в английских текстах нет кириллицы.
    #[test]
    fn same_placeholders() {
        let ru = RU.pairs();
        let en = EN.pairs();
        assert_eq!(ru.len(), en.len());
        for ((k_ru, s_ru), (k_en, s_en)) in ru.iter().zip(&en) {
            assert_eq!(k_ru, k_en);
            assert!(!s_ru.trim().is_empty() && !s_en.trim().is_empty(), "пустой текст {k_ru}");
            assert_eq!(placeholders(s_ru), placeholders(s_en), "подстановки в {k_ru}");
            assert!(
                !s_en.chars().any(|c| matches!(c, 'А'..='я' | 'Ё' | 'ё')),
                "кириллица в английском тексте {k_en}: {s_en}"
            );
            // переносы строк в исходнике не оставляют двойных пробелов
            assert!(!s_ru.contains("  ") && !s_en.contains("  "), "двойной пробел в {k_ru}");
        }
    }

    #[test]
    fn names() {
        assert_eq!(RU.tray.app, "Рупор");
        assert_eq!(EN.tray.app, "Rupor");
        assert_eq!(Lang::Ru.texts().tray.quit, "Выход");
        assert_eq!(Lang::En.texts().tray.quit, "Quit");
        // в тестах язык оболочки — русский, от Windows не зависит
        assert_eq!(current(), Lang::Ru);
        assert_eq!(t().tray.app, "Рупор");
    }

    /// Каталог строк репозитория (JSON) как плоский словарь `a.b.c` → строка.
    fn catalog(rel: &str) -> std::collections::HashMap<String, String> {
        fn walk(
            prefix: &str,
            v: &serde_json::Value,
            out: &mut std::collections::HashMap<String, String>,
        ) {
            match v {
                serde_json::Value::Object(m) => {
                    for (k, v) in m {
                        let key =
                            if prefix.is_empty() { k.clone() } else { format!("{prefix}.{k}") };
                        walk(&key, v, out);
                    }
                }
                serde_json::Value::String(s) => {
                    out.insert(prefix.to_string(), s.clone());
                }
                _ => {}
            }
        }
        let path = std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join(rel);
        let text = std::fs::read_to_string(&path)
            .unwrap_or_else(|e| panic!("не читается {}: {e}", path.display()));
        let v: serde_json::Value =
            serde_json::from_str(text.trim_start_matches('\u{feff}')).expect("JSON");
        let mut out = std::collections::HashMap::new();
        walk("", &v, &mut out);
        out
    }

    /// Ключ `update_tray.labels` → ключ каталога интерфейса, из которого App.tsx его шлёт.
    fn ui_key(tray_key: &str) -> String {
        match tray_key {
            "app" => "app.name".into(),
            "mode_auto" => "insertModes.auto".into(),
            k if k.starts_with("status_") => format!("tray.status.{}", &k["status_".len()..]),
            k => {
                // insert_mode → tray.insertMode
                let mut out = String::from("tray.");
                let mut upper = false;
                for c in k.chars() {
                    if c == '_' {
                        upper = true;
                    } else if upper {
                        out.push(c.to_ascii_uppercase());
                        upper = false;
                    } else {
                        out.push(c);
                    }
                }
                out
            }
        }
    }

    /// Встроенные строки (до подключения интерфейса или без ядра) совпадают с тем, что потом
    /// пришлёт интерфейс: иначе пункты трея и заголовок пилюли заметно переименовывались бы
    /// при первом `update_tray.labels`. Подписи трея и заголовки — из ui/src/i18n/<lang>.json
    /// (по той же раскладке ключей, что в App.tsx), встроенные стили — из каталога ядра
    /// core/rupor/locales/<lang>.json (их имена интерфейс берёт из `styles_list`).
    #[test]
    fn builtin_texts_match_ui_and_core() {
        let app_tsx =
            std::fs::read_to_string(concat!(env!("CARGO_MANIFEST_DIR"), "/../src/App.tsx"))
                .expect("ui/src/App.tsx");
        for (lang, texts) in [("ru", &RU), ("en", &EN)] {
            let ui = catalog(&format!("../src/i18n/{lang}.json"));
            let core = catalog(&format!("../../core/rupor/locales/{lang}.json"));
            for (key, builtin) in texts.tray.pairs() {
                let uk = ui_key(key);
                assert!(
                    app_tsx.contains(&format!("{key}: t(\"{uk}\")")),
                    "App.tsx шлёт labels.{key} не из {uk} — поправьте ui_key в тесте"
                );
                assert_eq!(
                    ui.get(&uk).map(String::as_str),
                    Some(builtin),
                    "{lang}: tray.{key} ↔ {uk}"
                );
            }
            assert_eq!(
                ui.get("pill.windowTitle").map(String::as_str),
                Some(texts.pill_title),
                "{lang}: pill_title ↔ pill.windowTitle"
            );
            assert_eq!(
                ui.get("meeting.windowTitle").map(String::as_str),
                Some(texts.meeting_title),
                "{lang}: meeting_title ↔ meeting.windowTitle"
            );
            for (id, builtin) in crate::tray::BUILTIN_STYLE_IDS.iter().zip(texts.styles) {
                let ck = format!("ai.style.{id}.name");
                assert_eq!(core.get(&ck).map(String::as_str), Some(builtin), "{lang}: стиль {id}");
            }
        }
    }
}
