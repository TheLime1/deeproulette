using System.Globalization;
using System.Text;
using System.Text.Json;

sealed class RotatingPcapng:IDisposable
{
    const int HeadersLength=48;
    readonly string basePath,parent,stem;
    readonly long maximumBytes;
    readonly int retainCount;
    readonly List<Chunk> chunks=new();
    FileStream? stream;BinaryWriter? writer;Chunk? current;
    FileIdentity? manifestIdentity;
    long generation,nextIndex,retiredThrough=-1,retentionOverruns,retentionDeleteFailures;
    bool finished;string? failure;
    public string ManifestPath{get;}
    public long RetiredThrough=>retiredThrough;
    public long RetentionOverrunCounter=>retentionOverruns;

    public RotatingPcapng(string path,long maximumBytes,int retainCount)
    {
        this.maximumBytes=maximumBytes;this.retainCount=retainCount;
        basePath=Path.GetFullPath(path);parent=Path.GetDirectoryName(basePath)??throw new ArgumentException("A concrete parent directory is required.");
        stem=Path.GetFileNameWithoutExtension(basePath);ManifestPath=basePath+".manifest.json";
        if(!Directory.Exists(parent))throw new DirectoryNotFoundException("The exact output parent must already exist.");
        if(File.Exists(ManifestPath)||Directory.Exists(ManifestPath))throw new IOException("A manifest already exists; receiver does not resume or replace an earlier recording.");
        try{OpenChunk(0);nextIndex=1;Publish();}
        catch
        {
            try{writer?.Dispose();}finally{writer=null;stream=null;current=null;finished=true;}
            throw; // No alternate file, path or capture session is attempted.
        }
    }
    public void Packet(DateTime utc,ReadOnlySpan<byte> frame,int original,long eventQpc,long arrivalQpc,long frequency)
    {
        if(finished||writer is null||stream is null||current is null)throw new ObjectDisposedException(nameof(RotatingPcapng));
        byte[] bytes=frame.ToArray();
        ulong micros=checked((ulong)((utc.Ticks-DateTime.UnixEpoch.Ticks)/10));
        byte[] comment=Encoding.UTF8.GetBytes(FormattableString.Invariant($"ROB_QA_QPC_V1 {eventQpc} {arrivalQpc} {frequency}"));
        byte[] block=Block(6,b=>
        {
            b.Write(0u);b.Write((uint)(micros>>32));b.Write((uint)micros);b.Write((uint)bytes.Length);b.Write((uint)original);
            b.Write(bytes);Pad(b,bytes.Length);
            b.Write((ushort)1);b.Write(checked((ushort)comment.Length));b.Write(comment);Pad(b,comment.Length);
            b.Write((ushort)0);b.Write((ushort)0); // End of options.
        });
        if(block.Length+HeadersLength>maximumBytes)throw new IOException("A complete packet block cannot fit the configured chunk size; no block is split.");
        if(stream.Position+block.Length>maximumBytes)Rotate();
        writer!.Write(block);writer.Flush();current!.Length=stream!.Position;
    }
    void Rotate()
    {
        CloseCurrent();
        // Intermediate closed-last manifests are valid. stopped/closed remain false
        // globally until Complete; readers may wait for the next chunk publication.
        Publish();
        while(chunks.Count>=retainCount)RetireOldest();
        long index=nextIndex;OpenChunk(index);nextIndex=checked(index+1);Publish();
    }
    string ChunkPath(long index)
    {
        string path=index==0?basePath:Path.Combine(parent,stem+".chunk-"+index.ToString("D6",CultureInfo.InvariantCulture)+".pcapng");
        path=Path.GetFullPath(path);
        if(!string.Equals(Path.GetDirectoryName(path),parent,StringComparison.OrdinalIgnoreCase))throw new IOException("Chunk path left the exact output parent.");
        return path;
    }
    void OpenChunk(long index)
    {
        string path=ChunkPath(index);
        // CreateNew refuses an existing file, directory or link. No index skipping or path fallback.
        stream=new FileStream(path,FileMode.CreateNew,FileAccess.Write,FileShare.Read);
        try
        {
            FileIdentity identity=OwnedFiles.Snapshot(stream).Identity;
            writer=new BinaryWriter(stream,Encoding.UTF8,false);
            current=new Chunk(index,path,identity);
            writer.Write(Block(0x0A0D0D0A,b=>{b.Write(0x1A2B3C4Du);b.Write((ushort)1);b.Write((ushort)0);b.Write(-1L);}));
            writer.Write(Block(1,b=>{b.Write((ushort)1);b.Write((ushort)0);b.Write(65535u);}));
            writer.Flush();current.Length=stream.Position;chunks.Add(current);
        }
        catch{stream.Dispose();writer=null;stream=null;current=null;throw;}
    }
    void CloseCurrent()
    {
        if(current is null)return;
        Chunk chunk=current;var oldWriter=writer!;var oldStream=stream!;
        writer=null;stream=null;current=null;
        try{oldWriter.Flush();chunk.Length=oldStream.Length;}
        finally{try{oldWriter.Dispose();}finally{chunk.Closed=true;}}
        FileSnapshot closed=OwnedFiles.Snapshot(chunk.Path);
        OwnedFiles.Verify(closed,chunk.Identity,chunk.Length);
        chunk.LastWrite=closed.LastWrite;
    }
    void RetireOldest()
    {
        Chunk old=chunks[0];
        if(!old.Closed||ReferenceEquals(old,current)||!old.LastWrite.HasValue)throw new IOException("Only a verified own closed chunk can be retired.");
        try
        {
            // Acquire exclusive access and verify identity/length/last-write before any retirement.
            // An open reader, a reparse point or a replacement file causes receiver failure,
            // rather than deleting it or opening a fifth chunk.
            using var deletion=OwnedFiles.OpenClosedForDelete(old.Path,old.Identity,old.Length,old.LastWrite.Value);
            chunks.RemoveAt(0);retiredThrough=old.Index;retentionOverruns=checked(retentionOverruns+1);
            // Publish the explicit lost-history boundary BEFORE deleting the verified file.
            // A reader never has to infer an intentional retention gap from silent file loss.
            Publish();
            deletion.MarkDelete(); // By the verified handle, not another pathname lookup.
        }
        catch(Exception e){retentionDeleteFailures=checked(retentionDeleteFailures+1);MarkFailed(e.Message);throw;}
    }
    public void MarkFailed(string message){failure??=message.Length<=2048?message:message[..2048];}
    public void Complete()
    {
        if(finished)return;
        Exception? closeFailure=null;
        try{CloseCurrent();}catch(Exception e){MarkFailed(e.Message);closeFailure=e;}
        finished=true;Publish();
        if(closeFailure is not null)throw new IOException("The final own chunk could not be fully finalized.",closeFailure);
    }
    void Publish()
    {
        long nextGeneration=checked(generation+1);
        byte[] json=JsonSerializer.SerializeToUtf8Bytes(new
        {
            version=1,basePath,generation=nextGeneration,
            chunks=chunks.Select(c=>new{index=c.Index,path=c.Path,closed=c.Closed,length=c.Length}).ToArray(),
            retiredThrough,retentionOverrunCounter=retentionOverruns,retentionDeleteFailures,
            stopped=finished,closed=finished,state=finished?(failure is null?"stopped":"error"):"running",error=failure,
            chunkBytes=maximumBytes,retainChunks=retainCount
        });
        string temporary=ManifestPath+".tmp-"+Guid.NewGuid().ToString("N");
        FileSnapshot? temporaryIdentity=null;bool moved=false;
        try
        {
            using(var file=new FileStream(temporary,FileMode.CreateNew,FileAccess.Write,FileShare.None))
            {
                file.Write(json);file.Flush();
            }
            temporaryIdentity=OwnedFiles.Snapshot(temporary);
            for(int attempt=0;;attempt++)
            {
                if(manifestIdentity.HasValue)OwnedFiles.Verify(OwnedFiles.Snapshot(ManifestPath),manifestIdentity.Value);
                else if(File.Exists(ManifestPath)||Directory.Exists(ManifestPath))throw new IOException("An unowned manifest appeared; it was not replaced.");
                try{File.Move(temporary,ManifestPath,true);moved=true;break;}
                catch(Exception e)when(attempt<99&&(e is IOException||e is UnauthorizedAccessException)){Thread.Sleep(5);}
            }
            manifestIdentity=temporaryIdentity.Value.Identity;generation=nextGeneration;
        }
        finally
        {
            if(!moved&&temporaryIdentity.HasValue)
            {
                // Never glob. Cleanup only this exact own temporary file with unchanged identity.
                try{OwnedFiles.DeleteClosedOwnFile(temporary,temporaryIdentity.Value);}catch{ }
            }
        }
    }
    static byte[] Block(uint type,Action<BinaryWriter> emit)
    {
        using var memory=new MemoryStream();
        using(var body=new BinaryWriter(memory,Encoding.UTF8,true)){emit(body);}
        uint size=checked((uint)memory.Length+12);
        using var block=new MemoryStream();using(var output=new BinaryWriter(block,Encoding.UTF8,true))
        {
            output.Write(type);output.Write(size);output.Write(memory.GetBuffer(),0,checked((int)memory.Length));output.Write(size);
        }
        return block.ToArray();
    }
    static void Pad(BinaryWriter writer,int length){for(int i=0;i<((-length)&3);i++)writer.Write((byte)0);}
    public void Dispose()=>Complete();
    sealed class Chunk
    {
        public long Index{get;}public string Path{get;}public FileIdentity Identity{get;}
        public long Length{get;set;}public bool Closed{get;set;}public ulong? LastWrite{get;set;}
        public Chunk(long index,string path,FileIdentity identity){Index=index;Path=path;Identity=identity;}
    }
}
