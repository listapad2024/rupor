//! Команды для `invoke` из окон. Имена и аргументы — по docs/ARCHITECTURE.md («Tauri (Rust)»).
//! Аргументы принимаются в snake_case, как в контракте: `invoke("update_tray", {insert_mode})`.
//! Ошибки — строки на языке оболочки (`i18n`: ru/en; в JS промис отклоняется с этой строкой),
//! кроме ошибок разбора аргументов — они для разработчика.

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use tauri::ipc::{InvokeBody, Request};
use tauri::{AppHandle, Manager, State};

use crate::i18n;
use crate::sidecar::{BackendInfo, Supervisor, READY_TIMEOUT};
use crate::tray::TrayUpdate;

/// Перезапуск от администратора уже запрошен — ждём ответа в окне UAC.
static RELAUNCHING: AtomicBool = AtomicBool::new(false);
/// Сколько ждать, пока главное окно станет активным перед запросом UAC.
const FOREGROUND_WAIT: Duration = Duration::from_millis(1000);

/// `backend_info` → `{port, token}`. Ждёт готовности ядра (TCP-подключение) до 30 с.
#[tauri::command]
pub async fn backend_info(sup: State<'_, Arc<Supervisor>>) -> Result<BackendInfo, String> {
    let sup = Arc::clone(sup.inner());
    tauri::async_runtime::spawn_blocking(move || sup.wait_ready(READY_TIMEOUT))
        .await
        .map_err(|e| internal_error(&e))?
}

fn internal_error(e: &dyn std::fmt::Display) -> String {
    i18n::fill(i18n::t().internal_error, &[("error", e)])
}

/// `update_tray {state, paused, insert_mode?, style?, styles?, ai_line?, rdp?, labels?}` —
/// обновить значок, подсказку и меню (поля — [`TrayUpdate`]).
/// `styles` — `[{id, name}]` для подменю «Стиль»; без него — 5 встроенных.
/// `ai_line` — неактивная строка расхода ИИ; `rdp` — `[{key, title, included}]` для подменю
/// «Рассылка в RDP»; `labels` — подписи меню и подсказки на языке интерфейса
/// ([`crate::tray::TrayLabels`]). Для них важно отличать «ключа нет» (не менять) от `null`
/// (скрыть / встроенные подписи), а аргументы-`Option` этого не различают, поэтому тело
/// вызова разбирается целиком.
#[tauri::command]
pub fn update_tray(app: AppHandle, request: Request<'_>) -> Result<(), String> {
    crate::tray::update(&app, parse_tray_update(request.body())?)
}

fn parse_tray_update(body: &InvokeBody) -> Result<TrayUpdate, String> {
    match body {
        InvokeBody::Json(v) => TrayUpdate::from_json(v),
        InvokeBody::Raw(_) => Err("update_tray: аргументы должны быть объектом JSON".into()),
    }
}

/// `relaunch_admin` — перезапуск от имени администратора (UAC), затем выход.
/// Если пользователь отказался в окне UAC — ошибка, приложение продолжает работать.
///
/// Windows показывает запрос UAC сразу, только если окно-владелец на переднем плане; иначе
/// лишь мигает кнопка на панели задач. Пилюля никогда не активируется, поэтому запрос
/// привязан к главному окну: оно показывается и активируется. Повторный вызов, пока запрос
/// открыт, — ошибка (иначе каждый клик — новый запрос UAC). Было открыто главное окно —
/// новый экземпляр откроет его тоже (`--show-main`).
#[tauri::command]
pub async fn relaunch_admin(app: AppHandle) -> Result<(), String> {
    if RELAUNCHING.swap(true, Ordering::SeqCst) {
        return Err(i18n::t().relaunch_pending.into());
    }
    let result = relaunch_admin_now(&app).await;
    if result.is_err() {
        RELAUNCHING.store(false, Ordering::SeqCst);
    }
    result
}

async fn relaunch_admin_now(app: &AppHandle) -> Result<(), String> {
    let exe = std::env::current_exe()
        .map_err(|e| i18n::fill(i18n::t().exe_path_failed, &[("error", &e)]))?;
    let main = app.get_webview_window(crate::MAIN_LABEL);
    let main_was_open = main
        .as_ref()
        .is_some_and(|w| w.is_visible().unwrap_or(false) && !w.is_minimized().unwrap_or(false));
    crate::show_main(app, None);
    let owner = main.as_ref().and_then(|w| w.hwnd().ok()).map_or(0, |h| h.0 as isize);
    let params = crate::relaunch_params(
        std::process::id(),
        main_was_open,
        std::env::var("RUPOR_CORE").ok().as_deref(),
        std::env::var("RUPOR_CORE_ARGS").ok().as_deref(),
    );
    crate::log_info!("Перезапуск от имени администратора");
    let result = tauri::async_runtime::spawn_blocking(move || {
        if owner != 0 && !crate::winapi::wait_foreground(owner, FOREGROUND_WAIT) {
            crate::log_warn!(
                "Главное окно не стало активным — запрос UAC может появиться свёрнутым"
            );
        }
        crate::winapi::shell_execute_runas(owner, &exe, &params)
    })
    .await
    .map_err(|e| internal_error(&e))?;
    if let Err(e) = result {
        crate::log_info!("Перезапуск от имени администратора не состоялся: {e}");
        // главное окно показывали только ради запроса UAC — вернуть как было
        if let (false, Some(w)) = (main_was_open, &main) {
            let _ = w.hide();
        }
        return Err(e);
    }
    crate::quit(app);
    Ok(())
}

/// `show_main {route?}` — показать, развернуть и активировать главное окно;
/// с `route` — ещё и событие `navigate {route}`.
#[tauri::command(rename_all = "snake_case")]
pub fn show_main(app: AppHandle, route: Option<String>) {
    crate::show_main(&app, route);
}

/// `quit_app` — выход: остановить ядро и закрыть приложение.
#[tauri::command]
pub fn quit_app(app: AppHandle) {
    crate::quit(&app);
}

/// `set_pill_passthrough {on}` — клики мыши проходят сквозь пилюлю.
#[tauri::command(rename_all = "snake_case")]
pub fn set_pill_passthrough(app: AppHandle, on: bool) -> Result<(), String> {
    crate::pill::set_passthrough(&app, on)
}

/// `reset_pill_position` — вернуть пилюлю вниз по центру основного монитора.
/// (Сохранить `pill_position: null` в настройках ядра фронтенд должен сам.)
#[tauri::command]
pub fn reset_pill_position(app: AppHandle) -> Result<(), String> {
    crate::pill::reset_position(&app)
}

/// `pill_raise` — пилюля наверх слоя «поверх всех» (начало записи: её могло закрыть окно,
/// которое тоже «поверх всех», например полноэкранный RDP).
#[tauri::command]
pub fn pill_raise() {
    crate::pill::raise();
}

/// `pill_log {level, message}` — ошибка страницы пилюли в журнал оболочки (ui.log): иначе
/// упавшая пилюля пропадает молча. Одна строка, не длиннее LOG_MESSAGE_MAX символов.
#[tauri::command(rename_all = "snake_case")]
pub fn pill_log(level: String, message: String) {
    page_log("Пилюля", &level, &message);
}

/// `meeting_window_show` — показать окно перевода встречи (при первом открытии — создать):
/// без активации, поверх всех, под пилюлей. Async: окно строится не в главном потоке.
#[tauri::command]
pub async fn meeting_window_show(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || crate::meeting::show_blocking(&app))
        .await
        .map_err(|e| internal_error(&e))?
}

/// `meeting_window_minimize` — окно перевода свернуть в полоску (справа внизу или где её
/// оставили), поверх всех, без фокуса; окна нет — ничего.
#[tauri::command]
pub async fn meeting_window_minimize(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || crate::meeting::minimize_blocking(&app))
        .await
        .map_err(|e| internal_error(&e))?
}

/// `meeting_window_restore` — полоску (или скрытое окно) снова панелью на её месте.
#[tauri::command]
pub async fn meeting_window_restore(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || crate::meeting::show_blocking(&app))
        .await
        .map_err(|e| internal_error(&e))?
}

/// `meeting_window_hide` — скрыть окно перевода (сеанс перевода идёт дальше).
#[tauri::command]
pub fn meeting_window_hide(app: AppHandle) -> Result<(), String> {
    crate::meeting::hide(&app)
}

/// `meeting_window_toggle` → видно ли окно теперь: видно — скрыть, нет — показать.
#[tauri::command]
pub async fn meeting_window_toggle(app: AppHandle) -> Result<bool, String> {
    tauri::async_runtime::spawn_blocking(move || crate::meeting::toggle_blocking(&app))
        .await
        .map_err(|e| internal_error(&e))?
}

/// `meeting_window_reset` — окно перевода снова к правому краю рабочей области, на всю высоту.
/// (Сохранить `meeting_rect: null` в настройках ядра фронтенд должен сам.)
#[tauri::command]
pub async fn meeting_window_reset(app: AppHandle) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || crate::meeting::reset(&app))
        .await
        .map_err(|e| internal_error(&e))?
}

/// `meeting_suggest_show {suggestion}` — ядро заметило встречу (`suggestion` — событие
/// `meeting_suggest` как есть; приносит главное окно): скрытое окно перевода становится
/// карточкой «Обнаружена новая встреча» в правом верхнем углу (без фокуса), видимая панель
/// показывает предложение сама. Через 45 с без ответа — пропадает. Async: может создать окно.
#[tauri::command(rename_all = "snake_case")]
pub async fn meeting_suggest_show(
    app: AppHandle,
    suggestion: serde_json::Value,
) -> Result<(), String> {
    tauri::async_runtime::spawn_blocking(move || crate::meeting::suggest_blocking(&app, suggestion))
        .await
        .map_err(|e| internal_error(&e))?
}

/// `meeting_suggest_done {id}` — на предложение ответили («Не сейчас», «Не предлагать») или
/// закрыли его: карточка скрывается, полоска в панели пропадает.
#[tauri::command(rename_all = "snake_case")]
pub fn meeting_suggest_done(app: AppHandle, id: String) -> Result<(), String> {
    crate::meeting::suggest_done(&app, &id)
}

/// `meeting_window_mode` → `{mode: "full" | "suggest", suggestion, hide_in_ms}` — что сейчас
/// показывает окно перевода (страница спрашивает при загрузке, дальше — событие
/// `meeting-window-mode`).
#[tauri::command]
pub fn meeting_window_mode() -> crate::meeting::ModeInfo {
    crate::meeting::mode_info()
}

/// `meeting_window_log {level, message}` — ошибка страницы окна перевода в ui.log
/// («Окно перевода: …»), как `pill_log`.
#[tauri::command(rename_all = "snake_case")]
pub fn meeting_window_log(level: String, message: String) {
    page_log("Окно перевода", &level, &message);
}

/// Сообщение страницы окна в журнал оболочки с префиксом окна.
fn page_log(prefix: &str, level: &str, message: &str) {
    let msg = log_line(message);
    match level {
        "error" => crate::log_error!("{prefix}: {msg}"),
        "warn" => crate::log_warn!("{prefix}: {msg}"),
        _ => crate::log_info!("{prefix}: {msg}"),
    }
}

const LOG_MESSAGE_MAX: usize = 500;

/// Сообщение страницы → одна строка без управляющих символов, не длиннее LOG_MESSAGE_MAX.
fn log_line(message: &str) -> String {
    let one: String = message
        .chars()
        .map(|c| if c.is_control() { ' ' } else { c })
        .collect::<String>()
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ");
    one.chars().take(LOG_MESSAGE_MAX).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tray::{Patch, RdpItem, TrayLabels};
    use serde_json::json;

    #[test]
    fn pill_log_line_is_one_short_line() {
        assert_eq!(
            log_line("TypeError: x\n    at Pill (pill.js:1)\r\n"),
            "TypeError: x at Pill (pill.js:1)"
        );
        assert_eq!(log_line(&"я".repeat(900)).chars().count(), LOG_MESSAGE_MAX);
    }

    /// Тело `invoke("update_tray", {...})` — как его отправляет фронтенд.
    #[test]
    fn update_tray_body() {
        let body = InvokeBody::Json(json!({
            "state": "idle", "paused": false, "insert_mode": "auto", "style": "normal",
            "ai_line": "ИИ: сегодня $0.03 · баланс $12.34",
            "rdp": [{"key": "srv-analytics", "title": "srv-analytics - Remote Desktop Connection",
                     "included": true}],
        }));
        let u = parse_tray_update(&body).expect("разбор");
        assert_eq!(u.ai_line, Patch::Set("ИИ: сегодня $0.03 · баланс $12.34".into()));
        assert_eq!(
            u.rdp,
            Patch::Set(vec![RdpItem {
                key: "srv-analytics".into(),
                title: "srv-analytics - Remote Desktop Connection".into(),
                included: true,
            }])
        );

        let old = InvokeBody::Json(json!({"state": "ai", "paused": false}));
        let u = parse_tray_update(&old).expect("разбор прежнего формата");
        assert_eq!((u.ai_line, u.rdp, u.labels), (Patch::Keep, Patch::Keep, Patch::Keep));

        // подписи трея на языке интерфейса
        let en = InvokeBody::Json(json!({"state": "idle", "paused": false,
            "labels": {"lang": "en", "app": "Rupor", "status_idle": "Ready", "quit": "Quit"}}));
        let u = parse_tray_update(&en).expect("разбор labels");
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
        let bad = InvokeBody::Json(json!({"state": "idle", "paused": false, "labels": ["Quit"]}));
        assert!(parse_tray_update(&bad).is_err());

        assert!(parse_tray_update(&InvokeBody::Raw(b"{}".to_vec())).is_err());
        assert!(parse_tray_update(&InvokeBody::Json(json!(null))).is_err());
    }
}
