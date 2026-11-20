<#
S1-06 배포 폴더 생성(개발 PC에서 실행, 관리자 불필요).

  .\Publish-PcTelemetry.ps1 [-Out <폴더>] [-NoWebView2Bootstrapper]
  .\Publish-PcTelemetry.ps1 -ReleaseCandidate -Out <새 폴더> -SigningCertificate <인증서> -TimestampServer <HTTPS URL>

결과(-Out, 기본 desktop/windows/publish — Git 제외):
  agent\   PcTelemetry.Agent (self-contained win-x64, LHM 0.9.6 고정)
  shell\   PcTelemetry.Shell (self-contained win-x64, WebView2 SDK 고정)
  shell\redist\MicrosoftEdgeWebview2Setup.exe   Microsoft Evergreen 부트스트래퍼(서명 확인 후 포함)
  Install-PcTelemetry.ps1, Uninstall-PcTelemetry.ps1, manifest.json(버전·SHA-256)

부트스트래퍼는 Microsoft 공식 링크에서 받으며 저장소에 커밋하지 않는다. Authenticode 서명이 유효하고
서명자가 Microsoft Corporation이 아니면 지우고 실패한다.
#>
[CmdletBinding()]
param(
    [string]$Out,
    [switch]$NoWebView2Bootstrapper,
    [switch]$ReleaseCandidate,
    [System.Security.Cryptography.X509Certificates.X509Certificate2]$SigningCertificate,
    [uri]$TimestampServer
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1은 param 기본값에서 $PSScriptRoot가 비어 있다.
if (-not $Out) { $Out = Join-Path $PSScriptRoot '..\publish' }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Out = [IO.Path]::GetFullPath($Out)
$version = $null
if ($ReleaseCandidate) {
    $version = '0.1.0-rc1'
    if (-not $SigningCertificate -or -not $SigningCertificate.HasPrivateKey -or
        -not $TimestampServer -or -not $TimestampServer.IsAbsoluteUri -or $TimestampServer.Scheme -ne 'https') {
        throw 'RC publishing requires a signing certificate with a private key and an HTTPS timestamp server.'
    }
    if (Test-Path -LiteralPath $Out) { throw 'RC output must be a new directory.' }
    $sourceCommit = & git -C $repo rev-parse HEAD
    if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the RC source commit.' }
    $changes = & git -C $repo status --porcelain
    if ($LASTEXITCODE -ne 0 -or $changes) { throw 'RC publishing requires a clean Git checkout.' }
} elseif (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force $Out | Out-Null

function Publish-Project([string]$project, [string]$target) {
    $properties = @()
    if ($version) { $properties += "-p:Version=$version" }
    & dotnet publish (Join-Path $repo $project) -c Release -r win-x64 --self-contained true -o $target -nologo -v q @properties
    if ($LASTEXITCODE -ne 0) { throw "dotnet publish failed: $project" }
}
Publish-Project 'agents\windows\src\PcTelemetry.Agent' (Join-Path $Out 'agent')
Publish-Project 'desktop\windows\src\PcTelemetry.Shell' (Join-Path $Out 'shell')

$bootstrapper = $null
if (-not $NoWebView2Bootstrapper) {
    # Microsoft 문서의 Evergreen Bootstrapper 링크.
    $url = 'https://go.microsoft.com/fwlink/p/?LinkId=2124703'
    $redist = Join-Path $Out 'shell\redist'
    New-Item -ItemType Directory -Force $redist | Out-Null
    $bootstrapper = Join-Path $redist 'MicrosoftEdgeWebview2Setup.exe'
    [Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
    Invoke-WebRequest -Uri $url -OutFile $bootstrapper -UseBasicParsing
    $sig = Get-AuthenticodeSignature -FilePath $bootstrapper
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.Subject -notmatch 'O=Microsoft Corporation') {
        Remove-Item -Force $bootstrapper
        throw "WebView2 bootstrapper signature check failed: $($sig.Status) $($sig.SignerCertificate.Subject)"
    }
}

Copy-Item (Join-Path $PSScriptRoot 'Install-PcTelemetry.ps1'), (Join-Path $PSScriptRoot 'Uninstall-PcTelemetry.ps1') $Out

$firstPartyFiles = @(
    'agent\PcTelemetry.Agent.exe'
    'agent\PcTelemetry.Agent.dll'
    'agent\PcTelemetry.Collector.dll'
    'shell\PcTelemetry.Shell.exe'
    'shell\PcTelemetry.Shell.dll'
    'shell\PcTelemetry.Shell.Core.dll'
    'Install-PcTelemetry.ps1'
    'Uninstall-PcTelemetry.ps1'
)
if ($ReleaseCandidate) {
    foreach ($relativePath in $firstPartyFiles) {
        $file = Join-Path $Out $relativePath
        $signature = Set-AuthenticodeSignature -LiteralPath $file -Certificate $SigningCertificate `
            -HashAlgorithm SHA256 -TimestampServer $TimestampServer.AbsoluteUri
        if ($signature.Status -ne 'Valid' -or -not $signature.TimeStamperCertificate -or
            $signature.SignerCertificate.Thumbprint -ne $SigningCertificate.Thumbprint) {
            throw "RC Authenticode verification failed: $relativePath ($($signature.Status))"
        }
    }
}

function Describe([string]$path) {
    $item = Get-Item -LiteralPath $path
    [ordered]@{
        path = $item.FullName.Substring($Out.Length + 1)
        version = $item.VersionInfo.FileVersion
        sha256 = (Get-FileHash -LiteralPath $path -Algorithm SHA256).Hash.ToLowerInvariant()
        bytes = $item.Length
    }
}
$commit = & git -C $repo rev-parse HEAD
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the source commit.' }
$dirty = [bool](& git -C $repo status --porcelain)
if ($ReleaseCandidate -and ($LASTEXITCODE -ne 0 -or $dirty -or $commit -ne $sourceCommit)) {
    throw 'RC source changed during publishing. Use a fresh output directory to retry.'
}
$sdkVersion = & dotnet --version
if ($LASTEXITCODE -ne 0) { throw 'Could not resolve the .NET SDK version.' }
$manifest = [ordered]@{
    task = 'S1-06'
    built_at_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    commit = $commit
    dirty = $dirty
    dotnet_sdk = $sdkVersion.Trim()
}
if ($ReleaseCandidate) {
    $manifest.task = 'S4-08'
    $manifest.release = "v$version"
    $manifest.signer_thumbprint = $SigningCertificate.Thumbprint
    $manifest.signed_paths = $firstPartyFiles
    $manifest.files = @(Get-ChildItem -LiteralPath $Out -Recurse -File |
        Sort-Object FullName | ForEach-Object { Describe $_.FullName })
} else {
    $manifest.files = @($firstPartyFiles | Where-Object { $_ -ne 'agent\PcTelemetry.Collector.dll' } |
        ForEach-Object { Describe (Join-Path $Out $_) })
    if ($bootstrapper) {
        $entry = Describe $bootstrapper
        $entry.signer = (Get-AuthenticodeSignature $bootstrapper).SignerCertificate.Subject
        $manifest.files += $entry
    }
}
$manifest | ConvertTo-Json -Depth 5 | Set-Content -LiteralPath (Join-Path $Out 'manifest.json') -Encoding UTF8
Write-Output "published: $Out"
