"""Folder packaging and the same bounds on a hostile wire payload."""

import base64
import os
from pathlib import Path
import stat
from tempfile import TemporaryDirectory

import pytest

from kotoyose import child_turn, skill_folders
from kotoyose.child_protocol import MAX_RECORD, decode_frame, encode_frame
from kotoyose.runtime.child import build_turn_payload
from kotoyose.skill_folders import (
    MAX_FOLDER_BYTES, MAX_FOLDER_FILES, MAX_TURN_FOLDER_BYTES, SkillFolder, decode_skill_folders, encode_skill_folders,
    read_skill_folder, write_skill_folders, write_skill_plugin,
)
from tests.test_child_sdk_skills import _FOLDERS, _payload
from tests.test_child_sdk_skills import alias_config as alias_config


@pytest.fixture
def source(tmp_path):
    plugin = tmp_path / "plugin"
    folder = plugin / "skills" / "example"
    folder.mkdir(parents=True)
    (folder / "SKILL.md").write_bytes(b"x")
    return plugin, folder


@pytest.mark.parametrize("kind", ["symlink_file", "symlink_dir", "hardlink", "fifo", "abs_name"])
def test_package_rejects_unsafe_source(source, tmp_path, kind):
    plugin, folder = source
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "private").write_bytes(b"fixture")
    if kind.startswith("symlink"):
        try:
            if kind == "symlink_file":
                (folder / "asset").symlink_to(outside / "private")
            elif kind == "symlink_dir":
                (folder / "linked").symlink_to(outside, target_is_directory=True)
            else:
                folder.rename(folder.with_name("real"))
                folder.symlink_to(folder.with_name("real"), target_is_directory=True)
        except OSError:
            pytest.skip("symlinks are unavailable")
    elif kind == "hardlink":
        os.link(outside / "private", folder / "asset")
    elif kind == "fifo":
        if not hasattr(os, "mkfifo"):
            pytest.skip("FIFO is unavailable")
        os.mkfifo(folder / "pipe")
    else:
        if os.name == "nt":
            pytest.skip("Windows disallows this filename")
        (folder / "C:asset").touch()
    with pytest.raises(ValueError, match="symlink|hardlink|non-regular|invalid skill file path"):
        read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)


def test_package_skips_hidden_and_cache_entries_at_every_depth(source):
    plugin, folder = source
    for relative in (".private", ".git/object", "node_modules/package", "sub/__pycache__/module", "sub/.ignored"):
        path = folder / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * MAX_FOLDER_BYTES)
    (folder / "sub/asset").write_bytes(b"keep")
    assert read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES) == SkillFolder(
        "example", {"SKILL.md": b"x", "sub/asset": b"keep"},
    )


@pytest.mark.parametrize("path", [
    "../outside", "sub/../outside", "/outside", "C:/outside", r"\outside", "NUL.txt", "sub//data",
    ".private", "sub/.private/file", "node_modules/package", "sub/__pycache__/module",
])
def test_decode_rejects_unsafe_path(path):
    with pytest.raises(ValueError, match="invalid skill file path"):
        decode_skill_folders([{"name": "example", "files": {"SKILL.md": "eA==", path: "eA=="}}])


@pytest.mark.parametrize("other", ["source", "SOURCE/index", "source/INDEX", "ｓource/index", "source/ｉｎｄｅｘ"])
def test_decode_rejects_colliding_paths(other):
    with pytest.raises(ValueError, match="conflicting skill file paths"):
        decode_skill_folders([{"name": "example", "files": {"SKILL.md": "eA==", "source/index": "", other: ""}}])


def test_utf8_path_limit_on_both_sides(source):
    plugin, folder = source
    relative = "sub/" + "é" * 48
    asset = folder / relative
    asset.parent.mkdir()
    asset.write_bytes(b"")
    snapshot = read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    assert snapshot is not None
    [wire] = encode_skill_folders([snapshot])
    assert decode_skill_folders([wire]) == (snapshot,)
    asset.rename(folder / (relative + "x"))
    wire["files"][relative + "x"] = wire["files"].pop(relative)
    with pytest.raises(ValueError, match="invalid skill file path"):
        read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    with pytest.raises(ValueError, match="invalid skill file path"):
        decode_skill_folders([wire])


def test_count_at_and_above_limit_on_both_sides(source):
    plugin, folder = source
    for index in range(MAX_FOLDER_FILES - 1):
        (folder / str(index)).touch()
    snapshot = read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    assert snapshot is not None
    [wire] = encode_skill_folders([snapshot])
    assert decode_skill_folders([wire]) == (snapshot,)
    (folder / "more").touch()
    wire["files"]["more"] = ""
    with pytest.raises(ValueError, match=f"{MAX_FOLDER_FILES} files"):
        read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    with pytest.raises(ValueError, match=f"{MAX_FOLDER_FILES} files"):
        decode_skill_folders([wire])


def test_bytes_at_and_above_limit_on_both_sides(source):
    plugin, folder = source
    asset = folder / "asset"
    data = b"\x00" * (MAX_FOLDER_BYTES - 1)
    asset.write_bytes(data)
    snapshot = read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    assert snapshot is not None
    [wire] = encode_skill_folders([snapshot])
    assert decode_skill_folders([wire]) == (snapshot,)
    data += b"\x00"
    asset.write_bytes(data)
    wire["files"]["asset"] = base64.b64encode(data).decode()
    with pytest.raises(ValueError, match=f"skill 'example' exceeds {MAX_FOLDER_BYTES} bytes"):
        read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    with pytest.raises(ValueError, match=f"skill 'example' exceeds {MAX_FOLDER_BYTES} bytes"):
        decode_skill_folders([wire])


def test_writer_refuses_an_existing_root_symlink(source, tmp_path):
    plugin, folder = source
    snapshot = read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
    assert snapshot is not None
    root = tmp_path / "link"
    try:
        root.symlink_to(folder, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    with pytest.raises(ValueError, match="could not write skill folders") as error:
        write_skill_folders(root, [snapshot])
    assert isinstance(error.value.__cause__, FileExistsError)
    assert list(folder.iterdir()) == [folder / "SKILL.md"]


def test_writer_reports_a_nested_directory_creation_collision(tmp_path, monkeypatch):
    mkdir = skill_folders._mkdir

    def collide(path, parent):
        mkdir(path, parent)
        if path.name == "sub":
            raise FileExistsError(path.name)

    monkeypatch.setattr(skill_folders, "_mkdir", collide)
    root = tmp_path / "output"
    with pytest.raises(ValueError, match="could not write skill folders") as error:
        write_skill_folders(root, [SkillFolder("example", {"sub/data": b"x"})])
    assert isinstance(error.value.__cause__, FileExistsError)
    assert not (root / "example/sub/data").exists()


def test_written_skill_plugin_is_read_only_and_temp_cleanup_removes_it(tmp_path):
    with TemporaryDirectory(dir=tmp_path) as temporary:
        plugin = write_skill_plugin(Path(temporary) / "plugin", [
            SkillFolder("example", {"SKILL.md": b"x", "sub/one": b"1", "sub/two": b"2"}),
        ])
        assert (plugin / "skills/example/sub/two").read_bytes() == b"2"
        for entry in (plugin, *plugin.rglob("*")):
            mode = entry.stat()
            if os.name == "nt":
                if entry.is_file():
                    assert mode.st_file_attributes & stat.FILE_ATTRIBUTE_READONLY
            else:
                assert stat.S_IMODE(mode.st_mode) == (0o500 if entry.is_dir() else 0o400)
    assert not Path(temporary).exists()


def test_large_turn_round_trips_and_total_is_bounded(alias_config, tmp_path):
    config, home, plugin = alias_config("claude.agent-sdk")
    config.models["dev"].skill_refs = ["demo"]
    folder = plugin / "skills" / "demo"
    attachment = folder / "source/index.html"
    content = b"\xff" * (MAX_FOLDER_BYTES - (folder / "SKILL.md").stat().st_size)
    attachment.write_bytes(content)
    payload = _payload(config, tmp_path, live=_FOLDERS)
    frame = encode_frame(1, "turn", payload)
    assert len(frame.encode()) > MAX_RECORD
    prepared = child_turn._prepare(home, decode_frame(frame).payload)
    try:
        [staged] = prepared["config"].models["dev"].plugins
        assert (Path(staged) / "skills/demo/source/index.html").read_bytes() == content
    finally:
        prepared["cleanup"]()

    config.models["dev"].skill_refs.append("review")
    with pytest.raises(ValueError, match=f"turn skill folders exceed {MAX_TURN_FOLDER_BYTES} bytes"):
        build_turn_payload(
            alias="dev", config=config, preset=config.models["dev"], conversation_key=None,
            cwd=str(tmp_path), prompt="test", live=_FOLDERS,
        )
    payload["skill_folders"].append({"name": "second", "files": {"SKILL.md": "eA=="}})
    with pytest.raises(child_turn.ChildTurnError, match=f"turn skill folders exceed {MAX_TURN_FOLDER_BYTES} bytes"):
        child_turn._prepare(home, payload)


def test_package_follows_configured_links_to_the_skill_root(source, tmp_path):
    """Skill bundles link skills/<name> (and sometimes the plugin itself) to a shared skill repository.

    Those two configured levels are followed once; links inside the resolved skill folder are not.
    """
    plugin, folder = source
    shared = tmp_path / "shared" / "example"
    shared.parent.mkdir()
    folder.rename(shared)
    (shared / "source").mkdir()
    (shared / "source" / "index.html").write_bytes(b"<p>ok</p>")
    try:
        folder.symlink_to(shared, target_is_directory=True)
        linked_plugin = tmp_path / "linked-plugin"
        linked_plugin.symlink_to(plugin, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks are unavailable")
    expected = SkillFolder("example", {"SKILL.md": b"x", "source/index.html": b"<p>ok</p>"})
    assert read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES) == expected
    assert read_skill_folder(linked_plugin, "example", MAX_TURN_FOLDER_BYTES) == expected
    (shared / "escape").symlink_to(tmp_path, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        read_skill_folder(plugin, "example", MAX_TURN_FOLDER_BYTES)
