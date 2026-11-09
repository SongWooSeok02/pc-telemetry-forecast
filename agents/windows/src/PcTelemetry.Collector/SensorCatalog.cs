using System.Text.RegularExpressions;

namespace PcTelemetry.Collector;

/// <summary>
/// S1-02a에서 채택한 CPU·GPU 센서를 계약 논리 센서로 매핑한다(docs/sensor-support.md).
/// 원천 식별자를 sensor_id로 그대로 쓰지 않는다: LHM 식별자는 중복(/gpu-nvidia/0/load/3)·
/// 128자 초과(Intel iGPU)·드라이버 상태에 따른 증감이 관측됐다.
/// </summary>
public static partial class SensorCatalog
{
    internal sealed record Rule(
        string Slot,           // cpu | gpu
        string Suffix,         // sensor_id에서 component_id 뒤의 부분
        string Kind,
        string Scope,
        string[] HardwareTypes,
        string SensorType,
        Func<string, bool> NameMatch,
        Aggregation Aggregation,
        bool RequiresDriver,
        string? ScopeOverrideAmd = null);

    private static Func<string, bool> Names(params string[] names) =>
        n => names.Contains(n, StringComparer.Ordinal);

    [GeneratedRegex(@"^(?:[PE]-)?Core #\d+$")]
    private static partial Regex CoreClockName();

    private static readonly string[] CpuTypes = ["Cpu"];
    private static readonly string[] GpuTypes = ["GpuNvidia", "GpuAmd"];
    private static readonly string[] BoardTypes = ["Motherboard", "SuperIO", "EmbeddedController"];

    // 순서가 곧 구성 내 readings 순서다. 바꾸면 sensor_config가 새로 발급된다.
    internal static readonly Rule[] Rules =
    [
        // CPU: 온도·전력·클럭은 PawnIO(ring0) 접근이 있어야 값이 나온다(S1-02a 5.1/5.4).
        new("cpu", "package/temp", "temperature", "cpu_package", CpuTypes, "Temperature",
            Names("CPU Package", "Core (Tctl/Tdie)"), Aggregation.Single, true),
        new("cpu", "core-max/temp", "temperature", "cpu_core_max", CpuTypes, "Temperature",
            Names("Core Max"), Aggregation.Single, true),
        new("cpu", "package/power", "power", "cpu_package", CpuTypes, "Power",
            Names("CPU Package", "Package"), Aggregation.Single, true),
        // 코어 전력은 패키지 전력의 일부다. 패키지 전력과 합산하지 않는다.
        new("cpu", "cores/power", "power", "cpu_cores", CpuTypes, "Power",
            Names("CPU Cores"), Aggregation.Single, true),
        new("cpu", "total/load", "utilization", "cpu_total", CpuTypes, "Load",
            Names("CPU Total"), Aggregation.Single, false),
        new("cpu", "core-max/load", "utilization", "cpu_core_max", CpuTypes, "Load",
            Names("CPU Core Max"), Aggregation.Single, false),
        new("cpu", "core-max/clock", "clock", "cpu_core_max", CpuTypes, "Clock",
            n => CoreClockName().IsMatch(n), Aggregation.Max, true),
        new("cpu", "fan", "fan", "cpu_fan", BoardTypes, "Fan",
            Names("CPU Fan", "CPU"), Aggregation.Single, true),

        // 외장 GPU(NVIDIA는 실측, AMD는 미검증). Intel 내장 GPU는 후보로만 두고 수집하지 않는다.
        new("gpu", "core/temp", "temperature", "gpu_core", GpuTypes, "Temperature",
            Names("GPU Core"), Aggregation.Single, false),
        new("gpu", "hotspot/temp", "temperature", "gpu_hotspot", GpuTypes, "Temperature",
            Names("GPU Hot Spot"), Aggregation.Single, false),
        // NVIDIA "GPU Package"는 NVML 보드 전력이다. AMD "GPU Package"는 패키지 범위이므로 scope를 달리 둔다.
        new("gpu", "board/power", "power", "gpu_board", GpuTypes, "Power",
            Names("GPU Package"), Aggregation.Single, false, ScopeOverrideAmd: "gpu_package"),
        new("gpu", "core/load", "utilization", "gpu_core", GpuTypes, "Load",
            Names("GPU Core"), Aggregation.Single, false),
        new("gpu", "core/clock", "clock", "gpu_core", GpuTypes, "Clock",
            Names("GPU Core"), Aggregation.Single, false),
        new("gpu", "memory/clock", "clock", "gpu_memory", GpuTypes, "Clock",
            Names("GPU Memory"), Aggregation.Single, false),
        new("gpu", "fan", "fan", "gpu_fan", GpuTypes, "Fan",
            n => n.StartsWith("GPU Fan", StringComparison.Ordinal) || n == "GPU", Aggregation.Max, false),
    ];

    public static string UnitOf(string kind) => kind switch
    {
        "temperature" => "Cel",
        "power" => "W",
        "utilization" => "%",
        "clock" => "MHz",
        "fan" => "rpm",
        _ => throw new ArgumentOutOfRangeException(nameof(kind)),
    };

    [GeneratedRegex("^[a-z0-9._/-]{1,128}$")]
    public static partial Regex SensorIdPattern();

    /// <summary>
    /// ring0(PawnIO) 접근 여부. CPU 온도 중 하나라도 유한값이면 접근 가능으로 본다.
    /// 접근이 없으면 CPU 전력이 null이 아니라 0으로 나오므로(S1-02a 관찰 1) 전력값으로 판단하지 않는다.
    /// </summary>
    public static bool DriverAvailable(IReadOnlyList<RawHardware> hardware) =>
        hardware.Where(h => CpuTypes.Contains(h.Type) && h.UpdateError is null)
            .SelectMany(h => h.Sensors)
            .Any(s => s.Type == "Temperature" && s.Value is { } v && float.IsFinite(v)
                      && !s.Name.Contains("Distance to TjMax", StringComparison.Ordinal));

    /// <summary>LHM 식별자는 중복될 수 있어(GPU Bus·GPU Memory 모두 /gpu-nvidia/0/load/3) 이름까지 묶어 찾는다.</summary>
    public static string SourceKey(RawSensor s) => $"{s.Identifier}#{s.Type}#{s.Name}";

    public sealed record Discovery(IReadOnlyList<SensorDefinition> Definitions, IReadOnlyList<SensorBinding> Bindings);

    public static Discovery Discover(IReadOnlyList<RawHardware> hardware, bool driverAvailable,
        ComponentRegistry components, string sourceTag)
    {
        var definitions = new List<SensorDefinition>();
        var bindings = new List<SensorBinding>();

        var cpus = hardware.Where(h => CpuTypes.Contains(h.Type)).ToList();
        var gpus = hardware.Where(h => GpuTypes.Contains(h.Type)).ToList();
        var boards = hardware.Where(h => BoardTypes.Contains(h.Type)).ToList();

        // CPU 1개·외장 GPU 1개 이상을 슬롯으로 둔다. 없는 부품도 논리 센서를 등록하고 unsupported로 보고한다.
        AddSlot("cpu", cpus, i => i == 0 ? boards : []);
        AddSlot("gpu", gpus, _ => []);

        var dup = definitions.GroupBy(d => d.SensorId).FirstOrDefault(g => g.Count() > 1);
        if (dup is not null) throw new InvalidOperationException($"duplicate sensor_id {dup.Key}");
        return new Discovery(definitions, bindings);

        void AddSlot(string slot, List<RawHardware> parts, Func<int, List<RawHardware>> extra)
        {
            var count = Math.Max(1, parts.Count);
            for (var i = 0; i < count; i++)
            {
                var hw = i < parts.Count ? parts[i] : null;
                var fingerprint = hw is null ? ComponentRegistry.Absent : $"{hw.Type}|{hw.Name}";
                var componentId = components.Resolve($"{slot}{i}", fingerprint);
                foreach (var rule in Rules.Where(r => r.Slot == slot))
                {
                    var candidates = rule.HardwareTypes == BoardTypes ? extra(i) : (hw is null ? [] : [hw]);
                    AddRule(rule, componentId, candidates, hw);
                }
            }
        }

        void AddRule(Rule rule, string componentId, List<RawHardware> candidates, RawHardware? owner)
        {
            var sensorId = $"{componentId}/{rule.Suffix}";
            if (!SensorIdPattern().IsMatch(sensorId)) throw new InvalidOperationException($"invalid sensor_id {sensorId}");
            var unit = UnitOf(rule.Kind);
            var scope = owner?.Type == "GpuAmd" && rule.ScopeOverrideAmd is not null ? rule.ScopeOverrideAmd : rule.Scope;

            RawHardware? matchHw = null;
            List<RawSensor> matched = [];
            foreach (var c in candidates)
            {
                matched = c.Sensors.Where(s => s.Type == rule.SensorType && rule.NameMatch(s.Name)).ToList();
                if (rule.Aggregation == Aggregation.Single && matched.Count > 1)
                    matched = [matched[0]];
                if (matched.Count > 0) { matchHw = c; break; }
            }

            string? unsupported = null;
            if (owner is null) unsupported = "component_absent";
            else if (matchHw is null) unsupported = "sensor_not_exposed";
            else if (rule.RequiresDriver && !driverAvailable) unsupported = "driver_unavailable";

            var source = unsupported is null
                ? $"{sourceTag}:{matchHw!.Type}:{rule.SensorType}:{(rule.Aggregation == Aggregation.Max ? $"max({matched.Count}x{rule.Suffix.Split('/')[^1]})" : matched[0].Name)}"
                : $"{sourceTag}:none";
            // 계약 문자열은 출력 가능한 ASCII만 허용한다.
            source = new string(source.Select(c => c is >= ' ' and <= '~' ? c : '?').ToArray());
            if (source.Length > 200) source = source[..200];

            definitions.Add(new SensorDefinition(sensorId, componentId, rule.Kind, unit, source, scope,
                unsupported is null ? "supported" : "unsupported"));
            bindings.Add(new SensorBinding(sensorId, unit,
                matchHw?.Identifier ?? "",
                unsupported is null ? matched.Select(SourceKey).ToList() : [],
                rule.Aggregation,
                rule.RequiresDriver,
                unsupported));
        }
    }
}

/// <summary>
/// 하드웨어 경로(지문)→component_id 매핑을 영속화한다. 계약: 센서 교체에는 새 ID, ID 재활용 금지.
/// 같은 슬롯에 다른 부품이 들어오면 cpu0-r2처럼 새 component_id(따라서 새 sensor_id)를 발급한다.
/// </summary>
public sealed class ComponentRegistry
{
    public const string Absent = "absent";
    private const string FileName = "components.json";
    private readonly StateStore _store;
    private readonly Dictionary<string, List<Entry>> _slots;

    public ComponentRegistry(StateStore store)
    {
        _store = store;
        _slots = store.Load<Dictionary<string, List<Entry>>>(FileName) ?? new Dictionary<string, List<Entry>>();
    }

    public string Resolve(string slot, string fingerprint)
    {
        if (!_slots.TryGetValue(slot, out var entries))
        {
            entries = [];
            _slots[slot] = entries;
        }

        var known = entries.FirstOrDefault(e => e.Fingerprint == fingerprint);
        if (known is not null) return known.ComponentId;

        string id;
        if (fingerprint == Absent)
        {
            // 부품이 빠진 상태는 값을 보내지 않으므로(unsupported) 마지막 ID를 이어 쓴다.
            id = entries.Count > 0 ? entries[^1].ComponentId : slot;
        }
        else
        {
            var placeholder = entries.FirstOrDefault(e => e.Fingerprint == Absent);
            var realCount = entries.Count(e => e.Fingerprint != Absent);
            if (realCount == 0)
            {
                id = placeholder?.ComponentId ?? slot;
                if (placeholder is not null) entries.Remove(placeholder);
            }
            else
            {
                id = $"{slot}-r{realCount + 1}";
            }
        }
        entries.Add(new Entry(fingerprint, id));
        _store.Save(FileName, _slots);
        return id;
    }

    public sealed record Entry(string Fingerprint, string ComponentId);
}

/// <summary>
/// 불변 센서 구성 저장소. 같은 정의 집합은 같은 ID를 재사용하고, 달라지면 새 UUID를 발급한다.
/// 발급한 구성은 수정·삭제하지 않는다. 등록 요청 본문을 sensor-configs/에 남긴다(등록 API 합의 전).
/// </summary>
public sealed class SensorConfigStore
{
    private const string FileName = "sensor-configs.json";
    private readonly StateStore _store;
    private readonly List<SensorConfig> _configs;

    public SensorConfigStore(StateStore store)
    {
        _store = store;
        _configs = store.Load<List<SensorConfig>>(FileName) ?? [];
    }

    public IReadOnlyList<SensorConfig> All => _configs;

    public (SensorConfig Config, bool Created) GetOrCreate(IReadOnlyList<SensorDefinition> definitions)
    {
        var existing = _configs.FirstOrDefault(c => c.Sensors.SequenceEqual(definitions));
        if (existing is not null) return (existing, false);
        var config = new SensorConfig(Guid.NewGuid(), definitions.ToList());
        _configs.Add(config);
        _store.Save(FileName, _configs);
        StateStore.WriteAtomic(Path.Combine(_store.Directory, "sensor-configs", $"{config.Id:D}.json"),
            RegistrationBody(config));
        return (config, true);
    }

    /// <summary>backend/README의 등록 API 설계안 형식(schema_version + sensors). 정규 JSON으로 저장한다.</summary>
    public static string RegistrationBody(SensorConfig config)
    {
        var sensors = config.Sensors.ToDictionary(s => s.SensorId, s => new Dictionary<string, string>
        {
            ["component_id"] = s.ComponentId,
            ["kind"] = s.Kind,
            ["unit"] = s.Unit,
            ["source"] = s.Source,
            ["scope"] = s.Scope,
            ["support"] = s.Support,
        });
        var json = System.Text.Json.JsonSerializer.Serialize(new Dictionary<string, object>
        {
            ["schema_version"] = "0.1.0",
            ["sensor_config_id"] = config.Id.ToString("D"),
            ["sensors"] = sensors,
        });
        return Canonical.Normalize(json);
    }
}
