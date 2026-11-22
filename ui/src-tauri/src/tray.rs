//! Трей: иконка по состоянию, подсказка «Рупор — <статус>», меню.
//!
//! Пункты меню, которые должен выполнить фронтенд (через WS к ядру), приходят в окна событием
//! `tray-action` `{id}`: `pause`, `mode:auto|type|clipboard`, `style:<id>`, `last_insert`,
//! `rdp:<key>` (флажок сессии в «Рассылка в RDP», ключ — как прислал фронтенд).
//! «Открыть Рупор», «Вернуть пилюлю», «Перевод встречи» (показать/скрыть окно перевода) и
//! «Выход» выполняет сама оболочка. Левый клик по значку — главное окно.
//!
//! Меню: статус, [строка расхода ИИ], —, Пауза, Режим вставки ▸, Стиль ▸, [Рассылка в RDP ▸],
//! Вставить последнее, Вернуть пилюлю, Перевод встречи, —, Открыть Рупор, Выход. Пункты в скобках появляются, когда фронтенд
//! присылает `ai_line` / `rdp`, и перестраиваются только при изменении данных.
//!
//! Идёт перевод встречи (`update_tray.meeting_running`, от главного окна по статусу ядра) —
//! пункт «Перевод встречи (идёт)», а подсказка значка без диктовки — «<app> <версия> —
//! перевод встречи идёт» (окно перевода могло быть скрыто: перевод идёт «в трее»).
//!
//! Подписи меню и подсказки значка — [`TrayTexts`]: до первого `update_tray` со словарём
//! `labels` — встроенные на языке оболочки ([`crate::i18n`]), потом — от интерфейса. Смена
//! подписей меню не перестраивает: новый текст получают только изменившиеся пункты.

use std::collections::HashSet;
use std::sync::{Mutex, MutexGuard};

use serde::{Deserialize, Deserializer, Serialize};
use tauri::image::Image;
use tauri::menu::{CheckMenuItem, Menu, MenuItem, PredefinedMenuItem, Submenu};
use tauri::tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent};
use tauri::{AppHandle, Emitter, Manager, Wry};

use crate::i18n::{self, Lang};

pub const TRAY_ID: &str = "rupor";

const ICON_IDLE: &[u8] = include_bytes!("../icons/tray/idle.png");
const ICON_RECORDING: &[u8] = include_bytes!("../icons/tray/recording.png");
const ICON_PROCESSING: &[u8] = include_bytes!("../icons/tray/processing.png");
const ICON_PAUSED: &[u8] = include_bytes!("../icons/tray/paused.png");
const ICON_LOADING: &[u8] = include_bytes!("../icons/tray/loading.png");
const ICON_ERROR: &[u8] = include_bytes!("../icons/tray/error.png");

/// Встроенные стили (ai.STYLES), по порядку подписей `i18n::Texts::styles`. Фронтенд
/// присылает свой список (с подписями на языке интерфейса) в `update_tray.styles`.
pub const BUILTIN_STYLE_IDS: [&str; 5] = ["as_is", "normal", "business", "messenger", "code"];

/// id пунктов меню, которые появляются по данным фронтенда.
const AI_LINE_ID: &str = "ai_line";
const RDP_MENU_ID: &str = "rdp_menu";
const RDP_NONE_ID: &str = "rdp_none";
/// Префикс id флажков сессий: `rdp:<key>` — он же id события `tray-action`.
pub const RDP_PREFIX: &str = "rdp:";
/// «Перевод встречи»: показать/скрыть окно перевода (оболочка сама, `tray-action` не шлётся).
const MEETING_ID: &str = "meeting";

/// Предельная длина подписей (символов): заголовок окна может быть очень длинным.
const AI_LINE_MAX: usize = 80;
const RDP_LABEL_MAX: usize = 60;
/// Постоянные пункты меню (подписи от интерфейса).
const LABEL_MAX: usize = 60;
/// Строка статуса «Рупор: <статус>».
const STATUS_MAX: usize = 80;
/// Подсказка значка: Windows хранит до 128 знаков UTF-16 вместе с завершающим нулём.
const TOOLTIP_MAX: usize = 127;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum IconKind {
    Idle,
    Recording,
    Processing,
    Paused,
    Loading,
    Error,
}

impl IconKind {
    fn bytes(self) -> &'static [u8] {
        match self {
            IconKind::Idle => ICON_IDLE,
            IconKind::Recording => ICON_RECORDING,
            IconKind::Processing => ICON_PROCESSING,
            IconKind::Paused => ICON_PAUSED,
            IconKind::Loading => ICON_LOADING,
            IconKind::Error => ICON_ERROR,
        }
    }
}

/// Состояние ядра, известное только оболочке (до того, как фронтенд пришлёт `update_tray`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CoreStatus {
    Starting,
    Restarting,
    /// Ядро не запустить: его нет, или оно раз за разом завершается, не успев открыть порт.
    Failed,
    /// Мьютекс ядра занят: работает другой Рупор (например, старая версия из автозагрузки).
    AlreadyRunning,
}

/// Строки трея: встроенные ([`TrayDefaults`], ru/en — в `i18n`), действующие ([`TrayTexts`])
/// и присланные интерфейсом ([`TrayLabels`]) — с одним и тем же набором ключей.
macro_rules! tray_texts {
    ($($(#[$doc:meta])* $key:ident,)+) => {
        /// Встроенные подписи трея на одном языке (`i18n::RU.tray`, `i18n::EN.tray`).
        #[derive(Debug, Clone, Copy, PartialEq, Eq)]
        pub struct TrayDefaults {
            $($(#[$doc])* pub $key: &'static str,)+
        }

        /// Действующие подписи трея: встроенные на языке оболочки, поверх — присланные
        /// интерфейсом в `update_tray.labels`. Текст уже без управляющих символов.
        #[derive(Debug, Clone, PartialEq, Eq, Serialize)]
        pub struct TrayTexts {
            $($(#[$doc])* pub $key: String,)+
        }

        /// Аргумент `update_tray.labels` — подписи трея от интерфейса. Любой ключ можно не
        /// присылать: нет ключа, `null` или пустая строка — встроенная подпись на языке
        /// `lang`. Незнакомые ключи пропускаются.
        #[derive(Debug, Clone, Default, PartialEq, Eq, Deserialize)]
        #[serde(default)]
        pub struct TrayLabels {
            /// Язык интерфейса (`ru` | `en`, `State.ui_language`): на нём встроенные подписи
            /// для недостающих ключей и сообщения самой оболочки. Нет ключа или незнакомое
            /// значение — язык оболочки не меняется.
            pub lang: Option<String>,
            $($(#[$doc])* pub $key: Option<String>,)+
        }

        impl TrayDefaults {
            pub fn texts(&self) -> TrayTexts {
                TrayTexts { $($key: self.$key.to_string(),)+ }
            }

            #[cfg(test)]
            pub fn pairs(&self) -> Vec<(&'static str, &'static str)> {
                vec![$((stringify!($key), self.$key),)+]
            }
        }

        impl TrayTexts {
            /// Ключи `labels` (кроме `lang`).
            #[cfg(test)]
            pub const KEYS: &'static [&'static str] = &[$(stringify!($key),)+];

            /// Эти подписи, поверх — непустые подписи из `labels`.
            fn merged(mut self, labels: &TrayLabels) -> TrayTexts {
                $(if let Some(v) = labels.$key.as_deref().and_then(clean_text) {
                    self.$key = v;
                })+
                self
            }
        }
    };
}

tray_texts! {
    /// Название программы: строка статуса «<app>: <статус>» и подсказка «<app> — <статус>».
    app,
    /// Статусы по `State.state` ядра (с `paused` — `status_paused`).
    status_loading,
    status_idle,
    status_recording,
    status_hands_free,
    status_processing,
    status_ai,
    status_paused,
    status_error,
    /// Статусы, которые ставит сама оболочка ([`CoreStatus`]).
    status_starting,
    status_restarting,
    status_failed,
    status_already_running,
    /// Подсказка значка, пока идёт перевод встречи, а диктовки нет.
    status_meeting,
    /// Пункты меню.
    pause,
    insert_mode,
    mode_auto,
    mode_type,
    mode_clipboard,
    style,
    rdp,
    rdp_empty,
    last_insert,
    pill_restore,
    /// Показать или скрыть окно перевода встречи.
    meeting,
    /// …то же, пока перевод встречи идёт.
    meeting_running,
    open,
    quit,
}

/// Элемент списка стилей из `update_tray` (необязательный аргумент `styles`).
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct StyleItem {
    pub id: String,
    pub name: String,
}

/// Элемент подменю «Рассылка в RDP» из `update_tray` (необязательный аргумент `rdp`).
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct RdpItem {
    /// Ключ сессии: имя сервера для mstsc, для прочих клиентов — заголовок окна.
    pub key: String,
    #[serde(default)]
    pub title: String,
    #[serde(default)]
    pub included: bool,
}

/// Необязательное поле `update_tray`: ключа нет — оставить как было, `null` — убрать пункт
/// (для `labels` — вернуть встроенные подписи), значение — показать. Так частичный вызов
/// (например, только смена состояния) не прячет строку расхода и подменю рассылки.
#[derive(Debug, Clone, Default, PartialEq)]
pub enum Patch<T> {
    #[default]
    Keep,
    Clear,
    Set(T),
}

impl<'de, T: Deserialize<'de>> Deserialize<'de> for Patch<T> {
    fn deserialize<D: Deserializer<'de>>(d: D) -> Result<Self, D::Error> {
        // нет ключа — `#[serde(default)]` (Keep); `null` — None
        Ok(Option::<T>::deserialize(d)?.map_or(Patch::Clear, Patch::Set))
    }
}

/// Аргументы команды `update_tray` — тело вызова `invoke`, ключи в snake_case:
/// `{state, paused, insert_mode?, style?, styles?, ai_line?, rdp?, labels?}`.
#[derive(Debug, Clone, Deserialize, PartialEq)]
pub struct TrayUpdate {
    pub state: String,
    pub paused: bool,
    #[serde(default)]
    pub insert_mode: Option<String>,
    #[serde(default)]
    pub style: Option<String>,
    /// `[{id, name}]` для подменю «Стиль»; нет или пусто — без изменений.
    #[serde(default)]
    pub styles: Option<Vec<StyleItem>>,
    /// Неактивная строка «ИИ: сегодня $0.03 · баланс $12.34»; `null` или "" — скрыть.
    #[serde(default)]
    pub ai_line: Patch<String>,
    /// Открытые RDP-сессии `[{key, title, included}]` → подменю с флажками; `[]` — пункт
    /// «Нет открытых RDP-сессий»; `null` — подменю скрыто.
    #[serde(default)]
    pub rdp: Patch<Vec<RdpItem>>,
    /// Подписи меню и подсказки значка на языке интерфейса ([`TrayLabels`]); нет ключа —
    /// прежние, `null` — встроенные на текущем языке оболочки.
    #[serde(default)]
    pub labels: Patch<TrayLabels>,
    /// Идёт ли перевод встречи (`MeetingStatus.running`); нет ключа — как было.
    #[serde(default)]
    pub meeting_running: Option<bool>,
}

impl TrayUpdate {
    pub fn from_json(body: &serde_json::Value) -> Result<Self, String> {
        // serde принял бы и массив по порядку полей — аргументы invoke всегда объект
        if !body.is_object() {
            return Err("update_tray: аргументы должны быть объектом JSON".into());
        }
        // и словарь подписей — только объект (массив serde разобрал бы по порядку полей)
        if body.get("labels").is_some_and(|l| !l.is_object() && !l.is_null()) {
            return Err("update_tray: неверные аргументы: labels должен быть объектом".into());
        }
        Self::deserialize(body).map_err(|e| format!("update_tray: неверные аргументы: {e}"))
    }
}

/// Событие `tray-action`.
#[derive(Debug, Clone, Serialize)]
pub struct TrayAction {
    pub id: String,
}

/// Текст без управляющих символов, с одиночными пробелами, без пробелов по краям.
fn clean(text: &str) -> String {
    text.split(|c: char| c.is_control() || c.is_whitespace())
        .filter(|p| !p.is_empty())
        .collect::<Vec<_>>()
        .join(" ")
}

/// [`clean`]; пустой текст — `None` (подпись из `labels` не годится, берётся встроенная).
fn clean_text(text: &str) -> Option<String> {
    Some(clean(text)).filter(|s| !s.is_empty())
}

/// Обрезать до `max` единиц (`units` — сколько единиц занимает символ) с многоточием в конце;
/// пробел перед многоточием не остаётся.
fn shorten(text: String, max: usize, units: impl Fn(char) -> usize) -> String {
    if text.chars().map(&units).sum::<usize>() <= max {
        return text;
    }
    let budget = max.saturating_sub(1); // «…» — один символ и один знак UTF-16
    let mut out = String::new();
    let mut used = 0;
    for c in text.chars() {
        used += units(c);
        if used > budget {
            break;
        }
        out.push(c);
    }
    out.truncate(out.trim_end().len());
    out.push('…');
    out
}

/// Подпись пункта меню из чужого текста: без управляющих символов и лишних пробелов,
/// не длиннее `max` символов, `&` удвоен (иначе Windows сочтёт его мнемоникой).
fn menu_label(text: &str, max: usize) -> String {
    shorten(clean(text), max, |_| 1).replace('&', "&&")
}

/// Подсказка значка: без управляющих символов, не длиннее [`TOOLTIP_MAX`] знаков UTF-16
/// (иначе Windows обрежет её сам и без завершающего нуля). `&` здесь не мнемоника.
fn tooltip_text(text: &str) -> String {
    shorten(clean(text), TOOLTIP_MAX, char::len_utf16)
}

/// Версия оболочки — в подсказке значка, чтобы было видно, какая запущена.
const APP_VERSION: &str = env!("CARGO_PKG_VERSION");

/// Строка статуса в меню («Рупор: готов») и подсказка значка («Рупор 0.5.0 — готов»).
fn status_texts(t: &TrayTexts, status: &str) -> (String, String) {
    (
        menu_label(&format!("{}: {status}", t.app), STATUS_MAX),
        tooltip_text(&format!("{} {APP_VERSION} — {status}", t.app)),
    )
}

/// Постоянные пункты меню, подписи которых задают [`TrayTexts`] (строку статуса собирает
/// [`status_texts`]).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
enum Label {
    Pause,
    InsertMode,
    ModeAuto,
    ModeType,
    ModeClipboard,
    Style,
    Rdp,
    RdpEmpty,
    LastInsert,
    PillRestore,
    Meeting,
    MeetingRunning,
    Open,
    Quit,
}

/// Режимы вставки: id (в `mode:<id>` и `insert_mode`) → пункт с подписью.
const MODES: [(&str, Label); 3] =
    [("auto", Label::ModeAuto), ("type", Label::ModeType), ("clipboard", Label::ModeClipboard)];

impl Label {
    const ALL: [Label; 14] = [
        Label::Pause,
        Label::InsertMode,
        Label::ModeAuto,
        Label::ModeType,
        Label::ModeClipboard,
        Label::Style,
        Label::Rdp,
        Label::RdpEmpty,
        Label::LastInsert,
        Label::PillRestore,
        Label::Meeting,
        Label::MeetingRunning,
        Label::Open,
        Label::Quit,
    ];

    fn text(self, t: &TrayTexts) -> &str {
        match self {
            Label::Pause => &t.pause,
            Label::InsertMode => &t.insert_mode,
            Label::ModeAuto => &t.mode_auto,
            Label::ModeType => &t.mode_type,
            Label::ModeClipboard => &t.mode_clipboard,
            Label::Style => &t.style,
            Label::Rdp => &t.rdp,
            Label::RdpEmpty => &t.rdp_empty,
            Label::LastInsert => &t.last_insert,
            Label::PillRestore => &t.pill_restore,
            Label::Meeting => &t.meeting,
            Label::MeetingRunning => &t.meeting_running,
            Label::Open => &t.open,
            Label::Quit => &t.quit,
        }
    }

    /// Готовая подпись пункта меню.
    fn menu_text(self, t: &TrayTexts) -> String {
        menu_label(self.text(t), LABEL_MAX)
    }
}

/// Пункт «Перевод встречи» — его подпись: пока перевод идёт, — «… (идёт)».
fn meeting_label(running: bool) -> Label {
    if running {
        Label::MeetingRunning
    } else {
        Label::Meeting
    }
}

/// Статус для подсказки значка и строки статуса: идёт перевод встречи, а диктовки нет
/// (готов, не пауза) — «перевод встречи идёт».
fn status_with_meeting(
    state: &str,
    paused: bool,
    meeting: bool,
    t: &TrayTexts,
) -> (IconKind, String) {
    let (icon, text) = describe(state, paused, t);
    if meeting && !paused && state == "idle" {
        return (icon, t.status_meeting.clone());
    }
    (icon, text)
}

/// Пункты, чьи подписи поменялись: только они получают новый текст, меню не перестраивается.
fn changed_labels(old: &TrayTexts, new: &TrayTexts) -> Vec<Label> {
    Label::ALL.into_iter().filter(|l| l.text(old) != l.text(new)).collect()
}

/// Что сделать с подписями по `update_tray.labels`.
#[derive(Debug, PartialEq)]
struct LabelsPlan {
    /// Язык оболочки после вызова.
    lang: Lang,
    texts: TrayTexts,
    /// Пункты меню с новыми подписями.
    changed: Vec<Label>,
}

/// `lang` и `cur` — язык и подписи сейчас. `None` — ничего не меняется.
fn plan_labels(lang: Lang, cur: &TrayTexts, patch: Patch<TrayLabels>) -> Option<LabelsPlan> {
    let (new_lang, texts) = match patch {
        Patch::Keep => return None,
        Patch::Clear => (lang, lang.texts().tray.texts()),
        Patch::Set(labels) => {
            let l = labels.lang.as_deref().and_then(Lang::parse).unwrap_or(lang);
            (l, l.texts().tray.texts().merged(&labels))
        }
    };
    if new_lang == lang && texts == *cur {
        return None;
    }
    Some(LabelsPlan { lang: new_lang, changed: changed_labels(cur, &texts), texts })
}

/// Встроенное название стиля на языке `lang` (не встроенный стиль — `None`).
fn builtin_style_name(lang: Lang, id: &str) -> Option<&'static str> {
    BUILTIN_STYLE_IDS.iter().position(|s| *s == id).map(|i| lang.texts().styles[i])
}

/// Строка подменю «Рассылка в RDP».
#[derive(Debug, Clone, PartialEq)]
struct RdpRow {
    key: String,
    label: String,
    included: bool,
}

/// Сессии для подменю: без пустых ключей и повторов (ключи — без учёта регистра, первая
/// сессия выигрывает; id пунктов меню должны быть уникальны).
fn rdp_rows(items: Vec<RdpItem>) -> Vec<RdpRow> {
    let mut seen = HashSet::new();
    let mut rows = Vec::new();
    for it in items {
        if it.key.trim().is_empty() || !seen.insert(it.key.to_lowercase()) {
            continue;
        }
        let mut label = menu_label(&it.key, RDP_LABEL_MAX);
        if label.is_empty() {
            label = menu_label(&it.title, RDP_LABEL_MAX);
        }
        if label.is_empty() {
            continue;
        }
        rows.push(RdpRow { key: it.key, label, included: it.included });
    }
    rows
}

/// Что сделать со строкой расхода ИИ.
#[derive(Debug, PartialEq)]
enum AiPlan {
    Nothing,
    Show(String),
    Text(String),
    Hide,
}

fn plan_ai(current: Option<&str>, patch: Patch<String>) -> AiPlan {
    let want = match patch {
        Patch::Keep => return AiPlan::Nothing,
        Patch::Clear => None,
        Patch::Set(s) => Some(menu_label(&s, AI_LINE_MAX)).filter(|s| !s.is_empty()),
    };
    match (current, want) {
        (None, None) => AiPlan::Nothing,
        (Some(_), None) => AiPlan::Hide,
        (None, Some(t)) => AiPlan::Show(t),
        (Some(c), Some(t)) if c == t => AiPlan::Nothing,
        (Some(_), Some(t)) => AiPlan::Text(t),
    }
}

/// Что сделать с подменю «Рассылка в RDP». Пункты пересоздаются, только когда меняется
/// набор сессий; смена флажков — без перестройки (меню не мигает).
#[derive(Debug, PartialEq)]
enum RdpPlan {
    Nothing,
    /// Подменю было скрыто — заполнить и вставить.
    Show(Vec<RdpRow>),
    /// Другой набор сессий — пересоздать пункты.
    Rebuild(Vec<RdpRow>),
    /// Те же сессии, другие флажки.
    Checks(Vec<RdpRow>),
    Hide,
}

fn plan_rdp(current: Option<&[RdpRow]>, patch: Patch<Vec<RdpItem>>) -> RdpPlan {
    let rows = match patch {
        Patch::Keep => return RdpPlan::Nothing,
        Patch::Clear if current.is_some() => return RdpPlan::Hide,
        Patch::Clear => return RdpPlan::Nothing,
        Patch::Set(items) => rdp_rows(items),
    };
    let Some(cur) = current else { return RdpPlan::Show(rows) };
    let same_items = cur.len() == rows.len()
        && cur.iter().zip(&rows).all(|(a, b)| a.key == b.key && a.label == b.label);
    if !same_items {
        RdpPlan::Rebuild(rows)
    } else if cur == rows.as_slice() {
        RdpPlan::Nothing
    } else {
        RdpPlan::Checks(rows)
    }
}

/// Иконка и текст статуса по `State.state` ядра.
pub fn describe(state: &str, paused: bool, t: &TrayTexts) -> (IconKind, String) {
    if paused {
        return (IconKind::Paused, t.status_paused.clone());
    }
    let (icon, text) = match state {
        "loading" => (IconKind::Loading, &t.status_loading),
        "idle" => (IconKind::Idle, &t.status_idle),
        "recording" => (IconKind::Recording, &t.status_recording),
        "hands_free" => (IconKind::Recording, &t.status_hands_free),
        "processing" => (IconKind::Processing, &t.status_processing),
        "ai" => (IconKind::Processing, &t.status_ai),
        "paused" => (IconKind::Paused, &t.status_paused),
        "error" => (IconKind::Error, &t.status_error),
        other => return (IconKind::Idle, other.to_string()),
    };
    (icon, text.clone())
}

fn describe_core(status: CoreStatus, t: &TrayTexts) -> (IconKind, String) {
    let (icon, text) = match status {
        CoreStatus::Starting => (IconKind::Loading, &t.status_starting),
        CoreStatus::Restarting => (IconKind::Loading, &t.status_restarting),
        CoreStatus::Failed => (IconKind::Error, &t.status_failed),
        CoreStatus::AlreadyRunning => (IconKind::Error, &t.status_already_running),
    };
    (icon, text.clone())
}

struct View {
    icon: Option<IconKind>,
    /// Показанные строка статуса и подсказка — чтобы не дёргать трей без изменений.
    status_line: String,
    tooltip: String,
    /// Язык встроенных подписей (для ключей, которых нет в `labels`, и встроенных стилей).
    lang: Lang,
    /// Действующие подписи меню.
    texts: TrayTexts,
    paused: bool,
    insert_mode: String,
    style: String,
    styles: Vec<(String, CheckMenuItem<Wry>)>,
    /// В подменю «Стиль» ещё встроенные стили (фронтенд не присылал `styles`): их подписи
    /// меняются вместе с языком.
    styles_builtin: bool,
    /// Показанная строка расхода ИИ (уже подпись меню); None — строки нет в меню.
    ai_line: Option<String>,
    /// Сессии в подменю «Рассылка в RDP»; None — подменю нет в меню.
    rdp_rows: Option<Vec<RdpRow>>,
    /// Флажки сессий, в том же порядке, что `rdp_rows`.
    rdp_items: Vec<CheckMenuItem<Wry>>,
    /// Идёт перевод встречи (подпись пункта и подсказка значка).
    meeting_running: bool,
    /// Показанная подпись пункта «Перевод встречи».
    meeting_shown: String,
}

pub struct TrayMenu {
    menu: Menu<Wry>,
    status: MenuItem<Wry>,
    pause: CheckMenuItem<Wry>,
    mode_menu: Submenu<Wry>,
    modes: Vec<(String, CheckMenuItem<Wry>)>,
    style_menu: Submenu<Wry>,
    ai_item: MenuItem<Wry>,
    rdp_menu: Submenu<Wry>,
    rdp_none: MenuItem<Wry>,
    last: MenuItem<Wry>,
    pill_restore: MenuItem<Wry>,
    meeting: MenuItem<Wry>,
    open: MenuItem<Wry>,
    quit: MenuItem<Wry>,
    view: Mutex<View>,
}

impl TrayMenu {
    fn view(&self) -> MutexGuard<'_, View> {
        self.view.lock().unwrap_or_else(|e| e.into_inner())
    }

    /// Новый текст постоянного пункта меню.
    fn set_label(&self, label: Label, text: &str) -> tauri::Result<()> {
        match label {
            Label::Pause => self.pause.set_text(text),
            Label::InsertMode => self.mode_menu.set_text(text),
            Label::ModeAuto | Label::ModeType | Label::ModeClipboard => {
                let id = MODES.iter().find(|(_, l)| *l == label).map(|(id, _)| *id);
                match self.modes.iter().find(|(m, _)| Some(m.as_str()) == id) {
                    Some((_, item)) => item.set_text(text),
                    None => Ok(()),
                }
            }
            Label::Style => self.style_menu.set_text(text),
            Label::Rdp => self.rdp_menu.set_text(text),
            Label::RdpEmpty => self.rdp_none.set_text(text),
            Label::LastInsert => self.last.set_text(text),
            Label::PillRestore => self.pill_restore.set_text(text),
            // у пункта встречи — две подписи: какую показать, решает set_meeting_item
            Label::Meeting | Label::MeetingRunning => self.meeting.set_text(text),
            Label::Open => self.open.set_text(text),
            Label::Quit => self.quit.set_text(text),
        }
    }
}

fn style_item(
    app: &AppHandle,
    id: &str,
    name: &str,
    checked: bool,
) -> tauri::Result<CheckMenuItem<Wry>> {
    CheckMenuItem::with_id(app, format!("style:{id}"), name, true, checked, None::<&str>)
}

/// Создать значок в трее. Вызывается из `setup`; подписи — встроенные на языке оболочки.
pub fn create(app: &AppHandle) -> tauri::Result<()> {
    let lang = i18n::current();
    let texts = lang.texts().tray.texts();
    let (icon, status_text) = describe_core(CoreStatus::Starting, &texts);
    let (status_line, tooltip) = status_texts(&texts, &status_text);
    let item = |id: &str, label: Label| {
        MenuItem::with_id(app, id, label.menu_text(&texts), true, None::<&str>)
    };

    let status = MenuItem::with_id(app, "status", &status_line, false, None::<&str>)?;
    let pause = CheckMenuItem::with_id(
        app,
        "pause",
        Label::Pause.menu_text(&texts),
        true,
        false,
        None::<&str>,
    )?;
    let mut modes = Vec::new();
    for (id, label) in MODES {
        let item = CheckMenuItem::with_id(
            app,
            format!("mode:{id}"),
            label.menu_text(&texts),
            true,
            id == "auto",
            None::<&str>,
        )?;
        modes.push((id.to_string(), item));
    }
    let mode_menu = Submenu::with_id(app, "mode", Label::InsertMode.menu_text(&texts), true)?;
    for (_, item) in &modes {
        mode_menu.append(item)?;
    }
    let style_menu = Submenu::with_id(app, "style", Label::Style.menu_text(&texts), true)?;
    let mut styles = Vec::new();
    for (id, name) in BUILTIN_STYLE_IDS.into_iter().zip(lang.texts().styles) {
        let item = style_item(app, id, name, id == "normal")?;
        style_menu.append(&item)?;
        styles.push((id.to_string(), item));
    }
    // Строка расхода ИИ и подменю рассылки создаются сразу, а в меню попадают по данным
    // фронтенда (update_tray: ai_line, rdp).
    let ai_item = MenuItem::with_id(app, AI_LINE_ID, "", false, None::<&str>)?;
    let rdp_menu = Submenu::with_id(app, RDP_MENU_ID, Label::Rdp.menu_text(&texts), true)?;
    let rdp_none = MenuItem::with_id(
        app,
        RDP_NONE_ID,
        Label::RdpEmpty.menu_text(&texts),
        false,
        None::<&str>,
    )?;
    let last = item("last_insert", Label::LastInsert)?;
    let pill_restore = item("pill_restore", Label::PillRestore)?;
    let meeting = item(MEETING_ID, Label::Meeting)?;
    let meeting_shown = Label::Meeting.menu_text(&texts);
    let open = item("open", Label::Open)?;
    let quit = item("quit", Label::Quit)?;
    let menu = Menu::with_items(
        app,
        &[
            &status,
            &PredefinedMenuItem::separator(app)?,
            &pause,
            &mode_menu,
            &style_menu,
            &last,
            &pill_restore,
            &meeting,
            &PredefinedMenuItem::separator(app)?,
            &open,
            &quit,
        ],
    )?;

    TrayIconBuilder::with_id(TRAY_ID)
        .icon(Image::from_bytes(icon.bytes())?)
        .tooltip(&tooltip)
        .menu(&menu)
        .show_menu_on_left_click(false)
        .on_menu_event(|app, event| on_menu(app, event.id().as_ref()))
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                crate::show_main(tray.app_handle(), None);
            }
        })
        .build(app)?;

    app.manage(TrayMenu {
        menu,
        status,
        pause,
        mode_menu,
        modes,
        style_menu,
        ai_item,
        rdp_menu,
        rdp_none,
        last,
        pill_restore,
        meeting,
        open,
        quit,
        view: Mutex::new(View {
            icon: Some(icon),
            status_line,
            tooltip,
            lang,
            texts,
            paused: false,
            insert_mode: "auto".into(),
            style: "normal".into(),
            styles,
            styles_builtin: true,
            ai_line: None,
            rdp_rows: None,
            rdp_items: Vec::new(),
            meeting_running: false,
            meeting_shown,
        }),
    });
    Ok(())
}

fn on_menu(app: &AppHandle, id: &str) {
    match id {
        "open" => crate::show_main(app, None),
        "pill_restore" => crate::pill::restore(app),
        MEETING_ID => crate::meeting::tray_toggle(app),
        "quit" => crate::quit(app),
        "status" | AI_LINE_ID | RDP_NONE_ID => {}
        _ => {
            // Windows сам переключает галочку при клике — возвращаем фактическое состояние,
            // новое придёт от фронтенда через update_tray, когда ядро применит действие.
            if let Some(menu) = app.try_state::<TrayMenu>() {
                let v = menu.view();
                sync_checks(&menu, &v);
            }
            // имя RDP-сессии (или заголовок окна клиента) в журнал не пишем
            let shown = if id.starts_with(RDP_PREFIX) { "rdp:…" } else { id };
            crate::log_info!("Трей: {shown}");
            if let Err(e) = app.emit("tray-action", TrayAction { id: id.to_string() }) {
                crate::log_warn!("Не удалось отправить tray-action: {e}");
            }
        }
    }
}

fn sync_checks(menu: &TrayMenu, v: &View) {
    let _ = menu.pause.set_checked(v.paused);
    for (id, item) in &menu.modes {
        let _ = item.set_checked(*id == v.insert_mode);
    }
    for (id, item) in &v.styles {
        let _ = item.set_checked(*id == v.style);
    }
    if let Some(rows) = &v.rdp_rows {
        for (row, item) in rows.iter().zip(&v.rdp_items) {
            let _ = item.set_checked(row.included);
        }
    }
}

/// Позиция сразу после пункта `id` в корне меню (нет такого — в конец).
fn position_after(menu: &Menu<Wry>, id: &str) -> Result<usize, String> {
    let items = menu.items().map_err(|e| e.to_string())?;
    Ok(items.iter().position(|i| i.id() == id).map_or(items.len(), |p| p + 1))
}

/// Подписи из `update_tray.labels`: новый текст — только изменившимся пунктам; сменился
/// язык — и язык оболочки ([`i18n::set_current`]), и встроенные стили, если они ещё в меню.
/// Строку статуса и подсказку потом соберёт [`set_icon_and_text`] из новых подписей.
fn apply_labels(menu: &TrayMenu, v: &mut View, patch: Patch<TrayLabels>) -> Result<(), String> {
    let Some(plan) = plan_labels(v.lang, &v.texts, patch) else { return Ok(()) };
    let mut result = Ok(());
    if plan.lang != v.lang {
        if i18n::set_current(plan.lang) {
            crate::log_info!("Язык оболочки: {}", plan.lang.code());
        }
        if v.styles_builtin {
            for (id, item) in &v.styles {
                if let Some(name) = builtin_style_name(plan.lang, id) {
                    if let Err(e) = item.set_text(name) {
                        result = result.and(Err(e.to_string()));
                    }
                }
            }
        }
    }
    for label in &plan.changed {
        if matches!(label, Label::Meeting | Label::MeetingRunning) {
            continue; // подпись пункта встречи — set_meeting_item (зависит от сеанса)
        }
        if let Err(e) = menu.set_label(*label, &label.menu_text(&plan.texts)) {
            result = result.and(Err(e.to_string()));
        }
    }
    v.lang = plan.lang;
    v.texts = plan.texts;
    result
}

fn apply_ai_line(menu: &TrayMenu, v: &mut View, patch: Patch<String>) -> Result<(), String> {
    match plan_ai(v.ai_line.as_deref(), patch) {
        AiPlan::Nothing => {}
        AiPlan::Text(text) => {
            menu.ai_item.set_text(&text).map_err(|e| e.to_string())?;
            v.ai_line = Some(text);
        }
        AiPlan::Show(text) => {
            menu.ai_item.set_text(&text).map_err(|e| e.to_string())?;
            let pos = position_after(&menu.menu, "status")?;
            menu.menu.insert(&menu.ai_item, pos).map_err(|e| e.to_string())?;
            v.ai_line = Some(text);
        }
        AiPlan::Hide => {
            v.ai_line = None;
            menu.menu.remove(&menu.ai_item).map_err(|e| e.to_string())?;
        }
    }
    Ok(())
}

/// Убрать из подменю рассылки все пункты (флажки сессий или заглушку).
fn clear_rdp_items(menu: &TrayMenu, v: &mut View) {
    v.rdp_items.clear();
    let n = menu.rdp_menu.items().map_or(0, |items| items.len());
    for _ in 0..n {
        if !matches!(menu.rdp_menu.remove_at(0), Ok(Some(_))) {
            break;
        }
    }
}

/// Заполнить подменю рассылки: флажки сессий или неактивная заглушка, если сессий нет.
fn fill_rdp_items(
    app: &AppHandle,
    menu: &TrayMenu,
    v: &mut View,
    rows: &[RdpRow],
) -> Result<(), String> {
    if rows.is_empty() {
        return menu.rdp_menu.append(&menu.rdp_none).map_err(|e| e.to_string());
    }
    for row in rows {
        let item = CheckMenuItem::with_id(
            app,
            format!("{RDP_PREFIX}{}", row.key),
            &row.label,
            true,
            row.included,
            None::<&str>,
        )
        .map_err(|e| e.to_string())?;
        menu.rdp_menu.append(&item).map_err(|e| e.to_string())?;
        v.rdp_items.push(item);
    }
    Ok(())
}

fn apply_rdp(
    app: &AppHandle,
    menu: &TrayMenu,
    v: &mut View,
    patch: Patch<Vec<RdpItem>>,
) -> Result<(), String> {
    match plan_rdp(v.rdp_rows.as_deref(), patch) {
        RdpPlan::Nothing => {}
        // флажки выставит sync_checks
        RdpPlan::Checks(rows) => v.rdp_rows = Some(rows),
        RdpPlan::Rebuild(rows) => {
            clear_rdp_items(menu, v);
            // до заполнения — пустой список: при ошибке флажки не разойдутся с пунктами
            v.rdp_rows = Some(Vec::new());
            fill_rdp_items(app, menu, v, &rows)?;
            v.rdp_rows = Some(rows);
        }
        RdpPlan::Show(rows) => {
            // подменю заполняется до вставки в меню — пункты не появляются по одному
            clear_rdp_items(menu, v);
            let shown = fill_rdp_items(app, menu, v, &rows).and_then(|()| {
                let pos = position_after(&menu.menu, "style")?;
                menu.menu.insert(&menu.rdp_menu, pos).map_err(|e| e.to_string())
            });
            if let Err(e) = shown {
                clear_rdp_items(menu, v);
                return Err(e);
            }
            v.rdp_rows = Some(rows);
        }
        RdpPlan::Hide => {
            v.rdp_rows = None;
            let removed = menu.menu.remove(&menu.rdp_menu).map_err(|e| e.to_string());
            clear_rdp_items(menu, v);
            removed?;
        }
    }
    Ok(())
}

/// Подпись пункта «Перевод встречи» — по сеансу и подписям; меняется, только если другая.
fn set_meeting_item(menu: &TrayMenu, v: &mut View) -> Result<(), String> {
    let text = meeting_label(v.meeting_running).menu_text(&v.texts);
    if v.meeting_shown != text {
        menu.meeting.set_text(&text).map_err(|e| e.to_string())?;
        v.meeting_shown = text;
    }
    Ok(())
}

/// Иконка, подсказка и строка статуса; меняется только то, что изменилось.
fn set_icon_and_text(app: &AppHandle, menu: &TrayMenu, v: &mut View, icon: IconKind, status: &str) {
    let (line, tip) = status_texts(&v.texts, status);
    if let Some(tray) = app.tray_by_id(TRAY_ID) {
        if v.icon != Some(icon) {
            match Image::from_bytes(icon.bytes()) {
                Ok(img) => {
                    if tray.set_icon(Some(img)).is_ok() {
                        v.icon = Some(icon);
                    }
                }
                Err(e) => crate::log_warn!("Иконка трея не читается: {e}"),
            }
        }
        if v.tooltip != tip && tray.set_tooltip(Some(&tip)).is_ok() {
            v.tooltip = tip;
        }
    }
    if v.status_line != line {
        let _ = menu.status.set_text(&line);
        v.status_line = line;
    }
}

/// Команда `update_tray`: состояние от фронтенда (из события `state` ядра).
/// Строка расхода ИИ, подменю рассылки и подписи меняются, только если их поля пришли
/// (см. [`Patch`]).
pub fn update(app: &AppHandle, args: TrayUpdate) -> Result<(), String> {
    let TrayUpdate {
        state,
        paused,
        insert_mode,
        style,
        styles,
        ai_line,
        rdp,
        labels,
        meeting_running,
    } = args;
    let menu = app.try_state::<TrayMenu>().ok_or("Трей ещё не создан")?;
    let mut v = menu.view();
    let insert_mode = insert_mode.unwrap_or_else(|| v.insert_mode.clone());
    let style = style.unwrap_or_else(|| v.style.clone());
    if let Some(list) = styles.filter(|l| !l.is_empty()) {
        // список от фронтенда — уже на языке интерфейса, по языку оболочки его не переводим
        v.styles_builtin = false;
        let same = list.len() == v.styles.len()
            && list.iter().zip(&v.styles).all(|(n, (id, item))| {
                n.id == *id && item.text().map(|t| t == n.name).unwrap_or(false)
            });
        if !same {
            for (_, item) in v.styles.drain(..) {
                let _ = menu.style_menu.remove(&item);
            }
            for s in &list {
                let item =
                    style_item(app, &s.id, &s.name, s.id == style).map_err(|e| e.to_string())?;
                menu.style_menu.append(&item).map_err(|e| e.to_string())?;
                v.styles.push((s.id.clone(), item));
            }
        }
    }
    // Ошибка в подписях и новых пунктах не должна мешать значку и флажкам — вернём её в конце.
    // Подписи — до статуса: строка статуса собирается уже из новых.
    let labels = apply_labels(&menu, &mut v, labels)
        .map_err(|e| format!("Подписи трея обновились не полностью: {e}"));
    let ai = apply_ai_line(&menu, &mut v, ai_line)
        .map_err(|e| format!("Строка расхода ИИ в трее не обновилась: {e}"));
    let rdp = apply_rdp(app, &menu, &mut v, rdp)
        .map_err(|e| format!("Подменю рассылки в RDP не обновилось: {e}"));
    v.paused = paused;
    v.insert_mode = insert_mode;
    v.style = style;
    if let Some(on) = meeting_running {
        v.meeting_running = on;
    }
    let meeting = set_meeting_item(&menu, &mut v)
        .map_err(|e| format!("Пункт «Перевод встречи» в трее не обновился: {e}"));
    sync_checks(&menu, &v);
    let (icon, text) = status_with_meeting(&state, paused, v.meeting_running, &v.texts);
    set_icon_and_text(app, &menu, &mut v, icon, &text);
    for e in [&labels, &ai, &rdp, &meeting].into_iter().filter_map(|r| r.as_ref().err()) {
        crate::log_warn!("{e}");
    }
    labels.and(ai).and(rdp).and(meeting)
}

/// Состояние ядра со стороны оболочки: запуск, перезапуск, не запускается, мешает другой Рупор.
/// Можно звать из любого потока: работа с меню уходит в главный поток без ожидания
/// (иначе поток супервизора и `update_tray` могли бы заблокировать друг друга).
pub fn set_core_status(app: &AppHandle, status: CoreStatus) {
    let handle = app.clone();
    let _ = app.run_on_main_thread(move || {
        let Some(menu) = handle.try_state::<TrayMenu>() else { return };
        let mut v = menu.view();
        let (icon, text) = describe_core(status, &v.texts);
        set_icon_and_text(&handle, &menu, &mut v, icon, &text);
    });
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn parse(v: serde_json::Value) -> TrayUpdate {
        TrayUpdate::from_json(&v).expect("аргументы update_tray")
    }

    fn item(key: &str, title: &str, included: bool) -> RdpItem {
        RdpItem { key: key.into(), title: title.into(), included }
    }

    fn row(key: &str, label: &str, included: bool) -> RdpRow {
        RdpRow { key: key.into(), label: label.into(), included }
    }

    fn ru() -> TrayTexts {
        i18n::RU.tray.texts()
    }

    fn en() -> TrayTexts {
        i18n::EN.tray.texts()
    }

    /// Полный словарь `labels`, как его присылает интерфейс: все ключи + `lang`.
    fn full_labels(lang: &str, texts: &TrayTexts) -> serde_json::Value {
        let mut v = serde_json::to_value(texts).expect("подписи в JSON");
        v["lang"] = json!(lang);
        v
    }

    /// Вызов без новых полей (как у прежнего фронтенда) разбирается и ничего в них не меняет.
    #[test]
    fn update_args_old_format() {
        let u = parse(json!({"state": "idle", "paused": false, "insert_mode": "auto",
            "style": "normal", "styles": [{"id": "normal", "name": "Обычный"}]}));
        assert_eq!(u.state, "idle");
        assert!(!u.paused);
        assert_eq!(u.insert_mode.as_deref(), Some("auto"));
        assert_eq!(u.style.as_deref(), Some("normal"));
        assert_eq!(u.styles, Some(vec![StyleItem { id: "normal".into(), name: "Обычный".into() }]));
        assert_eq!(u.ai_line, Patch::Keep);
        assert_eq!(u.rdp, Patch::Keep);
        assert_eq!(u.labels, Patch::Keep);

        let u = parse(json!({"state": "recording", "paused": true}));
        assert_eq!((u.insert_mode, u.style, u.styles), (None, None, None));
        assert_eq!((u.ai_line, u.rdp, u.labels), (Patch::Keep, Patch::Keep, Patch::Keep));
    }

    /// `ai_line`: строка — показать, `null` — скрыть, нет ключа — не менять.
    #[test]
    fn update_args_ai_line() {
        let u = parse(json!({"state": "idle", "paused": false,
            "ai_line": "ИИ: сегодня $0.03 · баланс $12.34"}));
        assert_eq!(u.ai_line, Patch::Set("ИИ: сегодня $0.03 · баланс $12.34".into()));
        let u = parse(json!({"state": "idle", "paused": false, "ai_line": null}));
        assert_eq!(u.ai_line, Patch::Clear);
        assert!(TrayUpdate::from_json(&json!({"state": "idle", "paused": false, "ai_line": 3}))
            .unwrap_err()
            .starts_with("update_tray: неверные аргументы"));
    }

    /// `rdp`: список `{key, title, included}` (лишние поля — например, `process` — не мешают),
    /// `[]` — сессий нет, `null` — подменю скрыть.
    #[test]
    fn update_args_rdp() {
        let u = parse(json!({"state": "idle", "paused": false, "rdp": [
            {"key": "srv-analytics", "title": "srv-analytics - Подключение к удалённому рабочему столу",
             "included": true, "process": "mstsc.exe"},
            {"key": "dev-box", "title": "dev-box - Remote Desktop Connection", "included": false},
            {"key": "only-key"},
        ]}));
        assert_eq!(
            u.rdp,
            Patch::Set(vec![
                item(
                    "srv-analytics",
                    "srv-analytics - Подключение к удалённому рабочему столу",
                    true
                ),
                item("dev-box", "dev-box - Remote Desktop Connection", false),
                item("only-key", "", false),
            ])
        );
        let u = parse(json!({"state": "idle", "paused": false, "rdp": []}));
        assert_eq!(u.rdp, Patch::Set(vec![]));
        let u = parse(json!({"state": "idle", "paused": false, "rdp": null, "ai_line": null}));
        assert_eq!((u.rdp, u.ai_line), (Patch::Clear, Patch::Clear));
        // без ключа сессии пункт не построить
        assert!(TrayUpdate::from_json(
            &json!({"state": "idle", "paused": false, "rdp": [{"title": "x", "included": true}]})
        )
        .is_err());
    }

    /// `labels`: полный словарь, частичный, `null`, незнакомые ключи и значения.
    #[test]
    fn update_args_labels() {
        let u = parse(json!({"state": "idle", "paused": false,
            "labels": {"lang": "en", "app": "Rupor", "status_idle": "Ready", "quit": "Quit",
                       "status_idle_new": "будущий ключ", "pause": null}}));
        assert_eq!(
            u.labels,
            Patch::Set(TrayLabels {
                lang: Some("en".into()),
                app: Some("Rupor".into()),
                status_idle: Some("Ready".into()),
                quit: Some("Quit".into()),
                ..TrayLabels::default()
            })
        );
        let u = parse(json!({"state": "idle", "paused": false, "labels": {}}));
        assert_eq!(u.labels, Patch::Set(TrayLabels::default()));
        let u = parse(json!({"state": "idle", "paused": false, "labels": null}));
        assert_eq!(u.labels, Patch::Clear);
        // незнакомый язык разбирается (и потом не меняет язык оболочки)
        let u = parse(json!({"state": "idle", "paused": false, "labels": {"lang": "de"}}));
        assert_eq!(
            u.labels,
            Patch::Set(TrayLabels { lang: Some("de".into()), ..TrayLabels::default() })
        );
        // подпись — только строка; словарь — только объект
        for bad in [json!({"quit": 1}), json!({"lang": 1}), json!(["Quit"]), json!("Quit")] {
            let err =
                TrayUpdate::from_json(&json!({"state": "idle", "paused": false, "labels": bad}))
                    .unwrap_err();
            assert!(err.starts_with("update_tray: неверные аргументы"), "{err}");
        }
    }

    /// Ключи `labels` = поля подписей: встроенные подписи сериализуются ровно этими ключами,
    /// а такой JSON целиком разбирается обратно в `labels` (ни один ключ не теряется).
    #[test]
    fn labels_keys_round_trip() {
        for (lang, texts) in [("ru", ru()), ("en", en())] {
            let v = serde_json::to_value(&texts).unwrap();
            let keys: Vec<&str> = v.as_object().unwrap().keys().map(String::as_str).collect();
            let mut expected = TrayTexts::KEYS.to_vec();
            expected.sort_unstable();
            let mut keys_sorted = keys.clone();
            keys_sorted.sort_unstable();
            assert_eq!(keys_sorted, expected);

            let u = parse(json!({"state": "idle", "paused": false,
                "labels": full_labels(lang, &texts)}));
            let Patch::Set(labels) = u.labels else { panic!("labels не разобраны") };
            assert_eq!(labels.lang.as_deref(), Some(lang));
            // поверх подписей на другом языке — ровно присланные
            let other = if lang == "ru" { en() } else { ru() };
            assert_eq!(other.merged(&labels), texts);
        }
        assert_eq!(
            TrayTexts::KEYS,
            [
                "app",
                "status_loading",
                "status_idle",
                "status_recording",
                "status_hands_free",
                "status_processing",
                "status_ai",
                "status_paused",
                "status_error",
                "status_starting",
                "status_restarting",
                "status_failed",
                "status_already_running",
                "status_meeting",
                "pause",
                "insert_mode",
                "mode_auto",
                "mode_type",
                "mode_clipboard",
                "style",
                "rdp",
                "rdp_empty",
                "last_insert",
                "pill_restore",
                "meeting",
                "meeting_running",
                "open",
                "quit",
            ]
        );
    }

    /// Перевод встречи идёт: `meeting_running` в `update_tray` (нет ключа — как было), пункт
    /// «… (идёт)», подсказка без диктовки — «перевод встречи идёт», при диктовке и паузе — их
    /// статус.
    #[test]
    fn meeting_running_label_and_tooltip() {
        let u = parse(json!({"state": "idle", "paused": false, "meeting_running": true}));
        assert_eq!(u.meeting_running, Some(true));
        assert_eq!(parse(json!({"state": "idle", "paused": false})).meeting_running, None);
        for t in [ru(), en()] {
            assert_eq!(meeting_label(true).menu_text(&t), menu_label(&t.meeting_running, 60));
            assert_eq!(meeting_label(false).menu_text(&t), menu_label(&t.meeting, 60));
            assert_eq!(status_with_meeting("idle", false, true, &t).1, t.status_meeting);
            assert_eq!(status_with_meeting("idle", false, false, &t).1, t.status_idle);
            assert_eq!(status_with_meeting("recording", false, true, &t).1, t.status_recording);
            assert_eq!(status_with_meeting("idle", true, true, &t).1, t.status_paused);
        }
        let (_, tip) = status_texts(&ru(), &ru().status_meeting);
        assert_eq!(tip, format!("Рупор {APP_VERSION} — перевод встречи идёт"));
        assert_eq!(ru().meeting_running, "Перевод встречи (идёт)");
    }

    /// Подписи из `labels`: пустые и пробельные — встроенные, управляющие символы убираются.
    #[test]
    fn labels_merge_cleans_text() {
        let labels = TrayLabels {
            pause: Some("  Pause\tnow ".into()),
            quit: Some(" \u{7} ".into()),
            open: Some(String::new()),
            app: Some("R&D\r\nRupor".into()),
            ..TrayLabels::default()
        };
        let t = en().merged(&labels);
        assert_eq!(t.pause, "Pause now");
        assert_eq!(t.quit, "Quit");
        assert_eq!(t.open, "Open Rupor");
        // `&` удваивается только в подписи меню
        assert_eq!(t.app, "R&D Rupor");
        assert_eq!(
            status_texts(&t, "Ready"),
            ("R&&D Rupor: Ready".into(), format!("R&D Rupor {APP_VERSION} — Ready"))
        );
    }

    /// Меню не трогается, если подписи те же; меняются только изменившиеся пункты;
    /// недостающие ключи — встроенные на языке `lang`.
    #[test]
    fn labels_plan() {
        let cur = ru();
        assert_eq!(plan_labels(Lang::Ru, &cur, Patch::Keep), None);
        assert_eq!(plan_labels(Lang::Ru, &cur, Patch::Clear), None);
        assert_eq!(plan_labels(Lang::Ru, &cur, Patch::Set(TrayLabels::default())), None);
        let same = TrayLabels {
            lang: Some("ru".into()),
            pause: Some("Пауза".into()),
            ..TrayLabels::default()
        };
        assert_eq!(plan_labels(Lang::Ru, &cur, Patch::Set(same)), None);

        // одна новая подпись — один пункт
        let one = TrayLabels {
            pause: Some("Приостановить".into()), ..TrayLabels::default()
        };
        let plan = plan_labels(Lang::Ru, &cur, Patch::Set(one)).expect("есть изменения");
        assert_eq!((plan.lang, plan.changed.as_slice()), (Lang::Ru, [Label::Pause].as_slice()));
        assert_eq!(plan.texts.pause, "Приостановить");

        // статус и название — не пункты меню (строку статуса собирает set_icon_and_text)
        let st = TrayLabels {
            app: Some("Рупор 2".into()),
            status_idle: Some("ок".into()),
            ..TrayLabels::default()
        };
        let plan = plan_labels(Lang::Ru, &cur, Patch::Set(st)).expect("есть изменения");
        assert!(plan.changed.is_empty());
        assert_eq!((plan.texts.app.as_str(), plan.texts.status_idle.as_str()), ("Рупор 2", "ок"));

        // смена языка: недостающее — встроенное английское, меняются все пункты
        let to_en = TrayLabels {
            lang: Some("en".into()),
            quit: Some("Exit".into()),
            ..TrayLabels::default()
        };
        let plan = plan_labels(Lang::Ru, &cur, Patch::Set(to_en)).expect("есть изменения");
        assert_eq!(plan.lang, Lang::En);
        assert_eq!(plan.texts, TrayTexts { quit: "Exit".into(), ..en() });
        assert_eq!(plan.changed, Label::ALL.to_vec());

        // язык сменился, а подписи прислали те же — план есть (язык оболочки и стили)
        let same_ru: TrayLabels = serde_json::from_value(full_labels("en", &cur)).unwrap();
        let plan = plan_labels(Lang::Ru, &cur, Patch::Set(same_ru)).expect("язык сменился");
        assert_eq!((plan.lang, plan.changed.len()), (Lang::En, 0));

        // незнакомый язык — прежний; `null` — встроенные на текущем языке
        let de = TrayLabels { lang: Some("de".into()), ..TrayLabels::default() };
        assert_eq!(plan_labels(Lang::En, &en(), Patch::Set(de)), None);
        let plan = plan_labels(Lang::En, &TrayTexts { pause: "Hold".into(), ..en() }, Patch::Clear)
            .expect("вернуть встроенные");
        assert_eq!((plan.texts, plan.changed), (en(), vec![Label::Pause]));
    }

    #[test]
    fn builtin_styles_by_lang() {
        assert_eq!(builtin_style_name(Lang::Ru, "normal"), Some("Обычный"));
        assert_eq!(builtin_style_name(Lang::En, "normal"), Some("Standard"));
        assert_eq!(builtin_style_name(Lang::En, "code"), Some("Code / prompt"));
        assert_eq!(builtin_style_name(Lang::En, "my_style"), None);
    }

    /// Событие клика по сессии: `{id: "rdp:<key>"}`, ключ — как пришёл (регистр не меняется).
    #[test]
    fn rdp_tray_action_id() {
        let id = format!("{RDP_PREFIX}{}", "SRV-Analytics");
        let ev = serde_json::to_value(TrayAction { id }).unwrap();
        assert_eq!(ev, json!({"id": "rdp:SRV-Analytics"}));
    }

    #[test]
    fn labels_are_safe() {
        assert_eq!(menu_label("  srv\tone\r\n two  ", 60), "srv one two");
        assert_eq!(menu_label("R&D сервер", 60), "R&&D сервер");
        assert_eq!(menu_label("абвгдеёжзи", 5), "абвг…");
        // обрезка не оставляет пробел перед многоточием и не рвёт «&&»
        assert_eq!(menu_label("abc def", 5), "abc…");
        assert_eq!(menu_label("a&b&c&d", 4), "a&&b…");
        assert_eq!(menu_label(" \u{7} ", 10), "");
    }

    /// Подсказка значка не длиннее 127 знаков UTF-16 (эмодзи — два знака), `&` как есть.
    #[test]
    fn tooltip_is_short() {
        let t = TrayTexts { app: "Rupor".into(), ..en() };
        let (_, tip) = status_texts(&t, &"x".repeat(300));
        assert_eq!(tip.encode_utf16().count(), TOOLTIP_MAX);
        assert!(tip.starts_with(&format!("Rupor {APP_VERSION} — xxx")) && tip.ends_with('…'));
        let tip = tooltip_text(&"😀".repeat(100));
        assert!(tip.encode_utf16().count() <= TOOLTIP_MAX);
        assert_eq!(tip.chars().filter(|c| *c == '😀').count(), 63);
        assert_eq!(tooltip_text("R&D\n"), "R&D");
    }

    #[test]
    fn status_line_by_lang() {
        let (icon, s) = describe("idle", false, &ru());
        assert_eq!(icon, IconKind::Idle);
        assert_eq!(
            status_texts(&ru(), &s),
            ("Рупор: готов".into(), format!("Рупор {APP_VERSION} — готов"))
        );
        let (_, s) = describe("idle", false, &en());
        assert_eq!(
            status_texts(&en(), &s),
            ("Rupor: Ready".into(), format!("Rupor {APP_VERSION} — Ready"))
        );
        let (icon, s) = describe_core(CoreStatus::AlreadyRunning, &en());
        assert_eq!((icon, s.as_str()), (IconKind::Error, "Another Rupor is already running"));
        let (icon, s) = describe_core(CoreStatus::Starting, &ru());
        assert_eq!((icon, s.as_str()), (IconKind::Loading, "запуск ядра…"));
    }

    #[test]
    fn changed_labels_only_differences() {
        assert!(changed_labels(&ru(), &ru()).is_empty());
        assert_eq!(changed_labels(&ru(), &en()), Label::ALL.to_vec());
        let t = TrayTexts { rdp_empty: "—".into(), mode_type: "Keys".into(), ..en() };
        assert_eq!(changed_labels(&en(), &t), vec![Label::ModeType, Label::RdpEmpty]);
        // «Перевод встречи» — постоянный пункт меню со своей подписью
        assert_eq!(Label::Meeting.text(&ru()), "Перевод встречи");
        assert_eq!(Label::Meeting.menu_text(&en()), "Meeting translation");
        let t = TrayTexts { meeting: "Live translation".into(), ..en() };
        assert_eq!(changed_labels(&en(), &t), vec![Label::Meeting]);
        // каждому режиму вставки — своя подпись
        let e = en();
        let modes: Vec<(&str, &str)> = MODES.iter().map(|(id, l)| (*id, l.text(&e))).collect();
        assert_eq!(
            modes,
            [
                ("auto", "Auto"),
                ("type", "Type as keystrokes"),
                ("clipboard", "Paste via clipboard")
            ]
        );
    }

    #[test]
    fn rdp_rows_dedup_and_labels() {
        let rows = rdp_rows(vec![
            item("srv-A", "srv-A - Подключение к удалённому рабочему столу", true),
            item("SRV-a", "SRV-a - Remote Desktop Connection", false),
            item("  ", "пустой ключ", true),
            item("Tom & Jerry - mRemoteNG", "Tom & Jerry - mRemoteNG", false),
            item("Сервер-Б", "", true),
            item("сервер-б", "", false),
        ]);
        assert_eq!(
            rows,
            vec![
                row("srv-A", "srv-A", true),
                row("Tom & Jerry - mRemoteNG", "Tom && Jerry - mRemoteNG", false),
                row("Сервер-Б", "Сервер-Б", true),
            ]
        );
        let long = "x".repeat(200);
        assert_eq!(rdp_rows(vec![item(&long, "", true)])[0].label.chars().count(), RDP_LABEL_MAX);
        assert_eq!(rdp_rows(vec![item(&long, "", true)])[0].key, long);
    }

    #[test]
    fn ai_line_plan() {
        let set = |s: &str| Patch::Set(s.to_string());
        assert_eq!(plan_ai(None, Patch::Keep), AiPlan::Nothing);
        assert_eq!(plan_ai(Some("ИИ: $1"), Patch::Keep), AiPlan::Nothing);
        assert_eq!(plan_ai(None, Patch::Clear), AiPlan::Nothing);
        assert_eq!(plan_ai(Some("ИИ: $1"), Patch::Clear), AiPlan::Hide);
        assert_eq!(plan_ai(Some("ИИ: $1"), set("  ")), AiPlan::Hide);
        assert_eq!(plan_ai(None, set("")), AiPlan::Nothing);
        assert_eq!(plan_ai(None, set("ИИ: $1")), AiPlan::Show("ИИ: $1".into()));
        assert_eq!(plan_ai(Some("ИИ: $1"), set("ИИ: $1")), AiPlan::Nothing);
        assert_eq!(plan_ai(Some("ИИ: $1"), set("ИИ: $2")), AiPlan::Text("ИИ: $2".into()));
        // сравнивается уже готовая подпись
        assert_eq!(plan_ai(Some("R&&D"), set("R&D")), AiPlan::Nothing);
    }

    /// Подменю перестраивается только при смене набора сессий; флажки — без перестройки.
    #[test]
    fn rdp_plan() {
        let cur_rows = vec![row("srv-a", "srv-a", true), row("dev", "dev", false)];
        let cur = cur_rows.as_slice();
        let empty: &[RdpRow] = &[];
        let same = || vec![item("srv-a", "t", true), item("dev", "t", false)];
        assert_eq!(plan_rdp(Some(cur), Patch::Keep), RdpPlan::Nothing);
        assert_eq!(plan_rdp(None, Patch::Keep), RdpPlan::Nothing);
        assert_eq!(plan_rdp(Some(cur), Patch::Set(same())), RdpPlan::Nothing);
        assert_eq!(
            plan_rdp(Some(cur), Patch::Set(vec![item("srv-a", "", false), item("dev", "", true)])),
            RdpPlan::Checks(vec![row("srv-a", "srv-a", false), row("dev", "dev", true)])
        );
        assert_eq!(
            plan_rdp(Some(cur), Patch::Set(vec![item("dev", "", false), item("srv-a", "", true)])),
            RdpPlan::Rebuild(vec![row("dev", "dev", false), row("srv-a", "srv-a", true)])
        );
        assert_eq!(
            plan_rdp(Some(cur), Patch::Set(vec![item("srv-a", "", true)])),
            RdpPlan::Rebuild(vec![row("srv-a", "srv-a", true)])
        );
        assert_eq!(plan_rdp(Some(cur), Patch::Set(vec![])), RdpPlan::Rebuild(vec![]));
        assert_eq!(plan_rdp(Some(empty), Patch::Set(vec![])), RdpPlan::Nothing);
        assert_eq!(plan_rdp(None, Patch::Set(vec![])), RdpPlan::Show(vec![]));
        assert_eq!(plan_rdp(None, Patch::Set(same())), RdpPlan::Show(cur_rows.clone()));
        assert_eq!(plan_rdp(Some(cur), Patch::Clear), RdpPlan::Hide);
        assert_eq!(plan_rdp(Some(empty), Patch::Clear), RdpPlan::Hide);
        assert_eq!(plan_rdp(None, Patch::Clear), RdpPlan::Nothing);
    }

    #[test]
    fn icons_by_state() {
        let t = ru();
        assert_eq!(describe("idle", false, &t).0, IconKind::Idle);
        assert_eq!(describe("recording", false, &t).0, IconKind::Recording);
        assert_eq!(describe("hands_free", false, &t).0, IconKind::Recording);
        assert_eq!(describe("processing", false, &t).0, IconKind::Processing);
        assert_eq!(describe("ai", false, &t).0, IconKind::Processing);
        assert_eq!(describe("loading", false, &t).0, IconKind::Loading);
        assert_eq!(describe("paused", false, &t).0, IconKind::Paused);
        assert_eq!(describe("error", false, &t).0, IconKind::Error);
        // флаг паузы важнее состояния
        assert_eq!(describe("idle", true, &t), (IconKind::Paused, "пауза".to_string()));
        assert_eq!(describe("idle", true, &en()), (IconKind::Paused, "Paused".to_string()));
        assert_eq!(
            describe("что-то новое", false, &t),
            (IconKind::Idle, "что-то новое".to_string())
        );
    }

    #[test]
    fn icons_are_valid_png() {
        for k in [
            IconKind::Idle,
            IconKind::Recording,
            IconKind::Processing,
            IconKind::Paused,
            IconKind::Loading,
            IconKind::Error,
        ] {
            let img = Image::from_bytes(k.bytes()).expect("png");
            assert_eq!((img.width(), img.height()), (32, 32));
        }
    }
}
