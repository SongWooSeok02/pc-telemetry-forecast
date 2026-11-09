using System.Text.Json;
using System.Windows;
using Microsoft.Web.WebView2.Core;
using PcTelemetry.Shell.Core;

namespace PcTelemetry.Shell;

internal static class Program
{
    internal static readonly JsonSerializerOptions Json = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Converters = { new System.Text.Json.Serialization.JsonStringEnumConverter(JsonNamingPolicy.SnakeCaseLower) },
    };

    public static string ShellVersion => typeof(Program).Assembly.GetName().Version?.ToString(3) ?? "unknown";
    public static string SdkVersion => typeof(CoreWebView2Environment).Assembly.GetName().Version?.ToString() ?? "unknown";

    /// <summary>
    /// 사용법:
    ///   PcTelemetry.Shell.exe [--dashboard-url URL]
    ///   PcTelemetry.Shell.exe --check [--out PATH] [--dashboard-url URL]   창 없이 탐지 결과만 JSON으로 남긴다.
    /// 종료 코드(--check): 0 대시보드 표시 가능, 2 런타임 없음, 3 설정 오류, 64 인자 오류.
    /// </summary>
    [STAThread]
    private static int Main(string[] args)
    {
        string? url = null, outPath = null;
        bool check = false;
        for (int i = 0; i < args.Length; i++)
        {
            switch (args[i])
            {
                case "--dashboard-url" when i + 1 < args.Length: url = args[++i]; break;
                case "--out" when i + 1 < args.Length: outPath = args[++i]; break;
                case "--check": check = true; break;
                default: return 64;
            }
        }

        var paths = ShellPaths.Default();
        var log = new ShellLog(paths.LogFile);
        var probe = new WebView2RuntimeProbe();
        var config = ShellConfigLoader.Load(url, paths.ConfigFile);
        var plan = LaunchPlanner.Plan(config, probe, paths.AppDirectory, File.Exists);
        log.Write($"start shell={ShellVersion} sdk={SdkVersion} screen={plan.Screen} runtime={plan.Runtime.Version ?? "none"} " +
                  $"scope={plan.Runtime.Scope} config={(plan.Config.Error?.ToString() ?? "ok")} check={check}");

        if (check)
        {
            var report = CheckReport(plan, paths);
            var target = outPath ?? Path.Combine(paths.UserRoot, "check.json");
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(target))!);
            File.WriteAllText(target, JsonSerializer.Serialize(report, Json));
            return plan.Screen switch { ShellScreen.Dashboard => 0, ShellScreen.RuntimeMissing => 2, _ => 3 };
        }

        var app = new Application { ShutdownMode = ShutdownMode.OnMainWindowClose };
        return app.Run(new MainWindow(plan, config, probe, paths, log));
    }

    internal static object CheckReport(LaunchPlan plan, ShellPaths paths) => new
    {
        Task = "S1-06",
        CheckedAtUtc = DateTime.UtcNow.ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ"),
        ShellVersion,
        WebView2SdkVersion = SdkVersion,
        OsVersion = Environment.OSVersion.VersionString,
        Is64BitProcess = Environment.Is64BitProcess,
        Screen = plan.Screen,
        Runtime = new
        {
            plan.Runtime.Available,
            plan.Runtime.Version,
            plan.Runtime.Scope,
            plan.Runtime.Location,
            plan.Runtime.Error,
        },
        Config = new
        {
            Ok = plan.Config.Error is null,
            Error = plan.Config.Error,
            DashboardOrigin = plan.Config.Config is { } c ? ShellConfigLoader.Redact(c.DashboardUrl) : null,
            ServiceName = plan.Config.Config?.AgentServiceName,
        },
        Install = new
        {
            BootstrapperPresent = plan.Install.BootstrapperPath is not null,
            DownloadPage = plan.Install.DownloadPage.AbsoluteUri,
        },
        // 사용자 이름이 들어간 절대 경로 대신 기준 폴더 이름만 남긴다.
        UserDataFolder = @"%LOCALAPPDATA%\PcTelemetry\Shell\WebView2",
        UserDataFolderMatchesDefault = string.Equals(paths.UserDataFolder,
            Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PcTelemetry", "Shell", "WebView2"),
            StringComparison.OrdinalIgnoreCase),
    };
}
