use crate::metadata::ReleaseMetadata;
use crate::supervisor::{HostLaunchState, SharedSupervisor};
use chrono::Utc;
use reqwest::Client;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use tauri::AppHandle;

#[derive(Debug, Deserialize)]
pub struct ExportDiagnosticsRequest {
    #[serde(default)]
    pub include_user_content: bool,
    #[serde(default)]
    pub client: Option<Value>,
}

#[derive(Debug, Serialize)]
pub struct ExportDiagnosticsResponse {
    pub path: String,
}

#[derive(Debug, Serialize)]
struct DiagnosticsBundle {
    exported_at: String,
    user_content_included: bool,
    prompt_dumps_included: bool,
    release: ReleaseMetadata,
    runtime_mode: String,
    launch_state: HostLaunchState,
    startup_history: Vec<HostLaunchState>,
    logs: Vec<crate::logs::LogLine>,
    backend: BackendDiagnostics,
    client: Option<Value>,
}

#[derive(Debug, Serialize)]
struct BackendDiagnostics {
    version: Option<Value>,
    health: Option<Value>,
    setup_state: Option<Value>,
    snapshot: Option<Value>,
}

pub async fn export_diagnostics(
    app: AppHandle,
    supervisor: SharedSupervisor,
    request: ExportDiagnosticsRequest,
) -> Result<ExportDiagnosticsResponse, String> {
    let app_version = app.package_info().version.to_string();
    let release = ReleaseMetadata::current(&app_version);
    let guard = supervisor.lock().await;
    let layout = guard.layout();
    let paths = guard.paths();
    let launch_state = guard.launch_state();
    let startup_history = guard.startup_history.clone();
    let logs = if request.include_user_content {
        guard.log_store.snapshot()
    } else {
        Vec::new()
    };
    let backend_port = guard.backend_port();
    let client = guard.client();
    drop(guard);

    crate::paths::ensure_dir(&paths.logs_dir)?;

    let base = format!("http://127.0.0.1:{backend_port}/api/v1");
    let version = fetch_json(&client, &format!("{base}/version")).await.ok();
    let health = fetch_json_allow_error(&client, &format!("{base}/health"))
        .await
        .ok();
    let setup_state = fetch_json(&client, &format!("{base}/setup/state"))
        .await
        .ok();

    let snapshot = if request.include_user_content {
        fetch_snapshot(&client, &base).await.ok()
    } else {
        None
    };

    let client = request.client.and_then(sanitize_client_diagnostics);

    let bundle = DiagnosticsBundle {
        exported_at: Utc::now().to_rfc3339(),
        user_content_included: request.include_user_content,
        prompt_dumps_included: false,
        release,
        runtime_mode: format!("{:?}", layout.mode),
        launch_state,
        startup_history,
        logs,
        backend: BackendDiagnostics {
            version,
            health,
            setup_state,
            snapshot,
        },
        client,
    };

    let filename = format!(
        "jarvis-diagnostics-{}.json",
        Utc::now().format("%Y%m%dT%H%M%SZ")
    );
    let export_path = paths.logs_dir.join(filename);
    let payload = serde_json::to_string_pretty(&bundle)
        .map_err(|error| format!("Could not serialize diagnostics: {error}"))?;
    std::fs::write(&export_path, payload)
        .map_err(|error| format!("Could not write diagnostics export: {error}"))?;

    Ok(ExportDiagnosticsResponse {
        path: export_path.display().to_string(),
    })
}

pub fn open_logs(app: &AppHandle) -> Result<(), String> {
    let layout = crate::runtime::RuntimeLayout::detect(app);
    let paths = crate::paths::HostPaths::for_mode(layout.is_packaged());
    crate::logs::open_logs_dir(&paths.logs_dir)
}

async fn fetch_json(client: &Client, url: &str) -> Result<Value, String> {
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    let response = response
        .error_for_status()
        .map_err(|error| error.to_string())?;
    response.json().await.map_err(|error| error.to_string())
}

/// Like [`fetch_json`], but keeps the body when the Host reports infra failure (503).
async fn fetch_json_allow_error(client: &Client, url: &str) -> Result<Value, String> {
    let response = client
        .get(url)
        .send()
        .await
        .map_err(|error| error.to_string())?;
    response.json().await.map_err(|error| error.to_string())
}

async fn fetch_snapshot(client: &Client, base: &str) -> Result<Value, String> {
    let response = client
        .post(format!("{base}/snapshots/"))
        .json(&json!({ "reason": "desktop_diagnostics_export" }))
        .send()
        .await
        .map_err(|error| error.to_string())?;
    let response = response
        .error_for_status()
        .map_err(|error| error.to_string())?;
    response.json().await.map_err(|error| error.to_string())
}

/// Keep only the bounded client breadcrumb snapshot; drop unexpected shapes.
fn sanitize_client_diagnostics(value: Value) -> Option<Value> {
    const EVENTS: [&str; 7] = [
        "transport_transition",
        "mic_acquire",
        "mic_interrupted",
        "mic_flatline",
        "playback_summary",
        "playback_failed",
        "notification_failed",
    ];

    fn bounded_string(value: &Value, max_chars: usize) -> Option<Value> {
        let text = value.as_str()?;
        let cleaned: String = text
            .chars()
            .filter(|character| !character.is_control())
            .take(max_chars)
            .collect();
        Some(Value::String(cleaned))
    }

    let object = value.as_object()?;
    let events = object.get("events")?.as_array()?;
    let mut sanitized_events = Vec::new();
    for event in events.iter().take(50) {
        let Some(entry) = event.as_object() else {
            continue;
        };
        let Some(event_name) = entry.get("event").and_then(Value::as_str) else {
            continue;
        };
        if !EVENTS.contains(&event_name) {
            continue;
        }
        let mut clean = serde_json::Map::new();
        if let Some(seq) = entry.get("seq").and_then(Value::as_u64) {
            clean.insert("seq".to_string(), Value::from(seq));
        }
        for key in [
            "ts",
            "category",
            "event",
            "severity",
            "turn_id",
            "message_id",
        ] {
            if let Some(item) = entry.get(key).and_then(|item| bounded_string(item, 64)) {
                clean.insert(key.to_string(), item);
            }
        }
        if let Some(metadata) = entry.get("metadata").and_then(Value::as_object) {
            let mut meta = serde_json::Map::new();
            for (key, item) in metadata.iter().take(12) {
                let clean_key: String = key
                    .chars()
                    .filter(|character| !character.is_control())
                    .take(32)
                    .collect();
                if clean_key.is_empty() {
                    continue;
                }
                match item {
                    Value::Null | Value::Bool(_) | Value::Number(_) => {
                        meta.insert(clean_key, item.clone());
                    }
                    Value::String(_) => {
                        if let Some(value) = bounded_string(item, 64) {
                            meta.insert(clean_key, value);
                        }
                    }
                    _ => {}
                }
            }
            clean.insert("metadata".to_string(), Value::Object(meta));
        }
        sanitized_events.push(Value::Object(clean));
    }
    Some(json!({
        "events": sanitized_events,
        "dropped_count": object.get("dropped_count").and_then(Value::as_u64).unwrap_or(0),
        "pending_count": object.get("pending_count").and_then(Value::as_u64).unwrap_or(0),
    }))
}

#[cfg(test)]
mod tests {
    use super::sanitize_client_diagnostics;
    use serde_json::json;

    #[test]
    fn keeps_bounded_client_snapshot() {
        let value = json!({
            "events": [
                {
                    "seq": 1,
                    "ts": "2026-07-23T00:00:00Z",
                    "category": "mic",
                    "event": "mic_flatline",
                    "severity": "warning",
                    "metadata": {
                        "reason": "flatline",
                        "unicode": "🎙️".repeat(80),
                        "nested": { "x": 1 }
                    }
                },
                { "event": "arbitrary_event", "metadata": { "secret": "drop-me" } }
            ],
            "dropped_count": 2,
            "pending_count": 1,
            "secret": "drop-me"
        });
        let sanitized = sanitize_client_diagnostics(value).expect("snapshot");
        assert_eq!(sanitized["dropped_count"], 2);
        assert_eq!(sanitized["events"].as_array().unwrap().len(), 1);
        assert_eq!(sanitized["events"][0]["event"], "mic_flatline");
        assert!(sanitized["events"][0]["metadata"].get("nested").is_none());
        assert!(sanitized.get("secret").is_none());
    }
}
