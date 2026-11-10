using System.Net.Http.Headers;
using System.Text;
using System.Text.Json;
using PcTelemetry.Collector;

namespace PcTelemetry.Agent;

/// <summary>
/// 스풀의 동결 배치를 POST /v1/telemetry/batches로 보낸다. 본문·ID·해시를 바꾸지 않고 재시도한다.
/// 토큰은 파일에서 읽고 로그·이벤트에 남기지 않는다(DPAPI 저장은 S2-03/15.8 범위).
/// </summary>
internal sealed class Uploader(Uri server, string tokenFile, Spool spool, IEventSink events, Guid? boot) : IDisposable
{
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(10) };
    private readonly HashSet<string> _pendingConfigsReported = new(StringComparer.Ordinal);
    private int _attempt;
    private long _reconnections;

    public long Reconnections => Interlocked.Read(ref _reconnections);

    public long Sent { get; private set; }
    public long Retries { get; private set; }
    public long Quarantined { get; private set; }

    /// <summary>공개 상태 파일(S1-06)용: idle, ok, retrying, paused_auth.</summary>
    public string State { get; private set; } = "idle";
    public DateTime? LastSuccessUtc { get; private set; }

    public async Task RunAsync(CancellationToken token)
    {
        var lastPrune = DateTime.MinValue;
        while (!token.IsCancellationRequested)
        {
            if (DateTime.UtcNow - lastPrune > TimeSpan.FromMinutes(1))
            {
                spool.Prune();
                lastPrune = DateTime.UtcNow;
            }
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

    /// <summary>종료 시 남은 배치를 제한 시간 동안 보낸다. 못 보낸 배치는 스풀에 남는다.</summary>
    public async Task DrainAsync(TimeSpan limit)
    {
        using var cts = new CancellationTokenSource(limit);
        while (!cts.IsCancellationRequested && spool.Pending().Count > 0)
        {
            var delay = await SendPendingAsync(cts.Token).ConfigureAwait(false);
            if (delay > TimeSpan.Zero)
            {
                try { await Task.Delay(delay, cts.Token).ConfigureAwait(false); }
                catch (OperationCanceledException) { break; }
            }
        }
    }

    private async Task<TimeSpan> SendPendingAsync(CancellationToken token)
    {
        foreach (var path in spool.Pending())
        {
            if (token.IsCancellationRequested) return TimeSpan.Zero;
            var body = await File.ReadAllTextAsync(path, token).ConfigureAwait(false);
            var (status, code) = await PostAsync(path, body, token).ConfigureAwait(false);
            switch (UploadPolicy.Classify(status, code))
            {
                case UploadAction.Done:
                    spool.Complete(path);
                    Sent++;
                    _attempt = 0;
                    if (State is "retrying" or "paused_auth") Interlocked.Increment(ref _reconnections);
                    State = "ok";
                    LastSuccessUtc = DateTime.UtcNow;
                    continue;
                case UploadAction.Retry:
                    Retries++;
                    State = "retrying";
                    if (code == "SENSOR_CONFIG_PENDING") ReportPendingConfig(body);
                    // 순서를 지키기 위해 같은 배치부터 다시 시도한다.
                    return UploadPolicy.Backoff(_attempt++);
                case UploadAction.PauseAuth:
                    State = "paused_auth";
                    Emit("upload_paused_auth", new() { ["status"] = status, ["code"] = code });
                    return TimeSpan.FromSeconds(60);
                case UploadAction.Quarantine:
                    spool.Quarantine(path, $"{status} {code}");
                    Quarantined++;
                    Emit("batch_quarantined", new() { ["file"] = Path.GetFileName(path), ["status"] = status, ["code"] = code });
                    continue;
            }
        }
        return TimeSpan.FromSeconds(1);
    }

    private async Task<(int? Status, string? Code)> PostAsync(string path, string body, CancellationToken token)
    {
        string bearer;
        try
        {
            bearer = (await File.ReadAllTextAsync(tokenFile, token).ConfigureAwait(false)).Trim();
        }
        catch (IOException)
        {
            return (401, "TOKEN_FILE_UNREADABLE");
        }
        using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(server, "/v1/telemetry/batches"))
        {
            Content = new StringContent(body, Encoding.UTF8, "application/json"),
        };
        request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
        request.Headers.Add("X-Content-SHA256", Canonical.Sha256Hex(body));
        try
        {
            spool.RecordTransmission(path, boot);
            using var response = await _http.SendAsync(request, token).ConfigureAwait(false);
            var status = (int)response.StatusCode;
            if (status == 200) return (200, null);
            var text = await response.Content.ReadAsStringAsync(token).ConfigureAwait(false);
            return (status, ErrorCode(text));
        }
        catch (Exception ex) when (ex is HttpRequestException or TaskCanceledException && !token.IsCancellationRequested)
        {
            return (null, ex.GetType().Name);
        }
    }

    private static string? ErrorCode(string text)
    {
        try
        {
            using var doc = JsonDocument.Parse(text);
            return doc.RootElement.GetProperty("error").GetProperty("code").GetString();
        }
        catch (Exception)
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
        events.Emit(new CollectionEvent(Guid.NewGuid(), type, DateTime.UtcNow, 0, null, details));

    public void Dispose() => _http.Dispose();
}
