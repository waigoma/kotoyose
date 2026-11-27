use std::{
    io,
    path::Path,
    sync::{
        Arc,
        atomic::{AtomicU64, Ordering},
    },
    time::{Duration, SystemTime},
};

use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{Engine, engine::general_purpose::STANDARD};
use futures_util::{SinkExt, StreamExt};
use rustls::{
    CertificateError, ClientConfig, DigitallySignedStruct, SignatureScheme,
    client::danger::{HandshakeSignatureValid, ServerCertVerified, ServerCertVerifier},
    crypto::{WebPkiSupportedAlgorithms, verify_tls12_signature, verify_tls13_signature},
    pki_types::{CertificateDer, PrivateKeyDer, ServerName, UnixTime, pem::PemObject},
    time_provider::TimeProvider,
};
use serde_json::json;
use tokio::{net::TcpStream, time::timeout};
use tokio_rustls::{TlsConnector, client::TlsStream};
use tokio_tungstenite::{
    WebSocketStream, client_async_with_config,
    tungstenite::{Error as WsError, Message, protocol::WebSocketConfig},
};
use x509_parser::prelude::{FromDer, X509Certificate};

use crate::{
    VERSION,
    frame::{Frame, MAX_SKILL_TURN_RECORD, write_timeout},
    state::{self, Host},
};

pub const OPEN_TIMEOUT: Duration = Duration::from_secs(5);
const RESPONSE_TIMEOUT: Duration = Duration::from_secs(10);
pub type Socket = WebSocketStream<TlsStream<TcpStream>>;

#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Stop {
    Unregistered,
    Version,
    Revoked,
}

#[derive(Debug)]
pub struct Rejected {
    reason: String,
    stop: Option<Stop>,
}

impl std::fmt::Display for Rejected {
    fn fmt(&self, format: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        format.write_str(&self.reason)
    }
}

impl std::error::Error for Rejected {}

pub fn stop_reason(error: &anyhow::Error) -> Option<Stop> {
    error
        .downcast_ref::<Rejected>()
        .and_then(|rejected| rejected.stop)
}

#[derive(Debug)]
pub struct SlowRetry(&'static str);

impl std::fmt::Display for SlowRetry {
    fn fmt(&self, format: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        format.write_str(self.0)
    }
}

pub fn status_error(error: &anyhow::Error) -> String {
    fn valid_version(value: &str) -> bool {
        !value.is_empty()
            && value.len() <= 24
            && value
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b".+-_".contains(&b))
    }

    if let Some(rejected) = error.downcast_ref::<Rejected>() {
        let reason = rejected.reason.as_str();
        return match rejected.stop {
            Some(Stop::Revoked | Stop::Unregistered) => reason,
            Some(Stop::Version) => {
                let valid = reason
                    .split_once(": child ")
                    .and_then(|(_, versions)| versions.split_once(" != host "))
                    .is_some_and(|(child, host)| valid_version(child) && valid_version(host));
                if valid {
                    reason
                } else {
                    "host and child version mismatch"
                }
            }
            None => match reason {
                "child name is invalid"
                | "child name already enrolled"
                | "child certificate fingerprint mismatch"
                | "enrollment is incomplete"
                | "enrollment code fingerprint mismatch"
                | "enrollment code rejected"
                | "enrollment code expired"
                | "enrollment code attempts exhausted" => reason,
                _ => "host rejected the connection",
            },
        }
        .to_owned();
    }
    for source in error.chain() {
        let reason = source.to_string();
        match reason.as_str() {
            "invalid app.json"
            | "cannot read app.json"
            | "cannot read policy.json"
            | "invalid policy.json"
            | "invalid policy.json fields"
            | "cannot read cert.pem"
            | "cannot read key.pem"
            | "child is not enrolled or host.json is invalid"
            | "invalid pending enrollment"
            | "pending enrollment could not be confirmed"
            | "enrollment not confirmed; recovering the staged certificate"
            | "host heartbeat timed out"
            | "disconnected from host"
            | "host connection timed out"
            | "host response timed out"
            | "host socket write timed out" => return reason,
            _ => {}
        }
        if reason
            .strip_prefix("invalid host frame envelope (total: ")
            .and_then(|count| count.strip_suffix(')'))
            .is_some_and(|count| count.parse::<u64>().is_ok())
        {
            return reason;
        }
    }
    error.downcast_ref::<SlowRetry>().map_or_else(
        || "host communication failed".into(),
        |retry| retry.0.to_owned(),
    )
}

pub fn invalid_state(error: anyhow::Error) -> anyhow::Error {
    error.context(SlowRetry("cannot use local child state"))
}

pub fn reject(frame: &Frame) -> anyhow::Error {
    let payload = frame.payload().unwrap_or_default();
    let Some(raw_reason) = payload["reason"]
        .as_str()
        .map(str::trim)
        .filter(|s| !s.is_empty())
    else {
        return anyhow!("host reject frame has no reason");
    };
    let stop = match raw_reason {
        "child revoked" | "certificate revoked" => Some(Stop::Revoked),
        "unknown child"
        | "not enrolled"
        | "child is not enrolled"
        | "unknown certificate"
        | "unregistered certificate" => Some(Stop::Unregistered),
        "version mismatch" | "protocol mismatch" => Some(Stop::Version),
        reason
            if [
                "package version mismatch:",
                "protocol mismatch:",
                "version mismatch:",
            ]
            .iter()
            .any(|prefix| reason.starts_with(prefix)) =>
        {
            Some(Stop::Version)
        }
        _ => None,
    };
    let reason = raw_reason
        .chars()
        .filter(|c| !c.is_control())
        .take(200)
        .collect();
    anyhow::Error::new(Rejected { reason, stop })
}

#[derive(Debug)]
struct CheckedClock;

impl TimeProvider for CheckedClock {
    fn current_time(&self) -> Option<UnixTime> {
        unix_time(SystemTime::now())
    }
}

fn unix_time(now: SystemTime) -> Option<UnixTime> {
    now.duration_since(SystemTime::UNIX_EPOCH)
        .ok()
        .map(UnixTime::since_unix_epoch)
}

#[derive(Debug)]
struct LeafPin {
    fingerprint: String,
    algorithms: WebPkiSupportedAlgorithms,
}

impl ServerCertVerifier for LeafPin {
    fn verify_server_cert(
        &self,
        leaf: &CertificateDer<'_>,
        _chain: &[CertificateDer<'_>],
        _name: &ServerName<'_>,
        _ocsp: &[u8],
        _now: UnixTime,
    ) -> Result<ServerCertVerified, rustls::Error> {
        if fingerprint(leaf) != self.fingerprint {
            return Err(CertificateError::ApplicationVerificationFailure.into());
        }
        Ok(ServerCertVerified::assertion())
    }

    fn verify_tls12_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls12_signature(message, cert, signature, &self.algorithms)
    }

    fn verify_tls13_signature(
        &self,
        message: &[u8],
        cert: &CertificateDer<'_>,
        signature: &DigitallySignedStruct,
    ) -> Result<HandshakeSignatureValid, rustls::Error> {
        verify_tls13_signature(message, cert, signature, &self.algorithms)
    }

    fn supported_verify_schemes(&self) -> Vec<SignatureScheme> {
        self.algorithms.supported_schemes()
    }
}

pub fn fingerprint(der: &[u8]) -> String {
    use std::fmt::Write;
    let mut hex = String::with_capacity(64);
    for byte in ring::digest::digest(&ring::digest::SHA256, der).as_ref() {
        write!(&mut hex, "{byte:02x}").expect("write to String");
    }
    hex
}

fn peer_pem(der: &[u8]) -> String {
    let mut pem = String::from(concat!("-----BEGIN ", "CERTIFICATE-----\n"));
    for line in STANDARD.encode(der).as_bytes().chunks(64) {
        pem.push_str(std::str::from_utf8(line).expect("base64 is ASCII"));
        pem.push('\n');
    }
    pem.push_str("-----END CERTIFICATE-----\n");
    pem
}

pub struct Identity {
    cert: CertificateDer<'static>,
    key: PrivateKeyDer<'static>,
    not_before: i64,
    not_after: i64,
}

impl Identity {
    pub fn parse(cert: &[u8], key: &[u8]) -> Result<Self> {
        let cert = CertificateDer::from_pem_slice(cert).context("invalid cert.pem")?;
        let (_, parsed) = X509Certificate::from_der(&cert)
            .map_err(|_| anyhow!("invalid client certificate DER"))?;
        let not_before = parsed.validity().not_before.timestamp();
        let not_after = parsed.validity().not_after.timestamp();
        Ok(Self {
            cert,
            key: PrivateKeyDer::from_pem_slice(key).context("invalid key.pem")?,
            not_before,
            not_after,
        })
    }

    pub fn load(home: &Path) -> Result<Self> {
        let cert = std::fs::read(home.join("cert.pem")).context("cannot read cert.pem")?;
        let key = std::fs::read(home.join("key.pem")).context("cannot read key.pem")?;
        Self::parse(&cert, &key)
    }

    fn validity_hint(&self, now: i64) -> Option<&'static str> {
        if now < self.not_before {
            Some("client certificate is not yet valid; check the child and host clocks")
        } else if now > self.not_after {
            Some("client certificate has expired; check the clocks and re-enroll if needed")
        } else {
            None
        }
    }
}

pub fn address(value: &str) -> Result<(&str, u16)> {
    let value = value.trim();
    let (host, port) = if let Some(ip) = value.strip_prefix('[') {
        ip.split_once("]:")
    } else {
        value
            .rsplit_once(':')
            .filter(|(host, _)| !host.contains(':'))
    }
    .ok_or_else(|| anyhow!("address must be host:port"))?;
    let port = port.parse::<u16>().context("invalid host port")?;
    ensure!(port != 0 && !host.is_empty(), "address must be host:port");
    Ok((host, port))
}

fn tls_error(error: io::Error) -> anyhow::Error {
    let tls = error
        .get_ref()
        .and_then(|e| e.downcast_ref::<rustls::Error>());
    let detail = match tls {
        Some(rustls::Error::InvalidCertificate(
            CertificateError::ApplicationVerificationFailure,
        )) => "host certificate fingerprint mismatch",
        Some(rustls::Error::InvalidCertificate(_)) => "host certificate validation failed",
        Some(rustls::Error::FailedToGetCurrentTime) => {
            "TLS could not read the current time; check the child clock"
        }
        Some(rustls::Error::AlertReceived(_)) => {
            "TLS alert from peer; check the client certificate expiry and the child and host clocks"
        }
        Some(_) => "TLS negotiation failed",
        None => return error.into(),
    };
    anyhow::Error::new(error).context(SlowRetry(detail))
}

pub fn ws_error(error: WsError) -> anyhow::Error {
    match error {
        WsError::Io(error) => tls_error(error),
        _ => error.into(),
    }
}

static INVALID_HOST_FRAMES: AtomicU64 = AtomicU64::new(0);

pub fn parse_host_text(body: &str) -> Result<Frame> {
    Frame::parse(body.to_owned()).map_err(|_| {
        let count = INVALID_HOST_FRAMES.fetch_add(1, Ordering::Relaxed) + 1;
        tracing::debug!(count, "invalid host text frame");
        anyhow!("invalid host frame envelope (total: {count})")
    })
}

pub async fn connect(
    address_text: &str,
    pin: &str,
    identity: Option<&Identity>,
) -> Result<(Socket, String)> {
    let (host, port) = address(address_text).map_err(invalid_state)?;
    let name = ServerName::try_from(host.to_owned())
        .context("invalid host address")
        .map_err(invalid_state)?;
    let provider = rustls::crypto::ring::default_provider();
    let verifier = LeafPin {
        fingerprint: pin.to_owned(),
        algorithms: provider.signature_verification_algorithms,
    };
    let config = ClientConfig::builder_with_details(Arc::new(provider), Arc::new(CheckedClock))
        .with_safe_default_protocol_versions()?
        .dangerous()
        .with_custom_certificate_verifier(Arc::new(verifier));
    let config = match identity {
        Some(identity) => config
            .with_client_auth_cert(vec![identity.cert.clone()], identity.key.clone_key())
            .context("invalid client certificate or key")
            .map_err(invalid_state)?,
        None => config.with_no_client_auth(),
    };
    timeout(OPEN_TIMEOUT, async {
        let tcp = TcpStream::connect((host, port)).await?;
        tcp.set_nodelay(true)?;
        let tls = TlsConnector::from(Arc::new(config))
            .connect(name, tcp)
            .await
            .map_err(tls_error)
            .context("host TLS handshake failed")?;
        let leaf = tls
            .get_ref()
            .1
            .peer_certificates()
            .and_then(|certs| certs.first())
            .ok_or_else(|| anyhow!("host did not present a certificate"))?;
        let pem = peer_pem(leaf);
        let settings = WebSocketConfig::default()
            .read_buffer_size(4096)
            .write_buffer_size(4096)
            .max_write_buffer_size(MAX_SKILL_TURN_RECORD + 32768)
            .max_message_size(Some(MAX_SKILL_TURN_RECORD))
            .max_frame_size(Some(MAX_SKILL_TURN_RECORD));
        let (ws, _) = client_async_with_config(
            format!("wss://{}/", address_text.trim()),
            tls,
            Some(settings),
        )
        .await
        .map_err(ws_error)
        .context("host WebSocket upgrade failed")?;
        Ok((ws, pem))
    })
    .await
    .context("host connection timed out")?
}

pub async fn request(ws: &mut Socket, frame: Frame, want: &str) -> Result<Frame> {
    timeout(
        write_timeout(frame.body.len()),
        ws.send(Message::Text(frame.body.into())),
    )
    .await
    .context("host write timed out")?
    .map_err(ws_error)?;
    timeout(RESPONSE_TIMEOUT, async {
        while let Some(message) = ws.next().await {
            if let Message::Text(body) = message.map_err(ws_error)? {
                let frame = parse_host_text(&body)?;
                ensure!(frame.envelope.stream == 0, "unexpected host stream");
                match frame.envelope.kind.as_str() {
                    "heartbeat" => continue,
                    "reject" => return Err(reject(&frame)),
                    kind if kind == want => return Ok(frame),
                    _ => bail!("unexpected host response"),
                }
            }
        }
        bail!("connection closed before host response")
    })
    .await
    .context("host response timed out")?
}

// Keep the optional report within the host's workspace-policy/1 limits.
fn workspace_policy_report(catalog: &state::Catalog) -> Option<serde_json::Value> {
    const MAX_ROOTS: usize = 64;
    const MAX_ROOT_BYTES: usize = 4096;
    const MAX_TOTAL_ROOT_BYTES: usize = 16 * 1024;
    if catalog.workspace_roots.len() > MAX_ROOTS {
        return None;
    }
    let mut total = 0;
    for root in &catalog.workspace_roots {
        let root = root.to_str()?;
        total += root.len();
        if root.len() > MAX_ROOT_BYTES
            || total > MAX_TOTAL_ROOT_BYTES
            || root.chars().any(|ch| ch.is_ascii_control())
        {
            return None;
        }
    }
    Some(json!({
        "workspace_roots": catalog.workspace_roots,
        "codex_sandbox_max": catalog.codex_sandbox_max,
    }))
}

/// Features this link app advertises for a platform key. MCP turns for data providers rely on
/// Grok's Landlock sandbox (Linux x86_64 / aarch64, matching `provider_data_runtime.LANDLOCK_PLATFORMS`;
/// the runner still checks Landlock per turn). Windows has no Grok sandbox, so it offers only the
/// explicitly approved unrestricted mode.
fn hello_features(platform: &str, has_runner: bool, app_update: bool) -> Vec<&'static str> {
    let mut features = vec![
        "codex-turns",
        "login-status",
        "provider-data/1",
        "workspace-policy/1",
        "prompt-prefix-skills/1",
    ];
    if has_runner {
        features.push("session-collect/1");
    }
    if app_update {
        features.push(crate::update::FEATURE);
    }
    if has_runner && matches!(platform, "linux-x64" | "linux-arm64" | "windows-x64") {
        features.extend(["skill-folders/1", "repository-skills/1"]);
    }
    if has_runner {
        features.push("local-llm/1");
    }
    match platform {
        "windows-x64" => features.push("provider-data-unrestricted/1"),
        "linux-x64" | "linux-arm64" => features.push("provider-data-mcp/1"),
        _ => {}
    }
    // Claude and Codex get their skills natively (original SKILL.md and folder) on Windows too;
    // the legacy text form lacks frontmatter, so Claude did not register the skill there.
    if platform.starts_with("linux-") || platform == "windows-x64" {
        features.push("sdk-skills/1");
    }
    if platform.starts_with("linux-") && has_runner {
        features.push("host-http-mcp/1");
    }
    features
}

/// Build a hello from the current local policy, including its bounded workspace report.
pub async fn hello_frame(
    home: &Path,
    python: Option<&Path>,
    host_fingerprint: &str,
) -> Result<Frame> {
    let catalog = state::catalog(home, python).await?;
    let platform = crate::declarations::platform_key();
    let features = hello_features(&platform, python.is_some(), crate::update::can_update());
    let mut body = json!({
        "protocol":2, "version":VERSION, "adapters":catalog.adapters, "mcp":catalog.mcp,
        "features":features, "platform":platform, "build_id":crate::update::build_id(),
        "quota_providers":catalog.quota_providers,
    });
    if features.contains(&"host-http-mcp/1") {
        body["mcp_approvals"] = json!(crate::host_mcp::approved(home, host_fingerprint));
    }
    if !catalog.providers.is_empty() {
        body["providers"] = json!(catalog.providers);
    }
    if let Some(report) = workspace_policy_report(&catalog) {
        body["workspace_policy"] = report;
    }
    if features.contains(&"local-llm/1") {
        body["local_llm"] = json!(catalog.local_llm);
    }
    if features.contains(&"session-collect/1") {
        body["session_import_sources"] = json!(catalog.session_import_sources);
        body["session_import_origin_hint"] = json!(catalog.session_import_origin_hint);
    }
    Frame::try_new(0, "hello", body)
}

pub async fn hello(host: &Host, identity: Identity, hello: Frame) -> Result<(Socket, Duration)> {
    let hint = identity.validity_hint(time::OffsetDateTime::now_utc().unix_timestamp());
    let (mut ws, _) = connect(&host.address, &host.fingerprint, Some(&identity))
        .await
        .map_err(|error| match hint {
            Some(hint) => error.context(SlowRetry(hint)),
            None => error,
        })?;
    if let Some(hint) = hint {
        tracing::warn!("{hint}");
    }
    let response = request(&mut ws, hello, "hello_ok").await?;
    let payload = response.payload()?;
    ensure!(
        payload["generation"].as_u64().is_some(),
        "invalid host generation"
    );
    let interval = payload["heartbeat_interval"]
        .as_f64()
        .and_then(|seconds| Duration::try_from_secs_f64(seconds).ok())
        .filter(|interval| {
            !interval.is_zero()
                && interval
                    .checked_mul(3)
                    .and_then(|deadline| tokio::time::Instant::now().checked_add(deadline))
                    .is_some()
        })
        .ok_or_else(|| anyhow!("invalid host heartbeat interval"))?;
    Ok((ws, interval))
}

#[cfg(test)]
mod tests {
    #[test]
    fn python_and_rust_advertise_the_same_features_with_a_runner() {
        let Ok(python) = std::env::var("KOTOYOSE_CHILD_PYTHON") else {
            return;
        };
        for (platform, app_update) in ["linux-x64", "linux-arm64", "windows-x64", "macos-arm64"]
            .into_iter()
            .flat_map(|platform| [(platform, false), (platform, true)])
        {
            let output = std::process::Command::new(&python)
                .args(["-B", "-c", "import json, sys; from kotoyose.child_client import _hello_features; print(json.dumps(_hello_features(sys.argv[1], sys.argv[2] == 'true')))", platform, if app_update { "true" } else { "false" }])
                .env("PYTHONPATH", "../src")
                .env("PYTHON_DOTENV_DISABLED", "1")
                .output()
                .unwrap();
            assert!(
                output.status.success(),
                "{}",
                String::from_utf8_lossy(&output.stderr)
            );
            let theirs: Vec<String> = serde_json::from_slice(&output.stdout).unwrap();
            let ours = super::hello_features(platform, true, app_update);
            assert_eq!(ours, theirs, "{platform}");
        }
    }

    #[test]
    fn hello_features_match_the_runner_platforms() {
        for platform in ["linux-x64", "linux-arm64", "windows-x64", "macos-arm64"] {
            let features = super::hello_features(platform, true, true);
            assert!(features.contains(&"provider-data/1"));
            assert!(features.contains(&"prompt-prefix-skills/1"));
            for feature in ["skill-folders/1", "repository-skills/1"] {
                assert_eq!(
                    features.contains(&feature),
                    matches!(platform, "linux-x64" | "linux-arm64" | "windows-x64")
                );
                assert!(!super::hello_features(platform, false, false).contains(&feature));
            }
            assert_eq!(
                features.contains(&"sdk-skills/1"),
                platform.starts_with("linux-") || platform == "windows-x64"
            );
            assert_eq!(
                features.contains(&"provider-data-mcp/1"),
                platform.starts_with("linux-")
            );
            assert_eq!(
                features.contains(&"provider-data-unrestricted/1"),
                platform == "windows-x64"
            );
        }
    }

    use super::*;
    use rustls::internal::msgs::codec::{Codec, Reader};

    #[test]
    fn tls_errors_are_never_a_registration_decision() {
        let cases = [
            (
                CertificateError::ApplicationVerificationFailure.into(),
                "fingerprint mismatch",
            ),
            (CertificateError::NotValidYet.into(), "validation failed"),
            (CertificateError::Expired.into(), "validation failed"),
            (CertificateError::BadEncoding.into(), "validation failed"),
            (
                rustls::Error::AlertReceived(rustls::AlertDescription::UnknownCA),
                "check the client certificate expiry",
            ),
            (
                rustls::Error::AlertReceived(rustls::AlertDescription::CertificateRevoked),
                "check the client certificate expiry",
            ),
            (
                rustls::Error::FailedToGetCurrentTime,
                "check the child clock",
            ),
        ];
        for (source, detail) in cases {
            let error = tls_error(io::Error::new(io::ErrorKind::InvalidData, source));
            assert_eq!(stop_reason(&error), None);
            assert!(error.is::<SlowRetry>());
            assert!(error.to_string().contains(detail), "{error:#}");
        }
    }

    #[test]
    fn only_explicit_identity_rejections_are_terminal() {
        let cases = [
            (
                "package version mismatch: child 1 != host 2",
                Some(Stop::Version),
            ),
            ("protocol mismatch: child 1 != host 2", Some(Stop::Version)),
            ("child revoked", Some(Stop::Revoked)),
            ("unknown child", Some(Stop::Unregistered)),
            ("client certificate required", None),
            ("service busy", None),
            ("", None),
        ];
        for (reason, expected) in cases {
            let error = reject(&Frame::new(0, "reject", json!({"reason": reason})));
            assert_eq!(stop_reason(&error), expected);
        }
    }

    #[test]
    fn clock_before_the_unix_epoch_is_a_tls_time_error() {
        assert!(unix_time(SystemTime::UNIX_EPOCH - Duration::from_secs(1)).is_none());
        assert_eq!(unix_time(SystemTime::UNIX_EPOCH).unwrap().as_secs(), 0);
    }

    #[test]
    fn client_certificate_validity_is_advisory() {
        let key = rcgen::KeyPair::generate().unwrap();
        let mut params = rcgen::CertificateParams::default();
        params.not_before = time::OffsetDateTime::from_unix_timestamp(100).unwrap();
        params.not_after = time::OffsetDateTime::from_unix_timestamp(200).unwrap();
        let cert = params.self_signed(&key).unwrap();
        let identity =
            Identity::parse(cert.pem().as_bytes(), key.serialize_pem().as_bytes()).unwrap();
        assert!(
            identity
                .validity_hint(99)
                .unwrap()
                .contains("not yet valid")
        );
        assert_eq!(identity.validity_hint(100), None);
        assert_eq!(identity.validity_hint(200), None);
        assert!(identity.validity_hint(201).unwrap().contains("expired"));
    }

    #[test]
    fn pin_checks_leaf_bytes_independently_of_the_clock() {
        let key = rcgen::KeyPair::generate().unwrap();
        let mut params = rcgen::CertificateParams::new(vec!["localhost".to_owned()]).unwrap();
        params.not_before = time::OffsetDateTime::from_unix_timestamp(100).unwrap();
        params.not_after = time::OffsetDateTime::from_unix_timestamp(200).unwrap();
        let cert = params.self_signed(&key).unwrap();
        let different = params
            .self_signed(&rcgen::KeyPair::generate().unwrap())
            .unwrap();
        let verifier = LeafPin {
            fingerprint: fingerprint(cert.der()),
            algorithms: rustls::crypto::ring::default_provider().signature_verification_algorithms,
        };
        let name = ServerName::try_from("different.example").unwrap();
        let check = |leaf, seconds| {
            verifier.verify_server_cert(
                leaf,
                &[],
                &name,
                &[],
                UnixTime::since_unix_epoch(Duration::from_secs(seconds)),
            )
        };
        assert!(check(cert.der(), 150).is_ok());
        assert!(matches!(
            check(different.der(), 150),
            Err(rustls::Error::InvalidCertificate(
                CertificateError::ApplicationVerificationFailure
            ))
        ));
        assert!(check(cert.der(), 99).is_ok());
        assert!(check(cert.der(), 201).is_ok());

        // ECDSA P-256, one byte of invalid signature. A matching fingerprint
        // alone never makes TLS CertificateVerify succeed.
        let mut wire_signature = Reader::init(&[0x04, 0x03, 0, 1, 0]);
        let invalid = DigitallySignedStruct::read(&mut wire_signature).unwrap();
        assert!(
            verifier
                .verify_tls12_signature(b"handshake", cert.der(), &invalid)
                .is_err()
        );
        assert!(
            verifier
                .verify_tls13_signature(b"handshake", cert.der(), &invalid)
                .is_err()
        );
    }
}
