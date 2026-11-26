#!/usr/bin/env bash
set -euo pipefail

if ! command -v wine >/dev/null; then
    echo 'Wine is unavailable; skipping the installer smoke test.'
    exit 0
fi
if [[ $# != 1 ]]; then
    echo "Usage: $0 path/to/KotoyoseChild-Setup-<version>.exe" >&2
    exit 2
fi
# Wine can expose invalid standard handles to console Python in a headless shell.
if [[ ! -t 0 || ! -t 1 || ! -t 2 ]]; then
    printf -v smoke_command '%q ' "$0" "$@"
    SHELL=/bin/bash exec script -qec "$smoke_command" /dev/null
fi
if [[ -z ${DISPLAY:-} ]]; then
    exec xvfb-run -a "$0" "$@"
fi

root=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
exe=$(realpath -- "$1")
name=${exe##*/}
version=${name#KotoyoseChild-Setup-}
version=${version%.exe}
test -f "$exe"
scratch=$(mktemp -d "$root/dist/.child-wine.XXXXXX")
export WINEPREFIX="$scratch/prefix" WINEARCH=win64 WINEDEBUG=-all
export WINEDLLOVERRIDES='mscoree,mshtml='
export HOME="$scratch/home" XDG_CONFIG_HOME="$scratch/home/xdg-config"
unset KOTOYOSE_CHILD_HOME KOTOYOSE_CHILD_PYTHON
mkdir -p "$HOME"
# Setup opens Settings at the end, and Wine hands URLs to the host's xdg-open. Record them instead,
# so the smoke test never launches a browser or another app on the build machine.
mkdir -p "$scratch/host-bin"
cat > "$scratch/host-bin/xdg-open" <<'OPEN'
#!/bin/sh
printf '%s\n' "$*" >> "${0%/*}/opened-urls"
OPEN
chmod +x "$scratch/host-bin/xdg-open"
export PATH="$scratch/host-bin:$PATH" BROWSER="$scratch/host-bin/xdg-open"
cleanup() {
    wineserver -k || true
    wineserver -w || true
    rm -rf -- "$scratch"
}
trap cleanup EXIT

wait_for_output() {
    for (( attempt = 0; attempt < 150; attempt++ )); do
        [[ ! -s "$1" ]] || return 0
        sleep 0.1
    done
    echo "Process did not write to $1" >&2
    return 1
}

wait_for_exit() {
    # The uninstaller's temporary copy can spend 20 seconds closing an old app.
    for (( attempt = 0; attempt < 300; attempt++ )); do
        kill -0 "$1" 2>/dev/null || return 0
        sleep 0.1
    done
    echo "Process $1 did not exit" >&2
    return 1
}

wait_for_settings() {
    for (( attempt = 0; attempt < 150; attempt++ )); do
        if find "$WINEPREFIX/drive_c/users" -path '*/kotoyose-child/settings.port' -print -quit | grep -q .; then
            return 0
        fi
        sleep 0.1
    done
    echo 'Setup did not reopen Settings.' >&2
    return 1
}

# NSIS requires /D to be last and the path to be passed without literal quotes.
custom='C:\Program Choices\Kotoyose Test'
expected_exe="$custom\\$version\bin\kotoyose-child-background.exe"
install_root="$WINEPREFIX/drive_c/Program Choices/Kotoyose Test"
mkdir -p "$install_root"
touch "$install_root/keep-me.txt"
cp -- "$exe" "$WINEPREFIX/drive_c/kotoyose-setup.exe"
wine cmd.exe /c "start /wait C:\kotoyose-setup.exe /S /D=$custom"
installed="$install_root/$version"
test -f "$installed/bin/kotoyose-child-background.exe"
test -f "$installed/python/kotoyose-ready"
test -f "$install_root/Uninstall.exe"
test -f "$installed/bin/parts/requirements-claude.txt"
test -f "$installed/bin/parts/requirements-codex.txt"
test -f "$installed/bin/child-build-id"
wait_for_settings
wine "$installed/bin/kotoyose-child-app.exe" quit
repair_log=$(find "$WINEPREFIX/drive_c/users" -path '*/kotoyose-child/logs/repair.log' -print -quit)
test -n "$repair_log"
grep -F 'pip: exit code: 0' "$repair_log"
grep -F 'The runtime is ready.' "$repair_log"
wine "$installed/bin/kotoyose-child-app.exe" --version | tr -d '\r' | grep -Fx "kotoyose-child-app $version"
wine "$installed/python/python.exe" -I -c \
    'from importlib.metadata import version; from importlib.util import find_spec; from kotoyose.child_runner import Runner; import sys; assert version("kotoyose") == sys.argv[1]; assert find_spec("claude_agent_sdk") is None; assert find_spec("openai_codex") is None' \
    "$version"
wine "$installed/python/python.exe" -I -m kotoyose.child_runner --home "$custom" --components availability \
    | python3 -c 'import json, sys; rows = {row["id"]: row for row in json.load(sys.stdin)}; assert rows["claude"]["turns_supported"]; assert rows["codex"]["turns_supported"]; assert not any(row["sdk_importable"] or row["bundled_cli"] for row in rows.values())'
# Open the installed settings UI and log in with a fake CLI that requires console handles.
mkdir -p "$WINEPREFIX/drive_c/kotoyose-smoke-child" "$WINEPREFIX/drive_c/kotoyose-fake-bin"
printf '%s\n' '{"claude_cli":"system"}' > "$WINEPREFIX/drive_c/kotoyose-smoke-child/app.json"
x86_64-w64-mingw32-gcc -Wall -Wextra -Werror "$root/tests/fixtures/child_login_windows.c" \
    -o "$WINEPREFIX/drive_c/kotoyose-fake-bin/claude.exe"
KOTOYOSE_CHILD_HOME='C:\kotoyose-smoke-child' WINEPATH='C:\kotoyose-fake-bin' \
    wine "$installed/bin/kotoyose-child-app.exe" ui > "$scratch/settings-url" &
ui_pid=$!
python3 - "$scratch/settings-url" "$WINEPREFIX/drive_c/kotoyose-fake-bin" "$ui_pid" <<'PYUI'
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.parse import urlsplit
from urllib.request import Request, build_opener, ProxyHandler

settings_url = Path(sys.argv[1])
for _ in range(150):
    source = settings_url.read_text().strip() if settings_url.exists() else ""
    if source.startswith("http://127.0.0.1:"):
        break
    time.sleep(0.1)
else:
    raise SystemExit("Settings did not open")

url = urlsplit(source)
origin = f"http://{url.netloc}"
opener = build_opener(ProxyHandler({}))
headers = {"Origin": origin, "Content-Type": "application/json"}

def request(path, payload=None, method=None):
    body = json.dumps(payload).encode() if payload is not None else None
    with opener.open(Request(origin + path, data=body, headers=headers, method=method), timeout=20) as response:
        return response.read().decode()

html = request("/")
assert re.findall(r'data-page="([^"]+)"', html) == ["providers", "workspace", "settings", "connection"]
assert 'id="connection-enrollment"' in html and 'id="codex-sandbox"' in html
assert request("/providers.js")
session = json.loads(request("/api/session", {"code": url.fragment}))
headers["Authorization"] = f"Bearer {session['token']}"
state = json.loads(request("/api/status"))
assert not state["enrolled"] and state["link"]["connection"] == "unregistered"

checked = json.loads(request("/api/login/status/claude", {}))
assert checked == {"state": "logged_out"}, checked
assert json.loads(request("/api/login/claude", {})) == {"state": "running"}
for _ in range(150):
    view = json.loads(request("/api/login/claude"))
    if view["state"] != "running":
        break
    time.sleep(0.1)
assert view == {"state": "logged_in"}, view

fake_bin = Path(sys.argv[2])
(fake_bin / "hold").touch()
marker = fake_bin / "pids"

def start_held_login():
    marker.unlink(missing_ok=True)
    assert json.loads(request("/api/login/claude", {})) == {"state": "running"}
    for _ in range(150):
        if marker.exists() and len(pids := marker.read_text().split()) == 2:
            return pids
        time.sleep(0.1)
    raise AssertionError("Console CLI did not start its descendant")

def assert_stopped(pids):
    for pid in pids:
        subprocess.run(["wine", str(fake_bin / "claude.exe"), "--stopped", pid], check=True, timeout=15)

pids = start_held_login()
assert json.loads(request("/api/login/claude", method="DELETE")) == {"state": "cancelled"}
assert_stopped(pids)

pids = start_held_login()
os.kill(int(sys.argv[3]), signal.SIGKILL)
assert_stopped(pids)
PYUI
if wait "$ui_pid"; then
    echo 'The link app should have been forcibly stopped.' >&2
    exit 1
fi
echo 'Wine local console login, cancellation, and forced-exit cleanup smoke passed.'

wine reg query 'HKCU\Software\Microsoft\Windows\CurrentVersion\Run' /v KotoyoseChild \
    | grep -F "$expected_exe"
wine reg query 'HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\KotoyoseChild' /v UninstallString \
    | grep -F "$custom\Uninstall.exe"
shortcut=$(find "$WINEPREFIX/drive_c/users" -name 'Kotoyose Child.lnk' -print -quit)
test -n "$shortcut"
python3 - "$shortcut" "$expected_exe" <<'PY'
from pathlib import Path
import sys

raw = Path(sys.argv[1]).read_bytes()
target = sys.argv[2]
assert any(target.encode(codec) in raw for codec in ("ascii", "utf-16le"))
PY

# A previous version directory with Claude metadata must be reinstalled or marked for Repair.
old="$install_root/0.0.1"
mkdir -p "$old/python/Lib/site-packages/claude_agent_sdk-9.9.0.dist-info"
mkdir -p "$old/bin"
: > "$old/bin/kotoyose-child-app.exe"
mkdir -p "$install_root/notes"
echo keep > "$install_root/notes/keep.txt"
arp='HKCU\Software\Microsoft\Windows\CurrentVersion\Uninstall\KotoyoseChild'
wine reg delete "$arp" /v InstalledBuildDirectory /f
wine reg add "$arp" /v DisplayVersion /d 0.0.1 /f
wine "$installed/bin/kotoyose-child-app.exe" ui > "$scratch/upgrade-url" &
upgrade_pid=$!
wait_for_output "$scratch/upgrade-url"
grep -q '^http://127\.0\.0\.1:' "$scratch/upgrade-url"
endpoint=$(find "$WINEPREFIX/drive_c/users" -path '*/kotoyose-child/instance.endpoint' -print -quit)
test -f "$endpoint"
# This is the same setup mode used by Settings. Keep the previous app and runtime intact.
wine cmd.exe /c "start /wait C:\kotoyose-setup.exe /S /UPDATE"
wait_for_exit "$upgrade_pid"
wait "$upgrade_pid"
wait_for_settings
build_id=$(cat "$installed/bin/child-build-id")
updated="$install_root/$version-$build_id"
test -f "$installed/python/kotoyose-ready"
test -f "$updated/python/kotoyose-ready"
test -f "$old/bin/kotoyose-child-app.exe"
test -f "$updated/python/claude.needs-reinstall"
wine reg query "$arp" /v InstalledBuildDirectory | grep -F "$version-$build_id"
wine "$updated/bin/kotoyose-child-app.exe" quit
test ! -e "$endpoint"
echo 'Wine silent update kept the previous app, recorded provider repair, and reopened Settings.'

# start /wait under Wine does not surface the installer exit code, so check the files.
wine cmd.exe /c "start /wait C:\kotoyose-setup.exe /S /D=C:\." || true
test ! -e "$WINEPREFIX/drive_c/$version/kotoyose-child.install"
wine cmd.exe /c "start /wait C:\kotoyose-setup.exe /S /D=C:\Program Choices\Somewhere Else" || true
test ! -e "$WINEPREFIX/drive_c/Program Choices/Somewhere Else/$version/bin/kotoyose-child-app.exe"
test -f "$installed/bin/kotoyose-child-background.exe"

# Simulate a pre-quit background app and its runtime. An identical image name
# outside the install root must survive the uninstaller's fallback.
fixture="$WINEPREFIX/drive_c/kotoyose-fake-bin/claude.exe"
outside="$WINEPREFIX/drive_c/kotoyose-fake-bin/kotoyose-child-background.exe"
cp -- "$fixture" "$old/bin/kotoyose-child-background.exe"
cp -- "$fixture" "$old/python/python.exe"
touch "$old/kotoyose-child.install"
cp -- "$fixture" "$outside"
wine reg add "$arp" /v InstalledBuildDirectory /d 0.0.1 /f
wine "$old/bin/kotoyose-child-background.exe" --hold-lock > "$scratch/legacy-ready" &
legacy_pid=$!
wine "$old/python/python.exe" --hold > "$scratch/python-ready" &
python_pid=$!
wine "$outside" --hold > "$scratch/outside-ready" &
outside_pid=$!
wait_for_output "$scratch/legacy-ready"
wait_for_output "$scratch/python-ready"
wait_for_output "$scratch/outside-ready"
wine "$install_root/Uninstall.exe" /S
wait_for_exit "$legacy_pid"
wait_for_exit "$python_pid"
# Both fixtures sleep indefinitely after reporting readiness. Wine's launcher
# can return zero when another Windows process calls TerminateProcess.
wait "$legacy_pid" || true
wait "$python_pid" || true
kill -0 "$outside_pid"
kill -KILL "$outside_pid"
wait "$outside_pid" || true
for (( attempt = 0; attempt < 100; attempt++ )); do
    [[ -d "$installed" || -d "$updated" || -d "$old" ]] || break
    sleep 0.1
done
test ! -d "$installed"
test ! -d "$updated"
test ! -d "$old"
test -f "$install_root/keep-me.txt"
test -f "$install_root/notes/keep.txt"
wineserver -w
if wine reg query 'HKCU\Software\Microsoft\Windows\CurrentVersion\Run' /v KotoyoseChild >/dev/null 2>&1; then
    echo 'Start at sign-in remained after uninstall.' >&2
    exit 1
fi
echo 'Wine custom-directory installer and uninstall smoke test passed.'
