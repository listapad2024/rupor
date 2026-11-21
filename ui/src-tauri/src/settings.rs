//! Чтение нужных оболочке ключей из `%APPDATA%\Rupor\settings.json`.
//!
//! Файлом владеет ядро (пишет атомарно через .tmp → replace); оболочка его только читает
//! при старте: показать ли главное окно (мастер первого запуска), где поставить пилюлю и на
//! каком языке подписать трей и окна, пока интерфейс не подключился, — и при открытии окна
//! перевода встречи (где его поставить, прятать ли от демонстрации экрана).

use serde_json::Value;

#[derive(Debug, Clone, PartialEq)]
pub struct UiSettings {
    /// `first_run_done` — мастер первого запуска пройден. Нет файла/ключа → `false`.
    pub first_run_done: bool,
    /// `pill_enabled` — показывать пилюлю. По умолчанию `true`.
    pub pill_enabled: bool,
    /// `pill_position` — `[x, y]` в физических пикселях или `null` (снизу по центру).
    pub pill_position: Option<(i32, i32)>,
    /// `autostart` — запускать с Windows. По умолчанию `true`.
    pub autostart: bool,
    /// `language` — язык интерфейса `system` | `ru` | `en`, как записан (разбирает
    /// `i18n::resolve`). Нет ключа — `None`, как `system`.
    pub language: Option<String>,
    /// `meeting_rect` — окно перевода встречи `[x, y, ширина, высота]` в физических пикселях
    /// или `null` (у правого края рабочей области, на всю высоту).
    pub meeting_rect: Option<MeetingRect>,
    /// `meeting_protect` — прятать окно перевода от захвата экрана. По умолчанию `true`.
    pub meeting_protect: bool,
    /// `meeting_mini_pos` — компактная полоска окна перевода: левый верхний угол её видимой
    /// части `[x, y]` в физических пикселях или `null` (справа внизу рабочей области).
    pub meeting_mini_pos: Option<(i32, i32)>,
}

/// Геометрия окна перевода встречи в физических пикселях.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct MeetingRect {
    pub x: i32,
    pub y: i32,
    pub w: i32,
    pub h: i32,
}

/// Меньше этого (физ. px) сохранённое окно перевода не бывает — как валидатор ядра.
pub const MEETING_RECT_MIN: i32 = 200;

impl Default for UiSettings {
    fn default() -> Self {
        Self {
            first_run_done: false,
            pill_enabled: true,
            pill_position: None,
            autostart: true,
            language: None,
            meeting_rect: None,
            meeting_protect: true,
            meeting_mini_pos: None,
        }
    }
}

pub fn load() -> UiSettings {
    let Some(path) = crate::logger::app_dir().map(|d| d.join("settings.json")) else {
        return UiSettings::default();
    };
    match std::fs::read_to_string(&path) {
        Ok(text) => parse(&text),
        Err(_) => UiSettings::default(), // файла нет — первый запуск
    }
}

pub fn parse(text: &str) -> UiSettings {
    let Ok(v) = serde_json::from_str::<Value>(text.trim_start_matches('\u{feff}')) else {
        crate::log_warn!("settings.json не читается как JSON — беру значения по умолчанию");
        return UiSettings::default();
    };
    let d = UiSettings::default();
    UiSettings {
        first_run_done: v
            .get("first_run_done")
            .and_then(Value::as_bool)
            .unwrap_or(d.first_run_done),
        pill_enabled: v.get("pill_enabled").and_then(Value::as_bool).unwrap_or(d.pill_enabled),
        pill_position: v.get("pill_position").and_then(parse_position),
        autostart: v.get("autostart").and_then(Value::as_bool).unwrap_or(d.autostart),
        language: v.get("language").and_then(Value::as_str).map(str::to_string),
        meeting_rect: v.get("meeting_rect").and_then(parse_rect),
        meeting_protect: v
            .get("meeting_protect")
            .and_then(Value::as_bool)
            .unwrap_or(d.meeting_protect),
        meeting_mini_pos: v.get("meeting_mini_pos").and_then(parse_position),
    }
}

/// Координата в физических пикселях: конечное число, |v| < 1e6, округлённое.
fn coord(x: &Value) -> Option<i32> {
    let f = x.as_f64()?;
    (f.is_finite() && f.abs() < 1_000_000.0).then(|| f.round() as i32)
}

fn parse_position(v: &Value) -> Option<(i32, i32)> {
    let arr = v.as_array()?;
    if arr.len() != 2 {
        return None;
    }
    Some((coord(&arr[0])?, coord(&arr[1])?))
}

/// `[x, y, w, h]`; ширина и высота — не меньше [`MEETING_RECT_MIN`].
fn parse_rect(v: &Value) -> Option<MeetingRect> {
    let arr = v.as_array()?;
    if arr.len() != 4 {
        return None;
    }
    let (x, y, w, h) = (coord(&arr[0])?, coord(&arr[1])?, coord(&arr[2])?, coord(&arr[3])?);
    (w >= MEETING_RECT_MIN && h >= MEETING_RECT_MIN).then_some(MeetingRect { x, y, w, h })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn defaults_when_keys_missing() {
        let s = parse(r#"{"ptt_key": "rctrl"}"#);
        assert_eq!(s, UiSettings::default());
        assert!(!s.first_run_done);
        assert!(s.pill_enabled);
    }

    #[test]
    fn reads_keys() {
        let s = parse(
            r#"{"first_run_done": true, "pill_enabled": false, "pill_position": [100, -20.6]}"#,
        );
        assert!(s.first_run_done);
        assert!(!s.pill_enabled);
        assert_eq!(s.pill_position, Some((100, -21)));
    }

    #[test]
    fn bad_position_is_ignored() {
        assert_eq!(parse(r#"{"pill_position": null}"#).pill_position, None);
        assert_eq!(parse(r#"{"pill_position": [1]}"#).pill_position, None);
        assert_eq!(parse(r#"{"pill_position": ["a", 2]}"#).pill_position, None);
        assert_eq!(parse(r#"{"pill_position": {"x": 1}}"#).pill_position, None);
    }

    #[test]
    fn language_key() {
        assert_eq!(parse(r#"{"language": "en"}"#).language.as_deref(), Some("en"));
        assert_eq!(parse(r#"{"language": "system"}"#).language.as_deref(), Some("system"));
        assert_eq!(parse(r#"{"language": 1}"#).language, None);
        assert_eq!(parse("{}").language, None);
    }

    #[test]
    fn meeting_keys() {
        let d = parse("{}");
        assert_eq!((d.meeting_rect, d.meeting_protect), (None, true));
        let s = parse(r#"{"meeting_rect": [1500, 0, 420.4, 1040], "meeting_protect": false}"#);
        assert_eq!(s.meeting_rect, Some(MeetingRect { x: 1500, y: 0, w: 420, h: 1040 }));
        assert!(!s.meeting_protect);
        // отрицательные координаты (монитор слева) — можно
        assert_eq!(
            parse(r#"{"meeting_rect": [-1920, -8, 600, 900]}"#).meeting_rect,
            Some(MeetingRect { x: -1920, y: -8, w: 600, h: 900 })
        );
        for bad in [
            "null",
            "[1, 2, 3]",
            "[1, 2, 199, 500]",
            "[1, 2, 500, 150]",
            "[1, 2, \"500\", 500]",
            "[1e7, 0, 500, 500]",
            "{\"x\": 1}",
        ] {
            let text = format!("{{\"meeting_rect\": {bad}}}");
            assert_eq!(parse(&text).meeting_rect, None, "{bad}");
        }
        assert!(parse(r#"{"meeting_protect": "нет"}"#).meeting_protect);
    }

    #[test]
    fn meeting_mini_pos_key() {
        assert_eq!(parse("{}").meeting_mini_pos, None);
        assert_eq!(
            parse(r#"{"meeting_mini_pos": [1560.6, -8]}"#).meeting_mini_pos,
            Some((1561, -8))
        );
        for bad in ["null", "[1]", "[1, 2, 3]", "[\"a\", 2]", "[1e7, 0]"] {
            let text = format!("{{\"meeting_mini_pos\": {bad}}}");
            assert_eq!(parse(&text).meeting_mini_pos, None, "{bad}");
        }
    }

    #[test]
    fn broken_json_and_bom() {
        assert_eq!(parse("{не json"), UiSettings::default());
        assert!(parse("\u{feff}{\"first_run_done\": true}").first_run_done);
    }
}
