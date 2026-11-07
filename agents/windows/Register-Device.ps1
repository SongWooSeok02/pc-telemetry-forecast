# Elevated registration UI adapter. The caller supplies code via Read-Host, never argv/URL.
param([ValidateSet('register','clear')][string]$Action = 'register',
      [Parameter(Mandatory)][string]$ServiceSid)
Set-StrictMode -Version Latest
$ErrorActionPreference = 'Stop'
$pipe = [IO.Pipes.NamedPipeClientStream]::new('.', 'PcTelemetry.Registration.v1',
    [IO.Pipes.PipeDirection]::InOut, [IO.Pipes.PipeOptions]::Asynchronous,
    [Security.Principal.TokenImpersonationLevel]::Anonymous)
try {
    $pipe.Connect(5000)
    # Verify server owner before transmitting the one-time capability.
    $acl = [IO.Pipes.PipesAclExtensions]::GetAccessControl($pipe)
    $owner = $acl.GetOwner([Security.Principal.SecurityIdentifier]).Value
    if ($owner -notin @($ServiceSid, 'S-1-5-32-544')) { throw 'Unexpected pipe owner' }
    $command = @{action=$Action}
    if ($Action -eq 'register') {
        $secret = Read-Host 'Registration code' -AsSecureString
        $credential = [pscredential]::new('code', $secret)
        $command.code = $credential.GetNetworkCredential().Password
    }
    $bytes = [Text.Encoding]::UTF8.GetBytes(($command | ConvertTo-Json -Compress) + "`n")
    $cancel = [Threading.CancellationTokenSource]::new(20000)
    try {
        $pipe.WriteAsync($bytes, 0, $bytes.Length, $cancel.Token).GetAwaiter().GetResult()
        $reply = [byte[]]::new(64)
        $count = 0
        do {
            $read = $pipe.ReadAsync($reply, $count, 1, $cancel.Token).GetAwaiter().GetResult()
            if ($read -eq 0 -or $count -ge 63) { throw 'Invalid service response' }
            $count++
        } until ($reply[$count - 1] -eq 10)
        $value = [Text.Encoding]::UTF8.GetString($reply, 0, $count) | ConvertFrom-Json
        if (-not $value.ok) { throw 'Device operation failed; obtain a new code if enrollment was consumed' }
        Write-Output 'Device operation completed'
    } finally {
        $cancel.Dispose()
        [Array]::Clear($bytes, 0, $bytes.Length)
    }
} finally { $pipe.Dispose() }
