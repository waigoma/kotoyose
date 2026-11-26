use std::{
    path::Path,
    sync::{Arc, Mutex},
    time::Duration,
};

use serde::Serialize;
use tokio::sync::watch;

#[derive(Clone, Copy, Default, PartialEq, Eq)]
pub struct Changes {
    pub policy: u64,
    pub components: u64,
    pub saving_policy: bool,
    pub idle: Option<(u64, Duration)>,
}

#[derive(Clone, Copy, Default, PartialEq, Eq, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum Connection {
    #[default]
    Trying,
    Connected,
    Waiting,
    Unregistered,
    Stopped,
}

#[derive(Clone, Default, Serialize)]
pub struct Status {
    pub connection: Connection,
    pub host: Option<String>,
    pub host_version: Option<String>,
    pub error: Option<String>,
    pub work: usize,
    pub applying_policy: bool,
    pub runner: bool,
    #[serde(skip)]
    pub memory: Option<MemoryProbe>,
}

pub type MemoryProbe = Arc<dyn Fn() -> Option<u64> + Send + Sync>;

pub struct Control {
    pub update: crate::update::Update,
    pub repairs: crate::components::AutoRepairs,
    status: Mutex<Status>,
    wake: Box<dyn Fn() + Send + Sync>,
    changes: watch::Sender<Changes>,
}

impl Control {
    pub fn new(wake: impl Fn() + Send + Sync + 'static) -> Self {
        Self {
            status: Mutex::default(),
            update: crate::update::Update::default(),
            repairs: crate::components::AutoRepairs::default(),
            wake: Box::new(wake),
            changes: watch::Sender::new(Changes::default()),
        }
    }

    pub fn status(&self) -> Status {
        self.status.lock().expect("desktop status").clone()
    }

    pub fn modify(&self, change: impl FnOnce(&mut Changes)) {
        self.changes.send_modify(change);
    }

    pub fn update_idle(&self, idle: Duration) {
        self.modify(|changes| {
            changes.idle = Some((changes.idle.map_or(1, |(version, _)| version + 1), idle));
        });
    }

    pub fn begin_policy_save(self: &Arc<Self>) -> PolicySave {
        self.modify(|changes| changes.saving_policy = true);
        PolicySave {
            control: self.clone(),
            committed: false,
        }
    }

    pub fn changes(&self) -> watch::Receiver<Changes> {
        self.changes.subscribe()
    }

    pub fn connection(&self, home: &Path, connection: Connection, error: Option<String>) {
        let host = matches!(
            connection,
            Connection::Trying | Connection::Connected | Connection::Unregistered
        )
        .then(|| crate::state::Host::load(home).ok().map(|host| host.address));
        let mut status = self.status.lock().expect("desktop status");
        status.connection = connection;
        if error.is_some() || connection == Connection::Connected {
            status.error = error;
        }
        status.host_version =
            (connection == Connection::Connected).then(|| crate::VERSION.to_owned());
        if let Some(host) = host {
            status.host = host;
        }
        let stopped = (connection == Connection::Stopped)
            .then(|| status.error.clone())
            .flatten();
        drop(status);
        (self.wake)();

        let event = match connection {
            Connection::Trying => crate::local_log::Event::Connecting,
            Connection::Connected => crate::local_log::Event::Connected,
            Connection::Waiting => return,
            Connection::Unregistered => crate::local_log::Event::Unregistered,
            Connection::Stopped => crate::local_log::Event::Stopped(stopped.as_deref()),
        };
        crate::local_log::record(home, event);
    }

    pub fn runner(
        &self,
        runner: bool,
        work: usize,
        memory: Option<MemoryProbe>,
        applying_policy: bool,
    ) {
        let mut status = self.status.lock().expect("desktop status");
        status.runner = runner;
        status.work = work;
        status.memory = memory;
        status.applying_policy = applying_policy;
    }
}

pub struct PolicySave {
    control: Arc<Control>,
    committed: bool,
}

impl PolicySave {
    pub fn commit(mut self) {
        self.committed = true;
    }
}

impl Drop for PolicySave {
    fn drop(&mut self) {
        self.control.modify(|changes| {
            changes.saving_policy = false;
            changes.policy += u64::from(self.committed);
        });
    }
}

pub async fn changed(changes: &mut Option<watch::Receiver<Changes>>) {
    match changes {
        Some(changes) => {
            let _ = changes.changed().await;
        }
        None => std::future::pending().await,
    }
}

pub fn current(changes: &mut Option<watch::Receiver<Changes>>) -> Changes {
    changes
        .as_mut()
        .map(|changes| *changes.borrow_and_update())
        .unwrap_or_default()
}
