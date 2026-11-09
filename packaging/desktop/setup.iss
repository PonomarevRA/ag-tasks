; Shared GUI installer. Compile on Windows with Inno Setup 6.
#ifndef ProductName
  #error Product definitions must be supplied by build_installer.py
#endif
[Setup]
AppId=AGDesktop.{#ProductId}
AppName={#ProductName}
AppVersion={#ProductVersion}
DefaultDirName={localappdata}\Programs\{#ProductName}
DefaultGroupName={#ProductName}
PrivilegesRequired=lowest
OutputDir={#Output}
OutputBaseFilename={#OutputName}
Compression=lzma2
SolidCompression=yes
WizardStyle=modern
DisableProgramGroupPage=yes
CloseApplications=no
RestartApplications=no
UninstallDisplayIcon={app}\{#ProductBinary}
[Files]
Source: "{#ProductManifest}"; DestDir: "{app}"; DestName: ".ag-desktop-product.json"; Flags: ignoreversion
Source: "windows-preflight.ps1"; Flags: dontcopy
Source: "{#Payload}\*"; DestDir: "{app}"; Flags: ignoreversion recursesubdirs createallsubdirs
[Icons]
Name: "{autoprograms}\{#ProductName}"; Filename: "{app}\{#ProductBinary}"
[Tasks]
Name: "background"; Description: "Keep running in the tray when the window closes"; Flags: checkedonce
Name: "login"; Description: "Start when I sign in"; Flags: unchecked
[Registry]
Root: HKCU; Subkey: "Software\Microsoft\Windows\CurrentVersion\Run"; ValueType: string; ValueName: "AGDesktop.{#ProductId}"; ValueData: """{app}\{#ProductBinary}"""; Tasks: login; Flags: uninsdeletevalue
[Run]
Filename: "{app}\{#ProductBinary}"; Description: "Open {#ProductName}"; Flags: nowait postinstall skipifsilent
[Code]
function AGGetTickCount: Cardinal; external 'GetTickCount@kernel32.dll stdcall';
var
  StoragePage: TInputDirWizardPage;
  OldData, Locator, PreviousApplication, PreviousPort: String;
  InstallCompleted: Boolean;

// Decode a string property only; unknown or malformed configuration fails visibly.
function JsonString(const Text, Key: String): String;
var P: Integer; C: Char;
begin
  Result := '';
  P := Pos('"' + Key + '"', Text);
  if P = 0 then RaiseException('Installation settings are incomplete: ' + Key);
  P := P + Length(Key) + 2;
  while (P <= Length(Text)) and (Text[P] <= ' ') do P := P + 1;
  if (P > Length(Text)) or (Text[P] <> ':') then RaiseException('Invalid installation settings');
  P := P + 1;
  while (P <= Length(Text)) and (Text[P] <= ' ') do P := P + 1;
  if (P > Length(Text)) or (Text[P] <> '"') then RaiseException('Invalid installation path');
  P := P + 1;
  while P <= Length(Text) do begin
    C := Text[P]; P := P + 1;
    if C = '"' then exit;
    if C = '\' then begin
      if P > Length(Text) then RaiseException('Invalid installation escape');
      C := Text[P]; P := P + 1;
      if (C <> '\') and (C <> '"') and (C <> '/') then RaiseException('Unsupported installation escape');
    end;
    if C < ' ' then RaiseException('Invalid installation path');
    Result := Result + C;
  end;
  RaiseException('Unterminated installation path');
end;

// Preserve a validated optional port; updates cannot silently change the service origin.
function JsonPort(const Text: String): String;
var P: Integer;
begin
  Result := '';
  P := Pos('"preferredPort"', Text);
  if P = 0 then exit;
  P := P + Length('"preferredPort"');
  while (P <= Length(Text)) and ((Text[P] <= ' ') or (Text[P] = ':')) do P := P + 1;
  while (P <= Length(Text)) and (Text[P] >= '0') and (Text[P] <= '9') do begin Result := Result + Text[P]; P := P + 1; end;
  if (StrToIntDef(Result, 0) < 1024) or (StrToIntDef(Result, 0) > 65535) then RaiseException('Invalid existing preferred port');
end;

// JSON path values are escaped before writing, including UNC separators.
function QuoteJson(Value: String): String;
begin
  StringChangeEx(Value, '\', '\\', True);
  StringChangeEx(Value, '"', '\"', True);
  Result := '"' + Value + '"';
end;

// Existing paths are loaded once; changing nonempty data roots is not an update.
procedure InitializeWizard();
var Raw: AnsiString; Text, Compact, Tasks: String;
begin
  Locator := ExpandConstant('{localappdata}\AGDesktop\{#ProductId}\installation.json');
  OldData := ExpandConstant('{localappdata}\{#DataName}');
  StoragePage := CreateInputDirPage(wpSelectDir, 'Artifacts and backups', 'Choose external storage folders',
    'Updates replace application files only. Keep existing data folders when updating.', False, '');
  StoragePage.Add('Artifacts and data:'); StoragePage.Add('Backups:');
  StoragePage.Values[0] := OldData;
  StoragePage.Values[1] := OldData + '-backups';
  if FileExists(Locator) then begin
    if not LoadStringFromFile(Locator, Raw) then RaiseException('Cannot read installation settings');
    Text := UTF8Decode(Raw);
    if (JsonString(Text, 'schemaVersion') <> 'ag-desktop/install-v1') or
       (JsonString(Text, 'appId') <> '{#ProductId}') then RaiseException('Unsupported installation settings');
    PreviousPort := JsonPort(Text);
    Compact := Text; StringChangeEx(Compact, ' ', '', True);
    StringChangeEx(Compact, #13, '', True); StringChangeEx(Compact, #10, '', True);
    Tasks := '';
    if Pos('"closeToTray":false', Compact) = 0 then Tasks := 'background';
    if Pos('"launchAtLogin":true', Compact) > 0 then begin
      if Tasks <> '' then Tasks := Tasks + ',';
      Tasks := Tasks + 'login';
    end;
    WizardSelectTasks(Tasks);
    OldData := JsonString(Text, 'dataDirectory');
    StoragePage.Values[0] := OldData; StoragePage.Values[1] := JsonString(Text, 'backupDirectory');
    WizardForm.DirEdit.Text := JsonString(Text, 'applicationPath');
  end;
end;

// Compare whole canonical directory boundaries, never raw substring prefixes.
function Nested(const First, Second: String): Boolean;
begin
  Result := Pos(AddBackslash(Lowercase(ExpandFileName(First))), AddBackslash(Lowercase(ExpandFileName(Second)))) = 1;
end;

// Fail closed for different data roots and overlapping paths before copying payloads.
function NextButtonClick(CurPageID: Integer): Boolean;
begin
  Result := True;
  if CurPageID = StoragePage.ID then begin
    if (CompareText(OldData, StoragePage.Values[0]) <> 0) and DirExists(OldData) then begin
      MsgBox('Existing data folder found. Keep its path for this update; migrate separately.', mbError, MB_OK); Result := False;
    end else if Nested(WizardDirValue, StoragePage.Values[0]) or Nested(StoragePage.Values[0], WizardDirValue) or
      Nested(WizardDirValue, StoragePage.Values[1]) or Nested(StoragePage.Values[1], WizardDirValue) or
      Nested(StoragePage.Values[0], StoragePage.Values[1]) or Nested(StoragePage.Values[1], StoragePage.Values[0]) then begin
      MsgBox('Application, artifacts and backups must be separate folders.', mbError, MB_OK); Result := False;
    end;
  end;
end;

// Write locator after application install. Uninstall never removes artifact or backup folders.
procedure CurStepChanged(CurStep: TSetupStep);
var Data, Temp, Tray, Login: String;
begin
  if CurStep = ssDone then InstallCompleted := True;
  if CurStep = ssPostInstall then begin
    ForceDirectories(StoragePage.Values[0]); ForceDirectories(StoragePage.Values[1]);
    ForceDirectories(ExtractFileDir(Locator));
    Tray := 'false'; Login := 'false';
    if WizardIsTaskSelected('background') then Tray := 'true';
    if WizardIsTaskSelected('login') then Login := 'true';
    Data := '{"schemaVersion":"ag-desktop/install-v1","appId":"{#ProductId}","applicationPath":' + QuoteJson(WizardDirValue) +
      ',"dataDirectory":' + QuoteJson(StoragePage.Values[0]) + ',"backupDirectory":' + QuoteJson(StoragePage.Values[1]) +
      ',"closeToTray":' + Tray + ',"launchAtLogin":' + Login;
    if PreviousPort <> '' then Data := Data + ',"preferredPort":' + PreviousPort;
    Data := Data + '}';
    Temp := Locator + '.new';
    if not SaveStringToFile(Temp, UTF8Encode(Data), False) then RaiseException('Cannot save installation settings');
    if FileExists(Locator) then begin
      if not CopyFile(Locator, Locator + '.previous', False) then RaiseException('Cannot preserve installation settings');
      if not DeleteFile(Locator) then RaiseException('Cannot replace installation settings');
    end;
    if not RenameFile(Temp, Locator) then RaiseException('Cannot publish installation settings');
  end;
end;

// Empty destinations need no product marker; nonempty folders must prove ownership.
function DirectoryNotEmpty(const Directory: String): Boolean;
var Item: TFindRec;
begin
  Result := False;
  if FindFirst(AddBackslash(Directory) + '*', Item) then begin
    try
      repeat
        if (Item.Name <> '.') and (Item.Name <> '..') then begin Result := True; exit; end;
      until not FindNext(Item);
    finally
      FindClose(Item);
    end;
  end;
end;

// Run a fixed bundled policy helper before Inno copies any application files.
function PrepareToInstall(var NeedsRestart: Boolean): String;
var ExitCode: Integer; ErrorPath, Detail, Arguments: String; Raw: AnsiString;
begin
  Result := '';
  ExtractTemporaryFile('windows-preflight.ps1');
  ErrorPath := ExpandConstant('{tmp}\ag-desktop-preflight.txt');
  Arguments := '-NoProfile -NonInteractive -ExecutionPolicy Bypass -File ' + AddQuotes(ExpandConstant('{tmp}\windows-preflight.ps1')) +
    ' -ApplicationRoot ' + AddQuotes(WizardDirValue) + ' -DataDirectory ' + AddQuotes(StoragePage.Values[0]) +
    ' -BackupDirectory ' + AddQuotes(StoragePage.Values[1]) + ' -ErrorFile ' + AddQuotes(ErrorPath);
  if not Exec(ExpandConstant('{sys}\WindowsPowerShell\v1.0\powershell.exe'), Arguments, '', SW_HIDE, ewWaitUntilTerminated, ExitCode) or (ExitCode <> 0) then begin
    Detail := 'Cannot verify inactive application and safe storage paths.';
    if LoadStringFromFile(ErrorPath, Raw) then Detail := UTF8Decode(Raw);
    Result := Detail;
  end;
  if (Result = '') and DirExists(WizardDirValue) then begin
    if DirectoryNotEmpty(WizardDirValue) then begin
    if not LoadStringFromFile(AddBackslash(WizardDirValue) + '.ag-desktop-product.json', Raw) then begin
      Result := 'Existing application has no installation identity. Choose an empty destination.';
      exit;
    end;
    if JsonString(UTF8Decode(Raw), 'appId') <> '{#ProductId}' then begin
      Result := 'The selected folder belongs to another product.';
      exit;
    end;
    end;
    PreviousApplication := WizardDirValue + '.previous-' + GetDateTimeString('yyyymmddhhnnss', '-', ':') + '-' + IntToStr(AGGetTickCount);
    if not RenameFile(WizardDirValue, PreviousApplication) then begin
      Result := 'Cannot stage inactive previous application for rollback.';
      PreviousApplication := '';
    end;
  end;
end;

// Failed/canceled installs restore only the reviewed application folder and prior locator.
procedure DeinitializeSetup();
begin
  if not InstallCompleted and (PreviousApplication <> '') then begin
    if DirExists(WizardDirValue) then DelTree(WizardDirValue, True, True, True);
    RenameFile(PreviousApplication, WizardDirValue);
    if FileExists(Locator + '.previous') then CopyFile(Locator + '.previous', Locator, False);
  end;
end;
