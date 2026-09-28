// Windows-tjänst för Predikan -> Podcast.
//
// En liten "omslutning" som Windows tjänsthanterare kan starta och stoppa.
// Själva appen är Python (uvicorn), som inte själv kan prata med
// tjänsthanteraren - den här tjänsten startar därför Python som en
// underprocess, startar om den om den skulle krascha och stoppar den (med
// alla dess underprocesser, t.ex. transkriberingen) när tjänsten stoppas.
//
// Kompileras av install-service.ps1 med C#-kompilatorn som redan finns i
// Windows (.NET Framework 4.8) - inga nedladdade program behövs.
//
// Inställningarna läses från service.config i samma mapp (skrivs av
// install-service.ps1), en rad per inställning:
//   Python=C:\PredikanApp\venv\Scripts\python.exe
//   Arguments=-m uvicorn app:app --host 127.0.0.1 --port 8000
//   WorkingDirectory=C:\PredikanApp
//   PathPrepend=C:\ffmpeg\bin
//   LogFile=C:\PredikanApp\logs\service.log
//   env.NAMN=värde           (miljövariabel till appen)
//
// Provköra utan att installera som tjänst:  PredikanService.exe --console

using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.ServiceProcess;
using System.Text;
using System.Threading;

public class PredikanService : ServiceBase
{
    public const string Name = "PredikanApp";

    // Loggfilen roteras (döps om till .1) när den blir större än så här.
    const long MaxLogBytes = 10L * 1024 * 1024;

    readonly Dictionary<string, string> settings = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    readonly Dictionary<string, string> env = new Dictionary<string, string>(StringComparer.OrdinalIgnoreCase);
    readonly object logLock = new object();
    readonly ManualResetEvent stopping = new ManualResetEvent(false);
    Thread supervisor;
    Process child;

    public PredikanService()
    {
        ServiceName = Name;
        CanStop = true;
        CanShutdown = true;
        AutoLog = true;
    }

    public static int Main(string[] args)
    {
        var service = new PredikanService();
        if (args.Length > 0 && args[0] == "--console")
        {
            // Provkörning i ett vanligt fönster: Ctrl+C stoppar.
            service.StartSupervisor();
            Console.WriteLine("Predikan-tjänsten körs i konsolläge. Tryck Ctrl+C för att stoppa.");
            var done = new ManualResetEvent(false);
            Console.CancelKeyPress += (s, e) => { e.Cancel = true; done.Set(); };
            done.WaitOne();
            service.StopSupervisor();
            return 0;
        }
        ServiceBase.Run(service);
        return 0;
    }

    protected override void OnStart(string[] args) { StartSupervisor(); }
    protected override void OnStop() { StopSupervisor(); }
    protected override void OnShutdown() { StopSupervisor(); }

    void StartSupervisor()
    {
        LoadConfig();
        supervisor = new Thread(Supervise) { IsBackground = true, Name = "supervisor" };
        supervisor.Start();
    }

    void StopSupervisor()
    {
        // Ge tjänsthanteraren besked om att stoppet kan ta några sekunder.
        try { RequestAdditionalTime(30000); } catch { }
        stopping.Set();
        KillChild();
        if (supervisor != null) supervisor.Join(20000);
        Log("Tjänsten stoppad.");
    }

    void LoadConfig()
    {
        string dir = AppDomain.CurrentDomain.BaseDirectory;
        string path = Path.Combine(dir, "service.config");
        foreach (string raw in File.ReadAllLines(path, Encoding.UTF8))
        {
            string line = raw.Trim();
            if (line.Length == 0 || line.StartsWith("#")) continue;
            int eq = line.IndexOf('=');
            if (eq <= 0) continue;
            string key = line.Substring(0, eq).Trim();
            string value = line.Substring(eq + 1).Trim();
            if (key.StartsWith("env.", StringComparison.OrdinalIgnoreCase))
                env[key.Substring(4)] = value;
            else
                settings[key] = value;
        }
        foreach (string required in new[] { "Python", "Arguments", "WorkingDirectory" })
        {
            if (!settings.ContainsKey(required))
                throw new InvalidOperationException("service.config saknar " + required);
        }
    }

    string Setting(string key, string fallback)
    {
        string value;
        return settings.TryGetValue(key, out value) && value.Length > 0 ? value : fallback;
    }

    // Startar appen och startar om den om den avslutas utan att tjänsten
    // stoppats. Väntetiden mellan försöken ökar (5 s upp till 60 s), så att
    // ett fel som kvarstår (t.ex. en trasig .env) inte ger en tät loop.
    void Supervise()
    {
        int delaySeconds = 5;
        while (!stopping.WaitOne(0))
        {
            DateTime started = DateTime.Now;
            try
            {
                RunChildOnce();
            }
            catch (Exception ex)
            {
                Log("Kunde inte starta appen: " + ex.Message);
            }
            if (stopping.WaitOne(0)) break;

            // Körde appen en stund innan den föll räknas det som ett nytt fel,
            // inte samma fel igen - börja om med kort väntetid.
            if ((DateTime.Now - started).TotalMinutes > 5) delaySeconds = 5;
            Log("Appen avslutades oväntat - startar om om " + delaySeconds + " s.");
            if (stopping.WaitOne(delaySeconds * 1000)) break;
            delaySeconds = Math.Min(delaySeconds * 2, 60);
        }
    }

    void RunChildOnce()
    {
        var psi = new ProcessStartInfo
        {
            FileName = Setting("Python", null),
            Arguments = Setting("Arguments", ""),
            WorkingDirectory = Setting("WorkingDirectory", null),
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            StandardOutputEncoding = Encoding.UTF8,
            StandardErrorEncoding = Encoding.UTF8,
        };
        string prepend = Setting("PathPrepend", "");
        if (prepend.Length > 0)
            psi.EnvironmentVariables["PATH"] = prepend + ";" + psi.EnvironmentVariables["PATH"];
        // Pythons utskrifter som UTF-8 (å, ä, ö) och utan fördröjning i loggen.
        psi.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
        psi.EnvironmentVariables["PYTHONUNBUFFERED"] = "1";
        foreach (var pair in env) psi.EnvironmentVariables[pair.Key] = pair.Value;

        Log("Startar: " + psi.FileName + " " + psi.Arguments);
        var p = new Process { StartInfo = psi };
        p.OutputDataReceived += (s, e) => { if (e.Data != null) Log(e.Data); };
        p.ErrorDataReceived += (s, e) => { if (e.Data != null) Log(e.Data); };
        p.Start();
        ChildJob.Add(p);
        child = p;
        p.BeginOutputReadLine();
        p.BeginErrorReadLine();
        p.WaitForExit();
        Log("Appen avslutades med kod " + p.ExitCode + ".");
        child = null;
    }

    // Stoppar appen med alla dess underprocesser. venv:ens python.exe är en
    // startare som i sin tur startar den riktiga Python-processen, och
    // transkriberingen körs i en egen process - taskkill /T tar hela trädet.
    void KillChild()
    {
        Process p = child;
        if (p == null) return;
        try
        {
            if (p.HasExited) return;
            var kill = Process.Start(new ProcessStartInfo
            {
                FileName = "taskkill.exe",
                Arguments = "/PID " + p.Id + " /T /F",
                UseShellExecute = false,
                CreateNoWindow = true,
            });
            kill.WaitForExit(15000);
            p.WaitForExit(15000);
        }
        catch (Exception ex)
        {
            Log("Kunde inte stoppa appen: " + ex.Message);
        }
    }

    // Ett Windows-jobbobjekt som appen och alla dess underprocesser läggs i.
    // Stängs jobbet - vilket Windows gör automatiskt när den här processen
    // avslutas, även vid en krasch - dör hela trädet. Annars skulle en
    // kvarglömd Python-process kunna hålla porten upptagen, så att appen
    // inte går att starta igen.
    static class ChildJob
    {
        [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
        static extern IntPtr CreateJobObject(IntPtr attributes, string name);

        [DllImport("kernel32.dll")]
        static extern bool SetInformationJobObject(IntPtr job, int infoClass, IntPtr info, uint length);

        [DllImport("kernel32.dll")]
        static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);

        [StructLayout(LayoutKind.Sequential)]
        struct BasicLimits
        {
            public long PerProcessUserTimeLimit;
            public long PerJobUserTimeLimit;
            public uint LimitFlags;
            public UIntPtr MinimumWorkingSetSize;
            public UIntPtr MaximumWorkingSetSize;
            public uint ActiveProcessLimit;
            public UIntPtr Affinity;
            public uint PriorityClass;
            public uint SchedulingClass;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct IoCounters
        {
            public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
            public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
        }

        [StructLayout(LayoutKind.Sequential)]
        struct ExtendedLimits
        {
            public BasicLimits BasicLimitInformation;
            public IoCounters IoInfo;
            public UIntPtr ProcessMemoryLimit;
            public UIntPtr JobMemoryLimit;
            public UIntPtr PeakProcessMemoryUsed;
            public UIntPtr PeakJobMemoryUsed;
        }

        const int ExtendedLimitInformation = 9;
        const uint KillOnJobClose = 0x2000;

        static readonly IntPtr job = Create();

        static IntPtr Create()
        {
            IntPtr handle = CreateJobObject(IntPtr.Zero, null);
            var limits = new ExtendedLimits();
            limits.BasicLimitInformation.LimitFlags = KillOnJobClose;
            int size = Marshal.SizeOf(typeof(ExtendedLimits));
            IntPtr buffer = Marshal.AllocHGlobal(size);
            try
            {
                Marshal.StructureToPtr(limits, buffer, false);
                SetInformationJobObject(handle, ExtendedLimitInformation, buffer, (uint)size);
            }
            finally
            {
                Marshal.FreeHGlobal(buffer);
            }
            return handle;
        }

        public static void Add(Process process)
        {
            // Underprocesser som startas efter detta hamnar automatiskt i samma jobb.
            AssignProcessToJobObject(job, process.Handle);
        }
    }

    void Log(string message)
    {
        string line = DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message;
        string path = Setting("LogFile", null);
        if (path == null)
        {
            Console.WriteLine(line);
            return;
        }
        lock (logLock)
        {
            try
            {
                Directory.CreateDirectory(Path.GetDirectoryName(path));
                var info = new FileInfo(path);
                if (info.Exists && info.Length > MaxLogBytes)
                {
                    string old = path + ".1";
                    if (File.Exists(old)) File.Delete(old);
                    File.Move(path, old);
                }
                File.AppendAllText(path, line + Environment.NewLine, Encoding.UTF8);
            }
            catch
            {
                // Loggning får aldrig fälla tjänsten.
            }
            if (Environment.UserInteractive) Console.WriteLine(line);
        }
    }
}
