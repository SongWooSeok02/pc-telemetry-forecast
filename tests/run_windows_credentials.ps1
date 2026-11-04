param([string]$Report = "$PSScriptRoot/results/s2-03-windows.json")
$ErrorActionPreference = 'Stop'
$started = [DateTimeOffset]::UtcNow.ToString('o')
$output = & (Get-Process -Id $PID).Path -NoProfile -File "$PSScriptRoot/test_windows_credentials.ps1" 2>&1 | Out-String
$success = $LASTEXITCODE -eq 0
$hashes = @{}
foreach ($file in @(Get-ChildItem "$PSScriptRoot/../agents/windows" -File) + @(
    Get-Item "$PSScriptRoot/test_windows_credentials.ps1", "$PSScriptRoot/RestrictedCredentialProbe.cs", $PSCommandPath)) {
    $hashes[$file.Name] = (Get-FileHash -LiteralPath $file.FullName -Algorithm SHA256).Hash.ToLowerInvariant()
}
$record = @{
    executed_at_utc = $started; pc = [Environment]::MachineName
    os = [Environment]::OSVersion.VersionString; powershell = $PSVersionTable.PSVersion.ToString()
    runtime = [Runtime.InteropServices.RuntimeInformation]::FrameworkDescription
    base_commit = (git -C "$PSScriptRoot/.." rev-parse HEAD)
    source_sha256 = $hashes; success = $success
    reproduction = 'pwsh -NoProfile -File tests/run_windows_credentials.ps1'
    expected = @('DPAPI LocalMachine roundtrip', 'encrypted atomic replacement', 'no plaintext on disk',
        'restricted token file access denied', 'unsafe ACL rejected', 'deleted credential unreadable',
        'authorized IPC clear succeeds', 'restricted token IPC access denied')
    actual = $output.Trim(); status = $(if ($success) { 'pass' } else { 'fail' })
    limitations = @('Restricted BUILTIN\Users token, not a separate interactive local account',
        'Service identity simulated by current process SID; installed S2-02 service integration pending',
        'No live TLS registration server/UI test; authenticated IPC clear exercised')
}
$parent = Split-Path $Report
$null = New-Item -ItemType Directory -Path $parent -Force
$record | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath $Report -Encoding utf8
Write-Output $output.Trim()
if (-not $success) { exit 1 }
