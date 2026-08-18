use std::fs::OpenOptions;
use std::path::{Path, PathBuf};
use std::process::{Child, Command, Stdio};
use tokio::time::{sleep, Duration};

pub const MONGODB_SHUTDOWN_MS: u64 = 30_000;

pub struct ManagedProcess {
    pub child: Child,
    pub pid: u32,
    pub log_path: Option<PathBuf>,
}

pub fn spawn_process(
    name: impl Into<String>,
    program: &Path,
    args: &[&str],
    log_dir: Option<&Path>,
) -> Result<ManagedProcess, String> {
    let name = name.into();
    let mut cmd = Command::new(program);
    cmd.args(args);
    let log_path = match log_dir {
        Some(log_dir) => {
            let path = log_dir.join(format!("{name}.process.log"));
            let stdout = OpenOptions::new()
                .create(true)
                .append(true)
                .open(&path)
                .map_err(|error| format!("Could not open {} logs: {error}", name))?;
            let stderr = stdout
                .try_clone()
                .map_err(|error| format!("Could not open {} error logs: {error}", name))?;
            cmd.stdout(Stdio::from(stdout)).stderr(Stdio::from(stderr));
            Some(path)
        }
        None => {
            cmd.stdout(Stdio::null()).stderr(Stdio::null());
            None
        }
    };

    #[cfg(unix)]
    {
        use std::os::unix::process::CommandExt;
        cmd.process_group(0);
    }

    let child = cmd
        .spawn()
        .map_err(|error| format!("Could not start {name}: {error}"))?;
    let pid = child.id();
    Ok(ManagedProcess {
        child,
        pid,
        log_path,
    })
}

pub fn tail_file(path: &Path, max_lines: usize) -> Option<String> {
    let contents = std::fs::read_to_string(path).ok()?;
    let lines = contents.lines().collect::<Vec<_>>();
    let start = lines.len().saturating_sub(max_lines);
    let tail = lines[start..].join("\n");
    (!tail.trim().is_empty()).then_some(tail)
}

pub async fn stop_process_gracefully(process: &mut ManagedProcess, graceful_ms: u64) {
    #[cfg(unix)]
    {
        let pgid = process.pid as i32;
        unsafe {
            libc::kill(-pgid, libc::SIGTERM);
        }
        let steps = graceful_ms.div_ceil(100).max(1);
        for _ in 0..steps {
            if process.child.try_wait().ok().flatten().is_some() {
                return;
            }
            sleep(Duration::from_millis(100)).await;
        }
        if process.child.try_wait().ok().flatten().is_none() {
            unsafe {
                libc::kill(-pgid, libc::SIGKILL);
            }
            let _ = process.child.kill();
        }
    }

    #[cfg(not(unix))]
    {
        let _ = process.child.kill();
    }

    let _ = process.child.wait();
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    #[cfg(unix)]
    fn spawn_process_retains_output_in_service_log() {
        let log_dir = std::env::temp_dir().join(format!(
            "jarvis-process-test-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .expect("clock")
                .as_nanos()
        ));
        std::fs::create_dir_all(&log_dir).expect("log dir");

        let mut process = spawn_process(
            "fixture",
            Path::new("/bin/sh"),
            &["-c", "echo retained-output >&2"],
            Some(&log_dir),
        )
        .expect("spawn");
        assert!(process.child.wait().expect("wait").success());

        let log_path = process.log_path.expect("log path");
        assert_eq!(tail_file(&log_path, 5).as_deref(), Some("retained-output"));
        std::fs::remove_dir_all(log_dir).expect("cleanup");
    }

    #[test]
    #[cfg(unix)]
    fn spawned_process_owns_its_process_group() {
        let mut process = spawn_process(
            "group-owner",
            Path::new("/bin/sh"),
            &["-c", "sleep 30"],
            None,
        )
        .expect("spawn");

        let process_group = unsafe { libc::getpgid(process.pid as i32) };
        assert_eq!(process_group, process.pid as i32);

        unsafe {
            libc::kill(-(process.pid as i32), libc::SIGKILL);
        }
        let _ = process.child.wait();
    }
}
