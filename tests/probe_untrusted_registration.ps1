# The synthetic registration code arrives on stdin, never argv/URL/disk.
param([Parameter(Mandatory)][uri]$Server)
Set-StrictMode -Version Latest
$ErrorActionPreference='Stop'
Import-Module "$PSScriptRoot/../agents/windows/Credentials.psm1" -Force
$script:CodeForPrompt=[Console]::In.ReadLine()
function Read-Host { param($Prompt,[switch]$AsSecureString); ConvertTo-SecureString $script:CodeForPrompt -AsPlainText -Force }
$sid=[Security.Principal.WindowsIdentity]::GetCurrent().User
$root=Join-Path ([IO.Path]::GetTempPath()) ('s203-untrusted-'+[Guid]::NewGuid())
$acl=[Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true,$false); $acl.SetOwner($sid)
foreach ($identity in @($sid,[Security.Principal.SecurityIdentifier]::new('S-1-5-32-544'))) {
    $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,
        'FullControl','ContainerInherit,ObjectInherit','None','Allow'))
}
[IO.FileSystemAclExtensions]::Create([IO.DirectoryInfo]::new($root),$acl)
$job=$null
try {
    $store=New-CredentialStore -ServiceSid $sid.Value -Path (Join-Path $root 'Credentials')
    $job=Start-Job -ScriptBlock {
        param($module,$state,$origin)
        Import-Module $module -Force
        Start-RegistrationPipe -Store $state -Server $origin
    } -ArgumentList "$PSScriptRoot/../agents/windows/Credentials.psm1",$store,$Server.AbsoluteUri
    $denied=$false
    try { $null=& "$PSScriptRoot/../agents/windows/Register-Device.ps1" -ServiceSid $sid.Value }
    catch { $denied=$true }
    if (-not $denied) { throw 'Untrusted HTTPS registration accepted' }
    if (@(Get-ChildItem -LiteralPath $store.Path -Force).Count) { throw 'Failed TLS persisted a credential' }
    Write-Output 'PASS: production UI/IPC HTTP client rejects untrusted TLS without persisting credentials'
} finally {
    if ($job) { Stop-Job $job; $null=Receive-Job $job -ErrorAction SilentlyContinue; Remove-Job $job }
    $script:CodeForPrompt=$null
    if ((Split-Path $root) -ne ([IO.Path]::GetTempPath()).TrimEnd('\')) { throw 'Unsafe cleanup target' }
    Remove-Item -LiteralPath $root -Recurse -Force
}
