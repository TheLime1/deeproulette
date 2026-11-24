using System.Runtime.InteropServices;
using System.Text.Json;
using System.ComponentModel;

// QueryAllTraces metadata only. No StartTrace, ControlTrace UPDATE/STOP,
// provider enable/disable, driver configuration or packet-filter calls.
string? Arg(string name){int i=Array.IndexOf(args,name);return i>=0&&i+1<args.Length?args[i+1]:null;}
if(args.Contains("--layout"))
{
    Console.WriteLine(JsonSerializer.Serialize(new{kind="layout",pointerSize=IntPtr.Size,size=Marshal.SizeOf<Properties>(),fields=typeof(Properties).GetFields().Select(f=>new{f.Name,offset=Marshal.OffsetOf<Properties>(f.Name).ToInt64()})}));return;
}
if(!Guid.TryParse(Arg("--expected-guid"),out Guid expected)||expected==Guid.Empty)
    throw new ArgumentException("A nonempty fresh CIM session GUID is required.");
if(IntPtr.Size!=8||Marshal.SizeOf<Properties>()!=120)throw new PlatformNotSupportedException("Verified x64 EVENT_TRACE_PROPERTIES ABI required.");
var buffers=new List<QueryBuffer>();
try
{
    const uint limit=128;
    for(int i=0;i<limit;i++)buffers.Add(new QueryBuffer());
    uint code=Native.QueryAllTracesW(buffers.Select(b=>b.Pointer).ToArray(),limit,out uint count);
    if(code!=0)throw new Win32Exception((int)code,"QueryAllTraces failed; no session name guessed.");
    if(count>limit)throw new InvalidOperationException("Session discovery exceeded the fixed bound.");
    var matches=new List<object>();
    for(int i=0;i<count;i++)
    {
        var p=buffers[i].Read();string logger=buffers[i].LoggerName();
        bool wnode=p.Wnode.Guid==expected;
        bool literal=Guid.TryParse(logger,out Guid loggerGuid)&&loggerGuid==expected;
        if(!wnode&&!literal)continue;
        matches.Add(new{loggerName=logger,wnodeGuid=p.Wnode.Guid,matchWnodeGuid=wnode,
                        matchLiteralLoggerGuid=literal,realtime=(p.LogFileMode&0x100)!=0,
                        modeHex="0x"+p.LogFileMode.ToString("X8"),bufferSizeKb=p.BufferSize,
                        p.MinimumBuffers,p.MaximumBuffers,p.FlushTimer,p.EventsLost,p.BuffersWritten,
                        p.LogBuffersLost,p.RealTimeBuffersLost,logFileName=buffers[i].FileName()});
    }
    bool unique=matches.Count==1;
    Console.WriteLine(JsonSerializer.Serialize(new{kind="guid_query",expectedCimGuid=expected,sessionCount=count,
        uniqueMatch=unique,matches,note="Only actual QueryAllTraces logger names are returned; no friendly-name or GUID-prefix construction."}));
    if(!unique)Environment.ExitCode=3;
}
catch(Exception e)
{
    Console.WriteLine(JsonSerializer.Serialize(new{kind="error",expectedCimGuid=expected,message=e.Message,
        nativeCode=e is Win32Exception w?w.NativeErrorCode:0,traceActionsPerformed=false}));Environment.ExitCode=2;
}
finally{foreach(var buffer in buffers)buffer.Dispose();}

sealed class QueryBuffer:IDisposable
{
    public IntPtr Pointer{get;}const uint Capacity=65536;
    public QueryBuffer()
    {
        Pointer=Marshal.AllocHGlobal((int)Capacity);Marshal.Copy(new byte[(int)Capacity],0,Pointer,(int)Capacity);
        Properties p=new(){Wnode=new(){BufferSize=Capacity,Flags=0x20000},LoggerNameOffset=120,LogFileNameOffset=4216};
        Marshal.StructureToPtr(p,Pointer,false);
    }
    public Properties Read()=>Marshal.PtrToStructure<Properties>(Pointer);
    string At(uint offset)
    {
        if(offset<120||offset>=Capacity||offset%2!=0)return "";
        int length=(int)Math.Min((Capacity-offset)/2,4096);
        string value=Marshal.PtrToStringUni(IntPtr.Add(Pointer,(int)offset),length)??"";
        int end=value.IndexOf('\0');return end<0?value:value[..end];
    }
    public string LoggerName()=>At(Read().LoggerNameOffset);
    public string FileName()=>At(Read().LogFileNameOffset);
    public void Dispose()=>Marshal.FreeHGlobal(Pointer);
}
[StructLayout(LayoutKind.Sequential)]struct WnodeHeader
{
    public uint BufferSize,ProviderId;public ulong HistoricalContext,TimeStamp;public Guid Guid;public uint ClientContext,Flags;
}
[StructLayout(LayoutKind.Sequential)]struct Properties
{
    public WnodeHeader Wnode;public uint BufferSize,MinimumBuffers,MaximumBuffers,MaximumFileSize,LogFileMode,FlushTimer,EnableFlags;public int AgeLimit;
    public uint NumberOfBuffers,FreeBuffers,EventsLost,BuffersWritten,LogBuffersLost,RealTimeBuffersLost;
    public IntPtr LoggerThreadId;public uint LogFileNameOffset,LoggerNameOffset;
}
static class Native
{
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,ExactSpelling=true)]
    public static extern uint QueryAllTracesW([In,Out]IntPtr[] properties,uint count,out uint loggerCount);
}
