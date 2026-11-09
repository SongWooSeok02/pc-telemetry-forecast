using System.Text.Json;
using PcTelemetry.Collector;
using Xunit;

namespace PcTelemetry.Collector.Tests;

public class ContractTests
{
    private static string Fixture(string name) =>
        File.ReadAllText(Path.Combine(AppContext.BaseDirectory, "fixtures", name));

    public static IEnumerable<object[]> Vectors()
    {
        using var doc = JsonDocument.Parse(Fixture("hash-vectors.json"));
        foreach (var v in doc.RootElement.EnumerateArray())
            yield return [v.GetProperty("name").GetString()!, v.GetProperty("input").GetString()!,
                v.GetProperty("canonical").GetString()!, v.GetProperty("sha256").GetString()!];
    }

    [Theory]
    [MemberData(nameof(Vectors))]
    public void Canonical_matches_shared_vectors(string name, string input, string canonical, string sha)
    {
        var actual = Canonical.Normalize(input);
        Assert.True(canonical == actual, $"{name}: {actual}");
        Assert.Equal(sha, Canonical.Sha256Hex(actual));
    }

    [Theory]
    [InlineData("1e400")]
    [InlineData("0.0000001")]
    [InlineData("9007199254740992")]
    [InlineData("9007199254740991.5")]
    [InlineData("123456789012345678901234567890123")]
    public void Canonical_rejects_out_of_contract_numbers(string raw) =>
        Assert.ThrowsAny<FormatException>(() => Canonical.NormalizeNumber(raw));

    [Fact]
    public void Canonical_rejects_non_ascii_and_duplicate_keys()
    {
        Assert.ThrowsAny<FormatException>(() => Canonical.Normalize("{\"s\":\"온도\"}"));
        Assert.ThrowsAny<FormatException>(() => Canonical.Normalize("{\"a\":1,\"a\":2}"));
    }

    [Fact]
    public void Batch_builder_reproduces_contract_fixture_hash()
    {
        var samples = new[]
        {
            new Sample(0, new DateTime(2026, 10, 29, 0, 0, 0, DateTimeKind.Utc), 1000,
            [
                Reading.Ok("cpu/package/temp", "Cel", 52.5m),
                new Reading("gpu/board/power", "W", Quality.Unsupported, null, null),
                Reading.Null("cpu/fan", "rpm", Quality.Missing, "not_ready"),
            ]),
        };
        var batches = BatchFactory.Freeze(
            Guid.Parse("11111111-1111-4111-8111-111111111111"),
            Guid.Parse("22222222-2222-4222-8222-222222222222"),
            Guid.Parse("66666666-6666-4666-8666-666666666666"),
            samples, () => Guid.Parse("33333333-3333-4333-8333-333333333333"));

        var expected = Vectors().First(v => (string)v[0] == "full_batch");
        var batch = Assert.Single(batches);
        Assert.Equal((string)expected[2], batch.Body);
        Assert.Equal((string)expected[3], batch.Sha256);
    }

    [Fact]
    public void Batch_never_carries_zero_for_non_ok_and_rejects_bad_readings()
    {
        var id = Guid.NewGuid();
        Assert.Throws<InvalidOperationException>(() => BatchFactory.Build(id, id, id, id,
            [new Sample(0, DateTime.UtcNow, 0, [new Reading("cpu0/package/power", "W", Quality.Unsupported, 0m, null)])]));
        Assert.Throws<InvalidOperationException>(() => BatchFactory.Build(id, id, id, id,
            [new Sample(1, DateTime.UtcNow, 0, []), new Sample(1, DateTime.UtcNow, 0, [])]));
    }

    [Fact]
    public void Freeze_splits_at_sample_limit_before_issuing_ids()
    {
        var samples = Enumerable.Range(0, BatchFactory.MaxSamples + 1)
            .Select(i => new Sample(i, DateTime.UnixEpoch.AddSeconds(i), i * 1000L, [Reading.Ok("cpu0/total/load", "%", 1m)]))
            .ToList();
        var issued = 0;
        var batches = BatchFactory.Freeze(Guid.NewGuid(), Guid.NewGuid(), Guid.NewGuid(), samples,
            () => { issued++; return Guid.NewGuid(); });
        Assert.Equal(2, batches.Count);
        Assert.Equal(2, issued);
        Assert.Equal(BatchFactory.MaxSamples, batches[0].SampleCount);
        Assert.Equal(BatchFactory.MaxSamples, batches[1].FirstSeq);
        Assert.All(batches, b => Assert.True(b.Body.Length <= BatchFactory.MaxBodyBytes));
    }

    [Theory]
    [InlineData(200, null, UploadAction.Done)]
    [InlineData(null, null, UploadAction.Retry)]
    [InlineData(409, "SENSOR_CONFIG_PENDING", UploadAction.Retry)]
    [InlineData(409, "BATCH_CONFLICT", UploadAction.Quarantine)]
    [InlineData(409, "SAMPLE_CONFLICT", UploadAction.Quarantine)]
    [InlineData(413, "PAYLOAD_TOO_LARGE", UploadAction.Quarantine)]
    [InlineData(422, "HASH_MISMATCH", UploadAction.Quarantine)]
    [InlineData(401, "DEVICE_UNAUTHENTICATED", UploadAction.PauseAuth)]
    [InlineData(403, "DEVICE_FORBIDDEN", UploadAction.PauseAuth)]
    [InlineData(429, "RATE_LIMITED", UploadAction.Retry)]
    [InlineData(503, "SERVICE_UNAVAILABLE", UploadAction.Retry)]
    [InlineData(504, "REQUEST_TIMEOUT", UploadAction.Retry)]
    public void Upload_policy_follows_contract(int? status, string? code, UploadAction expected) =>
        Assert.Equal(expected, UploadPolicy.Classify(status, code));

    [Fact]
    public void Spool_prunes_expired_batches_and_records_range()
    {
        using var dir = new TempDir();
        var sink = new ListSink();
        var now = new DateTime(2026, 11, 4, 0, 0, 0, DateTimeKind.Utc);
        var spool = new Spool(dir.Path, sink, () => now);
        var id = Guid.NewGuid();
        var batch = BatchFactory.Freeze(id, id, id, [new Sample(0, now, 0, [Reading.Ok("cpu0/total/load", "%", 1m)])])[0];
        spool.Accept(batch);
        now = now.AddHours(25);
        spool.Accept(batch with { FirstSeq = 1 });
        Assert.Equal(1, spool.Prune());
        Assert.Single(spool.Pending());
        Assert.Single(sink.OfType("buffer_pruned"));
    }
}
