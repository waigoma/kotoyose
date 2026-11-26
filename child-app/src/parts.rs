#[cfg(windows)]
use std::{env, os::windows::process::CommandExt};
#[cfg(any(windows, target_os = "linux"))]
use std::{ffi::OsStr, fs};
#[cfg(any(windows, target_os = "linux", test))]
use std::{
    fs::File,
    io::{self, Write},
    process::ExitStatus,
};
use std::{path::Path, process::Command};

#[cfg(any(windows, target_os = "linux", test))]
use anyhow::Result;
#[cfg(any(windows, target_os = "linux"))]
use anyhow::{Context, ensure};

#[cfg(any(windows, test))]
use crate::local_log;
#[cfg(any(windows, target_os = "linux"))]
use crate::messages;
#[cfg(any(windows, target_os = "linux", test))]
use crate::{components, state};

pub fn pip(python: &Path) -> Command {
    let mut command = Command::new(python);
    #[cfg(windows)]
    command.creation_flags(windows_sys::Win32::System::Threading::CREATE_NO_WINDOW);
    command
        .env(
            "PIP_CONFIG_FILE",
            if cfg!(windows) { "nul" } else { "/dev/null" },
        )
        .args([
            "-I",
            "-u",
            "-m",
            "pip",
            "--isolated",
            "--disable-pip-version-check",
            "--no-input",
            "--no-color",
            "--timeout",
            "10",
            "--retries",
            "2",
            "--keyring-provider",
            "disabled",
        ]);
    command
}

pub const INSTALL_LOCKED: &[&str] = &[
    "install",
    "--require-hashes",
    "--no-deps",
    "--only-binary=:all:",
    "--force-reinstall",
    "--index-url",
    "https://pypi.org/simple",
    "--no-warn-script-location",
    "--progress-bar",
    "off",
    "-r",
];

#[cfg(any(windows, target_os = "linux", test))]
struct RepairLog(File);

#[cfg(any(windows, target_os = "linux", test))]
impl RepairLog {
    fn create(folder: &Path) -> Result<Self> {
        state::private_dir(folder)?;
        let temp = tempfile::NamedTempFile::new_in(folder)?;
        state::protect_secret(temp.path())?;
        Ok(Self(temp.persist(folder.join("repair.log"))?))
    }

    fn write(&self, line: &str) -> io::Result<()> {
        writeln!(&self.0, "{line}")
    }

    fn record(&self, line: &str) -> io::Result<()> {
        self.write(line)?;
        if cfg!(windows) {
            let _ = writeln!(io::stdout().lock(), "{line}");
        }
        Ok(())
    }
}

#[cfg(any(windows, target_os = "linux", test))]
async fn run_logged_pip(
    command: tokio::process::Command,
    python: &Path,
    log: &RepairLog,
) -> Result<ExitStatus> {
    let status = components::run_pip(
        components::start_pip(command)?,
        state::python_root(python),
        |line| log.record(line),
        None,
        std::future::pending(),
    )
    .await?;
    log.record(&format!("pip: {status}"))?;
    Ok(status)
}

#[cfg(windows)]
pub async fn repair(home: &Path, previous_version: Option<&OsStr>) -> Result<()> {
    state::private_dir(home)?;
    let _lock = state::lock(home)?.context(messages::windows::REPAIR_LOCKED)?;
    let log =
        RepairLog::create(&local_log::folder(home)).context("Could not open logs\\repair.log.")?;
    let result = repair_runtime(previous_version, &log).await;
    if let Err(error) = &result {
        let reason = crate::component_output::redact(&local_log::diagnostic(error), None);
        log.write(&format!("Repair failed: {reason}"))
            .context("Could not write logs\\repair.log.")?;
    }
    result
}

#[cfg(windows)]
async fn repair_runtime(previous_version: Option<&OsStr>, log: &RepairLog) -> Result<()> {
    let exe = env::current_exe()?;
    let runtime = components::Runtime::installed().context(messages::windows::REPAIR_SOURCE)?;
    let mut previous_python = None;
    if let Some(previous_version) = previous_version {
        let previous_version = previous_version
            .to_str()
            .filter(|version| {
                version.starts_with(|ch: char| ch.is_ascii_digit())
                    && version
                        .bytes()
                        .all(|ch| ch.is_ascii_alphanumeric() || matches!(ch, b'.' | b'-'))
            })
            .context("Invalid previous installation version.")?;
        let current = exe
            .parent()
            .and_then(Path::parent)
            .context(messages::windows::REPAIR_SOURCE)?;
        let previous = current.with_file_name(previous_version);
        if !current
            .file_name()
            .is_some_and(|name| name.eq_ignore_ascii_case(previous_version))
        {
            previous_python = Some(previous.join("python/python.exe"));
        }
    }
    install_runtime(&runtime, previous_python.as_deref(), log).await?;
    log.record(messages::windows::REPAIR_DONE)?;
    Ok(())
}

#[cfg(target_os = "linux")]
pub async fn repair_linux(version: &Path, previous: Option<&Path>, log_dir: &Path) -> Result<()> {
    let exe = version.join("bin/kotoyose-child-app");
    let runtime = components::Runtime {
        python: state::bundled_python(&exe).context("Bundled Python is missing.")?,
        payload: exe.with_file_name("parts"),
    };
    let previous_python =
        previous.and_then(|path| state::bundled_python(&path.join("bin/kotoyose-child-app")));
    let log = RepairLog::create(log_dir)?;
    let result = install_runtime(&runtime, previous_python.as_deref(), &log).await;
    if let Err(error) = &result {
        log.write(&format!(
            "Repair failed: {}",
            crate::local_log::diagnostic(error)
        ))?;
    }
    result
}

#[cfg(any(windows, target_os = "linux"))]
async fn install_runtime(
    runtime: &components::Runtime,
    previous_python: Option<&Path>,
    log: &RepairLog,
) -> Result<()> {
    let python = &runtime.python;
    let payload = &runtime.payload;
    let requirements = payload.join("requirements.txt");
    let wheels: Vec<_> = fs::read_dir(payload)?
        .map(|entry| entry.map(|entry| entry.path()))
        .collect::<io::Result<Vec<_>>>()?
        .into_iter()
        .filter(|path| path.extension() == Some(OsStr::new("whl")))
        .collect();
    let [wheel] = wheels.as_slice() else {
        anyhow::bail!(messages::REPAIR_WHEEL);
    };
    if let Some(previous) = previous_python {
        components::carry_provider_repairs(previous, python, &state::site_packages(previous))
            .context(messages::COMPONENT_REPAIR_STATE)?;
    }
    let ready = python.with_file_name("kotoyose-ready");
    match fs::remove_file(&ready) {
        Err(error) if error.kind() == io::ErrorKind::NotFound => (),
        result => result?,
    }

    log.record("Installing the bundled base runtime packages.")?;
    let mut command = tokio::process::Command::from(pip(python));
    command
        .args(["install", "--no-index", "--find-links"])
        .arg(payload.join("runtime"))
        .args([
            "--require-hashes",
            "--no-deps",
            "--force-reinstall",
            "--no-warn-script-location",
            "-r",
        ])
        .arg(requirements);
    let status = run_logged_pip(command, python, log).await?;
    ensure!(status.success(), messages::repair_failed(status));

    log.record("Installing the bundled Kotoyose wheel.")?;
    let mut command = tokio::process::Command::from(pip(python));
    command
        .args([
            "install",
            "--no-index",
            "--no-deps",
            "--force-reinstall",
            "--no-warn-script-location",
        ])
        .arg(wheel);
    let status = run_logged_pip(command, python, log).await?;
    ensure!(status.success(), messages::repair_failed(status));

    fs::write(ready, [])?;
    Ok(())
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::{fs, os::unix::fs::PermissionsExt};

    #[tokio::test]
    async fn repair_log_captures_both_pip_streams_and_exit_status_then_replaces_the_file() {
        let home = tempfile::tempdir().unwrap();
        let path = local_log::folder(home.path()).join("repair.log");
        let log = RepairLog::create(&local_log::folder(home.path())).unwrap();
        let mut fake_pip = tokio::process::Command::new("/bin/sh");
        fake_pip.args([
            "-c",
            concat!(
                "printf 'pip stdout\n'; printf '%s' ",
                "'pip stderr: https://name:fixture,password@proxy.example/connect?key=fixture-key' ",
                ">&2; exit 7",
            ),
        ]);
        let status = run_logged_pip(fake_pip, Path::new("/opt/python/bin/python"), &log)
            .await
            .unwrap();
        assert_eq!(status.code(), Some(7));
        let contents = fs::read_to_string(&path).unwrap();
        assert!(contents.contains("pip stdout\n"));
        assert!(contents.contains("pip stderr: https://proxy.example/connect\n"));
        assert!(contents.ends_with("pip: exit status: 7\n"));
        assert_eq!(
            fs::metadata(&path).unwrap().permissions().mode() & 0o777,
            0o600
        );
        drop(log);

        let next = RepairLog::create(&local_log::folder(home.path())).unwrap();
        next.write("Repair failed: could not start pip").unwrap();
        assert_eq!(
            fs::read_to_string(path).unwrap(),
            "Repair failed: could not start pip\n"
        );
    }
}
