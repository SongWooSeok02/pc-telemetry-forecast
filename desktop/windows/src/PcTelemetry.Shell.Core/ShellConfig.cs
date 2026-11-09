using System.Net;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace PcTelemetry.Shell.Core;

/// <summary>셸 설정. 설치 관리자가 실행 파일 옆 shell.json에 두며, 개발 시 명령행으로 덮어쓴다.</summary>
public sealed record ShellConfig(Uri DashboardUrl, string AgentServiceName);

public enum ConfigError
{
    Missing,
    Unreadable,
    NotAbsolute,
    InsecureScheme,
    UserInfo,
    SecretInQuery,
}

public sealed record ConfigResult(ShellConfig? Config, ConfigError? Error, string? Detail)
{
    public static ConfigResult Ok(ShellConfig config) => new(config, null, null);
    public static ConfigResult Fail(ConfigError error, string? detail = null) => new(null, error, detail);
}

public static partial class ShellConfigLoader
{
    public const string FileName = "shell.json";
    public const string DefaultServiceName = "PcTelemetryAgent";

    // 로그인 세션과 장비 토큰은 URL로 전달하지 않는다(README 7절). 이런 이름의 쿼리는 설정 단계에서 거부한다.
    [GeneratedRegex("^(token|access_token|id_token|refresh_token|password|passwd|pwd|secret|session|session_id|auth|authorization|code|registration_code|key|api_key)$",
        RegexOptions.IgnoreCase | RegexOptions.CultureInvariant)]
    private static partial Regex SecretQueryKey();

    /// <summary>명령행 값이 있으면 그것을, 없으면 shell.json을 쓴다.</summary>
    public static ConfigResult Load(string? commandLineUrl, string? configPath)
    {
        string? url = commandLineUrl;
        string service = DefaultServiceName;
        if (configPath is not null && File.Exists(configPath))
        {
            try
            {
                using var doc = JsonDocument.Parse(File.ReadAllText(configPath));
                var root = doc.RootElement;
                if (url is null && root.TryGetProperty("dashboard_url", out var u) && u.ValueKind == JsonValueKind.String)
                    url = u.GetString();
                if (root.TryGetProperty("agent_service_name", out var s) && s.ValueKind == JsonValueKind.String
                    && !string.IsNullOrWhiteSpace(s.GetString()))
                    service = s.GetString()!;
            }
            catch (Exception e) when (e is JsonException or IOException or UnauthorizedAccessException)
            {
                if (url is null) return ConfigResult.Fail(ConfigError.Unreadable, e.GetType().Name);
            }
        }
        if (string.IsNullOrWhiteSpace(url)) return ConfigResult.Fail(ConfigError.Missing);
        var checkedUrl = ValidateDashboardUrl(url);
        return checkedUrl.Error is { } error
            ? ConfigResult.Fail(error, checkedUrl.Detail)
            : ConfigResult.Ok(new ShellConfig(checkedUrl.Url!, service));
    }

    public static (Uri? Url, ConfigError? Error, string? Detail) ValidateDashboardUrl(string value)
    {
        if (!Uri.TryCreate(value.Trim(), UriKind.Absolute, out var uri)) return (null, ConfigError.NotAbsolute, null);
        bool https = uri.Scheme == Uri.UriSchemeHttps;
        bool loopbackHttp = uri.Scheme == Uri.UriSchemeHttp && IsLoopback(uri);
        if (!https && !loopbackHttp) return (null, ConfigError.InsecureScheme, uri.Scheme);
        if (!string.IsNullOrEmpty(uri.UserInfo)) return (null, ConfigError.UserInfo, null);
        foreach (var key in QueryKeys(uri.Query))
            if (SecretQueryKey().IsMatch(key)) return (null, ConfigError.SecretInQuery, key);
        return (uri, null, null);
    }

    public static bool IsLoopback(Uri uri) =>
        uri.IsLoopback || (IPAddress.TryParse(uri.IdnHost.Trim('[', ']'), out var ip) && IPAddress.IsLoopback(ip));

    internal static IEnumerable<string> QueryKeys(string query)
    {
        foreach (var part in query.TrimStart('?').Split('&', StringSplitOptions.RemoveEmptyEntries))
        {
            var key = part.Split('=', 2)[0];
            yield return Uri.UnescapeDataString(key.Replace('+', ' '));
        }
    }

    /// <summary>로그·화면 표시용. 쿼리와 조각은 지운다.</summary>
    public static string Redact(Uri uri) => uri.GetLeftPart(UriPartial.Path);
}
