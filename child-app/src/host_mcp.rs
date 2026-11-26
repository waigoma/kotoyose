use std::{collections::BTreeMap, fs, path::Path};

use anyhow::{Context, Result, ensure};
use serde::{Deserialize, Serialize};
use serde_json::Value;

use crate::state::{self, Host};

const CATALOG: &str = "host-mcp-catalog.json";
const APPROVALS: &str = "host-mcp-approvals.json";
const LIMIT: usize = 64;

#[derive(Default, Deserialize, Serialize)]
pub struct Catalog {
    pub fingerprint: String,
    pub offers: Vec<Offer>,
}

#[derive(Deserialize, Serialize)]
pub struct Offer {
    pub name: String,
    pub version: String,
    pub description: String,
    pub tools: Vec<String>,
    #[serde(default)]
    pub all_tools: bool,
    /// Host stdio command, shown to the owner. The child never executes it.
    #[serde(default)]
    pub command: String,
    /// Host stdio arguments, shown to the owner. Values are not hidden; the child never executes them.
    #[serde(default)]
    pub args: Vec<String>,
    /// Host working directory, shown to the owner.
    #[serde(default)]
    pub cwd: String,
    /// Environment variable names only. Values, including secrets, are not part of the offer.
    #[serde(default)]
    pub env_names: Vec<String>,
}

#[derive(Default, Deserialize, Serialize)]
struct Approvals {
    fingerprint: String,
    versions: BTreeMap<String, String>,
}

pub fn digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
}

fn name(value: &str) -> bool {
    !value.is_empty()
        && value.len() <= 128
        && value.trim() == value
        && !value.contains(':')
        && !value.chars().any(char::is_control)
}

pub fn store_catalog(home: &Path, fingerprint: &str, payload: &Value) -> Result<()> {
    let offers: Vec<Offer> = serde_json::from_value(payload["offers"].clone())?;
    ensure!(
        digest(fingerprint) && offers.len() <= LIMIT,
        "invalid MCP catalog"
    );
    let mut names = std::collections::BTreeSet::new();
    ensure!(
        offers.iter().all(|offer| {
            name(&offer.name)
                && names.insert(&offer.name)
                && digest(&offer.version)
                && offer.description.len() <= 2000
                // Kotoyose carries complete alias names in the catalog frame.
                && (offer.name == "kotoyose"
                    || (offer.tools.len() <= LIMIT
                        && offer.tools.iter().all(|tool| tool.len() <= 512)))
                && offer.command.len() <= 4096
                && !offer.command.chars().any(char::is_control)
                && offer.args.len() <= 64
                && offer
                    .args
                    .iter()
                    .all(|arg| arg.len() <= 4096 && !arg.chars().any(char::is_control))
                && offer.cwd.len() <= 4096
                && !offer.cwd.chars().any(char::is_control)
                && offer.env_names.len() <= 64
                && offer.env_names.iter().all(|name| {
                    !name.is_empty()
                        && name.len() <= 256
                        && !name.contains('=')
                        && !name.chars().any(char::is_control)
                })
        }),
        "invalid MCP catalog"
    );
    let catalog = Catalog {
        fingerprint: fingerprint.to_owned(),
        offers,
    };
    state::write_private(&home.join(CATALOG), &serde_json::to_vec(&catalog)?)
}

pub fn catalog(home: &Path) -> Catalog {
    let Some(host) = Host::load(home).ok() else {
        return Catalog::default();
    };
    state::read_json::<Catalog>(&home.join(CATALOG))
        .ok()
        .filter(|catalog| catalog.fingerprint == host.fingerprint)
        .unwrap_or_default()
}

pub fn approved(home: &Path, fingerprint: &str) -> BTreeMap<String, String> {
    let path = home.join(APPROVALS);
    let Ok(metadata) = fs::symlink_metadata(&path) else {
        return BTreeMap::new();
    };
    if !metadata.is_file() || metadata.len() > 65536 {
        return BTreeMap::new();
    }
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        if metadata.permissions().mode() & 0o077 != 0 {
            return BTreeMap::new();
        }
    }
    state::read_json::<Approvals>(&path)
        .ok()
        .filter(|row| row.fingerprint == fingerprint)
        .map(|row| {
            let versions: BTreeMap<_, _> = row
                .versions
                .into_iter()
                .filter(|(key, version)| name(key) && digest(version))
                .collect();
            if versions.len() > LIMIT {
                BTreeMap::new()
            } else {
                versions
            }
        })
        .unwrap_or_default()
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub struct Edit {
    fingerprint: String,
    name: String,
    version: String,
    approve: bool,
}

pub fn edit(home: &Path, edit: Edit) -> Result<()> {
    ensure!(
        cfg!(target_os = "linux") && digest(&edit.fingerprint),
        "MCP relay is unavailable"
    );
    let host = Host::load(home).context("MCP relay requires an enrolled host")?;
    ensure!(
        edit.fingerprint == host.fingerprint,
        "MCP host changed. Reload and review again."
    );
    let mut versions = approved(home, &host.fingerprint);
    if edit.approve {
        let catalog = catalog(home);
        ensure!(
            catalog.fingerprint == host.fingerprint
                && catalog
                    .offers
                    .iter()
                    .any(|offer| offer.name == edit.name && offer.version == edit.version),
            "MCP publication changed. Reload and review again."
        );
        versions.retain(|name, version| {
            catalog
                .offers
                .iter()
                .any(|offer| &offer.name == name && &offer.version == version)
        });
        versions.insert(edit.name, edit.version);
    } else {
        ensure!(
            versions.get(&edit.name) == Some(&edit.version),
            "MCP approval changed. Reload and review again."
        );
        versions.remove(&edit.name);
    }
    state::write_private(
        &home.join(APPROVALS),
        &serde_json::to_vec(&Approvals {
            fingerprint: host.fingerprint,
            versions,
        })?,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tighten(_path: &std::path::Path) {
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            fs::set_permissions(_path, fs::Permissions::from_mode(0o600)).unwrap();
        }
    }

    #[test]
    fn one_bad_approval_keeps_the_rest_and_names_stop_at_128() {
        let home = std::env::temp_dir().join(format!("kotoyose-host-mcp-{}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(&home).unwrap();
        let fingerprint = "ab".repeat(32);
        let good = "cd".repeat(32);
        let long_name = "n".repeat(129);
        let path = home.join(APPROVALS);
        fs::write(
            &path,
            format!(
                r#"{{"fingerprint":"{fingerprint}","versions":{{"ok":"{good}","{long_name}":"{good}","bad":"zz"}}}}"#
            ),
        )
        .unwrap();
        tighten(&path);
        let approved = approved(&home, &fingerprint);
        assert_eq!(approved.get("ok").map(String::as_str), Some(good.as_str()));
        assert_eq!(approved.len(), 1);
        assert!(!name(&long_name));
        assert!(name(&"n".repeat(128)));
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn delegation_offer_keeps_the_full_alias_list() {
        let home = std::env::temp_dir().join(format!(
            "kotoyose-host-mcp-delegation-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(&home).unwrap();
        let fingerprint = "ab".repeat(32);
        let version = "cd".repeat(32);
        let mut aliases: Vec<_> = (0..65).map(|index| format!("reader-{index}")).collect();
        aliases.push("long-delegate-".repeat(50));
        let mut payload = serde_json::json!({"offers": [{
            "name": "kotoyose", "version": version, "description": "Kotoyose delegation",
            "tools": aliases, "all_tools": false,
        }]});
        store_catalog(&home, &fingerprint, &payload).unwrap();
        let saved = state::read_json::<Catalog>(&home.join(CATALOG)).unwrap();
        assert_eq!(saved.offers[0].tools, aliases);
        assert!(!saved.offers[0].all_tools);
        payload["offers"][0]["name"] = serde_json::json!("files");
        assert!(store_catalog(&home, &fingerprint, &payload).is_err());
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn stdio_command_is_stored_and_control_characters_are_rejected() {
        let home =
            std::env::temp_dir().join(format!("kotoyose-host-mcp-stdio-{}", std::process::id()));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(&home).unwrap();
        let fingerprint = "ab".repeat(32);
        let version = "cd".repeat(32);
        let command = "/opt/kotoyose/host-only-stdio";
        let payload = serde_json::json!({"offers": [{
            "name": "files", "version": version, "description": "Host files",
            "tools": ["write"], "command": command,
        }]});
        store_catalog(&home, &fingerprint, &payload).unwrap();
        let saved: serde_json::Value =
            serde_json::from_slice(&fs::read(home.join(CATALOG)).unwrap()).unwrap();
        assert_eq!(saved["offers"][0]["command"], command);
        let mut broken = payload;
        broken["offers"][0]["command"] = serde_json::json!("/tmp/bad\ncommand");
        assert!(store_catalog(&home, &fingerprint, &broken).is_err());
        let _ = fs::remove_dir_all(&home);
    }

    #[test]
    fn stdio_offer_keeps_args_cwd_and_env_names_and_drops_secret_values() {
        let home = std::env::temp_dir().join(format!(
            "kotoyose-host-mcp-stdio-offer-{}",
            std::process::id()
        ));
        let _ = fs::remove_dir_all(&home);
        fs::create_dir_all(&home).unwrap();
        let fingerprint = "ab".repeat(32);
        let version = "cd".repeat(32);
        let payload = serde_json::json!({"offers": [{
            "name": "files",
            "version": version,
            "description": "Host files",
            "tools": ["write"],
            "command": "/opt/kotoyose/host-only-stdio",
            "args": ["--serve", "--root", "/var/lib/kotoyose"],
            "cwd": "/var/lib/kotoyose",
            "env_names": ["TOKEN", "VISIBLE"],
            "env": {"TOKEN": "secret-value-must-not-appear"},
        }]});
        store_catalog(&home, &fingerprint, &payload).unwrap();
        let text = fs::read_to_string(home.join(CATALOG)).unwrap();
        let saved: serde_json::Value = serde_json::from_str(&text).unwrap();
        assert_eq!(
            saved["offers"][0]["args"],
            serde_json::json!(["--serve", "--root", "/var/lib/kotoyose"])
        );
        assert_eq!(saved["offers"][0]["cwd"], "/var/lib/kotoyose");
        assert_eq!(
            saved["offers"][0]["env_names"],
            serde_json::json!(["TOKEN", "VISIBLE"])
        );
        assert!(!text.contains("secret-value-must-not-appear"));
        let mut broken = payload;
        broken["offers"][0]["args"] = serde_json::json!(["ok", "bad\narg"]);
        assert!(store_catalog(&home, &fingerprint, &broken).is_err());
        let _ = fs::remove_dir_all(&home);
    }
}
