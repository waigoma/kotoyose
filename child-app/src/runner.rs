use std::{
    path::{Path, PathBuf},
    time::Duration,
};

use anyhow::{Context, Result, ensure};
use serde_json::json;
use tokio::{
    sync::mpsc,
    task::JoinHandle,
    time::{Instant, timeout},
};

use crate::{
    VERSION,
    frame::{self, Frame, IPC, write_timeout},
    local_log::{self, Event as LogEvent},
    outbox::Outbox,
    process::{self, Process},
    state,
};

const START_TIMEOUT: Duration = Duration::from_secs(15);
const SHUTDOWN_TIMEOUT: Duration = Duration::from_secs(5);

#[derive(Clone, Copy, PartialEq, Eq)]
pub enum Phase {
    Starting,
    Ready,
    Stopping,
    Stopped,
}

pub enum Event {
    Read(Result<Option<Frame>>),
    WriteFailed,
}

pub struct Runner {
    pub phase: Phase,
    pub deadline: Option<Instant>,
    pub events: mpsc::Receiver<Event>,
    process: Process,
    pid: u32,
    log_home: Option<PathBuf>,
    shutdown_reason: &'static str,
    pub memory: Option<crate::control::MemoryProbe>,
    temp: Option<tempfile::TempDir>,
    reader: JoinHandle<()>,
    writer: JoinHandle<()>,
}

impl Runner {
    pub fn start(
        home: &Path,
        idle: Duration,
        pipe: &Outbox,
        monitor: bool,
        host_fingerprint: &str,
    ) -> Result<Self> {
        let python = state::python()?;
        let temp = tempfile::Builder::new()
            .prefix(".runner-")
            .tempdir_in(home)?;
        let (process, mut input, mut output) = process::spawn(&python, home, temp.path())?;
        pipe.enable_data(false);
        ensure!(
            pipe.priority(Frame::new(
                0,
                "runner_init",
                json!({
                    "ipc":IPC, "version":VERSION, "runner_idle_seconds":idle.as_secs(),
                    "host_fingerprint":host_fingerprint,
                })
            )),
            "runner control queue is full"
        );
        let (sender, events) = mpsc::channel(1);
        let reading = sender.clone();
        let reader = tokio::task::spawn_local(async move {
            loop {
                let result = frame::read_record(&mut output).await;
                let end = !matches!(result, Ok(Some(_)));
                if reading.send(Event::Read(result)).await.is_err() || end {
                    break;
                }
            }
        });
        let outbox = pipe.clone();
        let writer = tokio::task::spawn_local(async move {
            loop {
                let frame = outbox.next().await;
                if !matches!(
                    timeout(
                        write_timeout(frame.body.len()),
                        frame::write_record(&mut input, &frame.body)
                    )
                    .await,
                    Ok(Ok(()))
                ) {
                    let _ = sender.send(Event::WriteFailed).await;
                    break;
                }
            }
        });
        let pid = process.id();
        tracing::info!("runner started pid={pid}");
        if monitor {
            local_log::record(home, LogEvent::RunnerStart(pid));
        }
        Ok(Self {
            pid,
            log_home: monitor.then(|| home.to_owned()),
            shutdown_reason: "idle",
            memory: monitor.then(|| process.memory_probe()),
            phase: Phase::Starting,
            deadline: Some(Instant::now() + START_TIMEOUT),
            events,
            process,
            temp: Some(temp),
            reader,
            writer,
        })
    }

    pub fn ready(&mut self, frame: &Frame, pipe: &Outbox) -> Result<()> {
        let payload = frame.payload()?;
        ensure!(
            self.phase == Phase::Starting
                && payload.get("ipc").and_then(|v| v.as_u64()) == Some(IPC)
                && payload.get("version").and_then(|v| v.as_str()) == Some(VERSION),
            "incompatible runner_ready"
        );
        self.phase = Phase::Ready;
        self.deadline = None;
        pipe.enable_data(true);
        Ok(())
    }

    pub fn shutdown(&mut self, pipe: &Outbox, now: Instant, reason: &'static str) -> Result<()> {
        self.shutdown_reason = reason;
        self.phase = Phase::Stopping;
        self.deadline = Some(now + SHUTDOWN_TIMEOUT);
        pipe.enable_data(false);
        ensure!(
            pipe.priority(Frame::new(0, "runner_shutdown", json!({}))),
            "runner control queue is full"
        );
        Ok(())
    }

    pub fn stopped(&mut self, frame: &Frame) -> Result<()> {
        ensure!(
            self.phase == Phase::Stopping
                && frame.payload()?.as_object().is_some_and(|p| p.is_empty()),
            "unexpected runner_stopped"
        );
        self.phase = Phase::Stopped;
        Ok(())
    }

    pub fn reap_exited_descendants(&self) {
        self.process.reap_exited_descendants();
    }

    pub fn close(mut self, reason: &'static str) -> JoinHandle<()> {
        let reason = if self.phase == Phase::Stopped && reason != "failure" {
            self.shutdown_reason
        } else {
            reason
        };
        let pid = self.pid;
        self.reader.abort();
        self.writer.abort();
        tokio::task::spawn_local(async move {
            match self.process.stop().await {
                Ok(()) => {
                    tracing::info!("runner stopped pid={pid} reason={reason}");
                    if let Some(home) = &self.log_home {
                        local_log::record(home, LogEvent::RunnerStop { pid, reason });
                    }
                }
                Err(error) => {
                    tracing::warn!(
                        "runner cleanup failed pid={pid}: {}",
                        local_log::diagnostic(&error)
                    );
                    if let Some(home) = &self.log_home {
                        local_log::record(home, LogEvent::RunnerFailure);
                    }
                }
            }
            if let Err(error) = self
                .temp
                .take()
                .expect("runner temporary directory")
                .close()
            {
                tracing::warn!(
                    "runner temporary directory cleanup failed: {}",
                    error.kind()
                );
            }
        })
    }
}

impl Drop for Runner {
    fn drop(&mut self) {
        self.reader.abort();
        self.writer.abort();
    }
}

pub async fn next(runner: Option<&mut Runner>) -> Result<Event> {
    match runner {
        Some(runner) => runner.events.recv().await.context("runner I/O stopped"),
        None => std::future::pending().await,
    }
}
