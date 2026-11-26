// User-facing messages shared by the settings server, desktop, and repair command.
pub const WEB_TIMEOUT: &str = "The settings request timed out.";
pub const INVALID_INPUT: &str = "Check the format and size of your input.";
pub const SETTINGS_OPEN: &str = "Could not open Settings.";
pub const INVALID_CODE: &str = "This Settings code is invalid. Open Settings again from the tray.";
pub const POLICY_LOAD: &str = "Could not load the policy.";
pub const POLICY_VALIDATE: &str = "Could not validate the policy.";
pub const POLICY_SAVE: &str = "Could not save policy.json.";
pub const POLICY_READ: &str = "Could not read policy.json. Enter a policy and save it again.";
pub const POLICY_UTF8: &str = "policy.json must be UTF-8. Enter a policy and save it again.";
pub const POLICY_INVALID: &str = "policy.json is invalid. Enter a policy and save it again.";
pub const ROOT_ABSOLUTE: &str = "Enter an absolute path for each workspace folder.";
pub const ROOT_PARENT: &str = "Workspace folder paths cannot contain '..'.";
pub const ROOT_MISSING: &str = "A workspace folder is missing or unreadable.";
pub const ROOT_INVALID: &str = "A workspace folder path is invalid.";
pub const ADAPTER_MISSING: &str = "You can only add adapters detected on this machine.";
pub const MCP_INVALID: &str = "Check MCP names, commands, arguments, and environment variables.";
pub const MCP_NAME_INVALID: &str =
    "Use 1 to 64 ASCII letters, digits, '_', '.', or '-' for MCP names.";
pub const POLICY_CONTROL_CHARS: &str =
    "The policy contains control characters that cannot be saved.";
pub const SETTINGS_LOAD: &str = "Could not load Settings.";
pub const SETTINGS_SAVE: &str = "Could not save app.json.";
pub const SETTINGS_READ: &str = "Could not read app.json. Fix the file before saving Settings.";
pub const FILE_CONFLICT: &str =
    "The file changed while you were editing it. Reload it and try again.";
pub const LOGS_LOAD: &str = "Could not load the logs.";
pub const WORK_ACTIVE: &str = "Work is active. Check its status and try again.";
pub const APP_UNAVAILABLE: &str = "The app is unavailable.";
#[cfg(windows)]
pub const AUTOSTART_CHANGE: &str = "Could not change start at sign-in.";
#[cfg(not(windows))]
pub const AUTOSTART_WINDOWS: &str = "Start at sign-in is available on Windows.";
#[cfg(windows)]
pub const LOGS_OPEN: &str = "Could not open the log folder.";
#[cfg(not(windows))]
pub const LOGS_WINDOWS: &str = "The log folder can be opened from Settings on Windows.";
pub const APP_RESTART: &str = "Restarting Kotoyose Child.";
pub const RESTART_UNNEEDED: &str = "The app already uses the installed build.";
pub const UPDATE_UNAVAILABLE: &str = "The update is unavailable. Refresh Settings and try again.";
pub const APP_EXIT: &str = "Closing Kotoyose Child.";
pub const PYTHON_START: &str = "Could not start the bundled Python runtime.";
pub const COMPONENT_PROBE_FAILED: &str = "Could not check the Python runtime for components.";
pub const COMPONENT_RUNTIME_REQUIRED: &str =
    "Install or repair the bundled Python runtime before managing packages.";
pub const COMPONENT_PINS_MISSING: &str = "The locked package list is missing. Run Setup again.";
pub const COMPONENT_TIMEOUT: &str = "The package operation timed out and may have left packages partially changed. Select Repair to reinstall this provider.";
pub const COMPONENT_REPAIR_STATE: &str = "Could not update the provider's repair status.";
pub const COMPONENT_PIP_FAILED: &str = "The package operation failed. See the progress log for details, then select Repair to reinstall this provider.";
pub const COMPONENT_PACKAGES_CHANGED: &str =
    "Package operation complete. Reconnecting to the host.";
pub const COMPONENT_CLI_CHANGED: &str = "CLI preference saved. Reconnecting to the host.";
pub const COMPONENT_CLI_UPDATED: &str = "Managed CLI updated.";
pub const COMPONENT_CLI_FAILED: &str =
    "Could not change the managed CLI. Check that it is available and idle.";
pub const COMPONENT_UNKNOWN: &str = "Unknown provider.";
pub const COMPONENT_BUSY: &str = "A component change is already in progress.";

pub const LOGIN_START_FAILED: &str = "Could not start local login.";
pub const LOGIN_NOT_RUNNING: &str = "No local login is running.";
pub const LOGIN_NOT_WAITING: &str = "No local login is waiting for input.";
pub const LOGIN_CODE_INVALID: &str = "Enter the authorization code from the sign-in page.";

pub fn enrolled(name: &str) -> String {
    format!("Enrolled {name}.")
}

#[cfg(windows)]
pub fn drive_warning(path: &std::path::Path) -> String {
    format!(
        "Workspace folder {} allows access to the entire drive.",
        path.display()
    )
}

#[cfg(any(windows, target_os = "linux"))]
pub const REPAIR_WHEEL: &str = "Could not identify the bundled Kotoyose wheel.";

#[cfg(any(windows, target_os = "linux"))]
pub fn repair_failed(status: std::process::ExitStatus) -> String {
    format!("Could not install the runtime packages ({status}).")
}

#[cfg(windows)]
pub mod windows {
    pub const REPAIR_SOURCE: &str = "Run repair from the installed Kotoyose Child app.";
    pub const REPAIR_LOCKED: &str = "Exit Kotoyose Child from the system tray, then try again.";
    pub const REPAIR_DONE: &str =
        "The runtime is ready. You can open Kotoyose Child from the Start menu.";
    pub const AUTOSTART_DISABLED: &str = "Start at sign-in is off.";
    pub const AUTOSTART_ENABLED: &str = "Start at sign-in is on.";
    pub const AUTOSTART_MIGRATED: &str =
        "Start at sign-in is on. The old KotoyoseChild scheduled task was removed.";
    pub const CONNECTED: &str = "Connected";
    pub const CONNECTING: &str = "Connecting";
    pub const WAITING: &str = "Waiting to reconnect";
    pub const NEEDS_ENROLLMENT: &str = "Enrollment required";
    pub const STOPPED: &str = "Disconnected";
    pub const SHOW_STATUS: &str = "Show status";
    pub const OPEN_SETTINGS: &str = "Open Settings";
    pub const OPEN_LOGS: &str = "Open log folder";
    pub const RESTART: &str = "Restart";
    pub const EXIT: &str = "Exit";
    pub const LOGS_CREATE: &str = "Could not create the log folder.";
    pub const APP_BUSY: &str = "Kotoyose Child is busy. Try again shortly.";

    pub fn autostart_warning(error: impl std::fmt::Display) -> String {
        format!("Start at sign-in is on. Could not confirm removal of the old task.\n{error}")
    }

    pub fn startup_error(error: &anyhow::Error) -> String {
        format!("Could not start Kotoyose Child.\n{error:#}")
    }

    pub fn tray_status(label: &str) -> String {
        format!("{SHOW_STATUS} ({label})")
    }

    pub fn tray_host(host: &str) -> String {
        format!("\nHost {host}")
    }

    pub fn tray_last_error(reason: &str) -> String {
        format!("\n\nLast error\n{reason}")
    }

    pub fn tray_interrupt(restart: bool, work: usize) -> String {
        let action = if restart { RESTART } else { EXIT };
        format!("{action} and interrupt {work} active or queued jobs?")
    }

    pub fn shell_error(code: isize) -> String {
        format!("Windows could not open the requested location (error {code}).")
    }

    pub fn open_error(problem: impl std::fmt::Display) -> String {
        format!("{}\n{problem}", super::SETTINGS_OPEN)
    }

    pub fn action_error(message: &str) -> String {
        format!("Could not complete the operation.\n{message}")
    }
}
