use std::{
    collections::BTreeMap,
    fs, io,
    path::{Path, PathBuf},
    process::{ExitStatus, Stdio},
    sync::OnceLock,
    time::{Duration, SystemTime},
};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;
use tokio::{
    io::{AsyncBufReadExt, BufReader},
    process::{Child, Command},
    sync::{Mutex, RwLock, RwLockWriteGuard, mpsc, oneshot},
    time::{Instant, timeout},
};

use crate::{messages, parts, state};

mod auto_repair;
pub use auto_repair::{AutoRepairs, RepairTask};

#[derive(Clone, Deserialize, Serialize)]
pub struct Provider {
    pub id: String,
    pub name: String,
    initial: String,
    tone: String,
    pub driver: String,
    pub packages: Vec<String>,
    aliases: Vec<String>,
    provider: Option<String>,
    login: Option<Login>,
    quota: bool,
    managed: Option<ManagedCli>,
    #[serde(skip_deserializing)]
    local_login: bool,
}

#[derive(Clone, Deserialize, Serialize)]
struct Login {
    command: Vec<String>,
    cli_name: String,
    #[serde(skip_serializing)]
    transports: BTreeMap<String, String>,
    status: Vec<String>,
}

#[derive(Clone, Deserialize, Serialize)]
struct ManagedCli {
    version: String,
}

#[derive(Deserialize)]
struct Catalog {
    version: String,
    providers: Vec<Provider>,
}

fn catalog() -> &'static Catalog {
    static CATALOG: OnceLock<Catalog> = OnceLock::new();
    CATALOG.get_or_init(|| {
        let mut catalog: Catalog = serde_json::from_str(include_str!(concat!(
            env!("OUT_DIR"),
            "/child_components.json"
        )))
        .expect("valid child components catalog");
        let platform = if cfg!(windows) {
            "win32"
        } else {
            std::env::consts::OS
        };
        for provider in &mut catalog.providers {
            provider.local_login = provider
                .login
                .as_ref()
                .is_some_and(|login| login.transports.contains_key(platform));
        }
        catalog
    })
}

pub fn providers() -> Vec<Provider> {
    catalog().providers.clone()
}

pub fn manages_adapter(driver: &str) -> bool {
    catalog()
        .providers
        .iter()
        .any(|provider| !provider.packages.is_empty() && provider.driver == driver)
}

pub fn provider(id: &str) -> Option<Provider> {
    catalog().providers.iter().find(|row| row.id == id).cloned()
}

pub fn command(program: &Path) -> Command {
    let mut command = Command::new(program);
    command.stdin(Stdio::null()).kill_on_drop(true);
    #[cfg(windows)]
    command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    command
}

#[derive(Deserialize, Serialize)]
pub struct Probe {
    id: String,
    #[serde(skip_serializing)]
    catalog_version: String,
    packages: Value,
    system_cli: bool,
    system_version: Option<String>,
    bundled_cli: bool,
    choice: state::CliChoice,
    turns_supported: bool,
    sdk_importable: bool,
    managed_available: bool,
}

pub static COMPONENT_LOCK: RwLock<()> = RwLock::const_new(());

#[derive(Clone, Copy)]
pub enum ProbeMode {
    Availability,
    Status,
    Check,
}

async fn probe(home: &Path, python: &Path, mode: ProbeMode) -> Result<Vec<Probe>> {
    let (arg, seconds) = match mode {
        ProbeMode::Availability => ("availability", 15),
        ProbeMode::Status => ("status", 35),
        ProbeMode::Check => ("check", 35),
    };
    let child = command(python)
        .args(["-I", "-m", "kotoyose.child_runner", "--home"])
        .arg(home)
        .args(["--components", arg])
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()?;
    #[cfg(target_os = "linux")]
    let _tracked = crate::process::ManagedChild::track(child.id().expect("spawned probe PID"));
    let output = timeout(Duration::from_secs(seconds), child.wait_with_output()).await??;
    ensure!(output.status.success(), "runner components probe failed");
    let rows: Vec<Probe> = serde_json::from_slice(&output.stdout)?;
    let catalog = catalog();
    ensure!(
        rows.len() == catalog.providers.len()
            && rows
                .iter()
                .all(|row| row.catalog_version == catalog.version)
            && catalog
                .providers
                .iter()
                .all(|provider| rows.iter().any(|row| row.id == provider.id)),
        "runner components probe does not match the embedded catalog"
    );
    Ok(rows)
}

fn detected_adapters(rows: &[Probe], python: &Path) -> Vec<String> {
    providers()
        .into_iter()
        .filter(|provider| {
            !provider.packages.is_empty()
                && !needs_reinstall(python, &provider.id)
                && rows.iter().any(|row| {
                    row.id == provider.id
                        && row.turns_supported
                        && row.sdk_importable
                        && match row.choice {
                            state::CliChoice::Bundled => row.bundled_cli,
                            state::CliChoice::System => row.system_cli,
                        }
                })
        })
        .map(|row| row.driver)
        .collect()
}

#[derive(Debug)]
pub enum AvailabilityError {
    Busy,
    Failed,
}

impl std::fmt::Display for AvailabilityError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(match self {
            Self::Busy => messages::COMPONENT_BUSY,
            Self::Failed => messages::COMPONENT_PROBE_FAILED,
        })
    }
}

impl std::error::Error for AvailabilityError {}

#[derive(PartialEq)]
struct ProbeStamp {
    python: Option<PathBuf>,
    files: Vec<(PathBuf, Option<SystemTime>)>,
}

impl ProbeStamp {
    fn read(home: &Path, python: Option<&Path>, sites: &[PathBuf]) -> Self {
        let files = std::iter::once(home.join("app.json"))
            .chain(sites.iter().cloned())
            .map(|path| {
                let modified = path.metadata().and_then(|meta| meta.modified()).ok();
                (path, modified)
            })
            .collect();
        Self {
            python: python.map(Path::to_owned),
            files,
        }
    }
}

struct CachedAvailability {
    home: PathBuf,
    stamp: Option<ProbeStamp>,
    last_good: Option<Vec<String>>,
    retry_at: Option<Instant>,
}

static AVAILABILITY: Mutex<Option<CachedAvailability>> = Mutex::const_new(None);

pub async fn cached_adapters(home: &Path, python: Option<&Path>) -> Vec<String> {
    let cache = AVAILABILITY.lock().await;
    cache
        .as_ref()
        .filter(|cached| cached.home == home)
        .and_then(|cached| cached.last_good.as_ref())
        .into_iter()
        .flatten()
        .filter(|driver| !driver_needs_reinstall(python, driver))
        .cloned()
        .collect()
}

pub async fn available(
    home: &Path,
    python: Option<&Path>,
    sites: &[PathBuf],
) -> Result<Vec<String>, AvailabilityError> {
    let _guard = COMPONENT_LOCK
        .try_read()
        .map_err(|_| AvailabilityError::Busy)?;
    let mut cache = AVAILABILITY.lock().await;
    let cached = cache.get_or_insert_with(|| CachedAvailability {
        home: home.to_owned(),
        stamp: None,
        last_good: None,
        retry_at: None,
    });
    if cached.home != home {
        cached.home = home.to_owned();
        cached.stamp = None;
        cached.last_good = None;
    }
    let stamp = ProbeStamp::read(home, python, sites);
    if cached.stamp.as_ref() != Some(&stamp)
        || cached.retry_at.is_some_and(|retry| Instant::now() >= retry)
    {
        let result = match python {
            Some(python) => probe(home, python, ProbeMode::Availability)
                .await
                .map(|rows| detected_adapters(&rows, python)),
            None => Err(anyhow::anyhow!("runner Python unavailable")),
        };
        cached.stamp = Some(stamp);
        match result {
            Ok(adapters) => {
                cached.last_good = Some(adapters);
                cached.retry_at = None;
            }
            Err(error) => {
                tracing::debug!("component availability probe: {error}");
                cached.retry_at = Some(Instant::now() + Duration::from_secs(5));
            }
        }
    }
    cached.last_good.clone().ok_or(AvailabilityError::Failed)
}

#[derive(Serialize)]
pub struct Status {
    pub providers: Vec<Provider>,
    pub installed: Vec<Probe>,
    pub can_manage_packages: bool,
    pub needs_reinstall: Vec<String>,
    auto_repair: BTreeMap<String, auto_repair::RepairStatus>,
    pub error: Option<&'static str>,
}

#[derive(Clone)]
pub struct Runtime {
    pub python: PathBuf,
    pub payload: PathBuf,
}

impl Runtime {
    pub fn installed() -> Option<Self> {
        let exe = std::env::current_exe().ok()?;
        Some(Self {
            python: state::bundled_python(&exe)?,
            payload: exe.with_file_name("parts"),
        })
    }

    fn ready(&self) -> bool {
        self.python.with_file_name("kotoyose-ready").is_file()
    }
}

fn reinstall_marker(python: &Path, provider: &str) -> PathBuf {
    python.with_file_name(format!("{provider}.needs-reinstall"))
}

pub fn needs_reinstall(python: &Path, provider: &str) -> bool {
    reinstall_marker(python, provider).is_file()
}

pub fn driver_needs_reinstall(python: Option<&Path>, driver: &str) -> bool {
    python.is_some_and(|python| {
        catalog().providers.iter().any(|provider| {
            provider.driver == driver
                && !provider.packages.is_empty()
                && needs_reinstall(python, &provider.id)
        })
    })
}

pub fn mark_for_reinstall(python: &Path, provider: &str) -> io::Result<()> {
    fs::write(reinstall_marker(python, provider), [])
}

pub fn clear_reinstall(python: &Path, provider: &str) -> io::Result<()> {
    match fs::remove_file(reinstall_marker(python, provider)) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        result => result,
    }
}

#[cfg(any(windows, target_os = "linux", test))]
fn has_provider_metadata(entries: &[String], provider: &Provider) -> bool {
    provider.packages.iter().any(|package| {
        let prefix = format!("{}-", package.replace('-', "_"));
        entries
            .iter()
            .any(|entry| entry.starts_with(&prefix) && entry.ends_with(".dist-info"))
    })
}

#[cfg(any(windows, target_os = "linux", test))]
pub fn carry_provider_repairs(previous: &Path, python: &Path, sites: &[PathBuf]) -> io::Result<()> {
    let mut entries = Vec::new();
    for site in sites {
        let directory = match fs::read_dir(site) {
            Err(error) if error.kind() == io::ErrorKind::NotFound => continue,
            result => result?,
        };
        for entry in directory {
            entries.push(entry?.file_name().to_string_lossy().to_ascii_lowercase());
        }
    }
    for provider in providers() {
        if !provider.packages.is_empty()
            && (needs_reinstall(previous, &provider.id)
                || has_provider_metadata(&entries, &provider))
        {
            mark_for_reinstall(python, &provider.id)?;
        }
    }
    Ok(())
}

pub async fn status(
    home: &Path,
    runtime: Option<&Runtime>,
    mode: ProbeMode,
    repairs: &AutoRepairs,
) -> Status {
    let providers = providers();
    // A busy response must keep polling if it raced with the end of a repair.
    let auto_repair = repairs.snapshot();
    let Ok(_guard) = COMPONENT_LOCK.try_read() else {
        return Status {
            providers,
            installed: Vec::new(),
            can_manage_packages: false,
            needs_reinstall: Vec::new(),
            auto_repair,
            error: Some(messages::COMPONENT_BUSY),
        };
    };
    let needs_reinstall: Vec<_> = providers
        .iter()
        .filter(|provider| {
            !provider.packages.is_empty()
                && runtime.is_some_and(|runtime| needs_reinstall(&runtime.python, &provider.id))
        })
        .map(|provider| provider.id.clone())
        .collect();
    let can_manage_packages = runtime.is_some_and(Runtime::ready);
    let python = runtime
        .filter(|runtime| runtime.ready())
        .map(|runtime| runtime.python.clone())
        .or_else(|| state::python().ok());
    let result = match python {
        Some(python) => probe(home, &python, mode).await.map(|mut rows| {
            for row in &mut rows {
                if let Some(version) = &mut row.system_version {
                    *version =
                        crate::component_output::redact(version, state::python_root(&python));
                }
            }
            rows
        }),
        None => Err(anyhow::anyhow!("runner Python unavailable")),
    };
    let auto_repair = repairs.reconcile(&needs_reinstall);
    Status {
        providers,
        can_manage_packages,
        needs_reinstall,
        auto_repair,
        error: result
            .as_ref()
            .err()
            .map(|_| messages::COMPONENT_PROBE_FAILED),
        installed: result.unwrap_or_default(),
    }
}

#[derive(Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum Change {
    Install,
    Remove,
    UseCli(state::CliChoice),
}

#[derive(Serialize)]
#[serde(tag = "event", rename_all = "snake_case")]
pub enum Event {
    Progress { message: String },
    Result { ok: bool, message: String },
}

pub struct Job {
    pub runtime: Option<Runtime>,
    pub provider: Provider,
    pub change: Change,
    pub progress: mpsc::Sender<String>,
    pub result: oneshot::Sender<Event>,
    pub lock: RwLockWriteGuard<'static, ()>,
}

impl Job {
    pub async fn run(self, home: &Path) {
        let result = self.apply(home).await;
        let (ok, message) = match result {
            Ok(message) => (true, message.to_owned()),
            Err(error) => (false, error.to_string()),
        };
        invalidate_availability().await;
        drop(self.lock);
        drop(self.progress);
        let _ = self.result.send(Event::Result { ok, message });
    }

    async fn apply(&self, home: &Path) -> Result<&'static str> {
        match self.change {
            Change::Install | Change::Remove if self.provider.packages.is_empty() => {
                ensure!(self.provider.managed.is_some(), messages::COMPONENT_UNKNOWN);
                let python = state::python()?;
                let action = if matches!(self.change, Change::Install) {
                    "install"
                } else {
                    "remove"
                };
                let status = timeout(
                    PACKAGE_TIMEOUT,
                    command(&python)
                        .args(["-I", "-m", "kotoyose.child_tools"])
                        .arg(home)
                        .args([&self.provider.id, action])
                        .stdout(Stdio::null())
                        .stderr(Stdio::null())
                        .status(),
                )
                .await
                .context(messages::COMPONENT_TIMEOUT)??;
                ensure!(status.success(), messages::COMPONENT_CLI_FAILED);
                Ok(messages::COMPONENT_CLI_UPDATED)
            }
            Change::Install | Change::Remove => {
                let runtime = self
                    .runtime
                    .as_ref()
                    .filter(|runtime| runtime.ready())
                    .context(messages::COMPONENT_RUNTIME_REQUIRED)?;
                let pip = if matches!(self.change, Change::Install) {
                    install_command(runtime, &self.provider)?
                } else {
                    let mut pip = Command::from(parts::pip(&runtime.python));
                    pip.args(["uninstall", "--yes"])
                        .args(&self.provider.packages);
                    pip
                };
                let child = start_package_change(runtime, &self.provider, pip)?;
                finish_package_change(
                    runtime,
                    &self.provider,
                    child,
                    |message| {
                        let _ = self.progress.try_send(message.to_owned());
                        Ok(())
                    },
                    std::future::pending(),
                )
                .await
            }
            Change::UseCli(choice) => {
                let _lock = state::SAVE_LOCK.lock().await;
                let (revision, settings) = state::settings_revision(home);
                let mut settings = settings.context(messages::SETTINGS_LOAD)?;
                settings.extra.insert(
                    format!("{}_cli", self.provider.id),
                    serde_json::json!(choice),
                );
                settings
                    .save(home, &revision)
                    .context(messages::SETTINGS_SAVE)?;
                Ok(messages::COMPONENT_CLI_CHANGED)
            }
        }
    }
}

async fn invalidate_availability() {
    if let Some(cached) = AVAILABILITY.lock().await.as_mut() {
        cached.stamp = None;
    }
}

pub const PACKAGE_TIMEOUT: Duration = Duration::from_secs(900);

fn install_command(runtime: &Runtime, provider: &Provider) -> Result<Command> {
    ensure!(
        !provider.packages.is_empty(),
        messages::COMPONENT_PINS_MISSING
    );
    let requirements = runtime
        .payload
        .join(format!("requirements-{}.txt", provider.id));
    ensure!(requirements.is_file(), messages::COMPONENT_PINS_MISSING);
    let mut pip = Command::from(parts::pip(&runtime.python));
    pip.args(parts::INSTALL_LOCKED).arg(requirements);
    Ok(pip)
}

fn start_package_change(runtime: &Runtime, provider: &Provider, pip: Command) -> Result<Pip> {
    mark_for_reinstall(&runtime.python, &provider.id).context(messages::COMPONENT_REPAIR_STATE)?;
    start_pip(pip)
}

async fn finish_package_change(
    runtime: &Runtime,
    provider: &Provider,
    pip: Pip,
    output: impl Fn(&str) -> io::Result<()>,
    cancelled: impl Future<Output = ()>,
) -> Result<&'static str> {
    let status = run_pip(
        pip,
        state::python_root(&runtime.python),
        output,
        Some(PACKAGE_TIMEOUT),
        cancelled,
    )
    .await?;
    ensure!(status.success(), messages::COMPONENT_PIP_FAILED);
    clear_reinstall(&runtime.python, &provider.id).context(messages::COMPONENT_REPAIR_STATE)?;
    Ok(messages::COMPONENT_PACKAGES_CHANGED)
}

pub struct Pip {
    child: Child,
    #[cfg(target_os = "linux")]
    _tracked: crate::process::ManagedChild,
}

pub fn start_pip(mut pip: Command) -> Result<Pip> {
    let child = pip
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
        .context(messages::PYTHON_START)?;
    Ok(Pip {
        #[cfg(target_os = "linux")]
        _tracked: crate::process::ManagedChild::track(child.id().expect("spawned pip PID")),
        child,
    })
}

pub async fn run_pip(
    mut pip: Pip,
    runtime_root: Option<&Path>,
    output: impl Fn(&str) -> io::Result<()>,
    deadline: Option<Duration>,
    cancelled: impl Future<Output = ()>,
) -> Result<ExitStatus> {
    let child = &mut pip.child;
    let stdout = child.stdout.take().expect("pip stdout");
    let stderr = child.stderr.take().expect("pip stderr");
    let wait = async {
        let (exit, out, err) = tokio::join!(
            child.wait(),
            progress(stdout, runtime_root, &output),
            progress(stderr, runtime_root, &output),
        );
        out?;
        err?;
        exit.context(messages::COMPONENT_PIP_FAILED)
    };
    let result = tokio::select! {
        biased;
        _ = cancelled => Err(messages::COMPONENT_CANCELLED),
        result = async {
            match deadline {
                Some(deadline) => timeout(deadline, wait).await.map_err(|_| messages::COMPONENT_TIMEOUT),
                None => Ok(wait.await),
            }
        } => result,
    };
    match result {
        Ok(exit) => exit,
        Err(message) => {
            // Keep the component lock until pip has stopped changing files.
            let _ = child.kill().await;
            let _ = timeout(Duration::from_secs(15), child.wait()).await;
            anyhow::bail!(message);
        }
    }
}

async fn progress(
    input: impl tokio::io::AsyncRead + Unpin,
    runtime_root: Option<&Path>,
    output: &impl Fn(&str) -> io::Result<()>,
) -> io::Result<()> {
    let mut reader = BufReader::new(input);
    let mut line = Vec::new();
    while reader.read_until(b'\n', &mut line).await? > 0 {
        let decoded = String::from_utf8_lossy(&line);
        let message =
            crate::component_output::redact(decoded.trim_end_matches(['\r', '\n']), runtime_root);
        output(&message)?;
        line.clear();
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn previous_provider_metadata_and_markers_are_carried() {
        let root = tempfile::tempdir().unwrap();
        let previous = root.path().join("0.0.1").join("python").join("python.exe");
        let python = root.path().join("1.2.0").join("python").join("python.exe");
        let site = previous.with_file_name("Lib").join("site-packages");
        std::fs::create_dir_all(&site).unwrap();
        std::fs::create_dir_all(python.parent().unwrap()).unwrap();
        std::fs::create_dir(site.join("claude_agent_sdk-9.9.0.dist-info")).unwrap();
        mark_for_reinstall(&previous, "codex").unwrap();

        carry_provider_repairs(&previous, &python, &[site]).unwrap();

        assert!(needs_reinstall(&python, "claude"));
        assert!(needs_reinstall(&python, "codex"));
        clear_reinstall(&python, "claude").unwrap();
        assert!(!needs_reinstall(&python, "claude"));
    }
}
