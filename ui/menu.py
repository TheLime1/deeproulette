# Copyright 2026 alaevate
# Licensed under the Apache License, Version 2.0
"""Interactive startup menu — guided strategy & session configuration."""

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.prompt import Prompt, Confirm, FloatPrompt
from rich.text import Text
from rich.align import Align
from rich.columns import Columns
from rich import box

from strategies import ALL_STRATEGIES, INSIDE_STRATEGIES, OUTSIDE_STRATEGIES, AI_STRATEGIES
from config.settings import DEFAULT_BALANCE, DEFAULT_BET_AMOUNT
from utils.constants import SPEED_PRESETS

console = Console()

_RISK_COLOUR = {
    "Extreme":  "red",
    "High":     "orange1",
    "Medium":   "yellow",
    "Low":      "green",
    "Very Low": "bright_green",
    "Variable": "cyan",
}
# "DEEP" block — 32 chars wide
_LOGO_DEEP = [
    ("██████╗ ███████╗███████╗██████╗ ", "bold cyan"),
    ("██╔══██╗██╔════╝██╔════╝██╔══██╗", "bold cyan"),
    ("██║  ██║█████╗  █████╗  ██████╔╝", "bold bright_cyan"),
    ("██║  ██║██╔══╝  ██╔══╝  ██╔═══╝ ", "bold bright_cyan"),
    ("██████╔╝███████╗███████╗██║     ", "bold cyan"),
    ("╚═════╝ ╚══════╝╚══════╝╚═╝     ", "cyan"),
]

# "ROULETTE" block — 68 chars wide
_LOGO_ROULETTE = [
    ("██████╗  ██████╗ ██╗   ██╗██╗     ███████╗████████╗████████╗███████╗",  "bold bright_cyan"),
    ("██╔══██╗██╔═══██╗██║   ██║██║     ██╔════╝╚══██╔══╝╚══██╔══╝██╔════╝",  "bold bright_cyan"),
    ("██████╔╝██║   ██║██║   ██║██║     █████╗     ██║      ██║   █████╗  ",   "bold cyan"),
    ("██╔══██╗██║   ██║██║   ██║██║     ██╔══╝     ██║      ██║   ██╔══╝  ",   "bold cyan"),
    ("██║  ██║╚██████╔╝╚██████╔╝███████╗███████╗   ██║      ██║   ███████╗",   "bold bright_cyan"),
    ("╚═╝  ╚═╝ ╚═════╝  ╚═════╝ ╚══════╝╚══════╝   ╚═╝      ╚═╝   ╚══════╝",  "cyan"),
]

def show_welcome(robrun_profile=None):
    console.clear()

    ROULETTE_W = 68   # width of ROULETTE block (the wider part)
    DEEP_W     = 32   # width of DEEP block

    term_w   = console.width or 120
    base_pad = max(2, (term_w - ROULETTE_W) // 2)
    indent_r = " " * base_pad                                       # ROULETTE left edge
    indent_d = " " * (base_pad + (ROULETTE_W - DEEP_W) // 2)       # DEEP centred above ROULETTE

    # Build logo text block
    logo = Text()
    logo.append("\n")
    for line, colour in _LOGO_DEEP:
        logo.append(indent_d + line + "\n", style=colour)
    for line, colour in _LOGO_ROULETTE:
        logo.append(indent_r + line + "\n", style=colour)

    # Subtitle — each line individually centred under the ROULETTE block
    def _centre(text: str) -> str:
        """Return indent that centres `text` within the ROULETTE block."""
        pad = base_pad + max(0, (ROULETTE_W - len(text)) // 2)
        return " " * pad

    logo.append("\n")

    line1 = "Three-layer LSTM  •  Roulette research"
    line2 = "━" * 52
    line3 = "v2.0  •  github.com/alaevate/deeproulette  •  2026"

    logo.append(_centre(line1) + line1 + "\n", style="dim cyan")
    logo.append(_centre(line2) + line2 + "\n", style="cyan")
    logo.append(_centre(line3) + line3 + "\n", style="dim")
    # logo.append("\n")

    console.print(Panel(
        logo,
        border_style="cyan",
        padding=(0, 2),
        expand=True,
    ))
    console.print()
    if robrun_profile:
        console.print(Panel(
            f"[bold cyan]Your ROB RUN profile is loaded[/bold cyan]\n"
            f"Training history: {len(robrun_profile.history)} selected consecutive results\n"
            "Trained checkpoint: loaded; used by ROB RUN QA Live and Manual Entry.\n"
            "Auto-training updates are saved to this profile for the next launch.\n"
            "[dim]Archived history remains separate from current results after a 60-second gap.\n"
            "Experimental labels; predictive accuracy has not been validated.[/dim]",
            border_style="cyan"))

def ask_strategy() -> type:
    """Show categorised strategy table and return the chosen strategy class."""
    table = Table(
        title="[bold]Step 1 of 5  —  Choose Your Strategy[/bold]",
        box=box.ROUNDED,
        border_style="cyan",
        show_header=True,
        header_style="bold cyan",
        padding=(0, 1),
    )
    table.add_column("#",            width=3,  justify="center")
    table.add_column("Strategy",     width=22)
    table.add_column("Risk Level",   width=12, justify="center")
    table.add_column("Numbers Bet",  width=12, justify="center")
    table.add_column("Win Chance",   width=12, justify="center")
    table.add_column("Description",  min_width=40)

    idx = 0
    for label, group in [
        ("🎯  Inside Bets (straight-up numbers)", INSIDE_STRATEGIES),
        ("🎨  Outside Bets (red/black, odd/even …)", OUTSIDE_STRATEGIES),
        ("🤖  AI Combo Strategies", AI_STRATEGIES),
    ]:
        table.add_row("", f"[bold underline]{label}[/bold underline]", "", "", "", "", end_section=True)
        for cls in group:
            idx += 1
            s          = cls()
            risk_color = _RISK_COLOUR.get(s.RISK_LEVEL, "white")
            nums       = str(s.NUMBERS_TO_BET) if s.NUMBERS_TO_BET > 0 else "1–18"
            chance     = f"{s.TARGET_WIN_RATE:.1f}%" if s.TARGET_WIN_RATE > 0 else "Varies"
            table.add_row(
                str(idx),
                s.STRATEGY_NAME,
                f"[{risk_color}]{s.RISK_LEVEL}[/{risk_color}]",
                nums,
                chance,
                s.DESCRIPTION,
            )

    console.print(table)
    console.print()
    console.print(
        "  [dim]Not sure which to pick?  "
        "[bold]3 (Balanced)[/bold] is a great starting point.[/dim]"
    )
    console.print()

    choice = Prompt.ask(
        "[bold]Enter the number of your strategy[/bold]",
        choices=[str(i) for i in range(1, len(ALL_STRATEGIES) + 1)],
        default="3",
    )
    selected = ALL_STRATEGIES[int(choice) - 1]
    console.print(
        f"\n  ✅ Strategy selected: [bold cyan]{selected().STRATEGY_NAME}[/bold cyan]\n"
    )
    return selected


def ask_balance() -> float:
    """Prompt for the starting virtual wallet size."""
    console.print(Panel(
        "[bold]Step 2 of 5  —  Starting Balance[/bold]\\n\\n"
        "  This is your virtual wallet for this session.\n"
        "  [dim]No real money is used — this is a simulation of betting.[/dim]\n\n"
        "  💡 Recommended: [bold]$50 – $500[/bold] to get meaningful statistics.",
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()

    balance = FloatPrompt.ask(
        "[bold]Enter starting balance ($)[/bold]",
        default=DEFAULT_BALANCE,
    )
    balance = max(1.0, float(balance))
    console.print(f"\n  ✅ Balance set to: [bold yellow]${balance:.2f}[/bold yellow]\n")
    return balance


def ask_bet_amount() -> float:
    """Prompt for the flat bet amount per number."""
    console.print(Panel(
        "[bold]Step 3 of 5  —  Bet Amount Per Number[/bold]\n\n"
        "  How much do you want to bet on each predicted number?\n"
        "  [dim]This is a flat dollar amount per number (not a percentage).[/dim]\n\n"
        "  💡 Recommended: [bold]$0.50 – $5.00[/bold] for balanced risk.",
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()

    bet_amount = FloatPrompt.ask(
        "[bold]Enter bet amount per number ($)[/bold]",
        default=DEFAULT_BET_AMOUNT,
    )
    bet_amount = max(0.10, float(bet_amount))
    console.print(f"\n  ✅ Bet amount set to: [bold yellow]${bet_amount:.2f}[/bold yellow] per number\n")
    return bet_amount


def ask_mode() -> tuple:
    """Choose bundled ROB RUN QA, simulation, manual entry, or file training."""
    console.print(Panel(
        "[bold]Step 4 of 5  —  Data Source[/bold]\n\n"
        "  [bold cyan][1] ROB RUN QA Live[/bold cyan]\n"
        "      Starts the bundled passive collector for your running ROB RUN client.\n"
        "      [dim]Saved ROB RUN model, live candidates and 60-second streaks. Windows capture permission required.[/dim]\n\n"
        "  [bold yellow][2] Simulation Mode[/bold yellow]\n"
        "      Generates random spins locally on your computer.\n"
        "      [dim]No internet needed. Great for testing strategies.[/dim]\n\n"
        "  [bold magenta][3] Manual Entry  ← Use AI advice at a real casino[/bold magenta]\n"
        "      You type each spin result — AI shows where to bet before each spin.\n"
        "      [dim]Perfect for using the AI at a live or online casino.[/dim]\n\n"
        "  [bold green][4] File Training  ← Train AI from real casino data[/bold green]\n"
        "      Place .txt files with spin numbers in data_store/training_files/\n"
        "      [dim]AI trains on each file and shows per-file accuracy results.[/dim]",
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()

    choice = Prompt.ask(
        "[bold]Choose mode[/bold]",
        choices=["1", "2", "3", "4"],
        default="1",
    )
    use_live    = (choice == "1")
    manual_mode = (choice == "3")
    file_train  = (choice == "4")
    labels = {
        "1": "[cyan]ROB RUN QA Live[/cyan]",
        "2": "[yellow]Simulation[/yellow]",
        "3": "[magenta]Manual Entry[/magenta]",
        "4": "[green]File Training[/green]",
    }
    console.print(f"\n  ✅ Mode set to: {labels[choice]}\n")
    return use_live, manual_mode, file_train


def ask_sim_speed() -> float:
    """Let the user pick how fast the simulator generates spins."""
    table = Table(
        title="[bold]Step 4 of 5  —  Simulation Speed[/bold]",
        box=box.ROUNDED,
        border_style="cyan",
        show_header=True,
        header_style="bold cyan",
        padding=(0, 1),
    )
    table.add_column("#",           width=3,  justify="center")
    table.add_column("Speed",       width=10)
    table.add_column("Description", min_width=42)

    for i, (label, desc, _) in enumerate(SPEED_PRESETS, 1):
        colour = ["white", "yellow", "orange1", "red"][i - 1]
        table.add_row(str(i), f"[{colour}]{label}[/{colour}]", desc)

    console.print(table)
    console.print()
    console.print(
        "  [dim]Tip: use [bold]Turbo[/bold] or [bold]Max[/bold] to collect training data quickly.[/dim]"
    )
    console.print()

    choice = Prompt.ask(
        "[bold]Choose speed[/bold]",
        choices=[str(i) for i in range(1, len(SPEED_PRESETS) + 1)],
        default="1",
    )
    label, _, interval = SPEED_PRESETS[int(choice) - 1]
    console.print(f"\n  ✅ Speed set to: [bold cyan]{label}[/bold cyan]\n")
    return interval


def ask_auto_train() -> bool:
    """Ask whether to enable online model updates during the session."""
    console.print(Panel(
        "[bold]Step 5 of 5  —  AI Auto-Training[/bold]\n\n"
        "  When enabled, model weights are updated after enough new results arrive.\n"
        "  Improvement must be measured on later unseen results.\n\n"
        "  [dim]This uses additional CPU.\n"
        "  The updated model is saved to disk and reused in future sessions.[/dim]",
        border_style="dim",
        padding=(0, 2),
    ))
    console.print()

    enabled = Confirm.ask(
        "🤖 Enable [bold]AI auto-training[/bold]?",
        default=False,
    )
    label = "[green]Enabled[/green]" if enabled else "[dim]Disabled[/dim]"
    console.print(f"\n  ✅ Auto-training: {label}\n")
    return enabled


def show_confirmation(strategy_cls, balance: float, bet_amount: float, use_live: bool, auto_train: bool, spin_interval: float = 5.0, manual_mode: bool = False):
    """Display a summary of all chosen settings before launching."""
    s          = strategy_cls()
    risk_color = _RISK_COLOUR.get(s.RISK_LEVEL, "white")
    if manual_mode:
        mode_label = "[magenta]Manual Entry[/magenta]"
    elif use_live:
        mode_label = "[cyan]ROB RUN QA Live[/cyan]"
    else:
        mode_label = "[yellow]Simulation[/yellow]"
    train_label = "[green]On[/green]" if auto_train else "[dim]Off[/dim]"

    # Find matching speed label
    speed_label = next(
        (lbl for lbl, _, iv in SPEED_PRESETS if iv == spin_interval),
        f"{spin_interval}s"
    )
    speed_line = "" if (use_live or manual_mode) else f"\n  Sim Speed  :  [bold cyan]{speed_label}[/bold cyan]"

    console.print(Panel(
        f"  [bold green]All set! Here's your configuration:[/bold green]\n\n"
        f"  Strategy   :  [bold]{s.STRATEGY_NAME}[/bold]\n"
        f"  Risk Level :  [{risk_color}]{s.RISK_LEVEL}[/{risk_color}]\n"
        f"  Balance    :  [bold yellow]${balance:.2f}[/bold yellow]\n"
        f"  Bet/Number :  [bold yellow]${bet_amount:.2f}[/bold yellow]\n"
        f"  Mode       :  {mode_label}{speed_line}\n"
        f"  Auto-Train :  {train_label}",
        title="[bold cyan]Ready to Start[/bold cyan]",
        border_style="green",
        padding=(0, 2),
    ))
    console.print()
    if not Confirm.ask("[bold]Press Enter to launch[/bold]", default=True):
        raise KeyboardInterrupt


def run_menu(robrun_profile=None) -> dict:
    """
    Run all menu steps and return a configuration dict.

    Returns:
        {
          "strategy_cls": <class>,
          "balance":      float,
          "bet_per_number": float,
          "use_live":     bool,
          "auto_train":   bool,
        }
    """
    show_welcome(robrun_profile)

    strategy_cls                       = ask_strategy()
    use_live, manual_mode, file_train  = ask_mode()

    if file_train:
        # File training only needs the strategy — no balance/bet/speed prompts
        console.print(Panel(
            "[bold green]Ready to train![/bold green]\n\n"
            f"  Strategy : [bold]{strategy_cls().STRATEGY_NAME}[/bold]\n"
            "  Mode     : [green]File Training[/green]\n\n"
            "  [dim]The AI will train on every .txt file in data_store/training_files/[/dim]",
            title="[bold cyan]File Training[/bold cyan]",
            border_style="green",
            padding=(0, 2),
        ))
        console.print()
        Confirm.ask("[bold]Press Enter to start training[/bold]", default=True)
        return {
            "strategy_cls":   strategy_cls,
            "file_train":     True,
            "balance":        0,
            "bet_per_number": 0,
            "use_live":       False,
            "manual_mode":    False,
            "auto_train":     False,
            "spin_interval":  0,
        }

    balance               = ask_balance()
    bet_amount            = ask_bet_amount()
    spin_interval         = 5.0 if (use_live or manual_mode) else ask_sim_speed()
    auto_train            = ask_auto_train()

    show_confirmation(strategy_cls, balance, bet_amount, use_live, auto_train, spin_interval, manual_mode)

    return {
        "strategy_cls":   strategy_cls,
        "balance":        balance,
        "bet_per_number": bet_amount,
        "use_live":       use_live,
        "manual_mode":    manual_mode,
        "auto_train":     auto_train,
        "spin_interval":  spin_interval,
        "file_train":     False,
    }
