"""Causality regressions using deterministic fakes, no TensorFlow or network."""
import asyncio
import importlib.util
import logging
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


class FakeModel:
    def __init__(self):
        self.inputs = []

    def predict(self, context, verbose=0):
        self.inputs.append(tuple(context))
        probabilities = [0.0] * 37
        probabilities[(context[-1] + 1) % 37] = 1.0
        return [probabilities]


class FakeNN:
    instances = []

    def __init__(self, model_name="fake", save_dir=None):
        self.model_path = os.path.join(os.fspath(save_dir), model_name + ".keras")
        self.save_dir = save_dir
        self.model = None
        self.loads = 0
        self.builds = 0
        self.instances.append(self)

    def build(self):
        self.builds += 1
        return FakeModel()

    def load(self):
        self.loads += 1
        self.model = self.build()

    def is_trained(self):
        return False

    @staticmethod
    def build_prediction_input(history):
        return tuple(history[-15:])


class FakeTrainer:
    instances = []
    offline_success = True

    def __init__(self, nn, verbose=False, save_dir=None):
        self.nn = nn
        self.offline_inputs = []
        self.online_inputs = []
        self.instances.append(self)

    def train_offline(self, history):
        self.offline_inputs.append(tuple(history))
        return self.offline_success

    def train_online(self, history):
        self.online_inputs.append((tuple(history), self.nn.model.inputs[-1]))
        return False


class FakeStrategy:
    model_filename = "fake_model"
    STRATEGY_NAME = "Causality fixture"

    def select_numbers(self, probabilities):
        return [max(range(37), key=probabilities.__getitem__)]

    def calculate_bets(self, numbers, balance, bet_per_number):
        return {number: bet_per_number for number in numbers}

    def evaluate_result(self, actual, predicted, bets, balance):
        won = actual in predicted
        net = 36 * bets.get(actual, 0) - sum(bets.values())
        return won, net, balance + net

    def get_display_label(self):
        return None


class DummyConsole:
    def __init__(self, *args, **kwargs):
        self.messages = []

    def print(self, *args, **kwargs):
        self.messages.append(args)

    def rule(self, *args, **kwargs):
        pass


class DummyTable:
    def __init__(self, *args, **kwargs):
        pass

    def add_column(self, *args, **kwargs):
        pass

    def add_row(self, *args, **kwargs):
        pass


class DummyProgress:
    def __init__(self, *args, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def add_task(self, *args, **kwargs):
        return 0

    def advance(self, *args, **kwargs):
        pass


def module(name, **attributes):
    result = types.ModuleType(name)
    result.__dict__.update(attributes)
    return result


def load_under_test(name, filename):
    """Substitute heavy learning/display dependencies, not evaluation logic."""
    stubs = {
        "models.neural_network": module("models.neural_network", RouletteNeuralNetwork=FakeNN),
        "strategies.base": module("strategies.base", BaseStrategy=object),
        "core.trainer": module("core.trainer", ModelTrainer=FakeTrainer),
        "rich": module("rich", box=types.SimpleNamespace(ROUNDED=None)),
        "rich.console": module("rich.console", Console=DummyConsole),
        "rich.panel": module("rich.panel", Panel=lambda content, **kwargs: content),
        "rich.table": module("rich.table", Table=DummyTable),
        "rich.progress": module("rich.progress", Progress=DummyProgress,
            BarColumn=lambda **kwargs: None, TextColumn=lambda *args, **kwargs: None,
            TimeElapsedColumn=lambda: None),
    }
    with patch.dict(sys.modules, stubs):
        spec = importlib.util.spec_from_file_location(name, ROOT / filename)
        result = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(result)
        return result


engine_module = load_under_test("engine_causality_fixture", "core/engine.py")
file_module = load_under_test("file_causality_fixture", "core/file_trainer.py")


class EngineCausalityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        FakeNN.instances = []
        FakeTrainer.instances = []
        self.temp = tempfile.TemporaryDirectory()
        self.logger = patch.object(engine_module, "setup_logger", return_value=logging.getLogger("causality-test"))
        self.logger.start()

    def tearDown(self):
        self.logger.stop()
        self.temp.cleanup()

    def make_engine(self, **kwargs):
        return engine_module.PredictionEngine(FakeStrategy(), 1000.0,
            model_dir=self.temp.name, **kwargs)

    async def test_injected_trained_network_is_used_without_building_another_model(self):
        network = FakeNN(save_dir=self.temp.name)
        network.load()
        engine = self.make_engine(neural_net=network, use_live=False, auto_train=True)
        self.assertIs(engine.nn, network)
        self.assertEqual(network.loads, 1)
        self.assertEqual(len(FakeNN.instances), 1)
        self.assertIs(engine.trainer.nn, network)
        self.assertEqual(engine.history, [])
        for actual in range(20):
            await engine.handle_spin(actual)
        self.assertEqual(engine.training_updates, 1)

    async def test_first_fifteen_are_unscored_and_target_is_not_an_input(self):
        engine = self.make_engine(use_live=False)
        for actual in range(15):
            await engine.handle_spin(actual)
        self.assertEqual(engine.tracker.total_spins, 0)
        self.assertEqual(engine.nn.model.inputs, [])
        await engine.handle_spin(15)
        self.assertEqual(engine.nn.model.inputs, [tuple(range(15))])
        self.assertEqual(engine.tracker.history[0]["predicted"], [15])
        self.assertEqual(engine.history, list(range(16)))

    async def test_displayed_pending_prediction_is_reused_for_its_next_result(self):
        engine = self.make_engine(use_live=True)
        advice = []
        engine.on_advice = advice.append
        for actual in range(15):
            await engine.handle_spin(actual)
        self.assertEqual(advice[-1]["predicted"], [15])
        self.assertEqual(len(engine.nn.model.inputs), 1)
        await engine.handle_spin(15)
        self.assertEqual(engine.tracker.history[0]["predicted"], advice[0]["predicted"])
        self.assertEqual(engine.nn.model.inputs, [tuple(range(15)), tuple(range(1, 16))])

    async def test_stale_pending_context_is_not_scored(self):
        engine = self.make_engine(use_live=False)
        engine.history = list(range(15))
        await engine.show_advice()
        engine.history[-1] = 20
        await engine.handle_spin(21)
        self.assertEqual(engine.tracker.history[0]["predicted"], [21])
        self.assertEqual(engine.nn.model.inputs[-1][-1], 20)

    async def test_reset_clears_context_and_pending_but_retains_paper_stats(self):
        engine = self.make_engine(use_live=True)
        for actual in range(16):
            await engine.handle_spin(actual)
        await engine.reset_history("generated source gap")
        self.assertEqual(engine.history, [])
        self.assertIsNone(engine._pending_predicted)
        self.assertIsNone(engine._pending_history)
        self.assertEqual(engine.tracker.total_spins, 1)
        for actual in [5] * 15:
            await engine.handle_spin(actual)
        self.assertEqual(engine.tracker.total_spins, 1)
        await engine.handle_spin(6)
        self.assertEqual(engine.tracker.total_spins, 2)
        self.assertEqual(engine.tracker.history[-1]["predicted"], [6])

    async def test_online_training_happens_only_after_current_result_is_scored(self):
        engine = self.make_engine(use_live=True, auto_train=True)
        for actual in range(20):
            await engine.handle_spin(actual)
        training_history, prediction_context = engine.trainer.online_inputs[0]
        self.assertEqual(training_history, tuple(range(20)))
        self.assertEqual(prediction_context, tuple(range(4, 19)))

    async def test_injected_feed_registers_reset_and_number_without_default_socket(self):
        class Feed:
            def on_number(self, callback):
                self.number_callback = callback

            def on_reset(self, callback):
                self.reset_callback = callback

            async def listen(self):
                self.listened = True
                await self.reset_callback("generated new session")
                for actual in range(16):
                    await self.number_callback(actual)

        feed = Feed()
        engine = self.make_engine(data_feed=feed)
        await engine.run()
        self.assertTrue(feed.listened)
        self.assertIs(engine._feed, feed)
        self.assertEqual(engine.nn.save_dir, self.temp.name)
        self.assertEqual(engine.tracker.total_spins, 1)

    async def test_existing_simulation_feed_still_uses_before_spin_hook(self):
        class Feed:
            def __init__(self, **kwargs):
                pass

            def on_before_spin(self, callback):
                self.before = callback

            def on_number(self, callback):
                self.number = callback

            async def run(self):
                for actual in range(16):
                    await self.before()
                    await self.number(actual)

        engine = self.make_engine(use_live=False)
        with patch.dict(sys.modules, {"data.simulator": module("data.simulator", Simulator=Feed)}):
            await engine.run()
        self.assertEqual(engine.tracker.total_spins, 1)
        self.assertEqual(engine.tracker.history[0]["predicted"], [15])

    async def test_existing_manual_feed_still_uses_run_and_before_spin_hook(self):
        class Feed:
            def on_before_spin(self, callback):
                self.before = callback

            def on_number(self, callback):
                self.number = callback

            async def run(self):
                for actual in range(16):
                    await self.before()
                    await self.number(actual)

        engine = self.make_engine(use_live=False, manual_mode=True)
        with patch.dict(sys.modules, {"data.manual_feed": module("data.manual_feed", ManualFeed=Feed)}):
            await engine.run()
        self.assertEqual(engine.tracker.total_spins, 1)
        self.assertEqual(engine.tracker.history[0]["predicted"], [15])

    async def test_default_live_feed_remains_listen_based_without_reset_requirement(self):
        class Feed:
            def __init__(self, *args):
                self.arguments = args

            def on_number(self, callback):
                self.number = callback

            async def listen(self):
                for actual in range(16):
                    await self.number(actual)

        engine = self.make_engine(use_live=True)
        with patch.dict(sys.modules, {"data.live_feed": module("data.live_feed", LiveFeed=Feed)}):
            await engine.run()
        self.assertEqual(engine.tracker.total_spins, 1)
        self.assertEqual(engine.tracker.history[0]["predicted"], [15])

    async def test_bounded_history_still_predicts_from_the_prior_full_window(self):
        engine = self.make_engine(use_live=False)
        with patch.object(engine_module, "MAX_HISTORY", 15):
            for actual in range(25):
                await engine.handle_spin(actual)
        self.assertEqual(len(engine.history), 15)
        self.assertEqual(engine.nn.model.inputs, [tuple(range(i - 15, i)) for i in range(15, 25)])


class FileTrainingCausalityTests(unittest.TestCase):
    def setUp(self):
        FakeNN.instances = []
        FakeTrainer.instances = []
        FakeTrainer.offline_success = True
        self.temp = tempfile.TemporaryDirectory()

    def tearDown(self):
        self.temp.cleanup()

    def run_files(self, numbers_by_file):
        entries = [{"name": f"generated_{index}.txt", "numbers": numbers}
                   for index, numbers in enumerate(numbers_by_file)]
        feed = types.SimpleNamespace(load_all=lambda: entries)
        with patch.object(file_module, "FileFeed", return_value=feed):
            return file_module.run_file_training(FakeStrategy(), model_dir=self.temp.name)

    def test_only_prefix_trains_and_each_target_follows_its_input_window(self):
        numbers = [index % 37 for index in range(50)]
        results = self.run_files([numbers])
        self.assertEqual(FakeTrainer.instances[0].offline_inputs, [tuple(numbers[:40])])
        self.assertEqual(FakeTrainer.instances[0].online_inputs, [])
        self.assertEqual(FakeNN.instances[0].loads, 0)
        self.assertEqual(FakeNN.instances[0].builds, 1)
        expected_inputs = [tuple(numbers[index - 15:index]) for index in range(40, 50)]
        self.assertEqual(FakeNN.instances[0].model.inputs, expected_inputs)
        self.assertEqual((results[0]["train_spins"], results[0]["test_spins"], results[0]["evaluated"]), (40, 10, 10))

    def test_each_file_gets_fresh_weights_and_separate_checkpoint_directory(self):
        files = [[index % 37 for index in range(40)], [(36 - index) % 37 for index in range(40)]]
        results = self.run_files(files)
        self.assertEqual(len(FakeNN.instances), 2)
        self.assertIsNot(FakeNN.instances[0].model, FakeNN.instances[1].model)
        self.assertNotEqual(results[0]["model_dir"], results[1]["model_dir"])
        self.assertTrue(all(nn.loads == 0 for nn in FakeNN.instances))
        self.assertEqual(FakeTrainer.instances[1].offline_inputs, [tuple(files[1][:32])])

    def test_short_files_are_skipped_without_fitting_or_scoring(self):
        results = self.run_files([[0] * 25])
        self.assertEqual(results[0]["status"], "SKIPPED")
        self.assertEqual(results[0]["evaluated"], 0)
        self.assertEqual(FakeNN.instances, [])

    def test_training_failure_does_not_claim_a_score_or_saved_model(self):
        FakeTrainer.offline_success = False
        results = self.run_files([[index % 37 for index in range(40)]])
        self.assertEqual(results[0]["status"], "TRAIN_FAILED")
        self.assertFalse(results[0]["model_saved"])
        self.assertEqual(results[0]["evaluated"], 0)
        self.assertEqual(FakeNN.instances[0].model.inputs, [])

    def test_smallest_valid_split_preserves_one_heldout_result(self):
        self.assertEqual(file_module.chronological_train_count(26), 25)
        self.assertEqual(file_module.chronological_train_count(50), 40)
        for fraction in (0, 1, -0.1, float("nan")):
            with self.assertRaises(ValueError):
                file_module.chronological_train_count(50, fraction)


if __name__ == "__main__":
    unittest.main()
