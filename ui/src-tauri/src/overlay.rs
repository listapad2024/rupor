//! Окна поверх всех, которые **никогда не забирают фокус**: пилюля (`pill.rs`) и окно
//! перевода встречи (`meeting.rs`). Иначе распознанный текст напечатается в них, а не в окно
//! пользователя.
//!
//! Как это обеспечивается (общее для обоих окон, окно — по HWND, вид — [`Overlay`]):
//! - в tauri.conf.json у окна `focus: false` (первый показ — `SW_SHOWNOACTIVATE`) и
//!   `focusable: false` (tao держит `WS_EX_NOACTIVATE`);
//! - после создания ставим `WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW` (нет в Alt+Tab и на панели задач);
//! - сабклассинг окна: tao переписывает `GWL_EXSTYLE` при любой смене своих флагов
//!   (например, `setIgnoreCursorEvents`), поэтому в `WM_STYLECHANGING` нужные биты
//!   возвращаются; `WM_MOUSEACTIVATE` → `MA_NOACTIVATE`; `WM_WINDOWPOSCHANGING` → `SWP_NOACTIVATE`;
//!   если окно всё же активировали (например, WebView2 взял фокус), активность сразу
//!   возвращается окну, у которого её отняли.
//!
//! Окна не пропадают, пока Рупор работает:
//! - «поверх всех» подтверждается при каждой смене активного окна (`EVENT_SYSTEM_FOREGROUND`,
//!   один хук на все окна): сначала окно перевода, потом пилюля — пилюля остаётся над ним;
//! - после смены мониторов, разрешения, масштаба или рабочей области (`WM_DISPLAYCHANGE`,
//!   `WM_DPICHANGED`, `WM_SETTINGCHANGE`/`SPI_SETWORKAREA`) окно проверяет своё место
//!   (`pill::rescue` / `meeting::rescue`) — отложенно, когда tao и Windows обработают сообщение;
//! - упал процесс WebView2 (`ProcessFailed`: страница, GPU) — страница перезагружается, упал
//!   сам браузерный процесс — оболочка перезапускается (без этого прозрачное окно пустое,
//!   а диктовка идёт «невидимо»).

use std::collections::HashMap;
use std::ptr::null_mut;
use std::sync::atomic::{AtomicBool, AtomicIsize, Ordering};
use std::sync::{Mutex, Once, OnceLock};
use std::time::{Duration, Instant};

use tauri::{AppHandle, WebviewWindow};
use windows_sys::Win32::Foundation::{HWND, LPARAM, LRESULT, WPARAM};
use windows_sys::Win32::UI::Accessibility::{SetWinEventHook, HWINEVENTHOOK};
use windows_sys::Win32::UI::Shell::{DefSubclassProc, RemoveWindowSubclass, SetWindowSubclass};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetForegroundWindow, GetWindowLongPtrW, IsWindow, IsWindowVisible, PostMessageW,
    SetForegroundWindow, SetWindowLongPtrW, SetWindowPos, EVENT_SYSTEM_FOREGROUND, GWL_EXSTYLE,
    HWND_TOPMOST, MA_NOACTIVATE, SPI_SETWORKAREA, STYLESTRUCT, SWP_FRAMECHANGED, SWP_NOACTIVATE,
    SWP_NOMOVE, SWP_NOOWNERZORDER, SWP_NOSIZE, SWP_NOZORDER, WA_INACTIVE, WINDOWPOS,
    WINEVENT_OUTOFCONTEXT, WINEVENT_SKIPOWNPROCESS, WM_ACTIVATE, WM_APP, WM_DISPLAYCHANGE,
    WM_DPICHANGED, WM_ENTERSIZEMOVE, WM_EXITSIZEMOVE, WM_MOUSEACTIVATE, WM_NCDESTROY,
    WM_SETTINGCHANGE, WM_STYLECHANGING, WM_WINDOWPOSCHANGING, WS_EX_APPWINDOW, WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
};

const SUBCLASS_ID: usize = 0x5255_5052; // «RUPR»
const WM_GIVE_BACK_FOCUS: u32 = WM_APP + 0x52;
/// Мониторы или рабочая область изменились — проверить, видно ли окно.
const WM_RESCUE: u32 = WM_APP + 0x53;
/// Браузерный процесс WebView2 упал раньше — оболочку сама не перезапускает (защита от цикла;
/// вручную — пункт трея «Вернуть пилюлю»).
const RESTART_AFTER: Duration = Duration::from_secs(120);
/// Зависание страницы окна поверх всех: перезагрузить, если WebView2 сообщил о нём столько раз
/// за окно (событие повторяется примерно раз в 15 с, пока страница не ответит).
const UNRESPONSIVE_TIMES: usize = 3;
const UNRESPONSIVE_WINDOW: Duration = Duration::from_secs(60);
/// Не больше стольких автоматических перезагрузок окна за окно времени — дальше только журнал.
const RELOADS_MAX: usize = 3;
const RELOADS_WINDOW: Duration = Duration::from_secs(300);

/// Окно поверх всех без фокуса.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Overlay {
    Pill = 0,
    Meeting = 1,
}

impl Overlay {
    /// Порядок подъёма при смене активного окна: снизу вверх (пилюля — над окном перевода).
    const RAISE_ORDER: [Overlay; 2] = [Overlay::Meeting, Overlay::Pill];

    /// Вид окна по `dwRefData` сабкласса.
    fn from_ref(data: usize) -> Option<Overlay> {
        match data {
            0 => Some(Overlay::Pill),
            1 => Some(Overlay::Meeting),
            _ => None,
        }
    }

    fn label(self) -> &'static str {
        match self {
            Overlay::Pill => crate::pill::LABEL,
            Overlay::Meeting => crate::meeting::LABEL,
        }
    }
}

/// HWND окон по [`Overlay`] (0 — окна ещё нет или оно уничтожено): нужны хуку смены
/// активного окна.
static HWNDS: [AtomicIsize; 2] = [AtomicIsize::new(0), AtomicIsize::new(0)];
static HOOK: Once = Once::new();
static STARTED: OnceLock<Instant> = OnceLock::new();
static RESTARTING: AtomicBool = AtomicBool::new(false);
/// Браузерный процесс WebView2 умер: окна интерфейса не оживить без перезапуска оболочки.
static BROWSER_DEAD: AtomicBool = AtomicBool::new(false);
/// Сообщения о зависании страницы — по метке окна.
static UNRESPONSIVE: OnceLock<Mutex<HashMap<String, Vec<Instant>>>> = OnceLock::new();
static RELOADS: OnceLock<Mutex<HashMap<String, Vec<Instant>>>> = OnceLock::new();

/// Запомнить время запуска оболочки (перезапуск после сбоя WebView2 — не раньше RESTART_AFTER).
pub fn mark_started() {
    STARTED.get_or_init(Instant::now);
}

/// Окно никогда не активируется и поднимается при смене активного окна. Вызывать в потоке,
/// владеющем окном (главном: он крутит цикл сообщений Tauri, через него приходят события хука).
///
/// # Safety
/// `hwnd` — живое окно этого потока.
pub unsafe fn install(hwnd: HWND, which: Overlay) {
    HWNDS[which as usize].store(hwnd as isize, Ordering::SeqCst);
    if SetWindowSubclass(hwnd, Some(subclass_proc), SUBCLASS_ID, which as usize) == 0 {
        crate::log_warn!("Не удалось установить сабкласс окна {}", which.label());
    }
    let ex = GetWindowLongPtrW(hwnd, GWL_EXSTYLE) as u32;
    SetWindowLongPtrW(hwnd, GWL_EXSTYLE, no_activate_style(ex) as isize);
    SetWindowPos(
        hwnd,
        null_mut(),
        0,
        0,
        0,
        0,
        SWP_NOMOVE | SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED,
    );
    HOOK.call_once(|| install_foreground_hook());
}

/// Тесты: окно этого вида — `hwnd` (null — окна нет), без сабкласса и хука.
#[cfg(test)]
pub(crate) fn set_hwnd_for_test(which: Overlay, hwnd: HWND) {
    HWNDS[which as usize].store(hwnd as isize, Ordering::SeqCst);
}

/// HWND окна, если оно есть.
pub fn hwnd(which: Overlay) -> Option<HWND> {
    match HWNDS[which as usize].load(Ordering::SeqCst) {
        0 => None,
        h => Some(h as HWND),
    }
}

/// Поднять окно наверх слоя «поверх всех» — без активации; скрытое не трогаем.
pub fn raise(which: Overlay) {
    let Some(hwnd) = hwnd(which) else { return };
    // SAFETY: SetWindowPos можно звать из любого потока; окно проверяется на жизнь.
    unsafe {
        if IsWindow(hwnd) != 0 && IsWindowVisible(hwnd) != 0 {
            SetWindowPos(
                hwnd,
                HWND_TOPMOST,
                0,
                0,
                0,
                0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER,
            );
        }
    }
}

/// Все окна наверх: окно перевода, над ним пилюля.
pub fn raise_all() {
    for w in Overlay::RAISE_ORDER {
        raise(w);
    }
}

/// Расширенный стиль окна поверх всех: не активируется, служебное окно, без кнопки на панели
/// задач.
pub fn no_activate_style(ex: u32) -> u32 {
    (ex | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW) & !WS_EX_APPWINDOW
}

/// Хук смены активного окна: после каждой — окна снова наверх слоя «поверх всех».
unsafe fn install_foreground_hook() {
    let hook = SetWinEventHook(
        EVENT_SYSTEM_FOREGROUND,
        EVENT_SYSTEM_FOREGROUND,
        null_mut(),
        Some(on_foreground),
        0,
        0,
        WINEVENT_OUTOFCONTEXT | WINEVENT_SKIPOWNPROCESS,
    );
    if hook.is_null() {
        crate::log_warn!("Не удалось поставить хук смены активного окна для окон поверх всех");
    }
}

unsafe extern "system" fn on_foreground(
    _hook: HWINEVENTHOOK,
    _event: u32,
    _hwnd: HWND,
    _id_object: i32,
    _id_child: i32,
    _thread: u32,
    _time: u32,
) {
    raise_all();
}

unsafe extern "system" fn subclass_proc(
    hwnd: HWND,
    msg: u32,
    wparam: WPARAM,
    lparam: LPARAM,
    _id: usize,
    data: usize,
) -> LRESULT {
    let which = Overlay::from_ref(data);
    match msg {
        // клик мышью не активирует окно (сам клик до страницы доходит)
        WM_MOUSEACTIVATE => return MA_NOACTIVATE as LRESULT,
        // tao пересчитывает GWL_EXSTYLE из своих флагов — возвращаем наши биты
        WM_STYLECHANGING if wparam as i32 == GWL_EXSTYLE => {
            if let Some(ss) = (lparam as *mut STYLESTRUCT).as_mut() {
                ss.styleNew = no_activate_style(ss.styleNew);
            }
        }
        // любые перемещения/показы — без активации
        WM_WINDOWPOSCHANGING => {
            if let Some(wp) = (lparam as *mut WINDOWPOS).as_mut() {
                wp.flags |= SWP_NOACTIVATE;
            }
        }
        // если окно всё же стало активным — вернуть активность прежнему окну
        WM_ACTIVATE => {
            if (wparam & 0xFFFF) as u32 != WA_INACTIVE && lparam != 0 {
                PostMessageW(hwnd, WM_GIVE_BACK_FOCUS, 0, lparam);
            }
        }
        WM_GIVE_BACK_FOCUS => {
            let prev = lparam as HWND;
            if prev != hwnd && IsWindow(prev) != 0 && GetForegroundWindow() == hwnd {
                SetForegroundWindow(prev);
            }
            return 0;
        }
        // мониторы, разрешение, масштаб, рабочая область — проверить, видно ли окно
        // (после того как tao и Windows обработают сообщение — поэтому отложенно)
        WM_DISPLAYCHANGE | WM_DPICHANGED => {
            PostMessageW(hwnd, WM_RESCUE, 0, 0);
        }
        WM_SETTINGCHANGE if wparam as u32 == SPI_SETWORKAREA => {
            PostMessageW(hwnd, WM_RESCUE, 0, 0);
        }
        WM_RESCUE => {
            match which {
                Some(Overlay::Pill) => crate::pill::rescue(hwnd),
                Some(Overlay::Meeting) => crate::meeting::rescue(hwnd),
                None => {}
            }
            return 0;
        }
        // пользователь начал и закончил перетаскивать окно или менять его размер (за край)
        WM_ENTERSIZEMOVE if which == Some(Overlay::Meeting) => {
            crate::meeting::on_move_start(hwnd);
        }
        WM_EXITSIZEMOVE if which == Some(Overlay::Meeting) => {
            crate::meeting::on_user_moved(hwnd);
        }
        WM_NCDESTROY => {
            if let Some(w) = which {
                let _ = HWNDS[w as usize].compare_exchange(
                    hwnd as isize,
                    0,
                    Ordering::SeqCst,
                    Ordering::SeqCst,
                );
            }
            RemoveWindowSubclass(hwnd, Some(subclass_proc), SUBCLASS_ID);
        }
        _ => {}
    }
    DefSubclassProc(hwnd, msg, wparam, lparam)
}

// ——— WebView2: сбои процессов ———

/// Подписаться на падение процессов WebView2 окна: страница или GPU — перезагрузить окно,
/// браузерный процесс — перезапустить оболочку. Без этого упавшее прозрачное окно пустое.
pub fn watch_webview(app: &AppHandle, win: &WebviewWindow) {
    use webview2_com::Microsoft::Web::WebView2::Win32::COREWEBVIEW2_PROCESS_FAILED_KIND;
    use webview2_com::ProcessFailedEventHandler;

    let label = win.label().to_string();
    let handle = app.clone();
    let res = win.with_webview(move |pw| {
        // SAFETY: COM-вызовы WebView2 — в его (главном) потоке, куда with_webview и зовёт.
        unsafe {
            let core = match pw.controller().CoreWebView2() {
                Ok(c) => c,
                Err(e) => {
                    crate::log_warn!("WebView2 окна {label} недоступен: {e}");
                    return;
                }
            };
            let lbl = label.clone();
            let handler = ProcessFailedEventHandler::create(Box::new(move |_wv, args| {
                let kind = args
                    .map(|a| {
                        let mut k = COREWEBVIEW2_PROCESS_FAILED_KIND(-1);
                        let _ = a.ProcessFailedKind(&mut k);
                        k.0
                    })
                    .unwrap_or(-1);
                on_process_failed(&handle, &lbl, kind);
                Ok(())
            }));
            let mut token = 0i64;
            if let Err(e) = core.add_ProcessFailed(&handler, &mut token) {
                crate::log_warn!("Не удалось подписаться на сбои WebView2 окна {label}: {e}");
            }
        }
    });
    if let Err(e) = res {
        crate::log_warn!("Нет доступа к WebView2 окна {}: {e}", win.label());
    }
}

/// Что делать при сбое процесса WebView2 (`COREWEBVIEW2_PROCESS_FAILED_KIND`).
#[derive(Debug, PartialEq, Eq)]
pub enum Recovery {
    /// Упал браузерный процесс — живых окон интерфейса нет: перезапустить оболочку.
    Restart,
    /// Упала страница окна — перезагрузить окно.
    Reload,
    /// Зависла страница окна поверх всех — перезагрузить, если это повторяется.
    ReloadIfRepeated,
    /// Не фатально: GPU, утилиты, плагины WebView2 поднимает сам (перезагрузка только сбила
    /// бы пилюлю посреди диктовки и состояние главного окна); зависание главного окна ждём.
    Nothing,
}

/// Восстановление по виду сбоя: 0 — браузерный процесс, 1/3 — страница, 2 — зависание
/// страницы (перезагружаем только окна поверх всех, `overlay`), остальное (6 — GPU,
/// 4 — утилиты…) — WebView2 справится сам.
pub fn recovery(kind: i32, overlay: bool) -> Recovery {
    match kind {
        0 => Recovery::Restart,
        1 | 3 => Recovery::Reload,
        2 if overlay => Recovery::ReloadIfRepeated,
        _ => Recovery::Nothing,
    }
}

/// Отметить событие и сказать, набралось ли `times` за `window` (тогда счётчик обнуляется).
fn repeated(events: &mut Vec<Instant>, now: Instant, times: usize, window: Duration) -> bool {
    events.retain(|t| now.duration_since(*t) < window);
    events.push(now);
    if events.len() >= times {
        events.clear();
        return true;
    }
    false
}

/// Ещё можно перезагрузить окно автоматически (не больше RELOADS_MAX за RELOADS_WINDOW)?
fn reload_allowed(label: &str, now: Instant) -> bool {
    let map = RELOADS.get_or_init(|| Mutex::new(HashMap::new()));
    let mut map = map.lock().unwrap_or_else(|e| e.into_inner());
    let list = map.entry(label.to_string()).or_default();
    list.retain(|t| now.duration_since(*t) < RELOADS_WINDOW);
    if list.len() >= RELOADS_MAX {
        return false;
    }
    list.push(now);
    true
}

/// Страница окна `label` снова не отвечает — пора перезагрузить?
fn unresponsive_repeated(label: &str, now: Instant) -> bool {
    let map = UNRESPONSIVE.get_or_init(|| Mutex::new(HashMap::new()));
    let mut map = map.lock().unwrap_or_else(|e| e.into_inner());
    let events = map.entry(label.to_string()).or_default();
    repeated(events, now, UNRESPONSIVE_TIMES, UNRESPONSIVE_WINDOW)
}

fn on_process_failed(app: &AppHandle, label: &str, kind: i32) {
    let what = recovery(kind, label != crate::MAIN_LABEL);
    crate::log_warn!("Сбой процесса WebView2 окна {label} (вид {kind}) — {what:?}");
    let reload = match what {
        Recovery::Nothing => false,
        Recovery::Reload => true,
        Recovery::ReloadIfRepeated => unresponsive_repeated(label, Instant::now()),
        Recovery::Restart => {
            BROWSER_DEAD.store(true, Ordering::SeqCst);
            let uptime = STARTED.get().map(|t| t.elapsed()).unwrap_or_default();
            if uptime < RESTART_AFTER {
                crate::log_error!(
                    "WebView2 упал сразу после запуска — оболочку сам не перезапускаю \
                     (вручную: трей «Вернуть пилюлю»)"
                );
            } else if !RESTARTING.swap(true, Ordering::SeqCst) {
                restart_shell(app);
            }
            false
        }
    };
    if !reload {
        return;
    }
    if !reload_allowed(label, Instant::now()) {
        crate::log_error!("Окно {label} падает снова и снова — больше не перезагружаю");
        return;
    }
    let app = app.clone();
    let label = label.to_string();
    std::thread::spawn(move || {
        std::thread::sleep(Duration::from_millis(300));
        if let Some(w) = tauri::Manager::get_webview_window(&app, &label) {
            if let Err(e) = w.reload() {
                crate::log_warn!("Не удалось перезагрузить окно {label}: {e}");
            }
        }
    });
}

/// Браузерный процесс WebView2 умер — окна интерфейса оживит только перезапуск оболочки.
pub fn browser_dead() -> bool {
    BROWSER_DEAD.load(Ordering::SeqCst)
}

/// Запустить новый экземпляр оболочки, который дождётся выхода этого (`--wait-pid`, иначе
/// single-instance отдал бы запуск старому), и выйти. Ядро перезапустится вместе с ней.
pub fn restart_shell(app: &AppHandle) {
    let exe = match std::env::current_exe() {
        Ok(p) => p,
        Err(e) => {
            crate::log_error!("Не найден путь к Рупору для перезапуска: {e}");
            return;
        }
    };
    let pid = std::process::id().to_string();
    match std::process::Command::new(exe).args(["--wait-pid", &pid]).spawn() {
        Ok(_) => {
            crate::log_warn!("Перезапускаю оболочку после сбоя WebView2");
            crate::quit(app);
        }
        Err(e) => crate::log_error!("Не удалось перезапустить оболочку: {e}"),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn webview_failure_recovery() {
        assert_eq!(recovery(0, true), Recovery::Restart); // браузерный процесс
        assert_eq!(recovery(1, false), Recovery::Reload); // страница упала
        assert_eq!(recovery(2, true), Recovery::ReloadIfRepeated); // пилюля/окно перевода зависли
        assert_eq!(recovery(2, false), Recovery::Nothing); // главное окно — ждём
        assert_eq!(recovery(6, true), Recovery::Nothing); // GPU — WebView2 сам
        assert_eq!(recovery(4, true), Recovery::Nothing); // утилиты
        assert_eq!(recovery(-1, true), Recovery::Nothing);
    }

    #[test]
    fn unresponsive_needs_repeats_and_reloads_are_limited() {
        let t0 = Instant::now();
        let mut ev = Vec::new();
        let w = Duration::from_secs(60);
        assert!(!repeated(&mut ev, t0, 3, w));
        assert!(!repeated(&mut ev, t0 + Duration::from_secs(15), 3, w));
        assert!(repeated(&mut ev, t0 + Duration::from_secs(30), 3, w));
        assert!(ev.is_empty()); // после перезагрузки — с нуля
                                // события дальше окна друг от друга не накапливаются
        assert!(!repeated(&mut ev, t0 + Duration::from_secs(100), 3, w));
        assert!(!repeated(&mut ev, t0 + Duration::from_secs(200), 3, w));
        assert!(!repeated(&mut ev, t0 + Duration::from_secs(300), 3, w));

        let now = Instant::now();
        for _ in 0..RELOADS_MAX {
            assert!(reload_allowed("test-window", now));
        }
        assert!(!reload_allowed("test-window", now));
        assert!(reload_allowed("other-window", now));
    }

    /// Зависания считаются для каждого окна отдельно: пилюля и окно перевода не сбивают
    /// друг другу счёт.
    #[test]
    fn unresponsive_counted_per_window() {
        let now = Instant::now();
        assert!(!unresponsive_repeated("test-a", now));
        assert!(!unresponsive_repeated("test-b", now));
        assert!(!unresponsive_repeated("test-a", now));
        assert!(!unresponsive_repeated("test-b", now));
        assert!(unresponsive_repeated("test-a", now));
        assert!(unresponsive_repeated("test-b", now));
    }

    #[test]
    fn style_bits() {
        let s = no_activate_style(WS_EX_APPWINDOW | 0x8 /* WS_EX_TOPMOST */);
        assert_eq!(s & WS_EX_APPWINDOW, 0);
        assert_ne!(s & WS_EX_NOACTIVATE, 0);
        assert_ne!(s & WS_EX_TOOLWINDOW, 0);
        assert_ne!(s & 0x8, 0);
    }

    /// Вид окна в `dwRefData` сабкласса и порядок подъёма: пилюля — последней (сверху).
    #[test]
    fn overlay_kinds() {
        for w in [Overlay::Pill, Overlay::Meeting] {
            assert_eq!(Overlay::from_ref(w as usize), Some(w));
        }
        assert_eq!(Overlay::from_ref(7), None);
        assert_eq!(Overlay::RAISE_ORDER.last(), Some(&Overlay::Pill));
        assert_eq!(Overlay::Meeting.label(), "meeting");
        assert_eq!(Overlay::Pill.label(), "pill");
    }
}
