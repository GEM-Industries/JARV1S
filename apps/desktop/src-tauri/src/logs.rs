use chrono::{DateTime, Utc};
use serde::{Deserialize, Serialize};
use std::collections::VecDeque;
use std::fs::{File, OpenOptions};
use std::io::{BufRead, BufReader, Write};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};

const MAX_BUFFER_LINES: usize = 500;
const MAX_LOG_LINE_BYTES: usize = 16 * 1024;
const MAX_LOG_FILE_BYTES: u64 = 5 * 1024 * 1024;
const MAX_LOG_ARCHIVES: usize = 3;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct LogLine {
    pub ts: DateTime<Utc>,
    pub source: String,
    pub message: String,
}

#[derive(Clone)]
pub struct LogStore {
    inner: Arc<Mutex<LogStoreInner>>,
}

struct LogStoreInner {
    buffer: VecDeque<LogLine>,
    log_file: Option<File>,
    log_path: PathBuf,
    log_file_bytes: u64,
    max_file_bytes: u64,
    max_archives: usize,
}

impl LogStore {
    pub fn new(logs_dir: &Path) -> Self {
        Self::new_with_limits(logs_dir, MAX_LOG_FILE_BYTES, MAX_LOG_ARCHIVES)
    }

    fn new_with_limits(logs_dir: &Path, max_file_bytes: u64, max_archives: usize) -> Self {
        let log_path = logs_dir.join("host.log");
        let _ = prune_log_archives(&log_path, max_archives);
        if log_path
            .metadata()
            .map(|metadata| metadata.len() >= max_file_bytes)
            .unwrap_or(false)
        {
            let _ = rotate_log_files(&log_path, max_archives);
        }
        let log_file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(&log_path)
            .ok();
        let log_file_bytes = log_file
            .as_ref()
            .and_then(|file| file.metadata().ok())
            .map(|metadata| metadata.len())
            .unwrap_or(0);

        Self {
            inner: Arc::new(Mutex::new(LogStoreInner {
                buffer: VecDeque::with_capacity(MAX_BUFFER_LINES),
                log_file,
                log_path,
                log_file_bytes,
                max_file_bytes,
                max_archives,
            })),
        }
    }

    pub fn append(&self, source: impl Into<String>, message: impl Into<String>) {
        let source = sanitize_line(&source.into(), 128);
        let message = sanitize_line(&message.into(), MAX_LOG_LINE_BYTES);
        let line = LogLine {
            ts: Utc::now(),
            source,
            message,
        };
        eprintln!("[{}] {}", line.source, line.message);

        let mut guard = self.inner.lock().expect("log store lock");
        if guard.buffer.len() >= MAX_BUFFER_LINES {
            guard.buffer.pop_front();
        }
        guard.buffer.push_back(line.clone());

        let rendered = format!(
            "{} [{}] {}\n",
            line.ts.to_rfc3339(),
            line.source,
            line.message
        );
        let rendered_bytes = rendered.len() as u64;
        if guard.log_file_bytes > 0
            && guard.log_file_bytes.saturating_add(rendered_bytes) > guard.max_file_bytes
        {
            guard.log_file = None;
            if rotate_log_files(&guard.log_path, guard.max_archives).is_ok() {
                guard.log_file = OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(&guard.log_path)
                    .ok();
                guard.log_file_bytes = 0;
            } else {
                guard.log_file = OpenOptions::new().append(true).open(&guard.log_path).ok();
            }
        }

        if let Some(file) = guard.log_file.as_mut() {
            if file.write_all(rendered.as_bytes()).is_ok() {
                guard.log_file_bytes = guard.log_file_bytes.saturating_add(rendered_bytes);
            }
        }
    }

    pub fn snapshot(&self) -> Vec<LogLine> {
        self.inner
            .lock()
            .expect("log store lock")
            .buffer
            .iter()
            .cloned()
            .collect()
    }

    pub fn spawn_reader(&self, source: &'static str, stream: impl std::io::Read + Send + 'static) {
        let store = self.clone();
        std::thread::spawn(move || {
            let reader = BufReader::new(stream);
            for line in reader.lines().map_while(Result::ok) {
                store.append(source, line);
            }
        });
    }
}

fn sanitize_line(value: &str, max_bytes: usize) -> String {
    let mut sanitized = String::with_capacity(value.len().min(max_bytes));
    for character in value.chars() {
        let replacement = if character.is_control() {
            ' '
        } else {
            character
        };
        if sanitized.len() + replacement.len_utf8() > max_bytes {
            while sanitized.len() + '…'.len_utf8() > max_bytes {
                sanitized.pop();
            }
            if '…'.len_utf8() <= max_bytes {
                sanitized.push('…');
            }
            break;
        }
        sanitized.push(replacement);
    }
    sanitized
}

fn rotate_log_files(log_path: &Path, max_archives: usize) -> std::io::Result<()> {
    if max_archives == 0 {
        if log_path.exists() {
            std::fs::remove_file(log_path)?;
        }
        return Ok(());
    }

    let oldest = archive_path(log_path, max_archives);
    if oldest.exists() {
        std::fs::remove_file(oldest)?;
    }
    for index in (1..max_archives).rev() {
        let from = archive_path(log_path, index);
        if from.exists() {
            std::fs::rename(from, archive_path(log_path, index + 1))?;
        }
    }
    if log_path.exists() {
        std::fs::rename(log_path, archive_path(log_path, 1))?;
    }
    Ok(())
}

fn archive_path(log_path: &Path, index: usize) -> PathBuf {
    log_path.with_file_name(format!(
        "{}.{index}",
        log_path
            .file_name()
            .and_then(|name| name.to_str())
            .unwrap_or("host.log")
    ))
}

fn prune_log_archives(log_path: &Path, max_archives: usize) -> std::io::Result<()> {
    let Some(directory) = log_path.parent() else {
        return Ok(());
    };
    let Some(filename) = log_path.file_name().and_then(|name| name.to_str()) else {
        return Ok(());
    };
    let prefix = format!("{filename}.");
    for entry in std::fs::read_dir(directory)? {
        let entry = entry?;
        let name = entry.file_name();
        let Some(name) = name.to_str() else {
            continue;
        };
        let Some(index) = name
            .strip_prefix(&prefix)
            .and_then(|suffix| suffix.parse::<usize>().ok())
        else {
            continue;
        };
        if index == 0 || index > max_archives {
            std::fs::remove_file(entry.path())?;
        }
    }
    Ok(())
}

pub fn open_logs_dir(logs_dir: &PathBuf) -> Result<(), String> {
    if !logs_dir.is_dir() {
        return Err("Log directory does not exist yet.".to_string());
    }

    #[cfg(target_os = "macos")]
    {
        std::process::Command::new("open")
            .arg(logs_dir)
            .spawn()
            .map_err(|error| format!("Could not open logs folder: {error}"))?;
        return Ok(());
    }

    #[cfg(not(target_os = "macos"))]
    {
        Err("open_logs is only supported on macOS.".to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::time::{SystemTime, UNIX_EPOCH};

    fn test_dir() -> PathBuf {
        let suffix = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .expect("clock")
            .as_nanos();
        let path = std::env::temp_dir().join(format!("jarvis-log-test-{suffix}"));
        std::fs::create_dir_all(&path).expect("create test directory");
        path
    }

    #[test]
    fn bounds_memory_and_rotates_host_log() {
        let directory = test_dir();
        let store = LogStore::new_with_limits(&directory, 120, 2);

        for index in 0..(MAX_BUFFER_LINES + 20) {
            store.append("test", format!("line-{index:04}-with-padding"));
        }

        assert_eq!(store.snapshot().len(), MAX_BUFFER_LINES);
        assert!(directory.join("host.log").exists());
        assert!(directory.join("host.log.1").exists());
        assert!(directory.join("host.log.2").exists());
        assert!(!directory.join("host.log.3").exists());
        std::fs::remove_dir_all(directory).expect("remove test directory");
    }

    #[test]
    fn sanitizes_control_characters_and_large_messages() {
        let directory = test_dir();
        let store = LogStore::new(&directory);
        store.append(
            "back\nend",
            format!("secret\r\n{}", "x".repeat(MAX_LOG_LINE_BYTES * 2)),
        );

        let line = store.snapshot().pop().expect("captured line");
        assert_eq!(line.source, "back end");
        assert!(!line.message.contains('\n'));
        assert!(line.message.len() <= MAX_LOG_LINE_BYTES);
        std::fs::remove_dir_all(directory).expect("remove test directory");
    }
}
