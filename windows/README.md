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

   Log in and complete any verification in the visible Chrome window, then
   press Enter in PowerShell. Login is verified before setup reports success.
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
