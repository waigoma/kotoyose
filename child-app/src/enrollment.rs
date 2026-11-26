use std::{fs, io, path::Path};

use anyhow::{Context, Result, anyhow, bail, ensure};
use base64::{Engine, engine::general_purpose::URL_SAFE_NO_PAD};
use rcgen::{CertificateParams, DnType, KeyPair};
use ring::rand::{SecureRandom, SystemRandom};
use serde::{Deserialize, Serialize};
use serde_json::json;
use tokio::sync::watch;

use crate::{
    Backoff,
    frame::Frame,
    link::{self, Identity, Rejected, Stop},
    state::{self, Host},
    until_stopped,
};

const PENDING: &str = "enrollment.pending.json";

#[derive(Deserialize)]
struct Claims {
    host: String,
    fp: String,
}

fn claims(code: &str) -> Result<Claims> {
    let mut pieces = code.split('.');
    ensure!(pieces.next() == Some("kyc1"), "invalid enrollment code");
    let (Some(body), Some(signature), None) = (pieces.next(), pieces.next(), pieces.next()) else {
        bail!("invalid enrollment code");
    };
    ensure!(
        !body.is_empty() && !signature.is_empty(),
        "invalid enrollment code"
    );
    URL_SAFE_NO_PAD
        .decode(signature)
        .context("invalid enrollment code")?;
    let claims: Claims = serde_json::from_slice(
        &URL_SAFE_NO_PAD
            .decode(body)
            .context("invalid enrollment code")?,
    )
    .map_err(|_| anyhow!("invalid enrollment code"))?;
    ensure!(
        claims.fp.len() == 64
            && claims
                .fp
                .bytes()
                .all(|b| b.is_ascii_hexdigit() && !b.is_ascii_uppercase()),
        "invalid enrollment fingerprint"
    );
    link::address(&claims.host)?;
    Ok(claims)
}

#[derive(Serialize, Deserialize)]
struct Pending {
    host: Host,
    cert: String,
    key: String,
    accepted: bool,
}

impl Pending {
    fn save(&self, home: &Path) -> Result<()> {
        state::write_secret(&home.join(PENDING), &serde_json::to_vec(self)?)
    }

    fn commit(&mut self, home: &Path) -> Result<()> {
        self.accepted = true;
        self.save(home)?;
        state::write_secret(&home.join("key.pem"), self.key.as_bytes())?;
        state::write_private(&home.join("cert.pem"), self.cert.as_bytes())?;
        state::write_private(
            &home.join("host.json"),
            &serde_json::to_vec_pretty(&self.host)?,
        )?;
        discard(home)
    }
}

fn discard(home: &Path) -> Result<()> {
    match fs::remove_file(home.join(PENDING)) {
        Ok(()) => Ok(()),
        Err(error) if error.kind() == io::ErrorKind::NotFound => Ok(()),
        Err(error) => {
            Err(link::invalid_state(error.into()).context("cannot remove pending enrollment"))
        }
    }
}

pub fn needed(home: &Path) -> bool {
    matches!(home.join("host.json").try_exists(), Ok(false)) && !is_pending(home)
}

pub fn is_pending(home: &Path) -> bool {
    home.join(PENDING).is_file()
}

/// Returns the recovered name when an interrupted enrollment has been committed.
pub async fn recover(home: &Path, python: Option<&Path>) -> Result<Option<String>> {
    let path = home.join(PENDING);
    let bytes = match fs::read(&path) {
        Ok(bytes) => bytes,
        Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(None),
        Err(error) => return Err(link::invalid_state(error.into())),
    };
    let mut pending: Pending = serde_json::from_slice(&bytes)
        .context("invalid pending enrollment")
        .map_err(link::invalid_state)?;
    if !pending.accepted {
        let identity = Identity::parse(pending.cert.as_bytes(), pending.key.as_bytes())
            .map_err(link::invalid_state)?;
        let hello = link::hello_frame(home, python, &pending.host.fingerprint)
            .await
            .map_err(link::invalid_state)?;
        match link::hello(&pending.host, identity, hello).await {
            Ok(_) => {}
            Err(error)
                if matches!(
                    link::stop_reason(&error),
                    Some(Stop::Unregistered | Stop::Revoked)
                ) =>
            {
                discard(home)?;
                return Err(
                    error.context("pending enrollment was refused; obtain a new enrollment code")
                );
            }
            Err(error) => return Err(error.context("pending enrollment could not be confirmed")),
        }
    }
    pending.commit(home).map_err(link::invalid_state)?;
    Ok(Some(pending.host.name))
}

pub async fn enroll(
    home: &Path,
    name: &str,
    code: &str,
    stopping: &mut watch::Receiver<bool>,
) -> Result<Option<String>> {
    let mut backoff = Backoff::new();
    loop {
        match until_stopped(stopping, attempt(home, name, code)).await {
            None => return Ok(None),
            Some(Ok(name)) => return Ok(Some(name)),
            Some(Err((error, false))) => return Err(error),
            Some(Err((error, true))) => {
                if !backoff.wait(&error, stopping).await {
                    return Ok(None);
                }
            }
        }
    }
}

// The second error field indicates whether a CLI caller should retry.
pub async fn attempt(home: &Path, name: &str, code: &str) -> Result<String, (anyhow::Error, bool)> {
    let python = state::python().ok();
    match recover(home, python.as_deref()).await {
        Ok(Some(name)) => return Ok(name),
        Ok(None) => {}
        Err(error) => {
            let retry = link::stop_reason(&error).is_none();
            return Err((error, retry));
        }
    }
    let claims = enrollment_claims(name, code).map_err(|error| (error, false))?;
    start(home, name, code, claims).await.map_err(|error| {
        let retry = !error.is::<Rejected>();
        (error, retry)
    })?;
    Ok(name.to_owned())
}

fn enrollment_claims(name: &str, code: &str) -> Result<Claims> {
    ensure!(
        name.len() <= 64
            && name
                .as_bytes()
                .first()
                .is_some_and(u8::is_ascii_alphanumeric)
            && name
                .bytes()
                .all(|b| b.is_ascii_alphanumeric() || b"_.-".contains(&b)),
        "child name is invalid"
    );
    claims(code)
}

async fn start(home: &Path, name: &str, code: &str, claims: Claims) -> Result<()> {
    let mut nonce = [0; 12];
    SystemRandom::new()
        .fill(&mut nonce)
        .map_err(|_| anyhow!("cannot generate certificate name"))?;
    let common_name = format!("kotoyose-child-{}", URL_SAFE_NO_PAD.encode(nonce));
    let now = time::OffsetDateTime::now_utc();
    let mut params = CertificateParams::new(vec!["localhost".into(), "127.0.0.1".into()])?;
    params
        .distinguished_name
        .push(DnType::CommonName, common_name);
    params.not_before = now - time::Duration::minutes(5);
    params.not_after = now + time::Duration::days(825);
    let key = KeyPair::generate()?;
    let cert = params.self_signed(&key)?;
    let frame = Frame::try_new(
        0,
        "enroll",
        json!({
            "code":code, "name":name, "cert_pem":cert.pem(), "fingerprint":link::fingerprint(cert.der()),
        }),
    )?;
    state::ensure_policy(home).map_err(link::invalid_state)?;
    let (mut ws, host_cert_pem) = link::connect(&claims.host, &claims.fp, None).await?;
    let mut pending = Pending {
        host: Host {
            address: claims.host,
            fingerprint: claims.fp,
            name: name.to_owned(),
            host_cert_pem,
        },
        cert: cert.pem(),
        key: key.serialize_pem(),
        accepted: false,
    };
    pending.save(home).map_err(link::invalid_state)?;
    match link::request(&mut ws, frame, "enroll_ok").await {
        Ok(response) => {
            let payload = response.payload()?;
            ensure!(
                payload["name"].as_str() == Some(name) && payload["generation"].as_u64().is_some(),
                "invalid enrollment response"
            );
            pending.commit(home).map_err(link::invalid_state)
        }
        Err(error) if error.downcast_ref::<Rejected>().is_some() => {
            discard(home)?;
            Err(error)
        }
        Err(error) => {
            Err(error.context("enrollment not confirmed; recovering the staged certificate"))
        }
    }
}
