"""Profile discovery, checkpoint integrity and result-gap boundaries."""
import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest

from data.robrun_profile import load_robrun_profile, sha256


class ProfileTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.private = self.root / 'data_store/robrun'
        self.archive = self.private / 'archives/fixture'
        self.archive.mkdir(parents=True)
        self.numbers = list(range(26))
        selected = self.archive / 'selected-26.txt'
        selected.write_text('\n'.join(map(str, self.numbers)) + '\n')
        self.write(self.private / 'profile.json', {'version': 1, 'archive': 'archives/fixture'})
        self.write(self.archive / 'manifest.json', {'selected_numbers': self.numbers, 'selected_count': 26,
            'selected_end_utc': '2026-01-01T00:00:00Z', 'quality': 'unverified_candidate',
            'hashes': {'selected-26.txt': sha256(selected)}})
        self.make_report(self.archive, '2026-01-01T00:01:00Z', b'original fixture weights')

    def tearDown(self):
        self.temp.cleanup()

    def write(self, path, value):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value), encoding='utf-8')

    def make_report(self, folder, completed, data):
        folder.mkdir(parents=True, exist_ok=True)
        model = folder / 'model.keras'
        model.write_bytes(data)
        report = {'state': 'complete', 'training_results': 26, 'checkpoint_reload_passed': True,
                  'completed_utc': completed, 'model_path': str(model), 'model_sha256': sha256(model)}
        self.write(folder / 'training-report.json', report)
        return model

    def test_loads_only_registered_history_and_latest_completed_checkpoint(self):
        newest = self.make_report(self.archive / 'manual-runs/new', '2026-01-01T00:02:00Z', b'manual fixture weights')
        self.write(self.archive / 'manual-runs/pending/training-report.json', {'state': 'training'})
        partial = self.archive / 'manual-runs/partial/training-report.json'
        partial.parent.mkdir(parents=True)
        partial.write_text('{"state":')
        profile = load_robrun_profile(self.root)
        self.assertEqual(profile.history, self.numbers)
        self.assertEqual(profile.source_checkpoint, newest)
        self.assertTrue(profile.active_dir.is_relative_to(self.private))

    def test_old_training_history_is_not_treated_as_current_context(self):
        profile = load_robrun_profile(self.root)
        base = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
        self.assertEqual(profile.recent_context(base + dt.timedelta(seconds=59)), self.numbers)
        self.assertEqual(profile.recent_context(base + dt.timedelta(seconds=60)), [])
        self.assertEqual(profile.recent_context(base - dt.timedelta(seconds=1)), [])
        self.assertEqual(profile.history, self.numbers)

    def test_changed_training_data_and_changed_model_fail_clearly(self):
        path = self.archive / 'selected-26.txt'
        original = path.read_bytes()
        path.write_text('0\n')
        with self.assertRaisesRegex(ValueError, 'history differs'):
            load_robrun_profile(self.root)
        path.write_bytes(original)
        (self.archive / 'model.keras').write_bytes(b'changed')
        with self.assertRaisesRegex(ValueError, 'checkpoint differs'):
            load_robrun_profile(self.root)

    def test_profile_cannot_load_a_checkpoint_outside_its_archive(self):
        report_path = self.archive / 'training-report.json'
        report = json.loads(report_path.read_text())
        external = self.root / 'elsewhere.keras'
        external.write_bytes(b'outside')
        report.update(model_path=str(external), model_sha256=sha256(external))
        self.write(report_path, report)
        with self.assertRaisesRegex(ValueError, 'must stay inside'):
            load_robrun_profile(self.root)

    def test_absent_profile_does_not_change_other_installations(self):
        (self.private / 'profile.json').unlink()
        self.assertIsNone(load_robrun_profile(self.root))


if __name__ == '__main__':
    unittest.main()
