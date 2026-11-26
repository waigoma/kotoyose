use std::{
    ffi::OsString,
    fs, io,
    os::windows::{
        ffi::{OsStrExt, OsStringExt},
        fs::MetadataExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle},
    },
    path::Path,
    ptr::{null, null_mut},
};

use windows_sys::Win32::{
    Foundation::{ERROR_SUCCESS, HANDLE, LocalFree},
    Security::{
        ACCESS_ALLOWED_ACE, ACE_HEADER, ACL, ACL_REVISION, AddAccessAllowedAce,
        AddAccessAllowedAceEx,
        Authorization::{
            ConvertSidToStringSidW, GetNamedSecurityInfoW, SE_FILE_OBJECT, SetNamedSecurityInfoW,
        },
        CONTAINER_INHERIT_ACE, CreateWellKnownSid, DACL_SECURITY_INFORMATION, EqualSid, GetAce,
        GetLengthSid, GetSecurityDescriptorControl, GetTokenInformation, INHERIT_ONLY_ACE,
        InitializeAcl, InitializeSecurityDescriptor, LookupAccountSidW, OBJECT_INHERIT_ACE,
        OWNER_SECURITY_INFORMATION, PROTECTED_DACL_SECURITY_INFORMATION, PSECURITY_DESCRIPTOR,
        PSID, SE_DACL_PROTECTED, SECURITY_DESCRIPTOR, SECURITY_MAX_SID_SIZE, SetFileSecurityW,
        SetSecurityDescriptorControl, SetSecurityDescriptorDacl, TOKEN_QUERY, TOKEN_USER,
        TokenUser, WinBuiltinAdministratorsSid, WinLocalSystemSid,
    },
    Storage::FileSystem::FILE_ALL_ACCESS,
    System::Threading::{GetCurrentProcess, OpenProcessToken},
};

struct UserToken {
    // TOKEN_USER contains a pointer to the SID bytes inside this allocation.
    buffer: Vec<usize>,
}

impl UserToken {
    fn current() -> io::Result<Self> {
        let mut handle: HANDLE = null_mut();
        if unsafe { OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut handle) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let token = unsafe { OwnedHandle::from_raw_handle(handle) };
        // The buffer is aligned for TOKEN_USER and large enough for a maximum-length SID.
        let capacity = size_of::<TOKEN_USER>() + SECURITY_MAX_SID_SIZE as usize;
        let mut buffer = vec![0; capacity.div_ceil(size_of::<usize>())];
        let mut written = 0;
        if unsafe {
            GetTokenInformation(
                token.as_raw_handle(),
                TokenUser,
                buffer.as_mut_ptr().cast(),
                capacity as u32,
                &mut written,
            )
        } == 0
        {
            return Err(io::Error::last_os_error());
        }
        Ok(Self { buffer })
    }

    fn sid(&self) -> PSID {
        unsafe { self.buffer.as_ptr().cast::<TOKEN_USER>().read().User.Sid }
    }
}

fn wide_path(path: &Path) -> Vec<u16> {
    path.as_os_str().encode_wide().chain([0]).collect()
}

pub fn restrict_to_user(path: &Path) -> io::Result<()> {
    let user = UserToken::current()?;
    const ACL_CAPACITY: usize = size_of::<ACL>()
        + std::mem::offset_of!(ACCESS_ALLOWED_ACE, SidStart)
        + SECURITY_MAX_SID_SIZE as usize;
    let mut storage = [0u32; ACL_CAPACITY.div_ceil(size_of::<u32>())];
    let acl = storage.as_mut_ptr().cast();
    if unsafe { InitializeAcl(acl, size_of_val(&storage) as u32, ACL_REVISION) } == 0
        || unsafe { AddAccessAllowedAce(acl, ACL_REVISION, FILE_ALL_ACCESS, user.sid()) } == 0
    {
        return Err(io::Error::last_os_error());
    }
    let status = unsafe {
        SetNamedSecurityInfoW(
            wide_path(path).as_ptr(),
            SE_FILE_OBJECT,
            DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION,
            null_mut(),
            null_mut(),
            acl,
            null(),
        )
    };
    if status == ERROR_SUCCESS {
        Ok(())
    } else {
        Err(io::Error::from_raw_os_error(status as i32))
    }
}

const FILE_ATTRIBUTE_REPARSE_POINT: u32 = 0x400;

fn denied(message: &str) -> io::Error {
    io::Error::new(io::ErrorKind::PermissionDenied, message)
}

fn wide_text(raw: *const u16) -> String {
    if raw.is_null() {
        return String::new();
    }
    let mut length = 0;
    unsafe {
        while *raw.add(length) != 0 {
            length += 1;
        }
        OsString::from_wide(std::slice::from_raw_parts(raw, length))
            .to_string_lossy()
            .into_owned()
    }
}

fn sid_text(sid: PSID) -> io::Result<String> {
    let mut raw = null_mut();
    if unsafe { ConvertSidToStringSidW(sid, &mut raw) } == 0 {
        return Err(io::Error::last_os_error());
    }
    let text = wide_text(raw);
    unsafe { LocalFree(raw.cast()) };
    Ok(text)
}

fn principal_label(sid: PSID) -> String {
    let text = sid_text(sid).unwrap_or_else(|_| "unknown".to_owned());
    let mut name = [0u16; 256];
    let mut domain = [0u16; 256];
    let (mut name_len, mut domain_len) = (name.len() as u32, domain.len() as u32);
    let mut use_kind = 0;
    let known = unsafe {
        LookupAccountSidW(
            null(),
            sid,
            name.as_mut_ptr(),
            &mut name_len,
            domain.as_mut_ptr(),
            &mut domain_len,
            &mut use_kind,
        )
    } != 0;
    if known {
        let account = wide_text(name.as_ptr());
        if account.is_empty() {
            text
        } else {
            format!("{account} ({text})")
        }
    } else {
        text
    }
}

struct SecurityInfo {
    descriptor: *mut core::ffi::c_void,
}

impl Drop for SecurityInfo {
    fn drop(&mut self) {
        if !self.descriptor.is_null() {
            unsafe { LocalFree(self.descriptor) };
        }
    }
}

fn security_info(path: &Path) -> io::Result<(*mut ACL, SecurityInfo, PSID)> {
    let mut owner = null_mut();
    let mut dacl = null_mut();
    let mut descriptor = null_mut();
    let status = unsafe {
        GetNamedSecurityInfoW(
            wide_path(path).as_ptr(),
            SE_FILE_OBJECT,
            OWNER_SECURITY_INFORMATION | DACL_SECURITY_INFORMATION,
            &mut owner,
            null_mut(),
            &mut dacl,
            null_mut(),
            &mut descriptor,
        )
    };
    if status != ERROR_SUCCESS {
        return Err(io::Error::from_raw_os_error(status as i32));
    }
    Ok((dacl, SecurityInfo { descriptor }, owner))
}

fn reparse(path: &Path) -> io::Result<bool> {
    Ok(fs::symlink_metadata(path)?.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0)
}

fn unexpected_principals(
    acl: *mut ACL,
    user: PSID,
    system: PSID,
    administrators: PSID,
) -> io::Result<Vec<String>> {
    if acl.is_null() {
        return Ok(Vec::new());
    }
    let mut labels = Vec::new();
    let count = unsafe { (*acl).AceCount };
    for index in 0..count {
        let mut entry = null_mut();
        if unsafe { GetAce(acl, u32::from(index), &mut entry) } == 0 {
            return Err(io::Error::last_os_error());
        }
        let header = unsafe { &*entry.cast::<ACE_HEADER>() };
        if header.AceType == 1 || header.AceFlags & INHERIT_ONLY_ACE as u8 != 0 {
            continue;
        }
        let sid = unsafe { entry.byte_add(8) as PSID };
        let allowed = unsafe {
            header.AceType == 0
                && (EqualSid(sid, user) != 0
                    || EqualSid(sid, system) != 0
                    || EqualSid(sid, administrators) != 0)
        };
        if !allowed {
            labels.push(principal_label(sid));
        }
    }
    Ok(labels)
}

fn well_known(kind: i32) -> io::Result<[u8; SECURITY_MAX_SID_SIZE as usize]> {
    let mut sid = [0u8; SECURITY_MAX_SID_SIZE as usize];
    let mut size = SECURITY_MAX_SID_SIZE;
    if unsafe { CreateWellKnownSid(kind, null_mut(), sid.as_mut_ptr().cast(), &mut size) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(sid)
}

fn protected_dacl(user: PSID, system: PSID, administrators: PSID) -> io::Result<Vec<u32>> {
    let bytes = 8 + [user, system, administrators]
        .into_iter()
        .map(|sid| 8 + unsafe { GetLengthSid(sid) } as usize)
        .sum::<usize>();
    let mut storage = vec![0u32; bytes.div_ceil(4)];
    let acl = storage.as_mut_ptr().cast();
    let inherit = OBJECT_INHERIT_ACE | CONTAINER_INHERIT_ACE;
    if unsafe { InitializeAcl(acl, (storage.len() * 4) as u32, ACL_REVISION) } == 0 {
        return Err(io::Error::last_os_error());
    }
    for sid in [user, system, administrators] {
        if unsafe { AddAccessAllowedAceEx(acl, ACL_REVISION, inherit, FILE_ALL_ACCESS, sid) } == 0 {
            return Err(io::Error::last_os_error());
        }
    }
    Ok(storage)
}

fn dacl_is_protected(path: &Path) -> io::Result<bool> {
    let (_acl, info, _owner) = security_info(path)?;
    let (mut control, mut revision) = (0, 0);
    if unsafe { GetSecurityDescriptorControl(info.descriptor, &mut control, &mut revision) } == 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(control & SE_DACL_PROTECTED != 0)
}

fn apply_dacl(path: &Path, acl: *const ACL) -> io::Result<()> {
    let wide = wide_path(path);
    let flags = DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION;
    let status = unsafe {
        SetNamedSecurityInfoW(
            wide.as_ptr(),
            SE_FILE_OBJECT,
            flags,
            null_mut(),
            null_mut(),
            acl,
            null(),
        )
    };
    if status != ERROR_SUCCESS {
        return Err(io::Error::from_raw_os_error(status as i32));
    }
    // Wine 9 reports success from SetNamedSecurityInfoW and leaves the inherited DACL,
    // including Everyone, in place. A protected descriptor is the signal that the write
    // landed; otherwise write the same DACL through SetFileSecurityW.
    if dacl_is_protected(path)? {
        return Ok(());
    }
    let mut absolute = SECURITY_DESCRIPTOR::default();
    let descriptor: PSECURITY_DESCRIPTOR =
        (&mut absolute as *mut SECURITY_DESCRIPTOR).cast::<core::ffi::c_void>();
    if unsafe { InitializeSecurityDescriptor(descriptor, 1) } == 0
        || unsafe { SetSecurityDescriptorDacl(descriptor, 1, acl, 0) } == 0
        || unsafe { SetSecurityDescriptorControl(descriptor, SE_DACL_PROTECTED, SE_DACL_PROTECTED) }
            == 0
        || unsafe { SetFileSecurityW(wide.as_ptr(), flags, descriptor) } == 0
    {
        return Err(io::Error::last_os_error());
    }
    Ok(())
}

fn propagate_dacl(directory: &Path, acl: *const ACL) -> io::Result<()> {
    for entry in fs::read_dir(directory)? {
        let child = entry?.path();
        if reparse(&child)? {
            continue;
        }
        apply_dacl(&child, acl)?;
        if fs::symlink_metadata(&child)?.is_dir() {
            propagate_dacl(&child, acl)?;
        }
    }
    Ok(())
}

/// Replace a child-home DACL that grants anyone besides the user, SYSTEM, and Administrators.
///
/// LocalAppData may contain sandbox ACEs. This does not follow reparse points and does not
/// rewrite directories outside the child home; callers pass only that home.
pub fn own_child_home(path: &Path) -> io::Result<()> {
    let metadata = fs::symlink_metadata(path)?;
    if !metadata.is_dir() || metadata.file_attributes() & FILE_ATTRIBUTE_REPARSE_POINT != 0 {
        return Err(denied(
            "provider paths must be ordinary directories or files without reparse points",
        ));
    }
    let user = UserToken::current()?;
    let mut system = well_known(WinLocalSystemSid)?;
    let mut administrators = well_known(WinBuiltinAdministratorsSid)?;
    let (acl, info, owner) = security_info(path)?;
    if owner.is_null() || unsafe { EqualSid(owner, user.sid()) } == 0 {
        drop(info);
        return Err(denied("provider path must be owned by the current user"));
    }
    let removed = unexpected_principals(
        acl,
        user.sid(),
        system.as_mut_ptr().cast(),
        administrators.as_mut_ptr().cast(),
    )?;
    drop(info);
    if removed.is_empty() {
        if acl.is_null() {
            return Err(denied("provider storage must have an owner-only ACL"));
        }
        return Ok(());
    }
    tracing::warn!(
        "replaced provider storage ACL; removed {}",
        removed.join(", ")
    );
    let storage = protected_dacl(
        user.sid(),
        system.as_mut_ptr().cast(),
        administrators.as_mut_ptr().cast(),
    )?;
    let replacement = storage.as_ptr().cast();
    apply_dacl(path, replacement)?;
    propagate_dacl(path, replacement)?;
    let (acl, info, owner) = security_info(path)?;
    let owned = !owner.is_null() && unsafe { EqualSid(owner, user.sid()) } != 0;
    let removed = unexpected_principals(
        acl,
        user.sid(),
        system.as_mut_ptr().cast(),
        administrators.as_mut_ptr().cast(),
    )?;
    drop(info);
    if owned && removed.is_empty() && !acl.is_null() {
        Ok(())
    } else if !owned {
        Err(denied("provider path must be owned by the current user"))
    } else {
        Err(denied(
            "provider storage ACL grants access outside the current user",
        ))
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use windows_sys::Win32::{
        Foundation::LocalFree,
        Security::{
            Authorization::GetNamedSecurityInfoW, EqualSid, GetAce, GetSecurityDescriptorControl,
            SE_DACL_PROTECTED,
        },
    };

    #[test]
    fn secret_replacement_keeps_only_the_current_users_ace() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join("key.pem");
        std::fs::write(&path, b"previous").unwrap();
        super::super::write_secret(&path, b"new").unwrap();
        assert_owner_only(&path);
    }

    #[test]
    fn policy_replacement_keeps_only_the_current_users_ace() {
        let home = tempfile::tempdir().unwrap();
        let path = home.path().join("policy.json");
        std::fs::write(&path, b"previous").unwrap();
        crate::policy::save(
            home.path(),
            &crate::policy::load(home.path(), Vec::new()).revision,
            super::super::EMPTY_POLICY.as_bytes(),
        )
        .unwrap();
        assert_owner_only(&path);
    }

    fn assert_owner_only(path: &Path) {
        let mut acl = null_mut();
        let mut descriptor = null_mut();
        let status = unsafe {
            GetNamedSecurityInfoW(
                wide_path(path).as_ptr(),
                SE_FILE_OBJECT,
                DACL_SECURITY_INFORMATION,
                null_mut(),
                null_mut(),
                &mut acl,
                null_mut(),
                &mut descriptor,
            )
        };
        assert_eq!(status, ERROR_SUCCESS);
        let user = UserToken::current().unwrap();
        unsafe {
            assert!(!acl.is_null());
            assert_eq!((*acl).AceCount, 1);
            let mut entry = null_mut();
            assert_ne!(GetAce(acl, 0, &mut entry), 0);
            let ace = &mut *entry.cast::<ACCESS_ALLOWED_ACE>();
            assert_eq!(ace.Header.AceType, 0); // ACCESS_ALLOWED_ACE_TYPE
            assert_eq!(ace.Mask, FILE_ALL_ACCESS);
            assert_ne!(EqualSid((&raw mut ace.SidStart).cast(), user.sid()), 0);
            let (mut control, mut revision) = (0, 0);
            assert_ne!(
                GetSecurityDescriptorControl(descriptor, &mut control, &mut revision),
                0
            );
            assert_ne!(control & SE_DACL_PROTECTED, 0);
            LocalFree(descriptor);
        }
    }
}
