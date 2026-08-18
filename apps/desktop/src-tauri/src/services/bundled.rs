use super::process::{
    spawn_process, stop_process_gracefully, tail_file, ManagedProcess, MONGODB_SHUTDOWN_MS,
};
use super::provider::services_root;
use crate::paths::{ensure_dir, HostPaths};
use crate::runtime::{RuntimeLayout, RuntimeMode};
use std::path::{Path, PathBuf};
use tokio::process::Command;
use tokio::time::{sleep, timeout, Duration};

const SERVICE_WAIT_SECS: u64 = 30;
const HEALTH_CHECK_TIMEOUT_SECS: u64 = 4;
pub const FAILURE_SERVICES: &str = "JARV1S could not start its local database.\n\nTry starting JARV1S again. If this keeps happening, open the debug logs from the app.";

#[derive(Debug)]
enum ServiceStartupError {
    ChildExited { detail: String },
    TimedOut { detail: String },
}

impl std::fmt::Display for ServiceStartupError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::ChildExited { detail } | Self::TimedOut { detail } => formatter.write_str(detail),
        }
    }
}

const HEALTH_CHECK_SCRIPT: &str = r#"
import asyncio
import os
from motor.motor_asyncio import AsyncIOMotorClient

async def main() -> None:
    mongo = AsyncIOMotorClient(
        os.environ["JARVIS_MONGO_URL"],
        serverSelectionTimeoutMS=1500,
    )
    try:
        await mongo.admin.command("ping")
    finally:
        mongo.close()

asyncio.run(asyncio.wait_for(main(), timeout=3))
"#;

pub struct BundledServices {
    pub mongodb_url: String,
    mongod: ManagedProcess,
}

impl BundledServices {
    pub fn summary_tuples(&self) -> Vec<(String, String, Option<String>)> {
        vec![(
            "mongodb".to_string(),
            "running".to_string(),
            Some("unix socket".to_string()),
        )]
    }
}

pub fn check_prerequisites(layout: &RuntimeLayout) -> Result<(), String> {
    if !mongod_bin(layout).is_file() {
        return Err("JARV1S is missing its bundled MongoDB server.".to_string());
    }
    Ok(())
}

pub async fn start(layout: &RuntimeLayout, paths: &HostPaths) -> Result<BundledServices, String> {
    ensure_dir(&paths.mongo_dir())?;
    ensure_dir(&paths.run_dir())?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if let Ok(metadata) = std::fs::metadata(paths.run_dir()) {
            let mut perms = metadata.permissions();
            perms.set_mode(0o700);
            let _ = std::fs::set_permissions(paths.run_dir(), perms);
        }
    }

    let use_tcp = std::env::var("JARVIS_SERVICES_USE_TCP").as_deref() == Ok("1");
    let (mongod_conf, mongodb_url) = if use_tcp {
        write_tcp_config(paths)?
    } else {
        write_socket_config(paths)?
    };

    clear_stale_socket(&paths.mongodb_socket());

    let mut mongod = spawn_process(
        "mongod",
        &mongod_bin(layout),
        &["--config", mongod_conf.to_str().unwrap_or("")],
        Some(&paths.logs_dir),
    )?;

    if let Err(error) = wait_for_service(layout, paths, &mongodb_url, &mut mongod).await {
        stop_process_gracefully(&mut mongod, MONGODB_SHUTDOWN_MS).await;
        return Err(error.to_string());
    }

    Ok(BundledServices {
        mongodb_url,
        mongod,
    })
}

pub async fn stop(bundled: &mut BundledServices) {
    stop_process_gracefully(&mut bundled.mongod, MONGODB_SHUTDOWN_MS).await;
}

fn mongod_bin(layout: &RuntimeLayout) -> PathBuf {
    services_root(&layout.host_root).join("mongodb/bin/mongod")
}

fn clear_stale_socket(path: &Path) {
    if path.exists() {
        let _ = std::fs::remove_file(path);
    }
}

fn yaml_quote(path: &Path) -> String {
    format!("'{}'", path.to_string_lossy().replace('\'', "''"))
}

fn write_socket_config(paths: &HostPaths) -> Result<(PathBuf, String), String> {
    let mongod_conf = paths.logs_dir.join("mongod.conf");
    let mongodb_url = mongo_socket_url(&paths.mongodb_socket());
    let mongod_yaml = format!(
        "storage:\n  dbPath: {db}\nsystemLog:\n  destination: file\n  path: {log}\n  logAppend: true\nnet:\n  port: 0\n  unixDomainSocket:\n    enabled: true\n    pathPrefix: {run}\n    filePermissions: 448\n",
        db = yaml_quote(&paths.mongo_dir()),
        log = yaml_quote(&paths.logs_dir.join("mongod.log")),
        run = yaml_quote(&paths.run_dir()),
    );
    std::fs::write(&mongod_conf, mongod_yaml)
        .map_err(|error| format!("Could not write mongod config: {error}"))?;
    Ok((mongod_conf, mongodb_url))
}

fn write_tcp_config(paths: &HostPaths) -> Result<(PathBuf, String), String> {
    let mongod_conf = paths.logs_dir.join("mongod.conf");
    let mongod_yaml = format!(
        "storage:\n  dbPath: {db}\nsystemLog:\n  destination: file\n  path: {log}\n  logAppend: true\nnet:\n  bindIp: 127.0.0.1\n  port: 27018\n",
        db = yaml_quote(&paths.mongo_dir()),
        log = yaml_quote(&paths.logs_dir.join("mongod.log")),
    );
    std::fs::write(&mongod_conf, mongod_yaml)
        .map_err(|error| format!("Could not write mongod config: {error}"))?;
    Ok((mongod_conf, "mongodb://127.0.0.1:27018".to_string()))
}

fn mongo_socket_url(socket_path: &Path) -> String {
    let encoded: String = urlencoding::encode(&socket_path.to_string_lossy()).into_owned();
    format!("mongodb://{encoded}")
}

async fn wait_for_service(
    layout: &RuntimeLayout,
    paths: &HostPaths,
    mongodb_url: &str,
    mongod: &mut ManagedProcess,
) -> Result<(), ServiceStartupError> {
    let python = python_bin(layout);
    let mut last_health_error = String::new();
    let readiness = async {
        loop {
            ensure_mongodb_running(paths, mongod)?;
            match mongodb_healthy(&python, mongodb_url).await {
                Ok(()) => return Ok(()),
                Err(error) => last_health_error = error,
            }
            ensure_mongodb_running(paths, mongod)?;
            sleep(Duration::from_millis(200)).await;
        }
    };

    match timeout(Duration::from_secs(SERVICE_WAIT_SECS), readiness).await {
        Ok(result) => result,
        Err(_) => {
            ensure_mongodb_running(paths, mongod)?;
            let health_detail = if last_health_error.is_empty() {
                String::new()
            } else {
                format!("\n\nLast health check: {last_health_error}")
            };
            Err(ServiceStartupError::TimedOut {
                detail: format!(
                    "MongoDB did not become ready within {SERVICE_WAIT_SECS} seconds. Check that the JARV1S database directory is writable, then try again.{health_detail}{}",
                    mongodb_logs(paths, mongod)
                ),
            })
        }
    }
}

fn python_bin(layout: &RuntimeLayout) -> PathBuf {
    match layout.mode {
        RuntimeMode::DevRepo => PathBuf::from("uv"),
        RuntimeMode::PackagedRuntime => layout.python_bin.clone(),
    }
}

async fn mongodb_healthy(python: &Path, mongodb_url: &str) -> Result<(), String> {
    run_health_check_with_timeout(
        python,
        mongodb_url,
        Duration::from_secs(HEALTH_CHECK_TIMEOUT_SECS),
    )
    .await
}

async fn run_health_check_with_timeout(
    python: &Path,
    mongodb_url: &str,
    timeout_duration: Duration,
) -> Result<(), String> {
    let mut command = if python == Path::new("uv") {
        let mut command = Command::new("uv");
        command
            .args(["run", "python", "-c", HEALTH_CHECK_SCRIPT])
            .current_dir(crate::runtime::resolve_repo_root().join("backend"));
        command
    } else {
        let mut command = Command::new(python);
        command.args(["-c", HEALTH_CHECK_SCRIPT]);
        command
    };
    command
        .env("JARVIS_MONGO_URL", mongodb_url)
        .kill_on_drop(true);

    let output = timeout(timeout_duration, command.output())
        .await
        .map_err(|_| "MongoDB health check timed out.".to_string())?
        .map_err(|error| format!("Could not run MongoDB health check: {error}"))?;

    if output.status.success() {
        Ok(())
    } else {
        let detail = String::from_utf8_lossy(&output.stderr).trim().to_string();
        Err(if detail.is_empty() {
            "MongoDB health check failed.".to_string()
        } else {
            detail
        })
    }
}

fn ensure_mongodb_running(
    paths: &HostPaths,
    process: &mut ManagedProcess,
) -> Result<(), ServiceStartupError> {
    let status = process
        .child
        .try_wait()
        .map_err(|error| ServiceStartupError::ChildExited {
            detail: format!("Could not inspect MongoDB child status: {error}"),
        })?;
    let Some(status) = status else {
        return Ok(());
    };
    Err(ServiceStartupError::ChildExited {
        detail: format!(
            "MongoDB exited during startup. Exit status: {status}. Check that the JARV1S database directory is writable, then try again.{}",
            mongodb_logs(paths, process)
        ),
    })
}

fn mongodb_logs(paths: &HostPaths, process: &ManagedProcess) -> String {
    let mut sections = Vec::new();
    for path in [
        Some(paths.logs_dir.join("mongod.log")),
        process.log_path.clone(),
    ]
    .into_iter()
    .flatten()
    {
        if let Some(tail) = tail_file(&path, 12) {
            sections.push(format!("\n\nMongoDB log ({}):\n{tail}", path.display()));
        }
    }
    sections.concat()
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::Instant;

    #[test]
    #[cfg(unix)]
    fn child_exit_error_includes_mongodb_logs() {
        let root = std::env::temp_dir().join(format!(
            "jarvis-bundled-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        let paths = HostPaths {
            data_dir: root.join("data"),
            logs_dir: root.join("logs"),
        };
        std::fs::create_dir_all(&paths.logs_dir).expect("logs");
        std::fs::write(paths.logs_dir.join("mongod.log"), "mongodb-native-detail")
            .expect("native log");

        let mut process = spawn_process(
            "mongod",
            Path::new("/bin/sh"),
            &["-c", "echo mongodb-process-detail >&2; exit 23"],
            Some(&paths.logs_dir),
        )
        .expect("spawn");
        process.child.wait().expect("wait");

        let error = ensure_mongodb_running(&paths, &mut process)
            .expect_err("child exit")
            .to_string();
        assert!(error.contains("MongoDB exited during startup"));
        assert!(error.contains("database directory is writable"));
        assert!(error.contains("mongodb-native-detail"));
        assert!(error.contains("mongodb-process-detail"));
        std::fs::remove_dir_all(root).expect("cleanup");
    }

    #[test]
    fn mongodb_configs_quote_paths_with_spaces() {
        let root =
            std::env::temp_dir().join(format!("jarvis bundled path test {}", std::process::id()));
        let paths = HostPaths {
            data_dir: root.join("application support"),
            logs_dir: root.join("service logs"),
        };
        std::fs::create_dir_all(&paths.logs_dir).expect("logs");

        let (socket_config, _) = write_socket_config(&paths).expect("socket config");
        let socket_text = std::fs::read_to_string(socket_config).expect("socket config text");
        assert!(socket_text.contains(&format!("dbPath: {}", yaml_quote(&paths.mongo_dir()))));
        assert!(socket_text.contains(&format!("pathPrefix: {}", yaml_quote(&paths.run_dir()))));

        let (tcp_config, _) = write_tcp_config(&paths).expect("tcp config");
        let tcp_text = std::fs::read_to_string(tcp_config).expect("tcp config text");
        assert!(tcp_text.contains(&format!(
            "path: {}",
            yaml_quote(&paths.logs_dir.join("mongod.log"))
        )));
        std::fs::remove_dir_all(root).expect("cleanup");
    }

    #[tokio::test]
    #[cfg(unix)]
    async fn health_check_enforces_wall_clock_timeout() {
        use std::os::unix::fs::PermissionsExt;

        let root =
            std::env::temp_dir().join(format!("jarvis health timeout {}", std::process::id()));
        std::fs::create_dir_all(&root).expect("fixture dir");
        let fixture = root.join("slow python");
        std::fs::write(&fixture, "#!/bin/sh\nexec sleep 5\n").expect("fixture");
        let mut permissions = std::fs::metadata(&fixture).expect("metadata").permissions();
        permissions.set_mode(0o700);
        std::fs::set_permissions(&fixture, permissions).expect("permissions");

        let started = Instant::now();
        let error =
            run_health_check_with_timeout(&fixture, "mongodb://unused", Duration::from_millis(50))
                .await
                .expect_err("timeout");

        assert!(error.contains("timed out"));
        assert!(started.elapsed() < Duration::from_secs(1));
        std::fs::remove_dir_all(root).expect("cleanup");
    }
}
