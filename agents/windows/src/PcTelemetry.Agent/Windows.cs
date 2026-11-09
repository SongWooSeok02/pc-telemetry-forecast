using System.Runtime.InteropServices;
using System.Security.Principal;
using LibreHardwareMonitor.Hardware;
using Microsoft.Win32;
using PcTelemetry.Collector;

namespace PcTelemetry.Agent;

/// <summary>계약 monotonic_ms 소스. QueryUnbiasedInterruptTime(절전 제외)과 QueryInterruptTime(절전 포함).</summary>
internal sealed partial class WindowsClock : IClock
{
    [LibraryImport("kernel32.dll")]
    [return: MarshalAs(UnmanagedType.Bool)]
    private static partial bool QueryUnbiasedInterruptTime(out ulong unbiasedTime);

    [LibraryImport("api-ms-win-core-realtime-l1-1-1.dll")]
    private static partial void QueryInterruptTime(out ulong interruptTime);

    public DateTime UtcNow => DateTime.UtcNow;

    public long UnbiasedMs
    {
        get
        {
            if (!QueryUnbiasedInterruptTime(out var t)) throw new InvalidOperationException("QueryUnbiasedInterruptTime failed");
            return (long)(t / 10000);
        }
    }

    public long InterruptMs
    {
        get
        {
            QueryInterruptTime(out var t);
            return (long)(t / 10000);
        }
    }
}

/// <summary>
/// HKLM\...\PrefetchParameters\BootId: 부팅마다 증가하는 OS 카운터. 서비스 재시작에는 바뀌지 않는다.
/// 읽지 못하면 null을 돌려 수집을 유보한다(계약 1절 boot_session_id).
/// </summary>
internal sealed class RegistryBootId : IBootIdProvider
{
    public string? GetBootId()
    {
        try
        {
            using var key = Registry.LocalMachine.OpenSubKey(
                @"SYSTEM\CurrentControlSet\Control\Session Manager\Memory Management\PrefetchParameters");
            return key?.GetValue("BootId") is int id ? $"bootid:{(uint)id}" : null;
        }
        catch (Exception)
        {
            return null;
        }
    }
}

/// <summary>LibreHardwareMonitorLib 0.9.6 어댑터. CPU·GPU·메인보드·컨트롤러만 연다(S1-02a 범위).</summary>
internal sealed class LhmSource : ISensorSource, IDisposable
{
    private readonly Computer _computer;

    public LhmSource()
    {
        _computer = new Computer
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
        var started = Environment.TickCount64;
        _computer.Open();
        OpenMs = Environment.TickCount64 - started;
        var v = typeof(Computer).Assembly.GetName().Version!;
        SourceTag = $"lhm-{v.Major}.{v.Minor}.{v.Build}";
    }

    public string SourceTag { get; }
    public long OpenMs { get; }

    public IReadOnlyList<RawHardware> Read()
    {
        var result = new List<RawHardware>();
        foreach (var hw in _computer.Hardware)
        {
            Add(hw, result);
            foreach (var sub in hw.SubHardware) Add(sub, result);
        }
        return result;
    }

    private static void Add(IHardware hw, List<RawHardware> result)
    {
        string? error = null;
        try
        {
            hw.Update();
        }
        catch (Exception ex)
        {
            // 한 장치의 갱신 실패는 그 장치의 센서만 error로 만든다.
            error = ex.GetType().Name;
        }
        result.Add(new RawHardware(
            hw.Identifier.ToString(),
            hw.HardwareType.ToString(),
            hw.Name,
            hw.Sensors.Select(s => new RawSensor(s.Identifier.ToString(), s.SensorType.ToString(), s.Name, s.Value)).ToList(),
            error));
    }

    public void Dispose() => _computer.Close();
}

internal static class HostInfo
{
    public static Dictionary<string, object?> Describe()
    {
        using var identity = WindowsIdentity.GetCurrent();
        using var pawn = Registry.LocalMachine.OpenSubKey(@"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall\PawnIO");
        return new Dictionary<string, object?>
        {
            ["os_version"] = Environment.OSVersion.VersionString,
            ["is_elevated"] = new WindowsPrincipal(identity).IsInRole(WindowsBuiltInRole.Administrator),
            ["is_system"] = identity.IsSystem,
            ["pawnio_installed"] = pawn is not null,
            ["pawnio_version"] = pawn?.GetValue("DisplayVersion") as string,
            ["agent_version"] = typeof(HostInfo).Assembly.GetName().Version?.ToString(),
            ["lhm_version"] = typeof(Computer).Assembly.GetName().Version?.ToString(),
        };
    }
}
