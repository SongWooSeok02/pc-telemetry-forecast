using System.Net;
using System.Text;
using System.Text.Json;
using Microsoft.Data.Sqlite;
using Xunit;

namespace PcTelemetry.Collector.Tests;

/// <summary>S2-01 SQLite 버퍼: 영속화·재시작 복원·불변 배치·상한/누락 범위. 값은 합성값이다.</summary>
public class BufferTests : IDisposable
{
    private static readonly Guid Machine = Guid.Parse("11111111-1111-4111-8111-111111111111");
    private static readonly Guid Boot = Guid.Parse("22222222-2222-4222-8222-222222222222");
    private static readonly Guid Config = Guid.Parse("33333333-3333-4333-8333-333333333333");

    private readonly TempDir _dir = new();
    private readonly ListSink _events = new();
    private DateTime _now = new(2026, 11, 9, 0, 0, 0, DateTimeKind.Utc);

    private string DbPath => Path.Combine(_dir.Path, SqliteBuffer.FileName);

    private SqliteBuffer Open(BufferOptions? options = null, Guid? machine = null) =>
        SqliteBuffer.Open(DbPath, machine ?? Machine, _events, options, () => _now);

    private Sample S(long seq) => new(seq, _now, 10_000 + seq * 1000,
        [Reading.Ok("cpu0/total/load", "%", 12.5m + seq), Reading.Null("cpu0/package/temp", "C", Quality.Unsupported, "driver_required")]);

    private void AddSamples(SqliteBuffer b, long from, int count, Guid? boot = null)
    {
        for (var i = 0; i < count; i++)
        {
            b.Add(boot ?? Boot, Config, S(from + i));
            _now = _now.AddSeconds(1);
        }
    }

    private static SqliteConnection Raw(string path)
    {
        var c = new SqliteConnection($"Data Source={path};Pooling=False");
        c.Open();
        return c;
    }

    [Fact]
    public void Freezes_every_three_samples_with_contract_body()
    {
        using var b = Open();
        AddSamples(b, 0, 7);
        var s = b.Stats();
        Assert.Equal(2, s.PendingBatches);
        Assert.Equal(1, s.OpenSamples);
        var next = b.NextPending()!;
        Assert.Equal(3, next.SampleCount);
        Assert.Equal(Canonical.Sha256Hex(next.Body), next.Sha256);
        Assert.Equal(Canonical.Normalize(next.Body), next.Body);
        using var doc = JsonDocument.Parse(next.Body);
        Assert.Equal(next.BatchId.ToString("D"), doc.RootElement.GetProperty("batch_id").GetString());
        Assert.Equal([0L, 1L, 2L], doc.RootElement.GetProperty("samples").EnumerateArray().Select(x => x.GetProperty("sample_seq").GetInt64()));
        // 미지원 값은 0이 아니라 null로 남는다.
        Assert.Equal(JsonValueKind.Null, doc.RootElement.GetProperty("samples")[0].GetProperty("readings")[1].GetProperty("value").ValueKind);
    }

    [Fact]
    public void Restart_restores_open_samples_and_keeps_frozen_ids()
    {
        Guid firstId;
        string firstBody;
        using (var b = Open())
        {
            AddSamples(b, 0, 5);           // 배치 1개 + 미동결 2개. Flush 없이 닫아 강제 종료를 흉내 낸다.
            var p = b.NextPending()!;
            firstId = p.BatchId;
            firstBody = p.Body;
        }
        using (var b = Open())
        {
            var s = b.Stats();
            Assert.Equal(2, s.PendingBatches);
            Assert.Equal(0, s.OpenSamples);
            var p = b.NextPending()!;
            Assert.Equal(firstId, p.BatchId);
            Assert.Equal(firstBody, p.Body);
            b.MarkSent(p.BatchId, null);
            var restored = b.NextPending()!;
            Assert.Equal(2, restored.SampleCount);
            using var doc = JsonDocument.Parse(restored.Body);
            Assert.Equal([3L, 4L], doc.RootElement.GetProperty("samples").EnumerateArray().Select(x => x.GetProperty("sample_seq").GetInt64()));
        }
        var opened = _events.OfType("buffer_opened").Last();
        Assert.Equal(2, opened.Details["restored_open_samples"]);
    }

    [Fact]
    public void Retry_returns_same_id_and_body_until_sent()
    {
        using var b = Open();
        AddSamples(b, 0, 3);
        var first = b.NextPending()!;
        b.RecordAttempt(first.BatchId, null, "HttpRequestException");
        b.RecordAttempt(first.BatchId, 503, "SERVICE_UNAVAILABLE");
        var again = b.NextPending()!;
        Assert.Equal(first.BatchId, again.BatchId);
        Assert.Equal(first.Body, again.Body);
        Assert.Equal(2, again.Attempts);
        b.MarkSent(first.BatchId, "2026-11-09T00:00:05.000000Z");
        Assert.Null(b.NextPending());
        Assert.Equal(1, b.Stats().SentLedgerBatches);
    }

    [Fact]
    public void Frozen_batches_are_immutable_in_the_database()
    {
        using (var b = Open())
        {
            AddSamples(b, 0, 7);           // 배치 2개 + 미동결 1개
            b.MarkSent(b.NextPending()!.BatchId, null);
        }
        using var db = Raw(DbPath);
        void Fails(string sql) => Assert.Throws<SqliteException>(() =>
        {
            using var c = db.CreateCommand();
            c.CommandText = sql;
            c.ExecuteNonQuery();
        });
        Fails("UPDATE batches SET body = body || ' ' WHERE state = 'pending'");
        Fails("UPDATE batches SET batch_id = '44444444-4444-4444-8444-444444444444' WHERE state = 'pending'");
        Fails("UPDATE batches SET first_seq = 9 WHERE state = 'pending'");
        Fails("UPDATE batches SET body = NULL WHERE state = 'pending'");
        Fails("UPDATE batches SET state = 'pending' WHERE state = 'sent'");
        // 같은 샘플을 두 번 미동결로 넣을 수 없다.
        Fails("INSERT INTO open_samples SELECT * FROM open_samples");
    }

    [Fact]
    public void Prunes_by_age_and_records_missing_range()
    {
        // 수집 중 1분마다 도는 자동 정리를 꺼서 이 호출의 결과만 본다.
        using var b = Open(new BufferOptions { PruneInterval = TimeSpan.FromDays(3650) });
        AddSamples(b, 0, 6);              // 동결 2개(0-2, 3-5)
        _now = _now.AddHours(25);
        AddSamples(b, 6, 3);              // 새 배치 하나는 남는다
        var dropped = b.Prune();
        Assert.Equal(6, dropped);
        var gap = Assert.Single(b.Gaps());
        Assert.Equal(("max_age", "pending", Boot, 0L, 5L, 2, 6), (gap.Reason, gap.State, gap.BootSessionId, gap.FirstSeq, gap.LastSeq, gap.Batches, gap.Samples));
        var s = b.Stats();
        Assert.Equal(1, s.PendingBatches);
        Assert.Equal(6, s.DroppedSamplesTotal);
        Assert.Equal(gap.LastSeq, s.LastGap!.LastSeq);
        var e = Assert.Single(_events.OfType("buffer_pruned"));
        Assert.Equal(6, e.Details["samples"]);
    }

    [Fact]
    public void Prunes_oldest_first_by_bytes()
    {
        using (var probe = SqliteBuffer.Open(Path.Combine(_dir.Path, "probe.sqlite3"), Machine, new ListSink(), null, () => _now))
        {
            AddSamples(probe, 0, 3);
        }
        long one;
        using (var b = Open(new BufferOptions { MaxBytes = long.MaxValue }))
        {
            AddSamples(b, 0, 3);
            one = b.Stats().UnsentBytes;
        }
        using (var b = Open(new BufferOptions { MaxBytes = one * 2 + one / 2 }))
        {
            AddSamples(b, 3, 9);          // 총 4배치 → 2개만 상한 안에 든다
            b.Prune();
            var s = b.Stats();
            Assert.Equal(2, s.PendingBatches);
            Assert.True(s.UnsentBytes <= b.Options.MaxBytes);
            var gap = Assert.Single(b.Gaps());
            Assert.Equal(("max_bytes", 0L, 5L), (gap.Reason, gap.FirstSeq, gap.LastSeq));
            using var doc = JsonDocument.Parse(b.NextPending()!.Body);
            Assert.Equal(6, doc.RootElement.GetProperty("samples")[0].GetProperty("sample_seq").GetInt64());
        }
    }

    [Fact]
    public void Batch_being_sent_is_not_pruned_until_its_result_is_known()
    {
        using var b = Open(new BufferOptions { PruneInterval = TimeSpan.FromDays(3650) });
        AddSamples(b, 0, 6);
        var head = b.NextPending()!;      // 업로더가 보내는 중
        _now = _now.AddHours(25);
        Assert.Equal(3, b.Prune());       // 다음 배치(3-5)만 지운다
        b.MarkSent(head.BatchId, null);   // 서버가 수락: 누락으로 기록되지 않아야 한다
        var gap = Assert.Single(b.Gaps());
        Assert.Equal((3L, 5L), (gap.FirstSeq, gap.LastSeq));
        Assert.Equal(1, b.Stats().SentLedgerBatches);

        AddSamples(b, 6, 3);
        var next = b.NextPending()!;
        b.RecordAttempt(next.BatchId, null, "HttpRequestException");   // 실패가 기록되면 다시 정리 대상
        _now = _now.AddHours(25);
        Assert.Equal(3, b.Prune());
    }

    [Fact]
    public void Clock_moving_back_does_not_prune()
    {
        using var b = Open();
        AddSamples(b, 0, 3);
        _now = _now.AddHours(-30);
        Assert.Equal(0, b.Prune());
        Assert.Equal(1, b.Stats().PendingBatches);
    }

    [Fact]
    public void Quarantined_batches_count_toward_limit_and_expire()
    {
        using var b = Open();
        AddSamples(b, 0, 3);
        b.Quarantine(b.NextPending()!.BatchId, 422, "VALIDATION_ERROR", "422 VALIDATION_ERROR");
        Assert.Equal(1, b.Stats().QuarantinedBatches);
        Assert.Null(b.NextPending());
        _now = _now.AddHours(25);
        b.Prune();
        Assert.Equal(0, b.Stats().QuarantinedBatches);
        Assert.Equal("quarantined", Assert.Single(b.Gaps()).State);
    }

    [Fact]
    public void Sent_ledger_is_kept_for_retention_only()
    {
        using var b = Open();
        AddSamples(b, 0, 3);
        b.MarkSent(b.NextPending()!.BatchId, null);
        _now = _now.AddHours(25);
        b.Prune();
        Assert.Equal(0, b.Stats().SentLedgerBatches);
        Assert.Empty(b.Gaps());   // 전송 완료 행 정리는 누락이 아니다
    }

    [Fact]
    public void Imports_legacy_file_spool_with_same_ids()
    {
        var spool = Path.Combine(_dir.Path, "spool");
        Directory.CreateDirectory(spool);
        var batch = BatchFactory.Freeze(Machine, Boot, Config, [S(0), S(1)])[0];
        File.WriteAllText(Path.Combine(spool, $"20261108T2300000000000-{0:D16}-{batch.BatchId:D}.json"), batch.Body);
        var broken = Path.Combine(spool, $"20261108T2300010000000-{2:D16}-{Guid.NewGuid():D}.json");
        File.WriteAllText(broken, "{ not json");
        using var b = Open();
        Assert.Equal(1, b.ImportLegacySpool(_dir.Path));
        var p = b.NextPending()!;
        Assert.Equal((batch.BatchId, batch.Body, batch.Sha256), (p.BatchId, p.Body, p.Sha256));
        Assert.Equal(new DateTime(2026, 11, 8, 23, 0, 0, DateTimeKind.Utc), p.FrozenAtUtc);
        Assert.True(File.Exists(broken));   // 해석할 수 없는 파일은 지우지 않는다
        Assert.Single(_events.OfType("legacy_spool_unreadable"));
        // 같은 범위를 다른 ID로 다시 넣지 않는다.
        File.WriteAllText(Path.Combine(spool, $"20261108T2300020000000-{0:D16}-x.json"),
            BatchFactory.Freeze(Machine, Boot, Config, [S(0), S(1)])[0].Body);
        Assert.Equal(0, b.ImportLegacySpool(_dir.Path));
        Assert.Equal(1, b.Stats().PendingBatches);
    }

    [Fact]
    public void Machine_change_quarantines_old_pending_batches()
    {
        using (var b = Open())
        {
            AddSamples(b, 0, 3);
        }
        using var reopened = Open(machine: Guid.NewGuid());
        Assert.Null(reopened.NextPending());
        Assert.Equal(1, reopened.Stats().QuarantinedBatches);
    }

    [Fact]
    public void Corrupt_file_is_kept_aside_and_replaced()
    {
        Directory.CreateDirectory(_dir.Path);
        File.WriteAllText(DbPath, "this is not a sqlite database, synthetic corruption for the test ....");
        using var b = Open();
        AddSamples(b, 0, 3);
        Assert.Equal(1, b.Stats().PendingBatches);
        Assert.Single(_events.OfType("buffer_reset"));
        Assert.Single(Directory.GetFiles(_dir.Path, SqliteBuffer.FileName + ".corrupt-*"));
    }

    [Fact]
    public void Boot_or_config_change_never_mixes_samples()
    {
        using var b = Open();
        AddSamples(b, 0, 2);
        AddSamples(b, 0, 2, Guid.Parse("55555555-5555-4555-8555-555555555555"));
        b.Flush();
        Assert.Equal(2, b.Stats().PendingBatches);
    }

    public void Dispose() => _dir.Dispose();
}

/// <summary>업로더: 응답 유실·장애·충돌에서 같은 ID·본문만 재전송하는지 가짜 서버 원장으로 확인한다.</summary>
public class UploaderTests : IDisposable
{
    private static readonly Guid Machine = Guid.Parse("11111111-1111-4111-8111-111111111111");
    private static readonly Guid Boot = Guid.Parse("22222222-2222-4222-8222-222222222222");
    private static readonly Guid Config = Guid.Parse("33333333-3333-4333-8333-333333333333");
    private const string Token = "synthetic-token-not-real";

    private readonly TempDir _dir = new();
    private readonly ListSink _events = new();
    private readonly FakeServer _server = new();
    private readonly SqliteBuffer _buffer;
    private readonly Uploader _uploader;

    public UploaderTests()
    {
        Directory.CreateDirectory(_dir.Path);
        var tokenFile = Path.Combine(_dir.Path, "token");
        File.WriteAllText(tokenFile, Token);
        _buffer = SqliteBuffer.Open(Path.Combine(_dir.Path, SqliteBuffer.FileName), Machine, _events);
        _uploader = new Uploader(new Uri("https://server.invalid"), tokenFile, _buffer, _events, _server);
    }

    private void Collect(int samples, long from = 0)
    {
        for (var i = 0; i < samples; i++)
            _buffer.Add(Boot, Config, new Sample(from + i, DateTime.UtcNow, i * 1000, [Reading.Ok("cpu0/total/load", "%", 10m + i)]));
    }

    private async Task<List<TimeSpan>> PumpAsync(int rounds)
    {
        var delays = new List<TimeSpan>();
        for (var i = 0; i < rounds && _buffer.NextPending() is not null; i++) delays.Add(await _uploader.SendPendingAsync(CancellationToken.None));
        return delays;
    }

    [Fact]
    public async Task Response_loss_after_commit_resends_same_batch_without_duplicates()
    {
        Collect(9);
        _server.DropResponseAfterCommit = [1, 3];    // 1번째·3번째 요청은 커밋 후 응답 유실
        await PumpAsync(10);
        Assert.Equal(3, _server.Ledger.Count);
        Assert.Equal(9, _server.SampleKeys.Count);
        Assert.Equal(5, _server.Requests.Count);
        Assert.Equal(2, _server.Replays);
        // 재전송 요청은 직전 요청과 ID·본문·해시가 같다.
        Assert.Equal(_server.Requests[0], _server.Requests[1]);
        Assert.Equal(_server.Requests[2], _server.Requests[3]);
        Assert.Equal(3, _buffer.Stats().SentLedgerBatches);
        Assert.Equal(0, _buffer.Stats().PendingBatches);
    }

    [Fact]
    public async Task Outage_keeps_order_backs_off_and_recovers()
    {
        Collect(12);
        _server.Offline = true;
        var delays = await PumpAsync(8);
        Assert.All(delays, d => Assert.InRange(d.TotalSeconds, 3, 72));
        Assert.True(delays[^1] > delays[0]);
        Assert.Equal("retrying", _uploader.State);
        Assert.Single(_events.OfType("upload_retrying"));
        _server.Offline = false;
        await PumpAsync(10);
        Assert.Equal(4, _server.Ledger.Count);
        var seqs = _server.Accepted.SelectMany(b => b.Seqs).ToList();
        Assert.Equal(Enumerable.Range(0, 12).Select(i => (long)i), seqs);
        Assert.Single(_events.OfType("upload_recovered"));
        Assert.Equal("ok", _uploader.State);
    }

    [Fact]
    public async Task Retry_after_is_honoured_but_capped()
    {
        Collect(3);
        _server.Status503RetryAfter = TimeSpan.FromSeconds(120);
        var d = (await PumpAsync(1)).Single();
        Assert.Equal(TimeSpan.FromSeconds(120), d);
        Assert.Equal(TimeSpan.FromMinutes(5), UploadPolicy.RetryDelay(0, TimeSpan.FromHours(1)));
    }

    [Fact]
    public async Task Conflict_is_quarantined_not_rebuilt_under_new_id()
    {
        Collect(6);
        var first = _buffer.NextPending()!;
        _server.Ledger[first.BatchId.ToString("D")] = first.Body.Replace("\"value\":10", "\"value\":11");
        await PumpAsync(5);
        Assert.Equal(1, _buffer.Stats().QuarantinedBatches);
        Assert.Equal(2, _server.Ledger.Count);
        Assert.Equal(2, _server.Requests.Select(r => r.Id).Distinct().Count());
        Assert.Single(_events.OfType("batch_quarantined"));
    }

    [Fact]
    public async Task Partial_ack_is_recorded_and_no_new_batch_is_made()
    {
        Collect(3);
        _server.AcceptedOverride = 2;
        await PumpAsync(2);
        Assert.Single(_events.OfType("upload_ack_mismatch"));
        Assert.Single(_server.Requests);
        Assert.Equal(0, _buffer.Stats().PendingBatches);
    }

    [Fact]
    public async Task Auth_failure_pauses_and_never_logs_the_token()
    {
        Collect(3);
        _server.Unauthorized = true;
        var d = (await PumpAsync(1)).Single();
        Assert.Equal(TimeSpan.FromSeconds(60), d);
        Assert.Equal("paused_auth", _uploader.State);
        Assert.Equal(1, _buffer.Stats().PendingBatches);
        Assert.All(_server.Requests, r => Assert.Equal("Bearer " + Token, r.Authorization));
        var logged = JsonSerializer.Serialize(_events.Events.Select(e => e.Details));
        Assert.DoesNotContain(Token, logged, StringComparison.Ordinal);
        foreach (var file in new[] { _buffer.Path, _buffer.Path + "-wal" }.Where(File.Exists))
        {
            using var fs = new FileStream(file, FileMode.Open, FileAccess.Read, FileShare.ReadWrite | FileShare.Delete);
            using var ms = new MemoryStream();
            fs.CopyTo(ms);
            Assert.DoesNotContain(Token, Encoding.Latin1.GetString(ms.ToArray()), StringComparison.Ordinal);
        }
    }

    public void Dispose()
    {
        _uploader.Dispose();
        _buffer.Dispose();
        _dir.Dispose();
    }

    /// <summary>계약 2절 원장 동작을 흉내 내는 가짜 서버(합성 데이터만).</summary>
    private sealed class FakeServer : HttpMessageHandler
    {
        public Dictionary<string, string> Ledger { get; } = [];
        public HashSet<(string Boot, long Seq)> SampleKeys { get; } = [];
        public List<(string Id, string Body, string Sha, string? Authorization)> Requests { get; } = [];
        public List<(string Id, List<long> Seqs)> Accepted { get; } = [];
        public int Replays { get; private set; }
        public int[] DropResponseAfterCommit { get; set; } = [];
        public bool Offline { get; set; }
        public bool Unauthorized { get; set; }
        public TimeSpan? Status503RetryAfter { get; set; }
        public int? AcceptedOverride { get; set; }

        protected override async Task<HttpResponseMessage> SendAsync(HttpRequestMessage request, CancellationToken cancellationToken)
        {
            if (Offline) throw new HttpRequestException("connection refused (synthetic)");
            var body = await request.Content!.ReadAsStringAsync(cancellationToken);
            using var doc = JsonDocument.Parse(body);
            var id = doc.RootElement.GetProperty("batch_id").GetString()!;
            Requests.Add((id, body, request.Headers.GetValues("X-Content-SHA256").Single(), request.Headers.Authorization?.ToString()));
            if (Unauthorized) return Error(HttpStatusCode.Unauthorized, "DEVICE_UNAUTHENTICATED");
            if (Status503RetryAfter is { } ra)
            {
                var r = Error(HttpStatusCode.ServiceUnavailable, "SERVICE_UNAVAILABLE");
                r.Headers.RetryAfter = new System.Net.Http.Headers.RetryConditionHeaderValue(ra);
                return r;
            }
            if (Canonical.Sha256Hex(Canonical.Normalize(body)) != request.Headers.GetValues("X-Content-SHA256").Single())
                return Error(HttpStatusCode.UnprocessableEntity, "HASH_MISMATCH");
            var samples = doc.RootElement.GetProperty("samples").EnumerateArray().Select(s => s.GetProperty("sample_seq").GetInt64()).ToList();
            var boot = doc.RootElement.GetProperty("boot_session_id").GetString()!;
            if (Ledger.TryGetValue(id, out var prior))
            {
                if (prior != Canonical.Normalize(body)) return Error(HttpStatusCode.Conflict, "BATCH_CONFLICT");
                Replays++;
                return Ok(id, samples.Count);
            }
            if (samples.Any(s => SampleKeys.Contains((boot, s)))) return Error(HttpStatusCode.Conflict, "SAMPLE_CONFLICT");
            Ledger[id] = Canonical.Normalize(body);
            foreach (var s in samples) SampleKeys.Add((boot, s));
            Accepted.Add((id, samples));
            if (DropResponseAfterCommit.Contains(Requests.Count)) throw new HttpRequestException("response lost after commit (synthetic)");
            return Ok(id, AcceptedOverride ?? samples.Count);
        }

        private static HttpResponseMessage Ok(string id, int accepted) => new(HttpStatusCode.OK)
        {
            Content = new StringContent(JsonSerializer.Serialize(new { schema_version = "0.1.0", batch_id = id, accepted_samples = accepted, received_at = "2026-11-09T00:00:03.000000Z" })),
        };

        private static HttpResponseMessage Error(HttpStatusCode status, string code) => new(status)
        {
            Content = new StringContent(JsonSerializer.Serialize(new
            {
                error = new { code, message = code, request_id = "44444444-4444-4444-8444-444444444444", retryable = false, details = Array.Empty<object>() },
            })),
        };
    }
}
