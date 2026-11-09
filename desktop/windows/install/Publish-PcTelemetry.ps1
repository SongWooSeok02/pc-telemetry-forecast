<#
S1-06 배포 폴더 생성(개발 PC에서 실행, 관리자 불필요).

  .\Publish-PcTelemetry.ps1 [-Out <폴더>] [-NoWebView2Bootstrapper]

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
    [switch]$NoWebView2Bootstrapper
)
$ErrorActionPreference = 'Stop'
# Windows PowerShell 5.1은 param 기본값에서 $PSScriptRoot가 비어 있다.
if (-not $Out) { $Out = Join-Path $PSScriptRoot '..\publish' }
$repo = (Resolve-Path (Join-Path $PSScriptRoot '..\..\..')).Path
$Out = [IO.Path]::GetFullPath($Out)
if (Test-Path $Out) { Remove-Item -Recurse -Force $Out }
New-Item -ItemType Directory -Force $Out | Out-Null

function Publish-Project([string]$project, [string]$target) {
    & dotnet publish (Join-Path $repo $project) -c Release -r win-x64 --self-contained true -o $target -nologo -v q
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

function Describe([string]$path) {
    $item = Get-Item $path
    [ordered]@{
        path = $item.FullName.Substring($Out.Length + 1)
        version = $item.VersionInfo.FileVersion
        sha256 = (Get-FileHash $path -Algorithm SHA256).Hash.ToLowerInvariant()
        bytes = $item.Length
    }
}
$files = @(
    # exe는 공통 실행 호스트라 코드 변경을 반영하지 않을 수 있어 코드가 든 dll도 기록한다.
    Describe (Join-Path $Out 'agent\PcTelemetry.Agent.exe')
    Describe (Join-Path $Out 'agent\PcTelemetry.Agent.dll')
    Describe (Join-Path $Out 'shell\PcTelemetry.Shell.exe')
    Describe (Join-Path $Out 'shell\PcTelemetry.Shell.dll')
    Describe (Join-Path $Out 'shell\PcTelemetry.Shell.Core.dll')
    Describe (Join-Path $Out 'Install-PcTelemetry.ps1')
    Describe (Join-Path $Out 'Uninstall-PcTelemetry.ps1')
)
if ($bootstrapper) {
    $d = Describe $bootstrapper
    $d.signer = (Get-AuthenticodeSignature $bootstrapper).SignerCertificate.Subject
    $files += $d
}
$commit = (& git -C $repo rev-parse HEAD).Trim()
$dirty = [bool](& git -C $repo status --porcelain)
[ordered]@{
    task = 'S1-06'
    built_at_utc = (Get-Date).ToUniversalTime().ToString('yyyy-MM-ddTHH:mm:ssZ')
    commit = $commit
    dirty = $dirty
    dotnet_sdk = (& dotnet --version).Trim()
    files = $files
} | ConvertTo-Json -Depth 5 | Set-Content (Join-Path $Out 'manifest.json') -Encoding UTF8
Write-Output "published: $Out"
