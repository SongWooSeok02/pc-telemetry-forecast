param(
    [Parameter(Mandatory)] [int] $FixturePid,
    [Parameter(Mandatory)] [ValidateSet('minimize', 'restore')] [string] $Action
)

$ErrorActionPreference = 'Stop'
Add-Type -MemberDefinition @'
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool ShowWindowAsync(System.IntPtr window, int command);
[System.Runtime.InteropServices.DllImport("user32.dll")]
public static extern bool IsIconic(System.IntPtr window);
'@ -Name RefreshFixture -Namespace WindowValidation
$fixture = Get-Process -Id $FixturePid
if ($fixture.ProcessName -ne 'PcTelemetry.Shell' -or $fixture.MainWindowHandle -eq 0) {
    throw 'Expected the running PcTelemetry.Shell fixture window.'
}
$handle = $fixture.MainWindowHandle
$minimized = $Action -eq 'minimize'
$command = if ($minimized) { 6 } else { 9 }
if (-not [WindowValidation.RefreshFixture]::ShowWindowAsync($handle, $command)) {
    throw 'Could not change the fixture window state.'
}
for ($attempt = 0; $attempt -lt 30; $attempt++) {
    if ([WindowValidation.RefreshFixture]::IsIconic($handle) -eq $minimized) { return }
    Start-Sleep -Milliseconds 50
}
throw 'Fixture window did not enter the requested native state.'
