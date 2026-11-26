#[cfg(windows)]
mod autostart;
mod component_output;
mod components;
mod control;
mod declarations;
mod desktop;
mod local_log;
mod messages;
mod parts;
mod policy;
#[cfg(windows)]
mod tray;
mod web;

mod enrollment;
mod frame;
mod host_mcp;
mod instance;
mod link;
mod outbox;
mod process;
mod runner;
mod session;
mod state;
mod update;
#[cfg(target_os = "linux")]
mod update_linux;

use std::{ffi::OsString, future::Future, path::Path, time::Duration};

use anyhow::{Result, anyhow, bail};
use tokio::{sync::watch, time::sleep};
use tracing_subscriber::{
    filter::{LevelFilter, Targets},
    layer::SubscriberExt,
    util::SubscriberInitExt,
};

use crate::{
    control::{Connection, Control},
    link::Identity,
};

const VERSION: &str = env!("KOTOYOSE_PACKAGE_VERSION");

struct Backoff(Duration);

impl Backoff {
    fn new() -> Self {
        Self(Duration::from_millis(500))
    }

    fn next(&mut self, error: &anyhow::Error) -> Duration {
        let (floor, cap) = if error.is::<link::SlowRetry>() {
            (Duration::from_secs(5), Duration::from_secs(300))
        } else {
            (Duration::from_millis(500), Duration::from_secs(30))
        };
        let delay = self.0.clamp(floor, cap);
        self.0 = (delay * 2).min(cap);
        delay
    }

    fn session_ended(&mut self, uptime: Duration) {
        if uptime >= Duration::from_secs(30) {
            *self = Self::new();
        }
    }

    async fn wait(&mut self, error: &anyhow::Error, stopping: &mut watch::Receiver<bool>) -> bool {
        let delay = self.next(error);
        tracing::warn!(
            "host link: {}; retrying in {:.1}s",
            link::status_error(error),
            delay.as_secs_f64()
        );
        until_stopped(stopping, sleep(delay)).await.is_some()
    }
}

async fn until_stopped<T>(
    stopping: &mut watch::Receiver<bool>,
    work: impl Future<Output = T>,
) -> Option<T> {
    tokio::select! {
        biased;
        _ = stopping.wait_for(|stop| *stop) => None,
        result = work => Some(result),
    }
}

async fn signal() -> std::io::Result<()> {
    #[cfg(unix)]
    {
        let mut terminate =
            tokio::signal::unix::signal(tokio::signal::unix::SignalKind::terminate())?;
        let mut hangup = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::hangup())?;
        tokio::select! {
            interrupt = tokio::signal::ctrl_c() => interrupt,
            _ = terminate.recv() => Ok(()),
            _ = hangup.recv() => Ok(()),
        }
    }
    #[cfg(windows)]
    {
        let mut console_break = tokio::signal::windows::ctrl_break()?;
        tokio::select! {
            interrupt = tokio::signal::ctrl_c() => interrupt,
            _ = console_break.recv() => Ok(()),
        }
    }
}

fn watch_stop() -> (watch::Sender<bool>, watch::Receiver<bool>) {
    let (stop, stopping) = watch::channel(false);
    let signal_stop = stop.clone();
    tokio::task::spawn_local(async move {
        if let Err(error) = signal().await {
            tracing::error!("cannot watch for stop signal: {}", error.kind());
        }
        let _ = signal_stop.send(true);
    });
    (stop, stopping)
}

async fn run(
    home: &Path,
    stopping: &mut watch::Receiver<bool>,
    monitor: &Control,
) -> Result<Option<update::Package>> {
    process::prepare()?;
    let mut changes = Some(monitor.changes());
    let mut backoff = Backoff::new();
    loop {
        let applied = control::current(&mut changes);
        if enrollment::needed(home) {
            monitor.connection(home, Connection::Unregistered, None);
            return Ok(None);
        }
        monitor.connection(home, Connection::Trying, None);
        let registration = async {
            let idle = state::idle(home).map_err(link::invalid_state)?;
            let python = state::python().ok();
            enrollment::recover(home, python.as_deref()).await?;
            let host = state::Host::load(home).map_err(link::invalid_state)?;
            let identity = Identity::load(home).map_err(link::invalid_state)?;
            let hello = link::hello_frame(home, python.as_deref(), &host.fingerprint)
                .await
                .map_err(link::invalid_state)?;
            let (ws, interval) = link::hello(&host, identity, hello).await?;
            Ok(((ws, host.fingerprint), interval, idle))
        };
        let Some(connected) = until_stopped(stopping, registration).await else {
            return Ok(None);
        };
        let failure = match connected {
            Ok((ws, interval, idle)) => {
                tracing::info!("connected to host");
                monitor.connection(home, Connection::Connected, None);
                let ended =
                    session::run(ws, home, idle, interval, stopping, Some(monitor), applied).await;
                control::current(&mut changes);
                backoff.session_ended(ended.uptime);
                if ended.reconnect {
                    backoff = Backoff::new();
                    continue;
                }
                match ended.result {
                    Ok(()) => return Ok(ended.installer),
                    Err(error) => error,
                }
            }
            Err(error) => error,
        };
        let reason = link::status_error(&failure);
        if link::stop_reason(&failure).is_some() {
            monitor.connection(home, Connection::Stopped, Some(reason.clone()));
            return Err(anyhow!(reason));
        }
        let delay = backoff.next(&failure);
        tracing::warn!(
            "host link: {reason}; retrying in {:.1}s",
            delay.as_secs_f64()
        );
        local_log::record(
            home,
            local_log::Event::Reconnecting {
                delay,
                reason: &reason,
            },
        );
        monitor.connection(home, Connection::Waiting, Some(reason));
        tokio::select! {
            waiting = until_stopped(stopping, sleep(delay)) => {
                if waiting.is_none() {
                    return Ok(None);
                }
            }
            _ = control::changed(&mut changes) => backoff = Backoff::new(),
        }
    }
}

enum Command {
    Enroll {
        code: String,
        name: String,
    },
    Run {
        no_tray: bool,
    },
    Ui,
    Quit,
    Status,
    Autostart(Autostart),
    Repair {
        previous: Option<OsString>,
        offline: bool,
    },
}

enum Autostart {
    On,
    Off,
    Status,
}

const USAGE: &str = "Usage: kotoyose-child-app enroll <code> --name <name> | run [--no-tray] | ui | quit | status | autostart on|off|status | repair";

fn arguments(args: &[OsString]) -> Result<Option<Command>> {
    match args {
        [flag] if flag == "--help" || flag == "-h" => {
            println!("{USAGE}");
            Ok(None)
        }
        [flag] if flag == "--version" || flag == "-V" => {
            println!("kotoyose-child-app {VERSION}");
            Ok(None)
        }
        [] => Ok(Some(Command::Run { no_tray: false })),
        [command] if command == "ui" => Ok(Some(Command::Ui)),
        [command] if command == "quit" => Ok(Some(Command::Quit)),
        [command] if command == "run" => Ok(Some(Command::Run { no_tray: false })),
        [command, flag] if command == "run" && flag == "--no-tray" => {
            Ok(Some(Command::Run { no_tray: true }))
        }
        [command, action] if command == "autostart" => {
            let action = match action.to_str() {
                Some("on") => Autostart::On,
                Some("off") => Autostart::Off,
                Some("status") => Autostart::Status,
                _ => bail!("{USAGE}"),
            };
            Ok(Some(Command::Autostart(action)))
        }
        [command] if command == "status" => Ok(Some(Command::Status)),
        [command, args @ ..] if command == "repair" => {
            let (offline, args) = match args {
                [flag, args @ ..] if flag == "--offline" => (true, args),
                _ => (false, args),
            };
            let previous = match args {
                [] => None,
                [flag, version] if flag == "--previous" => Some(version.clone()),
                _ => bail!("{USAGE}"),
            };
            Ok(Some(Command::Repair { previous, offline }))
        }
        [command, code, flag, name] if command == "enroll" && flag == "--name" => {
            Ok(Some(Command::Enroll {
                code: code
                    .to_str()
                    .ok_or_else(|| anyhow!("invalid enrollment code"))?
                    .to_owned(),
                name: name
                    .to_str()
                    .ok_or_else(|| anyhow!("invalid child name"))?
                    .to_owned(),
            }))
        }
        _ => bail!("{USAGE}"),
    }
}

async fn entry(command: Command) -> Result<()> {
    if let Command::Autostart(action) = command {
        #[cfg(windows)]
        {
            match action {
                Autostart::On => println!("{}", autostart::set(true)?.log()),
                Autostart::Off => println!("{}", autostart::set(false)?.log()),
                Autostart::Status => {
                    println!("{}", if autostart::enabled()? { "on" } else { "off" })
                }
            }
            return Ok(());
        }
        #[cfg(not(windows))]
        {
            let _ = action;
            bail!("autostart is available on Windows");
        }
    }
    let home = state::home()?;
    if matches!(command, Command::Quit) {
        return instance::quit(&home);
    }
    if let Command::Repair { previous, offline } = command {
        #[cfg(windows)]
        return parts::repair(&home, previous.as_deref(), offline).await;
        #[cfg(not(windows))]
        {
            let _ = (previous, offline);
            bail!("repair is available on Windows");
        }
    }
    if matches!(command, Command::Status) {
        println!("kotoyose-child-app {VERSION}");
        if enrollment::is_pending(&home) {
            println!("enrollment pending");
        } else if let Ok(host) = state::Host::load(&home) {
            println!("enrolled as {} at {}", host.name, host.address);
        } else {
            println!("child is not enrolled");
        }
        if home.is_dir() {
            let running = state::lock(&home)?.is_none();
            println!("instance lock: {}", if running { "held" } else { "free" });
        }
        return Ok(());
    }
    let (stop, mut stopping) = watch_stop();
    let (code, name) = match command {
        Command::Run { no_tray } => {
            let _ = no_tray;
            return run_terminal(home, stop, stopping, desktop::Mode::Terminal).await;
        }
        Command::Ui => return run_terminal(home, stop, stopping, desktop::Mode::Preview).await,
        Command::Enroll { code, name } => (code, name),
        Command::Status | Command::Quit | Command::Autostart(_) | Command::Repair { .. } => {
            unreachable!()
        }
    };
    let mut backoff = Backoff::new();
    let _lock = loop {
        let lock = state::private_dir(&home)
            .map_err(anyhow::Error::from)
            .and_then(|()| state::lock(&home))
            .map_err(link::invalid_state);
        match lock {
            Ok(Some(lock)) => break lock,
            Ok(None) => bail!("child is already running"),
            Err(error) => {
                if !backoff.wait(&error, &mut stopping).await {
                    return Ok(());
                }
            }
        }
    };
    if let Some(name) = enrollment::enroll(&home, &name, &code, &mut stopping).await? {
        println!("enrolled {name}");
    }
    Ok(())
}

async fn run_terminal(
    home: std::path::PathBuf,
    stop: watch::Sender<bool>,
    shutdown: watch::Receiver<bool>,
    mode: desktop::Mode,
) -> Result<()> {
    #[cfg(not(target_os = "linux"))]
    return terminal_session(&home, &stop, &shutdown, mode)
        .await?
        .finish(&home, std::env::args_os().skip(1));

    #[cfg(target_os = "linux")]
    {
        let args: Vec<_> = std::env::args_os().skip(1).collect();
        loop {
            match terminal_session(&home, &stop, &shutdown, mode).await? {
                desktop::Exit::Stop => return Ok(()),
                desktop::Exit::Exec(activation) => {
                    let error = activation
                        .exec(&home, &args)
                        .expect_err("successful exec replaces the app");
                    tracing::warn!("child restart failed: {}", local_log::diagnostic(&error));
                    update::record_setup_error(&home);
                }
            }
        }
    }
}

async fn terminal_session(
    home: &Path,
    stop: &watch::Sender<bool>,
    shutdown: &watch::Receiver<bool>,
    mode: desktop::Mode,
) -> Result<desktop::Exit> {
    use std::{io::Write, sync::Arc};
    use tokio::sync::mpsc;

    let Some((instance, listener)) = instance::claim(home)? else {
        return Ok(desktop::Exit::Stop);
    };
    let state = Arc::new(Control::new(|| {}));
    let (commands, inbox) = mpsc::channel(8);
    mode.request_initial_page(home, &commands);
    let listener = listener.listen(commands.clone(), stop.clone())?;
    #[cfg(target_os = "linux")]
    update_linux::notify_ready()?;
    let result = desktop::run(
        home.to_path_buf(),
        state,
        commands,
        inbox,
        shutdown.clone(),
        mode,
        |notice| match notice {
            desktop::Notice::Browser(url) => {
                if let Err(error) = writeln!(std::io::stdout().lock(), "{url}") {
                    tracing::warn!("settings URL write failed: {}", error.kind());
                }
                #[cfg(target_os = "linux")]
                update_linux::open_browser(&url);
            }
            desktop::Notice::Error(message) => tracing::warn!("{message}"),
        },
    )
    .await;
    listener.abort();
    let _ = listener.await;
    drop(instance);
    result
}

fn runtime() -> std::io::Result<tokio::runtime::Runtime> {
    tokio::runtime::Builder::new_current_thread()
        .enable_all()
        .build()
}

fn tracing() {
    let level = std::env::var("RUST_LOG")
        .ok()
        .and_then(|value| value.parse().ok())
        .unwrap_or(LevelFilter::INFO);
    tracing_subscriber::registry()
        .with(
            Targets::new()
                .with_default(LevelFilter::INFO)
                .with_target(env!("CARGO_CRATE_NAME"), level),
        )
        .with(
            tracing_subscriber::fmt::layer()
                .without_time()
                .with_ansi(false)
                .with_target(false)
                .with_writer(std::io::stderr),
        )
        .init();
}

#[cfg(windows)]
pub fn background() -> std::process::ExitCode {
    update::remember_executable();
    tracing();
    if let Err(error) = tray::run(true) {
        tray::error(&messages::windows::startup_error(&error));
        std::process::ExitCode::FAILURE
    } else {
        std::process::ExitCode::SUCCESS
    }
}

pub fn cli() -> std::process::ExitCode {
    update::remember_executable();
    tracing();
    let args: Vec<_> = std::env::args_os().skip(1).collect();
    let result = (|| {
        #[cfg(target_os = "linux")]
        if let Some(result) = linux_install_command(&args) {
            return result;
        }
        if let Some(command) = arguments(&args)? {
            #[cfg(windows)]
            if matches!(command, Command::Run { no_tray: false }) {
                return tray::run(false);
            }
            runtime()?.block_on(tokio::task::LocalSet::new().run_until(entry(command)))?;
        }
        Ok::<(), anyhow::Error>(())
    })();
    if let Err(error) = result {
        tracing::error!("connection stopped: {}", local_log::diagnostic(&error));
        std::process::ExitCode::FAILURE
    } else {
        std::process::ExitCode::SUCCESS
    }
}

#[cfg(target_os = "linux")]
fn linux_install_command(args: &[OsString]) -> Option<Result<()>> {
    use update_linux::Reopen;

    let run = || match args {
        [command, flags @ ..] if command == "install-linux" => {
            let systemd = match flags {
                [] => false,
                [flag] if flag == "--systemd" => true,
                _ => bail!("Usage: install.sh [--systemd]"),
            };
            runtime()?.block_on(update_linux::install_package(systemd))
        }
        [command, name, home, flags @ ..] if command == "restart-linux-service" => {
            let reopen = match flags {
                [] => Reopen::Quiet,
                [flag] if flag == "--open-settings" => Reopen::Settings,
                _ => bail!("invalid child restart arguments"),
            };
            let home = Path::new(home);
            anyhow::ensure!(home.is_absolute(), "child state path must be absolute");
            runtime()?.block_on(update_linux::restart_service(name, home, reopen))
        }
        _ => bail!("invalid child installation arguments"),
    };
    args.first()
        .is_some_and(|arg| arg == "install-linux" || arg == "restart-linux-service")
        .then(run)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn no_arguments_selects_the_same_run_mode_as_run() {
        for args in [vec![], vec![OsString::from("run")]] {
            assert!(matches!(
                arguments(&args).unwrap(),
                Some(Command::Run { no_tray: false })
            ));
        }
    }

    #[test]
    fn ui_selects_preview_mode_and_rejects_extra_arguments() {
        assert!(matches!(
            arguments(&["ui".into()]).unwrap(),
            Some(Command::Ui)
        ));
        assert!(arguments(&["ui".into(), "--no-tray".into()]).is_err());
    }

    #[test]
    fn backoff_doubles_and_caps_for_transport_and_state() {
        let mut backoff = Backoff::new();
        let transport = anyhow!("host closed the connection");
        for millis in [500, 1000, 2000, 4000, 8000, 16000, 30000, 30000] {
            assert_eq!(backoff.next(&transport), Duration::from_millis(millis));
        }

        let mut backoff = Backoff::new();
        let state = link::invalid_state(anyhow!("cannot read policy.json"));
        for seconds in [5, 10, 20, 40, 80, 160, 300, 300] {
            assert_eq!(backoff.next(&state), Duration::from_secs(seconds));
        }
        assert_eq!(backoff.next(&transport), Duration::from_secs(30));
    }

    #[test]
    fn only_a_stable_session_resets_backoff() {
        let mut backoff = Backoff::new();
        let transport = anyhow!("host closed the connection");
        assert_eq!(backoff.next(&transport), Duration::from_millis(500));
        backoff.session_ended(Duration::from_millis(29_999));
        assert_eq!(backoff.next(&transport), Duration::from_secs(1));
        backoff.session_ended(Duration::from_secs(30));
        assert_eq!(backoff.next(&transport), Duration::from_millis(500));
    }
}
