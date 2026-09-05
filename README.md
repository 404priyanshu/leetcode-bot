# LeetCode Bot

This script chooses unsolved LeetCode problems, gets Python solutions from
[walkccc.me](https://walkccc.me/LeetCode/), runs the example tests, and submits
solutions through your logged-in LeetCode account.

> **Note:** This automates submissions using third-party solutions. Make sure
> that this use is acceptable to you and complies with LeetCode's rules before
> running it. Automated activity can put an account at risk.

## Requirements

- macOS or Linux
- Python 3.10 or newer
- Google Chrome
- A LeetCode account

## Install

From this folder, create a virtual environment and install Playwright:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
python -m playwright install chrome
```

The last command installs the Chrome browser that Playwright needs. If Chrome
is already installed, Playwright may reuse or update that installation.

## Log in once

```bash
source .venv/bin/activate
python leetcode_bot.py --setup
```

A Chrome window will open. Log in to LeetCode in that window, return to the
terminal, and press Enter. The login session is saved in `leetcode_profile/`
for future runs.

Run this setup command again if the bot later reports that the session has
expired.

## Run the bot

For the simplest experience, start the interactive menu:

```bash
source .venv/bin/activate
python leetcode_bot.py
```

Use the arrow keys to choose the difficulty, number of problems, and timing.
Press Enter to select, Space to toggle difficulty choices, or `q` to quit.

You can also run it directly with command-line options:

```bash
# Solve 1 easy problem without waiting
python leetcode_bot.py --difficulty easy --count 1 --instant

# Solve 3 medium or hard problems without waiting
python leetcode_bot.py --difficulty medium,hard --count 3 --instant

# Use all difficulties and human-like timing
python leetcode_bot.py --difficulty all --count 5 --no-menu
```

Human-like timing is the default. In non-interactive mode it can wait up to
three hours before starting, then waits 3–12 minutes after a real test or
submission attempt. Missing solutions and non-network errors before testing move
directly to the next candidate. Use `--instant` to disable these timing waits;
network recovery backoff still applies.

### Options

| Option | Meaning |
| --- | --- |
| `--setup` | Open Chrome and save a LeetCode login |
| `--setup-telegram` | Configure optional Telegram notifications |
| `--export-report` | Rebuild the Excel report from the structured history |
| `--difficulty easy` | Use easy problems; also accepts `medium`, `hard`, `all`, or a comma-separated list |
| `--count N` | Target `N` accepted submissions; `N` must be a positive integer |
| `--instant` | Skip the startup delay and gaps between problems |
| `--no-menu` | Run without the interactive menu, useful for scheduled jobs |
| `--help` | Show command-line help |

If `--count` is omitted, the bot chooses a random daily target. If
`--difficulty` is omitted, it uses easy problems.

Invalid counts or difficulty names are rejected before opening a browser or
changing history. A run stops at its target, when its candidate pool is empty,
or after `N + 10` selected problems. The target is therefore best effort.

Only one bot command can use this folder at a time, including login setup and
report export. A second command exits with a clear message. The `.bot.lock`
file can remain on disk after a run; the operating system releases its lock
when the process exits, including after a crash. Do not delete the lock file
while a bot command is running.

Exit codes are `0` for a completed run, `1` for a partial/failed run or busy
profile, `2` for invalid command-line arguments, and `130` for Ctrl+C.

## Optional Telegram notifications

Run:

```bash
python leetcode_bot.py --setup-telegram
```

Follow the prompts to enter a Telegram bot token and chat ID. The settings are
saved in `telegram.json`. Alternatively, set both environment variables before
starting the bot:

```bash
export TELEGRAM_BOT_TOKEN="your-bot-token"
export TELEGRAM_CHAT_ID="your-chat-id"
python leetcode_bot.py
```

Environment variables take priority over `telegram.json`.

## Files created while running

- `leetcode_profile/` — saved browser login; keep it private
- `solved.json` — problem IDs successfully submitted by the bot
- `activity.log` — run history and errors
- `attempts.db` — durable SQLite history for every session and problem attempt
- `leetcode_report.xlsx` — color-coded report rebuilt after each session
- `bot_state.json` — saved wait times and temporary API cooldown state
- `telegram.json` — Telegram credentials, if configured; keep it private

Do not commit `leetcode_profile/` or `telegram.json`, because they contain
private login information or credentials.

## History and Excel report

Every selected problem is recorded in `attempts.db` with its problem details,
stage reached, outcome, exact failure reason, status, runtime, submission ID,
and duration. Stages and submission IDs are saved while an attempt is in progress,
so they survive interruptions during polling. Each run gets a session record
before startup waits, browser launch, login checks, and problem discovery, so
failures at those stages are included. Existing IDs in `solved.json` without
recorded acceptances are preserved separately as historical IDs because their
original dates and details are not known.

After every run, the bot regenerates `leetcode_report.xlsx` with four sheets:

- `Summary` — totals by outcome with a color legend
- `Attempts` — one filterable row per selected problem
- `Sessions` — one row per run with result counts
- `Historical solved IDs` — older IDs imported from `solved.json`

Accepted rows are green. Missing solutions are blue, test failures yellow,
submission failures orange, rate limits purple, errors red, and interruptions
gray. Network failures have their own outcome and session count. To rebuild
the workbook without running the solver:

```bash
python leetcode_bot.py --export-report
```

SQLite is the source of truth, so no history is lost if the Excel file is open
or temporarily cannot be replaced. Close the workbook and run
`--export-report` again. If the bot process crashes, its unfinished session and
attempt are marked as interrupted when the next run starts.
Recovery runs only after obtaining exclusive ownership of the profile and
history. Starting a database session by itself does not interrupt other records.
Accepted IDs in SQLite are also used for filtering if a previous run was
interrupted before updating `solved.json`. JSON progress writes are atomic.

## Troubleshooting

**`ModuleNotFoundError: No module named 'playwright'`**

Activate the virtual environment and install the dependency:

```bash
source .venv/bin/activate
python -m pip install -r requirements.txt
```

**Chrome does not open or Playwright cannot find it**

```bash
python -m playwright install chrome
```

**`csrftoken not found in cookies - not logged in?`**

Refresh the saved login:

```bash
python leetcode_bot.py --setup
```

**The API circuit breaker is open**

LeetCode returned a `403` or `429` response. After one retry for a Cloudflare
challenge, the bot stops and pauses requests for about 30 minutes. Wait for the
cooldown instead of repeatedly restarting it.

**Skip a wait or stop a run**

Interactive waits show a progress bar and an `MM:SS` countdown that updates
once per second. Press `s` to skip that wait, or press Ctrl+C at any time to
stop the entire session cleanly. Scheduled runs do not print the live countdown
because it would add hundreds of nearly identical lines to their logs.
An interrupted gap is saved and resumed on the next human-timing run. Instant
mode clears saved gaps, but does not bypass an API cooldown.

**`ERR_NETWORK_CHANGED`, `ERR_CONNECTION_REFUSED`, or a temporary server error**

Read-only operations retry the same request up to four times, waiting 5, 15,
and 30 seconds between tries. This applies to page loads, GraphQL reads, and
result polling. Network retries remain enabled in instant mode. If the outage
persists, the run stops with `Network error`, preserving accepted submissions
and recording one failed attempt instead of burning through more candidates.
Restore connectivity and start a new run when ready.

Submission and test POSTs are not automatically resent after an ambiguous
network failure or server error. A submission may already have reached LeetCode.
Check its submission history before rerunning; a received submission ID is
retained in the report even if polling fails. A completed result that never
arrives is reported as an explicit timeout, rather than an unknown test failure.

**No Python solution found**

The scraper reads inactive language tabs, preserves indentation, and validates
Python syntax before selecting a snippet. A genuine 404 or page with no usable
Python solution is skipped; some pages only contain another language. Page-load
timeouts and server failures are recorded separately from missing solutions.

## Development and tests

The code is split between `leetcode_bot.py` (CLI and session workflow),
`runtime.py` (process lock and safe read retries), and `reporting.py` (SQLite
history and Excel generation). Dependencies are pinned in `requirements.txt`.

Run the unit and regression tests without accessing your account:

```bash
python -m unittest discover -s tests -v
```

Include the real-browser tests with an isolated, temporary Chromium profile:

```bash
python -m playwright install chromium
BROWSER_TESTS=1 python -m unittest discover -s tests -v
```

To use an already installed Google Chrome instead, set both `BROWSER_TESTS=1`
and `BOT_TEST_CHROME=1`. Browser tests intercept all page requests and use local
fixtures; they never log in or submit solutions. Tests use temporary databases
and reports. GitHub Actions runs the full suite on supported Python versions
for pushes and pull requests.
