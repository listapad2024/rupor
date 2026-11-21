//! Окно перевода встречи (`meeting`, страница `meeting.html`): узкая панель у правого края
//! экрана с живой расшифровкой и переводом речи собеседников — и, в компактном виде,
//! карточка «Обнаружена новая встреча» в правом верхнем углу или полоска «свёрнутого»
//! перевода (последняя строка бегущей строкой) у правого нижнего края.
//!
//! - Создаётся при первом открытии (`create: false` в tauri.conf.json, флаги окна — оттуда же):
//!   третий WebView2 и ещё один WS-клиент живут, только если перевод встречи хоть раз открыли
//!   (или ядро хоть раз предложило встречу). Окно строится вне главного потока (иначе WebView2
//!   на Windows может зависнуть), стили «без фокуса», место и показ — уже в главном потоке,
//!   владельце окна.
//! - **Никогда не забирает фокус** — как пилюля (`overlay.rs`): диктовка во время встречи
//!   печатает в окно пользователя, а клики и колесо мыши в панели работают без активации.
//!   Клавиатуры в панели нет: копирование — командой ядра `meeting_copy`.
//! - Непрозрачное (упавший WebView2 в прозрачном окне невидим), с тенью; размер — за края
//!   (`resizable` без рамки — Tauri сам добавляет невидимые края), перетаскивание — за шапку.
//! - Три вида ([`Mode`]): `full` — панель перевода; `mini` — компактная полоска
//!   ([`MINI_SIZE`], «Свернуть» в шапке панели — `meeting_window_minimize`; развернуть —
//!   `meeting_window_restore` / `meeting_window_show`, панель встаёт на своё место); `suggest`
//!   — карточка предложения ядра
//!   (`meeting_suggest`: какое-то приложение держит микрофон и играет звук). Предложение
//!   приносит главное окно (его WS-клиент живёт всегда, у Rust клиента ядра нет): команда
//!   `meeting_suggest_show`. Окно скрыто — оно становится карточкой (без изменения размера за
//!   края, не меньше ничего не требует) и показывается без фокуса; панель уже видна — она не
//!   меняется, страница показывает предложение полоской над лентой. Через 45 с предложение
//!   пропадает само (карточка скрывается). Ответ — страница: «Начать расшифровку» →
//!   `meeting_window_show` (полный вид на прежнем месте) и `meeting_start`; «Не сейчас» /
//!   «Не предлагать» → `meeting_suggest_done` (карточка скрывается). Вид и предложение
//!   страница берёт командой `meeting_window_mode` и событием [`MODE_EVENT`].
//! - Место панели: `meeting_rect` из настроек (прямоугольник окна, физические пиксели), если
//!   окно на каком-нибудь мониторе (иначе подвигается в рабочую область); `null` — у правого
//!   края рабочей области монитора активного окна, на всю высоту, видимая ширина 420
//!   логических px ([`dock_right`]). Места считаются по видимой части окна: невидимые края,
//!   которые tao оставляет для изменения размера ([`Insets`]), уходят за край рабочей области,
//!   а видимый край встаёт вровень с ним (и у карточки отступ — от видимого края).
//!   Пользователь передвинул окно или поменял размер (`WM_ENTERSIZEMOVE` → `WM_EXITSIZEMOVE`,
//!   прямоугольник изменился — щелчок по шапке без переноса окно не «отстыковывает») —
//!   событие `meeting-window-rect` `[x, y, w, h]` странице окна, она сохраняет `meeting_rect`.
//!   Карточка ([`suggest_rect`]) место панели не трогает и не сохраняется. Полоска — справа
//!   внизу рабочей области ([`mini_rect`]) или где её оставил пользователь: после переноса —
//!   событие [`MINI_POS_EVENT`] `[x, y]` (левый верхний угол видимой части), страница
//!   сохраняет `meeting_mini_pos`; вне всех мониторов — снова справа внизу.
//! - Смена мониторов, разрешения, масштаба или рабочей области — [`rescue`]: пристыкованное
//!   окно встаёт к правому краю заново, перенесённое — возвращается в рабочую область,
//!   карточка — снова в правый верхний угол, полоска — в рабочую область (или справа внизу).
//! - Над всеми окнами, но под пилюлей: `overlay::raise_all` (окно, над ним пилюля) — при
//!   каждом показе панели или карточки, после переноса окна пользователем и при смене
//!   активного окна; сама расстановка окна (`SetWindowPos`) порядок окон не меняет.
//! - `contentProtected` — из `meeting_protect` при создании (дальше страница меняет сама):
//!   при демонстрации экрана собеседники панель (и карточку) не видят.
//! - Закрытие (`CloseRequested`) — скрыть; пункт трея «Перевод встречи» — показать панель или
//!   скрыть её (полоска и скрытое окно — показать панель). Скрытое окно сеанс не трогает:
//!   перевод идёт дальше, в трее — «Перевод встречи (идёт)» (`tray.rs`).
//! - Клики до страницы доходят без активации окна, как у пилюли: сабкласс отвечает на
//!   `WM_MOUSEACTIVATE` `MA_NOACTIVATE` (клик не «съедается», как при `MA_NOACTIVATEANDEAT`), а
//!   окно Chromium внутри WebView2 при `WS_EX_NOACTIVATE` у корневого окна само отвечает так же.
//!   Кнопки шапки — вне перетаскивания (страница не зовёт `startDragging` с кнопки).

use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{mpsc, Mutex, OnceLock};
use std::time::{Duration, Instant};

use serde::Serialize;
use serde_json::Value;
use tauri::{AppHandle, Emitter, LogicalSize, Manager, Size, WebviewWindow, WebviewWindowBuilder};
use windows_sys::Win32::Foundation::{HWND, POINT, RECT};
use windows_sys::Win32::Graphics::Dwm::{DwmGetWindowAttribute, DWMWA_EXTENDED_FRAME_BOUNDS};
use windows_sys::Win32::Graphics::Gdi::{
    GetMonitorInfoW, MonitorFromPoint, MonitorFromWindow, HMONITOR, MONITORINFO,
    MONITOR_DEFAULTTONULL, MONITOR_DEFAULTTOPRIMARY,
};
use windows_sys::Win32::UI::HiDpi::{
    GetDpiForMonitor, GetDpiForWindow, GetSystemMetricsForDpi, MDT_EFFECTIVE_DPI,
};
use windows_sys::Win32::UI::WindowsAndMessaging::{
    GetForegroundWindow, GetWindowRect, IsWindowVisible, SetWindowPos, SM_CXPADDEDBORDER,
    SM_CXSIZEFRAME, SWP_NOACTIVATE, SWP_NOOWNERZORDER, SWP_NOZORDER,
};

use crate::overlay::{self, Overlay};
use crate::settings::{self, MeetingRect, MEETING_RECT_MIN};

pub const LABEL: &str = "meeting";
/// Ширина пристыкованного окна (логические px), если в tauri.conf.json её нет.
const DOCK_WIDTH: f64 = 420.0;
/// Наименьший размер панели (логические px), если в tauri.conf.json его нет.
const MIN_SIZE: (f64, f64) = (300.0, 260.0);
/// Событие странице окна: пользователь передвинул окно или поменял размер.
pub const RECT_EVENT: &str = "meeting-window-rect";
/// Событие странице окна: сменился вид или предложение ([`ModeInfo`]).
pub const MODE_EVENT: &str = "meeting-window-mode";
/// Событие странице окна: пользователь передвинул полоску (`[x, y]` видимой части).
pub const MINI_POS_EVENT: &str = "meeting-window-mini-pos";
/// Полоска свёрнутого перевода: размер (логические px) и отступ от краёв рабочей области.
pub const MINI_SIZE: (f64, f64) = (340.0, 64.0);
const MINI_MARGIN: f64 = 12.0;
/// Сколько ждать главный поток (показ окна после создания).
const MAIN_THREAD_WAIT: Duration = Duration::from_secs(10);
/// Карточка предложения: размер (логические px) и отступ от краёв рабочей области.
const SUGGEST_SIZE: (f64, f64) = (372.0, 176.0);
const SUGGEST_MARGIN: f64 = 12.0;
/// Предложение без ответа пропадает само через столько.
pub const SUGGEST_TTL: Duration = Duration::from_secs(45);
/// Только что созданное окно-карточку показываем, когда страница ожила (спросила вид), —
/// иначе на полсекунды мелькнёт пустая карточка; но не дольше стольких.
const PAGE_WAIT: Duration = Duration::from_millis(2500);

/// Окно строится один раз: второй запрос ждёт первый, а не падает на занятой метке.
static CREATING: Mutex<()> = Mutex::new(());
/// Для событий из оконной процедуры (`WM_EXITSIZEMOVE`).
static APP: OnceLock<AppHandle> = OnceLock::new();
/// Прямоугольник окна в начале перетаскивания или изменения размера (`WM_ENTERSIZEMOVE`).
static MOVE_START: Mutex<Option<[i32; 4]>> = Mutex::new(None);
/// Вид окна и предложение ядра.
static MODE: Mutex<ModeState> = Mutex::new(ModeState::new());
/// Страница окна хоть раз спросила вид (`meeting_window_mode`) — она загружена.
static PAGE_SEEN: AtomicBool = AtomicBool::new(false);

/// Прямоугольник (left, top, right, bottom) в физических пикселях.
type Ltrb = (i32, i32, i32, i32);
/// Прямоугольник (x, y, ширина, высота) в физических пикселях.
type Xywh = (i32, i32, i32, i32);
/// Невидимый край окна толще стольких физических px — измерение рамки DWM бессмысленно.
const MAX_INSET: i32 = 64;

/// Невидимые края окна (физические px): между прямоугольником окна (`GetWindowRect`,
/// `SetWindowPos`) и тем, что видно на экране (рамка DWM, `DWMWA_EXTENDED_FRAME_BOUNDS`).
/// У окна без рамки с тенью (`decorations: false`, `shadow: true`) tao оставляет вокруг
/// страницы рамку для изменения размера (`WM_NCCALCSIZE`: `SM_CXSIZEFRAME` +
/// `SM_CXPADDEDBORDER` слева, справа и снизу) — её не видно, кроме полоски в 1 px у самой
/// страницы; сверху невидимого края нет. Места панели и карточки считаются по видимому
/// прямоугольнику: видимый край встаёт вровень с краем рабочей области, а невидимые уходят
/// за него.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Default)]
pub struct Insets {
    pub left: i32,
    pub top: i32,
    pub right: i32,
    pub bottom: i32,
}

impl Insets {
    /// Видимый прямоугольник → прямоугольник окна (для `SetWindowPos`).
    pub fn outer(self, (x, y, w, h): Xywh) -> Xywh {
        (x - self.left, y - self.top, w + self.left + self.right, h + self.top + self.bottom)
    }

    /// Прямоугольник окна → видимый.
    pub fn visible(self, (x, y, w, h): Xywh) -> Xywh {
        (
            x + self.left,
            y + self.top,
            (w - self.left - self.right).max(1),
            (h - self.top - self.bottom).max(1),
        )
    }

    /// Края по прямоугольнику окна и рамке DWM (оба — left, top, right, bottom). Рамка не
    /// внутри окна или край толще [`MAX_INSET`] — `None` (тогда — [`Insets::estimated`]).
    pub fn measured(window: Ltrb, frame: Ltrb) -> Option<Insets> {
        let i = Insets {
            left: frame.0 - window.0,
            top: frame.1 - window.1,
            right: window.2 - frame.2,
            bottom: window.3 - frame.3,
        };
        let sane = |v: i32| (0..=MAX_INSET).contains(&v);
        let ok = frame.2 > frame.0
            && frame.3 > frame.1
            && [i.left, i.top, i.right, i.bottom].into_iter().all(sane);
        ok.then_some(i)
    }

    /// Расчётом, когда рамку DWM не узнать: `frame` — `SM_CXSIZEFRAME` + `SM_CXPADDEDBORDER` в
    /// DPI окна (столько tao отнимает у страницы слева, справа и снизу), из них видна полоска
    /// в 1 px; сверху невидимого края нет.
    pub fn estimated(frame: i32) -> Insets {
        let s = (frame - 1).max(0);
        Insets { left: s, top: 0, right: s, bottom: s }
    }
}

/// Вид окна перевода.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize)]
#[serde(rename_all = "lowercase")]
pub enum Mode {
    /// Панель перевода (у правого края или где её оставил пользователь).
    Full,
    /// Карточка «Обнаружена новая встреча» в правом верхнем углу рабочей области.
    Suggest,
    /// Полоска свёрнутого перевода (справа внизу или где её оставили), поверх всех.
    Mini,
}

/// Что показывает окно (`meeting_window_mode`, событие [`MODE_EVENT`]).
#[derive(Debug, Clone, PartialEq, Serialize)]
pub struct ModeInfo {
    pub mode: Mode,
    /// Предложение ядра (`meeting_suggest`, как пришло), пока на него не ответили и оно не
    /// истекло: в виде `suggest` — карточка, в полном виде — полоска над лентой.
    pub suggestion: Option<Value>,
    /// Через сколько мс предложение пропадёт само; `null` — предложения нет.
    pub hide_in_ms: Option<u64>,
}

/// Вид окна и текущее предложение. Переходы — чистые функции (тесты внизу), окно двигают
/// вызывающие.
#[derive(Debug)]
pub struct ModeState {
    mode: Mode,
    suggestion: Option<Value>,
    /// Поле `id` предложения — для `meeting_suggest_done`.
    suggestion_id: Option<String>,
    expires: Option<Instant>,
    /// Растёт с каждым предложением: таймер прежнего не трогает новое.
    generation: u64,
}

impl ModeState {
    pub const fn new() -> Self {
        Self {
            mode: Mode::Full,
            suggestion: None,
            suggestion_id: None,
            expires: None,
            generation: 0,
        }
    }

    /// Новое предложение. Панель уже видна (`full_visible`) — вид не меняется (полоска в
    /// панели), иначе окно становится карточкой. Возвращает номер предложения для таймера.
    pub fn suggest(&mut self, suggestion: Value, now: Instant, full_visible: bool) -> u64 {
        self.suggestion_id = suggestion.get("id").and_then(Value::as_str).map(str::to_owned);
        self.suggestion = Some(suggestion);
        self.expires = Some(now + SUGGEST_TTL);
        self.generation += 1;
        if !full_visible {
            self.mode = Mode::Suggest;
        }
        self.generation
    }

    /// Предложение `id` отвечено. true — оно и было текущим (тогда его больше нет).
    pub fn done(&mut self, id: &str) -> bool {
        if self.suggestion.is_none() || self.suggestion_id.as_deref() != Some(id) {
            return false;
        }
        self.clear();
        true
    }

    /// Таймер предложения `generation` истёк. true — оно было текущим и пропало.
    pub fn expire(&mut self, generation: u64) -> bool {
        if self.generation != generation || self.suggestion.is_none() {
            return false;
        }
        self.clear();
        true
    }

    /// Убрать предложение (ответ, истекло, окно скрыли).
    pub fn clear(&mut self) {
        self.suggestion = None;
        self.suggestion_id = None;
        self.expires = None;
    }

    /// Полный вид; true — окно было карточкой или полоской (место и размер панели надо
    /// поставить заново).
    pub fn switch_to_full(&mut self) -> bool {
        std::mem::replace(&mut self.mode, Mode::Full) != Mode::Full
    }

    /// Свернуть в полоску; true — окно ещё не было полоской.
    pub fn switch_to_mini(&mut self) -> bool {
        std::mem::replace(&mut self.mode, Mode::Mini) != Mode::Mini
    }

    pub fn mode(&self) -> Mode {
        self.mode
    }

    pub fn info(&self, now: Instant) -> ModeInfo {
        ModeInfo {
            mode: self.mode,
            suggestion: self.suggestion.clone(),
            hide_in_ms: self
                .suggestion
                .as_ref()
                .and(self.expires)
                .map(|t| t.saturating_duration_since(now).as_millis() as u64),
        }
    }
}

impl Default for ModeState {
    fn default() -> Self {
        Self::new()
    }
}

fn state() -> std::sync::MutexGuard<'static, ModeState> {
    MODE.lock().unwrap_or_else(|e| e.into_inner())
}

/// Команда `meeting_window_mode`: вид окна и предложение (страница спрашивает при загрузке).
pub fn mode_info() -> ModeInfo {
    PAGE_SEEN.store(true, Ordering::SeqCst);
    state().info(Instant::now())
}

/// Сообщить странице окна вид и предложение.
fn emit_mode(app: &AppHandle) {
    let info = state().info(Instant::now());
    if app.get_webview_window(LABEL).is_none() {
        return;
    }
    if let Err(e) = app.emit_to(LABEL, MODE_EVENT, info) {
        crate::log_warn!("Не удалось отправить {MODE_EVENT}: {e}");
    }
}

/// Показать окно перевода в полном виде (создать при первом открытии), без активации, поверх
/// всех — под пилюлей. Было карточкой или полоской — встаёт на место панели. Создание ждёт WebView2,
/// поэтому звать **не из главного потока** (команды — async, трей — [`request_show`]).
pub fn show_blocking(app: &AppHandle) -> Result<(), String> {
    let _guard = CREATING.lock().unwrap_or_else(|e| e.into_inner());
    let was_compact = state().switch_to_full();
    if let Some(win) = app.get_webview_window(LABEL) {
        let rect = was_compact.then(|| settings::load().meeting_rect);
        on_main(app, move |app| {
            if let Some(rect) = rect {
                apply_full(app, &win, rect);
            }
            show_existing(&win);
            emit_mode(app);
        })?;
        return Ok(());
    }
    let win = build(app)?;
    let rect = settings::load().meeting_rect;
    on_main(app, move |app| {
        if let Some(hwnd) = setup_new(app, &win) {
            // SAFETY: главный поток — владелец окна; окно только что создано и живо.
            unsafe { place(hwnd, rect, logical_width(app)) };
        }
        show_existing(&win);
    })?;
    crate::log_info!("Окно перевода встречи создано");
    Ok(())
}

/// Команда `meeting_suggest_show {suggestion}`: ядро заметило встречу (событие
/// `meeting_suggest`, его приносит главное окно). Панель видна — предложение покажет она
/// (полоской), иначе окно становится карточкой в правом верхнем углу рабочей области и
/// показывается без фокуса. Через [`SUGGEST_TTL`] без ответа — пропадает. Не из главного
/// потока (может создать окно).
pub fn suggest_blocking(app: &AppHandle, suggestion: Value) -> Result<(), String> {
    if !suggestion.get("id").is_some_and(Value::is_string) {
        return Err("meeting_suggest_show: suggestion — объект meeting_suggest с полем id".into());
    }
    let _guard = CREATING.lock().unwrap_or_else(|e| e.into_inner());
    let existing = app.get_webview_window(LABEL);
    let visible = existing.as_ref().is_some_and(|w| w.is_visible().unwrap_or(false));
    let generation = {
        let mut st = state();
        let full_visible = visible && st.mode() == Mode::Full;
        st.suggest(suggestion, Instant::now(), full_visible)
    };
    schedule_expiry(app, generation);
    if state().mode() == Mode::Full {
        // панель уже на экране — предложение покажет она сама
        emit_mode(app);
        return Ok(());
    }
    let created = existing.is_none();
    let win = match existing {
        Some(w) => w,
        None => build(app)?,
    };
    // страница уже живая — сначала новый вид (не мелькнёт старое содержимое), потом показ
    if !created {
        emit_mode(app);
    }
    let w = win.clone();
    on_main(app, move |app| {
        let hwnd = if created { setup_new(app, &w) } else { w.hwnd().ok().map(|h| h.0 as HWND) };
        apply_suggest(&w, hwnd);
    })?;
    if created {
        wait_page();
    } else {
        std::thread::sleep(Duration::from_millis(60));
    }
    on_main(app, move |app| {
        // пока ждали страницу, на предложение могли ответить (или истекло)
        let still = {
            let st = state();
            st.mode() == Mode::Suggest && st.info(Instant::now()).suggestion.is_some()
        };
        if still {
            show_existing(&win);
        }
        emit_mode(app);
    })?;
    crate::log_info!("Предложение встречи показано");
    Ok(())
}

/// Через [`SUGGEST_TTL`] — убрать предложение `generation`, если оно ещё висит (карточку —
/// скрыть).
fn schedule_expiry(app: &AppHandle, generation: u64) {
    let app = app.clone();
    std::thread::spawn(move || {
        std::thread::sleep(SUGGEST_TTL);
        let hide_card = {
            let mut st = state();
            st.expire(generation) && st.mode() == Mode::Suggest
        };
        if hide_card {
            if let Err(e) = hide_window(&app) {
                crate::log_warn!("Не удалось скрыть карточку встречи: {e}");
            }
        }
        emit_mode(&app);
    });
}

/// Ждать, пока только что созданная страница спросит вид, — не дольше [`PAGE_WAIT`].
fn wait_page() {
    let until = Instant::now() + PAGE_WAIT;
    while !PAGE_SEEN.load(Ordering::SeqCst) && Instant::now() < until {
        std::thread::sleep(Duration::from_millis(25));
    }
    // и ещё кадр-другой на отрисовку
    std::thread::sleep(Duration::from_millis(80));
}

/// Команда `meeting_suggest_done {id}`: на предложение ответили («Не сейчас», «Не предлагать»)
/// или его закрыли. Окно-карточка скрывается, панель остаётся (полоска пропадает). Ответ на
/// прежнее предложение, когда карточка уже показывает новое, новое не трогает.
pub fn suggest_done(app: &AppHandle, id: &str) -> Result<(), String> {
    let hide_card = {
        let mut st = state();
        st.done(id);
        st.mode() == Mode::Suggest && st.info(Instant::now()).suggestion.is_none()
    };
    if hide_card {
        hide_window(app)?;
    }
    emit_mode(app);
    Ok(())
}

/// Команда `meeting_window_minimize`: окно — полоской (страница сначала узнаёт вид — не
/// мелькнёт панель в маленьком окне), справа внизу рабочей области или где полоску оставили
/// (`meeting_mini_pos`), поверх всех, без фокуса. Окна нет — ничего. Не из главного потока.
pub fn minimize_blocking(app: &AppHandle) -> Result<(), String> {
    let _guard = CREATING.lock().unwrap_or_else(|e| e.into_inner());
    let Some(win) = app.get_webview_window(LABEL) else { return Ok(()) };
    state().switch_to_mini();
    emit_mode(app);
    std::thread::sleep(Duration::from_millis(60));
    let pos = settings::load().meeting_mini_pos;
    on_main(app, move |app| {
        apply_mini(&win, pos);
        show_existing(&win);
        emit_mode(app);
    })?;
    crate::log_info!("Окно перевода встречи свёрнуто в полоску");
    Ok(())
}

/// [`show_blocking`] в фоне — для главного потока (меню трея).
pub fn request_show(app: &AppHandle) {
    let app = app.clone();
    tauri::async_runtime::spawn_blocking(move || {
        if let Err(e) = show_blocking(&app) {
            crate::log_error!("Не удалось открыть окно перевода встречи: {e}");
        }
    });
}

/// Скрыть окно (сеанс перевода при этом идёт дальше). Была карточка — предложение снимается.
/// Окна нет — ничего.
pub fn hide(app: &AppHandle) -> Result<(), String> {
    let was_card = {
        let mut st = state();
        let card = st.mode() == Mode::Suggest && st.info(Instant::now()).suggestion.is_some();
        if card {
            st.clear();
        }
        card
    };
    hide_window(app)?;
    if was_card {
        emit_mode(app);
    }
    Ok(())
}

fn hide_window(app: &AppHandle) -> Result<(), String> {
    match app.get_webview_window(LABEL) {
        Some(w) => w.hide().map_err(|e| e.to_string()),
        None => Ok(()),
    }
}

/// Видна панель — скрыть, иначе (скрыта или видна карточка) — показать панель. Возвращает,
/// видно ли окно теперь. Не из главного потока (см. [`show_blocking`]).
pub fn toggle_blocking(app: &AppHandle) -> Result<bool, String> {
    if full_visible(app) {
        hide(app)?;
        return Ok(false);
    }
    show_blocking(app)?;
    Ok(true)
}

/// Пункт трея «Перевод встречи»: показать панель или скрыть её. Скрыть можно сразу, показ (и,
/// может быть, создание) — в фоне.
pub fn tray_toggle(app: &AppHandle) {
    if full_visible(app) {
        if let Err(e) = hide(app) {
            crate::log_warn!("Не удалось скрыть окно перевода: {e}");
        }
        return;
    }
    request_show(app);
}

/// Окно есть, видно и в полном виде. (Замок вида не держим, пока спрашиваем окно: из
/// фонового потока это ожидание главного, а он тоже берёт замок — `rescue`.)
fn full_visible(app: &AppHandle) -> bool {
    let full = state().mode() == Mode::Full;
    full && app.get_webview_window(LABEL).is_some_and(|w| w.is_visible().unwrap_or(false))
}

/// Команда `meeting_window_reset`: снова к правому краю рабочей области, на всю высоту.
/// (`meeting_rect: null` в настройках ядра сохраняет фронтенд.) Окна нет или оно сейчас
/// карточка или полоска — ничего: панель и так встанет к краю, когда её покажут.
pub fn reset(app: &AppHandle) -> Result<(), String> {
    let Some(win) = app.get_webview_window(LABEL) else { return Ok(()) };
    if state().mode() != Mode::Full {
        return Ok(());
    }
    on_main(app, move |app| match win.hwnd() {
        // SAFETY: главный поток — владелец окна.
        Ok(h) => unsafe { place(h.0 as HWND, None, logical_width(app)) },
        Err(e) => crate::log_warn!("Нет HWND окна перевода: {e}"),
    })
}

fn build(app: &AppHandle) -> Result<WebviewWindow, String> {
    let cfg = window_config(app).cloned().ok_or("В tauri.conf.json нет окна meeting")?;
    let s = settings::load();
    let _ = APP.set(app.clone());
    PAGE_SEEN.store(false, Ordering::SeqCst);
    WebviewWindowBuilder::from_config(app, &cfg)
        .map_err(|e| e.to_string())?
        .title(crate::i18n::t().meeting_title)
        .content_protected(s.meeting_protect)
        .build()
        .map_err(|e| crate::i18n::fill(crate::i18n::t().meeting_window_failed, &[("error", &e)]))
}

/// Только что созданное окно (в главном потоке): «без фокуса», подъём, надзор за WebView2.
fn setup_new(app: &AppHandle, win: &WebviewWindow) -> Option<HWND> {
    let hwnd = match win.hwnd() {
        Ok(h) => h.0 as HWND,
        Err(e) => {
            crate::log_warn!("Нет HWND окна перевода: {e}");
            return None;
        }
    };
    // SAFETY: главный поток — владелец окна; окно только что создано и живо.
    unsafe { overlay::install(hwnd, Overlay::Meeting) };
    overlay::watch_webview(app, win);
    Some(hwnd)
}

/// Окно — карточкой (в главном потоке): без изменения размера за края и без наименьшего
/// размера панели, в правый верхний угол рабочей области монитора активного окна.
fn apply_suggest(win: &WebviewWindow, hwnd: Option<HWND>) {
    if let Err(e) = win.set_resizable(false) {
        crate::log_warn!("Окно перевода: set_resizable(false): {e}");
    }
    if let Err(e) = win.set_min_size(None::<Size>) {
        crate::log_warn!("Окно перевода: set_min_size(None): {e}");
    }
    if let Some(hwnd) = hwnd {
        // SAFETY: главный поток — владелец окна.
        unsafe { place_suggest(hwnd, GetForegroundWindow()) };
    }
}

/// Окно — полоской (в главном потоке): без изменения размера за края и без наименьшего
/// размера панели; место — `pos` (видимый левый верхний угол) или справа внизу.
fn apply_mini(win: &WebviewWindow, pos: Option<(i32, i32)>) {
    if let Err(e) = win.set_resizable(false) {
        crate::log_warn!("Окно перевода: set_resizable(false): {e}");
    }
    if let Err(e) = win.set_min_size(None::<Size>) {
        crate::log_warn!("Окно перевода: set_min_size(None): {e}");
    }
    match win.hwnd() {
        // SAFETY: главный поток — владелец окна.
        Ok(hwnd) => unsafe { place_mini(hwnd.0 as HWND, pos) },
        Err(e) => crate::log_warn!("Нет HWND окна перевода: {e}"),
    }
}

/// Окно — снова панелью (в главном потоке): размер за края, наименьший размер из
/// tauri.conf.json, место — `rect` (или у правого края).
fn apply_full(app: &AppHandle, win: &WebviewWindow, rect: Option<MeetingRect>) {
    if let Err(e) = win.set_resizable(true) {
        crate::log_warn!("Окно перевода: set_resizable(true): {e}");
    }
    let (w, h) = min_size(app);
    if let Err(e) = win.set_min_size(Some(Size::Logical(LogicalSize::new(w, h)))) {
        crate::log_warn!("Окно перевода: set_min_size: {e}");
    }
    match win.hwnd() {
        // SAFETY: главный поток — владелец окна.
        Ok(hwnd) => unsafe { place(hwnd.0 as HWND, rect, logical_width(app)) },
        Err(e) => crate::log_warn!("Нет HWND окна перевода: {e}"),
    }
}

/// Выполнить в главном потоке и дождаться (звать не из главного потока).
fn on_main(app: &AppHandle, f: impl FnOnce(&AppHandle) + Send + 'static) -> Result<(), String> {
    let (tx, rx) = mpsc::channel();
    let handle = app.clone();
    app.run_on_main_thread(move || {
        f(&handle);
        let _ = tx.send(());
    })
    .map_err(|e| e.to_string())?;
    rx.recv_timeout(MAIN_THREAD_WAIT).map_err(|e| e.to_string())
}

/// Показать уже созданное окно и поднять (в главном потоке: подъём — после показа) — вместе
/// с пилюлей, чтобы она осталась над ним.
fn show_existing(win: &WebviewWindow) {
    if let Err(e) = win.show() {
        crate::log_warn!("Не удалось показать окно перевода: {e}");
    }
    overlay::raise_all();
}

fn window_config(app: &AppHandle) -> Option<&tauri::utils::config::WindowConfig> {
    app.config().app.windows.iter().find(|w| w.label == LABEL)
}

/// Видимая ширина пристыкованного окна в логических px — из tauri.conf.json.
fn logical_width(app: &AppHandle) -> f64 {
    window_config(app).map(|w| w.width).filter(|w| *w >= 200.0).unwrap_or(DOCK_WIDTH)
}

/// Наименьший размер панели (логические px) — из tauri.conf.json.
fn min_size(app: &AppHandle) -> (f64, f64) {
    let cfg = window_config(app);
    (
        cfg.and_then(|w| w.min_width).unwrap_or(MIN_SIZE.0),
        cfg.and_then(|w| w.min_height).unwrap_or(MIN_SIZE.1),
    )
}

// ——— место окна (Win32, физические пиксели) ———
//
// Все места ниже — видимые прямоугольники; в прямоугольник окна их переводят невидимые края
// ([`Insets`]) в [`set_rect`].

/// Пристыковать к правому краю рабочей области `work`: видимая ширина `logical_w` в масштабе
/// монитора (`scale` = DPI / 96), не шире рабочей области, высота — вся рабочая область.
/// Возвращает видимый (x, y, w, h).
pub fn dock_right(work: Ltrb, scale: f64, logical_w: f64) -> Xywh {
    let (l, t, r, b) = work;
    let w = ((logical_w * scale).round() as i32).min(r - l).max(1);
    (r - w, t, w, (b - t).max(1))
}

/// Карточка предложения — в правом верхнем углу рабочей области `work` с отступом
/// [`SUGGEST_MARGIN`], видимый размер [`SUGGEST_SIZE`] в масштабе монитора (не больше рабочей
/// области).
pub fn suggest_rect(work: Ltrb, scale: f64) -> Xywh {
    let (l, t, r, b) = work;
    let m = (SUGGEST_MARGIN * scale).round() as i32;
    let w = ((SUGGEST_SIZE.0 * scale).round() as i32).min(r - l - 2 * m).max(1);
    let h = ((SUGGEST_SIZE.1 * scale).round() as i32).min(b - t - 2 * m).max(1);
    (r - m - w, t + m, w, h)
}

/// Видимый размер полоски в масштабе монитора (не больше рабочей области за вычетом отступов).
pub fn mini_size(work: Ltrb, scale: f64) -> (i32, i32) {
    let (l, t, r, b) = work;
    let m = (MINI_MARGIN * scale).round() as i32;
    let w = ((MINI_SIZE.0 * scale).round() as i32).min(r - l - 2 * m).max(1);
    let h = ((MINI_SIZE.1 * scale).round() as i32).min(b - t - 2 * m).max(1);
    (w, h)
}

/// Полоска по умолчанию — в правом нижнем углу рабочей области `work` с отступом
/// [`MINI_MARGIN`], видимый размер [`MINI_SIZE`] в масштабе монитора.
pub fn mini_rect(work: Ltrb, scale: f64) -> Xywh {
    let (_, _, r, b) = work;
    let m = (MINI_MARGIN * scale).round() as i32;
    let (w, h) = mini_size(work, scale);
    (r - m - w, b - m - h, w, h)
}

/// Полоска на сохранённом месте `pos` (видимый левый верхний угол) — в пределах рабочей
/// области `work` её монитора.
pub fn mini_at(pos: (i32, i32), work: Ltrb, scale: f64) -> Xywh {
    let (w, h) = mini_size(work, scale);
    fit_rect((pos.0, pos.1, w, h), work)
}

/// Видимый прямоугольник — целиком в рабочую область `work`: не больше её, сдвинут внутрь.
pub fn fit_rect((x, y, w, h): Xywh, work: Ltrb) -> Xywh {
    let (l, t, r, b) = work;
    let w = w.min(r - l).max(1);
    let h = h.min(b - t).max(1);
    (x.clamp(l, r - w), y.clamp(t, b - h), w, h)
}

/// Центр прямоугольника.
fn center((x, y, w, h): Xywh) -> POINT {
    POINT { x: x + w / 2, y: y + h / 2 }
}

/// Невидимые края окна сейчас (в его масштабе): рамка DWM (известна и у скрытого окна),
/// иначе — расчётом ([`Insets::estimated`]).
unsafe fn insets(hwnd: HWND) -> Insets {
    let mut wr: RECT = std::mem::zeroed();
    let mut fr: RECT = std::mem::zeroed();
    if GetWindowRect(hwnd, &mut wr) != 0
        && DwmGetWindowAttribute(
            hwnd,
            DWMWA_EXTENDED_FRAME_BOUNDS as u32,
            (&mut fr as *mut RECT).cast(),
            std::mem::size_of::<RECT>() as u32,
        ) == 0
    {
        let ltrb = |r: &RECT| (r.left, r.top, r.right, r.bottom);
        if let Some(i) = Insets::measured(ltrb(&wr), ltrb(&fr)) {
            return i;
        }
    }
    let dpi = match GetDpiForWindow(hwnd) {
        0 => 96,
        d => d,
    };
    Insets::estimated(
        GetSystemMetricsForDpi(SM_CXSIZEFRAME, dpi)
            + GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi),
    )
}

/// Рабочая область монитора и его масштаб (DPI / 96).
unsafe fn monitor_work(mon: HMONITOR) -> Option<(Ltrb, f64)> {
    let mut mi: MONITORINFO = std::mem::zeroed();
    mi.cbSize = std::mem::size_of::<MONITORINFO>() as u32;
    if GetMonitorInfoW(mon, &mut mi) == 0 {
        return None;
    }
    let (mut dx, mut dy) = (0u32, 0u32);
    let dpi = if GetDpiForMonitor(mon, MDT_EFFECTIVE_DPI, &mut dx, &mut dy) == 0 && dx > 0 {
        dx
    } else {
        96
    };
    let w = &mi.rcWork;
    Some(((w.left, w.top, w.right, w.bottom), dpi as f64 / 96.0))
}

/// Поставить окно: `saved` (прямоугольник окна, как его сохранил [`on_user_moved`]) на
/// каком-нибудь мониторе — туда (видимой частью в пределах его рабочей области: окно,
/// придвинутое к краю вплотную, так и остаётся), иначе — к правому краю монитора активного
/// окна (встреча идёт в браузере, панель — рядом).
unsafe fn place(hwnd: HWND, saved: Option<MeetingRect>, logical_w: f64) {
    if let Some(r) = saved {
        let vis = insets(hwnd).visible((r.x, r.y, r.w, r.h));
        let mon = MonitorFromPoint(center(vis), MONITOR_DEFAULTTONULL);
        if !mon.is_null() {
            if let Some((work, _)) = monitor_work(mon) {
                set_rect(hwnd, fit_rect(vis, work));
                return;
            }
        }
        crate::log_info!("Сохранённое место окна перевода вне экранов — ставлю к правому краю");
    }
    let mon = MonitorFromWindow(GetForegroundWindow(), MONITOR_DEFAULTTOPRIMARY);
    match monitor_work(mon) {
        Some((work, scale)) => set_rect(hwnd, dock_right(work, scale, logical_w)),
        None => crate::log_warn!("Не удалось определить монитор для окна перевода"),
    }
}

/// Карточку — в правый верхний угол монитора окна `near` (активного окна: встреча там).
unsafe fn place_suggest(hwnd: HWND, near: HWND) {
    let mon = MonitorFromWindow(near, MONITOR_DEFAULTTOPRIMARY);
    match monitor_work(mon) {
        Some((work, scale)) => set_rect(hwnd, suggest_rect(work, scale)),
        None => crate::log_warn!("Не удалось определить монитор для карточки встречи"),
    }
}

/// Полоску — на сохранённое место `pos` (на каком-нибудь мониторе: в пределах его рабочей
/// области), иначе — в правый нижний угол монитора активного окна.
unsafe fn place_mini(hwnd: HWND, pos: Option<(i32, i32)>) {
    if let Some((x, y)) = pos {
        let mon = MonitorFromPoint(POINT { x: x + 1, y: y + 1 }, MONITOR_DEFAULTTONULL);
        if !mon.is_null() {
            if let Some((work, scale)) = monitor_work(mon) {
                set_rect(hwnd, mini_at((x, y), work, scale));
                return;
            }
        }
        crate::log_info!("Сохранённое место полоски перевода вне экранов — ставлю справа внизу");
    }
    let mon = MonitorFromWindow(GetForegroundWindow(), MONITOR_DEFAULTTOPRIMARY);
    match monitor_work(mon) {
        Some((work, scale)) => set_rect(hwnd, mini_rect(work, scale)),
        None => crate::log_warn!("Не удалось определить монитор для полоски перевода"),
    }
}

/// Поставить окно так, чтобы его видимая часть заняла `target` (физические пиксели): к нему
/// добавляются невидимые края ([`Insets`]). Переезд на монитор с другим масштабом tao
/// доделывает по `WM_DPICHANGED` (пересчитывает размер) — тогда ставим ещё раз, с краями уже
/// в новом масштабе. Порядок окон не меняется (`SWP_NOZORDER`): поверх всех окно держит
/// `alwaysOnTop`, а пилюлю над ним — `overlay::raise_all`.
unsafe fn set_rect(hwnd: HWND, target: Xywh) {
    let flags = SWP_NOACTIVATE | SWP_NOOWNERZORDER | SWP_NOZORDER;
    for _ in 0..2 {
        let dpi = GetDpiForWindow(hwnd);
        let (x, y, w, h) = insets(hwnd).outer(target);
        SetWindowPos(hwnd, std::ptr::null_mut(), x, y, w, h, flags);
        if GetDpiForWindow(hwnd) == dpi {
            break;
        }
    }
}

/// Мониторы, разрешение, масштаб или рабочая область поменялись (`overlay`, `WM_RESCUE`):
/// карточка — снова в правый верхний угол своего монитора; полоска — в рабочую область
/// монитора под ней (размер — в его масштабе), а вне всех мониторов — справа внизу
/// основного; пристыкованная панель
/// (`meeting_rect: null`) — снова к правому краю своего монитора (ширина — в его масштабе,
/// высота — вся рабочая область), перенесённая — в пределы рабочей области монитора под её
/// центром, а вне всех мониторов — к правому краю основного.
///
/// # Safety
/// `hwnd` — окно перевода, вызов — в его потоке.
pub(crate) unsafe fn rescue(hwnd: HWND) {
    let mut r: RECT = std::mem::zeroed();
    if GetWindowRect(hwnd, &mut r) == 0 {
        return;
    }
    let cur = (r.left, r.top, r.right - r.left, r.bottom - r.top);
    let ins = insets(hwnd);
    let vis = ins.visible(cur);
    let here = MonitorFromPoint(center(vis), MONITOR_DEFAULTTONULL);
    let (mon, off_screen) = if here.is_null() {
        (MonitorFromPoint(POINT { x: 0, y: 0 }, MONITOR_DEFAULTTOPRIMARY), true)
    } else {
        (here, false)
    };
    let Some((work, scale)) = monitor_work(mon) else { return };
    let mode = state().mode();
    let target = if mode == Mode::Suggest {
        suggest_rect(work, scale)
    } else if mode == Mode::Mini {
        if off_screen {
            mini_rect(work, scale)
        } else {
            mini_at((vis.0, vis.1), work, scale)
        }
    } else {
        let docked = settings::load().meeting_rect.is_none();
        let logical_w = APP.get().map_or(DOCK_WIDTH, logical_width);
        if docked || off_screen {
            dock_right(work, scale, logical_w)
        } else {
            fit_rect(vis, work)
        }
    };
    // окно против окна (а не видимое против видимого): то, что уже стоит, не двигается снова
    if ins.outer(target) != cur {
        set_rect(hwnd, target);
        crate::log_info!(
            "Окно перевода встречи поставлено заново после смены мониторов или рабочей области"
        );
    }
    overlay::raise_all();
}

/// Прямоугольник окна `[x, y, w, h]`.
unsafe fn window_rect(hwnd: HWND) -> Option<[i32; 4]> {
    let mut r: RECT = std::mem::zeroed();
    (GetWindowRect(hwnd, &mut r) != 0).then(|| [r.left, r.top, r.right - r.left, r.bottom - r.top])
}

/// Пользователь начал перетаскивать окно или менять размер (`overlay`, `WM_ENTERSIZEMOVE`).
///
/// # Safety
/// `hwnd` — окно перевода.
pub(crate) unsafe fn on_move_start(hwnd: HWND) {
    *MOVE_START.lock().unwrap_or_else(|e| e.into_inner()) = window_rect(hwnd);
}

/// Сохранять ли прямоугольник после перетаскивания: окно сдвинулось или поменяло размер и не
/// меньше [`MEETING_RECT_MIN`].
pub fn moved(start: Option<[i32; 4]>, end: [i32; 4]) -> bool {
    start != Some(end) && end[2] >= MEETING_RECT_MIN && end[3] >= MEETING_RECT_MIN
}

/// Сохранять ли место полоски после перетаскивания: она сдвинулась (размер у неё свой).
pub fn mini_moved(start: Option<[i32; 4]>, end: [i32; 4]) -> bool {
    !matches!(start, Some(s) if s[0] == end[0] && s[1] == end[1])
}

/// Пользователь закончил перетаскивать окно или менять размер (`overlay`, `WM_EXITSIZEMOVE`):
/// пилюля — снова над окном (перенос мог поднять его), панель сдвинулась — сообщить странице
/// окна новый прямоугольник окна, она сохранит `meeting_rect`; полоска сдвинулась — левый
/// верхний угол её видимой части ([`MINI_POS_EVENT`], `meeting_mini_pos`). Карточка место
/// панели не меняет.
///
/// # Safety
/// `hwnd` — окно перевода.
pub(crate) unsafe fn on_user_moved(hwnd: HWND) {
    let start = MOVE_START.lock().unwrap_or_else(|e| e.into_inner()).take();
    overlay::raise_all();
    let mode = state().mode();
    if IsWindowVisible(hwnd) == 0 || mode == Mode::Suggest {
        return;
    }
    let Some(rect) = window_rect(hwnd) else { return };
    if mode == Mode::Mini {
        if mini_moved(start, rect) {
            let (x, y, _, _) = insets(hwnd).visible((rect[0], rect[1], rect[2], rect[3]));
            if let Some(app) = APP.get() {
                if let Err(e) = app.emit_to(LABEL, MINI_POS_EVENT, [x, y]) {
                    crate::log_warn!("Не удалось отправить {MINI_POS_EVENT}: {e}");
                }
            }
        }
        return;
    }
    if !moved(start, rect) {
        return;
    }
    if let Some(app) = APP.get() {
        if let Err(e) = app.emit_to(LABEL, RECT_EVENT, rect) {
            crate::log_warn!("Не удалось отправить {RECT_EVENT}: {e}");
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn docks_to_the_right_edge() {
        // 1920×1040 (панель задач снизу), 100 %
        assert_eq!(dock_right((0, 0, 1920, 1040), 1.0, 420.0), (1500, 0, 420, 1040));
        // 150 %: 2560×1392 → ширина 630
        assert_eq!(dock_right((0, 0, 2560, 1392), 1.5, 420.0), (1930, 0, 630, 1392));
        // монитор слева, панель задач сверху
        assert_eq!(dock_right((-1920, 48, 0, 1080), 1.0, 420.0), (-420, 48, 420, 1032));
        // узкий экран — не шире рабочей области
        assert_eq!(dock_right((0, 0, 400, 700), 1.25, 420.0), (0, 0, 400, 700));
    }

    /// Карточка — в правом верхнем углу рабочей области, с отступом, в масштабе монитора.
    #[test]
    fn suggestion_card_sits_top_right() {
        // 100 %: 372×176, отступ 12
        assert_eq!(suggest_rect((0, 0, 1920, 1040), 1.0), (1536, 12, 372, 176));
        // 150 %: 558×264, отступ 18
        assert_eq!(suggest_rect((0, 0, 2560, 1392), 1.5), (1984, 18, 558, 264));
        // панель задач сверху, монитор слева
        assert_eq!(suggest_rect((-1920, 48, 0, 1080), 1.0), (-384, 60, 372, 176));
        // крошечная рабочая область — не больше неё
        assert_eq!(suggest_rect((0, 0, 300, 150), 1.0), (12, 12, 276, 126));
    }

    /// Щелчок по шапке без переноса (прямоугольник тот же) окно не «отстыковывает».
    #[test]
    fn only_real_moves_are_saved() {
        let r = [1500, 0, 420, 1040];
        assert!(!moved(Some(r), r));
        assert!(moved(Some(r), [1400, 0, 420, 1040]));
        assert!(moved(Some(r), [1500, 0, 520, 1040]));
        // начала не видели (сообщение пропало) — сохраняем
        assert!(moved(None, r));
        // окно меньше допустимого ядром — не сохраняем
        assert!(!moved(Some(r), [1500, 0, 150, 1040]));
    }

    #[test]
    fn saved_rect_fits_the_work_area() {
        let r = |x, y, w, h| (x, y, w, h);
        let work = (0, 0, 1920, 1040);
        // целиком внутри — как есть
        assert_eq!(fit_rect(r(1400, 20, 480, 900), work), (1400, 20, 480, 900));
        // вылез за правый и нижний край — сдвинут внутрь
        assert_eq!(fit_rect(r(1700, 500, 480, 900), work), (1440, 140, 480, 900));
        // выше рабочей области (панель задач сверху) и левее монитора
        assert_eq!(fit_rect(r(-50, -30, 480, 600), (0, 48, 1920, 1080)), (0, 48, 480, 600));
        // больше рабочей области — по её размеру
        assert_eq!(fit_rect(r(100, 0, 3000, 2000), work), (0, 0, 1920, 1040));
        // монитор с отрицательными координатами
        assert_eq!(fit_rect(r(-500, 0, 480, 1040), (-1920, 0, 0, 1032)), (-500, 0, 480, 1032));
    }

    /// Невидимые края окна без рамки с тенью: SM_CXSIZEFRAME + SM_CXPADDEDBORDER (Windows 11:
    /// 4+4 при 100 %, 4+5 при 125 %, 5+6 при 150 %) минус видимая полоска в 1 px; сверху — 0.
    /// Рамка DWM, измеренная у такого окна при 125 % (окно 200,200–500,400), — те же края.
    #[test]
    fn invisible_frame_insets() {
        let at100 = Insets::estimated(8);
        let at125 = Insets::estimated(9);
        let at150 = Insets::estimated(11);
        assert_eq!(at100, Insets { left: 7, top: 0, right: 7, bottom: 7 });
        assert_eq!(at125, Insets { left: 8, top: 0, right: 8, bottom: 8 });
        assert_eq!(at150, Insets { left: 10, top: 0, right: 10, bottom: 10 });
        assert_eq!(Insets::estimated(0), Insets::default());
        assert_eq!(Insets::measured((200, 200, 500, 400), (208, 200, 492, 392)), Some(at125));
        // окно без невидимых краёв — нули
        assert_eq!(Insets::measured((0, 0, 100, 100), (0, 0, 100, 100)), Some(Insets::default()));
        // рамка не внутри окна, пустая или край неправдоподобно толстый — не верим
        assert_eq!(Insets::measured((0, 0, 100, 100), (-1, 0, 100, 100)), None);
        assert_eq!(Insets::measured((0, 0, 100, 100), (10, 10, 10, 50)), None);
        assert_eq!(Insets::measured((0, 0, 300, 300), (100, 0, 300, 300)), None);
        // туда и обратно — тот же прямоугольник
        for i in [at100, at125, at150] {
            assert_eq!(i.visible(i.outer((1500, 0, 420, 1040))), (1500, 0, 420, 1040));
        }
    }

    /// Пристыкованная панель и карточка — видимым краем вровень с рабочей областью (и с
    /// отступом от видимого края) при 100, 125 и 150 %: невидимые края — за ней.
    #[test]
    fn visible_edge_is_flush_with_the_work_area() {
        let edges = |(x, y, w, h): Xywh| (x, y, x + w, y + h);
        let cases = [
            ((0, 0, 1920, 1040), 1.0, Insets::estimated(8)),
            ((0, 0, 1920, 1032), 1.25, Insets::estimated(9)),
            ((0, 0, 2560, 1392), 1.5, Insets::estimated(11)),
            ((-1920, 48, 0, 1080), 1.25, Insets::estimated(9)),
        ];
        for (work, scale, ins) in cases {
            let (l, t, r, b) = work;
            let dock = dock_right(work, scale, 420.0);
            let (_, vt, vr, vb) = edges(dock);
            assert_eq!((vt, vr, vb), (t, r, b), "панель при {scale}");
            assert_eq!(dock.2, (420.0 * scale).round() as i32, "видимая ширина при {scale}");
            let (ox, oy, or, ob) = edges(ins.outer(dock));
            assert_eq!((ox, oy, or, ob), (r - dock.2 - ins.left, t, r + ins.right, b + ins.bottom));

            let card = suggest_rect(work, scale);
            let m = (SUGGEST_MARGIN * scale).round() as i32;
            let (_, ct, cr, _) = edges(card);
            assert_eq!((ct, cr), (t + m, r - m), "карточка при {scale}");
            let (cw, ch) =
                ((SUGGEST_SIZE.0 * scale).round() as i32, (SUGGEST_SIZE.1 * scale).round() as i32);
            assert_eq!((card.2, card.3), (cw, ch));
            assert!(l < card.0);
        }
        // числа целиком — 100 % и 150 %
        let i100 = Insets::estimated(8);
        assert_eq!(i100.outer(dock_right((0, 0, 1920, 1040), 1.0, 420.0)), (1493, 0, 434, 1047));
        assert_eq!(i100.outer(suggest_rect((0, 0, 1920, 1040), 1.0)), (1529, 12, 386, 183));
        let i150 = Insets::estimated(11);
        assert_eq!(i150.outer(dock_right((0, 0, 2560, 1392), 1.5, 420.0)), (1920, 0, 650, 1402));
    }

    /// Панель, придвинутую вплотную к краю (её невидимые края — за рабочей областью), при
    /// показе и после смены мониторов внутрь не сдвигает; вылезшую видимой частью — сдвигает.
    #[test]
    fn saved_window_flush_to_the_edge_stays() {
        let work = (0, 0, 1920, 1040);
        let ins = Insets::estimated(8);
        let flush = ins.outer((1500, 100, 420, 800)); // окно (1493, 100, 434, 807)
        assert_eq!(ins.outer(fit_rect(ins.visible(flush), work)), flush);
        let bottom = ins.outer((200, 240, 420, 800)); // видимый низ — 1040
        assert_eq!(ins.outer(fit_rect(ins.visible(bottom), work)), bottom);
        let out = ins.outer((1600, 100, 420, 800));
        assert_eq!(fit_rect(ins.visible(out), work), (1500, 100, 420, 800));
    }

    /// Рамка DWM у настоящего (ни разу не показанного) окна с рамкой для изменения размера —
    /// те же невидимые края, что и расчёт; у окна без рамки — нули. Как стоит окно, так и
    /// ставит его `set_rect`: видимая часть — ровно заданный прямоугольник.
    #[test]
    fn insets_are_measured_on_a_real_window() {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            CreateWindowExW, DestroyWindow, WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW,
            WS_OVERLAPPEDWINDOW, WS_POPUP,
        };
        let class: Vec<u16> = "STATIC\0".encode_utf16().collect();
        // SAFETY: окна этого потока, создаются и уничтожаются здесь же; не показываются.
        unsafe {
            let make = |style| {
                let h = CreateWindowExW(
                    WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                    class.as_ptr(),
                    class.as_ptr(),
                    style,
                    -32000,
                    -32000,
                    300,
                    200,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null(),
                );
                assert!(!h.is_null(), "окно для проверки не создалось");
                h
            };
            let framed = make(WS_OVERLAPPEDWINDOW);
            let dpi = GetDpiForWindow(framed);
            let frame = GetSystemMetricsForDpi(SM_CXSIZEFRAME, dpi)
                + GetSystemMetricsForDpi(SM_CXPADDEDBORDER, dpi);
            let ins = insets(framed);
            assert_eq!(ins, Insets::estimated(frame), "DPI {dpi}");
            assert!(ins.left > 0);
            set_rect(framed, (-31000, -31000, 420, 300));
            let mut fr: RECT = std::mem::zeroed();
            DwmGetWindowAttribute(
                framed,
                DWMWA_EXTENDED_FRAME_BOUNDS as u32,
                (&mut fr as *mut RECT).cast(),
                std::mem::size_of::<RECT>() as u32,
            );
            assert_eq!((fr.left, fr.top, fr.right, fr.bottom), (-31000, -31000, -30580, -30700));

            let bare = make(WS_POPUP);
            assert_eq!(insets(bare), Insets::default());
            DestroyWindow(bare);
            DestroyWindow(framed);
        }
    }

    /// Пилюля над окном перевода: расстановка окна (`set_rect`) порядок не меняет, подъём при
    /// показе (`raise_all`) возвращает пилюлю наверх. Два настоящих окна поверх всех без
    /// активации — за пределами экранов.
    #[test]
    fn placing_and_showing_keep_the_pill_above() {
        use windows_sys::Win32::UI::WindowsAndMessaging::{
            CreateWindowExW, DestroyWindow, GetWindow, ShowWindow, GW_HWNDPREV, SW_SHOWNOACTIVATE,
            WS_EX_NOACTIVATE, WS_EX_TOOLWINDOW, WS_EX_TOPMOST, WS_POPUP,
        };
        let class: Vec<u16> = "STATIC\0".encode_utf16().collect();
        // SAFETY: окна этого потока, создаются и уничтожаются здесь же.
        unsafe {
            let make = || {
                let h = CreateWindowExW(
                    WS_EX_TOPMOST | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW,
                    class.as_ptr(),
                    class.as_ptr(),
                    WS_POPUP,
                    -32000,
                    -32000,
                    120,
                    80,
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null_mut(),
                    std::ptr::null(),
                );
                assert!(!h.is_null(), "окно для проверки не создалось");
                ShowWindow(h, SW_SHOWNOACTIVATE);
                h
            };
            let meeting = make();
            let pill = make();
            // a выше b в порядке окон?
            let above = |a: HWND, b: HWND| {
                let mut h = GetWindow(b, GW_HWNDPREV);
                while !h.is_null() {
                    if h == a {
                        return true;
                    }
                    h = GetWindow(h, GW_HWNDPREV);
                }
                false
            };
            overlay::set_hwnd_for_test(Overlay::Meeting, meeting);
            overlay::set_hwnd_for_test(Overlay::Pill, pill);
            overlay::raise_all();
            assert!(above(pill, meeting));
            set_rect(meeting, (-31000, -31000, 200, 150));
            assert!(above(pill, meeting), "set_rect поднял окно перевода над пилюлей");
            // проверка видит обратный порядок
            overlay::raise(Overlay::Meeting);
            assert!(above(meeting, pill));
            // как при показе (show_existing)
            overlay::raise_all();
            assert!(above(pill, meeting), "после показа пилюля не над окном перевода");
            overlay::set_hwnd_for_test(Overlay::Meeting, std::ptr::null_mut());
            overlay::set_hwnd_for_test(Overlay::Pill, std::ptr::null_mut());
            DestroyWindow(pill);
            DestroyWindow(meeting);
        }
    }

    /// Полоска — в правом нижнем углу рабочей области, с отступом, в масштабе монитора; на
    /// сохранённом месте — в пределах рабочей области.
    #[test]
    fn mini_bar_sits_bottom_right_or_where_left() {
        // 100 %: 340×64, отступ 12
        assert_eq!(mini_rect((0, 0, 1920, 1040), 1.0), (1568, 964, 340, 64));
        // 150 %: 510×96, отступ 18
        assert_eq!(mini_rect((0, 0, 2560, 1392), 1.5), (2032, 1278, 510, 96));
        // монитор слева, панель задач сверху
        assert_eq!(mini_rect((-1920, 48, 0, 1080), 1.0), (-352, 1004, 340, 64));
        // крошечная рабочая область — не больше неё
        assert_eq!(mini_rect((0, 0, 300, 70), 1.0), (12, 12, 276, 46));
        // сохранённое место — как есть, вылезшее — внутрь
        let work = (0, 0, 1920, 1040);
        assert_eq!(mini_at((100, 200), work, 1.0), (100, 200, 340, 64));
        assert_eq!(mini_at((1800, 1030), work, 1.0), (1580, 976, 340, 64));
        assert_eq!(mini_at((-50, -5), work, 1.25), (0, 0, 425, 80));
    }

    /// Место полоски сохраняется, только если она сдвинулась (щелчок по ней — нет).
    #[test]
    fn only_real_moves_of_the_bar_are_saved() {
        let r = [1561, 957, 354, 71];
        assert!(!mini_moved(Some(r), r));
        assert!(mini_moved(Some(r), [1400, 957, 354, 71]));
        assert!(mini_moved(Some(r), [1561, 900, 354, 71]));
        assert!(mini_moved(None, r));
    }

    /// «Свернуть» — полоска; развернуть — снова панель на своём месте (ставить заново).
    #[test]
    fn minimize_and_restore_switch_modes() {
        let now = Instant::now();
        let mut st = ModeState::new();
        assert!(st.switch_to_mini());
        assert!(!st.switch_to_mini());
        assert_eq!(st.mode(), Mode::Mini);
        let v = serde_json::to_value(st.info(now)).expect("json");
        assert_eq!(v["mode"], "mini");
        assert!(st.switch_to_full());
        assert!(!st.switch_to_full());
        // предложение при свёрнутом окне (сеанс кончился) — карточкой
        st.switch_to_mini();
        st.suggest(sug("s9"), now, false);
        assert_eq!(st.mode(), Mode::Suggest);
    }

    fn sug(id: &str) -> Value {
        json!({"id": id, "app": "Brave", "title": "Google Meet",
               "source": {"id": "proc:42"}, "detected_at": "2026-10-19T12:00:00.000"})
    }

    /// Скрытое окно становится карточкой; видимая панель остаётся панелью (полоска в ней).
    #[test]
    fn suggestion_turns_hidden_window_into_card() {
        let now = Instant::now();
        let mut st = ModeState::new();
        assert_eq!(st.info(now), ModeInfo { mode: Mode::Full, suggestion: None, hide_in_ms: None });

        st.suggest(sug("s1"), now, false);
        let info = st.info(now + Duration::from_secs(5));
        assert_eq!(info.mode, Mode::Suggest);
        assert_eq!(info.suggestion.as_ref().and_then(|v| v["id"].as_str()), Some("s1"));
        assert_eq!(info.hide_in_ms, Some(40_000));
        // «Начать расшифровку» → панель на прежнем месте (было карточкой — ставить заново)
        assert!(st.switch_to_full());
        assert!(!st.switch_to_full());

        let mut panel = ModeState::new();
        panel.suggest(sug("s2"), now, true);
        assert_eq!(panel.mode(), Mode::Full);
        assert!(panel.info(now).suggestion.is_some());
        assert!(!panel.switch_to_full());
    }

    /// Ответ снимает только своё предложение; таймер прежнего не трогает новое.
    #[test]
    fn answers_and_expiry_match_their_suggestion() {
        let now = Instant::now();
        let mut st = ModeState::new();
        let g1 = st.suggest(sug("s1"), now, false);
        assert!(!st.done("other"));
        assert!(st.info(now).suggestion.is_some());
        let g2 = st.suggest(sug("s2"), now, false);
        assert!(!st.expire(g1)); // таймер первого — мимо
        assert!(st.info(now).suggestion.is_some());
        assert!(st.done("s2"));
        assert!(!st.done("s2"));
        assert!(!st.expire(g2)); // уже отвечено
        assert_eq!(st.info(now).hide_in_ms, None);

        let g3 = st.suggest(sug("s3"), now, false);
        assert!(st.expire(g3));
        assert_eq!(st.info(now).suggestion, None);
        // предложения без id не бывает в контракте, но и с ним ничего не ломается
        st.suggest(json!({"app": "Zoom"}), now, false);
        assert!(!st.done(""));
        st.clear();
        assert_eq!(st.info(now).suggestion, None);
    }

    #[test]
    fn mode_info_serializes_for_the_page() {
        let now = Instant::now();
        let mut st = ModeState::new();
        st.suggest(sug("s1"), now, false);
        let v = serde_json::to_value(st.info(now)).expect("json");
        assert_eq!(v["mode"], "suggest");
        assert_eq!(v["suggestion"]["app"], "Brave");
        assert_eq!(v["hide_in_ms"], 45_000);
        st.clear();
        st.switch_to_full();
        let v = serde_json::to_value(st.info(now)).expect("json");
        assert_eq!(v, json!({"mode": "full", "suggestion": null, "hide_in_ms": null}));
    }
}
