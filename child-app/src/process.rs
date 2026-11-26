use std::{path::Path, time::Duration};

use anyhow::Result;
use tokio::io::{AsyncRead, AsyncWrite};

#[cfg(unix)]
#[path = "process_unix.rs"]
mod platform;
#[cfg(windows)]
#[path = "process_windows.rs"]
mod platform;

#[cfg(target_os = "linux")]
pub(crate) use platform::ManagedChild;
#[cfg(windows)]
pub(crate) use platform::spawn_in_job;
pub use platform::{Process, prepare};

const STOP_TIMEOUT: Duration = Duration::from_secs(5);

pub type Input = Box<dyn AsyncWrite + Send + Unpin>;
pub type Output = Box<dyn AsyncRead + Send + Unpin>;

pub fn spawn(python: &Path, home: &Path, temp: &Path) -> Result<(Process, Input, Output)> {
    platform::spawn(python, home, temp)
}
