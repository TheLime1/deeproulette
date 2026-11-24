"""Offline PCAPNG inspection; output metadata only, never application payloads."""
from pathlib import Path
import struct,sys,socket,collections,json,datetime,hashlib,math
SERVERS=set()  # Discovered from the currently owned game connection.
def options(d,endian):
    o=0
    while o+4<=len(d):
        code,n=struct.unpack_from(endian+'HH',d,o);o+=4
        if code==0:break
        yield code,d[o:o+n];o+=(n+3)&~3
def packets(p):
    d=Path(p).read_bytes();pos=0;endian='<';interfaces=[]
    while pos+12<=len(d):
        if d[pos:pos+4]==b'\x0a\x0d\x0d\x0a':
            endian='<' if d[pos+8:pos+12]==b'\x4d\x3c\x2b\x1a' else '>';interfaces=[]
        typ,n=struct.unpack_from(endian+'II',d,pos)
        if n<12 or pos+n>len(d):raise ValueError('Invalid PCAPNG block length')
        b=d[pos+8:pos+n-4]
        if typ==1:
            link,_,snap=struct.unpack_from(endian+'HHI',b);res=1e-6;offset=0
            for code,v in options(b[8:],endian):
                if code==9:res=(2**-(v[0]&127)) if v[0]&128 else 10**-v[0]
                if code==14:offset=struct.unpack(endian+'q',v)[0]
            interfaces.append((link,res,offset))
        elif typ==6:
            interface,hi,lo,cap,orig=struct.unpack_from(endian+'5I',b)
            link,res,offset=interfaces[interface]
            yield ((hi<<32)|lo)*res+offset,link,b[20:20+cap]
        pos+=n
def udp_frame(link,d):
    if link==1:
        if len(d)<14:return
        kind=struct.unpack_from('>H',d,12)[0];off=14
        if kind in [0x8100,0x88a8]:kind=struct.unpack_from('>H',d,16)[0];off=18
        if kind!=0x800:return
        d=d[off:]
    elif link not in [101,228]:return
    if len(d)<28 or d[0]>>4!=4 or d[9]!=17:return
    ihl=(d[0]&15)*4
    frag=struct.unpack_from('>H',d,6)[0]
    if frag&0x3fff:return
    src=socket.inet_ntoa(d[12:16]);dst=socket.inet_ntoa(d[16:20])
    sp,dp,n,_=struct.unpack_from('>4H',d,ihl)
    return src,sp,dst,dp,d[ihl+8:ihl+n]
def application_datagrams(p):
    for ts,link,frame in packets(p):
        f=udp_frame(link,frame)
        if not f:continue
        src,sp,dst,dp,d=f
        if (src,sp) in SERVERS:yield ts,'incoming',(src,sp),d
        elif (dst,dp) in SERVERS:yield ts,'outgoing',(dst,dp),d
def inspect(p):
    flows={};lengths=collections.Counter();firstbytes=collections.Counter();samples=[]
    for ts,direction,server,d in application_datagrams(p):
        key=str(server)+':'+direction
        v=flows.setdefault(key,{'server':server,'direction':direction,'packets':0,'bytes':0,'first_ts':ts,'last_ts':ts})
        v['packets']+=1;v['bytes']+=len(d);v['last_ts']=ts
        lengths[len(d)]+=1
        if d:firstbytes[d[:2].hex()]+=1
        if len(samples)<20:
            # ENet framing header only, not application payload or credential data.
            flags=struct.unpack_from('>H',d)[0] if len(d)>=2 else None
            hdr=4 if flags is not None and flags&0x8000 else 2
            samples.append({'utc':datetime.datetime.fromtimestamp(ts,datetime.timezone.utc).isoformat(),'direction':direction,'server':server,'udp_payload_bytes':len(d),'enet_candidate_peer':flags&0xfff if flags is not None else None,'enet_candidate_compressed':bool(flags&0x4000) if flags is not None else None,'enet_candidate_command':d[hdr]&15 if len(d)>hdr else None})
    return {'flows':list(flows.values()),'payload_length_counts':lengths.most_common(25),'first_two_byte_counts':firstbytes.most_common(20),'framing_samples':samples,'note':'ENet interpretation is provisional until command parsing and lengths validate. No payload data is printed.'}
if __name__=='__main__':
    result=inspect(sys.argv[1]);Path(__file__).with_name('capture-summary.json').write_text(json.dumps(result,indent=2));print(json.dumps(result,indent=2))
