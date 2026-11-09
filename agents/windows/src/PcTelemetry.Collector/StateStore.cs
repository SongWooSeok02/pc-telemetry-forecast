using System.Text.Json;
using System.Text.Json.Serialization;

namespace PcTelemetry.Collector;

/// <summary>
/// 상태 디렉터리의 JSON 파일을 원자적으로 저장한다(임시 파일 기록·디스크 반영 후 교체).
/// S2-01에서 SQLite 버퍼로 옮길 때까지의 로컬 영속화 계층이다.
/// </summary>
public sealed class StateStore
{
    internal static readonly JsonSerializerOptions Json = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull,
    };

    public StateStore(string directory)
    {
        Directory = Path.GetFullPath(directory);
        System.IO.Directory.CreateDirectory(Directory);
    }

    public string Directory { get; }

    public string PathOf(string name) => Path.Combine(Directory, name);

    public T? Load<T>(string name) where T : class
    {
        var path = PathOf(name);
        if (!File.Exists(path)) return null;
        return JsonSerializer.Deserialize<T>(File.ReadAllText(path), Json);
    }

    public void Save<T>(string name, T value) => WriteAtomic(PathOf(name), JsonSerializer.Serialize(value, Json));

    public static void WriteAtomic(string path, string content)
    {
        var dir = Path.GetDirectoryName(path)!;
        System.IO.Directory.CreateDirectory(dir);
        var tmp = path + ".tmp";
        using (var fs = new FileStream(tmp, FileMode.Create, FileAccess.Write, FileShare.None, 4096, FileOptions.WriteThrough))
        using (var sw = new StreamWriter(fs, new System.Text.UTF8Encoding(false)))
        {
            sw.Write(content);
            sw.Flush();
            fs.Flush(flushToDisk: true);
        }
        File.Move(tmp, path, overwrite: true);
    }
}

/// <summary>
/// 계약: boot_session_id는 OS 부팅마다 새 UUID, 서비스 재시작에서는 유지.
/// sample_seq는 부팅 내 0부터 증가, 재사용 금지. 재시작 시 예약 상한부터 이어가므로 누락은 생겨도 재사용은 없다.
/// </summary>
public sealed class BootSession
{
    private const string FileName = "boot-session.json";
    private readonly StateStore _store;
    private readonly int _reserveBlock;
    private State _state;
    private long _next;

    private BootSession(StateStore store, State state, long next, int reserveBlock, bool isNewBoot, string? previousBootId)
    {
        _store = store;
        _state = state;
        _next = next;
        _reserveBlock = reserveBlock;
        IsNewBoot = isNewBoot;
        PreviousBootId = previousBootId;
    }

    public Guid Id => _state.BootSessionId;
    public string BootId => _state.BootId;
    public bool IsNewBoot { get; }
    public string? PreviousBootId { get; }
    public long NextSeq => _next;

    /// <exception cref="BootIdentityUnavailableException">OS 부팅을 식별할 수 없을 때. 수집을 유보한다.</exception>
    public static BootSession Open(StateStore store, IBootIdProvider boot, int reserveBlock = 600)
    {
        var bootId = boot.GetBootId() ?? throw new BootIdentityUnavailableException();
        var saved = store.Load<State>(FileName);
        if (saved is not null && saved.BootId == bootId)
        {
            // 같은 부팅의 서비스 재시작: 예약 상한까지는 이미 발급됐을 수 있으므로 그 다음부터 쓴다.
            var resumed = new BootSession(store, saved, saved.ReservedUntil, reserveBlock, false, saved.BootId);
            resumed.Reserve();
            return resumed;
        }
        var state = new State { BootId = bootId, BootSessionId = Guid.NewGuid(), ReservedUntil = 0 };
        var fresh = new BootSession(store, state, 0, reserveBlock, true, saved?.BootId);
        fresh.Reserve();
        return fresh;
    }

    public long Take()
    {
        if (_next >= _state.ReservedUntil) Reserve();
        return _next++;
    }

    private void Reserve()
    {
        _state = _state with { ReservedUntil = _next + _reserveBlock };
        _store.Save(FileName, _state);
    }

    private sealed record State
    {
        public required string BootId { get; init; }
        public required Guid BootSessionId { get; init; }
        public required long ReservedUntil { get; init; }
    }
}

public sealed class BootIdentityUnavailableException()
    : Exception("OS boot identity unavailable; collection is held (contract: boot_session_id)");
