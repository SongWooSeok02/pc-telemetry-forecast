using Microsoft.Web.WebView2.Core;
using Microsoft.Win32;
using PcTelemetry.Shell.Core;

namespace PcTelemetry.Shell;

/// <summary>
/// Evergreen WebView2 런타임 탐지. 사용 가능 여부는 SDK 로더(GetAvailableBrowserVersionString)로 판정하고,
/// 설치 범위·경로는 진단용으로 EdgeUpdate 레지스트리에서 읽는다(Microsoft 배포 문서의 탐지 키).
/// </summary>
internal sealed class WebView2RuntimeProbe : IRuntimeProbe
{
    private const string ClientKey = @"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

    /// <summary>
    /// 고정 버전 런타임 폴더. 설정되면 Evergreen 대신 이 폴더만 사용한다(탐지·초기화 모두). 런타임 누락 재현에도 쓴다.
    /// </summary>
    public static string? FixedRuntimeFolder =>
        Environment.GetEnvironmentVariable("WEBVIEW2_BROWSER_EXECUTABLE_FOLDER") is { Length: > 0 } folder ? folder : null;

    public RuntimeStatus Probe()
    {
        string? version;
        var fixedFolder = FixedRuntimeFolder;
        try
        {
            version = CoreWebView2Environment.GetAvailableBrowserVersionString(fixedFolder);
        }
        catch (WebView2RuntimeNotFoundException)
        {
            return RuntimeStatus.Missing("runtime_not_found");
        }
        catch (Exception e)
        {
            return RuntimeStatus.Missing(e.GetType().Name);
        }
        if (string.IsNullOrEmpty(version)) return RuntimeStatus.Missing("runtime_not_found");

        if (fixedFolder is not null) return new RuntimeStatus(version, RuntimeScope.Unknown, RedactProfile(fixedFolder), null);
        var (scope, location) = Registered();
        return new RuntimeStatus(version, scope, location is null ? null : RedactProfile(location), null);
    }

    /// <summary>사용자별 설치 경로에 들어가는 계정 이름을 진단 출력에서 가린다.</summary>
    internal static string RedactProfile(string path)
    {
        var profile = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
        return !string.IsNullOrEmpty(profile) && path.StartsWith(profile, StringComparison.OrdinalIgnoreCase)
            ? "%USERPROFILE%" + path[profile.Length..]
            : path;
    }

    internal static (RuntimeScope Scope, string? Location) Registered()
    {
        foreach (var (hive, path, scope) in new[]
        {
            (RegistryHive.LocalMachine, @"SOFTWARE\WOW6432Node\" + ClientKey, RuntimeScope.PerMachine),
            (RegistryHive.LocalMachine, @"SOFTWARE\" + ClientKey, RuntimeScope.PerMachine),
            (RegistryHive.CurrentUser, @"Software\" + ClientKey, RuntimeScope.PerUser),
        })
        {
            try
            {
                using var root = RegistryKey.OpenBaseKey(hive, RegistryView.Registry64);
                using var key = root.OpenSubKey(path);
                var pv = key?.GetValue("pv") as string;
                // 제거 후에는 pv가 비거나 0.0.0.0으로 남는다.
                if (string.IsNullOrEmpty(pv) || pv == "0.0.0.0") continue;
                return (scope, key!.GetValue("location") as string);
            }
            catch (Exception e) when (e is System.Security.SecurityException or UnauthorizedAccessException or IOException)
            {
            }
        }
        return (RuntimeScope.Unknown, null);
    }
}
