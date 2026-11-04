using Microsoft.Extensions.DependencyInjection;
using Microsoft.Extensions.Hosting;

namespace SensorProbe;

// 서비스 계정에서 센서를 읽을 수 있는지만 확인한다. 결과는 out-dir/service-<계정>.json에 60초마다 갱신한다.
internal static class ServiceHost
{
    public static int Run(string outDir, int intervalMs)
    {
        var builder = Host.CreateApplicationBuilder();
        builder.Services.AddWindowsService(o => o.ServiceName = "SensorProbe");
        builder.Services.AddHostedService(_ => new ProbeWorker(outDir, intervalMs));
        builder.Build().Run();
        return 0;
    }

    private sealed class ProbeWorker(string outDir, int intervalMs) : BackgroundService
    {
        protected override Task ExecuteAsync(CancellationToken stoppingToken) => Task.Run(() =>
        {
            var account = Environment.UserName.Replace('$', '_');
            var started = DateTime.UtcNow.ToString("yyyyMMddTHHmmssZ");
            var outPath = Path.Combine(outDir, $"service-{account}-{started}.json");
            Program.RunLoop(outPath, TimeSpan.MaxValue, intervalMs, stoppingToken, TimeSpan.FromSeconds(60));
        }, stoppingToken);
    }
}
