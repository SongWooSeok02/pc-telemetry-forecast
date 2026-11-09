using PcTelemetry.Shell.Core;
using Xunit;

namespace PcTelemetry.Shell.Tests;

public class ConfigTests
{
    [Theory]
    [InlineData("https://dashboard.example.com/")]
    [InlineData("https://dashboard.example.com/app?machine_id=6f1c")]
    [InlineData("http://127.0.0.1:5173/")]
    [InlineData("http://localhost:5173/")]
    [InlineData("http://[::1]:5173/")]
    public void AcceptsHttpsAndLoopbackHttp(string url)
    {
        var (uri, error, _) = ShellConfigLoader.ValidateDashboardUrl(url);
        Assert.Null(error);
        Assert.NotNull(uri);
    }

    [Theory]
    [InlineData("http://dashboard.example.com/", ConfigError.InsecureScheme)]
    [InlineData("http://192.168.0.10:8000/", ConfigError.InsecureScheme)]
    [InlineData("file:///C:/dashboard/index.html", ConfigError.InsecureScheme)]
    [InlineData("javascript:alert(1)", ConfigError.InsecureScheme)]
    [InlineData("dashboard.example.com", ConfigError.NotAbsolute)]
    [InlineData("https://user:pass@dashboard.example.com/", ConfigError.UserInfo)]
    [InlineData("https://dashboard.example.com/?token=abc", ConfigError.SecretInQuery)]
    [InlineData("https://dashboard.example.com/?machine_id=1&Access_Token=abc", ConfigError.SecretInQuery)]
    [InlineData("https://dashboard.example.com/?pass%77ord=abc", ConfigError.SecretInQuery)]
    [InlineData("https://dashboard.example.com/?code=123456", ConfigError.SecretInQuery)]
    public void RejectsUnsafeUrls(string url, ConfigError expected)
    {
        var (_, error, _) = ShellConfigLoader.ValidateDashboardUrl(url);
        Assert.Equal(expected, error);
    }

    [Fact]
    public void CommandLineOverridesFileAndServiceNameIsRead()
    {
        using var dir = new TempDir();
        var file = Path.Combine(dir.Path, "shell.json");
        File.WriteAllText(file, """{"dashboard_url":"https://a.example.com/","agent_service_name":"OtherAgent"}""");

        var fromFile = ShellConfigLoader.Load(null, file);
        Assert.Equal("https://a.example.com/", fromFile.Config!.DashboardUrl.AbsoluteUri);
        Assert.Equal("OtherAgent", fromFile.Config.AgentServiceName);

        var fromArgs = ShellConfigLoader.Load("http://127.0.0.1:5173/", file);
        Assert.Equal("http://127.0.0.1:5173/", fromArgs.Config!.DashboardUrl.AbsoluteUri);
    }

    [Fact]
    public void MissingAndBrokenConfig()
    {
        using var dir = new TempDir();
        Assert.Equal(ConfigError.Missing, ShellConfigLoader.Load(null, Path.Combine(dir.Path, "none.json")).Error);
        var broken = Path.Combine(dir.Path, "shell.json");
        File.WriteAllText(broken, "{ not json");
        Assert.Equal(ConfigError.Unreadable, ShellConfigLoader.Load(null, broken).Error);
        // 명령행 주소가 있으면 깨진 파일 때문에 막지 않는다(기본 서비스 이름 사용).
        var withArg = ShellConfigLoader.Load("https://a.example.com/", broken);
        Assert.Equal(ShellConfigLoader.DefaultServiceName, withArg.Config!.AgentServiceName);
    }

    [Fact]
    public void RedactDropsQueryAndFragment() =>
        Assert.Equal("https://a.example.com/app",
            ShellConfigLoader.Redact(new Uri("https://a.example.com/app?machine_id=1#x")));
}

public class NavigationTests
{
    private static readonly Uri Dashboard = new("https://dash.example.com/app");

    [Theory]
    [InlineData("https://dash.example.com/other?machine_id=1", NavigationDecision.Allow)]
    [InlineData("https://DASH.example.com:443/", NavigationDecision.Allow)]
    [InlineData("about:blank", NavigationDecision.Allow)]
    [InlineData("https://docs.example.org/help", NavigationDecision.OpenExternally)]
    [InlineData("https://dash.example.com:8443/", NavigationDecision.OpenExternally)]
    [InlineData("http://dash.example.com/", NavigationDecision.Block)]
    [InlineData("https://u:p@evil.example.com/", NavigationDecision.Block)]
    [InlineData("file:///C:/Windows/win.ini", NavigationDecision.Block)]
    [InlineData("javascript:alert(1)", NavigationDecision.Block)]
    [InlineData("not a url", NavigationDecision.Block)]
    public void Decide(string target, NavigationDecision expected) =>
        Assert.Equal(expected, NavigationPolicy.Decide(Dashboard, target));
}

public class LaunchPlannerTests
{
    private sealed class FixedProbe(RuntimeStatus status) : IRuntimeProbe
    {
        public int Calls { get; private set; }
        public RuntimeStatus Probe() { Calls++; return status; }
    }

    private static readonly ConfigResult Good = ConfigResult.Ok(new ShellConfig(new Uri("https://a.example.com/"), "PcTelemetryAgent"));
    private static readonly RuntimeStatus Installed = new("157.0.4341.42", RuntimeScope.PerMachine, @"C:\Program Files (x86)\Microsoft\EdgeWebView\Application", null);

    [Fact]
    public void RuntimePresentAndConfigOk_ShowsDashboard()
    {
        var plan = LaunchPlanner.Plan(Good, new FixedProbe(Installed), @"C:\app", _ => false);
        Assert.Equal(ShellScreen.Dashboard, plan.Screen);
        Assert.Null(plan.Install.BootstrapperPath);
    }

    [Fact]
    public void RuntimeMissing_TakesPriorityOverConfigError_AndOffersBundledBootstrapper()
    {
        var expected = Path.Combine(@"C:\app", LaunchPlanner.BootstrapperRelativePath);
        var plan = LaunchPlanner.Plan(ConfigResult.Fail(ConfigError.Missing), new FixedProbe(RuntimeStatus.Missing("runtime_not_found")),
            @"C:\app", p => p == expected);
        Assert.Equal(ShellScreen.RuntimeMissing, plan.Screen);
        Assert.Equal(expected, plan.Install.BootstrapperPath);
        Assert.Equal("https", plan.Install.DownloadPage.Scheme);
    }

    [Fact]
    public void RuntimePresentButConfigBad_ShowsConfigError()
    {
        var plan = LaunchPlanner.Plan(ConfigResult.Fail(ConfigError.InsecureScheme), new FixedProbe(Installed), @"C:\app", _ => false);
        Assert.Equal(ShellScreen.ConfigError, plan.Screen);
    }
}

internal sealed class TempDir : IDisposable
{
    public string Path { get; } = Directory.CreateTempSubdirectory("pctf-shell-").FullName;
    public void Dispose() { try { Directory.Delete(Path, true); } catch (IOException) { } }
}
