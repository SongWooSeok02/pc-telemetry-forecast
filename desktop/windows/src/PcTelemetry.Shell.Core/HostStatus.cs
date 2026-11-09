using System.Globalization;
using System.Text.Json;

namespace PcTelemetry.Shell.Core;

public enum ServiceState { NotInstalled, Stopped, StartPending, StopPending, Running, Paused, Unknown }

public enum StatusFreshness { Fresh, Stale, Missing, Unreadable }

/// <summary>에이전트가 10초마다 쓰는 공개 상태 파일(pc-telemetry/agent-status/1)의 해석 결과.</summary>
public sealed record AgentStatus(
    StatusFreshness Freshness,
    DateTime? UpdatedAtUtc,
    bool? Collecting,
    bool? UploadConfigured,
    string? UploadState,
    int? PendingBatches,
    DateTime? LastSuccessUtc)
{
    public static AgentStatus Of(StatusFreshness freshness) => new(freshness, null, null, null, null, null, null);
}

public enum ServerState { Connecting, Connected, Unreachable }

public sealed record HostSnapshot(ServiceState Service, AgentStatus Agent, ServerState Server, string? ServerDetail);

public static class AgentStatusReader
{
    public const string Schema = "pc-telemetry/agent-status/1";

    /// <summary>에이전트는 10초마다 쓴다. 60초 넘게 갱신이 없으면 오래된 값으로 표시한다.</summary>
    public static readonly TimeSpan StaleAfter = TimeSpan.FromSeconds(60);

    public static string DefaultPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.CommonApplicationData), "PcTelemetry", "status", "agent-status.json");

    public static AgentStatus Read(string path, DateTime nowUtc)
    {
        string json;
        try
        {
            if (!File.Exists(path)) return AgentStatus.Of(StatusFreshness.Missing);
            json = File.ReadAllText(path);
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            return AgentStatus.Of(StatusFreshness.Unreadable);
        }
        return Parse(json, nowUtc);
    }

    public static AgentStatus Parse(string json, DateTime nowUtc)
    {
        try
        {
            using var doc = JsonDocument.Parse(json);
            var root = doc.RootElement;
            if (root.ValueKind != JsonValueKind.Object || Str(root, "schema") != Schema) return AgentStatus.Of(StatusFreshness.Unreadable);
            var updated = Time(root, "updated_at_utc");
            if (updated is null) return AgentStatus.Of(StatusFreshness.Unreadable);
            var upload = root.TryGetProperty("upload", out var u) && u.ValueKind == JsonValueKind.Object ? u : default;
            var fresh = nowUtc - updated.Value <= StaleAfter ? StatusFreshness.Fresh : StatusFreshness.Stale;
            return new AgentStatus(
                fresh,
                updated,
                Bool(root, "collecting"),
                upload.ValueKind == JsonValueKind.Object ? Bool(upload, "configured") : null,
                upload.ValueKind == JsonValueKind.Object ? Str(upload, "state") : null,
                upload.ValueKind == JsonValueKind.Object && upload.TryGetProperty("pending_batches", out var p)
                    && p.ValueKind == JsonValueKind.Number && p.TryGetInt32(out var n) && n >= 0 ? n : null,
                upload.ValueKind == JsonValueKind.Object ? Time(upload, "last_success_at_utc") : null);
        }
        catch (JsonException)
        {
            return AgentStatus.Of(StatusFreshness.Unreadable);
        }
    }

    private static string? Str(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind == JsonValueKind.String ? v.GetString() : null;

    private static bool? Bool(JsonElement e, string name) =>
        e.TryGetProperty(name, out var v) && v.ValueKind is JsonValueKind.True or JsonValueKind.False ? v.GetBoolean() : null;

    private static DateTime? Time(JsonElement e, string name) =>
        Str(e, name) is { } s && DateTime.TryParse(s, CultureInfo.InvariantCulture,
            DateTimeStyles.AdjustToUniversal | DateTimeStyles.AssumeUniversal, out var t) ? t : null;
}

/// <summary>상태 줄·오프라인 안내 문구. 확인하지 못한 값은 0이나 정상으로 표시하지 않는다.</summary>
public static class HostStatusText
{
    public static string Service(ServiceState state) => state switch
    {
        ServiceState.Running => "실행 중",
        ServiceState.Stopped => "중지됨",
        ServiceState.StartPending => "시작 중",
        ServiceState.StopPending => "중지 중",
        ServiceState.Paused => "일시 중지됨",
        ServiceState.NotInstalled => "설치되지 않음",
        _ => "확인 불가",
    };

    public static string Pending(AgentStatus agent) => agent.Freshness switch
    {
        StatusFreshness.Missing => "확인 불가(상태 파일 없음)",
        StatusFreshness.Unreadable => "확인 불가",
        _ when agent.UploadConfigured == false => "전송 미설정",
        _ when agent.PendingBatches is not { } n => "확인 불가",
        StatusFreshness.Stale => $"{agent.PendingBatches}개 배치(갱신 지연)",
        _ => $"{agent.PendingBatches}개 배치",
    };

    public static string Upload(AgentStatus agent) => agent.Freshness is StatusFreshness.Missing or StatusFreshness.Unreadable
        ? "확인 불가"
        : agent.UploadState switch
        {
            "ok" => "정상",
            "retrying" => "재시도 중",
            "paused_auth" => "인증 오류로 중지(재등록 필요)",
            "idle" => "대기",
            "not_configured" => "전송 미설정",
            _ => "확인 불가",
        };

    public static string Server(ServerState state, string? detail) => state switch
    {
        ServerState.Connected => "연결됨",
        ServerState.Connecting => "연결 중",
        _ => detail is null ? "연결 안 됨" : $"연결 안 됨({detail})",
    };

    /// <summary>서비스가 수집 중이 아닌데 상태 파일이 최신처럼 보이는 모순을 피하기 위해 서비스 상태를 우선한다.</summary>
    public static string Summary(HostSnapshot s) =>
        $"수집 서비스: {Service(s.Service)} · 서버: {Server(s.Server, s.ServerDetail)} · 전송 대기: {Pending(s.Agent)}";

    /// <summary>CoreWebView2WebErrorStatus 이름을 안내 문구로 바꾼다(SDK 비의존: 이름 문자열로 받는다).</summary>
    public static string WebError(string status, int httpStatus) =>
        httpStatus > 0 ? $"HTTP {httpStatus}" : status switch
        {
            "ConnectionAborted" or "ConnectionReset" or "CannotConnect" or "Disconnected" => "연결 거부",
            "HostNameNotResolved" => "서버 주소를 찾을 수 없음",
            "Timeout" => "응답 시간 초과",
            "ServerUnreachable" => "서버에 닿을 수 없음",
            "CertificateCommonNameIsIncorrect" or "CertificateExpired" or "ClientCertificateContainsErrors"
                or "CertificateRevoked" or "CertificateIsInvalid" => "인증서 오류",
            "ErrorHttpInvalidServerResponse" => "잘못된 서버 응답",
            _ => "연결 실패",
        };

    public static string Time(DateTime? utc) =>
        utc is { } t ? t.ToLocalTime().ToString("HH:mm:ss", CultureInfo.InvariantCulture) : "—";
}
