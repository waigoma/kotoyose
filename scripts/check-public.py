#!/usr/bin/env python3
"""Check an export and report locations and finding kinds without values."""

import argparse
import ipaddress
import os
from pathlib import Path
import re


HOME_PATH = re.compile(r"/" r"home/[^/\s\"'<>]+/[^\s\"'<>),]*")
IPV4 = re.compile(r"(?<![\w.])(?:\d{1,3}\.){3}\d{1,3}(?!\w|\.\d)")
EMAIL = re.compile(r"[\w.!#$%&'*+/=?^`{|}~-]+@(?:[\w-]+\.)+[a-z]{2,}\b", re.I)
SECRET = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{16,}|ghp_[A-Za-z0-9]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{12,}|AKIA[A-Z0-9]{16})\b|"
    r"-----BEGIN [A-Z0-9 ]+-----"
)
PRIVATE = tuple(map(ipaddress.ip_network, ("10." "0.0.0/8", "172." "16.0.0/12", "192." "168.0.0/16", "100." "64.0.0/10")))
# Only these source files and their tests use the fixed isolation mount points.
RUNTIME_HOME_FILES = {
    "src/kotoyose/container_claude.py",
    "src/kotoyose/container_codex.py",
    "src/kotoyose/native_sessions.py",
    "src/kotoyose/provider_jail.py",
    "tests/test_agent_container.py",
    "tests/test_container_claude.py",
    "tests/test_container_codex.py",
    "tests/test_container_print.py",
    "tests/test_native_codex.py",
    "tests/test_native_session_retention.py",
    "tests/test_place_conformance.py",
}
RUNTIME_HOME_ROOT = "/home/"
RUNTIME_HOMES = {
    RUNTIME_HOME_ROOT + suffix for suffix in (
        "agent/.claude", "agent/.codex", "agent/.codex/sessions",
        "agent/.codex/sessions/", "agent/.cursor", "kotoyose/.codex",
        "kotoyose/.codex/sessions", "kotoyose/.codex/sessions/",
    )
}
RUNTIME_TEST_PATHS = {
    ("tests/test_agent_container.py", RUNTIME_HOME_ROOT + "agent/x"),
    ("tests/test_native_session_retention.py", RUNTIME_HOME_ROOT + "kotoyose/.codex/sessions/../../"),
}


def private_ip(value):
    try:
        address = ipaddress.IPv4Address(value)
    except ValueError:
        return False
    return any(address in network for network in PRIVATE)


def public_email(value):
    user, domain = value.lower().split("@")
    return (user in {"noreply", "no-reply"} or domain == "users.noreply.github.com"
            or domain.split(".")[-1] == "example"
            or domain.split(".")[-2] == "example")


def findings(line, path, denylist, fake_ips):
    if any(term in line.casefold() for term in denylist):
        yield "denylist"
    if any(
        not (path in RUNTIME_HOME_FILES and match[0] in RUNTIME_HOMES)
        and (path, match[0]) not in RUNTIME_TEST_PATHS
        for match in HOME_PATH.finditer(line)
    ):
        yield "absolute-home"
    if (not (path == "release/public-fake-ips.txt" and tuple(line.split()) in fake_ips)
            and any(private_ip(match[0]) and (path, match[0]) not in fake_ips for match in IPV4.finditer(line))):
        yield "private-ipv4"
    if any(not public_email(match[0]) for match in EMAIL.finditer(line)):
        yield "email"
    if SECRET.search(line):
        yield "secret-pattern"


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    root = parser.parse_args().directory
    if not root.is_dir():
        parser.exit(1, "Expected an export directory.\n")
    denylist = [line.strip().casefold() for line in os.environ.get("PUBLIC_DENYLIST", "").splitlines() if line.strip()]
    manifest = Path(__file__).resolve().parents[1] / "release/public-fake-ips.txt"
    fake_ips = {
        tuple(line.split()) for line in manifest.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    count = 0
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        location = relative
        if any(findings(relative, "", denylist, set())) or any(ord(c) < 32 for c in relative):
            location = "<masked-path>"
            count += 1
            print(f"{location}:0: sensitive-path")
        try:
            if path.is_symlink():
                contents = os.readlink(path)
            elif path.is_file():
                contents = path.read_bytes().decode("utf-8", errors="replace")
            else:
                continue
        except OSError:
            contents = ""
            count += 1
            print(f"{location}:0: unreadable")
        for number, line in enumerate(contents.splitlines(), 1):
            for kind in findings(line, relative, denylist, fake_ips):
                count += 1
                print(f"{location}:{number}: {kind}")
    print(f"Public check: {count} findings.")
    return int(bool(count))


if __name__ == "__main__":
    raise SystemExit(main())
