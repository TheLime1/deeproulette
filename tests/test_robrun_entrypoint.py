"""Standard-library checks for local collection output; no model or network."""
import contextlib
import datetime as dt
import io
import json
from pathlib import Path
import tempfile
import unittest

from robrun import RunJournal, parse_args, score_mode


class LocalEntryPointTests(unittest.TestCase):
    def test_recent_historical_target_is_not_labeled_live(self):
        now = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.timezone.utc)
        self.assertEqual(score_mode("2026-01-01T11:59:45Z", "2026-01-01T11:59:50Z", now), "replay")
        self.assertEqual(score_mode("2026-01-01T11:59:45Z", "2026-01-01T11:59:30Z", now), "live_observation")
        self.assertEqual(score_mode("2026-01-01T11:59:45Z", None, now), "replay")

    def test_histories_preserve_repeats_zero_and_reset_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            journal = RunJournal(root)
            def row(status, number=None):
                return {"status": status, "number": number, "quality": "unverified_candidate",
                        "source_id": "fixture", "round_id": "fixture", "reason": None,
                        "observed_at": None, "phaseEndUtc": None, "phase": "rolling"}
            journal.observation(row("accepted", 0))
            journal.observation(row("accepted", 0))
            journal.observation(row("reset"))
            journal.observation(row("accepted", 14))
            first = (root / "histories/part_00001.txt").read_text().splitlines()
            second = (root / "histories/part_00002.txt").read_text().splitlines()
            self.assertEqual(first[1:], ["0", "0"])
            self.assertEqual(second[1:], ["14"])
            records = [json.loads(line) for line in (root / "observations.jsonl").read_text().splitlines()]
            self.assertEqual(records[0]["color"], "green")
            self.assertEqual(records[-1]["color"], "red")

    def test_status_is_valid_json_and_reports_no_wagers(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal = RunJournal(Path(temporary))
            journal.publish()
            status = json.loads((Path(temporary) / "status.json").read_text())
            self.assertEqual(status["wagers_submitted"], 0)
            self.assertEqual(status["source"], "local_file")

    def test_cli_rejects_nonfinite_settings_and_path_run_names(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "events.jsonl"
            source.write_text("")
            for args in (["--max-seconds", "nan"], ["--poll-interval", "inf"],
                         ["--run-id", "../escape"], ["--top-n", "38"],
                         ["--train-candidates"], ["--allow-candidates", "--no-train", "--train-candidates"]):
                with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        parse_args(["--events", str(source), *args])


if __name__ == "__main__":
    unittest.main()
