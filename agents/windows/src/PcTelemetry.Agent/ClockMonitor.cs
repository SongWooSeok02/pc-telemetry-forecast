using System.Net.Http.Headers;
using System.Net.Http.Json;
using System.Text.Json;
using PcTelemetry.Collector;

namespace PcTelemetry.Agent;

internal sealed class ClockMonitor(Uri server, string tokenFile, IClock clock, Guid? boot,
    IEventSink events, Uploader uploader) : IDisposable
{
    private static readonly int[] RetrySeconds = [5, 10, 20, 40, 60];
    private readonly HttpClient _http = new() { Timeout = TimeSpan.FromSeconds(2) };
    private int _exceeded;

    private readonly record struct Stamp(DateTime Wall, long Mono, long Interrupt)
    {
        public bool ResumedSince(Stamp earlier) => Interrupt - earlier.Interrupt - (Mono - earlier.Mono) >= 1000;
        public bool JumpedSince(Stamp earlier) =>
            Math.Abs((Wall - earlier.Wall).TotalMilliseconds - (Mono - earlier.Mono)) >= 2000;
    }

    private Stamp Now() => new(clock.UtcNow, clock.UnbiasedMs, clock.InterruptMs);

    public async Task RunAsync(CancellationToken token)
    {
        var baseline = Now();
        var due = baseline.Mono;
        var failures = 0;
        var reconnections = uploader.Reconnections;
        try
        {
            while (!token.IsCancellationRequested)
            {
                var current = Now();
                if (current.ResumedSince(baseline) || current.JumpedSince(baseline))
                {
                    _exceeded = 0;
                    failures = 0;
                    due = current.Mono;
                    Emit(current, new() { ["clock_status"] = "unknown",
                        ["reason"] = current.ResumedSince(baseline) ? "resume" : "clock_jump" });
                }
                if (uploader.Reconnections != reconnections)
                {
                    reconnections = uploader.Reconnections;
                    _exceeded = 0;
                    failures = 0;
                    due = current.Mono;
                }
                baseline = current;
                if (current.Mono >= due)
                {
                    var accepted = await MeasureAsync(token);
                    var delay = accepted ? 60 : RetrySeconds[Math.Min(failures++, RetrySeconds.Length - 1)];
                    if (accepted) failures = 0;
                    // Keep the pre-request baseline so a resume/jump during the exchange
                    // resets the schedule at the next poll.
                    due = clock.UnbiasedMs + delay * 1000;
                }
                await Task.Delay(250, token);
            }
        }
        catch (OperationCanceledException) when (token.IsCancellationRequested) { }
    }

    private async Task<bool> MeasureAsync(CancellationToken token)
    {
        var id = Guid.NewGuid();
        var start = Now();
        Stamp? finish = null;
        var details = new Dictionary<string, object?> { ["request_id"] = id, ["clock_status"] = "unknown" };
        try
        {
            var bearer = (await File.ReadAllTextAsync(tokenFile, token)).Trim();
            using var request = new HttpRequestMessage(HttpMethod.Post, new Uri(server, "/v1/device/time"))
            {
                Content = JsonContent.Create(new { schema_version = "0.1.0", request_id = id }),
            };
            request.Headers.Authorization = new AuthenticationHeaderValue("Bearer", bearer);
            start = Now();
            using var response = await _http.SendAsync(request, token);
            var end = Now();
            finish = end;
            if (response.IsSuccessStatusCode)
            {
                using var document = JsonDocument.Parse(await response.Content.ReadAsStringAsync(token));
                var body = document.RootElement;
                var t2 = body.GetProperty("server_received_at").GetDateTimeOffset().UtcDateTime;
                var t3 = body.GetProperty("server_sent_at").GetDateTimeOffset().UtcDateTime;
                var precision = body.GetProperty("precision_us").GetInt32();
                var serverStatus = body.GetProperty("clock_status").GetString();
                var duration = end.Mono - start.Mono;
                var processing = (t3 - t2).TotalMilliseconds;
                var rtt = duration - processing;
                details["server_received_at"] = Utc.Format(t2);
                details["server_sent_at"] = Utc.Format(t3);
                details["precision_us"] = precision;
                details["server_clock_status"] = serverStatus;
                details["network_rtt_ms"] = rtt;

                if (body.GetProperty("schema_version").GetString() == "0.1.0" &&
                    body.GetProperty("request_id").GetGuid() == id && serverStatus == "synchronized" &&
                    precision > 0 && duration is >= 0 and < 2000 && processing >= 0 &&
                    rtt is >= 0 and <= 250 && !end.ResumedSince(start) && !end.JumpedSince(start))
                {
                    var offset = ((t2 - start.Wall).TotalMilliseconds + (t3 - end.Wall).TotalMilliseconds) / 2;
                    details["offset_ms"] = offset;
                    details["uncertainty_ms"] = rtt / 2 + precision / 1000.0 + 16;
                    _exceeded = Math.Abs(offset) > 5000 ? Math.Min(3, _exceeded + 1) : 0;
                    details["clock_status"] = _exceeded == 3 ? "clock_skew_suspected" :
                        _exceeded == 0 ? "normal" : "unknown";
                    return true;
                }
                details["reason"] = "measurement_rejected";
            }
            else
            {
                details["http_status"] = (int)response.StatusCode;
            }
        }
        catch (Exception ex) when (ex is IOException or UnauthorizedAccessException or HttpRequestException or
            JsonException or FormatException or InvalidOperationException or KeyNotFoundException ||
            ex is OperationCanceledException && !token.IsCancellationRequested)
        {
            details["reason"] = ex.GetType().Name;
        }
        finally
        {
            if (!details.ContainsKey("offset_ms")) _exceeded = 0;
            details["t1"] = Utc.Format(start.Wall);
            details["m1"] = start.Mono;
            details["t4"] = finish is { } end ? Utc.Format(end.Wall) : null;
            details["m4"] = finish?.Mono;
            if (!token.IsCancellationRequested) Emit(finish ?? Now(), details);
        }
        return false;
    }

    private void Emit(Stamp stamp, Dictionary<string, object?> details) =>
        events.Emit(new CollectionEvent(Guid.NewGuid(), "clock_measurement", stamp.Wall, stamp.Mono, boot, details));

    public void Dispose() => _http.Dispose();
}
