# ROB RUN local research feed

This mode consumes a **local recorder file**, records number/color observations and ranks the next unseen observation. It uses the repository's existing 15-number LSTM input. It does not connect to the default Pragmatic Play feed, control ROB RUN, or submit wagers. Other players' stakes and identities are not inputs.

The recorder is a separate application. A path to an existing JSONL journal is required; this repository does not bypass the game client or make roulette available outside its server replication scope.

## Run

Use Python 3.12 in an isolated environment. Keep dependencies, runtime data and models on a drive with sufficient space.

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements-robrun.txt
.\.venv\Scripts\python.exe robrun.py --events 'C:\path\to\realtime-events-v2.jsonl' --driver-state 'C:\path\to\driver-state.json' --allow-candidates
```

`--allow-candidates` explicitly enables **unverified research observations** from the current recorder. Candidate fitting remains disabled unless the separate `--train-candidates` switch is also supplied. These are not confirmed final results or reliable training labels. Ambiguous interpretations are rejected. The current source has missed round transitions; filtered observations must not be treated as a complete random sample of all spins. Results from this mode do not establish predictive skill.

Without that flag, only records with `kind="verified_final_result"`, `final=true`, `fieldIdentityVerified=true` and a valid integer `number` from0 through36 are accepted. Records also need an aware `receivedUtc` timestamp, a consistent round `phaseEndUtc`, and either a `tableId` or an `endpoint` containing an IP address and integer port. Verification flags must come from an independently validated source; setting them on guesses does not verify anything.

Candidate input uses the existing recorder's `unverified_candidate` schema and additionally requires `phase="rolling"` and exactly one interpretation. `--driver-state` starts at that recorder session's ready timestamp; `--since` can set an explicit aware ISO timestamp instead. Historical startup data is replayed chronologically and labeled **REPLAY**, rather than presented as live forecasts.

Useful options:

- `--no-train`: collect and rank without online fitting.
- `--collect-only`: record observations without TensorFlow or model inference.
- `--stop-file PATH`: stop cooperatively when this file is created.
- `--train-candidates`: additionally opt into experimental fitting on unverified labels; requires `--allow-candidates`. This does not verify those labels.
- `--top-n 1`: number of model-ranked pockets. Values1–37 are supported; the chance baseline changes with coverage.
- `--max-seconds 60`: bounded local run; the default waits until Ctrl+C.
- `--output data_store/robrun`: location of private runtime output.
- `--run-id NAME`: fresh run directory. Existing nonempty runs are refused.
- `--seed 42`: model initialization seed for reproducibility; it is not the server's roulette seed.

The first15 contiguous accepted observations are context only. The next observation is scored against a prediction made from prior context; its result enters future context/training only afterward. Online fitting starts only when enough examples exist. Small/noisy histories can overfit and do not demonstrate an edge.

## Data quality and isolation

The feed opens local files only. It handles partial lines, finite journal rotations and bounded memory. Round identity uses source plus phase deadline, so legitimate repeated numbers in different rounds are retained. Source changes, missing cadence, conflicts, ambiguity and source-loss diagnostics clear the input history and pending ranking before another result is delivered.

Each research run has a fresh model directory, and each source has its own model within that run. ROB RUN models are separate from casino/live/manual models. A source change cannot silently append another table's observations to the same input sequence.

The live entry point exports only sanitized observation fields, session-salted source/round identifiers and derived color. It does not write raw player identifiers, names, server addresses or packet bodies. It writes:

- `status.json`: process/data/model status, verification mode and counters.
- `observations.jsonl`: accepted/rejected/duplicate/reset metadata and verification quality.
- `feed.jsonl`: sanitized accepted observations and explicit reset barriers for a separate local consumer.
- `histories/part_*.txt`: number-only histories split at reset barriers; comments identify their research provenance.
- `rankings.jsonl`: model scores and generation timestamps. Scores are uncalibrated, not established win probabilities.
- `scores.jsonl`: chronological ranking hits, with replay/live labeling and zero wager amount. A score is labeled live only when its ranking was generated before the target's recorded receipt time and the target is fresh. This is a receipt-time check, not independent proof of a server betting-close cutoff.
- `models/<source>/`: fitted checkpoints, only after actual fitting.

Every fresh run contains a deny-all `.gitignore`, including custom checkout-local `--output` locations. The default `data_store/` is also ignored. Keep output local; publishing this fork does not publish collected data or model files. Existing recorder packet captures are separate raw evidence and are not anonymized by this feed.

## Continuous streak collection

Run `python streaks.py --events data_store/robrun/RUN/feed.jsonl --output data_store/robrun/RUN/streaks --timeout 60` alongside the collector. The output directory must be fresh. This follows the collector's append-only sanitized feed; it does not read the rotating packet journal directly.

A streak ends after 60 seconds without a new accepted result, or at an explicit source/reset barrier. Ordinary recorder activity does not extend it. A result arriving exactly 60 seconds later starts another streak. Repeated numbers in separate rounds remain separate entries. Streaks describe contiguous result sequences, not runs of one color.

`status.json` lists every streak's length and whether it is open. `streak_*.jsonl` retains raw accepted observations and colors, including singletons. **Singleton streaks never enter model input.** On the second result within the timeout, both initial results are released to the streak output's `feed.jsonl`; subsequent results are passed through. Only eligible streaks receive number-only `streak_*.txt` files. Feed that output `feed.jsonl` to `robrun.py --allow-candidates --no-train` for a separate local model process. Boundaries are explicit and each sequence needs sufficient history; fragmented data cannot be treated as one uninterrupted training sequence. Buffered initial entries are historical observations when released.

## Evaluation correction

The original replay path appended the current result before predicting that result and trained on the same full file it evaluated. The patched engine predicts before appending the current target. File evaluation uses a fresh model and a chronological training prefix/held-out tail, with holdout excluded from fitting. Those changes prevent those particular leakage paths; they do not establish that roulette is predictable.

Use a complete independently verified dataset and later unseen rounds for any real claim. Preserve table/session boundaries, repeated outcomes, missing rounds and prediction timestamps. Post-close data for a round is not an eligible pre-close feature for that round.

## Local tests

```powershell
python -m unittest data.test_robrun_feed
python -m unittest discover -s tests
```

The feed fixtures do not require TensorFlow. Engine/evaluation tests can use deterministic fake models to prove temporal isolation. A separate TensorFlow smoke check verifies the installed CPU runtime; synthetic smoke data is not a real roulette performance result.
