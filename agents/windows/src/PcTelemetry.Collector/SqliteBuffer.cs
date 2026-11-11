using System.Globalization;
using System.Text.Json;
using Microsoft.Data.Sqlite;

namespace PcTelemetry.Collector;

/// <summary>수집 루프가 샘플을 넘기는 곳. 메모리 배처(<see cref="Batcher"/>)와 SQLite 버퍼가 구현한다.</summary>
public interface ISampleBatcher
{
    void Add(Guid bootSessionId, Guid configId, Sample sample);

    /// <summary>모아 둔 샘플을 지금 동결한다(구성 변경·절전 재개·종료).</summary>
    void Flush();
}

public sealed record BufferOptions
{
    /// <summary>README 5절: 로컬 미전송 버퍼는 24시간 또는 500MB 중 먼저 도달하는 상한.</summary>
    public TimeSpan MaxAge { get; init; } = TimeSpan.FromHours(24);

    /// <summary>미전송 데이터(대기·격리 배치 본문과 미동결 샘플)의 바이트 합 상한.</summary>
    public long MaxBytes { get; init; } = 500L * 1024 * 1024;

    /// <summary>전송 완료 배치는 본문을 지우고 원장 행만 이 기간 보관한다(서버 원장 대조용).</summary>
    public TimeSpan SentRetention { get; init; } = TimeSpan.FromHours(24);

    /// <summary>누락 범위 기록 보관 기간.</summary>
    public TimeSpan GapRetention { get; init; } = TimeSpan.FromDays(30);

    public TimeSpan PruneInterval { get; init; } = TimeSpan.FromMinutes(1);

    /// <summary>1초 수집·3초 전송(README).</summary>
    public int SamplesPerBatch { get; init; } = 3;
}

public sealed record PendingBatch(Guid BatchId, string Body, string Sha256, int SampleCount, int Attempts, DateTime FrozenAtUtc);

public sealed record BufferGap(
    string Reason,
    string State,
    Guid BootSessionId,
    long FirstSeq,
    long LastSeq,
    DateTime FirstObservedUtc,
    DateTime LastObservedUtc,
    int Batches,
    int Samples,
    long Bytes,
    DateTime RecordedAtUtc);

public sealed record BufferStats(
    int PendingBatches,
    int QuarantinedBatches,
    int OpenSamples,
    long UnsentBytes,
    DateTime? OldestUnsentUtc,
    int SentLedgerBatches,
    long DroppedSamplesTotal,
    BufferGap? LastGap,
    long FileBytes);

/// <summary>
/// S2-01 SQLite 미전송 버퍼. 계약(S1-01 2절, README 15.3)의 에이전트 쪽 불변 조건을 지킨다.
/// <list type="bullet">
/// <item>샘플은 수집 즉시 영속화하고, batch_id 발급과 본문 동결·샘플 제거를 한 트랜잭션으로 커밋한다.</item>
/// <item>동결된 ID·본문·해시는 트리거로 수정을 막는다. 본문은 전송 완료(sent) 때만 지운다.</item>
/// <item>재시도는 같은 행을 그대로 다시 읽어 보낸다. 일부 샘플로 새 배치를 만들 경로가 없다.</item>
/// <item>상한(기본 24시간/500MB) 초과 시 오래된 미전송 배치부터 지우고 누락 범위를 gaps 표·이벤트·상태에 남긴다.</item>
/// </list>
/// 프로세스가 강제 종료돼도 커밋된 샘플·배치는 남고, 다음 시작 때 미동결 샘플을 동결해 이어 보낸다.
/// </summary>
public sealed class SqliteBuffer : ISampleBatcher, IDisposable
{
    public const string FileName = "buffer.sqlite3";
    private const int SchemaVersion = 1;
    private const string TimeFormat = "yyyy-MM-dd'T'HH:mm:ss.ffffff'Z'";

    private readonly object _gate = new();
    private readonly SqliteConnection _db;
    private readonly Guid _machineId;
    private readonly IEventSink _events;
    private readonly Func<DateTime> _now;
    private readonly BufferOptions _options;
    private (Guid Boot, Guid Config)? _key;
    private int _openForKey;
    private DateTime _lastPrune = DateTime.MinValue;

    // 업로더가 보내는 중인 배치. 상한 정리가 이 배치를 지우면 서버에 들어간 데이터를 누락으로 잘못 기록할 수 있다.
    private Guid? _inFlight;

    private SqliteBuffer(SqliteConnection db, string path, Guid machineId, IEventSink events, BufferOptions options, Func<DateTime> now)
    {
        _db = db;
        Path = path;
        _machineId = machineId;
        _events = events;
        _options = options;
        _now = now;
    }

    public string Path { get; }
    public BufferOptions Options => _options;

    /// <summary>
    /// 버퍼를 열고 재시작 복원을 한다: 이전 프로세스가 남긴 미동결 샘플을 동결하고, 다른 machine_id의 대기 배치를 격리한다.
    /// 파일이 손상돼 열 수 없으면 옆으로 옮겨 보존하고 새로 만든다(buffer_reset 이벤트).
    /// </summary>
    public static SqliteBuffer Open(string path, Guid machineId, IEventSink events, BufferOptions? options = null, Func<DateTime>? now = null)
    {
        path = System.IO.Path.GetFullPath(path);
        Directory.CreateDirectory(System.IO.Path.GetDirectoryName(path)!);
        options ??= new BufferOptions();
        var clock = now ?? (() => DateTime.UtcNow);
        SqliteConnection db;
        try
        {
            db = Connect(path);
        }
        catch (SqliteException ex) when (ex.SqliteErrorCode is 11 or 26)   // SQLITE_CORRUPT, SQLITE_NOTADB
        {
            var kept = path + ".corrupt-" + clock().ToString("yyyyMMdd'T'HHmmss'Z'", CultureInfo.InvariantCulture);
            foreach (var suffix in new[] { "", "-wal", "-shm" })
            {
                if (File.Exists(path + suffix)) File.Move(path + suffix, kept + suffix, overwrite: true);
            }
            events.Emit(Event(clock(), "buffer_reset", new()
            {
                ["reason"] = ex.SqliteErrorCode == 11 ? "corrupt" : "not_a_database",
                ["kept_as"] = System.IO.Path.GetFileName(kept),
            }));
            db = Connect(path);
        }
        var buffer = new SqliteBuffer(db, path, machineId, events, options, clock);
        buffer.Restore();
        return buffer;
    }

    private static SqliteConnection Connect(string path)
    {
        // 풀링을 끄면 Dispose 후 파일 핸들이 남지 않는다(손상 파일 이동·테스트 정리).
        var db = new SqliteConnection(new SqliteConnectionStringBuilder
        {
            DataSource = path,
            Mode = SqliteOpenMode.ReadWriteCreate,
            Pooling = false,
        }.ToString());
        try
        {
            db.Open();
            Exec(db, "PRAGMA busy_timeout=5000;");
            var version = Convert.ToInt32(Scalar(db, "PRAGMA user_version;"), CultureInfo.InvariantCulture);
            if (version > SchemaVersion)
                throw new InvalidOperationException($"buffer schema {version} is newer than this agent ({SchemaVersion})");
            if (version == 0)
            {
                // auto_vacuum은 표를 만들기 전에 정해야 한다. 상한 제거 후 파일을 줄이는 데 쓴다.
                Exec(db, "PRAGMA auto_vacuum=INCREMENTAL;");
            }
            Exec(db, "PRAGMA journal_mode=WAL;");
            Exec(db, "PRAGMA synchronous=FULL;");
            // 손상 파일은 여기서 SQLITE_NOTADB/CORRUPT로 드러난다.
            var check = (string)Scalar(db, "PRAGMA quick_check;")!;
            if (check != "ok") throw new SqliteException("quick_check: " + check, 11);
            if (version == 0) CreateSchema(db);
            return db;
        }
        catch
        {
            db.Dispose();
            throw;
        }
    }

    private static void CreateSchema(SqliteConnection db)
    {
        using var tx = db.BeginTransaction();
        Exec(db, """
            CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT NOT NULL) WITHOUT ROWID;

            -- 아직 배치로 동결하지 않은 샘플. 동결과 같은 트랜잭션에서 지운다.
            CREATE TABLE open_samples(
                boot_session_id TEXT NOT NULL,
                sample_seq INTEGER NOT NULL,
                machine_id TEXT NOT NULL,
                sensor_config_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                body TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                enqueued_at TEXT NOT NULL,
                PRIMARY KEY(boot_session_id, sample_seq));

            -- 동결 배치와 전송 상태. seq 순서가 동결 순서이자 전송 순서다.
            CREATE TABLE batches(
                seq INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id TEXT NOT NULL UNIQUE,
                machine_id TEXT NOT NULL,
                boot_session_id TEXT NOT NULL,
                sensor_config_id TEXT NOT NULL,
                first_seq INTEGER NOT NULL,
                last_seq INTEGER NOT NULL,
                sample_count INTEGER NOT NULL,
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                body TEXT,
                sha256 TEXT NOT NULL,
                bytes INTEGER NOT NULL,
                frozen_at TEXT NOT NULL,
                state TEXT NOT NULL CHECK(state IN ('pending', 'sent', 'quarantined')),
                attempts INTEGER NOT NULL DEFAULT 0,
                last_attempt_at TEXT,
                last_status INTEGER,
                last_code TEXT,
                sent_at TEXT,
                server_received_at TEXT,
                quarantine_reason TEXT,
                CHECK(state = 'sent' OR body IS NOT NULL));
            CREATE INDEX batches_state ON batches(state, seq);
            CREATE INDEX batches_range ON batches(boot_session_id, first_seq, last_seq);

            -- 동결 내용은 바꿀 수 없다. 본문은 전송 완료 때 지우는 것만 허용한다.
            CREATE TRIGGER batches_frozen BEFORE UPDATE OF batch_id, machine_id, boot_session_id, sensor_config_id,
                first_seq, last_seq, sample_count, first_observed_at, last_observed_at, sha256, bytes, frozen_at ON batches
            BEGIN SELECT RAISE(ABORT, 'frozen batch is immutable'); END;
            CREATE TRIGGER batches_body BEFORE UPDATE OF body ON batches
                WHEN NEW.body IS NOT OLD.body AND NOT (NEW.body IS NULL AND NEW.state = 'sent')
            BEGIN SELECT RAISE(ABORT, 'frozen batch is immutable'); END;
            CREATE TRIGGER batches_sent_final BEFORE UPDATE OF state ON batches
                WHEN OLD.state = 'sent' AND NEW.state <> 'sent'
            BEGIN SELECT RAISE(ABORT, 'sent batch cannot be resent'); END;

            -- 상한 초과로 지운 미전송 데이터의 범위(README 5절: 누락 범위를 로그와 상태에 남긴다).
            CREATE TABLE gaps(
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                recorded_at TEXT NOT NULL,
                reason TEXT NOT NULL CHECK(reason IN ('max_age', 'max_bytes')),
                state TEXT NOT NULL,
                boot_session_id TEXT NOT NULL,
                first_seq INTEGER NOT NULL,
                last_seq INTEGER NOT NULL,
                first_observed_at TEXT NOT NULL,
                last_observed_at TEXT NOT NULL,
                batches INTEGER NOT NULL,
                samples INTEGER NOT NULL,
                bytes INTEGER NOT NULL);
            """, tx);
        Exec(db, $"PRAGMA user_version={SchemaVersion};", tx);
        tx.Commit();
    }

    // ───────────────────────── 재시작 복원 ─────────────────────────

    private void Restore()
    {
        lock (_gate)
        {
            var open = Count("SELECT count(*) FROM open_samples");
            var frozen = open > 0 ? FreezeAllLocked() : 0;
            using var tx = _db.BeginTransaction();
            var foreign = Exec(_db, """
                UPDATE batches SET state = 'quarantined', quarantine_reason = 'machine_id_changed'
                WHERE state = 'pending' AND machine_id <> $m
                """, tx, ("$m", _machineId.ToString("D")));
            Exec(_db, "INSERT OR REPLACE INTO meta VALUES('machine_id', $m)", tx, ("$m", _machineId.ToString("D")));
            tx.Commit();
            var stats = StatsLocked();
            _events.Emit(Event(_now(), "buffer_opened", new()
            {
                ["file"] = FileName,
                ["pending_batches"] = stats.PendingBatches,
                ["quarantined_batches"] = stats.QuarantinedBatches,
                ["restored_open_samples"] = open,
                ["restored_batches_frozen"] = frozen,
                ["quarantined_machine_changed"] = foreign,
                ["max_age_hours"] = _options.MaxAge.TotalHours,
                ["max_bytes"] = _options.MaxBytes,
            }));
            if (foreign > 0)
                _events.Emit(Event(_now(), "batch_quarantined", new() { ["batches"] = foreign, ["reason"] = "machine_id_changed" }));
        }
    }

    /// <summary>
    /// S1-02b 파일 스풀(spool/, quarantine/)을 가져온다. 본문·ID를 그대로 옮기고 커밋 후 파일을 지운다.
    /// 정규 본문이 아니면 보내지 않고 격리로 가져온다.
    /// </summary>
    public int ImportLegacySpool(string stateDirectory)
    {
        var imported = 0;
        foreach (var (dir, quarantined) in new[] { ("spool", false), ("quarantine", true) })
        {
            var folder = System.IO.Path.Combine(stateDirectory, dir);
            if (!Directory.Exists(folder)) continue;
            foreach (var file in Directory.GetFiles(folder, "*.json").Order(StringComparer.Ordinal))
            {
                var name = System.IO.Path.GetFileName(file);
                var body = File.ReadAllText(file);
                string? reason = quarantined && File.Exists(file + ".reason.txt")
                    ? "legacy: " + File.ReadAllText(file + ".reason.txt").Trim()
                    : quarantined ? "legacy" : null;
                DateTime frozenAt;
                try
                {
                    frozenAt = DateTime.ParseExact(name[..22], "yyyyMMdd'T'HHmmssfffffff", CultureInfo.InvariantCulture,
                        DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);
                }
                catch (Exception e) when (e is FormatException or ArgumentOutOfRangeException)
                {
                    frozenAt = _now();
                }
                lock (_gate)
                {
                    try
                    {
                        if (Canonical.Normalize(body) != body) reason ??= "legacy_not_canonical";
                        var meta = Describe(body);
                        using var tx = _db.BeginTransaction();
                        if (reason is null && meta.Machine != _machineId.ToString("D")) reason = "machine_id_changed";
                        imported += Insert(meta with
                        {
                            FrozenAt = frozenAt,
                            State = reason is null ? "pending" : "quarantined",
                            QuarantineReason = reason,
                        }, tx, ignoreExisting: true);
                        tx.Commit();
                    }
                    catch (Exception e) when (e is JsonException or FormatException or KeyNotFoundException or InvalidOperationException)
                    {
                        // 해석할 수 없는 파일은 지우지 않고 남긴다.
                        _events.Emit(Event(_now(), "legacy_spool_unreadable", new() { ["file"] = name, ["error"] = e.GetType().Name }));
                        continue;
                    }
                }
                File.Delete(file);
                if (File.Exists(file + ".reason.txt")) File.Delete(file + ".reason.txt");
            }
            if (!Directory.EnumerateFileSystemEntries(folder).Any()) Directory.Delete(folder);
        }
        if (imported > 0) _events.Emit(Event(_now(), "legacy_spool_imported", new() { ["batches"] = imported }));
        return imported;
    }

    // ───────────────────────── 수집 → 동결 ─────────────────────────

    public void Add(Guid bootSessionId, Guid configId, Sample sample)
    {
        lock (_gate)
        {
            // 부팅 세션·구성이 바뀌면 이전 샘플을 먼저 동결한다(배치 하나에 섞지 않는다).
            if (_key is { } k && (k.Boot != bootSessionId || k.Config != configId)) FreezeAllLocked();
            _key = (bootSessionId, configId);
            var body = JsonSerializer.Serialize(SampleRow.From(sample));
            using (var tx = _db.BeginTransaction())
            {
                Exec(_db, """
                    INSERT INTO open_samples VALUES($boot, $seq, $machine, $config, $observed, $body, $bytes, $now)
                    """, tx,
                    ("$boot", bootSessionId.ToString("D")), ("$seq", sample.Seq), ("$machine", _machineId.ToString("D")),
                    ("$config", configId.ToString("D")), ("$observed", Utc.Format(sample.ObservedAtUtc)), ("$body", body),
                    ("$bytes", body.Length), ("$now", Format(_now())));
                _openForKey++;
                if (_openForKey >= _options.SamplesPerBatch) FreezeLocked(tx);
                tx.Commit();
            }
            if (_now() - _lastPrune >= _options.PruneInterval || _now() < _lastPrune) PruneLocked();
        }
    }

    public void Flush()
    {
        lock (_gate) FreezeAllLocked();
    }

    private int FreezeAllLocked()
    {
        using var tx = _db.BeginTransaction();
        var n = FreezeLocked(tx);
        tx.Commit();
        return n;
    }

    /// <summary>미동결 샘플을 (부팅 세션, 구성)별로 순번 순서대로 동결한다. 호출자 트랜잭션 안에서 실행한다.</summary>
    private int FreezeLocked(SqliteTransaction tx)
    {
        var rows = new List<(string Machine, string Boot, string Config, Sample Sample)>();
        using (var cmd = Command("""
            SELECT machine_id, boot_session_id, sensor_config_id, body FROM open_samples
            ORDER BY enqueued_at, boot_session_id, sample_seq
            """, tx))
        using (var r = cmd.ExecuteReader())
        {
            while (r.Read())
                rows.Add((r.GetString(0), r.GetString(1), r.GetString(2), JsonSerializer.Deserialize<SampleRow>(r.GetString(3))!.ToSample()));
        }
        var frozen = 0;
        foreach (var group in rows.GroupBy(x => (x.Machine, x.Boot, x.Config)))
        {
            var samples = group.Select(x => x.Sample).OrderBy(s => s.Seq).ToList();
            var batches = BatchFactory.Freeze(Guid.Parse(group.Key.Machine), Guid.Parse(group.Key.Boot), Guid.Parse(group.Key.Config), samples);
            var now = _now();
            foreach (var b in batches)
            {
                Insert(Describe(b.Body) with { FrozenAt = now, State = "pending" }, tx, ignoreExisting: false);
                frozen++;
            }
            Exec(_db, "DELETE FROM open_samples WHERE machine_id = $m AND boot_session_id = $b AND sensor_config_id = $c", tx,
                ("$m", group.Key.Machine), ("$b", group.Key.Boot), ("$c", group.Key.Config));
        }
        _openForKey = 0;
        return frozen;
    }

    private sealed record BatchRow(
        Guid BatchId, string Machine, string Boot, string Config, long FirstSeq, long LastSeq, int Count,
        string FirstObserved, string LastObserved, string Body, string Sha, DateTime FrozenAt, string State, string? QuarantineReason);

    private static BatchRow Describe(string body)
    {
        using var doc = JsonDocument.Parse(body);
        var root = doc.RootElement;
        var samples = root.GetProperty("samples");
        if (samples.GetArrayLength() == 0) throw new FormatException("empty batch");
        var first = samples[0];
        var last = samples[samples.GetArrayLength() - 1];
        return new BatchRow(
            Guid.Parse(root.GetProperty("batch_id").GetString()!),
            root.GetProperty("machine_id").GetString()!,
            root.GetProperty("boot_session_id").GetString()!,
            root.GetProperty("sensor_config_id").GetString()!,
            first.GetProperty("sample_seq").GetInt64(),
            last.GetProperty("sample_seq").GetInt64(),
            samples.GetArrayLength(),
            first.GetProperty("observed_at").GetString()!,
            last.GetProperty("observed_at").GetString()!,
            body,
            Canonical.Sha256Hex(body),
            default,
            "pending",
            null);
    }

    private int Insert(BatchRow b, SqliteTransaction tx, bool ignoreExisting)
    {
        if (ignoreExisting && Count("SELECT count(*) FROM batches WHERE batch_id = $id", tx, ("$id", b.BatchId.ToString("D"))) > 0) return 0;
        // 같은 샘플이 두 배치에 들어가지 않는다(계약: 다른 배치에 다시 포함하지 않는다).
        var overlap = Count("""
            SELECT count(*) FROM batches WHERE boot_session_id = $boot AND first_seq <= $last AND last_seq >= $first
            """, tx, ("$boot", b.Boot), ("$first", b.FirstSeq), ("$last", b.LastSeq));
        if (overlap > 0) throw new InvalidOperationException($"sample range already batched: {b.Boot} {b.FirstSeq}..{b.LastSeq}");
        Exec(_db, """
            INSERT INTO batches(batch_id, machine_id, boot_session_id, sensor_config_id, first_seq, last_seq, sample_count,
                first_observed_at, last_observed_at, body, sha256, bytes, frozen_at, state, quarantine_reason)
            VALUES($id, $m, $boot, $c, $first, $last, $n, $fo, $lo, $body, $sha, $bytes, $frozen, $state, $reason)
            """, tx,
            ("$id", b.BatchId.ToString("D")), ("$m", b.Machine), ("$boot", b.Boot), ("$c", b.Config),
            ("$first", b.FirstSeq), ("$last", b.LastSeq), ("$n", b.Count), ("$fo", b.FirstObserved), ("$lo", b.LastObserved),
            ("$body", b.Body), ("$sha", b.Sha), ("$bytes", System.Text.Encoding.UTF8.GetByteCount(b.Body)),
            ("$frozen", Format(b.FrozenAt)), ("$state", b.State), ("$reason", b.QuarantineReason));
        return 1;
    }

    // ───────────────────────── 전송 상태 ─────────────────────────

    /// <summary>
    /// 가장 먼저 동결된 대기 배치. 실패한 배치를 건너뛰지 않아 순서가 유지된다.
    /// 돌려준 배치는 결과(<see cref="MarkSent"/>·<see cref="RecordAttempt"/>·<see cref="Quarantine"/>)가 기록될 때까지 상한 정리에서 제외한다.
    /// </summary>
    public PendingBatch? NextPending()
    {
        lock (_gate)
        {
            _inFlight = null;
            using var cmd = Command("""
                SELECT batch_id, body, sha256, sample_count, attempts, frozen_at FROM batches
                WHERE state = 'pending' ORDER BY seq LIMIT 1
                """);
            using var r = cmd.ExecuteReader();
            if (!r.Read()) return null;
            var batch = new PendingBatch(Guid.Parse(r.GetString(0)), r.GetString(1), r.GetString(2), r.GetInt32(3), r.GetInt32(4), Parse(r.GetString(5)));
            _inFlight = batch.BatchId;
            return batch;
        }
    }

    /// <summary>서버 200(최초 커밋 또는 기존 성공 재반환). 본문을 지우고 원장 행만 남긴다.</summary>
    public void MarkSent(Guid batchId, string? serverReceivedAt)
    {
        lock (_gate)
        {
            _inFlight = null;
            Exec(_db, """
                UPDATE batches SET state = 'sent', body = NULL, attempts = attempts + 1, last_attempt_at = $now,
                    last_status = 200, last_code = NULL, sent_at = $now, server_received_at = $recv
                WHERE batch_id = $id AND state = 'pending'
                """, null, ("$now", Format(_now())), ("$recv", serverReceivedAt), ("$id", batchId.ToString("D")));
        }
    }

    public void RecordAttempt(Guid batchId, int? status, string? code)
    {
        lock (_gate)
        {
            _inFlight = null;
            Exec(_db, """
                UPDATE batches SET attempts = attempts + 1, last_attempt_at = $now, last_status = $s, last_code = $c
                WHERE batch_id = $id AND state = 'pending'
                """, null, ("$now", Format(_now())), ("$s", status), ("$c", code), ("$id", batchId.ToString("D")));
        }
    }

    /// <summary>재시도해도 성공할 수 없는 배치. 새 ID로 우회하지 않고 진단용으로 보관한다(상한 적용 대상).</summary>
    public void Quarantine(Guid batchId, int? status, string? code, string reason)
    {
        lock (_gate)
        {
            _inFlight = null;
            Exec(_db, """
                UPDATE batches SET state = 'quarantined', attempts = attempts + 1, last_attempt_at = $now,
                    last_status = $s, last_code = $c, quarantine_reason = $r
                WHERE batch_id = $id AND state = 'pending'
                """, null, ("$now", Format(_now())), ("$s", status), ("$c", code), ("$r", reason), ("$id", batchId.ToString("D")));
        }
    }

    // ───────────────────────── 상한 ─────────────────────────

    /// <summary>상한을 적용한다. 지운 미전송 샘플 수를 돌려준다.</summary>
    public int Prune()
    {
        lock (_gate) return PruneLocked();
    }

    private int PruneLocked()
    {
        var now = _now();
        _lastPrune = now;
        var cutoff = Format(now - _options.MaxAge);
        var dropped = new List<(long Seq, string Boot, long First, long Last, string Fo, string Lo, int Count, long Bytes, string State, string Reason)>();
        using (var tx = _db.BeginTransaction())
        {
            Exec(_db, "DELETE FROM batches WHERE state = 'sent' AND sent_at < $c", tx, ("$c", Format(now - _options.SentRetention)));
            Exec(_db, "DELETE FROM gaps WHERE recorded_at < $c", tx, ("$c", Format(now - _options.GapRetention)));
            var total = Convert.ToInt64(Scalar(_db, """
                SELECT (SELECT coalesce(sum(bytes), 0) FROM batches WHERE state <> 'sent')
                     + (SELECT coalesce(sum(bytes), 0) FROM open_samples)
                """, tx), CultureInfo.InvariantCulture);
            using (var cmd = Command("""
                SELECT seq, boot_session_id, first_seq, last_seq, first_observed_at, last_observed_at, sample_count, bytes, state, frozen_at, batch_id
                FROM batches WHERE state <> 'sent' ORDER BY seq
                """, tx))
            using (var r = cmd.ExecuteReader())
            {
                while (r.Read())
                {
                    // 벽시계가 뒤로 가 동결 시각이 미래가 돼도 만료로 보지 않는다(문자열 비교 = UTC 시간 순서).
                    var expired = string.CompareOrdinal(r.GetString(9), cutoff) < 0;
                    if (!expired && total <= _options.MaxBytes) break;
                    if (_inFlight?.ToString("D") == r.GetString(10)) continue;   // 결과를 본 뒤 다음 정리에서 판단한다
                    var bytes = r.GetInt64(7);
                    total -= bytes;
                    dropped.Add((r.GetInt64(0), r.GetString(1), r.GetInt64(2), r.GetInt64(3), r.GetString(4), r.GetString(5),
                        r.GetInt32(6), bytes, r.GetString(8), expired ? "max_age" : "max_bytes"));
                }
            }
            if (dropped.Count == 0)
            {
                tx.Commit();
                return 0;
            }
            foreach (var d in dropped) Exec(_db, "DELETE FROM batches WHERE seq = $s", tx, ("$s", d.Seq));
            foreach (var g in Ranges(dropped))
            {
                Exec(_db, """
                    INSERT INTO gaps(recorded_at, reason, state, boot_session_id, first_seq, last_seq, first_observed_at, last_observed_at,
                        batches, samples, bytes) VALUES($now, $reason, $state, $boot, $first, $last, $fo, $lo, $batches, $samples, $bytes)
                    """, tx, ("$now", Format(now)), ("$reason", g.Reason), ("$state", g.State), ("$boot", g.BootSessionId.ToString("D")),
                    ("$first", g.FirstSeq), ("$last", g.LastSeq), ("$fo", Utc.Format(g.FirstObservedUtc)), ("$lo", Utc.Format(g.LastObservedUtc)),
                    ("$batches", g.Batches), ("$samples", g.Samples), ("$bytes", g.Bytes));
            }
            var samples = dropped.Sum(d => d.Count);
            Exec(_db, """
                INSERT INTO meta VALUES('dropped_samples_total', $n)
                ON CONFLICT(key) DO UPDATE SET value = CAST(value AS INTEGER) + $n
                """, tx, ("$n", samples));
            tx.Commit();
            var ranges = Ranges(dropped);
            _events.Emit(Event(now, "buffer_pruned", new()
            {
                ["batches"] = dropped.Count,
                ["samples"] = samples,
                ["bytes"] = dropped.Sum(d => d.Bytes),
                ["reasons"] = dropped.GroupBy(d => d.Reason).ToDictionary(g => g.Key, g => (object?)g.Sum(d => d.Count)),
                ["ranges"] = ranges.Take(20).Select(g => new Dictionary<string, object?>
                {
                    ["boot_session_id"] = g.BootSessionId.ToString("D"),
                    ["first_seq"] = g.FirstSeq,
                    ["last_seq"] = g.LastSeq,
                    ["first_observed_at"] = Utc.Format(g.FirstObservedUtc),
                    ["last_observed_at"] = Utc.Format(g.LastObservedUtc),
                    ["samples"] = g.Samples,
                    ["reason"] = g.Reason,
                    ["state"] = g.State,
                }).ToList(),
                ["ranges_truncated"] = ranges.Count > 20,
                ["max_age_hours"] = _options.MaxAge.TotalHours,
                ["max_bytes"] = _options.MaxBytes,
            }));
            Exec(_db, "PRAGMA incremental_vacuum;");
            return samples;
        }

        // 같은 부팅 세션에서 동결 순서로 이어진 배치를 하나의 누락 범위로 묶는다.
        static List<BufferGap> Ranges(List<(long Seq, string Boot, long First, long Last, string Fo, string Lo, int Count, long Bytes, string State, string Reason)> items)
        {
            var result = new List<BufferGap>();
            foreach (var d in items)
            {
                var boot = Guid.Parse(d.Boot);
                if (result.Count > 0 && result[^1] is var g && g.BootSessionId == boot && g.Reason == d.Reason && g.State == d.State
                    && d.First > g.LastSeq)
                {
                    result[^1] = g with
                    {
                        LastSeq = d.Last,
                        LastObservedUtc = Parse(d.Lo),
                        Batches = g.Batches + 1,
                        Samples = g.Samples + d.Count,
                        Bytes = g.Bytes + d.Bytes,
                    };
                }
                else
                {
                    result.Add(new BufferGap(d.Reason, d.State, boot, d.First, d.Last, Parse(d.Fo), Parse(d.Lo), 1, d.Count, d.Bytes, default));
                }
            }
            return result;
        }
    }

    // ───────────────────────── 조회 ─────────────────────────

    public BufferStats Stats()
    {
        lock (_gate) return StatsLocked();
    }

    private BufferStats StatsLocked()
    {
        int pending = 0, quarantined = 0, sent = 0;
        long unsentBytes = 0;
        string? oldest = null;
        using (var cmd = Command("SELECT state, count(*), coalesce(sum(bytes), 0), min(frozen_at) FROM batches GROUP BY state"))
        using (var r = cmd.ExecuteReader())
        {
            while (r.Read())
            {
                var state = r.GetString(0);
                var n = r.GetInt32(1);
                if (state == "sent")
                {
                    sent = n;
                    continue;
                }
                if (state == "pending") pending = n; else quarantined = n;
                unsentBytes += r.GetInt64(2);
                var min = r.GetString(3);
                if (oldest is null || string.CompareOrdinal(min, oldest) < 0) oldest = min;
            }
        }
        var open = Count("SELECT count(*) FROM open_samples");
        unsentBytes += Convert.ToInt64(Scalar(_db, "SELECT coalesce(sum(bytes), 0) FROM open_samples"), CultureInfo.InvariantCulture);
        var droppedTotal = Convert.ToInt64(Scalar(_db, "SELECT coalesce((SELECT CAST(value AS INTEGER) FROM meta WHERE key = 'dropped_samples_total'), 0)"),
            CultureInfo.InvariantCulture);
        BufferGap? last = null;
        using (var cmd = Command("""
            SELECT reason, state, boot_session_id, first_seq, last_seq, first_observed_at, last_observed_at, batches, samples, bytes, recorded_at
            FROM gaps ORDER BY id DESC LIMIT 1
            """))
        using (var r = cmd.ExecuteReader())
        {
            if (r.Read())
            {
                last = new BufferGap(r.GetString(0), r.GetString(1), Guid.Parse(r.GetString(2)), r.GetInt64(3), r.GetInt64(4),
                    Parse(r.GetString(5)), Parse(r.GetString(6)), r.GetInt32(7), r.GetInt32(8), r.GetInt64(9), Parse(r.GetString(10)));
            }
        }
        var pageSize = Convert.ToInt64(Scalar(_db, "PRAGMA page_size;"), CultureInfo.InvariantCulture);
        var pages = Convert.ToInt64(Scalar(_db, "PRAGMA page_count;"), CultureInfo.InvariantCulture);
        return new BufferStats(pending, quarantined, open, unsentBytes, oldest is null ? null : Parse(oldest), sent, droppedTotal, last,
            pageSize * pages);
    }

    public IReadOnlyList<BufferGap> Gaps()
    {
        lock (_gate)
        {
            var result = new List<BufferGap>();
            using var cmd = Command("""
                SELECT reason, state, boot_session_id, first_seq, last_seq, first_observed_at, last_observed_at, batches, samples, bytes, recorded_at
                FROM gaps ORDER BY id
                """);
            using var r = cmd.ExecuteReader();
            while (r.Read())
            {
                result.Add(new BufferGap(r.GetString(0), r.GetString(1), Guid.Parse(r.GetString(2)), r.GetInt64(3), r.GetInt64(4),
                    Parse(r.GetString(5)), Parse(r.GetString(6)), r.GetInt32(7), r.GetInt32(8), r.GetInt64(9), Parse(r.GetString(10))));
            }
            return result;
        }
    }

    public void Dispose()
    {
        lock (_gate) _db.Dispose();
    }

    // ───────────────────────── 보조 ─────────────────────────

    private static string Format(DateTime t) => t.ToUniversalTime().ToString(TimeFormat, CultureInfo.InvariantCulture);

    private static DateTime Parse(string s) =>
        DateTime.ParseExact(s, TimeFormat, CultureInfo.InvariantCulture, DateTimeStyles.AssumeUniversal | DateTimeStyles.AdjustToUniversal);

    private SqliteCommand Command(string sql, SqliteTransaction? tx = null)
    {
        var cmd = _db.CreateCommand();
        cmd.CommandText = sql;
        cmd.Transaction = tx;
        return cmd;
    }

    private int Count(string sql, SqliteTransaction? tx = null, params (string Name, object? Value)[] args)
    {
        using var cmd = Command(sql, tx);
        foreach (var (n, v) in args) cmd.Parameters.AddWithValue(n, v ?? DBNull.Value);
        return Convert.ToInt32(cmd.ExecuteScalar(), CultureInfo.InvariantCulture);
    }

    private static int Exec(SqliteConnection db, string sql, SqliteTransaction? tx = null, params (string Name, object? Value)[] args)
    {
        using var cmd = db.CreateCommand();
        cmd.CommandText = sql;
        cmd.Transaction = tx;
        foreach (var (n, v) in args) cmd.Parameters.AddWithValue(n, v ?? DBNull.Value);
        return cmd.ExecuteNonQuery();
    }

    private static object? Scalar(SqliteConnection db, string sql, SqliteTransaction? tx = null)
    {
        using var cmd = db.CreateCommand();
        cmd.CommandText = sql;
        cmd.Transaction = tx;
        return cmd.ExecuteScalar();
    }

    private static CollectionEvent Event(DateTime now, string type, Dictionary<string, object?> details) =>
        new(Guid.NewGuid(), type, now, 0, null, details);

    /// <summary>미동결 샘플 직렬화. 관측 시각은 계약 형식(µs) 문자열로 저장해 동결 본문과 같은 값을 다시 만든다.</summary>
    private sealed record SampleRow(long S, string O, long M, List<ReadingRow> R)
    {
        public static SampleRow From(Sample s) => new(s.Seq, Utc.Format(s.ObservedAtUtc), s.MonotonicMs,
            s.Readings.Select(r => new ReadingRow(r.SensorId, r.Unit, (int)r.Quality, r.Value, r.Reason)).ToList());

        public Sample ToSample() => new(S, Parse(O), M,
            R.Select(r => new Reading(r.I, r.U, (Quality)r.Q, r.V, r.Why)).ToList());
    }

    private sealed record ReadingRow(string I, string U, int Q, decimal? V, string? Why);
}
