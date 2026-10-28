using System;
using System.Runtime.InteropServices;

// The window is hosted by PowerShell, so Explorer needs an explicit application
// identity and relaunch target instead of inferring powershell.exe's branding.
public static class LaneTaskbar
{
    public const string AppId = "Stonefletcher.BuildLane.Desktop";
    private static readonly Guid PropertySet = new Guid("9F4C2855-9F79-4B39-A8D0-E1D42DE1D5F3");
    private static readonly Guid StoreId = new Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99");

    [StructLayout(LayoutKind.Sequential)]
    private struct PropertyKey { public Guid Format; public uint Id; }
    [StructLayout(LayoutKind.Explicit, Size = 24)]
    private struct Variant
    {
        [FieldOffset(0)] public ushort Type;
        [FieldOffset(8)] public IntPtr Text;
    }
    [ComImport, Guid("886D8EEB-8CF2-4446-8D02-CDBA1DBDCF99"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
    private interface IPropertyStore
    {
        [PreserveSig] int GetCount(out uint count);
        [PreserveSig] int GetAt(uint index, out PropertyKey key);
        [PreserveSig] int GetValue(ref PropertyKey key, out Variant value);
        [PreserveSig] int SetValue(ref PropertyKey key, ref Variant value);
        [PreserveSig] int Commit();
    }
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SetCurrentProcessExplicitAppUserModelID(string id);
    [DllImport("shell32.dll")]
    private static extern int SHGetPropertyStoreForWindow(IntPtr window, ref Guid iid, out IPropertyStore store);
    [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
    private static extern int SHGetPropertyStoreFromParsingName(string path, IntPtr binding, uint flags, ref Guid iid, out IPropertyStore store);
    [DllImport("ole32.dll")]
    private static extern int PropVariantClear(ref Variant value);

    public static void InitializeProcess()
    {
        Marshal.ThrowExceptionForHR(SetCurrentProcessExplicitAppUserModelID(AppId));
    }
    private static void Set(IPropertyStore store, uint id, string text)
    {
        var key = new PropertyKey { Format = PropertySet, Id = id };
        var value = new Variant { Type = 31, Text = Marshal.StringToCoTaskMemUni(text) };
        try { Marshal.ThrowExceptionForHR(store.SetValue(ref key, ref value)); }
        finally { PropVariantClear(ref value); }
    }
    private static string Get(IPropertyStore store, uint id)
    {
        var key = new PropertyKey { Format = PropertySet, Id = id };
        Variant value;
        Marshal.ThrowExceptionForHR(store.GetValue(ref key, out value));
        try { return value.Type == 31 ? Marshal.PtrToStringUni(value.Text) : null; }
        finally { PropVariantClear(ref value); }
    }
    public static void ConfigureWindow(IntPtr window, string command, string icon)
    {
        IPropertyStore store; Guid iid = StoreId;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreForWindow(window, ref iid, out store));
        try
        {
            // Set relaunch properties before identity, as required by the Shell.
            Set(store, 2, command);
            Set(store, 3, icon + ",0");
            Set(store, 4, "Buildlane");
            Set(store, 5, AppId);
            Marshal.ThrowExceptionForHR(store.Commit());
        }
        finally { Marshal.FinalReleaseComObject(store); }
    }
    public static string WindowProperty(IntPtr window, uint id)
    {
        IPropertyStore store; Guid iid = StoreId;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreForWindow(window, ref iid, out store));
        try { return Get(store, id); }
        finally { Marshal.FinalReleaseComObject(store); }
    }
    public static void ConfigureShortcut(string path)
    {
        IPropertyStore store; Guid iid = StoreId;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 2, ref iid, out store));
        try { Set(store, 5, AppId); Marshal.ThrowExceptionForHR(store.Commit()); }
        finally { Marshal.FinalReleaseComObject(store); }
    }
    public static string ShortcutIdentity(string path)
    {
        IPropertyStore store; Guid iid = StoreId;
        Marshal.ThrowExceptionForHR(SHGetPropertyStoreFromParsingName(path, IntPtr.Zero, 0, ref iid, out store));
        try { return Get(store, 5); }
        finally { Marshal.FinalReleaseComObject(store); }
    }
}
