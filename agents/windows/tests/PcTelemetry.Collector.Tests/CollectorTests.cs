using System.Text.Json;
using PcTelemetry.Collector;
using Xunit;

namespace PcTelemetry.Collector.Tests;

public class CollectorTests : IDisposable
{
    private static readonly Guid Machine = Guid.Parse("11111111-1111-4111-8111-111111111111");
    private readonly TempDir _dir = new();
    private readonly FakeSource _source = new();
    private readonly FakeClock _clock = new();
    private readonly FakeBoot _boot = new();
    private readonly ListSink _sink = new();

    public void Dispose() => _dir.Dispose();

    private SensorCollector NewCollector(CollectorOptions? options = null) =>
        new(_source, _clock, _boot, _sink, new StateStore(_dir.Path), new Batcher(Machine, _sink),
            options ?? new CollectorOptions { QualityEventCooldown = TimeSpan.Zero });

    private List<Sample> Run(SensorCollector c, int ticks)
    {
        var samples = new List<Sample>();
        for (var i = 0; i < ticks; i++)
        {
            if (c.Tick() is { } s) samples.Add(s);
            _clock.Tick();
        }
        return samples;
    }

    private static Reading R(Sample s, string id) => s.Readings.Single(r => r.SensorId == id);

    [Fact]
    public void Maps_adopted_sensors_to_contract_ids_units_scopes()
    {
        var c = NewCollector();
        c.Start();
        Run(c, 3);
        var defs = c.Config!.Sensors.ToDictionary(d => d.SensorId);

        Assert.All(defs.Keys, id => Assert.Matches(SensorCatalog.SensorIdPattern(), id));
        Assert.Equal(defs.Count, defs.Keys.Distinct().Count());
        Assert.Equal(("temperature", "Cel", "cpu_package", "supported"),
            (defs["cpu0/package/temp"].Kind, defs["cpu0/package/temp"].Unit, defs["cpu0/package/temp"].Scope, defs["cpu0/package/temp"].Support));
        Assert.Equal(("power", "W", "gpu_board"), (defs["gpu0/board/power"].Kind, defs["gpu0/board/power"].Unit, defs["gpu0/board/power"].Scope));
        Assert.Equal("cpu_cores", defs["cpu0/cores/power"].Scope);
        Assert.Equal("MHz", defs["cpu0/core-max/clock"].Unit);
        Assert.Equal("rpm", defs["cpu0/fan"].Unit);
        // 노트북: 팬은 노출되지 않음 → 등록하되 unsupported.
        Assert.Equal("unsupported", defs["cpu0/fan"].Support);
        Assert.Equal("unsupported", defs["gpu0/fan"].Support);
        // 계약 밖·제외 센서: iGPU(후보), 항상 0인 CPU Memory/Platform, Distance to TjMax, 중복 식별자 GPU Memory 부하.
        Assert.DoesNotContain(c.Bindings, b => b.HardwareIdentifier.StartsWith("/gpu-intel", StringComparison.Ordinal));
        Assert.DoesNotContain(c.Bindings.SelectMany(b => b.SourceSensorIds), k => k.Contains("CPU Memory") || k.Contains("TjMax") || k.Contains("Load#GPU Memory"));
        Assert.Equal(15, defs.Count);
    }

    [Fact]
    public void Collects_every_second_with_increasing_seq_and_frozen_three_sample_batches()
    {
        var c = NewCollector();
        c.Start();
        var samples = Run(c, 2 + 9);   // 워밍업 2회는 순번을 쓰지 않는다

        Assert.Equal(Enumerable.Range(0, 9).Select(i => (long)i), samples.Select(s => s.Seq));
        Assert.All(samples.Zip(samples.Skip(1)), p =>
        {
            Assert.Equal(1000, p.Second.MonotonicMs - p.First.MonotonicMs);
            Assert.True(p.Second.ObservedAtUtc > p.First.ObservedAtUtc);
        });
        Assert.Equal(3, _sink.Batches.Count);
        Assert.All(_sink.Batches, b => Assert.Equal(3, b.SampleCount));

        var first = R(samples[0], "cpu0/package/temp");
        Assert.Equal((Quality.Ok, 71m), (first.Quality, first.Value));
        Assert.Equal(4558.453m, R(samples[0], "cpu0/core-max/clock").Value); // 코어 클럭 최대값
        Assert.Equal(27m, R(samples[0], "gpu0/core/load").Value);            // GPU Bus와 같은 식별자여도 GPU Core 부하

        using var body = JsonDocument.Parse(_sink.Batches[0].Body);
        foreach (var reading in body.RootElement.GetProperty("samples")[0].GetProperty("readings").EnumerateArray())
        {
            if (reading.GetProperty("quality").GetString() != "ok")
                Assert.Equal(JsonValueKind.Null, reading.GetProperty("value").ValueKind);
        }
        Assert.Equal(1.0, c.Stats.IntervalsOnTime / (double)c.Stats.IntervalsTotal);
    }

    [Fact]
    public void Without_driver_cpu_sensors_are_unsupported_never_zero()
    {
        _source.Next = () => Synthetic.Laptop(driver: false);
        var c = NewCollector();
        c.Start();
        var s = Run(c, 3).Single();

        foreach (var id in new[] { "cpu0/package/temp", "cpu0/package/power", "cpu0/cores/power", "cpu0/core-max/clock" })
        {
            var r = R(s, id);
            Assert.Equal((Quality.Unsupported, (decimal?)null, "driver_unavailable"), (r.Quality, r.Value, r.Reason));
        }
        Assert.Equal(Quality.Ok, R(s, "cpu0/total/load").Quality);
        Assert.Equal(Quality.Ok, R(s, "gpu0/core/temp").Quality);
    }

    [Fact]
    public void One_failing_device_does_not_stop_other_sensors_and_recovers()
    {
        var c = NewCollector();
        c.Start();
        Run(c, 3);
        _source.Next = () => Synthetic.Laptop(gpuError: "NVApiException");
        var failed = Run(c, 1).Single();
        _source.Next = () => Synthetic.Laptop(gpuTemp: null);
        var missing = Run(c, 1).Single();
        _source.Next = () => Synthetic.Laptop();
        var recovered = Run(c, 1).Single();

        Assert.Equal((Quality.Error, "read_failed"), (R(failed, "gpu0/core/temp").Quality, R(failed, "gpu0/core/temp").Reason));
        Assert.Equal(Quality.Ok, R(failed, "cpu0/package/temp").Quality);
        Assert.Equal((Quality.Missing, "no_value"), (R(missing, "gpu0/core/temp").Quality, R(missing, "gpu0/core/temp").Reason));
        Assert.Equal(Quality.Ok, R(recovered, "gpu0/core/temp").Quality);
        Assert.Equal(Quality.Ok, R(missing, "gpu0/hotspot/temp").Quality);   // 같은 장치의 다른 센서는 영향 없음
        Assert.Equal(3, _sink.OfType("sensor_quality_changed").Count(e => (string)e.Details["sensor_id"]! == "gpu0/core/temp"));
        Assert.Single(c.Config!.Sensors, s => s.SensorId == "gpu0/core/temp");
    }

    [Fact]
    public void Whole_source_failure_keeps_loop_alive_with_error_readings()
    {
        var c = NewCollector();
        c.Start();
        Run(c, 3);
        _source.Throw = new InvalidOperationException("driver gone");
        var s = Run(c, 1).Single();
        _source.Throw = null;
        var after = Run(c, 1).Single();

        Assert.All(s.Readings.Where(r => r.Quality != Quality.Unsupported), r => Assert.Equal((Quality.Error, "read_failed"), (r.Quality, r.Reason)));
        Assert.Equal(s.Seq + 1, after.Seq);
        Assert.Equal(Quality.Ok, R(after, "cpu0/package/temp").Quality);
    }

    [Fact]
    public void Driver_loss_reports_error_then_issues_new_config_without_mixing_batches()
    {
        var c = NewCollector(new CollectorOptions { DriverDebounceTicks = 3, QualityEventCooldown = TimeSpan.Zero });
        c.Start();
        Run(c, 3);
        var before = c.Config!.Id;
        _source.Next = () => Synthetic.Laptop(driver: false);
        var lost = Run(c, 3);

        // 디바운스 동안: 0 대신 error. 3번째 주기에 새 구성(unsupported).
        Assert.All(lost.Take(2), s => Assert.Equal((Quality.Error, "driver_unavailable"),
            (R(s, "cpu0/package/power").Quality, R(s, "cpu0/package/power").Reason)));
        Assert.Equal(Quality.Unsupported, R(lost[2], "cpu0/package/power").Quality);
        Assert.NotEqual(before, c.Config!.Id);

        var changed = Assert.Single(_sink.OfType("sensor_config_changed"));
        Assert.Contains("cpu0/package/power:supported->unsupported", (List<string>)changed.Details["support_changed"]!);
        c.Stop();
        Assert.All(_sink.Batches, b =>
        {
            using var doc = JsonDocument.Parse(b.Body);
            Assert.Equal(b.SensorConfigId.ToString("D"), doc.RootElement.GetProperty("sensor_config_id").GetString());
        });
        Assert.Contains(_sink.Batches, b => b.SensorConfigId == before);
        Assert.Contains(_sink.Batches, b => b.SensorConfigId == c.Config.Id);
    }

    [Fact]
    public void Sensor_change_issues_new_config_and_absent_gpu_is_unsupported()
    {
        var c = NewCollector();
        c.Start();
        Run(c, 4);
        var first = c.Config!.Id;
        _source.Next = () => Synthetic.Laptop(gpu: false);
        var s = Run(c, 1).Single();

        Assert.NotEqual(first, c.Config!.Id);
        Assert.Equal((Quality.Unsupported, "component_absent"), (R(s, "gpu0/core/temp").Quality, R(s, "gpu0/core/temp").Reason));
        Assert.Single(_sink.OfType("sensor_config_changed"));
        // 구성 변경 직전 샘플은 이전 구성으로 먼저 동결된다.
        Assert.Equal(first, _sink.Batches[^1].SensorConfigId);
        Assert.True(File.Exists(Path.Combine(_dir.Path, "sensor-configs", $"{c.Config.Id:D}.json")));

        // GPU가 돌아오면 같은 정의 → 기존 구성 ID 재사용(불변 구성).
        _source.Next = () => Synthetic.Laptop();
        Run(c, 1);
        Assert.Equal(first, c.Config!.Id);
    }

    [Fact]
    public void Replaced_gpu_gets_new_sensor_ids()
    {
        var c = NewCollector();
        c.Start();
        Run(c, 3);
        _source.Next = () => Synthetic.Laptop(gpuName: "NVIDIA GeForce RTX 9999");
        var s = Run(c, 1).Single();
        Assert.Contains(s.Readings, r => r.SensorId == "gpu0-r2/core/temp");
        Assert.DoesNotContain(s.Readings, r => r.SensorId == "gpu0/core/temp");
    }

    [Theory]
    [InlineData(0)]
    [InlineData(-6000)]
    public void Suspend_resume_is_an_event_not_a_clock_jump_and_splits_batches(int wallAdjustmentMs)
    {
        var c = NewCollector();
        c.Start();
        Run(c, 3);                               // 순번 0
        _clock.Advance(3_601_000 + wallAdjustmentMs, 1_000, 3_601_000); // 1시간 절전: 벽시계·절전 포함 시계만 크게 증가
        var after = c.Tick()!;

        var e = Assert.Single(_sink.OfType("suspend_resume"));
        Assert.Equal(3_600_000L, (long)e.Details["suspended_ms"]!);
        Assert.Empty(_sink.OfType("clock_jump"));
        Assert.Equal(1, after.Seq);
        Assert.Equal(14_000, after.MonotonicMs);                    // 단조 시간은 절전을 제외(10s 시작 + 4s)
        Assert.Equal(0, Assert.Single(_sink.Batches).LastSeq);       // 재개 전 샘플은 따로 동결
        Assert.Equal(0, c.Stats.IntervalsTotal);                    // 절전 구간은 간격 통계에서 제외
    }

    [Fact]
    public void Wall_clock_jump_without_suspend_is_reported()
    {
        var c = NewCollector();
        c.Start();
        Run(c, 3);
        _clock.Advance(-5_000, 1_000, 1_000);
        c.Tick();
        Assert.Equal(-6_000L, (long)Assert.Single(_sink.OfType("clock_jump")).Details["jump_ms"]!);
        Assert.Empty(_sink.OfType("suspend_resume"));
    }

    [Fact]
    public void Service_restart_keeps_boot_session_and_never_reuses_seq()
    {
        var c1 = NewCollector();
        c1.Start();
        var s1 = Run(c1, 7);
        c1.Stop();
        var c2 = NewCollector();
        c2.Start();
        var s2 = Run(c2, 3);

        Assert.Equal(c1.BootSessionId, c2.BootSessionId);
        Assert.True(s2[0].Seq > s1[^1].Seq);
        Assert.Equal(c1.Config!.Id, c2.Config!.Id);
        Assert.Empty(_sink.OfType("sensor_config_changed"));
        Assert.Single(_sink.OfType("boot_session_started"));
    }

    [Fact]
    public void Reboot_starts_new_session_from_zero()
    {
        var c1 = NewCollector();
        c1.Start();
        Run(c1, 4);
        _boot.Id = "bootid:39";
        var c2 = NewCollector();
        c2.Start();
        var s = Run(c2, 3).Single();

        Assert.NotEqual(c1.BootSessionId, c2.BootSessionId);
        Assert.Equal(0, s.Seq);
        var started = _sink.OfType("boot_session_started").ToList();
        Assert.Equal(["first_run", "reboot"], started.Select(e => (string)e.Details["reason"]!));
    }

    [Fact]
    public void Unknown_boot_identity_holds_collection()
    {
        _boot.Id = null;
        Assert.Throws<BootIdentityUnavailableException>(() => NewCollector().Start());
    }

    [Fact]
    public async Task Run_loop_stops_on_cancel_and_flushes_pending_samples()
    {
        var c = NewCollector(new CollectorOptions { IntervalMs = 20, WarmupReads = 0 });
        using var cts = new CancellationTokenSource(TimeSpan.FromMilliseconds(150));
        await c.RunAsync(cts.Token).WaitAsync(TimeSpan.FromSeconds(5));
        Assert.True(c.Stats.Samples > 0);
        Assert.Equal(c.Stats.Samples, _sink.Batches.Sum(b => b.SampleCount));
        Assert.Single(_sink.OfType("agent_stopped"));
    }
}
