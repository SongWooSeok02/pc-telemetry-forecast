using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;

namespace PcTelemetry.Collector;

public enum UploadAction { Done, Retry, PauseAuth, Quarantine }

/// <summary>계약 2·3절의 응답 처리. 재시도는 동결 본문 그대로만 한다.</summary>
public static class UploadPolicy
{
    public static readonly int[] BackoffSeconds = [3, 6, 12, 24, 48, 60];

    /// <summary>서버 Retry-After를 따르되 이보다 오래 멈추지 않는다.</summary>
    public static readonly TimeSpan MaxRetryAfter = TimeSpan.FromMinutes(5);

    public static UploadAction Classify(int? status, string? code) => status switch
    {
        null => UploadAction.Retry,                       // 연결 실패·응답 유실·클라이언트 타임아웃: 커밋 여부 불명
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

    /// <summary>재시도 대기: 지수 백오프와 서버 Retry-After 중 긴 쪽, 최대 5분.</summary>
    public static TimeSpan RetryDelay(int attempt, TimeSpan? retryAfter, Random? random = null)
    {
        var backoff = Backoff(attempt, random);
        if (retryAfter is not { } ra || ra <= backoff) return backoff;
        return ra > MaxRetryAfter ? MaxRetryAfter : ra;
    }
}

/// <summary>
/// SQLite 버퍼의 동결 배치를 POST /v1/telemetry/batches로 보낸다. 본문·ID·해시를 바꾸지 않고 재시도한다.
/// 토큰은 파일에서 읽어 Authorization 헤더에만 쓰고 로그·이벤트·버퍼에 남기지 않는다(DPAPI 저장은 S2-03/15.8 범위).
/// </summary>
public sealed class Uploader : IDisposable
{
    private readonly Uri _server;
    private readonly string _tokenFile;
    private readonly SqliteBuffer _buffer;
    private readonly IEventSink _events;
    private readonly HttpClient _http;
    private readonly HashSet<string> _pendingConfigsReported = new(StringComparer.Ordinal);
    private int _attempt;
    private DateTime? _retryingSince;
    private int _retriesInOutage;

    public Uploader(Uri server, string tokenFile, SqliteBuffer buffer, IEventSink events, HttpMessageHandler? handler = null)
    {
        _server = server;
        _tokenFile = tokenFile;
        _buffer = buffer;
        _events = events;
        _http = handler is null ? new HttpClient() : new HttpClient(handler);
        _http.Timeout = TimeSpan.FromSeconds(10);
    }

    public long Sent { get; private set; }
    public long Retries { get; private set; }
    public long Quarantined { get; private set; }
    public long AckMismatches { get; private set; }

    /// <summary>공개 상태 파일(S1-06)용: idle, ok, retrying, paused_auth.</summary>
    public string State { get; private set; } = "idle";
    public DateTime? LastSuccessUtc { get; private set; }

    public async Task RunAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            var delay = await SendPendingAsync(token).ConfigureAwait(false);
            try
            {
                await Task.Delay(delay, token).ConfigureAwait(false);
            }
            catch (OperationCanceledException)
            {
                break;
            }
        }
    }

    /// <summary>종료 시 남은 배치를 제한 시간 동안 보낸다. 못 보낸 배치는 버퍼에 남아 다음 시작 때 이어 보낸다.</summary>
    public async Task DrainAsync(TimeSpan limit)
    {
        using var cts = new CancellationTokenSource(limit);
        while (!cts.IsCancellationRequested && _buffer.NextPending() is not null)
        {
            var delay = await SendPendingAsync(cts.Token).ConfigureAwait(false);
            if (delay > TimeSpan.Zero)
            {
                try { await Task.Delay(delay, cts.Token).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
            }
        }
    }

    /// <summary>대기 배치를 순서대로 보낸다. 다음 시도까지 기다릴 시간을 돌려준다.</summary>
    public async Task<TimeSpan> SendPendingAsync(CancellationToken token)
    {
        while (!token.IsCancellationRequested)
        {
            var batch = _buffer.NextPending();
            if (batch is null) return TimeSpan.FromSeconds(1);
            if (Canonical.Sha256Hex(batch.Body) != batch.Sha256)
            {
                // 디스크 손상 등으로 동결 내용이 달라졌다. 다른 내용을 같은 ID로 보내지 않는다.
                _buffer.Quarantine(batch.BatchId, null, "LOCAL_HASH_MISMATCH", "local body does not match frozen sha256");
                Quarantined++;
                Emit("batch_quarantined", new() { ["batch_id"] = batch.BatchId.ToString("D"), ["code"] = "LOCAL_HASH_MISMATCH" });
                continue;
            }
            var result = await PostAsync(batch, token).ConfigureAwait(false);
            if (result.Cancelled) return TimeSpan.Zero;
            switch (UploadPolicy.Classify(result.Status, result.Code))
            {
                case UploadAction.Done:
                    CheckAck(batch, result.Body);
                    _buffer.MarkSent(batch.BatchId, ReceivedAt(result.Body));
                    Sent++;
                    _attempt = 0;
                    State = "ok";
                    LastSuccessUtc = DateTime.UtcNow;
                    if (_retryingSince is { } since)
                    {
                        Emit("upload_recovered", new()
                        {
                            ["retrying_since"] = Utc.Format(since),
                            ["outage_s"] = Math.Round((DateTime.UtcNow - since).TotalSeconds, 1),
                            ["retries"] = _retriesInOutage,
                            ["pending_batches"] = _buffer.Stats().PendingBatches,
                        });
                        _retryingSince = null;
                        _retriesInOutage = 0;
                    }
                    continue;
                case UploadAction.Retry:
                    _buffer.RecordAttempt(batch.BatchId, result.Status, result.Code);
                    Retries++;
                    State = "retrying";
                    if (_retryingSince is null)
                    {
                        _retryingSince = DateTime.UtcNow;
                        Emit("upload_retrying", new() { ["batch_id"] = batch.BatchId.ToString("D"), ["status"] = result.Status, ["code"] = result.Code });
                    }
                    _retriesInOutage++;
                    if (result.Code == "SENSOR_CONFIG_PENDING") ReportPendingConfig(batch.Body);
                    // 순서를 지키기 위해 같은 배치부터 다시 시도한다. 폭주를 막으려고 지수 백오프(최대 60초)와 Retry-After를 따른다.
                    return UploadPolicy.RetryDelay(_attempt++, result.RetryAfter);
                case UploadAction.PauseAuth:
                    _buffer.RecordAttempt(batch.BatchId, result.Status, result.Code);
                    if (State != "paused_auth")
                        Emit("upload_paused_auth", new() { ["status"] = result.Status, ["code"] = result.Code });
                    State = "paused_auth";
                    return TimeSpan.FromSeconds(60);
                case UploadAction.Quarantine:
                    _buffer.Quarantine(batch.BatchId, result.Status, result.Code, $"{result.Status} {result.Code}");
                    Quarantined++;
                    Emit("batch_quarantined", new() { ["batch_id"] = batch.BatchId.ToString("D"), ["status"] = result.Status, ["code"] = result.Code });
                    continue;
            }
        }
        return TimeSpan.Zero;
    }

    private sealed record PostResult(int? Status, string? Code, string? Body, TimeSpan? RetryAfter, bool Cancelled = false);

    private async Task<PostResult> PostAsync(PendingBatch batch, CancellationToken token)
    {
        string bearer;
        try
        {
            bearer = (await File.ReadAllTextAsync(_tokenFile, token).ConfigureAwait(false)).Trim();
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return new PostResult(401, "TOKEN_FILE_UNREADABLE", null, null);
        }
        catch (OperationCanceledException)
        {
            return new PostResult(null, null, null, null, Cancelled: true);
        }
        using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(_server, "/v1/telemetry/batches"))
        {
            Content = new StringContent(batch.Body, Encoding.UTF8, "application/json"),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        request.Headers.Add("X-Content-SHA256", batch.Sha256);
        try
        {
            using var response = await _http.SendAsync(request, token).ConfigureAwait(false);
            var status = (int)response.StatusCode;
            var text = await response.Content.ReadAsStringAsync(token).ConfigureAwait(false);
            var retryAfter = response.Headers.RetryAfter is { } ra
                ? ra.Delta ?? (ra.Date is { } d ? d - DateTimeOffset.UtcNow : null)
                : null;
            return status == 200
                ? new PostResult(200, null, text, null)
                : new PostResult(status, ErrorCode(text), null, retryAfter);
        }
        catch (Exception) when (token.IsCancellationRequested)
        {
            return new PostResult(null, null, null, null, Cancelled: true);
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException or IOException)
        {
            // 응답 유실·연결 실패·타임아웃: 커밋 여부를 알 수 없으므로 같은 ID·본문으로 다시 보낸다.
            return new PostResult(null, ex.GetType().Name, null, null);
        }
    }

    /// <summary>
    /// 200 응답이 이 배치 전체 수락인지 확인한다. 계약상 부분 성공은 없으므로 불일치는 서버 결함으로 기록만 하고,
    /// 남은 샘플을 새 ID로 다시 묶어 보내지 않는다.
    /// </summary>
    private void CheckAck(PendingBatch batch, string? body)
    {
        string? id = null;
        long? accepted = null;
        try
        {
            using var doc = JsonDocument.Parse(body ?? "");
            id = doc.RootElement.GetProperty("batch_id").GetString();
            accepted = doc.RootElement.GetProperty("accepted_samples").GetInt64();
        }
        catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException or FormatException)
        {
        }
        if (id == batch.BatchId.ToString("D") && accepted == batch.SampleCount) return;
        AckMismatches++;
        Emit("upload_ack_mismatch", new()
        {
            ["batch_id"] = batch.BatchId.ToString("D"),
            ["ack_batch_id"] = id,
            ["sample_count"] = batch.SampleCount,
            ["accepted_samples"] = accepted,
        });
    }

    private static string? ReceivedAt(string? body)
    {
        try
        {
            using var doc = JsonDocument.Parse(body ?? "");
            return doc.RootElement.TryGetProperty("received_at", out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;
        }
        catch (JsonException)
        {
            return null;
        }
    }

    private static string? ErrorCode(string text)
    {
        try
        {
            using var doc = JsonDocument.Parse(text);
            return doc.RootElement.GetProperty("error").GetProperty("code").GetString();
        }
        catch (Exception e) when (e is JsonException or KeyNotFoundException or InvalidOperationException)
        {
            return null;
        }
    }

    private void ReportPendingConfig(string body)
    {
        using var doc = JsonDocument.Parse(body);
        var id = doc.RootElement.GetProperty("sensor_config_id").GetString()!;
        if (!_pendingConfigsReported.Add(id)) return;
        // 등록 API 합의 전에는 관리자가 sensor-configs/<id>.json을 등록한다(backend/README).
        Emit("sensor_config_pending", new() { ["sensor_config_id"] = id, ["registration_file"] = $"sensor-configs/{id}.json" });
    }

    private void Emit(string type, Dictionary<string, object?> details) =>
        _events.Emit(new CollectionEvent(Guid.NewGuid(), type, DateTime.UtcNow, 0, null, details));

    public void Dispose() => _http.Dispose();
}

/// <summary>이벤트를 JSON Lines로 남긴다. collection_events 전송 계약이 정해지기 전의 로컬 기록.</summary>
public sealed class JsonlEventSink(string path) : IEventSink
{
    private readonly object _gate = new();

    public event Action<CollectionEvent>? Emitted;

    public void Emit(CollectionEvent e)
    {
        var line = JsonSerializer.Serialize(new Dictionary<string, object?>
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
