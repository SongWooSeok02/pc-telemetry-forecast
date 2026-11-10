using System.Globalization;

namespace PcTelemetry.Collector;

/// <summary>
/// 동결 배치의 파일 스풀. ID 발급과 본문 동결을 한 번의 원자적 파일 기록으로 영속화한다.
/// S2-01의 SQLite 버퍼가 들어오기 전까지의 최소 구현이며, 상한(24시간/500MB)만 적용한다.
/// </summary>
public sealed class Spool : IBatchSink
{
    private readonly IEventSink _events;
    private readonly Func<DateTime> _now;
    private readonly IClock? _clock;

    public Spool(string stateDirectory, IEventSink events, Func<DateTime>? now = null, IClock? clock = null)
    {
        PendingDir = Path.Combine(stateDirectory, "spool");
        QuarantineDir = Path.Combine(stateDirectory, "quarantine");
        Directory.CreateDirectory(PendingDir);
        Directory.CreateDirectory(QuarantineDir);
        _events = events;
        _clock = clock;
        _now = now ?? (() => DateTime.UtcNow);
    }

    public string PendingDir { get; }
    public string QuarantineDir { get; }
    public TimeSpan MaxAge { get; init; } = TimeSpan.FromHours(24);
    public long MaxBytes { get; init; } = 500L * 1024 * 1024;

    public void Accept(FrozenBatch batch)
    {
        // 파일명: 동결(enqueue) UTC + 첫 순번 + batch_id. 정렬 순서가 곧 전송 순서다.
        var stamp = _clock is null ? "" : $"_{batch.BootSessionId:N}_{_clock.InterruptMs:D16}";
        var name = $"{_now():yyyyMMdd'T'HHmmssfffffff}-{batch.FirstSeq:D16}-{batch.BatchId:D}{stamp}.json";
        StateStore.WriteAtomic(Path.Combine(PendingDir, name), batch.Body);
    }

    public IReadOnlyList<string> Pending() =>
        Directory.GetFiles(PendingDir, "*.json").Order(StringComparer.Ordinal).ToList();

    public static DateTime EnqueuedAt(string path) =>
        DateTime.ParseExact(Path.GetFileName(path)[..22], "yyyyMMdd'T'HHmmssfffffff", CultureInfo.InvariantCulture,
            DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);

    public void RecordTransmission(string path, Guid? currentBoot)
    {
        var name = Path.GetFileNameWithoutExtension(path).Split('_');
        var sent = _clock?.InterruptMs;
        long? residence = null;
        if (sent is { } now && currentBoot is { } boot && name.Length == 3 &&
            name[1] == boot.ToString("N") && long.TryParse(name[2], out var enqueued) && enqueued >= 0 && enqueued <= now)
            residence = now - enqueued;

        _events.Emit(new CollectionEvent(Guid.NewGuid(), "buffer_transmission", _now(),
            _clock?.UnbiasedMs ?? 0, currentBoot, new Dictionary<string, object?>
            {
                ["batch_id"] = name[0][^36..],
                ["enqueued_at"] = Utc.Format(EnqueuedAt(path)),
                ["buffer_residence_ms"] = residence,
            }));
    }

    public void Complete(string path) => File.Delete(path);

    /// <summary>재시도해도 성공할 수 없는 배치. 새 ID로 우회하지 않고 진단용으로 보관한다.</summary>
    public void Quarantine(string path, string reason)
    {
        var target = Path.Combine(QuarantineDir, Path.GetFileName(path));
        File.Move(path, target, overwrite: true);
        File.WriteAllText(target + ".reason.txt", reason);
    }

    /// <summary>상한 초과 시 오래된 미전송 배치부터 제거하고 누락 범위를 이벤트로 남긴다(README 5절).</summary>
    public int Prune()
    {
        var files = Pending().Select(p => new FileInfo(p)).ToList();
        var total = files.Sum(f => f.Length);
        var cutoff = _now() - MaxAge;
        var removed = new List<string>();
        foreach (var f in files)
        {
            var expired = EnqueuedAt(f.FullName) < cutoff;
            if (!expired && total <= MaxBytes) break;
            total -= f.Length;
            removed.Add(f.Name);
            f.Delete();
        }
        if (removed.Count > 0)
        {
            _events.Emit(new CollectionEvent(Guid.NewGuid(), "buffer_pruned", _now(), 0, null,
                new Dictionary<string, object?>
                {
                    ["batches"] = removed.Count,
                    ["first"] = removed[0],
                    ["last"] = removed[^1],
                    ["max_age_hours"] = MaxAge.TotalHours,
                    ["max_bytes"] = MaxBytes,
                }));
        }
        return removed.Count;
    }
}

public enum UploadAction { Done, Retry, PauseAuth, Quarantine }

/// <summary>계약 2·3절의 응답 처리. 재시도는 동결 본문 그대로만 한다.</summary>
public static class UploadPolicy
{
    public static readonly int[] BackoffSeconds = [3, 6, 12, 24, 48, 60];

    public static UploadAction Classify(int? status, string? code) => status switch
    {
        null => UploadAction.Retry,                       // 연결 실패·클라이언트 타임아웃: 커밋 여부 불명
        200 => UploadAction.Done,
        401 or 403 => UploadAction.PauseAuth,
        409 when code == "SENSOR_CONFIG_PENDING" => UploadAction.Retry,
        429 or 503 or 504 => UploadAction.Retry,
        >= 500 => UploadAction.Retry,
        _ => UploadAction.Quarantine,                     // 그 외 409/413/422/400: 격리·진단
    };

    public static TimeSpan Backoff(int attempt, Random? random = null)
    {
        var baseSeconds = BackoffSeconds[Math.Clamp(attempt, 0, BackoffSeconds.Length - 1)];
        var jitter = (random ?? Random.Shared).NextDouble() * 0.2;
        return TimeSpan.FromSeconds(baseSeconds * (1 + jitter));
    }
}

/// <summary>이벤트를 JSON Lines로 남긴다. collection_events 전송 계약이 정해지기 전의 로컬 기록.</summary>
public sealed class JsonlEventSink(string path) : IEventSink
{
    private readonly object _gate = new();

    public event Action<CollectionEvent>? Emitted;

    public void Emit(CollectionEvent e)
    {
        var line = System.Text.Json.JsonSerializer.Serialize(new Dictionary<string, object?>
        {
            ["event_id"] = e.EventId.ToString("D"),
            ["type"] = e.Type,
            ["observed_at"] = Utc.Format(e.ObservedAtUtc),
            ["monotonic_ms"] = e.MonotonicMs,
            ["boot_session_id"] = e.BootSessionId?.ToString("D"),
            ["details"] = e.Details,
        });
        lock (_gate)
        {
            Directory.CreateDirectory(Path.GetDirectoryName(Path.GetFullPath(path))!);
            File.AppendAllText(path, line + "\n");
        }
        Emitted?.Invoke(e);
    }
}
