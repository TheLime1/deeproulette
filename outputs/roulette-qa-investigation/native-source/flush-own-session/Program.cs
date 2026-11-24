using System.ComponentModel;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Text;
using System.Text.Json;

// STAGED, NOT ACTIVATED. Normal ETW QUERY + FLUSH only. No mode/provider/filter
// calls and no trace-session start/stop. Root must validate before deployment.
return Run(args);

static int Run(string[] args)
{
    Options options;
    try{options=Parse(args);}
    catch(Exception error)
    {
        Console.WriteLine(JsonSerializer.Serialize(new{kind="flush_summary",state="argument_error",error=error.Message,
            helperPid=Environment.ProcessId,queryCount=0,flushCount=0,traceActionsPerformed=false}));
        return 2;
    }
    Process? owner=null;string? ownerStartUtc=null;string? session=null;string state="starting";
    string? errorText=null;int nativeError=0;int queryCount=0;long flushCount=0;object? lastQuery=null;
    bool cancelled=false,readyWritten=false;int result=0;
    var processTimer=Stopwatch.StartNew();var loopTimer=new Stopwatch();
    ConsoleCancelEventHandler cancel=(_,eventArgs)=>{eventArgs.Cancel=true;cancelled=true;};
    Console.CancelKeyPress+=cancel;
    try
    {
        if(IntPtr.Size!=8||Marshal.SizeOf<Properties>()!=120)
            throw new PlatformNotSupportedException("Verified x64 EVENT_TRACE_PROPERTIES layout required.");
        if(options.OwnerPid is int ownerPid)
        {
            // Capture this Process object once. Enabling exit notifications
            // retains a synchronization wait on this original owner process;
            // the loop never looks up a possibly reused PID again.
            owner=Process.GetProcessById(ownerPid);
            owner.EnableRaisingEvents=true;
            if(owner.HasExited){state="owner_exited";return result;}
            ownerStartUtc=owner.StartTime.ToUniversalTime().ToString("O");
        }
        string? LeaseReason()
        {
            if(owner is not null&&owner.HasExited)return "owner_exited";
            if(options.StopFile is not null&&StopFileExists(options.StopFile))return "stop_file";
            return null;
        }
        string? initialLease=LeaseReason();
        if(initialLease is not null){state=initialLease;return result;}
        session=FindOwned(options.ExpectedGuid);
        using var buffer=new QueryBuffer();
        Properties QueryOwned()
        {
            buffer.Reset();uint code=Native.ControlTraceW(0,session,buffer.Pointer,0);
            if(code!=0)throw new Win32Exception((int)code,"Owned logger QUERY failed; no further flush authorized.");
            queryCount++;var value=buffer.Read();
            if(value.Wnode.Guid!=options.ExpectedGuid||!string.Equals(buffer.LoggerName(),session,StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("Owned logger GUID/name changed; no further flush authorized.");
            if((value.LogFileMode&0x100)==0)
                throw new InvalidOperationException("Owned logger is no longer realtime; no further flush authorized.");
            lastQuery=View(value);return value;
        }
        var initial=QueryOwned();
        initialLease=LeaseReason();
        if(initialLease is not null){state=initialLease;return result;}
        var ready=new{kind="flush_ready",expectedGuid=options.ExpectedGuid,session,helperPid=Environment.ProcessId,
            ownerPid=options.OwnerPid,ownerStartUtc,stopFile=options.StopFile,intervalMs=options.IntervalMs,
            maxSeconds=options.MaxSeconds,utc=DateTime.UtcNow.ToString("O"),qpc=Stopwatch.GetTimestamp(),
            qpcFrequency=Stopwatch.Frequency,initial=View(initial),quiet=!options.Verbose,
            normalBufferFlushOnly=true,producerLifetimeControlled=false};
        string readyJson=JsonSerializer.Serialize(ready);
        if(options.ReadyFile is not null){WriteReady(options.ReadyFile,readyJson);readyWritten=true;}
        Console.WriteLine(readyJson);
        state="running";loopTimer.Start();
        while(true)
        {
            if(cancelled){state="cancelled";break;}
            string? lease=LeaseReason();
            if(lease is not null){state=lease;break;}
            if(options.MaxSeconds>0&&loopTimer.Elapsed.TotalSeconds>=options.MaxSeconds){state="bounded_complete";break;}
            var queryUtc=DateTime.UtcNow;long queryQpc=Stopwatch.GetTimestamp();
            var before=QueryOwned();long queriedQpc=Stopwatch.GetTimestamp();
            // Check the independent lease again after QUERY and immediately
            // before FLUSH. No lease failure triggers another name or retry.
            lease=LeaseReason();
            if(lease is not null){state=lease;break;}
            buffer.EnsureCapacityHeader();
            uint code=Native.ControlTraceW(0,session,buffer.Pointer,3); // Installed EVENT_TRACE_CONTROL_FLUSH.
            long flushedQpc=Stopwatch.GetTimestamp();
            if(code!=0)throw new Win32Exception((int)code,"Owned logger FLUSH failed; no further flush authorized.");
            flushCount++;
            if(options.Verbose)
                Console.WriteLine(JsonSerializer.Serialize(new{kind="flush_iteration",iteration=flushCount,session,
                    utc=queryUtc.ToString("O"),queryQpc,queriedQpc,flushedQpc,qpcFrequency=Stopwatch.Frequency,
                    queryDurationMs=(queriedQpc-queryQpc)*1000d/Stopwatch.Frequency,
                    flushDurationMs=(flushedQpc-queriedQpc)*1000d/Stopwatch.Frequency,
                    ownershipVerifiedBeforeFlush=true,query=View(before)}));
            int sleep=options.IntervalMs;
            if(options.MaxSeconds>0)
                sleep=(int)Math.Ceiling(Math.Min(sleep,Math.Max(0,(options.MaxSeconds-loopTimer.Elapsed.TotalSeconds)*1000)));
            if(sleep>0)Thread.Sleep(sleep);
        }
    }
    catch(Exception error)
    {
        state="stopped_on_error";errorText=error.Message;nativeError=error is Win32Exception win?win.NativeErrorCode:0;result=2;
    }
    finally
    {
        Console.CancelKeyPress-=cancel;
        owner?.Dispose();
        Console.WriteLine(JsonSerializer.Serialize(new{kind="flush_summary",session,expectedGuid=options.ExpectedGuid,
            helperPid=Environment.ProcessId,ownerPid=options.OwnerPid,ownerStartUtc,stopFile=options.StopFile,state,
            utc=DateTime.UtcNow.ToString("O"),queryCount,flushCount,elapsedMs=processTimer.Elapsed.TotalMilliseconds,
            loopElapsedMs=loopTimer.Elapsed.TotalMilliseconds,intervalMs=options.IntervalMs,maxSeconds=options.MaxSeconds,
            nativeError,error=errorText,readyWritten,lastQuery,sessionStopped=false,providerOrFilterCalls=false,
            modeUpdateCalls=false}));
    }
    return result;
}

static Options Parse(string[] args)
{
    var values=new Dictionary<string,string>(StringComparer.Ordinal);
    var flags=new HashSet<string>(StringComparer.Ordinal);
    var valueNames=new HashSet<string>{"--expected-guid","--interval-ms","--max-seconds","--owner-pid","--stop-file","--ready-file"};
    var flagNames=new HashSet<string>{"--flush-loop","--quiet","--verbose"};
    for(int i=0;i<args.Length;i++)
    {
        string key=args[i];
        if(flagNames.Contains(key)){if(!flags.Add(key))throw new ArgumentException("Duplicate option: "+key);continue;}
        if(!valueNames.Contains(key)||i+1>=args.Length||args[i+1].StartsWith("--",StringComparison.Ordinal))
            throw new ArgumentException("Unknown or incomplete option: "+key);
        if(!values.TryAdd(key,args[++i]))throw new ArgumentException("Duplicate option: "+key);
    }
    if(!flags.Contains("--flush-loop"))throw new ArgumentException("Explicit --flush-loop is required.");
    if(flags.Contains("--quiet")&&flags.Contains("--verbose"))throw new ArgumentException("Choose --quiet or --verbose.");
    if(!values.TryGetValue("--expected-guid",out string? guidText)||!Guid.TryParse(guidText,out Guid guid)||guid==Guid.Empty)
        throw new ArgumentException("A nonempty fresh owned GUID is required.");
    int interval=Number(values,"--interval-ms",10);
    int seconds=Number(values,"--max-seconds",15);
    if(interval<5||interval>1000)throw new ArgumentException("Interval must be5..1000ms.");
    if(seconds<0||seconds>60)throw new ArgumentException("Duration must be0(leased) or1..60seconds.");
    int? ownerPid=values.ContainsKey("--owner-pid")?Number(values,"--owner-pid",0):null;
    if(ownerPid.HasValue&&(ownerPid.Value<=0||ownerPid.Value==Environment.ProcessId))
        throw new ArgumentException("Owner PID must identify another existing process.");
    string? stop=values.TryGetValue("--stop-file",out string? stopText)?LocalAbsolute(stopText,"stop file"):null;
    string? ready=values.TryGetValue("--ready-file",out string? readyText)?LocalAbsolute(readyText,"ready file"):null;
    if(seconds==0&&(!ownerPid.HasValue||stop is null))
        throw new ArgumentException("Unbounded mode requires both an actual owner PID and absolute stop file.");
    if(ready is not null&&string.Equals(ready,stop,StringComparison.OrdinalIgnoreCase))
        throw new ArgumentException("Ready and stop files must differ.");
    if(ready is not null&&File.Exists(ready))throw new ArgumentException("Ready file already exists; refusing stale readiness.");
    return new Options(guid,interval,seconds,ownerPid,stop,ready,flags.Contains("--verbose"));
}
static int Number(Dictionary<string,string> values,string name,int fallback)
    =>!values.TryGetValue(name,out string? value)?fallback:int.TryParse(value,out int parsed)?parsed:throw new ArgumentException("Invalid integer: "+name);
static string LocalAbsolute(string value,string label)
{
    if(!Path.IsPathFullyQualified(value)||value.StartsWith(@"\\",StringComparison.Ordinal))
        throw new ArgumentException("A local absolute "+label+" path is required.");
    string full=Path.GetFullPath(value);
    string? directory=Path.GetDirectoryName(full);
    if(string.IsNullOrWhiteSpace(directory)||!Directory.Exists(directory))
        throw new ArgumentException("The "+label+" directory must already exist.");
    return full;
}
static bool StopFileExists(string path)
{
    try{_ = File.GetAttributes(path);return true;}
    catch(FileNotFoundException){return false;}
    // Missing directory, denied access and other IO failures are lease failures,
    // not reasons to keep flushing invisibly.
}
static void WriteReady(string path,string json)
{
    string temporary=path+"."+Guid.NewGuid().ToString("N")+".tmp";
    try
    {
        using(var output=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.Read))
        {
            byte[] bytes=Encoding.UTF8.GetBytes(json+Environment.NewLine);output.Write(bytes);output.Flush(true);
        }
        File.Move(temporary,path,false);
    }
    finally{if(File.Exists(temporary))File.Delete(temporary);}
}
static object View(Properties value)=>new{guid=value.Wnode.Guid,modeHex="0x"+value.LogFileMode.ToString("X8"),
    bufferSizeKb=value.BufferSize,flushSeconds=value.FlushTimer,value.EventsLost,value.LogBuffersLost,
    value.RealTimeBuffersLost,value.BuffersWritten};
static string FindOwned(Guid expected)
{
    var buffers=new List<QueryBuffer>();
    try
    {
        const uint limit=128;for(int i=0;i<limit;i++)buffers.Add(new QueryBuffer());
        uint code=Native.QueryAllTracesW(buffers.Select(buffer=>buffer.Pointer).ToArray(),limit,out uint count);
        if(code!=0)throw new Win32Exception((int)code,"Initial metadata discovery failed; no flush attempted.");
        if(count>limit)throw new InvalidOperationException("Initial metadata discovery exceeded the fixed bound.");
        var matches=new List<string>();
        for(int i=0;i<count;i++)if(buffers[i].Read().Wnode.Guid==expected)
        {
            string name=buffers[i].LoggerName();
            if(string.IsNullOrWhiteSpace(name))throw new InvalidOperationException("Matching GUID has no API logger name.");
            matches.Add(name);
        }
        if(matches.Count!=1)throw new InvalidOperationException($"Expected exactly one owned WNODE.Guid match; found{matches.Count}. No name guessed.");
        return matches[0];
    }
    finally{foreach(var buffer in buffers)buffer.Dispose();}
}

sealed record Options(Guid ExpectedGuid,int IntervalMs,int MaxSeconds,int? OwnerPid,string? StopFile,string? ReadyFile,bool Verbose);
sealed class QueryBuffer:IDisposable
{
    const uint Capacity=65536;
    static readonly byte[] Zeroes=new byte[(int)Capacity];
    public IntPtr Pointer{get;}
    public QueryBuffer(){Pointer=Marshal.AllocHGlobal((int)Capacity);Reset();}
    public void Reset()
    {
        // Reuse one immutable zero-filled managed array, including every loop
        // iteration. No fresh 64KB managed allocation occurs per QUERY.
        Marshal.Copy(Zeroes,0,Pointer,(int)Capacity);
        Properties value=new(){Wnode=new(){BufferSize=Capacity,Flags=0x20000},LoggerNameOffset=120,LogFileNameOffset=4216};
        Marshal.StructureToPtr(value,Pointer,false);
    }
    public void EnsureCapacityHeader(){var value=Read();value.Wnode.BufferSize=Capacity;Marshal.StructureToPtr(value,Pointer,false);}
    public Properties Read()=>Marshal.PtrToStructure<Properties>(Pointer);
    public string LoggerName()
    {
        uint offset=Read().LoggerNameOffset;if(offset<120||offset>=Capacity||offset%2!=0)return "";
        string value=Marshal.PtrToStringUni(IntPtr.Add(Pointer,(int)offset),(int)Math.Min((Capacity-offset)/2,4096))??"";
        int end=value.IndexOf('\0');return end<0?value:value[..end];
    }
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
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,ExactSpelling=true)]public static extern uint QueryAllTracesW([In,Out]IntPtr[] properties,uint count,out uint loggerCount);
    [DllImport("advapi32.dll",CharSet=CharSet.Unicode,ExactSpelling=true)]public static extern uint ControlTraceW(ulong handle,string session,IntPtr properties,uint code);
}
