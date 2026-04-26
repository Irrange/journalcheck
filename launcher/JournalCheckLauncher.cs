using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Linq;
using System.Windows.Forms;

internal static class JournalCheckLauncher
{
    [STAThread]
    private static int Main(string[] args)
    {
        string baseDir = AppDomain.CurrentDomain.BaseDirectory;
        List<string> roots = new List<string>();
        if (!string.IsNullOrWhiteSpace(baseDir))
        {
            roots.Add(baseDir);
        }
        DirectoryInfo parent = Directory.GetParent(baseDir);
        if (parent != null && !string.IsNullOrWhiteSpace(parent.FullName))
        {
            roots.Add(parent.FullName);
        }
        roots = roots.Distinct(StringComparer.OrdinalIgnoreCase).ToList();

        string scriptPath = null;
        foreach (string root in roots)
        {
            string candidate = Path.Combine(root, "main_gui.py");
            if (File.Exists(candidate))
            {
                scriptPath = candidate;
                break;
            }
        }

        if (string.IsNullOrEmpty(scriptPath))
        {
            ShowError("Could not find main_gui.py next to the launcher or in its parent folder.");
            return 1;
        }

        string projectRoot = Path.GetDirectoryName(scriptPath);
        PythonCandidate python = FindUsablePython(projectRoot);
        if (python == null)
        {
            ShowError("Could not find a usable Python runtime. JournalCheck needs a Python that can import requests, bs4, lxml, and tkinter.");
            return 1;
        }

        foreach (string arg in args)
        {
            if (string.Equals(arg, "--check-only", StringComparison.OrdinalIgnoreCase))
            {
                return 0;
            }
        }

        string finalArgs = Quote(scriptPath);
        if (args != null && args.Length > 0)
        {
            finalArgs += " " + string.Join(" ", args.Select(Quote).ToArray());
        }

        try
        {
            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = python.RunPath;
            info.Arguments = finalArgs;
            info.WorkingDirectory = projectRoot;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            Process.Start(info);
            return 0;
        }
        catch (Exception ex)
        {
            ShowError("Failed to start JournalCheck.\n\n" + ex.Message);
            return 1;
        }
    }

    private static PythonCandidate FindUsablePython(string projectRoot)
    {
        List<PythonCandidate> candidates = new List<PythonCandidate>();
        string[] localCandidates = new[]
        {
            Path.Combine(projectRoot, ".venv", "Scripts", "python.exe"),
            Path.Combine(projectRoot, ".venv", "Scripts", "pythonw.exe"),
        };
        foreach (string path in localCandidates)
        {
            if (File.Exists(path))
            {
                candidates.Add(BuildCandidate(path));
            }
        }

        AddPathCandidate(candidates, "python.exe");
        AddPathCandidate(candidates, "pythonw.exe");

        foreach (PythonCandidate candidate in candidates)
        {
            if (ProbePython(candidate.ProbePath, projectRoot))
            {
                return candidate;
            }
        }
        return null;
    }

    private static void AddPathCandidate(List<PythonCandidate> candidates, string executable)
    {
        string found = FindOnPath(executable);
        if (!string.IsNullOrEmpty(found) && candidates.All(c => !string.Equals(c.ProbePath, found, StringComparison.OrdinalIgnoreCase)))
        {
            candidates.Add(BuildCandidate(found));
        }
    }

    private static PythonCandidate BuildCandidate(string path)
    {
        string directory = Path.GetDirectoryName(path) ?? string.Empty;
        string fileName = Path.GetFileName(path) ?? string.Empty;
        bool isPythonw = string.Equals(fileName, "pythonw.exe", StringComparison.OrdinalIgnoreCase);
        string probePath = path;
        string runPath = path;
        if (isPythonw)
        {
            string pairedPython = Path.Combine(directory, "python.exe");
            if (File.Exists(pairedPython))
            {
                probePath = pairedPython;
            }
        }
        else
        {
            string pairedPythonw = Path.Combine(directory, "pythonw.exe");
            if (File.Exists(pairedPythonw))
            {
                runPath = pairedPythonw;
            }
        }
        return new PythonCandidate(probePath, runPath);
    }

    private static bool ProbePython(string pythonPath, string projectRoot)
    {
        try
        {
            string code =
                "import sys; " +
                "sys.path.insert(0, r'" + EscapePython(projectRoot) + "'); " +
                "from journalcheck.bootstrap import bootstrap_vendor; " +
                "bootstrap_vendor(); " +
                "import requests, bs4, lxml, tkinter";

            ProcessStartInfo info = new ProcessStartInfo();
            info.FileName = pythonPath;
            info.Arguments = "-c " + Quote(code);
            info.WorkingDirectory = projectRoot;
            info.UseShellExecute = false;
            info.CreateNoWindow = true;
            info.RedirectStandardError = true;
            info.RedirectStandardOutput = true;
            using (Process process = Process.Start(info))
            {
                if (process == null)
                {
                    return false;
                }
                if (!process.WaitForExit(15000))
                {
                    try { process.Kill(); } catch { }
                    return false;
                }
                return process.ExitCode == 0;
            }
        }
        catch
        {
            return false;
        }
    }

    private static string FindOnPath(string executable)
    {
        string pathValue = Environment.GetEnvironmentVariable("PATH") ?? string.Empty;
        foreach (string path in pathValue.Split(new[] { ';' }, StringSplitOptions.RemoveEmptyEntries))
        {
            try
            {
                string candidate = Path.Combine(path.Trim(), executable);
                if (File.Exists(candidate))
                {
                    return candidate;
                }
            }
            catch
            {
            }
        }
        return null;
    }

    private static string EscapePython(string value)
    {
        return value.Replace("\\", "\\\\").Replace("'", "\\'");
    }

    private static string Quote(string value)
    {
        if (string.IsNullOrEmpty(value))
        {
            return "\"\"";
        }
        return "\"" + value.Replace("\"", "\\\"") + "\"";
    }

    private static void ShowError(string message)
    {
        MessageBox.Show(message, "JournalCheck", MessageBoxButtons.OK, MessageBoxIcon.Error);
    }

    private sealed class PythonCandidate
    {
        public PythonCandidate(string probePath, string runPath)
        {
            ProbePath = probePath;
            RunPath = runPath;
        }

        public string ProbePath { get; private set; }
        public string RunPath { get; private set; }
    }
}
