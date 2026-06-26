using System.Windows.Forms;

namespace CodexMonitorWidget;

internal static class Program
{
    [STAThread]
    private static void Main(string[] args)
    {
        ApplicationConfiguration.Initialize();
        Application.Run(new FloatingStatusForm(ResolveApiUrl(args)));
    }

    private static Uri ResolveApiUrl(string[] args)
    {
        var configured =
            args.FirstOrDefault()
            ?? Environment.GetEnvironmentVariable("CODEX_MONITOR_API_URL")
            ?? "http://localhost:8765";

        if (!configured.StartsWith("http://", StringComparison.OrdinalIgnoreCase)
            && !configured.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
        {
            configured = "http://" + configured;
        }

        var uri = new Uri(configured, UriKind.Absolute);
        if (uri.AbsolutePath is "/" or "")
        {
            return new Uri(uri, "/api/sessions");
        }
        return uri;
    }
}
