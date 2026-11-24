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

## Automatic loading in the interactive menu

The interactive `main.py` app reads a private `data_store/robrun/profile.json` registration with `{"version":1,"archive":"archives/YOUR_ARCHIVE"}`. It loads the selected history and the latest completed, hash-checked training checkpoint in that archive, including completed manual training runs. The opening menu confirms the loaded result count. Registrations, training data and weights remain local and ignored by Git.

Choose **ROB RUN QA Live** or **Manual Entry** to use that ROB RUN model with any strategy. The model is copied into a separate active profile directory, so auto-training preserves the original training checkpoints. Completed online updates are saved on normal session exit and reused at the next launch. A newly trained archive checkpoint starts a new active copy. Simulation and file-training modes retain their own models.

**Fast Start** is offered in QA Live and Manual Entry when a saved streak contains at least 15 results. It uses the last 15 results of the newest eligible local streak or training archive, regardless of age. This lets the loaded model show a suggestion immediately. New consecutive results gradually replace the saved inputs. Scoring and auto-training use fresh consecutive results only. Choose `n` at the Fast Start prompt for a fresh warmup.

QA Live accepts results decoded after the rolling phase once they pass the usual source, order and receipt-time checks. The 60-second streak boundary applies to consecutive new observations. Missing or corrupt registered data or checkpoints produce a loading error.

## Bundled QA Live collector

Open `python main.py`, choose a strategy, then **[1] ROB RUN QA Live**. The collector uses your running ROB RUN client and its normal connection; open the game in the relevant hub first. A standard Windows consent prompt starts only the capture host. The menu process stays at its original privilege level. Auto-training is off unless you explicitly enable it. Ctrl+C requests cooperative capture cleanup and saves data plus any completed model updates.

The scrolling app displays number, color and streak length. A streak ends after 60 seconds without an accepted result or an explicit source gap. Singletons are archived and withheld from model input. QA Live accepts a single distinct number supported by the recorded readings for the rolling-phase result, including when another reading carries no number. Conflicting numeric readings leave the result unresolved. The collector records rolling-phase observations and does not verify betting open or close times.

QA Live uses the Manual Entry setup and scrolling result and advice screens. Set a starting virtual balance and a suggested stake before launching. After 15 consecutive results, the app shows the selected strategy's suggestion for the next round. Enter `bet 17 100` to log 100 game units you placed on 17, or use a supported group such as `bet red 100` or `bet col 2 100`. You can enter more than one bet per round. An empty entry is recorded as skipped and leaves the virtual balance unchanged. The result screen and session summary calculate virtual payouts from your logged bets. A capture gap or ambiguous result leaves any pending bet unresolved. Bets are placed manually in the game.

Sanitized results, streaks, rankings, manually entered bets and status are saved to `data_store/robrun/live-*/`; bounded raw packet evidence stays in ignored `work/roulette-qa/live-*/`. Each capture host stops when its app owner exits. Capture, model and history are separate processes/files so model fitting does not stop the native packet recorder. The old casino WebSocket is no longer a selectable menu Live Mode.

Requirements: Windows x64, Python 3.12, .NET 10, installed ROB RUN and its own `enet.dll`. `ROBRUN_ENET_DLL` and `ROBRUN_DOTNET` override installed-library/runtime discovery. No game binaries are included. On a fresh checkout, build native helpers with `powershell -File outputs/roulette-qa-investigation/Build-QA.ps1`; set `ROBRUN_DOTNET` to a .NET 10 SDK and `ROBRUN_TRACEEVENT_DIR` to an installed TraceEvent assembly directory if needed. These helpers and all runtime data are ignored by Git.

## Evaluation correction

The original replay path appended the current result before predicting that result and trained on the same full file it evaluated. The patched engine predicts before appending the current target. File evaluation uses a fresh model and a chronological training prefix/held-out tail, with holdout excluded from fitting. Those changes prevent those particular leakage paths; they do not establish that roulette is predictable.

Use a complete independently verified dataset and later unseen rounds for any real claim. Preserve table/session boundaries, repeated outcomes, missing rounds and prediction timestamps. Post-close data for a round is not an eligible pre-close feature for that round.

## Local tests

```powershell
python -m unittest data.test_robrun_feed
python -m unittest discover -s tests
```

The feed fixtures do not require TensorFlow. Engine/evaluation tests can use deterministic fake models to prove temporal isolation. A separate TensorFlow smoke check verifies the installed CPU runtime; synthetic smoke data is not a real roulette performance result.
