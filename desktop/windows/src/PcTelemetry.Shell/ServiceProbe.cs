using System.ComponentModel;
using System.ServiceProcess;
using PcTelemetry.Shell.Core;

namespace PcTelemetry.Shell;

/// <summary>
/// 수집 서비스 상태 조회만 한다. 일반 사용자에게 기본 허용되는 SERVICE_QUERY_STATUS 범위이며
/// 시작·중지 등 제어는 하지 않는다(UI를 닫거나 실패해도 서비스에 영향 없음).
/// </summary>
internal static class ServiceProbe
{
    private const int ErrorServiceDoesNotExist = 1060;

    public static ServiceState Query(string serviceName)
    {
        try
        {
            using var sc = new ServiceController(serviceName);
            return sc.Status switch
            {
                ServiceControllerStatus.Running => ServiceState.Running,
                ServiceControllerStatus.Stopped => ServiceState.Stopped,
                ServiceControllerStatus.StartPending or ServiceControllerStatus.ContinuePending => ServiceState.StartPending,
                ServiceControllerStatus.StopPending or ServiceControllerStatus.PausePending => ServiceState.StopPending,
                ServiceControllerStatus.Paused => ServiceState.Paused,
                _ => ServiceState.Unknown,
            };
        }
        catch (InvalidOperationException e) when (e.InnerException is Win32Exception { NativeErrorCode: ErrorServiceDoesNotExist })
        {
            return ServiceState.NotInstalled;
        }
        catch (Exception e) when (e is InvalidOperationException or Win32Exception)
        {
            return ServiceState.Unknown;
        }
    }
}
