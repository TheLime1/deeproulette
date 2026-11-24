"""Offline bitstream primitives and framing summaries. No prediction heuristics."""
import sys,collections,json
from inspect_enet import application_datagrams,commands
class Bits:
    def __init__(self,data,pos=0):self.data=data;self.pos=pos
    def read(self,n):
        if n<0 or self.pos+n>len(self.data)*8:raise ValueError('Bitstream exhausted')
        start=self.pos//8;shift=self.pos%8;end=(self.pos+n+7)//8
        value=(int.from_bytes(self.data[start:end],'little')>>shift)&((1<<n)-1)
        self.pos+=n;return value
    def ranged(self,minimum,maximum):
        span=maximum-minimum;n=span.bit_length();v=self.read(n)
        if v>span:raise ValueError('Out-of-range encoded value')
        return v+minimum
    def left(self):return len(self.data)*8-self.pos
def app_messages(path,audit=None):
    # ENet fragments become messages only after all offsets/lengths validate.
    # Their receipt time is the last distinct fragment arrival.
    from pathlib import Path
    sys.path.insert(0,str(Path(__file__).resolve().parent/'capture-audit'))
    from enet_reassembly import application_messages
    yield from application_messages(path,audit=audit)
MIN_TS=1767225600000
MAX_TS=2019686400000
MAX_TICK=0x1fffffff
def battle_header(payload):
    b=Bits(payload);kind=b.ranged(0,8);r={'type':kind}
    if kind==0:r['server_tick_start_ts']=b.ranged(MIN_TS,MAX_TS)
    if kind in [0,1,2]:
        r['real_ts']=b.ranged(MIN_TS,MAX_TS)
        if kind==1:r['from_tick']=b.ranged(0,MAX_TICK)
        r['tick']=b.ranged(0,MAX_TICK)
        n=b.ranged(0,15);r['input_buffer_count']=n
        for _ in range(n):b.ranged(0,MAX_TICK);b.ranged(-512,511)
        if kind==0:r['init_extra_byte']=b.read(8)
    r['body_bit_offset']=b.pos
    return r,b
if __name__=='__main__':
    counts=collections.Counter();bad=0;headers=[]
    for ts,direction,d in app_messages(sys.argv[1]):
        if direction!='incoming':continue
        try:r,b=battle_header(d);typ=r['type']
        except ValueError:bad+=1;continue
        counts[typ]+=1
        if 'real_ts' in r:headers.append(dict(r,receive_utc_seconds=ts,receipt_minus_server_ms=ts*1000-r['real_ts']))
    result={'incoming_message_types':dict(sorted(counts.items())),'invalid_headers':bad,'input_buffer_counts':dict(collections.Counter(h['input_buffer_count'] for h in headers)),'first_headers':headers[:3],'last_headers':headers[-3:],'note':'Header fields decoded from recovered bounded bit-stream schema; server real_ts is not result decision time.'}
    print(json.dumps(result,indent=2))
