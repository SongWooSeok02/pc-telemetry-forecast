using System.Security.Cryptography;
using System.Text;
using System.Text.Json;
using System.Text.RegularExpressions;

namespace PcTelemetry.Collector;

/// <summary>
/// 계약 2절 ascii-json-v1 정규화. RFC 8785/JCS와 다르다.
/// 숫자는 원문 토큰을 십진 문자열로 다뤄 binary64 변환을 거치지 않는다.
/// </summary>
public static partial class Canonical
{
    public const string MaxSafeInteger = "9007199254740991";

    public static string Normalize(string json)
    {
        using var doc = JsonDocument.Parse(json, new JsonDocumentOptions
        {
            AllowTrailingCommas = false,
            CommentHandling = JsonCommentHandling.Disallow,
            MaxDepth = 64,
        });
        var sb = new StringBuilder(json.Length);
        Write(doc.RootElement, sb);
        return sb.ToString();
    }

    public static string Sha256Hex(string canonical) =>
        Convert.ToHexStringLower(SHA256.HashData(Encoding.ASCII.GetBytes(canonical)));

    private static void Write(JsonElement e, StringBuilder sb)
    {
        switch (e.ValueKind)
        {
            case JsonValueKind.Object:
                var props = e.EnumerateObject().ToList();
                var names = new HashSet<string>(StringComparer.Ordinal);
                foreach (var p in props)
                {
                    if (!names.Add(p.Name)) throw new FormatException($"duplicate key: {p.Name}");
                }
                props.Sort((a, b) => string.CompareOrdinal(a.Name, b.Name));
                sb.Append('{');
                for (var i = 0; i < props.Count; i++)
                {
                    if (i > 0) sb.Append(',');
                    WriteString(props[i].Name, sb);
                    sb.Append(':');
                    Write(props[i].Value, sb);
                }
                sb.Append('}');
                break;
            case JsonValueKind.Array:
                sb.Append('[');
                var first = true;
                foreach (var item in e.EnumerateArray())
                {
                    if (!first) sb.Append(',');
                    first = false;
                    Write(item, sb);
                }
                sb.Append(']');
                break;
            case JsonValueKind.String:
                WriteString(e.GetString()!, sb);
                break;
            case JsonValueKind.Number:
                sb.Append(NormalizeNumber(e.GetRawText()));
                break;
            case JsonValueKind.True:
                sb.Append("true");
                break;
            case JsonValueKind.False:
                sb.Append("false");
                break;
            case JsonValueKind.Null:
                sb.Append("null");
                break;
            default:
                throw new FormatException($"unsupported JSON kind {e.ValueKind}");
        }
    }

    private static void WriteString(string s, StringBuilder sb)
    {
        sb.Append('"');
        foreach (var c in s)
        {
            if (c < ' ' || c > '~') throw new FormatException("non-printable-ASCII character");
            if (c == '"') sb.Append("\\\"");
            else if (c == '\\') sb.Append("\\\\");
            else sb.Append(c);
        }
        sb.Append('"');
    }

    [GeneratedRegex(@"^(-?)(\d+)(?:\.(\d+))?(?:[eE]([+-]?\d+))?$")]
    private static partial Regex NumberPattern();

    /// <summary>고정 소수점 최소 표현(1e1→10, 1.00→1, -0→0). 32자·안전 정수·소수 6자리 상한.</summary>
    public static string NormalizeNumber(string raw)
    {
        if (raw.Length > 32) throw new FormatException("numeric length");
        var m = NumberPattern().Match(raw);
        if (!m.Success) throw new FormatException("number");
        var negative = m.Groups[1].Value == "-";
        var intPart = m.Groups[2].Value;
        var frac = m.Groups[3].Value;
        var exp = 0;
        if (m.Groups[4].Success)
        {
            if (!int.TryParse(m.Groups[4].Value, out exp) || Math.Abs(exp) > 400)
                throw new FormatException("exponent");
        }

        var digits = intPart + frac;
        var point = intPart.Length + exp;
        if (point <= 0)
        {
            digits = new string('0', 1 - point) + digits;
            point = 1;
        }
        if (point > digits.Length) digits += new string('0', point - digits.Length);

        var ip = digits[..point].TrimStart('0');
        if (ip.Length == 0) ip = "0";
        var fp = digits[point..].TrimEnd('0');
        if (fp.Length > 6) throw new FormatException("fraction digits");
        if (ip.Length > MaxSafeInteger.Length
            || (ip.Length == MaxSafeInteger.Length && string.CompareOrdinal(ip, MaxSafeInteger) > 0)
            || (ip == MaxSafeInteger && fp.Length > 0))
            throw new FormatException("magnitude");

        var isZero = ip == "0" && fp.Length == 0;
        return (negative && !isZero ? "-" : "") + ip + (fp.Length > 0 ? "." + fp : "");
    }
}

/// <summary>ID 발급과 함께 본문 전체를 동결한 배치. Body는 정규 본문이며 그대로 전송한다.</summary>
public sealed record FrozenBatch(
    Guid BatchId,
    Guid BootSessionId,
    Guid SensorConfigId,
    long FirstSeq,
    long LastSeq,
    int SampleCount,
    string Body,
    string Sha256);

public static class BatchFactory
{
    public const int MaxBodyBytes = 1024 * 1024;
    public const int MaxSamples = 3600;
    public const decimal MaxAbsValue = 1_000_000_000_000m;

    /// <summary>
    /// 크기 상한에 맞춰 ID 발급 전에 분할한 뒤 각 배치를 동결한다.
    /// 반환된 배치만 "발급"된 것으로 보고, 호출자가 영속화한다.
    /// </summary>
    public static IReadOnlyList<FrozenBatch> Freeze(Guid machineId, Guid bootSessionId, Guid configId,
        IReadOnlyList<Sample> samples, Func<Guid>? newId = null)
    {
        if (samples.Count == 0) return [];
        newId ??= Guid.NewGuid;
        var result = new List<FrozenBatch>();
        Split(samples, 0, samples.Count);
        return result;

        void Split(IReadOnlyList<Sample> all, int start, int count)
        {
            if (count > MaxSamples)
            {
                Split(all, start, MaxSamples);
                Split(all, start + MaxSamples, count - MaxSamples);
                return;
            }
            var part = all.Skip(start).Take(count).ToList();
            // 크기 판정용 본문은 ID 자리 길이가 같으므로 빈 GUID로 만든다. 실제 ID는 상한 통과 후에만 발급한다.
            var probe = Build(machineId, bootSessionId, Guid.Empty, configId, part);
            if (Encoding.UTF8.GetByteCount(probe) > MaxBodyBytes)
            {
                if (count == 1) throw new InvalidOperationException("single sample exceeds 1MiB");
                Split(all, start, count / 2);
                Split(all, start + count / 2, count - count / 2);
                return;
            }
            var id = newId();
            var body = Build(machineId, bootSessionId, id, configId, part);
            result.Add(new FrozenBatch(id, bootSessionId, configId, part[0].Seq, part[^1].Seq, part.Count,
                body, Canonical.Sha256Hex(body)));
        }
    }

    public static string Build(Guid machineId, Guid bootSessionId, Guid batchId, Guid configId,
        IReadOnlyList<Sample> samples)
    {
        var buffer = new System.Buffers.ArrayBufferWriter<byte>();
        using (var w = new Utf8JsonWriter(buffer))
        {
            w.WriteStartObject();
            w.WriteString("schema_version", "0.1.0");
            w.WriteString("machine_id", machineId.ToString("D"));
            w.WriteString("boot_session_id", bootSessionId.ToString("D"));
            w.WriteString("batch_id", batchId.ToString("D"));
            w.WriteString("sensor_config_id", configId.ToString("D"));
            w.WriteStartArray("samples");
            long previous = -1;
            foreach (var s in samples)
            {
                if (s.Seq <= previous) throw new InvalidOperationException("sample_seq must increase");
                previous = s.Seq;
                w.WriteStartObject();
                w.WriteNumber("sample_seq", s.Seq);
                w.WriteString("observed_at", Utc.Format(s.ObservedAtUtc));
                w.WriteNumber("monotonic_ms", s.MonotonicMs);
                w.WriteStartArray("readings");
                foreach (var r in s.Readings)
                {
                    w.WriteStartObject();
                    w.WriteString("sensor_id", r.SensorId);
                    w.WriteString("unit", r.Unit);
                    w.WriteString("quality", r.Quality.ToContract());
                    if (r.Quality == Quality.Ok)
                    {
                        if (r.Value is not { } v || Math.Abs(v) > MaxAbsValue || r.Reason is not null)
                            throw new InvalidOperationException($"invalid ok reading {r.SensorId}");
                        w.WriteNumber("value", v);
                    }
                    else
                    {
                        // 미지원·결측·오류는 0이 아니라 null로 보낸다(계약 1절).
                        if (r.Value is not null) throw new InvalidOperationException($"non-ok reading with value {r.SensorId}");
                        w.WriteNull("value");
                        if (r.Reason is not null) w.WriteString("reason", r.Reason);
                    }
                    w.WriteEndObject();
                }
                w.WriteEndArray();
                w.WriteEndObject();
            }
            w.WriteEndArray();
            w.WriteEndObject();
        }
        return Canonical.Normalize(Encoding.UTF8.GetString(buffer.WrittenSpan));
    }
}
