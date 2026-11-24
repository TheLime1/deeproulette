"""Load a private local ROB RUN training archive for the interactive app."""
from collections import deque
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil

from config.settings import SEQUENCE_LENGTH


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def inside(path, parent):
    path = path.resolve()
    if not path.is_relative_to(parent.resolve()):
        raise ValueError('ROB RUN profile path must stay inside its private data directory')
    return path


def timestamp(value):
    result = dt.datetime.fromisoformat(value.replace('Z', '+00:00'))
    if result.utcoffset() is None:
        raise ValueError('ROB RUN timestamp must include a timezone')
    return result


@dataclass(frozen=True)
class SavedContext:
    numbers: tuple[int, ...]
    last_result_utc: str
    source: str


def find_saved_context(root, profile=None):
    """Select the newest complete sequence, independently of its age or model weights."""
    private = Path(root) / 'data_store/robrun'
    choices = []
    if profile and len(profile.history) >= SEQUENCE_LENGTH:
        choices.append((timestamp(profile.last_result_utc), None, profile.history[-SEQUENCE_LENGTH:],
                        profile.last_result_utc, 'training archive'))
    for manifest in private.glob('*/streaks-60s/status.json'):
        try:
            streaks = read_json(manifest)['streaks']
        except (OSError, ValueError, KeyError, TypeError):
            continue
        if not isinstance(streaks, list):
            continue
        for streak in streaks:
            try:
                length, identifier = streak['length'], streak['id']
                if type(length) is not int or length < SEQUENCE_LENGTH or type(identifier) is not int or identifier < 1:
                    continue
                last_result = streak['last_result_utc']
                choices.append((timestamp(last_result), manifest.parent / f'streak_{identifier:05d}.txt', length,
                                last_result, f'{manifest.parents[1].name}, streak {identifier}'))
            except (ValueError, KeyError, TypeError, AttributeError):
                continue

    for _, path, values, last_result, source in sorted(choices, key=lambda choice: choice[0], reverse=True):
        if path is None:
            return SavedContext(tuple(values), last_result, source)
        try:
            with path.open(encoding='utf-8') as stream:
                # The manifest can be read while the writer is appending the next round.
                numbers = deque(maxlen=SEQUENCE_LENGTH)
                for _ in range(values):
                    number = int(next(stream))
                    if not 0 <= number <= 36:
                        raise ValueError('Invalid stored roulette number')
                    numbers.append(number)
        except (OSError, ValueError, StopIteration):
            continue
        return SavedContext(tuple(numbers), last_result, source)
    return None


@dataclass
class RobrunProfile:
    archive: Path
    history: list
    last_result_utc: str
    source_checkpoint: Path
    source_sha256: str
    active_dir: Path
    quality: str
    network: object = field(default=None, repr=False)

    def recent_context(self, now=None):
        """Old training data is loaded, but cannot bridge a 60-second result gap."""
        age = ((now or dt.datetime.now(dt.timezone.utc)) - timestamp(self.last_result_utc)).total_seconds()
        return list(self.history) if 0 <= age < 60 else []

    def load_network(self):
        from models.neural_network import RouletteNeuralNetwork
        self.active_dir.mkdir(parents=True, exist_ok=True)
        checkpoint = self.active_dir / 'robrun_profile_model.keras'
        if not checkpoint.exists():
            temporary = checkpoint.with_suffix('.keras.tmp')
            shutil.copy2(self.source_checkpoint, temporary)
            if sha256(temporary) != self.source_sha256:
                raise ValueError('ROB RUN checkpoint changed while loading')
            temporary.replace(checkpoint)
        network = RouletteNeuralNetwork(model_name='robrun_profile_model', save_dir=str(self.active_dir))
        network.load()
        self.network = network
        return network


def load_robrun_profile(root):
    """No profile means the ordinary upstream menu; invalid profiles fail clearly."""
    private = Path(root).resolve() / 'data_store/robrun'
    settings = private / 'profile.json'
    if not settings.exists():
        return None
    registration = read_json(settings)
    if registration.get('version') != 1:
        raise ValueError('Unsupported ROB RUN profile version')
    archive = inside(private / registration['archive'], private)
    manifest = read_json(archive / 'manifest.json')
    selected = archive / 'selected-26.txt'
    if sha256(selected) != manifest['hashes']['selected-26.txt']:
        raise ValueError('ROB RUN selected history differs from its archived hash')
    history = [int(line) for line in selected.read_text(encoding='utf-8').splitlines()]
    if (history != manifest['selected_numbers'] or len(history) != manifest['selected_count']
            or not history or any(not 0 <= n <= 36 for n in history)):
        raise ValueError('Invalid ROB RUN selected history')
    timestamp(manifest['selected_end_utc'])
    reports = [archive / 'training-report.json', *archive.glob('manual-runs/*/training-report.json')]
    completed = []
    for path in reports:
        try:
            completed.append(read_json(path))
        except (OSError, json.JSONDecodeError):
            # An unfinished training job may be writing its report right now.
            continue
    completed = [row for row in completed if row.get('state') == 'complete'
                 and row.get('training_results') == len(history) and row.get('checkpoint_reload_passed') is True]
    if not completed:
        raise ValueError('ROB RUN archive has no completed, reload-checked checkpoint')
    report = max(completed, key=lambda row: timestamp(row['completed_utc']))
    checkpoint = inside(Path(report['model_path']), archive)
    digest = sha256(checkpoint)
    if digest != report['model_sha256']:
        raise ValueError('ROB RUN checkpoint differs from its training report')
    active = private / 'active' / archive.name / digest
    return RobrunProfile(archive=archive, history=history, last_result_utc=manifest['selected_end_utc'],
                         source_checkpoint=checkpoint, source_sha256=digest, active_dir=active,
                         quality=manifest['quality'])
