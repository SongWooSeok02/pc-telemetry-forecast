<#
S1-02a 스파이크 단계 실행 스크립트. 관리자 PowerShell에서 한 단계씩 실행한다.

  모든 단계에 -PcLabel pc-a 처럼 익명 PC 라벨을 함께 준다.
  .\Invoke-Spike.ps1 -Step Env            # 보안 설정·하드웨어·사전 상태 기록 (+ 복원 지점)
  .\Invoke-Spike.ps1 -Step Publish        # SensorProbe를 ProgramData로 배포
  .\Invoke-Spike.ps1 -Step ProbeAdmin     # 관리자 콘솔에서 once + 60초 run
  .\Invoke-Spike.ps1 -Step InstallPawnIO  # PawnIO 2.2.0 설치 (기본: 대화형, -Silent: 무인 스위치 실측)
  .\Invoke-Spike.ps1 -Step ServiceInstall -Account LocalSystem   # 또는 LocalService
  .\Invoke-Spike.ps1 -Step ServiceStatus  # 서비스 상태와 최신 결과 요약
  .\Invoke-Spike.ps1 -Step ServiceRemove
  .\Invoke-Spike.ps1 -Step UninstallPawnIO
  .\Invoke-Spike.ps1 -Step Residue        # 제거 후 남은 항목
  .\Invoke-Spike.ps1 -Step Events         # CodeIntegrity·Defender·SCM 이벤트 로그 저장

모든 출력은 tests/results/s1-02a/<PcLabel>/ 에 남는다. 호스트명·계정명은 기록하지 않는다. 보안 기능을 끄거나 예외를 추가하는 단계는 없다.
#>
[CmdletBinding()]
param(
    [Parameter(Mandatory)]
    [ValidateSet('Env', 'Publish', 'ProbeAdmin', 'InstallPawnIO', 'ServiceInstall', 'ServiceStatus',
        'ServiceRemove', 'UninstallPawnIO', 'Residue', 'Events')]
    [string]$Step,
    [Parameter(Mandatory)]
    [ValidatePattern('^pc-[a-z0-9]{1,16}$')]
    [string]$PcLabel,
    [ValidateSet('LocalSystem', 'LocalService')]
    [string]$Account = 'LocalSystem',
    [switch]$Silent,
    [string]$Installer = "$PSScriptRoot\..\..\..\.tools\PawnIO_setup.exe"
)

$ErrorActionPreference = 'Stop'
$PawnIOSha256 = '1F519A22E47187F70A1379A48CA604981C4FCF694F4E65B734AAA74A9FBA3032'
$ServiceName = 'SensorProbe'
$DeployDir = 'C:\ProgramData\SensorProbeSpike'
$RepoRoot = (Resolve-Path "$PSScriptRoot\..\..\..").Path
$OutDir = Join-Path $RepoRoot "tests\results\s1-02a\$PcLabel"
New-Item -ItemType Directory -Force $OutDir | Out-Null

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $isAdmin) { throw '관리자 PowerShell에서 실행해야 한다.' }

function Stamp { (Get-Date).ToUniversalTime().ToString('yyyyMMddTHHmmssZ') }

function Save-Json($Name, $Object) {
    $path = Join-Path $OutDir "$(Stamp)-$Name.json"
    $Object | ConvertTo-Json -Depth 6 | Out-File -Encoding utf8 $path
    Write-Host "saved $path"
}

function Get-PawnIOState {
    $key = Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO' -ErrorAction SilentlyContinue
    $svc = Get-CimInstance Win32_SystemDriver -Filter "Name='PawnIO'" -ErrorAction SilentlyContinue
    $sys = if ($svc) { $svc.PathName -replace '^\\\?\?\\', '' } else { $null }
    $sig = if ($sys -and (Test-Path $sys)) { Get-AuthenticodeSignature $sys } else { $null }
    [ordered]@{
        uninstall_key   = [bool]$key
        display_version = $key.DisplayVersion
        uninstall_cmd   = $key.UninstallString
        driver_state    = $svc.State
        driver_start    = $svc.StartMode
        driver_path     = $sys
        driver_sha256   = if ($sys -and (Test-Path $sys)) { (Get-FileHash $sys -Algorithm SHA256).Hash } else { $null }
        driver_sig      = if ($sig) { "$($sig.Status)" } else { $null }
        driver_signer   = $sig.SignerCertificate.Subject
        winring0_any    = [bool](Get-CimInstance Win32_SystemDriver | Where-Object { $_.Name -match 'WinRing0' })
    }
}

function Get-RegValue($Path, $Name) {
    (Get-ItemProperty $Path -Name $Name -ErrorAction SilentlyContinue).$Name
}

switch ($Step) {
    'Env' {
        try { Checkpoint-Computer -Description 'S1-02a before PawnIO' -RestorePointType MODIFY_SETTINGS; $rp = 'created' }
        catch { $rp = "failed: $($_.Exception.Message)" }
        $dg = Get-CimInstance -Namespace root\Microsoft\Windows\DeviceGuard -ClassName Win32_DeviceGuard
        # 다른 백신을 쓰는 PC나 레거시 BIOS에서도 기록이 멈추지 않도록 실패를 값으로 남긴다.
        try { $mp = Get-MpComputerStatus } catch { $mp = $null; $mpError = $_.Exception.Message }
        try { $secureBoot = Confirm-SecureBootUEFI } catch { $secureBoot = "unavailable: $($_.Exception.Message)" }
        $os = Get-CimInstance Win32_OperatingSystem
        Save-Json 'env' ([ordered]@{
            executed_at_utc        = (Get-Date).ToUniversalTime().ToString('o')
            computer               = $PcLabel
            os                     = "$($os.Caption) $($os.Version) build $($os.BuildNumber)"
            model                  = (Get-CimInstance Win32_ComputerSystem).Model
            board                  = (Get-CimInstance Win32_BaseBoard | ForEach-Object { "$($_.Manufacturer) $($_.Product)" })
            cpu                    = (Get-CimInstance Win32_Processor).Name
            gpu                    = @(Get-CimInstance Win32_VideoController | ForEach-Object { "$($_.Name) / driver $($_.DriverVersion)" })
            secure_boot            = $secureBoot
            vbs_status             = $dg.VirtualizationBasedSecurityStatus
            hvci_running           = ($dg.SecurityServicesRunning -contains 2)
            hvci_reg_enabled       = Get-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\DeviceGuard\Scenarios\HypervisorEnforcedCodeIntegrity' 'Enabled'
            vulnerable_driver_list = Get-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Config' 'VulnerableDriverBlocklistEnable'
            defender               = [ordered]@{
                product_version   = $mp.AMProductVersion
                signature_version = $mp.AntivirusSignatureVersion
                realtime          = $mp.RealTimeProtectionEnabled
                tamper_protection = $mp.IsTamperProtected
                error             = $mpError
            }
            smart_app_control      = Get-RegValue 'HKLM:\SYSTEM\CurrentControlSet\Control\CI\Policy' 'VerifiedAndReputablePolicyState'
            restore_point          = $rp
            pawnio                 = Get-PawnIOState
        })
    }
    'Publish' {
        $proj = Join-Path $PSScriptRoot 'SensorProbe\SensorProbe.csproj'
        dotnet publish $proj -c Release -o "$DeployDir\bin"
        if ($LASTEXITCODE -ne 0) { throw "publish failed: $LASTEXITCODE" }
        New-Item -ItemType Directory -Force "$DeployDir\out" | Out-Null
        # LocalService 검증용으로 결과 폴더에만 쓰기 권한을 준다. 실행 파일 폴더는 읽기만 허용된 기본 ACL을 유지한다.
        icacls "$DeployDir\out" /grant 'NT AUTHORITY\LOCAL SERVICE:(OI)(CI)M' | Out-Null
        Write-Host "published to $DeployDir\bin"
    }
    'ProbeAdmin' {
        $tag = if ((Get-PawnIOState).uninstall_key) { 'pawnio' } else { 'nopawnio' }
        & "$DeployDir\bin\SensorProbe.exe" once --out (Join-Path $OutDir "$(Stamp)-$tag-admin-once.json")
        & "$DeployDir\bin\SensorProbe.exe" run --out (Join-Path $OutDir "$(Stamp)-$tag-admin-run.json") --seconds 60
    }
    'InstallPawnIO' {
        if (-not (Test-Path $Installer)) { throw "설치 파일 없음: $Installer (pawnio.eu 공식 링크에서 받는다)" }
        $hash = (Get-FileHash $Installer -Algorithm SHA256).Hash
        if ($hash -ne $PawnIOSha256) { throw "SHA256 불일치: $hash" }
        $sig = Get-AuthenticodeSignature $Installer
        if ($sig.Status -ne 'Valid') { throw "서명 상태: $($sig.Status)" }
        $before = Get-PawnIOState
        $started = Get-Date
        $p = if ($Silent) { Start-Process $Installer -ArgumentList '-install', '-silent' -Wait -PassThru }
             else { Start-Process $Installer -Wait -PassThru }
        Save-Json 'install-pawnio' ([ordered]@{
            started_at_utc = $started.ToUniversalTime().ToString('o')
            seconds        = ((Get-Date) - $started).TotalSeconds
            mode           = if ($Silent) { 'silent (-install -silent)' } else { 'interactive' }
            exit_code      = $p.ExitCode
            note           = '대화형이면 동의 화면·SmartScreen·Defender 반응을 스크린샷으로 따로 남긴다.'
            before         = $before
            after          = Get-PawnIOState
        })
    }
    'ServiceInstall' {
        $obj = if ($Account -eq 'LocalService') { 'NT AUTHORITY\LocalService' } else { 'LocalSystem' }
        $existing = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
        if ($existing) {
            Write-Host "이미 설치됨: 상태=$($existing.State) 계정=$($existing.StartName) 시작=$($existing.StartMode)"
            if ($existing.StartName -notlike "*$Account") {
                throw "다른 계정($($existing.StartName))으로 설치되어 있다. -Step ServiceRemove 후 다시 실행한다."
            }
            return
        }
        # PowerShell 5.1은 네이티브 인수의 큰따옴표를 지우므로 \" 로 넘겨 sc.exe가 경로 따옴표를 유지하게 한다.
        $bin = "\`"$DeployDir\bin\SensorProbe.exe\`" service --out-dir \`"$DeployDir\out\`""
        sc.exe create $ServiceName binPath= $bin start= auto obj= $obj DisplayName= 'S1-02a SensorProbe'
        if ($LASTEXITCODE -ne 0) { throw "sc create failed: $LASTEXITCODE" }
        sc.exe start $ServiceName
        Write-Host "2분 이상 기다린 뒤 -Step ServiceStatus 로 확인한다."
    }
    'ServiceStatus' {
        $svc = Get-CimInstance Win32_Service -Filter "Name='$ServiceName'"
        $latest = Get-ChildItem "$DeployDir\out" -Filter 'service-*.json' | Sort-Object LastWriteTime | Select-Object -Last 1
        if ($latest) { Copy-Item $latest.FullName (Join-Path $OutDir "$(Stamp)-$($latest.Name)") }
        $j = if ($latest) { Get-Content $latest.FullName -Raw | ConvertFrom-Json } else { $null }
        $props = if ($j) { @($j.sensors.PSObject.Properties) } else { @() }
        $sensors = @($props | ForEach-Object Value)
        Save-Json 'service-status' ([ordered]@{
            service_state = $svc.State
            start_mode    = $svc.StartMode
            account       = $svc.StartName
            last_boot_utc = (Get-CimInstance Win32_OperatingSystem).LastBootUpTime.ToUniversalTime().ToString('o')
            result_file   = $latest.Name
            cycles        = $j.cycles
            cycle_errors  = $j.cycle_errors
            open_error    = $j.open_error
            sensors_total = $sensors.Count
            sensors_ok    = @($sensors | Where-Object { $_.ok -gt 0 }).Count
            cpu_temp_ok   = @($props | Where-Object { $_.Name -match '^/(intelcpu|amdcpu)/.*/temperature/' -and $_.Value.ok -gt 0 }).Count
            pawnio        = Get-PawnIOState
        })
    }
    'ServiceRemove' {
        sc.exe stop $ServiceName | Out-Null
        Start-Sleep -Seconds 3
        sc.exe delete $ServiceName
    }
    'UninstallPawnIO' {
        $state = Get-PawnIOState
        if (-not $state.uninstall_cmd) { throw 'PawnIO 제거 명령을 찾지 못했다.' }
        Write-Host "실행: $($state.uninstall_cmd)"
        $started = Get-Date
        $p = Start-Process cmd.exe -ArgumentList "/c `"$($state.uninstall_cmd)`"" -Wait -PassThru
        Save-Json 'uninstall-pawnio' ([ordered]@{
            started_at_utc = $started.ToUniversalTime().ToString('o')
            exit_code      = $p.ExitCode
            before         = $state
            after          = Get-PawnIOState
        })
    }
    'Residue' {
        Save-Json 'residue' ([ordered]@{
            pawnio            = Get-PawnIOState
            sc_query          = (sc.exe query PawnIO) -join "`n"
            driver_store      = @((pnputil /enum-drivers) | Select-String -Pattern 'pawn' -Context 2, 4 | ForEach-Object { $_.ToString() })
            sys_files         = @(Get-ChildItem C:\Windows\System32\drivers -Filter '*pawn*' -ErrorAction SilentlyContinue | ForEach-Object FullName)
            program_files     = @(Get-ChildItem $env:ProgramFiles, ${env:ProgramFiles(x86)} -Filter '*PawnIO*' -ErrorAction SilentlyContinue | ForEach-Object FullName)
            probe_service     = [bool](Get-Service $ServiceName -ErrorAction SilentlyContinue)
            probe_deploy_dir  = Test-Path $DeployDir
        })
    }
    'Events' {
        $since = (Get-Date).AddDays(-2)
        $logs = @{
            'codeintegrity' = @{ LogName = 'Microsoft-Windows-CodeIntegrity/Operational'; StartTime = $since }
            'defender'      = @{ LogName = 'Microsoft-Windows-Windows Defender/Operational'; StartTime = $since }
            'scm'           = @{ LogName = 'System'; ProviderName = 'Service Control Manager'; StartTime = $since }
        }
        foreach ($name in $logs.Keys) {
            $events = Get-WinEvent -FilterHashtable $logs[$name] -ErrorAction SilentlyContinue |
                Where-Object { $name -ne 'scm' -or $_.Message -match 'PawnIO|SensorProbe' } |
                Where-Object { $name -ne 'codeintegrity' -or $_.Message -match 'PawnIO|SensorProbe|LibreHardware' -or $_.LevelDisplayName -ne 'Information' } |
                Select-Object TimeCreated, Id, LevelDisplayName, ProviderName, Message
            Save-Json "events-$name" @($events)
        }
    }
}
