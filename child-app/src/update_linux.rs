mod archive;
mod service;

use std::{
    ffi::{OsStr, OsString},
    fs::{self, File},
    io,
    os::unix::{fs::symlink, process::CommandExt},
    path::{Path, PathBuf},
    process::{Command, Stdio},
};

use anyhow::{Context, Result, ensure};

use crate::{VERSION, local_log, parts, state, update};

const ROOT: &str = ".local/share/kotoyose-child-app";
const BIN: &str = "bin/kotoyose-child-app";
const MARKER: &str = ".kotoyose-child-install";
const INSTALLING: &str = ".kotoyose-child-installing";
const EXTRACT_PREFIX: &str = ".install-";
const REOPEN: &str = "update-open-settings";

pub use service::{notify_ready, restart_service};

fn root() -> Result<PathBuf> {
    let home = std::env::home_dir().context("user home is unavailable")?;
    ensure!(home.is_absolute(), "user home must be absolute");
    Ok(home.join(ROOT))
}

fn release_name(version: &str, build_id: &str) -> Result<String> {
    ensure!(
        version.starts_with(|ch: char| ch.is_ascii_digit())
            && version.len() <= 48
            && version
                .bytes()
                .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, b'.' | b'-' | b'+'))
            && update::valid_build_id(build_id),
        "invalid child release version or build ID"
    );
    Ok(format!("{version}-{build_id}"))
}

fn read_build_marker(marker: &Path) -> Option<String> {
    let meta = marker.symlink_metadata().ok()?;
    if !meta.is_file() || meta.len() > 59 {
        return None;
    }
    fs::read_to_string(marker).ok()
}

fn release_build(path: &Path) -> Option<String> {
    if !path.join(BIN).is_file() {
        return None;
    }
    let build = read_build_marker(&path.join(MARKER))?;
    let name = path.file_name()?.to_str()?;
    let version = name.strip_suffix(&build)?.strip_suffix('-')?;
    (release_name(version, &build).ok()?.as_str() == name).then_some(build)
}

fn running(root: &Path, exe: &Path) -> Option<PathBuf> {
    let release = exe.parent()?.parent()?;
    if release.parent()? != root.join("versions").canonicalize().ok()?
        || exe != release.join(BIN)
        || release_build(release)? != update::build_id()
    {
        return None;
    }
    Some(release.to_path_buf())
}

pub fn installed() -> bool {
    root()
        .ok()
        .zip(std::env::current_exe().ok())
        .is_some_and(|(root, exe)| running(&root, &exe).is_some())
}

fn current(root: &Path) -> Result<Option<PathBuf>> {
    let path = match root.join("current").canonicalize() {
        Ok(path) => path,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error.into()),
    };
    ensure!(
        path.parent() == Some(root.join("versions").canonicalize()?.as_path())
            && release_build(&path).is_some(),
        "current does not point to a child release"
    );
    Ok(Some(path))
}

pub fn installed_change() -> Option<PathBuf> {
    let root = root().ok()?;
    running(&root, &std::env::current_exe().ok()?)?;
    let current = current(&root).ok()??;
    (release_build(&current)? != update::build_id()).then(|| current.join(BIN))
}

pub fn sweep() {
    let Ok(root) = root() else { return };
    if root.is_dir()
        && let Ok(Some(_lock)) = state::lock(&root)
    {
        sweep_extracts(&root);
    }
}

fn sweep_extracts(root: &Path) {
    let versions = root.join("versions");
    if !versions.symlink_metadata().is_ok_and(|meta| meta.is_dir()) {
        return;
    }
    let Ok(entries) = versions.read_dir() else {
        return;
    };
    for entry in entries.flatten() {
        if entry
            .file_name()
            .as_encoded_bytes()
            .starts_with(EXTRACT_PREFIX.as_bytes())
            && entry.file_type().is_ok_and(|kind| kind.is_dir())
        {
            let _ = fs::remove_dir_all(entry.path());
        }
    }
}

struct Install {
    root: PathBuf,
    _lock: File,
}

impl Install {
    fn lock(root: PathBuf) -> Result<Self> {
        state::private_dir(&root)?;
        let lock = state::lock(&root)?.context("another child installation is in progress")?;
        fs::create_dir_all(root.join("versions"))?;
        sweep_extracts(&root);
        Ok(Self {
            root: root.canonicalize()?,
            _lock: lock,
        })
    }

    async fn prepare(
        &self,
        name: &str,
        build_id: &str,
        log_dir: &Path,
        populate: impl FnOnce(&Path) -> Result<()>,
    ) -> Result<PathBuf> {
        let destination = self.root.join("versions").join(name);
        let previous = current(&self.root)?;
        match destination.symlink_metadata() {
            Ok(meta) => {
                ensure!(meta.is_dir(), "release path is not a directory");
                if release_build(&destination).as_deref() == Some(build_id) {
                    return Ok(destination);
                }
                ensure!(
                    read_build_marker(&destination.join(INSTALLING)).as_deref() == Some(build_id),
                    "release directory already exists"
                );
                fs::remove_dir_all(&destination)?;
            }
            Err(error) if error.kind() == io::ErrorKind::NotFound => (),
            Err(error) => return Err(error.into()),
        }

        let staging = update::private_tempdir(&self.root.join("versions"), EXTRACT_PREFIX)?;
        populate(staging.path())?;
        ensure!(
            update::has_build(&staging.path().join(BIN), build_id)?,
            "package executable does not match its build ID"
        );
        ensure!(
            !staging.path().join(MARKER).exists() && !staging.path().join(INSTALLING).exists(),
            "child package contains an installation marker"
        );
        state::write_secret(&staging.path().join(INSTALLING), build_id.as_bytes())?;
        // Install wheels at their final path so generated entry points keep working.
        fs::rename(staging.path(), &destination)?;
        let result: Result<()> = async {
            let mut check = tokio::process::Command::new(destination.join(BIN));
            check.arg("--version").stdout(Stdio::null());
            service::checked(check, "launch the packaged binary").await?;
            parts::repair_linux(&destination, previous.as_deref(), log_dir).await?;
            state::write_secret(&destination.join(MARKER), build_id.as_bytes())?;
            Ok(())
        }
        .await;
        if result.is_err() {
            let _ = fs::remove_dir_all(&destination);
        }
        result?;
        Ok(destination)
    }

    fn switch(&self, release: &Path) -> Result<()> {
        let relative = release.strip_prefix(&self.root)?;
        replace_link(&self.root.join("current"), relative)
    }

    fn finish(&self, release: &Path) -> Result<()> {
        match fs::remove_file(release.join(INSTALLING)) {
            Ok(()) => Ok(()),
            Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
            Err(error) => Err(error.into()),
        }
    }

    fn discard(&self, release: &Path) {
        if current(&self.root).is_ok_and(|active| active.as_deref() != Some(release))
            && release.join(INSTALLING).is_file()
        {
            let _ = fs::remove_dir_all(release);
        }
    }

    fn restore(&self, previous: Option<&Path>) -> Result<()> {
        match previous {
            Some(previous) => self.switch(previous),
            None => Ok(fs::remove_file(self.root.join("current"))?),
        }
    }

    fn prune(&self, current: &Path, previous: Option<&Path>) {
        let Ok(versions) = self.root.join("versions").read_dir() else {
            return;
        };
        for entry in versions.flatten() {
            let path = entry.path();
            if path == current || previous == Some(path.as_path()) {
                continue;
            }
            if entry.file_type().is_ok_and(|kind| kind.is_dir())
                && release_build(&path).is_some()
                && let Err(error) = fs::remove_dir_all(path)
            {
                tracing::warn!("could not prune a child release: {}", error.kind());
            }
        }
    }

    fn prune_after_install(&self, target: &Path, previous: Option<&Path>, homes: &[&Path]) {
        if previous != Some(target)
            && homes
                .iter()
                .all(|home| state::instance_running(home).is_ok_and(|running| !running))
        {
            self.prune(target, previous);
        }
    }
}

fn replace_link(link: &Path, target: &Path) -> Result<()> {
    let parent = link.parent().context("installation link has no parent")?;
    let stage = tempfile::tempdir_in(parent)?;
    symlink(target, stage.path().join("link"))?;
    fs::rename(stage.path().join("link"), link)?;
    Ok(())
}

fn launcher_target(link: &Path, root: &Path) -> Result<Option<PathBuf>> {
    let target = match fs::read_link(link) {
        Ok(target) => target,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(error).context("refusing to replace the existing child launcher"),
    };
    let mut path = link
        .parent()
        .context("launcher has no parent")?
        .join(&target);
    let resolved = loop {
        match path.canonicalize() {
            Ok(path) => break path,
            Err(error)
                if error.kind() == io::ErrorKind::NotFound
                    && path
                        .symlink_metadata()
                        .is_err_and(|error| error.kind() == io::ErrorKind::NotFound)
                    && path.file_name().is_some() =>
            {
                path.pop();
            }
            Err(error) => {
                return Err(error).context("could not resolve the existing child launcher");
            }
        }
    };
    ensure!(
        resolved.starts_with(root),
        "refusing to replace a child launcher outside the install root"
    );
    Ok(Some(target))
}

fn activate_package(
    install: &Install,
    target: &Path,
    previous: Option<&Path>,
    launcher: &Path,
) -> Result<()> {
    install.switch(target)?;
    let result = (|| {
        let old_launcher = launcher_target(launcher, &install.root)?;
        let link_target = install.root.join("current").join(BIN);
        if old_launcher.is_some() {
            replace_link(launcher, &link_target)?;
        } else {
            symlink(link_target, launcher)?;
        }
        if let Err(error) = install.finish(target) {
            let restore = match old_launcher {
                Some(path) => replace_link(launcher, &path),
                None => fs::remove_file(launcher).map_err(Into::into),
            };
            restore.with_context(|| format!("could not restore the launcher after {error:#}"))?;
            return Err(error);
        }
        Ok(())
    })();
    if let Err(error) = result {
        install
            .restore(previous)
            .with_context(|| format!("could not restore current after {error:#}"))?;
        return Err(error);
    }
    Ok(())
}

pub async fn install_package(systemd: bool) -> Result<()> {
    let exe = std::env::current_exe()?;
    let source = exe
        .parent()
        .and_then(Path::parent)
        .context("missing package directory")?;
    ensure!(
        source.join("install.sh").is_file()
            && source.file_name() == Some(OsStr::new(&format!("kotoyose-child-{VERSION}"))),
        "run install.sh from the extracted child package"
    );
    let home = state::home()?;
    let profile = std::env::home_dir().context("user home is unavailable")?;
    let install = Install::lock(root()?)?;
    let previous = current(&install.root)?;
    let launcher = profile.join(".local/bin/kotoyose-child-app");
    fs::create_dir_all(launcher.parent().expect("launcher directory"))?;
    launcher_target(&launcher, &install.root)?;
    let name = release_name(VERSION, update::build_id())?;
    let target = install
        .prepare(&name, update::build_id(), &install.root, |stage| {
            archive::copy_from(source, stage)
        })
        .await?;
    let activate = || activate_package(&install, &target, previous.as_deref(), &launcher);
    let result = if systemd {
        service::enable(activate).await
    } else {
        activate().map(|()| false)
    };
    if result.is_err() {
        install.discard(&target);
    }
    let enabled = result?;
    let service_home = profile.join(".local/share/kotoyose-child");
    install.prune_after_install(&target, previous.as_deref(), &[&home, &service_home]);
    println!("Installed {}", target.display());
    if enabled {
        println!("Start with: systemctl --user start kotoyose-child.service");
    } else {
        println!("Start with: ~/.local/bin/kotoyose-child-app run");
    }
    Ok(())
}

pub enum Reopen {
    Settings,
    Quiet,
}

pub struct Activation {
    install: Install,
    previous: PathBuf,
    target: PathBuf,
    reopen: Reopen,
}

impl Activation {
    pub async fn prepare(
        home: &Path,
        package: Option<update::Package>,
        reopen: Reopen,
    ) -> Result<Self> {
        let install = Install::lock(root()?)?;
        let previous = running(&install.root, &std::env::current_exe()?)
            .context("updates require a Linux package installed with install.sh")?;
        let current = current(&install.root)?.context("current child release is missing")?;
        let target = match package {
            Some(mut package) => {
                ensure!(
                    current == previous,
                    "restart into the current release before updating"
                );
                let offer = package.offer.clone();
                let name = release_name(&offer.version, &offer.build_id)?;
                let top = format!("kotoyose-child-{}", offer.version);
                let log = local_log::folder(home);
                install
                    .prepare(&name, &offer.build_id, &log, |stage| {
                        package.verify(|reader| archive::extract(reader, &top, stage))
                    })
                    .await?
            }
            None => current,
        };
        Ok(Self {
            install,
            previous,
            target,
            reopen,
        })
    }

    pub fn exec(self, home: &Path, args: &[OsString]) -> Result<()> {
        let result = self.exec_target(args);
        if matches!(self.reopen, Reopen::Settings) {
            let _ = state::write_secret(&home.join(REOPEN), b"");
        }
        result
    }

    fn exec_target(&self, args: &[OsString]) -> Result<()> {
        if let Err(error) = self.install.switch(&self.target) {
            self.install.discard(&self.target);
            return Err(error);
        }
        self.install.prune(&self.target, Some(&self.previous));
        let mut command = Command::new(self.target.join(BIN));
        command.args(args);
        if matches!(self.reopen, Reopen::Settings) {
            command.env(update::OPEN_SETTINGS, "1");
        } else {
            command.env_remove(update::OPEN_SETTINGS);
        }
        let error = command.exec();
        self.install
            .switch(&self.previous)
            .context("could not restore current after exec failed")?;
        self.install.discard(&self.target);
        Err(error).context("could not execute the new child build")
    }

    pub async fn delegate(self, home: &Path) -> Result<()> {
        drop(self.install);
        service::delegate(&self.previous.join(BIN), &self.target, home, self.reopen).await
    }
}

pub fn in_service() -> bool {
    std::env::var_os("INVOCATION_ID").is_some()
        && fs::read_to_string("/proc/self/cgroup")
            .is_ok_and(|groups| service::is_child_unit(&groups))
}

pub fn finish_running() {
    // A foreground exec can only finalize after the new process acquires the instance lock.
    let Ok(root) = root() else { return };
    let Ok(Some(running)) = std::env::current_exe().map(|exe| running(&root, &exe)) else {
        return;
    };
    if !running.join(INSTALLING).is_file() || in_service() {
        return;
    }
    if let Ok(install) = Install::lock(root)
        && current(&install.root).is_ok_and(|active| active.as_ref() == Some(&running))
    {
        let _ = install.finish(&running);
    }
}

pub fn take_reopen(home: &Path) -> bool {
    fs::remove_file(home.join(REOPEN)).is_ok()
}

pub fn open_browser(url: &str) {
    if (["DISPLAY", "WAYLAND_DISPLAY"]
        .iter()
        .any(|name| std::env::var_os(name).is_some_and(|value| !value.is_empty())))
        && (in_service() || std::env::var_os(update::OPEN_SETTINGS).is_some())
    {
        let url = url.to_owned();
        tokio::spawn(async move {
            let mut command = tokio::process::Command::new("xdg-open");
            command.arg(&url);
            let _ = service::checked(command, "open child settings").await;
        });
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn manual_installs_keep_the_running_release_across_two_switches() {
        let directory = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let install = Install::lock(directory.path().to_path_buf()).unwrap();
        let releases = ['a', 'b', 'c'].map(|byte| {
            let build_id = byte.to_string().repeat(40);
            let release = install
                .root
                .join("versions")
                .join(release_name(VERSION, &build_id).unwrap());
            fs::create_dir_all(release.join("bin")).unwrap();
            fs::write(release.join(BIN), b"exe").unwrap();
            fs::write(release.join(MARKER), build_id).unwrap();
            release
        });
        let [running, previous, target] = &releases;
        install.switch(running).unwrap();
        let instance = state::lock(home.path()).unwrap().unwrap();
        install.switch(previous).unwrap();
        install.prune_after_install(previous, Some(running), &[home.path()]);
        install.switch(target).unwrap();
        install.prune_after_install(target, Some(previous), &[home.path()]);
        assert!(releases.iter().all(|release| release.join(BIN).is_file()));
        drop(instance);
        install.prune_after_install(target, Some(previous), &[home.path()]);
        assert!(!running.exists());
        assert!(previous.exists() && target.exists());
    }

    #[test]
    fn launcher_accepts_only_missing_or_install_owned_links() {
        let directory = tempfile::tempdir().unwrap();
        let root = directory.path().join("install");
        fs::create_dir(&root).unwrap();
        let launcher = directory.path().join("launcher");
        assert_eq!(launcher_target(&launcher, &root).unwrap(), None);
        fs::write(&launcher, "owner's launcher").unwrap();
        assert!(launcher_target(&launcher, &root).is_err());
        assert_eq!(fs::read_to_string(&launcher).unwrap(), "owner's launcher");
        fs::remove_file(&launcher).unwrap();
        symlink("outside", &launcher).unwrap();
        assert!(launcher_target(&launcher, &root).is_err());
        fs::remove_file(&launcher).unwrap();
        symlink("install/current/bin/kotoyose-child-app", &launcher).unwrap();
        assert!(launcher_target(&launcher, &root).unwrap().is_some());
    }

    #[test]
    fn failed_exec_restores_current_and_marks_settings_for_reopening() {
        let directory = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let install = Install::lock(directory.path().to_path_buf()).unwrap();
        let previous = install.root.join("versions/old");
        let target = install.root.join("versions/new");
        let pointer = install.root.join("current");
        install.switch(&previous).unwrap();
        let activation = Activation {
            install,
            previous,
            target,
            reopen: Reopen::Settings,
        };
        assert!(activation.exec(home.path(), &["run".into()]).is_err());
        assert_eq!(pointer.read_link().unwrap(), Path::new("versions/old"));
        assert!(take_reopen(home.path()));
    }
}
