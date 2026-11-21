//! Пилюля — плавающее окно поверх всех окон, которое **никогда не забирает фокус**
//! (иначе распознанный текст напечатается в неё, а не в окно пользователя). Как это
//! обеспечивается и как окно защищено от пропадания (подъём при смене активного окна,
//! сбои WebView2) — общее с окном перевода встречи, см. `overlay.rs`.
//!
//! Здесь — то, что относится только к пилюле:
//! - место по умолчанию — внизу по центру основного монитора, над панелью задач;
//! - после смены мониторов, разрешения, масштаба или рабочей области пилюля, оказавшаяся вне
//!   экранов, возвращается вниз по центру основного монитора ([`rescue`]);
//! - пункт трея «Вернуть пилюлю» — то же вручную: на место, наверх, перезагрузить.

use tauri::{AppHandle, Emitter, Manager, PhysicalPosition, PhysicalSize, WebviewWindow};
use windows_sys::Win32::Foundation::{HWND, POINT, RECT};
use windows_sys::Win32::Graphics::Gdi::{
    GetMonitorInfoW, MonitorFromPoint, MONITORINFO, MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTOPRIMARY,
};
use windows_sys::Win32::UI::HiDpi::{GetDpiForMonitor, GetDpiForWindow, MDT_EFFECTIVE_DPI};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetWindowRect, SetWindowPos, HWND_TOPMOST, SWP_NOACTIVATE, SWP_NOOWNERZORDER, SWP_NOSIZE,
};

use crate::overlay::{self, Overlay};
use crate::settings::UiSettings;

pub const LABEL: &str = "pill";
/// Отступ пилюли над панелью задач, логические пиксели.
const MARGIN_BOTTOM: f64 = 12.0;

/// Настроить пилюлю после создания окна: стили, позиция, видимость, защита от пропадания.
pub fn setup(app: &AppHandle, s: &UiSettings) {
    overlay::mark_started();
    let Some(pill) = app.get_webview_window(LABEL) else {
        crate::log_warn!("Окно пилюли не найдено");
        return;
    };
    match pill.hwnd() {
        // SAFETY: окно создано в этом (главном) потоке и живо; хук ставится в нём же —
        // этот поток крутит цикл сообщений Tauri, через него приходят события хука.
        Ok(h) => unsafe { overlay::install(h.0 as HWND, Overlay::Pill) },
        Err(e) => crate::log_warn!("Нет HWND пилюли: {e}"),
    }
    place(app, &pill, s.pill_position);
    if !s.pill_enabled {
        let _ = pill.hide();
    }
    for label in [LABEL, crate::MAIN_LABEL] {
        if let Some(w) = app.get_webview_window(label) {
            overlay::watch_webview(app, &w);
        }
    }
}

/// Команда `reset_pill_position`: вернуть пилюлю вниз по центру основного монитора.
pub fn reset_position(app: &AppHandle) -> Result<(), String> {
    let pill = app.get_webview_window(LABEL).ok_or(crate::i18n::t().pill_not_found)?;
    place(app, &pill, None);
    Ok(())
}

/// Команда `set_pill_passthrough`: пропускать клики мыши сквозь пилюлю.
pub fn set_passthrough(app: &AppHandle, on: bool) -> Result<(), String> {
    let pill = app.get_webview_window(LABEL).ok_or(crate::i18n::t().pill_not_found)?;
    pill.set_ignore_cursor_events(on).map_err(|e| e.to_string())
}

/// Поднять пилюлю наверх слоя «поверх всех» — без активации; скрытую не трогаем.
pub fn raise() {
    overlay::raise(Overlay::Pill);
}

/// Пункт трея «Вернуть пилюлю»: на место по умолчанию (и в настройках — через интерфейс,
/// `tray-action pill_restore`), показать, наверх и перезагрузить страницу — на случай, если
/// её содержимое пропало. WebView2 умер целиком — перезапустить оболочку; пилюля выключена
/// в настройках — открыть настройки.
pub fn restore(app: &AppHandle) {
    if overlay::browser_dead() {
        crate::log_warn!("Трей «Вернуть пилюлю»: WebView2 не работает — перезапускаю оболочку");
        overlay::restart_shell(app);
        return;
    }
    let Some(pill) = app.get_webview_window(LABEL) else {
        crate::log_warn!("Окно пилюли не найдено");
        return;
    };
    place(app, &pill, None);
    if let Err(e) = app.emit("tray-action", serde_json::json!({ "id": "pill_restore" })) {
        crate::log_warn!("Не удалось отправить tray-action: {e}");
    }
    if !crate::settings::load().pill_enabled {
        crate::log_info!("Трей «Вернуть пилюлю»: пилюля выключена в настройках — открываю их");
        crate::show_main(app, Some("settings".into()));
        return;
    }
    let _ = pill.show();
    raise();
    if let Err(e) = pill.reload() {
        crate::log_warn!("Не удалось перезагрузить пилюлю: {e}");
    }
    crate::log_info!("Пилюля возвращена на место (трей)");
}

fn place(app: &AppHandle, pill: &WebviewWindow, saved: Option<(i32, i32)>) {
    if let Some((x, y)) = saved {
        let size = pill.outer_size().unwrap_or(PhysicalSize::new(360, 96));
        if on_some_monitor(pill, x, y, size) {
            let _ = pill.set_position(PhysicalPosition::new(x, y));
            return;
        }
        crate::log_info!("Сохранённая позиция пилюли ({x}, {y}) вне экранов — ставлю по умолчанию");
    }
    match default_position(app, pill) {
        Some(p) => {
            let _ = pill.set_position(p);
        }
        None => crate::log_warn!("Не удалось определить основной монитор для пилюли"),
    }
}

/// Размер пилюли в логических пикселях — из tauri.conf.json.
fn logical_size(app: &AppHandle) -> (f64, f64) {
    app.config()
        .app
        .windows
        .iter()
        .find(|w| w.label == LABEL)
        .map(|w| (w.width, w.height))
        .unwrap_or((360.0, 96.0))
}

/// Снизу по центру рабочей области основного монитора (над панелью задач).
fn default_position(app: &AppHandle, pill: &WebviewWindow) -> Option<PhysicalPosition<i32>> {
    let mon = pill.primary_monitor().ok().flatten()?;
    let wa = mon.work_area();
    let (lw, lh) = logical_size(app);
    let (x, y) = bottom_center(
        (wa.position.x, wa.position.y, wa.size.width, wa.size.height),
        mon.scale_factor(),
        (lw, lh),
    );
    Some(PhysicalPosition::new(x, y))
}

/// Чистая арифметика позиции по умолчанию: `work_area` = (x, y, ширина, высота) в физических px.
pub fn bottom_center(
    work_area: (i32, i32, u32, u32),
    scale: f64,
    logical: (f64, f64),
) -> (i32, i32) {
    let (wx, wy, ww, wh) = work_area;
    let w = (logical.0 * scale).round() as i32;
    let h = (logical.1 * scale).round() as i32;
    let margin = (MARGIN_BOTTOM * scale).round() as i32;
    (wx + (ww as i32 - w) / 2, wy + wh as i32 - h - margin)
}

/// Позиция окна размером `size` (физ. px) снизу по центру рабочей области
/// `work` = (left, top, right, bottom) с отступом `margin`.
pub fn bottom_center_in(work: (i32, i32, i32, i32), size: (i32, i32), margin: i32) -> (i32, i32) {
    let (l, _t, r, b) = work;
    (l + (r - l - size.0) / 2, b - size.1 - margin)
}

/// Центр пилюли попадает на какой-нибудь монитор?
fn on_some_monitor(pill: &WebviewWindow, x: i32, y: i32, size: PhysicalSize<u32>) -> bool {
    let cx = x + size.width as i32 / 2;
    let cy = y + size.height as i32 / 2;
    pill.available_monitors()
        .map(|mons| {
            mons.iter().any(|m| {
                let p = m.position();
                let s = m.size();
                cx >= p.x && cy >= p.y && cx < p.x + s.width as i32 && cy < p.y + s.height as i32
            })
        })
        .unwrap_or(true)
}

/// Пилюля вне всех мониторов (после смены мониторов, разрешения, масштаба) — вернуть вниз по
/// центру основного; в любом случае — наверх. Только Win32: зовётся из оконной процедуры
/// (`overlay`, `WM_RESCUE`).
///
/// # Safety
/// `hwnd` — окно пилюли, вызов — в его потоке.
pub(crate) unsafe fn rescue(hwnd: HWND) {
    let mut r: RECT = std::mem::zeroed();
    if GetWindowRect(hwnd, &mut r) == 0 {
        return;
    }
    let center = POINT { x: (r.left + r.right) / 2, y: (r.top + r.bottom) / 2 };
    if !MonitorFromPoint(center, MONITOR_DEFAULTTONULL).is_null() {
        raise();
        return;
    }
    let mon = MonitorFromPoint(POINT { x: 0, y: 0 }, MONITOR_DEFAULTTOPRIMARY);
    let mut mi: MONITORINFO = std::mem::zeroed();
    mi.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
    if GetMonitorInfoW(mon, &mut mi) == 0 {
        return;
    }
    // Размер и отступ — в масштабе основного монитора (окно переедет туда, и tao пересчитает
    // его размер по WM_DPICHANGED)
    let win_dpi = GetDpiForWindow(hwnd).max(1);
    let (mut mx, mut my) = (0u32, 0u32);
    let mon_dpi = if GetDpiForMonitor(mon, MDT_EFFECTIVE_DPI, &mut mx, &mut my) == 0 && mx > 0 {
        mx
    } else {
        win_dpi
    };
    let k = mon_dpi as f64 / win_dpi as f64;
    let size = (
        ((r.right - r.left) as f64 * k).round() as i32,
        ((r.bottom - r.top) as f64 * k).round() as i32,
    );
    let w = &mi.rcWork;
    let (x, y) = bottom_center_in(
        (w.left, w.top, w.right, w.bottom),
        size,
        (MARGIN_BOTTOM * mon_dpi as f64 / 96.0).round() as i32,
    );
    SetWindowPos(hwnd, HWND_TOPMOST, x, y, 0, 0, SWP_NOSIZE | SWP_NOACTIVATE | SWP_NOOWNERZORDER);
    crate::log_info!(
        "Пилюля оказалась вне экранов после смены мониторов — вернул вниз по центру основного"
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_position_is_bottom_center() {
        // 1920×1040 рабочая область (панель задач 40 px), масштаб 100 %
        assert_eq!(bottom_center((0, 0, 1920, 1040), 1.0, (360.0, 96.0)), (780, 1040 - 96 - 12));
        // 150 %: 2560×1392, пилюля 540×144, отступ 18
        assert_eq!(bottom_center((0, 0, 2560, 1392), 1.5, (360.0, 96.0)), (1010, 1392 - 144 - 18));
        // основной монитор не в (0,0) и панель задач сверху
        assert_eq!(
            bottom_center((-1920, 48, 1920, 1032), 1.0, (360.0, 96.0)),
            (-1140, 48 + 1032 - 108)
        );
    }

    #[test]
    fn rescue_position_is_bottom_center_of_work_area() {
        // рабочая область 2752×1104 (панель задач 48 px), пилюля 360×96, отступ 12
        assert_eq!(bottom_center_in((0, 0, 2752, 1104), (360, 96), 12), (1196, 996));
        // монитор слева с отрицательными координатами
        assert_eq!(bottom_center_in((-1920, 0, 0, 1032), (360, 96), 12), (-1140, 924));
        // совпадает с позицией по умолчанию при тех же числах
        assert_eq!(
            bottom_center_in((0, 0, 1920, 1040), (360, 96), 12),
            bottom_center((0, 0, 1920, 1040), 1.0, (360.0, 96.0))
        );
    }
}
