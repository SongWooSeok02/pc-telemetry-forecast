namespace PcTelemetry.Collector;

/// <summary>센서 원천(LHM 등)이 한 번 읽은 하드웨어. 원천 구현과 수집 로직을 분리하기 위한 중립 모델.</summary>
public sealed record RawHardware(
    string Identifier,
    string Type,
    string Name,
    IReadOnlyList<RawSensor> Sensors,
    string? UpdateError = null);

public sealed record RawSensor(string Identifier, string Type, string Name, float? Value);

public interface ISensorSource
{
    /// <summary>모든 하드웨어를 갱신하고 읽는다. 하드웨어별 갱신 실패는 예외 대신 UpdateError로 돌려준다.</summary>
    IReadOnlyList<RawHardware> Read();

    /// <summary>원천 출처 표기(예: lhm-0.9.6). 센서 메타데이터 source에 들어간다.</summary>
    string SourceTag { get; }
}

public interface IClock
{
    DateTime UtcNow { get; }

    /// <summary>QueryUnbiasedInterruptTime / 10000 내림. 절전·최대절전 시간 제외(계약 monotonic_ms).</summary>
    long UnbiasedMs { get; }

    /// <summary>QueryInterruptTime / 10000 내림. 절전 시간 포함. 절전 길이와 벽시계 급변을 구분하는 데만 쓴다.</summary>
    long InterruptMs { get; }
}

public interface IBootIdProvider
{
    /// <summary>OS 부팅마다 바뀌는 식별자. 확인할 수 없으면 null(계약: 수집 유보).</summary>
    string? GetBootId();
}

public enum Quality { Ok, Unsupported, Missing, Error }

public static class QualityText
{
    public static string ToContract(this Quality q) => q switch
    {
        Quality.Ok => "ok",
        Quality.Unsupported => "unsupported",
        Quality.Missing => "missing",
        Quality.Error => "error",
        _ => throw new ArgumentOutOfRangeException(nameof(q)),
    };
}

public sealed record Reading(string SensorId, string Unit, Quality Quality, decimal? Value, string? Reason)
{
    public static Reading Ok(string id, string unit, decimal value) => new(id, unit, Quality.Ok, value, null);
    public static Reading Null(string id, string unit, Quality quality, string reason) => new(id, unit, quality, null, reason);
}

public sealed record Sample(long Seq, DateTime ObservedAtUtc, long MonotonicMs, IReadOnlyList<Reading> Readings);

/// <summary>계약 센서 등록 메타데이터. 서버로 보내는 필드만 갖는다.</summary>
public sealed record SensorDefinition(
    string SensorId,
    string ComponentId,
    string Kind,
    string Unit,
    string Source,
    string Scope,
    string Support);

/// <summary>sensor_id를 원천 센서에 연결하는 로컬 매핑. 서버로 보내지 않는다.</summary>
public sealed record SensorBinding(
    string SensorId,
    string Unit,
    string HardwareIdentifier,
    IReadOnlyList<string> SourceSensorIds,
    Aggregation Aggregation,
    bool RequiresDriver,
    string? UnsupportedReason);

public enum Aggregation { Single, Max }

public sealed record SensorConfig(Guid Id, IReadOnlyList<SensorDefinition> Sensors);

public sealed record CollectionEvent(
    Guid EventId,
    string Type,
    DateTime ObservedAtUtc,
    long MonotonicMs,
    Guid? BootSessionId,
    IReadOnlyDictionary<string, object?> Details);

public interface IEventSink
{
    void Emit(CollectionEvent e);
}

public static class Utc
{
    /// <summary>계약 UTC 형식 YYYY-MM-DDTHH:mm:ss.ffffffZ(1µs 표현, 정확도 아님).</summary>
    public static string Format(DateTime t) =>
        t.ToUniversalTime().ToString("yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'", System.Globalization.CultureInfo.InvariantCulture);
}
