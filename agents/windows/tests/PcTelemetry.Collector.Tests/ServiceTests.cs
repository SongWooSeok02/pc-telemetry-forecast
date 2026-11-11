using PcTelemetry.Collector;
using Xunit;

namespace PcTelemetry.Collector.Tests;

/// <summary>S2-02 서비스 수명주기 중 LHM·SCM 없이 검증할 수 있는 부분.</summary>
public class ServiceTests : IDisposable
{
    private readonly TempDir _dir = new();

    [Fact]
    public void Clean_release_is_not_reported_as_unclean()
    {
        var store = new StateStore(_dir.Path);
        var first = RunMarker.Acquire(store, "service", DateTime.UtcNow);
        Assert.Null(first.Previous);
        first.Release();
        Assert.False(File.Exists(store.PathOf(RunMarker.FileName)));
        Assert.Null(RunMarker.Acquire(store, "service", DateTime.UtcNow).Previous);
    }

    [Fact]
    public void Marker_left_by_a_killed_process_is_reported_once()
    {
        var store = new StateStore(_dir.Path);
        var started = new DateTime(2026, 11, 11, 3, 0, 0, DateTimeKind.Utc);
        RunMarker.Acquire(store, "service", started);     // 강제 종료: Release 없음
        var next = RunMarker.Acquire(store, "console", started.AddMinutes(5));
        Assert.NotNull(next.Previous);
        Assert.Equal("service", next.Previous!.Mode);
        Assert.Equal(Utc.Format(started), next.Previous.StartedAtUtc);
        Assert.Equal(Environment.ProcessId, next.Previous.Pid);
        next.Release();
        Assert.Null(RunMarker.Acquire(store, "service", DateTime.UtcNow).Previous);
    }

    [Fact]
    public void Torn_marker_still_counts_as_unclean()
    {
        var store = new StateStore(_dir.Path);
        File.WriteAllText(store.PathOf(RunMarker.FileName), "{\"mode\":\"serv");   // 쓰는 도중 전원 차단
        var marker = RunMarker.Acquire(store, "service", DateTime.UtcNow);
        Assert.NotNull(marker.Previous);
        Assert.Null(marker.Previous!.Mode);
    }

    [Fact]
    public void Boot_session_survives_service_restart_and_changes_on_reboot()
    {
        // 재부팅 시 순번/부팅 세션 계약(S1-01): 서비스 재시작은 같은 세션·순번 재사용 없음, 재부팅은 새 세션·0부터.
        var store = new StateStore(_dir.Path);
        var boot = new FixedBootId("bootid:41");
        var a = BootSession.Open(store, boot, reserveBlock: 10);
        var used = Enumerable.Range(0, 4).Select(_ => a.Take()).ToList();
        var b = BootSession.Open(store, boot, reserveBlock: 10);   // 서비스 재시작(같은 부팅)
        Assert.Equal(a.Id, b.Id);
        Assert.True(b.Take() > used.Max());
        boot.Value = "bootid:42";                                  // 재부팅
        var c = BootSession.Open(store, boot, reserveBlock: 10);
        Assert.NotEqual(a.Id, c.Id);
        Assert.True(c.IsNewBoot);
        Assert.Equal("bootid:41", c.PreviousBootId);
        Assert.Equal(0, c.Take());
    }

    public void Dispose() => _dir.Dispose();

    private sealed class FixedBootId(string value) : IBootIdProvider
    {
        public string Value { get; set; } = value;
        public string? GetBootId() => Value;
    }
}
