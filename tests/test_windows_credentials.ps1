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
    [RestrictedCredentialProbe]::Check((Join-Path $store.Path 'device.dpapi'), $false)
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
    Remove-DeviceCredential $store
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
        & "$PSScriptRoot/../agents/windows/Register-Device.ps1" -Action clear -ServiceSid $sid.Value
        # Allow the service to recreate its first instance after the completed command.
        Start-Sleep -Milliseconds 100
        [RestrictedCredentialProbe]::Check('', $true)
    } finally {
        Stop-Job $job
        $null = Receive-Job $job -ErrorAction Continue
        Remove-Job $job
    }
    Write-Output 'PASS: DPAPI, atomic replacement, plaintext absence, ACL fail-closed, deletion'
} finally {
    # root is a directly created, unique test directory; never traverse a supplied path.
    if ((Split-Path $root) -ne ([IO.Path]::GetTempPath()).TrimEnd('\')) { throw 'Unsafe cleanup target' }
    Remove-Item -LiteralPath $root -Recurse -Force
}
