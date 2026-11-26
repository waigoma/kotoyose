use std::{
    fs::{self, OpenOptions, Permissions},
    io::{self, Read},
    os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
    path::{Component, Path},
};

use anyhow::{Result, ensure};
use flate2::read::MultiGzDecoder;

const MAX_EXPANDED: u64 = 256 * 1024 * 1024;
const MAX_ENTRIES: usize = 24_000;

pub(super) fn extract(compressed: impl Read, top: &str, destination: &Path) -> Result<()> {
    extract_bounded(compressed, top, destination, MAX_EXPANDED)
}

fn extract_bounded(compressed: impl Read, top: &str, destination: &Path, limit: u64) -> Result<()> {
    let mut tar = tar::Archive::new(MultiGzDecoder::new(compressed).take(limit + 1));
    for (index, entry) in tar.entries()?.raw(true).enumerate() {
        ensure!(index < MAX_ENTRIES, "too many child package entries");
        let mut entry = entry?;
        let kind = entry.header().entry_type();
        ensure!(
            kind.is_file() || kind.is_dir(),
            "unsupported child package entry"
        );
        ensure!(
            entry.size() <= limit && (!kind.is_dir() || entry.size() == 0),
            "invalid child package entry size"
        );
        let name = entry.path()?;
        let mut components = name.components();
        ensure!(
            components.next() == Some(Component::Normal(top.as_ref())),
            "unexpected child package directory"
        );
        ensure!(
            components
                .clone()
                .all(|part| matches!(part, Component::Normal(_))),
            "unsafe child package path"
        );
        let path = destination.join(components.as_path());
        if kind.is_dir() {
            fs::create_dir_all(path)?;
        } else {
            ensure!(path != destination, "missing child package filename");
            fs::create_dir_all(path.parent().expect("package member parent"))?;
            let mode = entry.header().mode()? & 0o111;
            let mut file = OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o644 | mode)
                .open(path)?;
            let size = io::copy(&mut entry, &mut file)?;
            ensure!(size == entry.size(), "truncated child package entry");
        }
    }
    // Read through the gzip footer and account for any padding after the tar end.
    let mut reader = tar.into_inner();
    io::copy(&mut reader, &mut io::sink())?;
    ensure!(
        reader.limit() > 0,
        "child package exceeds the expanded size limit"
    );
    Ok(())
}

pub(super) fn copy_from(source: &Path, destination: &Path) -> Result<()> {
    for entry in source.read_dir()? {
        let entry = entry?;
        let path = entry.path();
        let target = destination.join(entry.file_name());
        let meta = entry.metadata()?;
        if meta.is_dir() {
            fs::create_dir(&target)?;
            copy_from(&path, &target)?;
        } else {
            ensure!(meta.is_file(), "unsupported child package entry");
            fs::copy(path, &target)?;
            fs::set_permissions(target, Permissions::from_mode(0o644 | meta.mode() & 0o111))?;
        }
    }
    Ok(())
}

#[cfg(test)]
mod tests;
