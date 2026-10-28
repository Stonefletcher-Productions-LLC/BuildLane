using System;
using System.ComponentModel;
using System.Runtime.InteropServices;
using System.Text;

// Start suspended so no child can escape before the kill-on-close job is assigned.
public static class LaneJob {
    [StructLayout(LayoutKind.Sequential, CharSet=CharSet.Unicode)]
    struct Startup { public int size; public string reserved, desktop, title; public int x,y,cx,cy,xc,yc,fill,flags; public short show, reservedSize; public IntPtr reservedPtr, input, output, error; }
    [StructLayout(LayoutKind.Sequential)]
    struct ProcessInfo { public IntPtr process, thread; public uint pid, tid; }
    [StructLayout(LayoutKind.Sequential)]
    struct BasicLimit { public long processTime, jobTime; public uint flags; public UIntPtr minWorking, maxWorking; public uint active; public UIntPtr affinity; public uint priority, scheduling; }
    [StructLayout(LayoutKind.Sequential)]
    struct Io { public ulong readOps, writeOps, otherOps, readBytes, writeBytes, otherBytes; }
    [StructLayout(LayoutKind.Sequential)]
    struct Limits { public BasicLimit basic; public Io io; public UIntPtr processMemory, jobMemory, peakProcess, peakJob; }
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr CreateJobObject(IntPtr attributes, string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool SetInformationJobObject(IntPtr job, int kind, ref Limits limits, uint size);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode, SetLastError=true)] static extern bool CreateProcess(string app, StringBuilder command, IntPtr pa, IntPtr ta, bool inherit, uint flags, IntPtr env, string cwd, ref Startup startup, out ProcessInfo process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool AssignProcessToJobObject(IntPtr job, IntPtr process);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint ResumeThread(IntPtr thread);
    [DllImport("kernel32.dll", SetLastError=true)] static extern uint WaitForSingleObject(IntPtr handle, uint timeout);
    [DllImport("kernel32.dll", SetLastError=true)] static extern bool GetExitCodeProcess(IntPtr process, out uint code);
    [DllImport("kernel32.dll")] static extern bool TerminateProcess(IntPtr process, uint code);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    static void Check(bool result) { if (!result) throw new Win32Exception(Marshal.GetLastWin32Error()); }
    public static int Run(string executable, string arguments, string directory) {
        IntPtr job=CreateJobObject(IntPtr.Zero,null);
        if(job==IntPtr.Zero) throw new Win32Exception(Marshal.GetLastWin32Error());
        ProcessInfo p=new ProcessInfo();
        try {
            Limits limits=new Limits(); limits.basic.flags=0x2000; // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            Check(SetInformationJobObject(job,9,ref limits,(uint)Marshal.SizeOf(typeof(Limits))));
            Startup startup=new Startup(); startup.size=Marshal.SizeOf(typeof(Startup));
            Check(CreateProcess(executable,new StringBuilder("\""+executable+"\" "+arguments),IntPtr.Zero,IntPtr.Zero,false,0x08000004,IntPtr.Zero,directory,ref startup,out p));
            try { Check(AssignProcessToJobObject(job,p.process)); }
            catch { TerminateProcess(p.process,1); throw; }
            if(ResumeThread(p.thread)==0xffffffff) throw new Win32Exception(Marshal.GetLastWin32Error());
            if(WaitForSingleObject(p.process,0xffffffff)!=0) throw new Win32Exception(Marshal.GetLastWin32Error());
            uint code; Check(GetExitCodeProcess(p.process,out code)); return unchecked((int)code);
        } finally {
            // Also terminates descendants left behind after the direct command exits.
            CloseHandle(job);
            if(p.thread!=IntPtr.Zero) CloseHandle(p.thread);
            if(p.process!=IntPtr.Zero) CloseHandle(p.process);
        }
    }
}
