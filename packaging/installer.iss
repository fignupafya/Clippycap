; Inno Setup script for Clippycap. Builds  dist\Clippycap-Setup.exe  from the one-folder PyInstaller
; build in  dist\Clippycap\  (so run packaging\build.ps1 -- or at least `pyinstaller packaging\clippycap.spec`
; -- first). Requires Inno Setup 6.1 or newer (for the built-in downloader). Compile with:
;
;   "C:\Program Files (x86)\Inno Setup 6\ISCC.exe" packaging\installer.iss
;
; packaging\build.ps1 does this automatically when Inno Setup is installed; if it isn't, install it from
; https://jrsoftware.org/isdl.php -- the script and the .exe are open and reproducible from source.
;
; What it does that's special: if ffmpeg isn't already available, it offers (a ticked task on the
; "Select Additional Tasks" page) to download BtbN's static ffmpeg/ffprobe build and drop it into
; %APPDATA%\Clippycap\bin\ -- exactly where the app's own "auto" detection looks first. If the user
; declines, or the download fails, the install still completes (ffmpeg is optional; the app can fetch
; it later from Settings > FFmpeg).

#define AppName        "Clippycap"
#define AppVersion     "0.1.0"
#define AppPublisher   "Clippycap"
#define AppExeName     "Clippycap.exe"
#define AppSourceDir   "..\dist\Clippycap"
#define FfmpegUrl      "https://github.com/BtbN/FFmpeg-Builds/releases/latest/download/ffmpeg-master-latest-win64-gpl.zip"

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
; A per-user install (no UAC prompt). {autopf} then resolves to %LocalAppData%\Programs\{#AppName},
; and {userappdata} is this user's %APPDATA% -- which is exactly the app's data dir (@appdata\Clippycap).
PrivilegesRequired=lowest
ArchitecturesAllowed=x64
ArchitecturesInstallIn64BitMode=x64
;SetupIconFile=clippycap.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; GroupDescription: "{cm:AdditionalIcons}"; Flags: unchecked
Name: "ffmpeg"; Description: "Download && install FFmpeg (~80 MB) - needed for clip thumbnails and trimming / cutting"; Check: ShouldOfferFfmpeg

[Files]
Source: "{#AppSourceDir}\*"; DestDir: "{app}"; Flags: recursesubdirs createallsubdirs ignoreversion

[Icons]
Name: "{group}\{#AppName}"; Filename: "{app}\{#AppExeName}"
Name: "{group}\Uninstall {#AppName}"; Filename: "{uninstallexe}"
Name: "{autodesktop}\{#AppName}"; Filename: "{app}\{#AppExeName}"; Tasks: desktopicon

[Run]
Filename: "{app}\{#AppExeName}"; Description: "{cm:LaunchProgram,{#AppName}}"; Flags: nowait postinstall skipifsilent

[Code]
var
  FfmpegChecked: Boolean;
  FfmpegPresent: Boolean;
  DownloadPage: TDownloadWizardPage;

function SQ(const S: String): String;        { wrap S in single quotes, for embedding in a PowerShell command }
begin
  Result := Chr(39) + S + Chr(39);
end;

function CmdSucceeds(const Cmd, Params: String): Boolean;
var
  ResultCode: Integer;
begin
  Result := Exec(Cmd, Params, '', SW_HIDE, ewWaitUntilTerminated, ResultCode) and (ResultCode = 0);
end;

function IsFfmpegPresent: Boolean;
begin
  if not FfmpegChecked then
  begin
    FfmpegChecked := True;
    FfmpegPresent :=
      FileExists(ExpandConstant('{userappdata}\Clippycap\bin\ffmpeg.exe')) or
      CmdSucceeds(ExpandConstant('{cmd}'), '/C where ffmpeg >nul 2>nul');
  end;
  Result := FfmpegPresent;
end;

function ShouldOfferFfmpeg: Boolean;
begin
  Result := not IsFfmpegPresent;            { task hidden (and not run) when ffmpeg is already around }
end;

function OnDownloadProgress(const Url, FileName: String; const Progress, ProgressMax: Int64): Boolean;
begin
  Result := True;                           { keep going (the page shows the progress bar itself) }
end;

procedure InitializeWizard;
begin
  DownloadPage := CreateDownloadPage(SetupMessage(msgWizardPreparing), SetupMessage(msgPreparingDesc), @OnDownloadProgress);
end;

function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if (CurPageID = wpReady) and IsTaskSelected('ffmpeg') then
  begin
    DownloadPage.Clear;
    DownloadPage.Add('{#FfmpegUrl}', 'ffmpeg.zip', '');     { empty hash = no verification (it's a moving "latest" build) }
    DownloadPage.Show;
    try
      try
        DownloadPage.Download;
      except
        if DownloadPage.AbortedByUser then
          Result := False
        else
          SuppressibleMsgBox(
            'Could not download FFmpeg:' + #13#10 + GetExceptionMessage + #13#10 + #13#10 +
            'Clippycap will still install. You can install FFmpeg later from Settings > FFmpeg inside the app.',
            mbInformation, MB_OK, IDOK);
      end;
    finally
      DownloadPage.Hide;
    end;
  end;
end;

procedure ExtractFfmpeg;
var
  ZipPath, DestDir, Ps: String;
  ResultCode: Integer;
begin
  ZipPath := ExpandConstant('{tmp}\ffmpeg.zip');
  if not FileExists(ZipPath) then exit;
  DestDir := ExpandConstant('{userappdata}\Clippycap\bin');
  ForceDirectories(DestDir);
  { Extract just ffmpeg.exe and ffprobe.exe (the archive is a single folder containing bin\...).      }
  { PowerShell's System.IO.Compression is on every supported Windows; no third-party unzip tool needed. }
  Ps :=
    '$ErrorActionPreference=' + SQ('Stop') + ';' +
    'Add-Type -AssemblyName System.IO.Compression.FileSystem;' +
    '$zip=[System.IO.Compression.ZipFile]::OpenRead(' + SQ(ZipPath) + ');' +
    'try{ foreach($e in $zip.Entries){' +
      'if(($e.Name -ieq ' + SQ('ffmpeg.exe') + ') -or ($e.Name -ieq ' + SQ('ffprobe.exe') + ')){' +
        '[System.IO.Compression.ZipFileExtensions]::ExtractToFile($e,(Join-Path ' + SQ(DestDir) + ' $e.Name),$true)' +
      '} } } finally{ $zip.Dispose() }';
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'),
              '-NoProfile -ExecutionPolicy Bypass -Command "' + Ps + '"',
              '', SW_HIDE, ewWaitUntilTerminated, ResultCode) or (ResultCode <> 0) then
    SuppressibleMsgBox(
      'FFmpeg was downloaded but could not be unpacked automatically. You can install it later from '
      + 'Settings > FFmpeg inside the app.', mbInformation, MB_OK, IDOK);
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
  if (CurStep = ssPostInstall) and IsTaskSelected('ffmpeg') then
    ExtractFfmpeg;
end;
