use std::{
    collections::BTreeMap,
    fs,
    path::Path,
    sync::{Arc, Mutex, OnceLock},
};

use serde::Serialize;
use tokio::{sync::watch, task::JoinHandle};

use super::{
    COMPONENT_LOCK, Provider, Runtime, available, finish_package_change, install_command,
    invalidate_availability, needs_reinstall, providers, start_package_change,
};
use crate::{control::Control, local_log, state, until_stopped};

#[derive(Clone, Copy, Serialize)]
#[serde(rename_all = "snake_case")]
pub enum RepairStatus {
    Pending,
    Installing,
    Done,
    Failed,
}

#[derive(Default)]
pub struct AutoRepairs {
    statuses: Mutex<BTreeMap<String, RepairStatus>>,
}

impl AutoRepairs {
    pub fn snapshot(&self) -> BTreeMap<String, RepairStatus> {
        self.statuses.lock().expect("provider repairs").clone()
    }

    pub(super) fn reconcile(&self, needs_reinstall: &[String]) -> BTreeMap<String, RepairStatus> {
        let mut statuses = self.statuses.lock().expect("provider repairs");
        statuses.retain(|provider, status| match status {
            RepairStatus::Done => !needs_reinstall.contains(provider),
            RepairStatus::Failed => needs_reinstall.contains(provider),
            RepairStatus::Pending | RepairStatus::Installing => true,
        });
        statuses.clone()
    }

    fn set(&self, provider: &Provider, status: RepairStatus) {
        self.statuses
            .lock()
            .expect("provider repairs")
            .insert(provider.id.clone(), status);
    }

    fn remove_pending(&self) {
        self.statuses
            .lock()
            .expect("provider repairs")
            .retain(|_, status| !matches!(status, RepairStatus::Pending));
    }
}

fn load_policy(home: &Path) -> Option<state::Policy> {
    let policy = state::Policy::parse(&fs::read_to_string(home.join("policy.json")).ok()?).ok()?;
    policy.validate(home).ok()?;
    Some(policy)
}

fn eligible(runtime: &Runtime, provider: &Provider, policy: &state::Policy) -> bool {
    needs_reinstall(&runtime.python, &provider.id) && policy.adapters.contains(&provider.driver)
}

pub struct RepairTask {
    stop: watch::Sender<bool>,
    task: JoinHandle<()>,
}

impl RepairTask {
    pub fn start(home: &Path, control: &Arc<Control>) -> Option<Self> {
        static STARTED: OnceLock<()> = OnceLock::new();
        Self::start_once(&STARTED, home, control, Runtime::installed())
    }

    fn start_once(
        started: &OnceLock<()>,
        home: &Path,
        control: &Arc<Control>,
        runtime: Option<Runtime>,
    ) -> Option<Self> {
        started.set(()).ok()?;
        let runtime = runtime.filter(Runtime::ready)?;
        let mut pending: Vec<_> = providers()
            .into_iter()
            .filter(|provider| {
                !provider.packages.is_empty() && needs_reinstall(&runtime.python, &provider.id)
            })
            .collect();
        if pending.is_empty() {
            return None;
        }
        let policy = load_policy(home)?;
        pending.retain(|provider| policy.adapters.contains(&provider.driver));
        if pending.is_empty() {
            return None;
        }
        for provider in &pending {
            control.repairs.set(provider, RepairStatus::Pending);
        }
        let (stop, stopping) = watch::channel(false);
        let home = home.to_owned();
        let control = control.clone();
        let task = tokio::spawn(async move {
            restore(&home, &control, &runtime, pending, stopping).await;
            control.repairs.remove_pending();
        });
        Some(Self { stop, task })
    }

    pub async fn stop(self) {
        self.stop.send_replace(true);
        let _ = self.task.await;
    }
}

async fn restore(
    home: &Path,
    control: &Control,
    runtime: &Runtime,
    pending: Vec<Provider>,
    mut stopping: watch::Receiver<bool>,
) {
    let sites = state::site_packages(&runtime.python);
    for provider in pending {
        // Keep a snapshot of unaffected adapters for hello while pip holds the write lock.
        if until_stopped(
            &mut stopping,
            available(home, Some(&runtime.python), &sites),
        )
        .await
        .is_none()
        {
            break;
        }
        let Some(lock) = until_stopped(&mut stopping, COMPONENT_LOCK.write()).await else {
            break;
        };
        let pip = install_command(runtime, &provider);
        let Some(save) = until_stopped(&mut stopping, state::SAVE_LOCK.lock()).await else {
            break;
        };
        if !load_policy(home).is_some_and(|policy| eligible(runtime, &provider, &policy)) {
            control
                .repairs
                .statuses
                .lock()
                .expect("provider repairs")
                .remove(&provider.id);
            continue;
        }
        if *stopping.borrow() {
            break;
        }
        control.repairs.set(&provider, RepairStatus::Installing);
        // Serialize the policy check and spawn with policy saves.
        let started = pip.and_then(|pip| start_package_change(runtime, &provider, pip));
        drop(save);
        let log = |message: &str| {
            local_log::record(
                home,
                local_log::Event::ProviderRepair {
                    provider: &provider.id,
                    message,
                },
            );
        };
        log("installing");
        let result = match started {
            Ok(pip) => {
                finish_package_change(
                    runtime,
                    &provider,
                    pip,
                    |message| {
                        log(message);
                        Ok(())
                    },
                    async {
                        let _ = stopping.wait_for(|stop| *stop).await;
                    },
                )
                .await
            }
            Err(error) => Err(error),
        };
        invalidate_availability().await;
        drop(lock);
        match result {
            Ok(_) => {
                log("done");
                control.repairs.set(&provider, RepairStatus::Done);
                control.modify(|changes| changes.components += 1);
            }
            Err(error) => {
                log(&format!("failed: {error}"));
                control.repairs.set(&provider, RepairStatus::Failed);
            }
        }
    }
}

#[cfg(all(test, unix))]
mod tests {
    use std::{os::unix::fs::PermissionsExt, time::Duration};

    use super::*;
    use crate::components::{clear_reinstall, mark_for_reinstall, provider};

    fn fixture(root: &Path, exit_code: u8) -> Runtime {
        let python = root.join("python/bin/python3");
        let payload = root.join("parts");
        fs::create_dir_all(python.parent().unwrap()).unwrap();
        fs::create_dir(&payload).unwrap();
        fs::write(python.with_file_name("kotoyose-ready"), []).unwrap();
        for name in ["claude", "codex"] {
            fs::write(payload.join(format!("requirements-{name}.txt")), []).unwrap();
        }
        fs::write(
            &python,
            format!(
                r#"#!/bin/sh
[ "$4" = pip ] || exit 3
for arg do requirements=$arg; done
printf '%s\n' "${{requirements##*/}}" >> "$0.calls"
if [ -f "$0.block" ]; then exec /bin/sleep 120; fi
exit {exit_code}
"#
            ),
        )
        .unwrap();
        fs::set_permissions(&python, fs::Permissions::from_mode(0o700)).unwrap();
        let policy = state::Policy {
            adapters: vec![provider("claude").unwrap().driver],
            ..state::Policy::default()
        };
        fs::write(
            root.join("policy.json"),
            serde_json::to_vec(&policy).unwrap(),
        )
        .unwrap();
        Runtime { python, payload }
    }

    async fn complete(mut task: RepairTask) {
        tokio::time::timeout(Duration::from_secs(10), &mut task.task)
            .await
            .unwrap()
            .unwrap();
    }

    #[tokio::test]
    async fn restores_only_marked_providers_allowed_by_policy() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path();
        let runtime = fixture(home, 0);
        mark_for_reinstall(&runtime.python, "claude").unwrap();
        mark_for_reinstall(&runtime.python, "codex").unwrap();

        let state = Arc::new(Control::new(|| {}));
        complete(
            RepairTask::start_once(&OnceLock::new(), home, &state, Some(runtime.clone())).unwrap(),
        )
        .await;

        assert!(!needs_reinstall(&runtime.python, "claude"));
        assert!(needs_reinstall(&runtime.python, "codex"));
        assert_eq!(
            fs::read_to_string(runtime.python.with_extension("calls")).unwrap(),
            "requirements-claude.txt\n"
        );
        assert!(matches!(
            state.repairs.snapshot()["claude"],
            RepairStatus::Done
        ));
        assert!(!state.repairs.snapshot().contains_key("codex"));
        assert!(state.repairs.reconcile(&["claude".into()]).is_empty());
    }

    #[tokio::test]
    async fn leaves_unmarked_providers_alone() {
        let root = tempfile::tempdir().unwrap();
        let runtime = fixture(root.path(), 0);
        let state = Arc::new(Control::new(|| {}));

        assert!(
            RepairTask::start_once(&OnceLock::new(), root.path(), &state, Some(runtime.clone()))
                .is_none()
        );
        assert!(!runtime.python.with_extension("calls").exists());
    }

    #[tokio::test]
    async fn failure_keeps_the_marker_and_only_retries_on_a_new_start() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path();
        let runtime = fixture(home, 7);
        mark_for_reinstall(&runtime.python, "claude").unwrap();

        let started = OnceLock::new();
        let state = Arc::new(Control::new(|| {}));
        complete(RepairTask::start_once(&started, home, &state, Some(runtime.clone())).unwrap())
            .await;
        assert!(needs_reinstall(&runtime.python, "claude"));
        assert!(matches!(
            state.repairs.snapshot()["claude"],
            RepairStatus::Failed
        ));
        assert!(RepairTask::start_once(&started, home, &state, Some(runtime.clone())).is_none());
        let calls = runtime.python.with_extension("calls");
        assert_eq!(fs::read_to_string(&calls).unwrap().lines().count(), 1);

        let restarted = Arc::new(Control::new(|| {}));
        complete(
            RepairTask::start_once(&OnceLock::new(), home, &restarted, Some(runtime.clone()))
                .unwrap(),
        )
        .await;
        assert_eq!(fs::read_to_string(calls).unwrap().lines().count(), 2);
        assert!(needs_reinstall(&runtime.python, "claude"));
        clear_reinstall(&runtime.python, "claude").unwrap();
        assert!(restarted.repairs.reconcile(&[]).is_empty());
    }

    #[tokio::test]
    async fn shutdown_cancels_pip_and_keeps_the_marker() {
        let root = tempfile::tempdir().unwrap();
        let home = root.path();
        let runtime = fixture(home, 0);
        mark_for_reinstall(&runtime.python, "claude").unwrap();
        fs::write(runtime.python.with_extension("block"), []).unwrap();
        let state = Arc::new(Control::new(|| {}));
        let task =
            RepairTask::start_once(&OnceLock::new(), home, &state, Some(runtime.clone())).unwrap();

        tokio::time::timeout(Duration::from_secs(10), async {
            while !runtime.python.with_extension("calls").exists() {
                tokio::time::sleep(Duration::from_millis(5)).await;
            }
        })
        .await
        .unwrap();
        tokio::time::timeout(Duration::from_secs(6), task.stop())
            .await
            .unwrap();
        assert!(needs_reinstall(&runtime.python, "claude"));
        assert!(matches!(
            state.repairs.snapshot()["claude"],
            RepairStatus::Failed
        ));
        assert!(
            local_log::tail(home)
                .unwrap()
                .iter()
                .any(|line| line.contains("cancelled"))
        );
    }
}
