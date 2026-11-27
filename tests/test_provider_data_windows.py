"""C4 Windows data-provider contracts with pinned fakes only."""

import asyncio
from contextlib import ExitStack, contextmanager
import ctypes
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import subprocess
import sys
from textwrap import dedent
import threading
from types import SimpleNamespace

import pytest

from kotoyose import cli_install_spec, provider_data_child, provider_data_runtime, provider_data_storage, provider_data_windows
from kotoyose.child_protocol import Frame, PROVIDER_DATA_MCP_FEATURE, PROVIDER_DATA_UNRESTRICTED_FEATURE, REPOSITORY_SKILLS_FEATURE
from kotoyose.child_turn import ChildTurnError, ChildTurns, _prepare
from kotoyose.config import Client, HarnessConfig, ModelPreset
from kotoyose.execution_resources import agent_profile_allowed, authenticate_client
from kotoyose.print_cli_grok_profile import GROK_ACP_UNRESTRICTED_ARGV, GROK_INTEGRATION_ENV
from kotoyose.provider_data import UNRESTRICTED, parse_declaration, supports_capability
from kotoyose.runtime.child import declared_turn_capability

_DOCUMENT = Path(__file__).resolve().parents[1] / "examples" / "provider-declarations" / "grok-build.json"
_FINGERPRINT = "ab" * 32
_ALIAS = {
    "route": "grok", "on": "child:box", "agent": {"place": "host", "surface": "write"}, "allow": {"full": True},
}


def _document():
    return json.loads(_DOCUMENT.read_bytes())


def _parse(body):
    return parse_declaration(json.dumps(body).encode())


def test_unrestricted_is_a_separate_windows_capability():
    row = _parse(_document())
    target = row.platform("windows-x64")
    assert target.binary == "grok.exe"
    assert target.artifact.digest == "sha256:24384a5a03d61d2c45416ef3d535bff5379fde4844b87a52c1bb882686e39469"
    assert target.artifact.url_templates == (
        "https://x.ai/cli/grok-1.0.89-windows-x86_64.exe",
        "https://storage.googleapis.com/grok-build-public-artifacts/cli/grok-1.0.89-windows-x86_64.exe",
    )
    assert target.supports(UNRESTRICTED)
    assert row.commands("windows-x64")[-1]["argv"] == ["grok.exe", *GROK_ACP_UNRESTRICTED_ARGV]
    for capability in ("read", "write", "mcp"):
        assert not target.supports(capability)
        assert not supports_capability(capability, UNRESTRICTED)
    assert not row.platform("linux-x64").supports(UNRESTRICTED)


@pytest.mark.parametrize(("platform", "capability"), [
    ("windows-x64", "read"), ("windows-x64", "write"), ("windows-x64", "mcp"),
    ("linux-x64", UNRESTRICTED), ("darwin-x64", UNRESTRICTED), ("windows-arm64", UNRESTRICTED),
])
def test_declaration_rejects_cross_platform_capabilities(platform, capability):
    body = _document()
    body["platforms"][platform] = {**body["platforms"]["windows-x64"], "max_capability": capability}
    with pytest.raises(ValueError, match="capability"):
        _parse(body)


@pytest.mark.parametrize("invalid", ["argv", "guard", "isolation", "login", "status", "driver", "device"])
def test_windows_requires_validated_grok_contract(invalid):
    body = _document()
    body.pop("native_skill_layout")
    del body["platforms"]["linux-x64"]
    del body["acp"]["argv"]
    del body["acp"]["write_argv"]
    assert _parse(body).supports(UNRESTRICTED)
    match = {
        "argv": "unsupported declaration arguments", "guard": "mcp_guard", "isolation": "Grok isolation",
        "login": "device login", "status": "models status", "driver": "requires validated ACP", "device": "ordinary filename",
    }[invalid]
    if invalid == "argv":
        body["acp"]["unrestricted_argv"] = ["--sandbox", "strict", *GROK_ACP_UNRESTRICTED_ARGV]
    if invalid == "guard":
        del body["acp"]["mcp_guard"]
    if invalid == "isolation":
        del body["isolation"]
    if invalid == "login":
        body["operations"]["login"] = {"argv": ["login"], "timeout_seconds": 60}
    if invalid == "status":
        body["operations"]["status"] = {"argv": ["status"], "timeout_seconds": 8}
    if invalid == "driver":
        del body["acp"]
        del body["isolation"]
        body.update(driver={"id": "cli.print", "contract_version": 1}, print={"argv": ["--", "{prompt}"], "placeholders": ["prompt"]})
    if invalid == "device":
        body["platforms"]["windows-x64"]["install"]["binary"] = "nul.exe"
    with pytest.raises(ValueError, match=match):
        _parse(body)


@pytest.fixture
def windows_child(tmp_path, monkeypatch):
    profile = tmp_path / "profile"
    local = profile / "AppData" / "Local"
    home = local / "kotoyose-child"
    home.mkdir(parents=True)
    (profile / ".grok").mkdir()
    (home / "host.json").write_text(json.dumps({"fingerprint": _FINGERPRINT}))
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("SYSTEMROOT", "C:\\Windows")
    for module in (provider_data_child, provider_data_storage, provider_data_runtime):
        monkeypatch.setattr(module, "_windows", lambda: True)
    monkeypatch.setattr(cli_install_spec, "platform_key", lambda: "windows-x64")
    monkeypatch.setattr(provider_data_runtime, "platform_key", cli_install_spec.platform_key)
    folders = {provider_data_windows._PROFILE: profile, provider_data_windows._LOCAL_APP_DATA: local}
    monkeypatch.setattr(provider_data_windows, "_known_folder", folders.__getitem__)
    monkeypatch.setattr(provider_data_windows, "require_user_owner", lambda path, **_: None)
    locks = []
    monkeypatch.setitem(sys.modules, "msvcrt", SimpleNamespace(
        LK_LOCK=1, LK_UNLCK=2, LK_NBLCK=3, locking=lambda fd, mode, size: locks.append((mode, size)),
    ))
    return home, profile, locks


@pytest.mark.parametrize("key", ["USERPROFILE", "LOCALAPPDATA"])
def test_windows_environment_cannot_redirect_known_folders(windows_child, monkeypatch, key):
    home, profile, _ = windows_child
    expected = profile if key == "USERPROFILE" else home.parent
    monkeypatch.setenv(key, str(expected.parent).upper() + f"\\unused\\..\\{expected.name.upper()}")
    assert provider_data_windows.storage_directory(home) == home
    assert provider_data_runtime.data_env()[key] == str(expected)
    monkeypatch.delenv(key)
    assert provider_data_runtime.data_env()[key] == str(expected)
    monkeypatch.setenv(key, str(expected.with_name("wrong")))
    with pytest.raises(ValueError, match=rf"{key} must match"):
        provider_data_windows.storage_directory(home)


@pytest.mark.parametrize(("guid", "status"), [
    ("5e6c858f-0e22-4760-9afe-ea3317b67173", 0), ("f1b32785-6fba-4fcf-9d55-7b8e7f157091", -2147024891),
])
def test_known_folder_uses_windows_api_and_frees_its_buffer(monkeypatch, guid, status):
    from uuid import UUID

    expected = r"C:\Users\Account"
    freed = []

    def lookup(folder, flags, token, output):
        assert ctypes.string_at(folder, 16) == UUID(guid).bytes_le
        assert flags == 0 and token is None
        output._obj.value = expected
        return status

    def free(value):
        freed.append(value.value)

    dlls = {
        "shell32": SimpleNamespace(SHGetKnownFolderPath=lookup),
        "ole32": SimpleNamespace(CoTaskMemFree=free),
    }
    monkeypatch.setattr(ctypes, "WinDLL", dlls.__getitem__, raising=False)
    if status == 0:
        assert provider_data_windows._known_folder(provider_data_windows._PROFILE) == Path(expected)
    else:
        with pytest.raises(OSError, match="80070005"):
            provider_data_windows._known_folder(provider_data_windows._LOCAL_APP_DATA)
    assert freed == [expected]


def test_windows_grok_home_requires_private_acl(windows_child, monkeypatch):
    _, profile, _ = windows_child

    def reject(path, *, private=False, trusted_owner=False):
        if path == profile / ".grok" and private:
            raise ValueError("unsafe ACL")

    monkeypatch.setattr(provider_data_windows, "require_user_owner", reject)
    with pytest.raises(ValueError, match="unsafe ACL"):
        provider_data_windows.real_grok_home()


_REAL_REQUIRE_USER_OWNER = provider_data_windows.require_user_owner
_USER_SID = "S-1-5-21-1-2-3-1000"
_OTHER_SID = "S-1-5-21-1-2-3-1002"
_SANDBOX_SID = "S-1-5-21-9-9-9-1001"
_UNNAMED_SID = "S-1-5-99-7"
_SYSTEM_SID = "S-1-5-18"
_ADMIN_SID = "S-1-5-32-544"
_FILE_ALL = 0x1F01FF
_PROTECTED_DACL = 0x80000004


def _sid_bytes(text):
    parts = text.split("-")
    revision, authority, subs = int(parts[1]), int(parts[2]), [int(part) for part in parts[3:]]
    raw = bytearray(8 + 4 * len(subs))
    raw[0] = revision
    raw[1] = len(subs)
    raw[2:8] = authority.to_bytes(6, "big")
    for index, sub in enumerate(subs):
        raw[8 + 4 * index:12 + 4 * index] = sub.to_bytes(4, "little")
    return bytes(raw)


def _ace(flags, sid):
    body = _sid_bytes(sid)
    raw = bytearray(8 + len(body))
    raw[1] = flags
    raw[2:4] = len(raw).to_bytes(2, "little")
    raw[4:8] = _FILE_ALL.to_bytes(4, "little")
    raw[8:] = body
    return bytes(raw)


def _acl(aces):
    body = b"".join(aces)
    raw = bytearray(8 + len(body))
    raw[0] = 2
    raw[2:4] = len(raw).to_bytes(2, "little")
    raw[4:6] = len(aces).to_bytes(2, "little")
    raw[8:] = body
    return ctypes.create_string_buffer(bytes(raw))


def _sid_text(address):
    count = ctypes.c_ubyte.from_address(address + 1).value
    raw = ctypes.string_at(address, 8 + 4 * count)
    authority = int.from_bytes(raw[2:8], "big")
    subs = [str(int.from_bytes(raw[8 + 4 * index:12 + 4 * index], "little")) for index in range(count)]
    return "-".join(("S", str(raw[0]), str(authority), *subs))


def _pointer(value):
    if isinstance(value, int):
        return value
    if isinstance(value, ctypes.c_void_p):
        return value.value or 0
    try:
        return ctypes.addressof(value)
    except TypeError:
        return ctypes.cast(value, ctypes.c_void_p).value or 0


class _SecurityFake:
    """Just enough of advapi32 for the child-home ACL owner to run on Linux."""

    def __init__(self, acls, owners=None, *, stick=False):
        self.acls = {Path(path): buffer for path, buffer in acls.items()}
        self.owners = {Path(path): sid for path, sid in (owners or {}).items()}
        self.stick = stick
        self.sets = []
        self._sid_buffers = {}
        user = ctypes.create_string_buffer(_sid_bytes(_USER_SID))
        self._user = user
        token = ctypes.create_string_buffer(ctypes.sizeof(ctypes.c_void_p) + 8)
        ctypes.c_void_p.from_buffer(token).value = ctypes.addressof(user)
        self._token = token

    def _owner(self, path):
        sid = self.owners.get(Path(path), _USER_SID)
        return self._sid(sid)

    def _sid(self, text):
        buffer = self._sid_buffers.get(text)
        if buffer is None:
            buffer = ctypes.create_string_buffer(_sid_bytes(text))
            self._sid_buffers[text] = buffer
        return buffer

    def _acl(self, path):
        path = Path(path)
        buffer = self.acls.get(path)
        if buffer is None:
            buffer = _private_acl()
            self.acls[path] = buffer
        return buffer

    def api(self):
        return SimpleNamespace(
            OpenProcessToken=self._open_token,
            GetTokenInformation=self._token_info,
            ConvertSidToStringSidW=self._sid_string,
            ConvertStringSidToSidW=self._sid_string_to_sid,
            GetNamedSecurityInfoW=self._get_security,
            SetNamedSecurityInfoW=self._set_security,
            GetAce=self._get_ace,
            GetLengthSid=self._sid_length,
            CopySid=self._copy_sid,
            InitializeAcl=self._initialize_acl,
            AddAccessAllowedAceEx=self._add_ace,
            LookupAccountSidW=self._lookup,
        ), SimpleNamespace(
            GetCurrentProcess=lambda: 1,
            CloseHandle=lambda handle: None,
            LocalFree=lambda value: None,
        )

    def _open_token(self, process, access, out):
        out._obj.value = 1
        return 1

    def _token_info(self, handle, kind, data, size, out):
        out._obj.value = len(self._token)
        if data is None:
            return 0
        ctypes.memmove(_pointer(data), self._token, len(self._token))
        return 1

    def _sid_string(self, sid, out):
        out._obj.value = _sid_text(_pointer(sid))
        return 1

    def _sid_string_to_sid(self, text, out):
        out._obj.value = ctypes.addressof(self._sid(text))
        return 1

    def _get_security(self, name, kind, flags, owner, group, dacl, sacl, descriptor):
        owner._obj.value = ctypes.addressof(self._owner(name))
        if dacl is not None:
            dacl._obj.value = ctypes.addressof(self._acl(name))
        descriptor._obj.value = 1
        return 0

    def _set_security(self, name, kind, flags, owner, group, dacl, sacl):
        path = Path(name)
        self.sets.append((path, flags))
        if not self.stick:
            size = ctypes.c_ushort.from_address(_pointer(dacl) + 2).value
            self.acls[path] = ctypes.create_string_buffer(ctypes.string_at(_pointer(dacl), size))
        return 0

    def _get_ace(self, acl, index, out):
        base = _pointer(acl)
        offset = 8
        for current in range(index + 1):
            ace = base + offset
            if current == index:
                out._obj.value = ace
                return 1
            offset += ctypes.c_ushort.from_address(ace + 2).value
        return 0

    def _sid_length(self, sid):
        return 8 + 4 * ctypes.c_ubyte.from_address(_pointer(sid) + 1).value

    def _copy_sid(self, size, dest, source):
        ctypes.memmove(_pointer(dest), _pointer(source), self._sid_length(source))
        return 1

    def _initialize_acl(self, acl, size, revision):
        base = _pointer(acl)
        ctypes.memset(base, 0, size)
        ctypes.c_ubyte.from_address(base).value = revision
        ctypes.c_ushort.from_address(base + 2).value = size
        return 1

    def _add_ace(self, acl, revision, flags, mask, sid):
        base = _pointer(acl)
        count = ctypes.c_ushort.from_address(base + 4).value
        capacity = ctypes.c_ushort.from_address(base + 2).value
        offset = 8
        for _ in range(count):
            offset += ctypes.c_ushort.from_address(base + offset + 2).value
        sid_raw = ctypes.string_at(_pointer(sid), self._sid_length(sid))
        ace_size = 8 + len(sid_raw)
        if offset + ace_size > capacity:
            return 0
        ace = base + offset
        ctypes.c_ubyte.from_address(ace + 1).value = flags
        ctypes.c_ushort.from_address(ace + 2).value = ace_size
        ctypes.c_uint32.from_address(ace + 4).value = mask
        ctypes.memmove(ace + 8, sid_raw, len(sid_raw))
        ctypes.c_ushort.from_address(base + 4).value = count + 1
        return 1

    def _lookup(self, system, sid, name, name_size, domain, domain_size, use):
        if _sid_text(_pointer(sid)) != _SANDBOX_SID:
            return 0
        name.value = "CodexSandboxUsers"
        domain.value = "GOMWAICHI14TH"
        return 1


def _private_acl(*extra):
    aces = [_ace(0x03, sid) for sid in (_USER_SID, _SYSTEM_SID, _ADMIN_SID)]
    aces.extend(_ace(0x10, sid) for sid in extra)
    return _acl(aces)


def _use_security(monkeypatch, fake):
    monkeypatch.setattr(provider_data_windows, "require_user_owner", _REAL_REQUIRE_USER_OWNER)
    monkeypatch.setattr(provider_data_windows, "_security_api", fake.api)


def test_child_home_replaces_inherited_acl_entries_and_leaves_the_profile_alone(windows_child, monkeypatch, caplog):
    home, profile, _ = windows_child
    local = home.parent
    nested = home / "provider-declarations"
    nested.mkdir()
    (nested / "row.json").write_text("{}", encoding="utf-8")
    (home / "kept").write_text("x", encoding="utf-8")
    (home / "jump").symlink_to(profile / ".grok", target_is_directory=True)
    fake = _SecurityFake({
        profile: _private_acl(),
        local: _private_acl(_SANDBOX_SID, _UNNAMED_SID),
        profile / ".grok": _private_acl(),
        home: _private_acl(_SANDBOX_SID, _UNNAMED_SID),
    })
    _use_security(monkeypatch, fake)
    with caplog.at_level(logging.WARNING, logger="kotoyose.provider_data_windows"):
        assert provider_data_windows.storage_directory(home) == home
    replaced = {path for path, _flags in fake.sets}
    assert home in replaced and nested in replaced and nested / "row.json" in replaced and home / "kept" in replaced
    assert local not in replaced and profile not in replaced and profile / ".grok" not in replaced
    assert home / "jump" not in replaced
    assert all(flags == _PROTECTED_DACL for _path, flags in fake.sets)
    base = ctypes.addressof(fake.acls[home])
    assert ctypes.c_ushort.from_address(base + 4).value == 3
    offset = 8
    seen = []
    for _ in range(3):
        assert ctypes.c_ubyte.from_address(base + offset + 1).value == 0x03
        seen.append(_sid_text(base + offset + 8))
        offset += ctypes.c_ushort.from_address(base + offset + 2).value
    assert seen == [_USER_SID, _SYSTEM_SID, _ADMIN_SID]
    line = next(record.message for record in caplog.records if "removed" in record.message)
    assert "CodexSandboxUsers" in line and _UNNAMED_SID in line
    assert str(home) not in line and "kotoyose" not in line


def test_child_home_acl_reset_fails_closed_when_the_replacement_is_still_open(windows_child, monkeypatch):
    home, profile, _ = windows_child
    fake = _SecurityFake({
        profile: _private_acl(), home.parent: _private_acl(), home: _private_acl(_SANDBOX_SID),
    }, stick=True)
    _use_security(monkeypatch, fake)
    with pytest.raises(ValueError, match="outside the current user"):
        provider_data_windows.storage_directory(home)
    assert any(path == home and flags == _PROTECTED_DACL for path, flags in fake.sets)


def test_child_home_acl_reset_refuses_another_owners_directory(windows_child, monkeypatch):
    home, profile, _ = windows_child
    fake = _SecurityFake({
        profile: _private_acl(), home.parent: _private_acl(), home: _private_acl(_SANDBOX_SID),
    }, owners={home: _OTHER_SID})
    _use_security(monkeypatch, fake)
    with pytest.raises(ValueError, match="owned by the current user"):
        provider_data_windows.storage_directory(home)
    assert fake.sets == []


def test_child_home_acl_reset_refuses_a_reparse_point(windows_child, monkeypatch):
    home, profile, _ = windows_child
    target = home.with_name("real-child")
    home.rename(target)
    home.symlink_to(target, target_is_directory=True)
    fake = _SecurityFake({profile: _private_acl(), home.parent: _private_acl(), target: _private_acl(_SANDBOX_SID)})
    _use_security(monkeypatch, fake)
    with pytest.raises(ValueError, match="without reparse points"):
        provider_data_windows.storage_directory(home)
    assert fake.sets == []


def test_grok_home_with_extra_aces_is_not_rewritten(windows_child, monkeypatch):
    home, profile, _ = windows_child
    grok = profile / ".grok"
    fake = _SecurityFake({
        profile: _private_acl(), home.parent: _private_acl(), grok: _private_acl(_SANDBOX_SID),
    })
    _use_security(monkeypatch, fake)
    with pytest.raises(ValueError, match="outside the current user"):
        provider_data_windows.real_grok_home()
    assert fake.sets == []


def _offer(home, row):
    provider_data_child.store_offers(home, _FINGERPRINT, [row.offer()])


def _approve(home, row):
    return provider_data_child.approve(home, row.id, sha256=row.sha256, capability=UNRESTRICTED, confirm_unrestricted=True)


def test_approval_is_confirmed_and_atomically_replaced(windows_child, monkeypatch):
    home, _, locks = windows_child
    body = _document()
    row = _parse(body)
    _offer(home, row)
    for capability in ("read", "write", "mcp"):
        with pytest.raises(ValueError, match="not offered"):
            provider_data_child.approve(home, row.id, sha256=row.sha256, capability=capability)
    with pytest.raises(ValueError, match="explicit confirmation"):
        provider_data_child.validate_request({
            "action": "approve", "home": str(home), "id": row.id, "sha256": row.sha256,
            "capability": UNRESTRICTED, "confirm_unrestricted": "true",
        })
    first = _approve(home, row)
    record = home / "provider-declarations" / "approved" / f"{row.id}.json"
    original = record.read_bytes()
    with pytest.raises(ValueError, match="has not approved read"):
        provider_data_child.approved_for(home, row)
    body["identity"]["display_name"] += " Updated"
    update = _parse(body)
    _offer(home, update)
    assert provider_data_child.preview(home)["offers"][0]["status"] == "stale"
    def fail_before_replace(src, dst):
        assert src.parent == dst.parent
        assert dst.read_bytes() == original
        raise PermissionError("simulated atomic replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(Path, "replace", fail_before_replace)
        with pytest.raises(PermissionError, match="replace failure"):
            _approve(home, update)
    assert record.read_bytes() == original
    assert not list(record.parent.glob(".provider-*"))
    second = _approve(home, update)
    assert record.read_bytes() != original
    assert first.generation != second.generation
    provider_data_child.revoke(home, row.id)
    assert not record.exists()
    assert locks == [(1, 1), (2, 1)] * (len(locks) // 2)


@pytest.mark.parametrize("persistent", [False, True])
async def test_watch_approval_retries_windows_lock_errors_off_the_event_loop(windows_child, monkeypatch, persistent):
    home, _, _ = windows_child
    row = _parse(_document())
    _offer(home, row)
    approval = _approve(home, row)
    event_loop_thread = threading.get_ident()
    loop = asyncio.get_running_loop()
    checked = asyncio.Event()
    attempts, stopped = [], []

    def locking(fd, mode, size):
        assert threading.get_ident() != event_loop_thread
        if mode == 3:
            attempts.append(mode)
            if len(attempts) == 1 or persistent:
                raise OSError("busy lock")
        else:
            assert mode == 2
            loop.call_soon_threadsafe(checked.set)

    monkeypatch.setattr(sys.modules["msvcrt"], "locking", locking)
    monkeypatch.setattr(provider_data_child, "stop_process", lambda name, pid: stopped.append((name, pid)))
    watch = asyncio.create_task(provider_data_child.watch_approval(home, approval, 123))
    try:
        if persistent:
            await asyncio.wait_for(watch, 3)
            assert stopped == [(row.id, 123)]
            assert len(attempts) == 3
        else:
            await asyncio.wait_for(checked.wait(), 3)
            assert not stopped and not watch.done()
            assert len(attempts) == 2
    finally:
        watch.cancel()
        await asyncio.gather(watch, return_exceptions=True)


@pytest.mark.parametrize("target", ["child", "junction", "approval", "outside", "acl"])
def test_windows_storage_rejects_unsafe_paths(windows_child, tmp_path, monkeypatch, target):
    home, profile, _ = windows_child
    row = _parse(_document())
    _offer(home, row)
    directory = home / "provider-declarations"
    if target == "child":
        link = home.with_name("other-child")
        link.symlink_to(home, target_is_directory=True)
        with pytest.raises(ValueError, match="without reparse points"):
            _offer(link, row)
    elif target == "junction":
        original = Path.lstat
        monkeypatch.setattr(Path, "lstat", lambda path: SimpleNamespace(
            st_mode=stat.S_IFDIR, st_file_attributes=0x400,
        ) if path == directory else original(path))
        with pytest.raises(ValueError, match="without reparse points"):
            _offer(home, row)
    elif target == "approval":
        _approve(home, row)
        record = directory / "approved" / f"{row.id}.json"
        moved = record.with_name("other.json")
        record.rename(moved)
        record.symlink_to(moved)
        with pytest.raises(ValueError, match="without reparse points"):
            _approve(home, row)
        assert provider_data_child.approvals(home) == ()
    elif target == "outside":
        outside = profile / "other"
        outside.mkdir()
        with pytest.raises(ValueError, match="LOCALAPPDATA"):
            provider_data_storage.private_directory(outside)
    else:
        def reject(path, *, private=False, repair=False, trusted_owner=False):
            if path == home and private:
                raise ValueError("unsafe ACL")

        monkeypatch.setattr(provider_data_windows, "require_user_owner", reject)
        with pytest.raises(ValueError, match="unsafe ACL"):
            _approve(home, row)


def test_windows_acp_isolation_validates_real_grok_home_and_private_env(windows_child, monkeypatch, request):
    home, profile, _ = windows_child
    row = _parse(_document())
    for key in ("HOME", "APPDATA", "GROK_HOME", "CODEX_HOME", "XAI_API_KEY", "TMPDIR"):
        monkeypatch.setenv(key, "/ignored")
    # The shell tool needs these to start PowerShell and find tools (unrestricted runs with the user's rights).
    monkeypatch.setenv("PATH", r"C:\Windows\System32;C:\Program Files\Git\cmd")
    monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
    monkeypatch.setenv("PATHEXT", ".COM;.EXE;.BAT;.CMD")
    leases = ExitStack()
    request.addfinalizer(leases.close)
    env = provider_data_runtime.acp_data_env(row, home, leases)
    assert env["PATH"] == r"C:\Windows\System32;C:\Program Files\Git\cmd"
    assert env["COMSPEC"] == r"C:\Windows\System32\cmd.exe" and env["PATHEXT"] == ".COM;.EXE;.BAT;.CMD"
    private = Path(env["HOME"])
    assert private.parent == home / "provider-data" / row.id
    assert env["HOME"] == env["USERPROFILE"] == str(private)
    assert env["APPDATA"] == str(private / "AppData" / "Roaming")
    assert env["LOCALAPPDATA"] == str(private / "AppData" / "Local")
    assert env["GROK_HOME"] == str(profile / ".grok")
    assert all(Path(env[key]).is_dir() for key in ("APPDATA", "LOCALAPPDATA", "TEMP"))
    assert dict(GROK_INTEGRATION_ENV).items() <= env.items()
    assert not {"TMPDIR", "CODEX_HOME", "XAI_API_KEY"} & env.keys()
    with pytest.raises(ValueError, match="child-managed isolation"):
        provider_data_runtime.acp_data_env(row, None, leases)
    grok = profile / ".grok"
    grok.rmdir()
    with pytest.raises(ValueError, match="private USERPROFILE"):
        provider_data_runtime.acp_data_env(row, home, leases)
    grok.symlink_to(private, target_is_directory=True)
    with pytest.raises(ValueError, match="without reparse points"):
        provider_data_runtime.acp_data_env(row, home, leases)


@pytest.mark.parametrize(("change", "error"), [
    ({}, None), ({"features": set()}, PROVIDER_DATA_UNRESTRICTED_FEATURE),
    ({"features": set(), "platform": None}, "has not advertised its platform"),
    ({"providers": []}, "has not advertised unrestricted approval"),
    ({"capability": "mcp"}, "has not advertised unrestricted approval"),
    ({"wrong_hash": True}, "has not advertised unrestricted approval"),
    ({"surface": "read"}, "agent.surface"), ({"full": False}, "allow.full"),
    ({"on": "local"}, "on: child"), ({"on": "child:other"}, "must target"),
    ({"driver": "cli.print"}, "cli.acp"),
    ({"platform": "linux-x64", "features": {PROVIDER_DATA_MCP_FEATURE}}, None),
])
def test_host_dispatch_gates(change, error):
    row = _parse(_document())
    preset = ModelPreset.model_validate({
        **_ALIAS, "on": change.get("on", "child:box"), "allow": {"full": change.get("full", True)},
        "agent": {"place": "host", "surface": change.get("surface", "write")},
    })
    pins = [{"profile": row.profile_id, "provider": row.id, "driver": row.driver, "contract_version": row.contract_version,
             "sha256": "ff" * 32 if change.get("wrong_hash") else row.sha256, "capability": change.get("capability", UNRESTRICTED)}]
    args = dict(driver=change.get("driver", row.driver), name="box", platform=change.get("platform", "windows-x64"),
                features=change.get("features", {PROVIDER_DATA_UNRESTRICTED_FEATURE}), providers=change.get("providers", pins))
    if error:
        with pytest.raises(ValueError, match=error):
            declared_turn_capability(row, preset, **args)
    else:
        assert declared_turn_capability(row, preset, **args) == ("write" if args["platform"] == "linux-x64" else UNRESTRICTED)


def test_version_capture_drains_output_written_just_before_exit(windows_child, monkeypatch):
    payload = b"grok 1.0.89\n"
    read_fd, write_fd = os.pipe()
    with os.fdopen(read_fd, "rb") as source, os.fdopen(write_fd, "wb") as writer:
        writer.write(payload)
        writer.flush()
        readiness = iter((None, len(payload), 0))
        monkeypatch.setattr(provider_data_runtime, "_windows_pipe_ready", lambda fd: lambda wait: next(readiness))
        process = SimpleNamespace(stdout=source, poll=lambda: 0)
        assert provider_data_runtime._capture(process, 1) == payload


@pytest.mark.parametrize("allow_full", [False, True])
def test_unrestricted_alias_uses_existing_client_authority(allow_full):
    preset = ModelPreset.model_validate(_ALIAS)
    client = Client(models=["run"], places=["child:box"], max_surface="write", allow_full=allow_full)
    _, policy = authenticate_client("windows", clients={"windows": client})
    assert agent_profile_allowed("run", policy, config=SimpleNamespace(models={"run": preset}, pools={}, preset_for=lambda *_args, **_kw: preset)) == allow_full


def _turn(home, workspace, row, *, refs=()):
    (home / "policy.json").write_text(json.dumps({
        "workspace_roots": [str(workspace)], "adapters": [row.id],
        "mcp": {"files": {"command": "local-only", "args": ["serve"]}},
    }))
    return {
        "alias": "grok-win", "preset": {**_ALIAS, "cwd_allowed_roots": [str(workspace)], "mcp_refs": list(refs)},
        "route": {"driver": row.driver, "profile": row.profile_id, "connection": "win", "model": "fake-model"},
        "connection": {"provider": row.id, "profile": row.profile_id, "kind": "subscription", "on": "child:box", "data_policy": "no_training"},
        "declaration_sha256": row.sha256, "capability": UNRESTRICTED, "cwd": str(workspace), "prompt": {"text": "test"},
    }


@pytest.mark.parametrize(("gate", "error"), [
    ("approval", "unapproved"), ("cwd", "requires a cwd"), ("policy-root", "outside workspace_roots"),
    ("alias-root", "outside the alias cwd_allowed_roots"), ("full", "allow.full"), ("surface", "agent.surface"),
    ("placement", "on: child"), ("driver", "cli.acp"), ("capability", "capability must match"),
    ("mcp-name", "unknown MCP"), ("mcp-syntax", "MCP names"), ("builtin", "requires an approved unrestricted"),
    ("tools", "cannot set tools or deny"), ("deny", "cannot set tools or deny"),
])
def test_child_rechecks_windows_turn_requirements(windows_child, tmp_path, gate, error):
    home, _, _ = windows_child
    workspace = tmp_path / "work"
    workspace.mkdir()
    row = _parse(_document())
    _offer(home, row)
    _approve(home, row)
    payload = _turn(home, workspace, row)
    if gate == "approval":
        provider_data_child.revoke(home, row.id)
    elif gate == "cwd":
        payload["cwd"] = ""
    elif gate == "policy-root":
        payload["cwd"] = str(tmp_path)
    elif gate == "alias-root":
        payload["preset"]["cwd_allowed_roots"] = []
    elif gate == "full":
        payload["preset"]["allow"] = {"full": False}
    elif gate == "surface":
        payload["preset"]["agent"] = {"surface": "read"}
    elif gate == "placement":
        payload["preset"]["on"] = "local"
    elif gate == "driver":
        payload["route"]["driver"] = "cli.print"
    elif gate == "capability":
        payload["capability"] = "write"
    elif gate == "mcp-name":
        payload["preset"]["mcp_refs"] = ["unknown"]
    elif gate == "mcp-syntax":
        payload["preset"]["mcp_refs"] = ["name__collision"]
    elif gate == "builtin":
        payload["route"]["profile"] = "grok"
        payload.pop("declaration_sha256")
    elif gate in ("tools", "deny"):
        payload["preset"][gate] = [] if gate == "tools" else ["execute"]
    with provider_data_child.use_approved(home), pytest.raises(ChildTurnError, match=error):
        _prepare(home, payload)


@pytest.mark.parametrize(("field", "value"), [("tools", []), ("deny", ["execute"])])
def test_unrestricted_host_config_rejects_tool_limits(field, value):
    from kotoyose.print_cli_profiles import use_data_profiles

    row = _parse(_document())
    config = {
        "connections": {"c": {"kind": "subscription", "provider": row.id, "profile": row.profile_id,
                              "on": "child:box", "data_policy": "no_training"}},
        "routes": {"grok": {"driver": row.driver, "connection": "c", "profile": row.profile_id, "model": "fake"}},
        "models": {"win": _ALIAS},
    }
    with use_data_profiles((row.profile(),)):
        valid = HarnessConfig.model_validate(config)
        HarnessConfig.model_validate(valid.model_dump())
        config["models"]["win"] = {**_ALIAS, field: value}
        with pytest.raises(ValueError, match="cannot set tools or deny"):
            HarnessConfig.model_validate(config)


@pytest.mark.parametrize(("location", "error"), [
    ("profile", "OS account home"), ("parent", "OS account home"),
    ("child", "child home"), ("local", "child home"), ("child-descendant", "child home"),
    ("grok", "GROK_HOME"), ("grok-descendant", "GROK_HOME"),
])
def test_windows_child_rejects_sensitive_unrestricted_cwd(windows_child, location, error):
    home, profile, _ = windows_child
    row = _parse(_document())
    _offer(home, row)
    _approve(home, row)
    grok = profile / ".grok"
    cwd = {"profile": profile, "parent": profile.parent, "child": home, "local": home.parent,
           "child-descendant": home / "sub", "grok": grok, "grok-descendant": grok / "sub"}[location]
    cwd.mkdir(exist_ok=True)
    payload = _turn(home, cwd, row)
    with provider_data_child.use_approved(home), pytest.raises(ChildTurnError, match=error):
        _prepare(home, payload)


def _install_fake(home, payload, monkeypatch):
    body = _document()
    body["platforms"]["windows-x64"]["install"]["sha256"] = hashlib.sha256(payload).hexdigest()
    row = _parse(body)
    _offer(home, row)
    _approve(home, row)
    calls = []

    def check_version(binary, args, **options):
        calls.append(args)
        assert options["timeout"] == 15
        assert binary.name == "grok.exe"
        assert binary.read_bytes() == payload
        return subprocess.CompletedProcess(args, 0, b"grok 1.0.89\n", b"")

    monkeypatch.setattr(provider_data_runtime, "_run", check_version)
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        provider_data_runtime.install_bytes(row, b"wrong", child_home=home, source="fake")
    assert not calls
    installed = provider_data_runtime.install_bytes(row, payload, child_home=home, source="fake")
    assert calls == [("--version",)]
    assert provider_data_runtime.managed_binary(row, home) == installed
    return row, installed


@pytest.mark.parametrize("windows", [True, False])
def test_unrestricted_binary_is_read_only_and_is_rehashed_with_unchanged_metadata(windows_child, monkeypatch, windows):
    home, _, _ = windows_child
    row, path = _install_fake(home, b"fake", monkeypatch)
    info = path.stat()
    assert not info.st_mode & stat.S_IWRITE
    provider_data_runtime._cached_digest(path, info)
    path.chmod(stat.S_IREAD | stat.S_IWRITE | stat.S_IEXEC)
    path.write_bytes(b"edit")
    path.chmod(stat.S_IREAD | stat.S_IEXEC)
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    monkeypatch.setattr(provider_data_runtime, "_windows", lambda: windows)
    with pytest.raises(ValueError, match="integrity verification"):
        provider_data_runtime.managed_binary(row, home)


def test_read_only_is_best_effort_for_an_existing_windows_binary(windows_child, monkeypatch):
    home, _, _ = windows_child
    row, path = _install_fake(home, b"fake", monkeypatch)
    original = Path.chmod

    def chmod(target, mode):
        if target == path:
            raise OSError("attribute change refused")
        return original(target, mode)

    monkeypatch.setattr(Path, "chmod", chmod)
    assert provider_data_runtime.managed_binary(row, home) == path


@pytest.mark.parametrize("failure", ["cwd", "registration"])
async def test_unrestricted_acp_connect_rechecks_cwd_and_cleans_up_registration_failure(windows_child, tmp_path, monkeypatch, failure):
    from kotoyose import acp_client, child_process_tree
    from kotoyose.acp_turn import AcpPermissionPolicy
    from kotoyose.runtime.acp import AcpRuntimeSession

    home, profile, _ = windows_child
    row, binary = _install_fake(home, b"fake", monkeypatch)
    cwd = profile if failure == "cwd" else tmp_path / "work"
    cwd.mkdir(exist_ok=True)
    actions = []
    pids = {321, 322}
    monkeypatch.setattr(child_process_tree, "bound_windows_job", lambda: pids)
    monkeypatch.setattr(child_process_tree, "kill_pids", lambda tracked: actions.append(("kill job", tracked)))
    monkeypatch.setattr(acp_client, "os", SimpleNamespace(name="nt"))

    class Process:
        pid = 321
        stdin = None
        returncode = None

        def kill(self):
            actions.append("kill process")
            self.returncode = 1

        async def wait(self):
            return self.returncode

    async def spawn(*args, **kwargs):
        actions.append("spawn")
        proc = Process()
        proc.stdout = asyncio.StreamReader()
        proc.stdout.feed_eof()
        return acp_client.AcpClient(proc)

    def reject_registration(*args):
        raise OSError("registration failed")

    monkeypatch.setattr(acp_client.AcpClient, "spawn", spawn)
    monkeypatch.setattr(provider_data_runtime, "register_declared_process", reject_registration)
    session = AcpRuntimeSession(
        row.profile(), binary, profile, cwd, "fake-model", 1, "", [],
        AcpPermissionPolicy("write", "host", unrestricted_child=True), None,
    )
    token = child_process_tree.TURN_PIDS.set(pids)
    try:
        with provider_data_child.use_approved(home):
            if failure == "cwd":
                with pytest.raises(ValueError, match="OS account home"):
                    await session._connect()
                assert actions == []
            else:
                with pytest.raises(OSError, match="registration failed"):
                    await session._connect()
                assert actions == ["spawn", ("kill job", pids), "kill process"]
    finally:
        child_process_tree.TURN_PIDS.reset(token)
        await session.disconnect()


@pytest.mark.parametrize(("report", "mcp"), [
    ("empty", False), ("empty", True), ("missing", False), ("wrong-session", False), ("invalid", True),
    ("native", False), ("late-native", False),
])
async def test_windows_acp_guard_and_permissions(windows_child, tmp_path, monkeypatch, report, mcp):
    home, profile, _ = windows_child
    workspace = tmp_path / "work"
    workspace.mkdir()
    marker = workspace / "reached-prompt"
    row, binary = _install_fake(home, (f"#!{sys.executable}\n" + dedent(f"""
        import json, os, sys
        from pathlib import Path

        assert sys.argv[1:] == {list(item.replace("{model}", "fake-model") for item in GROK_ACP_UNRESTRICTED_ARGV)!r}
        assert Path.cwd() == Path({str(workspace)!r})
        assert os.environ["GROK_HOME"] == {str(profile / ".grok")!r}
        assert os.environ["HOME"] == os.environ["USERPROFILE"] != {str(profile)!r}
        assert Path(os.environ["APPDATA"]).is_dir() and Path(os.environ["LOCALAPPDATA"]).is_dir()
        assert all(os.environ[key] == value for key, value in {dict(GROK_INTEGRATION_ENV)!r}.items())

        def send(**message):
            print(json.dumps(dict(jsonrpc="2.0", **message)), flush=True)

        def server_report(servers, sid="c4"):
            send(method="_x.ai/mcp/servers_updated", params={{"sessionId": sid, "mcpServers": servers}})

        for line in sys.stdin:
            request = json.loads(line)
            method = request.get("method")
            if method == "initialize":
                result = {{"protocolVersion": 1}}
            elif method == "session/new":
                servers = request["params"]["mcpServers"]
                assert servers == {([{"name": "files", "command": "local-only", "args": ["serve"], "env": []}] if mcp else [])!r}
                if {report!r} != "missing":
                    native = [{{"name": "native"}}] if {report!r} == "native" else {{}} if {report!r} == "invalid" else []
                    server_report(native, "wrong" if {report!r} == "wrong-session" else "c4")
                result = {{"sessionId": "c4"}}
            elif method == "session/prompt":
                assert request["params"]["prompt"] == [{{"type": "text", "text": "## Skill: review\\n\\nInstructions\\n\\ntest"}}]
                Path({str(marker)!r}).touch()
                if {report!r} == "late-native":
                    server_report([{{"name": "native"}}])
                for index, (kind, title, raw, expected) in enumerate([
                    *((name, name, {{}}, "yes") for name in ("read", "search", "think", "edit", "delete", "move", "execute")),
                    *((name, name, {{}}, "no") for name in ("fetch", "switch_mode", "unknown")),
                    ("other", "files__search", {{"tool_name": "files__search"}}, {"yes" if mcp else "no"!r}),
                    ("other", "files__search", {{"tool_name": "files__other"}}, "no"),
                    ("other", "other__search", {{"tool_name": "other__search"}}, "no"),
                ]):
                    send(id=str(index), method="session/request_permission", params={{
                        "sessionId": "c4",
                        "toolCall": {{"toolCallId": str(index), "kind": kind, "title": title, "rawInput": raw}},
                        "options": [{{"kind": "allow_once", "optionId": "yes"}}, {{"kind": "reject_once", "optionId": "no"}}],
                    }})
                    assert json.loads(sys.stdin.readline())["result"]["outcome"]["optionId"] == expected
                result = {{"stopReason": "end_turn"}}
            else:
                continue
            send(id=request["id"], result=result)
    """)).encode(), monkeypatch)
    binary.chmod(0o700)  # A POSIX interpreter stands in for the Windows EXE in this test.
    payload = _turn(home, workspace, row, refs=("files",) if mcp else ())
    payload.update(skill_format="prompt_prefix", prompts=[], skills=[{"name": "review", "text": "Instructions"}])
    messages = []

    async def send(stream, kind, body):
        messages.append((kind, body))

    turns = ChildTurns(home, send, None)
    turns.start()
    try:
        await asyncio.wait_for(turns.handle(Frame(1, "turn", payload)), 10)
    finally:
        await turns.finish()
    kind, body = messages[-1]
    if report == "empty":
        assert kind == "result", messages
        assert marker.exists()
    else:
        assert kind == "error"
        assert "native MCP" in body["message"]
        assert marker.exists() == (report == "late-native")


def test_windows_legacy_grok_cannot_resolve_a_binary_for_child_operations(monkeypatch, tmp_path):
    from kotoyose import child_ops

    monkeypatch.setattr(child_ops, "sys", SimpleNamespace(platform="win32"))

    def resolve(*args):
        pytest.fail("Windows must not resolve the legacy Grok profile from PATH")

    monkeypatch.setattr(child_ops, "resolve_print_cli_binary", resolve)
    with pytest.raises(ValueError, match="requires an approved unrestricted"):
        child_ops.resolve_child_binary("grok", "grok", tmp_path)


@pytest.mark.parametrize(("status", "expected"), [(b"not authenticated\n", "logged_out"), (b"fake-model\n", "logged_in")])
async def test_windows_local_login_uses_console_then_declared_status(windows_child, monkeypatch, capsys, status, expected):
    from kotoyose import child_local_login

    home, _, _ = windows_child
    row, binary = _install_fake(home, b"fake executable", monkeypatch)
    launches, jobs = [], []

    @contextmanager
    def job_scope():
        jobs.append("enter")
        try:
            yield set()
        finally:
            jobs.append("exit")

    async def launch(*argv, **options):
        launches.append((argv, options))
        stdout = asyncio.StreamReader()
        stdout.feed_data(status)
        stdout.feed_eof()

        async def wait():
            return 1 if status.startswith(b"not") else 0

        return SimpleNamespace(stdout=stdout, returncode=0, wait=wait)

    monkeypatch.setattr(subprocess, "CREATE_NEW_CONSOLE", 0x10, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(child_local_login, "windows_job_scope", job_scope)
    monkeypatch.setattr(child_local_login, "kill_pids", lambda pids: jobs.append("kill"))
    monkeypatch.setattr(child_local_login, "release_windows_job", lambda pids: jobs.append("release"))
    monkeypatch.setattr(child_local_login.asyncio, "create_subprocess_exec", launch)
    await child_local_login.run(home, row.id, "start", "win32", child_local_login._Parent())
    assert launches[0][0] == (str(binary), "login", "--device-auth")
    assert launches[0][1]["creationflags"] == subprocess.CREATE_NEW_CONSOLE
    assert not {"stdin", "stdout", "stderr", "shell"} & launches[0][1].keys()
    assert launches[1][0] == (str(binary), "models")
    assert launches[1][1]["creationflags"] == subprocess.CREATE_NO_WINDOW
    assert jobs == ["enter", "release", "exit", "enter", "kill", "exit"]
    assert [json.loads(line) for line in capsys.readouterr().out.splitlines()] == [{"state": "running"}, {"state": expected}]


async def test_windows_host_dispatch_and_cache_require_the_current_client_grant(monkeypatch):
    from kotoyose import session
    from kotoyose.config import HarnessConfig
    from kotoyose.print_cli_profiles import use_data_profiles
    from kotoyose.runtime import child
    from kotoyose.runtime.base import RuntimeTurnError

    row = _parse(_document())
    pins = [{"profile": row.profile_id, "provider": row.id, "driver": row.driver, "contract_version": row.contract_version,
             "sha256": row.sha256, "capability": UNRESTRICTED}]
    frames = asyncio.Queue()
    sent = []

    async def send_frame(*args, **kwargs):
        sent.append(args[2:])
        frames.put_nowait(Frame(1, "result", {"text": "fake", "provider": "cli_acp"}))
        return True

    hub = SimpleNamespace(allocate_stream=lambda: 1, send_frame=send_frame)
    live = SimpleNamespace(name="box", platform="windows-x64", features={PROVIDER_DATA_UNRESTRICTED_FEATURE, REPOSITORY_SKILLS_FEATURE},
                           providers=pins, mux=SimpleNamespace(queue=lambda _: frames, discard=lambda _stream: None))
    for module in (session, child):
        monkeypatch.setattr(module, "require_child_link", lambda *_: (hub, live))
    for field in ("sessions", "session_last_used", "session_cache_identities"):
        monkeypatch.setattr(session, field, {})

    with use_data_profiles((row.profile(),)):
        cfg = HarnessConfig.model_validate({
            "connections": {"c": {"kind": "subscription", "provider": row.id, "profile": row.profile_id,
                                  "on": "child:box", "data_policy": "no_training"}},
            "routes": {"grok": {"driver": row.driver, "connection": "c", "profile": row.profile_id, "model": "fake"}},
            "models": {"win": _ALIAS},
        })
        authorized, _ = await session.get_or_resume_client(
            "same-key", "win", "C:\\work", config_snapshot=cfg, client_allows_full=True,
        )
        assert (await authorized.ask("first")).text == "fake"
        assert sent[0][0] == "turn" and sent[0][1]["capability"] == UNRESTRICTED

        denied, _ = await session.get_or_resume_client("same-key", "win", "C:\\work", config_snapshot=cfg)
        assert denied is not authorized
        with pytest.raises(RuntimeTurnError, match="authenticated client with allow_full"):
            await denied.ask("second")
        assert len(sent) == 1


@pytest.mark.parametrize("owner, ok", [("admin", True), ("system", True), ("other", False)])
def test_profile_ancestors_may_be_owned_by_trusted_principals(windows_child, monkeypatch, owner, ok):
    """On an administrator account AppData or the profile is often owned by Administrators or SYSTEM."""
    home, profile, _ = windows_child
    sid = {"admin": _ADMIN_SID, "system": _SYSTEM_SID, "other": _OTHER_SID}[owner]
    fake = _SecurityFake({
        profile: _private_acl(), home.parent: _private_acl(), home: _private_acl(),
    }, owners={home.parent: sid})
    _use_security(monkeypatch, fake)
    if ok:
        assert provider_data_windows.storage_directory(home) == home
    else:
        with pytest.raises(ValueError, match="owned by the current user"):
            provider_data_windows.storage_directory(home)


def test_files_directly_in_the_child_home_are_repaired(windows_child, monkeypatch):
    home, profile, _ = windows_child
    pin = home / "host.json"
    pin.write_text("{}", encoding="utf-8")
    fake = _SecurityFake({
        profile: _private_acl(), home.parent: _private_acl(), home: _private_acl(),
        pin: _private_acl(_SANDBOX_SID),
    })
    _use_security(monkeypatch, fake)
    provider_data_windows.storage_file(pin)
    assert pin in {path for path, _flags in fake.sets}


@pytest.mark.parametrize("provider", ["agy-cli", "cursor-cli"])
def test_windows_bundled_non_grok_unrestricted_preview(windows_child, monkeypatch, provider):
    from kotoyose.provider_data import validate_catalog
    from kotoyose.provider_data_host import bundled_declaration_bytes

    home, profile, _ = windows_child
    rows = [parse_declaration(bundled_declaration_bytes(name)) for name in ("agy-cli", "cursor-cli", "grok-build")]
    assert len(validate_catalog(rows)) == 3
    row = next(row for row in rows if row.id == provider)
    assert {target.artifact.platform for target in row.platforms} == {"linux-x64", "windows-x64"}
    assert row.acp_unrestricted.argv == ("acp",)
    assert row.isolation is None and row.mcp_guard is None
    _offer(home, row)
    _approve(home, row)
    offer = provider_data_child.preview(home)["offers"][0]
    assert offer["can_install"] is True
    assert offer["rights"].startswith(f"{row.name} runs with your rights")
    monkeypatch.delenv("HOME", raising=False)
    monkeypatch.setenv("PATH", "normal-path")
    monkeypatch.setenv("API_TOKEN", "secret")
    with ExitStack() as leases:
        env = provider_data_runtime.acp_data_env(row, home, leases)
    assert env["HOME"] == env["USERPROFILE"] == str(profile)
    assert env["LOCALAPPDATA"] == str(home.parent)
    assert env["PATH"] == "normal-path" and "API_TOKEN" not in env
    if provider == "cursor-cli":
        assert offer["commands"][0]["argv"] == ["node.exe", "index.js", "--version"]


def _cursor_zip(bad=None):
    from io import BytesIO
    import zipfile

    from kotoyose.provider_data_host import bundled_declaration_bytes

    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        archive.writestr("dist-package/node.exe", b"node executable")
        if bad != "missing":
            archive.writestr("dist-package/index.js", b"entrypoint script")
        if bad in ("../escaped", "/absolute", "dist-package\\escaped"):
            archive.writestr(bad, b"unsafe")
        if bad == "symlink":
            entry = zipfile.ZipInfo("dist-package/link")
            entry.create_system = 3
            entry.external_attr = (stat.S_IFLNK | 0o777) << 16
            archive.writestr(entry, "index.js")
    payload = stream.getvalue()
    body = json.loads(bundled_declaration_bytes("cursor-cli"))
    body["platforms"]["windows-x64"]["install"]["sha256"] = hashlib.sha256(payload).hexdigest()
    return _parse(body), payload


def test_cursor_zip_install_and_entrypoint_integrity(windows_child, monkeypatch):
    from io import BytesIO

    home, _, _ = windows_child
    row, payload = _cursor_zip()
    _offer(home, row)
    _approve(home, row)
    calls = []

    def run(binary, argv, **kwargs):
        calls.append((binary, argv))
        return subprocess.CompletedProcess([], 0, row.install.version.encode(), b"")

    monkeypatch.setattr(provider_data_runtime, "_run", run)
    binary = provider_data_runtime.install_stream(row, BytesIO(payload), child_home=home, source="test")
    script = binary.parent / "index.js"
    assert binary.name == "node.exe" and script.read_bytes() == b"entrypoint script"
    assert len(calls) == 1
    assert calls[0][1] == (str(calls[0][0].parent / "index.js"), "--version")
    assert calls[0][0].name == "node.exe"
    assert not (binary.parent / "artifact.part").exists()
    assert provider_data_runtime.managed_command(row, home) == (str(binary), str(script))
    manifest = json.loads((binary.parent / "manifest.json").read_bytes())
    assert manifest["entrypoint_arg_digests"] == {"index.js": hashlib.sha256(script.read_bytes()).hexdigest()}
    script.chmod(0o600)
    script.write_bytes(b"tampered script")
    assert provider_data_runtime.installed_path(row, home) is None


@pytest.mark.parametrize("bad", ["../escaped", "/absolute", "dist-package\\escaped", "symlink", "missing"])
def test_cursor_zip_rejects_unsafe_or_missing_files(windows_child, monkeypatch, bad):
    from io import BytesIO

    home, _, _ = windows_child
    row, payload = _cursor_zip(bad)
    _offer(home, row)
    _approve(home, row)
    monkeypatch.setattr(provider_data_runtime, "_run", lambda *args, **kwargs: pytest.fail("must reject before execution"))
    with pytest.raises(ValueError, match="Unsafe|missing"):
        provider_data_runtime.install_stream(row, BytesIO(payload), child_home=home, source="test")
    assert provider_data_runtime.installed_path(row, home) is None


def test_cursor_zip_verifies_sha_before_opening(windows_child, monkeypatch):
    from io import BytesIO

    home, _, _ = windows_child
    row, payload = _cursor_zip()
    _offer(home, row)
    _approve(home, row)
    monkeypatch.setattr(provider_data_runtime.zipfile, "ZipFile", lambda *args: pytest.fail("must verify SHA first"))
    with pytest.raises(ValueError, match="SHA-256 mismatch"):
        provider_data_runtime.install_stream(row, BytesIO(payload + b"changed"), child_home=home, source="test")


def test_non_grok_workspace_check_does_not_require_grok_credentials(windows_child, tmp_path):
    from kotoyose.provider_data_host import bundled_declaration_bytes

    home, profile, _ = windows_child
    (profile / ".grok").rmdir()
    row = parse_declaration(bundled_declaration_bytes("cursor-cli"))
    cwd = tmp_path / "workspace"
    cwd.mkdir()
    assert provider_data_runtime.checked_write_cwd(cwd, home, declaration=row) == cwd
    with pytest.raises(ValueError, match="OS account home"):
        provider_data_runtime.checked_write_cwd(profile, home, declaration=row)


@pytest.mark.parametrize("limit", ["members", "file", "total"])
def test_zip_rejects_expansion_limits(tmp_path, monkeypatch, limit):
    import zipfile

    row, payload = _cursor_zip()
    target = row.platform("windows-x64")
    stage = tmp_path / "stage"
    stage.mkdir()
    download = stage / "artifact.part"
    download.write_bytes(payload)
    entry = zipfile.ZipInfo("dist-package/node.exe")
    entry.file_size = 512 * 1024 * 1024 + (limit == "file")
    members = [entry] * (20_001 if limit == "members" else 2 if limit == "total" else 1)

    class Archive:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def infolist(self):
            return members

        def open(self, *args):
            from io import BytesIO

            return BytesIO(b"small")

    monkeypatch.setattr(provider_data_runtime.zipfile, "ZipFile", lambda *args: Archive())
    with pytest.raises(ValueError, match="limit"):
        provider_data_runtime._extract_zip(download, stage, target)
