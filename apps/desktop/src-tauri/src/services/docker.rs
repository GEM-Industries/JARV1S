use crate::runtime::RuntimeLayout;
use std::process::{Command, Stdio};

pub const FAILURE_DOCKER: &str = "Docker Desktop is not running.\n\nJARV1S uses Docker for its contributor database. Open Docker Desktop, wait until it says it is running, then start JARV1S again.";

pub const FAILURE_DOCKER_CLI: &str = "JARV1S could not find Docker's command-line tools.\n\nDocker Desktop may be running, but the app cannot see the `docker` command. Install Docker Desktop's command-line tools or make sure Docker is available at `/usr/local/bin/docker`, `/opt/homebrew/bin/docker`, or inside Docker.app.";

pub fn check_prerequisites() -> Result<(), String> {
    if resolve_command("docker").is_none() {
        return Err(FAILURE_DOCKER_CLI.to_string());
    }
    Ok(())
}

pub fn start(layout: &RuntimeLayout) -> Result<(), String> {
    if !docker_daemon_running()? {
        return Err(FAILURE_DOCKER.to_string());
    }

    let compose_dir = layout
        .docker_compose
        .parent()
        .unwrap_or(&layout.host_root)
        .to_path_buf();

    let docker = resolve_command("docker").ok_or_else(|| FAILURE_DOCKER_CLI.to_string())?;
    let output = Command::new(docker)
        .args([
            "compose",
            "-p",
            "jarv1s",
            "-f",
            "docker-compose.yml",
            "up",
            "-d",
            "--wait",
        ])
        .current_dir(&compose_dir)
        .output()
        .map_err(|error| format!("Could not run Docker Compose: {error}"))?;

    if !output.status.success() {
        return Err(format!(
            "JARV1S could not start its local services.\n{}",
            String::from_utf8_lossy(&output.stderr).trim()
        ));
    }
    Ok(())
}

fn docker_daemon_running() -> Result<bool, String> {
    let docker = resolve_command("docker").ok_or_else(|| FAILURE_DOCKER_CLI.to_string())?;
    Command::new(docker)
        .args(["info"])
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .status()
        .map(|status| status.success())
        .map_err(|error| format!("Could not check Docker: {error}"))
}

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
        .find(|path| path.is_file())
}
