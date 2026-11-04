// S1-02a 스파이크용 센서 탐색 도구. 운영 에이전트 코드가 아니다.
//
//   SensorProbe once  --out <file.json>
//       센서를 한 번 읽어 장비·센서 목록과 현재값을 기록한다.
//   SensorProbe run   --out <file.json> [--seconds 600] [--interval-ms 1000]
//       주기적으로 읽어 센서별 ok/null/error 횟수와 최소·최대값을 기록한다.
//   SensorProbe service --out-dir <dir> [--interval-ms 1000]
//       Windows 서비스로 실행되며 60초마다 run 결과 파일을 갱신한다.

using System.Security.Principal;
using System.Text.Json;
using LibreHardwareMonitor.Hardware;
using Microsoft.Win32;

namespace SensorProbe;

internal static class Program
{
    private static readonly JsonSerializerOptions JsonOptions = new() { WriteIndented = true };

    private static int Main(string[] args)
    {
        if (args.Length == 0)
        {
            Console.Error.WriteLine("usage: SensorProbe once|run|service [options]");
            return 2;
        }

        var options = ParseOptions(args.Skip(1).ToArray());
        try
        {
            return args[0] switch
            {
                "once" => RunOnce(Require(options, "out")),
                "run" => RunLoop(
                    Require(options, "out"),
                    TimeSpan.FromSeconds(GetInt(options, "seconds", 600)),
                    GetInt(options, "interval-ms", 1000),
                    CancellationToken.None),
                "service" => ServiceHost.Run(
                    Require(options, "out-dir"),
                    GetInt(options, "interval-ms", 1000)),
                _ => throw new ArgumentException($"unknown command: {args[0]}"),
            };
        }
        catch (ArgumentException ex)
        {
            Console.Error.WriteLine(ex.Message);
            return 2;
        }
    }

    internal static int RunOnce(string outPath)
    {
        var report = NewReport("once");
        var computer = OpenComputer(report);
        try
        {
            var hardware = new List<object>();
            foreach (var hw in AllHardware(computer))
            {
                hw.Update();
                hardware.Add(new
                {
                    identifier = hw.Identifier.ToString(),
                    type = hw.HardwareType.ToString(),
                    name = hw.Name,
                    sensors = hw.Sensors.Select(s => new
                    {
                        identifier = s.Identifier.ToString(),
                        index = s.Index,
                        name = s.Name,
                        type = s.SensorType.ToString(),
                        contract_unit = ContractUnit(s.SensorType),
                        value = Finite(s.Value),
                    }).ToList(),
                });
            }
            report["hardware"] = hardware;
            var ids = AllHardware(computer).SelectMany(h => h.Sensors).Select(s => s.Identifier.ToString()).ToList();
            report["sensor_count"] = ids.Count;
            // 계약의 sensor_id는 장비 내 유일해야 하므로 LHM 식별자 중복을 따로 기록한다.
            report["duplicate_identifiers"] = ids.GroupBy(i => i).Where(g => g.Count() > 1)
                .ToDictionary(g => g.Key, g => g.Count());
        }
        finally
        {
            computer.Close();
        }

        Write(outPath, report);
        Console.WriteLine($"wrote {outPath}");
        return 0;
    }

    internal static int RunLoop(string outPath, TimeSpan duration, int intervalMs, CancellationToken token,
        TimeSpan? flushEvery = null)
    {
        var report = NewReport("run");
        report["interval_ms"] = intervalMs;
        var stats = new SortedDictionary<string, SensorStats>(StringComparer.Ordinal);
        var computer = OpenComputer(report);
        var started = DateTime.UtcNow;
        var lastFlush = DateTime.UtcNow;
        long cycles = 0, cycleErrors = 0;
        var overruns = 0;

        try
        {
            using var timer = new PeriodicTimer(TimeSpan.FromMilliseconds(intervalMs));
            while (!token.IsCancellationRequested && DateTime.UtcNow - started < duration)
            {
                var cycleStart = Environment.TickCount64;
                try
                {
                    foreach (var hw in AllHardware(computer))
                    {
                        hw.Update();
                        foreach (var s in hw.Sensors)
                        {
                            var key = s.Identifier.ToString();
                            if (!stats.TryGetValue(key, out var st))
                            {
                                st = new SensorStats(s.Name, s.SensorType.ToString(), ContractUnit(s.SensorType), hw.Name);
                                stats[key] = st;
                            }
                            st.Add(Finite(s.Value));
                        }
                    }
                }
                catch (Exception ex)
                {
                    cycleErrors++;
                    report["last_cycle_error"] = $"{ex.GetType().Name}: {ex.Message}";
                }
                cycles++;
                if (Environment.TickCount64 - cycleStart > intervalMs) overruns++;

                if (flushEvery is { } every && DateTime.UtcNow - lastFlush >= every)
                {
                    Flush(outPath, report, stats, started, cycles, cycleErrors, overruns);
                    lastFlush = DateTime.UtcNow;
                }

                try
                {
                    if (!timer.WaitForNextTickAsync(token).AsTask().GetAwaiter().GetResult()) break;
                }
                catch (OperationCanceledException)
                {
                    break;
                }
            }
        }
        finally
        {
            computer.Close();
        }

        Flush(outPath, report, stats, started, cycles, cycleErrors, overruns);
        Console.WriteLine($"wrote {outPath} ({cycles} cycles)");
        return 0;
    }

    private static void Flush(string outPath, Dictionary<string, object?> report,
        SortedDictionary<string, SensorStats> stats, DateTime started, long cycles, long cycleErrors, int overruns)
    {
        report["started_at_utc"] = Utc(started);
        report["finished_at_utc"] = Utc(DateTime.UtcNow);
        report["cycles"] = cycles;
        report["cycle_errors"] = cycleErrors;
        report["cycle_overruns"] = overruns;
        report["sensors"] = stats.ToDictionary(kv => kv.Key, kv => kv.Value.ToJson());
        Write(outPath, report);
    }

    private static Computer OpenComputer(Dictionary<string, object?> report)
    {
        var computer = new Computer
        {
            IsCpuEnabled = true,
            IsGpuEnabled = true,
            IsMotherboardEnabled = true,
            IsControllerEnabled = true,
            IsMemoryEnabled = false,
            IsStorageEnabled = false,
            IsNetworkEnabled = false,
            IsBatteryEnabled = false,
            IsPsuEnabled = false,
        };
        var opened = DateTime.UtcNow;
        try
        {
            computer.Open();
            report["open_error"] = null;
        }
        catch (Exception ex)
        {
            report["open_error"] = $"{ex.GetType().Name}: {ex.Message}";
        }
        report["open_ms"] = (DateTime.UtcNow - opened).TotalMilliseconds;
        return computer;
    }

    private static IEnumerable<IHardware> AllHardware(Computer computer)
    {
        foreach (var hw in computer.Hardware)
        {
            yield return hw;
            foreach (var sub in hw.SubHardware) yield return sub;
        }
    }

    private static Dictionary<string, object?> NewReport(string mode)
    {
        using var identity = WindowsIdentity.GetCurrent();
        var principal = new WindowsPrincipal(identity);
        return new Dictionary<string, object?>
        {
            ["task"] = "S1-02a",
            ["mode"] = mode,
            ["probe_version"] = typeof(Program).Assembly.GetName().Version?.ToString(),
            ["lhm_version"] = typeof(Computer).Assembly.GetName().Version?.ToString(),
            ["executed_at_utc"] = Utc(DateTime.UtcNow),
            ["os_version"] = Environment.OSVersion.VersionString,
            ["account"] = identity.Name,
            ["is_elevated"] = principal.IsInRole(WindowsBuiltInRole.Administrator),
            ["is_system"] = identity.IsSystem,
            ["pawnio"] = PawnIoState(),
        };
    }

    private static object PawnIoState()
    {
        using var key = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO");
        return new
        {
            installed = key is not null,
            display_version = key?.GetValue("DisplayVersion") as string,
        };
    }

    // S1-01 계약 단위. 계약에 없는 센서 종류는 null로 두고 보고서에서 수집 대상 여부를 정한다.
    private static string? ContractUnit(SensorType type) => type switch
    {
        SensorType.Temperature => "Cel",
        SensorType.Power => "W",
        SensorType.Load => "%",
        SensorType.Clock => "MHz",
        SensorType.Fan => "rpm",
        _ => null,
    };

    private static double? Finite(float? value) =>
        value is { } v && float.IsFinite(v) ? Math.Round(v, 3) : null;

    private static string Utc(DateTime t) => t.ToUniversalTime().ToString("yyyy-MM-ddTHH:mm:ss.ffffffZ");

    private static void Write(string path, object report)
    {
        var dir = Path.GetDirectoryName(Path.GetFullPath(path));
        if (dir is not null) Directory.CreateDirectory(dir);
        var tmp = path + ".tmp";
        File.WriteAllText(tmp, JsonSerializer.Serialize(report, JsonOptions));
        File.Move(tmp, path, overwrite: true);
    }

    private static Dictionary<string, string> ParseOptions(string[] args)
    {
        var result = new Dictionary<string, string>(StringComparer.Ordinal);
        for (var i = 0; i < args.Length; i++)
        {
            if (!args[i].StartsWith("--", StringComparison.Ordinal) || i + 1 >= args.Length)
                throw new ArgumentException($"bad option: {args[i]}");
            result[args[i][2..]] = args[++i];
        }
        return result;
    }

    private static string Require(Dictionary<string, string> options, string name) =>
        options.TryGetValue(name, out var v) ? v : throw new ArgumentException($"missing --{name}");

    private static int GetInt(Dictionary<string, string> options, string name, int fallback) =>
        options.TryGetValue(name, out var v) ? int.Parse(v) : fallback;

    private sealed class SensorStats(string name, string type, string? unit, string hardware)
    {
        private long _ok, _null;
        private double _min = double.MaxValue, _max = double.MinValue;
        private double? _last;

        public void Add(double? value)
        {
            _last = value;
            if (value is not { } v)
            {
                _null++;
                return;
            }
            _ok++;
            _min = Math.Min(_min, v);
            _max = Math.Max(_max, v);
        }

        public object ToJson() => new
        {
            hardware,
            name,
            type,
            contract_unit = unit,
            ok = _ok,
            @null = _null,
            min = _ok > 0 ? _min : (double?)null,
            max = _ok > 0 ? _max : (double?)null,
            last = _last,
        };
    }
}
