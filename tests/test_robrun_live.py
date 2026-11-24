"""Live collection keeps singletons and missing rounds out of model context."""
import datetime as dt
import json
import os
import ctypes
from pathlib import Path
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, patch

from core.robrun_live import LivePipeline
from data.robrun_feed import RobrunFeed
from data.qa_capture import CaptureRuntime
from data.qa_owner import OwnerProcess

BASE=dt.datetime(2026,1,1,tzinfo=dt.timezone.utc)


class Engine:
    def __init__(self):
        self.history=[]
        self.training_updates=0
        self.delivered=[]
        self.resets=[]
        self.nn=types.SimpleNamespace(is_trained=lambda:True)
    async def handle_spin(self,number):
        self.delivered.append(number)
        self.history.append(number)
    async def reset_history(self,reason):
        self.resets.append(reason)
        self.history.clear()


def accepted(second,number=7):
    observed=BASE+dt.timedelta(seconds=second)
    return {'status':'accepted','number':number,'quality':'unverified_candidate',
            'source_id':'fixture','round_id':str(second),'observed_at':observed.isoformat(),
            'phaseEndUtc':(observed+dt.timedelta(seconds=15)).isoformat(),'phase':'rolling'}


class LiveChecks(unittest.IsolatedAsyncioTestCase):
    async def test_unexpected_producer_stop_is_reported_instead_of_waiting_forever(self):
        with tempfile.TemporaryDirectory() as temporary:
            folder=Path(temporary)
            runtime=CaptureRuntime(folder,folder)
            (folder/'driver-state.json').write_text(json.dumps({'phase':'stopped','stopReason':'lease_expired_or_invalid'}))
            with self.assertRaisesRegex(RuntimeError,'stopped unexpectedly'):
                runtime.check()
            (folder/'app-stop.request').touch()
            runtime.check()
    @unittest.skipUnless(os.name=='nt','Windows process identity')
    async def test_owner_identity_rejects_a_reused_pid(self):
        owner=OwnerProcess(os.getpid())
        try:
            self.assertTrue(owner.alive())
            with self.assertRaisesRegex(RuntimeError,'already exited'):
                OwnerProcess(os.getpid(),owner.created+1)
        finally:owner.close()
    @unittest.skipUnless(os.name=='nt','Windows capture launcher')
    async def test_capture_uses_normal_consent_and_cooperative_owned_stop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            folder=root/'work/roulette-qa/live-fixture'
            process=types.SimpleNamespace(returncode=None,wait=AsyncMock(return_value=0))
            spawn=AsyncMock(return_value=process)
            with patch('data.qa_capture.preflight',return_value=(root/'outputs/roulette-qa-investigation',root/'installed enet.dll',root/'dotnet.exe')), \
                 patch.object(ctypes.windll.shell32,'IsUserAnAdmin',return_value=0), \
                 patch('data.qa_capture.asyncio.create_subprocess_exec',spawn):
                runtime=CaptureRuntime(root,folder)
                await runtime.start()
                command=spawn.call_args.args[-1]
                self.assertIn('-Verb RunAs',command)
                self.assertIn('--owner-pid',command)
                self.assertIn('--owner-created',command)
                self.assertIn('--game-log',command)
                self.assertIn('--run-dir',command)
                self.assertFalse((folder/'app-stop.request').exists())
                await runtime.stop()
                self.assertTrue((folder/'app-stop.request').is_file())
                self.assertIsNone(runtime.lock)
                process.wait.assert_awaited_once()

    async def test_first_result_is_withheld_and_timeout_clears_context(self):
        with tempfile.TemporaryDirectory() as temporary:
            engine=Engine()
            pipeline=LivePipeline(Path(temporary)/'live',engine,emit=lambda _:None)
            await pipeline.observation(accepted(0))
            self.assertEqual(engine.delivered,[])
            await pipeline.observation(accepted(43))
            self.assertEqual(engine.delivered,[7,7])
            await pipeline.tick(BASE+dt.timedelta(seconds=102))
            self.assertEqual(engine.history,[7,7])
            await pipeline.tick(BASE+dt.timedelta(seconds=103))
            self.assertEqual(engine.history,[])
            await pipeline.observation(accepted(130,14))
            await pipeline.tick(BASE+dt.timedelta(seconds=190))
            self.assertEqual(engine.delivered,[7,7])
            self.assertEqual([s['length'] for s in pipeline.streaks.streaks],[2,1])

    async def test_real_feed_quarantines_ambiguity_and_never_joins_rounds_across_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary)
            source=root/'raw.jsonl'
            rows=[]
            for index in range(5):
                observed=BASE+dt.timedelta(seconds=43*index)
                rows.append({'kind':'unverified_candidate','number':index,'phase':'rolling',
                    'tableId':'synthetic','receivedUtc':observed.isoformat(),
                    'phaseEndUtc':(observed+dt.timedelta(seconds=15)).isoformat(),
                    'alternativesInMessage':2 if index==2 else 1})
            source.write_text(''.join(json.dumps(row)+'\n' for row in rows))
            engine=Engine()
            pipeline=LivePipeline(root/'live',engine,emit=lambda _:None)
            feed=RobrunFeed(source,allow_candidates=True)
            feed.on_observation(pipeline.observation)
            await feed.poll_once()
            self.assertEqual(engine.delivered,[0,1,3,4])
            self.assertEqual(engine.history,[3,4])
            self.assertEqual(feed.counters['ambiguous'],1)
            self.assertEqual([s['length'] for s in pipeline.streaks.streaks],[2,2])
            self.assertEqual(pipeline.journal.status['wagers_submitted'],0)
            rows=[json.loads(line) for line in (root/'live/observations.jsonl').read_text().splitlines()]
            self.assertTrue(any(row.get('reason')=='ambiguous_round' for row in rows))


if __name__=='__main__':unittest.main()
