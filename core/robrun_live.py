"""Interactive ROB RUN QA collection and research model; no wagers or casino socket."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import uuid

from data.qa_capture import CaptureRuntime
from data.robrun_feed import RobrunFeed
from robrun import RunJournal, score_mode, utcnow
from streaks import StreakRecorder, RED


class LivePipeline:
    def __init__(self, folder, engine, emit=print):
        self.folder=Path(folder)
        self.journal=RunJournal(self.folder)
        self.engine=engine
        self.emit=emit
        self.pending=[]
        self.lock=asyncio.Lock()
        self.streaks=StreakRecorder(self.folder/'streaks-60s',60,on_feed=self.pending.append)
        self.current=None
        self.forecast_utc=None
        self.journal.status.update(mode='robrun_qa_live',quality='unverified_candidate',training_updates=0)
        self.engine.on_spin_complete=self.scored
        self.engine.on_advice=self.advice

    async def drain(self):
        rows=list(self.pending)
        self.pending.clear()
        for row in rows:
            if row['kind']=='journal_gap':
                await self.engine.reset_history('streak_boundary')
                self.forecast_utc=None
            else:
                self.current=row
                await self.engine.handle_spin(row['number'])

    async def observation(self,row):
        async with self.lock:
            self.journal.observation(row)
            if row['status']=='accepted':
                event={'kind':row['quality'],'number':row['number'],'tableId':row['source_id'],
                       'receivedUtc':row['observed_at'],'phaseEndUtc':row['phaseEndUtc'],
                       'phase':row['phase'],'alternativesInMessage':1,
                       'final':row['quality']=='verified_final_result',
                       'fieldIdentityVerified':row['quality']=='verified_final_result'}
                self.streaks.accept(event)
                active=self.streaks.active
                color='green' if row['number']==0 else 'red' if row['number'] in RED else 'black'
                self.emit(f"RESULT CANDIDATE {row['number']} {color.upper()} | streak {active['id']}: {active['length']} entries | unverified")
                self.journal.status.update(last_number=row['number'],last_observation_utc=row['observed_at'])
            elif row['status']=='reset':
                self.streaks.accept({'kind':'journal_gap'})
                # Even an unqualified singleton can leave pending context from a
                # previous model run; explicit reset barriers always clear it.
                await self.engine.reset_history(row.get('reason'))
                self.forecast_utc=None
            elif row['status']=='rejected' and row.get('reason')!='before_since_or_missing_timestamp':
                self.emit(f"QUARANTINED: {row.get('reason')} | no model label accepted")
            await self.drain()

    async def tick(self,now=None):
        async with self.lock:
            self.streaks.expire(now or dt.datetime.now(dt.timezone.utc))
            await self.drain()
            self.streaks.publish()
            self.journal.status.update(updated_utc=utcnow(),contiguous_history=len(self.engine.history),
                training_updates=self.engine.training_updates,streaks=len(self.streaks.streaks),
                eligible_streaks=sum(s['model_eligible'] for s in self.streaks.streaks))
            self.journal.publish()

    def advice(self,result):
        self.forecast_utc=utcnow()
        ranking=[int(n) for n in result['predicted']]
        self.journal.append('rankings.jsonl',{'generated_utc':self.forecast_utc,'ranking':ranking,
            'after_result_received_utc':self.current['receivedUtc'],'trained_checkpoint':self.engine.nn.is_trained(),
            'quality':'unverified_candidate','wager_amount':0})
        self.emit(f'NEXT OBSERVATION ranking: {ranking} | experimental, uncalibrated | no wagers')

    def scored(self,result):
        mode=score_mode(self.current['receivedUtc'],self.forecast_utc)
        self.journal.append('scores.jsonl',{'observed_at':self.current['receivedUtc'],'actual':result['number'],
            'ranking':[int(n) for n in result['predicted']],'ranking_hit':bool(result['is_win']),
            'evaluation_mode':mode,'forecast_generated_utc':self.forecast_utc,'wager_amount':0,
            'quality':'unverified_candidate'})


async def run_qa_live(root,config,*,runtime_factory=CaptureRuntime,max_seconds=0):
    from core.engine import PredictionEngine
    from strategies.base import BaseStrategy
    root=Path(root)
    strategy=config['strategy_cls']()
    class ResearchStrategy(BaseStrategy):
        MODEL_LABEL='robrun_qa_live'
        def select_numbers(self,probabilities):return strategy.select_numbers(probabilities)
        def calculate_bets(self,numbers,balance,bet_per_number=None):return {int(n):0.0 for n in numbers}
        def evaluate_result(self,actual,predicted,bets,balance):return actual in predicted,0.0,balance
        def get_display_label(self):return strategy.get_display_label()
    run_id='live-'+dt.datetime.now(dt.timezone.utc).strftime('%Y%m%d-%H%M%S')+'-'+uuid.uuid4().hex[:6]
    folder=root/'data_store/robrun'/run_id
    capture_folder=root/'work/roulette-qa'/run_id
    capture_folder.mkdir(parents=True)
    events=capture_folder/'realtime-events-v2.jsonl'
    events.touch()
    since=dt.datetime.now(dt.timezone.utc)
    profile=config.get('robrun_profile')
    engine=PredictionEngine(ResearchStrategy(),1.0,bet_per_number=0.0,auto_train=config['auto_train'],use_live=True,
        model_dir=str(profile.active_dir if profile else folder/'models'),neural_net=profile.network if profile else None)
    # Live always starts a new current stream; archived training data is not replayed.
    pipeline=LivePipeline(folder,engine,emit=lambda text:print(text,flush=True))
    feed=RobrunFeed(events,allow_candidates=True,since=since,poll_interval=.02)
    feed.on_observation(pipeline.observation)
    runtime=runtime_factory(root,capture_folder)
    pipeline.journal.status.update(state='starting',capture_directory=str(capture_folder),
                                  online_training_enabled=config['auto_train'])
    pipeline.journal.publish()
    print(f'ROB RUN QA LIVE | model: {"saved ROB RUN profile" if profile else "fresh untrained"}',flush=True)
    print('60-second streak timeout; singleton streaks withheld from model input. Ambiguous candidates are quarantined.',flush=True)
    print(f'Data: {folder}\nWaiting for the game connection. Windows may request capture permission. Ctrl+C stops and saves.',flush=True)
    started=asyncio.get_running_loop().time()
    last_tick=0
    try:
        await runtime.start()
        pipeline.journal.status['state']='collecting'
        while feed.running:
            runtime.check()
            await feed.poll_once()
            now=asyncio.get_running_loop().time()
            if now-last_tick>=.5:
                pipeline.journal.status['feed_counts']=dict(feed.counters)
                await pipeline.tick()
                last_tick=now
            if max_seconds and asyncio.get_running_loop().time()-started>=max_seconds:break
            await asyncio.sleep(.02)
        pipeline.journal.status['state']='stopped'
    except BaseException as error:
        pipeline.journal.status['state']='stopped' if isinstance(error,(KeyboardInterrupt,asyncio.CancelledError)) else 'failed'
        pipeline.journal.status['error_type']=type(error).__name__
        raise
    finally:
        feed.stop()
        await runtime.stop()
        pipeline.streaks.close('app_stopped',dt.datetime.now(dt.timezone.utc))
        if engine.training_updates:engine.nn.save()
        pipeline.journal.status.update(updated_utc=utcnow(),feed_counts=dict(feed.counters),training_updates=engine.training_updates)
        pipeline.journal.publish()
        print(f'QA Live stopped; data saved in {folder}',flush=True)
    return folder
