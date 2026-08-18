use std::path::{Path, PathBuf};

pub const APP_SUPPORT_DIR: &str = "JARV1S";
pub const LOGS_DIR: &str = "JARV1S";

#[derive(Debug, Clone)]
pub struct HostPaths {
    pub data_dir: PathBuf,
    pub logs_dir: PathBuf,
}

impl HostPaths {
    pub fn for_mode(packaged: bool) -> Self {
        if packaged {
            Self::macos_app_paths()
        } else {
            Self::dev_repo_paths()
        }
    }

    fn dev_repo_paths() -> Self {
        let repo_root = crate::runtime::resolve_repo_root();
        Self {
            data_dir: repo_root.join(".data"),
            logs_dir: repo_root.join("backend").join("logs"),
        }
    }

    fn macos_app_paths() -> Self {
        let home = dirs::home_dir().unwrap_or_else(|| PathBuf::from("."));
        Self {
            data_dir: home
                .join("Library")
                .join("Application Support")
                .join(APP_SUPPORT_DIR),
            logs_dir: home.join("Library").join("Logs").join(LOGS_DIR),
        }
    }
}

impl HostPaths {
    pub fn mongo_dir(&self) -> PathBuf {
        self.data_dir.join("mongo")
    }

    pub fn run_dir(&self) -> PathBuf {
        self.data_dir.join("run")
    }

    pub fn mongodb_socket(&self) -> PathBuf {
        self.run_dir().join("mongodb-0.sock")
    }

    pub fn models_dir(&self) -> PathBuf {
        self.data_dir.join("models")
    }

    pub fn ollama_models_dir(&self) -> PathBuf {
        self.models_dir().join("ollama")
    }

    pub fn managed_ollama_ready_path(&self) -> PathBuf {
        self.run_dir().join("managed-ollama.ready")
    }
}

pub fn ensure_dir(path: &Path) -> Result<(), String> {
    std::fs::create_dir_all(path).map_err(|error| format!("Could not create {path:?}: {error}"))
}
