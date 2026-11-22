//! Рупор — оболочка Tauri 2: окна `main`, `pill` и `meeting` (перевод встречи), трей,
//! супервизор Python-ядра.
//!
//! Контракты — docs/ARCHITECTURE.md, разделы «Процессы» и «Tauri (Rust)».

macro_rules! log_info {
    ($($t:tt)*) => { $crate::logger::write("INFO", &format!($($t)*)) };
}
macro_rules! log_warn {
    ($($t:tt)*) => { $crate::logger::write("WARNING", &format!($($t)*)) };
}
macro_rules! log_error {
    ($($t:tt)*) => { $crate::logger::write("ERROR", &format!($($t)*)) };
}
pub(crate) use {log_error, log_info, log_warn};

mod commands;
mod i18n;
mod logger;
mod meeting;
mod overlay;
mod pill;
mod settings;
mod sidecar;
mod tray;
mod winapi;

use std::sync::Arc;
use std::time::Duration;

use serde::Serialize;
use tauri::{AppHandle, Emitter, Manager, RunEvent, WindowEvent};

use crate::sidecar::Supervisor;

pub const MAIN_LABEL: &str = "main";

/// Событие `navigate`.
#[derive(Debug, Clone, Serialize)]
struct Navigate {
    route: String,
}

/// Показать главное окно (развернуть, активировать); с `route` — событие `navigate {route}`.
pub fn show_main(app: &AppHandle, route: Option<String>) {
    match app.get_webview_window(MAIN_LABEL) {
        Some(w) => {
            let _ = w.show();
            if w.is_minimized().unwrap_or(false) {
                let _ = w.unminimize();
            }
            let _ = w.set_focus();
        }
        None => log_warn!("Главное окно не найдено"),
    }
    if let Some(route) = route.filter(|r| !r.is_empty()) {
        if let Err(e) = app.emit("navigate", Navigate { route }) {
            log_warn!("Не удалось отправить navigate: {e}");
        }
    }
}

/// Заголовки окон по умолчанию на языке оболочки: «Рупор» / «Rupor». Дальше заголовки
/// ставит интерфейс (`setTitle`) на языке из настроек. Окно перевода встречи создаётся
/// позже — заголовок ему ставит `meeting::build`.
fn set_default_titles(app: &AppHandle) {
    let t = i18n::t();
    for (label, title) in [(MAIN_LABEL, t.tray.app), (pill::LABEL, t.pill_title)] {
        if let Some(w) = app.get_webview_window(label) {
            if let Err(e) = w.set_title(title) {
                log_warn!("Не удалось задать заголовок окна {label}: {e}");
            }
        }
    }
}

/// Выход по команде (трей «Выход», `quit_app`, перезапуск от администратора):
/// остановить ядро (без перезапуска) и завершить приложение.
pub fn quit(app: &AppHandle) {
    log_info!("Выход");
    if let Some(sup) = app.try_state::<Arc<Supervisor>>() {
        sup.shutdown();
    }
    app.exit(0);
}

fn arg_value<'a>(args: &'a [String], name: &str) -> Option<&'a str> {
    args.iter().position(|a| a == name).and_then(|i| args.get(i + 1)).map(String::as_str)
}

fn has_flag(args: &[String], name: &str) -> bool {
    args.iter().any(|a| a == name)
}

/// Аргументы нового экземпляра при перезапуске от администратора (`relaunch_admin`).
/// Повышенный процесс не наследует окружение прежнего, поэтому отладочные `RUPOR_CORE`
/// и `RUPOR_CORE_ARGS` (например, `--no-hook --no-mutex`) передаются аргументами.
pub fn relaunch_params(
    pid: u32,
    show_main: bool,
    core: Option<&str>,
    core_args: Option<&str>,
) -> String {
    let mut args = vec!["--elevated".to_string(), "--wait-pid".into(), pid.to_string()];
    if show_main {
        args.push("--show-main".into());
    }
    for (flag, value) in [("--core-cmd", core), ("--core-args", core_args)] {
        if let Some(v) = value.filter(|v| !v.trim().is_empty()) {
            args.push(flag.into());
            args.push(v.into());
        }
    }
    args.iter().map(|a| winapi::quote_arg(a)).collect::<Vec<_>>().join(" ")
}

pub fn run() {
    let args: Vec<String> = std::env::args().collect();
    log_info!(
        "Рупор {} запускается (pid {}, аргументы: {:?})",
        env!("CARGO_PKG_VERSION"),
        std::process::id(),
        &args[1.min(args.len())..]
    );
    // Перезапуск от администратора: дождаться, пока прежний экземпляр закроется
    // (иначе single-instance отдаст запуск ему, а ядро упрётся в его мьютекс).
    if let Some(pid) = arg_value(&args, "--wait-pid").and_then(|v| v.parse::<u32>().ok()) {
        winapi::wait_process_exit(pid, Duration::from_secs(20));
    }
    if has_flag(&args, "--elevated") {
        // см. relaunch_params; других потоков ещё нет — менять окружение безопасно
        if let Some(v) = arg_value(&args, "--core-cmd") {
            std::env::set_var("RUPOR_CORE", v);
        }
        if let Some(v) = arg_value(&args, "--core-args") {
            std::env::set_var("RUPOR_CORE_ARGS", v);
        }
    }
    let show_main_at_start = has_flag(&args, "--show-main");
    // Язык трея, заголовков окон и сообщений оболочки — до подключения интерфейса:
    // `language` из настроек, для `system` — язык Windows. Потом его задаёт интерфейс
    // (`update_tray.labels.lang`).
    let s = settings::load();
    let lang = i18n::resolve(s.language.as_deref());
    i18n::set_current(lang);
    log_info!(
        "Язык оболочки: {} (настройка {})",
        lang.code(),
        s.language.as_deref().unwrap_or("system")
    );

    let supervisor = Arc::new(Supervisor::new());
    let sup_setup = Arc::clone(&supervisor);

    let built = tauri::Builder::default()
        // single-instance должен быть первым плагином
        .plugin(tauri_plugin_single_instance::init(|app, argv, _cwd| {
            log_info!(
                "Повторный запуск ({:?}) — показываю главное окно",
                &argv[1.min(argv.len())..]
            );
            show_main(app, None);
        }))
        .plugin(tauri_plugin_autostart::Builder::new().args(["--autostart"]).build())
        // Выбор папки моделей («Обзор…»); разрешено только окну main (dialog:allow-open)
        .plugin(tauri_plugin_dialog::init())
        .manage(supervisor)
        .invoke_handler(tauri::generate_handler![
            commands::backend_info,
            commands::update_tray,
            commands::relaunch_admin,
            commands::show_main,
            commands::quit_app,
            commands::set_pill_passthrough,
            commands::reset_pill_position,
            commands::pill_raise,
            commands::pill_log,
            commands::meeting_window_show,
            commands::meeting_window_hide,
            commands::meeting_window_minimize,
            commands::meeting_window_restore,
            commands::meeting_window_toggle,
            commands::meeting_window_reset,
            commands::meeting_window_log,
            commands::meeting_window_mode,
            commands::meeting_suggest_show,
            commands::meeting_suggest_done,
        ])
        .on_window_event(|window, event| {
            if let WindowEvent::CloseRequested { api, .. } = event {
                let quitting = window
                    .app_handle()
                    .try_state::<Arc<Supervisor>>()
                    .is_some_and(|s| s.is_quitting());
                if quitting {
                    return;
                }
                // Окна не закрываются: главное прячется в трей, окно перевода встречи — тоже
                // скрывается (сеанс идёт дальше), пилюля живёт всегда.
                api.prevent_close();
                if window.label() == MAIN_LABEL || window.label() == meeting::LABEL {
                    let _ = window.hide();
                }
            }
        })
        .setup(move |app| {
            let handle = app.handle().clone();
            set_default_titles(&handle);
            tray::create(&handle)?;
            pill::setup(&handle, &s);
            // Рупор стартует в трей. На первом запуске окно (мастер) открывает сам
            // фронтенд, и только если без настройки диктовка не заработает.
            if s.autostart {
                // Запись HKCU\...\Run могли стереть (деинсталлятор при обновлении,
                // чистильщики) — восстанавливаем, пока пользователь не выключил автозапуск.
                use tauri_plugin_autostart::ManagerExt;
                let al = app.autolaunch();
                match al.is_enabled() {
                    Ok(true) => {}
                    Ok(false) => match al.enable() {
                        Ok(()) => log_info!("Автозапуск с Windows включён"),
                        Err(e) => log_error!("Не удалось включить автозапуск: {e}"),
                    },
                    Err(e) => log_error!("Не удалось проверить автозапуск: {e}"),
                }
            }
            if show_main_at_start {
                // перезапуск от администратора из открытого главного окна
                show_main(&handle, None);
            }
            sup_setup.start(sidecar::AppHost(handle));
            Ok(())
        })
        .build(tauri::generate_context!());

    let app = match built {
        Ok(app) => app,
        Err(e) => {
            log_error!("Не удалось запустить интерфейс: {e}");
            winapi::error_box(&i18n::fill(i18n::t().startup_failed, &[("error", &e)]));
            std::process::exit(1);
        }
    };

    app.run(|handle, event| match event {
        // Все окна «закрыты» (их прячут) — приложение продолжает жить в трее.
        RunEvent::ExitRequested { code: None, api, .. } => {
            let quitting = handle.try_state::<Arc<Supervisor>>().is_some_and(|s| s.is_quitting());
            if !quitting {
                api.prevent_exit();
            }
        }
        RunEvent::Exit => {
            if let Some(sup) = handle.try_state::<Arc<Supervisor>>() {
                sup.shutdown();
            }
        }
        _ => {}
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn arg_value_finds_pair() {
        let a: Vec<String> =
            ["rupor.exe", "--elevated", "--wait-pid", "123"].map(String::from).to_vec();
        assert_eq!(arg_value(&a, "--wait-pid"), Some("123"));
        assert_eq!(arg_value(&a, "--nope"), None);
        let b: Vec<String> = ["rupor.exe", "--wait-pid"].map(String::from).to_vec();
        assert_eq!(arg_value(&b, "--wait-pid"), None);
    }

    /// Параметры перезапуска от администратора новый экземпляр разбирает обратно:
    /// окно, pid и отладочные переменные ядра не теряются.
    #[test]
    fn relaunch_params_round_trip() {
        let core = r#""C:\Program Files\Python\python.exe" -m rupor"#;
        let p = relaunch_params(4242, true, Some(core), Some("--no-hook --no-mutex -v"));
        // так Windows разберёт командную строку нового процесса: exe + параметры
        let args = winapi::command_line_to_argv(&format!("rupor.exe {p}"));
        assert!(has_flag(&args, "--elevated"));
        assert!(has_flag(&args, "--show-main"));
        assert_eq!(arg_value(&args, "--wait-pid"), Some("4242"));
        assert_eq!(arg_value(&args, "--core-cmd"), Some(core));
        assert_eq!(arg_value(&args, "--core-args"), Some("--no-hook --no-mutex -v"));

        assert_eq!(relaunch_params(7, false, None, Some("  ")), "--elevated --wait-pid 7");
    }
}
