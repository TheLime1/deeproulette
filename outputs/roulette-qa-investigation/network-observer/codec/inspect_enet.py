from inspect_capture import *
import ctypes
from game_library import find_enet
EN=ctypes.CDLL(str(find_enet()))
EN.enet_range_coder_create.restype=ctypes.c_void_p
EN.enet_range_coder_destroy.argtypes=[ctypes.c_void_p]
EN.enet_range_coder_decompress.argtypes=[ctypes.c_void_p,ctypes.c_void_p,ctypes.c_size_t,ctypes.c_void_p,ctypes.c_size_t]
EN.enet_range_coder_decompress.restype=ctypes.c_size_t
def decompress(d):
    state=EN.enet_range_coder_create();out=ctypes.create_string_buffer(65536);inp=ctypes.create_string_buffer(d)
    try:
        n=EN.enet_range_coder_decompress(state,inp,len(d),out,len(out))
        if n:return out.raw[:n]
    finally:EN.enet_range_coder_destroy(state)
def commands(d,checksum):
    if len(d)<2:return
    flags=struct.unpack_from('>H',d)[0];o=(4 if flags&0x8000 else 2)+checksum
    if flags&0x4000:
        d=decompress(d[o:])
        if not d:return
        o=0
    result=[]
    fixed={1:8,2:48,3:44,4:8,5:4,10:12,11:16}
    while o<len(d):
        if len(d)-o<4:return
        cmd=d[o]&15;channel=d[o+1];seq=struct.unpack_from('>H',d,o+2)[0]
        data=b''
        if cmd in fixed:n=fixed[cmd]
        elif cmd in [6,7,8,9,12]:
            head={6:6,7:8,8:24,9:8,12:24}[cmd]
            lenoff=4 if cmd==6 else 6
            if o+head>len(d):return
            payload=struct.unpack_from('>H',d,o+lenoff)[0];n=head+payload;data=d[o+head:o+n]
        else:return
        if o+n>len(d):return
        result.append((cmd,channel,seq,data));o+=n
    return result
if __name__=='__main__':
    results={}
    for checksum in [0,4]:
        seen=0;valid=0;counts=collections.Counter();types=collections.Counter();lengths=collections.Counter()
        for ts,direction,server,d in application_datagrams(sys.argv[1]):
            seen+=1;c=commands(d,checksum)
            if c is None:continue
            valid+=1
            for cmd,channel,seq,payload in c:
                counts[(direction,cmd,channel)]+=1
                if payload:
                    lengths[len(payload)]+=1
                    if payload[0]<=2:types[(direction,payload[0])]+=1
        results[str(checksum)]={'datagrams':seen,'valid_framing':valid,'command_counts':[(list(k),v) for k,v in counts.most_common()],'application_type_candidates':[(list(k),v) for k,v in types.most_common()],'application_lengths':lengths.most_common(15)}
    print(json.dumps(results,indent=2))
