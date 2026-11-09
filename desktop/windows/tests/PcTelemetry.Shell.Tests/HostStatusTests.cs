using PcTelemetry.Shell.Core;
using Xunit;

namespace PcTelemetry.Shell.Tests;

public class HostStatusTests
{
    private static readonly DateTime Now = new(2026, 11, 9, 2, 0, 0, DateTimeKind.Utc);

    // agents/windows Program.WriteStatus가 쓰는 형식과 같은 예.
    private const string AgentJson = """
        {
          "schema": "pc-telemetry/agent-status/1",
          "updated_at_utc": "2026-11-09T01:59:55.000000Z",
          "agent_version": "0.1.0",
          "collecting": true,
          "upload": { "configured": true, "state": "retrying", "pending_batches": 12, "last_success_at_utc": "2026-11-09T01:58:00.000000Z" }
        }
        """;

    [Fact]
    public void ParsesAgentStatus()
    {
        var s = AgentStatusReader.Parse(AgentJson, Now);
        Assert.Equal(StatusFreshness.Fresh, s.Freshness);
        Assert.True(s.Collecting);
        Assert.Equal(12, s.PendingBatches);
        Assert.Equal("retrying", s.UploadState);
        Assert.Equal(new DateTime(2026, 11, 9, 1, 58, 0, DateTimeKind.Utc), s.LastSuccessUtc);
        Assert.Equal("12개 배치", HostStatusText.Pending(s));
        Assert.Equal("재시도 중", HostStatusText.Upload(s));
    }

    [Fact]
    public void OldStatusIsStale_NotHidden()
    {
        var s = AgentStatusReader.Parse(AgentJson, Now.AddMinutes(5));
        Assert.Equal(StatusFreshness.Stale, s.Freshness);
        Assert.Equal("12개 배치(갱신 지연)", HostStatusText.Pending(s));
    }

    [Theory]
    [InlineData("{ not json")]
    [InlineData("""{"schema":"other/1","updated_at_utc":"2026-11-09T01:59:55Z"}""")]
    [InlineData("""{"schema":"pc-telemetry/agent-status/1"}""")]
    [InlineData("[]")]
    public void UnknownOrBrokenStatusIsUnreadable(string json)
    {
        var s = AgentStatusReader.Parse(json, Now);
        Assert.Equal(StatusFreshness.Unreadable, s.Freshness);
        Assert.Equal("확인 불가", HostStatusText.Pending(s));
    }

    [Fact]
    public void MissingCountIsNeverShownAsZero()
    {
        var s = AgentStatusReader.Parse("""
            {"schema":"pc-telemetry/agent-status/1","updated_at_utc":"2026-11-09T01:59:55Z","collecting":true,
             "upload":{"configured":true,"state":"ok","pending_batches":null}}
            """, Now);
        Assert.Null(s.PendingBatches);
        Assert.Equal("확인 불가", HostStatusText.Pending(s));
    }

    [Fact]
    public void UploadNotConfigured()
    {
        var s = AgentStatusReader.Parse("""
            {"schema":"pc-telemetry/agent-status/1","updated_at_utc":"2026-11-09T01:59:55Z","collecting":true,
             "upload":{"configured":false,"state":"not_configured","pending_batches":null}}
            """, Now);
        Assert.Equal("전송 미설정", HostStatusText.Pending(s));
        Assert.Equal("전송 미설정", HostStatusText.Upload(s));
    }

    [Fact]
    public void FileMissingAndUnreadable()
    {
        using var dir = new TempDir();
        Assert.Equal(StatusFreshness.Missing, AgentStatusReader.Read(Path.Combine(dir.Path, "none.json"), Now).Freshness);
        Assert.Equal("확인 불가(상태 파일 없음)", HostStatusText.Pending(AgentStatus.Of(StatusFreshness.Missing)));
    }

    [Theory]
    [InlineData(ServiceState.Running, "실행 중")]
    [InlineData(ServiceState.Stopped, "중지됨")]
    [InlineData(ServiceState.NotInstalled, "설치되지 않음")]
    [InlineData(ServiceState.Unknown, "확인 불가")]
    public void ServiceText(ServiceState state, string expected) => Assert.Equal(expected, HostStatusText.Service(state));

    [Theory]
    [InlineData("ConnectionAborted", 0, "연결 거부")]
    [InlineData("HostNameNotResolved", 0, "서버 주소를 찾을 수 없음")]
    [InlineData("CertificateExpired", 0, "인증서 오류")]
    [InlineData("Unknown", 0, "연결 실패")]
    [InlineData("Unknown", 502, "HTTP 502")]
    public void WebErrorText(string status, int http, string expected) =>
        Assert.Equal(expected, HostStatusText.WebError(status, http));

    [Fact]
    public void Summary()
    {
        var snap = new HostSnapshot(ServiceState.Stopped, AgentStatusReader.Parse(AgentJson, Now), ServerState.Unreachable, "ConnectionAborted");
        Assert.Equal("수집 서비스: 중지됨 · 서버: 연결 안 됨(ConnectionAborted) · 전송 대기: 12개 배치", HostStatusText.Summary(snap));
    }
}
