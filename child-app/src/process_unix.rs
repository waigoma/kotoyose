#[cfg(target_os = "linux")]
use std::{collections::BTreeSet, sync::Mutex, time::Duration};
use std::{io, path::Path, process::Stdio};

use anyhow::{Context, Result};
use tokio::process::{Child, Command};

use super::{Input, Output, STOP_TIMEOUT};

#[cfg(target_os = "linux")]
static MANAGED_CHILDREN: Mutex<BTreeSet<i32>> = Mutex::new(BTreeSet::new());

/// Keep a directly managed subprocess out of the runner's orphan cleanup.
#[cfg(target_os = "linux")]
pub(crate) struct ManagedChild(i32);

#[cfg(target_os = "linux")]
impl ManagedChild {
    pub(crate) fn track(pid: u32) -> Self {
        let pid = pid as i32;
        MANAGED_CHILDREN
            .lock()
            .expect("managed child PIDs")
            .insert(pid);
        Self(pid)
    }
}

#[cfg(target_os = "linux")]
impl Drop for ManagedChild {
    fn drop(&mut self) {
        MANAGED_CHILDREN
            .lock()
            .expect("managed child PIDs")
            .remove(&self.0);
    }
}

pub fn prepare() -> io::Result<()> {
    // PTY logins, quota and some adapters create new sessions. Adopt their
    // descendants when a runner dies so teardown also reaches escaped groups.
    #[cfg(target_os = "linux")]
    prctl_value(libc::PR_SET_CHILD_SUBREAPER, 1)?;
    Ok(())
}

#[cfg(target_os = "linux")]
fn prctl_value(option: libc::c_int, value: libc::c_ulong) -> io::Result<()> {
    let unused: libc::c_ulong = 0;
    if unsafe { libc::prctl(option, value, unused, unused, unused) } == 0 {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

pub struct Process {
    child: Child,
    group: Option<i32>,
}

pub fn spawn(python: &Path, home: &Path, temp: &Path) -> Result<(Process, Input, Output)> {
    let mut command = Command::new(python);
    command
        .args(["-I", "-m", "kotoyose.child_runner", "--home"])
        .arg(home)
        .arg("--temp")
        .arg(temp)
        .process_group(0)
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::null())
        .kill_on_drop(true);
    #[cfg(target_os = "linux")]
    {
        let parent = unsafe { libc::getpid() };
        // prctl の直前に Link app が終了する場合に備え、設定後に親 PID を照合する。
        unsafe {
            command.pre_exec(move || {
                prctl_value(libc::PR_SET_PDEATHSIG, libc::SIGKILL as libc::c_ulong)?;
                if libc::getppid() != parent {
                    return Err(io::Error::from_raw_os_error(libc::ESRCH));
                }
                Ok(())
            });
        }
    }
    let mut child = command.spawn().context("cannot start runner")?;
    let group = child.id().map(|pid| pid as i32);
    let input = Box::new(child.stdin.take().expect("piped stdin"));
    let output = Box::new(child.stdout.take().expect("piped stdout"));
    Ok((Process { child, group }, input, output))
}

impl Process {
    pub fn id(&self) -> u32 {
        self.child.id().expect("runner process ID before wait")
    }

    fn kill_group(&mut self) {
        if let Some(group) = self.group.take() {
            // The direct child is still owned here, so its group ID cannot be reused.
            unsafe { libc::killpg(group, libc::SIGKILL) };
        }
    }

    pub async fn stop(&mut self) -> Result<()> {
        self.kill_group();
        tokio::time::timeout(STOP_TIMEOUT, async {
            self.child.wait().await?;
            #[cfg(target_os = "linux")]
            stop_adopted().await?;
            Ok(())
        })
        .await
        .context("runner tree did not exit within 5 seconds")?
    }

    pub fn memory_probe(&self) -> crate::control::MemoryProbe {
        let pid = self.group;
        #[cfg(target_os = "linux")]
        {
            let parent_children = std::fs::read_link("/proc/thread-self")
                .ok()
                .map(|path| Path::new("/proc").join(path).join("children"));
            std::sync::Arc::new(move || tree_rss(pid?, parent_children.as_deref()))
        }
        #[cfg(not(target_os = "linux"))]
        {
            let _ = pid;
            std::sync::Arc::new(|| None)
        }
    }

    pub fn reap_exited_descendants(&self) {
        #[cfg(target_os = "linux")]
        for pid in adopted() {
            if Some(pid) != self.group {
                // Do not reap the child owned by tokio.
                unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) };
            }
        }
    }
}

#[cfg(target_os = "linux")]
fn tree_rss(root: i32, parent_children: Option<&Path>) -> Option<u64> {
    use std::collections::HashSet;
    use std::fs::{read_dir, read_to_string};

    let mut seen = HashSet::new();
    let mut pending = parent_children.map_or_else(Vec::new, runner_children);
    pending.push(root);
    let mut bytes = 0;
    while let Some(pid) = pending.pop() {
        if !seen.insert(pid) {
            continue;
        }
        let rss = read_to_string(format!("/proc/{pid}/status"))
            .ok()
            .and_then(|status| {
                status.lines().find_map(|line| {
                    line.strip_prefix("VmRSS:")?
                        .split_whitespace()
                        .next()?
                        .parse::<u64>()
                        .ok()
                })
            });
        if pid == root && rss.is_none() {
            return None;
        }
        bytes += rss.unwrap_or(0) * 1024;
        if let Ok(threads) = read_dir(format!("/proc/{pid}/task")) {
            for thread in threads.flatten() {
                pending.extend(children(&thread.path().join("children")));
            }
        }
    }
    Some(bytes)
}

#[cfg(target_os = "linux")]
async fn stop_adopted() -> io::Result<()> {
    // 子を回収すると、その子孫がこのスレッドに引き取られる場合がある。
    // waitpid(-1) は他の処理が所有する子も回収するため、対象 PID ごとに待つ。
    loop {
        let children = adopted();
        if children.is_empty() {
            return Ok(());
        }
        let mut waiting = false;
        for pid in children {
            unsafe { libc::kill(pid, libc::SIGKILL) };
            match unsafe { libc::waitpid(pid, std::ptr::null_mut(), libc::WNOHANG) } {
                0 => waiting = true,
                -1 => match io::Error::last_os_error() {
                    error if error.raw_os_error() == Some(libc::ECHILD) => {}
                    error if error.kind() == io::ErrorKind::Interrupted => waiting = true,
                    error => return Err(error),
                },
                _ => {}
            }
        }
        if waiting {
            tokio::time::sleep(Duration::from_millis(5)).await;
        } else {
            tokio::task::yield_now().await;
        }
    }
}

#[cfg(target_os = "linux")]
fn adopted() -> Vec<i32> {
    runner_children(Path::new("/proc/thread-self/children"))
}

#[cfg(target_os = "linux")]
fn runner_children(path: &Path) -> Vec<i32> {
    let excluded = MANAGED_CHILDREN.lock().expect("managed child PIDs");
    children(path)
        .into_iter()
        .filter(|pid| !excluded.contains(pid))
        .collect()
}

#[cfg(target_os = "linux")]
fn children(path: &Path) -> Vec<i32> {
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .split_whitespace()
        .filter_map(|pid| pid.parse().ok())
        .collect()
}

impl Drop for Process {
    fn drop(&mut self) {
        self.kill_group();
    }
}
