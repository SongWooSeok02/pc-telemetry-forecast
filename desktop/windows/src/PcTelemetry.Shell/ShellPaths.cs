using System.Text;
using PcTelemetry.Shell.Core;

namespace PcTelemetry.Shell;

/// <summary>
/// 실행 파일은 Program Files(사용자 쓰기 불가)에 설치되므로 WebView2 사용자 데이터·로그는 사용자별
/// %LOCALAPPDATA%에 둔다. 로그인 쿠키·저장소가 사용자별로 분리되고 서비스 상태 디렉터리와도 섞이지 않는다.
/// </summary>
internal sealed record ShellPaths(string AppDirectory, string UserRoot)
{
    public string ConfigFile => Path.Combine(AppDirectory, ShellConfigLoader.FileName);
    public string UserDataFolder => Path.Combine(UserRoot, "WebView2");
    public string LogFile => Path.Combine(UserRoot, "shell.log");

    public static ShellPaths Default() => new(
        AppContext.BaseDirectory,
        Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "PcTelemetry", "Shell"));
}

/// <summary>최소 진단 로그. URL은 쿼리를 지우고 기록하며 토큰·쿠키·응답 본문은 쓰지 않는다.</summary>
internal sealed class ShellLog(string path)
{
    private readonly Lock gate = new();

    public void Write(string message)
    {
        try
        {
            lock (gate)
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path)!);
                var info = new FileInfo(path);
                if (info.Exists && info.Length > 1_000_000) File.Move(path, path + ".1", overwrite: true);
                File.AppendAllText(path, $"{DateTime.UtcNow:yyyy-MM-ddTHH:mm:ss.fffZ} {message}{Environment.NewLine}", Encoding.UTF8);
            }
        }
        catch (Exception e) when (e is IOException or UnauthorizedAccessException)
        {
            // 로그 실패로 화면 표시를 막지 않는다.
        }
    }
}
