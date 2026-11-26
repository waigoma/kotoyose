use super::*;
use flate2::{Compression, write::GzEncoder};
use tar::{Builder, EntryType, Header};

const TOP: &str = "kotoyose-child-1.3.0";

fn archive(name: &str, kind: EntryType, size: u64, contents: &[u8]) -> Vec<u8> {
    let mut archive = Builder::new(GzEncoder::new(Vec::new(), Compression::fast()));
    let mut header = Header::new_ustar();
    header.set_entry_type(kind);
    header.set_size(size);
    header.set_mode(0o4755);
    header.as_mut_bytes()[..name.len()].copy_from_slice(name.as_bytes());
    if kind.is_symlink() || kind.is_hard_link() {
        header.set_link_name("../../outside").unwrap();
    }
    header.set_cksum();
    archive.append(&header, contents).unwrap();
    archive.into_inner().unwrap().finish().unwrap()
}

#[test]
fn rejects_absolute_wrong_root_and_parent_paths() {
    let parent = tempfile::tempdir().unwrap();
    let root = parent.path().join("release");
    fs::create_dir(&root).unwrap();
    for path in [
        "/kotoyose-child-1.3.0/outside",
        "different-release/outside",
        "kotoyose-child-1.3.0/../outside",
        "kotoyose-child-1.3.0/sub/../../outside",
    ] {
        let bytes = archive(path, EntryType::Regular, 1, b"a");
        assert!(extract(&bytes[..], TOP, &root).is_err(), "{path}");
    }
    assert_eq!(fs::read_dir(&root).unwrap().count(), 0);
    assert!(!parent.path().join("outside").exists());
}

#[test]
fn rejects_links_and_special_files() {
    for kind in [
        EntryType::Symlink,
        EntryType::Link,
        EntryType::Char,
        EntryType::Block,
        EntryType::Fifo,
        EntryType::GNUSparse,
        EntryType::XHeader,
    ] {
        let root = tempfile::tempdir().unwrap();
        let bytes = archive(&format!("{TOP}/entry"), kind, 0, b"");
        assert!(extract(&bytes[..], TOP, root.path()).is_err(), "{kind:?}");
        assert_eq!(fs::read_dir(root.path()).unwrap().count(), 0);
    }
}

#[test]
fn enforces_member_and_expanded_stream_limits() {
    let root = tempfile::tempdir().unwrap();
    let name = format!("{TOP}/entry");
    let oversized_member = archive(&name, EntryType::Regular, MAX_EXPANDED + 1, b"");
    assert!(extract(&oversized_member[..], TOP, root.path()).is_err());
    assert!(!root.path().join("entry").exists());
    let oversized_padding = archive(&name, EntryType::Regular, 1, b"a");
    assert!(extract_bounded(&oversized_padding[..], TOP, root.path(), 1024).is_err());
}

#[test]
fn places_regular_files_under_the_root_without_special_permissions() {
    let root = tempfile::tempdir().unwrap();
    let bytes = archive(&format!("{TOP}/bin/entry"), EntryType::Regular, 1, b"a");
    extract(&bytes[..], TOP, root.path()).unwrap();
    let binary = root.path().join("bin/entry");
    assert_eq!(fs::read(&binary).unwrap(), b"a");
    assert_eq!(fs::metadata(binary).unwrap().mode() & 0o4777, 0o755);
}
