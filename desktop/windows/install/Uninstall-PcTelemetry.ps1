<#
S1-06 제거(관리자 Windows PowerShell 5.1 이상).

  .\Uninstall-PcTelemetry.ps1 [-RemoveData] [-ReportPath <json>]

기본: 서비스·프로그램 파일·바로 가기를 지우고 %ProgramData%\PcTelemetry(미전송 데이터·상태)는 남긴다.
-RemoveData: state\, status\까지 지운다. Credentials\(S2-03)는 서버 회수(revoke) 후 등록 UI의 clear로 지우므로
여기서 지우지 않는다. 공유 구성요소인 PawnIO·WebView2 런타임은 제거하지 않는다.
사용자별 %LOCALAPPDATA%\PcTelemetry\Shell(WebView2 프로필·로그)은 각 사용자 프로필에 남으며 결과에 표시한다.
#>
[CmdletBinding()]
param(
    [switch]$RemoveData,
    [string]$ReportPath
)
$ErrorActionPreference = 'Stop'
Set-StrictMode -Version 2

$ServiceName = 'PcTelemetryAgent'
$InstallDir = Join-Path $env:ProgramFiles 'PcTelemetry'
$DataRoot = Join-Path $env:ProgramData 'PcTelemetry'
$Shortcut = Join-Path $env:ProgramData 'Microsoft\Windows\Start Menu\Programs\PC Telemetry.lnk'
$report = [ordered]@{ task = 'S1-06'; action = 'uninstall'; remove_data = [bool]$RemoveData; started_at_utc = (Get-Date).ToUniversalTime().ToString('o') }

try {
    $principal = New-Object Security.Principal.WindowsPrincipal ([Security.Principal.WindowsIdentity]::GetCurrent())
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) { throw 'run from an elevated (administrator) PowerShell' }

    $svc = Get-Service $ServiceName -ErrorAction SilentlyContinue
    if ($svc) {
        if ($svc.Status -ne 'Stopped') {
            Stop-Service $ServiceName -Force
            $svc.WaitForStatus('Stopped', [TimeSpan]::FromSeconds(60))
        }
        & sc.exe delete $ServiceName | Out-Null
        $deadline = (Get-Date).AddSeconds(30)
        while ((Get-Service $ServiceName -ErrorAction SilentlyContinue) -and (Get-Date) -lt $deadline) { Start-Sleep -Milliseconds 500 }
    }
    # S2-02 이벤트 로그 원본 등록만 지운다(이미 기록된 항목은 Windows 로그 보존 정책을 따른다).
    if ([Diagnostics.EventLog]::SourceExists($ServiceName)) { Remove-EventLog -Source $ServiceName }
    # 셸이 열려 있으면 파일이 잠긴다. 사용자 UI는 종료해도 수집에 영향이 없다.
    Get-Process PcTelemetry.Shell -ErrorAction SilentlyContinue | Stop-Process -Force
    if (Test-Path $Shortcut) { Remove-Item -Force $Shortcut }
    if (Test-Path $InstallDir) { Remove-Item -Recurse -Force $InstallDir }
    if ($RemoveData) {
        foreach ($d in 'state', 'status') {
            $p = Join-Path $DataRoot $d
            if (Test-Path $p) { Remove-Item -Recurse -Force $p }
        }
        if ((Test-Path $DataRoot) -and -not (Get-ChildItem $DataRoot -Force)) { Remove-Item -Force $DataRoot }
    }

    $profiles = @(Get-ChildItem 'C:\Users' -Directory -ErrorAction SilentlyContinue |
        Where-Object { Test-Path (Join-Path $_.FullName 'AppData\Local\PcTelemetry\Shell') })
    $report.residue = [ordered]@{
        service = [bool](Get-Service $ServiceName -ErrorAction SilentlyContinue)
        event_source = [Diagnostics.EventLog]::SourceExists($ServiceName)
        install_dir = Test-Path $InstallDir
        shortcut = Test-Path $Shortcut
        data_root = Test-Path $DataRoot
        data_items = @(if (Test-Path $DataRoot) { Get-ChildItem $DataRoot -Force | ForEach-Object Name })
        # 계정 이름 대신 개수만 남긴다.
        user_shell_profiles = $profiles.Count
        pawnio_kept = [bool](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO' -ErrorAction SilentlyContinue)
    }
    $report.success = -not ($report.residue.service -or $report.residue.install_dir -or $report.residue.shortcut)
}
catch {
    $report.success = $false
    $report.error = $_.Exception.Message
    Write-Error $_ -ErrorAction Continue
}
finally {
    $report.finished_at_utc = (Get-Date).ToUniversalTime().ToString('o')
    $json = $report | ConvertTo-Json -Depth 5
    if ($ReportPath) { $json | Set-Content -Path $ReportPath -Encoding UTF8 }
    Write-Output $json
}
if (-not $report.success) { exit 1 }
