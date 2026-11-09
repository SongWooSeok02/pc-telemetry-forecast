namespace PcTelemetry.Shell.Core;

public enum RuntimeScope { Unknown, PerMachine, PerUser }

/// <summary>WebView2 런타임 탐지 결과. Version이 null이면 사용할 수 있는 런타임이 없다.</summary>
public sealed record RuntimeStatus(string? Version, RuntimeScope Scope, string? Location, string? Error)
{
    public bool Available => Version is not null;
    public static RuntimeStatus Missing(string? error = null) => new(null, RuntimeScope.Unknown, null, error);
}

public interface IRuntimeProbe
{
    RuntimeStatus Probe();
}

/// <summary>누락 시 안내 화면에서 제공할 설치 경로.</summary>
public sealed record InstallOptions(string? BootstrapperPath, Uri DownloadPage);

public enum ShellScreen
{
    Dashboard,
    ConfigError,
    RuntimeMissing,
}

public sealed record LaunchPlan(ShellScreen Screen, ConfigResult Config, RuntimeStatus Runtime, InstallOptions Install);

public static class LaunchPlanner
{
    /// <summary>Microsoft 공식 Evergreen 런타임 배포 페이지. 설치 파일을 저장소에 포함하지 않는다.</summary>
    public static readonly Uri DownloadPage = new("https://developer.microsoft.com/microsoft-edge/webview2/");

    public const string BootstrapperRelativePath = @"redist\MicrosoftEdgeWebview2Setup.exe";

    public static LaunchPlan Plan(ConfigResult config, IRuntimeProbe probe, string appDirectory, Func<string, bool> fileExists)
    {
        var bootstrapper = Path.Combine(appDirectory, BootstrapperRelativePath);
        var install = new InstallOptions(fileExists(bootstrapper) ? bootstrapper : null, DownloadPage);
        // 런타임은 설정 오류와 관계없이 먼저 확인한다. 둘 다 문제면 런타임 안내가 먼저다(설정은 관리자가 고쳐야 한다).
        var runtime = probe.Probe();
        if (!runtime.Available) return new LaunchPlan(ShellScreen.RuntimeMissing, config, runtime, install);
        if (config.Config is null) return new LaunchPlan(ShellScreen.ConfigError, config, runtime, install);
        return new LaunchPlan(ShellScreen.Dashboard, config, runtime, install);
    }
}
