using System.ComponentModel;
using System.Runtime.InteropServices;
using Microsoft.Win32.SafeHandles;

// Normal filesystem handles for files created by this receiver only. No process/session APIs.
readonly record struct FileIdentity(uint VolumeSerial,ulong FileIndex,ulong CreationTime);
readonly record struct FileSnapshot(FileIdentity Identity,long Length,ulong LastWrite);

static class OwnedFiles
{
    const uint ReadAttributes=0x80,DeleteAccess=0x10000,OpenExisting=3,OpenReparsePoint=0x00200000;
    const uint ReparseAttribute=0x400,DirectoryAttribute=0x10;
    public static FileSnapshot Snapshot(FileStream stream)=>Inspect(stream.SafeFileHandle);
    public static FileSnapshot Snapshot(string path)
    {
        using var handle=Open(path,ReadAttributes,7); // Read/Write/Delete sharing: metadata inspection only.
        return Inspect(handle);
    }
    public static void Verify(FileSnapshot actual,FileIdentity expected,long? length=null,ulong? lastWrite=null)
    {
        if(actual.Identity!=expected)throw new IOException("An owned file's identity changed; no deletion or replacement is allowed.");
        if(length.HasValue&&actual.Length!=length.Value)throw new IOException("An owned closed file's length changed; no deletion is allowed.");
        if(lastWrite.HasValue&&actual.LastWrite!=lastWrite.Value)throw new IOException("An owned closed file's last-write identity changed; no deletion is allowed.");
    }
    public static DeleteLease OpenClosedForDelete(string path,FileIdentity expected,long length,ulong lastWrite)
    {
        // Exclusive access refuses files still open by a reader/writer. OPEN_REPARSE_POINT
        // inspects the named entry itself instead of following a symlink.
        var handle=Open(path,ReadAttributes|DeleteAccess,0);
        try
        {
            Verify(Inspect(handle),expected,length,lastWrite);
            return new DeleteLease(handle);
        }
        catch{handle.Dispose();throw;}
    }
    public static void DeleteClosedOwnFile(string path,FileSnapshot expected)
    {
        using var deletion=OpenClosedForDelete(path,expected.Identity,expected.Length,expected.LastWrite);
        deletion.MarkDelete();
    }
    static SafeFileHandle Open(string path,uint access,uint sharing)
    {
        var handle=Native.CreateFileW(path,access,sharing,IntPtr.Zero,OpenExisting,OpenReparsePoint,IntPtr.Zero);
        if(handle.IsInvalid){int error=Marshal.GetLastWin32Error();handle.Dispose();throw new Win32Exception(error,"Opening the exact owned file failed; no alternate path is used.");}
        return handle;
    }
    static FileSnapshot Inspect(SafeFileHandle handle)
    {
        if(!Native.GetFileInformationByHandle(handle,out var info))throw new Win32Exception(Marshal.GetLastWin32Error(),"Could not establish the exact owned file identity.");
        if((info.Attributes&(ReparseAttribute|DirectoryAttribute))!=0)throw new IOException("Reparse points and directories are not eligible owned files.");
        ulong index=((ulong)info.FileIndexHigh<<32)|info.FileIndexLow;
        if(index==0)throw new IOException("The filesystem did not supply a usable owned file identity.");
        ulong size=((ulong)info.FileSizeHigh<<32)|info.FileSizeLow;
        return new(new(info.VolumeSerial,index,info.Creation.Value),checked((long)size),info.LastWrite.Value);
    }
    public sealed class DeleteLease:IDisposable
    {
        readonly SafeFileHandle handle;
        internal DeleteLease(SafeFileHandle handle){this.handle=handle;}
        public void MarkDelete()
        {
            var disposition=new Disposition{Delete=1};
            // Delete by the verified exclusive handle, never by a second path lookup.
            if(!Native.SetFileInformationByHandle(handle,4,ref disposition,(uint)Marshal.SizeOf<Disposition>()))throw new Win32Exception(Marshal.GetLastWin32Error(),"Deleting the verified owned closed file failed.");
        }
        public void Dispose()=>handle.Dispose();
    }
    [StructLayout(LayoutKind.Sequential)]struct NativeTime
    {
        public uint Low,High;
        public readonly ulong Value=>((ulong)High<<32)|Low;
    }
    [StructLayout(LayoutKind.Sequential)]struct HandleInfo
    {
        public uint Attributes;public NativeTime Creation,LastAccess,LastWrite;
        public uint VolumeSerial,FileSizeHigh,FileSizeLow,Links,FileIndexHigh,FileIndexLow;
    }
    [StructLayout(LayoutKind.Sequential)]struct Disposition
    {
        // FILE_DISPOSITION_INFO.DeleteFile is the one-byte native BOOLEAN.
        public byte Delete;
    }
    static class Native
    {
        [DllImport("kernel32.dll",CharSet=CharSet.Unicode,ExactSpelling=true,SetLastError=true)]
        internal static extern SafeFileHandle CreateFileW(string name,uint access,uint sharing,IntPtr security,uint creation,uint flags,IntPtr template);
        [DllImport("kernel32.dll",ExactSpelling=true,SetLastError=true)]
        [return:MarshalAs(UnmanagedType.Bool)]internal static extern bool GetFileInformationByHandle(SafeFileHandle handle,out HandleInfo info);
        [DllImport("kernel32.dll",ExactSpelling=true,SetLastError=true)]
        [return:MarshalAs(UnmanagedType.Bool)]internal static extern bool SetFileInformationByHandle(SafeFileHandle handle,int informationClass,ref Disposition information,uint size);
    }
}
