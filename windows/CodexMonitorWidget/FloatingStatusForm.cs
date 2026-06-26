using System.Drawing;
using System.Drawing.Drawing2D;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using System.Windows.Forms;

namespace CodexMonitorWidget;

internal sealed class FloatingStatusForm : Form
{
    private const int DotSize = 14;
    private const int DotGap = 8;
    private const int PaddingX = 10;
    private const int PanelHeight = 32;
    private const int MinimumPanelWidth = 32;
    private const string StatusIdle = "\u672a\u8fd0\u884c";
    private const string StatusRunning = "\u8fd0\u884c\u4e2d";
    private const string StatusSuccess = "\u6210\u529f";
    private const string StatusFailed = "\u5931\u8d25";

    private readonly Uri _apiUrl;
    private readonly HttpClient _httpClient = new() { Timeout = TimeSpan.FromSeconds(2) };
    private readonly System.Windows.Forms.Timer _timer = new() { Interval = 1500 };
    private readonly ToolTip _toolTip = new()
    {
        InitialDelay = 250,
        ReshowDelay = 100,
        AutoPopDelay = 10000,
        ShowAlways = true,
    };
    private readonly List<SessionItem> _sessions = [];
    private readonly ContextMenuStrip _menu = new();
    private string? _lastError;
    private bool _dragging;
    private Point _dragStart;
    private int _hoveredDot = -1;

    public FloatingStatusForm(Uri apiUrl)
    {
        _apiUrl = apiUrl;
        AutoScaleMode = AutoScaleMode.Dpi;
        BackColor = Color.FromArgb(34, 34, 34);
        ClientSize = new Size(MinimumPanelWidth, PanelHeight);
        DoubleBuffered = true;
        FormBorderStyle = FormBorderStyle.None;
        MaximizeBox = false;
        MinimizeBox = false;
        ShowIcon = false;
        ShowInTaskbar = false;
        StartPosition = FormStartPosition.Manual;
        TopMost = true;
        Location = new Point(Screen.PrimaryScreen?.WorkingArea.Right - Width - 24 ?? 80, 80);

        _menu.Items.Add("\u5237\u65b0", null, async (_, _) => await RefreshSessionsAsync());
        _menu.Items.Add("\u9000\u51fa", null, (_, _) => Close());
        ContextMenuStrip = _menu;

        _timer.Tick += async (_, _) => await RefreshSessionsAsync();
    }

    protected override async void OnShown(EventArgs e)
    {
        base.OnShown(e);
        TopMost = true;
        await RefreshSessionsAsync();
        _timer.Start();
    }

    protected override void OnPaint(PaintEventArgs e)
    {
        base.OnPaint(e);
        e.Graphics.SmoothingMode = SmoothingMode.AntiAlias;

        using var background = new SolidBrush(BackColor);
        using var border = new Pen(Color.FromArgb(86, 86, 86), 1);
        var panel = new Rectangle(0, 0, ClientSize.Width - 1, ClientSize.Height - 1);
        e.Graphics.FillRectangle(background, panel);
        e.Graphics.DrawRectangle(border, panel);

        for (var index = 0; index < _sessions.Count; index++)
        {
            var dot = DotBounds(index);
            using var brush = new SolidBrush(StatusColor(_sessions[index].Status));
            e.Graphics.FillEllipse(brush, dot);
        }
    }

    protected override void OnMouseDown(MouseEventArgs e)
    {
        base.OnMouseDown(e);
        if (e.Button != MouseButtons.Left)
        {
            return;
        }
        _dragging = true;
        _dragStart = e.Location;
    }

    protected override void OnMouseMove(MouseEventArgs e)
    {
        base.OnMouseMove(e);
        if (_dragging)
        {
            Location = new Point(Location.X + e.X - _dragStart.X, Location.Y + e.Y - _dragStart.Y);
            return;
        }

        var hovered = DotIndexAt(e.Location);
        if (hovered == _hoveredDot)
        {
            return;
        }
        _hoveredDot = hovered;
        if (hovered >= 0)
        {
            _toolTip.SetToolTip(this, TooltipText(_sessions[hovered]));
        }
        else if (_lastError is not null)
        {
            _toolTip.SetToolTip(this, "\u63a5\u53e3\u4e0d\u53ef\u7528: " + _lastError);
        }
        else
        {
            _toolTip.SetToolTip(this, "");
        }
    }

    protected override void OnMouseUp(MouseEventArgs e)
    {
        base.OnMouseUp(e);
        if (e.Button == MouseButtons.Left)
        {
            _dragging = false;
        }
    }

    protected override void OnDeactivate(EventArgs e)
    {
        base.OnDeactivate(e);
        TopMost = true;
    }

    private async Task RefreshSessionsAsync()
    {
        try
        {
            var payload = await _httpClient.GetFromJsonAsync<SessionsPayload>(_apiUrl);
            _sessions.Clear();
            if (payload?.Sessions is not null)
            {
                _sessions.AddRange(payload.Sessions.OrderBy(item => item.Pid));
            }
            _lastError = null;
        }
        catch (Exception error) when (
            error is HttpRequestException
                or TaskCanceledException
                or NotSupportedException
                or JsonException
        )
        {
            _sessions.Clear();
            _lastError = error.Message;
        }

        ResizePanel();
        Invalidate();
    }

    private void ResizePanel()
    {
        var width = _sessions.Count == 0
            ? MinimumPanelWidth
            : PaddingX * 2 + _sessions.Count * DotSize + (_sessions.Count - 1) * DotGap;
        ClientSize = new Size(Math.Max(MinimumPanelWidth, width), PanelHeight);
    }

    private Rectangle DotBounds(int index)
    {
        var x = PaddingX + index * (DotSize + DotGap);
        var y = (PanelHeight - DotSize) / 2;
        return new Rectangle(x, y, DotSize, DotSize);
    }

    private int DotIndexAt(Point point)
    {
        for (var index = 0; index < _sessions.Count; index++)
        {
            if (DotBounds(index).Contains(point))
            {
                return index;
            }
        }
        return -1;
    }

    private static Color StatusColor(string? status) => status switch
    {
        StatusRunning => Color.FromArgb(47, 128, 237),
        StatusSuccess => Color.FromArgb(39, 174, 96),
        StatusFailed => Color.FromArgb(235, 87, 87),
        _ => Color.FromArgb(139, 143, 152),
    };

    private static string TooltipText(SessionItem session)
    {
        var startedAt = FormatStartedAt(session.StartedAtIso, session.StartedAt);
        var directory = string.IsNullOrWhiteSpace(session.Directory)
            ? "-"
            : session.Directory;
        return
            $"PID: {session.Pid}\n"
            + $"\u72b6\u6001: {session.Status}\n"
            + $"\u76ee\u5f55: {directory}\n"
            + $"\u542f\u52a8\u65f6\u95f4: {startedAt}";
    }

    private static string FormatStartedAt(string? startedAtIso, double? startedAt)
    {
        if (!string.IsNullOrWhiteSpace(startedAtIso)
            && DateTimeOffset.TryParse(startedAtIso, out var parsedIso))
        {
            return parsedIso.ToLocalTime().ToString("yyyy-MM-dd HH:mm:ss");
        }
        if (startedAt is not null)
        {
            return DateTimeOffset.FromUnixTimeMilliseconds((long)(startedAt.Value * 1000))
                .LocalDateTime
                .ToString("yyyy-MM-dd HH:mm:ss");
        }
        return "-";
    }

    private sealed class SessionsPayload
    {
        [JsonPropertyName("sessions")]
        public List<SessionItem>? Sessions { get; init; }
    }

    private sealed class SessionItem
    {
        [JsonPropertyName("pid")]
        public int Pid { get; init; }

        [JsonPropertyName("status")]
        public string? Status { get; init; }

        [JsonPropertyName("directory")]
        public string? Directory { get; init; }

        [JsonPropertyName("started_at")]
        public double? StartedAt { get; init; }

        [JsonPropertyName("started_at_iso")]
        public string? StartedAtIso { get; init; }
    }
}
