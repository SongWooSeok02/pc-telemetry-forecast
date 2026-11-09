using System.ComponentModel;
using System.Diagnostics;
using System.Windows;
using System.Windows.Controls;
using System.Windows.Media;
using System.Windows.Threading;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.Wpf;
using PcTelemetry.Shell.Core;

namespace PcTelemetry.Shell;

/// <summary>
/// 셸 창: 위쪽 호스트 상태 줄 + 본문(대시보드 WebView2 또는 안내 화면).
/// 창을 닫아도 수집 서비스에는 아무 동작도 하지 않는다(서비스 제어 코드·권한 없음).
/// </summary>
internal sealed class MainWindow : Window
{
    private static readonly TimeSpan StatusInterval = TimeSpan.FromSeconds(5);

    private readonly ConfigResult config;
    private readonly IRuntimeProbe probe;
    private readonly ShellPaths paths;
    private readonly ShellLog log;
    private readonly Border body = new();
    private readonly TextBlock statusText = new() { VerticalAlignment = VerticalAlignment.Center, TextTrimming = TextTrimming.CharacterEllipsis };
    private readonly TextBlock statusDetail = new() { VerticalAlignment = VerticalAlignment.Center, Foreground = Brushes.DimGray, Margin = new Thickness(16, 0, 0, 0) };
    private readonly DispatcherTimer statusTimer;
    private LaunchPlan plan;
    private WebView2? webView;
    private Border? offlineOverlay;
    private ServerState server = ServerState.Connecting;
    private string? serverDetail;
    private HostSnapshot? lastSnapshot;

    public MainWindow(LaunchPlan plan, ConfigResult config, IRuntimeProbe probe, ShellPaths paths, ShellLog log)
    {
        this.plan = plan;
        this.config = config;
        this.probe = probe;
        this.paths = paths;
        this.log = log;
        Title = "PC Telemetry";
        Width = 1280;
        Height = 820;
        MinWidth = 720;
        MinHeight = 480;
        Background = Brushes.White;

        var strip = new DockPanel { LastChildFill = true };
        strip.Children.Add(statusDetail);
        DockPanel.SetDock(statusDetail, Dock.Right);
        strip.Children.Add(statusText);
        var root = new DockPanel();
        var top = new Border
        {
            Child = strip,
            Padding = new Thickness(14, 6, 14, 6),
            Background = new SolidColorBrush(Color.FromRgb(0xF3, 0xF5, 0xF8)),
            BorderBrush = new SolidColorBrush(Color.FromRgb(0xDD, 0xE1, 0xE6)),
            BorderThickness = new Thickness(0, 0, 0, 1),
        };
        DockPanel.SetDock(top, Dock.Top);
        root.Children.Add(top);
        root.Children.Add(body);
        Content = root;

        statusTimer = new DispatcherTimer(StatusInterval, DispatcherPriority.Background, (_, _) => RefreshHostStatus(), Dispatcher);
        Loaded += async (_, _) =>
        {
            RefreshHostStatus();
            statusTimer.Start();
            await ShowPlanAsync();
        };
        StateChanged += (_, _) => OnWindowStateChanged();
        Closed += (_, _) =>
        {
            statusTimer.Stop();
            log.Write("shell closed (collection service untouched)");
        };
    }

    // ---- 호스트 상태 ----

    private void RefreshHostStatus()
    {
        var service = plan.Config.Config?.AgentServiceName ?? ShellConfigLoader.DefaultServiceName;
        var snapshot = new HostSnapshot(ServiceProbe.Query(service), AgentStatusReader.Read(AgentStatusReader.DefaultPath, DateTime.UtcNow),
            plan.Screen == ShellScreen.Dashboard ? server : ServerState.Unreachable,
            plan.Screen == ShellScreen.Dashboard ? serverDetail : "대시보드 미표시");
        if (lastSnapshot is null || lastSnapshot.Service != snapshot.Service || lastSnapshot.Server != snapshot.Server
            || lastSnapshot.Agent.Freshness != snapshot.Agent.Freshness)
            log.Write($"host service={snapshot.Service} server={snapshot.Server} agent_status={snapshot.Agent.Freshness} pending={snapshot.Agent.PendingBatches?.ToString() ?? "-"}");
        lastSnapshot = snapshot;
        statusText.Text = HostStatusText.Summary(snapshot);
        statusDetail.Text = $"전송: {HostStatusText.Upload(snapshot.Agent)} · 마지막 전송 {HostStatusText.Time(snapshot.Agent.LastSuccessUtc)}";
        if (offlineOverlay is not null) offlineOverlay.Child = OfflineContent(snapshot);
    }

    /// <summary>
    /// README 15.6: 최소화 시 WebView2 컨트롤러를 숨기고(IsVisible=false → 웹 visibilitychange), 복원 시 다시 보인다.
    /// 복원은 표시만 바꾸며 셸이 조회를 일으키지 않는다. 최소화 중에는 로컬 상태 확인도 멈춘다.
    /// </summary>
    private void OnWindowStateChanged()
    {
        var minimized = WindowState == WindowState.Minimized;
        UpdateWebViewVisibility();
        if (minimized) statusTimer.Stop();
        else
        {
            RefreshHostStatus();
            statusTimer.Start();
        }
        log.Write($"window {(minimized ? "minimized: webview hidden" : "restored: webview visible")}");
    }

    /// <summary>
    /// WebView2(WPF)는 별도 HWND로 그려져 WPF 요소를 위에 겹칠 수 없다(airspace). 그래서 오프라인 안내를 보일 때도
    /// 컨트롤을 숨긴다. 최소화가 아니고 오프라인 안내가 없을 때만 보인다.
    /// </summary>
    private void UpdateWebViewVisibility()
    {
        if (webView is null) return;
        var show = WindowState != WindowState.Minimized && offlineOverlay is null;
        webView.Visibility = show ? Visibility.Visible : Visibility.Hidden;
    }

    // ---- 화면 전환 ----

    private async Task ShowPlanAsync()
    {
        switch (plan.Screen)
        {
            case ShellScreen.Dashboard:
                await ShowDashboardAsync(plan.Config.Config!);
                break;
            case ShellScreen.RuntimeMissing:
                body.Child = RuntimeMissingView();
                break;
            default:
                body.Child = ConfigErrorView();
                break;
        }
        RefreshHostStatus();
    }

    private async Task RecheckAsync()
    {
        plan = LaunchPlanner.Plan(config, probe, paths.AppDirectory, File.Exists);
        log.Write($"recheck screen={plan.Screen} runtime={plan.Runtime.Version ?? "none"}");
        await ShowPlanAsync();
    }

    // ---- 대시보드 ----

    private async Task ShowDashboardAsync(ShellConfig shell)
    {
        var view = new WebView2();
        var host = new Grid();
        host.Children.Add(view);
        webView = view;
        body.Child = host;
        try
        {
            Directory.CreateDirectory(paths.UserDataFolder);
            var env = await CoreWebView2Environment.CreateAsync(browserExecutableFolder: WebView2RuntimeProbe.FixedRuntimeFolder, userDataFolder: paths.UserDataFolder);
            await view.EnsureCoreWebView2Async(env);
        }
        catch (Exception e)
        {
            // 탐지 후 제거·손상 등으로 초기화가 실패하면 런타임 안내로 돌아간다.
            log.Write($"webview2 init failed: {e.GetType().Name} 0x{e.HResult:X8}");
            webView = null;
            body.Child = Guidance(
                "대시보드 화면을 시작하지 못했습니다",
                $"WebView2 초기화에 실패했습니다({e.GetType().Name}). 런타임을 다시 설치하거나 잠시 후 다시 시도하세요. " +
                "수집 서비스는 이 화면과 관계없이 계속 동작합니다.",
                InstallButtons(includeRecheck: true));
            return;
        }

        var core = view.CoreWebView2;
        core.Settings.IsStatusBarEnabled = false;
        core.Settings.IsPasswordAutosaveEnabled = false;
        core.Settings.IsGeneralAutofillEnabled = false;
#if !DEBUG
        core.Settings.AreDevToolsEnabled = false;
#endif
        core.NavigationStarting += (_, e) => Guard(shell, e.Uri, () => e.Cancel = true);
        core.FrameNavigationStarting += (_, e) => Guard(shell, e.Uri, () => e.Cancel = true);
        core.NewWindowRequested += (_, e) =>
        {
            e.Handled = true;
            switch (NavigationPolicy.Decide(shell.DashboardUrl, e.Uri))
            {
                case NavigationDecision.Allow: core.Navigate(e.Uri); break;
                case NavigationDecision.OpenExternally: OpenExternally(e.Uri); break;
            }
        };
        core.NavigationCompleted += (_, e) =>
        {
            if (e.IsSuccess)
            {
                SetServer(ServerState.Connected, null);
                HideOffline(host);
                return;
            }
            // 정책으로 취소한 이동은 연결 실패가 아니다.
            if (e.WebErrorStatus == CoreWebView2WebErrorStatus.OperationCanceled) return;
            var detail = HostStatusText.WebError(e.WebErrorStatus.ToString(), e.HttpStatusCode);
            log.Write($"navigation failed: {e.WebErrorStatus} http={e.HttpStatusCode}");
            SetServer(ServerState.Unreachable, detail);
            ShowOffline(host, shell);
        };
        core.ProcessFailed += (_, e) =>
        {
            log.Write($"webview2 process failed: {e.ProcessFailedKind} {e.Reason}");
            SetServer(ServerState.Unreachable, "화면 프로세스 오류");
            ShowOffline(host, shell);
        };

        Navigate(core, shell);
    }

    private void Navigate(CoreWebView2 core, ShellConfig shell)
    {
        SetServer(ServerState.Connecting, null);
        log.Write($"navigate {ShellConfigLoader.Redact(shell.DashboardUrl)}");
        core.Navigate(shell.DashboardUrl.AbsoluteUri);
    }

    private void SetServer(ServerState state, string? detail)
    {
        server = state;
        serverDetail = detail;
        RefreshHostStatus();
    }

    /// <summary>
    /// 서버에 닿지 못하면 브라우저 오류 페이지 대신 연결 상태와 로컬 대기 데이터 수를 보여 준다(README 3절).
    /// 자동 재시도는 하지 않는다(수동 갱신 모드와 충돌하지 않도록 사용자가 [다시 연결]을 누른다).
    /// </summary>
    private void ShowOffline(Grid host, ShellConfig shell)
    {
        if (offlineOverlay is null)
        {
            offlineOverlay = new Border { Background = Brushes.White };
            offlineOverlay.Tag = shell;
            host.Children.Add(offlineOverlay);
        }
        offlineOverlay.Child = OfflineContent(lastSnapshot);
        UpdateWebViewVisibility();
    }

    private void HideOffline(Grid host)
    {
        if (offlineOverlay is null) return;
        host.Children.Remove(offlineOverlay);
        offlineOverlay = null;
        UpdateWebViewVisibility();
    }

    private UIElement OfflineContent(HostSnapshot? snapshot)
    {
        var lines = snapshot is null ? "" :
            $"수집 서비스: {HostStatusText.Service(snapshot.Service)}\n" +
            $"전송 대기: {HostStatusText.Pending(snapshot.Agent)}\n" +
            $"전송 상태: {HostStatusText.Upload(snapshot.Agent)} · 마지막 전송 {HostStatusText.Time(snapshot.Agent.LastSuccessUtc)}";
        return Guidance(
            "대시보드 서버에 연결할 수 없습니다",
            $"서버 연결: {HostStatusText.Server(server, serverDetail)}. 네트워크나 서버 상태를 확인한 뒤 [다시 연결]을 누르세요. " +
            "수집 서비스는 서버와 관계없이 이 PC에서 계속 수집하고, 보내지 못한 데이터는 로컬에 보관했다가 연결되면 전송합니다.\n\n" + lines,
            [Button("다시 연결", () =>
            {
                if (webView?.CoreWebView2 is { } core && offlineOverlay?.Tag is ShellConfig shell) Navigate(core, shell);
                return Task.CompletedTask;
            })]);
    }

    private void Guard(ShellConfig shell, string uri, Action cancel)
    {
        var decision = NavigationPolicy.Decide(shell.DashboardUrl, uri);
        if (decision == NavigationDecision.Allow) return;
        cancel();
        Uri.TryCreate(uri, UriKind.Absolute, out var parsed);
        log.Write($"navigation {decision}: {(parsed is null ? "invalid" : ShellConfigLoader.Redact(parsed))}");
        if (decision == NavigationDecision.OpenExternally) OpenExternally(uri);
    }

    private void OpenExternally(string uri)
    {
        try
        {
            using var _ = Process.Start(new ProcessStartInfo(uri) { UseShellExecute = true });
        }
        catch (Exception e) when (e is Win32Exception or InvalidOperationException)
        {
            log.Write($"open externally failed: {e.GetType().Name}");
        }
    }

    // ---- 안내 화면 ----

    private UIElement RuntimeMissingView() => Guidance(
        "WebView2 런타임이 필요합니다",
        "대시보드 화면은 Microsoft Edge WebView2 런타임으로 표시합니다. 이 PC에서 런타임을 찾지 못했습니다. " +
        (plan.Install.BootstrapperPath is not null
            ? "아래 [WebView2 런타임 설치]를 누르면 함께 배포된 Microsoft 설치 프로그램을 실행합니다(인터넷 연결 필요). "
            : "아래 [다운로드 페이지 열기]에서 Evergreen 런타임을 설치한 뒤 [다시 확인]을 누르세요. ") +
        "수집 서비스는 이 화면과 관계없이 계속 동작합니다.",
        InstallButtons(includeRecheck: true));

    private UIElement ConfigErrorView()
    {
        var reason = plan.Config.Error switch
        {
            ConfigError.Missing => "대시보드 주소가 설정되지 않았습니다.",
            ConfigError.Unreadable => "설정 파일을 읽을 수 없습니다.",
            ConfigError.NotAbsolute => "대시보드 주소 형식이 올바르지 않습니다.",
            ConfigError.InsecureScheme => "대시보드 주소는 https여야 합니다(개발용 루프백 http만 예외).",
            ConfigError.UserInfo => "대시보드 주소에 사용자 이름·비밀번호를 넣을 수 없습니다.",
            ConfigError.SecretInQuery => $"대시보드 주소에 인증 정보로 보이는 값({plan.Config.Detail})을 넣을 수 없습니다.",
            _ => "설정을 확인할 수 없습니다.",
        };
        return Guidance("대시보드 주소를 확인해야 합니다",
            $"{reason} 관리자가 설치 폴더의 {ShellConfigLoader.FileName}을 수정해야 합니다. 수집 서비스는 계속 동작합니다.",
            [Button("다시 확인", async () => await RecheckAsync())]);
    }

    private UIElement[] InstallButtons(bool includeRecheck)
    {
        var buttons = new List<UIElement>();
        if (plan.Install.BootstrapperPath is { } setup)
            buttons.Add(Button("WebView2 런타임 설치", async () => await RunBootstrapperAsync(setup)));
        buttons.Add(Button("다운로드 페이지 열기", () => { OpenExternally(plan.Install.DownloadPage.AbsoluteUri); return Task.CompletedTask; }));
        if (includeRecheck) buttons.Add(Button("다시 확인", async () => await RecheckAsync()));
        return buttons.ToArray();
    }

    private async Task RunBootstrapperAsync(string setup)
    {
        log.Write("bootstrapper start");
        try
        {
            using var process = Process.Start(new ProcessStartInfo(setup) { UseShellExecute = true });
            if (process is not null)
            {
                IsEnabled = false;
                await process.WaitForExitAsync();
                log.Write($"bootstrapper exit={process.ExitCode}");
            }
        }
        catch (Win32Exception e) when (e.NativeErrorCode == 1223)
        {
            log.Write("bootstrapper cancelled by user (UAC)");
        }
        catch (Exception e) when (e is Win32Exception or InvalidOperationException)
        {
            log.Write($"bootstrapper failed: {e.GetType().Name}");
        }
        finally
        {
            IsEnabled = true;
        }
        await RecheckAsync();
    }

    private UIElement Guidance(string title, string message, UIElement[] buttons)
    {
        var panel = new StackPanel { MaxWidth = 640, Margin = new Thickness(32), VerticalAlignment = VerticalAlignment.Center };
        panel.Children.Add(new TextBlock { Text = title, FontSize = 22, FontWeight = FontWeights.SemiBold, Margin = new Thickness(0, 0, 0, 12) });
        panel.Children.Add(new TextBlock { Text = message, FontSize = 14, TextWrapping = TextWrapping.Wrap, Margin = new Thickness(0, 0, 0, 20) });
        var row = new WrapPanel();
        foreach (var b in buttons) row.Children.Add(b);
        panel.Children.Add(row);
        var runtime = plan.Runtime.Available
            ? $"{plan.Runtime.Version} ({plan.Runtime.Scope}) {plan.Runtime.Location}"
            : $"없음 ({plan.Runtime.Error})";
        panel.Children.Add(new TextBlock
        {
            Text = $"셸 {Program.ShellVersion} · WebView2 SDK {Program.SdkVersion} · 런타임 {runtime}",
            FontSize = 11,
            Foreground = Brushes.Gray,
            TextWrapping = TextWrapping.Wrap,
            Margin = new Thickness(0, 24, 0, 0),
        });
        return new ScrollViewer { Content = panel, VerticalScrollBarVisibility = ScrollBarVisibility.Auto };
    }

    private static Button Button(string text, Func<Task> onClick)
    {
        var button = new Button { Content = text, Padding = new Thickness(14, 6, 14, 6), Margin = new Thickness(0, 0, 8, 8), MinWidth = 120 };
        button.Click += async (_, _) => await onClick();
        return button;
    }
}
