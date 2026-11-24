"""Read completed bounded captures; emit conditional suffix candidates, never forecasts.
Does not start/stop capture, read game memory, or alter controller records.
"""
import argparse,collections,ctypes,datetime,ipaddress,json,msvcrt,sys
from pathlib import Path
HERE=Path(__file__).resolve().parent
DURATION={'bets':20000,'rolling':15000,'dolly':5000,'reward':3000}

def options(parser=None):
    parser=parser or argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--records-dir',type=Path,help='Directory of completed bounded-*.json records and companion PCAPNG files')
    parser.add_argument('--output-ledger',type=Path,default=HERE/'completed-batches-summary.json')
    parser.add_argument('--base-dir',type=Path,help='Directory containing battle_bits.py and inspect_capture.py; capture-audit/ and codec-agent/ subdirectories are supported')
    return parser

def initialize(args):
    candidates=[args.base_dir.resolve()] if args.base_dir else [HERE,HERE.parent]
    base=next((p for p in candidates if (p/'battle_bits.py').is_file() and (p/'inspect_capture.py').is_file()),None)
    if base is None:raise ValueError('Decoder dependencies not found; supply --base-dir')
    for p in [base,base/'capture-audit',base/'codec-agent',HERE]:sys.path.insert(0,str(p))
    global Bits,battle_header,roulette,application_messages,inspect_capture
    from battle_bits import Bits,battle_header
    from roulette_codec import roulette
    from enet_reassembly import application_messages
    import inspect_capture
    args.records_dir=(args.records_dir or base/'private-capture').resolve()
    args.output_ledger=args.output_ledger.resolve()
    return args

def clock_mark():
    qpc=ctypes.c_longlong();frequency=ctypes.c_longlong();kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    if not kernel.QueryPerformanceCounter(ctypes.byref(qpc)) or not kernel.QueryPerformanceFrequency(ctypes.byref(frequency)):
        raise OSError('Performance counter unavailable')
    return dict(utc=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='microseconds'),qpc=qpc.value,qpcFrequency=frequency.value)

def suffixes(payload,header):
    n=len(payload)*8;matches=[]
    for start in range(max(110,n-320),n-44):
        if not Bits(payload,start-1).read(1):continue
        try:r=roulette(Bits(payload,start))
        except ValueError:continue
        fields=r['fields'];phase=fields.get('phase');deadline=fields.get('phase_end_ts')
        if phase is None or deadline is None:continue
        future=deadline-header['real_ts'];tail=n-r['end_bit']
        if not(-100<=future<=DURATION[phase]+100 and 0<=tail<=64):continue
        r.update(precedingCountBit=1,tailBits=tail,futureAtServerHeaderMs=future,allTrailingBitsZero=(int.from_bytes(payload,'little')>>r['end_bit'])==0)
        matches.append(r)
    return matches

def companion_capture(record_path,record,records_dir):
    recorded=Path(record['pcapng']);companion=record_path.parent/recorded.name
    pcap=(companion if companion.is_file() else recorded if recorded.is_absolute() else record_path.parent/recorded).resolve()
    if not pcap.is_relative_to(records_dir):raise ValueError('Capture is outside --records-dir')
    return pcap

def summarize(record_path,record,records_dir):
    pcap=companion_capture(record_path,record,records_dir)
    endpoint=record['endpoint'];address=str(ipaddress.ip_address(endpoint['address']));port=int(endpoint['port'])
    if not 1<=port<=65535:raise ValueError('Invalid capture endpoint port')
    inspect_capture.SERVERS={(address,port)}
    audit={};counts=collections.Counter();frames=[];invalid=0;processing_started=clock_mark()
    for received,direction,payload in application_messages(pcap,audit=audit):
        if direction!='incoming':continue
        try:header,b=battle_header(payload)
        except ValueError:invalid+=1;continue
        counts[header['type']]+=1
        if header['type']!=1:continue
        matches=suffixes(payload,header)
        if not matches:continue
        emission=clock_mark();emission_ms=datetime.datetime.fromisoformat(emission['utc']).timestamp()*1000
        for match in matches:
            fields=match['fields'];phase=fields['phase'];deadline=fields['phase_end_ts']
            match['deadlineRemainingAtDecoderEmissionMs']=deadline-emission_ms
            if phase=='rolling':
                match['candidateBettingCloseTs']=deadline-DURATION['rolling']
                match['serverHeaderAfterCandidateBettingCloseMs']=header['real_ts']-match['candidateBettingCloseTs']
        frames.append(dict(decoderEmission=emission,decoderEmissionUTC=emission['utc'],decoderEmissionQPC=emission['qpc'],decoderEmissionQPCFrequency=emission['qpcFrequency'],receivedUtc=datetime.datetime.fromtimestamp(received,datetime.timezone.utc).isoformat(timespec='microseconds'),receivedUnixSeconds=received,serverRealTs=header['real_ts'],tick=header['tick'],fromTick=header['from_tick'],matches=matches,classification='Conditional suffix candidate; identity unproven. Rolling matches are post-close checks, not forecasts.'))
    return dict(record=str(record_path),pcapng=str(pcap),iteration=record.get('iteration'),captureStart=record.get('startSucceeded'),captureStopped=record.get('stopCompleted'),replayReady=record.get('replayReady'),processingStarted=processing_started,processingCompleted=clock_mark(),incomingHeaderTypes=dict(counts),invalidHeaders=invalid,transportAudit=audit,frames=frames)

def process_locked(args):
    directory=args.records_dir;output=args.output_ledger;previous={}
    if output.exists():
        try:previous=json.loads(output.read_text(encoding='utf-8-sig'))
        except (ValueError,OSError) as error:raise ValueError('Existing ledger is unreadable; refusing to replace first-emission history') from error
    batches=previous.get('batches',[]);seen={q['record'] for q in batches};skipped=[];errors=[];new=[]
    for record_path in sorted(directory.glob('bounded-*.json')):
        key=str(record_path.resolve())
        if key in seen:continue
        try:record=json.loads(record_path.read_text(encoding='utf-8-sig'))
        except (ValueError,OSError):skipped.append(dict(record=key,reason='Record not yet complete JSON'));continue
        if record.get('stopped') is not True or record.get('error') is not None or not record.get('replayReady'):
            skipped.append(dict(record=key,reason='Not a stopped, error-null, replay-ready batch'));continue
        try:
            pcap=companion_capture(record_path.resolve(),record,directory)
            if not pcap.is_file():raise ValueError('Capture file is not ready')
            item=summarize(record_path.resolve(),record,directory)
        except Exception as error:errors.append(dict(record=key,error=str(error)));continue
        batches.append(item);seen.add(key);new.append(item)
    result=dict(kind='completed_batch_suffix_summary',summaryRun=clock_mark(),notice='Only completed capture records were processed. Every match is an unproven suffix candidate, never a prediction.',newBatchCount=len(new),batchCount=len(batches),skipped=skipped,errors=errors,batches=batches)
    temporary=output.with_suffix(output.suffix+'.tmp');temporary.write_text(json.dumps(result,indent=2));temporary.replace(output)
    return dict(output=str(output),summaryRun=result['summaryRun'],newBatchCount=len(new),batchCount=len(batches),skipped=skipped,errors=errors,newFrames=[dict(record=q['record'],frames=q['frames']) for q in new])

def run_once(args):
    target=args.output_ledger;target.parent.mkdir(parents=True,exist_ok=True)
    with target.with_suffix(target.suffix+'.lock').open('a+b') as lock:
        lock.seek(0,2)
        if lock.tell()==0:lock.write(b'0');lock.flush()
        lock.seek(0)
        try:msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
        except OSError:return dict(kind='summary_busy',output=str(target))
        try:return process_locked(args)
        finally:lock.seek(0);msvcrt.locking(lock.fileno(),msvcrt.LK_UNLCK,1)

if __name__=='__main__':
    args=initialize(options().parse_args());print(json.dumps(run_once(args),indent=2))
