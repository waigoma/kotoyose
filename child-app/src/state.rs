use crate::messages;
use std::{
    collections::BTreeMap,
    env, fs,
    fs::{File, OpenOptions},
    io::{self, Write},
    path::{Path, PathBuf},
    time::Duration,
};

use anyhow::{Context, Result, anyhow, ensure};
use serde::{Deserialize, Serialize};
use serde_json::{Map, Value, json};

#[cfg(windows)]
#[path = "state_windows.rs"]
mod windows;

pub fn home() -> Result<PathBuf> {
    let nonempty = |key| env::var(key).ok().filter(|value| !value.trim().is_empty());
    let profile = nonempty(if cfg!(windows) { "USERPROFILE" } else { "HOME" })
        .map(|value| PathBuf::from(value.trim()))
        .or_else(std::env::home_dir)
        .filter(|path| !path.as_os_str().is_empty())
        .ok_or_else(|| anyhow!("user home is unavailable"));
    let path = if let Some(path) = nonempty("KOTOYOSE_CHILD_HOME") {
        let path = path.trim();
        if path == "~" {
            profile?
        } else if let Some(rest) = path.strip_prefix("~/").or_else(|| {
            if cfg!(windows) {
                path.strip_prefix("~\\")
            } else {
                None
            }
        }) {
            profile?.join(rest)
        } else {
            PathBuf::from(path)
        }
    } else if cfg!(windows) {
        match nonempty("LOCALAPPDATA") {
            Some(path) => PathBuf::from(path.trim()),
            None => profile?.join("AppData").join("Local"),
        }
        .join("kotoyose-child")
    } else {
        profile?.join(".local").join("share").join("kotoyose-child")
    };
    Ok(std::path::absolute(path)?)
}

pub fn private_dir(path: &Path) -> io::Result<()> {
    let mut builder = fs::DirBuilder::new();
    builder.recursive(true);
    #[cfg(unix)]
    {
        use std::os::unix::fs::{DirBuilderExt, PermissionsExt};
        builder.mode(0o700).create(path)?;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))
    }
    #[cfg(windows)]
    {
        builder.create(path)?;
        // The profile ACL is not ours to rewrite. The child home breaks inheritance
        // so sandbox ACEs on LocalAppData do not grant access to child state.
        if home().ok().is_some_and(|home| home == path) {
            windows::own_child_home(path)?;
        }
        Ok(())
    }
}

pub fn write_private(path: &Path, contents: &[u8]) -> Result<()> {
    write_atomic(path, contents, |_| Ok(()))
}

pub fn write_secret(path: &Path, contents: &[u8]) -> Result<()> {
    write_atomic(path, contents, |temp| {
        if let Err(error) = protect_secret(temp) {
            tracing::warn!("child secret file ACL update failed: {}", error.kind());
        }
        Ok(())
    })
}

pub fn protect_secret(path: &Path) -> io::Result<()> {
    #[cfg(windows)]
    return windows::restrict_to_user(path);
    #[cfg(unix)]
    {
        let _ = path;
        Ok(())
    }
}

fn write_atomic(
    path: &Path,
    contents: &[u8],
    protect: impl FnOnce(&Path) -> io::Result<()>,
) -> Result<()> {
    replace_checked(path, contents, protect, || Ok(()))?;
    #[cfg(unix)]
    File::open(path.parent().expect("state file has directory"))?.sync_all()?;
    Ok(())
}

pub fn replace_checked(
    path: &Path,
    contents: &[u8],
    protect: impl FnOnce(&Path) -> io::Result<()>,
    check: impl FnOnce() -> Result<()>,
) -> Result<()> {
    let parent = path.parent().expect("state file has directory");
    let mut tmp = tempfile::NamedTempFile::new_in(parent)?;
    protect(tmp.path())?;
    tmp.write_all(contents)?;
    tmp.as_file().sync_all()?;
    check()?;
    tmp.persist(path)?;
    Ok(())
}

pub fn revision(path: &Path) -> (String, io::Result<Option<Vec<u8>>>) {
    match fs::read(path) {
        Ok(raw) => (crate::link::fingerprint(&raw), Ok(Some(raw))),
        Err(error) => {
            let metadata = fs::symlink_metadata(path);
            if error.kind() == io::ErrorKind::NotFound
                && metadata
                    .as_ref()
                    .is_err_and(|e| e.kind() == io::ErrorKind::NotFound)
            {
                return ("missing".into(), Ok(None));
            }
            // Fall back to the file's size and mtime.
            let marker = metadata.ok().map(|meta| (meta.len(), meta.modified().ok()));
            (
                format!("unreadable:{:?}:{marker:?}", error.kind()),
                Err(error),
            )
        }
    }
}

#[derive(Debug)]
pub struct Conflict;

impl std::fmt::Display for Conflict {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str(messages::FILE_CONFLICT)
    }
}

impl std::error::Error for Conflict {}

pub fn check_revision(path: &Path, expected: &str) -> Result<()> {
    ensure!(revision(path).0 == expected, Conflict);
    Ok(())
}

pub fn lock(home: &Path) -> Result<Option<File>> {
    let mut options = OpenOptions::new();
    options.read(true).write(true).create(true).truncate(false);
    #[cfg(unix)]
    {
        use std::os::unix::fs::OpenOptionsExt;
        options.mode(0o600);
    }
    let file = options.open(home.join("instance.lock"))?;
    if try_lock(&file)? {
        Ok(Some(file))
    } else {
        Ok(None)
    }
}

pub fn instance_running(home: &Path) -> io::Result<bool> {
    match OpenOptions::new()
        .read(true)
        .write(true)
        .open(home.join("instance.lock"))
    {
        Ok(file) => Ok(!try_lock(&file)?),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(false),
        Err(error) => Err(error),
    }
}

#[cfg(unix)]
fn try_lock(file: &File) -> io::Result<bool> {
    use std::os::fd::AsRawFd;
    // The open file description owns the lock until the descriptor closes.
    if unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) } == 0 {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.kind() == io::ErrorKind::WouldBlock {
        Ok(false)
    } else {
        Err(error)
    }
}

#[cfg(windows)]
fn try_lock(file: &File) -> io::Result<bool> {
    use std::os::windows::io::AsRawHandle;
    use windows_sys::Win32::{
        Foundation::ERROR_LOCK_VIOLATION,
        Storage::FileSystem::{LOCKFILE_EXCLUSIVE_LOCK, LOCKFILE_FAIL_IMMEDIATELY, LockFileEx},
        System::IO::OVERLAPPED,
    };
    // The default offset is zero, matching msvcrt.locking in the Python CLI.
    let mut offset: OVERLAPPED = unsafe { std::mem::zeroed() };
    if unsafe {
        LockFileEx(
            file.as_raw_handle(),
            LOCKFILE_EXCLUSIVE_LOCK | LOCKFILE_FAIL_IMMEDIATELY,
            0,
            1,
            0,
            &mut offset,
        )
    } != 0
    {
        return Ok(true);
    }
    let error = io::Error::last_os_error();
    if error.raw_os_error() == Some(ERROR_LOCK_VIOLATION as i32) {
        Ok(false)
    } else {
        Err(error)
    }
}

#[derive(Debug, Deserialize, Serialize)]
pub struct Host {
    pub address: String,
    pub fingerprint: String,
    pub name: String,
    pub host_cert_pem: String,
}

impl Host {
    pub fn load(home: &Path) -> Result<Self> {
        read_json(&home.join("host.json")).context("child is not enrolled or host.json is invalid")
    }
}

pub fn read_json<T: serde::de::DeserializeOwned>(path: &Path) -> Result<T> {
    let text = fs::read(path)?;
    Ok(serde_json::from_slice(&text)?)
}

#[derive(Deserialize, Serialize)]
pub struct Settings {
    #[serde(default = "default_idle")]
    pub runner_idle_seconds: u32,
    /// `claude_cli` と `codex_cli` を含む、この版が知らない `app.json` のキー。
    #[serde(flatten)]
    pub extra: Map<String, Value>,
}

fn default_idle() -> u32 {
    600
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            runner_idle_seconds: default_idle(),
            extra: Map::new(),
        }
    }
}

impl Settings {
    pub fn save(&self, home: &Path, expected: &str) -> Result<String> {
        let path = home.join("app.json");
        let contents = serde_json::to_vec_pretty(self)?;
        replace_checked(
            &path,
            &contents,
            |_| Ok(()),
            || check_revision(&path, expected),
        )?;
        #[cfg(unix)]
        File::open(home)?.sync_all()?;
        Ok(crate::link::fingerprint(&contents))
    }
}

#[derive(Clone, Copy, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum CliChoice {
    Bundled,
    System,
}

pub static SAVE_LOCK: tokio::sync::Mutex<()> = tokio::sync::Mutex::const_new(());

pub fn settings(home: &Path) -> Result<Settings> {
    let path = home.join("app.json");
    let raw = match fs::read(path) {
        Ok(raw) => Ok(Some(raw)),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error),
    };
    parse_settings(raw)
}

fn parse_settings(raw: io::Result<Option<Vec<u8>>>) -> Result<Settings> {
    match raw.context("cannot read app.json")? {
        Some(raw) => serde_json::from_slice(&raw).context("invalid app.json"),
        None => Ok(Settings::default()),
    }
}

pub fn settings_revision(home: &Path) -> (String, Result<Settings>) {
    let (revision, raw) = revision(&home.join("app.json"));
    (revision, parse_settings(raw))
}

pub fn idle(home: &Path) -> Result<Duration> {
    Ok(Duration::from_secs(
        settings(home)?.runner_idle_seconds.into(),
    ))
}

pub const EMPTY_POLICY: &str = r#"{"adapters":[],"logins":[],"mcp":{},"workspace_roots":[]}"#;

pub fn ensure_policy(home: &Path) -> Result<()> {
    let path = home.join("policy.json");
    if !path.try_exists()? {
        write_private(&path, format!("{EMPTY_POLICY}\n").as_bytes())?;
    }
    Ok(())
}

pub fn parse_policy_text(text: &str) -> serde_json::Result<Value> {
    let literal = || {
        let mut output = String::with_capacity(text.len());
        let mut chars = text.chars().peekable();
        while let Some(ch) = chars.next() {
            if ch == '\\' && chars.peek() != Some(&'"') {
                output.push('\\');
            }
            output.push(ch);
        }
        serde_json::from_str(&output)
    };
    fn controls(value: &Value) -> bool {
        let invalid = |s: &str| s.chars().any(|c| c < ' ');
        match value {
            Value::String(s) => invalid(s),
            Value::Array(values) => values.iter().any(controls),
            Value::Object(values) => values.iter().any(|(key, v)| invalid(key) || controls(v)),
            _ => false,
        }
    }
    match serde_json::from_str(text) {
        Ok(value) if controls(&value) => literal().or(Ok(value)),
        Ok(value) => Ok(value),
        Err(_) => literal(),
    }
}

#[derive(Default, Deserialize, Serialize)]
#[serde(rename_all = "kebab-case")]
/// `policy.json` で child の所有者が指定する Codex sandbox の上限。
pub enum CodexSandbox {
    ReadOnly,
    #[default]
    WorkspaceWrite,
    DangerFullAccess,
}

#[derive(Default, Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Policy {
    pub workspace_roots: Vec<PathBuf>,
    pub adapters: Vec<String>,
    pub mcp: Map<String, Value>,
    #[serde(default)]
    pub logins: Vec<String>,
    #[serde(default)]
    pub codex_sandbox_max: CodexSandbox,
    #[serde(default)]
    pub local_llm: BTreeMap<String, crate::policy::LocalLlm>,
}

pub struct Catalog {
    pub adapters: Vec<String>,
    pub mcp: Vec<Value>,
    pub quota_providers: Vec<String>,
    pub providers: Vec<Value>,
    pub workspace_roots: Vec<PathBuf>,
    pub codex_sandbox_max: CodexSandbox,
    pub local_llm: BTreeMap<String, Vec<String>>,
}

pub const SETTINGS_PORT_FILE: &str = "settings.port";

pub async fn catalog(home: &Path, python: Option<&Path>) -> Result<Catalog> {
    let source = fs::read_to_string(home.join("policy.json")).context("cannot read policy.json")?;
    let policy = Policy::parse(&source)?;
    policy.validate(home)?;
    let mut available = match available_adapters(home, python).await {
        Ok(adapters) => adapters,
        Err(error)
            if policy
                .adapters
                .iter()
                .any(|name| crate::components::manages_adapter(name)) =>
        {
            return Err(error.into());
        }
        Err(_) => legacy_adapters(python),
    };
    let providers: Vec<_> = crate::declarations::installed_providers(home.to_owned())
        .await
        .into_iter()
        .filter(|row| {
            [row["provider"].as_str(), row["profile"].as_str()]
                .into_iter()
                .flatten()
                .any(|name| policy.adapters.iter().any(|adapter| adapter == name))
        })
        .collect();
    available.extend(
        providers
            .iter()
            .filter_map(|row| row["provider"].as_str().map(str::to_owned)),
    );
    let mcp = policy
        .mcp
        .iter()
        .map(|(name, spec)| {
            let mut item = json!({"name":name});
            if let Some(description) = spec["description"].as_str() {
                item["description"] = json!(description);
            }
            item
        })
        .collect();
    Ok(Catalog {
        adapters: policy
            .adapters
            .into_iter()
            .filter(|name| available.contains(name))
            .collect(),
        mcp,
        quota_providers: policy.logins,
        providers,
        workspace_roots: policy.workspace_roots,
        codex_sandbox_max: policy.codex_sandbox_max,
        local_llm: policy
            .local_llm
            .into_iter()
            .map(|(name, entry)| (name, entry.models))
            .collect(),
    })
}

impl Policy {
    pub fn parse(source: &str) -> Result<Self> {
        serde_json::from_value(parse_policy_text(source).context("invalid policy.json")?)
            .context("invalid policy.json fields")
    }

    pub fn validate(&self, home: &Path) -> Result<()> {
        ensure!(
            self.workspace_roots.iter().all(|p| p.is_absolute()),
            "policy workspace roots must be absolute"
        );
        ensure!(
            self.adapters.iter().all(|s| !s.is_empty()),
            "policy adapters must be names"
        );
        let mut logins = std::collections::HashSet::new();
        ensure!(
            self.logins.iter().all(|s| {
                (1..=64).contains(&s.chars().count())
                    && !s.contains(|c: char| c.is_whitespace() || c == '\0')
                    && logins.insert(s.to_lowercase())
            }),
            "invalid policy login names"
        );
        for (name, spec) in &self.mcp {
            ensure!(
                (1..=64).contains(&name.len())
                    && name
                        .bytes()
                        .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte)),
                messages::MCP_NAME_INVALID
            );
            ensure!(
                spec["command"].as_str().is_some_and(|s| !s.is_empty())
                    && spec["args"]
                        .as_array()
                        .is_some_and(|args| args.iter().all(Value::is_string))
                    && (spec["description"].is_null() || spec["description"].is_string())
                    && (spec["env"].is_null()
                        || spec["env"]
                            .as_object()
                            .is_some_and(|env| env.values().all(Value::is_string))),
                "invalid policy MCP entry"
            );
        }
        let max_connections = crate::policy::MAX_LOCAL_LLM_CONNECTIONS;
        ensure!(
            self.local_llm.len() <= max_connections,
            "Local LLM policy allows at most {max_connections} connections"
        );
        if !self.local_llm.is_empty() {
            let reserved_ports = crate::policy::reserved_local_llm_ports(home)?;
            for (name, entry) in &self.local_llm {
                entry.validate(name, &reserved_ports)?;
            }
        }
        Ok(())
    }
}

pub async fn available_adapters(
    home: &Path,
    python: Option<&Path>,
) -> Result<Vec<String>, crate::components::AvailabilityError> {
    let sites = python.map_or_else(Vec::new, site_packages);
    let mut available = crate::components::available(home, python, &sites).await?;
    available.extend(legacy_adapters(python));
    Ok(available)
}

pub fn legacy_adapters(python: Option<&Path>) -> Vec<String> {
    let sites = python.map_or_else(Vec::new, site_packages);
    ["cursor.sdk", "antigravity.sdk", "cli.acp", "cli.print"]
        .into_iter()
        .filter(|name| installed(name, &sites))
        .map(str::to_owned)
        .collect()
}

pub fn python_root(python: &Path) -> Option<&Path> {
    let bin = python.parent()?;
    Some(
        if bin
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case("Scripts") || name == "bin")
        {
            bin.parent().unwrap_or(bin)
        } else {
            bin
        },
    )
}

pub fn site_packages(python: &Path) -> Vec<PathBuf> {
    let Some(root) = python_root(python) else {
        return Vec::new();
    };
    if cfg!(windows) {
        return vec![root.join("Lib").join("site-packages")];
    }
    let mut sites: Vec<_> = ["lib", "lib64"]
        .into_iter()
        .flat_map(|lib| fs::read_dir(root.join(lib)).into_iter().flatten().flatten())
        .filter(|entry| entry.file_name().to_string_lossy().starts_with("python"))
        .flat_map(|entry| ["site-packages", "dist-packages"].map(|name| entry.path().join(name)))
        .collect();
    sites.sort();
    sites
}

fn installed(name: &str, sites: &[PathBuf]) -> bool {
    let (distribution, binary) = match name {
        "cursor.sdk" if cfg!(unix) => ("cursor_sdk", Some("cursor-agent")),
        "antigravity.sdk" if cfg!(unix) => ("google_antigravity", Some("agy")),
        "cli.acp" if cfg!(unix) => ("", Some("agy")),
        "cli.print" if cfg!(unix) => ("kotoyose", None),
        _ => return false,
    };
    binary.is_some_and(|name| path_executable(name).is_some())
        || (!distribution.is_empty()
            && sites
                .iter()
                .any(|site| has_distribution(site, distribution)))
}

fn has_distribution(site: &Path, distribution: &str) -> bool {
    fs::read_dir(site)
        .into_iter()
        .flatten()
        .flatten()
        .any(|entry| {
            let file = entry.file_name();
            let file = file.to_string_lossy();
            (file.ends_with(".dist-info") || file.ends_with(".egg-info"))
                && (file
                    .strip_prefix(distribution)
                    .is_some_and(|s| s.starts_with('-'))
                    || file == format!("{distribution}.egg-info"))
        })
}

pub fn path_executable(name: &str) -> Option<PathBuf> {
    env::split_paths(&env::var_os("PATH")?)
        .filter(|path| !path.as_os_str().is_empty())
        .map(|path| path.join(name))
        .find(|path| executable(path))
        .and_then(|path| std::path::absolute(path).ok())
}

fn executable(path: &Path) -> bool {
    let Ok(metadata) = path.metadata() else {
        return false;
    };
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        metadata.is_file() && metadata.permissions().mode() & 0o111 != 0
    }
    #[cfg(windows)]
    metadata.is_file()
}

/// installer の配置 (`<版>\\bin\\<exe>` と `<版>\\python\\`) の同梱ランタイム。
pub fn bundled_python(exe: &Path) -> Option<PathBuf> {
    let path = exe.parent()?.parent()?.join(if cfg!(windows) {
        "python/python.exe"
    } else {
        "python/bin/python3"
    });
    executable(&path).then_some(path)
}

pub fn python() -> Result<PathBuf> {
    if let Some(path) = bundled_python(&env::current_exe()?) {
        ensure!(
            path.with_file_name("kotoyose-ready").is_file(),
            "runtime parts are missing; run kotoyose-child-app repair"
        );
        return Ok(path);
    }
    if let Some(path) = env::var_os("KOTOYOSE_CHILD_PYTHON") {
        let path = PathBuf::from(path);
        ensure!(
            path.is_absolute() && executable(&path),
            "KOTOYOSE_CHILD_PYTHON must name an absolute Python executable"
        );
        return Ok(path);
    }
    let (bin, executable_name) = if cfg!(windows) {
        ("Scripts", "python.exe")
    } else {
        ("bin", "python3")
    };
    if let Some(path) = env::var_os("VIRTUAL_ENV") {
        let path = PathBuf::from(path).join(bin).join(executable_name);
        if executable(&path) {
            return Ok(std::path::absolute(path)?);
        }
    }
    // `uv tool install <kotoyose の wheel>` で入れた Kotoyose。環境変数なしで (自動起動でも) 見つける。
    for dir in uv_tool_dirs() {
        let path = dir.join("kotoyose").join(bin).join(executable_name);
        if executable(&path) {
            return Ok(path);
        }
    }
    path_executable(executable_name).ok_or_else(|| anyhow!("runner Python is unavailable"))
}

/// uv の tool の置き場所の候補 (`uv tool dir` と同じ規則)。`UV_TOOL_DIR` があればそれだけ。
fn uv_tool_dirs() -> Vec<PathBuf> {
    if let Some(dir) = env::var_os("UV_TOOL_DIR").filter(|value| !value.is_empty()) {
        return vec![PathBuf::from(dir)];
    }
    let mut dirs = Vec::new();
    if cfg!(windows) {
        if let Some(appdata) = env::var_os("APPDATA").filter(|value| !value.is_empty()) {
            // 版によって `%APPDATA%\uv\tools` と `%APPDATA%\uv\data\tools` がある。両方を見る。
            let uv = PathBuf::from(appdata).join("uv");
            dirs.push(uv.join("tools"));
            dirs.push(uv.join("data").join("tools"));
        }
    } else {
        if let Some(data) = env::var_os("XDG_DATA_HOME").filter(|value| !value.is_empty()) {
            dirs.push(PathBuf::from(data).join("uv").join("tools"));
        }
        if let Some(home) = env::var_os("HOME").filter(|value| !value.is_empty()) {
            dirs.push(
                PathBuf::from(home)
                    .join(".local")
                    .join("share")
                    .join("uv")
                    .join("tools"),
            );
        }
    }
    dirs.into_iter().filter(|dir| dir.is_absolute()).collect()
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::{Value, json};

    #[test]
    fn bundled_python_is_resolved_relative_to_the_app() {
        let dir = tempfile::tempdir().unwrap();
        // installer の配置: <版>/bin/<exe> と <版>/python/。
        let exe = dir.path().join("bin").join("kotoyose-child-app");
        let python = if cfg!(windows) {
            dir.path().join("python").join("python.exe")
        } else {
            dir.path().join("python").join("bin").join("python3")
        };
        assert_eq!(bundled_python(&exe), None);
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::write(&python, []).unwrap();
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(bundled_python(&exe), None);
            fs::set_permissions(&python, fs::Permissions::from_mode(0o700)).unwrap();
        }
        assert_eq!(bundled_python(&exe), Some(python));
    }

    #[test]
    fn idle_defaults_to_ten_minutes_and_accepts_zero() {
        let home = tempfile::tempdir().unwrap();
        assert_eq!(idle(home.path()).unwrap(), Duration::from_secs(600));
        let app = home.path().join("app.json");
        fs::write(&app, "{}").unwrap();
        assert_eq!(idle(home.path()).unwrap(), Duration::from_secs(600));
        Settings {
            runner_idle_seconds: 0,
            ..Settings::default()
        }
        .save(home.path(), &revision(&app).0)
        .unwrap();
        assert_eq!(settings(home.path()).unwrap().runner_idle_seconds, 0);
        assert!(idle(home.path()).unwrap().is_zero());
        Settings {
            runner_idle_seconds: 617,
            ..Settings::default()
        }
        .save(home.path(), &revision(&app).0)
        .unwrap();
        assert_eq!(idle(home.path()).unwrap(), Duration::from_secs(617));
        fs::write(&app, r#"{"runner_idle_seconds":-1}"#).unwrap();
        assert!(idle(home.path()).is_err());
    }

    #[test]
    fn unreadable_app_json_is_not_a_default_settings_file() {
        let home = tempfile::tempdir().unwrap();
        let app = home.path().join("app.json");
        for raw in [b"{".as_slice(), b"[]", b"\"claude_cli\""] {
            fs::write(&app, raw).unwrap();
            assert!(
                settings(home.path()).is_err(),
                "{}",
                String::from_utf8_lossy(raw)
            );
        }
        fs::write(
            &app,
            br#"{"claude_cli":"system","codex_cli":"system","runner_idle_seconds":12}"#,
        )
        .unwrap();
        let mut loaded = settings(home.path()).unwrap();
        assert_eq!(loaded.extra["claude_cli"], json!("system"));
        assert_eq!(loaded.extra["codex_cli"], json!("system"));
        loaded.runner_idle_seconds = 34;
        loaded.save(home.path(), &revision(&app).0).unwrap();
        let saved: Value = serde_json::from_slice(&fs::read(&app).unwrap()).unwrap();
        assert_eq!(saved["claude_cli"], "system");
        assert_eq!(saved["codex_cli"], "system");
        assert_eq!(saved["runner_idle_seconds"], 34);
    }

    #[test]
    fn policy_matches_python_fixtures() {
        let fixtures: Vec<(String, Option<String>)> = serde_json::from_str(include_str!(
            "../../tests/fixtures/child_frames/policy.json"
        ))
        .unwrap();
        for (raw, expected) in fixtures {
            match expected {
                Some(path) => assert_eq!(
                    parse_policy_text(&raw).unwrap()["workspace_roots"],
                    json!([path])
                ),
                None => assert!(parse_policy_text(&raw).is_err()),
            }
        }
    }
}
