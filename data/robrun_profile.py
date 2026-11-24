"""Load a private local ROB RUN training archive for the interactive app."""
from dataclasses import dataclass, field
import datetime as dt
import hashlib
import json
from pathlib import Path
import shutil


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
