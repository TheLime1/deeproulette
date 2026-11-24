"""Local ROB RUN research entry point. No casino connection or wager submission."""
from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import inspect
import json
import math
import os
from pathlib import Path
import signal
import sys
import time
import uuid

# Configure the CPU runtime before importing TensorFlow.
os.environ.setdefault("TF_CPP_MIN_LOG_LEVEL", "2")
os.environ.setdefault("CUDA_VISIBLE_DEVICES", "")
os.environ.setdefault("TF_NUM_INTRAOP_THREADS", "2")
os.environ.setdefault("TF_NUM_INTEROP_THREADS", "1")


def utcnow():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def score_mode(observed_at, forecast_generated_at, now=None):
    """A recent replay target is not live if the ranking was generated later."""
    if forecast_generated_at is None:
        return "replay"
    observed = dt.datetime.fromisoformat(observed_at.replace("Z", "+00:00"))
    generated = dt.datetime.fromisoformat(forecast_generated_at.replace("Z", "+00:00"))
    current = now or dt.datetime.now(dt.timezone.utc)
    age = (current - observed).total_seconds()
    return "live_observation" if generated < observed and 0 <= age <= 60 else "replay"


class RunJournal:
    def __init__(self, folder: Path):
        self.folder = folder
        folder.mkdir(parents=True, exist_ok=True)
        # Protect custom checkout-local output paths as well as data_store/.
        (folder / ".gitignore").write_text("*\n", encoding="utf-8")
        self.status = {"started_utc": utcnow(), "state": "starting", "pid": os.getpid(),
                       "wagers_submitted": 0, "source": "local_file"}
        self.part = 1

    def append(self, name, row):
        with (self.folder / name).open("a", encoding="utf-8") as output:
            output.write(json.dumps(row, allow_nan=False) + "\n")

    def publish(self):
        temporary = self.folder / "status.json.tmp"
        temporary.write_text(json.dumps(self.status, indent=2, allow_nan=False), encoding="utf-8")
        for attempt in range(3):
            try:
                temporary.replace(self.folder / "status.json")
                return
            except PermissionError:
                if attempt < 2:
                    time.sleep(0.005)

    def observation(self, row):
        # The feed callback is an allowlisted, identity-free observation record.
        export = dict(row)
        if type(row.get("number")) is int:
            red = {1,3,5,7,9,12,14,16,18,19,21,23,25,27,30,32,34,36}
            export["color"] = "green" if row["number"] == 0 else "red" if row["number"] in red else "black"
        self.append("observations.jsonl", export)
        if row["status"] == "reset":
            self.part += 1
        if row["status"] == "accepted":
            folder = self.folder / "histories"
            folder.mkdir(exist_ok=True)
            path = folder / f"part_{self.part:05d}.txt"
            if not path.exists():
                path.write_text("# ROB RUN observations; check observations.jsonl for verification quality.\n", encoding="utf-8")
            with path.open("a", encoding="utf-8") as output:
                output.write(f"{row['number']}\n")
            # A sanitized local feed for a separately running model process.
            self.append("feed.jsonl", {"kind": row["quality"], "number": row["number"],
                "tableId": row["source_id"], "receivedUtc": row["observed_at"],
                "phaseEndUtc": row["phaseEndUtc"], "phase": row["phase"],
                "alternativesInMessage": 1, "final": row["quality"] == "verified_final_result",
                "fieldIdentityVerified": row["quality"] == "verified_final_result"})
        elif row["status"] == "reset":
            self.append("feed.jsonl", {"kind": "journal_gap"})


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--events", type=Path, required=True,
                        help="Local recorder JSONL file; rotated .1/.2/.3 siblings are supported")
    parser.add_argument("--driver-state", type=Path,
                        help="Start at this recorder's ready UTC timestamp, unless --since is given")
    parser.add_argument("--since", help="Earliest observation timestamp (ISO 8601 with timezone)")
    parser.add_argument("--collect-only", action="store_true", help="Record a local feed without importing TensorFlow")
    parser.add_argument("--stop-file", type=Path, help="Stop after this local signal file is created")
    parser.add_argument("--allow-candidates", action="store_true",
                        help="Research only: allow unambiguous but unverified recorder candidates")
    parser.add_argument("--train-candidates", action="store_true",
                        help="Explicitly fit experimental weights on unverified candidate labels")
    parser.add_argument("--output", type=Path, default=Path("data_store/robrun"))
    parser.add_argument("--run-id", help="Fresh run directory name; existing nonempty runs are refused")
    parser.add_argument("--poll-interval", type=float, default=0.1)
    parser.add_argument("--max-lateness-seconds", type=float, default=60,
                        help="Maximum observation timestamp delay beyond its phase deadline")
    parser.add_argument("--top-n", type=int, default=1)
    parser.add_argument("--no-train", action="store_true", help="Collect and rank without online fitting")
    parser.add_argument("--seed", type=int, default=42, help="Model initialization seed, not the game RNG seed")
    parser.add_argument("--max-seconds", type=float, default=0, help="Zero runs until Ctrl+C")
    args = parser.parse_args(argv)
    if not args.events.is_file():
        parser.error("--events must be an existing local recorder file")
    if not 1 <= args.top_n <= 37:
        parser.error("--top-n must be between 1 and 37")
    if not 0 <= args.seed <= 2**32 - 1:
        parser.error("--seed must be between 0 and 4294967295")
    if (not all(math.isfinite(n) for n in (args.max_seconds,args.poll_interval,args.max_lateness_seconds))
            or args.max_seconds < 0 or not .001 <= args.poll_interval <= 10 or not 0 <= args.max_lateness_seconds <= 3600):
        parser.error("Invalid duration/polling/lateness setting")
    if args.run_id and (Path(args.run_id).name != args.run_id or args.run_id in (".", "..")):
        parser.error("--run-id must be a single fresh directory name")
    if args.train_candidates and (not args.allow_candidates or args.no_train):
        parser.error("--train-candidates requires --allow-candidates and cannot be combined with --no-train")
    return args


def since_timestamp(args):
    value = args.since
    if value is None and args.driver_state:
        state = json.loads(args.driver_state.read_text(encoding="utf-8-sig"))
        value = state.get("ready", {}).get("utc")
        if value is None:
            raise ValueError("Driver state has no ready timestamp; use an explicit --since")
    if value is None:
        return None
    stamp = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    if stamp.tzinfo is None or stamp.utcoffset() is None:
        raise ValueError("--since must include a timezone")
    return stamp.astimezone(dt.timezone.utc)


def observation_after(row, cutoff):
    return cutoff is None or row.get("observed_at") is None or dt.datetime.fromisoformat(row["observed_at"]) >= cutoff


async def stop_when_requested(feed, args):
    started = time.monotonic()
    while feed.running:
        if args.stop_file and args.stop_file.is_file():
            feed.stop()
            return
        if args.max_seconds and time.monotonic() - started >= args.max_seconds:
            feed.stop()
            return
        await asyncio.sleep(0.2)


async def collect(args):
    from data.robrun_feed import RobrunFeed
    run_id = args.run_id or ("collection-" + dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    folder = args.output.resolve() / run_id
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("Run directory already contains data; choose a fresh --run-id")
    cutoff = since_timestamp(args)
    journal = RunJournal(folder)
    journal.status.update({"state": "collecting", "mode": "collection_only", "training_updates": 0,
                           "since_utc": cutoff.isoformat() if cutoff else None,
                           "quality_mode": "unverified_candidate_research" if args.allow_candidates else "verified_results_only"})
    journal.publish()
    feed = RobrunFeed(args.events, allow_candidates=args.allow_candidates, since=cutoff,
                      poll_interval=args.poll_interval, max_lateness_seconds=args.max_lateness_seconds)

    def observation(row):
        if not observation_after(row, cutoff):
            return
        journal.observation(row)
        if row["status"] == "accepted":
            journal.status.update(last_number=row["number"], last_observation_utc=row["observed_at"], last_quality=row["quality"])
            print(f"[{utcnow()}] RECORDED {row['number']} | {row['quality']}", flush=True)

    async def heartbeat():
        while feed.running:
            journal.status.update(updated_utc=utcnow(), feed_counts=dict(feed.counters))
            journal.publish()
            await asyncio.sleep(1)

    feed.on_observation(observation)
    tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(stop_when_requested(feed, args))]
    print(f"ROB RUN local collection active: {folder}\nSince: {cutoff}\nNo model fitting or wagering.", flush=True)
    try:
        await feed.listen()
        journal.status["state"] = "stopped"
    except BaseException as error:
        journal.status["state"] = "stopped" if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        journal.status["error_type"] = type(error).__name__
        raise
    finally:
        feed.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        journal.status.update(updated_utc=utcnow(), feed_counts=dict(feed.counters))
        journal.publish()


async def run(args):
    from data.robrun_feed import RobrunFeed
    import numpy as np
    import tensorflow as tf
    from config.settings import SEQUENCE_LENGTH, ONLINE_HISTORY_WINDOW
    from core.engine import PredictionEngine
    from core.trainer import ModelTrainer
    from models.neural_network import RouletteNeuralNetwork
    from strategies.base import BaseStrategy

    tf.keras.utils.set_random_seed(args.seed)

    class NoWagerRanking(BaseStrategy):
        STRATEGY_NAME = "ROB RUN research ranking (no wagers)"
        MODEL_LABEL = "robrun_research"

        def select_numbers(self, probabilities):
            return [int(n) for n in np.argsort(probabilities)[-args.top_n:][::-1]]

        def calculate_bets(self, numbers, balance, bet_per_number=None):
            return {int(n): 0.0 for n in numbers}

    run_id = args.run_id or (dt.datetime.now(dt.timezone.utc).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8])
    folder = args.output.resolve() / run_id
    if folder.exists() and any(folder.iterdir()):
        raise ValueError("Run directory already contains data; choose a fresh --run-id")
    journal = RunJournal(folder)
    training_enabled = not args.no_train and (not args.allow_candidates or args.train_candidates)
    journal.status.update({"quality_mode": "unverified_candidate_research" if args.allow_candidates else "verified_results_only",
                           "model_seed": args.seed, "online_training_enabled": training_enabled,
                           "candidate_training_opt_in": args.train_candidates,
                           "top_n": args.top_n, "training_updates": 0, "rankings_scored": 0})
    journal.publish()

    since = since_timestamp(args)
    feed = RobrunFeed(args.events, allow_candidates=args.allow_candidates, since=since,
                      poll_interval=args.poll_interval, max_lateness_seconds=args.max_lateness_seconds)
    engine = PredictionEngine(strategy=NoWagerRanking(), initial_balance=1.0,
                              bet_per_number=0.0, auto_train=training_enabled,
                              use_live=True, data_feed=feed, model_dir=str(folder / "models" / "initial"))
    current = {"source": None, "observation": None, "forecast_generated_utc": None}

    class RecordedTrainer(ModelTrainer):
        updates_completed = 0

        def train_online(self, history):
            enough = len(history[-ONLINE_HISTORY_WINDOW:]) >= SEQUENCE_LENGTH + 5
            saved = super().train_online(history)
            if enough:
                self.updates_completed += 1
                journal.status["training_updates"] += 1
                journal.status["source_training_updates"] = self.updates_completed
                # Save the first fitted checkpoint too; do not call an unfitted
                # random model a learned model merely because its directory exists.
                if not Path(self.nn.model_path).exists():
                    self.nn.save()
                    saved = True
            return saved

    async def observation(row):
        if not observation_after(row, since):
            return
        journal.observation(row)
        if row["status"] == "accepted":
            source = row["source_id"]
            if source != current["source"]:
                # Preserve every completed outgoing fit, not just the trainer's
                # periodic checkpoint, before changing the active source model.
                if getattr(engine.trainer, "updates_completed", 0):
                    engine.nn.save()
                result = engine.reset_history("model_source_changed")
                if inspect.isawaitable(result):
                    await result
                model_dir = folder / "models" / source
                engine.nn = RouletteNeuralNetwork(model_name="robrun_research_model", save_dir=str(model_dir))
                engine.nn.load()
                engine.trainer = RecordedTrainer(engine.nn, verbose=False, save_dir=str(model_dir))
                journal.status["source_training_updates"] = 0
                current["forecast_generated_utc"] = None
                current["source"] = source
            current["observation"] = row
            journal.status.update({"last_observation_utc": row["observed_at"], "last_number": row["number"],
                                   "last_quality": row["quality"], "source_id": source})
            print(f"[{utcnow()}] OBSERVED {row['number']:2d} | {row['quality']} | contiguous history {len(engine.history)+1}/{SEQUENCE_LENGTH}", flush=True)
        elif row["status"] == "reset":
            print(f"[{utcnow()}] HISTORY RESET: {row['reason']}", flush=True)

    def scored(result):
        source = current["observation"] or {}
        mode = score_mode(source["observed_at"], current["forecast_generated_utc"])
        row = {"scored_utc": utcnow(), "observed_at": source.get("observed_at"),
               "round_id": source.get("round_id"), "quality": source.get("quality"),
               "actual": result["number"], "ranking": result["predicted"],
               "ranking_hit": bool(result["is_win"]), "wager_amount": 0,
               "source_id": current["source"], "evaluation_mode": mode,
               "forecast_generated_utc": current["forecast_generated_utc"]}
        journal.append("scores.jsonl", row)
        journal.status["rankings_scored"] += 1
        print(f"[{utcnow()}] {mode.upper()}: result {result['number']}; prior ranking {result['predicted']}; hit={result['is_win']}", flush=True)

    def advice(result):
        probabilities = result["probabilities"]
        ranking = [int(n) for n in result["predicted"]]
        generated = utcnow()
        current["forecast_generated_utc"] = generated
        journal.append("rankings.jsonl", {"generated_utc": generated, "after_round_id": (current["observation"] or {}).get("round_id"),
                       "source_id": current["source"], "ranking": ranking,
                       "model_scores": [float(probabilities[n]) for n in ranking],
                       "trained_checkpoint_exists": engine.nn.is_trained(), "wager_amount": 0})
        fitted = "fitted, unvalidated" if engine.nn.is_trained() else "UNTRAINED"
        print(f"[{utcnow()}] NEXT OBSERVATION ranking: {ranking} | {fitted} | uncalibrated model scores | no wagers", flush=True)

    feed.on_observation(observation)
    engine.on_spin_complete = scored
    engine.on_advice = advice
    engine.on_model_status = lambda text: print(text, flush=True)
    engine.on_waiting = lambda count, needed: None

    async def heartbeat():
        while True:
            journal.status.update({"updated_utc": utcnow(), "state": "running",
                                   "contiguous_history": len(engine.history), "history_needed": SEQUENCE_LENGTH,
                                   "feed_counts": dict(feed.counters)})
            journal.publish()
            await asyncio.sleep(2)

    print("ROB RUN / DeepRoulette — LOCAL RESEARCH FEED", flush=True)
    print("Unverified candidates are experimental inputs; incomplete records are not verified spin labels." if args.allow_candidates
          else "Only explicitly verified final results are accepted.", flush=True)
    print("Histories reset on gaps. Historical startup replay is not a live forecast. No casino connection or bets.", flush=True)
    if args.allow_candidates and not training_enabled:
        print("Candidate training is disabled; collecting research observations and displaying an untrained baseline.", flush=True)
    print(f"Run data: {folder}", flush=True)
    tasks = [asyncio.create_task(heartbeat()), asyncio.create_task(stop_when_requested(feed, args))]
    try:
        await engine.run()
        journal.status["state"] = "stopped"
    except BaseException as error:
        journal.status["state"] = "stopped" if isinstance(error, (KeyboardInterrupt, asyncio.CancelledError)) else "failed"
        journal.status["error_type"] = type(error).__name__
        raise
    finally:
        feed.stop()
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if getattr(engine.trainer, "updates_completed", 0):
            try:
                engine.nn.save()
            except Exception as error:
                journal.status["checkpoint_error_type"] = type(error).__name__
                journal.status["state"] = "failed"
        journal.status["feed_counts"] = dict(feed.counters)
        journal.status["contiguous_history"] = len(engine.history)
        journal.status["updated_utc"] = utcnow()
        journal.publish()


def main(argv=None):
    args = parse_args(argv)
    try:
        asyncio.run(collect(args) if args.collect_only else run(args))
    except KeyboardInterrupt:
        print("ROB RUN research session stopped.", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
