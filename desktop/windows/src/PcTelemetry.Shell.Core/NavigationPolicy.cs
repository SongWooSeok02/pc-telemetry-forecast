namespace PcTelemetry.Shell.Core;

public enum NavigationDecision
{
    /// <summary>셸 안에서 연다(대시보드와 같은 origin).</summary>
    Allow,
    /// <summary>셸에서 막고 기본 브라우저로 연다(외부 https 링크).</summary>
    OpenExternally,
    /// <summary>막는다(file:, javascript:, 평문 원격 http 등).</summary>
    Block,
}

public static class NavigationPolicy
{
    public static NavigationDecision Decide(Uri dashboard, string target)
    {
        if (!Uri.TryCreate(target, UriKind.Absolute, out var uri)) return NavigationDecision.Block;
        // WebView2 내부 오류·빈 페이지는 그대로 둔다.
        if (uri.Scheme == "about" && uri.AbsoluteUri == "about:blank") return NavigationDecision.Allow;
        if (SameOrigin(dashboard, uri)) return NavigationDecision.Allow;
        if (uri.Scheme == Uri.UriSchemeHttps && string.IsNullOrEmpty(uri.UserInfo)) return NavigationDecision.OpenExternally;
        return NavigationDecision.Block;
    }

    public static bool SameOrigin(Uri a, Uri b) =>
        string.Equals(a.Scheme, b.Scheme, StringComparison.OrdinalIgnoreCase)
        && string.Equals(a.IdnHost, b.IdnHost, StringComparison.OrdinalIgnoreCase)
        && a.Port == b.Port;
}
