use serde::{Deserialize, Serialize};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ReleaseMetadata {
    pub app_version: String,
    pub host_version: String,
    pub release_channel: String,
    pub frontend_build: Option<String>,
    pub runtime_bundle: Option<String>,
    pub platform: String,
    pub arch: String,
}

impl ReleaseMetadata {
    pub fn current(app_version: &str) -> Self {
        let bundle = read_runtime_bundle();
        Self {
            app_version: app_version.to_string(),
            host_version: bundle
                .as_ref()
                .and_then(|b| b.get("host_version").and_then(|v| v.as_str()))
                .unwrap_or(app_version)
                .to_string(),
            release_channel: bundle
                .as_ref()
                .and_then(|b| b.get("release_channel").and_then(|v| v.as_str()))
                .unwrap_or("internal")
                .to_string(),
            frontend_build: bundle
                .as_ref()
                .and_then(|b| b.get("frontend_build").and_then(|v| v.as_str()))
                .map(str::to_string),
            runtime_bundle: bundle
                .as_ref()
                .and_then(|b| b.get("runtime_bundle").and_then(|v| v.as_str()))
                .map(str::to_string),
            platform: std::env::consts::OS.to_string(),
            arch: std::env::consts::ARCH.to_string(),
        }
    }
}

fn read_runtime_bundle() -> Option<serde_json::Value> {
    let path = super::runtime::bundled_host_root()?.join("runtime-bundle.json");
    let text = std::fs::read_to_string(path).ok()?;
    serde_json::from_str(&text).ok()
}
