"""Fresh per-file training with chronological, unseen-tail evaluation."""

import logging
import os
import uuid

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.progress import Progress, BarColumn, TextColumn, TimeElapsedColumn
from rich import box

from models.neural_network import RouletteNeuralNetwork
from core.trainer import ModelTrainer
from data.file_feed import FileFeed
from strategies.base import BaseStrategy
from utils.tracker import StatsTracker
from config.settings import SEQUENCE_LENGTH, REAL_MODELS_DIR, TRAINING_FILES_DIR

console = Console()
_log = logging.getLogger("FileTrainer")
# ModelTrainer requires at least ten sliding-window training examples.
MIN_TRAIN_SPINS = SEQUENCE_LENGTH + 10


def chronological_train_count(total_spins: int, train_fraction: float = 0.8):
    """Return a prefix size leaving at least one genuinely held-out result."""
    if not 0 < train_fraction < 1:
        raise ValueError("train_fraction must be between zero and one")
    if total_spins < MIN_TRAIN_SPINS + 1:
        return None
    return min(total_spins - 1, max(MIN_TRAIN_SPINS, int(total_spins * train_fraction)))


def run_file_training(
    strategy: BaseStrategy,
    auto_train: bool = True,
    *,
    train_fraction: float = 0.8,
    model_dir=None,
):
    """Train each fresh model on a prefix and score only its held-out tail.

    ``auto_train`` remains accepted for compatibility. There are never online
    weight updates during holdout evaluation. Earlier revealed holdout results
    may enter the context for later predictions, as they would in a live stream.
    Existing checkpoints, including ones from other files, are never loaded.
    """
    chronological_train_count(0, train_fraction)  # Validate even if no files exist.
    files = FileFeed().load_all()
    if not files:
        console.print(Panel(
            f"[bold red]No .txt files found![/bold red]\n\n"
            f"  Put your data files in:\n  [bold cyan]{os.path.abspath(TRAINING_FILES_DIR)}[/bold cyan]\n\n"
            "  Format: one number per line, or comma/space separated (0-36).\n"
            "  Lines starting with # are ignored.",
            title="[bold]File Training[/bold]", border_style="red",
        ))
        return []

    # Separate directories avoid overwriting live/manual checkpoints or loading
    # another file's fitted weights into an allegedly independent evaluation.
    evaluation_dir = os.path.join(
        os.fspath(model_dir if model_dir is not None else REAL_MODELS_DIR),
        "file_evaluations", uuid.uuid4().hex[:12],
    )
    console.print(Panel(
        f"  Files: [bold cyan]{len(files)}[/bold cyan]\n"
        f"  Strategy: [bold]{strategy.STRATEGY_NAME}[/bold]\n"
        f"  Training prefix target: {train_fraction:.0%}; remaining tail is held out.\n"
        f"  Separate per-file checkpoints: {os.path.abspath(evaluation_dir)}",
        title="[bold cyan]Chronological File Evaluation[/bold cyan]", border_style="cyan",
    ))
    results = []
    for index, data in enumerate(files, 1):
        name, numbers = data["name"], data["numbers"]
        total = len(numbers)
        train_count = chronological_train_count(total, train_fraction)
        console.rule(f"[bold yellow]File {index}/{len(files)}: {name}[/bold yellow]")
        result = {
            "file": name, "spins": total, "status": "SKIPPED",
            "train_spins": 0, "test_spins": 0, "evaluated": 0,
            "wins": 0, "losses": 0, "win_rate": 0, "roi": 0,
            "model_saved": False, "model_dir": None,
        }
        if train_count is None:
            console.print(
                f"  Skipped: {total} spins; need at least {MIN_TRAIN_SPINS + 1} "
                "for ten training examples and one held-out result."
            )
            results.append(result)
            continue

        result.update(train_spins=train_count, test_spins=total - train_count)
        file_dir = os.path.join(evaluation_dir, f"file_{index:03d}")
        os.makedirs(file_dir, exist_ok=True)
        nn = RouletteNeuralNetwork(model_name=strategy.model_filename, save_dir=file_dir)
        nn.model = nn.build()  # load() could read weights trained on future data.
        trainer = ModelTrainer(nn, verbose=True, save_dir=file_dir)
        console.print(
            f"  Train: {train_count} prefix spins ({train_count - SEQUENCE_LENGTH} examples); "
            f"test: {total - train_count} later spins."
        )
        if not trainer.train_offline(numbers[:train_count]):
            result["status"] = "TRAIN_FAILED"
            console.print("  Training failed; no held-out score or saved-model claim is reported.")
            results.append(result)
            continue

        result.update(model_saved=True, model_dir=file_dir)
        tracker = _evaluate_holdout(nn, strategy, numbers, train_count)
        result.update(
            status="OK", evaluated=tracker.total_spins,
            wins=tracker.total_wins, losses=tracker.total_losses,
            win_rate=tracker.win_rate, roi=tracker.roi,
        )
        _print_file_result(name, total, tracker, train_count)
        results.append(result)

    _print_overall_summary(results, strategy, evaluation_dir)
    return results


def _evaluate_holdout(nn, strategy, numbers, train_count):
    """Weights stay fixed; each target is appended only after its prediction."""
    tracker = StatsTracker()
    history = list(numbers[:train_count])
    with Progress(
        TextColumn("[dim]{task.description}[/dim]"), BarColumn(bar_width=30),
        TextColumn("[bold]{task.completed}/{task.total}[/bold]"),
        TimeElapsedColumn(), console=console, transient=True,
    ) as progress:
        task = progress.add_task("Evaluating held-out tail", total=len(numbers) - train_count)
        for actual in numbers[train_count:]:
            inp = RouletteNeuralNetwork.build_prediction_input(history)
            probabilities = nn.model.predict(inp, verbose=0)[0]
            predicted = strategy.select_numbers(probabilities)
            bets = strategy.calculate_bets(predicted, balance=1000.0, bet_per_number=1.00)
            is_win, net, _ = strategy.evaluate_result(actual, predicted, bets, 1000.0)
            tracker.record(actual, predicted, bets, is_win, net, 1000.0 + tracker.total_profit + net)
            history.append(actual)
            progress.advance(task)
    return tracker


def _print_file_result(name, total_spins, tracker, train_count):
    console.print(
        f"\n  [bold]{name}[/bold]: {total_spins} total spins; "
        f"{train_count} train / {total_spins - train_count} held out\n"
        f"  Evaluated: {tracker.total_spins}; wins: {tracker.total_wins}; "
        f"losses: {tracker.total_losses}\n"
        f"  Held-out win rate: {tracker.win_rate:.2f}%; "
        f"paper ROI: {tracker.roi:+.2f}%; paper profit: ${tracker.total_profit:+.2f}"
    )


def _print_overall_summary(results, strategy, evaluation_dir):
    table = Table(title=f"Held-out evaluation: {strategy.STRATEGY_NAME}", box=box.ROUNDED)
    for name in ("File", "Total", "Train", "Held out", "Scored", "Status", "Wins", "Losses", "Win rate", "Paper ROI"):
        table.add_column(name)
    for result in results:
        scored = result["status"] == "OK"
        table.add_row(
            result["file"], str(result["spins"]), str(result["train_spins"]),
            str(result["test_spins"]), str(result["evaluated"]), result["status"],
            str(result["wins"]), str(result["losses"]),
            f"{result['win_rate']:.2f}%" if scored else "—",
            f"{result['roi']:+.2f}%" if scored else "—",
        )
    console.print(table)
    console.print(Panel(
        f"Per-file checkpoint directory: {os.path.abspath(evaluation_dir)}\n"
        "Each file starts with fresh weights. Scores use only its unseen tail; "
        "weights remain fixed throughout that tail. Failed/skipped files have no score.\n"
        "Existing live/manual models were not loaded or overwritten.",
        border_style="cyan",
    ))
