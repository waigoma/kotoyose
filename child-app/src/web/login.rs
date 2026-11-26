use std::{collections::HashMap, ffi::OsStr, path::Path, sync::Arc, time::Duration};

use anyhow::{Result, ensure};
use axum::{
    Json,
    extract::{Path as RoutePath, State},
    http::StatusCode,
};
use serde::{Deserialize, Serialize};
use tokio::{
    io::{AsyncBufReadExt, AsyncWriteExt, BufReader},
    sync::{Mutex, mpsc, oneshot, watch},
    time::timeout,
};

use crate::{components, messages, state};

use super::{ApiError, Input, Web};

mod helper;
use helper::Helper;

const STATUS_TIMEOUT: Duration = Duration::from_secs(15);
const LOGIN_TIMEOUT: Duration = Duration::from_secs(625);

#[derive(Clone, Default)]
pub(crate) struct Jobs(Arc<Mutex<HashMap<String, Slot>>>);

#[derive(Default)]
struct Slot {
    view: Option<View>,
    active: Option<Active>,
}

type Completion = watch::Receiver<Option<View>>;

enum Active {
    Check(Completion),
    Login {
        codes: mpsc::Sender<String>,
        cancel: Option<oneshot::Sender<()>>,
        completed: Completion,
    },
}

#[derive(Clone, Deserialize, Serialize)]
#[serde(tag = "state", rename_all = "snake_case")]
pub(super) enum View {
    Unsupported,
    Unavailable,
    Unknown,
    Running {
        #[serde(skip_serializing_if = "Option::is_none")]
        url: Option<String>,
        #[serde(default, skip_serializing_if = "std::ops::Not::not")]
        needs_code: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        device_code: Option<String>,
    },
    LoggedIn,
    LoggedOut,
    Cancelled,
    Failed,
}

impl View {
    fn starting() -> Self {
        Self::Running {
            url: None,
            needs_code: false,
            device_code: None,
        }
    }

    fn running(&self) -> bool {
        matches!(self, Self::Running { .. })
    }
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Start {}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(super) struct Code {
    code: String,
}

async fn completed(mut result: Completion) -> View {
    result
        .wait_for(Option::is_some)
        .await
        .map(|view| view.clone().unwrap_or(View::Failed))
        .unwrap_or(View::Failed)
}

fn known(home: &Path, provider: &str) -> Result<(), ApiError> {
    if components::provider(provider).is_some()
        || crate::declarations::has_approval_file(home, provider)
    {
        Ok(())
    } else {
        Err(ApiError::new(
            StatusCode::NOT_FOUND,
            messages::COMPONENT_UNKNOWN,
        ))
    }
}

fn lock_components() -> Result<tokio::sync::RwLockReadGuard<'static, ()>, ApiError> {
    components::COMPONENT_LOCK
        .try_read()
        .map_err(|_| ApiError::new(StatusCode::CONFLICT, messages::COMPONENT_BUSY))
}

fn spawn_helper(python: &Path, home: &Path, provider: &str, action: &str) -> Result<Helper> {
    Helper::spawn(
        python,
        [
            OsStr::new("-I"),
            OsStr::new("-m"),
            OsStr::new("kotoyose.child_local_login"),
            OsStr::new("--home"),
            home.as_os_str(),
            OsStr::new(provider),
            OsStr::new(action),
        ],
    )
}

pub(super) async fn check(
    State(web): State<Web>,
    RoutePath(provider): RoutePath<String>,
    Input(_): Input<Start>,
) -> Result<Json<View>, ApiError> {
    known(&web.home, &provider)?;
    let result = {
        let mut slots = web.login.0.lock().await;
        let slot = slots.entry(provider.clone()).or_default();
        match &slot.active {
            Some(Active::Login { .. }) => {
                return Ok(Json(slot.view.clone().unwrap_or(View::Failed)));
            }
            Some(Active::Check(result)) => result.clone(),
            None => {
                let lock = lock_components()?;
                let Ok(python) = state::python() else {
                    return Ok(Json(View::Unavailable));
                };
                let Ok(child) = spawn_helper(&python, &web.home, &provider, "status") else {
                    return Ok(Json(View::Failed));
                };
                let (finish, result) = watch::channel(None);
                slot.active = Some(Active::Check(result.clone()));
                let jobs = web.login.clone();
                // The probe owns cleanup even if all its HTTP callers disconnect.
                tokio::spawn(async move {
                    let view = status(child).await;
                    drop(lock);
                    jobs.0.lock().await.entry(provider).or_default().active = None;
                    finish.send_replace(Some(view));
                });
                result
            }
        }
    };
    Ok(Json(completed(result).await))
}

async fn status(mut child: Helper) -> View {
    let result = timeout(STATUS_TIMEOUT, async {
        let mut output = BufReader::new(&mut child.output).lines();
        let line = output
            .next_line()
            .await?
            .ok_or_else(|| anyhow::anyhow!("missing status"))?;
        let view: View = serde_json::from_str(&line)?;
        ensure!(!view.running() && output.next_line().await?.is_none());
        ensure!(child.wait().await?);
        Ok::<_, anyhow::Error>(view)
    })
    .await;

    let result = result.ok().and_then(Result::ok);
    child.finish(result.is_none()).await;
    result.unwrap_or(View::Failed)
}

pub(super) async fn start(
    State(web): State<Web>,
    RoutePath(provider): RoutePath<String>,
    Input(_): Input<Start>,
) -> Result<Json<View>, ApiError> {
    known(&web.home, &provider)?;
    loop {
        let mut slots = web.login.0.lock().await;
        let slot = slots.entry(provider.clone()).or_default();
        match &slot.active {
            Some(Active::Login { .. }) => {
                return Ok(Json(slot.view.clone().unwrap_or(View::Failed)));
            }
            Some(Active::Check(result)) => {
                let result = result.clone();
                drop(slots);
                completed(result).await;
            }
            None => {
                let lock = lock_components()?;
                let Ok(python) = state::python() else {
                    return Ok(Json(View::Unavailable));
                };
                let child = spawn_helper(&python, &web.home, &provider, "start")
                    .map_err(|_| ApiError::internal(messages::LOGIN_START_FAILED))?;
                let (codes, input) = mpsc::channel(1);
                let (cancel, cancelled) = oneshot::channel();
                let (finish, result) = watch::channel(None);
                let view = View::starting();
                slot.view = Some(view.clone());
                slot.active = Some(Active::Login {
                    codes,
                    cancel: Some(cancel),
                    completed: result,
                });
                let jobs = web.login.clone();
                tokio::spawn(async move {
                    let result = supervise(child, &jobs, &provider, input, cancelled).await;
                    drop(lock);
                    let mut slots = jobs.0.lock().await;
                    let slot = slots.entry(provider).or_default();
                    slot.view = Some(result.clone());
                    slot.active = None;
                    finish.send_replace(Some(result));
                });
                return Ok(Json(view));
            }
        }
    }
}

async fn supervise(
    mut child: Helper,
    jobs: &Jobs,
    provider: &str,
    codes: mpsc::Receiver<String>,
    cancelled: oneshot::Receiver<()>,
) -> View {
    let (view, stop) = tokio::select! {
        biased;
        _ = cancelled => (View::Cancelled, true),
        result = timeout(LOGIN_TIMEOUT, follow(&mut child, jobs, provider, codes)) => {
            match result {
                Ok(Ok(view)) => (view, false),
                _ => (View::Failed, true),
            }
        }
    };
    #[cfg(windows)]
    if !stop && components::provider(provider).is_none() && child.release_job().is_err() {
        child.finish(true).await;
        return View::Failed;
    }
    child.finish(stop).await;
    view
}

async fn follow(
    child: &mut Helper,
    jobs: &Jobs,
    provider: &str,
    mut codes: mpsc::Receiver<String>,
) -> Result<View> {
    let stdin = child
        .input
        .as_mut()
        .ok_or_else(|| anyhow::anyhow!("missing login input"))?;
    let mut lines = BufReader::new(&mut child.output).lines();
    let mut result = None;
    loop {
        tokio::select! {
            line = lines.next_line() => {
                let Some(line) = line? else { break };
                let view: View = serde_json::from_str(&line)?;
                ensure!(result.is_none());
                if view.running() {
                    let mut slots = jobs.0.lock().await;
                    let slot = slots.entry(provider.to_owned()).or_default();
                    if matches!(&slot.active, Some(Active::Login { cancel: Some(_), .. })) {
                        slot.view = Some(view);
                    }
                } else {
                    result = Some(view);
                }
            }
            Some(mut code) = codes.recv() => {
                code.push('\n');
                stdin.write_all(code.as_bytes()).await?;
                stdin.flush().await?;
            }
        }
    }
    ensure!(child.wait().await?);
    result.ok_or_else(|| anyhow::anyhow!("missing login result"))
}

pub(super) async fn progress(
    State(web): State<Web>,
    RoutePath(provider): RoutePath<String>,
) -> Result<Json<View>, ApiError> {
    web.login
        .0
        .lock()
        .await
        .get(&provider)
        .and_then(|slot| slot.view.clone())
        .map(Json)
        .ok_or_else(not_running)
}

/// Cancel the active login and return its final view after its helper has exited.
pub(super) async fn cancel(
    State(web): State<Web>,
    RoutePath(provider): RoutePath<String>,
) -> Result<Json<View>, ApiError> {
    known(&web.home, &provider)?;
    web.login
        .cancel(&provider)
        .await
        .map(Json)
        .ok_or_else(not_running)
}

impl Jobs {
    pub(super) async fn cancel(&self, provider: &str) -> Option<View> {
        let result = {
            let mut slots = self.0.lock().await;
            let slot = slots.get_mut(provider)?;
            let Some(Active::Login {
                cancel, completed, ..
            }) = &mut slot.active
            else {
                return slot.view.clone();
            };
            if let Some(cancel) = cancel.take() {
                let _ = cancel.send(());
                slot.view = Some(View::starting());
            }
            completed.clone()
        };
        Some(completed(result).await)
    }
}

fn not_running() -> ApiError {
    ApiError::new(StatusCode::NOT_FOUND, messages::LOGIN_NOT_RUNNING)
}

pub(super) async fn submit_code(
    State(web): State<Web>,
    RoutePath(provider): RoutePath<String>,
    Input(input): Input<Code>,
) -> Result<Json<()>, ApiError> {
    if input.code.is_empty()
        || input.code.len() > 512
        || !input.code.bytes().all(|byte| byte.is_ascii_graphic())
    {
        return Err(ApiError::new(
            StatusCode::UNPROCESSABLE_ENTITY,
            messages::LOGIN_CODE_INVALID,
        ));
    }
    let conflict = || ApiError::new(StatusCode::CONFLICT, messages::LOGIN_NOT_WAITING);
    let mut slots = web.login.0.lock().await;
    let Some(Slot {
        active:
            Some(Active::Login {
                codes,
                cancel: Some(_),
                ..
            }),
        view: Some(View::Running { needs_code, .. }),
    }) = slots.get_mut(&provider)
    else {
        return Err(conflict());
    };
    if !*needs_code {
        return Err(conflict());
    }
    codes.try_send(input.code).map_err(|_| conflict())?;
    *needs_code = false;
    Ok(Json(()))
}
