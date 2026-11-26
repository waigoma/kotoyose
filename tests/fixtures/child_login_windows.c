/* Fake official CLI for the installer smoke test. */
#include <windows.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <wchar.h>

int main(int argc, char **argv) {
    WCHAR marker[MAX_PATH];
    DWORD length = GetModuleFileNameW(NULL, marker, MAX_PATH);
    if (!length || length >= MAX_PATH) return 1;
    if (argc == 2 && !strcmp(argv[1], "--wait")) Sleep(INFINITE);
    if (argc == 2 && !strcmp(argv[1], "--hold")) {
        puts("ready");
        fflush(stdout);
        Sleep(INFINITE);
    }
    if (argc == 2 && !strcmp(argv[1], "--hold-lock")) {
        WCHAR path[MAX_PATH];
        DWORD size = GetEnvironmentVariableW(L"LOCALAPPDATA", path, MAX_PATH);
        if (!size || size + wcslen(L"\\kotoyose-child\\instance.lock") >= MAX_PATH) return 10;
        wcscat(path, L"\\kotoyose-child\\instance.lock");
        HANDLE file = CreateFileW(path, GENERIC_READ | GENERIC_WRITE,
            FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, NULL, OPEN_ALWAYS, 0, NULL);
        if (file == INVALID_HANDLE_VALUE || !LockFile(file, 0, 0, 1, 0)) return 11;
        puts("locked");
        fflush(stdout);
        Sleep(INFINITE);
    }
    if (argc == 3 && !strcmp(argv[1], "--stopped")) {
        HANDLE process = OpenProcess(SYNCHRONIZE, FALSE, strtoul(argv[2], NULL, 10));
        if (!process) return GetLastError() == ERROR_INVALID_PARAMETER ? 0 : 6;
        DWORD status = WaitForSingleObject(process, 5000);
        CloseHandle(process);
        return status == WAIT_OBJECT_0 ? 0 : 7;
    }
    WCHAR *name = wcsrchr(marker, L'\\');
    if (!name) return 1;
    WCHAR flag[MAX_PATH];
    wcscpy(flag, marker);
    wcscpy(flag + (name - marker) + 1, L"hold");

    if (argc == 4 && !strcmp(argv[1], "auth") && !strcmp(argv[2], "login")
        && !strcmp(argv[3], "--claudeai")) {
        DWORD mode;
        if (!GetConsoleMode(GetStdHandle(STD_INPUT_HANDLE), &mode)
            || !GetConsoleMode(GetStdHandle(STD_OUTPUT_HANDLE), &mode)) return 2;
        if (GetFileAttributesW(flag) != INVALID_FILE_ATTRIBUTES) {
            WCHAR command[MAX_PATH + 16];
            swprintf(command, MAX_PATH + 16, L"\"%ls\" --wait", marker);
            STARTUPINFOW startup = {0};
            PROCESS_INFORMATION child;
            startup.cb = sizeof(startup);
            if (!CreateProcessW(marker, command, NULL, NULL, FALSE, CREATE_NO_WINDOW, NULL, NULL, &startup, &child)) return 8;
            CloseHandle(child.hThread);
            CloseHandle(child.hProcess);
            wcscpy(name + 1, L"pids");
            FILE *pids = _wfopen(marker, L"w");
            if (!pids) return 9;
            fprintf(pids, "%lu %lu\n", GetCurrentProcessId(), child.dwProcessId);
            fclose(pids);
            Sleep(INFINITE);
        }
        wcscpy(name + 1, L"ok");
        HANDLE file = CreateFileW(marker, GENERIC_WRITE, 0, NULL, CREATE_NEW, FILE_ATTRIBUTE_NORMAL, NULL);
        if (file == INVALID_HANDLE_VALUE) return 3;
        CloseHandle(file);
        return 0;
    }
    if (argc == 4 && !strcmp(argv[1], "auth") && !strcmp(argv[2], "status")
        && !strcmp(argv[3], "--json")) {
        wcscpy(name + 1, L"ok");
        const char *json = GetFileAttributesW(marker) == INVALID_FILE_ATTRIBUTES
            ? "{\"loggedIn\":false}\n" : "{\"loggedIn\":true}\n";
        DWORD written;
        return WriteFile(GetStdHandle(STD_OUTPUT_HANDLE), json, (DWORD)strlen(json), &written, NULL) ? 0 : 4;
    }
    return 5;
}
