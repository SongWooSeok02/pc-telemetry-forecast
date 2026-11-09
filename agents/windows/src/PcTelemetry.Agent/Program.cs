// S1-02b 수집 에이전트.
//
//   PcTelemetry.Agent discover [--out file.json]
//       센서를 탐색해 계약 매핑(정의·원천 연결)을 출력한다. 임시 상태 디렉터리를 쓴다.
//   PcTelemetry.Agent run [--state-dir DIR] [--seconds N]
//                         [--server URL --machine-id UUID --token-file FILE] [--status-file PATH|none]
//       콘솔에서 1초 수집. --machine-id가 있으면 3초 배치를 동결해 스풀에 쌓고, --server가 있으면 전송한다.
//   PcTelemetry.Agent service [위와 같은 옵션]
//       Windows 서비스로 실행(LocalSystem 필요, S1-02a 5.5).

using System.Text.Json;
using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;
using PcTelemetry.Collector;

namespace PcTelemetry.Agent;

internal sealed record AgentOptions(
    string StateDir,
    int? Seconds,
    Uri? Server,
    Guid? MachineId,
    string? TokenFile,
    int IntervalMs,
    string? StatusFile);

internal static class Program
{
    private static readonly JsonSerializerOptions Pretty = new() { WriteIndented = true, PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower };

    private static async Task<int> Main(string[] args)
    {
        if (args.Length == 0)
        {
            Console.Error.WriteLine("usage: PcTelemetry.Agent discover|run|service [options]");
            return 2;
        }
        Dictionary<string, string> o;
        AgentOptions options;
        try
        {
            o = Parse(args.Skip(1).ToArray());
            options = new AgentOptions(
                o.GetValueOrDefault("state-dir") ?? Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "PcTelemetry", "state"),
                o.TryGetValue("seconds", out var s) ? int.Parse(s) : null,
                o.TryGetValue("server", out var url) ? new Uri(url) : null,
                o.TryGetValue("machine-id", out var m) ? Guid.Parse(m) : null,
                o.GetValueOrDefault("token-file"),
                o.TryGetValue("interval-ms", out var i) ? int.Parse(i) : 1000,
                o.GetValueOrDefault("status-file") is { } sf
                    ? (sf == "none" ? null : sf)
                    : Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData),
                        "PcTelemetry", "status", "agent-status.json"));
            if (options.Server is not null && (options.MachineId is null || options.TokenFile is null))
                throw new ArgumentException("--server requires --machine-id and --token-file");
        }
        catch (Exception ex) when (ex is ArgumentException or FormatException)
        {
            Console.Error.WriteLine(ex.Message);
            return 2;
        }

        switch (args[0])
        {
            case "discover":
                return Discover(o.GetValueOrDefault("out"));
            case "run":
                using (var cts = new CancellationTokenSource())
                {
                    Console.CancelKeyPress += (_, e) => { e.Cancel = true; cts.Cancel(); };
                    return await RunAsync(options, cts.Token, console: true);
                }
            case "service":
                var builder = Host.CreateApplicationBuilder();
                builder.Services.AddWindowsService(x => x.ServiceName = "PcTelemetryAgent");
                builder.Services.AddHostedService(_ => new AgentService(options));
                await builder.Build().RunAsync();
                return 0;
            default:
                Console.Error.WriteLine($"unknown command: {args[0]}");
                return 2;
        }
    }

    internal static async Task<int> RunAsync(AgentOptions options, CancellationToken token, bool console)
    {
        var store = new StateStore(options.StateDir);
        var events = new JsonlEventSink(store.PathOf("events.jsonl"));
        if (console)
        {
            events.Emitted += e => Console.WriteLine(
                $"{Utc.Format(e.ObservedAtUtc)} {e.Type} {JsonSerializer.Serialize(e.Details)}");
        }

        var started = DateTime.UtcNow;
        Spool? spool = options.MachineId is null ? null : new Spool(store.Directory, events);
        var batcher = spool is null ? null : new Batcher(options.MachineId!.Value, spool);
        using var source = new LhmSource();
        var collector = new SensorCollector(source, new WindowsClock(), new RegistryBootId(), events, store, batcher,
            new CollectorOptions { IntervalMs = options.IntervalMs });

        while (true)
        {
            try
            {
                collector.Start();
                break;
            }
            catch (BootIdentityUnavailableException ex)
            {
                // 계약: OS 부팅 식별을 확인하지 못하면 수집을 유보한다.
                events.Emit(new CollectionEvent(Guid.NewGuid(), "collection_held", DateTime.UtcNow, 0, null,
                    new Dictionary<string, object?> { ["reason"] = ex.Message }));
                try { await Task.Delay(TimeSpan.FromSeconds(30), token); }
                catch (OperationCanceledException) { return 1; }
            }
        }

        using var stop = CancellationTokenSource.CreateLinkedTokenSource(token);
        if (options.Seconds is { } seconds) stop.CancelAfter(TimeSpan.FromSeconds(seconds));
        using var uploader = options.Server is null ? null
            : new Uploader(options.Server, options.TokenFile!, spool!, events);
        using var uploadStop = new CancellationTokenSource();

        var collect = Task.Run(() => collector.RunAsync(stop.Token), CancellationToken.None);
        var upload = uploader is null ? Task.CompletedTask : Task.Run(() => uploader.RunAsync(uploadStop.Token), CancellationToken.None);
        var statsPath = store.PathOf("stats.json");
        var tick = 0;
        WriteStatus(options.StatusFile, collecting: true, uploader, spool);
        while (!collect.IsCompleted)
        {
            await Task.WhenAny(collect, Task.Delay(TimeSpan.FromSeconds(10), CancellationToken.None));
            WriteStatus(options.StatusFile, collecting: !collect.IsCompleted, uploader, spool);
            if (++tick % 6 == 0 || collect.IsCompleted)
                WriteStats(statsPath, options, started, source, collector, uploader, spool);
        }
        await collect;
        await uploadStop.CancelAsync();
        await upload;
        if (uploader is not null) await uploader.DrainAsync(TimeSpan.FromSeconds(20));
        WriteStats(statsPath, options, started, source, collector, uploader, spool);
        WriteStatus(options.StatusFile, collecting: false, uploader, spool);
        if (console) Console.WriteLine($"stats: {statsPath}");
        return 0;
    }

    private static void WriteStats(string path, AgentOptions options, DateTime started, LhmSource source,
        SensorCollector collector, Uploader? uploader, Spool? spool)
    {
        var s = collector.Stats;
        var report = new Dictionary<string, object?>
        {
            ["task"] = "S1-02b",
            ["host"] = HostInfo.Describe(),
            ["started_at_utc"] = Utc.Format(started),
            ["updated_at_utc"] = Utc.Format(DateTime.UtcNow),
            ["interval_ms"] = options.IntervalMs,
            ["lhm_open_ms"] = source.OpenMs,
            ["boot_session_id"] = collector.BootSessionId?.ToString("D"),
            ["sensor_config_id"] = collector.Config?.Id.ToString("D"),
            ["sensors"] = collector.Config?.Sensors,
            ["cycles"] = s.Cycles,
            ["samples"] = s.Samples,
            ["source_errors"] = s.SourceErrors,
            ["cycle_failures"] = s.CycleFailures,
            ["resumes"] = s.Resumes,
            ["clock_jumps"] = s.ClockJumps,
            ["intervals"] = new Dictionary<string, object?>
            {
                ["total"] = s.IntervalsTotal,
                ["on_time_pm10pct"] = s.IntervalsOnTime,
                ["min_ms"] = s.IntervalsTotal > 0 ? s.IntervalMinMs : null,
                ["max_ms"] = s.IntervalsTotal > 0 ? s.IntervalMaxMs : null,
            },
            ["quality_counts"] = s.PerSensor,
            ["upload"] = uploader is null ? null : new Dictionary<string, object?>
            {
                ["sent"] = uploader.Sent,
                ["retries"] = uploader.Retries,
                ["quarantined"] = uploader.Quarantined,
                ["pending"] = spool?.Pending().Count,
            },
        };
        StateStore.WriteAtomic(path, JsonSerializer.Serialize(report, Pretty));
    }

    /// <summary>
    /// 일반 사용자 셸(S1-06)이 읽는 공개 상태. 토큰·machine_id·센서값·호스트 정보를 넣지 않는다.
    /// 상태 디렉터리(서비스 전용 ACL)와 분리해 %ProgramData%\PcTelemetry\status에 둔다. 실패해도 수집은 계속한다.
    /// </summary>
    internal static void WriteStatus(string? path, bool collecting, Uploader? uploader, Spool? spool)
    {
        if (path is null) return;
        var status = new Dictionary<string, object?>
        {
            ["schema"] = "pc-telemetry/agent-status/1",
            ["updated_at_utc"] = Utc.Format(DateTime.UtcNow),
            ["agent_version"] = typeof(Program).Assembly.GetName().Version?.ToString(3),
            ["collecting"] = collecting,
            ["upload"] = new Dictionary<string, object?>
            {
                ["configured"] = uploader is not null,
                ["state"] = uploader?.State ?? (spool is null ? "not_configured" : "idle"),
                ["pending_batches"] = spool?.Pending().Count,
                ["last_success_at_utc"] = uploader?.LastSuccessUtc is { } t ? Utc.Format(t) : null,
            },
        };
        try
        {
            StateStore.WriteAtomic(path, JsonSerializer.Serialize(status, Pretty));
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
        }
    }

    private static int Discover(string? outPath)
    {
        var dir = Path.Combine(Path.GetTempPath(), "pctf-discover-" + Guid.NewGuid().ToString("N"));
        try
        {
            var store = new StateStore(dir);
            var sink = new JsonlEventSink(store.PathOf("events.jsonl"));
            using var source = new LhmSource();
            var collector = new SensorCollector(source, new WindowsClock(), new RegistryBootId(), sink, store, null);
            collector.Start();
            Sample? sample = null;
            for (var i = 0; i < 5 && sample is null; i++)
            {
                sample = collector.Tick();
                if (sample is null) Thread.Sleep(1000);
            }
            var report = new Dictionary<string, object?>
            {
                ["task"] = "S1-02b",
                ["mode"] = "discover",
                ["executed_at_utc"] = Utc.Format(DateTime.UtcNow),
                ["host"] = HostInfo.Describe(),
                ["sensors"] = collector.Config?.Sensors,
                ["bindings"] = collector.Bindings,
                ["sample"] = sample?.Readings.Select(r => new { r.SensorId, r.Unit, quality = r.Quality.ToContract(), r.Value, r.Reason }),
            };
            var json = JsonSerializer.Serialize(report, Pretty);
            if (outPath is null) Console.WriteLine(json);
            else
            {
                StateStore.WriteAtomic(Path.GetFullPath(outPath), json);
                Console.WriteLine($"wrote {outPath}");
            }
            return 0;
        }
        finally
        {
            try { Directory.Delete(dir, recursive: true); } catch (IOException) { }
        }
    }

    private static Dictionary<string, string> Parse(string[] args)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var i = 0; i < args.Length; i++)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal) || i + 1 >= args.Length)
                throw new ArgumentException($"bad option: {args[i]}");
            result[args[i][2..]] = args[++i];
        }
        return result;
    }
}

internal sealed class AgentService(AgentOptions options) : BackgroundService
{
    protected override Task ExecuteAsync(CancellationToken stoppingToken) =>
        Task.Run(() => Program.RunAsync(options with { Seconds = null }, stoppingToken, console: false), stoppingToken);
}
