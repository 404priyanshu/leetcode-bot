"""Live run view: what is happening now, and what is still to come.

Everything here degrades to plain printing when Rich is missing or output is
not a terminal, so cron and Hermes logs keep their existing line-per-event
shape.
"""
import os
import sys

try:
    from rich.console import Console, Group
    from rich.panel import Panel
    from rich.table import Table
    from rich.text import Text
    from rich.live import Live
except ImportError:  # plain output is always acceptable
    Console = None

BAR_WIDTH = 24
DIFF_STYLE = {"Easy": "green", "Medium": "yellow", "Hard": "red"}
SECONDS_PER_PROBLEM = 40  # fetch, run the examples, submit
MIN_GAP, MAX_GAP = 3, 12  # kept in step with leetcode_bot


def supported():
    return Console is not None and sys.stdout.isatty() and sys.stdin.isatty()


def console():
    # Windows terminals need explicit legacy handling for box drawing.
    return Console(highlight=False, soft_wrap=False, legacy_windows=os.name == "nt")


def human_time(seconds):
    minutes = int(round(seconds / 60))
    if minutes < 60:
        return f"{max(minutes, 1)} min"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} h" if not minutes else f"{hours} h {minutes} min"


def estimate(accounts, per_account, timing):
    """Low and high estimates in seconds, so the range stays honest."""
    problems = accounts * per_account
    work = problems * SECONDS_PER_PROBLEM
    if timing != "human":
        return work, work
    gaps = accounts * max(per_account - 1, 0)
    return work + gaps * MIN_GAP * 60, work + gaps * MAX_GAP * 60


def bar(done, total, width=BAR_WIDTH):
    filled = 0 if not total else min(width, round(width * done / total))
    return "█" * filled + "░" * (width - filled)


def plan_lines(names, per_account, difficulties, timing):
    """The pre-run summary: who runs, how much, and how long it may take."""
    label = "random, 1-9" if per_account is None else str(per_account)
    assumed = 5 if per_account is None else per_account
    low, high = estimate(len(names), assumed, timing)
    rows = [(name, f"{label} problems") for name in names]
    total = f"{len(names) * assumed} problems" if per_account else "about 5 per account"
    span = human_time(low) if low == high else f"{human_time(low)} – {human_time(high)}"
    return rows, [
        f"{total} · {'+'.join(difficulties)} · "
        + ("human-like pacing" if timing == "human" else "no waiting"),
        f"roughly {span}",
    ]


class NullView:
    """No dashboard: messages print as they always did."""

    active = False

    def log(self, line):
        print(line, flush=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def account(self, name, position, total):
        pass

    def target(self, count):
        pass

    def problem(self, index, total, number, title, difficulty):
        pass

    def stage(self, text):
        pass

    def accepted(self, count):
        pass

    def countdown(self, remaining, total, hint=""):
        pass

    def clear_countdown(self):
        pass


class RunView(NullView):
    """A panel pinned below the log showing progress and the current step."""

    active = True

    def __init__(self, names):
        self.names = list(names)
        self.console = console()
        self.live = None
        self.name = names[0] if names else ""
        self.position, self.total_accounts = 1, len(names)
        self.done = self.want = 0
        self.current = ""
        self.step = "starting ..."
        self.wait = ""

    def __enter__(self):
        self.live = Live(self._render(), console=self.console,
                         refresh_per_second=8, transient=False)
        self.live.__enter__()
        return self

    def __exit__(self, *exc):
        live, self.live = self.live, None
        if live:
            live.update(self._render())
            return live.__exit__(*exc)
        return False

    def _render(self):
        head = Table.grid(padding=(0, 1))
        head.add_column(style="bold cyan")
        head.add_column(style="dim")
        where = (f"account {self.position} of {self.total_accounts}"
                 if self.total_accounts > 1 else "")
        head.add_row(self.name, where)
        body = Table.grid(padding=(0, 1))
        body.add_column()
        body.add_column()
        body.add_row(
            Text(bar(self.done, self.want), style="green"),
            Text(f"{self.done}/{self.want} accepted" if self.want else "getting ready",
                 style="bold"),
        )
        rows = [head, body]
        if self.current:
            rows.append(self.current)
        rows.append(Text(self.wait or self.step, style="yellow" if self.wait else "dim"))
        return Panel(Group(*rows), border_style="cyan", padding=(0, 1),
                     width=min(self.console.width, 76))

    def _refresh(self):
        if self.live:
            self.live.update(self._render())

    def log(self, line):
        if self.live:
            self.live.console.print(Text.from_ansi(line))
        else:
            print(line, flush=True)

    def account(self, name, position, total):
        self.name, self.position, self.total_accounts = name, position, total
        self.done = self.want = 0
        self.current, self.wait, self.step = "", "", "starting ..."
        self._refresh()

    def target(self, count):
        self.want = count
        self._refresh()

    def problem(self, index, total, number, title, difficulty):
        self.current = Text(f"{index}/{total}  #{number} {title} · ")
        self.current.append(difficulty, style=DIFF_STYLE.get(difficulty, "white"))
        self._refresh()

    def stage(self, text):
        self.step, self.wait = text, ""
        self._refresh()

    def accepted(self, count):
        self.done = count
        self._refresh()

    def countdown(self, remaining, total, hint=""):
        minutes, seconds = divmod(int(remaining), 60)
        self.wait = f"next question in {minutes}:{seconds:02d}  {hint}".rstrip()
        self._refresh()

    def clear_countdown(self):
        self.wait = ""
        self._refresh()


def build(names):
    return RunView(names) if supported() else NullView()
