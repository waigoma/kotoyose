use crate::messages;
use std::{
    path::{Path, PathBuf},
    sync::Arc,
};

use anyhow::Result;
use tokio::sync::{mpsc, oneshot, watch};

use crate::{
    components,
    control::{Connection, Control},
    enrollment, link, local_log, state, until_stopped, update, web,
};

#[derive(Clone, Copy)]
pub enum Mode {
    #[cfg(any(windows, test))]
    Tray,
    Terminal,
    Preview,
}

impl Mode {
    pub fn request_initial_page(self, home: &Path, commands: &mpsc::Sender<Request>) {
        #[cfg(target_os = "linux")]
        let service_reopen = crate::update_linux::take_reopen(home);
        #[cfg(not(target_os = "linux"))]
        let service_reopen = false;
        if service_reopen
            || matches!(self, Self::Preview)
            || enrollment::needed(home)
            || std::env::var_os(update::OPEN_SETTINGS).as_deref() == Some(std::ffi::OsStr::new("1"))
            || (cfg!(windows)
                && std::env::args_os()
                    .nth(1)
                    .is_some_and(|arg| arg == "--open-settings"))
        {
            let _ = commands.try_send(Request::Open);
        }
    }
}

pub enum Action {
    Enroll { name: String, code: String },
    Exit { restart: bool },
    Components(Box<components::Job>),
}

#[derive(Debug)]
pub enum ActionError {
    Busy,
    Unavailable,
    Failed(String),
}

type ActionResult = Result<String, ActionError>;

pub enum Request {
    Open,
    Action {
        action: Action,
        interrupt: bool,
        reply: Option<oneshot::Sender<ActionResult>>,
    },
}

// Kept separate from link status so that the GUI only receives messages it renders.
pub enum Notice {
    Browser(String),
    Error(String),
}

fn report(
    reply: Option<oneshot::Sender<ActionResult>>,
    result: ActionResult,
    notify: &impl Fn(Notice),
) {
    if let Some(reply) = reply {
        let _ = reply.send(result);
    } else if let Err(error) = result {
        let message = match error {
            ActionError::Busy => "app is busy".into(),
            ActionError::Unavailable => "app is unavailable".into(),
            ActionError::Failed(message) => message,
        };
        notify(Notice::Error(message));
    }
}

pub enum Exit {
    Stop,
    #[cfg(not(target_os = "linux"))]
    Restart,
    #[cfg(not(target_os = "linux"))]
    Install(update::Package),
    #[cfg(target_os = "linux")]
    Exec(crate::update_linux::Activation),
}

#[cfg(not(target_os = "linux"))]
impl Exit {
    pub fn finish(
        self,
        home: &Path,
        args: impl IntoIterator<Item = impl AsRef<std::ffi::OsStr>>,
    ) -> Result<()> {
        match self {
            Self::Stop => Ok(()),
            Self::Restart => {
                let exe = update::installed_change().map_or_else(std::env::current_exe, Ok)?;
                update::relaunch(&exe, args)
            }
            Self::Install(setup) => update::install_and_recover(home, setup, args),
        }
    }
}

#[cfg(target_os = "linux")]
async fn linux_restart(
    home: &Path,
    state: &Control,
    package: Option<update::Package>,
    server: Option<&web::Server>,
    shutdown: &mut watch::Receiver<bool>,
) -> Option<Exit> {
    use crate::update_linux::{self, Activation, Reopen};
    let reopen = if server.is_some_and(|server| !server.finished()) {
        Reopen::Settings
    } else {
        Reopen::Quiet
    };
    let result = async {
        let activation = Activation::prepare(home, package, reopen).await?;
        if update_linux::in_service() {
            until_stopped(shutdown, activation.delegate(home))
                .await
                .transpose()?;
            Ok(Exit::Stop)
        } else {
            Ok(Exit::Exec(activation))
        }
    }
    .await;
    match result {
        Ok(exit) => Some(exit),
        Err(error) => {
            tracing::warn!("child update failed: {}", local_log::diagnostic(&error));
            state.update.fail(update::Error::Setup);
            None
        }
    }
}

pub async fn run(
    home: PathBuf,
    state: Arc<Control>,
    commands: mpsc::Sender<Request>,
    mut inbox: mpsc::Receiver<Request>,
    mut shutdown: watch::Receiver<bool>,
    mode: Mode,
    notify: impl Fn(Notice),
) -> Result<Exit> {
    match std::fs::remove_file(home.join(state::SETTINGS_PORT_FILE)) {
        Ok(()) => {}
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
        Err(error) => return Err(error.into()),
    }
    update::sweep(&home);
    #[cfg(target_os = "linux")]
    crate::update_linux::finish_running();
    state.update.restore_error(&home);
    let mut server = None::<web::Server>;
    let login = web::LoginJobs::default();
    let exit = 'desktop: loop {
        let (stop, mut stopping) = watch::channel(false);
        let link_task = async {
            let result = crate::run(&home, &mut stopping, &state).await;
            if let Err(error) = &result
                && !matches!(state.status().connection, Connection::Stopped)
            {
                state.connection(&home, Connection::Stopped, Some(link::status_error(error)));
            }
            if !matches!(mode, Mode::Terminal) && result.is_err() {
                tracing::warn!(
                    "connection stopped: {}",
                    state.status().error.as_deref().unwrap_or("link failed")
                );
            }
            result
        };
        tokio::pin!(link_task);
        let mut link_done = false;
        let request = loop {
            let request = tokio::select! {
                biased;
                _ = async { let _ = shutdown.wait_for(|stop| *stop).await; } => None,
                request = inbox.recv() => request,
                _ = async { server.as_mut().expect("running settings server").wait().await }, if server.is_some() => {
                    server = None;
                    continue;
                },
                result = &mut link_task, if !link_done => {
                    if let Ok(Some(installer)) = result {
                        #[cfg(not(target_os = "linux"))]
                        break 'desktop Exit::Install(installer);
                        #[cfg(target_os = "linux")]
                        if let Some(exit) = linux_restart(&home, &state, Some(installer), server.as_ref(), &mut shutdown).await {
                            break 'desktop exit;
                        } else {
                            continue 'desktop;
                        }
                    }
                    if matches!(mode, Mode::Terminal) {
                        result?;
                    }
                    link_done = true;
                    continue;
                },
            };
            match request {
                None => break (Action::Exit { restart: false }, None),
                Some(Request::Open) => {
                    if server.as_ref().is_none_or(web::Server::finished) {
                        match web::Server::start(
                            home.clone(),
                            state.clone(),
                            commands.clone(),
                            login.clone(),
                        )
                        .await
                        {
                            Ok(live) => server = Some(live),
                            Err(error) => {
                                notify(Notice::Error(format!(
                                    "settings open failed: {}",
                                    error.kind()
                                )));
                                continue;
                            }
                        }
                    }
                    match server.as_ref().expect("running settings server").open() {
                        Ok(url) => notify(Notice::Browser(url)),
                        Err(error) => notify(Notice::Error(format!(
                            "settings open failed: {}",
                            error.kind()
                        ))),
                    }
                }
                Some(Request::Action {
                    action,
                    interrupt,
                    reply,
                }) => {
                    if reply.as_ref().is_some_and(oneshot::Sender::is_closed) {
                        continue;
                    }
                    if state.update.view().phase == update::Phase::Installing {
                        report(reply, Err(ActionError::Unavailable), &notify);
                    } else if !interrupt && state.status().work != 0 {
                        report(reply, Err(ActionError::Busy), &notify);
                    } else if matches!(action, Action::Exit { restart: true })
                        && state.update.activate(interrupt)
                    {
                        report(reply, Ok(messages::APP_RESTART.into()), &notify);
                    } else {
                        let reply = if matches!(action, Action::Components(_)) {
                            // Acknowledge before reaping the runner so the progress stream can open.
                            report(reply, Ok(String::new()), &notify);
                            None
                        } else {
                            reply
                        };
                        break (action, reply);
                    }
                }
            }
        };
        let _ = stop.send(true);
        // Poll the link to completion before touching registration or releasing the instance lock.
        if !link_done {
            let _ = link_task.await;
        }
        let (action, reply) = request;
        match action {
            Action::Components(job) => job.run(&home).await,
            Action::Exit { restart } => {
                report(
                    reply,
                    Ok(if restart {
                        messages::APP_RESTART
                    } else {
                        messages::APP_EXIT
                    }
                    .into()),
                    &notify,
                );
                if !restart {
                    break Exit::Stop;
                }
                #[cfg(not(target_os = "linux"))]
                break Exit::Restart;
                #[cfg(target_os = "linux")]
                if let Some(exit) =
                    linux_restart(&home, &state, None, server.as_ref(), &mut shutdown).await
                {
                    break exit;
                }
            }
            Action::Enroll { name, code } => {
                state.connection(&home, Connection::Trying, None);
                let result =
                    until_stopped(&mut shutdown, enrollment::attempt(&home, &name, &code)).await;
                let Some(result) = result else {
                    break Exit::Stop;
                };
                report(
                    reply,
                    result
                        .map(|name| messages::enrolled(&name))
                        .map_err(|(error, _)| ActionError::Failed(local_log::diagnostic(&error))),
                    &notify,
                );
            }
        }
    };
    if let Some(live) = server.take() {
        live.finish().await;
    }
    Ok(exit)
}
