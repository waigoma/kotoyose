use std::{env, fs, path::PathBuf, process::Command};

fn git(args: &[&str]) -> Option<String> {
    let output = Command::new("git")
        .env("GIT_OPTIONAL_LOCKS", "0")
        .env(
            "GIT_CONFIG_GLOBAL",
            if cfg!(windows) { "NUL" } else { "/dev/null" },
        )
        .env(
            "GIT_CONFIG_SYSTEM",
            if cfg!(windows) { "NUL" } else { "/dev/null" },
        )
        .args([
            "-c",
            "core.fsmonitor=false",
            "-c",
            if cfg!(windows) {
                "core.excludesFile=NUL"
            } else {
                "core.excludesFile=/dev/null"
            },
        ])
        .args(args)
        .output()
        .ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn build_id() -> String {
    println!("cargo:rerun-if-env-changed=KOTOYOSE_CHILD_BUILD_ID");
    for path in ["src", "installer", "Cargo.toml", "Cargo.lock", "../scripts"] {
        println!("cargo:rerun-if-changed={path}");
    }
    if let Ok(build) = env::var("KOTOYOSE_CHILD_BUILD_ID") {
        return build;
    }
    for path in ["HEAD", "index", "packed-refs"] {
        if let Some(path) = git(&["rev-parse", "--git-path", path]) {
            println!("cargo:rerun-if-changed={path}");
        }
    }
    if let Some(reference) = git(&["symbolic-ref", "-q", "HEAD"])
        && let Some(path) = git(&["rev-parse", "--git-path", &reference])
    {
        println!("cargo:rerun-if-changed={path}");
    }
    let Some(commit) = git(&["rev-parse", "HEAD"]) else {
        return "unknown".into();
    };
    let dirty = git(&["status", "--porcelain"]).is_some_and(|status| !status.is_empty());
    if dirty { "unknown".into() } else { commit }
}

fn main() {
    println!("cargo:rustc-env=KOTOYOSE_CHILD_BUILD_ID={}", build_id());
    println!("cargo:rerun-if-changed=../pyproject.toml");
    let source = std::fs::read_to_string("../pyproject.toml").expect("read pyproject.toml");
    let project: toml::Table = source.parse().expect("parse pyproject.toml");
    let version = project["project"]["version"]
        .as_str()
        .expect("project.version must be a string");
    assert_eq!(
        version,
        env!("CARGO_PKG_VERSION"),
        "Cargo.toml and pyproject.toml must use the same package version"
    );
    println!("cargo:rustc-env=KOTOYOSE_PACKAGE_VERSION={version}");

    println!("cargo:rerun-if-changed=../src/kotoyose");
    println!("cargo:rerun-if-env-changed=KOTOYOSE_BUILD_PYTHON");
    let python = env::var_os("KOTOYOSE_BUILD_PYTHON")
        .unwrap_or_else(|| if cfg!(windows) { "python" } else { "python3" }.into());
    let catalog = Command::new(python)
        .args(["-B", "-m", "kotoyose.child_catalog"])
        .env("PYTHONPATH", "../src")
        .output()
        .expect("start Python to generate the child catalog");
    assert!(
        catalog.status.success(),
        "generate child catalog: {}",
        String::from_utf8_lossy(&catalog.stderr)
    );
    let out = PathBuf::from(env::var_os("OUT_DIR").expect("Cargo OUT_DIR"));
    fs::write(out.join("child_components.json"), catalog.stdout).expect("write child catalog");
}
