use crate::host_prefs::{load_host_prefs, save_host_prefs};
use crate::logs::LogStore;
use crate::paths::{ensure_dir, HostPaths};
use crate::reachability::resync_tailscale_ingress;
use crate::runtime::{RuntimeLayout, RuntimeMode};
use crate::services::{
    check_service_prerequisites, resolve_provider_kind, start_services, stop_services,
    ActiveServices, BackendServiceEnv, ServiceProviderKind, FAILURE_DOCKER, FAILURE_DOCKER_CLI,
    FAILURE_SERVICES,
};
use chrono::{DateTime, Utc};
use reqwest::Client;
use serde::{Deserialize, Serialize};
use std::fmt::Write as _;
use std::fs::File;
use std::io::Read;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tauri::{AppHandle, Emitter};
use tokio::sync::Mutex;
use tokio::time::sleep;

const DEFAULT_PORT: u16 = 8000;
const HEALTH_TIMEOUT_SECS: u64 = 90;
const GRACEFUL_SHUTDOWN_MS: u64 = 500;
const MAX_STARTUP_HISTORY: usize = 24;

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LaunchPhase {
    CheckPrerequisites,
    PrepareDependencies,
    StartServices,
    StartBackend,
    WaitForHealth,
    ResolveSetupState,
    Ready,
    Failed,
}

#[derive(Debug, Clone, Copy, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum LaunchStateKind {
    Checking,
    Running,
    Waiting,
    NeedsSetup,
    Ready,
    Degraded,
    Failed,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ChildSummary {
    pub name: String,
    pub status: String,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostLaunchState {
    pub phase: LaunchPhase,
    pub state: LaunchStateKind,
    pub message: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub detail: Option<String>,
    pub started_at: DateTime<Utc>,
    pub updated_at: DateTime<Utc>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backend_url: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    pub backend_port: Option<u16>,
    pub children: Vec<ChildSummary>,
}

impl HostLaunchState {
    fn new(phase: LaunchPhase, state: LaunchStateKind, message: impl Into<String>) -> Self {
        let now = Utc::now();
        Self {
            phase,
            state,
            message: message.into(),
            detail: None,
            started_at: now,
            updated_at: now,
            backend_url: None,
            backend_port: None,
            children: Vec::new(),
        }
    }

    fn with_detail(mut self, detail: impl Into<String>) -> Self {
        self.detail = Some(detail.into());
        self.updated_at = Utc::now();
        self
    }

    fn with_children(mut self, children: Vec<ChildSummary>) -> Self {
        self.children = children;
        self.updated_at = Utc::now();
        self
    }

    fn with_backend(mut self, port: u16) -> Self {
        self.backend_port = Some(port);
        self.backend_url = Some(format!("http://127.0.0.1:{port}"));
        self.updated_at = Utc::now();
        self
    }
}

struct BackendProcess {
    child: Child,
    pid: u32,
}

struct SpeechHelperProcess {
    child: Child,
    pid: u32,
    port: u16,
    token: String,
}

struct TTSHelperProcess {
    child: Child,
    pid: u32,
    port: u16,
    token: String,
}

struct OllamaSidecarProcess {
    child: Child,
    pid: u32,
    port: u16,
}

pub struct HostSupervisor {
    layout: RuntimeLayout,
    paths: HostPaths,
    pub log_store: LogStore,
    backend_port: u16,
    launch_state: HostLaunchState,
    pub startup_history: Vec<HostLaunchState>,
    backend: Option<BackendProcess>,
    speech_helper: Option<SpeechHelperProcess>,
    tts_helper: Option<TTSHelperProcess>,
    ollama: Option<OllamaSidecarProcess>,
    service_kind: ServiceProviderKind,
    services: ActiveServices,
    service_env: BackendServiceEnv,
    running: bool,
    watchdog_generation: u64,
    client: Client,
}

impl HostSupervisor {
    pub fn new(layout: RuntimeLayout, paths: HostPaths, log_store: LogStore) -> Self {
        let service_kind = resolve_provider_kind(&layout);
        Self {
            layout,
            paths,
            log_store,
            backend_port: DEFAULT_PORT,
            launch_state: HostLaunchState::new(
                LaunchPhase::CheckPrerequisites,
                LaunchStateKind::Checking,
                "Checking your local setup",
            ),
            startup_history: Vec::new(),
            backend: None,
            speech_helper: None,
            tts_helper: None,
            ollama: None,
            service_kind,
            services: ActiveServices::None,
            service_env: BackendServiceEnv { mongodb_url: None },
            running: false,
            watchdog_generation: 0,
            client: Client::builder()
                .timeout(Duration::from_secs(5))
                .build()
                .expect("reqwest client"),
        }
    }

    pub fn launch_state(&self) -> HostLaunchState {
        self.launch_state.clone()
    }

    pub fn layout(&self) -> RuntimeLayout {
        self.layout.clone()
    }

    pub fn paths(&self) -> HostPaths {
        self.paths.clone()
    }

    pub fn backend_port(&self) -> u16 {
        self.backend_port
    }

    pub fn client(&self) -> Client {
        self.client.clone()
    }

    fn child_summaries(&self) -> Vec<ChildSummary> {
        let mut children = Vec::new();
        match &self.services {
            ActiveServices::Docker => {
                children.push(ChildSummary {
                    name: "docker".to_string(),
                    status: "running".to_string(),
                    detail: Some("MongoDB".to_string()),
                });
            }
            ActiveServices::Bundled(bundled) => {
                for (name, status, detail) in bundled.summary_tuples() {
                    children.push(ChildSummary {
                        name,
                        status,
                        detail,
                    });
                }
            }
            ActiveServices::None => {}
        }
        if self.backend.is_some() {
            children.push(ChildSummary {
                name: "backend".to_string(),
                status: "running".to_string(),
                detail: Some(format!("127.0.0.1:{}", self.backend_port)),
            });
        }
        if let Some(helper) = &self.speech_helper {
            children.push(ChildSummary {
                name: "speech_helper".to_string(),
                status: "running".to_string(),
                detail: Some(format!("ws://127.0.0.1:{}/asr", helper.port)),
            });
        }
        if let Some(helper) = &self.tts_helper {
            children.push(ChildSummary {
                name: "tts_helper".to_string(),
                status: "running".to_string(),
                detail: Some(format!("ws://127.0.0.1:{}/tts", helper.port)),
            });
        }
        if let Some(ollama) = &self.ollama {
            children.push(ChildSummary {
                name: "ollama".to_string(),
                status: "running".to_string(),
                detail: Some(format!("http://127.0.0.1:{}/v1", ollama.port)),
            });
        }
        children
    }
}

pub async fn start_supervisor(supervisor: SharedSupervisor, app: AppHandle) -> Result<(), String> {
    {
        let mut guard = supervisor.lock().await;
        if guard.running {
            return Ok(());
        }
        guard.running = true;
    }

    let result = run_startup(supervisor.clone(), app.clone()).await;
    supervisor.lock().await.running = false;
    result
}

pub async fn restart_supervisor(
    supervisor: SharedSupervisor,
    app: AppHandle,
) -> Result<(), String> {
    stop_supervisor(supervisor.clone()).await;
    start_supervisor(supervisor, app).await
}

pub async fn stop_supervisor(supervisor: SharedSupervisor) {
    let (backend, speech_helper, tts_helper, ollama, mut services) = {
        let mut guard = supervisor.lock().await;
        guard.running = false;
        guard.watchdog_generation = guard.watchdog_generation.wrapping_add(1);
        (
            guard.backend.take(),
            guard.speech_helper.take(),
            guard.tts_helper.take(),
            guard.ollama.take(),
            std::mem::replace(&mut guard.services, ActiveServices::None),
        )
    };

    if let Some(mut backend) = backend {
        kill_process_tree(backend.pid, &mut backend.child).await;
    }
    if let Some(mut helper) = speech_helper {
        kill_process_tree(helper.pid, &mut helper.child).await;
    }
    if let Some(mut helper) = tts_helper {
        kill_process_tree(helper.pid, &mut helper.child).await;
    }
    if let Some(mut ollama) = ollama {
        kill_process_tree(ollama.pid, &mut ollama.child).await;
    }
    {
        let paths = supervisor.lock().await.paths.clone();
        clear_managed_ollama_ready(&paths);
    }

    stop_services(&mut services).await;
    supervisor.lock().await.services = ActiveServices::None;
}

/// Persist the managed-local preference and start/stop the Ollama sidecar atomically.
pub async fn set_managed_local_llm_enabled(
    supervisor: SharedSupervisor,
    enabled: bool,
) -> Result<bool, String> {
    let (layout, paths, log_store) = {
        let guard = supervisor.lock().await;
        (
            guard.layout.clone(),
            guard.paths.clone(),
            guard.log_store.clone(),
        )
    };
    let mut prefs = load_host_prefs(&paths);

    if enabled {
        let existing_port = supervisor
            .lock()
            .await
            .ollama
            .as_ref()
            .map(|process| process.port);
        if let Some(port) = existing_port {
            prefs.managed_local_llm_enabled = true;
            save_host_prefs(&paths, &prefs)?;
            write_managed_ollama_ready(&paths, port);
            return Ok(true);
        }

        if let Some(port) = ollama_port(&layout) {
            if port_is_bound(port) {
                return Err(format!(
                    "Port {port} is already used by another process. Close that process and try On this Mac again."
                ));
            }
        }
        let Some(mut ollama) = start_ollama_sidecar(&layout, &paths, &log_store).await else {
            clear_managed_ollama_ready(&paths);
            return Err(
                "Could not start the on-device model runtime. Check Resources/ollama-runtime is present, then try again."
                    .into(),
            );
        };
        prefs.managed_local_llm_enabled = true;
        if let Err(error) = save_host_prefs(&paths, &prefs) {
            kill_process_tree(ollama.pid, &mut ollama.child).await;
            clear_managed_ollama_ready(&paths);
            return Err(error);
        }
        write_managed_ollama_ready(&paths, ollama.port);
        supervisor.lock().await.ollama = Some(ollama);
        return Ok(true);
    }

    let existing = {
        let mut guard = supervisor.lock().await;
        guard.ollama.take()
    };
    prefs.managed_local_llm_enabled = false;
    if let Err(error) = save_host_prefs(&paths, &prefs) {
        supervisor.lock().await.ollama = existing;
        return Err(error);
    }
    if let Some(mut ollama) = existing {
        kill_process_tree(ollama.pid, &mut ollama.child).await;
    }
    clear_managed_ollama_ready(&paths);
    Ok(false)
}

async fn run_startup(supervisor: SharedSupervisor, app: AppHandle) -> Result<(), String> {
    let startup_started = Instant::now();
    let result = async {
        timed_phase("check_prerequisites", || {
            phase_check_prerequisites(supervisor.clone(), &app)
        })
        .await?;
        timed_phase("prepare_dependencies", || {
            phase_prepare_dependencies(supervisor.clone(), &app)
        })
        .await?;
        timed_phase("start_services", || {
            phase_start_services(supervisor.clone(), &app)
        })
        .await?;
        timed_phase("start_backend", || {
            phase_start_backend(supervisor.clone(), &app)
        })
        .await?;
        timed_phase("wait_for_health", || {
            phase_wait_for_health(supervisor.clone(), &app)
        })
        .await?;
        // Tailscale is optional remote access — never block shell → React redirect.
        timed_phase("resolve_setup_state", || {
            phase_resolve_setup_state(supervisor.clone(), &app)
        })
        .await
    }
    .await;

    eprintln!(
        "startup total: {:.0}ms ({})",
        startup_started.elapsed().as_secs_f64() * 1000.0,
        if result.is_ok() { "ok" } else { "err" }
    );

    if result.is_ok() {
        spawn_tailscale_restore(supervisor.clone(), app.clone());
    }

    if let Err(message) = &result {
        let detail = if message.contains("Docker") {
            if message == FAILURE_DOCKER || message == FAILURE_DOCKER_CLI {
                None
            } else {
                Some(FAILURE_DOCKER.to_string())
            }
        } else if message.contains("local database") {
            Some(FAILURE_SERVICES.to_string())
        } else if message.contains("exited during startup") {
            // Message already carries the recent backend log excerpt.
            None
        } else if message.contains("JARV1S Host") || message.contains("backend") {
            Some(FAILURE_BACKEND.to_string())
        } else {
            None
        };
        set_failed(supervisor, &app, message.clone(), detail).await;
    }

    result
}

async fn timed_phase<F, Fut>(name: &str, f: F) -> Result<(), String>
where
    F: FnOnce() -> Fut,
    Fut: std::future::Future<Output = Result<(), String>>,
{
    let started = Instant::now();
    let result = f().await;
    eprintln!(
        "startup phase {name}: {:.0}ms ({})",
        started.elapsed().as_secs_f64() * 1000.0,
        if result.is_ok() { "ok" } else { "err" }
    );
    result
}

async fn phase_check_prerequisites(
    supervisor: SharedSupervisor,
    app: &AppHandle,
) -> Result<(), String> {
    update_state(
        &supervisor,
        app,
        HostLaunchState::new(
            LaunchPhase::CheckPrerequisites,
            LaunchStateKind::Checking,
            "Checking your local setup",
        ),
    )
    .await;

    let guard = supervisor.lock().await;
    check_service_prerequisites(guard.service_kind, &guard.layout)?;

    match guard.layout.mode {
        RuntimeMode::DevRepo => {
            if resolve_command("uv").is_none() {
                return Err(
                    "JARV1S could not find uv. Install project dependencies for contributors."
                        .to_string(),
                );
            }
            if !guard.layout.backend_dir.join("main.py").is_file() {
                return Err(
                    "JARV1S backend was not found. Run the desktop app from the JARV1S repository."
                        .to_string(),
                );
            }
        }
        RuntimeMode::PackagedRuntime => {
            if !guard.layout.python_bin.is_file() {
                return Err("JARV1S runtime is missing its bundled Python interpreter.".to_string());
            }
            if !guard.layout.backend_dir.join("main.py").is_file() {
                return Err("JARV1S backend bundle is missing from the app.".to_string());
            }
            // Prove the bundled interpreter starts without relying on any system Python.
            let status = Command::new(&guard.layout.python_bin)
                .args(["-c", "import encodings"])
                .env_remove("PYTHONHOME")
                .env_remove("VIRTUAL_ENV")
                .stdout(Stdio::null())
                .stderr(Stdio::null())
                .status()
                .map_err(|error| {
                    format!("JARV1S could not start its bundled Python runtime: {error}")
                })?;
            if !status.success() {
                return Err(
                    "JARV1S bundled Python runtime is broken. Reinstall the app, or open logs for details."
                        .to_string(),
                );
            }
        }
    }
    Ok(())
}

async fn phase_prepare_dependencies(
    supervisor: SharedSupervisor,
    app: &AppHandle,
) -> Result<(), String> {
    update_state(
        &supervisor,
        app,
        HostLaunchState::new(
            LaunchPhase::PrepareDependencies,
            LaunchStateKind::Running,
            "Preparing JARV1S",
        ),
    )
    .await;

    let guard = supervisor.lock().await;
    let dist_index = guard.layout.frontend_dist.join("index.html");
    if !dist_index.is_file() {
        return Err(match guard.layout.mode {
            RuntimeMode::DevRepo => {
                "JARV1S UI is not built yet. Run `task desktop:prepare` or `task fe:build`, then open JARV1S again.".to_string()
            }
            RuntimeMode::PackagedRuntime => {
                "JARV1S UI bundle is missing from the app.".to_string()
            }
        });
    }
    Ok(())
}

async fn phase_start_services(supervisor: SharedSupervisor, app: &AppHandle) -> Result<(), String> {
    update_state(
        &supervisor,
        app,
        HostLaunchState::new(
            LaunchPhase::StartServices,
            LaunchStateKind::Running,
            "Starting local services",
        ),
    )
    .await;

    let (kind, layout, paths) = {
        let guard = supervisor.lock().await;
        (
            guard.service_kind,
            guard.layout.clone(),
            guard.paths.clone(),
        )
    };

    let (services, env) = start_services(kind, &layout, &paths).await?;
    let mut guard = supervisor.lock().await;
    guard.services = services;
    guard.service_env = env;
    Ok(())
}

async fn phase_start_backend(supervisor: SharedSupervisor, app: &AppHandle) -> Result<(), String> {
    let backend_port = pick_backend_port(DEFAULT_PORT)?;

    update_state(
        &supervisor,
        app,
        HostLaunchState::new(
            LaunchPhase::StartBackend,
            LaunchStateKind::Running,
            "Starting JARV1S Host",
        )
        .with_backend(backend_port),
    )
    .await;

    let (
        existing_backend,
        existing_speech,
        existing_tts,
        existing_ollama,
        layout,
        paths,
        log_store,
        service_env,
    ) = {
        let mut guard = supervisor.lock().await;
        guard.backend_port = backend_port;
        let existing = guard.backend.take();
        let existing_speech = guard.speech_helper.take();
        let existing_tts = guard.tts_helper.take();
        let existing_ollama = guard.ollama.take();
        (
            existing,
            existing_speech,
            existing_tts,
            existing_ollama,
            guard.layout.clone(),
            guard.paths.clone(),
            guard.log_store.clone(),
            guard.service_env.clone(),
        )
    };

    if let Some(mut backend) = existing_backend {
        kill_process_tree(backend.pid, &mut backend.child).await;
    }
    if let Some(mut helper) = existing_speech {
        kill_process_tree(helper.pid, &mut helper.child).await;
    }
    if let Some(mut helper) = existing_tts {
        kill_process_tree(helper.pid, &mut helper.child).await;
    }
    if let Some(mut ollama) = existing_ollama {
        kill_process_tree(ollama.pid, &mut ollama.child).await;
    }

    ensure_dir(&paths.data_dir)?;
    ensure_dir(&paths.logs_dir)?;
    ensure_dir(&paths.run_dir())?;
    ensure_dir(&paths.ollama_models_dir())?;

    // Reserve distinct ports before starting helpers concurrently. Probing from
    // inside each future can select the same free port before either process binds.
    let speech_port = pick_backend_port(9091);
    let reserved_ports: Vec<u16> = speech_port.iter().copied().collect();
    let tts_port = pick_backend_port_excluding(9092, &reserved_ports);
    let (speech_helper, tts_helper) = tokio::join!(
        start_speech_helper(&layout, &log_store, speech_port),
        start_tts_helper(&layout, &log_store, tts_port),
    );
    let speech_env = speech_helper
        .as_ref()
        .map(|helper| (helper.port, helper.token.clone()));
    let tts_env = tts_helper
        .as_ref()
        .map(|helper| (helper.port, helper.token.clone()));

    // Managed Ollama stays sequential and preference-gated: it is only required
    // when the user selected the on-device model lane.
    let prefs = load_host_prefs(&paths);
    let ollama = if prefs.managed_local_llm_enabled {
        start_ollama_sidecar(&layout, &paths, &log_store).await
    } else {
        None
    };
    if let Some(process) = &ollama {
        write_managed_ollama_ready(&paths, process.port);
    } else {
        clear_managed_ollama_ready(&paths);
    }

    let port = backend_port.to_string();
    let mut cmd = match layout.mode {
        RuntimeMode::DevRepo => {
            let mut command = Command::new("uv");
            command.args([
                "run",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                &port,
                "--ws",
                "websockets",
                "--ws-ping-interval",
                "20",
                "--ws-ping-timeout",
                "20",
            ]);
            command.current_dir(&layout.backend_dir);
            command
        }
        RuntimeMode::PackagedRuntime => {
            let mut command = Command::new(&layout.python_bin);
            command.args([
                "-m",
                "uvicorn",
                "main:app",
                "--host",
                "127.0.0.1",
                "--port",
                &port,
                "--ws",
                "websockets",
                "--ws-ping-interval",
                "20",
                "--ws-ping-timeout",
                "20",
            ]);
            command.current_dir(&layout.backend_dir);
            // Host-shell PYTHONHOME (or a stale VIRTUAL_ENV) can break
            // python-build-standalone with ModuleNotFoundError: encodings.
            // Users do not need system Python; scrub inherited overrides.
            command.env_remove("PYTHONHOME");
            command.env_remove("VIRTUAL_ENV");
            command
        }
    };

    cmd.env("JARVIS_APP_MODE", "1")
        .env("ENVIRONMENT", "production")
        .env("PYTHONPATH", layout.backend_dir.as_os_str())
        .env("BACKEND_HOST", "127.0.0.1")
        .env("BACKEND_PORT", &port)
        .env("FRONTEND_ORIGIN", format!("http://127.0.0.1:{port}"))
        .env("WEBSOCKETS_MAX_LINE_LENGTH", "32768")
        .env(
            "JARVIS_FRONTEND_DIST",
            layout.frontend_dist.to_string_lossy().as_ref(),
        )
        .env("JARVIS_DATA_DIR", paths.data_dir.to_string_lossy().as_ref())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    if let Some(url) = &service_env.mongodb_url {
        cmd.env("MONGODB_URL", url);
    }
    if let Some((speech_port, speech_token)) = &speech_env {
        cmd.env(
            "VOICE__apple_speech_url",
            format!("ws://127.0.0.1:{speech_port}/asr"),
        );
        cmd.env("VOICE__apple_speech_token", speech_token);
    } else {
        // Prevent the Host from probing a stale default/dev helper port.
        cmd.env("VOICE__apple_speech_url", "");
        cmd.env("VOICE__apple_speech_token", "");
    }
    if let Some((tts_port, tts_token)) = &tts_env {
        cmd.env(
            "VOICE__local_tts_url",
            format!("ws://127.0.0.1:{tts_port}/tts"),
        );
        cmd.env("VOICE__local_tts_token", tts_token);
    } else {
        cmd.env("VOICE__local_tts_url", "");
        cmd.env("VOICE__local_tts_token", "");
    }
    if let Some(process) = &ollama {
        cmd.env(
            "JARVIS_MANAGED_LLM_URL",
            format!("http://127.0.0.1:{}", process.port),
        );
    } else {
        cmd.env("JARVIS_MANAGED_LLM_URL", "");
    }

    if layout.mode == RuntimeMode::DevRepo {
        cmd.env("DYLD_LIBRARY_PATH", "/opt/homebrew/lib");
    }

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }

    let mut child = cmd
        .spawn()
        .map_err(|error| format!("Could not start JARV1S Host: {error}"))?;
    let pid = child.id();

    if let Some(stdout) = child.stdout.take() {
        log_store.spawn_reader("backend", stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        log_store.spawn_reader("backend", stderr);
    }

    {
        let mut guard = supervisor.lock().await;
        guard.backend = Some(BackendProcess { child, pid });
        guard.speech_helper = speech_helper;
        guard.tts_helper = tts_helper;
        guard.ollama = ollama;
    }
    Ok(())
}

async fn start_speech_helper(
    layout: &RuntimeLayout,
    log_store: &LogStore,
    port: Result<u16, String>,
) -> Option<SpeechHelperProcess> {
    let port = match port {
        Ok(port) => port,
        Err(error) => {
            eprintln!("speech helper port unavailable: {error}");
            return None;
        }
    };
    let token = match speech_helper_token() {
        Ok(token) => token,
        Err(error) => {
            eprintln!("speech helper token generation failed: {error}");
            return None;
        }
    };
    spawn_speech_helper(layout, log_store, port, token).await
}

async fn spawn_speech_helper(
    layout: &RuntimeLayout,
    log_store: &LogStore,
    port: u16,
    token: String,
) -> Option<SpeechHelperProcess> {
    let mut command = match speech_helper_command(layout) {
        Some(command) => command,
        None => {
            eprintln!("speech helper binary not found; continuing without local STT");
            return None;
        }
    };
    command
        .env("JARVIS_SPEECH_HOST", "127.0.0.1")
        .env("JARVIS_SPEECH_PORT", port.to_string())
        .env("JARVIS_SPEECH_TOKEN", &token)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            eprintln!("speech helper failed to start: {error}");
            return None;
        }
    };
    let pid = child.id();
    if let Some(stdout) = child.stdout.take() {
        log_store.spawn_reader("speech_helper", stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        log_store.spawn_reader("speech_helper", stderr);
    }

    // Brief listen wait. Never fail Host startup; drop the helper if it never binds.
    for _ in 0..20 {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return Some(SpeechHelperProcess {
                child,
                pid,
                port,
                token,
            });
        }
        if child.try_wait().ok().flatten().is_some() {
            eprintln!("speech helper exited before becoming ready");
            return None;
        }
        sleep(Duration::from_millis(100)).await;
    }
    eprintln!("speech helper did not become ready; continuing without local STT");
    kill_process_tree(pid, &mut child).await;
    None
}

fn speech_helper_token() -> std::io::Result<String> {
    let mut bytes = [0_u8; 32];
    File::open("/dev/urandom")?.read_exact(&mut bytes)?;
    let mut token = String::with_capacity(bytes.len() * 2);
    for byte in bytes {
        let _ = write!(token, "{byte:02x}");
    }
    Ok(token)
}

fn speech_helper_executable(layout: &RuntimeLayout) -> Option<PathBuf> {
    let candidates = [
        // Packaged: Contents/Helpers (sibling of Resources/host)
        layout
            .host_root
            .join("..")
            .join("..")
            .join("Helpers")
            .join("JARV1SSpeechHelper.app")
            .join("Contents")
            .join("MacOS")
            .join("JARV1SSpeechHelper"),
        // Dev repo checkout
        layout
            .host_root
            .join("apps")
            .join("desktop")
            .join("resources")
            .join("helpers")
            .join("JARV1SSpeechHelper.app")
            .join("Contents")
            .join("MacOS")
            .join("JARV1SSpeechHelper"),
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("resources")
            .join("helpers")
            .join("JARV1SSpeechHelper.app")
            .join("Contents")
            .join("MacOS")
            .join("JARV1SSpeechHelper"),
    ];
    candidates.into_iter().find(|path| is_executable_file(path))
}

fn speech_helper_command(layout: &RuntimeLayout) -> Option<Command> {
    if let Some(path) = speech_helper_executable(layout) {
        return Some(Command::new(path));
    }

    if layout.mode == RuntimeMode::DevRepo {
        let helper_py = layout
            .backend_dir
            .join("tools")
            .join("apple_speech_helper.py");
        if helper_py.is_file() {
            let mut command = Command::new("uv");
            command
                .args(["run", "python"])
                .arg(helper_py)
                .current_dir(&layout.backend_dir);
            return Some(command);
        }
    }

    None
}

async fn start_tts_helper(
    layout: &RuntimeLayout,
    log_store: &LogStore,
    port: Result<u16, String>,
) -> Option<TTSHelperProcess> {
    let port = match port {
        Ok(port) => port,
        Err(error) => {
            eprintln!("tts helper port unavailable: {error}");
            return None;
        }
    };
    let token = match speech_helper_token() {
        Ok(token) => token,
        Err(error) => {
            eprintln!("tts helper token generation failed: {error}");
            return None;
        }
    };
    spawn_tts_helper(layout, log_store, port, token).await
}

async fn spawn_tts_helper(
    layout: &RuntimeLayout,
    log_store: &LogStore,
    port: u16,
    token: String,
) -> Option<TTSHelperProcess> {
    let assets_dir = local_tts_assets_dir(layout);
    if !assets_dir.join("kokoro-v1.0.int8.onnx").is_file()
        || !assets_dir.join("voices-v1.0.bin").is_file()
    {
        eprintln!(
            "local TTS assets missing under {}; continuing without on-device speech",
            assets_dir.display()
        );
        return None;
    }
    let mut command = match tts_helper_command(layout) {
        Some(command) => command,
        None => {
            eprintln!("tts helper not found; continuing without on-device speech");
            return None;
        }
    };
    command
        .env("JARVIS_TTS_HOST", "127.0.0.1")
        .env("JARVIS_TTS_PORT", port.to_string())
        .env("JARVIS_TTS_TOKEN", &token)
        .env(
            "JARVIS_TTS_ASSETS_DIR",
            assets_dir.to_string_lossy().as_ref(),
        )
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            eprintln!("tts helper failed to start: {error}");
            return None;
        }
    };
    let pid = child.id();
    if let Some(stdout) = child.stdout.take() {
        log_store.spawn_reader("tts_helper", stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        log_store.spawn_reader("tts_helper", stderr);
    }

    for _ in 0..20 {
        if std::net::TcpStream::connect(("127.0.0.1", port)).is_ok() {
            return Some(TTSHelperProcess {
                child,
                pid,
                port,
                token,
            });
        }
        if child.try_wait().ok().flatten().is_some() {
            eprintln!("tts helper exited before becoming ready");
            return None;
        }
        sleep(Duration::from_millis(100)).await;
    }
    eprintln!("tts helper did not become ready; continuing without on-device speech");
    kill_process_tree(pid, &mut child).await;
    None
}

fn local_tts_assets_dir(layout: &RuntimeLayout) -> PathBuf {
    match layout.mode {
        RuntimeMode::PackagedRuntime => layout.host_root.join("local-tts"),
        RuntimeMode::DevRepo => layout
            .host_root
            .join("apps")
            .join("desktop")
            .join("local-tts"),
    }
}

fn tts_helper_command(layout: &RuntimeLayout) -> Option<Command> {
    let helper_py = layout
        .backend_dir
        .join("tools")
        .join("kokoro_tts_server.py");
    if !helper_py.is_file() {
        return None;
    }
    match layout.mode {
        RuntimeMode::DevRepo => {
            let mut command = Command::new("uv");
            command
                .args(["run", "python"])
                .arg(&helper_py)
                .current_dir(&layout.backend_dir);
            Some(command)
        }
        RuntimeMode::PackagedRuntime => {
            let mut command = Command::new(&layout.python_bin);
            command
                .arg(&helper_py)
                .current_dir(&layout.backend_dir)
                .env_remove("PYTHONHOME")
                .env_remove("VIRTUAL_ENV")
                .env("PYTHONPATH", layout.backend_dir.as_os_str());
            Some(command)
        }
    }
}

async fn maybe_restart_tts_helper(supervisor: &SharedSupervisor) {
    let restart = {
        let mut guard = supervisor.lock().await;
        let Some(helper) = guard.tts_helper.as_mut() else {
            return;
        };
        match helper.child.try_wait() {
            Ok(Some(_)) => {
                let port = helper.port;
                let token = helper.token.clone();
                let layout = guard.layout.clone();
                let log_store = guard.log_store.clone();
                guard.tts_helper = None;
                Some((layout, log_store, port, token))
            }
            _ => None,
        }
    };
    let Some((layout, log_store, port, token)) = restart else {
        return;
    };
    eprintln!("tts helper exited; restarting on port {port}");
    if let Some(helper) = spawn_tts_helper(&layout, &log_store, port, token).await {
        supervisor.lock().await.tts_helper = Some(helper);
    } else {
        eprintln!("tts helper restart failed; local TTS remains unavailable");
    }
}

async fn phase_wait_for_health(
    supervisor: SharedSupervisor,
    app: &AppHandle,
) -> Result<(), String> {
    let backend_port = backend_port(&supervisor).await;
    update_state(
        &supervisor,
        app,
        state_with_children(
            &supervisor,
            HostLaunchState::new(
                LaunchPhase::WaitForHealth,
                LaunchStateKind::Waiting,
                "Checking JARV1S is reachable",
            )
            .with_backend(backend_port),
        )
        .await,
    )
    .await;

    let health_url = format!("http://127.0.0.1:{backend_port}/api/v1/health");
    let client = client(&supervisor).await;

    for _ in 0..HEALTH_TIMEOUT_SECS {
        if let Some(status) = backend_exit_status(&supervisor).await {
            let mut message = format!("JARV1S Host exited during startup ({status}).");
            if let Some(logs) = recent_backend_log_detail(&supervisor).await {
                message.push_str("\n\n");
                message.push_str(&logs);
            }
            return Err(message);
        }
        if let Ok(response) = client.get(&health_url).send().await {
            if host_infra_healthy(response).await {
                return Ok(());
            }
        }
        sleep(Duration::from_secs(1)).await;
    }

    Err(FAILURE_BACKEND.to_string())
}

fn spawn_tailscale_restore(supervisor: SharedSupervisor, app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let started = Instant::now();
        let backend_port = backend_port(&supervisor).await;
        let packaged = RuntimeLayout::detect(&app).is_packaged();
        let prefs = load_host_prefs(&HostPaths::for_mode(packaged));
        let _ = resync_tailscale_ingress(Some(backend_port), prefs.external_triggers_enabled).await;
        eprintln!(
            "startup background restore_tailscale_ingress: {:.0}ms",
            started.elapsed().as_secs_f64() * 1000.0
        );
    });
}

async fn phase_resolve_setup_state(
    supervisor: SharedSupervisor,
    app: &AppHandle,
) -> Result<(), String> {
    let backend_port = backend_port(&supervisor).await;
    update_state(
        &supervisor,
        app,
        state_with_children(
            &supervisor,
            HostLaunchState::new(
                LaunchPhase::ResolveSetupState,
                LaunchStateKind::Waiting,
                "Opening setup",
            )
            .with_backend(backend_port),
        )
        .await,
    )
    .await;

    let setup_url = format!("http://127.0.0.1:{backend_port}/api/v1/setup/state");
    let response = client(&supervisor)
        .await
        .get(&setup_url)
        .send()
        .await
        .map_err(|_| FAILURE_BACKEND.to_string())?;

    if !response.status().is_success() {
        return Err(FAILURE_BACKEND.to_string());
    }

    let setup: SetupStateResponse = response
        .json()
        .await
        .map_err(|error| format!("Could not read setup state: {error}"))?;

    let (state_kind, message, phase) = match setup.phase.as_str() {
        "ready" if setup.core_ready => (
            LaunchStateKind::Ready,
            "JARV1S is ready".to_string(),
            LaunchPhase::Ready,
        ),
        "needs_setup" => (
            LaunchStateKind::NeedsSetup,
            "JARV1S is ready for setup.".to_string(),
            LaunchPhase::ResolveSetupState,
        ),
        "degraded" => (
            LaunchStateKind::Degraded,
            setup
                .blocking_reason
                .unwrap_or_else(|| "JARV1S is reachable but not fully ready.".to_string()),
            LaunchPhase::ResolveSetupState,
        ),
        _ if setup.core_ready => (
            LaunchStateKind::Ready,
            "JARV1S is ready".to_string(),
            LaunchPhase::Ready,
        ),
        _ => (
            LaunchStateKind::NeedsSetup,
            setup
                .next_action
                .unwrap_or_else(|| "Choose a language model provider to continue.".to_string()),
            LaunchPhase::ResolveSetupState,
        ),
    };

    update_state(
        &supervisor,
        app,
        state_with_children(
            &supervisor,
            HostLaunchState::new(phase, state_kind, message).with_backend(backend_port),
        )
        .await,
    )
    .await;
    spawn_health_watchdog(supervisor.clone(), app.clone());
    Ok(())
}

fn spawn_health_watchdog(supervisor: SharedSupervisor, app: AppHandle) {
    tauri::async_runtime::spawn(async move {
        let generation = {
            let mut guard = supervisor.lock().await;
            guard.watchdog_generation = guard.watchdog_generation.wrapping_add(1);
            guard.watchdog_generation
        };
        let mut consecutive_failures = 0u32;
        loop {
            sleep(Duration::from_secs(15)).await;
            let (port, startup_in_progress, current_generation) = {
                let guard = supervisor.lock().await;
                (guard.backend_port, guard.running, guard.watchdog_generation)
            };
            if current_generation != generation {
                return;
            }
            if startup_in_progress {
                consecutive_failures = 0;
                continue;
            }
            let healthy = match client(&supervisor)
                .await
                .get(format!("http://127.0.0.1:{port}/api/v1/health"))
                .timeout(Duration::from_secs(3))
                .send()
                .await
            {
                Ok(response) => host_infra_healthy(response).await,
                Err(_) => false,
            };
            if healthy {
                consecutive_failures = 0;
                maybe_restart_speech_helper(&supervisor).await;
                maybe_restart_tts_helper(&supervisor).await;
                maybe_restart_ollama_sidecar(&supervisor).await;
                continue;
            }
            consecutive_failures += 1;
            if consecutive_failures == 2 {
                update_state(
                    &supervisor,
                    &app,
                    state_with_children(
                        &supervisor,
                        HostLaunchState::new(
                            LaunchPhase::Failed,
                            LaunchStateKind::Degraded,
                            "Host health check failed. Attempting automatic restart.",
                        )
                        .with_backend(port)
                        .with_detail(
                            "Post-startup watchdog: database unavailable or Host unreachable.",
                        ),
                    )
                    .await,
                )
                .await;
            }
            if consecutive_failures >= 3 {
                let _ = restart_supervisor(supervisor.clone(), app.clone()).await;
                break;
            }
        }
    });
}

async fn maybe_restart_speech_helper(supervisor: &SharedSupervisor) {
    let restart = {
        let mut guard = supervisor.lock().await;
        let Some(helper) = guard.speech_helper.as_mut() else {
            return;
        };
        match helper.child.try_wait() {
            Ok(Some(_)) => {
                let port = helper.port;
                let token = helper.token.clone();
                let layout = guard.layout.clone();
                let log_store = guard.log_store.clone();
                guard.speech_helper = None;
                Some((layout, log_store, port, token))
            }
            _ => None,
        }
    };
    let Some((layout, log_store, port, token)) = restart else {
        return;
    };
    eprintln!("speech helper exited; restarting on port {port}");
    if let Some(helper) = spawn_speech_helper(&layout, &log_store, port, token).await {
        supervisor.lock().await.speech_helper = Some(helper);
    } else {
        eprintln!("speech helper restart failed; local STT remains unavailable");
    }
}

async fn start_ollama_sidecar(
    layout: &RuntimeLayout,
    paths: &HostPaths,
    log_store: &LogStore,
) -> Option<OllamaSidecarProcess> {
    let port = match ollama_port(layout) {
        Some(port) => port,
        None => {
            eprintln!("managed Ollama manifest has no valid port");
            return None;
        }
    };
    if port_is_bound(port) {
        eprintln!("managed Ollama port {port} already in use; refusing unknown process");
        return None;
    }
    spawn_ollama_sidecar(layout, paths, log_store, port).await
}

async fn spawn_ollama_sidecar(
    layout: &RuntimeLayout,
    paths: &HostPaths,
    log_store: &LogStore,
    port: u16,
) -> Option<OllamaSidecarProcess> {
    let binary = match ollama_helper_executable(layout) {
        Some(path) => path,
        None => {
            eprintln!("managed Ollama binary not found; continuing without local LLM runtime");
            return None;
        }
    };
    if let Err(error) = ensure_dir(&paths.ollama_models_dir()) {
        eprintln!("managed Ollama models dir unavailable: {error}");
        return None;
    }

    let context_length = ollama_context_length(layout).unwrap_or(32768).to_string();
    let host = format!("127.0.0.1:{port}");
    let mut command = Command::new(&binary);
    command
        .arg("serve")
        .env("OLLAMA_HOST", &host)
        .env(
            "OLLAMA_MODELS",
            paths.ollama_models_dir().to_string_lossy().as_ref(),
        )
        .env("OLLAMA_CONTEXT_LENGTH", &context_length)
        // Keep the managed model resident while the Host is up — cold MLX reload
        // plus CodeAct prefill routinely exceeds the cloud first-token budget.
        .env("OLLAMA_KEEP_ALIVE", "-1")
        .current_dir(binary.parent().unwrap_or(Path::new(".")))
        .stdout(Stdio::piped())
        .stderr(Stdio::piped());

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        command.process_group(0);
    }

    let mut child = match command.spawn() {
        Ok(child) => child,
        Err(error) => {
            eprintln!("managed Ollama failed to start: {error}");
            return None;
        }
    };
    let pid = child.id();
    if let Some(stdout) = child.stdout.take() {
        log_store.spawn_reader("ollama", stdout);
    }
    if let Some(stderr) = child.stderr.take() {
        log_store.spawn_reader("ollama", stderr);
    }

    // HTTP readiness — longer than speech helper; model server bind can be slow.
    let readiness_client = match Client::builder().timeout(Duration::from_secs(1)).build() {
        Ok(client) => client,
        Err(error) => {
            eprintln!("managed Ollama readiness client failed: {error}");
            kill_process_tree(pid, &mut child).await;
            return None;
        }
    };
    for _ in 0..50 {
        if ollama_tags_ready(&readiness_client, port).await {
            return Some(OllamaSidecarProcess { child, pid, port });
        }
        if child.try_wait().ok().flatten().is_some() {
            eprintln!("managed Ollama exited before becoming ready");
            return None;
        }
        sleep(Duration::from_millis(100)).await;
    }
    eprintln!("managed Ollama did not become ready; continuing without local LLM runtime");
    kill_process_tree(pid, &mut child).await;
    None
}

async fn maybe_restart_ollama_sidecar(supervisor: &SharedSupervisor) {
    let prefs_enabled = {
        let paths = supervisor.lock().await.paths.clone();
        load_host_prefs(&paths).managed_local_llm_enabled
    };
    if !prefs_enabled {
        return;
    }
    let restart = {
        let mut guard = supervisor.lock().await;
        let Some(ollama) = guard.ollama.as_mut() else {
            return;
        };
        match ollama.child.try_wait() {
            Ok(Some(_)) => {
                let layout = guard.layout.clone();
                let paths = guard.paths.clone();
                let log_store = guard.log_store.clone();
                guard.ollama = None;
                Some((layout, paths, log_store))
            }
            _ => None,
        }
    };
    let Some((layout, paths, log_store)) = restart else {
        return;
    };
    eprintln!("managed Ollama exited; restarting");
    if let Some(ollama) = start_ollama_sidecar(&layout, &paths, &log_store).await {
        write_managed_ollama_ready(&paths, ollama.port);
        supervisor.lock().await.ollama = Some(ollama);
    } else {
        clear_managed_ollama_ready(&paths);
        eprintln!("managed Ollama restart failed; local LLM remains unavailable");
    }
}

fn write_managed_ollama_ready(paths: &HostPaths, port: u16) {
    let _ = ensure_dir(&paths.run_dir());
    let body = format!("http://127.0.0.1:{port}\n");
    if let Err(error) = std::fs::write(paths.managed_ollama_ready_path(), body) {
        eprintln!("could not write managed Ollama ready marker: {error}");
    }
}

fn clear_managed_ollama_ready(paths: &HostPaths) {
    let _ = std::fs::remove_file(paths.managed_ollama_ready_path());
}

fn ollama_helper_executable(layout: &RuntimeLayout) -> Option<PathBuf> {
    let candidates = [
        // Packaged: Contents/Resources/ollama-runtime (sibling of Resources/host).
        layout
            .host_root
            .join("..")
            .join("ollama-runtime")
            .join("ollama"),
        // Legacy Helpers paths from earlier managed-LLM builds.
        layout
            .host_root
            .join("..")
            .join("..")
            .join("Helpers")
            .join("ollama-runtime")
            .join("ollama"),
        layout
            .host_root
            .join("..")
            .join("..")
            .join("Helpers")
            .join("Ollama")
            .join("ollama"),
        layout
            .host_root
            .join("apps")
            .join("desktop")
            .join("resources")
            .join("helpers")
            .join("Ollama")
            .join("ollama"),
        PathBuf::from(env!("CARGO_MANIFEST_DIR"))
            .join("..")
            .join("resources")
            .join("helpers")
            .join("Ollama")
            .join("ollama"),
    ];
    candidates.into_iter().find(|path| is_executable_file(path))
}

fn ollama_port(layout: &RuntimeLayout) -> Option<u16> {
    let manifest = read_ollama_manifest(layout)?;
    if manifest.get("host")?.as_str()? != "127.0.0.1" {
        return None;
    }
    let port = manifest.get("port")?.as_u64()?;
    u16::try_from(port).ok()
}

fn ollama_context_length(layout: &RuntimeLayout) -> Option<u32> {
    let context = read_ollama_manifest(layout)?
        .get("context_length")?
        .as_u64()?;
    u32::try_from(context).ok()
}

fn read_ollama_manifest(layout: &RuntimeLayout) -> Option<serde_json::Value> {
    let raw = std::fs::read_to_string(ollama_manifest_path(layout)?).ok()?;
    serde_json::from_str(&raw).ok()
}

fn ollama_manifest_path(layout: &RuntimeLayout) -> Option<PathBuf> {
    let dir = ollama_helper_executable(layout)?.parent()?.to_path_buf();
    let path = dir.join("manifest.json");
    path.is_file().then_some(path)
}

fn port_is_bound(port: u16) -> bool {
    std::net::TcpStream::connect(("127.0.0.1", port)).is_ok()
}

async fn ollama_tags_ready(client: &Client, port: u16) -> bool {
    match client
        .get(format!("http://127.0.0.1:{port}/api/tags"))
        .send()
        .await
    {
        Ok(response) => response.status().is_success(),
        Err(_) => false,
    }
}

async fn recent_backend_log_detail(supervisor: &SharedSupervisor) -> Option<String> {
    let lines = supervisor.lock().await.log_store.snapshot();
    let backend: Vec<_> = lines
        .into_iter()
        .rev()
        .filter(|line| line.source == "backend")
        .take(8)
        .map(|line| line.message)
        .collect();
    if backend.is_empty() {
        return None;
    }
    let mut ordered = backend;
    ordered.reverse();
    Some(format!("Recent backend logs:\n{}", ordered.join("\n")))
}

async fn set_failed(
    supervisor: SharedSupervisor,
    app: &AppHandle,
    message: String,
    detail: Option<String>,
) {
    let mut state = HostLaunchState::new(LaunchPhase::Failed, LaunchStateKind::Failed, message);
    if let Some(detail) = detail {
        state = state.with_detail(detail);
    }
    update_state(
        &supervisor,
        app,
        state_with_children(&supervisor, state).await,
    )
    .await;
}

async fn update_state(supervisor: &SharedSupervisor, app: &AppHandle, state: HostLaunchState) {
    let state = {
        let mut guard = supervisor.lock().await;
        guard.launch_state = state.clone();
        let should_record = guard
            .startup_history
            .last()
            .map(|last| last.phase != state.phase || last.state != state.state)
            .unwrap_or(true);
        if should_record {
            guard.startup_history.push(state.clone());
            if guard.startup_history.len() > MAX_STARTUP_HISTORY {
                guard.startup_history.remove(0);
            }
        }
        guard.launch_state.clone()
    };
    let _ = app.emit("host-launch-update", state);
}

async fn state_with_children(
    supervisor: &SharedSupervisor,
    state: HostLaunchState,
) -> HostLaunchState {
    let children = supervisor.lock().await.child_summaries();
    state.with_children(children)
}

async fn backend_port(supervisor: &SharedSupervisor) -> u16 {
    supervisor.lock().await.backend_port
}

async fn client(supervisor: &SharedSupervisor) -> Client {
    supervisor.lock().await.client.clone()
}

async fn backend_exit_status(supervisor: &SharedSupervisor) -> Option<String> {
    let mut guard = supervisor.lock().await;
    let status = match guard.backend.as_mut() {
        Some(backend) => backend.child.try_wait().ok().flatten(),
        None => None,
    }?;
    guard.backend = None;
    Some(status.to_string())
}

#[derive(Debug, Deserialize)]
struct SetupStateResponse {
    phase: String,
    core_ready: bool,
    #[serde(default)]
    blocking_reason: Option<String>,
    #[serde(default)]
    next_action: Option<String>,
}

#[derive(Debug, Deserialize)]
struct HealthServices {
    #[serde(default)]
    database: Option<String>,
}

#[derive(Debug, Deserialize)]
struct HealthResponse {
    #[serde(default)]
    status: Option<String>,
    #[serde(default)]
    services: Option<HealthServices>,
}

/// True when the Host reports a reachable database.
/// Fail closed on non-2xx, parse errors, or missing service fields.
async fn host_infra_healthy(response: reqwest::Response) -> bool {
    if !response.status().is_success() {
        return false;
    }
    match response.json::<HealthResponse>().await {
        Ok(body) => health_body_infra_up(&body),
        Err(_) => false,
    }
}

fn health_body_infra_up(body: &HealthResponse) -> bool {
    if matches!(body.status.as_deref(), Some("unavailable")) {
        return false;
    }
    let Some(services) = &body.services else {
        return false;
    };
    services.database.as_deref() == Some("up")
}

const FAILURE_BACKEND: &str = "JARV1S Host started, but the app could not reach it.\n\nWait a moment and try again. If this continues, another app may be using the JARV1S port.";

fn resolve_command(name: &str) -> Option<std::path::PathBuf> {
    std::env::var_os("PATH")
        .into_iter()
        .flat_map(|paths| std::env::split_paths(&paths).collect::<Vec<_>>())
        .chain([
            std::path::PathBuf::from("/opt/homebrew/bin"),
            std::path::PathBuf::from("/usr/local/bin"),
            std::path::PathBuf::from("/usr/bin"),
            std::path::PathBuf::from("/bin"),
            std::path::PathBuf::from("/Applications/Docker.app/Contents/Resources/bin"),
        ])
        .map(|dir| dir.join(name))
        .find(|path| is_executable_file(path))
}

fn is_executable_file(path: &Path) -> bool {
    path.is_file()
}

fn pick_backend_port(preferred: u16) -> Result<u16, String> {
    pick_backend_port_excluding(preferred, &[])
}

fn pick_backend_port_excluding(preferred: u16, excluded: &[u16]) -> Result<u16, String> {
    use std::net::TcpListener;
    for port in preferred..preferred + 20 {
        if excluded.contains(&port) {
            continue;
        }
        if TcpListener::bind(("127.0.0.1", port)).is_ok() {
            return Ok(port);
        }
    }
    Err("No available localhost port found for JARV1S Host.".to_string())
}

async fn kill_process_tree(pid: u32, child: &mut Child) {
    #[cfg(unix)]
    {
        let pgid = pid as i32;
        unsafe {
            libc::kill(-pgid, libc::SIGTERM);
        }
        sleep(Duration::from_millis(GRACEFUL_SHUTDOWN_MS)).await;
        if child.try_wait().ok().flatten().is_none() {
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
            let _ = child.kill();
        }
    }

    #[cfg(not(unix))]
    {
        let _ = child.kill();
    }

    let _ = child.wait();
}

pub type SharedSupervisor = Arc<Mutex<HostSupervisor>>;

pub fn shared_supervisor(app: &AppHandle) -> SharedSupervisor {
    let layout = RuntimeLayout::detect(app);
    let paths = HostPaths::for_mode(layout.is_packaged());
    let _ = ensure_dir(&paths.data_dir);
    let _ = ensure_dir(&paths.logs_dir);
    let log_store = LogStore::new(&paths.logs_dir);
    Arc::new(Mutex::new(HostSupervisor::new(layout, paths, log_store)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn pick_backend_port_skips_an_occupied_preferred_port() {
        let occupied = loop {
            let listener =
                std::net::TcpListener::bind(("127.0.0.1", 0)).expect("ephemeral test port");
            if listener.local_addr().expect("local address").port() <= u16::MAX - 20 {
                break listener;
            }
        };
        let preferred = occupied.local_addr().expect("local address").port();
        let port = pick_backend_port(preferred).expect("port");
        assert!(port > preferred);
        drop(occupied);
    }

    #[test]
    fn pick_backend_port_skips_a_reserved_helper_port() {
        let available = loop {
            let listener =
                std::net::TcpListener::bind(("127.0.0.1", 0)).expect("ephemeral test port");
            let port = listener.local_addr().expect("local address").port();
            if port <= u16::MAX - 20 {
                drop(listener);
                break port;
            }
        };

        let port = pick_backend_port_excluding(available, &[available]).expect("port");
        assert_ne!(port, available);
    }

    #[test]
    fn health_body_requires_database_up() {
        assert!(health_body_infra_up(&HealthResponse {
            status: Some("healthy".into()),
            services: Some(HealthServices {
                database: Some("up".into()),
            }),
        }));
        assert!(health_body_infra_up(&HealthResponse {
            status: Some("needs_setup".into()),
            services: Some(HealthServices {
                database: Some("up".into()),
            }),
        }));
        assert!(!health_body_infra_up(&HealthResponse {
            status: Some("unavailable".into()),
            services: Some(HealthServices {
                database: Some("down".into()),
            }),
        }));
        assert!(!health_body_infra_up(&HealthResponse {
            status: Some("healthy".into()),
            services: None,
        }));
    }
}
