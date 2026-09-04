# LeetCode Bot

This script chooses unsolved LeetCode problems, gets Python solutions from
[walkccc.me](https://walkccc.me/LeetCode/), runs the example tests, and submits
solutions through your logged-in LeetCode account.

> **Note:** This automates submissions using third-party solutions. Make sure
> that this use is acceptable to you and complies with LeetCode's rules before
> running it. Automated activity can put an account at risk.

## Requirements

- macOS or Linux
- Python 3
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
submission attempt. Missing solutions and failures before testing move directly
to the next candidate. Use `--instant` to disable all of these waits.

### Options

| Option | Meaning |
| --- | --- |
| `--setup` | Open Chrome and save a LeetCode login |
| `--setup-telegram` | Configure optional Telegram notifications |
| `--export-report` | Rebuild the Excel report from the structured history |
| `--difficulty easy` | Use easy problems; also accepts `medium`, `hard`, `all`, or a comma-separated list |
| `--count N` | Try to get exactly `N` accepted submissions |
| `--instant` | Skip the startup delay and gaps between problems |
| `--no-menu` | Run without the interactive menu, useful for scheduled jobs |
| `--help` | Show command-line help |

If `--count` is omitted, the bot chooses a random daily target. If
`--difficulty` is omitted, it uses easy problems.

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
and duration. Each run also gets a session record with its target and stop
reason. Existing IDs in `solved.json` are preserved separately as historical
IDs because their original dates and details are not known.

After every run, the bot regenerates `leetcode_report.xlsx` with four sheets:

- `Summary` — totals by outcome with a color legend
- `Attempts` — one filterable row per selected problem
- `Sessions` — one row per run with result counts
- `Historical solved IDs` — older IDs imported from `solved.json`

Accepted rows are green. Missing solutions are blue, test failures yellow,
submission failures orange, rate limits purple, errors red, and interruptions
gray. To rebuild the workbook without running the solver:

```bash
python leetcode_bot.py --export-report
```

SQLite is the source of truth, so no history is lost if the Excel file is open
or temporarily cannot be replaced. Close the workbook and run
`--export-report` again. If the bot process crashes, its unfinished session and
attempt are marked as interrupted when the next run starts.

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
