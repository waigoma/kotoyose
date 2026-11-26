use crate::messages::windows as messages;
use std::{
    ffi::{OsStr, OsString},
    io,
    os::windows::{
        ffi::{OsStrExt, OsStringExt},
        process::CommandExt,
    },
    path::{Path, PathBuf},
    process::Command,
    ptr::{null, null_mut},
};

use anyhow::{Context, Result, ensure};
use windows_sys::Win32::{
    Foundation::{ERROR_FILE_NOT_FOUND, ERROR_SUCCESS, WIN32_ERROR},
    System::{
        Registry::{
            HKEY, HKEY_CURRENT_USER, KEY_SET_VALUE, REG_OPTION_NON_VOLATILE, REG_SZ, RRF_RT_REG_SZ,
            RegCloseKey, RegCreateKeyExW, RegDeleteValueW, RegGetValueW, RegOpenKeyExW,
            RegSetValueExW,
        },
        SystemInformation::GetSystemDirectoryW,
        Threading::CREATE_NO_WINDOW,
    },
};

const RUN: &str = r"Software\Microsoft\Windows\CurrentVersion\Run";
const VALUE: &str = "KotoyoseChild";
const LEGACY: &str = r"\KotoyoseChild";

pub const BACKGROUND: &str = "kotoyose-child-background.exe";

pub fn wide(value: impl AsRef<OsStr>) -> Vec<u16> {
    value.as_ref().encode_wide().chain([0]).collect()
}

fn check(code: WIN32_ERROR) -> io::Result<()> {
    if code == ERROR_SUCCESS {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(code as i32))
    }
}

struct Key(HKEY);

impl Key {
    fn open() -> Result<Self> {
        let mut handle = null_mut();
        check(unsafe {
            RegCreateKeyExW(
                HKEY_CURRENT_USER,
                wide(RUN).as_ptr(),
                0,
                null(),
                REG_OPTION_NON_VOLATILE,
                KEY_SET_VALUE,
                null(),
                &mut handle,
                null_mut(),
            )
        })
        .context("cannot open autostart registry key")?;
        Ok(Self(handle))
    }
    fn existing() -> Result<Option<Self>> {
        let mut handle = null_mut();
        let code = unsafe {
            RegOpenKeyExW(
                HKEY_CURRENT_USER,
                wide(RUN).as_ptr(),
                0,
                KEY_SET_VALUE,
                &mut handle,
            )
        };
        if code == ERROR_FILE_NOT_FOUND {
            return Ok(None);
        }
        check(code).context("cannot open autostart registry key")?;
        Ok(Some(Self(handle)))
    }
}

impl Drop for Key {
    fn drop(&mut self) {
        unsafe { RegCloseKey(self.0) };
    }
}

pub fn enabled() -> Result<bool> {
    let mut length = 0;
    let code = unsafe {
        RegGetValueW(
            HKEY_CURRENT_USER,
            wide(RUN).as_ptr(),
            wide(VALUE).as_ptr(),
            RRF_RT_REG_SZ,
            null_mut(),
            null_mut(),
            &mut length,
        )
    };
    if code == ERROR_FILE_NOT_FOUND {
        return Ok(false);
    }
    check(code).context("cannot read autostart setting")?;
    Ok(length > 2)
}

pub fn installed_bin() -> Option<PathBuf> {
    const ARP: &str = r"Software\Microsoft\Windows\CurrentVersion\Uninstall\KotoyoseChild";
    let field = |name: &str| {
        let mut value = [0u16; 1024];
        let mut size = std::mem::size_of_val(&value) as u32;
        let status = unsafe {
            RegGetValueW(
                HKEY_CURRENT_USER,
                wide(ARP).as_ptr(),
                wide(name).as_ptr(),
                RRF_RT_REG_SZ,
                null_mut(),
                value.as_mut_ptr().cast(),
                &mut size,
            )
        };
        if status != ERROR_SUCCESS || size <= 2 {
            return None;
        }
        Some(OsString::from_wide(&value[..size as usize / 2 - 1]))
    };
    let root = PathBuf::from(field("InstallRoot")?);
    let folder = field("InstalledBuildDirectory").or_else(|| field("DisplayVersion"))?;
    let folder = Path::new(&folder);
    if !root.is_absolute() || folder.file_name() != Some(folder.as_os_str()) {
        return None;
    }
    Some(root.join(folder).join("bin"))
}

fn background_path() -> Result<PathBuf> {
    let path = std::env::current_exe()?.with_file_name(BACKGROUND);
    ensure!(
        path.is_absolute() && path.is_file(),
        "{BACKGROUND} must be next to this executable"
    );
    Ok(path)
}

fn quoted(path: &Path) -> Vec<u16> {
    let mut value = OsString::from("\"");
    value.push(path);
    value.push("\"");
    wide(value)
}

pub enum Outcome {
    Disabled,
    Enabled,
    EnabledAndMigrated,
    EnabledWithWarning(anyhow::Error),
}

impl Outcome {
    pub fn log(&self) -> String {
        match self {
            Self::Disabled => "autostart disabled".into(),
            Self::Enabled => "autostart enabled".into(),
            Self::EnabledAndMigrated => {
                "autostart enabled; legacy KotoyoseChild task removed".into()
            }
            Self::EnabledWithWarning(error) => {
                format!("autostart enabled; legacy task cleanup failed: {error}")
            }
        }
    }

    pub fn ui(&self) -> String {
        match self {
            Self::Disabled => messages::AUTOSTART_DISABLED.into(),
            Self::Enabled => messages::AUTOSTART_ENABLED.into(),
            Self::EnabledAndMigrated => messages::AUTOSTART_MIGRATED.into(),
            Self::EnabledWithWarning(error) => messages::autostart_warning(error),
        }
    }
}

pub fn set(enabled: bool) -> Result<Outcome> {
    if !enabled {
        if let Some(key) = Key::existing()? {
            let code = unsafe { RegDeleteValueW(key.0, wide(VALUE).as_ptr()) };
            if code != ERROR_FILE_NOT_FOUND {
                check(code).context("cannot remove autostart setting")?;
            }
        }
        return Ok(Outcome::Disabled);
    }

    enable(write_run, remove_legacy)
}

fn enable(
    write_run: impl FnOnce() -> Result<()>,
    remove_legacy: impl FnOnce() -> Result<bool>,
) -> Result<Outcome> {
    write_run()?;
    Ok(match remove_legacy() {
        Ok(true) => Outcome::EnabledAndMigrated,
        Ok(false) => Outcome::Enabled,
        Err(error) => Outcome::EnabledWithWarning(error),
    })
}

fn write_run() -> Result<()> {
    let path = background_path()?;
    let key = Key::open()?;
    let command = quoted(&path);
    check(unsafe {
        RegSetValueExW(
            key.0,
            wide(VALUE).as_ptr(),
            0,
            REG_SZ,
            command.as_ptr().cast(),
            (command.len() * size_of::<u16>()) as u32,
        )
    })
    .context("cannot register autostart")
}

fn remove_legacy() -> Result<bool> {
    let mut buffer = [0u16; 260];
    let size = unsafe { GetSystemDirectoryW(buffer.as_mut_ptr(), buffer.len() as u32) } as usize;
    ensure!(
        size > 0 && size < buffer.len(),
        "cannot resolve Windows system directory"
    );
    let path = PathBuf::from(OsString::from_wide(&buffer[..size])).join("schtasks.exe");
    // Enumerating avoids interpreting a localized "task not found" error as absence.
    let query = Command::new(&path)
        .creation_flags(CREATE_NO_WINDOW)
        .args(["/Query", "/FO", "CSV", "/NH"])
        .output()
        .context("cannot query Task Scheduler")?;
    ensure!(
        query.status.success(),
        "could not check for legacy KotoyoseChild task"
    );
    let first_column = format!("\"{LEGACY}\",");
    if !query.stdout.split(|b| *b == b'\n').any(|row| {
        row.get(..first_column.len())
            .is_some_and(|prefix| prefix.eq_ignore_ascii_case(first_column.as_bytes()))
    }) {
        return Ok(false);
    }
    let delete = Command::new(path)
        .creation_flags(CREATE_NO_WINDOW)
        .args(["/Delete", "/TN", LEGACY, "/F"])
        .output()
        .context("could not remove legacy KotoyoseChild task")?;
    ensure!(
        delete.status.success(),
        "could not remove legacy KotoyoseChild task"
    );
    Ok(true)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::cell::Cell;

    #[test]
    fn registration_failure_leaves_legacy_task_alone() {
        let result = enable(
            || anyhow::bail!("write failed"),
            || panic!("must preserve legacy task"),
        );
        assert!(result.is_err());
    }

    #[test]
    fn cleanup_failure_preserves_successful_registration_and_reports_it() {
        let registered = Cell::new(false);
        let result = enable(
            || {
                registered.set(true);
                Ok(())
            },
            || {
                assert!(registered.get());
                anyhow::bail!("delete failed")
            },
        )
        .unwrap();
        assert!(result.log().contains("delete failed"));
    }
}
