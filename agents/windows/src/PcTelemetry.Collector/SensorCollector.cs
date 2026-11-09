namespace PcTelemetry.Collector;

public sealed class CollectorOptions
{
    public int IntervalMs { get; init; } = 1000;

    /// <summary>시작 직후 버리는 읽기 횟수. LHM 일부 값(부하 등)은 두 번째 갱신부터 유효하다.</summary>
    public int WarmupReads { get; init; } = 2;

    /// <summary>드라이버 접근 상태가 이 횟수만큼 연속으로 바뀌어야 새 구성을 발급한다(순간 결측과 구분).</summary>
    public int DriverDebounceTicks { get; init; } = 5;

    /// <summary>절전 판정: 절전 포함 시계와 제외 시계의 차이가 이 값 이상이면 재개로 본다.</summary>
    public long SuspendThresholdMs { get; init; } = 1000;

    /// <summary>계약 4절: 벽시계와 단조 시간 차이가 2초 이상이면 clock_jump.</summary>
    public long ClockJumpThresholdMs { get; init; } = 2000;

    /// <summary>센서별 품질 변경 이벤트 최소 간격. 깜빡이는 센서가 이벤트를 쏟아내지 않게 한다.</summary>
    public TimeSpan QualityEventCooldown { get; init; } = TimeSpan.FromSeconds(60);
}

/// <summary>
/// 1초 수집 루프. 한 센서·한 하드웨어의 실패가 전체 수집을 멈추지 않는다.
/// 샘플 순번·부팅 세션·관측 UTC·단조 시간을 기록하고, 재부팅·절전/재개·센서 변경을 이벤트로 남긴다.
/// </summary>
public sealed class SensorCollector
{
    private const string CurrentConfigFile = "current-config.json";

    private readonly ISensorSource _source;
    private readonly IClock _clock;
    private readonly IBootIdProvider _bootIds;
    private readonly IEventSink _events;
    private readonly Batcher? _batcher;
    private readonly StateStore _store;
    private readonly CollectorOptions _options;
    private readonly ComponentRegistry _components;
    private readonly SensorConfigStore _configs;
    private readonly Dictionary<string, QualityTrack> _quality = new(StringComparer.Ordinal);

    private BootSession? _boot;
    private SensorConfig? _config;
    private IReadOnlyList<SensorBinding> _bindings = [];
    private string? _structure;
    private bool _driver;
    private int _driverFlips;
    private int _warmupLeft;
    private DateTime? _prevWall;
    private long _prevUnbiased, _prevInterrupt;
    private long? _prevSampleMono;

    public SensorCollector(ISensorSource source, IClock clock, IBootIdProvider bootIds, IEventSink events,
        StateStore store, Batcher? batcher, CollectorOptions? options = null)
    {
        _source = source;
        _clock = clock;
        _bootIds = bootIds;
        _events = events;
        _store = store;
        _batcher = batcher;
        _options = options ?? new CollectorOptions();
        _components = new ComponentRegistry(store);
        _configs = new SensorConfigStore(store);
        _warmupLeft = _options.WarmupReads;
    }

    public CollectorStats Stats { get; } = new();
    public Guid? BootSessionId => _boot?.Id;
    public SensorConfig? Config => _config;
    public IReadOnlyList<SensorBinding> Bindings => _bindings;

    /// <exception cref="BootIdentityUnavailableException">부팅 식별 불가. 호출자가 유보하고 재시도한다.</exception>
    public void Start()
    {
        _boot = BootSession.Open(_store, _bootIds);
        Emit("agent_started", new Dictionary<string, object?>
        {
            ["source"] = _source.SourceTag,
            ["boot_id"] = _boot.BootId,
            ["next_seq"] = _boot.NextSeq,
        });
        if (_boot.IsNewBoot)
        {
            // 이전 부팅 기록이 있으면 재부팅, 없으면 이 상태 디렉터리의 첫 실행이다.
            Emit("boot_session_started", new Dictionary<string, object?>
            {
                ["reason"] = _boot.PreviousBootId is null ? "first_run" : "reboot",
                ["boot_id"] = _boot.BootId,
                ["previous_boot_id"] = _boot.PreviousBootId,
            });
        }
    }

    /// <summary>한 주기를 수행한다. 워밍업이거나 구성을 아직 정하지 못하면 null(순번 소비 없음).</summary>
    public Sample? Tick()
    {
        if (_boot is null) throw new InvalidOperationException("Start() first");
        Stats.Cycles++;

        var wall = _clock.UtcNow;
        var mono = _clock.UnbiasedMs;
        var interrupt = _clock.InterruptMs;
        DetectTimeGaps(wall, mono, interrupt);

        IReadOnlyList<RawHardware> raw;
        string? readError = null;
        try
        {
            raw = _source.Read();
        }
        catch (Exception ex)
        {
            raw = [];
            readError = ex.GetType().Name;
            Stats.SourceErrors++;
        }

        if (_warmupLeft > 0)
        {
            _warmupLeft--;
            return null;
        }

        if (readError is null)
        {
            var driverNow = SensorCatalog.DriverAvailable(raw);
            var structure = Structure(raw);
            var rediscover = _config is null || structure != _structure;
            // CPU 갱신 자체가 실패한 주기는 드라이버 상태 판단에 쓰지 않는다(read_failed와 driver_unavailable 구분).
            var cpuReadable = raw.Any(h => h.Type == "Cpu" && h.UpdateError is null);
            if (!cpuReadable)
            {
                driverNow = _driver;
            }
            if (_config is not null && driverNow != _driver)
            {
                if (++_driverFlips >= _options.DriverDebounceTicks) rediscover = true;
            }
            else
            {
                _driverFlips = 0;
            }
            if (rediscover) Discover(raw, driverNow, structure);
        }
        else if (_config is null)
        {
            Emit("source_read_failed", new Dictionary<string, object?> { ["error"] = readError, ["phase"] = "discovery" });
            return null;
        }

        var driverReadable = readError is null && SensorCatalog.DriverAvailable(raw);
        var readings = new List<Reading>(_bindings.Count);
        foreach (var b in _bindings) readings.Add(Read(b, raw, readError, driverReadable));

        var sample = new Sample(_boot.Take(), wall, mono, readings);
        Track(sample);
        _batcher?.Add(_boot.Id, _config!.Id, sample);
        return sample;
    }

    public async Task RunAsync(CancellationToken token)
    {
        if (_boot is null) Start();
        using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(_options.IntervalMs));
        try
        {
            do
            {
                try
                {
                    Tick();
                }
                catch (Exception ex) when (ex is not OperationCanceledException)
                {
                    // 주기 하나의 예기치 못한 실패도 루프를 멈추지 않는다.
                    Stats.CycleFailures++;
                    Emit("collector_cycle_failed", new Dictionary<string, object?> { ["error"] = ex.GetType().Name, ["message"] = ex.Message });
                }
            }
            while (await timer.WaitForNextTickAsync(token).ConfigureAwait(false));
        }
        catch (OperationCanceledException)
        {
        }
        finally
        {
            Stop();
        }
    }

    public void Stop()
    {
        _batcher?.Flush();
        Emit("agent_stopped", new Dictionary<string, object?>
        {
            ["cycles"] = Stats.Cycles,
            ["samples"] = Stats.Samples,
            ["next_seq"] = _boot?.NextSeq,
        });
    }

    private void Discover(IReadOnlyList<RawHardware> raw, bool driverNow, string structure)
    {
        var discovery = SensorCatalog.Discover(raw, driverNow, _components, _source.SourceTag);
        var (config, created) = _configs.GetOrCreate(discovery.Definitions);
        var previousId = _config?.Id ?? _store.Load<CurrentConfig>(CurrentConfigFile)?.ConfigId;

        if (previousId != config.Id)
        {
            // 같은 배치 안에서 구성을 바꾸지 않는다: 이전 구성의 샘플을 먼저 동결한다.
            _batcher?.Flush();
            var previous = _configs.All.FirstOrDefault(c => c.Id == previousId);
            var before = previous?.Sensors.ToDictionary(s => s.SensorId) ?? [];
            var after = config.Sensors.ToDictionary(s => s.SensorId);
            Emit(previousId is null ? "sensor_config_selected" : "sensor_config_changed", new Dictionary<string, object?>
            {
                ["from"] = previousId?.ToString("D"),
                ["to"] = config.Id.ToString("D"),
                ["created"] = created,
                ["driver_available"] = driverNow,
                ["added"] = after.Keys.Except(before.Keys).Order(StringComparer.Ordinal).ToList(),
                ["removed"] = before.Keys.Except(after.Keys).Order(StringComparer.Ordinal).ToList(),
                ["support_changed"] = after.Values
                    .Where(s => before.TryGetValue(s.SensorId, out var o) && o.Support != s.Support)
                    .Select(s => $"{s.SensorId}:{before[s.SensorId].Support}->{s.Support}").ToList(),
                ["supported"] = config.Sensors.Count(s => s.Support == "supported"),
                ["unsupported"] = config.Sensors.Count(s => s.Support == "unsupported"),
            });
            _store.Save(CurrentConfigFile, new CurrentConfig(config.Id));
        }

        _config = config;
        _bindings = discovery.Bindings;
        _structure = structure;
        _driver = driverNow;
        _driverFlips = 0;
    }

    private static Reading Read(SensorBinding b, IReadOnlyList<RawHardware> raw, string? readError, bool driverReadable)
    {
        if (b.UnsupportedReason is not null) return Reading.Null(b.SensorId, b.Unit, Quality.Unsupported, b.UnsupportedReason);
        if (readError is not null) return Reading.Null(b.SensorId, b.Unit, Quality.Error, "read_failed");

        var hw = raw.FirstOrDefault(h => h.Identifier == b.HardwareIdentifier);
        if (hw is null) return Reading.Null(b.SensorId, b.Unit, Quality.Missing, "sensor_absent");
        if (hw.UpdateError is not null) return Reading.Null(b.SensorId, b.Unit, Quality.Error, "read_failed");
        // 드라이버 접근이 끊기면 CPU 전력이 0으로 나온다. 0을 보내지 않고 오류로 표시한다(디바운스 후 새 구성).
        if (b.RequiresDriver && !driverReadable) return Reading.Null(b.SensorId, b.Unit, Quality.Error, "driver_unavailable");

        var present = hw.Sensors.Where(s => b.SourceSensorIds.Contains(SensorCatalog.SourceKey(s))).ToList();
        if (present.Count == 0) return Reading.Null(b.SensorId, b.Unit, Quality.Missing, "sensor_absent");
        var finite = present.Where(s => s.Value is { } v && float.IsFinite(v)).Select(s => (double)s.Value!.Value).ToList();
        if (finite.Count == 0) return Reading.Null(b.SensorId, b.Unit, Quality.Missing, "no_value");

        var value = b.Aggregation == Aggregation.Max ? finite.Max() : finite[0];
        var rounded = (decimal)Math.Round(value, 3);
        if (Math.Abs(rounded) > BatchFactory.MaxAbsValue) return Reading.Null(b.SensorId, b.Unit, Quality.Error, "out_of_range");
        return Reading.Ok(b.SensorId, b.Unit, rounded == 0 ? 0m : rounded);
    }

    private void DetectTimeGaps(DateTime wall, long mono, long interrupt)
    {
        if (_prevWall is { } prevWall)
        {
            var wallMs = (long)(wall - prevWall).TotalMilliseconds;
            var monoMs = mono - _prevUnbiased;
            var interruptMs = interrupt - _prevInterrupt;
            var suspendedMs = interruptMs - monoMs;   // 절전 포함 시계 - 절전 제외 시계
            var jumpMs = wallMs - interruptMs;          // 절전과 무관한 벽시계 변화

            if (suspendedMs >= _options.SuspendThresholdMs)
            {
                // 절전 구간을 가로지른 샘플을 같은 배치에 묶지 않는다.
                _batcher?.Flush();
                Stats.Resumes++;
                Emit("suspend_resume", new Dictionary<string, object?>
                {
                    ["suspended_ms"] = suspendedMs,
                    ["wall_elapsed_ms"] = wallMs,
                    ["monotonic_elapsed_ms"] = monoMs,
                    ["suspended_at_estimate"] = Utc.Format(prevWall.AddMilliseconds(monoMs)),
                    ["last_seq_before"] = _boot!.NextSeq - 1,
                });
                _prevSampleMono = null;
            }
            if (Math.Abs(jumpMs) >= _options.ClockJumpThresholdMs)
            {
                Stats.ClockJumps++;
                Emit("clock_jump", new Dictionary<string, object?>
                {
                    ["jump_ms"] = jumpMs,
                    ["wall_elapsed_ms"] = wallMs,
                    ["interrupt_elapsed_ms"] = interruptMs,
                });
            }
        }
        _prevWall = wall;
        _prevUnbiased = mono;
        _prevInterrupt = interrupt;
    }

    private void Track(Sample sample)
    {
        Stats.Samples++;
        if (_prevSampleMono is { } prev) Stats.AddInterval(sample.MonotonicMs - prev, _options.IntervalMs);
        _prevSampleMono = sample.MonotonicMs;

        foreach (var r in sample.Readings)
        {
            Stats.Add(r);
            if (!_quality.TryGetValue(r.SensorId, out var track))
            {
                _quality[r.SensorId] = new QualityTrack(r.Quality, r.Reason, DateTime.MinValue, 0);
                continue;
            }
            if (track.Quality == r.Quality && track.Reason == r.Reason) continue;
            if (sample.ObservedAtUtc - track.LastEvent < _options.QualityEventCooldown)
            {
                _quality[r.SensorId] = track with { Quality = r.Quality, Reason = r.Reason, Suppressed = track.Suppressed + 1 };
                continue;
            }
            Emit("sensor_quality_changed", new Dictionary<string, object?>
            {
                ["sensor_id"] = r.SensorId,
                ["from"] = $"{track.Quality.ToContract()}{(track.Reason is null ? "" : ":" + track.Reason)}",
                ["to"] = $"{r.Quality.ToContract()}{(r.Reason is null ? "" : ":" + r.Reason)}",
                ["sample_seq"] = sample.Seq,
                ["suppressed_changes"] = track.Suppressed,
            });
            _quality[r.SensorId] = new QualityTrack(r.Quality, r.Reason, sample.ObservedAtUtc, 0);
        }
    }

    private void Emit(string type, IReadOnlyDictionary<string, object?> details) =>
        _events.Emit(new CollectionEvent(Guid.NewGuid(), type, _clock.UtcNow, _clock.UnbiasedMs, _boot?.Id, details));

    private static string Structure(IReadOnlyList<RawHardware> raw) =>
        string.Join("\n", raw.Select(h => h.Type + "|" + h.Identifier + "|" + h.Name + "|"
            + string.Join(",", h.Sensors.Select(SensorCatalog.SourceKey))));

    private sealed record CurrentConfig(Guid ConfigId);

    private sealed record QualityTrack(Quality Quality, string? Reason, DateTime LastEvent, int Suppressed);
}

/// <summary>검증 보고서용 통계. 지원 센서 정상 구간 수집 성공률과 샘플 간격을 계산한다.</summary>
public sealed class CollectorStats
{
    public long Cycles { get; set; }
    public long Samples { get; set; }
    public long SourceErrors { get; set; }
    public long CycleFailures { get; set; }
    public long Resumes { get; set; }
    public long ClockJumps { get; set; }
    public long IntervalsTotal { get; private set; }
    public long IntervalsOnTime { get; private set; }
    public long IntervalMinMs { get; private set; } = long.MaxValue;
    public long IntervalMaxMs { get; private set; } = long.MinValue;
    public SortedDictionary<string, Dictionary<string, long>> PerSensor { get; } = new(StringComparer.Ordinal);

    /// <summary>정시: 설정 간격 ±10%. 계약상 누락·지연은 허용되므로 실패가 아니라 측정값이다.</summary>
    public void AddInterval(long ms, int intervalMs)
    {
        IntervalsTotal++;
        if (Math.Abs(ms - intervalMs) <= intervalMs / 10) IntervalsOnTime++;
        IntervalMinMs = Math.Min(IntervalMinMs, ms);
        IntervalMaxMs = Math.Max(IntervalMaxMs, ms);
    }

    public void Add(Reading r)
    {
        if (!PerSensor.TryGetValue(r.SensorId, out var counts))
        {
            counts = new Dictionary<string, long>(StringComparer.Ordinal);
            PerSensor[r.SensorId] = counts;
        }
        var key = r.Quality.ToContract() + (r.Reason is null ? "" : ":" + r.Reason);
        counts[key] = counts.GetValueOrDefault(key) + 1;
    }
}

public interface IBatchSink
{
    void Accept(FrozenBatch batch);
}

/// <summary>
/// 3초(기본 3샘플) 단위로 샘플을 모아 동결한다. 부팅 세션·구성이 바뀌거나 절전 재개 시 즉시 끊는다.
/// </summary>
public sealed class Batcher(Guid machineId, IBatchSink sink, int samplesPerBatch = 3)
{
    private readonly List<Sample> _pending = [];
    private (Guid Boot, Guid Config)? _key;

    public long Frozen { get; private set; }

    public void Add(Guid bootSessionId, Guid configId, Sample sample)
    {
        if (_key is { } k && (k.Boot != bootSessionId || k.Config != configId)) Flush();
        _key = (bootSessionId, configId);
        _pending.Add(sample);
        if (_pending.Count >= samplesPerBatch) Flush();
    }

    public void Flush()
    {
        if (_pending.Count == 0 || _key is not { } k) return;
        foreach (var batch in BatchFactory.Freeze(machineId, k.Boot, k.Config, _pending))
        {
            sink.Accept(batch);
            Frozen++;
        }
        _pending.Clear();
    }
}
