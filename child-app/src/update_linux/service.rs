use std::{
    ffi::{OsStr, OsString},
    fs, io,
    os::{
        linux::net::SocketAddrExt,
        unix::{
            ffi::OsStrExt,
            net::{SocketAddr, UnixDatagram},
        },
    },
    path::Path,
    process::Stdio,
    time::Duration,
};

use anyhow::{Context, Result, ensure};
use tokio::process::Command;

use super::{Install, REOPEN, Reopen, current, release_build, root, running};
use crate::{state, update};

const UNIT: &str = "kotoyose-child.service";
const MANAGED: &str = "# managed-by kotoyose";
const UNIT_BODY: &str = "\
[Unit]\nDescription=Kotoyose Child\n\n\
[Service]\nType=notify\nExecStart=%h/.local/share/kotoyose-child-app/current/bin/kotoyose-child-app run\n\
Restart=on-failure\nTimeoutStartSec=30s\nTimeoutStopSec=30s\n\n\
[Install]\nWantedBy=default.target\n";
const TIMEOUT: Duration = Duration::from_secs(150);

pub(super) fn is_child_unit(groups: &str) -> bool {
    groups
        .lines()
        .any(|line| line.split('/').any(|name| name == UNIT))
}

pub(super) async fn checked(mut command: Command, action: &str) -> Result<()> {
    let mut child = command
        .kill_on_drop(true)
        .spawn()
        .with_context(|| format!("could not {action}"))?;
    let status = tokio::time::timeout(TIMEOUT, child.wait())
        .await
        .with_context(|| format!("timed out trying to {action}"))??;
    ensure!(status.success(), "could not {action} ({status})");
    Ok(())
}

fn systemctl(action: &str) -> Command {
    let mut command = Command::new("systemctl");
    command.args(["--user", action]);
    command
}

pub(super) async fn enable(activate: impl FnOnce() -> Result<()>) -> Result<bool> {
    let mut probe = systemctl("show-environment");
    probe.stdout(Stdio::null()).stderr(Stdio::null());
    if checked(probe, "connect to the systemd user manager")
        .await
        .is_err()
    {
        activate()?;
        return Ok(false);
    }
    let config = std::env::var_os("XDG_CONFIG_HOME")
        .map(std::path::PathBuf::from)
        .filter(|path| path.is_absolute())
        .unwrap_or(
            std::env::home_dir()
                .context("user home is unavailable")?
                .join(".config"),
        );
    let units = config.join("systemd/user");
    fs::create_dir_all(&units)?;
    configure_unit(&units.join(UNIT), systemctl, activate).await?;
    Ok(true)
}

async fn configure_unit(
    unit: &Path,
    command: impl Fn(&str) -> Command,
    activate: impl FnOnce() -> Result<()>,
) -> Result<()> {
    let previous = match unit.symlink_metadata() {
        Ok(meta) => {
            ensure!(meta.is_file(), "refusing to overwrite a foreign {UNIT}");
            let bytes = fs::read(unit)?;
            let header = bytes
                .split(|&byte| byte == b'\n')
                .next()
                .unwrap_or_default();
            // The first installer shipped this exact unit without a header.
            ensure!(
                header.strip_suffix(b"\r").unwrap_or(header) == MANAGED.as_bytes()
                    || bytes == UNIT_BODY.as_bytes(),
                "refusing to overwrite a foreign {UNIT}"
            );
            Some((bytes, meta.permissions()))
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => None,
        Err(error) => return Err(error.into()),
    };
    let mut query = command("is-enabled");
    query.arg(UNIT).kill_on_drop(true);
    let status = tokio::time::timeout(TIMEOUT, query.output()).await??;
    let enabled = match (
        String::from_utf8_lossy(&status.stdout).trim(),
        status.status.code(),
    ) {
        ("enabled", Some(0)) => true,
        ("enabled-runtime" | "disabled" | "static" | "indirect" | "not-found", Some(0 | 1 | 4))
        | ("", Some(1 | 4)) => false,
        _ => anyhow::bail!("could not determine the enable state of {UNIT}"),
    };
    let contents = format!("{MANAGED}\n{UNIT_BODY}");
    let changed = previous
        .as_ref()
        .is_none_or(|(bytes, _)| bytes != contents.as_bytes());
    let mut enable_attempted = false;
    let result = async {
        if changed {
            state::write_secret(unit, contents.as_bytes())?;
        }
        checked(command("daemon-reload"), "reload systemd user units").await?;
        if !enabled {
            enable_attempted = true;
            let mut enable = command("enable");
            enable.arg(UNIT);
            checked(enable, "enable the child service").await?;
        }
        activate()
    }
    .await;
    if let Err(error) = result {
        // A failed enable can leave a link behind. Remove it while the new unit still exists.
        let disable = if enable_attempted {
            let mut disable = command("disable");
            disable.arg(UNIT);
            checked(disable, "undo enabling the child service").await
        } else {
            Ok(())
        };
        let restore = if changed {
            match previous {
                Some((bytes, permissions)) => state::write_secret(unit, &bytes)
                    .and_then(|()| Ok(fs::set_permissions(unit, permissions)?)),
                None => match fs::remove_file(unit) {
                    Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
                    result => result.map_err(Into::into),
                },
            }
        } else {
            Ok(())
        };
        let reload = checked(
            command("daemon-reload"),
            "reload the restored systemd user units",
        )
        .await;
        disable
            .and(restore)
            .and(reload)
            .with_context(|| format!("could not roll back {UNIT} after {error:#}"))?;
        return Err(error);
    }
    Ok(())
}

pub(super) async fn delegate(exe: &Path, target: &Path, home: &Path, reopen: Reopen) -> Result<()> {
    let name = target.file_name().context("missing target release")?;
    // The restart has to outlive the service's process group.
    let mut command = Command::new("systemd-run");
    command
        .args([
            "--user",
            "--quiet",
            "--collect",
            "--wait",
            "--property=Type=exec",
            "--",
        ])
        .arg(exe)
        .arg("restart-linux-service")
        .arg(name)
        .arg(escape_argument(home.as_os_str()));
    if matches!(reopen, Reopen::Settings) {
        command.arg("--open-settings");
    }
    checked(command, "run the child restart through systemd").await
}

fn escape_argument(arg: &OsStr) -> OsString {
    use std::os::unix::ffi::OsStringExt;
    let mut escaped = Vec::new();
    for byte in arg.as_bytes() {
        if *byte == b'$' {
            escaped.push(*byte);
        }
        escaped.push(*byte);
    }
    OsString::from_vec(escaped)
}

pub async fn restart_service(name: &OsStr, home: &Path, reopen: Reopen) -> Result<()> {
    let install = Install::lock(root()?)?;
    let previous = running(&install.root, &std::env::current_exe()?)
        .context("the restart helper must run from an installed release")?;
    ensure!(
        Path::new(name).file_name() == Some(name),
        "invalid target release"
    );
    let target = install.root.join("versions").join(name);
    ensure!(
        release_build(&target).is_some() && !target.symlink_metadata()?.is_symlink(),
        "target release is not installed"
    );
    let active = current(&install.root)?.context("current child release is missing")?;
    ensure!(
        active == previous || active == target,
        "current child release changed"
    );
    activate_service(&install, &previous, &target, home, reopen, || {
        restart_selected(&install.root, home)
    })
    .await
}

async fn restart_selected(root: &Path, home: &Path) -> Result<()> {
    let release = current(root)?.context("current child release is missing")?;
    let mut command = systemctl("restart");
    command.arg(UNIT);
    let result = checked(command, "restart the child service").await;
    if result.is_ok() || !state::instance_running(home).unwrap_or(false) {
        return result;
    }
    // A timed-out systemctl client can leave a job that already started the new process.
    let mut query = systemctl("show");
    query
        .args(["--property=MainPID", "--value", UNIT])
        .kill_on_drop(true);
    let started = tokio::time::timeout(Duration::from_secs(5), query.output())
        .await
        .ok()
        .and_then(Result::ok)
        .filter(|output| output.status.success())
        .and_then(|output| String::from_utf8(output.stdout).ok())
        .and_then(|pid| pid.trim().parse::<u32>().ok())
        .and_then(|pid| fs::read_link(format!("/proc/{pid}/exe")).ok())
        .is_some_and(|exe| exe == release.join(super::BIN));
    if started { Ok(()) } else { result }
}

async fn activate_service<F: Future<Output = Result<()>>>(
    install: &Install,
    previous: &Path,
    target: &Path,
    home: &Path,
    reopen: Reopen,
    restart: impl Fn() -> F,
) -> Result<()> {
    let reopen_settings = || {
        if matches!(reopen, Reopen::Settings) {
            state::write_secret(&home.join(REOPEN), b"")?;
        }
        Ok::<(), anyhow::Error>(())
    };
    if let Err(error) = reopen_settings().and_then(|()| install.switch(target)) {
        install.discard(target);
        return Err(error);
    }
    if let Err(error) = restart().await {
        install
            .switch(previous)
            .context("could not restore current after the service restart failed")?;
        install.discard(target);
        update::record_setup_error(home);
        let _ = reopen_settings();
        restart()
            .await
            .context("could not restart the previous child service")?;
        return Err(error);
    }
    if let Err(error) = install.finish(target) {
        tracing::warn!(
            "could not finalize the active child release: {}",
            crate::local_log::diagnostic(&error)
        );
    }
    install.prune(target, Some(previous));
    Ok(())
}

pub fn notify_ready() -> Result<()> {
    let Some(socket) = std::env::var_os("NOTIFY_SOCKET").filter(|_| super::in_service()) else {
        return Ok(());
    };
    let address = match socket.as_bytes().strip_prefix(b"@") {
        Some(name) => SocketAddr::from_abstract_name(name)?,
        None => SocketAddr::from_pathname(socket)?,
    };
    UnixDatagram::unbound()?.send_to_addr(b"READY=1", &address)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    fn fake_systemctl(directory: &Path, action: &str) -> Command {
        let mut command = Command::new("/bin/sh");
        command
            .arg("-c")
            .arg(
                r#"
                case "$2" in
                    is-enabled)
                        if [ -f "$1/enabled" ]; then
                            echo enabled
                        else
                            echo disabled
                            exit 1
                        fi ;;
                    enable)
                        touch "$1/enabled"
                        [ ! -f "$1/fail-enable" ] ;;
                    disable) rm "$1/enabled" ;;
                    daemon-reload) ;;
                    *) exit 2 ;;
                esac
            "#,
            )
            .arg("systemctl")
            .arg(directory)
            .arg(action);
        command
    }

    #[tokio::test]
    async fn managed_unit_can_change_and_foreign_unit_is_preserved() {
        let directory = tempfile::tempdir().unwrap();
        let unit = directory.path().join(UNIT);
        let command = |action: &str| fake_systemctl(directory.path(), action);
        fs::write(&unit, b"[Unit]\nDescription=Foreign unit\n").unwrap();
        assert!(
            configure_unit(&unit, command, || unreachable!())
                .await
                .is_err()
        );
        assert_eq!(
            fs::read(&unit).unwrap(),
            b"[Unit]\nDescription=Foreign unit\n"
        );
        assert!(!directory.path().join("enabled").exists());

        fs::write(&unit, format!("{MANAGED}\n[Unit]\nDescription=Old unit\n")).unwrap();
        configure_unit(&unit, command, || Ok(())).await.unwrap();
        assert_eq!(
            fs::read_to_string(&unit).unwrap(),
            format!("{MANAGED}\n{UNIT_BODY}")
        );
        assert!(directory.path().join("enabled").is_file());
    }

    #[tokio::test]
    async fn unit_and_enable_state_roll_back_after_enable_or_activation_fails() {
        for (previous, previously_enabled, enable_fails) in [
            (None, false, true),
            (Some(format!("{MANAGED}\nOriginal")), false, false),
            (Some(UNIT_BODY.to_owned()), true, false),
        ] {
            let directory = tempfile::tempdir().unwrap();
            let unit = directory.path().join(UNIT);
            if let Some(contents) = &previous {
                fs::write(&unit, contents).unwrap();
            }
            let enabled = directory.path().join("enabled");
            if previously_enabled {
                fs::write(&enabled, b"").unwrap();
            }
            if enable_fails {
                fs::write(directory.path().join("fail-enable"), b"").unwrap();
            }
            let result = configure_unit(
                &unit,
                |action| fake_systemctl(directory.path(), action),
                || {
                    assert!(!enable_fails);
                    anyhow::bail!("could not activate package")
                },
            )
            .await;

            assert!(result.is_err());
            assert_eq!(fs::read_to_string(&unit).ok(), previous);
            assert_eq!(enabled.exists(), previously_enabled);
        }
    }

    #[test]
    fn identifies_only_the_child_unit_and_builds_a_direct_restart_command() {
        assert!(is_child_unit(concat!(
            "0::/user.slice/user",
            "@1000.service/app.slice/kotoyose-child.service\n"
        )));
        assert!(!is_child_unit(concat!(
            "0::/user.slice/user",
            "@1000.service/app.slice/kotoyose-child-other.service\n"
        )));
        let mut command = systemctl("restart");
        command.arg(UNIT);
        let command = command.as_std();
        assert_eq!(command.get_program(), "systemctl");
        assert_eq!(
            command.get_args().collect::<Vec<_>>(),
            ["--user", "restart", UNIT]
        );
    }

    #[tokio::test]
    async fn failed_service_start_restores_the_previous_target_and_restarts_it() {
        let install_dir = tempfile::tempdir().unwrap();
        let home = tempfile::tempdir().unwrap();
        let install = Install::lock(install_dir.path().to_path_buf()).unwrap();
        let previous = install.root.join("versions/old");
        let target = install.root.join("versions/new");
        install.switch(&previous).unwrap();
        let attempts = Cell::new(0);
        let result = activate_service(
            &install,
            &previous,
            &target,
            home.path(),
            Reopen::Settings,
            || async {
                let current = fs::read_link(install.root.join("current")).unwrap();
                attempts.set(attempts.get() + 1);
                if attempts.get() == 1 {
                    assert_eq!(current, Path::new("versions/new"));
                    anyhow::bail!("new service did not start");
                }
                assert_eq!(current, Path::new("versions/old"));
                Ok(())
            },
        )
        .await;
        assert!(result.is_err());
        assert_eq!(attempts.get(), 2);
        assert!(home.path().join(REOPEN).is_file());
        let update = update::Update::default();
        update.restore_error(home.path());
        assert!(matches!(update.view().error, Some(update::Error::Setup)));
    }
}
