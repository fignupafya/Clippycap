; Inno Setup script for Clippycap. Builds  dist\Clippycap-Setup.exe  from the one-folder PyInstaller
; build in  dist\Clippycap\  + a standalone ffmpeg/ffprobe in  ..\bin\  (so run build.ps1, which prepares
; both; or at least run `pyinstaller packaging\clippycap.spec` and `packaging\get_ffmpeg.ps1` first).
; Requires Inno Setup 6.1 or newer (for the built-in downloader). Compile with:
;
;   "<Inno Setup 6 folder>\ISCC.exe" packaging\installer.iss
;
; build.ps1 (at the repo root) does all of this automatically when Inno Setup is installed, and then
; moves Clippycap-Setup.exe to the repo root; the script + the .exe are open and reproducible from source.
;
; What's special: this is a per-user install (no UAC). It BUNDLES ffmpeg/ffprobe (-> {app}\bin\, where
; the app's "auto" detection finds them) so the app works fully out of the box -- thumbnails, trimming --
; with no second download. On the "Select Additional Tasks" page it also offers (ticked) to install the
; Microsoft Edge WebView2 Runtime if it's missing (Clippycap's app window uses it; without it the app
; falls back to a chromeless Chrome/Edge "--app" window). FFmpeg is GPL -- see THIRD_PARTY_NOTICES.txt,
; which is installed alongside the app.

#define AppName        "Clippycap"
#define AppVersion     "0.2.0"
#define AppPublisher   "Clippycap"
#define AppExeName     "Clippycap.exe"
#define AppSourceDir   "..\dist\Clippycap"
#define WebView2Url    "https://go.microsoft.com/fwlink/p/?LinkId=2124703"

[Setup]
; A stable, unique GUID for this product (so upgrades replace, rather than stack).
AppId={{8E5C2F2A-4C1E-4E2A-9E7B-C71005CAFE01}
AppName={#AppName}
AppVersion={#AppVersion}
AppPublisher={#AppPublisher}
DefaultDirName={autopf}\{#AppName}
DefaultGroupName={#AppName}
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
OutputDir=..\dist
OutputBaseFilename=Clippycap-Setup
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern
; A per-user install (no UAC prompt). {autopf} then resolves to %LocalAppData%\Programs\{#AppName}.
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
SetupIconFile=clippycap.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "webview2"; Description: "Install the Microsoft Edge WebView2 Runtime (~2 MB) - Clippycap's app window uses it"; Check: ShouldOfferWebView2

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion
; bundled ffmpeg/ffprobe (the standalone static build that build.ps1 / get_ffmpeg.ps1 puts in ..\bin\)
Source: "..\bin\ffmpeg.exe";  DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\bin\ffprobe.exe"; DestDir: "{app}\bin"; Flags: ignoreversion
Source: "..\THIRD_PARTY_NOTICES.txt"; DestDir: "{app}"; Flags: ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  WebView2Checked, WebView2Present: Boolean;
  DownloadPage: TDownloadWizardPage;

function IsWebView2Present: Boolean;
var v: String;
begin
  if not WebView2Checked then
  begin
    WebView2Checked := True;
    // The Evergreen WebView2 Runtime records its version in the value 'pv' under the EdgeUpdate
    // "Clients" key for GUID F3017226-FE2A-4295-8BDF-00C3A9A7E4C5. Where the key lives depends on
    // which installer wrote it:
    //   - 64-bit machine-wide -> SOFTWARE\Microsoft\EdgeUpdate\Clients\... in the 64-bit view (HKLM64)
    //   - 32-bit / older / Win10 in-box runtime -> SOFTWARE\WOW6432Node\Microsoft\..., which the
    //     32-bit view of HKLM (HKLM32) sees as SOFTWARE\Microsoft\EdgeUpdate\Clients\...
    //   - per-user install -> HKCU\SOFTWARE\Microsoft\EdgeUpdate\Clients\...
    // The plain ``HKLM`` we used previously only looked at the 64-bit view, so a system whose
    // WebView2 lived under WOW6432Node (common on Win10) reported "missing" and the installer
    // offered + ran the bootstrapper, which then exited non-zero (it tried to update machine-wide
    // without admin) and the user saw a spurious "could not install" message.
    WebView2Present :=
      (RegQueryStringValue(HKLM64, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
      (RegQueryStringValue(HKLM32, 'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0')) or
      (RegQueryStringValue(HKCU,   'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}', 'pv', v) and (v <> '') and (v <> '0.0.0.0'));
  end;
  Result := WebView2Present;
end;

function ShouldOfferWebView2: Boolean;
begin
  Result := not IsWebView2Present;            { task hidden (and not run) when WebView2 is already there }
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;                             { keep going (the page shows the progress bar itself) }
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID <> wpReady) or (not IsTaskSelected('webview2')) then exit;
  DownloadPage.Clear;
  DownloadPage.Add('{#WebView2Url}', 'MicrosoftEdgeWebView2Setup.exe', '');   { '' hash = no verification }
  DownloadPage.Show;
  try
    try
      DownloadPage.Download;
    except
      if DownloadPage.AbortedByUser then
        Result := False
      else
        SuppressibleMsgBox(
          'Could not download the Edge WebView2 Runtime:' + #13#10 + GetExceptionMessage + #13#10 + #13#10 +
          'Clippycap will still install -- it''ll run in a Chrome/Edge app-mode window until WebView2 is present.',
          mbInformation, MB_OK, IDOK);
    end;
  finally
    DownloadPage.Hide;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Setup: String;
  ResultCode: Integer;
begin
  if (CurStep <> ssPostInstall) or (not IsTaskSelected('webview2')) then exit;
  Setup := ExpandConstant('{tmp}\MicrosoftEdgeWebView2Setup.exe');
  if not FileExists(Setup) then exit;
  if Exec(Setup, '/silent /install', '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0) then exit;
  // The bootstrapper sometimes exits non-zero even though the runtime is fine -- e.g. it found
  // a machine-wide install and tried to update it without admin (per-user setups can't), or its
  // /silent path raced with another Edge update. Re-probe before complaining: if WebView2 is now
  // visible from any of the three registry paths we check above, the app window will work, so
  // there's nothing to alarm the user about.
  WebView2Checked := False;
  if IsWebView2Present then exit;
  SuppressibleMsgBox(
    'The Microsoft Edge WebView2 Runtime could not be installed automatically. Clippycap will still '
    + 'run (in a Chrome/Edge app-mode window) -- or install the Runtime yourself from Microsoft and reopen.',
    mbInformation, MB_OK, IDOK);
end;
