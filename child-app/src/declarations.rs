//! Offered and approved provider declarations stored under the child home.
//!
//! The approval file matches the Python store: original document text, host
//! fingerprint, content hash, OS, capability, and approval time.

use std::{
    collections::HashMap,
    io::Write,
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::Mutex,
    time::Duration,
};

use anyhow::{Result, bail};
#[cfg(all(test, unix))]
use ring::digest::{SHA256, digest};
use serde_json::{Value, json};

const MAX_OFFERS: usize = 16;
const MAX_DOCUMENT: usize = 16 * 1024;
const MAX_CATALOG: usize = 96 * 1024;

fn store_errors() -> &'static Mutex<HashMap<PathBuf, String>> {
    static ERRORS: std::sync::LazyLock<Mutex<HashMap<PathBuf, String>>> =
        std::sync::LazyLock::new(|| Mutex::new(HashMap::new()));
    &ERRORS
}

pub(crate) fn store_failure_text(error: &str) -> String {
    let mut line = error.lines().next().unwrap_or("").trim().to_owned();
    if line.chars().count() > 300 {
        line = line.chars().take(300).collect();
        line.push('…');
    }
    line
}

pub(crate) fn note_store_failure(home: &Path, error: &str) {
    let line = store_failure_text(error);
    if line.is_empty() {
        return;
    }
    store_errors()
        .lock()
        .expect("catalog errors")
        .insert(home.to_path_buf(), line);
}

fn clear_store_failure(home: &Path) {
    store_errors().lock().expect("catalog errors").remove(home);
}

fn root(home: &Path) -> PathBuf {
    home.join("provider-declarations")
}

pub(crate) fn platform_key() -> String {
    let arch = match std::env::consts::ARCH {
        "x86_64" => "x64",
        "aarch64" => "arm64",
        other => other,
    };
    format!("{}-{arch}", std::env::consts::OS)
}

fn identifier(value: &str) -> bool {
    let mut chars = value.chars();
    matches!(chars.next(), Some(char) if char.is_ascii_lowercase())
        && value.len() <= 48
        && value
            .chars()
            .all(|char| char.is_ascii_lowercase() || char.is_ascii_digit() || char == '-')
}

#[cfg(all(test, unix))]
fn sha256_hex(bytes: &[u8]) -> String {
    digest(&SHA256, bytes)
        .as_ref()
        .iter()
        .map(|byte| format!("{byte:02x}"))
        .collect()
}

fn python_program() -> Result<PathBuf> {
    if let Some(path) = std::env::var_os("KOTOYOSE_CHILD_PYTHON") {
        let path = PathBuf::from(path);
        if !path.is_absolute() {
            bail!("KOTOYOSE_CHILD_PYTHON must name an absolute Python executable");
        }
        return Ok(path);
    }
    if let Some(root) = std::env::var_os("VIRTUAL_ENV") {
        let candidate = PathBuf::from(root).join("bin").join("python3");
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    #[cfg(debug_assertions)]
    {
        let candidate = PathBuf::from(env!("CARGO_MANIFEST_DIR")).join("../.venv/bin/python3");
        if candidate.is_file() {
            return Ok(candidate);
        }
    }
    crate::state::python()
}

fn validator(command: &mut Command) -> &mut Command {
    command.env_clear();
    for key in [
        "PATH",
        "HOME",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "SYSTEMROOT",
        "TEMP",
        "TMP",
    ] {
        if let Some(value) = std::env::var_os(key) {
            command.env(key, value);
        }
    }
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        use windows_sys::Win32::System::Threading::CREATE_NO_WINDOW;

        command.creation_flags(CREATE_NO_WINDOW);
    }
    command
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .env("PYTHON_DOTENV_DISABLED", "1")
        .args(["-I", "-m", "kotoyose.provider_data_child", "validate"])
}

fn validation_result(output: std::process::Output) -> Result<Value> {
    if !output.status.success() {
        let message = String::from_utf8_lossy(&output.stderr);
        let message = message.trim();
        bail!(if message.is_empty() {
            "invalid declaration".to_owned()
        } else {
            message.to_owned()
        });
    }
    serde_json::from_slice(&output.stdout).map_err(|_| anyhow::anyhow!("invalid declaration"))
}

fn python_validate(payload: &Value) -> Result<Value> {
    let mut child = validator(&mut Command::new(python_program()?))
        .spawn()
        .map_err(|_| anyhow::anyhow!("declaration validator is unavailable"))?;
    child
        .stdin
        .take()
        .ok_or_else(|| anyhow::anyhow!("declaration validator is unavailable"))?
        .write_all(&serde_json::to_vec(payload)?)
        .map_err(|_| anyhow::anyhow!("declaration validator is unavailable"))?;
    let output = child
        .wait_with_output()
        .map_err(|_| anyhow::anyhow!("declaration validator is unavailable"))?;
    validation_result(output)
}

pub fn store_catalog(home: &Path, payload: &Value) -> Result<()> {
    let result = store_catalog_validated(home, payload);
    match &result {
        Ok(()) => clear_store_failure(home),
        Err(err) => note_store_failure(home, &err.to_string()),
    }
    result
}

fn store_catalog_validated(home: &Path, payload: &Value) -> Result<()> {
    let offers = payload["offers"]
        .as_array()
        .ok_or_else(|| anyhow::anyhow!("invalid provider catalog"))?;
    if offers.len() > MAX_OFFERS {
        bail!("invalid provider catalog")
    }
    let mut total = 0;
    for offer in offers {
        let document = offer["document"].as_str().unwrap_or("");
        let bytes = document.as_bytes();
        if bytes.is_empty() || bytes.len() > MAX_DOCUMENT {
            bail!("invalid provider catalog");
        }
        total += bytes.len();
    }
    if total > MAX_CATALOG {
        bail!("invalid provider catalog");
    }
    python_validate(&json!({
        "action": "store_offers",
        "home": home.to_string_lossy(),
        "offers": offers,
    }))?;
    Ok(())
}

pub fn list(home: &Path) -> Value {
    let mut value =
        match python_validate(&json!({"action": "preview", "home": home.to_string_lossy()})) {
            Ok(value) => value,
            Err(err) => json!({"offers": [], "error": err.to_string()}),
        };
    if value.get("error").is_none()
        && let Some(error) = store_errors()
            .lock()
            .expect("catalog errors")
            .get(home)
            .cloned()
    {
        value["error"] = json!(error);
    }
    value
}

pub fn approve(
    home: &Path,
    id: &str,
    sha256: &str,
    capability: &str,
    confirm_unrestricted: bool,
) -> Result<Value> {
    python_validate(&json!({
        "action": "approve",
        "home": home.to_string_lossy(),
        "id": id,
        "sha256": sha256,
        "capability": capability,
        "confirm_unrestricted": confirm_unrestricted,
    }))
}

pub fn revoke(home: &Path, id: &str) -> Result<Value> {
    python_validate(&json!({"action": "revoke", "home": home.to_string_lossy(), "id": id}))
}

pub(crate) fn has_approval_file(home: &Path, id: &str) -> bool {
    identifier(id)
        && root(home)
            .join("approved")
            .join(format!("{id}.json"))
            .is_file()
}

pub async fn install(home: &Path, id: &str, sha256: &str) -> Result<Value> {
    use tokio::io::AsyncWriteExt;

    let mut command = Command::new(python_program()?);
    validator(&mut command);
    let mut child = tokio::process::Command::from(command)
        .kill_on_drop(true)
        .spawn()?;
    let request = serde_json::to_vec(&json!({
        "action": "install", "home": home.to_string_lossy(), "id": id, "sha256": sha256,
    }))?;
    let output = tokio::time::timeout(Duration::from_secs(90), async move {
        child
            .stdin
            .take()
            .expect("piped stdin")
            .write_all(&request)
            .await?;
        child.wait_with_output().await
    })
    .await
    .map_err(|_| anyhow::anyhow!("Artifact installation timed out"))??;
    validation_result(output)
}

fn advertised(home: &Path) -> Vec<Value> {
    if !root(home).join("approved").is_dir() {
        return Vec::new();
    }
    python_validate(&json!({
        "action": "advertise",
        "home": home.to_string_lossy(),
    }))
    .ok()
    .and_then(|value| value["providers"].as_array().cloned())
    .unwrap_or_default()
}

pub async fn installed_providers(home: PathBuf) -> Vec<Value> {
    tokio::task::spawn_blocking(move || advertised(&home))
        .await
        .unwrap_or_default()
}

#[cfg(all(test, unix))]
mod tests {
    use super::*;
    use std::fs;

    fn home(name: &str) -> PathBuf {
        let path =
            std::env::temp_dir().join(format!("kotoyose-decl-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&path);
        fs::create_dir_all(&path).unwrap();
        fs::write(path.join("host.json"), br#"{"fingerprint":"0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef","address":"127.0.0.1:1","name":"host","host_cert_pem":""}"#).unwrap();
        path
    }

    fn document(name: &str) -> String {
        format!(
            r#"{{"schema":"kotoyose.provider-data/1","identity":{{"id":"fake-cli","profile_id":"fake-cli","display_name":"{name}","publisher":"Example"}},"driver":{{"id":"cli.acp","contract_version":1}},"platforms":{{"{}":{{"max_capability":"read","install":{{"version":"1.0.0","urls":["https://example.com/fake-cli"],"sha256":"{}","format":"raw","binary":"fake-cli"}}}}}},"connection_kinds":["subscription"],"network":{{"download_hosts":["example.com"],"login_hosts":[],"runtime_hosts":[]}},"operations":{{"login":{{"argv":["login"],"timeout_seconds":30}},"status":{{"argv":["status"],"timeout_seconds":15}}}},"acp":{{"argv":["acp","--model","{{model}}"],"placeholders":["model"]}}}}"#,
            platform_key(),
            "ab".repeat(32),
        )
    }

    #[test]
    fn approval_file_is_owner_only_and_a_changed_hash_is_not_approved() {
        let home = home("approval");
        let first = document("Fake CLI");
        let sha = sha256_hex(first.as_bytes());
        store_catalog(
            &home,
            &json!({"offers":[{"id":"fake-cli","sha256":sha,"document":first}]}),
        )
        .unwrap();
        let listed = approve(&home, "fake-cli", &sha, "read", false).unwrap();
        let path = root(&home).join("approved").join("fake-cli.json");
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            assert_eq!(
                fs::metadata(&path).unwrap().permissions().mode() & 0o777,
                0o600
            );
            assert_eq!(
                fs::metadata(path.parent().unwrap())
                    .unwrap()
                    .permissions()
                    .mode()
                    & 0o777,
                0o700
            );
        }
        assert_eq!(listed["offers"][0]["status"], "approved");
        assert!(listed["offers"][0].get("document").is_none());
        assert_eq!(listed["offers"][0]["binary"], "fake-cli");
        let invalid = sha256_hex(b"other");
        assert!(
            store_catalog(
                &home,
                &json!({"offers":[{"id":"fake-cli","sha256":invalid,"document":"other"}]}),
            )
            .is_err()
        );
        let changed = document("Fake Tool");
        let changed_sha = sha256_hex(changed.as_bytes());
        store_catalog(
            &home,
            &json!({"offers":[{"id":"fake-cli","sha256":changed_sha,"document":changed}]}),
        )
        .unwrap();
        assert_eq!(list(&home)["offers"][0]["status"], "stale");
        note_store_failure(
            &home,
            "provider storage ACL grants access outside the current user\nsecret",
        );
        assert_eq!(
            list(&home)["error"],
            "provider storage ACL grants access outside the current user"
        );
        assert!(!list(&home)["error"].as_str().unwrap().contains("secret"));
        let again = document("Fake Tool");
        store_catalog(
            &home,
            &json!({"offers":[{"id":"fake-cli","sha256":changed_sha,"document":again}]}),
        )
        .unwrap();
        assert!(list(&home).get("error").is_none());
        revoke(&home, "fake-cli").unwrap();
        let _ = fs::remove_dir_all(home);
    }

    #[test]
    fn offered_store_refuses_a_link_to_an_existing_file() {
        let home = home("symlink");
        let existing = home.join("existing");
        fs::write(&existing, b"x").unwrap();
        let dir = root(&home);
        std::fs::create_dir(&dir).unwrap();
        use std::os::unix::fs::PermissionsExt;
        std::fs::set_permissions(&dir, std::fs::Permissions::from_mode(0o700)).unwrap();
        std::os::unix::fs::symlink(&existing, dir.join("offered.json")).unwrap();
        let document = document("Fake CLI");
        assert!(
            store_catalog(
                &home,
                &json!({"offers":[{"id":"fake-cli","sha256":sha256_hex(document.as_bytes()),"document":document}]}),
            )
            .is_err()
        );
        assert_eq!(fs::read(&existing).unwrap(), b"x");
        let _ = fs::remove_dir_all(home);
    }
}
