use serde::{Deserialize, Serialize};
use std::time::Duration;

pub const SPEAKER_SETUP_PORT: u16 = 8742;

#[derive(Debug, Clone, Serialize, Deserialize, PartialEq, Eq)]
pub struct PairSpeakerResult {
    pub ok: bool,
    pub node_id: Option<String>,
    pub detail: Option<String>,
}

/// Deploy default hostname used only for first setup (no known node_id).
const DEFAULT_SPEAKER_HOST: &str = "jarvis-satellite-1";

pub fn speaker_setup_urls(node_id: Option<&str>) -> Vec<String> {
    let hosts: Vec<String> = match node_id.map(str::trim).filter(|value| !value.is_empty()) {
        Some(id) => vec![format!("{id}.local"), id.to_string()],
        None => vec![
            format!("{DEFAULT_SPEAKER_HOST}.local"),
            DEFAULT_SPEAKER_HOST.to_string(),
        ],
    };
    hosts
        .into_iter()
        .map(|host| format!("http://{host}:{SPEAKER_SETUP_PORT}/pair"))
        .collect()
}

pub async fn pair_speaker(
    code: &str,
    backend_url: Option<&str>,
    node_id: Option<&str>,
) -> PairSpeakerResult {
    let mut last_detail = "Could not reach the speaker from this Mac.".to_string();
    let client = match reqwest::Client::builder()
        .timeout(Duration::from_secs(6))
        .build()
    {
        Ok(client) => client,
        Err(error) => {
            return PairSpeakerResult {
                ok: false,
                node_id: None,
                detail: Some(error.to_string()),
            };
        }
    };
    let mut body = serde_json::json!({ "code": code });
    if let Some(url) = backend_url.map(str::trim).filter(|value| !value.is_empty()) {
        body["backend_url"] = serde_json::Value::String(url.to_string());
    }
    for url in speaker_setup_urls(node_id) {
        match client.post(&url).json(&body).send().await {
            Ok(response) => {
                let status = response.status();
                let parsed = response.json::<serde_json::Value>().await.ok();
                if status.is_success()
                    && parsed
                        .as_ref()
                        .and_then(|value| value.get("ok"))
                        .and_then(serde_json::Value::as_bool)
                        == Some(true)
                {
                    let paired_id = parsed
                        .as_ref()
                        .and_then(|value| value.get("node_id"))
                        .and_then(serde_json::Value::as_str)
                        .map(str::to_string);
                    return PairSpeakerResult {
                        ok: true,
                        node_id: paired_id,
                        detail: None,
                    };
                }
                last_detail = parsed
                    .as_ref()
                    .and_then(|value| value.get("error"))
                    .and_then(serde_json::Value::as_str)
                    .map(str::to_string)
                    .unwrap_or_else(|| format!("Speaker setup failed ({status})"));
                if status.as_u16() == 401 {
                    return PairSpeakerResult {
                        ok: false,
                        node_id: None,
                        detail: Some(last_detail),
                    };
                }
            }
            Err(error) => {
                last_detail = error.to_string();
            }
        }
    }
    PairSpeakerResult {
        ok: false,
        node_id: None,
        detail: Some(last_detail),
    }
}

#[cfg(test)]
mod tests {
    use super::speaker_setup_urls;

    #[test]
    fn default_hosts_include_local_mdns() {
        let urls = speaker_setup_urls(None);
        assert!(urls.iter().any(|url| url.contains("jarvis-satellite-1.local")));
    }

    #[test]
    fn known_node_is_tried_first() {
        let urls = speaker_setup_urls(Some("bedroom-pi"));
        assert_eq!(urls[0], "http://bedroom-pi.local:8742/pair");
        assert_eq!(urls[1], "http://bedroom-pi:8742/pair");
        assert!(!urls.iter().any(|url| url.contains("jarvis-satellite-1")));
    }
}
