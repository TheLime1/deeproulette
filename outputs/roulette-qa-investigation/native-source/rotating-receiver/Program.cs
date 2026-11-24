using Microsoft.Diagnostics.Tracing;
using System.Buffers.Binary;
using System.Text.Json;
using System.Diagnostics;
using System.Globalization;
using System.Net;
using System.Net.Sockets;

// SOURCE-ONLY CANDIDATE. No build/invocation/tests performed during preparation.
// Receiver only: never creates/enables/changes/flushes/stops an ETW session.
if(args.Length==0||args.Contains("--help"))
{
    Console.WriteLine("LiveNdis (--etl FILE | --session EXISTING_REALTIME_SESSION) --pcapng NEW_FILE [--server-ip IPv4 --server-port PORT --local-ip IPv4 --local-port PORT | --local-ports CSV] [--chunk-bytes 16777216] [--retain-chunks 4] [--max-seconds N] [--quiet]");
    return;
}
string? Arg(string name){int i=Array.IndexOf(args,name);return i>=0&&i+1<args.Length?args[i+1]:null;}
string? input=Arg("--etl"),session=Arg("--session"),output=Arg("--pcapng");
if((input is null)==(session is null)||output is null)throw new ArgumentException("Specify exactly one input and a new PCAPNG output.");
bool realtime=session is not null,quiet=args.Contains("--quiet");
byte[] Ip(string name)
{
    if(!IPAddress.TryParse(Arg(name),out var ip)||ip.AddressFamily!=AddressFamily.InterNetwork)throw new ArgumentException(name+" requires an explicit discovered IPv4 address.");
    return ip.GetAddressBytes();
}
int Port(string value)
{
    if(!int.TryParse(value,out int port)||port<1||port>65535)throw new ArgumentException("Ports must be integers from1 to65535.");
    return port;
}
byte[] serverIp=Ip("--server-ip"),localIp=Ip("--local-ip");
int serverPort=Port(Arg("--server-port")??throw new ArgumentException("An explicit discovered server port is required."));
if(Arg("--local-port") is not null&&Arg("--local-ports") is not null)throw new ArgumentException("Use one local-port option.");
var localPorts=(Arg("--local-ports")??Arg("--local-port")??throw new ArgumentException("Explicit discovered local port membership is required.")).Split(',').Select(x=>Port(x.Trim())).ToHashSet();
var packetFilter=new{serverIp=new IPAddress(serverIp).ToString(),serverPort,localIp=new IPAddress(localIp).ToString(),localPorts=localPorts.Order().ToArray()};
long chunkBytes=long.TryParse(Arg("--chunk-bytes")??"16777216",NumberStyles.None,CultureInfo.InvariantCulture,out var cb)&&cb>=131072&&cb<=1073741824?cb:throw new ArgumentException("Chunk bytes must be131072..1073741824.");
int retainChunks=int.TryParse(Arg("--retain-chunks")??"4",out var rc)&&rc>=2&&rc<=4?rc:throw new ArgumentException("Retain chunks must be2..4, including the current chunk.");
double seconds=double.TryParse(Arg("--max-seconds"),out var n)?n:0;
Guid provider=new("2ed6006e-4729-4609-b423-3ee7bcd678ef");
using var capture=new RotatingPcapng(output,chunkBytes,retainChunks);
try
{
    using var source=new ETWTraceEventSource(session??input!,realtime?TraceEventSourceType.Session:TraceEventSourceType.FileOnly);
    long accepted=0,incoming=0,outgoing=0,malformed=0,nonprimary=0,ndisPackets=0,truncated=0,splitEventFragments=0,endpointMismatch=0,localPortMismatch=0;
    var delays=new LagSamples(4096);
    DateTime? first=null,last=null;
    source.AllEvents+=ev=>
    {
        if(ev.ProviderGuid!=provider||(int)ev.ID!=1001)return;
        DateTime arrivalUtc=DateTime.UtcNow;long arrivalQpc=Stopwatch.GetTimestamp();
        ndisPackets++;
        if(((ulong)ev.Keywords&0xC0000000UL)!=0xC0000000UL){splitEventFragments++;return;}
        // Copy only headers before endpoint/membership gating. Other packet payloads are not copied.
        if(ev.EventDataLength<54){malformed++;return;}
        byte[] data=new byte[54];ev.EventData(data,0,0,data.Length);
        uint mini=BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(0,4));
        uint lower=BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(4,4));
        uint size=BinaryPrimitives.ReadUInt32LittleEndian(data.AsSpan(8,4));
        if(size>ev.EventDataLength-12||size<42){malformed++;return;}
        ReadOnlySpan<byte> frame=data.AsSpan(12);
        if(frame[12]!=8||frame[13]!=0||frame[14]>>4!=4||frame[23]!=17)return;
        int ihl=(frame[14]&15)*4,udp=14+ihl;
        if(ihl<20||size<udp+8){malformed++;return;}
        int fragment=BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(20,2))&0x3fff;
        if(fragment!=0){malformed++;return;}
        bool localSrc=frame.Slice(26,4).SequenceEqual(localIp),localDst=frame.Slice(30,4).SequenceEqual(localIp);
        bool serverSrc=frame.Slice(26,4).SequenceEqual(serverIp),serverDst=frame.Slice(30,4).SequenceEqual(serverIp);
        if(!((localSrc&&serverDst)||(serverSrc&&localDst))){endpointMismatch++;return;}
        if(ihl!=20){data=new byte[12+udp+8];ev.EventData(data,0,0,data.Length);frame=data.AsSpan(12);}
        ushort sp=BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(udp,2)),dp=BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(udp+2,2));
        bool isIn=serverSrc&&localDst&&sp==serverPort,isOut=localSrc&&serverDst&&dp==serverPort;
        if(!isIn&&!isOut){endpointMismatch++;return;}
        if(!localPorts.Contains(isIn?dp:sp)){localPortMismatch++;return;}
        if(mini!=lower){nonprimary++;return;}
        data=ev.EventData();frame=data.AsSpan(12,(int)size);
        DateTime eventUtc=ev.TimeStamp.ToUniversalTime();
#pragma warning disable CS0618
        long eventQpc=ev.TimeStampQPC;
#pragma warning restore CS0618
        first??=eventUtc;last=eventUtc;
        int original=14+BinaryPrimitives.ReadUInt16BigEndian(frame.Slice(16,2));
        bool complete=frame.Length>=original;if(!complete)truncated++;
        capture.Packet(eventUtc,frame,Math.Max(original,frame.Length),eventQpc,arrivalQpc,Stopwatch.Frequency);
        accepted++;if(isIn)incoming++;else outgoing++;
        double? lagMs=realtime?(arrivalUtc-eventUtc).TotalMilliseconds:null;
        if(lagMs.HasValue)delays.Add(lagMs.Value);
        if(!quiet)Console.WriteLine(JsonSerializer.Serialize(new{kind="packet",index=accepted,direction=isIn?"incoming":"outgoing",eventUtc,arrivalUtc,eventQpc,arrivalQpc,qpcFrequency=Stopwatch.Frequency,lagMs,bytes=frame.Length,originalBytes=original,complete,miniport=mini,lower}));
    };
    Console.CancelKeyPress+=(_,e)=>{e.Cancel=true;source.StopProcessing();};
    using var timer=seconds>0?new Timer(_=>source.StopProcessing(),null,TimeSpan.FromSeconds(seconds),Timeout.InfiniteTimeSpan):null;
    if(!quiet)Console.WriteLine(JsonSerializer.Serialize(new{kind="ready",realtime,packetFilter,manifest=capture.ManifestPath,chunkBytes,retainChunks,utc=DateTime.UtcNow,qpc=Stopwatch.GetTimestamp(),qpcFrequency=Stopwatch.Frequency,note="Receiver attached; no session control performed."}));
    source.Process();
    capture.Complete();
    Console.WriteLine(JsonSerializer.Serialize(new{kind="summary",realtime,packetFilter,accepted,incoming,outgoing,ndisPackets,nonprimary,malformed,truncated,splitEventFragments,endpointMismatch,localPortMismatch,first,last,sourceEventsLostSnapshot=source.EventsLost,latencyMs=delays.Summary(),manifest=capture.ManifestPath,retiredThrough=capture.RetiredThrough,retentionOverrunCounter=capture.RetentionOverrunCounter,note="Lag statistics use a fixed last4096-sample window; no performance claim. Source.EventsLost is a realtime snapshot. Consumer stop does not stop the session."}));
}
catch(Exception e)
{
    capture.MarkFailed(e.Message);
    throw;
}

sealed class LagSamples
{
    readonly double[] samples;int count,next;long observed;
    public LagSamples(int capacity){samples=new double[capacity];}
    public void Add(double value){samples[next]=value;next=(next+1)%samples.Length;if(count<samples.Length)count++;observed=checked(observed+1);}
    public object Summary()
    {
        var values=samples.Take(count).ToArray();Array.Sort(values);
        double? P(double p)=>count==0?null:values[(int)Math.Min(count-1,Math.Floor(p*(count-1)))];
        return new{p50=P(.5),p95=P(.95),max=P(1),sampleCapacity=samples.Length,sampleCount=count,observedSamples=observed,samplePolicy="lastN"};
    }
}
