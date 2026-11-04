Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
Import-Module "$PSScriptRoot/../agents/windows/Credentials.psm1" -Force
Add-Type -Path "$PSScriptRoot/RestrictedCredentialProbe.cs"
$root = Join-Path ([IO.Path]::GetTempPath()) ('s203-' + [Guid]::NewGuid())
$sid = [Security.Principal.WindowsIdentity]::GetCurrent().User
$acl = [Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true, $false)
$acl.SetOwner($sid)
foreach ($identity in @($sid, [Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,
        'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
}
[IO.FileSystemAclExtensions]::Create([IO.DirectoryInfo]::new($root), $acl)
try {
    $store = New-CredentialStore -ServiceSid $sid.Value -Path (Join-Path $root 'Credentials')
    $synthetic = 'synthetic-windows-test-' + [Guid]::NewGuid()
    Set-DeviceCredential $store $synthetic
    if ((Get-DeviceCredential $store) -ne $synthetic) { throw 'DPAPI roundtrip failed' }
    $bytes = [IO.File]::ReadAllBytes((Join-Path $store.Path 'device.dpapi'))
    if ([Text.Encoding]::UTF8.GetString($bytes).Contains($synthetic)) { throw 'Plaintext persisted' }
    Set-DeviceCredential $store 'synthetic-replacement'
    if ((Get-DeviceCredential $store) -ne 'synthetic-replacement') { throw 'Replacement failed' }
    $restart = Start-Job -ScriptBlock {
        param($module,$state)
        Import-Module $module -Force
        (Get-DeviceCredential $state) -eq 'synthetic-replacement'
    } -ArgumentList "$PSScriptRoot/../agents/windows/Credentials.psm1",$store
    try {
        $null = Wait-Job $restart -Timeout 10
        if ($restart.State -ne 'Completed' -or (Receive-Job $restart) -ne $true) { throw 'Cross-process DPAPI persistence failed' }
    } finally { Stop-Job $restart; Remove-Job $restart }
    [RestrictedCredentialProbe]::Check((Join-Path $store.Path 'device.dpapi'), $false)
    $file = Join-Path $store.Path 'device.dpapi'
    [IO.File]::WriteAllBytes($file, [byte[]]@(1,2,3))
    $rejected = $false
    try { $null = Get-DeviceCredential $store } catch { $rejected = $true }
    if (-not $rejected) { throw 'Corrupt ciphertext accepted' }
    Set-DeviceCredential $store 'synthetic-replacement'
    $junction = Join-Path $root 'redirect'
    $null = New-Item -ItemType Junction -Path $junction -Target $store.Path
    try {
        $rejected = $false
        try { $null = New-CredentialStore -ServiceSid $sid.Value -Path $junction } catch { $rejected=$true }
        if (-not $rejected) { throw 'Reparse directory accepted' }
    } finally { Remove-Item -LiteralPath $junction -Force }
    # An overly permissive ACL must fail closed before decrypting.
    $file = Join-Path $store.Path 'device.dpapi'
    $bad = Get-Acl -LiteralPath $file
    $bad.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'), 'Read', 'Allow'))
    Set-Acl -LiteralPath $file -AclObject $bad
    $rejected = $false
    try { $null = Get-DeviceCredential $store } catch { $rejected = $true }
    if (-not $rejected) { throw 'Unsafe ACL accepted' }
    $bad.RemoveAccessRuleSpecific([Security.AccessControl.FileSystemAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-32-545'), 'Read', 'Allow'))
    Set-Acl -LiteralPath $file -AclObject $bad
    [IO.File]::WriteAllBytes((Join-Path $store.Path 'interrupted.tmp'), [IO.File]::ReadAllBytes($file))
    Remove-DeviceCredential $store
    if (@(Get-ChildItem -LiteralPath $store.Path -Force).Count -ne 0) { throw 'Encrypted staging file survived deletion' }
    $rejected = $false
    try { $null = Get-DeviceCredential $store } catch { $rejected = $true }
    if (-not $rejected) { throw 'Deleted credential readable' }
    # Exercise the real IPC host and UI adapter with an authorized identity.
    $job = Start-Job -ScriptBlock {
        param($module, $state)
        Import-Module $module -Force
        Start-RegistrationPipe -Store $state -Server 'https://127.0.0.1/'
    } -ArgumentList "$PSScriptRoot/../agents/windows/Credentials.psm1", $store
    try {
        function Send-TestFrame([string]$Frame, [switch]$Stall) {
            $client = [IO.Pipes.NamedPipeClientStream]::new('.', 'PcTelemetry.Registration.v1',
                'InOut', 'Asynchronous', [Security.Principal.TokenImpersonationLevel]::Anonymous)
            $timeout = [Threading.CancellationTokenSource]::new(15000)
            try {
                $client.Connect(5000)
                if (-not $Stall) {
                    $payload = [Text.Encoding]::UTF8.GetBytes($Frame + "`n")
                    $client.WriteAsync($payload,0,$payload.Length,$timeout.Token).GetAwaiter().GetResult()
                }
                $reply = [byte[]]::new(64)
                $count=0
                do {
                    $read=$client.ReadAsync($reply,$count,1,$timeout.Token).GetAwaiter().GetResult()
                    if ($read -eq 0 -or $count -ge 63) { throw 'Invalid IPC error response' }
                    $count++
                } until ($reply[$count-1] -eq 10)
                $text=[Text.Encoding]::UTF8.GetString($reply,0,$count).Trim()
                if ($text -ne '{"ok":false}') { throw 'Unsafe IPC command accepted or error reflected input' }
            } finally { $client.Dispose(); $timeout.Dispose() }
        }
        Send-TestFrame '{"action":"read-token","secret":"synthetic-sensitive-marker"}'
        Send-TestFrame '{"action":'
        Send-TestFrame ('x'*4096)
        Send-TestFrame '' -Stall
        & "$PSScriptRoot/../agents/windows/Register-Device.ps1" -Action clear -ServiceSid $sid.Value
        # Allow the service to recreate its first instance after the completed command.
        Start-Sleep -Milliseconds 100
        [RestrictedCredentialProbe]::Check('', $true)
    } finally {
        Stop-Job $job
        $null = Receive-Job $job -ErrorAction Continue
        Remove-Job $job
    }
    Write-Output 'PASS: DPAPI restart persistence, corrupt ciphertext rejection, reparse rejection, encrypted staging deletion, malformed/oversized/slow IPC rejection, redacted replies, restricted-token denial, IPC recovery'
} finally {
    # root is a directly created, unique test directory; never traverse a supplied path.
    if ((Split-Path $root) -ne ([IO.Path]::GetTempPath()).TrimEnd('\')) { throw 'Unsafe cleanup target' }
    Remove-Item -LiteralPath $root -Recurse -Force
}
