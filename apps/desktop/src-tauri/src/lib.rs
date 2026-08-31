mod call_activity;
mod diagnostics;
use call_activity::{get_call_activity, shared_call_activity, start_call_activity_monitor};
mod host_prefs;
mod calendar;
mod location;
mod logs;
mod metadata;
mod paths;
mod reachability;
mod runtime;
mod services;
mod speaker_pair;
mod supervisor;

use location::get_device_location;

use diagnostics::{
    export_diagnostics, open_logs, ExportDiagnosticsRequest, ExportDiagnosticsResponse,
};
use host_prefs::{load_host_prefs, save_host_prefs, HostPrefs};
use paths::HostPaths;
use reachability::{
    disable_host_funnel, enable_host_funnel, enable_host_serve, probe_host_reachability,
    probe_speaker_reachability, EnableHostFunnelResult, EnableHostServeResult,
    HostReachabilityStatus, SpeakerReachability,
};
use runtime::RuntimeLayout;
use speaker_pair::{pair_speaker as pair_speaker_on_lan, PairSpeakerResult};
use supervisor::{
    restart_supervisor, set_managed_local_llm_enabled, shared_supervisor, start_supervisor,
    stop_supervisor, HostLaunchState, SharedSupervisor,
};
use tauri::{
    menu::{Menu, MenuItem},
    tray::{MouseButton, MouseButtonState, TrayIconBuilder, TrayIconEvent},
    AppHandle, Manager, RunEvent, State, WindowEvent,
};
use tauri_plugin_autostart::MacosLauncher;
use tauri_plugin_autostart::ManagerExt;
use tauri_plugin_updater::UpdaterExt;

#[tauri::command]
async fn get_launch_state(
    supervisor: State<'_, SharedSupervisor>,
) -> Result<HostLaunchState, String> {
    Ok(supervisor.lock().await.launch_state())
}

#[tauri::command]
async fn start_host(app: AppHandle, supervisor: State<'_, SharedSupervisor>) -> Result<(), String> {
    let supervisor = supervisor.inner().clone();
    tauri::async_runtime::spawn(async move {
        let _ = start_supervisor(supervisor, app).await;
    });
    Ok(())
}

#[tauri::command]
async fn restart_host(
    app: AppHandle,
    supervisor: State<'_, SharedSupervisor>,
) -> Result<(), String> {
    let supervisor = supervisor.inner().clone();
    tauri::async_runtime::spawn(async move {
        let _ = restart_supervisor(supervisor, app).await;
    });
    Ok(())
}

#[tauri::command]
async fn stop_host(supervisor: State<'_, SharedSupervisor>) -> Result<(), String> {
    stop_supervisor(supervisor.inner().clone()).await;
    Ok(())
}

#[tauri::command]
async fn export_diagnostics_bundle(
    app: AppHandle,
    supervisor: State<'_, SharedSupervisor>,
    request: ExportDiagnosticsRequest,
) -> Result<ExportDiagnosticsResponse, String> {
    export_diagnostics(app, supervisor.inner().clone(), request).await
}

#[tauri::command]
async fn open_logs_folder(app: AppHandle) -> Result<(), String> {
    open_logs(&app)
}

#[tauri::command]
fn open_external_url(url: String) -> Result<(), String> {
    let trimmed = url.trim();
    let allowed = trimmed.starts_with("https://")
        || trimmed.starts_with("http://")
        || trimmed.starts_with("mailto:")
        || trimmed.starts_with("tel:");
    if !allowed {
        return Err("URL scheme is not allowed.".to_string());
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(trimmed)
            .spawn()
            .map_err(|error| format!("Could not open URL: {error}"))?;
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        let _ = trimmed;
        Err("Opening external URLs is only supported on macOS.".to_string())
    }
}

#[tauri::command]
async fn get_host_status(
    supervisor: State<'_, SharedSupervisor>,
) -> Result<HostReachabilityStatus, String> {
    let port = supervisor.lock().await.launch_state().backend_port;
    Ok(probe_host_reachability(port).await)
}

#[tauri::command]
async fn check_speaker_reachability(node_id: String) -> Result<SpeakerReachability, String> {
    Ok(probe_speaker_reachability(&node_id).await)
}

#[tauri::command]
async fn pair_speaker(
    code: String,
    backend_url: Option<String>,
    node_id: Option<String>,
) -> Result<PairSpeakerResult, String> {
    Ok(pair_speaker_on_lan(code.trim(), backend_url.as_deref(), node_id.as_deref()).await)
}

#[tauri::command]
async fn enable_host_serve_cmd(
    supervisor: State<'_, SharedSupervisor>,
) -> Result<EnableHostServeResult, String> {
    let port = supervisor.lock().await.launch_state().backend_port;
    Ok(enable_host_serve(port).await)
}

#[tauri::command]
async fn enable_host_funnel_cmd(
    app: AppHandle,
    supervisor: State<'_, SharedSupervisor>,
) -> Result<EnableHostFunnelResult, String> {
    let port = supervisor.lock().await.launch_state().backend_port;
    let result = enable_host_funnel(port).await;
    if result.ok {
        let packaged = RuntimeLayout::detect(&app).is_packaged();
        let paths = HostPaths::for_mode(packaged);
        let mut prefs = load_host_prefs(&paths);
        prefs.external_triggers_enabled = true;
        save_host_prefs(&paths, &prefs)?;
    }
    Ok(result)
}

#[tauri::command]
async fn disable_host_funnel_cmd(
    app: AppHandle,
    supervisor: State<'_, SharedSupervisor>,
) -> Result<EnableHostFunnelResult, String> {
    let port = supervisor.lock().await.launch_state().backend_port;
    let result = disable_host_funnel(port).await;
    let packaged = RuntimeLayout::detect(&app).is_packaged();
    let paths = HostPaths::for_mode(packaged);
    let mut prefs = load_host_prefs(&paths);
    prefs.external_triggers_enabled = false;
    save_host_prefs(&paths, &prefs)?;
    Ok(result)
}

#[tauri::command]
fn get_host_prefs(app: AppHandle) -> Result<HostPrefs, String> {
    let packaged = RuntimeLayout::detect(&app).is_packaged();
    let mut prefs = load_host_prefs(&HostPaths::for_mode(packaged));
    prefs.launch_at_login = app
        .autolaunch()
        .is_enabled()
        .map_err(|error| format!("Could not read launch-at-login state: {error}"))?;
    Ok(prefs)
}

#[tauri::command]
fn set_host_prefs(app: AppHandle, prefs: HostPrefs) -> Result<HostPrefs, String> {
    let packaged = RuntimeLayout::detect(&app).is_packaged();
    let paths = HostPaths::for_mode(packaged);
    let autostart = app.autolaunch();
    let result = if prefs.launch_at_login {
        autostart.enable()
    } else {
        autostart.disable()
    };
    result.map_err(|error| format!("Could not update launch-at-login: {error}"))?;
    save_host_prefs(&paths, &prefs)?;
    Ok(prefs)
}

#[tauri::command]
async fn set_managed_local_llm_enabled_cmd(
    supervisor: State<'_, SharedSupervisor>,
    enabled: bool,
) -> Result<bool, String> {
    set_managed_local_llm_enabled(supervisor.inner().clone(), enabled).await
}

async fn check_for_updates(app: AppHandle) {
    if std::env::var("JARVIS_ENABLE_AUTO_UPDATE").as_deref() != Ok("1") {
        return;
    }

    let Ok(updater) = app.updater() else {
        eprintln!("Updater is not available");
        return;
    };
    let Ok(Some(update)) = updater.check().await else {
        eprintln!("No JARV1S desktop update available");
        return;
    };

    match update
        .download_and_install(|_chunk, _total| {}, || {})
        .await
    {
        Ok(()) => app.restart(),
        Err(error) => eprintln!("JARV1S desktop update failed: {error}"),
    }
}

fn show_main_window(app: &AppHandle) {
    if let Some(window) = app.get_webview_window("main") {
        let _ = window.show();
        let _ = window.set_focus();
    }
}

fn tray_icon() -> tauri::Result<tauri::image::Image<'static>> {
    tauri::image::Image::from_bytes(include_bytes!("../icons/tray-icon.png"))
}

fn setup_tray(app: &AppHandle) -> tauri::Result<()> {
    let open = MenuItem::with_id(app, "open", "Open JARV1S", true, None::<&str>)?;
    let restart = MenuItem::with_id(app, "restart", "Restart Host", true, None::<&str>)?;
    let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
    let menu = Menu::with_items(app, &[&open, &restart, &quit])?;

    let handle = app.clone();
    let mut builder = TrayIconBuilder::new()
        .icon(tray_icon()?)
        .menu(&menu)
        .tooltip("JARV1S Host");
    #[cfg(target_os = "macos")]
    {
        builder = builder.icon_as_template(true);
    }
    builder
        .on_menu_event(move |app, event| match event.id.as_ref() {
            "open" => show_main_window(app),
            "restart" => {
                if let Some(state) = app.try_state::<SharedSupervisor>() {
                    let supervisor = state.inner().clone();
                    let app_handle = app.clone();
                    tauri::async_runtime::spawn(async move {
                        let _ = restart_supervisor(supervisor, app_handle).await;
                    });
                }
            }
            "quit" => {
                if let Some(state) = app.try_state::<SharedSupervisor>() {
                    tauri::async_runtime::block_on(async {
                        stop_supervisor(state.inner().clone()).await;
                    });
                }
                app.exit(0);
            }
            _ => {}
        })
        .on_tray_icon_event(|tray, event| {
            if let TrayIconEvent::Click {
                button: MouseButton::Left,
                button_state: MouseButtonState::Up,
                ..
            } = event
            {
                show_main_window(tray.app_handle());
            }
        })
        .build(&handle)?;
    Ok(())
}

#[cfg_attr(mobile, tauri::mobile_entry_point)]
pub fn run() {
    tauri::Builder::default()
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .plugin(
            tauri_plugin_autostart::Builder::new()
                .macos_launcher(MacosLauncher::LaunchAgent)
                .build(),
        )
        .setup(|app| {
            let supervisor = shared_supervisor(app.handle());
            app.manage(supervisor);
            let call_activity = shared_call_activity();
            app.manage(call_activity.clone());
            start_call_activity_monitor(app.handle().clone(), call_activity);
            setup_tray(app.handle())?;

            let handle = app.handle().clone();
            tauri::async_runtime::spawn(async move {
                check_for_updates(handle).await;
            });

            if let Some(window) = app.get_webview_window("main") {
                let app_handle = app.handle().clone();
                window.on_window_event(move |event| {
                    if let WindowEvent::CloseRequested { api, .. } = event {
                        let packaged = RuntimeLayout::detect(&app_handle).is_packaged();
                        let prefs = load_host_prefs(&HostPaths::for_mode(packaged));
                        if prefs.hide_on_close {
                            api.prevent_close();
                            if let Some(window) = app_handle.get_webview_window("main") {
                                let _ = window.hide();
                            }
                        }
                    }
                });
            }
            Ok(())
        })
        .invoke_handler(tauri::generate_handler![
            get_launch_state,
            start_host,
            restart_host,
            stop_host,
            export_diagnostics_bundle,
            open_logs_folder,
            open_external_url,
            get_host_status,
            check_speaker_reachability,
            pair_speaker,
            enable_host_serve_cmd,
            enable_host_funnel_cmd,
            disable_host_funnel_cmd,
            get_host_prefs,
            set_host_prefs,
            set_managed_local_llm_enabled_cmd,
            get_call_activity,
            get_device_location
        ])
        .build(tauri::generate_context!())
        .expect("failed to build JARV1S desktop app")
        .run(|app_handle, event| {
            if let RunEvent::Exit = event {
                if let Some(state) = app_handle.try_state::<SharedSupervisor>() {
                    tauri::async_runtime::block_on(async {
                        stop_supervisor(state.inner().clone()).await;
                    });
                }
            }
        });
}
