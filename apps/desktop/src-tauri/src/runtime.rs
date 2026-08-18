use std::path::PathBuf;
use tauri::{AppHandle, Manager};

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum RuntimeMode {
    DevRepo,
    PackagedRuntime,
}

#[derive(Debug, Clone)]
pub struct RuntimeLayout {
    pub mode: RuntimeMode,
    pub host_root: PathBuf,
    pub backend_dir: PathBuf,
    pub frontend_dist: PathBuf,
    pub python_bin: PathBuf,
    pub docker_compose: PathBuf,
}

impl RuntimeLayout {
    pub fn detect(app: &AppHandle) -> Self {
        match std::env::var("JARVIS_RUNTIME_MODE").as_deref() {
            Ok("dev_repo") => return Self::dev_repo(resolve_repo_root()),
            Ok("packaged_runtime") => return Self::packaged(packaged_host_root(app)),
            _ => {}
        }

        if std::env::var("JARVIS_REPO_ROOT").is_ok() {
            return Self::dev_repo(resolve_repo_root());
        }

        if let Some(root) = bundled_host_root_from_app(app).or_else(bundled_host_root) {
            return Self::packaged(root);
        }

        Self::dev_repo(resolve_repo_root())
    }

    pub fn is_packaged(&self) -> bool {
        self.mode == RuntimeMode::PackagedRuntime
    }

    fn packaged(host_root: PathBuf) -> Self {
        Self {
            mode: RuntimeMode::PackagedRuntime,
            backend_dir: host_root.join("backend"),
            frontend_dist: host_root.join("frontend-dist"),
            python_bin: resolve_python_bin(&host_root),
            docker_compose: host_root.join("docker-compose.yml"),
            host_root,
        }
    }

    fn dev_repo(repo_root: PathBuf) -> Self {
        Self {
            mode: RuntimeMode::DevRepo,
            backend_dir: repo_root.join("backend"),
            frontend_dist: repo_root.join("frontend").join("dist"),
            python_bin: PathBuf::from("uv"),
            docker_compose: repo_root.join("docker-compose.yml"),
            host_root: repo_root.clone(),
        }
    }
}

pub fn bundled_host_root() -> Option<PathBuf> {
    if let Ok(path) = std::env::var("JARVIS_HOST_ROOT") {
        let path = PathBuf::from(path);
        if path.join("backend").join("main.py").is_file() {
            return Some(path);
        }
    }

    let manifest = PathBuf::from(env!("CARGO_MANIFEST_DIR"));
    let dev_resource = manifest.join("..").join("resources").join("host");
    if dev_resource.join("backend").join("main.py").is_file() {
        return Some(dev_resource);
    }
    None
}

pub fn bundled_host_root_from_app(app: &AppHandle) -> Option<PathBuf> {
    if let Some(resource_dir) = app.path().resource_dir().ok() {
        let host = resource_dir.join("host");
        if host.join("backend").join("main.py").is_file() {
            return Some(host);
        }
    }
    bundled_host_root()
}

fn packaged_host_root(app: &AppHandle) -> PathBuf {
    std::env::var("JARVIS_HOST_ROOT")
        .ok()
        .map(PathBuf::from)
        .or_else(|| {
            app.path()
                .resource_dir()
                .ok()
                .map(|resource_dir| resource_dir.join("host"))
        })
        .unwrap_or_else(|| PathBuf::from("host"))
}

pub fn resolve_repo_root() -> PathBuf {
    if let Ok(root) = std::env::var("JARVIS_REPO_ROOT") {
        return PathBuf::from(root);
    }

    let mut candidates = Vec::new();
    if let Ok(manifest) = std::env::var("CARGO_MANIFEST_DIR") {
        candidates.push(PathBuf::from(manifest));
    }
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd);
    }

    for start in candidates {
        let mut cursor = start.as_path();
        while let Some(parent) = cursor.parent() {
            if parent.join("backend").join("main.py").is_file() {
                return parent.to_path_buf();
            }
            cursor = parent;
        }
        if start.join("backend").join("main.py").is_file() {
            return start;
        }
    }

    PathBuf::from(".")
}

fn resolve_python_bin(host_root: &PathBuf) -> PathBuf {
    host_root.join("runtime/python/bin/python3")
}
