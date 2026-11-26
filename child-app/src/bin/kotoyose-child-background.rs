#![cfg_attr(windows, windows_subsystem = "windows")]

#[cfg(windows)]
fn main() -> std::process::ExitCode {
    kotoyose_child_app::background()
}

#[cfg(not(windows))]
fn main() -> std::process::ExitCode {
    kotoyose_child_app::cli()
}
