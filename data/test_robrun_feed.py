"""Local journal fixtures only; no TensorFlow, training, casino or network I/O."""
import asyncio
from datetime import datetime,timedelta,timezone
import json
from pathlib import Path
import tempfile
import time
import unittest

from data.robrun_feed import RobrunFeed


START=datetime(2026,11,24,tzinfo=timezone.utc)
def event(round_index,number=24,**changes):
    deadline=START+timedelta(seconds=43*round_index)
    value={'kind':'unverified_candidate','number':number,'phase':'rolling','phaseEndUtc':deadline.isoformat(),
           'receivedUtc':(deadline-timedelta(seconds=15)).isoformat(),'alternativesInMessage':1,
           'endpoint':{'address':'192.0.2.199','port':36601},'fieldIdentityVerified':False,'phaseExpiredAtDecode':False}
    value.update(changes);return value
def encode(value):return (json.dumps(value)+'\n').encode('utf-8')


class FeedChecks(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.directory=tempfile.TemporaryDirectory();self.addCleanup(self.directory.cleanup)
        self.path=Path(self.directory.name)/'realtime-events-v2.jsonl'
        self.path.write_bytes(b'');self.calls=[];self.observations=[]

    def feed(self,**kwargs):
        feed=RobrunFeed(self.path,**kwargs)
        async def number(n):self.calls.append(('number',n))
        async def reset(reason):self.calls.append(('reset',reason))
        feed.on_number(number);feed.on_reset(reset);feed.on_observation(self.observations.append)
        return feed

    def append(self,*values):
        with self.path.open('ab') as writer:
            for value in values:writer.write(encode(value))

    async def drain(self,feed):
        for _ in range(100):
            before=feed._tail.offset;identity=feed._tail.identity
            await feed.poll_once()
            if feed._tail.offset==before and feed._tail.identity==identity:break

    async def test_default_rejects_candidates_and_requires_verified_final(self):
        feed=self.feed();self.append(event(0),event(1,kind='verified_final_result',final=True,fieldIdentityVerified=True))
        await feed.poll_once();self.assertEqual(self.calls,[('number',24)])
        self.assertEqual(feed.counters['unverified_rejected'],1)
        self.assertEqual(feed.counters['accepted_verified'],1)

    async def test_eligible_records_require_aware_timestamp_even_without_since(self):
        feed=self.feed()
        self.append(event(0,kind='verified_final_result',final=True,fieldIdentityVerified=True,receivedUtc=None),
                    event(1,kind='verified_final_result',final=True,fieldIdentityVerified=True,receivedUtc='2026-11-24T00:00:00'),
                    event(2,kind='verified_final_result',final=True,fieldIdentityVerified=True,receivedUtc=None,
                          decoderEmission={'utc':(START+timedelta(seconds=71)).isoformat()}))
        await feed.poll_once()
        self.assertEqual(self.calls,[('reset','missing_observed_timestamp'),('reset','missing_observed_timestamp'),('number',24)])
        accepted=[row for row in self.observations if row['status']=='accepted']
        self.assertIsNotNone(accepted[0]['observed_at'])

    async def test_repeat_numbers_are_distinct_rounds_but_duplicate_round_is_not(self):
        feed=self.feed(allow_candidates=True);self.append(event(0),event(0),event(1),event(2,0))
        await feed.poll_once();self.assertEqual(self.calls,[('number',24),('number',24),('number',0)])
        self.assertEqual(feed.counters['duplicates'],1)

    async def test_bool_and_out_of_range_numbers_rejected(self):
        feed=self.feed(allow_candidates=True)
        self.append(event(0,True),event(1,-1),event(2,37),event(3,'24'),event(4,0))
        await feed.poll_once()
        self.assertEqual([call for call in self.calls if call[0]=='number'],[('number',0)])
        self.assertEqual(feed.counters['invalid'],4)

    async def test_only_rolling_candidates(self):
        feed=self.feed(allow_candidates=True)
        self.append(event(0,phase='bets'),event(0,phase='dolly'),event(0,phase='reward'),event(0))
        await feed.poll_once();self.assertEqual(self.calls,[('number',24)])

    async def test_ambiguous_round_quarantined_and_reset_before_next_number(self):
        feed=self.feed(allow_candidates=True)
        self.append(event(0),event(1,alternativesInMessage=2),event(1),event(2,29))
        await feed.poll_once()
        self.assertEqual(self.calls,[('number',24),('reset','ambiguous_round'),('number',29)])
        self.assertEqual(feed.counters['ambiguous'],1);self.assertEqual(feed.counters['quarantined'],1)

    async def test_boolean_alternative_count_is_not_one(self):
        feed=self.feed(allow_candidates=True);self.append(event(0,alternativesInMessage=True));await feed.poll_once()
        self.assertEqual(self.calls,[('reset','ambiguous_round')])

    async def test_qa_live_accepts_single_number_alongside_a_numberless_reading(self):
        def observed(index, number, second_number=None):
            fields = {'phase': 'rolling', 'phase_end_ts': int(START.timestamp() + 43 * index) * 1000}
            other = dict(fields)
            if second_number is not None:
                other['winning_number'] = second_number
            return event(index, number, alternativesInMessage=2,
                         matches=[{'fields': dict(fields, winning_number=number)}, {'fields': other}])

        feed = self.feed(allow_candidates=True, accept_single_numeric_reading=True)
        twelve = observed(1, 12)
        self.append(event(0, 28), twelve, twelve, observed(2, 13), observed(3, 7, 32), event(4, 2))
        await feed.poll_once()
        self.assertEqual(self.calls, [('number', 28), ('number', 12), ('number', 13),
                                      ('reset', 'ambiguous_round'), ('number', 2)])
        self.assertEqual(feed.counters['accepted_single_numeric_reading'], 2)
        self.assertEqual(feed.counters['duplicates'], 1)
        self.assertEqual(feed.counters['ambiguous'], 1)
        accepted = [row for row in self.observations if row['status'] == 'accepted']
        self.assertEqual(accepted[1]['reason'], 'single_numeric_reading')
        self.assertEqual(accepted[1]['quality'], 'unverified_candidate')

    async def test_cadence_gap_resets_before_number(self):
        feed=self.feed(allow_candidates=True);self.append(event(0),event(2,19));await feed.poll_once()
        self.assertEqual(self.calls,[('number',24),('reset','round_cadence_gap'),('number',19)])

    async def test_source_change_including_table_resets_history(self):
        feed=self.feed(allow_candidates=True)
        self.append(event(0,tableId=1),event(1,tableId=2),event(2,endpoint={'address':'192.0.2.200','port':36601},tableId=2))
        await feed.poll_once()
        self.assertEqual(self.calls,[('number',24),('reset','source_change'),('number',24),('reset','source_change'),('number',24)])

    async def test_conflicting_round_and_unseen_out_of_order_reset(self):
        feed=self.feed(allow_candidates=True);self.append(event(2),event(2,19),event(1,17),event(3,29));await feed.poll_once()
        self.assertEqual(self.calls,[('number',24),('reset','conflicting_round'),('reset','out_of_order_round'),('number',29)])

    async def test_expired_and_late_rounds_reset_but_are_not_delivered(self):
        feed=self.feed(allow_candidates=True)
        self.append(event(0,phaseExpiredAtDecode=True),event(1,receivedUtc=(START+timedelta(seconds=200)).isoformat()),event(2,19))
        await feed.poll_once()
        self.assertEqual(self.calls,[('reset','late_round'),('reset','late_round'),('number',19)])

    async def test_qa_live_accepts_delayed_decode_within_receipt_bound(self):
        feed = self.feed(allow_candidates=True, allow_expired_candidates=True)
        self.append(event(0, 12, phaseExpiredAtDecode=True),
                    event(1, 13, phaseExpiredAtDecode=True, receivedUtc=(START+timedelta(seconds=104)).isoformat()))
        await feed.poll_once()
        self.assertEqual(self.calls, [('number', 12), ('reset', 'late_round')])
        self.assertTrue(next(row for row in self.observations if row['status']=='accepted')['phase_expired_at_decode'])

    async def test_since_filter_uses_recorded_aware_timestamp(self):
        feed=self.feed(allow_candidates=True,since=START)
        self.append(event(0),event(1,19),event(2,receivedUtc=None));await feed.poll_once()
        self.assertEqual(self.calls,[('number',19)]);self.assertEqual(feed.counters['before_since'],2)

    async def test_metadata_sanitized_and_round_ids_stable(self):
        feed=self.feed(allow_candidates=True)
        self.append(event(0,Name='PRIVATE-PLAYER',playerId='PRIVATE-ID'),event(0),{'kind':'other','name':'PRIVATE-PLAYER'},{'kind':{}})
        await feed.poll_once();encoded=json.dumps(self.observations)
        for private in ('192.0.2.199','PRIVATE-PLAYER','PRIVATE-ID','endpoint','tableId','playerId'):self.assertNotIn(private,encoded)
        self.assertEqual(self.observations[0]['round_id'],self.observations[-1]['round_id'])
        self.assertEqual(len(self.observations[0]['round_id']),24)
        other=self.feed(allow_candidates=True);await other.poll_once()
        self.assertNotEqual(self.observations[0]['round_id'],self.observations[-1]['round_id'])

    async def test_partial_line_survives_poll_until_complete(self):
        feed=self.feed(allow_candidates=True);raw=encode(event(0));self.path.write_bytes(raw[:31])
        await feed.poll_once();self.assertEqual(self.calls,[])
        with self.path.open('ab') as writer:writer.write(raw[31:])
        await feed.poll_once();await feed.poll_once();self.assertEqual(self.calls,[('number',24)])

    async def test_rotation_continues_old_file_and_reads_new_without_replay(self):
        feed=self.feed(allow_candidates=True);self.append(event(0));await feed.poll_once()
        self.path.rename(self.path.with_name(self.path.name+'.1'));self.path.write_bytes(encode(event(1,19)))
        await feed.poll_once();await feed.poll_once();self.assertEqual(self.calls,[('number',24),('number',19)])
        self.assertGreater(feed.counters['journal_rotations'],0)

    async def test_startup_archive_order_is_oldest_to_base(self):
        self.path.with_name(self.path.name+'.2').write_bytes(encode(event(0,0)))
        self.path.with_name(self.path.name+'.1').write_bytes(encode(event(1,19)))
        self.path.write_bytes(encode(event(2,29)));feed=self.feed(allow_candidates=True)
        await feed.poll_once();self.assertEqual(self.calls,[('number',0),('number',19),('number',29)])

    async def test_unread_file_retirement_is_gap_before_new_number(self):
        feed=self.feed(allow_candidates=True,rotation_files=1);self.append(event(0));await feed.poll_once()
        self.path.rename(self.path.with_name('gone'));self.path.write_bytes(encode(event(1,19)))
        await feed.poll_once();self.assertEqual(self.calls,[('number',24),('reset','journal_retention_gap'),('number',19)])

    async def test_incomplete_archive_not_spliced_into_fresh_line(self):
        feed=self.feed(allow_candidates=True);self.path.write_bytes(encode(event(0))[:30]);await feed.poll_once()
        self.path.rename(self.path.with_name(self.path.name+'.1'));self.path.write_bytes(encode(event(1,19)))
        await feed.poll_once();self.assertEqual(self.calls,[('reset','incomplete_rotated_line'),('number',19)])

    async def test_malformed_and_oversized_line_gaps_recover_bounded(self):
        feed=self.feed(allow_candidates=True,max_line_bytes=1024,max_read_bytes=1024)
        self.path.write_bytes(b'{bad json}\n'+b'x'*3000+b'\n'+encode(event(0)))
        await self.drain(feed)
        self.assertEqual(self.calls,[('reset','malformed_json'),('reset','oversized_line'),('number',24)])
        self.assertLessEqual(len(feed._tail.partial),1024)

    async def test_read_and_round_cache_bounds(self):
        feed=self.feed(allow_candidates=True,max_read_bytes=1024,max_rounds=3)
        self.append(*(event(i,i%37) for i in range(20)));await feed.poll_once()
        self.assertLessEqual(feed._tail.offset,1024);await self.drain(feed)
        self.assertEqual(feed.counters['accepted'],20);self.assertLessEqual(len(feed._seen),3)

    async def test_start_at_end_discards_initial_partial_line_only(self):
        raw=encode(event(1,19));self.path.write_bytes(encode(event(0))+raw[:20])
        feed=self.feed(allow_candidates=True,start_at_end=True);await feed.poll_once()
        with self.path.open('ab') as writer:writer.write(raw[20:]+encode(event(2,29)))
        await feed.poll_once();self.assertEqual(self.calls,[('number',29)])

    async def test_loss_diagnostic_reset_and_sync_callback_support(self):
        feed=self.feed(allow_candidates=True);resets=[];feed.on_reset(resets.append)
        self.append(event(0),{'kind':'capture_retention_gap'},event(1,19));await feed.poll_once()
        self.assertEqual(self.calls,[('number',24),('reset','source_data_gap'),('number',19)])
        self.assertEqual(resets,['source_data_gap'])

    async def test_listen_can_stop_and_does_not_import_tensorflow(self):
        import sys
        feed=self.feed(allow_candidates=True,poll_interval=.001)
        feed.on_number(lambda _:feed.stop());self.append(event(0))
        await asyncio.wait_for(feed.listen(),timeout=1)
        self.assertFalse(feed.running);self.assertNotIn('tensorflow',sys.modules)

    async def test_timer_stop_runs_between_synchronous_model_callbacks(self):
        feed=self.feed(allow_candidates=True,poll_interval=.001)
        # Simulate a synchronous per-record model operation without TensorFlow.
        feed.on_number(lambda _:time.sleep(.002))
        self.append(*(event(i,i%37) for i in range(100)))
        timer=asyncio.get_running_loop().call_later(.001,feed.stop)
        try:await asyncio.wait_for(feed.listen(),timeout=1)
        finally:timer.cancel()
        delivered=[call for call in self.calls if call[0]=='number']
        self.assertGreater(len(delivered),0);self.assertLess(len(delivered),100)


if __name__=='__main__':unittest.main(verbosity=2)
