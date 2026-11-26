#define WIN32_LEAN_AND_MEAN
#include <windows.h>
#include <tlhelp32.h>
#include <wchar.h>

enum { LOCK_FREE, LOCK_HELD, LOCK_UNKNOWN };
enum { PATH_CAPACITY = 4096, GRACE_MS = 15000, FORCE_MS = 5000 };
typedef wchar_t Path[PATH_CAPACITY];

static const wchar_t app[] = L"\\bin\\kotoyose-child-app.exe";
static const wchar_t background[] = L"\\bin\\kotoyose-child-background.exe";
static const wchar_t python[] = L"\\python\\python.exe";

typedef struct {
    Path app, background, python;
} Installation;

static int lock_state(const wchar_t *path)
{
    HANDLE file = CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING, 0, NULL);
    if (file == INVALID_HANDLE_VALUE) {
        DWORD error = GetLastError();
        return error == ERROR_FILE_NOT_FOUND || error == ERROR_PATH_NOT_FOUND ? LOCK_FREE : LOCK_UNKNOWN;
    }
    BOOL locked = LockFile(file, 0, 0, 1, 0);
    DWORD error = GetLastError();
    if (locked) UnlockFile(file, 0, 0, 1, 0);
    CloseHandle(file);
    if (locked) return LOCK_FREE;
    return error == ERROR_LOCK_VIOLATION ? LOCK_HELD : LOCK_UNKNOWN;
}

static DWORD remaining(ULONGLONG deadline)
{
    ULONGLONG now = GetTickCount64();
    return now < deadline ? (DWORD)(deadline - now) : 0;
}

static int wait_for_lock(const wchar_t *lock, ULONGLONG deadline)
{
    int state;
    DWORD delay;
    while ((state = lock_state(lock)) == LOCK_HELD && (delay = remaining(deadline)) != 0)
        Sleep(delay < 100 ? delay : 100);
    return state;
}

static BOOL final_path(const wchar_t *path, BOOL directory, Path result)
{
    HANDLE file = CreateFileW(path, FILE_READ_ATTRIBUTES,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_EXISTING,
        directory ? FILE_FLAG_BACKUP_SEMANTICS : 0, NULL);
    if (file == INVALID_HANDLE_VALUE) return FALSE;
    BY_HANDLE_FILE_INFORMATION info;
    DWORD size = 0;
    if (GetFileInformationByHandle(file, &info) &&
        ((info.dwFileAttributes & FILE_ATTRIBUTE_DIRECTORY) != 0) == directory)
        size = GetFinalPathNameByHandleW(file, result, PATH_CAPACITY, FILE_NAME_NORMALIZED);
    CloseHandle(file);
    return size > 0 && size < PATH_CAPACITY;
}

static BOOL read_registry(const wchar_t *key, const wchar_t *name, Path value)
{
    DWORD bytes = sizeof(Path);
    return RegGetValueW(HKEY_CURRENT_USER, key, name, RRF_RT_REG_SZ, NULL, value, &bytes) == ERROR_SUCCESS;
}

static void installed_file(const wchar_t *root, const wchar_t *version,
    const wchar_t *suffix, Path target)
{
    Path expected, resolved;
    int size = swprintf(expected, PATH_CAPACITY, L"%ls\\%ls%ls", root, version, suffix);
    // The directory and executable must resolve to this slot under the install root.
    if (size > 0 && size < PATH_CAPACITY && final_path(expected, FALSE, resolved) &&
        wcscmp(expected, resolved) == 0)
        wcscpy(target, resolved);
}

static void installed_version(const wchar_t *root, const wchar_t *version, Installation *result)
{
    if (version[0] < L'0' || version[0] > L'9' ||
        version[wcslen(version) - 1] == L'.' ||
        version[wcsspn(version, L"0123456789.ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz-")])
        return;
    installed_file(root, version, app, result->app);
    installed_file(root, version, background, result->background);
    installed_file(root, version, python, result->python);
}

static void run_version(const wchar_t *root, Installation *result)
{
    Path command, resolved;
    if (!read_registry(L"Software\\Microsoft\\Windows\\CurrentVersion\\Run", L"KotoyoseChild", command))
        return;
    const wchar_t *path = command;
    if (command[0] == L'"') {
        wchar_t *quote = wcschr(command + 1, L'"');
        if (!quote || quote[1]) return;
        *quote = L'\0';
        path++;
    }
    size_t length = wcslen(root);
    if (!final_path(path, FALSE, resolved) || wcsncmp(resolved, root, length) != 0 ||
        resolved[length] != L'\\')
        return;
    wchar_t *version = resolved + length + 1;
    wchar_t *suffix = wcschr(version, L'\\');
    if (!suffix || (wcscmp(suffix, app) != 0 && wcscmp(suffix, background) != 0)) return;
    *suffix = L'\0';
    installed_version(root, version, result);
}

static void request_quit(const wchar_t *exe, ULONGLONG deadline)
{
    wchar_t command[PATH_CAPACITY + 8];
    STARTUPINFOW startup = { .cb = sizeof(startup) };
    PROCESS_INFORMATION process;
    swprintf(command, ARRAYSIZE(command), L"\"%ls\" quit", exe);
    if (CreateProcessW(exe, command, NULL, NULL, FALSE, CREATE_NO_WINDOW,
        NULL, NULL, &startup, &process)) {
        CloseHandle(process.hThread);
        WaitForSingleObject(process.hProcess, remaining(deadline));
        CloseHandle(process.hProcess);
    }
}

typedef union {
    TOKEN_USER user;
    BYTE storage[sizeof(TOKEN_USER) + SECURITY_MAX_SID_SIZE];
} ProcessUser;

static BOOL process_user(HANDLE process, ProcessUser *user)
{
    HANDLE token;
    DWORD size;
    if (!OpenProcessToken(process, TOKEN_QUERY, &token)) return FALSE;
    BOOL found = GetTokenInformation(token, TokenUser, user, sizeof(*user), &size);
    CloseHandle(token);
    return found;
}

static void terminate_installed(const Installation installs[2], BOOL runtime, ULONGLONG deadline)
{
    ProcessUser owner;
    if (!process_user(GetCurrentProcess(), &owner)) return;
    HANDLE snapshot = CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0);
    if (snapshot == INVALID_HANDLE_VALUE) return;
    PROCESSENTRY32W entry = { .dwSize = sizeof(entry) };
    for (BOOL found = Process32FirstW(snapshot, &entry); found && remaining(deadline);
        found = Process32NextW(snapshot, &entry)) {
        HANDLE process = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_TERMINATE | SYNCHRONIZE,
            FALSE, entry.th32ProcessID);
        if (!process) continue;
        Path path, image;
        DWORD size = PATH_CAPACITY;
        if (QueryFullProcessImageNameW(process, 0, path, &size) && final_path(path, FALSE, image)) {
            for (size_t i = 0; i < 2; i++) {
                const Installation *target = &installs[i];
                BOOL matches = runtime ? wcscmp(image, target->python) == 0 :
                    wcscmp(image, target->app) == 0 || wcscmp(image, target->background) == 0;
                if (matches) {
                    ProcessUser user;
                    // Identity checks and termination use the same handle, even if PIDs are reused.
                    if (process_user(process, &user) && EqualSid(user.user.User.Sid, owner.user.User.Sid) &&
                        TerminateProcess(process, 1))
                        WaitForSingleObject(process, remaining(deadline));
                    break;
                }
            }
        }
        CloseHandle(process);
    }
    CloseHandle(snapshot);
}

static int close_child(const wchar_t *lock, const wchar_t *home, const wchar_t *install_root)
{
    int state = lock_state(lock);
    if (state != LOCK_HELD) return state;

    Path root, version;
    Installation installs[2] = {0};
    if (!final_path(install_root, TRUE, root) || root[wcslen(root) - 1] == L'\\') return LOCK_HELD;
    run_version(root, &installs[0]);
    const wchar_t *uninstall = L"Software\\Microsoft\\Windows\\CurrentVersion\\Uninstall\\KotoyoseChild";
    if (read_registry(uninstall, L"InstalledBuildDirectory", version) ||
        read_registry(uninstall, L"DisplayVersion", version))
        installed_version(root, version, &installs[1]);

    if (!SetEnvironmentVariableW(L"KOTOYOSE_CHILD_HOME", home)) return LOCK_UNKNOWN;
    ULONGLONG deadline = GetTickCount64() + GRACE_MS;
    for (size_t i = 0; i < ARRAYSIZE(installs) && remaining(deadline); i++) {
        if (installs[i].app[0] && (i == 0 || wcscmp(installs[0].app, installs[i].app) != 0)) {
            state = lock_state(lock);
            if (state != LOCK_HELD) return state;
            request_quit(installs[i].app, deadline);
        }
    }
    state = wait_for_lock(lock, deadline);
    if (state != LOCK_HELD) return state;

    deadline = GetTickCount64() + FORCE_MS;
    terminate_installed(installs, FALSE, deadline);
    terminate_installed(installs, TRUE, deadline);
    return wait_for_lock(lock, deadline);
}

int wmain(int argc, wchar_t **argv)
{
    if (argc != 4 || wcscmp(argv[1], L"close") != 0) return LOCK_UNKNOWN;
    Path lock;
    int size = swprintf(lock, PATH_CAPACITY, L"%ls\\instance.lock", argv[2]);
    if (size <= 0 || size >= PATH_CAPACITY) return LOCK_UNKNOWN;
    return close_child(lock, argv[2], argv[3]);
}
