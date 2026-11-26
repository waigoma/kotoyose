"""Fingerprint a staged child package and fill its embedded dirty build ID."""

from array import array
import hashlib
import os
from pathlib import Path
import struct
import sys

MARKER = b"KOTOYOSE_CHILD_BUILD:"
PLACEHOLDER = "-dirty-000000000000"


def pe_checksum(image: bytearray) -> None:
    offset = struct.unpack_from("<I", image, 0x3c)[0] + 24 + 64
    if not struct.unpack_from("<I", image, offset)[0]:
        return
    struct.pack_into("<I", image, offset, 0)
    words = array("H", image[:len(image) & ~1])
    if sys.byteorder == "big":
        words.byteswap()
    total = sum(words) + (image[-1] if len(image) % 2 else 0)
    total = (total & 0xffff) + (total >> 16)
    checksum = ((total & 0xffff) + (total >> 16)) + len(image)
    struct.pack_into("<I", image, offset, checksum)


def payload_files(root: Path, entries: list[str]):
    for name in entries:
        path = root / name
        if path.is_dir():
            for folder, dirs, files in os.walk(path, followlinks=True):
                if "__pycache__" in dirs:
                    dirs.remove("__pycache__")
                yield from (Path(folder) / file for file in files)
        else:
            yield path


def fingerprint(root: Path, provisional: str, entries: list[str]) -> str:
    if not provisional.endswith(PLACEHOLDER):
        raise ValueError("expected a placeholder build ID")
    digest = hashlib.sha256()
    binaries = []
    marker = MARKER + provisional.encode("ascii") + b"\0"
    for path in sorted(payload_files(root, entries), key=lambda path: os.fsencode(path.relative_to(root))):
        name = os.fsencode(path.relative_to(root))
        digest.update(len(name).to_bytes(8, "big"))
        digest.update(name)
        digest.update(path.stat().st_size.to_bytes(8, "big"))
        with path.open("rb") as stream:
            hashlib.file_digest(stream, lambda: digest)
        if path.parent == root / "bin" and path.name in {
            "kotoyose-child-app", "kotoyose-child-app.exe", "kotoyose-child-background.exe",
        }:
            binaries.append(path)

    # Hash with the placeholder in the staged executables to avoid a self-referential ID.
    # Replace only the marker and the PE checksum so the rest of the fingerprinted bytes stay intact.
    build_id = provisional[:-12] + digest.hexdigest()[:12]
    record = MARKER + build_id.encode("ascii") + b"\0"
    for path in binaries:
        image = bytearray(path.read_bytes())
        if image.count(marker) != 1:
            raise ValueError(f"expected exactly one build ID placeholder in {path}")
        image = image.replace(marker, record)
        if path.suffix == ".exe":
            pe_checksum(image)
        path.write_bytes(image)
    return build_id


if __name__ == "__main__":
    stage, build_id, *entries = sys.argv[1:]
    print(fingerprint(Path(stage), build_id, entries))
