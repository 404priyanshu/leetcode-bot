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
    """Plain phase lines for pipes, logs, and terminals without Rich."""

    active = False

    def __init__(self, names=()):
        self.names = list(names)
        self.name = self.names[0] if self.names else ""
        self.position, self.total_accounts = 1, len(self.names)
        self.progress = {
            name: {"done": 0, "target": 0, "state": "Waiting"}
            for name in self.names
        }

    def log(self, line):
        print(line, flush=True)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def account(self, name, position, total):
        self.name, self.position, self.total_accounts = name, position, total
        if name not in self.names:
            self.names.append(name)
        self.set_state(name, "Running", emit=False)
        self.log(f"[batch] Now: {name} · account {position} of {total}")
        self.show_progress()

    def targets(self, targets):
        for name, target in targets.items():
            if name not in self.names:
                self.names.append(name)
            self.progress.setdefault(name, {"done": 0, "target": 0, "state": "Waiting"})
            self.progress[name].update(done=0, target=target, state="Waiting")
        self.show_progress()

    def target(self, count):
        if self.name:
            self.progress.setdefault(
                self.name, {"done": 0, "target": 0, "state": "Running"}
            )["target"] = count

    def selecting(self, difficulties):
        label = "/".join(d.title() for d in difficulties)
        self.log(f"[{self.name}] Problem: Selecting an unsolved {label} problem")

    def problem(self, index, total, number, title, difficulty):
        self.log(f"[{self.name}] Problem: #{number} {title} · {difficulty}")

    def stage(self, text):
        self.log(f"[{self.name}] Step: {text}")

    def accepted(self, count):
        if self.name:
            self.progress[self.name]["done"] = count

    def set_state(self, name, state, emit=True):
        self.progress.setdefault(name, {"done": 0, "target": 0, "state": state})
        self.progress[name]["state"] = state
        if emit:
            self.show_progress()

    def next(self, name, remaining=None, action=None):
        if action:
            detail = action
        elif remaining is None:
            detail = f"{name} · select its next problem"
        else:
            detail = f"{name} in {human_time(remaining)} · all remaining accounts are waiting"
        self.log(f"[batch] Next: {detail}")

    def waiting(self, name):
        self.log(f"[batch] Now: no account active · waiting for {name}")

    def complete(self, stopped=False):
        step = "Run stopped" if stopped else "Run complete"
        self.log("[batch] Now: no account active")
        self.log(f"[batch] Step: {step}")
        self.log("[batch] Next: No further account work scheduled")

    def show_progress(self):
        parts = []
        for name in self.names:
            item = self.progress[name]
            target = item["target"] or "?"
            parts.append(f"{name} {item['done']}/{target} {item['state']}")
        if parts:
            self.log("[batch] Progress: " + " · ".join(parts))

    def countdown(self, remaining, total, hint=""):
        pass

    def clear_countdown(self):
        pass


class RunView(NullView):
    """A restrained live panel showing current work and the account queue."""

    active = True

    def __init__(self, names):
        super().__init__(names)
        self.console = console()
        self.live = None
        self.done = self.want = 0  # compatibility aliases for callers/tests
        self.now_text = "No account active"
        self.current = "No problem selected"
        self.step = "Checking account eligibility"
        self.next_text = "Targets not assigned yet"
        self.wait = ""  # compatibility alias for the countdown text

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
        facts = Table.grid(padding=(0, 1))
        facts.add_column(style="dim", width=9)
        facts.add_column(ratio=1)
        facts.add_row("Now", Text(self.now_text, style="cyan"))
        facts.add_row("Problem", self.current)
        facts.add_row("Step", Text(self.step, style="bold"))
        facts.add_row("Next", self.next_text)

        progress = Table.grid(padding=(0, 2))
        progress.add_column(style="dim", width=max([7] + [len(n) for n in self.names]))
        progress.add_column(justify="right")
        progress.add_column()
        for name in self.names:
            item = self.progress[name]
            target = item["target"] or "?"
            state = item["state"]
            progress.add_row(name, f"{item['done']}/{target}", state)
        return Panel(
            Group(facts, Text("Progress", style="dim"), progress),
            border_style="bright_black", padding=(0, 1),
            width=min(self.console.width, 78),
        )

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
        self.set_state(name, "Running", emit=False)
        item = self.progress[name]
        self.done, self.want = item["done"], item["target"]
        where = f"account {position} of {total}" if total > 1 else "only selected account"
        self.now_text = f"{name} · {where}"
        self.current = "Selecting an unsolved problem"
        self.step = "Opening browser profile"
        self._refresh()

    def targets(self, targets):
        for name, target in targets.items():
            self.progress[name].update(done=0, target=target, state="Waiting")
        if self.name:
            item = self.progress[self.name]
            self.done, self.want = item["done"], item["target"]
        self._refresh()

    def target(self, count):
        self.want = count
        if self.name:
            self.progress[self.name]["target"] = count
        self._refresh()

    def selecting(self, difficulties):
        label = "/".join(d.title() for d in difficulties)
        self.current = f"Selecting an unsolved {label} problem"
        self._refresh()

    def problem(self, index, total, number, title, difficulty):
        self.current = Text(f"#{number} {title} · ")
        self.current.append(difficulty, style=DIFF_STYLE.get(difficulty, "white"))
        self._refresh()

    def stage(self, text):
        self.step = text
        self.wait = ""
        self._refresh()

    def accepted(self, count):
        self.done = count
        if self.name:
            self.progress[self.name]["done"] = count
        self._refresh()

    def set_state(self, name, state, emit=True):
        self.progress[name]["state"] = state
        self._refresh()

    def show_progress(self):
        self._refresh()

    def next(self, name, remaining=None, action=None):
        if action:
            self.next_text = action
        elif remaining is None:
            self.next_text = f"{name} · select its next problem"
        else:
            self.next_text = (
                f"{name} in {_format_short_countdown(remaining)} · all remaining accounts are waiting"
            )
        self._refresh()

    def waiting(self, name):
        self.now_text = f"No account active · waiting for {name}"
        self._refresh()

    def complete(self, stopped=False):
        self.now_text = "No account active"
        self.step = "Run stopped" if stopped else "Run complete"
        self.next_text = "No further account work scheduled"
        self._refresh()

    def countdown(self, remaining, total, hint="", name=None):
        who = name or "Current account"
        self.next_text = f"{who} in {_format_short_countdown(remaining)} {hint}".rstrip()
        self.wait = self.next_text
        self._refresh()

    def clear_countdown(self):
        self.wait = ""
        self._refresh()


def _format_short_countdown(seconds):
    minutes, seconds = divmod(max(0, int(seconds)), 60)
    return f"{minutes}:{seconds:02d}"


def build(names):
    return RunView(names) if supported() else NullView(names)
