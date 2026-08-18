use crate::paths::{ensure_dir, HostPaths};
use serde::{Deserialize, Serialize};
use std::fs;
use std::path::PathBuf;

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(default)]
pub struct HostPrefs {
    pub launch_at_login: bool,
    pub hide_on_close: bool,
    pub external_triggers_enabled: bool,
    /// When true, the Host supervises the bundled Ollama sidecar on :11435.
    pub managed_local_llm_enabled: bool,
}

impl Default for HostPrefs {
    fn default() -> Self {
        Self {
            launch_at_login: false,
            hide_on_close: true,
            external_triggers_enabled: false,
            managed_local_llm_enabled: false,
        }
    }
}

fn prefs_path(paths: &HostPaths) -> PathBuf {
    paths.data_dir.join("host-prefs.json")
}

pub fn load_host_prefs(paths: &HostPaths) -> HostPrefs {
    let path = prefs_path(paths);
    match fs::read_to_string(path) {
        Ok(raw) => serde_json::from_str(&raw).unwrap_or_default(),
        Err(_) => HostPrefs::default(),
    }
}

pub fn save_host_prefs(paths: &HostPaths, prefs: &HostPrefs) -> Result<(), String> {
    ensure_dir(&paths.data_dir)?;
    let path = prefs_path(paths);
    let raw = serde_json::to_string_pretty(prefs)
        .map_err(|error| format!("Could not serialize host prefs: {error}"))?;
    fs::write(&path, raw).map_err(|error| format!("Could not write {path:?}: {error}"))
}
