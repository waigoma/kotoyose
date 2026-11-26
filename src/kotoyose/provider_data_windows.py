"""Windows filesystem checks for child-owned provider data."""

import ctypes
from ctypes import wintypes
from functools import cache
import logging
import ntpath
import os
from pathlib import Path
import stat
from uuid import UUID

log = logging.getLogger(__name__)


_REPARSE_POINT = 0x400
_SYSTEM = "S-1-5-18"
_ADMINISTRATORS = "S-1-5-32-544"
_FILE_ALL_ACCESS = 0x1F01FF
_OBJECT_AND_CONTAINER_INHERIT = 0x03
_PROTECTED_DACL = 0x80000004
_OWNED_TREES = {"provider-data", "provider-declarations"}
_PROFILE = UUID("5E6C858F-0E22-4760-9AFE-EA3317B67173")
_LOCAL_APP_DATA = UUID("F1B32785-6FBA-4FCF-9D55-7B8E7F157091")


def _known_folder(folder: UUID) -> Path:
    shell = ctypes.WinDLL("shell32")
    ole = ctypes.WinDLL("ole32")
    lookup = shell.SHGetKnownFolderPath
    lookup.argtypes = [ctypes.c_void_p, wintypes.DWORD, wintypes.HANDLE, ctypes.POINTER(wintypes.LPWSTR)]
    lookup.restype = ctypes.c_long
    ole.CoTaskMemFree.argtypes = [ctypes.c_void_p]
    ole.CoTaskMemFree.restype = None
    guid = (ctypes.c_uint32 * 4).from_buffer_copy(folder.bytes_le)
    result = wintypes.LPWSTR()
    status = lookup(ctypes.byref(guid), 0, None, ctypes.byref(result))
    try:
        if status < 0 or not result.value:
            raise OSError(f"SHGetKnownFolderPath failed (HRESULT 0x{status & 0xFFFFFFFF:08X})")
        return Path(result.value)
    finally:
        ole.CoTaskMemFree(result)


def _account_folder(folder: UUID, key: str) -> Path:
    path = _known_folder(folder)
    value = os.environ.get(key)
    if value is not None and ntpath.normcase(ntpath.normpath(value)) != ntpath.normcase(ntpath.normpath(path)):
        raise ValueError(f"{key} must match the Windows known folder for the current user")
    return path


def account_folders() -> tuple[Path, Path]:
    return _account_folder(_PROFILE, "USERPROFILE"), _account_folder(_LOCAL_APP_DATA, "LOCALAPPDATA")


def checked_path(path: Path, *, directory: bool) -> os.stat_result:
    info = path.lstat()
    matches_type = stat.S_ISDIR if directory else stat.S_ISREG
    if not matches_type(info.st_mode) or getattr(info, "st_file_attributes", 0) & _REPARSE_POINT:
        raise ValueError("provider paths must be ordinary directories or files without reparse points")
    return info


def checked_directory(path: Path, *, create: bool = False) -> Path:
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("provider directory must be an absolute path without '..'")
    for part in reversed((path, *path.parents)):
        try:
            checked_path(part, directory=True)
        except FileNotFoundError:
            if not create:
                raise
            part.mkdir(exist_ok=True)
            checked_path(part, directory=True)
    return path


def _sid_text(api, local_free, sid) -> str:
    value = wintypes.LPWSTR()
    if not api.ConvertSidToStringSidW(sid, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        return value.value
    finally:
        local_free(ctypes.cast(value, ctypes.c_void_p))


@cache
def _security_api():
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    api = ctypes.WinDLL("advapi32", use_last_error=True)
    ptr = ctypes.c_void_p
    out = ctypes.POINTER(ptr)
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.LocalFree.argtypes = [ptr]
    kernel.LocalFree.restype = ptr
    api.GetNamedSecurityInfoW.argtypes = [wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD, out, out, out, out, out]
    api.GetNamedSecurityInfoW.restype = wintypes.DWORD
    api.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, out]
    api.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, ptr, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    api.ConvertSidToStringSidW.argtypes = [ptr, ctypes.POINTER(wintypes.LPWSTR)]
    api.GetAce.argtypes = [ptr, wintypes.DWORD, out]
    api.ConvertStringSidToSidW.argtypes = [wintypes.LPCWSTR, out]
    api.GetLengthSid.argtypes = [ptr]
    api.GetLengthSid.restype = wintypes.DWORD
    api.CopySid.argtypes = [wintypes.DWORD, ptr, ptr]
    api.InitializeAcl.argtypes = [ptr, wintypes.DWORD, wintypes.DWORD]
    api.AddAccessAllowedAceEx.argtypes = [ptr, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ptr]
    api.LookupAccountSidW.argtypes = [
        wintypes.LPCWSTR, ptr, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD),
        wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD), ctypes.POINTER(ctypes.c_int),
    ]
    api.SetNamedSecurityInfoW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ptr, ptr, ptr, ptr]
    api.SetNamedSecurityInfoW.restype = wintypes.DWORD
    return api, kernel


def _check_acl(api, kernel, acl, owner: str) -> None:
    if not acl:
        raise ValueError("provider storage must have an owner-only ACL")
    # ACL.AceCount is a USHORT at offset 4. An ACCESS_ALLOWED_ACE's SID starts at offset 8.
    ace_count = ctypes.c_ushort.from_address(acl.value + 4).value
    for index in range(ace_count):
        ace = ctypes.c_void_p()
        if not api.GetAce(acl, index, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        kind, flags = (ctypes.c_ubyte * 2).from_address(ace.value)
        if kind == 1 or flags & 0x08:  # Deny ACE or INHERIT_ONLY; it grants no access to this object.
            continue
        if kind != 0 or _sid_text(api, kernel.LocalFree, ctypes.c_void_p(ace.value + 8)) not in {
            owner, _SYSTEM, _ADMINISTRATORS,
        }:
            raise ValueError("provider storage ACL grants access outside the current user")


def _current_user(api, kernel):
    handle = wintypes.HANDLE()
    if not api.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(handle)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        size = wintypes.DWORD()
        api.GetTokenInformation(handle, 1, None, 0, ctypes.byref(size))
        data = ctypes.create_string_buffer(size.value)
        if not api.GetTokenInformation(handle, 1, data, size, ctypes.byref(size)):
            raise ctypes.WinError(ctypes.get_last_error())
        sid = ctypes.c_void_p.from_buffer(data)
        text = _sid_text(api, kernel.LocalFree, sid)
        copied = ctypes.create_string_buffer(api.GetLengthSid(sid))
        if not api.CopySid(len(copied), copied, sid):
            raise ctypes.WinError(ctypes.get_last_error())
        return text, copied
    finally:
        kernel.CloseHandle(handle)


def _sid_copy(api, kernel, text: str):
    value = ctypes.c_void_p()
    if not api.ConvertStringSidToSidW(text, ctypes.byref(value)):
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        copied = ctypes.create_string_buffer(api.GetLengthSid(value))
        if not api.CopySid(len(copied), copied, value):
            raise ctypes.WinError(ctypes.get_last_error())
        return copied
    finally:
        kernel.LocalFree(value)


def _account_name(api, sid) -> str | None:
    name = ctypes.create_unicode_buffer(256)
    domain = ctypes.create_unicode_buffer(256)
    name_len = wintypes.DWORD(len(name))
    domain_len = wintypes.DWORD(len(domain))
    use = ctypes.c_int()
    if not api.LookupAccountSidW(None, sid, name, ctypes.byref(name_len), domain, ctypes.byref(domain_len), ctypes.byref(use)):
        return None
    return name.value or None


def _security_info(api, path: Path, *, private: bool):
    owner, acl, descriptor = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    error = api.GetNamedSecurityInfoW(
        str(path), 1, 0x01 | (0x04 if private else 0), ctypes.byref(owner), None,
        ctypes.byref(acl) if private else None, None, ctypes.byref(descriptor),
    )
    if error:
        raise ctypes.WinError(error)
    return owner, acl, descriptor


def _unexpected_principals(api, kernel, acl, owner: str) -> list[str]:
    if not acl:
        return []
    labels = []
    ace_count = ctypes.c_ushort.from_address(acl.value + 4).value
    for index in range(ace_count):
        ace = ctypes.c_void_p()
        if not api.GetAce(acl, index, ctypes.byref(ace)):
            raise ctypes.WinError(ctypes.get_last_error())
        kind, flags = (ctypes.c_ubyte * 2).from_address(ace.value)
        if kind == 1 or flags & 0x08:
            continue
        sid = ctypes.c_void_p(ace.value + 8)
        text = _sid_text(api, kernel.LocalFree, sid)
        if kind != 0 or text not in {owner, _SYSTEM, _ADMINISTRATORS}:
            name = _account_name(api, sid)
            labels.append(f"{name} ({text})" if name else text)
    return labels


def _protected_dacl(api, kernel, user_sid):
    sids = (user_sid, _sid_copy(api, kernel, _SYSTEM), _sid_copy(api, kernel, _ADMINISTRATORS))
    size = 8 + sum(8 + api.GetLengthSid(sid) for sid in sids)
    buffer = ctypes.create_string_buffer(size)
    if not api.InitializeAcl(buffer, size, 2):
        raise ctypes.WinError(ctypes.get_last_error())
    for sid in sids:
        if not api.AddAccessAllowedAceEx(buffer, 2, _OBJECT_AND_CONTAINER_INHERIT, _FILE_ALL_ACCESS, sid):
            raise ctypes.WinError(ctypes.get_last_error())
    return buffer


def _apply_dacl(api, path: Path, dacl) -> None:
    error = api.SetNamedSecurityInfoW(str(path), 1, _PROTECTED_DACL, None, None, dacl, None)
    if error:
        raise ctypes.WinError(error)


def _is_reparse(entry: os.DirEntry) -> bool:
    try:
        info = entry.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("provider storage ACL could not be restricted to the current user") from exc
    return entry.is_symlink() or bool(getattr(info, "st_file_attributes", 0) & _REPARSE_POINT)


def _propagate_dacl(api, directory: Path, dacl) -> None:
    """Copy the protected DACL onto existing children without walking reparse points."""
    try:
        entries = list(os.scandir(directory))
    except OSError as exc:
        raise ValueError("provider storage ACL could not be restricted to the current user") from exc
    for entry in entries:
        if _is_reparse(entry):
            continue
        child = Path(entry.path)
        _apply_dacl(api, child, dacl)
        try:
            directory_child = entry.is_dir(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("provider storage ACL could not be restricted to the current user") from exc
        if directory_child:
            _propagate_dacl(api, child, dacl)


def _child_home(local: Path) -> Path:
    return local / "kotoyose-child"


def _owns_tree(path: Path, local: Path) -> bool:
    home = _child_home(local)
    # The child home itself and the files directly in it (host.json, policy.json, ...) are written by
    # the child; so are the provider-data and provider-declarations trees.
    if path == home or path.parent == home:
        return True
    try:
        parts = path.relative_to(home).parts
    except ValueError:
        return False
    return bool(_OWNED_TREES.intersection(parts))


def require_user_owner(path: Path, *, private: bool = False, repair: bool = False, trusted_owner: bool = False) -> None:
    """Reject a path the current user does not own.

    ``repair`` replaces an overly broad DACL on a child-owned directory. Profile
    and ``GROK_HOME`` stay reject-only: those ACLs are not ours to rewrite.
    """
    api, kernel = _security_api()
    user, user_sid = _current_user(api, kernel)
    owner, acl, descriptor = _security_info(api, path, private=private)
    replacement = None
    try:
        owner_sid = _sid_text(api, kernel.LocalFree, owner) if owner else None
        # Profile ancestors (C:\Users\<name>, AppData, AppData\Local) are often owned by
        # Administrators or SYSTEM on an administrator account; both are trusted principals.
        allowed = {user, _SYSTEM, _ADMINISTRATORS} if trusted_owner else {user}
        if owner_sid not in allowed:
            raise ValueError(f"provider path must be owned by the current user (owner {owner_sid or 'none'})")
        if not private:
            return
        unexpected = _unexpected_principals(api, kernel, acl, user)
        if unexpected and repair:
            replacement = _protected_dacl(api, kernel, user_sid)
            # Names and SIDs only. The path would tell a log reader which tree was open.
            log.warning("replaced provider storage ACL; removed %s", ", ".join(unexpected))
        else:
            _check_acl(api, kernel, acl, user)
    finally:
        kernel.LocalFree(descriptor)
    if replacement is None:
        return
    _apply_dacl(api, path, replacement)
    if path.is_dir() and not path.is_symlink():
        _propagate_dacl(api, path, replacement)
    owner, acl, descriptor = _security_info(api, path, private=True)
    try:
        if not owner or _sid_text(api, kernel.LocalFree, owner) != user:
            raise ValueError("provider path must be owned by the current user")
        _check_acl(api, kernel, acl, user)
    finally:
        kernel.LocalFree(descriptor)


def storage_directory(path: Path, *, create: bool = False) -> Path:
    profile, local = account_folders()
    if (not profile.is_absolute() or not local.is_relative_to(profile) or local == profile
            or not path.is_relative_to(local) or path == local or ".." in path.parts):
        raise ValueError("Windows provider storage must be below the user's LOCALAPPDATA")
    checked_directory(local)
    # Sandbox ACEs on the profile are inherited by the child home. Leave the
    # profile ACL alone and break that inheritance on the directories we own.
    for directory in (local, *local.parents):
        if directory.is_relative_to(profile):
            require_user_owner(directory, trusted_owner=True)
    home = _child_home(local)
    repair_tree = _owns_tree(path, local)
    for directory in reversed((path, *path.parents)):
        if directory == local or not directory.is_relative_to(local):
            continue
        try:
            checked_path(directory, directory=True)
        except FileNotFoundError:
            if not create:
                raise
            directory.mkdir(exist_ok=True)
            checked_path(directory, directory=True)
        repair = repair_tree and directory.is_relative_to(home)
        require_user_owner(directory, private=True, repair=repair)
    return path


def storage_file(path: Path) -> os.stat_result:
    storage_directory(path.parent)
    info = checked_path(path, directory=False)
    _profile, local = account_folders()
    home = _child_home(local)
    repair = _owns_tree(path, local) and path.parent.is_relative_to(home)
    require_user_owner(path, private=True, repair=repair)
    return info


def real_grok_home() -> Path:
    profile = _account_folder(_PROFILE, "USERPROFILE")
    grok = checked_directory(profile / ".grok")
    require_user_owner(profile, trusted_owner=True)
    require_user_owner(grok, private=True)
    return grok
