; アプリは x64 だけなので、installer も 64 bit にする (32 bit の Windows には入れない)。
Target amd64-unicode
RequestExecutionLevel user
SetCompressor /SOLID lzma
ShowInstDetails show
ShowUninstDetails show

!include "MUI2.nsh"
!include "LogicLib.nsh"
!include "Sections.nsh"
!include "x64.nsh"
!include "FileFunc.nsh"

!define PRODUCT "Kotoyose Child"
!define PRODUCT_ROOT "$LOCALAPPDATA\Programs\${PRODUCT}"
!define ARP "Software\Microsoft\Windows\CurrentVersion\Uninstall\KotoyoseChild"
!define RUN "Software\Microsoft\Windows\CurrentVersion\Run"
!define APP "$INSTDIR\$VersionDir\bin\kotoyose-child-app.exe"
!define BACKGROUND "$INSTDIR\$VersionDir\bin\kotoyose-child-background.exe"

Name "${PRODUCT} ${PRODUCT_VERSION}"
OutFile "${OUTPUT}"
InstallDir "${PRODUCT_ROOT}"
InstallDirRegKey HKCU "${ARP}" "InstallRoot"
VIProductVersion "${FILE_VERSION}"
VIAddVersionKey /LANG=1033 "ProductName" "${PRODUCT}"
VIAddVersionKey /LANG=1033 "FileDescription" "${PRODUCT} Setup"
VIAddVersionKey /LANG=1033 "FileVersion" "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=1033 "ProductVersion" "${PRODUCT_VERSION}"
VIAddVersionKey /LANG=1033 "LegalCopyright" "Kotoyose contributors"

Var CheckedDirectory
Var PreviousVersion
Var RuntimeFailed
Var InstallerMutex
Var OtherRoot
Var ShutdownRoot
Var VersionDir
Var AppUpdate
Var UpdateStaged
Var UpdateSelected
Var UpdateShortcut
Var PreviousBuildDirectory
Var PreviousDisplayVersion
Var PreviousRun

!macro SetupLogReset
    CreateDirectory "$LOCALAPPDATA\kotoyose-child"
    CreateDirectory "$LOCALAPPDATA\kotoyose-child\logs"
    ClearErrors
    FileOpen $R7 "$LOCALAPPDATA\kotoyose-child\logs\setup.log" w
    ${IfNot} ${Errors}
        FileWrite $R7 "setup ${PRODUCT_VERSION} (${BUILD_ID})$\r$\n"
        FileClose $R7
    ${EndIf}
    ClearErrors
!macroend

!macro SetupLog LINE
    ClearErrors
    FileOpen $R7 "$LOCALAPPDATA\kotoyose-child\logs\setup.log" a
    ${IfNot} ${Errors}
        FileSeek $R7 0 END
        FileWrite $R7 "${LINE}$\r$\n"
        FileClose $R7
    ${EndIf}
    ClearErrors
!macroend

!define MUI_ABORTWARNING
!insertmacro MUI_PAGE_WELCOME
!define MUI_COMPONENTSPAGE_SMALLDESC
!insertmacro MUI_PAGE_COMPONENTS
!define MUI_PAGE_CUSTOMFUNCTION_LEAVE CheckDirectory
!insertmacro MUI_PAGE_DIRECTORY
!insertmacro MUI_PAGE_INSTFILES
!define MUI_FINISHPAGE_RUN
!define MUI_FINISHPAGE_RUN_TEXT "Launch now"
!define MUI_FINISHPAGE_TEXT "Setup has installed Kotoyose Child. Launch now opens Settings, where you can add providers."
!define MUI_FINISHPAGE_RUN_FUNCTION LaunchChild
!define MUI_PAGE_CUSTOMFUNCTION_SHOW ShowFinish
!insertmacro MUI_PAGE_FINISH
!insertmacro MUI_UNPAGE_CONFIRM
!insertmacro MUI_UNPAGE_COMPONENTS
!insertmacro MUI_UNPAGE_INSTFILES
!insertmacro MUI_LANGUAGE "English"

Section "Core runtime" SEC_BASE
    SectionIn RO
SectionEnd

Section "Start at sign-in" SEC_AUTOSTART
SectionEnd

!insertmacro MUI_FUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_BASE} "Installs the app and its bundled Python runtime."
    !insertmacro MUI_DESCRIPTION_TEXT ${SEC_AUTOSTART} "Opens Kotoyose Child in the system tray when you sign in to Windows."
!insertmacro MUI_FUNCTION_DESCRIPTION_END

!macro AcquireSetupMutex
    ; The process owns this handle until the installer exits.
    System::Call 'kernel32::SetLastError(i 0)'
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "KotoyoseChildInstaller") p .r0 ?e'
    Pop $1
    ${If} $0 == 0
    ${OrIf} $1 == 183 ; ERROR_ALREADY_EXISTS
        ${If} $0 != 0
            System::Call 'kernel32::CloseHandle(p r0)'
        ${EndIf}
        MessageBox MB_ICONEXCLAMATION|MB_OK "Another setup or uninstall is already running." /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    StrCpy $InstallerMutex $0
!macroend

!macro CheckChildLock
    ; Match lock_state() in stop_child.c. Return the state in $0 and the Win32 error in $1.
    ; GENERIC_READ | GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE, OPEN_EXISTING
    System::Call 'kernel32::CreateFileW(w "$LOCALAPPDATA\kotoyose-child\instance.lock", i 0xC0000000, i 7, p 0, i 3, i 0, p 0) p .r2 ?e'
    Pop $1
    ${If} $2 == -1 ; INVALID_HANDLE_VALUE
        ${If} $1 == 2 ; ERROR_FILE_NOT_FOUND
        ${OrIf} $1 == 3 ; ERROR_PATH_NOT_FOUND
            StrCpy $0 "free"
        ${Else}
            StrCpy $0 "unknown"
        ${EndIf}
    ${Else}
        System::Call 'kernel32::LockFile(p r2, i 0, i 0, i 1, i 0) i .r0 ?e'
        Pop $1
        ${If} $0 != 0
            System::Call 'kernel32::UnlockFile(p r2, i 0, i 0, i 1, i 0)'
            StrCpy $0 "free"
        ${ElseIf} $1 == 33 ; ERROR_LOCK_VIOLATION
            StrCpy $0 "held"
        ${Else}
            StrCpy $0 "unknown"
        ${EndIf}
        System::Call 'kernel32::CloseHandle(p r2)'
    ${EndIf}
!macroend

!macro StopChild PREFIX
Function ${PREFIX}StopChild
    !insertmacro CheckChildLock
    ${If} $0 == "free"
        Return
    ${EndIf}
    ${If} $0 == "unknown"
        MessageBox MB_ICONSTOP|MB_OK "Setup could not check whether Kotoyose Child is running (Win32 error $1). Check access to the child state folder, then try again." /SD IDOK
        SetErrorLevel 1
        Quit
    ${EndIf}
    MessageBox MB_ICONQUESTION|MB_YESNO|MB_DEFBUTTON2 "Kotoyose Child is running. Close it and continue?" /SD IDYES IDYES close_child
    SetErrorLevel 1
    Quit

close_child:
    InitPluginsDir
    ${IfNot} ${FileExists} "$PLUGINSDIR\stop-child.exe"
        ; If extraction is blocked, nsExec reports the start failure.
        SetOverwrite try
        File /oname=$PLUGINSDIR\stop-child.exe "${PAYLOAD}/stop-child.exe"
        SetOverwrite lastused
    ${EndIf}
    nsExec::Exec /TIMEOUT=60000 '"$PLUGINSDIR\stop-child.exe" close "$LOCALAPPDATA\kotoyose-child" "$ShutdownRoot\."'
    Pop $0
    ${If} $0 == 0
        Return
    ${ElseIf} $0 == "error"
        MessageBox MB_ICONSTOP|MB_OK "Setup could not start the close helper (nsExec $0). Exit Kotoyose Child from the system tray or allow stop-child.exe, then try again." /SD IDOK
    ${ElseIf} $0 == "timeout"
        MessageBox MB_ICONSTOP|MB_OK "The close helper timed out (nsExec $0). Exit Kotoyose Child from the system tray, then try again." /SD IDOK
    ${Else}
        MessageBox MB_ICONSTOP|MB_OK "Setup could not close Kotoyose Child (helper exit code $0). Exit it from the system tray, then try again." /SD IDOK
    ${EndIf}
    SetErrorLevel 1
    Quit
FunctionEnd
!macroend

!insertmacro StopChild ""
!insertmacro StopChild "un."

Function .onInit
    SetShellVarContext current
    ${IfNot} ${RunningX64}
        MessageBox MB_ICONSTOP|MB_OK "Kotoyose Child requires Windows 11 x64." /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    SetRegView 64
    !insertmacro AcquireSetupMutex
    ReadRegStr $ShutdownRoot HKCU "${ARP}" "InstallRoot"
    ${If} $ShutdownRoot == ""
        ReadRegStr $ShutdownRoot HKCU "${ARP}" "InstallLocation"
    ${EndIf}
    ${If} $ShutdownRoot == ""
        StrCpy $ShutdownRoot "${PRODUCT_ROOT}"
    ${EndIf}
    StrCpy $VersionDir "${PRODUCT_VERSION}"
    ${GetParameters} $0
    ${GetOptions} $0 "/UPDATE" $1
    ${IfNot} ${Errors}
        StrCpy $AppUpdate "1"
        StrCpy $VersionDir "${PRODUCT_VERSION}-${BUILD_ID}"
        StrCpy $INSTDIR $ShutdownRoot
    ${EndIf}
    !insertmacro SetupLogReset
    !insertmacro SetupLog "init: app_update=$AppUpdate version_dir=$VersionDir"
    Call StopChild
FunctionEnd

Function LaunchChild
    SetOutPath "$INSTDIR\$VersionDir\bin"
    ClearErrors
    Exec '"${BACKGROUND}" --open-settings'
    ${If} ${Errors}
        !insertmacro SetupLog "launch: failed"
        ${If} $AppUpdate == "1"
            Call AbortInstall
        ${EndIf}
        SetErrorLevel 2
    ${ElseIf} $AppUpdate == "1"
        !insertmacro SetupLog "launch: ok"
        StrCpy $UpdateStaged "0"
        SetErrorLevel 0
    ${EndIf}
FunctionEnd

Function .onInstSuccess
    IfSilent 0 done
    ${If} $RuntimeFailed != "1"
        Call LaunchChild
    ${EndIf}
done:
FunctionEnd

Function ReleaseInstallerMutex
    ${If} $InstallerMutex != 0
        System::Call 'kernel32::CloseHandle(p $InstallerMutex)'
        StrCpy $InstallerMutex 0
    ${EndIf}
FunctionEnd

Function AcquireInstallerMutex
    System::Call 'kernel32::SetLastError(i 0)'
    System::Call 'kernel32::CreateMutexW(p 0, i 0, w "KotoyoseChildInstaller") p .r0 ?e'
    Pop $1
    ${If} $0 == 0
    ${OrIf} $1 == 183 ; ERROR_ALREADY_EXISTS
        ${If} $0 != 0
            System::Call 'kernel32::CloseHandle(p r0)'
        ${EndIf}
        StrCpy $InstallerMutex 0
    ${Else}
        StrCpy $InstallerMutex $0
    ${EndIf}
FunctionEnd

Function ProviderRepairPending
    StrCpy $0 0
    !include "${PAYLOAD}\provider-repairs.nsh"
FunctionEnd

; Run a child app command passed in $R0. Return the exit code or error token in $0.
; Prefer nsExec::ExecToLog to keep the console hidden and copy output into Setup.
; If nsExec cannot load or cannot start the process (for example when %TEMP%
; plugin execution is blocked by endpoint policy), fall back to built-in ExecWait.
Function RunChildApp
    Push "__nsexec_missing__"
    ClearErrors
    nsExec::ExecToLog '$R0'
    ${If} ${Errors}
        StrCpy $0 "__nsexec_missing__"
        StrCpy $R8 0
        ${DoWhile} $R8 < 4
            Pop $R9
            ${If} $R9 == "__nsexec_missing__"
                ${ExitDo}
            ${EndIf}
            IntOp $R8 $R8 + 1
        ${Loop}
    ${Else}
        Pop $0
        ${If} $0 != "__nsexec_missing__"
            StrCpy $R8 0
            ${DoWhile} $R8 < 4
                Pop $R9
                ${If} $R9 == "__nsexec_missing__"
                    ${ExitDo}
                ${EndIf}
                IntOp $R8 $R8 + 1
            ${Loop}
        ${EndIf}
    ${EndIf}
    ${If} $0 == "__nsexec_missing__"
    ${OrIf} $0 == "error"
    ${OrIf} $0 == $R0
    ${OrIf} $0 < 0
        !insertmacro SetupLog "run_child_app: nsexec=$0 fallback=ExecWait"
        DetailPrint "nsExec could not start the command ($0); falling back to ExecWait."
        ClearErrors
        ExecWait '$R0' $0
        ${If} ${Errors}
            StrCpy $0 "exec-error"
        ${EndIf}
        !insertmacro SetupLog "run_child_app: execwait=$0"
    ${Else}
        !insertmacro SetupLog "run_child_app: nsexec=$0"
    ${EndIf}
FunctionEnd

Function RollbackUpdate
    ${If} $UpdateStaged != "1"
        Return
    ${EndIf}
    SetOutPath "$INSTDIR"
    Delete "$SMPROGRAMS\${PRODUCT}.pending.lnk"
    ${If} $UpdateSelected == "1"
        ClearErrors
        ${If} $PreviousBuildDirectory == ""
            DeleteRegValue HKCU "${ARP}" "InstalledBuildDirectory"
        ${Else}
            WriteRegStr HKCU "${ARP}" "InstalledBuildDirectory" "$PreviousBuildDirectory"
        ${EndIf}
        WriteRegStr HKCU "${ARP}" "DisplayVersion" "$PreviousDisplayVersion"
        ${If} ${Errors}
            Return
        ${EndIf}
        ${If} $PreviousRun == ""
            DeleteRegValue HKCU "${RUN}" "KotoyoseChild"
        ${Else}
            WriteRegStr HKCU "${RUN}" "KotoyoseChild" "$PreviousRun"
        ${EndIf}
    ${EndIf}
    ${If} $UpdateShortcut == "1"
        ${If} ${FileExists} "$PLUGINSDIR\${PRODUCT}.lnk"
            CopyFiles /SILENT "$PLUGINSDIR\${PRODUCT}.lnk" "$SMPROGRAMS"
        ${Else}
            Delete "$SMPROGRAMS\${PRODUCT}.lnk"
        ${EndIf}
    ${EndIf}
    ClearErrors
    RMDir /r "$INSTDIR\$VersionDir"
    ${If} ${Errors}
        Sleep 100
        ClearErrors
        RMDir /r "$INSTDIR\$VersionDir"
        ${If} ${Errors}
            FileOpen $0 "$INSTDIR\$VersionDir\kotoyose-child.pending" w
            FileClose $0
        ${EndIf}
    ${EndIf}
    StrCpy $UpdateStaged "0"
FunctionEnd

Function AbortInstall
    !insertmacro SetupLog "abort_install: staged=$UpdateStaged selected=$UpdateSelected shortcut=$UpdateShortcut"
    Call RollbackUpdate
    SetErrorLevel 2
    Quit
FunctionEnd

Function .onInstFailed
    Call RollbackUpdate
    SetErrorLevel 2
FunctionEnd

Function CheckDirectory
directory_start:
    GetFullPathName $INSTDIR "$INSTDIR"
    ${GetRoot} "$INSTDIR" $0
    ${If} $0 == ""
    ${OrIf} $INSTDIR == "$0"
    ${OrIf} $INSTDIR == "$0\"
        MessageBox MB_ICONSTOP|MB_OK "Choose a folder inside the drive. Setup cannot install in the drive root." /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}

    ReadRegStr $1 HKCU "${ARP}" "InstallRoot"
    ReadRegStr $PreviousVersion HKCU "${ARP}" "InstalledBuildDirectory"
    ${If} $PreviousVersion == ""
        ReadRegStr $PreviousVersion HKCU "${ARP}" "DisplayVersion"
    ${EndIf}
    ${If} $1 != ""
        GetFullPathName $1 "$1"
        ${If} $INSTDIR != $1
            ${If} ${FileExists} "$1\Uninstall.exe"
            ${OrIf} ${FileExists} "$1\$PreviousVersion\bin\kotoyose-child-app.exe"
                StrCpy $OtherRoot $1
                MessageBox MB_ICONQUESTION|MB_YESNOCANCEL "Kotoyose Child is already installed in $OtherRoot.$\r$\n$\r$\nYes uses that folder.$\r$\nNo uninstalls it, then installs in the folder you chose.$\r$\nCancel closes Setup." /SD IDCANCEL IDYES reuse IDNO remove_other
                SetErrorLevel 1
                Abort
            ${EndIf}
            StrCpy $PreviousVersion ""
        ${EndIf}
    ${EndIf}
    Goto path_length

remove_other:
    ${IfNot} ${FileExists} "$OtherRoot\Uninstall.exe"
        MessageBox MB_ICONSTOP|MB_OK "The existing installation has no uninstaller. Remove it from $OtherRoot, then run Setup again." /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    Call StopChild
    ; The uninstaller takes the same mutex. Release it, wait, then take it back.
    Call ReleaseInstallerMutex
    ExecWait '"$OtherRoot\Uninstall.exe" /S _?=$OtherRoot' $2
    Call AcquireInstallerMutex
    ${If} $InstallerMutex == 0
        MessageBox MB_ICONSTOP|MB_OK "Another setup started while the existing installation was being removed." /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    ${If} $2 != 0
        MessageBox MB_ICONSTOP|MB_OK "Could not uninstall the existing installation. Remove it from Windows Settings, then run Setup again." /SD IDOK
        SetErrorLevel 1
        Abort
    ${EndIf}
    StrCpy $PreviousVersion ""

path_length:
    ${If} $CheckedDirectory != $INSTDIR
        StrLen $0 "$INSTDIR\$VersionDir"
        ${If} $0 >= 120
            DetailPrint "The installation path may exceed Windows path limits after Python packages are added."
            MessageBox MB_ICONEXCLAMATION|MB_YESNO "Python packages may exceed Windows path limits in this folder. Long paths stay under Windows' own setting. Choose No to pick a shorter path, or Yes to continue." /SD IDYES IDYES checked
            SetErrorLevel 1
            Abort
        ${EndIf}
    ${EndIf}
checked:
    StrCpy $CheckedDirectory $INSTDIR
    SetErrorLevel 0
    Return
reuse:
    StrCpy $INSTDIR $OtherRoot
    Goto directory_start
FunctionEnd

Function ShowFinish
    Call ProviderRepairPending
    ${If} $0 == 1
        ${If} $RuntimeFailed == "1"
            ${NSD_SetText} $mui.FinishPage.Text "The base runtime could not be installed. Run Setup again. Providers marked Needs reinstall can also be repaired in Providers once the runtime is ready."
        ${Else}
            ${NSD_SetText} $mui.FinishPage.Text "When Kotoyose Child starts, it tries to reinstall previously installed providers allowed by your child policy. You can repair the others in Providers."
        ${EndIf}
    ${EndIf}
FunctionEnd

Section -Install
    !insertmacro SetupLog "section_install: start"
    Call CheckDirectory
    Call StopChild
    ${If} $AppUpdate == "1"
        ReadRegStr $PreviousBuildDirectory HKCU "${ARP}" "InstalledBuildDirectory"
        ReadRegStr $PreviousDisplayVersion HKCU "${ARP}" "DisplayVersion"
        ReadRegStr $PreviousRun HKCU "${RUN}" "KotoyoseChild"
        !insertmacro SetupLog "update_meta: prev=$PreviousVersion prev_dir=$PreviousBuildDirectory"
        ${If} $VersionDir == $PreviousVersion
            !insertmacro SetupLog "update_abort: same_version_dir"
            SetErrorLevel 2
            Quit
        ${EndIf}
        System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$VersionDir") i .r0'
        ${If} $0 != -1
            IntOp $0 $0 & 0x400 ; FILE_ATTRIBUTE_REPARSE_POINT
            ${If} $0 != 0
                !insertmacro SetupLog "update_abort: reparse_point"
                SetErrorLevel 2
                Quit
            ${EndIf}
        ${EndIf}
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\$VersionDir\*"
    ${AndIfNot} ${FileExists} "$INSTDIR\$VersionDir\kotoyose-child.install"
    ${AndIfNot} ${FileExists} "$INSTDIR\$VersionDir\kotoyose-child.pending"
        ; Earlier installers had no marker, and only used the default location.
        ${If} $INSTDIR != "${PRODUCT_ROOT}"
        ${OrIfNot} ${FileExists} "${APP}"
        ${OrIfNot} ${FileExists} "$INSTDIR\$VersionDir\python\python.exe"
            MessageBox MB_ICONSTOP|MB_OK "This location already contains a folder named ${PRODUCT_VERSION}. Choose a different installation directory." /SD IDOK
            SetErrorLevel 1
            Abort
        ${EndIf}
    ${EndIf}
    ClearErrors
    ${If} $AppUpdate == "1"
        StrCpy $UpdateStaged "1"
    ${EndIf}
    SetOutPath "$INSTDIR\$VersionDir"
    ${If} $AppUpdate == "1"
        FileOpen $0 "$INSTDIR\$VersionDir\kotoyose-child.pending" w
        FileClose $0
        ${If} ${Errors}
            Call AbortInstall
        ${EndIf}
    ${EndIf}
    SetOutPath "$INSTDIR\$VersionDir\bin"
    File "${PAYLOAD}/bin/kotoyose-child-app.exe"
    File "${PAYLOAD}/bin/kotoyose-child-background.exe"
    SetOutPath "$INSTDIR\$VersionDir\bin\parts"
    File "${PAYLOAD}/bin/parts/*.whl"
    File "${PAYLOAD}/bin/parts/requirements*.txt"
    SetOutPath "$INSTDIR\$VersionDir\bin\parts\runtime"
    File "${PAYLOAD}/bin/parts/runtime/*.whl"
    SetOutPath "$INSTDIR\$VersionDir\python"
    File /r "${PAYLOAD}/python/*"
    SetOutPath "$INSTDIR\$VersionDir\licenses"
    File /r "${PAYLOAD}/licenses/*"

    ${If} ${Errors}
        !insertmacro SetupLog "extract: failed"
        Call AbortInstall
    ${EndIf}
    !insertmacro SetupLog "extract: ok prev=$PreviousVersion"
    SetOutPath "$INSTDIR"
    DetailPrint "Installing the bundled base runtime packages."
    ${If} $PreviousVersion == ""
        StrCpy $R0 '"${APP}" repair'
    ${Else}
        StrCpy $R0 '"${APP}" repair --previous "$PreviousVersion"'
    ${EndIf}
    Call RunChildApp
    !insertmacro SetupLog "repair: status=$0"
    ${If} $0 != 0
        StrCpy $RuntimeFailed "1"
        DetailPrint "Could not install the base runtime packages ($0)."
        ${If} $AppUpdate == "1"
            Call AbortInstall
        ${EndIf}
        MessageBox MB_ICONEXCLAMATION|MB_OK "The app was installed, but the runtime packages could not be installed (status: $0). See repair.log in the child log folder (default: $LOCALAPPDATA\kotoyose-child\logs\repair.log). Run Setup again or run kotoyose-child-app.exe repair from the installed bin folder." /SD IDOK
        SetErrorLevel 2
    ${EndIf}
    Call ProviderRepairPending
    ${If} $0 == 1
        DetailPrint "When Kotoyose Child starts, it tries to reinstall previously installed providers allowed by your child policy. You can repair the others in Providers."
    ${EndIf}

    ClearErrors
    FileOpen $0 "$INSTDIR\$VersionDir\kotoyose-child.install" w
    FileClose $0
    FileOpen $0 "$INSTDIR\$VersionDir\bin\child-build-id" w
    FileWrite $0 "${BUILD_ID}"
    FileClose $0
    ${If} ${Errors}
        Call AbortInstall
    ${EndIf}
    WriteUninstaller "$INSTDIR\Uninstall.exe"
    WriteRegStr HKCU "${ARP}" "DisplayName" "${PRODUCT}"
    WriteRegStr HKCU "${ARP}" "InstallLocation" "$INSTDIR"
    WriteRegStr HKCU "${ARP}" "InstallRoot" "$INSTDIR"
    WriteRegStr HKCU "${ARP}" "UninstallString" '"$INSTDIR\Uninstall.exe"'
    WriteRegDWORD HKCU "${ARP}" "NoModify" 1
    WriteRegDWORD HKCU "${ARP}" "NoRepair" 1
    ${If} ${Errors}
        Call AbortInstall
    ${EndIf}
    WriteRegStr HKCU "${ARP}" "InstalledBuildDirectory" "$VersionDir"
    ${If} ${Errors}
        Call AbortInstall
    ${EndIf}
    StrCpy $UpdateSelected "1"
    WriteRegStr HKCU "${ARP}" "DisplayVersion" "${PRODUCT_VERSION}"
    ${If} ${Errors}
        Call AbortInstall
    ${EndIf}
    StrCpy $1 "off"
    ${If} $AppUpdate == "1"
        ${If} $PreviousRun != ""
            StrCpy $1 "on"
        ${EndIf}
    ${ElseIf} ${SectionIsSelected} ${SEC_AUTOSTART}
        StrCpy $1 "on"
    ${EndIf}
    StrCpy $R0 '"${APP}" autostart $1'
    Call RunChildApp
    ${If} $0 == "exec-error"
    ${OrIf} $0 < 0
        ${If} $1 == "on"
            ${If} ${FileExists} "${BACKGROUND}"
                ClearErrors
                WriteRegStr HKCU "${RUN}" "KotoyoseChild" '"${BACKGROUND}"'
                ${IfNot} ${Errors}
                    DetailPrint "Registered start at sign-in directly."
                    StrCpy $0 0
                ${EndIf}
            ${EndIf}
        ${Else}
            ClearErrors
            ReadRegStr $2 HKCU "${RUN}" "KotoyoseChild"
            ${If} ${Errors}
                StrCpy $0 0
            ${Else}
                ClearErrors
                DeleteRegValue HKCU "${RUN}" "KotoyoseChild"
                ${IfNot} ${Errors}
                    DetailPrint "Removed start at sign-in directly."
                    StrCpy $0 0
                ${EndIf}
            ${EndIf}
        ${EndIf}
    ${EndIf}
    ${If} $0 != 0
        !insertmacro SetupLog "autostart: status=$0"
        DetailPrint "Could not change start at sign-in ($0)."
        MessageBox MB_ICONEXCLAMATION|MB_OK "Could not change start at sign-in (status: $0). Open Settings from the system tray to check." /SD IDOK
        ${If} $AppUpdate != "1"
            SetErrorLevel 2
        ${EndIf}
    ${Else}
        !insertmacro SetupLog "autostart: ok ($1)"
    ${EndIf}
    SetOutPath "$INSTDIR\$VersionDir\bin"
    ClearErrors
    ${If} $AppUpdate == "1"
        ${If} ${FileExists} "$SMPROGRAMS\${PRODUCT}.lnk"
            CopyFiles /SILENT "$SMPROGRAMS\${PRODUCT}.lnk" "$PLUGINSDIR"
        ${EndIf}
        CreateShortCut "$SMPROGRAMS\${PRODUCT}.pending.lnk" "${BACKGROUND}"
        ${If} ${Errors}
            !insertmacro SetupLog "shortcut: create_failed"
            Call AbortInstall
        ${EndIf}
        System::Call 'kernel32::MoveFileExW(w "$SMPROGRAMS\${PRODUCT}.pending.lnk", w "$SMPROGRAMS\${PRODUCT}.lnk", i 1) i .r0'
        ${If} $0 == 0
            !insertmacro SetupLog "shortcut: move_failed"
            Call AbortInstall
        ${EndIf}
        StrCpy $UpdateShortcut "1"
        Delete "$INSTDIR\$VersionDir\kotoyose-child.pending"
        ${If} ${Errors}
            !insertmacro SetupLog "pending: delete_failed"
            Call AbortInstall
        ${EndIf}
        !insertmacro SetupLog "install_section: done"
    ${Else}
        CreateShortCut "$SMPROGRAMS\${PRODUCT}.lnk" "${BACKGROUND}"
        !insertmacro SetupLog "install_section: done"
    ${EndIf}
SectionEnd

Function un.onInit
    SetShellVarContext current
    SetRegView 64
    ReadRegStr $VersionDir HKCU "${ARP}" "InstalledBuildDirectory"
    ${If} $VersionDir == ""
        StrCpy $VersionDir "${PRODUCT_VERSION}"
    ${EndIf}
    IfFileExists "$INSTDIR\$VersionDir\kotoyose-child.install" +3
        SetErrorLevel 1
        Abort
    !insertmacro AcquireSetupMutex
    StrCpy $ShutdownRoot "$INSTDIR"
    Call un.StopChild
FunctionEnd

!macro RemoveInstalledFolder NAME
    ${If} ${FileExists} "$INSTDIR\$1\${NAME}"
        RMDir /r "$INSTDIR\$1\${NAME}"
    ${EndIf}
!macroend

Section "un.Program files" UN_SEC_BASE
    SectionIn RO
    Call un.StopChild
    SetOutPath "$LOCALAPPDATA"
    ; Remove marked versions and older versions identified by their app executables.
    FindFirst $0 $1 "$INSTDIR\*"
    ${DoWhile} $1 != ""
        ${If} $1 != "."
        ${AndIf} $1 != ".."
        ${AndIf} ${FileExists} "$INSTDIR\$1\*"
            Call un.RemoveVersion
        ${EndIf}
        FindNext $0 $1
    ${Loop}
    FindClose $0
    Delete "$INSTDIR\Uninstall.exe"
    RMDir "$INSTDIR"
    ReadRegStr $0 HKCU "${ARP}" "InstallRoot"
    ${If} $0 == $INSTDIR
        DeleteRegValue HKCU "${RUN}" "KotoyoseChild"
        Delete "$SMPROGRAMS\${PRODUCT}.lnk"
        DeleteRegKey HKCU "${ARP}"
    ${EndIf}
SectionEnd

Function un.RemoveVersion
    System::Call 'kernel32::GetFileAttributesW(w "$INSTDIR\$1") i .r2'
    IntOp $2 $2 & 0x400 ; FILE_ATTRIBUTE_REPARSE_POINT
    ${If} $2 != 0
        Return
    ${EndIf}
    ${If} ${FileExists} "$INSTDIR\$1\kotoyose-child.install"
    ${OrIf} ${FileExists} "$INSTDIR\$1\kotoyose-child.pending"
    ${OrIf} ${FileExists} "$INSTDIR\$1\bin\kotoyose-child-app.exe"
    ${OrIf} ${FileExists} "$INSTDIR\$1\bin\kotoyose-child-background.exe"
            ClearErrors
            !insertmacro RemoveInstalledFolder "bin"
            !insertmacro RemoveInstalledFolder "python"
            !insertmacro RemoveInstalledFolder "licenses"
            ${If} ${Errors}
                FindClose $0
                MessageBox MB_ICONSTOP|MB_OK "Could not remove the program files. Exit Kotoyose Child and try again." /SD IDOK
                SetErrorLevel 1
                Abort
            ${EndIf}
            Delete "$INSTDIR\$1\kotoyose-child.install"
            Delete "$INSTDIR\$1\kotoyose-child.pending"
            RMDir "$INSTDIR\$1"
    ${EndIf}
FunctionEnd

Section /o "un.Settings and logs" UN_SEC_STATE
    RMDir /r "$LOCALAPPDATA\kotoyose-child"
SectionEnd

!insertmacro MUI_UNFUNCTION_DESCRIPTION_BEGIN
    !insertmacro MUI_DESCRIPTION_TEXT ${UN_SEC_BASE} "Removes the app and its installed runtimes."
    !insertmacro MUI_DESCRIPTION_TEXT ${UN_SEC_STATE} "Also removes enrollment, policy, settings, and logs."
!insertmacro MUI_UNFUNCTION_DESCRIPTION_END
