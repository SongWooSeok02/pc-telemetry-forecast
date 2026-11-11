<#
S1-06 설치 구성(관리자 Windows PowerShell 5.1 이상). 정식 설치 파일(MSI 등)은 S3-01 범위.
S1-02a 결과를 반영한다:
  - 수집 서비스는 LocalSystem, 자동 시작(LocalService로는 CPU 센서를 읽지 못함, S1-02a 5.5).
  - S2-02: 실패 시 재시작(60초·60초·5분, 하루 뒤 초기화). failureflag=1로 종료 코드 오류(시작 실패)도 복구 대상.
    이벤트 로그 원본 PcTelemetryAgent(Application) 등록: 시작·정상 종료·시작 실패·권한 점검 기록.
  - S2-02 최소 권한: LocalSystem 계정은 유지하되(PawnIO 장치 접근) 필요 권한을 SeChangeNotifyPrivilege 하나로 줄인다
    (sc privs, 서비스 SID unrestricted). PC-A에서 이 상태로 CPU 온도·전력·클럭 포함 센서 13/15 ok(나머지 2개는 팬 미노출).
  - 서비스 경로는 따옴표로 감싼다(S1-02a에서 PowerShell 5.1이 binPath 따옴표를 제거한 문제).
  - CPU 온도·전력·클럭은 PawnIO 2.2.0 필요. 설치하지 않고 확인만 한다(설치·동의는 사용자 절차).
  - UI(셸)는 일반 사용자 권한(asInvoker). 센서·장비 토큰에 접근하지 않는다.
  - %ProgramData%\PcTelemetry: SYSTEM·Administrators만 허용(S2-03 Credentials.psm1의 상위 경로 조건).
    status\만 Users 읽기 허용(셸이 읽는 공개 상태 파일).
  - WebView2 Evergreen 런타임이 없으면 동봉 부트스트래퍼(서명 확인)로 PC 단위 설치.

  .\Install-PcTelemetry.ps1 -DashboardUrl https://dashboard.example.com/
      [-ServerUrl https://api.example.com -MachineId <uuid> -TokenFile <path>]   전송 설정(S2-03 연동 전 임시)
      [-Source <배포 폴더>] [-SkipWebView2] [-NoStart] [-ReportPath <json>]

업데이트: 같은 명령을 다시 실행한다. 서비스를 멈추고 프로그램 파일만 교체하며 %ProgramData% 상태·미전송 데이터는 유지한다.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)][string]$DashboardUrl,
    [string]$ServerUrl,
    [string]$MachineId,
    [string]$TokenFile,
    [string]$Source,
    [switch]$SkipWebView2,
    [switch]$NoStart,
    [string]$ReportPath
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2
# Windows PowerShell 5.1은 param 기본값에서 $PSScriptRoot가 비어 있다.
if (-not $Source) { $Source = $PSScriptRoot }

$ServiceName = 'PcTelemetryAgent'
$InstallDir = Join-Path $env:ProgramFiles 'PcTelemetry'
$DataRoot = Join-Path $env:ProgramData 'PcTelemetry'
$StateDir = Join-Path $DataRoot 'state'
$StatusDir = Join-Path $DataRoot 'status'
$Shortcut = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\PC Telemetry.lnk'
$WebView2Client = 'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}'
$Sid = @{ System = 'S-1-5-18'; Admins = 'S-1-5-32-544'; Users = 'S-1-5-32-545' }

$report = [ordered]@{ task = 'S1-06'; action = 'install'; started_at_utc = (Get-Date).ToUniversalTime().ToString('o'); steps = [ordered]@{} }
function Step([string]$name, $value) { $report.steps[$name] = $value; Write-Output ("[{0}] {1}" -f $name, ($value | ConvertTo-Json -Compress -Depth 4)) }
function Save-Report {
    $report.finished_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    if ($ReportPath) { $report | ConvertTo-Json -Depth 6 | Set-Content -Path $ReportPath -Encoding UTF8 }
}

try {
    # ---- 0. 사전 조건 ----
    $principal = New-Object Security.Principal.WindowsPrincipal ([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'run from an elevated (administrator) PowerShell' }
    if (-not [Environment]::Is64BitOperatingSystem) { throw 'x64 Windows is required' }
    $os = Get-CimInstance Win32_OperatingSystem
    if ([int]$os.BuildNumber -lt 17763) { throw "unsupported Windows build $($os.BuildNumber) (Windows 10 1809+ / 11 required)" }
    foreach ($f in 'agent\PcTelemetry.Agent.exe', 'shell\PcTelemetry.Shell.exe') {
        if (-not (Test-Path (Join-Path $Source $f))) { throw "missing $f in $Source (run Publish-PcTelemetry.ps1)" }
    }
    $upload = [bool]$ServerUrl
    if ($upload -and (-not $MachineId -or -not $TokenFile)) { throw '-ServerUrl requires -MachineId and -TokenFile' }
    if ($upload) {
        [void][guid]::Parse($MachineId)
        $u = [Uri]$ServerUrl
        if ($u.Scheme -ne 'https' -and -not $u.IsLoopback) { throw 'server must be https (loopback http only for development)' }
        if (-not (Test-Path $TokenFile)) { throw 'token file not found' }
    }
    Step 'preflight' ([ordered]@{ os = "$($os.Caption) $($os.Version)"; build = [int]$os.BuildNumber; upload_configured = $upload })

    # ---- 1. PawnIO (S1-02a: CPU 온도·전력·클럭 필요, 설치는 하지 않음) ----
    $pawn = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO' -ErrorAction SilentlyContinue
    $pawnVersion = if ($pawn) { $pawn.DisplayVersion } else { $null }
    Step 'pawnio' ([ordered]@{
        installed = [bool]$pawn; version = $pawnVersion; expected = '2.2.0'
        note = if (-not $pawn) { 'CPU temperature/power/clock will be reported as unsupported until PawnIO 2.2.0 is installed' } elseif ($pawnVersion -notlike '2.2.0*') { 'version differs from the S1-02a validated 2.2.0' } else { 'ok' }
    })

    # ---- 2. WebView2 Evergreen 런타임 ----
    function Get-WebView2 {
        foreach ($hive in 'HKLM:', 'HKCU:') {
            $k = Get-ItemProperty "$hive\$WebView2Client" -ErrorAction SilentlyContinue
            if (-not $k) { $k = Get-ItemProperty "$hive\$($WebView2Client -replace 'WOW6432Node\\','')" -ErrorAction SilentlyContinue }
            if ($k -and $k.pv -and $k.pv -ne '0.0.0.0') { return [ordered]@{ version = $k.pv; scope = if ($hive -eq 'HKLM:') { 'per_machine' } else { 'per_user' }; location = $k.location } }
        }
        return $null
    }
    $wv = Get-WebView2
    $wvAction = 'present'
    if (-not $wv -and -not $SkipWebView2) {
        $setup = Join-Path $Source 'shell\redist\MicrosoftEdgeWebview2Setup.exe'
        if (Test-Path $setup) {
            $sig = Get-AuthenticodeSignature $setup
            if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') { throw 'WebView2 bootstrapper signature invalid' }
            $p = Start-Process $setup -ArgumentList '/silent', '/install' -Wait -PassThru
            $wvAction = "bootstrapper_exit_$($p.ExitCode)"
            $wv = Get-WebView2
        } else { $wvAction = 'missing_no_bootstrapper' }
    } elseif (-not $wv) { $wvAction = 'skipped' }
    Step 'webview2' ([ordered]@{ action = $wvAction; runtime = $wv })

    # ---- 3. 기존 서비스 정지(업데이트) ----
    $existing = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($existing -and $existing.Status -ne 'Stopped') {
        Stop-Service $ServiceName -Force
        $existing.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
    }
    Step 'existing_service' ([ordered]@{ present = [bool]$existing; upgrade = [bool]$existing })

    # ---- 4. 프로그램 파일(Program Files, 기본 ACL: 사용자 읽기·실행) ----
    New-Item -ItemType Directory -Force $InstallDir | Out-Null
    foreach ($part in 'agent', 'shell') {
        $target = Join-Path $InstallDir $part
        if (Test-Path $target) { Remove-Item -Recurse -Force $target }
        Copy-Item -Recurse (Join-Path $Source $part) $target
    }
    $manifest = Join-Path $Source 'manifest.json'
    if (Test-Path $manifest) { Copy-Item $manifest (Join-Path $InstallDir 'manifest.json') -Force }
    Copy-Item (Join-Path $Source 'Uninstall-PcTelemetry.ps1') $InstallDir -Force -ErrorAction SilentlyContinue
    $agentExe = Join-Path $InstallDir 'agent\PcTelemetry.Agent.exe'
    $shellExe = Join-Path $InstallDir 'shell\PcTelemetry.Shell.exe'
    Step 'files' ([ordered]@{
        install_dir = $InstallDir
        # exe는 공통 실행 호스트라 코드가 바뀌어도 같을 수 있다. 실제 코드는 dll이다.
        agent_dll_sha256 = (Get-FileHash (Join-Path $InstallDir 'agent\PcTelemetry.Agent.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
        shell_dll_sha256 = (Get-FileHash (Join-Path $InstallDir 'shell\PcTelemetry.Shell.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
        shell_core_dll_sha256 = (Get-FileHash (Join-Path $InstallDir 'shell\PcTelemetry.Shell.Core.dll') -Algorithm SHA256).Hash.ToLowerInvariant()
        agent_version = (Get-Item $agentExe).VersionInfo.FileVersion
        shell_version = (Get-Item $shellExe).VersionInfo.FileVersion
    })

    # ---- 5. 데이터 폴더 ACL ----
    function Set-ProtectedAcl([string]$path, [string[]]$fullSids, [string[]]$readSids) {
        New-Item -ItemType Directory -Force $path | Out-Null
        $item = Get-Item -LiteralPath $path -Force
        if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw "reparse point not allowed: $path" }
        $acl = New-Object Security.AccessControl.DirectorySecurity
        $acl.SetAccessRuleProtection($true, $false)
        $acl.SetOwner((New-Object Security.Principal.SecurityIdentifier $Sid.Admins))
        foreach ($s in $fullSids) {
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule ((New-Object Security.Principal.SecurityIdentifier $s), 'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
        foreach ($s in $readSids) {
            $acl.AddAccessRule((New-Object Security.AccessControl.FileSystemAccessRule ((New-Object Security.Principal.SecurityIdentifier $s), 'ReadAndExecute', 'ContainerInherit,ObjectInherit', 'None', 'Allow')))
        }
        Set-Acl -LiteralPath $path -AclObject $acl
    }
    function Describe-Acl([string]$path) {
        $acl = Get-Acl -LiteralPath $path
        [ordered]@{
            owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
            protected = $acl.AreAccessRulesProtected
            allow = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | Where-Object AccessControlType -eq 'Allow' |
                ForEach-Object { "$($_.IdentityReference.Value):$($_.FileSystemRights)" })
        }
    }
    Set-ProtectedAcl $DataRoot @($Sid.System, $Sid.Admins) @()
    Set-ProtectedAcl $StateDir @($Sid.System, $Sid.Admins) @()
    Set-ProtectedAcl $StatusDir @($Sid.System, $Sid.Admins) @($Sid.Users)
    Step 'data_acl' ([ordered]@{ root = Describe-Acl $DataRoot; state = Describe-Acl $StateDir; status = Describe-Acl $StatusDir })

    # ---- 6. 전송 설정(선택). 토큰은 상태 폴더(SYSTEM·Administrators 전용)에 복사한다. S2-03 DPAPI 연동 전 임시. ----
    $serviceArgs = @('service', '--state-dir', "`"$StateDir`"")
    if ($upload) {
        $tokenTarget = Join-Path $StateDir 'device.token'
        Copy-Item $TokenFile $tokenTarget -Force
        $serviceArgs += @('--server', $ServerUrl, '--machine-id', $MachineId, '--token-file', "`"$tokenTarget`"")
    }

    # ---- 7. 서비스 등록: LocalSystem, 자동 시작, 따옴표 경로, 실패 시 재시작 ----
    $binPath = "`"$agentExe`" " + ($serviceArgs -join ' ')
    if (-not (Get-Service $ServiceName -ErrorAction SilentlyContinue)) {
        New-Service -Name $ServiceName -BinaryPathName $binPath -DisplayName 'PC Telemetry Agent' -StartupType Automatic `
            -Description 'PC Telemetry sensor collection service (LocalSystem). Keeps running when the PC Telemetry window is closed.' | Out-Null
    }
    # PowerShell 5.1 New-Service/sc.exe가 따옴표를 바꿀 수 있어 레지스트리에 정확한 값을 쓰고 다시 읽어 확인한다.
    $svcKey = "HKLM:\SYSTEM\CurrentControlSet\Services\$ServiceName"
    Set-ItemProperty $svcKey -Name ImagePath -Value $binPath -Type ExpandString
    Set-ItemProperty $svcKey -Name ObjectName -Value 'LocalSystem'
    Set-ItemProperty $svcKey -Name Start -Value 2 -Type DWord   # Automatic
    & sc.exe failure $ServiceName reset= 86400 actions= restart/60000/restart/60000/restart/300000 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe failure exited $LASTEXITCODE" }
    # 프로세스 충돌뿐 아니라 서비스가 오류 종료 코드로 멈춰도(시작 실패·권한 오류) 복구 동작을 적용한다(S2-02).
    & sc.exe failureflag $ServiceName 1 | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe failureflag exited $LASTEXITCODE" }
    # 필요 이상 권한을 주지 않는다: 토큰에서 SeDebug·SeLoadDriver·SeTcb·SeBackup 등을 뺀다.
    # 필요 권한 목록(RequiredPrivileges)은 서비스 SID 유형이 none이 아니어야 적용된다.
    & sc.exe sidtype $ServiceName unrestricted | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe sidtype exited $LASTEXITCODE" }
    & sc.exe privs $ServiceName SeChangeNotifyPrivilege | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "sc.exe privs exited $LASTEXITCODE" }
    # 서비스가 상태 폴더를 못 쓰는 경우에도 원인을 남기도록 이벤트 로그 원본을 등록한다.
    if (-not [Diagnostics.EventLog]::SourceExists($ServiceName)) { New-EventLog -LogName Application -Source $ServiceName }
    $svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
    if (-not $svc.PathName.StartsWith('"')) { throw 'service path is not quoted' }
    # sc.exe 조회 출력은 표시 언어에 따라 번역되므로 레지스트리 값을 읽는다.
    # FailureActions(REG_BINARY, SERVICE_FAILURE_ACTIONS): 12바이트 위치에 동작 수, 20바이트부터 (종류, 지연 ms) 쌍.
    $svcReg = Get-ItemProperty $svcKey
    $fa = [byte[]]$svcReg.FailureActions
    $recovery = @(for ($i = 0; $i -lt [BitConverter]::ToInt32($fa, 12); $i++) {
        $o = 20 + 8 * $i
        if ([BitConverter]::ToInt32($fa, $o) -eq 1) { [BitConverter]::ToInt32($fa, $o + 4) }   # 1 = SC_ACTION_RESTART
    })
    Step 'service' ([ordered]@{
        name = $ServiceName; account = $svc.StartName; start_mode = $svc.StartMode; path_quoted = $svc.PathName.StartsWith('"'); upload_configured = $upload
        recovery = $recovery
        recovery_reset_s = [BitConverter]::ToInt32($fa, 0)
        recovery_on_error_exit = $svcReg.FailureActionsOnNonCrashFailures -eq 1
        service_sid_type = $svcReg.ServiceSidType      # 1 = unrestricted
        required_privileges = @($svcReg.RequiredPrivileges)
        event_source = [Diagnostics.EventLog]::SourceExists($ServiceName)
    })

    # ---- 8. 셸 설정·바로 가기 ----
    $shellJson = [ordered]@{ dashboard_url = $DashboardUrl; agent_service_name = $ServiceName } | ConvertTo-Json
    [IO.File]::WriteAllText((Join-Path $InstallDir 'shell\shell.json'), $shellJson, (New-Object Text.UTF8Encoding $false))
    $check = Join-Path $env:TEMP ("pctf-shell-check-{0}.json" -f [guid]::NewGuid().ToString('N'))
    $p = Start-Process $shellExe -ArgumentList '--check', '--out', "`"$check`"" -Wait -PassThru
    $checkResult = if (Test-Path $check) { Get-Content $check -Raw | ConvertFrom-Json } else { $null }
    Remove-Item $check -ErrorAction SilentlyContinue
    if ($p.ExitCode -eq 3) { throw "shell configuration rejected: $($checkResult.config.error)" }
    $wsh = New-Object -ComObject WScript.Shell
    $lnk = $wsh.CreateShortcut($Shortcut)
    $lnk.TargetPath = $shellExe
    $lnk.WorkingDirectory = Split-Path $shellExe
    $lnk.Description = 'PC Telemetry dashboard'
    $lnk.Save()
    Step 'shell' ([ordered]@{ check_exit = $p.ExitCode; screen = $checkResult.screen; runtime = $checkResult.runtime.version; shortcut = (Test-Path $Shortcut) })

    # ---- 9. 서비스 시작과 공개 상태 확인 ----
    if (-not $NoStart) {
        Start-Service $ServiceName
        (Get-Service $ServiceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(60))
        $statusFile = Join-Path $StatusDir 'agent-status.json'
        $deadline = (Get-Date).AddSeconds(30); $status = $null
        while ((Get-Date) -lt $deadline) {
            if (Test-Path $statusFile) { $status = Get-Content $statusFile -Raw | ConvertFrom-Json; if ($status.collecting) { break } }
            Start-Sleep -Seconds 1
        }
        Step 'start' ([ordered]@{ service = (Get-Service $ServiceName).Status.ToString(); status_file = [bool]$status; collecting = if ($status) { $status.collecting } else { $null } })
    }
    $report.success = $true
}
catch {
    $report.success = $false
    $report.error = $_.Exception.Message
    Write-Error $_ -ErrorAction Continue
}
finally {
    Save-Report
}
if (-not $report.success) { exit 1 }
