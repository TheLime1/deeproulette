# Copyright 2026 TheLime1
# Licensed under the Apache License, Version 2.0
"""Bounded local journal feed. Candidate mode is explicitly unverified research.

This module opens local files only. It does not capture traffic or contact a
casino/game. Default input contract: kind=verified_final_result, final=true,
fieldIdentityVerified=true, number=0..36, source endpoint/table, phaseEndUtc.
"""
import asyncio
from collections import Counter, OrderedDict
from datetime import datetime, timezone
import hashlib
import inspect
import ipaddress
import json
import os
from pathlib import Path


def _utc(value):
    if isinstance(value,datetime):parsed=value
    elif isinstance(value,str) and len(value)<=64:
        try:parsed=datetime.fromisoformat(value.replace('Z','+00:00'))
        except ValueError:return None
    else:return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:return None
    return parsed.astimezone(timezone.utc)


def _microseconds(value):
    delta=value-datetime(1970,1,1,tzinfo=timezone.utc)
    return (delta.days*86400+delta.seconds)*1000000+delta.microseconds


def _only_numeric_reading(matches, alternatives, number, phase, deadline_us):
    if (number is None or type(alternatives) is not int or alternatives < 2
            or not isinstance(matches, list) or len(matches) != alternatives):
        return False
    found_number = False
    for match in matches:
        fields = match.get('fields') if isinstance(match, dict) else None
        if not isinstance(fields, dict):
            return False
        if 'winning_number' not in fields:
            continue
        reading = fields['winning_number']
        deadline_ms = fields.get('phase_end_ts')
        if (type(reading) is not int or reading != number or fields.get('phase') != phase
                or type(deadline_ms) is not int or deadline_ms * 1000 != deadline_us):
            return False
        found_number = True
    return found_number


class _JournalTail:
    """Track file identity through exact .1..N bounded rotation siblings."""
    def __init__(self,path,rotation_files,max_read_bytes,max_line_bytes,start_at_end):
        self.path=path;self.rotation_files=rotation_files;self.max_read_bytes=max_read_bytes
        self.max_line_bytes=max_line_bytes;self.start_at_end=start_at_end
        self.identity=None;self.offset=0;self.partial=bytearray();self.discard=False
        self.initialized=False;self.last_path=None;self.read_error=False

    def _inventory(self):
        items=[]
        for index in range(self.rotation_files-1,-1,-1):
            path=self.path if index==0 else self.path.with_name(self.path.name+f'.{index}')
            try:stat=path.stat()
            except FileNotFoundError:continue
            if not path.is_file():continue
            identity=(stat.st_dev,stat.st_ino)
            if not any(item[1]==identity for item in items):items.append((path,identity,stat.st_size))
        return items

    def _lines(self,data,events):
        self.partial.extend(data)
        while True:
            end=self.partial.find(b'\n')
            if end<0:break
            line=bytes(self.partial[:end]);del self.partial[:end+1]
            if self.discard:self.discard=False;continue
            if len(line)>self.max_line_bytes:events.append(('reset','oversized_line'))
            elif line.strip():events.append(('line',line))
        if len(self.partial)>self.max_line_bytes:
            self.partial.clear()
            if not self.discard:events.append(('reset','oversized_line'))
            self.discard=True

    def poll(self):
        events=[]
        try:
            inventory=self._inventory()
            if not inventory:return events
            if not self.initialized:
                chosen=inventory[-1] if self.start_at_end else inventory[0]
                self.last_path,self.identity,size=chosen;self.initialized=True
                if self.start_at_end:
                    self.offset=size
                    if size:
                        with self.last_path.open('rb') as handle:
                            handle.seek(size-1);self.discard=handle.read(1)!=b'\n'
                    return events
            remaining=self.max_read_bytes
            while remaining>0:
                position=next((i for i,item in enumerate(inventory) if item[1]==self.identity),None)
                if position is None:
                    events.append(('reset','journal_retention_gap'))
                    self.last_path,self.identity,_=inventory[0];self.offset=0;self.partial.clear();self.discard=False;position=0
                path,identity,size=inventory[position]
                if path!=self.last_path:events.append(('rotation',None));self.last_path=path
                if size<self.offset:
                    events.append(('reset','journal_truncated'));self.offset=0;self.partial.clear();self.discard=False
                if size>self.offset:
                    with path.open('rb') as handle:
                        stat=os.fstat(handle.fileno())
                        if (stat.st_dev,stat.st_ino)!=identity:return events
                        handle.seek(self.offset);data=handle.read(min(remaining,size-self.offset))
                    self.offset+=len(data);remaining-=len(data);self._lines(data,events)
                    if not data:break
                if self.offset<size or position==len(inventory)-1:break
                # Different files are record boundaries. Never splice an
                # interrupted archived JSON record into the next fresh record.
                if self.partial or self.discard:
                    events.append(('reset','incomplete_rotated_line'));self.partial.clear();self.discard=False
                self.last_path,self.identity,_=inventory[position+1];self.offset=0
                events.append(('rotation',None))
            self.read_error=False
        except OSError:
            if not self.read_error:events.append(('reset','journal_read_error'))
            self.read_error=True
        return events


class RobrunFeed:
    """Async local JSONL tail with round-aware deduplication and gap barriers.

    on_number(callback(int)), on_reset(callback(reason)), and
    on_observation(callback(dict)) register callbacks; reset/observation may be
    synchronous or asynchronous. poll_once() is also available for local replay.
    `since` filters by recorded receivedUtc (decoderEmission.utc fallback), not
    the current wall clock, so bounded historical research remains reproducible.
    """
    _LOSS_KINDS=frozenset(('capture_retention_gap','journal_gap','incomplete_fragment_group',
                          'invalid_fragment_group','late_fragment_conflict','capture_error','capture_stopped'))

    def __init__(self,path,*,allow_candidates=False,since=None,start_at_end=False,poll_interval=0.1,
                 cadence_seconds=43.0,gap_tolerance_seconds=2.0,max_lateness_seconds=60.0,
                 rotation_files=4,max_read_bytes=65536,max_line_bytes=262144,max_rounds=4096,
                 accept_single_numeric_reading=False):
        raw=os.fspath(path)
        if raw.startswith(('\\\\','//')) or '://' in raw:raise ValueError('A local journal path is required')
        if any(type(flag) is not bool for flag in (allow_candidates,start_at_end,accept_single_numeric_reading)):
            raise ValueError('Feed flags must be boolean')
        for value,minimum,maximum in ((rotation_files,1,16),(max_read_bytes,1024,1048576),(max_line_bytes,1024,4194304),(max_rounds,1,100000)):
            if type(value) is not int or not minimum<=value<=maximum:raise ValueError('Invalid local feed bound')
        for value,minimum,maximum in ((poll_interval,.001,10),(cadence_seconds,1,3600),(gap_tolerance_seconds,0,60),(max_lateness_seconds,0,3600)):
            if type(value) not in (int,float) or not minimum<=value<=maximum:raise ValueError('Invalid feed timing')
        self.path=Path(raw).absolute();self.allow_candidates=allow_candidates
        self.accept_single_numeric_reading = accept_single_numeric_reading
        self.since=_utc(since) if since is not None else None
        if since is not None and self.since is None:raise ValueError('since must be a timezone-aware ISO timestamp')
        self.poll_interval=float(poll_interval);self.cadence_us=round(cadence_seconds*1000000)
        self.tolerance_us=round(gap_tolerance_seconds*1000000);self.lateness_us=round(max_lateness_seconds*1000000)
        self._tail=_JournalTail(self.path,rotation_files,max_read_bytes,max_line_bytes,start_at_end)
        self._numbers=[];self._resets=[];self._observations=[];self._seen=OrderedDict();self._max_rounds=max_rounds
        self._highwater=OrderedDict();self._source=None;self._salt=os.urandom(32)
        self.counters=Counter();self.running=True

    def on_number(self,callback):self._numbers.append(callback)
    def on_reset(self,callback):self._resets.append(callback)
    def on_observation(self,callback):self._observations.append(callback)
    def stop(self):self.running=False
    async def disconnect(self):self.stop()

    @staticmethod
    async def _call(callbacks,*args):
        for callback in callbacks:
            result=callback(*args)
            if inspect.isawaitable(result):await result

    def _token(self,value):return hashlib.sha256(self._salt+repr(value).encode('utf-8')).hexdigest()[:24]

    @staticmethod
    def _source_key(event):
        endpoint=event.get('endpoint');table=event.get('tableId',event.get('tableEntityId'))
        if table is not None and (type(table) not in (str,int) or type(table) is str and not 1<=len(table)<=128):return None
        if isinstance(endpoint,dict):
            address=endpoint.get('address');port=endpoint.get('port')
            if not isinstance(address,str) or len(address)>64 or type(port) is not int or not 1<=port<=65535:return None
            try:address=str(ipaddress.ip_address(address))
            except ValueError:return None
            return address,port,table
        return ('table',table) if table is not None else None

    async def _observe(self,status,reason=None,context=None):
        context=context or {}
        row={'status':status,'reason':reason,'number':context.get('number'),
             'observed_at':context.get('observed_at'),'phaseEndUtc':context.get('phaseEndUtc'),
             'quality':context.get('quality'),'phase':context.get('phase'),
             'source_id':context.get('source_id'),'round_id':context.get('round_id')}
        await self._call(self._observations,row)

    async def _reset(self,reason,context=None):
        self.counters['resets']+=1
        await self._call(self._resets,reason)
        await self._observe('reset',reason,context)

    def _remember(self,key,number):
        self._seen[key]=number;self._seen.move_to_end(key)
        while len(self._seen)>self._max_rounds:self._seen.popitem(last=False)

    def _advance(self,source,deadline):
        self._highwater[source]=max(deadline,self._highwater.get(source,deadline));self._highwater.move_to_end(source)
        while len(self._highwater)>128:self._highwater.popitem(last=False)

    async def _event(self,event):
        if not isinstance(event,dict):self.counters['ignored']+=1;return
        kind=event.get('kind')
        if not isinstance(kind,str):self.counters['ignored']+=1;return
        if kind in self._LOSS_KINDS:
            self.counters['source_gaps']+=1;await self._reset('source_data_gap');return
        if kind not in ('unverified_candidate','verified_final_result'):
            self.counters['ignored']+=1;return
        self.counters['received']+=1
        number=event.get('number');number=number if type(number) is int and 0<=number<=36 else None
        phase=event.get('phase');phase=phase if phase in ('bets','rolling','dolly','reward','final') else None
        deadline=_utc(event.get('phaseEndUtc'));source=self._source_key(event)
        emitted=event.get('decoderEmission');emitted=_utc(emitted.get('utc')) if isinstance(emitted,dict) else None
        observed=_utc(event.get('receivedUtc')) or emitted
        deadline_us=_microseconds(deadline) if deadline is not None else None
        context={'number':number,'phase':phase,'quality':kind,
                 'observed_at':observed.isoformat() if observed else None,
                 'phaseEndUtc':deadline.isoformat() if deadline else None,
                 'source_id':self._token(source) if source else None,
                 'round_id':self._token((source,deadline_us)) if source and deadline else None}
        await self._observe('received',context=context)
        if kind=='unverified_candidate' and not self.allow_candidates:
            self.counters['unverified_rejected']+=1;await self._observe('rejected','unverified_not_enabled',context);return
        if kind=='unverified_candidate' and phase!='rolling':
            self.counters['non_rolling']+=1;await self._observe('rejected','non_rolling_candidate',context);return
        if self.since is not None and (observed is None or observed<self.since):
            self.counters['before_since']+=1;await self._observe('rejected','before_since_or_missing_timestamp',context);return
        if observed is None:
            self.counters['invalid']+=1
            await self._reset('missing_observed_timestamp',context)
            await self._observe('rejected','missing_observed_timestamp',context);return
        if source is None or deadline is None:
            self.counters['invalid']+=1;await self._reset('missing_round_identity',context);await self._observe('rejected','missing_round_identity',context);return
        key=(source,deadline_us)
        alternatives=event.get('alternativesInMessage',1 if kind=='verified_final_result' else None)
        valid_number=number is not None
        trusted=kind!='verified_final_result' or event.get('fieldIdentityVerified') is True and event.get('final') is True
        unambiguous=type(alternatives) is int and alternatives==1
        selected_numeric_reading = (
            not unambiguous and kind == 'unverified_candidate' and self.accept_single_numeric_reading
            and _only_numeric_reading(event.get('matches'), alternatives, number, phase, deadline_us)
        )
        admissible_reading = unambiguous or selected_numeric_reading
        if key in self._seen:
            old=self._seen[key]
            if old is not None and valid_number and admissible_reading and trusted and old==number:
                self.counters['duplicates']+=1;await self._observe('duplicate','same_source_round',context);return
            if old is None:
                self.counters['quarantined']+=1;await self._observe('rejected','quarantined_round',context);return
            self.counters['conflicts']+=1;self._remember(key,None)
            await self._reset('conflicting_round',context);await self._observe('rejected','conflicting_round',context);return
        if self._source is not None and self._source!=source:
            self.counters['source_changes']+=1;await self._reset('source_change',context)
        self._source=source
        previous=self._highwater.get(source)
        if previous is not None and deadline_us<=previous:
            self.counters['out_of_order']+=1;self._remember(key,None)
            await self._reset('out_of_order_round',context);await self._observe('rejected','out_of_order_round',context);return
        reason=None
        if not trusted:reason='unverified_final_result';self.counters['unverified_rejected']+=1
        elif not valid_number:reason='invalid_number';self.counters['invalid']+=1
        elif not admissible_reading:reason='ambiguous_round';self.counters['ambiguous']+=1
        elif kind=='unverified_candidate' and event.get('phaseExpiredAtDecode') is True or observed is not None and _microseconds(observed)-deadline_us>self.lateness_us:
            reason='late_round';self.counters['late']+=1
        if reason:
            self._remember(key,None);self._advance(source,deadline_us)
            await self._reset(reason,context);await self._observe('rejected',reason,context);return
        if previous is not None and abs((deadline_us-previous)-self.cadence_us)>self.tolerance_us:
            self.counters['gaps']+=1;await self._reset('round_cadence_gap',context)
        self._remember(key,number);self._advance(source,deadline_us)
        self.counters['accepted']+=1
        self.counters['accepted_candidates' if kind=='unverified_candidate' else 'accepted_verified']+=1
        if selected_numeric_reading:
            self.counters['accepted_single_numeric_reading'] += 1
        await self._observe('accepted','single_numeric_reading' if selected_numeric_reading else None,context)
        await self._call(self._numbers,number)

    async def poll_once(self):
        """Process one bounded local read; returns newly delivered number count."""
        before=self.counters['accepted']
        for kind,value in self._tail.poll():
            if not self.running:break
            if kind=='rotation':self.counters['journal_rotations']+=1
            elif kind=='reset':
                self.counters[value]+=1;await self._reset(value)
            else:
                self.counters['lines']+=1
                try:event=json.loads(value)
                except (ValueError,UnicodeError,RecursionError):
                    self.counters['malformed_json']+=1;await self._reset('malformed_json')
                else:await self._event(event)
            # Async callbacks may do synchronous model/filesystem work. Yield
            # between records so timers, stop requests and heartbeats can run.
            await asyncio.sleep(0)
        return self.counters['accepted']-before

    async def listen(self):
        try:
            while self.running:
                await self.poll_once()
                if self.running:await asyncio.sleep(self.poll_interval)
        finally:self.running=False
