use std::{
    ffi::OsStr,
    fs::OpenOptions,
    io,
    os::windows::{
        ffi::OsStrExt,
        io::{AsRawHandle, FromRawHandle, OwnedHandle, RawHandle},
    },
    path::Path,
    ptr::{null, null_mut},
    sync::Arc,
    time::Duration,
};

use anyhow::{Context, Result};
use windows_sys::Win32::{
    Foundation::{DUPLICATE_SAME_ACCESS, DuplicateHandle, HANDLE, WAIT_TIMEOUT},
    System::{
        Console::GetConsoleCP,
        JobObjects::{
            CreateJobObjectW, JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE,
            JOBOBJECT_BASIC_ACCOUNTING_INFORMATION, JOBOBJECT_EXTENDED_LIMIT_INFORMATION,
            JobObjectBasicAccountingInformation, JobObjectExtendedLimitInformation,
            QueryInformationJobObject, SetInformationJobObject, TerminateJobObject,
        },
        Threading::{
            CREATE_NO_WINDOW, CreateProcessW, DeleteProcThreadAttributeList,
            EXTENDED_STARTUPINFO_PRESENT, GetCurrentProcess, GetExitCodeProcess,
            InitializeProcThreadAttributeList, LPPROC_THREAD_ATTRIBUTE_LIST,
            PROC_THREAD_ATTRIBUTE_HANDLE_LIST, PROC_THREAD_ATTRIBUTE_JOB_LIST, PROCESS_INFORMATION,
            STARTF_USESTDHANDLES, STARTUPINFOEXW, UpdateProcThreadAttribute, WaitForSingleObject,
        },
    },
};

use super::{Input, Output, STOP_TIMEOUT};

pub fn prepare() -> io::Result<()> {
    Ok(())
}

pub struct Process {
    job: Option<Arc<OwnedHandle>>,
    process: OwnedHandle,
    pid: u32,
}

impl Process {
    pub fn id(&self) -> u32 {
        self.pid
    }

    pub(crate) async fn wait(&self) -> io::Result<bool> {
        loop {
            match unsafe { WaitForSingleObject(self.process.as_raw_handle(), 0) } {
                WAIT_TIMEOUT => tokio::time::sleep(Duration::from_millis(5)).await,
                windows_sys::Win32::Foundation::WAIT_OBJECT_0 => {
                    let mut code = 0;
                    checked(
                        unsafe { GetExitCodeProcess(self.process.as_raw_handle(), &mut code) } != 0,
                    )?;
                    return Ok(code == 0);
                }
                _ => return Err(io::Error::last_os_error()),
            }
        }
    }

    pub async fn stop(&mut self) -> Result<()> {
        tokio::time::timeout(STOP_TIMEOUT, self.stop_job())
            .await
            .context("runner Job did not exit within 5 seconds")
    }

    pub(crate) fn release_job(&mut self) -> io::Result<()> {
        if let Some(job) = &self.job {
            set_kill_on_close(job, false)?;
            self.job = None;
        }
        Ok(())
    }

    async fn stop_job(&mut self) {
        if let Some(job) = self.job.take() {
            if unsafe { TerminateJobObject(job.as_raw_handle(), 1) } != 0 {
                let mut accounting = JOBOBJECT_BASIC_ACCOUNTING_INFORMATION::default();
                while unsafe {
                    QueryInformationJobObject(
                        job.as_raw_handle(),
                        JobObjectBasicAccountingInformation,
                        (&raw mut accounting).cast(),
                        size_of_val(&accounting) as u32,
                        null_mut(),
                    )
                } != 0
                    && accounting.ActiveProcesses != 0
                {
                    tokio::time::sleep(Duration::from_millis(5)).await;
                }
            }
            // KILL_ON_JOB_CLOSE also covers failure above and an abrupt link-app exit.
            drop(job);
        }
        while unsafe { WaitForSingleObject(self.process.as_raw_handle(), 0) } == WAIT_TIMEOUT {
            tokio::time::sleep(Duration::from_millis(5)).await;
        }
    }

    pub fn reap_exited_descendants(&self) {}

    pub fn memory_probe(&self) -> crate::control::MemoryProbe {
        let job = self.job.as_ref().map(Arc::downgrade);
        Arc::new(move || {
            let job = job.as_ref()?.upgrade()?;
            // JobObjectMemoryUsageInformation returns current and peak commit in bytes.
            // The Windows 11 ABI uses two 64-bit values for this information class.
            const JOB_OBJECT_MEMORY_USAGE_INFORMATION: i32 = 28;
            let mut usage = [0u64; 2];
            let ok = unsafe {
                QueryInformationJobObject(
                    job.as_raw_handle(),
                    JOB_OBJECT_MEMORY_USAGE_INFORMATION,
                    usage.as_mut_ptr().cast(),
                    size_of_val(&usage) as u32,
                    null_mut(),
                )
            };
            (ok != 0).then_some(usage[0])
        })
    }
}

fn checked(success: bool) -> io::Result<()> {
    if success {
        Ok(())
    } else {
        Err(io::Error::last_os_error())
    }
}

fn job() -> Result<OwnedHandle> {
    // A null security-attributes pointer creates a non-inheritable handle.
    let raw = unsafe { CreateJobObjectW(null(), null()) };
    checked(!raw.is_null())?;
    let job = unsafe { OwnedHandle::from_raw_handle(raw) };
    set_kill_on_close(&job, true)?;
    Ok(job)
}

fn set_kill_on_close(job: &OwnedHandle, enabled: bool) -> io::Result<()> {
    let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
    if enabled {
        limits.BasicLimitInformation.LimitFlags = JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE;
    }
    checked(
        unsafe {
            SetInformationJobObject(
                job.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                (&raw const limits).cast(),
                size_of_val(&limits) as u32,
            )
        } != 0,
    )
}

fn inheritable(source: RawHandle) -> Result<OwnedHandle> {
    let mut result: HANDLE = null_mut();
    checked(
        unsafe {
            let current = GetCurrentProcess();
            DuplicateHandle(
                current,
                source,
                current,
                &mut result,
                0,
                1,
                DUPLICATE_SAME_ACCESS,
            )
        } != 0,
    )?;
    Ok(unsafe { OwnedHandle::from_raw_handle(result) })
}

struct Attributes {
    address: LPPROC_THREAD_ATTRIBUTE_LIST,
    _storage: Vec<usize>,
}

impl Attributes {
    fn process(handles: &[HANDLE], jobs: &[HANDLE]) -> Result<Self> {
        const ATTRIBUTE_COUNT: u32 = 2;
        let mut size = 0;
        // The first call returns the required size. The second uses aligned storage.
        unsafe { InitializeProcThreadAttributeList(null_mut(), ATTRIBUTE_COUNT, 0, &mut size) };
        let mut storage = vec![0; size.div_ceil(size_of::<usize>())];
        let address = storage.as_mut_ptr().cast();
        checked(
            unsafe { InitializeProcThreadAttributeList(address, ATTRIBUTE_COUNT, 0, &mut size) }
                != 0,
        )?;
        let attributes = Self {
            address,
            _storage: storage,
        };
        for (kind, values) in [
            (PROC_THREAD_ATTRIBUTE_HANDLE_LIST, handles),
            (PROC_THREAD_ATTRIBUTE_JOB_LIST, jobs),
        ] {
            checked(
                unsafe {
                    UpdateProcThreadAttribute(
                        attributes.address,
                        0,
                        kind as usize,
                        values.as_ptr().cast(),
                        size_of_val(values),
                        null_mut(),
                        null_mut(),
                    )
                } != 0,
            )?;
        }
        Ok(attributes)
    }
}

impl Drop for Attributes {
    fn drop(&mut self) {
        // The list must be destroyed before its backing storage is released.
        unsafe { DeleteProcThreadAttributeList(self.address) };
    }
}

fn quote(output: &mut Vec<u16>, arg: &OsStr) {
    const BACKSLASH: u16 = b'\\' as u16;
    const QUOTE: u16 = b'"' as u16;
    output.push(QUOTE);
    let mut slashes = 0;
    for character in arg.encode_wide() {
        if character == BACKSLASH {
            slashes += 1;
            continue;
        }
        output.extend(std::iter::repeat_n(BACKSLASH, slashes));
        if character == QUOTE {
            output.extend(std::iter::repeat_n(BACKSLASH, slashes + 1));
        }
        output.push(character);
        slashes = 0;
    }
    output.extend(std::iter::repeat_n(BACKSLASH, slashes * 2));
    output.push(QUOTE);
}

pub fn spawn(python: &Path, home: &Path, temp: &Path) -> Result<(Process, Input, Output)> {
    spawn_in_job(
        python,
        ["-I", "-m", "kotoyose.child_runner", "--home"]
            .map(OsStr::new)
            .into_iter()
            .chain([home.as_os_str(), OsStr::new("--temp"), temp.as_os_str()]),
    )
}

/// Create the process in a link-owned job so it is covered even during startup.
pub(crate) fn spawn_in_job<'a>(
    program: &Path,
    args: impl IntoIterator<Item = &'a OsStr>,
) -> Result<(Process, Input, Output)> {
    let job = job().context("cannot create process job")?;
    let (child_input, input) = io::pipe()?;
    let (output, child_output) = io::pipe()?;
    let stderr = OpenOptions::new().write(true).open("NUL")?;
    let child_input = inheritable(child_input.as_raw_handle())?;
    let child_output = inheritable(child_output.as_raw_handle())?;
    let stderr = inheritable(stderr.as_raw_handle())?;
    let handles = [
        child_input.as_raw_handle(),
        child_output.as_raw_handle(),
        stderr.as_raw_handle(),
    ];
    let jobs = [job.as_raw_handle()];
    let attributes = Attributes::process(&handles, &jobs)?;
    let mut startup = STARTUPINFOEXW::default();
    startup.StartupInfo.cb = size_of_val(&startup) as u32;
    startup.StartupInfo.dwFlags = STARTF_USESTDHANDLES;
    startup.StartupInfo.hStdInput = handles[0];
    startup.StartupInfo.hStdOutput = handles[1];
    startup.StartupInfo.hStdError = handles[2];
    startup.lpAttributeList = attributes.address;
    let path: Vec<u16> = program.as_os_str().encode_wide().chain([0]).collect();
    let mut command = Vec::new();
    quote(&mut command, program.as_os_str());
    for argument in args {
        command.push(b' ' as u16);
        quote(&mut command, argument);
    }
    command.push(0);
    let mut flags = EXTENDED_STARTUPINFO_PRESENT;
    if unsafe { GetConsoleCP() } == 0 {
        flags |= CREATE_NO_WINDOW;
    }
    let mut info = PROCESS_INFORMATION::default();
    checked(
        unsafe {
            CreateProcessW(
                path.as_ptr(),
                command.as_mut_ptr(),
                null(),
                null(),
                1,
                flags,
                null(),
                null(),
                &startup.StartupInfo,
                &mut info,
            )
        } != 0,
    )
    .context("cannot start process")?;
    let process = unsafe { OwnedHandle::from_raw_handle(info.hProcess) };
    drop(unsafe { OwnedHandle::from_raw_handle(info.hThread) });
    let input = std::fs::File::from(OwnedHandle::from(input));
    let output = std::fs::File::from(OwnedHandle::from(output));
    Ok((
        Process {
            job: Some(Arc::new(job)),
            process,
            pid: info.dwProcessId,
        },
        Box::new(tokio::fs::File::from_std(input)),
        Box::new(tokio::fs::File::from_std(output)),
    ))
}

#[cfg(test)]
mod tests {
    use super::*;

    fn limit_flags(job: &OwnedHandle) -> u32 {
        let mut limits = JOBOBJECT_EXTENDED_LIMIT_INFORMATION::default();
        checked(unsafe {
            QueryInformationJobObject(
                job.as_raw_handle(),
                JobObjectExtendedLimitInformation,
                (&raw mut limits).cast(),
                size_of_val(&limits) as u32,
                null_mut(),
            ) != 0
        })
        .unwrap();
        limits.BasicLimitInformation.LimitFlags
    }

    #[tokio::test]
    async fn releasing_a_completed_helper_clears_kill_on_close() {
        let test_binary = std::env::current_exe().unwrap();
        let (mut child, _input, _output) = spawn_in_job(
            &test_binary,
            [
                "--list",
                "releasing_a_completed_helper_clears_kill_on_close",
            ]
            .map(OsStr::new),
        )
        .unwrap();
        assert!(
            tokio::time::timeout(STOP_TIMEOUT, child.wait())
                .await
                .unwrap()
                .unwrap()
        );
        let job = child.job.clone().unwrap();
        assert_ne!(limit_flags(&job) & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0);
        child.release_job().unwrap();
        assert!(child.job.is_none());
        assert_eq!(limit_flags(&job) & JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE, 0);
    }
}
