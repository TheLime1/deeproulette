"""Decode the owned continuous recorder without stopping it during code reloads."""
import argparse
import collections
import ctypes
import datetime as dt
import importlib
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import time

HERE=Path(__file__).resolve().parent
CODEC=HERE/'network-observer/codec'
CONTROL=HERE.parent.parent/'work/roulette-qa/live-console-control.json'


def alive(pid):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    kernel.OpenProcess.argtypes=[ctypes.c_uint32,ctypes.c_bool,ctypes.c_uint32]
    kernel.OpenProcess.restype=ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes=[ctypes.c_void_p,ctypes.POINTER(ctypes.c_uint32)]
    kernel.CloseHandle.argtypes=[ctypes.c_void_p]
    handle=kernel.OpenProcess(0x1000,False,int(pid))
    if not handle:return False
    try:
        code=ctypes.c_uint32()
        return bool(kernel.GetExitCodeProcess(handle,ctypes.byref(code))) and code.value==259
    finally:kernel.CloseHandle(handle)


def read_json(path,default=None):
    try:return json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError,ValueError):return default


def write_json(path,value,*,attempts=3):
    temporary=path.with_name(path.name+f'.{os.getpid()}.tmp')
    temporary.write_text(json.dumps(value,indent=2),encoding='utf-8')
    # Windows readers may briefly hold a handle without FILE_SHARE_DELETE.
    # Retry normal replacement; a transient metadata lock must not kill capture.
    for attempt in range(attempts):
        try:
            temporary.replace(path)
            return True
        except PermissionError:
            if attempt+1<attempts:time.sleep(0.005)
    return False


def run(folder,*,poll_keys,should_stop,should_reload,signature,on_event,line,status):
    folder=Path(folder)
    lease=folder/'realtime-lease.json'
    stop_file=folder/'realtime-stop.request'
    driver_state=folder/'driver-state.json'
    checkpoint_file=folder/'decoder-checkpoint.json'
    before=signature()
    if not write_json(lease,{'pid':os.getpid(),'utc':dt.datetime.now(dt.timezone.utc).isoformat()},attempts=100):
        raise RuntimeError('Cannot publish the initial recorder lease')
    state=read_json(driver_state,{})
    if (read_json(CONTROL,{}) or {}).get('pause_capture'):
        if state.get('driverPid') and alive(state['driverPid']):
            stop_file.write_text('Coordinated receiver test',encoding='utf-8')
            deadline=time.monotonic()+20
            while alive(state['driverPid']) and time.monotonic()<deadline:time.sleep(0.1)
            if alive(state['driverPid']):raise RuntimeError('Owned recorder cleanup is still active; test capture must wait.')
        line('Recorder paused for a coordinated latency test; CMD remains live.')
        while (read_json(CONTROL,{}) or {}).get('pause_capture'):
            poll_keys()
            status(folder,'paused_for_receiver_test')
            if should_stop():return 'stop'
            if should_reload() or signature()!=before:return 'reload'
            time.sleep(0.2)
    if not state or not alive(state.get('driverPid',0)):
        if stop_file.exists():stop_file.unlink()
        script=HERE/'network-observer/Start-RobrunRealtime.ps1'
        if not script.is_file():raise RuntimeError('Continuous recorder script is missing')
        shell=Path(os.environ.get('SystemRoot',r'C:\Windows'))/'System32/WindowsPowerShell/v1.0/powershell.exe'
        command=[str(shell),'-NoProfile','-NonInteractive','-File',str(script),
                 '-RunDirectory',str(folder),'-LeaseFile',str(lease),'-StopFile',str(stop_file),
                 '-WorkspaceRoot',str(HERE.parent.parent)]
        if os.environ.get('ROBRUN_DOTNET'):
            command.extend(['-DotnetPath',os.environ['ROBRUN_DOTNET']])
        if os.environ.get('ROBRUN_GAME_LOG'):
            command.extend(['-GameLogPath',os.environ['ROBRUN_GAME_LOG']])
        with (folder/'recorder-console.log').open('a',encoding='utf-8') as output:
            child=subprocess.Popen(command,stdout=output,stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NEW_PROCESS_GROUP|subprocess.CREATE_NO_WINDOW)
        driver_pid=child.pid
    else:driver_pid=state['driverPid']
    sys.path.insert(0,str(CODEC))
    decoder=importlib.import_module('summarize_batches')
    decoder.initialize(argparse.Namespace(base_dir=CODEC,records_dir=folder,output_ledger=folder/'unused-ledger.json'))
    from stream_transport import StreamTransport
    from capture_tail import CaptureTail
    from bounded_jsonl import BoundedJsonl
    saved=read_json(checkpoint_file,{})
    seen=collections.OrderedDict.fromkeys(saved.get('seen',[])[-4096:]);tail=None;parser=None;segment=None
    lease_at=heartbeat_at=checkpoint_at=sample_at=0.;packet_count=message_count=0;last_lag=None;last_error=None
    # Keep the fast packet/key path free of directory scans and JSON reads.
    control_at=state_at=signature_at=driver_check_at=0.;control={};changed=False;attach_at=time.monotonic();segment_attach_at=attach_at
    delays=[];qpc_delays=[];delay_count=0
    line('CONTINUOUS MODE: packets are decoded while capture stays running.')
    # Versioned rings keep the previous unbounded logs as closed history rather
    # than carrying their old sizes into the new retention contract.
    with BoundedJsonl(folder/'realtime-events-v2.jsonl') as output, BoundedJsonl(folder/'latency-samples-v2.jsonl',max_bytes=1024*1024) as samples:
        while True:
            poll_keys()
            now=time.monotonic()
            if now-lease_at>0.5:
                write_json(lease,{'pid':os.getpid(),'utc':dt.datetime.now(dt.timezone.utc).isoformat()});lease_at=now
            if now-state_at>=0.2:
                state=read_json(driver_state,{});state_at=now
            if now-control_at>=0.25:
                control=read_json(CONTROL,{}) or {};control_at=now
            if now-signature_at>=0.25:
                changed=signature()!=before;signature_at=now
            if control.get('pause_capture'):
                stop_file.write_text('Coordinated receiver test',encoding='utf-8')
                line('Pausing the owned recorder for a short latency test...')
                until=time.monotonic()+20
                while alive(driver_pid) and time.monotonic()<until:time.sleep(0.1)
                if alive(driver_pid):raise RuntimeError('Owned recorder cleanup is still active; test capture must wait.')
                while (read_json(CONTROL,{}) or {}).get('pause_capture'):
                    poll_keys();status(folder,'paused_for_receiver_test')
                    if should_stop():return 'stop'
                    if should_reload() or signature()!=before:return 'reload'
                    time.sleep(0.2)
                return 'reload'
            if should_stop():
                stop_file.write_text('User requested stop',encoding='utf-8')
                line('Stopping the owned recorder and receiver safely...')
                until=time.monotonic()+20
                while alive(driver_pid) and time.monotonic()<until:time.sleep(0.1)
                if alive(driver_pid):line(f'Recorder cleanup is still running (PID {driver_pid}); no new recorder was started.')
                status(folder,'stopped')
                return 'stop'
            if now-driver_check_at>=1:
                if not alive(driver_pid):
                    raise RuntimeError('The owned capture driver exited unexpectedly: '+str(state.get('error') or state.get('stopReason') or 'unknown reason'))
                driver_check_at=now
            if should_reload() or changed:
                if tail:write_json(checkpoint_file,{'segment':segment,'checkpoint':tail.checkpoint(),'seen':list(seen)[-4096:]})
                line('Reloading decoder code; the continuous packet recorder stays running.')
                return 'reload'
            if state.get('phase')=='ready':
                last_error=None
                if state['segmentId']!=segment:
                    checkpoint=None
                    if saved.get('segment')==state['segmentId']:
                        checkpoint=saved.get('checkpoint')
                    tail=CaptureTail(state['pcapng'],checkpoint=checkpoint)
                    parser=StreamTransport(state['endpoint']['address'],state['endpoint']['port'],state['localAddress'],state['localPorts'],codec_dir=CODEC)
                    segment=state['segmentId']
                    segment_attach_at=time.monotonic()
                    line(f"Streaming {state['endpoint']['address']}:{state['endpoint']['port']} -- no batch export.")
                packets=tail.poll()
                for diagnostic in tail.drain_diagnostics():
                    # A bounded writer may retire unread chunks. Never join a
                    # partial transport message across that explicitly marked gap.
                    parser=StreamTransport(state['endpoint']['address'],state['endpoint']['port'],state['localAddress'],state['localPorts'],codec_dir=CODEC)
                    line('CAPTURE RETENTION GAP: unread data may have expired; pending fragments discarded.')
                    output.write(json.dumps(diagnostic)+'\n');output.flush()
                for packet in packets:
                    packet_count+=1
                    for message in parser.feed_packet(packet.receipt_ts,packet.frame,packet.link_type,
                            timing=(packet.event_qpc,packet.receiver_qpc,packet.qpc_frequency)):
                        message_count+=1
                        try:header,matches=parser.candidates(message)
                        except ValueError:continue
                        # Stamp after decoding; the old metric omitted decoder work.
                        mark=decoder.clock_mark();stamp=dt.datetime.fromisoformat(mark['utc']).timestamp()
                        decode_qpc_ns=time.perf_counter_ns()
                        last_lag=(stamp-message.receipt_ts)*1000
                        delay_count+=1
                        if len(delays)<4096:delays.append(last_lag)
                        qpc_lag=None
                        if message.event_qpc is not None and message.qpc_frequency:
                            qpc_lag=(decode_qpc_ns*message.qpc_frequency-message.event_qpc*1_000_000_000)/(message.qpc_frequency*1_000_000)
                            if len(qpc_delays)<4096:qpc_delays.append(qpc_lag)
                        for match in matches:
                            fields=match['fields'];number=fields.get('winning_number')
                            if not isinstance(number,int) or not 0<=number<=36:continue
                            key=json.dumps([state['endpoint'],fields['phase_end_ts'],fields['phase'],number],sort_keys=True)
                            if key in seen:continue
                            seen[key]=None
                            if len(seen)>4096:seen.popitem(last=False)
                            remaining=fields['phase_end_ts']-stamp*1000
                            event={'kind':'unverified_candidate','number':number,'phase':fields['phase'],
                                   'phaseEndUtc':dt.datetime.fromtimestamp(fields['phase_end_ts']/1000,dt.timezone.utc).isoformat(),
                                   'endpoint':state['endpoint'],'receivedUtc':dt.datetime.fromtimestamp(message.receipt_ts,dt.timezone.utc).isoformat(),
                                   'decoderEmission':mark,'serverRealTs':header['real_ts'],'tick':header['tick'],
                                   'alternativesInMessage':len(matches),'matches':matches,'phaseExpiredAtDecode':remaining<0,
                                   'mode':'continuous_realtime','fieldIdentityVerified':False,'latencyMs':last_lag,
                                   'packetEventQpc':message.event_qpc,'receiverCallbackQpc':message.receiver_qpc,
                                   'qpcFrequency':message.qpc_frequency,'decodeCompletedPerfCounterNs':decode_qpc_ns,
                                   'receiptToDecodeQpcMs':qpc_lag,'qpcClockDomainValidated':False}
                            # Write to the visible console before JSON serialization.
                            # render() appends the stdout completion timestamp.
                            on_event(event)
                            output.write(json.dumps(event)+'\n');output.flush()
                    for diagnostic in parser.drain_diagnostics():
                        output.write(json.dumps({'kind':'transport_diagnostic',**diagnostic})+'\n');output.flush()
                if now-checkpoint_at>0.5:
                    write_json(checkpoint_file,{'segment':segment,'checkpoint':tail.checkpoint(),'seen':list(seen)[-4096:]});checkpoint_at=now
            elif state.get('phase')=='error' and not alive(driver_pid):
                error=str(state.get('error','unknown'))
                if error!=last_error:
                    line('Recorder startup error: '+error.split(';',1)[0]+'. Waiting for a code fix or Ctrl+R.')
                    status(folder,'recorder_error',error=error)
                    last_error=error
                # Keep the window responsive to a saved fix or Ctrl+R.
            now=time.monotonic()
            if now-heartbeat_at>5 and last_error is None:
                lag=f'{last_lag:.0f} ms' if last_lag is not None else 'waiting for first packet'
                line(f'LIVE stream: {packet_count} packets, {message_count} messages; latest decode delay {lag}.')
                if output.dropped_records or samples.dropped_records:
                    line(f'DIAGNOSTIC LOG LOSS: {output.dropped_records} event records, {samples.dropped_records} timing records; capture continues.')
                status(folder,'continuous',driverPid=driver_pid,segment=segment,packets=packet_count,messages=message_count,lastLatencyMs=last_lag,
                       eventLogDroppedRecords=output.dropped_records,timingLogDroppedRecords=samples.dropped_records)
                heartbeat_at=now
            if delays and now-sample_at>1:
                ordered=sorted(delays)
                qpc_ordered=sorted(qpc_delays)
                def percentile(fraction):return ordered[max(0,math.ceil(len(ordered)*fraction)-1)]
                samples.write(json.dumps({'utc':dt.datetime.now(dt.timezone.utc).isoformat(),
                        'latencyMs':last_lag,'measurement':'packet_timestamp_to_decode_complete_utc',
                        'sampleCount':len(delays),'decodedCountInWindow':delay_count,'sampleCap':4096,
                        'minMs':ordered[0],'p50Ms':percentile(.50),'p95Ms':percentile(.95),'maxMs':ordered[-1],
                        'qpcAgeMs':None if not qpc_ordered else {'count':len(qpc_ordered),'min':qpc_ordered[0],
                            'p50':qpc_ordered[max(0,math.ceil(len(qpc_ordered)*.50)-1)],
                            'p95':qpc_ordered[max(0,math.ceil(len(qpc_ordered)*.95)-1)],'max':qpc_ordered[-1],
                            'clockDomainValidated':False},
                        'receiptToDecodeUtcMsSamples':delays,'receiptToDecodeQpcMsSamples':qpc_delays,
                        'startupWindow':sample_at==0 or sample_at-segment_attach_at<5,
                        'secondsSinceSegmentAttach':now-segment_attach_at,
                        'eventLogDroppedRecords':output.dropped_records,'timingLogDroppedRecords':samples.dropped_records,
                        'transportStatistics':dict(parser.stats) if parser is not None else {},
                        'secondsSinceAttach':now-attach_at,'packets':packet_count,'messages':message_count,'segment':segment})+'\n')
                sample_at=now;delays.clear();qpc_delays.clear();delay_count=0
            # Requested idle interval, not a Windows scheduling/latency guarantee.
            time.sleep((0 if packets else 0.005) if state.get('phase')=='ready' else 0.05)
