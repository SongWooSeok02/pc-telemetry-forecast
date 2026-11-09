using PcTelemetry.Collector;

namespace PcTelemetry.Collector.Tests;

/// <summary>
/// 가상 하드웨어. 이름·종류는 S1-02a 실측 LHM 0.9.6 출력의 형태를 따르지만 값은 합성값이다(README 13절).
/// </summary>
internal sealed class FakeSource : ISensorSource
{
    public Func<IReadOnlyList<RawHardware>> Next { get; set; } = () => Synthetic.Laptop();
    public Exception? Throw { get; set; }
    public string SourceTag => "lhm-0.9.6";

    public IReadOnlyList<RawHardware> Read()
    {
        if (Throw is not null) throw Throw;
        return Next();
    }
}

internal sealed class FakeClock : IClock
{
    public DateTime UtcNow { get; set; } = new(2026, 11, 4, 0, 0, 0, DateTimeKind.Utc);
    public long UnbiasedMs { get; set; } = 10_000;
    public long InterruptMs { get; set; } = 10_000;

    /// <summary>정상 1초 경과. 세 시계가 함께 간다.</summary>
    public void Tick(long ms = 1000) => Advance(ms, ms, ms);

    public void Advance(long wallMs, long unbiasedMs, long interruptMs)
    {
        UtcNow = UtcNow.AddMilliseconds(wallMs);
        UnbiasedMs += unbiasedMs;
        InterruptMs += interruptMs;
    }
}

internal sealed class FakeBoot(string? id = "bootid:38") : IBootIdProvider
{
    public string? Id { get; set; } = id;
    public string? GetBootId() => Id;
}

internal sealed class ListSink : IEventSink, IBatchSink
{
    public List<CollectionEvent> Events { get; } = [];
    public List<FrozenBatch> Batches { get; } = [];
    public void Emit(CollectionEvent e) => Events.Add(e);
    public void Accept(FrozenBatch batch) => Batches.Add(batch);
    public IEnumerable<CollectionEvent> OfType(string type) => Events.Where(e => e.Type == type);
}

internal sealed class TempDir : IDisposable
{
    public string Path { get; } = System.IO.Path.Combine(System.IO.Path.GetTempPath(), "pctf-test-" + Guid.NewGuid().ToString("N"));
    public void Dispose()
    {
        try { Directory.Delete(Path, recursive: true); } catch (IOException) { }
    }
}

internal static class Synthetic
{
    public const string IntelIgpuId =
        "/gpu-intel-integrated/%5C%5C%3F%5CPCI%23VEN_8086%26DEV_0000%26SUBSYS_00000000%26REV_00%230%260000000%260%260%23%7B00000000-0000-0000-0000-000000000000%7D";

    /// <param name="driver">PawnIO 접근 여부. false면 LHM처럼 CPU 온도·클럭은 null, CPU 전력은 0.</param>
    public static List<RawHardware> Laptop(bool driver = true, bool gpu = true, string gpuName = "NVIDIA GeForce RTX 0000 Laptop GPU",
        float? gpuTemp = 49f, string? gpuError = null, bool exposeDistance = true)
    {
        float? T(float v) => driver ? v : null;
        float? P(float v) => driver ? v : 0f;
        var cpuSensors = new List<RawSensor>
        {
            new("/intelcpu/0/load/0", "Load", "CPU Total", 13.37f),
            new("/intelcpu/0/load/1", "Load", "CPU Core Max", 71.43f),
            new("/intelcpu/0/load/4", "Load", "CPU Core #1", 2.86f),
            new("/intelcpu/0/temperature/0", "Temperature", "Core Max", T(78)),
            new("/intelcpu/0/temperature/1", "Temperature", "Core Average", T(63.5f)),
            new("/intelcpu/0/temperature/10", "Temperature", "P-Core #1", T(78)),
            new("/intelcpu/0/temperature/18", "Temperature", "CPU Package", T(71)),
            new("/intelcpu/0/clock/1", "Clock", "E-Core #1", T(990.968f)),
            new("/intelcpu/0/clock/9", "Clock", "P-Core #1", T(4558.453f)),
            new("/intelcpu/0/clock/10", "Clock", "P-Core #2", T(4459.356f)),
            new("/intelcpu/0/clock/0", "Clock", "Bus Speed", 99.097f),
            new("/intelcpu/0/power/0", "Power", "CPU Package", P(31.151f)),
            new("/intelcpu/0/power/1", "Power", "CPU Cores", P(23.665f)),
            new("/intelcpu/0/power/3", "Power", "CPU Memory", 0f),
            new("/intelcpu/0/power/4", "Power", "CPU Platform", 0f),
        };
        if (exposeDistance) cpuSensors.Add(new("/intelcpu/0/temperature/27", "Temperature", "P-Core #1 Distance to TjMax", T(32)));

        var list = new List<RawHardware>
        {
            new("/motherboard", "Motherboard", "Synthetic Board", []),
            new("/intelcpu/0", "Cpu", "Intel Core Synthetic", cpuSensors),
        };
        if (gpu)
        {
            list.Add(new RawHardware("/gpu-nvidia/0", "GpuNvidia", gpuName,
            [
                new("/gpu-nvidia/0/temperature/0", "Temperature", "GPU Core", gpuTemp),
                new("/gpu-nvidia/0/temperature/2", "Temperature", "GPU Hot Spot", 55.656f),
                new("/gpu-nvidia/0/clock/0", "Clock", "GPU Core", 645f),
                new("/gpu-nvidia/0/clock/4", "Clock", "GPU Memory", 810f),
                new("/gpu-nvidia/0/load/0", "Load", "GPU Core", 27f),
                new("/gpu-nvidia/0/load/3", "Load", "GPU Bus", 21f),
                new("/gpu-nvidia/0/load/3", "Load", "GPU Memory", 18.743f),
                new("/gpu-nvidia/0/power/0", "Power", "GPU Package", 8.294f),
            ], gpuError));
        }
        list.Add(new RawHardware(IntelIgpuId, "GpuIntel", "Intel(R) Arc(TM) Graphics",
        [
            new(IntelIgpuId + "/clock/0", "Clock", "GPU Core", 800f),
            new(IntelIgpuId + "/power/0", "Power", "GPU Power", 0.002f),
        ]));
        return list;
    }
}
