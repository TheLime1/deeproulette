# Copyright 2026 alaevate
# Licensed under the Apache License, Version 2.0
#!/usr/bin/env python3
"""DeepRoulette — AI-powered roulette prediction system."""

import sys
import os

# Suppress TensorFlow noise before any TF import
os.environ["TF_CPP_MIN_LOG_LEVEL"] = "3"
os.environ["TF_ENABLE_ONEDNN_OPTS"] = "0"
os.environ["CUDA_VISIBLE_DEVICES"] = ""

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

try:
    from rich.console import Console
except ImportError:
    print("\n  ERROR: Required packages are not installed.")
    print("  Run:  pip install -r requirements.txt\n")
    input("  Press Enter to exit...")
    sys.exit(1)

import asyncio

from ui.menu import run_menu, console
from ui.dashboard import LiveDashboard
from core.engine import PredictionEngine
from config.settings import (
    SAVED_MODELS_DIR, REAL_MODELS_DIR, SIMULATION_MODELS_DIR,
    LOGS_DIR, DATA_STORE_DIR, TRAINING_FILES_DIR,
)
from utils.updater import check_for_updates
from pathlib import Path
from data.robrun_profile import find_saved_context, load_robrun_profile

APP_ROOT = Path(__file__).resolve().parent


def ensure_directories():
    for d in (SAVED_MODELS_DIR, REAL_MODELS_DIR, SIMULATION_MODELS_DIR,
              LOGS_DIR, DATA_STORE_DIR, TRAINING_FILES_DIR):
        os.makedirs(d, exist_ok=True)


async def run_session(config: dict):
    """Set up the dashboard + engine, run until Ctrl+C or balance gone."""
    if config['use_live']:
        from core.robrun_live import run_qa_live
        await run_qa_live(APP_ROOT,config)
        return
    strategy = config["strategy_cls"]()
    balance = config["balance"]
    manual = config.get("manual_mode", False)
    use_live = config["use_live"]
    profile = config.get("robrun_profile") if manual else None

    if manual:
        mode_label = "[magenta]✍️ Manual[/magenta]"
    elif use_live:
        mode_label = "[cyan]🌐 Live[/cyan]"
    else:
        mode_label = "[yellow]🔄 Sim[/yellow]"

    dashboard = LiveDashboard(strategy, balance, mode_label, config["auto_train"])

    engine = PredictionEngine(
        strategy=strategy,
        initial_balance=balance,
        bet_per_number=config.get("bet_per_number", 1.00),
        auto_train=config["auto_train"],
        use_live=use_live,
        spin_interval=config.get("spin_interval", 5.0),
        manual_mode=manual,
        model_dir=str(profile.active_dir) if profile else None,
        neural_net=profile.network if profile else None,
    )
    saved_context = config.get('saved_context') if manual else None
    if saved_context:
        engine.saved_context = saved_context.numbers
        console.print(f"[cyan]Fast Start loaded {len(saved_context.numbers)} saved results from {saved_context.source}.[/cyan]")
    if profile:
        if not saved_context:
            engine.history = profile.recent_context()
        console.print(f"[cyan]ROB RUN trained model loaded; {len(profile.history)} archived training results.[/cyan]")
        if not engine.history and not saved_context:
            console.print("[dim]Archived results are over 60 seconds old. Enter 15 new consecutive results to establish current context.[/dim]")

    # Manual mode needs stdin so can't use alt-screen Live — use legacy scroll display
    if manual:
        from ui.display import (
            render_spin, render_waiting, render_session_header,
            render_balance_empty, render_session_summary, render_bet_advice,
        )
        engine.on_spin_complete = render_spin
        engine.on_waiting = render_waiting
        engine.on_balance_empty = render_balance_empty
        engine.on_advice = render_bet_advice
        render_session_header(strategy, balance, use_live, config["auto_train"], True)
        try:
            await engine.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass
        finally:
            if profile and engine.training_updates:
                engine.nn.save()
                console.print("[cyan]ROB RUN model updates saved for the next launch.[/cyan]")
            if not engine.checkpoint_mode_complete:
                render_session_summary(engine.tracker, strategy, balance)
        return

    # Simulation / Live: full-screen fixed-page dashboard
    engine.on_spin_complete = dashboard.on_spin_complete
    engine.on_waiting = dashboard.on_waiting
    engine.on_balance_empty = dashboard.on_balance_empty
    engine.on_advice = dashboard.on_advice
    engine.on_model_status = dashboard.on_model_status
    engine.on_milestone = dashboard.on_milestone   # checkpoint pauses at 100/250/500/1000 spins

    live_ctx = dashboard.start()
    with live_ctx:
        try:
            await engine.run()
        except (asyncio.CancelledError, KeyboardInterrupt):
            pass

    # Print final summary after Live screen closes
    if not engine.checkpoint_mode_complete:
        dashboard.print_summary(engine.tracker, strategy)


def main():
    os.chdir(APP_ROOT)
    ensure_directories()
    try:
        profile = load_robrun_profile(APP_ROOT)
        if profile:
            profile.load_network()
    except Exception as error:
        console.print(f"[red]Cannot load your ROB RUN profile: {error}[/red]")
        return
    check_for_updates()

    try:
        config = run_menu(robrun_profile=profile, saved_context=find_saved_context(APP_ROOT, profile))
        if profile and (config.get("manual_mode") or config.get('use_live')):
            config["robrun_profile"] = profile
    except KeyboardInterrupt:
        console.print("\n[bold yellow]  Goodbye![/bold yellow]\n")
        return

    # File-training mode runs synchronously (no live engine needed)
    if config.get("file_train"):
        from core.file_trainer import run_file_training
        strategy = config["strategy_cls"]()
        run_file_training(strategy)
        console.print("[bold yellow]  Thanks for using DeepRoulette![/bold yellow]\n")
        return

    try:
        asyncio.run(run_session(config))
    except KeyboardInterrupt:
        pass
    except RuntimeError as error:
        console.print(f'[red]Session could not continue: {error}[/red]')

    console.print("[bold yellow]  Thanks for using DeepRoulette![/bold yellow]\n")


if __name__ == "__main__":
    main()
