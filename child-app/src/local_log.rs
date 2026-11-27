use std::{
    fs::{self, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    time::Duration,
};

pub enum Event<'a> {
    SettingsOpened(&'a str),
    Connecting,
    Connected,
    Reconnecting {
        delay: Duration,
        reason: &'a str,
    },
    Unregistered,
    Stopped(Option<&'a str>),
    RunnerStart(u32),
    RunnerStop {
        pid: u32,
        reason: &'static str,
    },
    RunnerFailure,
    PolicySaved,
    IdleSaved,
    ProviderRepair {
        provider: &'a str,
        message: &'a str,
    },
    #[cfg_attr(not(windows), allow(dead_code))]
    AppUpdateFailed(&'a str),
}

pub fn diagnostic(error: &anyhow::Error) -> String {
    if error.is::<crate::link::Rejected>() {
        return crate::link::status_error(error);
    }
    error
        .chain()
        .map(|cause| match cause.downcast_ref::<io::Error>() {
            Some(error) if error.raw_os_error().is_some() => format!(
                "{} os_error={}",
                error.kind(),
                error.raw_os_error().expect("OS error code"),
            ),
            _ => cause.to_string(),
        })
        .collect::<Vec<_>>()
        .join(": ")
}

const LIMIT: u64 = 1024 * 1024;
const GENERATIONS: usize = 3;

pub fn folder(home: &Path) -> PathBuf {
    home.join("logs")
}

fn log_path(folder: &Path, generation: usize) -> PathBuf {
    folder.join(if generation == 0 {
        "child.log".to_owned()
    } else {
        format!("child.log.{generation}")
    })
}

fn append(home: &Path, line: &str) -> io::Result<()> {
    let folder = folder(home);
    let current = log_path(&folder, 0);
    if !folder.is_dir() {
        crate::state::private_dir(&folder)?;
    }
    if current
        .metadata()
        .is_ok_and(|meta| meta.len() + line.len() as u64 > LIMIT)
    {
        for generation in (1..GENERATIONS).rev() {
            let from = log_path(&folder, generation - 1);
            if from.try_exists()? {
                let to = log_path(&folder, generation);
                if to.try_exists()? {
                    fs::remove_file(&to)?;
                }
                fs::rename(from, to)?;
            }
        }
    }
    let mut open = OpenOptions::new();
    open.create(true).append(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        open.mode(0o600);
    }
    open.open(current)?.write_all(line.as_bytes())
}

pub fn record(home: &Path, event: Event<'_>) {
    let text = match event {
        Event::SettingsOpened(reason) => format!("settings opened ({reason})"),
        Event::Connecting => "connecting to host".into(),
        Event::Connected => "connected to host".into(),
        Event::Reconnecting { delay, reason } => {
            format!(
                "host link: {reason}; retrying in {:.1}s",
                delay.as_secs_f64()
            )
        }
        Event::Unregistered => "child is not enrolled".into(),
        Event::Stopped(Some(reason)) => format!("connection stopped: {reason}"),
        Event::Stopped(None) => "connection stopped".into(),
        Event::RunnerStart(pid) => format!("runner started pid={pid}"),
        Event::RunnerStop { pid, reason } => format!("runner stopped pid={pid} reason={reason}"),
        Event::RunnerFailure => "runner failed".into(),
        Event::PolicySaved => "policy saved".into(),
        Event::IdleSaved => "runner idle timeout saved".into(),
        Event::ProviderRepair { provider, message } => {
            format!("automatic provider repair [{provider}]: {message}")
        }
        Event::AppUpdateFailed(reason) => format!("app update failed: {reason}"),
    };
    let timestamp = time::OffsetDateTime::now_utc()
        .replace_nanosecond(0)
        .expect("zero nanoseconds");
    let line = format!("{timestamp} {text}\n");
    if let Err(error) = append(home, &line) {
        tracing::warn!("child log write failed: {}", error.kind());
    }
}

pub fn tail(home: &Path) -> io::Result<Vec<String>> {
    use std::{
        collections::VecDeque,
        io::{BufRead, BufReader, Read},
    };

    let mut lines = VecDeque::with_capacity(100);
    let folder = folder(home);
    for generation in (0..GENERATIONS).rev() {
        let path = log_path(&folder, generation);
        let file = match fs::File::open(path) {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            Err(error) => return Err(error),
        };
        for line in BufReader::new(file.take(LIMIT)).lines() {
            if lines.len() == 100 {
                lines.pop_front();
            }
            lines.push_back(line?);
        }
    }
    Ok(lines.into())
}
