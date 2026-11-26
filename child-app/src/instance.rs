use std::{
    fs::{self, File},
    io,
    net::{Ipv4Addr, UdpSocket},
    path::{Path, PathBuf},
    time::{Duration, Instant},
};

use anyhow::{Context, Result, bail};
use ring::hmac;
use serde::{Deserialize, Serialize};
use subtle::ConstantTimeEq;
use tokio::{
    sync::{mpsc, watch},
    task::JoinHandle,
};

use crate::{desktop::Request, state};

const RETRY: Duration = Duration::from_millis(100);
const OPEN_TIMEOUT: Duration = Duration::from_secs(3);
const QUIT_TIMEOUT: Duration = Duration::from_secs(15);

#[derive(Deserialize, Serialize)]
struct Endpoint {
    port: u16,
    key: [u8; 32],
}

impl Endpoint {
    fn tag(&self, message: &[u8]) -> hmac::Tag {
        hmac::sign(&hmac::Key::new(hmac::HMAC_SHA256, &self.key), message)
    }

    fn forward(&self, request: &[u8], ack: &[u8]) -> io::Result<bool> {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        socket.connect((Ipv4Addr::LOCALHOST, self.port))?;
        socket.set_read_timeout(Some(RETRY))?;
        socket.send(self.tag(request).as_ref())?;
        let mut reply = [0; 33];
        let size = socket.recv(&mut reply)?;
        Ok(bool::from(reply[..size].ct_eq(self.tag(ack).as_ref())))
    }
}

pub struct Primary {
    path: PathBuf,
    _lock: File,
}

pub struct Listener {
    socket: UdpSocket,
    endpoint: Endpoint,
}

impl Primary {
    fn start(path: PathBuf, lock: File) -> Result<(Self, Listener)> {
        let socket = UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))?;
        socket.set_nonblocking(true)?;
        let mut key = [0; 32];
        getrandom::fill(&mut key).map_err(io::Error::other)?;
        let endpoint = Endpoint {
            port: socket.local_addr()?.port(),
            key,
        };
        state::replace_checked(
            &path,
            &serde_json::to_vec(&endpoint)?,
            state::protect_secret,
            || Ok(()),
        )
        .context("cannot publish instance endpoint")?;
        Ok((Self { path, _lock: lock }, Listener { socket, endpoint }))
    }
}

impl Listener {
    pub fn listen(
        self,
        commands: mpsc::Sender<Request>,
        stop: watch::Sender<bool>,
    ) -> io::Result<JoinHandle<()>> {
        let socket = tokio::net::UdpSocket::from_std(self.socket)?;
        let open = self.endpoint.tag(b"open");
        let open_ack = self.endpoint.tag(b"ack");
        let quit = self.endpoint.tag(b"quit");
        let quit_ack = self.endpoint.tag(b"quit-ack");
        Ok(tokio::spawn(async move {
            let mut buf = [0; 33];
            loop {
                match socket.recv_from(&mut buf).await {
                    Ok((size, addr)) => {
                        if bool::from(buf[..size].ct_eq(quit.as_ref())) {
                            stop.send_replace(true);
                            let _ = socket.send_to(quit_ack.as_ref(), addr).await;
                        } else if bool::from(buf[..size].ct_eq(open.as_ref()))
                            && commands.try_send(Request::Open).is_ok()
                        {
                            let _ = socket.send_to(open_ack.as_ref(), addr).await;
                        }
                    }
                    #[cfg(windows)]
                    Err(error)
                        if matches!(
                            error.raw_os_error(),
                            Some(
                                windows_sys::Win32::Networking::WinSock::WSAEMSGSIZE
                                    | windows_sys::Win32::Networking::WinSock::WSAECONNRESET
                            )
                        ) => {}
                    Err(error) => {
                        tracing::warn!("instance listener stopped: {}", error.kind());
                        break;
                    }
                }
            }
        }))
    }
}

impl Drop for Primary {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

pub fn claim(home: &Path) -> Result<Option<(Primary, Listener)>> {
    state::private_dir(home).context("cannot create child state directory")?;
    let path = home.join("instance.endpoint");
    let deadline = Instant::now() + OPEN_TIMEOUT;
    loop {
        if let Some(lock) = state::lock(home)? {
            return Primary::start(path, lock).map(Some);
        }
        let next = Instant::now() + RETRY;
        if let Ok(endpoint) = state::read_json::<Endpoint>(&path)
            && endpoint.forward(b"open", b"ack").unwrap_or(false)
        {
            return Ok(None);
        }
        if Instant::now() >= deadline {
            bail!("child is already running; settings signal was not acknowledged");
        }
        std::thread::sleep(next.saturating_duration_since(Instant::now()));
    }
}

pub fn quit(home: &Path) -> Result<()> {
    wait_for_quit(home, QUIT_TIMEOUT)
}

fn wait_for_quit(home: &Path, timeout: Duration) -> Result<()> {
    let path = home.join("instance.endpoint");
    let deadline = Instant::now() + timeout;
    let mut acknowledged = false;
    while state::instance_running(home)? {
        let now = Instant::now();
        if now >= deadline {
            bail!("child did not release the instance lock after a quit request");
        }
        let next = (now + RETRY).min(deadline);
        if !acknowledged && let Ok(endpoint) = state::read_json::<Endpoint>(&path) {
            acknowledged = endpoint.forward(b"quit", b"quit-ack").unwrap_or(false);
        }
        std::thread::sleep(next.saturating_duration_since(Instant::now()));
    }
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn instance_listener_acknowledges_only_authenticated_requests() {
        let dir = tempfile::tempdir().unwrap();
        let (primary, listener) = claim(dir.path()).unwrap().unwrap();
        let addr = listener.socket.local_addr().unwrap();
        let (send, mut requests) = mpsc::channel(1);
        let (stop, stopping) = watch::channel(false);
        let listener = listener.listen(send, stop).unwrap();
        let socket = tokio::net::UdpSocket::bind((Ipv4Addr::LOCALHOST, 0))
            .await
            .unwrap();
        socket.send_to(&[0; 32], addr).await.unwrap();
        let mut reply = [0; 33];
        assert!(
            tokio::time::timeout(RETRY, socket.recv_from(&mut reply))
                .await
                .is_err()
        );
        assert!(requests.try_recv().is_err());
        assert!(!*stopping.borrow());

        let home = dir.path().to_owned();
        assert!(
            tokio::task::spawn_blocking(move || claim(&home))
                .await
                .unwrap()
                .unwrap()
                .is_none()
        );
        assert!(matches!(requests.try_recv().unwrap(), Request::Open));
        assert!(!*stopping.borrow());
        listener.abort();
        let _ = listener.await;
        drop(primary);
        assert!(!dir.path().join("instance.endpoint").exists());
    }

    #[tokio::test]
    async fn quit_signals_with_a_full_command_queue_and_waits_for_unlock() {
        let dir = tempfile::tempdir().unwrap();
        let (primary, listener) = claim(dir.path()).unwrap().unwrap();
        let (send, _requests) = mpsc::channel(1);
        send.try_send(Request::Open).unwrap();
        let (stop, mut stopping) = watch::channel(false);
        let listener = listener.listen(send, stop).unwrap();

        let home = dir.path().to_owned();
        let quitting = tokio::task::spawn_blocking(move || quit(&home));
        tokio::time::timeout(OPEN_TIMEOUT, stopping.wait_for(|stop| *stop))
            .await
            .unwrap()
            .unwrap();
        assert!(state::instance_running(dir.path()).unwrap());
        assert!(!quitting.is_finished());
        listener.abort();
        let _ = listener.await;
        drop(primary);
        quitting.await.unwrap().unwrap();
    }

    #[test]
    fn quit_is_idempotent_and_times_out_for_a_lock_without_a_listener() {
        let dir = tempfile::tempdir().unwrap();
        quit(dir.path()).unwrap();
        assert!(!dir.path().join("instance.lock").exists());

        let lock = state::lock(dir.path()).unwrap().unwrap();
        let error = wait_for_quit(dir.path(), RETRY).unwrap_err();
        assert!(
            error
                .to_string()
                .contains("did not release the instance lock")
        );
        drop(lock);
        quit(dir.path()).unwrap();
    }
}
