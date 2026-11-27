use std::{
    cell::Cell,
    collections::HashMap,
    path::{Path, PathBuf},
    rc::Rc,
    time::Duration,
};

use anyhow::{Context, Result, bail, ensure};
use futures_util::{SinkExt, StreamExt};
use serde_json::json;
use tokio::{
    sync::{Notify, oneshot, watch},
    task::JoinHandle,
    time::{Instant, sleep_until, timeout},
};
use tokio_tungstenite::tungstenite::Message;

use crate::{
    components,
    control::{self, Changes, Control},
    frame::{Frame, MAX_STREAM, Operation, write_timeout},
    link::{self, Socket},
    local_log::{self, Event as LogEvent},
    messages,
    outbox::Outbox,
    runner::{self, Event, Phase, Runner},
    update,
};

const WORK_LIMIT: usize = 32;
const CANCEL_TIMEOUT: Duration = Duration::from_secs(5);
const PARTIAL_LIMIT: usize = 16 * 1024;

struct Work {
    operation: Operation,
    relayed_mcp: bool,
    cancel_by: Option<Instant>,
    terminal: bool,
    partial: String,
    partial_full: bool,
}

impl Work {
    fn new(operation: Operation, relayed_mcp: bool) -> Self {
        Self {
            operation,
            relayed_mcp,
            cancel_by: None,
            terminal: false,
            partial: String::new(),
            partial_full: false,
        }
    }

    fn remember_delta(&mut self, frame: &Frame) {
        if self.partial_full {
            return;
        }
        if let Some(text) = frame
            .payload()
            .ok()
            .as_ref()
            .and_then(|p| p["text"].as_str())
        {
            let limit = (PARTIAL_LIMIT - self.partial.len()).min(text.len());
            self.partial_full = limit < text.len() || self.partial.len() + limit == PARTIAL_LIMIT;
            self.partial
                .push_str(&text[..text.floor_char_boundary(limit)]);
        }
    }
}

struct Mux<'a> {
    home: &'a Path,
    bundled_python: Option<PathBuf>,
    host_fingerprint: &'a str,
    idle_time: Duration,
    idle_at: Option<Instant>,
    applying_policy: bool,
    refreshing_components: bool,
    saving_policy: bool,
    draining_update: bool,
    reload_idle: bool,
    monitor: bool,
    work: HashMap<u32, Work>,
    runner: Option<Runner>,
    closing: Option<JoinHandle<()>>,
    pipe: Outbox,
    host: Outbox,
}

impl<'a> Mux<'a> {
    fn new(home: &'a Path, idle_time: Duration) -> Self {
        let host = Outbox::default();
        host.enable_data(true);
        Self {
            home,
            bundled_python: components::Runtime::installed().map(|runtime| runtime.python),
            host_fingerprint: "",
            idle_time,
            idle_at: None,
            applying_policy: false,
            refreshing_components: false,
            saving_policy: false,
            draining_update: false,
            reload_idle: false,
            monitor: false,
            work: HashMap::new(),
            runner: None,
            closing: None,
            pipe: Outbox::default(),
            host,
        }
    }

    fn ready(&self) -> bool {
        self.runner
            .as_ref()
            .is_some_and(|runner| runner.phase == Phase::Ready)
    }

    fn done(&mut self, id: u32) {
        self.work.remove(&id);
        if self.work.is_empty() && self.ready() {
            self.idle_at = Some(Instant::now() + self.idle_time);
        }
    }

    fn host_priority(&self, frame: Frame) -> Result<()> {
        ensure!(self.host.priority(frame), "host control queue is full");
        Ok(())
    }

    fn pipe_priority(&self, frame: Frame) -> Result<()> {
        ensure!(self.pipe.priority(frame), "runner control queue is full");
        Ok(())
    }

    fn host_frame(&mut self, frame: Frame) -> Result<()> {
        let id = frame.envelope.stream;
        let kind = frame.envelope.kind.as_str();
        if let Some(operation) = Operation::start(kind) {
            if self.work.contains_key(&id) {
                return Ok(());
            }
            if self.draining_update {
                return self.host_priority(operation.error(
                    id,
                    "child app update is waiting for work to finish",
                    "",
                ));
            }
            if self.saving_policy || self.applying_policy {
                return self.host_priority(operation.error(
                    id,
                    "child policy is being applied",
                    "",
                ));
            }
            let relayed_mcp = if operation == Operation::Turn {
                let payload = frame.payload()?;
                if let Some(driver) = payload["route"]["driver"].as_str()
                    && components::driver_needs_reinstall(self.bundled_python.as_deref(), driver)
                {
                    return self.host_priority(operation.error(
                        id,
                        messages::COMPONENT_REINSTALL_REQUIRED,
                        "",
                    ));
                }
                payload.get("host_mcps").is_some()
            } else {
                false
            };
            if self.refreshing_components {
                return self.host_priority(operation.error(
                    id,
                    "child providers are being refreshed",
                    "",
                ));
            }
            if self.work.len() == WORK_LIMIT || !self.pipe.push(frame) {
                return self.host_priority(operation.error(id, "child queue is full", ""));
            }
            self.idle_at = None;
            self.work.insert(id, Work::new(operation, relayed_mcp));
        } else if matches!(
            kind,
            "cancel"
                | "release"
                | "login_cancel"
                | "quota_cancel"
                | "status_cancel"
                | "session_collect_cancel"
        ) {
            let release = kind == "release";
            if let Some(work) = self.work.get(&id) {
                if kind != work.operation.cancel()
                    && !(release && work.operation == Operation::Turn)
                {
                    return Ok(());
                }
                self.cancel(id, Some(frame))?;
            } else {
                self.host.discard(id);
                if release && self.ready() {
                    self.pipe_priority(frame)?;
                }
            }
        } else {
            let forwards = self.work.get(&id).is_some_and(|work| {
                work.cancel_by.is_none()
                    && !work.terminal
                    && ((kind == "login_input" && work.operation == Operation::Login)
                        || (kind == "mcp_response"
                            && work.relayed_mcp
                            && !self.pipe.start_pending(id)))
            });
            if forwards && !self.pipe.push(frame) {
                self.overflow(id)?;
            }
        }
        Ok(())
    }

    fn ready_to_install(&self, writing: bool) -> bool {
        !self.saving_policy && self.work.is_empty() && self.host.is_empty() && !writing
    }

    fn observe_changes(&mut self, current: Changes, applied: Changes) {
        self.saving_policy = current.saving_policy;
        self.applying_policy = current.policy != applied.policy;
        self.refreshing_components = current.components != applied.components;
    }

    fn cancel(&mut self, id: u32, frame: Option<Frame>) -> Result<()> {
        self.host.discard(id);
        let pending = self.pipe.discard(id);
        let work = self.work.get_mut(&id).expect("active stream");
        let release = frame.as_ref().is_some_and(|f| f.envelope.kind == "release");
        if pending {
            self.done(id);
            if release && self.ready() {
                self.pipe_priority(frame.expect("release frame"))?;
            }
        } else if work.cancel_by.is_none() {
            work.cancel_by = Some(Instant::now() + CANCEL_TIMEOUT);
            let cancel =
                frame.unwrap_or_else(|| Frame::new(id, work.operation.cancel(), json!({})));
            self.pipe_priority(cancel)?;
        } else if release {
            self.pipe_priority(frame.expect("release frame"))?;
        }
        Ok(())
    }

    fn overflow(&mut self, id: u32) -> Result<()> {
        let work = &self.work[&id];
        let error = work
            .operation
            .error(id, "child stream queue overflow", &work.partial);
        self.cancel(id, None)?;
        self.host_priority(error)
    }

    fn runner_frame(&mut self, frame: Frame) -> Result<()> {
        let id = frame.envelope.stream;
        let kind = frame.envelope.kind.as_str();
        if id == 0 {
            let runner = self.runner.as_mut().expect("runner event has a runner");
            match kind {
                "runner_ready" => runner.ready(&frame, &self.pipe),
                "runner_stream_done" => {
                    ensure!(
                        runner.phase == Phase::Ready,
                        "unexpected runner_stream_done"
                    );
                    let id = frame
                        .payload()?
                        .get("stream")
                        .and_then(|v| v.as_u64())
                        .filter(|id| (1..=u64::from(MAX_STREAM)).contains(id))
                        .context("invalid runner_stream_done stream")?
                        as u32;
                    let work = self
                        .work
                        .get(&id)
                        .context("runner completed an unknown stream")?;
                    ensure!(
                        work.terminal || work.cancel_by.is_some(),
                        "runner omitted the stream terminal"
                    );
                    self.done(id);
                    Ok(())
                }
                "runner_stopped" => runner.stopped(&frame),
                _ => bail!("unexpected runner control"),
            }?;
            if kind == "runner_ready" && self.work.is_empty() {
                self.idle_at = Some(Instant::now() + self.idle_time);
            }
            return Ok(());
        }

        ensure!(self.ready(), "unexpected runner work frame");
        ensure!(
            !self.pipe.start_pending(id),
            "runner replied before dispatch"
        );
        let work = self
            .work
            .get_mut(&id)
            .context("runner replied to unknown stream")?;
        let terminal = work
            .operation
            .output(kind)
            .context("invalid runner output kind")?;
        if work.cancel_by.is_some() || (kind == "mcp_request" && !work.relayed_mcp) {
            return Ok(());
        }
        ensure!(!work.terminal, "runner output after stream terminal");
        if kind == "delta" {
            work.remember_delta(&frame);
        }
        if self.host.push(frame) {
            work.terminal = terminal;
        } else {
            self.overflow(id)?;
        }
        Ok(())
    }

    fn close_runner(&mut self, reason: &'static str) {
        self.pipe.enable_data(false);
        self.pipe.clear_priority();
        self.idle_at = None;
        if let Some(runner) = self.runner.take() {
            self.closing = Some(runner.close(reason));
        }
    }

    fn failed(&mut self, error: &anyhow::Error) -> Result<()> {
        tracing::warn!("runner failure: {}", local_log::diagnostic(error));
        if self.monitor {
            local_log::record(self.home, LogEvent::RunnerFailure);
        }
        let stopping = self
            .runner
            .as_ref()
            .is_some_and(|runner| matches!(runner.phase, Phase::Stopping | Phase::Stopped));
        self.close_runner("failure");
        if !stopping {
            for (id, work) in std::mem::take(&mut self.work) {
                self.pipe.discard(id);
                if !work.terminal && work.cancel_by.is_none() {
                    // Previously queued output from this stream stays ahead of its terminal.
                    let error = work
                        .operation
                        .error(id, "child runner failed", &work.partial);
                    if !self.host.push(error) {
                        self.host.discard(id);
                        self.host_priority(work.operation.error(
                            id,
                            "child runner failed",
                            &work.partial,
                        ))?;
                    }
                }
            }
        }
        Ok(())
    }

    fn runner_event(&mut self, event: Result<Event>) -> Result<()> {
        let result = match event {
            Ok(Event::Read(Ok(Some(frame)))) => self.runner_frame(frame),
            Ok(Event::Read(Ok(None)))
                if self
                    .runner
                    .as_ref()
                    .is_some_and(|r| r.phase == Phase::Stopped) =>
            {
                self.close_runner("link_closed");
                Ok(())
            }
            Ok(Event::Read(Ok(None))) => Err(anyhow::anyhow!("unexpected runner EOF")),
            Ok(Event::Read(Err(_))) => Err(anyhow::anyhow!("invalid or truncated runner record")),
            Ok(Event::WriteFailed) => Err(anyhow::anyhow!("runner write failed or timed out")),
            Err(error) => Err(error),
        };
        if let Err(error) = result {
            self.failed(&error)?;
        }
        Ok(())
    }

    fn deadline(&self) -> Option<Instant> {
        self.idle_at
            .into_iter()
            .chain(self.runner.as_ref().and_then(|r| r.deadline))
            .chain(self.work.values().filter_map(|work| work.cancel_by))
            .min()
    }

    fn update(&mut self, now: Instant) -> Result<()> {
        if self
            .work
            .values()
            .any(|work| work.cancel_by.is_some_and(|at| now >= at))
        {
            self.failed(&anyhow::anyhow!("runner cancellation timed out"))?;
        }
        if let Some(runner) = &self.runner
            && runner.deadline.is_some_and(|at| now >= at)
        {
            self.failed(&anyhow::anyhow!("runner lifecycle timed out"))?;
        }
        if self.work.is_empty()
            && (self.applying_policy || self.refreshing_components || self.reload_idle)
            && self.ready()
        {
            self.idle_at = Some(now);
        }
        if self.idle_at.is_some_and(|at| now >= at) {
            self.idle_at = None;
            if let Some(runner) = &mut self.runner {
                let reason = if self.applying_policy {
                    "policy_changed"
                } else if self.refreshing_components {
                    "components_changed"
                } else if self.reload_idle {
                    "settings_changed"
                } else {
                    "idle"
                };
                self.reload_idle = false;
                if let Err(error) = runner.shutdown(&self.pipe, now, reason) {
                    self.failed(&error)?;
                }
            }
        }
        if self.runner.is_none() {
            self.reload_idle = false;
        }
        if self.runner.is_none() && self.closing.is_none() && !self.work.is_empty() {
            match Runner::start(
                self.home,
                self.idle_time,
                &self.pipe,
                self.monitor,
                self.host_fingerprint,
            ) {
                Ok(runner) => self.runner = Some(runner),
                Err(error) => self.failed(&error)?,
            }
        }
        Ok(())
    }
}

pub struct SessionEnd {
    pub uptime: Duration,
    pub result: Result<()>,
    pub reconnect: bool,
    pub installer: Option<update::Package>,
}

pub async fn run(
    link: (Socket, String),
    home: &Path,
    idle: Duration,
    interval: Duration,
    stop: &mut watch::Receiver<bool>,
    control: Option<&Control>,
    applied: Changes,
) -> SessionEnd {
    let (ws, fingerprint) = link;
    let started = Instant::now();
    let mut mux = Mux::new(home, idle);
    mux.host_fingerprint = &fingerprint;
    mux.monitor = control.is_some();
    let mut changes = control.map(Control::changes);
    let mut seen = applied;
    let (mut sink, mut source) = ws.split();
    let outbox = mux.host.clone();
    let (writer_stopped, mut failure) = oneshot::channel();
    let writing = Rc::new((Cell::new(false), Notify::new()));
    let writer_state = writing.clone();
    let writer = tokio::task::spawn_local(async move {
        let result = loop {
            let frame = outbox.next().await;
            writer_state.0.set(true);
            let result = timeout(
                write_timeout(frame.body.len()),
                sink.send(Message::Text(frame.body.into())),
            )
            .await;
            writer_state.0.set(false);
            writer_state.1.notify_one();
            match result {
                Ok(Ok(())) => {}
                Ok(Err(error)) => break link::ws_error(error),
                Err(_) => break anyhow::anyhow!("host socket write timed out"),
            }
        };
        let _ = writer_stopped.send(result);
    });
    let mut reconnect = false;
    let mut installer = None;
    let mut transfer = update::Transfer::default();
    let update = control.map(|control| &control.update);
    let result = async {
        let mut send_at = Instant::now();
        let mut receive_by = send_at + interval * 3;
        let heartbeat = interval.min(Duration::from_secs(20));
        loop {
            let now = Instant::now();
            let next = control::current(&mut changes);
            mux.observe_changes(next, applied);
            if let Some((_, idle)) = next.idle
                && next.idle != seen.idle
            {
                mux.idle_time = idle;
                mux.reload_idle |= mux.runner.as_ref().is_some_and(|runner| {
                    matches!(runner.phase, Phase::Ready | Phase::Starting)
                });
            }
            seen = next;
            if let Some(update) = update {
                transfer.expire(update, now);
                if let Some(request) = transfer.request(update, home, now) {
                    mux.host_priority(request)?;
                }
                mux.draining_update = update.draining();
                if update.install(mux.ready_to_install(writing.0.get())) {
                    installer = transfer.take_verified();
                    return Ok(());
                }
            }
            if !mux.draining_update && (mux.applying_policy || mux.refreshing_components) && !mux.saving_policy && mux.work.is_empty() && mux.runner.is_none()
                && mux.closing.is_none() && mux.host.is_empty() && !writing.0.get()
            {
                reconnect = true;
                return Ok(());
            }
            ensure!(now < receive_by, "host heartbeat timed out");
            if now >= send_at {
                let mut beat = json!({});
                if cfg!(target_os = "linux") {
                    beat["mcp_approvals"] = json!(crate::host_mcp::approved(home, &fingerprint));
                }
                mux.host_priority(Frame::new(0, "heartbeat", beat))?;
                send_at = now + heartbeat;
                if let Some(runner) = &mux.runner {
                    runner.reap_exited_descendants();
                }
            }
            mux.update(now)?;
            if let Some(control) = control {
                control.runner(
                    mux.runner.is_some() || mux.closing.is_some(),
                    mux.work.len(),
                    mux.runner.as_ref().and_then(|runner| runner.memory.clone()),
                    mux.saving_policy || mux.applying_policy,
                );
            }
            let tick = send_at
                .min(receive_by)
                .min(mux.deadline().unwrap_or(send_at))
                .min(transfer.deadline().unwrap_or(send_at));
            tokio::select! {
                _ = stop.wait_for(|stop| *stop) => return Ok(()),
                _ = control::changed(&mut changes) => {},
                _ = writing.1.notified(), if mux.applying_policy || mux.refreshing_components || mux.draining_update => {},
                _ = async { update.expect("update control").changed().await }, if update.is_some() => {},
                error = &mut failure => return Err(error.context("host writer stopped")?),
                _ = sleep_until(tick) => {},
                event = runner::next(mux.runner.as_mut()) => mux.runner_event(event)?,
                // 旧 Runner の子孫を回収する間も heartbeat と host の入力を処理する。
                result = async { mux.closing.as_mut().expect("runner cleanup task").await }, if mux.closing.is_some() => {
                    mux.closing = None;
                    result.context("runner cleanup task failed")?;
                },
                message = source.next() => match message {
                    None | Some(Ok(Message::Close(_))) => bail!("disconnected from host"),
                    Some(Err(error)) => return Err(link::ws_error(error)),
                    Some(Ok(Message::Text(body))) => {
                        let frame = link::parse_host_text(&body)?;
                        if frame.envelope.stream != 0 {
                            if let Some(changes) = &changes {
                                mux.observe_changes(*changes.borrow(), applied);
                            }
                            mux.host_frame(frame)?;
                        } else if frame.envelope.kind == "heartbeat" {
                            receive_by = Instant::now() + interval * 3;
                        } else if frame.envelope.kind == "host_mcp_catalog" {
                            if frame.payload().and_then(|payload| {
                                crate::host_mcp::store_catalog(home, &fingerprint, &payload)
                            }).is_err() {
                                tracing::warn!("host MCP catalog could not be stored");
                            }
                        } else if frame.envelope.kind == "provider_catalog" {
                            match frame.payload() {
                                Ok(payload) => {
                                    if let Err(error) =
                                        crate::declarations::store_catalog(mux.home, &payload)
                                    {
                                        tracing::warn!(
                                            "provider catalog was not stored: {}",
                                            crate::declarations::store_failure_text(&error.to_string())
                                        );
                                    }
                                }
                                Err(_) => {
                                    crate::declarations::note_store_failure(
                                        mux.home,
                                        "invalid provider catalog",
                                    );
                                    tracing::warn!(
                                        "provider catalog was not stored: invalid provider catalog"
                                    );
                                }
                            }
                        } else if frame.envelope.kind.starts_with("app_update_") {
                            if let Some(update) = update {
                                transfer.frame(update, frame).await;
                            }
                        } else if frame.envelope.kind == "reject" {
                            return Err(link::reject(&frame));
                        }
                    }
                    Some(Ok(_)) => {}
                }
            }
        }
    }
    .await;
    drop(transfer);
    if let Some(update) = update {
        update.disconnected();
    }
    let uptime = started.elapsed();
    writer.abort();
    drop(source);
    if mux.ready()
        && mux
            .work
            .values()
            .any(|work| work.relayed_mcp && !work.terminal)
        && mux
            .pipe_priority(Frame::new(0, "runner_link_lost", json!({})))
            .is_ok()
    {
        // Let the runner return an unknown-result error before killing the CLI tree.
        tokio::time::sleep(Duration::from_millis(800)).await;
    }
    mux.close_runner("link_closed");
    if let Some(closing) = mux.closing
        && let Err(error) = closing.await
    {
        tracing::warn!("runner cleanup task failed: {error}");
    }
    if let Some(control) = control {
        control.runner(false, 0, None, false);
    }
    SessionEnd {
        uptime,
        result,
        reconnect,
        installer,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn pending_update_keeps_queued_turns_and_refuses_new_work() {
        let mut mux = Mux::new(Path::new("."), Duration::ZERO);
        mux.host_frame(Frame::new(1, "turn", json!({}))).unwrap();
        mux.draining_update = true;
        mux.host_frame(Frame::new(2, "turn", json!({}))).unwrap();
        assert!(mux.work.contains_key(&1));
        assert!(!mux.work.contains_key(&2));
        assert!(!mux.ready_to_install(false));
        mux.cancel(1, None).unwrap();
        assert!(
            !mux.ready_to_install(false),
            "wait for the host writer as well"
        );
        assert_eq!(mux.host.next().await.envelope.stream, 2);
        assert!(mux.ready_to_install(false));
    }

    #[test]
    fn capacity_and_pre_dispatch_cancel() {
        let mut mux = Mux::new(Path::new("."), Duration::ZERO);
        for stream in 1..=33 {
            mux.host_frame(Frame::new(stream, "turn", json!({})))
                .unwrap();
        }
        assert_eq!(mux.work.len(), WORK_LIMIT);
        assert!(!mux.work.contains_key(&33));
        mux.host_frame(Frame::new(4, "cancel", json!({}))).unwrap();
        assert!(!mux.work.contains_key(&4));
        mux.host_frame(Frame::new(34, "quota", json!({}))).unwrap();
        assert!(mux.work.contains_key(&34));
        assert!(mux.runner.is_none());
    }

    #[tokio::test]
    async fn policy_apply_preserves_existing_work_and_rejects_new_streams() {
        let mut mux = Mux::new(Path::new("."), Duration::ZERO);
        mux.host_frame(Frame::new(1, "turn", json!({}))).unwrap();
        mux.applying_policy = true;
        for (id, start, expected) in [
            (2, "turn", "error"),
            (3, "login", "login_error"),
            (4, "quota", "quota_error"),
            (5, "status", "status_error"),
        ] {
            mux.host_frame(Frame::new(id, start, json!({}))).unwrap();
            let error = mux.host.next().await;
            assert_eq!(
                (error.envelope.stream, error.envelope.kind.as_str()),
                (id, expected)
            );
        }
        assert_eq!(mux.work.len(), 1);
        assert!(mux.pipe.start_pending(1));
    }

    #[tokio::test]
    async fn a_failed_policy_write_resumes_accepting_work_without_reconnecting() {
        let mut mux = Mux::new(Path::new("."), Duration::ZERO);
        let control = std::sync::Arc::new(Control::new(|| {}));
        let changes = control.changes();
        let applied = *changes.borrow();
        let write = control.begin_policy_save();
        mux.observe_changes(*changes.borrow(), applied);
        mux.host_frame(Frame::new(1, "turn", json!({}))).unwrap();
        assert_eq!(mux.host.next().await.envelope.kind, "error");
        assert!(mux.work.is_empty());
        assert!(!mux.applying_policy);

        drop(write);
        mux.observe_changes(*changes.borrow(), applied);
        mux.host_frame(Frame::new(2, "turn", json!({}))).unwrap();
        assert!(mux.pipe.start_pending(2));
        assert!(!mux.applying_policy);
    }

    #[tokio::test]
    async fn cleanup_keeps_the_mux_available_and_defers_starting_a_replacement() {
        let mut mux = Mux::new(Path::new("."), Duration::ZERO);
        let (finish, cleaning) = oneshot::channel();
        let closing = tokio::spawn(async {
            let _ = cleaning.await;
        });
        mux.closing = Some(closing);

        let now = Instant::now();
        mux.host_frame(Frame::new(1, "turn", json!({}))).unwrap();
        mux.update(now).unwrap();
        assert!(mux.runner.is_none());
        mux.host_priority(Frame::new(0, "heartbeat", json!({})))
            .unwrap();
        assert_eq!(mux.host.next().await.envelope.kind, "heartbeat");
        assert!(mux.pipe.start_pending(1));

        mux.host_frame(Frame::new(1, "cancel", json!({}))).unwrap();
        assert!(mux.work.is_empty());
        finish.send(()).unwrap();
        mux.closing.take().unwrap().await.unwrap();
    }

    #[tokio::test]
    async fn overflow_discards_only_the_affected_lane_and_prioritizes_cancel() {
        let mut mux = Mux::new(Path::new("."), Duration::ZERO);
        mux.pipe.enable_data(true);
        mux.host_frame(Frame::new(1, "login", json!({}))).unwrap();
        assert_eq!(mux.pipe.next().await.envelope.kind, "login");
        mux.host_frame(Frame::new(2, "quota", json!({}))).unwrap();
        assert!(mux.host.push(Frame::new(1, "login_output", json!({}))));
        assert!(mux.host.push(Frame::new(2, "quota_result", json!({}))));

        mux.overflow(1).unwrap();
        assert!(mux.work[&1].cancel_by.is_some());
        assert!(mux.work[&2].cancel_by.is_none());
        assert_eq!(mux.pipe.next().await.envelope.kind, "login_cancel");
        assert_eq!(mux.pipe.next().await.envelope.kind, "quota");
        let error = mux.host.next().await;
        assert_eq!(
            (error.envelope.stream, error.envelope.kind.as_str()),
            (1, "login_error")
        );
        assert_eq!(mux.host.next().await.envelope.kind, "quota_result");
    }
}
