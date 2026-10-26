using System;
using System.Diagnostics;
using System.IO;
using System.Windows.Forms;

[assembly: System.Reflection.AssemblyVersion("1.1.0.0")]
[assembly: System.Reflection.AssemblyFileVersion("1.1.0.0")]

// GUI-subsystem launcher: neither the launcher nor its PowerShell child owns a console.
internal static class BuildLaneLauncher
{
    [STAThread]
    private static void Main(string[] args)
    {
        try
        {
            string root = AppDomain.CurrentDomain.BaseDirectory;
            string profile = args.Length == 0 ? null : Path.GetFullPath(args[0]);
            if (profile != null && !File.Exists(profile)) throw new FileNotFoundException("Project profile not found", profile);
            string script = Path.Combine(root, "LaneApp.ps1");
            if (!File.Exists(script)) throw new FileNotFoundException("Buildlane app not found", script);
            var start = new ProcessStartInfo(Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.System), @"WindowsPowerShell\v1.0\powershell.exe"));
            start.Arguments = "-NoLogo -NoProfile -NonInteractive -STA -ExecutionPolicy Bypass -File \"" + script + "\"" + (profile == null ? "" : " -Profile \"" + profile + "\"");
            start.UseShellExecute = false;
            start.CreateNoWindow = true;
            start.WindowStyle = ProcessWindowStyle.Hidden;
            start.WorkingDirectory = root;
            // PowerShell 7 parents can pass module paths incompatible with Windows PowerShell.
            start.EnvironmentVariables.Remove("PSModulePath");
            using (var process = Process.Start(start))
            {
                process.WaitForExit();
                if (process.ExitCode != 0) throw new Exception("Buildlane could not start. Check the project profile and LaneApp.ps1. Exit code: " + process.ExitCode);
            }
        }
        catch (Exception error) { MessageBox.Show(error.Message, "Buildlane", MessageBoxButtons.OK, MessageBoxIcon.Error); }
    }
}
