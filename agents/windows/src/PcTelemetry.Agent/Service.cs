// S2-02 Windows 서비스 수명주기.
//
// - 시작·정상 종료·시작 실패·실행 중 실패를 Windows 이벤트 로그(Application, 원본 PcTelemetryAgent)에 남긴다.
//   상태 폴더를 쓸 수 없는 권한 오류도 기록되도록 파일 로그가 아니라 이벤트 로그를 쓴다.
// - 실패하면 종료 코드 1로 끝낸다. 설치 스크립트가 SCM 복구 동작(재시작)과 failureflag=1을 설정하므로
//   SCM이 같은 서비스를 다시 시작한다(자동 복구).
// - 정상 종료(SCM 중지·OS 종료): 수집 취소 → 미동결 샘플 동결 → 전송 취소(짧은 마무리 전송) → DB 닫기 → 실행 표시 삭제.
//   OS 종료 시 서비스 대기 한도(WaitToKillServiceTimeout, 이 PC 5초) 안에 끝내려고 마무리 전송을 2초로 제한한다.
// - 이벤트 로그와 events.jsonl에는 토큰·계정 이름·호스트 이름을 넣지 않는다(계정은 종류만).

using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Security;
using System.Security.Principal;
using System.ServiceProcess;
using System.Text.Json;
using Microsoft.Extensions.Hosting;
using PcTelemetry.Collector;

namespace PcTelemetry.Agent;

/// <summary>
/// 실행 중 구성 요소(업로더 등)가 실패해 수집을 정상 절차로 멈췄다. 실행 표시는 지우고(DB는 닫힘)
/// 서비스는 실패 경로(이벤트 1005, 종료 코드 1 → SCM 재시작)로 간다.
/// </summary>
internal sealed class AgentFaultException(string message, Exception inner) : Exception(message, inner);

/// <summary>
/// RunAsync 진행 단계. 실패가 시작 중인지 수집 중인지 구분해 이벤트 ID를 고른다.
/// 센서 구성이 정해진 뒤(워밍업·탐색 완료)에 수집 중으로 바뀐다.
/// </summary>
internal sealed class RunProgress
{
    private volatile bool _collecting;
    public bool Collecting => _collecting;
    public void MarkCollecting() => _collecting = true;
}

internal sealed class AgentService(AgentOptions options, IHostApplicationLifetime lifetime, IHostLifetime hostLifetime)
    : BackgroundService
{
    public const string ServiceName = "PcTelemetryAgent";

    protected override async Task ExecuteAsync(CancellationToken stoppingToken)
    {
        await Task.Yield();
        var progress = new RunProgress();
        long stopRequested = 0;
        using var reg = stoppingToken.Register(() => Interlocked.Exchange(ref stopRequested, Stopwatch.GetTimestamp()));
        ServiceLog.Write(ServiceLog.Started, EventLogEntryType.Information,
            $"PC Telemetry Agent {Version} starting. account={AccessCheck.AccountKind()} " +
            $"state_dir={options.StateDir} upload_configured={options.Server is not null} stop_drain_s={options.StopDrain.TotalSeconds}");
        try
        {
            var code = await Program.RunAsync(options with { Seconds = null }, stoppingToken, console: false, progress)
                .ConfigureAwait(false);
            if (stoppingToken.IsCancellationRequested)
            {
                var ms = Stopwatch.GetElapsedTime(Interlocked.Read(ref stopRequested)).TotalMilliseconds;
                ServiceLog.Write(ServiceLog.Stopped, EventLogEntryType.Information,
                    $"PC Telemetry Agent stopped cleanly (collection and upload cancelled, buffer closed) in {ms:F0} ms.");
                return;
            }
            Fail(progress, $"collection ended without a stop request (exit {code})", code == 0 ? 1 : code);
        }
        catch (AgentFaultException ex)
        {
            // 수집은 정상 절차로 멈췄고(동결·DB 닫기) 실행 중 구성 요소가 실패했다(예: 업로더).
            Fail(progress, ex.Message, 1);
        }
        catch (Exception ex)
        {
            Fail(progress, $"{ex.GetType().Name}: {ex.Message}", 1);
        }
    }

    private void Fail(RunProgress progress, string message, int exitCode)
    {
        var id = progress.Collecting ? ServiceLog.Faulted : ServiceLog.StartFailed;
        var what = progress.Collecting ? "Collection failed" : "Start failed";
        ServiceLog.Write(id, EventLogEntryType.Error,
            $"{what}: {message}. Exiting with code {exitCode}; the service recovery action restarts the service.");
        // SCM에 보고하는 종료 코드는 ServiceBase.ExitCode다. Environment.ExitCode만 두면 .NET 서비스 호스트가 0으로
        // 보고해 복구 동작이 걸리지 않는다(S2-02 실측). failureflag=1이면 0이 아닌 종료도 복구 동작 대상이다.
        if (hostLifetime is ServiceBase service) service.ExitCode = exitCode;
        Environment.ExitCode = exitCode;
        lifetime.StopApplication();
    }

    internal static string? Version => typeof(AgentService).Assembly.GetName().Version?.ToString(3);
}

/// <summary>Windows 이벤트 로그 기록. 원본은 설치 스크립트가 등록한다. 쓰지 못해도 수집에는 영향을 주지 않는다.</summary>
internal static class ServiceLog
{
    public const string Source = "PcTelemetryAgent";
    public const int Started = 1000;
    public const int Stopped = 1001;
    public const int StartFailed = 1002;
    public const int UncleanShutdown = 1003;
    public const int AccessCheck = 1004;
    public const int Faulted = 1005;

    private static readonly Dictionary<string, (int Id, EventLogEntryType Type)> Mirrored = new(StringComparer.Ordinal)
    {
        ["unclean_shutdown_detected"] = (UncleanShutdown, EventLogEntryType.Warning),
        ["boot_session_started"] = (1010, EventLogEntryType.Information),
        ["collection_held"] = (1011, EventLogEntryType.Warning),
        ["upload_retrying"] = (1012, EventLogEntryType.Information),
        ["upload_recovered"] = (1013, EventLogEntryType.Information),
        ["upload_paused_auth"] = (1014, EventLogEntryType.Warning),
        ["upload_auth_recovered"] = (1015, EventLogEntryType.Information),
        ["batch_quarantined"] = (1016, EventLogEntryType.Warning),
        ["buffer_reset"] = (1017, EventLogEntryType.Warning),
        ["buffer_pruned"] = (1018, EventLogEntryType.Warning),
        ["upload_failed"] = (1019, EventLogEntryType.Error),
    };

    public static void Write(int id, EventLogEntryType type, string message)
    {
        try
        {
            EventLog.WriteEntry(Source, message, type, id);
        }
        catch (Exception e) when (e is InvalidOperationException or Win32Exception or SecurityException or ArgumentException)
        {
        }
    }

    /// <summary>운영상 중요한 수집 이벤트를 이벤트 로그에도 남긴다. events.jsonl과 같은 내용(비밀 없음)이다.</summary>
    public static void Mirror(CollectionEvent e)
    {
        if (e.Type == "service_access_check")
        {
            var ok = e.Details.TryGetValue("ok", out var v) && v is true;
            Write(AccessCheck, ok ? EventLogEntryType.Information : EventLogEntryType.Warning,
                $"{e.Type} {JsonSerializer.Serialize(e.Details)}");
            return;
        }
        if (Mirrored.TryGetValue(e.Type, out var m))
            Write(m.Id, m.Type, $"{e.Type} {JsonSerializer.Serialize(e.Details)}");
    }
}

/// <summary>
/// 서비스 계정 접근 점검(S2-02 2단계). 상태 폴더·SQLite 버퍼는 여기까지 왔으면 쓸 수 있다.
/// 장비 토큰 파일은 열 수 있는지만 확인하고 내용을 기록하지 않는다. 센서는 드라이버(PawnIO)가 필요한 센서가
/// 권한·드라이버 부족으로 unsupported가 됐는지 센다(LocalService면 CPU 온도·전력·클럭이 모두 여기에 걸린다, S1-02a 5.5).
/// </summary>
internal static partial class AccessCheck
{
    /// <summary>
    /// 이 프로세스 토큰에 남은 권한(privilege) 이름. 설치 스크립트가 서비스 필요 권한을 SeChangeNotifyPrivilege로
    /// 줄이면(sc privs) LocalSystem이어도 SeDebug·SeLoadDriver·SeTcb 등이 토큰에서 빠진다. 읽지 못하면 null.
    /// </summary>
    public static List<string>? Privileges()
    {
        try
        {
            using var identity = WindowsIdentity.GetCurrent();
            GetTokenInformation(identity.Token, 3 /* TokenPrivileges */, IntPtr.Zero, 0, out var size);
            var buffer = Marshal.AllocHGlobal(size);
            try
            {
                if (!GetTokenInformation(identity.Token, 3, buffer, size, out _)) return null;
                var count = Marshal.ReadInt32(buffer);
                var names = new List<string>(count);
                for (var i = 0; i < count; i++)
                {
                    // TOKEN_PRIVILEGES: DWORD PrivilegeCount; LUID_AND_ATTRIBUTES[] (LUID 8바이트 + Attributes 4바이트)
                    var luid = buffer + 4 + i * 12;
                    var length = 0;
                    LookupPrivilegeNameW(null, luid, null, ref length);
                    var name = new char[length + 1];
                    if (LookupPrivilegeNameW(null, luid, name, ref length)) names.Add(new string(name, 0, length));
                }
                names.Sort(StringComparer.Ordinal);
                return names;
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
        }
        catch (Exception e) when (e is Win32Exception or OutOfMemoryException or ArgumentException)
        {
            return null;
        }
    }

    [LibraryImport("advapi32.dll", SetLastError = true)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool GetTokenInformation(IntPtr token, int infoClass, IntPtr info, int length, out int returned);

    [LibraryImport("advapi32.dll", EntryPoint = "LookupPrivilegeNameW", SetLastError = true, StringMarshalling = StringMarshalling.Utf16)]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool LookupPrivilegeNameW(string? system, IntPtr luid, [Out] char[]? name, ref int length);

    public static string AccountKind()
    {
        using var identity = WindowsIdentity.GetCurrent();
        var sid = identity.User?.Value;
        return sid switch
        {
            "S-1-5-18" => "local_system",
            "S-1-5-19" => "local_service",
            "S-1-5-20" => "network_service",
            _ when sid?.StartsWith("S-1-5-80-", StringComparison.Ordinal) == true => "virtual_service_account",
            _ => new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator) ? "elevated_user" : "user",
        };
    }

    public static string TokenFile(string? path)
    {
        if (path is null) return "not_configured";
        try
        {
            using var fs = new FileStream(path, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
            return fs.Length > 0 ? "ok" : "empty";
        }
        catch (Exception e) when (e is FileNotFoundException or DirectoryNotFoundException)
        {
            return "missing";
        }
        catch (UnauthorizedAccessException)
        {
            return "denied";
        }
        catch (IOException)
        {
            return "error";
        }
    }

    public static Dictionary<string, object?> Run(AgentOptions options, bool service, SensorCollector collector, SqliteBuffer? buffer)
    {
        var account = AccountKind();
        var support = collector.Config?.Sensors.ToDictionary(s => s.SensorId, s => s.Support) ?? [];
        var driverSensors = collector.Bindings.Where(b => b.RequiresDriver).ToList();
        var driverUnavailable = driverSensors.Count(b => b.UnsupportedReason == "driver_unavailable");
        var token = TokenFile(options.TokenFile);
        var accountOk = !service || account == "local_system";
        var ok = accountOk && driverUnavailable == 0 && token is "ok" or "not_configured";
        return new Dictionary<string, object?>
        {
            ["ok"] = ok,
            ["mode"] = service ? "service" : "console",
            ["account"] = account,
            ["account_expected"] = service ? "local_system" : null,
            ["state_dir"] = "ok",
            ["buffer"] = buffer is null ? "not_configured" : "ok",
            ["token_file"] = token,
            ["sensors_total"] = support.Count,
            ["sensors_supported"] = support.Values.Count(s => s == "supported"),
            ["driver_sensors"] = driverSensors.Count,
            ["driver_sensors_supported"] = driverSensors.Count(b => support.GetValueOrDefault(b.SensorId) == "supported"),
            ["driver_unavailable"] = driverUnavailable,
            ["privileges"] = Privileges(),
        };
    }
}
