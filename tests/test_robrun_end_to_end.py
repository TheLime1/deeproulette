"""Exercise the actual local runner with a deterministic model double."""
import datetime as dt
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest


def child(source, output):
    import asyncio
    import types
    import numpy as np
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    tf = types.ModuleType("tensorflow")
    tf.keras = types.SimpleNamespace(utils=types.SimpleNamespace(set_random_seed=lambda _: None))
    sys.modules["tensorflow"] = tf

    class Model:
        def predict(self, inputs, verbose=0):
            assert len(inputs) == 15
            return np.ones((1, 37), dtype=float) / 37

    class Network:
        def __init__(self, model_name, save_dir):
            self.model_path = str(Path(save_dir) / (model_name + ".keras"))
            self.model = Model()
        def load(self): return self.model
        def is_trained(self): return False
        def save(self): raise AssertionError("An unfitted no-train model must not be saved")
        @staticmethod
        def build_prediction_input(history): return list(history[-15:])

    module = types.ModuleType("models.neural_network")
    module.RouletteNeuralNetwork = Network
    sys.modules["models.neural_network"] = module
    from robrun import parse_args, run
    args = parse_args(["--events", source, "--output", output, "--run-id", "fixture",
                       "--no-train", "--max-seconds", "0.3", "--poll-interval", "0.01"])
    asyncio.run(run(args))


class RunnerIntegrationTests(unittest.TestCase):
    def test_local_capture_replay_has_no_wagers_training_or_false_live_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source.jsonl"
            start = dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc)
            events = []
            for number in range(20):
                observed = start + dt.timedelta(seconds=43 * number)
                events.append({"kind": "verified_final_result", "number": number,
                    "final": True, "fieldIdentityVerified": True, "tableId": "synthetic-fixture",
                    "receivedUtc": observed.isoformat(), "phaseEndUtc": (observed + dt.timedelta(seconds=15)).isoformat()})
            source.write_text("".join(json.dumps(event) + "\n" for event in events))
            result = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--child", str(source), str(root / "out")],
                                    cwd=str(Path(__file__).resolve().parents[1]), capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            run_dir = root / "out/fixture"
            status = json.loads((run_dir / "status.json").read_text())
            scores = [json.loads(line) for line in (run_dir / "scores.jsonl").read_text().splitlines()]
            self.assertEqual(status["state"], "stopped")
            self.assertEqual(status["training_updates"], 0)
            self.assertEqual(status["feed_counts"]["accepted_verified"], 20)
            self.assertEqual([row["actual"] for row in scores], list(range(15, 20)))
            self.assertTrue(all(row["wager_amount"] == 0 and row["evaluation_mode"] == "replay" for row in scores))
            self.assertEqual((run_dir / ".gitignore").read_text(), "*\n")
            self.assertNotIn("synthetic-fixture", (run_dir / "observations.jsonl").read_text())


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--child":
        child(sys.argv[2], sys.argv[3])
    else:
        unittest.main()
