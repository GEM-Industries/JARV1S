use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::path::PathBuf;
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncRead, BufReader};
use tokio::process::Command;
use tokio::sync::mpsc;
use tokio::time::timeout;

const SERVE_HTTPS_PORT: u16 = 8443;
const FUNNEL_HTTPS_PORT: u16 = 443;
const WEBHOOK_PATH: &str = "/api/v1/webhooks";
const PUSH_PATH: &str = "/api/v1/push";

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum TailscaleStatus {
    Connected,
    Offline,
    NotInstalled,
    Unknown,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum HostStatus {
    Online,
    Degraded,
    Offline,
}

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
#[serde(rename_all = "snake_case")]
pub enum SpeakerNetworkState {
    /// Peer is connected to the tailnet right now.
    Online,
    /// Peer is known to the tailnet but not currently connected.
    Offline,
    /// No tailnet peer matches this speaker's node id.
    NotFound,
    /// Tailscale on this Mac could not be queried.
    Unavailable,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct SpeakerReachability {
    pub network: SpeakerNetworkState,
    pub last_seen: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct HostReachabilityStatus {
    pub state: HostStatus,
    pub backend_healthy: bool,
    pub remote_healthy: Option<bool>,
    pub backend_url: Option<String>,
    pub tailscale: TailscaleStatus,
    pub serve_url: Option<String>,
    pub funnel_url: Option<String>,
    pub funnel_configured: bool,
    pub funnel_needs_consent: bool,
    pub sleep_risk: bool,
    pub detail: Option<String>,
}

pub async fn probe_host_reachability(backend_port: Option<u16>) -> HostReachabilityStatus {
    let backend_url = backend_port.map(|port| format!("http://127.0.0.1:{port}"));
    let backend_healthy = match &backend_url {
        Some(url) => probe_backend_health(url).await,
        None => false,
    };
    let (tailscale, serve_url, funnel_url, funnel_configured, detail) =
        probe_tailscale(backend_port).await;
    let remote_healthy = match &serve_url {
        Some(url) => Some(probe_backend_health(url).await),
        None => None,
    };
    let state = if !backend_healthy {
        HostStatus::Offline
    } else if remote_healthy == Some(true) {
        HostStatus::Online
    } else {
        HostStatus::Degraded
    };
    HostReachabilityStatus {
        state,
        backend_healthy,
        remote_healthy,
        backend_url,
        tailscale,
        serve_url,
        funnel_url,
        funnel_configured,
        funnel_needs_consent: false,
        sleep_risk: true,
        detail,
    }
}

async fn probe_backend_health(backend_url: &str) -> bool {
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(2))
        .danger_accept_invalid_certs(true)
        .build()
    {
        Ok(client) => client,
        Err(_) => return false,
    };
    let health_url = format!("{}/api/v1/health", backend_url.trim_end_matches('/'));
    match client.get(health_url).send().await {
        Ok(response) => {
            if !response.status().is_success() {
                return false;
            }
            match response.json::<Value>().await {
                Ok(body) => {
                    let db = body
                        .pointer("/services/database")
                        .and_then(|v| v.as_str())
                        .unwrap_or("down");
                    db == "up"
                }
                Err(_) => false,
            }
        }
        Err(_) => false,
    }
}

async fn probe_tailscale(
    backend_port: Option<u16>,
) -> (
    TailscaleStatus,
    Option<String>,
    Option<String>,
    bool,
    Option<String>,
) {
    let Some(binary) = tailscale_binary() else {
        return (
            TailscaleStatus::NotInstalled,
            None,
            None,
            false,
            Some("Install Tailscale and enable Serve for remote access.".to_string()),
        );
    };
    let status = timeout(
        Duration::from_secs(3),
        Command::new(&binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args(["status", "--json"])
            .output(),
    )
    .await;
    let Ok(Ok(output)) = status else {
        return (
            TailscaleStatus::Offline,
            None,
            None,
            false,
            Some("Tailscale status did not respond.".to_string()),
        );
    };
    if !output.status.success() {
        return (
            TailscaleStatus::Offline,
            None,
            None,
            false,
            Some("Tailscale is installed but not connected.".to_string()),
        );
    }

    let parsed: serde_json::Value = match serde_json::from_slice(&output.stdout) {
        Ok(value) => value,
        Err(_) => {
            return (
                TailscaleStatus::Unknown,
                None,
                None,
                false,
                Some("Could not parse Tailscale status.".to_string()),
            )
        }
    };

    let backend_state = parsed
        .get("BackendState")
        .and_then(|value| value.as_str())
        .unwrap_or_default();
    if backend_state != "Running" {
        return (
            TailscaleStatus::Offline,
            None,
            None,
            false,
            Some(format!("Tailscale state: {backend_state}")),
        );
    }

    let dns_name = parsed
        .get("Self")
        .and_then(|value| value.get("DNSName"))
        .and_then(|value| value.as_str())
        .map(|value| value.trim_end_matches('.').to_string());

    let serve_url = match backend_port {
        Some(port) => probe_tailscale_serve_url(&binary, dns_name.as_deref(), port).await,
        None => None,
    };
    let (funnel_url, funnel_configured) = match backend_port {
        Some(port) => probe_tailscale_funnel(&binary, dns_name.as_deref(), port).await,
        None => (None, false),
    };
    let detail = if serve_url.is_some() {
        Some("Tailscale Serve routes HTTPS to the loopback JARV1S Host.".to_string())
    } else {
        Some("Tailscale is connected, but Serve is not routing to this Host.".to_string())
    };

    (
        TailscaleStatus::Connected,
        serve_url,
        funnel_url,
        funnel_configured,
        detail,
    )
}

/// Look up the speaker's device on the tailnet by matching its presence
/// node_id against peer hostnames (node ids default to `<hostname>` or
/// `<hostname>-<uuid8>` on the satellite).
pub async fn probe_speaker_reachability(node_id: &str) -> SpeakerReachability {
    let unavailable = SpeakerReachability {
        network: SpeakerNetworkState::Unavailable,
        last_seen: None,
    };
    let Some(binary) = tailscale_binary() else {
        return unavailable;
    };
    let output = timeout(
        Duration::from_secs(3),
        Command::new(&binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args(["status", "--json"])
            .output(),
    )
    .await;
    let Ok(Ok(output)) = output else {
        return unavailable;
    };
    if !output.status.success() {
        return unavailable;
    }
    let Ok(parsed) = serde_json::from_slice::<Value>(&output.stdout) else {
        return unavailable;
    };
    speaker_reachability_from_status(&parsed, node_id)
}

fn speaker_reachability_from_status(status: &Value, node_id: &str) -> SpeakerReachability {
    let node_id = node_id.to_ascii_lowercase();
    let peers = status.get("Peer").and_then(Value::as_object);
    let peer = peers.into_iter().flat_map(|map| map.values()).find(|peer| {
        peer_hostnames(peer)
            .any(|hostname| node_id == hostname || node_id.starts_with(&format!("{hostname}-")))
    });
    let Some(peer) = peer else {
        return SpeakerReachability {
            network: SpeakerNetworkState::NotFound,
            last_seen: None,
        };
    };
    let online = peer.get("Online").and_then(Value::as_bool).unwrap_or(false);
    let last_seen = peer
        .get("LastSeen")
        .and_then(Value::as_str)
        .filter(|value| !value.starts_with("0001-"))
        .map(str::to_string);
    SpeakerReachability {
        network: if online {
            SpeakerNetworkState::Online
        } else {
            SpeakerNetworkState::Offline
        },
        last_seen,
    }
}

fn peer_hostnames(peer: &Value) -> impl Iterator<Item = String> + '_ {
    // DNSName is the unique MagicDNS name Tailscale uses for display; HostName
    // is a fallback and is explicitly not guaranteed unique.
    let dns_label = peer
        .get("DNSName")
        .and_then(Value::as_str)
        .and_then(|value| value.split('.').next());
    let host_name = peer.get("HostName").and_then(Value::as_str);
    [dns_label, host_name]
        .into_iter()
        .flatten()
        .filter(|value| !value.is_empty())
        .map(|value| value.to_ascii_lowercase())
}

fn loopback_targets(backend_port: u16) -> [String; 3] {
    [
        format!("http://127.0.0.1:{backend_port}"),
        format!("http://localhost:{backend_port}"),
        format!("http://[::1]:{backend_port}"),
    ]
}

async fn probe_tailscale_serve_url(
    binary: &PathBuf,
    dns_name: Option<&str>,
    backend_port: u16,
) -> Option<String> {
    let output = timeout(
        Duration::from_secs(3),
        Command::new(binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args(["serve", "status", "--json"])
            .output(),
    )
    .await
    .ok()?
    .ok()?;
    if !output.status.success() {
        return None;
    }
    let status: Value = serde_json::from_slice(&output.stdout).ok()?;
    let targets = loopback_targets(backend_port);
    find_matching_serve_url(&status, dns_name, &targets, SERVE_HTTPS_PORT)
}

async fn probe_tailscale_funnel(
    binary: &PathBuf,
    dns_name: Option<&str>,
    backend_port: u16,
) -> (Option<String>, bool) {
    let output = timeout(
        Duration::from_secs(3),
        Command::new(binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args(["funnel", "status", "--json"])
            .output(),
    )
    .await;
    let Ok(Ok(output)) = output else {
        return (None, false);
    };
    if !output.status.success() {
        return (None, false);
    }
    let Ok(status) = serde_json::from_slice::<Value>(&output.stdout) else {
        return (None, false);
    };
    let targets = loopback_targets(backend_port);
    match find_matching_funnel_url(&status, dns_name, &targets) {
        Some(url) => (Some(url), true),
        None => (None, false),
    }
}

fn find_matching_serve_url(
    value: &Value,
    dns_name: Option<&str>,
    loopback_targets: &[String],
    https_port: u16,
) -> Option<String> {
    let suffix = format!(":{https_port}");
    match value {
        Value::Object(object) => {
            if let Some(web) = object.get("Web").and_then(Value::as_object) {
                for (host_port, config) in web {
                    let host = host_port
                        .strip_suffix(&suffix)
                        .or_else(|| host_port.strip_suffix(":443"))
                        .unwrap_or(host_port);
                    if dns_name.is_some_and(|expected| host != expected) {
                        continue;
                    }
                    if !host_port.ends_with(&suffix) && https_port != 443 {
                        continue;
                    }
                    let Some(handlers) = config.get("Handlers").and_then(Value::as_object) else {
                        continue;
                    };
                    let matches = handlers.values().any(|handler| {
                        handler
                            .get("Proxy")
                            .and_then(Value::as_str)
                            .is_some_and(|proxy| {
                                loopback_targets
                                    .iter()
                                    .any(|target| proxy.trim_end_matches('/') == target)
                            })
                    });
                    if matches {
                        return Some(if https_port == 443 {
                            format!("https://{host}")
                        } else {
                            format!("https://{host}:{https_port}")
                        });
                    }
                }
            }
            for child in object.values() {
                if let Some(url) =
                    find_matching_serve_url(child, dns_name, loopback_targets, https_port)
                {
                    return Some(url);
                }
            }
            None
        }
        Value::Array(values) => values.iter().find_map(|child| {
            find_matching_serve_url(child, dns_name, loopback_targets, https_port)
        }),
        _ => None,
    }
}

fn find_matching_funnel_url(
    value: &Value,
    dns_name: Option<&str>,
    loopback_targets: &[String],
) -> Option<String> {
    match value {
        Value::Object(object) => {
            if let Some(web) = object.get("Web").and_then(Value::as_object) {
                for (host_port, config) in web {
                    let host = host_port
                        .strip_suffix(":443")
                        .or_else(|| host_port.strip_suffix(&format!(":{FUNNEL_HTTPS_PORT}")))
                        .unwrap_or(host_port);
                    if dns_name.is_some_and(|expected| host != expected) {
                        continue;
                    }
                    let Some(handlers) = config.get("Handlers").and_then(Value::as_object) else {
                        continue;
                    };
                    let webhook_ok = handler_matches_path(handlers, WEBHOOK_PATH, loopback_targets);
                    let push_ok = handler_matches_path(handlers, PUSH_PATH, loopback_targets);
                    if webhook_ok && push_ok {
                        return Some(format!("https://{host}"));
                    }
                }
            }
            for child in object.values() {
                if let Some(url) = find_matching_funnel_url(child, dns_name, loopback_targets) {
                    return Some(url);
                }
            }
            None
        }
        Value::Array(values) => values
            .iter()
            .find_map(|child| find_matching_funnel_url(child, dns_name, loopback_targets)),
        _ => None,
    }
}

fn handler_matches_path(
    handlers: &serde_json::Map<String, Value>,
    path: &str,
    loopback_targets: &[String],
) -> bool {
    handlers.iter().any(|(mount, handler)| {
        let mount_ok = mount == path || mount.starts_with(&format!("{path}/")) || mount == "/";
        // Prefer exact path mounts; allow root only if both paths are not present separately.
        let exact = mount == path || mount.trim_end_matches('/') == path;
        if !exact && mount != path {
            // Accept exact path prefix match from Tailscale set-path
            if !mount_ok || mount == "/" {
                return false;
            }
        }
        if !exact {
            return false;
        }
        handler
            .get("Proxy")
            .and_then(Value::as_str)
            .is_some_and(|proxy| {
                let proxy = proxy.trim_end_matches('/');
                loopback_targets
                    .iter()
                    .any(|target| proxy == target || proxy.starts_with(&format!("{target}/")))
            })
    })
}

fn tailscale_binary() -> Option<PathBuf> {
    let candidates = [
        PathBuf::from("/Applications/Tailscale.app/Contents/MacOS/tailscale"),
        PathBuf::from("/Applications/Tailscale.app/Contents/MacOS/Tailscale"),
    ]
    .into_iter()
    .chain(
        std::env::var_os("PATH")
            .into_iter()
            .flat_map(|paths| std::env::split_paths(&paths).collect::<Vec<_>>())
            .map(|directory| directory.join("tailscale")),
    )
    .chain([
        PathBuf::from("/usr/local/bin/tailscale"),
        PathBuf::from("/opt/homebrew/bin/tailscale"),
    ]);
    candidates.into_iter().find(|path| path.is_file())
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnableHostServeResult {
    pub ok: bool,
    pub needs_consent: bool,
    pub consent_url: Option<String>,
    pub serve_url: Option<String>,
    pub detail: Option<String>,
}

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct EnableHostFunnelResult {
    pub ok: bool,
    pub needs_consent: bool,
    pub consent_url: Option<String>,
    pub funnel_url: Option<String>,
    pub detail: Option<String>,
}

enum InteractiveCommandResult {
    Completed { success: bool, output: String },
    ConsentRequired(String),
    TimedOut,
}

async fn forward_lines<R>(reader: R, sender: mpsc::UnboundedSender<String>)
where
    R: AsyncRead + Unpin,
{
    let mut lines = BufReader::new(reader).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let _ = sender.send(line);
    }
}

async fn run_tailscale_interactive(
    binary: &PathBuf,
    args: &[String],
    max_wait: Duration,
) -> Result<InteractiveCommandResult, String> {
    let mut child = Command::new(binary)
        .env("TAILSCALE_BE_CLI", "1")
        .kill_on_drop(true)
        .args(args)
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .map_err(|error| format!("Could not run Tailscale: {error}"))?;

    let (sender, mut receiver) = mpsc::unbounded_channel();
    if let Some(stdout) = child.stdout.take() {
        tokio::spawn(forward_lines(stdout, sender.clone()));
    }
    if let Some(stderr) = child.stderr.take() {
        tokio::spawn(forward_lines(stderr, sender));
    }

    let mut output = Vec::new();
    let deadline = tokio::time::sleep(max_wait);
    tokio::pin!(deadline);

    let result = {
        let wait = child.wait();
        tokio::pin!(wait);
        loop {
            tokio::select! {
                status = &mut wait => {
                    let status = status.map_err(|error| format!("Tailscale failed: {error}"))?;
                    while let Ok(line) = receiver.try_recv() {
                        output.push(line);
                    }
                    break InteractiveCommandResult::Completed {
                        success: status.success(),
                        output: output.join("\n"),
                    };
                }
                line = receiver.recv() => {
                    if let Some(line) = line {
                        output.push(line);
                        let combined = output.join("\n");
                        if let Some(url) = extract_tailscale_consent_url(&combined) {
                            break InteractiveCommandResult::ConsentRequired(url);
                        }
                    }
                }
                _ = &mut deadline => {
                    break InteractiveCommandResult::TimedOut;
                }
            }
        }
    };

    if matches!(
        result,
        InteractiveCommandResult::ConsentRequired(_) | InteractiveCommandResult::TimedOut
    ) {
        let _ = child.kill().await;
        let _ = child.wait().await;
    }
    Ok(result)
}

pub async fn enable_host_serve(backend_port: Option<u16>) -> EnableHostServeResult {
    let Some(port) = backend_port else {
        return EnableHostServeResult {
            ok: false,
            needs_consent: false,
            consent_url: None,
            serve_url: None,
            detail: Some("JARV1S is not listening yet.".to_string()),
        };
    };
    let Some(binary) = tailscale_binary() else {
        return EnableHostServeResult {
            ok: false,
            needs_consent: false,
            consent_url: None,
            serve_url: None,
            detail: Some("Install Tailscale first.".to_string()),
        };
    };

    let target = format!("http://127.0.0.1:{port}");
    let output = timeout(
        Duration::from_secs(12),
        Command::new(&binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args([
                "serve",
                "--bg",
                "--yes",
                &format!("--https={SERVE_HTTPS_PORT}"),
                &target,
            ])
            .output(),
    )
    .await;

    let Ok(Ok(output)) = output else {
        return EnableHostServeResult {
            ok: false,
            needs_consent: false,
            consent_url: None,
            serve_url: None,
            detail: Some("Tailscale did not respond while enabling private access.".to_string()),
        };
    };

    let stdout = String::from_utf8_lossy(&output.stdout);
    let stderr = String::from_utf8_lossy(&output.stderr);
    let combined = format!("{stdout}\n{stderr}");

    if !output.status.success() {
        let consent_url = extract_tailscale_consent_url(&combined);
        let needs_consent =
            consent_url.is_some() || combined.to_lowercase().contains("serve is not enabled");
        return EnableHostServeResult {
            ok: false,
            needs_consent,
            consent_url,
            serve_url: None,
            detail: Some(if needs_consent {
                "Private access needs a one-time approval in Tailscale.".to_string()
            } else {
                combined
                    .lines()
                    .map(str::trim)
                    .find(|line| !line.is_empty())
                    .unwrap_or("Could not enable private access.")
                    .to_string()
            }),
        };
    }

    for _ in 0..6 {
        let status = probe_host_reachability(Some(port)).await;
        if let Some(serve_url) = status.serve_url {
            return EnableHostServeResult {
                ok: true,
                needs_consent: false,
                consent_url: None,
                serve_url: Some(serve_url),
                detail: status.detail,
            };
        }
        tokio::time::sleep(Duration::from_millis(400)).await;
    }

    EnableHostServeResult {
        ok: false,
        needs_consent: false,
        consent_url: None,
        serve_url: None,
        detail: Some(
            "Private access was requested, but JARV1S could not confirm the share URL yet."
                .to_string(),
        ),
    }
}

pub async fn enable_host_funnel(backend_port: Option<u16>) -> EnableHostFunnelResult {
    let Some(port) = backend_port else {
        return EnableHostFunnelResult {
            ok: false,
            needs_consent: false,
            consent_url: None,
            funnel_url: None,
            detail: Some("JARV1S is not listening yet.".to_string()),
        };
    };
    let Some(binary) = tailscale_binary() else {
        return EnableHostFunnelResult {
            ok: false,
            needs_consent: false,
            consent_url: None,
            funnel_url: None,
            detail: Some("Install Tailscale first.".to_string()),
        };
    };

    let target = format!("http://127.0.0.1:{port}");
    // Port 443 may still have JARV1S's legacy private Serve route. Clear it
    // without requiring Funnel permission, then replace it with public paths.
    let clear_result = timeout(
        Duration::from_secs(12),
        Command::new(&binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args([
                "serve",
                "--yes",
                &format!("--https={FUNNEL_HTTPS_PORT}"),
                "off",
            ])
            .output(),
    )
    .await;
    match clear_result {
        Err(_) => {
            return EnableHostFunnelResult {
                ok: false,
                needs_consent: false,
                consent_url: None,
                funnel_url: None,
                detail: Some(
                    "Tailscale could not clear the previous private route. Quit and reopen Tailscale, then try again."
                        .to_string(),
                ),
            };
        }
        Ok(Err(error)) => {
            return EnableHostFunnelResult {
                ok: false,
                needs_consent: false,
                consent_url: None,
                funnel_url: None,
                detail: Some(format!("Could not run Tailscale: {error}")),
            };
        }
        Ok(Ok(output)) if !output.status.success() => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            if stderr.to_lowercase().contains("handler does not exist") {
                // Already clear; continue with Funnel setup.
            } else {
                let detail = stderr
                    .lines()
                    .map(str::trim)
                    .find(|line| !line.is_empty())
                    .unwrap_or("Could not clear the previous private route.")
                    .to_string();
                return EnableHostFunnelResult {
                    ok: false,
                    needs_consent: false,
                    consent_url: None,
                    funnel_url: None,
                    detail: Some(detail),
                };
            }
        }
        Ok(Ok(_)) => {}
    }

    for path in [WEBHOOK_PATH, PUSH_PATH] {
        let args = vec![
            "funnel".to_string(),
            "--bg".to_string(),
            "--yes".to_string(),
            format!("--https={FUNNEL_HTTPS_PORT}"),
            format!("--set-path={path}"),
            target.clone(),
        ];
        match run_tailscale_interactive(&binary, &args, Duration::from_secs(120)).await {
            Ok(InteractiveCommandResult::ConsentRequired(consent_url)) => {
                return EnableHostFunnelResult {
                    ok: false,
                    needs_consent: true,
                    consent_url: Some(consent_url),
                    funnel_url: None,
                    detail: Some(
                        "External triggers need a one-time Funnel approval in Tailscale."
                            .to_string(),
                    ),
                };
            }
            Ok(InteractiveCommandResult::TimedOut) => {
                return EnableHostFunnelResult {
                    ok: false,
                    needs_consent: false,
                    consent_url: None,
                    funnel_url: None,
                    detail: Some(
                        "Funnel approval was not completed. Sign in on the Tailscale page and approve Funnel, then try again."
                            .to_string(),
                    ),
                };
            }
            Err(error) => {
                return EnableHostFunnelResult {
                    ok: false,
                    needs_consent: false,
                    consent_url: None,
                    funnel_url: None,
                    detail: Some(error),
                };
            }
            Ok(InteractiveCommandResult::Completed {
                success: false,
                output,
            }) => {
                return EnableHostFunnelResult {
                    ok: false,
                    needs_consent: false,
                    consent_url: None,
                    funnel_url: None,
                    detail: Some(
                        output
                            .lines()
                            .map(str::trim)
                            .find(|line| !line.is_empty())
                            .unwrap_or("Could not enable external triggers.")
                            .to_string(),
                    ),
                };
            }
            Ok(InteractiveCommandResult::Completed { success: true, .. }) => {}
        }
    }

    for _ in 0..8 {
        let status = probe_host_reachability(Some(port)).await;
        if status.funnel_configured {
            if let Some(funnel_url) = status.funnel_url {
                let _ = reconcile_backend_ingress(port, &funnel_url, true).await;
                return EnableHostFunnelResult {
                    ok: true,
                    needs_consent: false,
                    consent_url: None,
                    funnel_url: Some(funnel_url),
                    detail: Some("External triggers are configured on public HTTPS.".to_string()),
                };
            }
        }
        tokio::time::sleep(Duration::from_millis(400)).await;
    }

    EnableHostFunnelResult {
        ok: false,
        needs_consent: false,
        consent_url: None,
        funnel_url: None,
        detail: Some(
            "Funnel was requested, but JARV1S could not confirm both public webhook paths yet."
                .to_string(),
        ),
    }
}

pub async fn disable_host_funnel(backend_port: Option<u16>) -> EnableHostFunnelResult {
    let Some(binary) = tailscale_binary() else {
        return EnableHostFunnelResult {
            ok: false,
            needs_consent: false,
            consent_url: None,
            funnel_url: None,
            detail: Some("Install Tailscale first.".to_string()),
        };
    };

    let _ = if let Some(port) = backend_port {
        reconcile_backend_ingress(port, "", false).await
    } else {
        false
    };

    let _ = timeout(
        Duration::from_secs(12),
        Command::new(&binary)
            .env("TAILSCALE_BE_CLI", "1")
            .kill_on_drop(true)
            .args([
                "funnel",
                "--yes",
                &format!("--https={FUNNEL_HTTPS_PORT}"),
                "off",
            ])
            .output(),
    )
    .await;

    let status = probe_host_reachability(backend_port).await;
    EnableHostFunnelResult {
        ok: !status.funnel_configured,
        needs_consent: false,
        consent_url: None,
        funnel_url: None,
        detail: Some("External triggers disabled.".to_string()),
    }
}

pub async fn resync_tailscale_ingress(
    backend_port: Option<u16>,
    external_triggers_enabled: bool,
) -> HostReachabilityStatus {
    if backend_port.is_some() {
        let _ = enable_host_serve(backend_port).await;
        if external_triggers_enabled {
            let _ = enable_host_funnel(backend_port).await;
        }
    }
    probe_host_reachability(backend_port).await
}

async fn reconcile_backend_ingress(backend_port: u16, base_url: &str, enabled: bool) -> bool {
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(8))
        .build()
    {
        Ok(client) => client,
        Err(_) => return false,
    };
    let body = if enabled {
        serde_json::json!({
            "enabled": true,
            "provider": "tailscale_funnel",
            "base_url": base_url.trim_end_matches('/'),
        })
    } else {
        serde_json::json!({
            "enabled": false,
            "provider": "none",
            "base_url": null,
        })
    };
    let url = format!("http://127.0.0.1:{backend_port}/api/v1/ingress/external");
    match client.post(url).json(&body).send().await {
        Ok(response) => response.status().is_success(),
        Err(_) => false,
    }
}

fn extract_tailscale_consent_url(text: &str) -> Option<String> {
    for token in text.split_whitespace() {
        let trimmed = token.trim_matches(|c: char| !c.is_ascii_graphic());
        if trimmed.starts_with("https://login.tailscale.com/f/") {
            return Some(trimmed.to_string());
        }
    }
    None
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn matches_https_host_on_serve_8443() {
        let status = json!({
            "Web": {
                "jarvis.example.ts.net:8443": {
                    "Handlers": {
                        "/": {"Proxy": "http://127.0.0.1:8000"}
                    }
                }
            }
        });
        let targets = vec!["http://127.0.0.1:8000".to_string()];
        assert_eq!(
            find_matching_serve_url(&status, Some("jarvis.example.ts.net"), &targets, 8443),
            Some("https://jarvis.example.ts.net:8443".to_string())
        );
    }

    #[test]
    fn matches_funnel_paths_on_443() {
        let status = json!({
            "Web": {
                "jarvis.example.ts.net:443": {
                    "Handlers": {
                        "/api/v1/webhooks": {"Proxy": "http://127.0.0.1:8000"},
                        "/api/v1/push": {"Proxy": "http://127.0.0.1:8000"}
                    }
                }
            }
        });
        let targets = vec!["http://127.0.0.1:8000".to_string()];
        assert_eq!(
            find_matching_funnel_url(&status, Some("jarvis.example.ts.net"), &targets),
            Some("https://jarvis.example.ts.net".to_string())
        );
    }

    #[test]
    fn speaker_probe_matches_peer_by_hostname_and_reports_offline() {
        let status = json!({
            "Peer": {
                "nodekey:abc": {
                    "HostName": "jarvis-satellite-1",
                    "DNSName": "jarvis-satellite-1.tail1234.ts.net.",
                    "Online": false,
                    "LastSeen": "2026-07-25T02:50:33Z"
                }
            }
        });
        let result = speaker_reachability_from_status(&status, "jarvis-satellite-1");
        assert_eq!(result.network, SpeakerNetworkState::Offline);
        assert_eq!(result.last_seen.as_deref(), Some("2026-07-25T02:50:33Z"));
    }

    #[test]
    fn speaker_probe_matches_generated_node_id_prefix_and_ignores_zero_last_seen() {
        let status = json!({
            "Peer": {
                "nodekey:abc": {
                    "HostName": "raspberrypi",
                    "Online": true,
                    "LastSeen": "0001-01-01T00:00:00Z"
                }
            }
        });
        let result = speaker_reachability_from_status(&status, "raspberrypi-1a2b3c4d");
        assert_eq!(result.network, SpeakerNetworkState::Online);
        assert_eq!(result.last_seen, None);
    }

    #[test]
    fn speaker_probe_reports_not_found_for_unknown_node() {
        let status = json!({ "Peer": {} });
        let result = speaker_reachability_from_status(&status, "jarvis-satellite-1");
        assert_eq!(result.network, SpeakerNetworkState::NotFound);
    }

    #[test]
    fn extracts_https_consent_url_from_serve_output() {
        let text = "Serve is not enabled on your tailnet.\nTo enable, visit:\n\n         https://login.tailscale.com/f/serve?node=abc\n";
        assert_eq!(
            extract_tailscale_consent_url(text),
            Some("https://login.tailscale.com/f/serve?node=abc".to_string())
        );
    }

    #[test]
    fn does_not_treat_funnel_endpoint_as_consent_url() {
        let text = "Available on the internet:\nhttps://jarvis.example.ts.net/api/v1/webhooks\n";
        assert_eq!(extract_tailscale_consent_url(text), None);
    }
}
