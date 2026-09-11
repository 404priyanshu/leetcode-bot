# Windows 11 setup

1. Install Python 3.10+ (64 bit) and Google Chrome. Copy/clone this repository
   to a permanent writable folder, for example `C:\Users\YOUR_NAME\leetcode-bot`.
   Do not copy a macOS/Linux virtual environment or browser profile. Open
   PowerShell as your usual Windows user and change to that folder.

2. Create the environment and install dependencies:

   ```powershell
   py -3 -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

   Chrome should already be installed. If it is missing, install it normally or run
   `.\.venv\Scripts\python.exe -m patchright install chrome`.
   No activation script is needed.

3. Save your login on this Windows machine:

   ```powershell
   .\.venv\Scripts\python.exe leetcode_bot.py --setup
   ```

   Setup opens ordinary installed Chrome without Patchright or a debugging
   connection. Log in and complete verification manually, then close all windows
   of this bot profile. Keep the command running until Chrome closes, so the
   profile lock stays held. Setup retains the profile but does not verify login;
   the next bot run checks it before selecting any problems.
   Cookies live in `%LOCALAPPDATA%\leetcode-bot\profile`, independent of the
   checkout and current working directory. This is a dedicated profile; do not
   share it with another Chrome instance, delete it between runs, or sync it
   between machines. Re-run setup if login expires. Other history files remain
   beside `leetcode_bot.py`; preserve these when updating the project.

4. Configure lid and power behavior while **plugged in**:

   - Control Panel → Hardware and Sound → Power Options → Choose what closing
     the lid does → Plugged in → **Do nothing** → Save changes.
   - Settings → System → Power & battery → Screen, sleep & hibernate timeouts:
     set sleep and hibernate while plugged in to **Never**. Display-off is fine.
   - Alternatively, in an elevated PowerShell terminal set the AC timeouts:

     ```powershell
     powercfg /change standby-timeout-ac 0
     powercfg /change hibernate-timeout-ac 0
     ```

   Keep the laptop ventilated and plugged in. Battery behavior is unchanged;
   the runner does not enforce AC-only execution or configure wake timers. Firmware, organization policy, Windows updates, loss of
   power, or shutdown can still interrupt runs. Sign back in after a reboot.

5. Test interactively (this **submits one solution**):

   ```powershell
   .\.venv\Scripts\python.exe leetcode_bot.py --no-menu --count 1 --instant --visible-browser
   ```

   Then test the scheduled entry point with the lid closed (also submits one):

   ```powershell
   .\.venv\Scripts\python.exe windows\run_daily.py --count 1 --instant
   ```

   Normal runs use headed Chrome (`headless=False`), a 1365×900 window,
   and `--window-position=-32000,-32000` on Windows. `--visible-browser` and
   `--setup` position it on screen. Native Chrome headers, user agent and images
   are retained. Patchright replaces Playwright; no separate CDP service is needed.
   Human timing includes small randomized pauses before tests/submission plus
   the existing startup jitter (up to three hours) and gaps between problems.
   `--instant` skips all those delays.

6. Have Hermes launch the logging wrapper once daily. In PowerShell, use
   absolute paths, replacing `YOUR_NAME` with your Windows username:

   ```powershell
   & 'C:\Users\YOUR_NAME\leetcode-bot\.venv\Scripts\python.exe' 'C:\Users\YOUR_NAME\leetcode-bot\windows\run_daily.py' --count 1
   ```

   Give Hermes these instructions:

   > Run this command once daily under my signed-in Windows account, using the
   > native Windows Python environment. Wait for completion and inspect the exit
   > code and daily log. Allow enough time for startup jitter (up to three hours)
   > plus the run itself. Do not automatically retry failed or interrupted runs:
   > a submission may already have reached LeetCode. Report failures requiring
   > login or verification so I can run `--setup` visibly.

   The wrapper sets its own working directory and targets one accepted easy
   problem. Change `--count 1` or add `--difficulty medium` as needed. `--instant`
   skips startup jitter and all other randomized delays.

   Hermes must launch the process in your signed-in Windows desktop session,
   under the same user who completed setup. Stay signed in; the session may be
   locked. A service running as SYSTEM, a container, or WSL does not provide this
   native headed-browser setup. Test locked/lid-closed behavior on the actual
   laptop: graphics drivers and device policy can affect it. Hermes handles the
   daily trigger; no Task Scheduler installation is required. The process lock
   protects the shared Windows profile across repository copies.

7. Inspect the logs:

   ```powershell
   Get-Content ('.\logs\run-' + (Get-Date -Format 'yyyy-MM-dd') + '.log') -Tail 60
   ```

   `logs\run-YYYY-MM-DD.log` appends timestamped stdout/stderr, START and END
   records, and the process exit code. `activity.log`, `attempts.db`, and
   `leetcode_report.xlsx` retain the existing history. Exit 0 means completed;
   1 means unsuccessful/partial; 130 means interrupted. An abrupt shutdown can
   leave a START without END. The wrapper returns the bot's exit code to Hermes.
   Logs accumulate; archive or remove old daily log files periodically.

   If you previously installed the Task Scheduler task, remove it to avoid a
   second daily launch:

   ```powershell
   Unregister-ScheduledTask -TaskName 'LeetCode Bot Daily' -Confirm:$false
   ```

## When Windows signup/login verification fails

Run `--setup` from PowerShell in your signed-in Windows desktop. The setup flow
opens the login page for your existing account; it does not create an account.
Do not launch a daily run while this Chrome window is open.

If verification still fails in this ordinary Chrome window, check whether it
also fails in Chrome opened from the Start menu. Update Chrome, confirm that
JavaScript is enabled and extensions/network filters are not blocking challenge
resources, and record the displayed error code and Ray ID. If ordinary browsing
also fails, contact LeetCode support with those details; changing the bot alone
may not fix it. See [Cloudflare's troubleshooting guide](https://developers.cloudflare.com/cloudflare-challenges/troubleshooting/challenge-solve-issues/).

Manual login is not a promise of automatic verification on later bot runs.
Cloudflare [does not support automation frameworks for production challenges](https://developers.cloudflare.com/cloudflare-challenges/reference/supported-browsers/).

## Verification and API behavior

Persistence and Patchright can improve browser compatibility, but cannot ensure
Cloudflare never challenges a session or clears a challenge automatically. The
bot waits briefly for a page to clear itself; a persistent block stops the run
with a cooldown and a recovery instruction. Use `--setup` to complete verification
visibly. Repeatedly restarting during the cooldown does not help.

Submission already uses authenticated in-page `fetch` with cookies and CSRF,
not editor clicks. Problem metadata uses `/graphql`; test and submission actions
use LeetCode's existing REST endpoints. There is no need to invent a GraphQL
submission mutation or export session secrets to a second HTTP client. Writes
are not blindly retried after a connection failure. These internal endpoints
can change, so live Windows testing with your account remains necessary.

Implementation reference: [Patchright setup](https://github.com/Kaliiiiiiiiii-Vinyzu/patchright-python).

Power references: [powercfg options](https://learn.microsoft.com/en-us/windows-hardware/design/device-experiences/powercfg-command-line-options), [lid settings](https://learn.microsoft.com/en-us/windows-hardware/customize/power-settings/power-button-and-lid-settings).

## Three accounts with Hermes

The original account is named `default`. Existing commands, its Chrome profile,
and its progress files stay in place; you do not need to log in again.

Register each additional account from the menu, by running the bot with no
flags and choosing **Add account**:

```powershell
.\.venv\Scripts\python.exe leetcode_bot.py
```

The menu lists each account with its username, solved count and last run.
**Accounts** selects which of them a run uses, and **Log in again** reopens the
browser for one of them. A menu run works through the selected
accounts one at a time, using the same stop rules as the Hermes batch below.

The equivalent commands, for scripting or a session without a terminal:

```powershell
.\.venv\Scripts\python.exe leetcode_bot.py --account account2 --setup
.\.venv\Scripts\python.exe leetcode_bot.py --account account3 --setup
```

Each new setup asks for the expected **LeetCode username** (not email or display
name), then opens ordinary Chrome with a separate profile. Log into that account,
complete verification manually, and close the profile's Chrome windows before
continuing. Setup stores the expected username; the next automated run verifies
it before selecting a question. A mismatch stops that account. Existing names
cannot be rebound to different usernames: use a new name to avoid mixing history.
For `default`, the first authenticated run records the username already logged in.

Additional accounts live under `%LOCALAPPDATA%\leetcode-bot\accounts\NAME`:
`profile`, `account.json`, `solved.json`, `bot_state.json`, `attempts.db`,
`leetcode_report.xlsx`, `activity.log`, `logs`, and an individual process lock.
The default identity record is `%LOCALAPPDATA%\leetcode-bot\default-account.json`.
On macOS/Linux additional accounts live in the repository's `accounts/NAME`.
Account directories and identity records are ignored by Git. No cookies, solved
history, or cooldowns are copied between accounts. Account names use lowercase
letters, digits, underscores and hyphens, start with a letter, and have at most
32 characters; Windows reserved names are rejected.

Have Hermes run this single command daily from the repository (or use absolute
paths as shown earlier):

```powershell
.\.venv\Scripts\python.exe windows\run_daily.py --accounts default account2 account3 --count 1 --difficulty easy
```

By default the first account waits a random delay of up to three hours after the
scheduled time, so the run does not begin at the same minute every day. Add
`--no-jitter` when the run must start when the scheduler fires:

```powershell
.\.venv\Scripts\python.exe windows\run_daily.py --accounts default account2 --count 1 --no-jitter
```

That trades the delay for punctuality: a run that starts at exactly the same
time daily is easier to recognise as automated.

The count applies **to each account**, so this targets three accepted solutions
total. Each account independently chooses unsolved questions; accounts may choose
the same question. Omit `--count` to let each account use the bot's built-in
random daily target; omit `--difficulty` to keep the default easy difficulty.
The runner validates all names up front and uses one scheduler process to rotate
eligible accounts after each problem attempt. Only one browser profile runs at
a time. Startup jitter applies once before the rotation; per-account human-mode
cooldowns remain enabled, and another eligible account runs instead of idling.
`--instant` skips all waits. Separate account locks prevent profile collisions;
a shared batch lock prevents overlapping Hermes wrappers.

An expired login, username mismatch, or other account-specific failure allows
the next account to run. Cloudflare/rate limiting, a network outage, interruption,
or failed browser cleanup stops the batch and marks remaining accounts skipped.
No failed submission is automatically replayed. Inspect logs before retrying.
A forced stop may require manually closing the bot's Chrome windows.

Per-account daily logs are in that account's `logs` directory. The default keeps
its existing repository `logs/run-YYYY-MM-DD.log` location. A repository
`logs/batch-YYYY-MM-DD.log` records each outcome and a completed/failed/skipped
summary, also printed for Hermes. The wrapper returns 0 only if all accounts
complete, 1 for failures/skips, or 130 for user interruption. Child exit 75 in a
log means the batch must stop for a shared failure; this is an internal runner
signal, not an instruction to retry. Ordinary direct bot commands retain their
existing exit codes. Telegram uses the existing shared destination, with account
names included in session messages.

For an individual account or report export:

```powershell
.\.venv\Scripts\python.exe leetcode_bot.py --account account2 --no-menu --count 1 --instant
.\.venv\Scripts\python.exe leetcode_bot.py --account account2 --export-report
```

The first command submits a solution. Confirm each expected username during
setup before asking Hermes to run the whole batch. Reports remain separate and
show the account name in the workbook heading.
