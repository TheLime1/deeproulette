"""Bounded polling of receiver-owned PCAPNG chunks; no capture/session control.

The manifest comes from our receiver, not the game. File retirement is reported
as an explicit gap; callers must discard pending transport fragments after it.
Prepared offline; runtime validation remains pending.
"""
import collections
import json
from pathlib import Path
import time

from stream_transport import PcapngTail


class CaptureTail:
    def __init__(self,path,checkpoint=None):
        self.base=Path(path).resolve()
        self.manifest_path=Path(str(self.base)+'.manifest.json')
        self.index=0;self.tail=None;self.generation=None;self.rotation=False
        self.manifest=None;self.manifest_key=None;self.manifest_read_at=0.
        self.recent=collections.deque(maxlen=4096)
        self.diagnostics=collections.deque(maxlen=32)
        self.pending=None
        # This is deliberately not restored across reload: a new parser must
        # reconstruct fragments from retained replay data rather than assume
        # that the previous parser's already-consumed bytes are still in memory.
        self.completed_through=-1
        if checkpoint:
            if checkpoint.get('rotationVersion')==1:
                if Path(checkpoint['basePath']).resolve()!=self.base:raise ValueError('Rotation checkpoint has another base path')
                generation=checkpoint.get('generation')
                if type(generation) is not int or generation<1:raise ValueError('Invalid saved capture generation')
                self.generation=generation
                cursor=checkpoint.get('replay') or checkpoint.get('current')
                if cursor:
                    self.index=int(cursor['index']);self.pending=cursor['checkpoint']
                self.rotation=True
            else:
                self.pending=dict(checkpoint,offset=checkpoint.get('replay_offset',checkpoint['offset']))

    def _path(self,index):
        if not isinstance(index,int) or not 0<=index<=2**31-1:raise ValueError('Invalid capture chunk index')
        return self.base if index==0 else self.base.with_name(f'{self.base.stem}.chunk-{index:06d}.pcapng')

    def _read_manifest(self):
        try:stat=self.manifest_path.stat()
        except FileNotFoundError:
            if self.rotation:raise ValueError('Rotation manifest disappeared')
            return None
        key=(stat.st_mtime_ns,stat.st_size,stat.st_ino);now=time.monotonic()
        if self.manifest is not None and key==self.manifest_key and now-self.manifest_read_at<.25:return self.manifest
        if stat.st_size>64*1024:raise ValueError('Oversized capture manifest')
        try:data=json.loads(self.manifest_path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:return self.manifest
        if data.get('version')!=1 or Path(data['basePath']).resolve()!=self.base:raise ValueError('Capture manifest identity mismatch')
        generation=data['generation'];chunks=data['chunks'];retired=data['retiredThrough']
        if type(generation) is not int or generation<1 or (self.generation is not None and generation<self.generation):raise ValueError('Capture generation moved backwards')
        if type(retired) is not int or retired< -1 or not isinstance(chunks,list) or not 1<=len(chunks)<=16:raise ValueError('Invalid capture retention manifest')
        expected=retired+1
        for chunk in chunks:
            index=chunk['index'];path=Path(chunk['path']).resolve();length=chunk['length']
            if type(index) is not int or index!=expected or path!=self._path(index):raise ValueError('Capture chunk identity/order mismatch')
            if type(chunk['closed']) is not bool or type(length) is not int or not 48<=length<=1024**3:raise ValueError('Invalid capture chunk metadata')
            expected+=1
        if any(not chunk['closed'] for chunk in chunks[:-1]):raise ValueError('A previous capture chunk is still open')
        self.manifest=data;self.generation=generation;self.manifest_key=key;self.manifest_read_at=now;self.rotation=True
        return data

    def _open(self):
        path=self._path(self.index)
        if self.pending and Path(self.pending['path']).resolve()!=path:raise ValueError('Capture checkpoint chunk mismatch')
        self.tail=PcapngTail(path,checkpoint=self.pending);self.pending=None

    def _remember(self,packets):
        template={'path':str(self.tail.path),'endian':self.tail.endian,
                  'interfaces':list(self.tail.interfaces),'identity':self.tail.identity}
        for packet in packets:
            checkpoint=dict(template,offset=packet.block_offset,replay_offset=packet.block_offset)
            self.recent.append((packet.receipt_ts,{'index':self.index,'checkpoint':checkpoint}))

    def poll(self,*,max_packets=128,max_frame_bytes=2*1024*1024):
        if min(max_packets,max_frame_bytes)<1:raise ValueError('Positive capture poll bounds required')
        manifest=self._read_manifest()
        if manifest is None:
            if self.tail is None:self._open()
            packets=self.tail.poll(max_packets=max_packets,max_frame_bytes=max_frame_bytes)
            self._remember(packets);return packets
        chunks=manifest['chunks'];first=chunks[0]['index'];last=chunks[-1]['index']
        if self.index<first:
            if self.completed_through<first-1:
                self.diagnostics.append({'kind':'capture_retention_gap','firstMissingChunk':self.index,
                    'resumeAtChunk':first,'retiredThrough':manifest['retiredThrough'],
                    'meaning':'Unread data may have been retired; pending fragments must be discarded.'})
                self.recent.clear()
            self.index=first;self.tail=None;self.pending=None
        if self.index>last:raise ValueError('Checkpoint is beyond the capture manifest')
        packets=[];frame_bytes=0
        while len(packets)<max_packets and frame_bytes<max_frame_bytes:
            chunk=chunks[self.index-first]
            if self.tail is None:self._open()
            try:batch=self.tail.poll(max_packets=max_packets-len(packets),max_frame_bytes=max_frame_bytes-frame_bytes)
            except FileNotFoundError:
                # A writer can retire a closed chunk after our manifest read.
                # Refresh next iteration and report a gap before decoding more.
                self.manifest_key=None;return packets
            self._remember(batch);packets.extend(batch);frame_bytes+=sum(len(packet.frame) for packet in batch)
            if not chunk['closed'] or self.tail.offset<chunk['length']:break
            if self.tail.offset!=chunk['length']:raise ValueError('Closed capture chunk length mismatch')
            self.completed_through=self.index
            if self.index==last:break
            self.index+=1;self.tail=None
        return packets

    def checkpoint(self,rewind_seconds=5.):
        current={'index':self.index,'checkpoint':self.tail.checkpoint() if self.tail else self.pending} if self.tail or self.pending else None
        cutoff=max((item[0] for item in self.recent),default=0)-rewind_seconds
        replay=next((cursor for stamp,cursor in self.recent if stamp>=cutoff),current)
        if not self.rotation:
            return self.tail.checkpoint(rewind_seconds) if self.tail else {}
        return {'rotationVersion':1,'basePath':str(self.base),'generation':self.generation,
                'current':current,'replay':replay}

    def drain_diagnostics(self):
        result=list(self.diagnostics);self.diagnostics.clear();return result
