#[cfg(any(windows, target_os = "linux"))]
use std::io::{self, Read, Seek, SeekFrom};
use std::{
    fs::File,
    io::Write,
    path::{Path, PathBuf},
    sync::{Mutex, OnceLock},
    time::Duration,
};

use anyhow::{Result, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use ring::digest::{Context, SHA256};
use serde::{Deserialize, Serialize};
use tempfile::{Builder, NamedTempFile, TempPath};
use tokio::{
    sync::{Notify, broadcast},
    time::Instant,
};

use crate::{frame::Frame, state};

pub const FEATURE: &str = "app-update/1";
const BUILD_ID_MARKER: &str = "KOTOYOSE_CHILD_BUILD:";
static BUILD_ID_RECORD: &str = concat!(
    "KOTOYOSE_CHILD_BUILD:",
    env!("KOTOYOSE_CHILD_BUILD_ID"),
    "\0"
);

pub fn build_id() -> &'static str {
    &BUILD_ID_RECORD[BUILD_ID_MARKER.len()..BUILD_ID_RECORD.len() - 1]
}
const MAX_SIZE: u64 = 64 * 1024 * 1024;
const CHUNK_SIZE: usize = 64 * 1024;
const TIMEOUT: Duration = Duration::from_secs(190);
const ERROR_FILE: &str = "update-error.json";
const TEMP_PREFIX: &str = "update-";
#[cfg(target_os = "linux")]
const DOWNLOAD_PREFIX: &str = "update-download-";
pub const OPEN_SETTINGS: &str = "KOTOYOSE_CHILD_OPEN_SETTINGS";

#[derive(Clone, Deserialize, PartialEq, Serialize)]
#[serde(deny_unknown_fields)]
pub struct Offer {
    pub version: String,
    pub build_id: String,
    pub sha256: String,
    size: u64,
}

fn hex(value: &str) -> bool {
    value
        .bytes()
        .all(|byte| byte.is_ascii_hexdigit() && !byte.is_ascii_uppercase())
}

pub fn valid_build_id(value: &str) -> bool {
    let Some(commit) = value.get(..40) else {
        return false;
    };
    let suffix = &value[40..];
    hex(commit)
        && (suffix.is_empty()
            || suffix
                .strip_prefix("-dirty-")
                .is_some_and(|hash| hash.len() == 12 && hex(hash)))
}

pub fn can_update() -> bool {
    #[cfg(target_os = "linux")]
    return crate::update_linux::installed();
    #[cfg(not(target_os = "linux"))]
    cfg!(windows)
}

impl Offer {
    fn valid(&self) -> bool {
        self.version.len() <= 48
            && semver::Version::parse(&self.version).is_ok_and(|offered| {
                let running =
                    semver::Version::parse(crate::VERSION).expect("Cargo package version");
                offered.cmp_precedence(&running).is_ge()
            })
            && self.build_id != build_id()
            && valid_build_id(&self.build_id)
            && self.sha256.len() == 64
            && hex(&self.sha256)
            && (1..=MAX_SIZE).contains(&self.size)
    }
}

#[derive(Clone, Copy, Debug, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Error {
    Download,
    Disconnect,
    Timeout,
    Verify,
    Storage,
    Setup,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Phase {
    #[default]
    Idle,
    Requested,
    Downloading,
    Verifying,
    Waiting,
    Deferred,
    Installing,
}

#[derive(Clone, Default, Serialize)]
pub struct View {
    pub offer: Option<Offer>,
    pub phase: Phase,
    pub received: u64,
    pub error: Option<Error>,
    pub retry_after: u64,
    pub expected_shutdown: bool,
}

#[derive(Default)]
struct Progress {
    view: View,
    latest: Option<Offer>,
    retry_at: Option<Instant>,
    defer: bool,
    interrupt: bool,
}

pub struct Update {
    progress: Mutex<Progress>,
    changed: Notify,
    views: broadcast::Sender<View>,
    shutdown_seen: Notify,
}

impl Default for Update {
    fn default() -> Self {
        Self {
            progress: Mutex::default(),
            changed: Notify::new(),
            views: broadcast::channel(32).0,
            shutdown_seen: Notify::new(),
        }
    }
}

impl Update {
    pub fn view(&self) -> View {
        Self::snapshot(&self.progress.lock().expect("update state"))
    }

    fn snapshot(state: &Progress) -> View {
        let mut view = state.view.clone();
        if let Some(deadline) = state.retry_at {
            let remaining = deadline.saturating_duration_since(Instant::now());
            view.retry_after = remaining.as_secs() + u64::from(remaining.subsec_nanos() != 0);
        }
        view
    }

    pub async fn changed(&self) {
        self.changed.notified().await;
    }

    pub fn views(&self) -> broadcast::Receiver<View> {
        self.views.subscribe()
    }

    fn publish(&self, state: &Progress) {
        if self.views.receiver_count() != 0 {
            let _ = self.views.send(Self::snapshot(state));
        }
    }

    pub async fn announce_shutdown(&self) {
        let seen = self.shutdown_seen.notified();
        {
            let mut state = self.progress.lock().expect("update state");
            if state.view.phase != Phase::Installing {
                return;
            }
            state.view.expected_shutdown = true;
            self.publish(&state);
        }
        let _ = tokio::time::timeout(Duration::from_secs(2), seen).await;
    }

    pub fn acknowledge_shutdown(&self) {
        let state = self.progress.lock().expect("update state");
        if state.view.expected_shutdown {
            self.shutdown_seen.notify_waiters();
        }
    }

    fn offer(&self, offer: Option<Offer>) {
        let mut state = self.progress.lock().expect("update state");
        state.latest = offer.filter(Offer::valid);
        if state.view.phase == Phase::Idle {
            state.view.offer = state.latest.clone();
        }
    }

    pub fn restore_error(&self, home: &Path) {
        if state::read_json::<String>(&home.join(ERROR_FILE)).is_ok_and(|build| build == build_id())
        {
            self.progress.lock().expect("update state").view.error = Some(Error::Setup);
        }
    }

    pub fn request(&self, home: &Path, sha256: &str, defer: bool) -> bool {
        if !can_update() || !self.request_download(sha256, defer) {
            return false;
        }
        let _ = std::fs::remove_file(home.join(ERROR_FILE));
        true
    }

    fn request_download(&self, sha256: &str, defer: bool) -> bool {
        let mut state = self.progress.lock().expect("update state");
        if state.view.phase != Phase::Idle
            || state
                .retry_at
                .is_some_and(|deadline| Instant::now() < deadline)
            || state
                .view
                .offer
                .as_ref()
                .is_none_or(|offer| offer.sha256 != sha256)
        {
            return false;
        }
        state.defer = defer;
        state.interrupt = false;
        state.view.error = None;
        state.retry_at = None;
        state.view.received = 0;
        state.view.phase = Phase::Requested;
        self.publish(&state);
        self.changed.notify_one();
        true
    }

    pub fn activate(&self, interrupt: bool) -> bool {
        let mut state = self.progress.lock().expect("update state");
        if !matches!(state.view.phase, Phase::Deferred | Phase::Waiting) {
            return false;
        }
        state.view.phase = Phase::Waiting;
        state.interrupt = interrupt;
        self.publish(&state);
        self.changed.notify_one();
        true
    }

    fn start(&self) -> Option<Offer> {
        let mut state = self.progress.lock().expect("update state");
        if state.view.phase != Phase::Requested {
            return None;
        }
        state.view.phase = Phase::Downloading;
        self.publish(&state);
        state.view.offer.clone()
    }

    fn advance(&self, received: u64) {
        let mut state = self.progress.lock().expect("update state");
        state.view.received = received;
        self.publish(&state);
    }

    fn verifying(&self) {
        let mut state = self.progress.lock().expect("update state");
        state.view.phase = Phase::Verifying;
        self.publish(&state);
    }

    fn ready(&self) {
        let mut state = self.progress.lock().expect("update state");
        state.view.phase = if state.defer {
            Phase::Deferred
        } else {
            Phase::Waiting
        };
        self.publish(&state);
    }

    pub fn draining(&self) -> bool {
        self.progress.lock().expect("update state").view.phase == Phase::Waiting
    }

    pub fn install(&self, idle: bool) -> bool {
        let mut state = self.progress.lock().expect("update state");
        if state.view.phase != Phase::Waiting || !(state.interrupt || idle) {
            return false;
        }
        state.view.phase = Phase::Installing;
        self.publish(&state);
        true
    }

    pub fn fail(&self, error: Error) {
        let mut state = self.progress.lock().expect("update state");
        if state.latest == state.view.offer {
            state.latest = None;
        }
        state.view.offer = state.latest.clone();
        state.view.phase = Phase::Idle;
        state.view.received = 0;
        state.view.error = Some(error);
        state.view.expected_shutdown = false;
        state.retry_at = None;
        self.publish(&state);
    }

    fn retry_after(&self, seconds: u64) {
        let mut state = self.progress.lock().expect("update state");
        state.view.offer = state.latest.clone();
        state.view.phase = Phase::Idle;
        state.view.received = 0;
        state.view.error = None;
        state.retry_at = Some(Instant::now() + Duration::from_secs(seconds));
        self.publish(&state);
    }

    pub fn disconnected(&self) {
        let mut state = self.progress.lock().expect("update state");
        state.latest = None;
        if state.view.phase == Phase::Installing {
            return;
        }
        state.view.offer = None;
        state.retry_at = None;
        if state.view.phase != Phase::Idle {
            state.view.phase = Phase::Idle;
            state.view.received = 0;
            state.view.error = Some(Error::Disconnect);
        }
        self.publish(&state);
    }
}

#[derive(Default)]
pub struct Transfer {
    receiving: Option<Download>,
    verified: Option<Package>,
}

pub struct Package {
    file: File,
    _path: TempPath,
    pub offer: Offer,
    #[cfg(target_os = "linux")]
    _directory: tempfile::TempDir,
}

impl Package {
    #[cfg(any(windows, target_os = "linux"))]
    pub fn verify(&mut self, consume: impl FnOnce(&mut dyn Read) -> Result<()>) -> Result<()> {
        self.file.seek(SeekFrom::Start(0))?;
        let limit = self.offer.size + 1;
        let mut reader = HashingReader {
            input: (&mut self.file).take(limit),
            hash: Context::new(&SHA256),
        };
        consume(&mut reader)?;
        io::copy(&mut reader, &mut io::sink())?;
        ensure!(
            limit - reader.input.limit() == self.offer.size
                && digest_hex(reader.hash) == self.offer.sha256,
            "update contents changed"
        );
        Ok(())
    }

    #[cfg(windows)]
    fn spawn(&mut self) -> Result<std::process::Child> {
        self.verify(|_| Ok(()))?;
        Ok(std::process::Command::new(&self._path)
            .args(["/S", "/UPDATE"])
            .spawn()?)
    }
}

#[cfg(any(windows, target_os = "linux"))]
struct HashingReader<R> {
    input: R,
    hash: Context,
}

#[cfg(any(windows, target_os = "linux"))]
impl<R: Read> Read for HashingReader<R> {
    fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
        let count = self.input.read(buffer)?;
        self.hash.update(&buffer[..count]);
        Ok(count)
    }
}

fn digest_hex(hash: Context) -> String {
    hash.finish()
        .as_ref()
        .iter()
        .map(|b| format!("{b:02x}"))
        .collect()
}

pub fn sweep(home: &Path) {
    sweep_downloads(home);
    #[cfg(windows)]
    if let Some(root) = windows_install_root()
        && root != home
    {
        sweep_downloads(&root);
    }
    #[cfg(target_os = "linux")]
    crate::update_linux::sweep();
}

#[cfg(any(windows, test))]
fn windows_install_root_for(exe: &Path) -> Option<PathBuf> {
    let bin = exe.parent()?;
    let version_dir = bin.parent()?;
    if !version_dir.join("kotoyose-child.install").is_file() {
        return None;
    }
    Some(version_dir.parent()?.to_path_buf())
}

#[cfg(windows)]
fn windows_install_root() -> Option<PathBuf> {
    windows_install_root_for(STARTED.get()?)
}

fn sweep_downloads(home: &Path) {
    let Ok(entries) = home.read_dir() else { return };
    for entry in entries.flatten() {
        let name = entry.file_name();
        let Some(name) = name.to_str() else { continue };
        let Ok(kind) = entry.file_type() else {
            continue;
        };
        if kind.is_file()
            && name.starts_with(TEMP_PREFIX)
            && (name.ends_with(".exe") || name.ends_with(".tar.gz"))
        {
            let _ = std::fs::remove_file(entry.path());
        }
        #[cfg(target_os = "linux")]
        if kind.is_dir() && name.starts_with(DOWNLOAD_PREFIX) {
            let _ = std::fs::remove_dir_all(entry.path());
        }
    }
}

impl Transfer {
    pub fn request(&mut self, update: &Update, home: &Path, now: Instant) -> Option<Frame> {
        let offer = update.start()?;
        let request = serde_json::json!({"sha256": &offer.sha256});
        match Download::new(home, offer, now) {
            Ok(download) => {
                self.receiving = Some(download);
                Some(Frame::new(0, "app_update_get", request))
            }
            Err(_) => {
                update.fail(Error::Storage);
                None
            }
        }
    }

    pub async fn frame(&mut self, update: &Update, frame: Frame) {
        let kind = frame.envelope.kind.as_str();
        if kind == "app_update_offer" {
            if let Ok(payload) = frame.payload() {
                if payload == serde_json::json!({}) {
                    update.offer(None);
                } else if let Ok(offer) = serde_json::from_value(payload) {
                    update.offer(Some(offer));
                }
            }
            return;
        }
        let Some(download) = &mut self.receiving else {
            return;
        };
        let result = match kind {
            "app_update_chunk" => frame
                .payload_as()
                .map_err(|_| Error::Verify)
                .and_then(|chunk| download.chunk(chunk))
                .map(|()| update.advance(download.received)),
            "app_update_end" => {
                update.verifying();
                let download = self.receiving.take().expect("receiving download");
                match tokio::task::spawn_blocking(move || download.finish())
                    .await
                    .unwrap_or(Err(Error::Storage))
                {
                    Ok(path) => {
                        self.verified = Some(path);
                        update.ready();
                        Ok(())
                    }
                    Err(error) => Err(error),
                }
            }
            "app_update_error" => {
                if let Ok(retry) = frame.payload_as::<Retry>()
                    && retry.code == "retry"
                    && (1..=300).contains(&retry.retry_after)
                {
                    self.receiving = None;
                    update.retry_after(retry.retry_after);
                    return;
                }
                Err(Error::Download)
            }
            _ => return,
        };
        if let Err(error) = result {
            self.receiving = None;
            update.fail(error);
        }
    }

    pub fn deadline(&self) -> Option<Instant> {
        self.receiving.as_ref().map(|download| download.deadline)
    }

    pub fn expire(&mut self, update: &Update, now: Instant) {
        if self.deadline().is_some_and(|deadline| now >= deadline) {
            self.receiving = None;
            update.fail(Error::Timeout);
        }
    }

    pub fn take_verified(&mut self) -> Option<Package> {
        self.verified.take()
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Retry {
    code: String,
    retry_after: u64,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Chunk {
    offset: u64,
    data: String,
}

struct Download {
    file: NamedTempFile,
    #[cfg(target_os = "linux")]
    directory: tempfile::TempDir,
    hash: Context,
    received: u64,
    offer: Offer,
    deadline: Instant,
}

impl Download {
    fn new(home: &Path, offer: Offer, now: Instant) -> Result<Self> {
        #[cfg(target_os = "linux")]
        let directory = private_tempdir(home, DOWNLOAD_PREFIX)?;
        #[cfg(target_os = "linux")]
        let home = directory.path();
        let make_temp = |dir: &Path| {
            Builder::new()
                .prefix(TEMP_PREFIX)
                .suffix(if cfg!(windows) { ".exe" } else { ".tar.gz" })
                .tempfile_in(dir)
        };
        #[cfg(windows)]
        let file = match windows_install_root()
            .filter(|root| root != home)
            .and_then(|root| make_temp(&root).ok())
        {
            Some(file) => file,
            None => make_temp(home)?,
        };
        #[cfg(not(windows))]
        let file = make_temp(home)?;
        state::protect_secret(file.path())?;
        Ok(Self {
            file,
            #[cfg(target_os = "linux")]
            directory,
            hash: Context::new(&SHA256),
            received: 0,
            offer,
            deadline: now + TIMEOUT,
        })
    }

    fn chunk(&mut self, chunk: Chunk) -> Result<(), Error> {
        if chunk.offset != self.received || chunk.data.len() > 4 * CHUNK_SIZE.div_ceil(3) {
            return Err(Error::Verify);
        }
        let bytes = STANDARD.decode(chunk.data).map_err(|_| Error::Verify)?;
        if bytes.is_empty()
            || bytes.len() > CHUNK_SIZE
            || self.received + bytes.len() as u64 > self.offer.size
        {
            return Err(Error::Verify);
        }
        self.file.write_all(&bytes).map_err(|_| Error::Storage)?;
        self.hash.update(&bytes);
        self.received += bytes.len() as u64;
        Ok(())
    }

    fn finish(self) -> Result<Package, Error> {
        if self.received != self.offer.size || digest_hex(self.hash) != self.offer.sha256 {
            return Err(Error::Verify);
        }
        self.file.as_file().sync_all().map_err(|_| Error::Storage)?;
        let (file, path) = self.file.into_parts();
        #[cfg(windows)]
        let file = {
            use std::os::windows::fs::OpenOptionsExt;
            use windows_sys::Win32::Storage::FileSystem::FILE_SHARE_READ;

            drop(file);
            // Deny write and delete sharing until setup has been launched.
            std::fs::OpenOptions::new()
                .read(true)
                .share_mode(FILE_SHARE_READ)
                .open(&path)
                .map_err(|_| Error::Storage)?
        };
        Ok(Package {
            file,
            _path: path,
            offer: self.offer,
            #[cfg(target_os = "linux")]
            _directory: self.directory,
        })
    }
}

#[cfg(target_os = "linux")]
pub fn private_tempdir(home: &Path, prefix: &str) -> std::io::Result<tempfile::TempDir> {
    use std::os::unix::fs::PermissionsExt;

    Builder::new()
        .prefix(prefix)
        .permissions(std::fs::Permissions::from_mode(0o700))
        .tempdir_in(home)
}

static STARTED: OnceLock<PathBuf> = OnceLock::new();
#[cfg(all(unix, not(target_os = "linux")))]
static STARTED_FILE: OnceLock<(u64, u64)> = OnceLock::new();
#[cfg(all(unix, not(target_os = "linux")))]
static CHECKED_FILE: Mutex<Option<((u64, u64), bool)>> = Mutex::new(None);

pub fn remember_executable() {
    if let Ok(path) = std::env::current_exe() {
        #[cfg(all(unix, not(target_os = "linux")))]
        if let Ok(file) = file_id(&path) {
            let _ = STARTED_FILE.set(file);
        }
        let _ = STARTED.set(path);
    }
}

#[cfg(all(unix, not(target_os = "linux")))]
fn file_id(path: &Path) -> std::io::Result<(u64, u64)> {
    use std::os::unix::fs::MetadataExt;
    let meta = path.metadata()?;
    Ok((meta.dev(), meta.ino()))
}

#[cfg(unix)]
pub fn has_build(path: &Path, build_id: &str) -> std::io::Result<bool> {
    use std::io::Read;
    let mut file = std::fs::File::open(path)?;
    let record = format!("{BUILD_ID_MARKER}{build_id}\0");
    let marker = record.as_bytes();
    let mut buffer = [0; 8192];
    let mut start = 0;
    loop {
        let read = file.read(&mut buffer[start..])?;
        if read == 0 {
            return Ok(false);
        }
        let end = start + read;
        if buffer[..end]
            .windows(marker.len())
            .any(|bytes| bytes == marker)
        {
            return Ok(true);
        }
        start = end.min(marker.len() - 1);
        buffer.copy_within(end - start..end, 0);
    }
}

pub fn installed_change() -> Option<PathBuf> {
    #[cfg(target_os = "linux")]
    return crate::update_linux::installed_change();
    #[cfg(windows)]
    {
        let path = STARTED.get()?;
        let installed = crate::autostart::installed_bin()?.join(path.file_name()?);
        let actual = std::fs::read_to_string(installed.with_file_name("child-build-id")).ok()?;
        (installed.is_file() && actual.trim() != build_id()).then_some(installed)
    }
    #[cfg(all(unix, not(target_os = "linux")))]
    {
        let path = STARTED.get()?;
        let actual = file_id(path).ok()?;
        if *STARTED_FILE.get()? == actual {
            return None;
        }
        let mut checked = CHECKED_FILE.lock().expect("installed build state");
        if let Some((last, differs)) = *checked
            && last == actual
        {
            return differs.then(|| path.clone());
        }
        let differs = !has_build(path, build_id()).ok()?;
        *checked = Some((actual, differs));
        differs.then(|| path.clone())
    }
}

#[cfg(not(target_os = "linux"))]
pub fn relaunch(
    path: &Path,
    args: impl IntoIterator<Item = impl AsRef<std::ffi::OsStr>>,
) -> Result<()> {
    std::process::Command::new(path)
        .args(args)
        .env(OPEN_SETTINGS, "1")
        .spawn()
        .map(|_| ())
        .map_err(Into::into)
}

#[cfg(not(target_os = "linux"))]
pub fn install_and_recover(
    home: &Path,
    setup: Package,
    args: impl IntoIterator<Item = impl AsRef<std::ffi::OsStr>>,
) -> Result<()> {
    #[cfg(windows)]
    {
        let mut setup = setup;
        let started = setup.spawn();
        drop(setup.file);
        let status = started.map(|mut process| process.wait());
        drop(setup._path);
        match status {
            Ok(Ok(status)) if status.success() => return Ok(()),
            // NSIS exit codes 1 and 2 indicate setup aborted before launching the child.
            Ok(Ok(status)) if matches!(status.code(), Some(1 | 2)) => {
                let reason = format!("setup exited with {status}");
                crate::local_log::record(home, crate::local_log::Event::AppUpdateFailed(&reason));
            }
            Err(error) => {
                let reason = format!(
                    "setup launch failed: {}",
                    crate::local_log::diagnostic(&error)
                );
                crate::local_log::record(home, crate::local_log::Event::AppUpdateFailed(&reason));
            }
            Ok(status) => {
                let reason = format!("setup wait failed: {status:?}");
                crate::local_log::record(home, crate::local_log::Event::AppUpdateFailed(&reason));
                record_setup_error(home);
                anyhow::bail!("could not confirm the update launch result: {status:?}");
            }
        }
        record_setup_error(home);
        if let Some(_lock) = state::lock(home)? {
            relaunch(STARTED.get().expect("running executable"), args)?;
        }
        Ok(())
    }
    #[cfg(not(windows))]
    {
        let _ = (home, setup, args);
        anyhow::bail!("updates are available only on Windows")
    }
}

pub fn record_setup_error(home: &Path) {
    let result = serde_json::to_vec(build_id())
        .map_err(anyhow::Error::from)
        .and_then(|bytes| state::write_secret(&home.join(ERROR_FILE), &bytes));
    if let Err(error) = result {
        tracing::warn!(
            "could not store update result: {}",
            crate::local_log::diagnostic(&error)
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn offer(size: u64, sha256: &str) -> Frame {
        Frame::new(
            0,
            "app_update_offer",
            json!({
                "version": crate::VERSION, "build_id": "a".repeat(40), "size": size, "sha256": sha256,
            }),
        )
    }

    const ABC: &str = "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad";

    async fn start(home: &Path, size: u64, sha256: &str, defer: bool) -> (Update, Transfer) {
        let update = Update::default();
        let mut transfer = Transfer::default();
        transfer.frame(&update, offer(size, sha256)).await;
        assert!(update.request_download(sha256, defer));
        assert_eq!(
            transfer
                .request(&update, home, Instant::now())
                .unwrap()
                .payload()
                .unwrap(),
            json!({"sha256": sha256}),
        );
        (update, transfer)
    }

    fn chunk(offset: u64, data: &str) -> Frame {
        Frame::new(0, "app_update_chunk", json!({"offset":offset,"data":data}))
    }

    #[tokio::test]
    async fn progress_reports_received_bytes_and_distinct_phases() {
        let home = tempfile::tempdir().unwrap();
        let (update, mut transfer) = start(home.path(), 3, ABC, false).await;
        let mut views = update.views();
        transfer.frame(&update, chunk(0, "YQ==")).await;
        transfer.frame(&update, chunk(1, "YmM=")).await;
        transfer
            .frame(&update, Frame::new(0, "app_update_end", json!({})))
            .await;
        assert!(update.install(true));

        for (phase, received) in [
            ("downloading", 1),
            ("downloading", 3),
            ("verifying", 3),
            ("waiting", 3),
            ("installing", 3),
        ] {
            let view = serde_json::to_value(views.try_recv().unwrap()).unwrap();
            assert_eq!(view["phase"], phase);
            assert_eq!(view["received"], received);
            assert_eq!(view["offer"]["size"], 3);
            assert_eq!(view["expected_shutdown"], false);
        }
    }

    #[tokio::test]
    async fn shutdown_keeps_the_offer_until_a_browser_sees_it_and_failure_clears_it() {
        let home = tempfile::tempdir().unwrap();
        let (update, mut transfer) = start(home.path(), 3, ABC, false).await;
        transfer.frame(&update, chunk(0, "YWJj")).await;
        transfer
            .frame(&update, Frame::new(0, "app_update_end", json!({})))
            .await;
        assert!(update.install(true));
        update.disconnected();
        update.acknowledge_shutdown();
        let mut views = update.views();
        let shutdown = update.announce_shutdown();
        tokio::pin!(shutdown);
        tokio::select! {
            view = views.recv() => {
                let view = serde_json::to_value(view.unwrap()).unwrap();
                assert_eq!(view["phase"], "installing");
                assert_eq!(view["expected_shutdown"], true);
                assert_eq!(view["offer"]["version"], crate::VERSION);
            }
            () = &mut shutdown => panic!("shutdown completed before acknowledgement"),
        }
        update.acknowledge_shutdown();
        tokio::time::timeout(Duration::from_millis(100), shutdown)
            .await
            .unwrap();

        update.fail(Error::Setup);
        let view = serde_json::to_value(views.try_recv().unwrap()).unwrap();
        assert_eq!(view["phase"], "idle");
        assert_eq!(view["expected_shutdown"], false);
        assert_eq!(view["error"], "setup");
    }

    #[tokio::test]
    async fn rejects_oversize_offer_and_owner_cannot_choose_a_different_hash() {
        let update = Update::default();
        let mut transfer = Transfer::default();
        transfer.frame(&update, offer(MAX_SIZE + 1, ABC)).await;
        assert!(update.view().offer.is_none());
        transfer.frame(&update, offer(3, ABC)).await;
        assert!(!update.request_download(&"0".repeat(64), false));
    }

    #[test]
    fn compares_semver_precedence_and_accepts_a_different_build_at_the_same_version() {
        let mut offered: Offer = offer(3, ABC).payload_as().unwrap();
        assert!(offered.valid());
        offered.version = "0.0.0".into();
        assert!(!offered.valid());
        let running = semver::Version::parse(crate::VERSION).unwrap();
        offered.version = format!("{}.0.0", running.major + 1);
        assert!(offered.valid());
        offered.version = format!("0{}.0.0", running.major);
        assert!(!offered.valid());
        let mut alternate = running;
        alternate.build = semver::BuildMetadata::new("different.1").unwrap();
        offered.version = alternate.to_string();
        assert!(offered.valid());
    }

    #[tokio::test]
    async fn host_error_clears_stale_offer_and_allows_a_fresh_offer() {
        let home = tempfile::tempdir().unwrap();
        let (update, mut transfer) = start(home.path(), 3, ABC, true).await;
        transfer
            .frame(&update, Frame::new(0, "app_update_error", json!({})))
            .await;
        assert!(update.view().offer.is_none());
        assert!(matches!(update.view().error, Some(Error::Download)));
        assert!(transfer.deadline().is_none());
        transfer.frame(&update, offer(3, ABC)).await;
        assert!(update.request_download(ABC, true));
    }

    #[tokio::test]
    async fn retryable_host_error_keeps_the_offer_and_delays_the_next_request() {
        let home = tempfile::tempdir().unwrap();
        let (update, mut transfer) = start(home.path(), 3, ABC, true).await;
        transfer
            .frame(
                &update,
                Frame::new(
                    0,
                    "app_update_error",
                    json!({"code": "retry", "retry_after": 12}),
                ),
            )
            .await;
        let view = update.view();
        assert!(view.offer.is_some() && view.error.is_none() && view.phase == Phase::Idle);
        assert!((1..=12).contains(&view.retry_after));
        assert!(transfer.deadline().is_none());
        assert!(!update.request_download(ABC, true));
        update.progress.lock().unwrap().retry_at = Some(Instant::now());
        assert_eq!(update.view().retry_after, 0);
        assert!(update.request_download(ABC, true));
    }

    #[test]
    fn rejects_dirty_build_without_a_payload_fingerprint() {
        let commit = "a".repeat(40);
        assert!(valid_build_id(&commit));
        assert!(!valid_build_id(&format!("{commit}-dirty")));
        assert!(valid_build_id(&format!("{commit}-dirty-0123456789ab")));
    }

    #[cfg(target_os = "linux")]
    #[tokio::test]
    async fn hashes_the_bytes_consumed_from_the_open_archive_in_a_private_directory() {
        use std::os::unix::fs::PermissionsExt;

        let home = tempfile::tempdir().unwrap();
        let (update, mut transfer) = start(home.path(), 3, ABC, true).await;
        transfer.frame(&update, chunk(0, "YWJj")).await;
        transfer
            .frame(&update, Frame::new(0, "app_update_end", json!({})))
            .await;
        let mut package = transfer.take_verified().unwrap();
        assert_eq!(
            package
                ._directory
                .path()
                .metadata()
                .unwrap()
                .permissions()
                .mode()
                & 0o777,
            0o700
        );
        let path = package._path.to_path_buf();
        let result = package.verify(|reader| {
            let mut first = [0];
            reader.read_exact(&mut first)?;
            assert_eq!(&first, b"a");
            std::fs::write(&path, b"xyz")?;
            let mut tail = Vec::new();
            reader.read_to_end(&mut tail)?;
            assert_eq!(tail, b"yz");
            Ok(())
        });
        assert!(
            result
                .unwrap_err()
                .to_string()
                .contains("update contents changed")
        );
    }

    #[test]
    fn startup_removes_stale_packages_and_keeps_update_state() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path();
        for name in ["update-old.exe", "update-old.tar.gz", ERROR_FILE] {
            std::fs::write(path.join(name), "").unwrap();
        }
        #[cfg(target_os = "linux")]
        std::fs::create_dir(path.join("update-download-old")).unwrap();
        sweep_downloads(path);
        assert_eq!(path.read_dir().unwrap().count(), 1);
        assert!(path.join(ERROR_FILE).is_file());
    }

    #[tokio::test]
    async fn truncated_extra_or_modified_bytes_delete_private_file() {
        for (size, data) in [(4, "YWJj"), (2, "YWJj"), (3, "YWJk")] {
            let home = tempfile::tempdir().unwrap();
            let (update, mut transfer) = start(home.path(), size, ABC, false).await;
            transfer.frame(&update, chunk(0, data)).await;
            transfer
                .frame(&update, Frame::new(0, "app_update_end", json!({})))
                .await;
            assert!(matches!(update.view().error, Some(Error::Verify)));
            assert!(transfer.take_verified().is_none());
            assert_eq!(home.path().read_dir().unwrap().count(), 0);
        }
    }

    #[tokio::test]
    async fn disconnect_discards_partial_and_deferred_installers() {
        for complete in [false, true] {
            let home = tempfile::tempdir().unwrap();
            let (update, mut transfer) = start(home.path(), 3, ABC, true).await;
            transfer.frame(&update, chunk(0, "YWJj")).await;
            if complete {
                transfer
                    .frame(&update, Frame::new(0, "app_update_end", json!({})))
                    .await;
            }
            assert_eq!(home.path().read_dir().unwrap().count(), 1);
            drop(transfer);
            update.disconnected();
            let view = update.view();
            assert!(view.offer.is_none() && matches!(view.error, Some(Error::Disconnect)));
            assert_eq!(home.path().read_dir().unwrap().count(), 0);
        }
    }

    #[tokio::test]
    async fn waits_for_active_work_or_explicit_interrupt_and_honors_deferral() {
        let home = tempfile::tempdir().unwrap();
        for (defer, interrupt) in [(false, false), (false, true), (true, false)] {
            let (update, mut transfer) = start(home.path(), 3, ABC, defer).await;
            transfer.frame(&update, chunk(0, "YWJj")).await;
            transfer
                .frame(&update, Frame::new(0, "app_update_end", json!({})))
                .await;
            assert_eq!(update.draining(), !defer);
            assert!(!update.install(false));
            if defer {
                assert!(!update.install(true), "deferred setup requires Restart");
                assert!(update.activate(false));
                assert!(!update.install(false));
            }
            if interrupt {
                assert!(update.activate(true));
            }
            assert!(update.install(!interrupt));
            let installer = transfer.take_verified().unwrap();
            assert_eq!(std::fs::read(&installer._path).unwrap(), b"abc");
            #[cfg(windows)]
            {
                assert!(std::fs::write(&installer._path, b"xyz").is_err());
                assert!(std::fs::remove_file(&installer._path).is_err());
                assert!(installer.verified_file().is_ok());
            }
        }
    }

    #[test]
    fn windows_install_root_requires_install_marker() {
        let root = tempfile::tempdir().unwrap();
        let version = root.path().join("1.4.0");
        let bin = version.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let exe = bin.join("kotoyose-child-background.exe");
        assert_eq!(windows_install_root_for(&exe), None);
        std::fs::write(version.join("kotoyose-child.install"), []).unwrap();
        assert_eq!(
            windows_install_root_for(&exe),
            Some(root.path().to_path_buf())
        );
    }
}
