import datetime as dt
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from streaks import StreakRecorder, stamp


def result(second, number=0, source="fixture"):
    return {"kind": "unverified_candidate", "number": number, "tableId": source,
            "receivedUtc": (stamp("2026-01-01T00:00:00Z") + dt.timedelta(seconds=second)).isoformat(),
            "phaseEndUtc": None, "phase": "rolling", "alternativesInMessage": 1}


class StreakTests(unittest.TestCase):
    def test_busy_status_reader_does_not_stop_result_collection(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = StreakRecorder(Path(temporary))
            original = Path.replace
            attempts = []
            def busy(path, target):
                attempts.append(1)
                if len(attempts) <= 5:
                    raise PermissionError("fixture: reader holds status open")
                return original(path, target)
            with patch.object(Path, "replace", busy), patch("streaks.time.sleep"):
                recorder.accept(result(0, 14))
                recorder.accept(result(43, 18))
            self.assertEqual(recorder.active["length"], 2)
            self.assertTrue((Path(temporary) / "status.json").exists())
            self.assertEqual((Path(temporary) / "streak_00001.txt").read_text().splitlines(), ["14", "18"])

    def test_singletons_excluded_and_exact_timeout_splits(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = StreakRecorder(Path(temporary))
            recorder.accept(result(0, 14))
            recorder.accept(result(60, 2))
            self.assertFalse((Path(temporary) / "feed.jsonl").exists())
            recorder.accept(result(103, 2))
            rows = [json.loads(line) for line in (Path(temporary) / "feed.jsonl").read_text().splitlines()]
            self.assertEqual([row["number"] for row in rows if "number" in row], [2, 2])
            self.assertEqual([s["length"] for s in recorder.streaks], [1, 2])
            recorder.expire(stamp(result(163)["receivedUtc"]))
            self.assertTrue(recorder.streaks[-1]["closed"])
            self.assertEqual((Path(temporary) / "streak_00002.txt").read_text().splitlines(), ["2", "2"])
            self.assertFalse((Path(temporary) / "streak_00001.txt").exists())

    def test_activity_cannot_extend_timer_and_gaps_keep_boundaries(self):
        with tempfile.TemporaryDirectory() as temporary:
            recorder = StreakRecorder(Path(temporary))
            recorder.accept(result(0))
            recorder.accept(result(43, 18))
            recorder.expire(stamp(result(102)["receivedUtc"]))
            self.assertIsNotNone(recorder.active)
            recorder.expire(stamp(result(103)["receivedUtc"]))
            self.assertIsNone(recorder.active)
            recorder.accept(result(110, 1))
            recorder.accept({"kind": "journal_gap"})
            recorder.accept(result(120, 3))
            recorder.accept(result(140, 5, "other"))
            self.assertEqual([s["length"] for s in recorder.streaks], [2, 1, 1, 1])
            self.assertEqual(recorder.streaks[1]["close_reason"], "source_gap")


if __name__ == "__main__":
    unittest.main()
