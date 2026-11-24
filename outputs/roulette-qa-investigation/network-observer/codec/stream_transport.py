"""Incremental transport-only reader for the existing filtered NDIS PCAPNG output.

No capture/session control. No entity identification. A yielded payload is a
complete channel-1 transport message, not a verified roulette result.
"""
from __future__ import annotations
import bisect
import collections
from dataclasses import dataclass
import hashlib
import ipaddress
import math
from pathlib import Path
import struct
import sys

_HERE=Path(__file__).resolve().parent
_CANDIDATES=[_HERE]
for _ancestor in (_HERE,*_HERE.parents):
    _CANDIDATES.extend([_ancestor/"network-observer/codec",
                        _ancestor/"outputs/roulette-qa-investigation/network-observer/codec"])
DEFAULT_CODEC=next((p for p in _CANDIDATES if (p/"inspect_capture.py").is_file() and
                   (p/"capture-audit/enet_reassembly.py").is_file()),
                  _HERE/"codec")


def load_existing_codec(directory=DEFAULT_CODEC):
    directory=Path(directory).resolve()
    for path in (directory,directory/"capture-audit"):
        sys.path.insert(0,str(path))
    from inspect_capture import udp_frame
    from enet_reassembly import transport_commands
    from battle_bits import battle_header
    from summarize_batches import suffixes
    return udp_frame,transport_commands,battle_header,suffixes


@dataclass(frozen=True)
class Packet:
    receipt_ts: float
    link_type: int
    frame: bytes
    block_offset: int
    next_offset: int
    event_qpc: int | None = None
    receiver_qpc: int | None = None
    qpc_frequency: int | None = None


@dataclass(frozen=True)
class Message:
    receipt_ts: float
    payload: bytes
    fragmented: bool
    message_id: int
    event_qpc: int | None = None
    receiver_qpc: int | None = None
    qpc_frequency: int | None = None


class PcapngTail:
    """Poll appended complete blocks; partial blocks leave the cursor unchanged.

    checkpoint() is JSON serializable. replay_offset supports a bounded rewind
    after a decoder restart, allowing pending fragments to be reconstructed.
    Callers must deduplicate already-emitted application events after a rewind.
    """
    MAX_BLOCK=16*1024*1024
    MAX_FRAME=256*1024

    def __init__(self,path,checkpoint=None):
        self.path=Path(path).resolve();self.offset=0;self.endian="<";self.interfaces=[]
        self.identity=None;self.recent=collections.deque(maxlen=4096)
        if checkpoint:
            if str(self.path)!=checkpoint["path"]:raise ValueError("Checkpoint belongs to another capture")
            self.offset=int(checkpoint["offset"]);self.endian=checkpoint["endian"]
            self.interfaces=[tuple(x) for x in checkpoint["interfaces"]]
            self.identity=tuple(checkpoint["identity"]) if checkpoint.get("identity") else None
            if self.offset<0 or self.endian not in ("<",">"):raise ValueError("Invalid checkpoint")

    @staticmethod
    def _options(data,endian):
        pos=0
        while pos+4<=len(data):
            code,n=struct.unpack_from(endian+"HH",data,pos);pos+=4
            if code==0:return
            if pos+n>len(data):raise ValueError("Truncated interface option")
            yield code,data[pos:pos+n]
            pos+=(n+3)&~3

    def poll(self,*,max_packets=128,max_blocks=512,max_frame_bytes=2*1024*1024):
        if min(max_packets,max_blocks,max_frame_bytes)<1:raise ValueError('Positive poll bounds required')
        try:stat=self.path.stat()
        except FileNotFoundError:return []
        identity=(stat.st_dev,stat.st_ino)
        if self.identity is not None and identity!=self.identity:raise ValueError("Capture file was replaced; new reader required")
        self.identity=identity
        if stat.st_size<self.offset:raise ValueError("Capture was truncated; new reader required")
        # At EOF, avoid opening and seeking the file on every short idle poll.
        if stat.st_size==self.offset:return []
        packets=[];blocks=0;frame_bytes=0
        with self.path.open("rb") as handle:
            while self.offset<stat.st_size and blocks<max_blocks and len(packets)<max_packets and frame_bytes<max_frame_bytes:
                handle.seek(self.offset);head=handle.read(12)
                if len(head)<12:break
                section=head[:4]==b"\x0a\x0d\x0d\x0a"
                endian=self.endian
                if section:
                    if head[8:12]==b"\x4d\x3c\x2b\x1a":endian="<"
                    elif head[8:12]==b"\x1a\x2b\x3c\x4d":endian=">"
                    else:raise ValueError("Invalid PCAPNG byte-order magic")
                kind,size=struct.unpack_from(endian+"II",head)
                if size<12 or size%4 or size>self.MAX_BLOCK:raise ValueError("Invalid PCAPNG block size")
                handle.seek(self.offset);block=handle.read(size)
                if len(block)<size:break
                if struct.unpack_from(endian+"I",block,size-4)[0]!=size:raise ValueError("PCAPNG block trailer mismatch")
                start=self.offset;body=block[8:-4]
                if section:
                    if size<28:raise ValueError("Short PCAPNG section")
                    self.endian=endian;self.interfaces=[];self.recent.clear()
                elif kind==1:
                    if len(body)<8:raise ValueError("Short interface block")
                    link,_,snap=struct.unpack_from(endian+"HHI",body)
                    resolution=1e-6;clock_offset=0
                    for code,value in self._options(body[8:],endian):
                        if code==9:
                            if len(value)!=1:raise ValueError("Invalid timestamp resolution")
                            resolution=2**-(value[0]&127) if value[0]&128 else 10**-value[0]
                        elif code==14:
                            if len(value)!=8:raise ValueError("Invalid timestamp offset")
                            clock_offset=struct.unpack(endian+"q",value)[0]
                    self.interfaces.append((link,resolution,clock_offset,snap))
                elif kind==6:
                    if len(body)<20:raise ValueError("Short enhanced packet block")
                    index,hi,lo,cap,original=struct.unpack_from(endian+"5I",body)
                    if index>=len(self.interfaces):raise ValueError("Unknown packet interface")
                    if cap!=original or cap>self.MAX_FRAME or len(body)<20+((cap+3)&~3):raise ValueError("Truncated or oversized packet")
                    link,resolution,clock_offset,_=self.interfaces[index]
                    timestamp=((hi<<32)|lo)*resolution+clock_offset
                    if not math.isfinite(timestamp):raise ValueError("Invalid receipt timestamp")
                    timing=(None,None,None)
                    for code,value in self._options(body[20+((cap+3)&~3):],endian):
                        if code!=1 or not value.startswith(b'ROB_QA_QPC_V1 '):continue
                        if timing[0] is not None or len(value)>160:raise ValueError('Duplicate or oversized QPC timing comment')
                        fields=value.split()
                        if len(fields)!=4 or any(not field.isdigit() for field in fields[1:]):raise ValueError('Invalid QPC timing comment')
                        timing=tuple(int(field) for field in fields[1:])
                        if min(timing)<=0 or timing[2]>10**12:raise ValueError('Invalid QPC timing values')
                    packets.append(Packet(timestamp,link,body[20:20+cap],start,start+size,*timing))
                    frame_bytes+=cap
                    self.recent.append((timestamp,start))
                self.offset+=size;blocks+=1
        return packets

    def checkpoint(self,rewind_seconds=5.0):
        recent=list(self.recent);cutoff=(max((x[0] for x in recent),default=0)-rewind_seconds)
        replay=next((offset for timestamp,offset in recent if timestamp>=cutoff),self.offset)
        return dict(path=str(self.path),offset=self.offset,endian=self.endian,interfaces=self.interfaces,
                    identity=self.identity,replay_offset=replay,
                    note="For fragment reconstruction after restart, replace offset with replay_offset and deduplicate application events.")


class StreamTransport:
    MAX_MESSAGE=8*1024*1024
    MAX_FRAGMENTS=8192
    MAX_GROUPS=512
    MAX_BUFFER=64*1024*1024
    MAX_TRACKED_PIECES=65536
    MAX_DIRECT_CACHE=4096
    TTL=5.0

    def __init__(self,server_ip,server_port,local_ip,local_ports,*,codec_dir=DEFAULT_CODEC):
        self.server_ip=str(ipaddress.IPv4Address(server_ip));self.local_ip=str(ipaddress.IPv4Address(local_ip))
        self.server_port=int(server_port);self.local_ports=frozenset(int(p) for p in local_ports)
        if not 1<=self.server_port<=65535 or not self.local_ports or len(self.local_ports)>1024 or any(not 1<=p<=65535 for p in self.local_ports):
            raise ValueError("Exact server/local port membership is required")
        self.udp_frame,self.commands,self.header,self.suffixes=load_existing_codec(codec_dir)
        self.groups={};self.direct=collections.OrderedDict();self.stats=collections.Counter()
        self.diagnostics=collections.deque(maxlen=1024);self.watermark=0.;self.buffered=0;self.piece_count=0;self.serial=0

    def _diag(self,kind,**metadata):
        self.stats[kind]+=1;self.diagnostics.append(dict(kind=kind,**metadata))

    def drain_diagnostics(self):
        result=list(self.diagnostics);self.diagnostics.clear();return result

    def _drop(self,key):
        group=self.groups.pop(key)
        self.piece_count-=len(group['pieces']);self.buffered-=group['buffered']

    def _expire(self,receipt_ts):
        self.watermark=max(self.watermark,receipt_ts)
        for key,group in list(self.groups.items()):
            if self.watermark-group['last']>self.TTL:
                if not group['emitted'] and not group['invalid']:
                    self._diag('incomplete_fragment_group',received=len(group['pieces']),expected=group['count'])
                self._drop(key)
        while self.direct:
            key,stamp=next(iter(self.direct.items()))
            if self.watermark-stamp<=self.TTL and len(self.direct)<=self.MAX_DIRECT_CACHE:break
            self.direct.popitem(last=False)

    def advance_time(self,receipt_ts):
        """Expire stalled groups even when no new packets arrive."""
        if not math.isfinite(receipt_ts):raise ValueError("Invalid clock")
        self._expire(receipt_ts)

    @staticmethod
    def _valid_ip_frame(link,frame):
        offset=0
        if link==1:
            if len(frame)<14:return False
            ether=struct.unpack_from('>H',frame,12)[0];offset=14
            if ether in (0x8100,0x88a8):
                if len(frame)<18:return False
                ether=struct.unpack_from('>H',frame,16)[0];offset=18
            if ether!=0x800:return False
        elif link not in (101,228):return False
        data=frame[offset:]
        if len(data)<28 or data[0]>>4!=4 or data[9]!=17:return False
        ihl=(data[0]&15)*4;total=struct.unpack_from('>H',data,2)[0]
        if ihl<20 or total<ihl+8 or len(data)<total:return False
        if struct.unpack_from('>H',data,6)[0]&0x3fff:return False
        udp_length=struct.unpack_from('>H',data,ihl+4)[0]
        return udp_length>=8 and udp_length==total-ihl

    def feed_packet(self,receipt_ts,frame,link_type=1,*,timing=None):
        if not math.isfinite(receipt_ts):raise ValueError("Invalid packet timestamp")
        timing=(None,None,None) if timing is None else tuple(timing)
        if len(timing)!=3:raise ValueError('Timing must have three fields')
        self._expire(receipt_ts);self.stats['frames_seen']+=1
        if not self._valid_ip_frame(link_type,frame):
            self.stats['nonmatching_or_invalid_ip_frames']+=1;return []
        source,sp,destination,dp,payload=self.udp_frame(link_type,frame)
        if (source,sp,destination)!=(self.server_ip,self.server_port,self.local_ip) or dp not in self.local_ports:
            self.stats['endpoint_mismatch']+=1;return []
        self.stats['incoming_datagrams']+=1;messages=[]
        try:commands=list(self.commands(payload))
        except (ValueError,KeyError,IndexError,struct.error):
            self._diag('invalid_enet_framing');return []
        for cmd,channel,seq,peer,part,meta in commands:
            self.stats[f'command_{cmd}_channel_{channel}']+=1
            if channel!=1 or not part:continue
            if cmd in (6,7,9):
                key=(cmd,channel,seq,peer,hashlib.sha256(part).digest())
                if key in self.direct:
                    self.stats['direct_duplicates']+=1;continue
                self.direct[key]=receipt_ts;self.direct.move_to_end(key)
                while len(self.direct)>self.MAX_DIRECT_CACHE:self.direct.popitem(last=False)
                self.serial+=1;messages.append(Message(receipt_ts,part,False,self.serial,*timing));self.stats['direct_messages']+=1
            elif cmd in (8,12):
                message=self._fragment(receipt_ts,cmd,channel,seq,peer,part,meta,timing)
                if message:messages.append(message)
            else:self._diag('unsupported_payload_command',command=cmd)
        self.stats['yielded_messages']+=len(messages)
        return messages

    def _fragment(self,ts,cmd,channel,seq,peer,part,meta,timing):
        self.stats['fragment_pieces']+=1
        count,number,total,offset=meta['count'],meta['number'],meta['total'],meta['offset']
        if not(1<=count<=self.MAX_FRAGMENTS and 0<=number<count and 1<=total<=self.MAX_MESSAGE and
               meta['declared']==len(part) and part and offset+len(part)<=total):
            self._diag('invalid_fragment_limits');return None
        if cmd==8 and ((seq-meta['start'])&65535)!=number:
            self._diag('invalid_reliable_fragment_sequence');return None
        key=(peer,channel,cmd,meta['start'],seq if cmd==12 else None)
        group=self.groups.get(key)
        if group is None:
            if len(self.groups)>=self.MAX_GROUPS:
                self._diag('fragment_group_limit');return None
            group=dict(count=count,total=total,first=ts,last=ts,pieces={},ranges=[],invalid=False,
                       emitted=False,message_id=None,buffered=0)
            self.groups[key]=group
        group['first']=min(group['first'],ts);group['last']=max(group['last'],ts)
        def reject(reason):
            group['invalid']=True
            self._diag('late_fragment_conflict' if group['emitted'] else 'invalid_fragment_group',
                       reason=reason,message_id=group['message_id'])
        if group['count']!=count or group['total']!=total or group['last']-group['first']>self.TTL:
            reject('inconsistent_metadata_or_expired');return None
        digest=hashlib.sha256(part).digest();old=group['pieces'].get(number)
        if old:
            if old['offset']==offset and old['length']==len(part) and old['digest']==digest:
                self.stats['identical_fragment_duplicates']+=1
            else:reject('conflicting_duplicate')
            return None
        if group['invalid'] or group['emitted']:return None
        if self.buffered+len(part)>self.MAX_BUFFER or self.piece_count>=self.MAX_TRACKED_PIECES:
            reject('bounded_storage_limit');return None
        intervals=group['ranges'];position=bisect.bisect_left(intervals,(offset,offset+len(part)))
        if (position and intervals[position-1][1]>offset) or (position<len(intervals) and intervals[position][0]<offset+len(part)):
            reject('overlap');return None
        intervals.insert(position,(offset,offset+len(part)))
        group['pieces'][number]=dict(offset=offset,length=len(part),digest=digest,payload=part,ts=ts,timing=timing)
        self.piece_count+=1;group['buffered']+=len(part);self.buffered+=len(part)
        if len(group['pieces'])!=count:return None
        cursor=0
        for start,end in intervals:
            if start!=cursor:reject('gap');return None
            cursor=end
        if cursor!=total:reject('incomplete_total');return None
        ordered=sorted(group['pieces'].values(),key=lambda p:p['offset'])
        assembled=b''.join(p['payload'] for p in ordered)
        frequencies={p['timing'][2] for p in ordered}
        qpc_complete=len(frequencies)==1 and None not in frequencies and all(None not in p['timing'] for p in ordered)
        completing=max(ordered,key=(lambda p:p['timing'][0]) if qpc_complete else (lambda p:p['ts']))
        completed=completing['ts']
        completion_timing=(completing['timing'][0],max(p['timing'][1] for p in ordered),completing['timing'][2]) if qpc_complete else (None,None,None)
        for piece in group['pieces'].values():piece.pop('payload')
        self.buffered-=group['buffered'];group['buffered']=0;group['emitted']=True
        self.serial+=1;group['message_id']=self.serial;self.stats['reassembled_messages']+=1
        return Message(completed,assembled,True,self.serial,*completion_timing)

    def candidates(self,message):
        """Apply the EXISTING conditional suffix scan; it proves no entity identity."""
        header,_=self.header(message.payload)
        return header,self.suffixes(message.payload,header) if header['type']==1 else []
