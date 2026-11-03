// Fletchery.exe: the native window for Fletchery. Owns the window, taskbar and tray icon, Windows notifications and
// single-instance behaviour; draws the screens with WebView2 (the Edge engine in Windows 10/11) from the local backend
// (python -m fletchery run), which it starts, restarts after an update (exit 75) or a crash, and stops on Quit.
// Built by Build-Fletchery.ps1 with the .NET Framework compiler that ships with Windows (C# 5: no $"", no ?.).
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;

[assembly: System.Reflection.AssemblyTitle("Fletchery")]
[assembly: System.Reflection.AssemblyProduct("Fletchery")]
[assembly: System.Reflection.AssemblyCompany("Stonefletcher Productions LLC")]
[assembly: System.Reflection.AssemblyVersion("0.1.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("0.1.0.0")]

internal static class Program
{
    internal const string ShowEventName = "Local\\Fletchery.Show";

    [STAThread]
    private static void Main(string[] args)
    {
        bool first;
        using (var mutex = new Mutex(true, "Local\\Fletchery.Instance", out first))
        {
            if (!first)
            {
                // Already open: bring that window forward instead of starting a second app (and a second backend).
                try { EventWaitHandle.OpenExisting(ShowEventName).Set(); } catch (Exception) { }
                return;
            }
            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Settings settings = Settings.Load(args);
            Application.Run(new MainWindow(settings));
            GC.KeepAlive(mutex);
        }
    }
}

// %APPDATA%\Fletchery\settings.json: which profile, which Python, where the backend lives, the window's last bounds.
internal sealed class Settings
{
    public string Path;
    public string Profile = "";
    public string Python = "python";
    public string Backend = "";          // the folder that holds fletchery\ and ui\
    public int Port = 8770;
    public string ProjectName = "Fletchery";
    public string StateDir = "";
    public Rectangle Bounds = Rectangle.Empty;
    public bool Maximized;

    public static Settings Load(string[] args)
    {
        var s = new Settings();
        string dir = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "Fletchery");
        Directory.CreateDirectory(dir);
        s.Path = System.IO.Path.Combine(dir, "settings.json");
        s.Backend = AppDomain.CurrentDomain.BaseDirectory;
        var json = new JavaScriptSerializer();
        if (File.Exists(s.Path))
        {
            try
            {
                var d = json.Deserialize<Dictionary<string, object>>(File.ReadAllText(s.Path));
                s.Profile = Str(d, "profile", s.Profile);
                s.Python = Str(d, "python", s.Python);
                s.Backend = Str(d, "backend", s.Backend);
                object b;
                if (d.TryGetValue("bounds", out b) && b is object[] && ((object[])b).Length == 4)
                {
                    var a = (object[])b;
                    s.Bounds = new Rectangle(Convert.ToInt32(a[0]), Convert.ToInt32(a[1]), Convert.ToInt32(a[2]), Convert.ToInt32(a[3]));
                }
                object m;
                if (d.TryGetValue("maximized", out m) && m is bool) s.Maximized = (bool)m;
            }
            catch (Exception) { }
        }
        if (args.Length > 0 && File.Exists(args[0])) s.Profile = System.IO.Path.GetFullPath(args[0]);
        s.ReadProfile();
        return s;
    }

    private static string Str(Dictionary<string, object> d, string key, string fallback)
    {
        object v;
        return d.TryGetValue(key, out v) && v is string && ((string)v).Length > 0 ? (string)v : fallback;
    }

    private void ReadProfile()
    {
        if (string.IsNullOrEmpty(Profile) || !File.Exists(Profile)) return;
        try
        {
            var d = new JavaScriptSerializer().Deserialize<Dictionary<string, object>>(File.ReadAllText(Profile));
            ProjectName = Str(d, "name", ProjectName);
            object w;
            if (d.TryGetValue("workers", out w) && w is Dictionary<string, object>)
            {
                var wd = (Dictionary<string, object>)w;
                object p;
                if (wd.TryGetValue("dashboardPort", out p) && p != null) Port = Convert.ToInt32(p);
                StateDir = Str(wd, "stateDir", StateDir);
            }
        }
        catch (Exception) { }
        if (string.IsNullOrEmpty(StateDir))
        {
            string safe = new string(Array.ConvertAll(ProjectName.ToCharArray(), c => char.IsLetterOrDigit(c) || c == '-' || c == '_' ? c : '-')).Trim('-');
            StateDir = System.IO.Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Fletchery", safe.Length > 0 ? safe : "project");
        }
    }

    public void Save()
    {
        var d = new Dictionary<string, object>();
        d["profile"] = Profile; d["python"] = Python; d["backend"] = Backend;
        d["bounds"] = new int[] { Bounds.X, Bounds.Y, Bounds.Width, Bounds.Height };
        d["maximized"] = Maximized;
        try { File.WriteAllText(Path, new JavaScriptSerializer().Serialize(d), new UTF8Encoding(false)); } catch (Exception) { }
    }
}

// Starts and watches `python -m fletchery run --profile <p>`. Never touches a process it did not start.
internal sealed class Backend
{
    private readonly Settings settings;
    private Process process;
    private readonly List<DateTime> crashes = new List<DateTime>();
    private bool stopping;
    public string LastError = "";
    public event Action Changed;

    public Backend(Settings s) { settings = s; }

    public string Url { get { return "http://127.0.0.1:" + settings.Port + "/"; } }

    public bool Answers()
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(Url + "api/status");
            req.Timeout = 3000; req.Proxy = null;
            using (var resp = (HttpWebResponse)req.GetResponse()) return resp.StatusCode == HttpStatusCode.OK;
        }
        catch (Exception) { return false; }
    }

    public string Status()
    {
        try
        {
            var req = (HttpWebRequest)WebRequest.Create(Url + "api/status");
            req.Timeout = 4000; req.Proxy = null;
            using (var resp = (HttpWebResponse)req.GetResponse())
            using (var r = new StreamReader(resp.GetResponseStream(), Encoding.UTF8)) return r.ReadToEnd();
        }
        catch (Exception) { return null; }
    }

    public bool Owned { get { return process != null && !process.HasExited; } }

    public void EnsureRunning()
    {
        if (Answers() || Owned) return;   // already up (perhaps started by Start-Fletchery.ps1): attach, never start a second
        if (string.IsNullOrEmpty(settings.Profile) || !File.Exists(settings.Profile))
        {
            LastError = "No project profile is set. Run Install-Fletchery.ps1 -Profile <file>, or start Fletchery with a profile path.";
            return;
        }
        Directory.CreateDirectory(settings.StateDir);
        var psi = new ProcessStartInfo(settings.Python, "-m fletchery run --profile \"" + settings.Profile + "\"");
        psi.WorkingDirectory = settings.Backend;
        psi.UseShellExecute = false; psi.CreateNoWindow = true;
        psi.RedirectStandardOutput = true; psi.RedirectStandardError = true;
        psi.StandardOutputEncoding = Encoding.UTF8; psi.StandardErrorEncoding = Encoding.UTF8;
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        string log = System.IO.Path.Combine(settings.StateDir, "fletchery.log");
        try
        {
            if (File.Exists(log)) File.Copy(log, System.IO.Path.Combine(settings.StateDir, "fletchery.prev.log"), true);
            File.WriteAllText(log, "");
            process = new Process();
            process.StartInfo = psi;
            process.EnableRaisingEvents = true;
            object gate = new object();
            DataReceivedEventHandler write = (s, e) => { if (e.Data != null) lock (gate) { try { File.AppendAllText(log, e.Data + Environment.NewLine); } catch (Exception) { } } };
            process.OutputDataReceived += write;
            process.ErrorDataReceived += write;
            process.Exited += OnExited;
            process.Start();
            try { process.PriorityClass = ProcessPriorityClass.BelowNormal; } catch (Exception) { }
            process.BeginOutputReadLine(); process.BeginErrorReadLine();
            LastError = "";
        }
        catch (Exception ex)
        {
            LastError = "Could not start the backend (" + settings.Python + "): " + ex.Message;
            process = null;
        }
        if (Changed != null) Changed();
    }

    private void OnExited(object sender, EventArgs e)
    {
        int code = -1;
        try { code = process.ExitCode; } catch (Exception) { }
        if (stopping) return;
        if (code == 75) { EnsureRunning(); return; }   // new code on disk: start again right away
        DateTime now = DateTime.Now;
        crashes.RemoveAll(t => (now - t).TotalMinutes > 5);
        crashes.Add(now);
        string tail = "";
        try
        {
            var lines = File.ReadAllLines(System.IO.Path.Combine(settings.StateDir, "fletchery.log"));
            tail = string.Join("\n", lines, Math.Max(0, lines.Length - 4), Math.Min(4, lines.Length));
        }
        catch (Exception) { }
        LastError = "The backend stopped (exit " + code + "). " + tail;
        if (crashes.Count <= 3) { Thread.Sleep(3000); EnsureRunning(); }
        if (Changed != null) Changed();
    }

    public void Stop()
    {
        stopping = true;
        if (!Owned) return;
        try
        {
            // the backend and the Claude sessions it started, by process tree of the PID we own
            var kill = new ProcessStartInfo("taskkill", "/PID " + process.Id + " /T /F");
            kill.CreateNoWindow = true; kill.UseShellExecute = false;
            using (var k = Process.Start(kill)) k.WaitForExit(10000);
        }
        catch (Exception) { }
    }
}

internal sealed class MainWindow : Form
{
    private readonly Settings settings;
    private readonly Backend backend;
    private readonly WebView2 web = new WebView2();
    private readonly Label message = new Label();
    private readonly NotifyIcon tray = new NotifyIcon();
    private readonly System.Windows.Forms.Timer poll = new System.Windows.Forms.Timer();
    private bool quitting, navigated;
    private readonly HashSet<string> seen = new HashSet<string>();
    private bool firstStatus = true;
    private string lastSignedOut = "";

    public MainWindow(Settings s)
    {
        settings = s;
        backend = new Backend(s);
        Text = "Fletchery: " + s.ProjectName;
        try { Icon = System.Drawing.Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch (Exception) { }
        MinimumSize = new Size(760, 520);
        Size = new Size(1400, 940);
        StartPosition = FormStartPosition.CenterScreen;
        if (s.Bounds.Width > 400 && Screen.FromRectangle(s.Bounds).WorkingArea.IntersectsWith(s.Bounds))
        {
            StartPosition = FormStartPosition.Manual;
            Bounds = s.Bounds;
        }
        if (s.Maximized) WindowState = FormWindowState.Maximized;
        BackColor = Color.FromArgb(0xED, 0xF1, 0xF5);

        message.Dock = DockStyle.Fill;
        message.TextAlign = ContentAlignment.MiddleCenter;
        message.Font = new Font("Segoe UI", 13f);
        message.ForeColor = Color.FromArgb(0x17, 0x21, 0x2B);
        message.Text = "Starting Fletchery…";
        web.Dock = DockStyle.Fill;
        web.Visible = false;
        Controls.Add(web);
        Controls.Add(message);

        tray.Icon = Icon;
        tray.Text = "Fletchery: " + s.ProjectName;
        tray.Visible = true;
        var menu = new ContextMenuStrip();
        menu.Items.Add("Open Fletchery", null, (o, e) => ShowWindow());
        menu.Items.Add("Open in browser", null, (o, e) => OpenExternal(backend.Url));
        menu.Items.Add("Open log folder", null, (o, e) => OpenExternal(settings.StateDir));
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("Quit Fletchery", null, (o, e) => Quit());
        tray.ContextMenuStrip = menu;
        tray.DoubleClick += (o, e) => ShowWindow();
        tray.BalloonTipClicked += (o, e) => ShowWindow();

        backend.Changed += () => { try { BeginInvoke(new Action(UpdateMessage)); } catch (Exception) { } };
        poll.Interval = 4000;
        poll.Tick += (o, e) => Tick();

        // a second Fletchery.exe asks this one to show itself
        var show = new EventWaitHandle(false, EventResetMode.AutoReset, Program.ShowEventName);
        var t = new Thread(() => { while (true) { show.WaitOne(); try { BeginInvoke(new Action(ShowWindow)); } catch (Exception) { } } });
        t.IsBackground = true; t.Start();
    }

    protected override async void OnShown(EventArgs e)
    {
        base.OnShown(e);
        await Task.Run(() => backend.EnsureRunning());
        UpdateMessage();
        try
        {
            string data = Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData), "Fletchery", "WebView2");
            var env = await CoreWebView2Environment.CreateAsync(null, data);
            await web.EnsureCoreWebView2Async(env);
            web.CoreWebView2.Settings.IsStatusBarEnabled = false;
            web.CoreWebView2.Settings.AreDevToolsEnabled = true;
            web.CoreWebView2.NewWindowRequested += (s, a) => { a.Handled = true; OpenExternal(a.Uri); };
            web.CoreWebView2.NavigationStarting += (s, a) =>
            {
                // the app shows its own screens only; anything else opens in the default browser
                if (!a.Uri.StartsWith(backend.Url, StringComparison.OrdinalIgnoreCase)) { a.Cancel = true; OpenExternal(a.Uri); }
            };
            web.CoreWebView2.DocumentTitleChanged += (s, a) => { Text = web.CoreWebView2.DocumentTitle; };
        }
        catch (Exception ex)
        {
            message.Text = "Fletchery needs the Microsoft Edge WebView2 Runtime, which Windows 10 and 11 normally include.\n\n" +
                           ex.Message + "\n\nThe screens are open in your browser instead.";
            OpenExternal(backend.Url);
        }
        poll.Start();
        Tick();
    }

    private void UpdateMessage()
    {
        if (!string.IsNullOrEmpty(backend.LastError)) { message.Text = backend.LastError; web.Visible = false; message.Visible = true; }
    }

    private async void Tick()
    {
        string status = await Task.Run(() => backend.Status());
        if (status == null)
        {
            if (!backend.Owned) await Task.Run(() => backend.EnsureRunning());
            if (!navigated) message.Text = string.IsNullOrEmpty(backend.LastError) ? "Starting Fletchery…" : backend.LastError;
            return;
        }
        if (!navigated && web.CoreWebView2 != null)
        {
            navigated = true;
            web.CoreWebView2.Navigate(backend.Url);
            web.Visible = true; message.Visible = false;
        }
        else if (navigated && !web.Visible && string.IsNullOrEmpty(backend.LastError)) { web.Visible = true; message.Visible = false; web.Reload(); }
        Notify(status);
    }

    // Windows notifications for what needs Alex: a blocked task, a new PR, the sign-in expiring.
    private void Notify(string json)
    {
        Dictionary<string, object> d;
        try { d = new JavaScriptSerializer { MaxJsonLength = 64 * 1024 * 1024 }.Deserialize<Dictionary<string, object>>(json); }
        catch (Exception) { return; }
        var news = new List<string>();
        object tasks;
        if (d.TryGetValue("tasks", out tasks) && tasks is object[])
        {
            foreach (object o in (object[])tasks)
            {
                var t = o as Dictionary<string, object>;
                if (t == null) continue;
                string n = Convert.ToString(t["number"]), st = Convert.ToString(t["status"]), title = Convert.ToString(t["title"]);
                string key = n + ":" + st;
                if (seen.Contains(key)) continue;
                seen.Add(key);
                if (firstStatus) continue;
                if (st == "blocked") news.Add("#" + n + " needs you: " + title);
                if (st == "pr") news.Add("#" + n + " has a PR: " + title);
            }
        }
        object so;
        string signedOut = d.TryGetValue("signed_out", out so) && so is string ? (string)so : "";
        if (signedOut.Length > 0 && signedOut != lastSignedOut) news.Add("Claude needs signing in again: run claude, then /login.");
        lastSignedOut = signedOut;
        firstStatus = false;
        if (news.Count == 0) return;
        tray.BalloonTipTitle = "Fletchery: " + settings.ProjectName;
        tray.BalloonTipText = string.Join("\n", news.ToArray());
        tray.BalloonTipIcon = ToolTipIcon.Info;
        tray.ShowBalloonTip(8000);
    }

    private static void OpenExternal(string target)
    {
        try { Process.Start(new ProcessStartInfo(target) { UseShellExecute = true }); } catch (Exception) { }
    }

    private void ShowWindow()
    {
        Show();
        if (WindowState == FormWindowState.Minimized) WindowState = FormWindowState.Normal;
        Activate();
    }

    protected override void OnFormClosing(FormClosingEventArgs e)
    {
        settings.Maximized = WindowState == FormWindowState.Maximized;
        if (WindowState == FormWindowState.Normal) settings.Bounds = Bounds;
        settings.Save();
        if (!quitting && e.CloseReason == CloseReason.UserClosing)
        {
            // closing the window keeps Fletchery working in the tray; Quit (tray menu) stops it
            e.Cancel = true;
            Hide();
            tray.BalloonTipTitle = "Fletchery keeps working";
            tray.BalloonTipText = "It is in the tray. Right-click the icon to quit.";
            tray.ShowBalloonTip(4000);
            return;
        }
        base.OnFormClosing(e);
    }

    private void Quit()
    {
        string status = backend.Status();
        bool busy = status != null && status.Contains("\"live\": [{");
        if (busy && backend.Owned &&
            MessageBox.Show(this, "Workers are writing code right now. Quit anyway? Their tasks go back to the queue and the next worker continues from their commits.",
                            "Quit Fletchery", MessageBoxButtons.YesNo, MessageBoxIcon.Question) != DialogResult.Yes)
            return;
        quitting = true;
        poll.Stop();
        backend.Stop();
        tray.Visible = false;
        Close();
        Application.Exit();
    }
}
