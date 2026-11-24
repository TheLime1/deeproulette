"""Bounded offline ENet message extraction; fragment pieces never escape as messages.

application_messages(path, audit=dict) yields (completion_timestamp, direction, payload)
for channel1, compatible with battle_bits.app_messages. Audit emits metadata only.
The entire bounded input is validated before yielding, so a later conflicting duplicate
can invalidate its group. No synthetic merged PCAP or early fragment timestamp is used.
"""
from pathlib import Path
import sys,struct,collections,hashlib,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from inspect_capture import application_datagrams
from inspect_enet import commands,decompress

FIXED={1:8,2:48,3:44,4:8,5:4,10:12,11:16}
HEAD={6:6,7:8,8:24,9:8,12:24}
MAX_MESSAGE=8*1024*1024
MAX_FRAGMENT_COUNT=8192
MAX_GROUPS=512
MAX_BUFFER=64*1024*1024
MAX_MESSAGES=100000
MAX_FRAGMENT_SPAN=5.0

def transport_commands(packet):
    if commands(packet,4) is None:raise ValueError('ENet checksum4 framing did not validate')
    flags=struct.unpack_from('>H',packet)[0];peer_session=flags&0x3fff
    off=(4 if flags&0x8000 else 2)+4
    data=packet
    if flags&0x4000:
        data=decompress(packet[off:]);off=0
        if data is None:raise ValueError('ENet decompression failed')
    while off<len(data):
        cmd=data[off]&15;channel=data[off+1];seq=struct.unpack_from('>H',data,off+2)[0]
        if cmd in FIXED:size=FIXED[cmd];payload=b'';meta={}
        else:
            head=HEAD[cmd];length=struct.unpack_from('>H',data,off+(4 if cmd==6 else 6))[0]
            size=head+length;payload=data[off+head:off+size];meta={}
            if cmd in (8,12):
                start,declared,count,num,total,offset=struct.unpack_from('>HHIIII',data,off+4)
                meta={'start':start,'declared':declared,'count':count,'number':num,'total':total,'offset':offset}
        yield cmd,channel,seq,peer_session,payload,meta
        off+=size

def application_messages(path,audit=None,channels=(1,)):
    stats=collections.Counter();groups={};messages=[];buffered=0;ordinal=0
    command_counts=collections.Counter();direct_signatures=collections.Counter()
    for ts,direction,server,packet in application_datagrams(path):
        stats['datagrams']+=1
        for cmd,channel,seq,peer,payload,meta in transport_commands(packet):
            command_counts[(direction,cmd,channel)]+=1;ordinal+=1
            if channel not in channels or not payload:continue
            if cmd not in (8,12):
                if cmd not in (6,7,9):raise ValueError('Unsupported payload-bearing ENet command')
                messages.append({'ts':ts,'direction':direction,'payload':payload,'ordinal':ordinal,'group':None})
                buffered+=len(payload)
                # Hash is never emitted; exact retransmission candidates are count-only metadata.
                direct_signatures[(direction,cmd,channel,seq,hashlib.sha256(payload).digest())]+=1
                stats['direct_messages']+=1
            else:
                stats['fragment_pieces']+=1
                # Reliable fragments share start sequence; unreliable fragments also share
                # the current reliable sequence context in their command header.
                key=(direction,server,peer,channel,cmd,meta['start'],seq if cmd==12 else None)
                group=groups.get(key)
                if group is None:
                    if len(groups)>=MAX_GROUPS:raise ValueError('Fragment group limit exceeded')
                    group={'ordinal':len(groups)+1,'direction':direction,'command':cmd,'channel':channel,
                           'count':meta['count'],'total':meta['total'],'first_ts':ts,'last_ts':ts,
                           'pieces':{},'invalid':False,'reasons':set(),'duplicates':0}
                    groups[key]=group
                def invalid(reason):group['invalid']=True;group['reasons'].add(reason)
                group['last_ts']=max(group['last_ts'],ts);group['first_ts']=min(group['first_ts'],ts)
                if meta['count']!=group['count'] or meta['total']!=group['total']:invalid('inconsistent_count_or_total')
                if not 1<=meta['count']<=MAX_FRAGMENT_COUNT or not 1<=meta['total']<=MAX_MESSAGE:invalid('size_limit')
                if meta['number']>=meta['count']:invalid('fragment_number_outside_count')
                if not payload or meta['declared']!=len(payload) or meta['offset']+len(payload)>meta['total']:invalid('length_or_offset_outside_total')
                if group['last_ts']-group['first_ts']>MAX_FRAGMENT_SPAN:invalid('arrival_span_limit')
                if cmd==8 and ((seq-meta['start'])&65535)!=meta['number']:invalid('reliable_sequence_mismatch')
                old=group['pieces'].get(meta['number'])
                if old is not None:
                    if old['offset']==meta['offset'] and old['payload']==payload:
                        group['duplicates']+=1;stats['identical_fragment_duplicates']+=1
                    else:invalid('conflicting_duplicate_fragment')
                else:
                    group['pieces'][meta['number']]={'offset':meta['offset'],'payload':payload,'ts':ts,'seq':seq}
                    buffered+=len(payload)
                # The ordering token is the arrival that completes the set, not its first piece.
                if len(group['pieces'])==group['count'] and 'completion_ordinal' not in group:
                    group['completion_ordinal']=ordinal;group['completion_ts']=ts
            if buffered>MAX_BUFFER or len(messages)>MAX_MESSAGES:raise ValueError('Bounded message storage exceeded')
    group_metadata=[]
    for group in groups.values():
        pieces=group['pieces'];ordered=sorted(pieces.values(),key=lambda p:p['offset']);cursor=0
        if not 1<=group['count']<=MAX_FRAGMENT_COUNT or len(pieces)!=group['count'] or any(n<0 or n>=group['count'] for n in pieces):
            group['invalid']=True;group['reasons'].add('missing_fragment_numbers')
        for piece in ordered:
            if piece['offset']!=cursor:group['invalid']=True;group['reasons'].add('overlap_or_gap')
            cursor=piece['offset']+len(piece['payload'])
        if cursor!=group['total']:group['invalid']=True;group['reasons'].add('coverage_does_not_equal_total')
        complete=not group['invalid']
        if complete:
            payload=b''.join(p['payload'] for p in ordered)
            buffered+=len(payload)
            if buffered>MAX_BUFFER or len(messages)>=MAX_MESSAGES:raise ValueError('Bounded assembled message storage exceeded')
            # Maximum first occurrence across distinct fragments is the complete-message receipt time.
            complete_ts=max(p['ts'] for p in pieces.values())
            messages.append({'ts':complete_ts,'direction':group['direction'],'payload':payload,
                             'ordinal':group['completion_ordinal'],'group':group['ordinal']})
            stats['reassembled_messages']+=1
        else:stats['rejected_fragment_groups']+=1
        group_metadata.append({k:group[k] for k in ('ordinal','direction','command','channel','count','total','first_ts','last_ts','duplicates')}|
            {'received_distinct_pieces':len(pieces),'complete':complete,'reasons':sorted(group['reasons']),
             'piece_lengths_offsets':[{'number':number,'length':len(p['payload']),'offset':p['offset'],'receipt_ts':p['ts']} for number,p in sorted(pieces.items())]})
    stats['yielded_messages']=len(messages)
    result={'counts':dict(stats),'commands':[(list(k),v) for k,v in command_counts.most_common()],
            'fragment_groups':group_metadata,
            'direct_exact_signature_duplicate_candidates':sum(n-1 for n in direct_signatures.values() if n>1),
            'limits':{'message_bytes':MAX_MESSAGE,'fragment_count':MAX_FRAGMENT_COUNT,'groups':MAX_GROUPS,'buffer_bytes':MAX_BUFFER,'fragment_span_seconds':MAX_FRAGMENT_SPAN},
            'note':'No application bytes or sequence/session values emitted. Reassembled message timestamp is last distinct fragment arrival; no synthetic PCAP.'}
    if audit is not None:audit.update(result)
    for message in sorted(messages,key=lambda m:(m['ts'],m['ordinal'])):
        yield message['ts'],message['direction'],message['payload']

if __name__=='__main__':
    path=Path(sys.argv[1]);quality={};types=collections.Counter();invalid_headers=0
    from battle_bits import battle_header
    for ts,direction,payload in application_messages(path,quality):
        if direction!='incoming':continue
        try:header,_=battle_header(payload);types[header['type']]+=1
        except ValueError:invalid_headers+=1
    quality['incoming_message_types_after_reassembly']=dict(sorted(types.items()))
    quality['invalid_incoming_headers_after_reassembly']=invalid_headers
    out=path.with_suffix('.quality.json');out.write_text(json.dumps(quality,indent=2))
    print(json.dumps(quality,indent=2))
