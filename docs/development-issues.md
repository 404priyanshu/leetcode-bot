# Development issues and resolutions

This log records issues encountered while adapting the bot for Windows 11.
It separates observed results from assumptions so future changes do not undo
working behavior.

## Cloudflare verification failed in the automated Windows browser

**Status:** Manual setup verification resolved and confirmed by the user on
2026-09-08. Later automated runs are not covered by that confirmation.

### Symptoms and diagnosis

The LeetCode signup/login flow failed Cloudflare verification in the bot's
browser on Windows. The user confirmed that verification worked in Chrome
opened normally and failed only in the bot's browser. No Cloudflare error code
or Ray ID was supplied.

That comparison pointed to the automated browser environment as the likely
cause. It did not identify a particular CDP command, browser flag, or other
individual signal as the cause.

### What we tried first

The Windows compatibility change replaced Playwright with Patchright, used
installed Google Chrome in headed mode, retained native headers and user agent,
kept images enabled, and stored the profile at
`%LOCALAPPDATA%\leetcode-bot\profile`. Daily runs used an off-screen window;
setup used an on-screen window. Randomized pauses were also added.

These changes did **not** resolve verification during setup: `--setup` still
launched Chrome through the automation framework. A persistent profile and a
headed window alone were insufficient in this case.

The old fallback that cleared the Cloudflare cooldown and restarted the whole
session was removed. Persistent blocks stop the run instead of triggering that
restart.

### Fix that worked

Commit [`a128a14`](https://github.com/404priyanshu/leetcode-bot/commit/a128a14)
changed Windows `--setup` to launch ordinary installed Chrome through
`subprocess.run`, without Patchright controlling it or a remote-debugging
connection. The implementation is in [windows/login.py](../windows/login.py).

The user completes verification and login manually. Chrome uses the same
persistent bot profile as subsequent runs. The setup command waits for Chrome
to exit while retaining the bot's process lock. The user closes all windows of
that profile before starting a daily run.

Setup does not claim to verify authentication programmatically. The next bot
run checks login before selecting problems. Daily automation still uses
Patchright; only Windows manual setup changed.

After pulling this fix and trying setup, the user reported:

> verification passes perfectly now!

### Recovery procedure

Stop the bot and close its browser, then run from the repository in PowerShell:

```powershell
git pull
.\.venv\Scripts\python.exe leetcode_bot.py --setup
```

Complete verification and login, then close the setup Chrome windows before
Hermes starts another run. Keep the existing profile; do not delete cookies as
a routine troubleshooting step.

### Lessons and verification limits

- Compare ordinary Chrome with the automated browser before attributing a
  challenge failure to the network or account.
- Keep Windows manual login independent of the automation framework. Do not
  replace this path with an automated launch without checking this regression.
- A successful manual login does not guarantee that future automated sessions
  will never be challenged. Preserve the cooldown and visible recovery path.
- Regression tests cover the Windows setup dispatch, profile argument, absence
  of remote-debugging arguments, and missing-Chrome error. At the time of this
  fix, 51 non-browser tests passed; six optional browser tests were skipped.
  The user's Windows test supplied the real verification result.

## Unix-only code prevented Windows compatibility

The original implementation imported `fcntl`, `termios`, and `tty`
unconditionally and used Unix terminal input handling. These are not portable
to Windows.

Windows now uses `msvcrt` for keyboard input and byte-range process locking;
macOS/Linux retain their Unix implementations. The Windows browser profile has
a fixed per-user location, and the lock is shared across repository copies.
Console output and logs use UTF-8 to handle status symbols reliably.

These changes were included in
[`5f89114`](https://github.com/404priyanshu/leetcode-bot/commit/5f89114).
Windows was added to the CI matrix; adding that matrix is not itself evidence
that a Windows CI run or every laptop-specific behavior passed.

## Daily execution with the laptop lid closed

Headed Chrome requires a signed-in desktop session. Closing the lid can suspend
the laptop independently of the bot. The setup guide documents plugged-in lid
behavior of **Do nothing** and sleep/hibernate settings of **Never**. Firmware,
power loss, updates, or organization policy can still interrupt execution.

We initially supplied a Task Scheduler installer. The user clarified that
Hermes handles the daily trigger, so the installer was removed. Hermes launches
[windows/run_daily.py](../windows/run_daily.py), which records timestamped
output and preserves the bot's exit code. Use one scheduler and avoid automatic
retries after an interrupted submission, since it may already have reached
LeetCode.

See the [Windows setup guide](../windows/README.md) for the current commands.
Lid-closed reliability is separate from the confirmed manual verification fix.

## API submission did not need a separate GraphQL rewrite

Inspection showed that the bot already submitted through authenticated in-page
`fetch`, with cookies and CSRF, rather than editor clicks. Metadata uses
GraphQL; test and submission actions use the existing REST endpoints. We kept
that implementation instead of introducing a second HTTP client or an assumed
GraphQL submission mutation.

## Multiple accounts need both profile and history isolation

Adding two more logins cannot safely reuse the original profile or solved list.
The multi-account implementation keeps `default` in its existing locations and
creates separate named-account profiles, identity records, progress databases,
reports, cooldowns, and locks. Expected usernames are checked before problem
selection to catch logging into the wrong account. Account names cannot be
rebound to a different username without choosing a new name.

Hermes runs one sequential batch with shared count/difficulty settings. Individual
account failures can continue, but a shared network or Cloudflare failure stops
the batch; switching accounts is not used to continue through blocking. Startup
jitter runs once, and there are no automatic submission retries. Manual Windows
setup continues using ordinary Chrome, preserving the confirmed verification fix.

Automated validation uses isolated fixtures and mocked submissions. Logging into
the two real additional accounts and verifying their usernames remains a manual
acceptance step; no successful live multi-account run is claimed here.
