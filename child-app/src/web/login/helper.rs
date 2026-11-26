use std::{ffi::OsStr, path::Path};

use anyhow::Result;

use crate::process::{Input, Output};

#[cfg(unix)]
type Process = tokio::process::Child;
#[cfg(windows)]
use crate::process::Process;

pub(super) struct Helper {
    pub(super) input: Option<Input>,
    pub(super) output: Output,
    process: Process,
    #[cfg(target_os = "linux")]
    _tracked: crate::process::LoginChild,
}

impl Helper {
    pub(super) fn spawn<'a>(
        python: &Path,
        args: impl IntoIterator<Item = &'a OsStr>,
    ) -> Result<Self> {
        #[cfg(unix)]
        let (process, input, output) = {
            use std::process::Stdio;

            let mut child = crate::components::command(python)
                .args(args)
                .stdin(Stdio::piped())
                .stdout(Stdio::piped())
                .stderr(Stdio::null())
                // Closing the pipe gives the helper time to kill and reap the CLI.
                .kill_on_drop(false)
                .spawn()?;
            let input: Input = Box::new(child.stdin.take().expect("piped stdin"));
            let output: Output = Box::new(child.stdout.take().expect("piped stdout"));
            (child, input, output)
        };
        #[cfg(windows)]
        let (process, input, output) = crate::process::spawn_in_job(python, args)?;
        #[cfg(target_os = "linux")]
        let tracked = crate::process::LoginChild::track(process.id().expect("spawned helper PID"));
        Ok(Self {
            input: Some(input),
            output,
            process,
            #[cfg(target_os = "linux")]
            _tracked: tracked,
        })
    }

    pub(super) async fn wait(&mut self) -> Result<bool> {
        #[cfg(unix)]
        return Ok(self.process.wait().await?.success());
        #[cfg(windows)]
        return Ok(self.process.wait().await?);
    }

    #[cfg(windows)]
    pub(super) fn release_job(&mut self) -> std::io::Result<()> {
        self.process.release_job()
    }

    pub(super) async fn finish(mut self, _stop: bool) {
        drop(self.input.take());
        #[cfg(unix)]
        if _stop {
            use std::time::Duration;

            if let Some(pid) = self.process.id() {
                // SIGTERM lets the helper clean up the separate CLI process group.
                unsafe { libc::kill(pid as libc::pid_t, libc::SIGTERM) };
            }
            if !matches!(
                tokio::time::timeout(Duration::from_secs(1), self.process.wait()).await,
                Ok(Ok(_))
            ) {
                let _ = self.process.start_kill();
                let _ = self.process.wait().await;
            }
        }
        #[cfg(windows)]
        {
            let _ = self.process.stop().await;
            let _ = self.process.wait().await;
        }
    }
}
