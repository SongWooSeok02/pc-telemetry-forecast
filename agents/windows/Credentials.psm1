# PowerShell 7 on Windows. Run in the service process; never dot-source from a web page.
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'

function New-CredentialStore {
    param([Parameter(Mandatory)][string]$ServiceSid,
          [string]$Path = "$env:ProgramData\PcTelemetry\Credentials")
    $sid = [System.Security.Principal.SecurityIdentifier]::new($ServiceSid)
    $admins = [System.Security.Principal.SecurityIdentifier]::new('S-1-5-32-544')
    # Installer must create the parent under an administrator-owned ProgramData tree.
    $parent = Get-Item -LiteralPath (Split-Path $Path)
    $store = @{ Path = [IO.Path]::GetFullPath($Path); ServiceSid = $sid.Value }
    Assert-CredentialAcl $store $parent.FullName
    for ($ancestor = $parent; $null -ne $ancestor; $ancestor = $ancestor.Parent) {
        if ($ancestor.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe credential ancestor' }
    }
    $acl = [Security.AccessControl.DirectorySecurity]::new()
    $acl.SetAccessRuleProtection($true, $false)
    $acl.SetOwner($sid)
    foreach ($identity in @($sid, $admins)) {
        $acl.AddAccessRule([Security.AccessControl.FileSystemAccessRule]::new($identity,
            'FullControl', 'ContainerInherit,ObjectInherit', 'None', 'Allow'))
    }
    $directory = [IO.DirectoryInfo]::new($Path)
    if (-not $directory.Exists) { [IO.FileSystemAclExtensions]::Create($directory, $acl) }
    Assert-CredentialAcl $store $store.Path
    return $store
}

function Assert-CredentialAcl($Store, [string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.Attributes -band [IO.FileAttributes]::ReparsePoint) { throw 'Unsafe credential path' }
    $acl = Get-Acl -LiteralPath $Path
    $allowed = @($Store.ServiceSid, 'S-1-5-32-544')
    if ($acl.GetOwner([Security.Principal.SecurityIdentifier]).Value -notin $allowed) { throw 'Unsafe credential owner' }
    foreach ($rule in $acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])) {
        if ($rule.AccessControlType -eq 'Allow' -and $rule.IdentityReference.Value -notin $allowed) {
            throw 'Unsafe credential ACL'
        }
    }
}

function Set-DeviceCredential($Store, [string]$Token) {
    Assert-CredentialAcl $Store $Store.Path
    $plain = [Text.Encoding]::UTF8.GetBytes($Token)
    $temp = Join-Path $Store.Path ([Guid]::NewGuid().ToString() + '.tmp')
    try {
        $cipher = [Security.Cryptography.ProtectedData]::Protect($plain, $null,
            [Security.Cryptography.DataProtectionScope]::LocalMachine)
        [IO.File]::WriteAllBytes($temp, $cipher)
        Assert-CredentialAcl $Store $temp
        [IO.File]::Move($temp, (Join-Path $Store.Path 'device.dpapi'), $true)
    } finally {
        [Array]::Clear($plain, 0, $plain.Length)
        if (Test-Path -LiteralPath $temp) { Remove-Item -LiteralPath $temp -Force }
    }
}

function Get-DeviceCredential($Store) {
    Assert-CredentialAcl $Store $Store.Path
    $path = Join-Path $Store.Path 'device.dpapi'
    Assert-CredentialAcl $Store $path
    $plain = [Security.Cryptography.ProtectedData]::Unprotect([IO.File]::ReadAllBytes($path), $null,
        [Security.Cryptography.DataProtectionScope]::LocalMachine)
    try { return [Text.Encoding]::UTF8.GetString($plain) }
    finally { [Array]::Clear($plain, 0, $plain.Length) }
}

function Remove-DeviceCredential($Store) {
    Assert-CredentialAcl $Store $Store.Path
    # Delete encrypted staging files too after an interrupted atomic replacement.
    foreach ($file in Get-ChildItem -LiteralPath $Store.Path -Force) {
        Assert-CredentialAcl $Store $file.FullName
        if ($file.PSIsContainer) { throw 'Unexpected credential directory' }
        Remove-Item -LiteralPath $file.FullName -Force
    }
    if (@(Get-ChildItem -LiteralPath $Store.Path -Force).Count) { throw 'Credential deletion failed' }
}

function Start-RegistrationPipe {
    param($Store, [Parameter(Mandatory)][uri]$Server)
    if ($Server.Scheme -ne 'https' -or $Server.UserInfo -or $Server.Query -or $Server.Fragment -or $Server.AbsolutePath -ne '/') {
        throw 'HTTPS server origin required'
    }
    $security = [IO.Pipes.PipeSecurity]::new()
    $security.SetAccessRuleProtection($true, $false)
    # NETWORK deny also applies when a remote caller belongs to Administrators.
    $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new(
        [Security.Principal.SecurityIdentifier]::new('S-1-5-2'), 'FullControl', 'Deny'))
    foreach ($sid in @($Store.ServiceSid, 'S-1-5-32-544')) {
        $security.AddAccessRule([IO.Pipes.PipeAccessRule]::new(
            [Security.Principal.SecurityIdentifier]::new($sid), 'FullControl', 'Allow'))
    }
    while ($true) {
        # FirstPipeInstance prevents an existing pipe from silently receiving UI secrets.
        $pipe = [IO.Pipes.NamedPipeServerStreamAcl]::Create('PcTelemetry.Registration.v1',
            'InOut', 1, 'Byte', [IO.Pipes.PipeOptions]::Asynchronous -bor [IO.Pipes.PipeOptions]::FirstPipeInstance,
            4096, 4096, $security)
        try {
            $acceptTimeout = [Threading.CancellationTokenSource]::new(1000)
            try { $pipe.WaitForConnectionAsync($acceptTimeout.Token).GetAwaiter().GetResult() }
            finally { $acceptTimeout.Dispose() }
            # Bounded frame, timeout, fixed command set; no token-read or arbitrary URL operation.
            $cancel = [Threading.CancellationTokenSource]::new(10000)
            try {
                $buffer = [byte[]]::new(4097)
                $length = 0
                do {
                    $read = $pipe.ReadAsync($buffer, $length, 1, $cancel.Token).GetAwaiter().GetResult()
                    if ($read -eq 0 -or $length -ge 4096) { throw 'Invalid frame' }
                    $length++
                } until ($buffer[$length - 1] -eq 10)
                $command = [Text.Encoding]::UTF8.GetString($buffer, 0, $length) | ConvertFrom-Json
                switch ($command.action) {
                    'register' {
                        if ($command.code -notmatch '^[A-Za-z0-9_-]{32}$') { throw 'Invalid code' }
                        $response = Invoke-RestMethod -Method Post -Uri ([uri]::new($Server, '/v1/device/registrations')) `
                            -ContentType 'application/json' -Body (@{registration_code=$command.code} | ConvertTo-Json -Compress) `
                            -TimeoutSec 15 -MaximumRedirection 0
                        Set-DeviceCredential $Store $response.device_token
                        $response = $null
                    }
                    'clear' { Remove-DeviceCredential $Store }
                    default { throw 'Unsupported command' }
                }
                $reply = [Text.Encoding]::UTF8.GetBytes('{"ok":true}' + "`n")
            } finally { $cancel.Dispose() }
        } catch {
            # Never log exception/request/HTTP response: they may contain credentials.
            $reply = [Text.Encoding]::UTF8.GetBytes('{"ok":false}' + "`n")
        } finally {
            if ($pipe.IsConnected) {
                try { $pipe.WriteAsync($reply, 0, $reply.Length).Wait(1000) | Out-Null } catch { }
            }
            $pipe.Dispose()
        }
    }
}
Export-ModuleMember -Function New-CredentialStore, Set-DeviceCredential, Get-DeviceCredential, Remove-DeviceCredential, Start-RegistrationPipe
