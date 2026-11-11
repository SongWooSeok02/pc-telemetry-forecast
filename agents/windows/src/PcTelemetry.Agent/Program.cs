// S1-02b 수집 에이전트.
//
//   PcTelemetry.Agent discover [--out file.json]
//       센서를 탐색해 계약 매핑(정의·원천 연결)을 출력한다. 임시 상태 디렉터리를 쓴다.
//   PcTelemetry.Agent run [--state-dir DIR] [--seconds N]
//                         [--server URL --machine-id UUID --token-file FILE] [--status-file PATH|none]
//                         [--buffer-max-age-seconds N] [--buffer-max-bytes N]
//       콘솔에서 1초 수집. --machine-id가 있으면 샘플을 SQLite 버퍼(state\buffer.sqlite3)에 즉시 저장하고
//       3초 배치로 동결한다. --server가 있으면 전송한다. 버퍼 상한 기본값은 24시간/500MB(S2-01, README 5절).
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
    string? StatusFile,
    BufferOptions Buffer);

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
                        "PcTelemetry", "status", "agent-status.json"),
                new BufferOptions
                {
                    MaxAge = o.TryGetValue("buffer-max-age-seconds", out var age)
                        ? TimeSpan.FromSeconds(int.Parse(age)) : TimeSpan.FromHours(24),
                    MaxBytes = o.TryGetValue("buffer-max-bytes", out var bytes) ? long.Parse(bytes) : 500L * 1024 * 1024,
                });
            if (options.Buffer.MaxAge <= TimeSpan.Zero || options.Buffer.MaxBytes <= 0)
                throw new ArgumentException("buffer limits must be positive");
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
        // 재시작 복원: 이전 프로세스의 미동결 샘플을 동결하고 S1-02b 파일 스풀을 가져온다.
        using var buffer = options.MachineId is null ? null
            : SqliteBuffer.Open(store.PathOf(SqliteBuffer.FileName), options.MachineId.Value, events, options.Buffer);
        buffer?.ImportLegacySpool(store.Directory);
        using var source = new LhmSource();
        var collector = new SensorCollector(source, new WindowsClock(), new RegistryBootId(), events, store, buffer,
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
            : new Uploader(options.Server, options.TokenFile!, buffer!, events);
        using var uploadStop = new CancellationTokenSource();

        var collect = Task.Run(() => collector.RunAsync(stop.Token), CancellationToken.None);
        var upload = uploader is null ? Task.CompletedTask : Task.Run(() => uploader.RunAsync(uploadStop.Token), CancellationToken.None);
        var statsPath = store.PathOf("stats.json");
        var tick = 0;
        WriteStatus(options.StatusFile, collecting: true, uploader, buffer);
        while (!collect.IsCompleted)
        {
            await Task.WhenAny(collect, Task.Delay(TimeSpan.FromSeconds(10), CancellationToken.None));
            WriteStatus(options.StatusFile, collecting: !collect.IsCompleted, uploader, buffer);
            if (++tick % 6 == 0 || collect.IsCompleted)
                WriteStats(statsPath, options, started, source, collector, uploader, buffer);
        }
        await collect;
        buffer?.Prune();
        await uploadStop.CancelAsync();
        await upload;
        if (uploader is not null) await uploader.DrainAsync(TimeSpan.FromSeconds(20));
        WriteStats(statsPath, options, started, source, collector, uploader, buffer);
        WriteStatus(options.StatusFile, collecting: false, uploader, buffer);
        if (console) Console.WriteLine($"stats: {statsPath}");
        return 0;
    }

    private static void WriteStats(string path, AgentOptions options, DateTime started, LhmSource source,
        SensorCollector collector, Uploader? uploader, SqliteBuffer? buffer)
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
                ["ack_mismatches"] = uploader.AckMismatches,
            },
            ["buffer"] = buffer?.Stats() is { } b ? new Dictionary<string, object?>
            {
                ["pending_batches"] = b.PendingBatches,
                ["quarantined_batches"] = b.QuarantinedBatches,
                ["open_samples"] = b.OpenSamples,
                ["unsent_bytes"] = b.UnsentBytes,
                ["oldest_unsent_at_utc"] = b.OldestUnsentUtc is { } t ? Utc.Format(t) : null,
                ["sent_ledger_batches"] = b.SentLedgerBatches,
                ["dropped_samples_total"] = b.DroppedSamplesTotal,
                ["file_bytes"] = b.FileBytes,
                ["max_age_hours"] = buffer.Options.MaxAge.TotalHours,
                ["max_bytes"] = buffer.Options.MaxBytes,
            } : null,
        };
        StateStore.WriteAtomic(path, JsonSerializer.Serialize(report, Pretty));
    }

    /// <summary>
    /// 일반 사용자 셸(S1-06)이 읽는 공개 상태. 토큰·machine_id·센서값·호스트 정보를 넣지 않는다.
    /// 상태 디렉터리(서비스 전용 ACL)와 분리해 %ProgramData%\PcTelemetry\status에 둔다. 실패해도 수집은 계속한다.
    /// S2-01: 버퍼 상한으로 지운 누락 범위는 관측 시각·샘플 수만 싣는다(부팅 세션 ID·순번은 state\buffer.sqlite3 gaps 표).
    /// </summary>
    internal static void WriteStatus(string? path, bool collecting, Uploader? uploader, SqliteBuffer? buffer)
    {
        if (path is null) return;
        BufferStats? b = null;
        try
        {
            b = buffer?.Stats();
        }
        catch (Microsoft.Data.Sqlite.SqliteException)
        {
        }
        var status = new Dictionary<string, object?>
        {
            ["schema"] = "pc-telemetry/agent-status/1",
            ["updated_at_utc"] = Utc.Format(DateTime.UtcNow),
            ["agent_version"] = typeof(Program).Assembly.GetName().Version?.ToString(3),
            ["collecting"] = collecting,
            ["upload"] = new Dictionary<string, object?>
            {
                ["configured"] = uploader is not null,
                ["state"] = uploader?.State ?? (buffer is null ? "not_configured" : "idle"),
                ["pending_batches"] = b?.PendingBatches,
                ["last_success_at_utc"] = uploader?.LastSuccessUtc is { } t ? Utc.Format(t) : null,
                ["quarantined_batches"] = b?.QuarantinedBatches,
                ["oldest_pending_at_utc"] = b?.OldestUnsentUtc is { } o ? Utc.Format(o) : null,
                ["dropped_samples_total"] = b?.DroppedSamplesTotal,
                ["last_gap"] = b?.LastGap is { } g ? new Dictionary<string, object?>
                {
                    ["reason"] = g.Reason,
                    ["from_observed_at_utc"] = Utc.Format(g.FirstObservedUtc),
                    ["to_observed_at_utc"] = Utc.Format(g.LastObservedUtc),
                    ["samples"] = g.Samples,
                    ["recorded_at_utc"] = Utc.Format(g.RecordedAtUtc),
                } : null,
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
