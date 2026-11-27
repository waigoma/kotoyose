use crate::messages;
use std::{
    fs, io,
    path::{Component, Path, PathBuf},
};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};

use crate::{
    link::{address, fingerprint},
    state::{self, Policy},
};

pub const MAX_LOCAL_LLM_CONNECTIONS: usize = 16;
const MAX_LOCAL_LLM_MODELS: usize = 64;
const MAX_MODEL_ID_BYTES: usize = 256;

pub fn reserved_local_llm_ports(home: &Path) -> Result<Vec<u16>> {
    let mut ports = Vec::new();
    if let Ok(host) = state::Host::load(home)
        && let Ok((_, port)) = address(&host.address)
    {
        ports.push(port);
    }
    match fs::read_to_string(home.join(state::SETTINGS_PORT_FILE)) {
        Ok(raw) => {
            let port = raw
                .parse::<u16>()
                .ok()
                .filter(|port| {
                    *port > 0
                        && (1..=5).contains(&raw.len())
                        && raw.bytes().all(|byte| byte.is_ascii_digit())
                })
                .context("Child settings UI port is unavailable")?;
            ports.push(port);
        }
        Err(error) if error.kind() == io::ErrorKind::NotFound => {}
        Err(error) => return Err(error).context("Child settings UI port is unavailable"),
    }
    Ok(ports)
}

#[derive(Deserialize, Serialize)]
#[serde(deny_unknown_fields)]
pub struct LocalLlm {
    pub url: String,
    pub models: Vec<String>,
}

impl LocalLlm {
    pub fn validate(&self, name: &str, reserved_ports: &[u16]) -> Result<()> {
        ensure!(
            (1..=64).contains(&name.len())
                && name
                    .bytes()
                    .all(|byte| byte.is_ascii_alphanumeric() || b"_.-".contains(&byte)),
            "Local LLM connection IDs must match [A-Za-z0-9_.-]{{1,64}}"
        );
        let port = self
            .url
            .strip_prefix("http://")
            .or_else(|| self.url.strip_prefix("https://"))
            .and_then(|address| {
                address
                    .strip_prefix("127.0.0.1:")
                    .or_else(|| address.strip_prefix("[::1]:"))
            })
            .and_then(|address| address.strip_suffix("/v1"))
            .filter(|port| (1..=5).contains(&port.len()) && port.bytes().all(|byte| byte.is_ascii_digit()))
            .and_then(|port| port.parse::<u16>().ok())
            .filter(|port| *port >= 1024)
            .context("Local LLM URL must use http(s), 127.0.0.1 or [::1], an explicit port, and /v1 (port 1024–65535)")?;
        ensure!(
            !reserved_ports.contains(&port),
            "Local LLM URL must not use the child settings UI or control-plane port"
        );
        ensure!(
            (1..=MAX_LOCAL_LLM_MODELS).contains(&self.models.len()),
            "Local LLM connections require 1 to {MAX_LOCAL_LLM_MODELS} models"
        );
        ensure!(
            self.models
                .iter()
                .all(|model| model.len() <= MAX_MODEL_ID_BYTES),
            "Local LLM model IDs must be valid UTF-8 and at most {MAX_MODEL_ID_BYTES} bytes"
        );
        let mut models = std::collections::HashSet::new();
        ensure!(
            self.models.iter().all(|model| {
                !model.trim().is_empty()
                    && !model.bytes().any(|byte| byte.is_ascii_control())
                    && models.insert(model)
            }),
            "Local LLM models must be a non-empty list of unique model IDs"
        );
        Ok(())
    }
}

#[derive(Serialize)]
pub struct Versioned {
    pub policy: Policy,
    pub revision: String,
    pub available_adapters: Vec<String>,
    pub error: Option<&'static str>,
    pub warnings: Vec<String>,
}

fn parse_saved(home: &Path, raw: io::Result<Option<Vec<u8>>>) -> Result<Policy, &'static str> {
    let Some(raw) = raw.map_err(|_| messages::POLICY_READ)? else {
        return Ok(Policy::default());
    };
    let text = std::str::from_utf8(&raw).map_err(|_| messages::POLICY_UTF8)?;
    let invalid = messages::POLICY_INVALID;
    let policy = Policy::parse(text).map_err(|_| invalid)?;
    policy.validate(home).map_err(|_| invalid)?;
    Ok(policy)
}

pub fn load(home: &Path, available_adapters: Vec<String>) -> Versioned {
    let (revision, raw) = state::revision(&home.join("policy.json"));
    let (policy, error) = match parse_saved(home, raw) {
        Ok(policy) => (policy, None),
        Err(error) => (Policy::default(), Some(error)),
    };
    Versioned {
        warnings: root_warnings(&policy),
        policy,
        revision,
        available_adapters,
        error,
    }
}

pub fn check_revision(home: &Path, expected: &str) -> Result<()> {
    state::check_revision(&home.join("policy.json"), expected)
}

pub fn save(home: &Path, expected: &str, contents: &[u8]) -> Result<String> {
    state::replace_checked(
        &home.join("policy.json"),
        contents,
        state::protect_secret,
        || check_revision(home, expected),
    )?;
    Ok(fingerprint(contents))
}

fn pasted(text: &str, windows: bool) -> String {
    let text = text.trim();
    let text = text
        .strip_prefix('"')
        .and_then(|text| text.strip_suffix('"'))
        .unwrap_or(text)
        .trim();
    if windows {
        text.replace('/', "\\")
    } else {
        text.to_owned()
    }
}

fn check_root(path: &Path) -> Result<()> {
    ensure!(path.is_absolute(), messages::ROOT_ABSOLUTE);
    ensure!(
        !path
            .components()
            .any(|component| component == Component::ParentDir),
        messages::ROOT_PARENT
    );
    #[cfg(windows)]
    {
        use std::path::Prefix;
        if let Some(Component::Prefix(prefix)) = path.components().next()
            && matches!(prefix.kind(), Prefix::UNC(..) | Prefix::VerbatimUNC(..))
        {
            return Ok(());
        }
    }
    ensure!(path.is_dir(), messages::ROOT_MISSING);
    Ok(())
}

fn root_warnings(policy: &Policy) -> Vec<String> {
    #[cfg(windows)]
    {
        use std::path::Prefix;
        policy
            .workspace_roots
            .iter()
            .filter_map(|root| {
                let mut components = root.components();
                let Some(Component::Prefix(prefix)) = components.next() else {
                    return None;
                };
                (matches!(prefix.kind(), Prefix::Disk(_) | Prefix::VerbatimDisk(_))
                    && components.next() == Some(Component::RootDir)
                    && components.next().is_none())
                .then(|| messages::drive_warning(root))
            })
            .collect()
    }
    #[cfg(not(windows))]
    {
        let _ = policy;
        Vec::new()
    }
}

pub fn validate(
    home: &Path,
    mut policy: Policy,
    available: Vec<String>,
) -> Result<(Vec<u8>, Vec<String>)> {
    for root in &mut policy.workspace_roots {
        let text = root.to_str().context(messages::ROOT_INVALID)?;
        *root = PathBuf::from(pasted(text, cfg!(windows)));
        check_root(root)?;
    }
    policy.validate(home)?;
    let saved = load(home, available);
    ensure!(
        policy.adapters.iter().all(|adapter| {
            saved.available_adapters.contains(adapter) || saved.policy.adapters.contains(adapter)
        }),
        messages::ADAPTER_MISSING
    );

    for spec in policy.mcp.values() {
        let valid_name = |name: &str| !name.trim().is_empty() && !name.contains('\0');
        let valid_value = |value: &str| !value.contains('\0');
        ensure!(
            spec["command"].as_str().is_some_and(valid_name)
                && spec["args"]
                    .as_array()
                    .is_some_and(|args| args.iter().all(|v| v.as_str().is_some_and(valid_value)))
                && spec["env"]
                    .as_object()
                    .is_none_or(|env| env.iter().all(|(key, value)| {
                        valid_name(key)
                            && !key.contains('=')
                            && value.as_str().is_some_and(valid_value)
                    })),
            messages::MCP_INVALID
        );
    }
    let mut contents = serde_json::to_vec_pretty(&policy)?;
    contents.push(b'\n');
    // The legacy policy reader also accepts unescaped Windows paths.
    let text = std::str::from_utf8(&contents)?;
    ensure!(
        state::parse_policy_text(text)? == serde_json::to_value(&policy)?,
        messages::POLICY_CONTROL_CHARS
    );
    Ok((contents, root_warnings(&policy)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::Value;
    use std::fs;

    #[test]
    fn validates_local_llm_policy_and_defaults_to_empty() {
        let home = tempfile::tempdir().unwrap();
        let mut policy: Value = serde_json::from_str(state::EMPTY_POLICY).unwrap();
        assert!(
            Policy::parse(&policy.to_string())
                .unwrap()
                .local_llm
                .is_empty()
        );
        for (url, valid) in [
            ("http://127.0.0.1:11434/v1", true),
            ("http://127.0.0.1:1024/v1", true),
            ("http://127.0.0.1:01023/v1", false),
            ("http://[::1]:1234/v1", true),
            ("http://127.0.0.2:11434/v1", false),
            ("http://localhost:11434/v1", false),
            ("http://127.0.0.1/v1", false),
            ("http://[::1]:65536/v1", false),
            ("http://[::1]:1234/v1?query=1", false),
            ("http://127.0.0.1:+80/v1", false),
        ] {
            policy["local_llm"] = serde_json::json!({"desk": {"url": url, "models": ["tiny"]}});
            let parsed = Policy::parse(&policy.to_string()).unwrap();
            assert_eq!(validate(home.path(), parsed, Vec::new()).is_ok(), valid);
        }
        policy["local_llm"]["desk"]["api_key_env"] = "TEST_KEY".into();
        assert!(Policy::parse(&policy.to_string()).is_err());
    }

    #[test]
    fn local_llm_rejects_ui_and_control_plane_ports() {
        let home = tempfile::tempdir().unwrap();
        fs::write(home.path().join(state::SETTINGS_PORT_FILE), "4322").unwrap();
        let pin = serde_json::json!({
            "address": "127.0.0.1:4321", "fingerprint": "fixture", "host_cert_pem": "fixture", "name": "dev",
        });
        fs::write(home.path().join("host.json"), pin.to_string()).unwrap();
        for port in [4321, 4322] {
            let mut policy = Policy::default();
            policy.local_llm.insert(
                "desk".into(),
                LocalLlm {
                    url: format!("http://[::1]:0{port}/v1"),
                    models: vec!["tiny".into()],
                },
            );
            assert!(
                policy
                    .validate(home.path())
                    .unwrap_err()
                    .to_string()
                    .contains("settings UI or control-plane port")
            );
        }
    }

    #[test]
    fn local_llm_limits_connection_count_model_count_and_utf8_bytes() {
        let home = tempfile::tempdir().unwrap();
        let mut policy = Policy::default();
        for i in 0..16 {
            let mut models = vec!["é".repeat(128)];
            models.extend((0..63).map(|id| format!("model-{id}")));
            policy.local_llm.insert(
                format!("connection-{i}"),
                LocalLlm {
                    url: "http://127.0.0.1:11434/v1".into(),
                    models,
                },
            );
        }
        assert!(policy.validate(home.path()).is_ok());
        let mut extra = policy.local_llm.remove("connection-0").unwrap();
        extra.models[0].push('a');
        assert!(
            extra
                .validate("extra", &[])
                .unwrap_err()
                .to_string()
                .contains("256 bytes")
        );
        extra.models[0].pop();
        extra.models.push("one more".into());
        assert!(
            extra
                .validate("extra", &[])
                .unwrap_err()
                .to_string()
                .contains("1 to 64 models")
        );
        extra.models.pop();
        policy.local_llm.insert("connection-0".into(), extra);
        policy.local_llm.insert(
            "extra".into(),
            LocalLlm {
                url: "http://127.0.0.1:11434/v1".into(),
                models: vec!["tiny".into()],
            },
        );
        assert!(
            policy
                .validate(home.path())
                .unwrap_err()
                .to_string()
                .contains("at most 16 connections")
        );
    }

    #[test]
    fn accepts_paths_pasted_from_explorer() {
        assert_eq!(pasted(r#"  "D:/work/project"  "#, true), r"D:\work\project");
        assert_eq!(pasted(r"D:\tmp\new", true), r"D:\tmp\new");
        assert_eq!(
            pasted(r#" "\\server/share/project" "#, true),
            r"\\server\share\project"
        );
    }

    #[test]
    fn codex_sandbox_max_defaults_and_round_trips() {
        let home = tempfile::tempdir().unwrap();
        let defaults = Policy::parse(state::EMPTY_POLICY).unwrap();
        assert_eq!(
            serde_json::to_value(defaults).unwrap()["codex_sandbox_max"],
            "workspace-write"
        );
        for sandbox in ["read-only", "workspace-write", "danger-full-access"] {
            let mut policy = serde_json::from_str::<Value>(state::EMPTY_POLICY).unwrap();
            policy["codex_sandbox_max"] = sandbox.into();
            let (raw, _) = validate(
                home.path(),
                Policy::parse(&policy.to_string()).unwrap(),
                Vec::new(),
            )
            .unwrap();
            assert_eq!(
                serde_json::from_slice::<Value>(&raw).unwrap()["codex_sandbox_max"],
                sandbox
            );
        }
        let mut policy = serde_json::from_str::<Value>(state::EMPTY_POLICY).unwrap();
        for invalid in [serde_json::json!("full-access"), Value::Null] {
            policy["codex_sandbox_max"] = invalid;
            assert!(Policy::parse(&policy.to_string()).is_err());
        }
    }

    #[test]
    fn validates_local_roots_and_mcp_fields() {
        let home = tempfile::tempdir().unwrap();
        let mut policy = Policy::parse(state::EMPTY_POLICY).unwrap();
        policy.workspace_roots = vec![PathBuf::from(format!("  \"{}\"  ", home.path().display()))];
        policy.mcp.insert("files".into(), serde_json::json!({
            "command": "files", "description": "files", "args": ["", "test"], "env": {"EXAMPLE": "value"}
        }));
        let (raw, warnings) = validate(home.path(), policy, Vec::new()).unwrap();
        assert!(warnings.is_empty());
        let mut policy = Policy::parse(std::str::from_utf8(&raw).unwrap()).unwrap();
        assert_eq!(policy.workspace_roots, [home.path()]);
        policy.workspace_roots.push(PathBuf::from("relative"));
        assert!(validate(home.path(), policy, Vec::new()).is_err());
        let mut policy = Policy::parse(std::str::from_utf8(&raw).unwrap()).unwrap();
        policy.workspace_roots.push(home.path().join("missing"));
        assert!(validate(home.path(), policy, Vec::new()).is_err());
        let mut policy = Policy::parse(std::str::from_utf8(&raw).unwrap()).unwrap();
        policy.workspace_roots.push(
            home.path()
                .join("..")
                .join(home.path().file_name().unwrap()),
        );
        assert!(
            validate(home.path(), policy, Vec::new())
                .unwrap_err()
                .to_string()
                .contains("..")
        );
        let mut policy = Policy::parse(std::str::from_utf8(&raw).unwrap()).unwrap();
        policy.mcp.get_mut("files").unwrap()["env"] = serde_json::json!({"A=B": "value"});
        assert!(validate(home.path(), policy, Vec::new()).is_err());
    }

    #[test]
    fn mcp_names_accept_only_the_ascii_alphabet_and_length_limit() {
        let home = tempfile::tempdir().unwrap();
        for (name, valid) in [
            ("a".into(), true),
            ("A_0.z-".into(), true),
            ("a".repeat(64), true),
            ("a".repeat(65), false),
            ("".into(), false),
            (" a".into(), false),
            ("a/b".into(), false),
            ("名前".into(), false),
        ] {
            let mut policy = Policy::default();
            policy
                .mcp
                .insert(name, serde_json::json!({"command": "files", "args": []}));
            assert_eq!(validate(home.path(), policy, Vec::new()).is_ok(), valid);
        }
    }

    #[cfg(windows)]
    #[test]
    fn a_pasted_drive_root_is_saved_with_a_warning() {
        let home = tempfile::tempdir().unwrap();
        let drive = home.path().ancestors().last().unwrap();
        let pasted = format!("  \"{}\"  ", drive.display().to_string().replace('\\', "/"));
        let policy = Policy {
            workspace_roots: vec![pasted.into(), home.path().into()],
            ..Policy::default()
        };
        let (contents, warnings) = validate(home.path(), policy, Vec::new()).unwrap();
        assert_eq!(warnings.len(), 1);
        assert!(warnings[0].contains(&drive.display().to_string()));
        save(home.path(), "missing", &contents).unwrap();
        assert_eq!(load(home.path(), Vec::new()).warnings, warnings);
    }

    #[test]
    fn invalid_policy_exposes_its_raw_revision_and_can_be_repaired() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join("policy.json");
        for raw in [
            b"broken".as_slice(),
            b"\xff",
            br#"{"adapters":["missing.adapter"],"mcp":{"x":null},"workspace_roots":[]}"#,
        ] {
            fs::write(&path, raw).unwrap();
            let loaded = load(home.path(), Vec::new());
            assert_eq!(loaded.revision, fingerprint(raw));
            assert!(loaded.error.is_some());
            assert!(loaded.policy.adapters.is_empty());
            assert!(loaded.policy.mcp.is_empty());

            let mut fresh = Policy::default();
            fresh.adapters.push("missing.adapter".into());
            assert!(validate(home.path(), fresh, Vec::new()).is_err());

            let (contents, _) = validate(home.path(), loaded.policy, Vec::new()).unwrap();
            save(home.path(), &loaded.revision, &contents).unwrap();
            assert!(load(home.path(), Vec::new()).error.is_none());
        }
    }

    #[cfg(unix)]
    #[test]
    fn an_unreadable_policy_can_be_replaced_when_the_directory_is_writable() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join("policy.json");
        std::os::unix::fs::symlink(home.path().join("missing"), &path).unwrap();
        let loaded = load(home.path(), Vec::new());
        assert!(loaded.error.is_some());
        assert!(loaded.revision.starts_with("unreadable:"));
        let (contents, _) = validate(home.path(), loaded.policy, Vec::new()).unwrap();
        save(home.path(), &loaded.revision, &contents).unwrap();
        assert!(load(home.path(), Vec::new()).error.is_none());
    }

    #[test]
    fn edits_compare_the_original_bytes_at_commit() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join("policy.json");
        let raw = state::EMPTY_POLICY.as_bytes();
        let original = load(home.path(), Vec::new()).revision;
        let written = save(home.path(), &original, raw).unwrap();
        assert_eq!(written, load(home.path(), Vec::new()).revision);
        assert!(
            save(home.path(), &original, raw)
                .unwrap_err()
                .is::<state::Conflict>()
        );
        let edited = format!("{}\n", state::EMPTY_POLICY);
        fs::write(&path, &edited).unwrap();
        assert!(
            save(home.path(), &written, raw)
                .unwrap_err()
                .is::<state::Conflict>()
        );
        assert_eq!(fs::read_to_string(&path).unwrap(), edited);
        save(home.path(), &load(home.path(), Vec::new()).revision, raw).unwrap();
    }

    #[test]
    fn validates_session_import_policy_fields() {
        let home = tempfile::tempdir().unwrap();
        let policy = Policy {
            session_import_sources: vec!["claude".into(), "codex".into(), "grok".into()],
            session_import_origin_hint: Some("work".into()),
            ..Policy::default()
        };
        assert!(validate(home.path(), policy, Vec::new()).is_ok());

        let bad_source = Policy {
            session_import_sources: vec!["unknown".into()],
            ..Policy::default()
        };
        assert!(validate(home.path(), bad_source, Vec::new()).is_err());

        let bad_hint = Policy {
            session_import_origin_hint: Some("secret".into()),
            ..Policy::default()
        };
        assert!(validate(home.path(), bad_hint, Vec::new()).is_err());
    }
}
