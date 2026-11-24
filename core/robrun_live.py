"""Interactive ROB RUN QA collection and research model; no wagers or casino socket."""
import asyncio
import datetime as dt
import json
from pathlib import Path
import uuid

from config.settings import AUTO_TRAIN_MIN, SEQUENCE_LENGTH
from core.qa_manual_bets import BET_HELP, ManualBetLog
from data.qa_capture import CaptureRuntime
from data.robrun_feed import RobrunFeed
from robrun import RunJournal, score_mode, utcnow
from streaks import StreakRecorder, RED
from ui.qa_bet_input import BetInput


def result_label(number):
    if number is None:
        return 'unknown number'
    color = 'GREEN' if number == 0 else 'RED' if number in RED else 'BLACK'
    return f'{number} {color}'


def capture_summary(snapshot, counters):
    phase = snapshot.get('phase')
    if phase != 'ready':
        return f"Capture starting ({phase or 'waiting for Windows capture host'})."
    packets = snapshot.get('packets')
    traffic = 'waiting for game traffic' if not packets else f"{packets:,} packets, {snapshot.get('messages', 0):,} messages"
    return (f"Capture ready | {traffic} | number records received: {counters.get('received', 0)} | "
            f"accepted: {counters.get('accepted', 0)} | ambiguous: {counters.get('ambiguous', 0)}")


class LivePipeline:
    def __init__(self, folder, engine, emit=print):
        self.folder=Path(folder)
        self.journal=RunJournal(self.folder)
        self.engine=engine
        self.emit=emit
        self.pending=[]
        self.lock=asyncio.Lock()
        self.streaks=StreakRecorder(self.folder/'streaks-60s',60,on_feed=self.pending.append)
        self.manual_bets = ManualBetLog(self.journal, self.emit, self.streaks.timeout)
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
            accepted = row['status'] == 'accepted'
            updates_before = self.engine.training_updates
            if accepted:
                event={'kind':row['quality'],'number':row['number'],'tableId':row['source_id'],
                       'receivedUtc':row['observed_at'],'phaseEndUtc':row['phaseEndUtc'],
                       'phase':row['phase'],'alternativesInMessage':1,
                       'final':row['quality']=='verified_final_result',
                       'fieldIdentityVerified':row['quality']=='verified_final_result'}
                self.streaks.accept(event)
                active=self.streaks.active
                self.emit(f"[{row['observed_at']}] RESULT: {result_label(row['number'])}")
                self.emit(f"  Phase: {row['phase'] or 'unknown'} | streak #{active['id']}: "
                          f"{active['length']} {'entry' if active['length'] == 1 else 'entries'} | saved to session log")
                self.manual_bets.observe(row)
                if active['length'] == 1:
                    self.emit('  Model history: waiting for a second result within 60 seconds.')
                self.journal.status.update(last_number=row['number'],last_observation_utc=row['observed_at'])
            elif row['status']=='reset':
                self.streaks.accept({'kind':'journal_gap'})
                self.manual_bets.close(reason=(row.get('reason') or 'source gap').replace('_', ' '))
                # Even an unqualified singleton can leave pending context from a
                # previous model run; explicit reset barriers always clear it.
                await self.engine.reset_history(row.get('reason'))
                self.forecast_utc=None
            elif row['status']=='rejected' and row.get('reason')!='before_since_or_missing_timestamp':
                receipt = row.get('observed_at') or 'time unavailable'
                reason = (row.get('reason') or 'unknown').replace('_', ' ')
                self.emit(f"[{receipt}] QUARANTINED: {result_label(row.get('number'))} | {reason} | excluded from model history")
            await self.drain()
            if accepted and active['length'] >= 2:
                history_size = len(self.engine.history)
                added = 'first two results added' if active['length'] == 2 else 'result added'
                context = f'{history_size}/{SEQUENCE_LENGTH} consecutive results' if history_size < SEQUENCE_LENGTH else f'{history_size} consecutive results, context ready'
                self.emit(f'  Model history: {added} | {context}')
                if self.engine.training_updates > updates_before:
                    self.emit(f'  Auto-training: update #{self.engine.training_updates} completed.')
                elif self.engine.auto_train and history_size < AUTO_TRAIN_MIN:
                    self.emit(f'  Auto-training: {history_size}/{AUTO_TRAIN_MIN} consecutive results needed for an update.')

    async def tick(self,now=None):
        async with self.lock:
            now = now or dt.datetime.now(dt.timezone.utc)
            self.streaks.expire(now)
            self.manual_bets.expire(now)
            await self.drain()
            self.streaks.publish()
            self.journal.status.update(updated_utc=utcnow(),contiguous_history=len(self.engine.history),
                training_updates=self.engine.training_updates,streaks=len(self.streaks.streaks),
                eligible_streaks=sum(s['model_eligible'] for s in self.streaks.streaks))
            self.journal.publish()

    def advice(self,result):
        self.forecast_utc=utcnow()
        ranking=[int(n) for n in result['predicted']]
        selection = result.get('bet_label') or ', '.join(map(str, ranking))
        self.journal.append('rankings.jsonl',{'generated_utc':self.forecast_utc,'ranking':ranking,'suggested_selection':selection,
            'after_result_received_utc':self.current['receivedUtc'],'trained_checkpoint':self.engine.nn.is_trained(),
            'quality':'unverified_candidate','wager_amount':0})
        self.emit(f'NEXT ROUND SUGGESTION: {selection}')
        self.emit('  Place bets in the game if you choose. Enter your actual bet here or leave it blank to skip.')

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
    keyboard = BetInput()
    pipeline=LivePipeline(folder,engine,emit=keyboard.emit)
    feed=RobrunFeed(events,allow_candidates=True,since=since,poll_interval=.02)
    feed.on_observation(pipeline.observation)
    runtime=runtime_factory(root,capture_folder)
    pipeline.journal.status.update(state='starting',capture_directory=str(capture_folder),
                                  online_training_enabled=config['auto_train'])
    pipeline.journal.publish()
    print(f'ROB RUN QA LIVE | model: {"saved ROB RUN profile" if profile else "fresh untrained"}',flush=True)
    print('60-second streak timeout; singleton streaks withheld from model input. Ambiguous candidates are quarantined.',flush=True)
    print(f'Suggestions start after {SEQUENCE_LENGTH} consecutive results. {BET_HELP}', flush=True)
    print(f'Data: {folder}\nStarting capture. Approve the Windows prompt if shown. Ctrl+C stops and saves.',flush=True)
    started=asyncio.get_running_loop().time()
    last_tick=0
    last_report = started
    previous_phase = None
    try:
        await runtime.start()
        pipeline.journal.status['state']='collecting'
        while feed.running:
            snapshot = runtime.check() or {}
            keyboard.poll(pipeline.manual_bets)
            await feed.poll_once()
            now=asyncio.get_running_loop().time()
            phase = snapshot.get('phase')
            if phase != previous_phase or now - last_report >= 15:
                keyboard.emit(capture_summary(snapshot, feed.counters))
                previous_phase = phase
                last_report = now
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
        pipeline.manual_bets.close(reason='app stopped before next result')
        await runtime.stop()
        pipeline.streaks.close('app_stopped',dt.datetime.now(dt.timezone.utc))
        if engine.training_updates:engine.nn.save()
        pipeline.journal.status.update(updated_utc=utcnow(),feed_counts=dict(feed.counters),training_updates=engine.training_updates)
        pipeline.journal.publish()
        print(f'QA Live stopped; data saved in {folder}',flush=True)
    return folder
