#!/usr/bin/env python3
"""Daily LeetCode solver: pulls Python solutions from walkccc.me, runs them
against LeetCode's test cases, and submits if they pass.

Run bare (`python leetcode_bot.py`) for an interactive arrow-key menu to pick
difficulty, problem count and timing. Flags (--count, --instant, --difficulty,
--no-menu) configure the same things non-interactively for cron jobs.
"""

import argparse
import ast
import io
import json
import math
import os
import random
import re
import shutil
import sys
import signal
import time
import tokenize
import urllib.request
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path
if os.name == "nt":
    import msvcrt
else:
    import termios
    import tty
    from select import select as _fd_select

from patchright.sync_api import sync_playwright

import reporting
import accounts
import dashboard
from runtime import (
    AlreadyRunning, NetworkUnavailable, TransientReadError,
    exclusive_run, is_network_error, retry_read,
)

BASE = Path(__file__).resolve().parent
# Keep Windows cookies independent of the checkout and scheduler working dir.
PROFILE_DIR = (
    Path(os.environ.get("LOCALAPPDATA", str(Path.home() / "AppData" / "Local")))
    / "leetcode-bot" / "profile"
    if os.name == "nt" else BASE / "leetcode_profile"
)
SOLVED_FILE = BASE / "solved.json"  # progress tracker
LOG_FILE = BASE / "activity.log"
TELEGRAM_FILE = BASE / "telegram.json"  # {"bot_token": ..., "chat_id": ...}
STATE_FILE = BASE / "bot_state.json"  # persistent waits + API circuit breaker
HISTORY_DB = BASE / "attempts.db"  # durable structured history
REPORT_FILE = BASE / "leetcode_report.xlsx"  # regenerated from HISTORY_DB
LOCK_FILE = PROFILE_DIR.parent / ".leetcode-bot.lock" if os.name == "nt" else BASE / ".bot.lock"

ACCOUNT_NAME = "default"
VIEW = dashboard.NullView()  # replaced by a live panel for interactive runs

STAGE_TEXT = {
    "Selected": "Selecting a problem",
    "Solution fetch": "Fetching solution",
    "Problem metadata": "Opening problem on LeetCode",
    "Example tests": "Running example tests",
    "Submission": "Submitting to LeetCode",
}


def configure_account(name):
    global ACCOUNT_NAME, PROFILE_DIR, SOLVED_FILE, LOG_FILE, STATE_FILE
    global HISTORY_DB, REPORT_FILE, LOCK_FILE
    selected = accounts.paths(name)
    ACCOUNT_NAME = name
    PROFILE_DIR, LOCK_FILE = selected["profile"], selected["lock"]
    data = selected["data"]
    SOLVED_FILE, LOG_FILE = data / "solved.json", data / "activity.log"
    STATE_FILE, HISTORY_DB = data / "bot_state.json", data / "attempts.db"
    REPORT_FILE = data / "leetcode_report.xlsx"


WALKCCC_URL = "https://walkccc.me/LeetCode/problems/{num}/"  # keyed by problem number
LEETCODE = "https://leetcode.com"

# ---- "human" knobs -------------------------------------------------------
MIN_PROBLEMS, MAX_PROBLEMS = 4, 9  # problems per day
MIN_GAP, MAX_GAP = 3, 12  # minutes between problems
START_JITTER_HOURS = 3  # up to N hours random start offset (non-interactive only)
SKIP_DAY_CHANCE = 0.05  # small chance of a light/skipped day
LOOSE_DAY_CHANCE = 0.15  # small chance of a lazy 1-2 problem day
BLOCKED_STATUSES = {403, 429}
BREAKER_THRESHOLD = 3  # blocked responses within the rolling window
BREAKER_WINDOW_SECONDS = 15 * 60
BREAKER_COOLDOWN_SECONDS = 30 * 60
# -------------------------------------------------------------------------

DIFFICULTIES = ("easy", "medium", "hard")
DIFF_LABEL = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}

# JS fetch snippet used for all LeetCode API calls (runs in the real page,
# so cookies + fingerprint are identical to normal browsing)
FETCH_JS = """
async ([url, method, body, csrf, referrer]) => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 30000);
    try {
        const r = await fetch(url, {
            method: method,
            headers: {
                'content-type': 'application/json',
                'x-csrftoken': csrf,
            },
            body: body,
            credentials: 'include',
            referrer: referrer,
            signal: controller.signal,
        });
        return {status: r.status, text: await r.text()};
    } catch (error) {
        if (error.name === 'AbortError') throw new Error('Failed to fetch: timeout');
        throw error;
    } finally {
        clearTimeout(timer);
    }
}
"""

# ---------------- console output -------------------------------------------


def _col(text, code):
    return f"\x1b[{code}m{text}\x1b[0m" if sys.stdout.isatty() else str(text)


def bold(t):
    return _col(t, "1")


def dim(t):
    return _col(t, "2")


def green(t):
    return _col(t, "32")


def yellow(t):
    return _col(t, "33")


def red(t):
    return _col(t, "31")


def cyan(t):
    return _col(t, "36")


def say(msg=""):
    # The live panel already names the account, so the prefix would be noise.
    VIEW.log(msg if VIEW.active else f"[{ACCOUNT_NAME}] {msg}")


def announce(msg=""):
    """Batch-level output that belongs to no single account."""
    VIEW.log(msg)


def log(msg):
    with open(LOG_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{ACCOUNT_NAME}] [{date.today()} {time.strftime('%H:%M:%S')}] {msg}\n")


def refresh_excel_report(announce=False):
    """Rebuild the readable workbook without risking the solver session."""
    try:
        path = reporting.export_excel(HISTORY_DB, REPORT_FILE, account_name=ACCOUNT_NAME)
        if announce:
            say(green(f"✓ report updated: {path.name}"))
        return True
    except Exception as e:
        log(f"REPORT ERROR: {e}")
        say(yellow(f"  ! Excel report could not be updated: {e}"))
        return False


class CircuitBreakerOpen(RuntimeError):
    """LeetCode requests are paused after repeated blocking responses."""


class CloudflareBlocked(CircuitBreakerOpen):
    """Cloudflare's challenge page did not clear within the wait period."""


class BrowserSessionClosed(RuntimeError):
    """The Playwright page, browser context, or driver is no longer usable."""


_CLOSED_BROWSER_MESSAGES = (
    "target page, context or browser has been closed",
    "connection closed while reading from the driver",
    "browser has been closed",
    "browser closed",
)


def browser_session_closed(error):
    """Return whether a Playwright error means this run cannot continue."""
    message = str(error).lower()
    return any(marker in message for marker in _CLOSED_BROWSER_MESSAGES)


def load_runtime_state():
    if not STATE_FILE.exists():
        return {}
    try:
        data = json.loads(STATE_FILE.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_runtime_state(state):
    """Atomically save restart-sensitive timing state."""
    tmp = STATE_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1, sort_keys=True))
    tmp.replace(STATE_FILE)


def update_runtime_state(**changes):
    state = load_runtime_state()
    for key, value in changes.items():
        if value is None:
            state.pop(key, None)
        else:
            state[key] = value
    save_runtime_state(state)


def _state_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def ensure_api_circuit_closed(now=None):
    now = time.time() if now is None else now
    state = load_runtime_state()
    open_until = _state_float(state.get("api_circuit_open_until"))
    if open_until > now:
        remaining = max(1, int((open_until - now + 59) // 60))
        raise CircuitBreakerOpen(
            f"API circuit breaker is open; retry in about {remaining} min"
        )
    if open_until:
        update_runtime_state(api_circuit_open_until=None, blocked_responses=None)


def record_api_status(status, url, now=None):
    """Open the circuit after repeated 403/429 responses in a short window."""
    if status not in BLOCKED_STATUSES:
        return

    now = time.time() if now is None else now
    state = load_runtime_state()
    timestamps = state.get("blocked_responses", [])
    if not isinstance(timestamps, list):
        timestamps = []
    recent = []
    for timestamp in timestamps:
        parsed = _state_float(timestamp, default=-1)
        if parsed >= 0 and 0 <= now - parsed <= BREAKER_WINDOW_SECONDS:
            recent.append(parsed)
    recent.append(now)
    changes = {"blocked_responses": recent}
    log(
        f"API BLOCK {status} ({len(recent)}/{BREAKER_THRESHOLD}) on {url}"
    )
    if len(recent) >= BREAKER_THRESHOLD:
        changes["api_circuit_open_until"] = now + BREAKER_COOLDOWN_SECONDS
    update_runtime_state(**changes)
    if len(recent) >= BREAKER_THRESHOLD:
        raise CircuitBreakerOpen(
            f"stopping after {len(recent)} blocked API responses "
            f"within {BREAKER_WINDOW_SECONDS // 60} min"
        )


# ---------------- arrow-key menu (stdlib only) -----------------------------


class _cbreak:
    """Temporarily switch the terminal to cbreak mode for single-char reads."""

    def __enter__(self):
        if os.name == "nt":
            return self
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        # TCSANOW, not the default TCSAFLUSH - flushing would drop keystrokes
        # that arrived while the terminal was still in canonical mode.
        tty.setcbreak(self.fd, termios.TCSANOW)

    def __exit__(self, *exc):
        if os.name != "nt":
            termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


def _read_key():
    """Read one keypress (cbreak required). Arrows map to 'up'/'down'/...

    Reads raw bytes from the fd - going through sys.stdin would buffer a
    whole escape sequence, hiding the rest of it from _fd_select.
    """
    if os.name == "nt":
        ch = msvcrt.getwch()
        if ch in ("\x00", "\xe0"):
            return {"H": "up", "P": "down", "M": "right", "K": "left"}.get(msvcrt.getwch(), "esc")
        if ch == "\x03":
            raise KeyboardInterrupt
        return {"\r": "enter", "\n": "enter", " ": "space", "\x1b": "esc"}.get(ch, ch)
    ch = os.read(sys.stdin.fileno(), 1)
    if ch == b"\x1b":  # a lone Esc, or an arrow key's escape sequence
        if _fd_select([sys.stdin], [], [], 0.05)[0]:
            seq = os.read(sys.stdin.fileno(), 1)
            if seq in (b"[", b"O") and _fd_select([sys.stdin], [], [], 0.05)[0]:
                return {
                    b"A": "up",
                    b"B": "down",
                    b"C": "right",
                    b"D": "left",
                }.get(os.read(sys.stdin.fileno(), 1), "esc")
        return "esc"
    if ch in (b"\r", b"\n"):
        return "enter"
    if ch == b" ":
        return "space"
    return ch.decode(errors="replace")


def _draw(lines, prev):
    """Redraw `lines` in place, overwriting the previously drawn block."""
    if prev:
        sys.stdout.write(f"\x1b[{prev - 1}A")
    sys.stdout.write("\r\x1b[J" + "\n".join(lines))
    sys.stdout.flush()
    return len(lines)


def _clear(count):
    if count:
        sys.stdout.write(f"\x1b[{count - 1}A\r\x1b[J")
        sys.stdout.flush()


class Heading(str):
    """A section label inside a menu; shown, but never selectable."""


def _step(options, idx, delta):
    """Move to the next selectable row, skipping section headings."""
    for _ in range(len(options)):
        idx = (idx + delta) % len(options)
        if not isinstance(options[idx], Heading):
            return idx
    return idx


def _footer(hint, keys):
    """Explain the highlighted row, then the keys that act on it."""
    return ["", dim(f"  {hint}" if hint else "  "), dim(f"  {keys}")]


def select(title, options, default=0, hints=None, header=None):
    """Arrow-key picker. Returns the chosen index, or None if cancelled."""
    if not options or not sys.stdin.isatty():
        return default
    idx = min(max(default, 0), len(options) - 1)
    if isinstance(options[idx], Heading):
        idx = _step(options, idx, 1)
    drawn = 0
    with _cbreak():
        while True:
            lines = [bold(f"  {title}")] + list(header or [])
            for i, opt in enumerate(options):
                if isinstance(opt, Heading):
                    lines.append(dim(f"  {opt}"))
                elif i == idx:
                    lines.append(cyan(f"  ❯ {opt}"))
                else:
                    lines.append(f"    {opt}")
            lines += _footer((hints or {}).get(idx), "↑/↓ move · Enter choose · Esc quit")
            drawn = _draw(lines, drawn)
            key = _read_key()
            if key in ("up", "k"):
                idx = _step(options, idx, -1)
            elif key in ("down", "j"):
                idx = _step(options, idx, 1)
            elif key == "enter":
                _clear(drawn)
                return idx
            elif key in ("esc", "q"):
                _clear(drawn)
                return None


def checkbox(title, options, checked, hints=None, header=None):
    """Multi-select toggled with Space. Returns list of bools, or None."""
    if not sys.stdin.isatty():
        return list(checked)
    state, idx, drawn = list(checked), 0, 0
    note = ""
    with _cbreak():
        while True:
            picked = sum(1 for on in state if on)
            lines = [bold(f"  {title}"), dim(f"  {picked} of {len(options)} selected")]
            lines += list(header or [])
            for i, opt in enumerate(options):
                mark = green("[x]") if state[i] else "[ ]"
                row = f"{mark} {opt}"
                lines.append(cyan(f"  ❯ {row}") if i == idx else f"    {row}")
            lines += _footer(
                note or (hints or {}).get(idx),
                "↑/↓ move · Space tick · Enter save · Esc cancel",
            )
            drawn = _draw(lines, drawn)
            key = _read_key()
            note = ""
            if key in ("up", "k"):
                idx = (idx - 1) % len(options)
            elif key in ("down", "j"):
                idx = (idx + 1) % len(options)
            elif key == "space":
                state[idx] = not state[idx]
            elif key == "enter":
                if any(state):
                    _clear(drawn)
                    return state
                note = yellow("tick at least one with Space first")
            elif key in ("esc", "q"):
                _clear(drawn)
                return None


def ask(title, prompt, help_lines=(), check=None):
    """Inline text field. Returns the accepted text, or None if cancelled."""
    if not sys.stdin.isatty():
        raise EOFError("a terminal is required for this prompt")
    text, note, drawn = "", "", 0
    with _cbreak():
        while True:
            lines = [bold(f"  {title}"), ""]
            lines += [dim(f"  {line}") for line in help_lines]
            lines += ["", f"  {prompt}: {cyan(text)}{dim('▏')}"]
            lines += _footer(note or "", "type · Enter confirm · Esc cancel")
            drawn = _draw(lines, drawn)
            key = _read_key()
            if key == "enter":
                try:
                    value = check(text) if check else text
                except ValueError as error:
                    note = yellow(str(error))
                    continue
                _clear(drawn)
                return value
            if key == "esc":
                _clear(drawn)
                return None
            if key in ("\x7f", "\x08"):
                text, note = text[:-1], ""
            elif key == "space":
                text, note = text + " ", ""
            elif len(key) == 1 and key.isprintable():
                text, note = text + key, ""


def confirm(title, rows, action, warning=""):
    """Show what is about to happen and let the user back out."""
    width = max((len(label) for label, _ in rows if label), default=0)
    header = [""]
    for label, value in rows:
        if label:
            header.append(f"    {dim(label.ljust(width))}   {value}")
        elif value:
            header.append(f"    {dim(value)}")
        else:
            header.append("")
    if warning:
        header += [f"  {yellow(warning)}"]
    header += [""]
    chosen = select(title, [action, "Back"],
                    hints={1: "Return to the menu without running anything"},
                    header=header)
    return chosen == 0


# ---------------- LeetCode API helpers (fetch through the real page) -------


def get_csrf(page):
    """Read csrftoken from the leetcode.com page's cookies."""
    m = re.search(r"csrftoken=([A-Za-z0-9]+)", page.evaluate("() => document.cookie"))
    if not m:
        raise RuntimeError("csrftoken not found in cookies - not logged in?")
    return m.group(1)


def network_notice(message):
    say(yellow(f"  ! {message}"))
    log(message)


def _is_leetcode_url(url):
    return url == LEETCODE or url.startswith(LEETCODE + "/")


def _challenge_cleared(page):
    """True when the page is no longer Cloudflare's "Just a moment..." page."""
    try:
        return "just a moment" not in page.title().lower()
    except Exception:
        return True


def _wait_for_challenge_clear(page, attempts=30):
    """Wait briefly for an interstitial to clear on its own."""
    for _ in range(attempts):
        if _challenge_cleared(page):
            return
        try:
            page.wait_for_timeout(1000)
        except Exception:
            return


def navigate(page, url):
    """Retry safe page loads without spending another problem attempt."""
    def load():
        response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if response is None:
            raise TransientReadError(f"No HTTP response from {url}")
        if response.status == 403 and _is_leetcode_url(url):
            # Cloudflare sometimes answers a page load with its "Just a
            # moment..." interstitial (HTTP 403). It may clear on its
            # own, so wait for that and reload once before treating
            # the response as a block. The wait is a no-op for pages that
            # are not a challenge page.
            if not _challenge_cleared(page):
                log(f"CLOUDFLARE CHALLENGE on {url}")
                say(yellow("  ! Cloudflare challenge - waiting for it to clear ..."))
            _wait_for_challenge_clear(page)
            response = page.goto(url, wait_until="domcontentloaded", timeout=30000)
            if response is None:
                raise TransientReadError(f"No HTTP response from {url}")
        if _is_leetcode_url(url) and response.status in BLOCKED_STATUSES:
            record_api_status(response.status, url)
            update_runtime_state(
                api_circuit_open_until=time.time() + BREAKER_COOLDOWN_SECONDS
            )
            if response.status == 403 and not _challenge_cleared(page):
                raise CloudflareBlocked("Cloudflare challenge did not clear; run --setup visibly to complete verification")
            raise CircuitBreakerOpen(f"LeetCode returned {response.status}")
        if response.status in (408, 429) or response.status >= 500:
            raise TransientReadError(f"HTTP {response.status} from {url}")
        return response
    return retry_read(load, f"Loading {url}", network_notice)


def api_fetch(
    page,
    url,
    method="GET",
    payload=None,
    csrf="",
    referrer=LEETCODE,
    _retried=False,
    retry_safe=False,
):
    ensure_api_circuit_closed()
    body = json.dumps(payload) if payload is not None else None
    def fetch():
        res = page.evaluate(FETCH_JS, [url, method, body, csrf, referrer])
        if (method == "GET" or retry_safe) and (
            res["status"] == 408 or res["status"] >= 500
        ):
            raise TransientReadError(f"HTTP {res['status']} from {url}")
        return res

    if method == "GET" or retry_safe:
        res = retry_read(fetch, f"Reading {url}", network_notice)
    else:
        try:
            res = fetch()
        except Exception as error:
            if not is_network_error(error):
                raise
            raise NetworkUnavailable(
                f"Connection lost during {method} {url}. The request may have "
                "reached LeetCode; it was not resent. Check submission history "
                "before running again."
            ) from error
        if res["status"] == 408 or res["status"] >= 500:
            raise NetworkUnavailable(
                f"HTTP {res['status']} during {method} {url}; request was not "
                "resent. Check submission history before running again."
            )
    status, text = res["status"], res["text"]
    record_api_status(status, url)
    # LeetCode's Cloudflare occasionally answers the in-page fetch with its
    # "Just a moment..." challenge page. Reload the page through navigate(),
    # which waits for the browser to clear the challenge, then retry once.
    if status == 403 and "Just a moment" in text and not _retried:
        log(f"CLOUDFLARE CHALLENGE on {url}")
        say(yellow("  ! Cloudflare challenge - backing off, then retrying ..."))
        navigate(page, referrer)
        return api_fetch(page, url, method, payload, csrf, referrer,
                         _retried=True, retry_safe=retry_safe)
    if status in BLOCKED_STATUSES:
        # Moving straight to a different problem still uses the same blocked
        # LeetCode session. Stop this run and persist a real cooldown instead.
        update_runtime_state(
            api_circuit_open_until=time.time() + BREAKER_COOLDOWN_SECONDS
        )
        if status == 403 and "Just a moment" in text:
            raise CloudflareBlocked("Cloudflare challenge did not clear; run --setup visibly to complete verification")
        raise CircuitBreakerOpen(
            f"LeetCode returned {status}; retry in about "
            f"{BREAKER_COOLDOWN_SECONDS // 60} min"
        )
    return status, text


def gql(page, query, variables=None):
    status, text = api_fetch(
        page,
        f"{LEETCODE}/graphql",
        "POST",
        {"query": query, "variables": variables or {}},
        retry_safe=True,
    )
    if status >= 400:
        raise RuntimeError(f"GraphQL error {status}: {text[:200]}")
    data = json.loads(text)
    if data.get("errors"):
        raise RuntimeError(f"GraphQL errors: {reporting.clean_reason(data['errors'])}")
    return data


def verify_login(page):
    data = gql(page, "query { userStatus { isSignedIn username } }")
    if not data.get("data", {}).get("userStatus", {}).get("isSignedIn"):
        raise RuntimeError("LeetCode session expired; run --setup to log in again")
    accounts.check_identity(ACCOUNT_NAME, data["data"]["userStatus"].get("username"))
    return get_csrf(page)


def get_problem_list(page):
    """All free problems not already accepted by you."""
    query = """
    query problemsetQuestionList($categorySlug: String, $limit: Int,
        $skip: Int, $filters: QuestionListFilterInput) {
      problemsetQuestionList: questionList(categorySlug: $categorySlug,
        limit: $limit, skip: $skip, filters: $filters) {
        total: totalNum
        questions: data {
          frontendQuestionId: questionFrontendId
          title titleSlug isPaidOnly difficulty status
        }
      }
    }"""
    out, skip = [], 0
    while True:
        data = gql(
            page,
            query,
            {
                "categorySlug": "all-code-essentials",
                "limit": 100,
                "skip": skip,
                "filters": {"status": "NOT_STARTED"},
            },
        )
        qs = data["data"]["problemsetQuestionList"]["questions"]
        if not qs:
            break
        out += qs
        skip += 100
    return [q for q in out if not q["isPaidOnly"]]


def get_question_meta(page, slug):
    query = """
    query questionDetail($titleSlug: String!) {
      question(titleSlug: $titleSlug) {
        questionId
        exampleTestcaseList
      }
    }"""
    data = gql(page, query, {"titleSlug": slug})["data"]["question"]
    qid = data["questionId"]
    tests = data.get("exampleTestcaseList") or []
    return qid, "\n".join(tests)


def poll_check(page, check_id):
    """Poll until run/submit finishes; return JSON result."""
    for _ in range(60):
        status, text = api_fetch(
            page, f"{LEETCODE}/submissions/detail/{check_id}/check/"
        )
        if status < 400:
            d = json.loads(text)
            if d.get("state") == "SUCCESS":
                return d
        time.sleep(2)
    raise NetworkUnavailable(
        f"Timed out waiting for LeetCode result {check_id}. Request was not "
        "resent; check submission history before running again."
    )


def run_solution(page, csrf, slug, qid, code, data_input):
    status, text = api_fetch(
        page,
        f"{LEETCODE}/problems/{slug}/interpret_solution/",
        "POST",
        {
            "lang": "python3",
            "question_id": int(qid),
            "typed_code": code,
            "data_input": data_input,
        },
        csrf=csrf,
        referrer=f"{LEETCODE}/problems/{slug}/description/",
    )
    if status >= 400:
        raise RuntimeError(f"run failed {status}: {text[:200]}")
    return poll_check(page, json.loads(text)["interpret_id"])


def submit_solution(page, csrf, slug, qid, code, on_submitted=None):
    status, text = api_fetch(
        page,
        f"{LEETCODE}/problems/{slug}/submit/",
        "POST",
        {"lang": "python3", "question_id": int(qid), "typed_code": code},
        csrf=csrf,
        referrer=f"{LEETCODE}/problems/{slug}/description/",
    )
    if status >= 400:
        raise RuntimeError(f"submit failed {status}: {text[:200]}")
    submission_id = json.loads(text)["submission_id"]
    if on_submitted is not None:
        on_submitted(submission_id)
    result = poll_check(page, submission_id)
    result.setdefault("submission_id", submission_id)
    return result


# ---------------- walkccc.me scraping -------------------------------------


def strip_comments(code):
    """Remove # comments and docstrings from a solution snippet.

    Masks the offending character ranges out of the original text (via the
    token stream), so the surviving code keeps its exact hand-written
    formatting. Falls back to the original text if the result no longer
    parses - never break a working solution.
    """
    src_lines = code.splitlines()
    drop = [False] * len(src_lines)
    try:
        toks = list(tokenize.generate_tokens(io.StringIO(code).readline))
    except (SyntaxError, tokenize.TokenError, IndentationError):
        return code

    def blank_out(r1, c1, r2, c2):
        """Remove the (row, col) span; drop lines that become empty."""
        if r1 == r2:
            line = src_lines[r1]
            rest = line[:c1] + line[c2:]
            if rest.strip():
                src_lines[r1] = rest
            else:
                drop[r1] = True
        else:
            if src_lines[r1][:c1].strip():
                src_lines[r1] = src_lines[r1][:c1].rstrip()
            else:
                drop[r1] = True
            for r in range(r1 + 1, r2):
                drop[r] = True
            tail = src_lines[r2][c2:]
            if tail.strip():
                src_lines[r2] = tail
            else:
                drop[r2] = True

    # stmt_start tracks "nothing real emitted since the last NEWLINE/INDENT";
    # a STRING there is a docstring (implicit concatenations included).
    stmt_start = True
    for tok in toks:
        if tok.type == tokenize.COMMENT:
            blank_out(
                tok.start[0] - 1,
                tok.start[1],
                tok.start[0] - 1,
                len(src_lines[tok.start[0] - 1]),
            )
        elif tok.type == tokenize.STRING and stmt_start:
            blank_out(
                tok.start[0] - 1,
                tok.start[1],
                tok.end[0] - 1,
                tok.end[1],
            )
        elif tok.type in (tokenize.NEWLINE, tokenize.INDENT):
            stmt_start = True
        elif tok.type not in (tokenize.NL, tokenize.DEDENT, tokenize.ENDMARKER):
            stmt_start = False

    out, prev_blank = [], False
    for line, killed in zip((ln.rstrip() for ln in src_lines), drop):
        if killed:
            continue
        blank = not line
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    while out and not out[0]:
        out.pop(0)
    while out and not out[-1]:
        out.pop()
    result = "\n".join(out) + "\n"
    try:
        ast.parse(result)
        return result
    except SyntaxError:
        return code


class NoSolutionYet(RuntimeError):
    """walkccc.me hasn't published a solution for this problem yet."""


def get_python_solution(page, num):
    """Scrape the Python solution from walkccc.me (keyed by problem number)."""
    res = navigate(page, WALKCCC_URL.format(num=num))
    if res.status == 404:
        raise NoSolutionYet(f"no page for #{num} on walkccc.me yet")
    if res.status >= 400:
        raise NetworkUnavailable(f"Solution source returned HTTP {res.status}")
    retry_read(
        lambda: page.wait_for_selector("pre code", state="attached", timeout=15000),
        f"Loading code blocks for #{num}", network_notice,
    )
    # textContent preserves line breaks and includes inactive language tabs.
    blocks = page.locator("pre code").all_text_contents()
    code = extract_python_solution(blocks)
    cleaned = strip_comments(code)
    if cleaned != code:
        say(dim("  stripped comments/docstrings from the snippet"))
    return cleaned


def extract_python_solution(blocks):
    candidates = []
    for block in blocks:
        code = block.strip()
        if not re.search(r"\b(?:async\s+)?def\s+\w+\s*\(", code):
            continue
        try:
            tree = ast.parse(code)
        except SyntaxError:
            continue
        if not any(isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                   for node in ast.walk(tree)):
            continue
        priority = 0 if any(isinstance(node, ast.ClassDef) and node.name == "Solution"
                            for node in tree.body) else 1
        candidates.append((priority, code))
    if not candidates:
        raise NoSolutionYet("page loaded, but contains no usable Python solution")
    return min(candidates, key=lambda candidate: candidate[0])[1]


# ---------------- browser + interactive menu ------------------------------


def _launch(p, offscreen=False):
    """Use a dedicated persistent Chrome profile with native browser defaults."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    args = ["--window-size=1365,900"]
    if offscreen and os.name == "nt":
        args.append("--window-position=-32000,-32000")
    else:
        args.append("--window-position=80,80")
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=False,
        channel="chrome",
        no_viewport=True,
        args=args,
    )


def _ago(stamp):
    """Turn a stored timestamp into a short, readable age."""
    try:
        days = (date.today() - datetime.fromisoformat(stamp).date()).days
    except (TypeError, ValueError):
        return "run before"
    if days <= 0:
        return "run today"
    if days == 1:
        return "run yesterday"
    return f"run {days} days ago"


def _signed_in(paths):
    """A persistent Chrome profile with contents means a login was saved."""
    try:
        return any(paths["profile"].iterdir())
    except OSError:
        return False


def account_summary(name):
    """One readable line per account, read from files without creating any."""
    paths = accounts.paths(name)
    try:
        identity = accounts.expected_username(name)
    except ValueError:
        identity = None
    facts = []
    try:
        facts.append(f"{len(json.loads((paths['data'] / 'solved.json').read_text()))} solved")
    except (OSError, ValueError):
        pass
    last = reporting.last_session_at(paths["data"] / "attempts.db")
    facts.append(_ago(last) if last else "never run")
    if identity:
        return (name, dim(" · ".join([identity] + facts)))
    if _signed_in(paths):
        # Only a run can read the username back from LeetCode.
        return (name, dim(" · ".join(facts + ["username saved on next run"])))
    return (name, yellow("not set up yet · choose “Log in again”"))


def account_header(names, limit=4):
    """Show who the bot knows about, so the menu is not a blind list."""
    known = accounts.list_accounts()
    rows = [account_summary(name) for name in known[:limit]]
    width = max((len(name) for name, _ in rows), default=0)
    lines = []
    for name, detail in rows:
        mark = green("●") if name in names else dim("○")
        lines.append(f"  {mark} {bold(name.ljust(width))}  {detail}")
    if len(known) > limit:
        lines.append(dim(f"    +{len(known) - limit} more"))
    return lines + [""]


def register_account_if_needed(name):
    """Bind a named account to its expected username before the first login."""
    if name == "default":
        return
    existing = (
        accounts.expected_username(name)
        if accounts.paths(name)["identity"].exists() else None
    )
    if existing is None:
        accounts.register(name, input("Expected LeetCode username for this account: "))
    else:
        say(f"Log in as {existing}; other usernames will be rejected.")


def account_setup(name, username=None):
    """Register if needed and log in with every path bound to that account."""
    previous = ACCOUNT_NAME
    try:
        configure_account(name)
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_run(LOCK_FILE):
            if username:
                accounts.register(name, username)
            else:
                register_account_if_needed(name)
            setup_login()
    finally:
        configure_account(previous)


def _new_name(text):
    name = accounts.validate_name(text.strip().lower())
    if name in accounts.list_accounts():
        raise ValueError(f"“{name}” is already set up; pick another nickname.")
    return name


def _new_username(text):
    text = text.strip()
    if not text:
        raise ValueError("Enter the username you sign in with.")
    if "@" in text:
        raise ValueError("That looks like an email. LeetCode usernames have no @.")
    return text


def add_account():
    """Walk through naming, binding and logging into a new account."""
    name = ask(
        "Add an account  ·  step 1 of 3",
        "Nickname",
        [
            "A short name for this login on this computer only.",
            "Lowercase letters, digits, - or _.  Example: jaagrett",
        ],
        check=_new_name,
    )
    if name is None:
        return None
    username = ask(
        "Add an account  ·  step 2 of 3",
        "LeetCode username",
        [
            f"The username “{name}” signs in with on leetcode.com.",
            "It is checked before every run, so the bot can never",
            "submit to the wrong account.",
        ],
        check=_new_username,
    )
    if username is None:
        return None
    ready = confirm(
        "Add an account  ·  step 3 of 3",
        [("Nickname", name), ("LeetCode username", username)],
        "Open the browser and log in",
        "A browser window opens next. Log in there, then come back here.",
    )
    if not ready:
        return None
    account_setup(name, username)
    return name


def setup_login():
    if os.name == "nt":
        from windows.login import manual_login
        manual_login(PROFILE_DIR, say)
        return
    with sync_playwright() as p:
        ctx = _launch(p)
        try:
            page = ctx.new_page()
            page.goto(f"{LEETCODE}/accounts/login/")
            input("Log in inside the browser, then press Enter here... ")
            verify_login(page)
        finally:
            ctx.close()
        say(green(f"✓ login saved to {PROFILE_DIR}"))


# ---------------- Telegram notifications -----------------------------------


def telegram_config():
    """(bot_token, chat_id) from env or telegram.json; (None, None) if unset."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if token and chat:
        return token, chat
    if TELEGRAM_FILE.exists():
        data = json.loads(TELEGRAM_FILE.read_text())
        return data.get("bot_token"), data.get("chat_id")
    return None, None


def send_telegram(text):
    """Send a message; returns False (quietly) if not configured."""
    try:
        token, chat = telegram_config()
        if not token or not chat:
            return False
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data=json.dumps({"chat_id": chat, "text": text}).encode(),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=15) as r:
            return 200 <= r.status < 300
    except Exception as e:
        log(f"TELEGRAM ERROR: {e}")
        return False


def setup_telegram():
    say("1) In Telegram, message @BotFather -> /newbot -> follow the prompts")
    say("   and copy the token it gives you (123456:ABC-...).")
    say("2) Open a chat with your new bot and press Start (or send it anything).")
    say("3) Message @userinfobot and copy your numeric Id.")
    token = input("Bot token: ").strip()
    chat = input("Chat id: ").strip()
    TELEGRAM_FILE.write_text(
        json.dumps({"bot_token": token, "chat_id": chat}, indent=1)
    )
    if send_telegram("leetcode-bot connected - you'll get a message here after each run."):
        say(green("✓ telegram connected (saved to telegram.json)"))
    else:
        say(red("✗ test message failed - check the token and chat id"))


MENU_HINTS = {
    "start": "Solves and submits to LeetCode using the accounts marked ●.",
    "accounts": "Pick which logins this run uses. Several rotate one problem at a time.",
    "difficulty": "Which difficulties problems are chosen from.",
    "count": "How many accepted solutions to aim for, per account.",
    "timing": "Human-like spreads the work out. Instant does not wait at all.",
    "add": "Set up another LeetCode login: nickname, username, then sign in.",
    "login": "Reopen the browser for an account, or fix an expired session.",
    "telegram": "Optional: get a message when a run finishes.",
    "quit": "Leave without running anything.",
}


def menu_rows(cfg, names):
    """Build the menu as (key, label, value) rows and section headings."""
    diffs = cfg["difficulties"]
    diff_label = (
        "All"
        if set(diffs) >= set(DIFFICULTIES)
        else "+".join(diff_colour(DIFF_LABEL[d]) for d in DIFFICULTIES if d in diffs)
    )
    count_label = "Random, 1-9" if cfg["count"] is None else str(cfg["count"])
    timing_label = "Human-like" if cfg["timing"] == "human" else "Instant"
    account_label = names[0] if len(names) == 1 else f"{len(names)} accounts, in turn"
    return [
        Heading("RUN"),
        ("start", "Start run", f"{account_label} · {diff_label} · {count_label} each"),
        ("accounts", "Accounts", account_label),
        ("difficulty", "Difficulty", diff_label),
        ("count", "Problems per account", count_label),
        ("timing", "Speed", timing_label),
        Heading("SET UP"),
        ("add", "Add an account", "another LeetCode login"),
        ("login", "Log in again", "reopen the browser"),
        ("telegram", "Telegram alerts", "when a run finishes"),
        ("quit", "Quit", ""),
    ]


def diff_colour(name):
    return {"Easy": green, "Medium": yellow, "Hard": red}.get(name, dim)(name)


def run_menu(cfg):
    """Arrow-key configuration menu. Returns the config, or None to quit."""
    while True:
        names = cfg.get("accounts") or [ACCOUNT_NAME]
        rows = menu_rows(cfg, names)
        width = max(len(row[1]) for row in rows if not isinstance(row, Heading))
        options, hints = [], {}
        for i, row in enumerate(rows):
            if isinstance(row, Heading):
                options.append(row)
                continue
            key, label, value = row
            options.append(f"{label.ljust(width)}  {dim(value)}" if value else label)
            hints[i] = MENU_HINTS[key]
        chosen = select(
            "LeetCode Bot", options, default=1, hints=hints,
            header=account_header(names),
        )
        key = "quit" if chosen is None else rows[chosen][0]
        if key == "quit":
            return None
        if key == "start":
            return cfg
        try:
            cfg = _menu_action(key, cfg, names)
        except (AlreadyRunning, ValueError, OSError, RuntimeError) as error:
            say(red(f"✗ {error}"))
        except (KeyboardInterrupt, EOFError):
            say(yellow("cancelled"))


def _menu_action(key, cfg, names):
    """Apply one menu choice and return the updated config."""
    if key == "accounts":
        known = accounts.list_accounts()
        res = checkbox(
            "Run on which accounts?", known, [n in known and n in names for n in known],
            header=["", dim("  Several accounts rotate one problem at a time, never at once."), ""],
        )
        if res:
            return {**cfg, "accounts": [n for n, on in zip(known, res) if on]}
    elif key == "difficulty":
        diffs = cfg["difficulties"]
        res = checkbox(
            "Which difficulties?",
            [diff_colour(DIFF_LABEL[d]) for d in DIFFICULTIES],
            [d in diffs for d in DIFFICULTIES],
        )
        if res:
            return {
                **cfg,
                "difficulties": tuple(d for d, on in zip(DIFFICULTIES, res) if on),
            }
    elif key == "count":
        res = select(
            "How many problems per account?",
            ["Random, 1-9 (looks more human)"] + [str(x) for x in range(1, 11)],
        )
        if res is not None:
            return {**cfg, "count": None if res == 0 else res}
    elif key == "timing":
        res = select(
            "How fast should it work?",
            ["Human-like — 3-12 min between problems", "Instant — no waiting"],
            hints={
                0: "Recommended. Spacing the work out looks less automated.",
                1: "Fast, but an obvious pattern on your account.",
            },
        )
        if res is not None:
            return {**cfg, "timing": ("human", "instant")[res]}
    elif key == "add":
        added = add_account()
        if added:
            selected = set(names) | {added}
            return {**cfg, "accounts": [n for n in accounts.list_accounts() if n in selected]}
    elif key == "login":
        chosen = 0 if len(names) == 1 else select("Log in to which account?", names)
        if chosen is not None:
            account_setup(names[chosen])
    elif key == "telegram":
        setup_telegram()
    return cfg


# ---------------- solver session -------------------------------------------


def parse_difficulty(spec):
    spec = spec.strip().lower()
    if spec == "all":
        return DIFFICULTIES
    picked = tuple(d.strip() for d in spec.split(","))
    if not picked or any(d not in DIFFICULTIES for d in picked):
        raise argparse.ArgumentTypeError(
            "difficulty must be easy, medium, hard, all, or a comma-separated list"
        )
    return tuple(dict.fromkeys(picked))


def positive_count(value):
    try:
        count = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("count must be a positive integer") from error
    if count <= 0:
        raise argparse.ArgumentTypeError("count must be a positive integer")
    return count


def pick_count(cfg):
    if cfg.get("count") is not None:
        return cfg["count"]
    r = random.random()
    if r < SKIP_DAY_CHANCE:
        return random.randint(1, 2)  # light day, keeps the streak alive
    if r < SKIP_DAY_CHANCE + LOOSE_DAY_CHANCE:
        return random.randint(2, 4)
    return random.randint(MIN_PROBLEMS, MAX_PROBLEMS)


def _format_countdown(seconds):
    """Format a countdown as MM:SS, or H:MM:SS for long startup waits."""
    seconds = max(0, math.ceil(seconds))
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    if hours:
        return f"{hours}:{minutes:02d}:{seconds:02d}"
    return f"{minutes:02d}:{seconds:02d}"


def _countdown_line(remaining, total, width=20):
    """Build the single-line progress display used for interactive waits."""
    progress = 1.0 if total <= 0 else 1 - (remaining / total)
    progress = min(1.0, max(0.0, progress))
    filled = min(width, max(0, int(progress * width)))
    bar = "█" * filled + "░" * (width - filled)
    return (
        f"  next question [{bar}] {_format_countdown(remaining)} remaining"
        " · s skip · Ctrl+C stop"
    )


def _wait_until(deadline, next_account=None):
    """Wait until a timestamp and show a live countdown in a terminal."""
    remaining = deadline - time.time()
    if remaining <= 0:
        return False
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        time.sleep(remaining)
        return False

    total = remaining
    last_second = None
    completed = False
    try:
        with _cbreak():
            while True:
                remaining = max(0, deadline - time.time())
                shown_second = math.ceil(remaining)
                if shown_second != last_second:
                    if VIEW.active:
                        VIEW.countdown(
                            remaining, total, "· press s to skip", name=next_account
                        )
                    else:
                        sys.stdout.write(
                            "\r\x1b[2K" + dim(_countdown_line(remaining, total))
                        )
                        sys.stdout.flush()
                    last_second = shown_second
                if remaining <= 0:
                    completed = True
                    return False
                if os.name == "nt":
                    if msvcrt.kbhit() and _read_key().lower() == "s":
                        return True
                    time.sleep(min(0.25, remaining))
                elif _fd_select([sys.stdin], [], [], min(0.25, remaining))[0]:
                    if os.read(sys.stdin.fileno(), 1).lower() == b"s":
                        return True
    finally:
        if VIEW.active:
            VIEW.clear_countdown()
        elif completed:
            sys.stdout.write("\n")
        else:
            # Remove a partial countdown before printing a skip/stop message.
            sys.stdout.write("\r\x1b[2K")
        if not VIEW.active:
            sys.stdout.flush()


def wait_before_next_question(cfg):
    """Apply the configured gap at the boundary between question attempts."""
    if cfg["timing"] != "human":
        return

    gap = random.uniform(MIN_GAP, MAX_GAP)
    deadline = time.time() + gap * 60
    update_runtime_state(next_question_at=deadline)
    say(
        dim(
            f"  waiting {gap:.0f} min before the next question ..."
        )
    )
    log(f"Waiting {gap:.1f} min before next question.")
    if _wait_until(deadline):
        say(dim("  wait skipped"))
        log("Wait skipped by user.")
    update_runtime_state(next_question_at=None)


def wait_for_saved_question_slot(cfg):
    """Resume an unfinished human-mode gap after a process restart."""
    if cfg["timing"] != "human":
        if "next_question_at" in load_runtime_state():
            update_runtime_state(next_question_at=None)
        return

    deadline = _state_float(load_runtime_state().get("next_question_at"))
    remaining = deadline - time.time()
    if remaining <= 0:
        if deadline:
            update_runtime_state(next_question_at=None)
        return

    say(
        dim(
            f"  resuming saved wait: {remaining / 60:.0f} min remaining ..."
        )
    )
    log(f"Resuming saved wait ({remaining / 60:.1f} min remaining).")
    if _wait_until(deadline):
        say(dim("  wait skipped"))
        log("Saved wait skipped by user.")
    update_runtime_state(next_question_at=None)


@dataclass
class SessionResult:
    target: int
    done: int = 0
    attempts: int = 0
    paced: bool = False
    run_failed: int = 0
    submit_failed: int = 0
    errors: int = 0
    no_solution: int = 0
    accepted: list = field(default_factory=list)
    status: str = "Completed"
    reason: str = ""


def run_session(ctx, cfg, session_id, result):
    page = ctx.new_page()
    source_page = ctx.new_page()
    VIEW.stage("Opening LeetCode")
    say(dim(f"opening {LEETCODE} ..."))
    navigate(page, LEETCODE)
    VIEW.stage("Checking signed-in account")
    csrf = verify_login(page)
    say(green("✓ logged in"))

    solved = load_solved()
    reporting.import_historical_solved(HISTORY_DB, solved)
    # SQLite can recover an accepted ID if the process died before JSON was saved.
    solved = sorted(set(solved) | set(reporting.accepted_problem_ids(HISTORY_DB)))
    want = {d.upper() for d in cfg["difficulties"]}
    VIEW.selecting(cfg["difficulties"])
    VIEW.stage("Loading unsolved problem list")
    say(dim(f"fetching problem list ({'/'.join(cfg['difficulties'])}) ..."))
    pool = [q for q in get_problem_list(page)
            if q["difficulty"].upper() in want
            and str(q["frontendQuestionId"]) not in solved]
    say(f"  {len(pool)} problems to pick from · {len(solved)} already solved")
    random.shuffle(pool)
    n = result.target
    display_target = cfg.get("report_target", n)
    accepted_before = cfg.get("accepted_before", 0)
    max_attempts = cfg.get("max_attempts", n + 10)
    if cfg.get("scheduler_turn"):
        say(bold(
            f"Account progress: {accepted_before}/{display_target} accepted"
            " · this turn attempts one problem"
        ))
    else:
        say(bold(f"Today's target: {n} problem{'s' if n != 1 else ''}"))
    VIEW.target(display_target)
    log(f"Solving {n} problems, {len(pool)} candidates; timing={cfg['timing']}.")
    attempts = 0
    current_attempt_id = current_attempt_started = None
    current_stage = "Selected"

    def stage(value):
        nonlocal current_stage
        current_stage = value
        VIEW.stage(STAGE_TEXT.get(value, value.lower()))
        reporting.update_attempt_stage(HISTORY_DB, current_attempt_id, value)

    def finish_current(outcome, reason="", status_message="", runtime="", submission_id=""):
        nonlocal current_attempt_id, current_attempt_started
        if current_attempt_id is None:
            return
        reporting.finish_attempt(
            HISTORY_DB, current_attempt_id, current_attempt_started,
            current_stage, outcome, reason=reason, status_message=status_message,
            runtime=runtime, submission_id=submission_id,
        )
        current_attempt_id = current_attempt_started = None

    while result.done < n and pool and attempts < max_attempts:
        if not cfg.get("scheduler_turn"):
            wait_for_saved_question_slot(cfg)
        q = pool.pop()
        slug, name = q["titleSlug"], q["title"]
        attempts += 1
        result.attempts = attempts
        paced_attempt = False
        current_attempt_id, current_attempt_started = reporting.start_attempt(
            HISTORY_DB, session_id, q
        )
        try:
            say()
            say(bold(f"[{result.done + 1}/{n}] #{q['frontendQuestionId']} {name}"
                     f" · {q['difficulty'].title()}"))
            VIEW.problem(accepted_before + result.done + 1, display_target,
                         q["frontendQuestionId"], name,
                         q["difficulty"].title())
            stage("Solution fetch")
            say(dim("  fetching solution from walkccc.me ..."))
            code = get_python_solution(source_page, q["frontendQuestionId"])
            stage("Problem metadata")
            navigate(page, f"{LEETCODE}/problems/{slug}/")
            csrf = get_csrf(page)
            qid, data_input = get_question_meta(page, slug)
            tests_passed = True
            if data_input:
                say(dim("  running example tests ..."))
                paced_attempt = True
                stage("Example tests")
                if cfg["timing"] != "instant":
                    page.wait_for_timeout(random.uniform(600, 1800))
                run = run_solution(page, csrf, slug, qid, code, data_input)
                tests_passed = bool(run.get("correct_answer"))
                if not tests_passed:
                    result.run_failed += 1
                    status_message = run.get("status_msg", "unknown")
                    finish_current("Test failed", reason=status_message,
                                   status_message=status_message)
                    log(f"RUN FAILED ({status_message}), skipping: {name}")
                    say(red(f"  ✗ example tests failed ({status_message}) - next problem"))
                else:
                    say(green("  ✓ example tests passed"))
            else:
                say(yellow("  ! no example tests, submitting anyway"))

            if tests_passed:
                say(dim("  submitting ..."))
                paced_attempt = True
                stage("Submission")
                if cfg["timing"] != "instant":
                    page.wait_for_timeout(random.uniform(800, 2200))
                res = submit_solution(
                    page, csrf, slug, qid, code,
                    on_submitted=lambda submission_id: reporting.record_submission(
                        HISTORY_DB, current_attempt_id, submission_id
                    ),
                )
                status_message = res.get("status_msg", "unknown")
                rt = res.get("status_runtime") or ""
                submission_id = res.get("submission_id", "")
                if res.get("status_code") == 10:
                    finish_current("Accepted", status_message=status_message,
                                   runtime=rt, submission_id=submission_id)
                    result.done += 1
                    VIEW.accepted(accepted_before + result.done)
                    if (not cfg.get("scheduler_turn")
                            and result.done < display_target):
                        VIEW.next(
                            ACCOUNT_NAME,
                            action=f"{ACCOUNT_NAME} · select another unsolved problem",
                        )
                    result.accepted.append(name)
                    solved.append(str(q["frontendQuestionId"]))
                    save_solved(solved)
                    log(f"ACCEPTED ({result.done}/{n}): {name} [{q['difficulty']}]")
                    say(green(f"  ✓ ACCEPTED — {name}") + (dim(f" ({rt})") if rt else ""))
                else:
                    result.submit_failed += 1
                    finish_current("Submission failed", reason=status_message,
                                   status_message=status_message, runtime=rt,
                                   submission_id=submission_id)
                    log(f"SUBMIT FAILED ({status_message}): {name}")
                    say(red(f"  ✗ submit failed ({status_message})"))
        except KeyboardInterrupt:
            finish_current("Interrupted", reason="Stopped by user")
            raise
        except CircuitBreakerOpen as error:
            finish_current("Rate limited", reason=error)
            raise
        except NetworkUnavailable as error:
            finish_current("Network error", reason=error)
            raise
        except NoSolutionYet as error:
            result.no_solution += 1
            finish_current("No solution", reason=error)
            log(f"NO SOLUTION YET, skipping: {name}: {error}")
            say(yellow(f"  ! {error} - next problem"))
        except Exception as error:
            if browser_session_closed(error):
                finish_current("Browser error", reason=error)
                raise BrowserSessionClosed(str(error)) from error
            if is_network_error(error):
                finish_current("Network error", reason=error)
                raise NetworkUnavailable(str(error)) from error
            # Persistence failures after recording a result are fatal, not a
            # second, contradictory outcome for the accepted submission.
            if current_attempt_id is None:
                raise
            if current_stage == "Example tests":
                outcome = "Test failed"
                result.run_failed += 1
            elif current_stage == "Submission":
                outcome = "Submission failed"
                result.submit_failed += 1
            else:
                outcome = "Error"
                result.errors += 1
            finish_current(outcome, reason=error)
            log(f"{outcome.upper()} on {name}: {error}")
            say(red(f"  ✗ {outcome.lower()}: {error}"))

        result.paced = result.paced or paced_attempt
        if (not cfg.get("scheduler_turn") and paced_attempt and result.done < n
                and pool and attempts < max_attempts):
            wait_before_next_question(cfg)

    if result.done < n:
        result.status = "Partial"
        result.reason = (f"Only solved {result.done}/{n}: "
                         + ("candidate pool exhausted" if not pool else "attempt limit reached"))


def execute_session(cfg):
    """Own the entire run lifecycle, including startup failures and cleanup."""
    t0 = time.monotonic()
    cleanup_failed = False
    session_target = cfg.get("session_target")
    result = SessionResult(
        target=session_target if session_target is not None else pick_count(cfg)
    )
    if not cfg.get("scheduler_turn"):
        VIEW.targets({ACCOUNT_NAME: result.target})
        VIEW.account(ACCOUNT_NAME, 1, 1)
        VIEW.next(
            ACCOUNT_NAME,
            action=f"{ACCOUNT_NAME} · select its first unsolved problem",
        )
    reporting.recover_interrupted_sessions(HISTORY_DB)
    session_id = reporting.start_session(
        HISTORY_DB, cfg["difficulties"], cfg["timing"],
        cfg.get("report_target", result.target),
    )
    try:
        ensure_api_circuit_closed()
        if (not cfg["interactive"] and cfg["timing"] == "human"
                and not cfg.get("skip_start_jitter", False)
                and not cfg.get("scheduler_turn")):
            jitter = random.uniform(0, START_JITTER_HOURS * 3600)
            say(dim(f"start jitter: sleeping {jitter / 60:.0f} min ..."))
            _wait_until(time.time() + jitter)
        with sync_playwright() as p:
            VIEW.stage("Opening browser profile")
            ctx = _launch(p, offscreen=not cfg.get("visible_browser", False))
            try:
                run_session(ctx, cfg, session_id, result)
            finally:
                VIEW.stage("Closing browser profile")
                try:
                    ctx.close()
                except Exception as error:
                    # Closing an already dead browser must not mask its cause.
                    cleanup_failed = True
                    log(f"BROWSER CLEANUP ERROR: {error}")
    except KeyboardInterrupt:
        result.status, result.reason = "Interrupted", "Stopped by user"
    except CircuitBreakerOpen as error:
        result.status, result.reason = "Rate limited", str(error)
    except NetworkUnavailable as error:
        result.errors += 1
        result.status, result.reason = "Network error", str(error)
    except Exception as error:
        result.errors += 1
        result.status = "Browser error" if (
            isinstance(error, BrowserSessionClosed) or browser_session_closed(error)
        ) else "Failed"
        result.reason = str(error)
    finally:
        reporting.finish_session(HISTORY_DB, session_id, result.status, result.reason)
        VIEW.stage("Updating account report")
        refresh_excel_report()

    mins = (time.monotonic() - t0) / 60
    say()
    if cfg.get("scheduler_turn"):
        aggregate_done = cfg.get("accepted_before", 0) + result.done
        aggregate_target = cfg.get("report_target", result.target)
        say(bold(
            f"Turn done — {aggregate_done}/{aggregate_target} accepted"
            f" · {result.status}"
        ))
    else:
        say(bold(f"Session done — {result.done}/{result.target} accepted · {result.status}"))
    if result.reason:
        say(yellow(f"  {reporting.clean_reason(result.reason)}"))
        log(f"{result.status}: {result.reason}")
    counts = (f"run failed {result.run_failed} · submit failed {result.submit_failed}"
              f" · no solution yet {result.no_solution} · errors {result.errors}")
    say(f"  {counts}")
    for name in result.accepted:
        say(f"  {green('✓')} {name}")
    say(dim(f"  {mins:.1f} min · progress in solved.json · report in leetcode_report.xlsx"))
    log("Session done.")
    msg = [f"LeetCode bot [{ACCOUNT_NAME}]: {result.done}/{result.target} accepted · {result.status}", counts]
    if result.reason:
        msg.append(reporting.clean_reason(result.reason))
    msg += [f"- {name}" for name in result.accepted]
    msg.append(f"{mins:.0f} min total")
    send_telegram("\n".join(msg))
    if cfg.get("batch_child") and (cleanup_failed or result.status in ("Rate limited", "Network error")):
        code = 75
    else:
        code = 0 if result.status == "Completed" else (130 if result.status == "Interrupted" else 1)
    if not cfg.get("scheduler_turn"):
        VIEW.accepted(result.done)
        VIEW.set_state(ACCOUNT_NAME, "Done" if code == 0 else "Failed")
        VIEW.complete(stopped=code != 0)
    return (code, result) if cfg.get("return_result") else code


def load_solved():
    if SOLVED_FILE.exists():
        data = json.loads(SOLVED_FILE.read_text())
        if not isinstance(data, list) or any(
            not isinstance(value, (str, int)) or isinstance(value, bool)
            for value in data
        ):
            raise ValueError("solved.json must contain a list of problem IDs")
        return sorted({str(value) for value in data})
    return []


def save_solved(skus):
    temporary = SOLVED_FILE.with_suffix(".tmp")
    temporary.write_text(json.dumps(sorted({str(sku) for sku in skus}), indent=1))
    temporary.replace(SOLVED_FILE)


def main():
    if os.name == "nt":
        def interrupt(signum, frame):
            raise KeyboardInterrupt
        signal.signal(signal.SIGBREAK, interrupt)
    ap = argparse.ArgumentParser(
        description="Auto-solve LeetCode problems (scrapes walkccc.me, "
        "submits via your saved login)."
    )
    ap.add_argument(
        "--setup",
        action="store_true",
        help="open a browser to log into LeetCode (saved for later runs)",
    )
    ap.add_argument(
        "--setup-telegram",
        action="store_true",
        help="configure Telegram notifications (token + chat id)",
    )
    ap.add_argument(
        "--export-report",
        action="store_true",
        help="rebuild leetcode_report.xlsx from the structured history",
    )
    ap.add_argument("--instant", action="store_true", help="skip all random waits")
    ap.add_argument("--count", type=positive_count, help="target N accepted submissions")
    ap.add_argument(
        "--difficulty",
        type=parse_difficulty,
        default=None,
        help="easy, medium, hard, all, or comma list (default: easy)",
    )
    ap.add_argument(
        "--no-menu", action="store_true", help="skip the interactive menu (cron mode)"
    )
    ap.add_argument("--visible-browser", action="store_true",
                    help="keep the headed Chrome window on screen on Windows")
    selection = ap.add_mutually_exclusive_group()
    selection.add_argument("--account", default="default",
                           help="named account (default: existing account)")
    selection.add_argument("--accounts", nargs="+",
                           help="named accounts to rotate one problem at a time")
    ap.add_argument("--skip-start-jitter", action="store_true", help=argparse.SUPPRESS)
    ap.add_argument("--batch-child", action="store_true", help=argparse.SUPPRESS)
    args = ap.parse_args()

    try:
        names = args.accounts or [args.account]
        if len(set(names)) != len(names):
            raise ValueError("Each account may appear only once in a batch.")
        if args.setup and args.accounts:
            raise ValueError("Set up one account at a time with --account NAME --setup.")
        configure_account(names[0])
        if not args.setup:
            for name in names:
                accounts.expected_username(name)
        # The menu can switch accounts, so it runs before any account is locked.
        if wants_menu(args):
            return run_interactive(args)
        if len(names) > 1:
            cfg = {**session_config(args), "accounts": names}
            if args.batch_child:
                return run_accounts(cfg)
            batch_lock = accounts.storage_root() / ".batch.lock"
            batch_lock.parent.mkdir(parents=True, exist_ok=True)
            with exclusive_run(batch_lock):
                return run_accounts(cfg)
        LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_run(LOCK_FILE):
            return run_command(args)
    except (AlreadyRunning, ValueError, OSError, RuntimeError) as error:
        say(red(f"✗ {error}"))
        return 1
    except EOFError:
        say(red("✗ Setup needs a terminal to read the expected username."))
        return 1
    except KeyboardInterrupt:
        say(yellow("Stopped by user"))
        return 130


def session_config(args):
    cfg = {
        "difficulties": args.difficulty or ("easy",),
        "count": args.count,
        "timing": "instant" if args.instant else "human",
        "interactive": False,
        "visible_browser": getattr(args, "visible_browser", False),
        "skip_start_jitter": getattr(args, "skip_start_jitter", False),
        "batch_child": getattr(args, "batch_child", False),
    }
    if getattr(args, "accounts", None):
        cfg["accounts"] = list(args.accounts)
    return cfg


def wants_menu(args):
    """A bare run in a terminal opens the menu; flags go straight to work."""
    return (
        not (args.setup or args.setup_telegram or args.export_report)
        and not args.no_menu
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and not getattr(args, "accounts", None)
        and not (args.count or args.instant or args.difficulty)
    )


def plan_rows(cfg, names):
    """Say what the run will do before it starts, and roughly how long."""
    rows, notes = dashboard.plan_lines(
        names, cfg["count"], cfg["difficulties"], cfg["timing"]
    )
    shown = [(name, detail) for name, detail in rows]
    if len(names) > 1:
        shown.append(("order", " → ".join(names)))
    return shown + [("", "")] + [("", note) for note in notes]


def run_interactive(args):
    cfg = {**session_config(args), "interactive": True, "accounts": [ACCOUNT_NAME]}
    while True:
        cfg = run_menu(cfg)
        if cfg is None:
            say(dim("bye"))
            return 0
        names = cfg.get("accounts") or [ACCOUNT_NAME]
        if confirm(
            "Ready to run",
            plan_rows(cfg, names),
            "Start now",
            "This submits accepted solutions to LeetCode.",
        ):
            return run_accounts(cfg)


def outcome_label(code):
    if code == 0:
        return green("completed")
    if code == 130:
        return yellow("stopped by user")
    if code == 75:
        return yellow("stopped — network, Cloudflare or rate limit")
    return red(f"failed (exit {code})")


def report_batch(results):
    width = max(len(name) for name, _, _ in results)
    announce("")
    announce(bold("Summary"))
    for name, _, label in results:
        announce(f"  {name.ljust(width)}  {label}")


@dataclass
class AccountRunState:
    name: str
    target: int
    accepted: int = 0
    attempts: int = 0
    next_eligible_at: float = 0
    state: str = "Waiting"
    code: int | None = None
    reason: str = ""


@dataclass
class TurnOutcome:
    code: int
    accepted: int = 0
    attempted: bool = True
    paced: bool = False
    status: str = "Completed"
    reason: str = ""


def run_one_account_turn(name, cfg, target):
    """Run at most one problem attempt while holding only this account's lock."""
    configure_account(name)
    accounts.expected_username(name)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    turn_cfg = {
        **cfg,
        "count": 1,
        "session_target": 1,
        "max_attempts": 1,
        "report_target": target,
        "accepted_before": cfg.get("accepted_before", 0),
        "scheduler_turn": True,
        "return_result": True,
        "batch_child": True,
        "skip_start_jitter": True,
    }
    with exclusive_run(LOCK_FILE):
        code, result = execute_session(turn_cfg)
    return TurnOutcome(
        code=code,
        accepted=result.done,
        attempted=result.attempts > 0,
        paced=result.paced,
        status=result.status,
        reason=result.reason,
    )


def _account_deadline(name, timing):
    configure_account(name)
    if timing != "human":
        if "next_question_at" in load_runtime_state():
            update_runtime_state(next_question_at=None)
        return 0
    return _state_float(load_runtime_state().get("next_question_at"))


def _save_account_deadline(name, deadline):
    configure_account(name)
    update_runtime_state(next_question_at=deadline or None)


def _next_eligible_in_rotation(states, start, now, exclude=None):
    """Find the next eligible unfinished account in circular order."""
    for offset in range(len(states)):
        index = (start + offset) % len(states)
        state = states[index]
        if (state is not exclude
                and state.state not in ("Done", "Failed", "Skipped")
                and state.next_eligible_at <= now):
            return index
    return None


def _earliest_in_rotation(states, start, exclude=None):
    """Find the earliest deadline, using circular order to break ties."""
    candidates = []
    for offset in range(len(states)):
        state = states[(start + offset) % len(states)]
        if (state is not exclude
                and state.state not in ("Done", "Failed", "Skipped")):
            candidates.append(state)
    return min(candidates, key=lambda state: state.next_eligible_at)


def _show_next_after_turn(view, states, cursor, now, active):
    """Describe the best known next action while one account is running."""
    next_index = _next_eligible_in_rotation(states, cursor, now, exclude=active)
    if next_index is not None:
        view.next(states[next_index].name)
        return
    others = [
        state for state in states
        if state is not active and state.state not in ("Done", "Failed", "Skipped")
    ]
    if others:
        earliest = _earliest_in_rotation(states, cursor, exclude=active)
        remaining = max(0, earliest.next_eligible_at - now)
        view.next(
            earliest.name,
            action=f"{earliest.name} when eligible ({_format_countdown(remaining)})",
        )
    else:
        view.next(
            active.name,
            action="After this turn: finish or select this account's next problem",
        )


def _mark_shared_stop(states, active, code, reason, view):
    """Stop one account and mark every other unfinished account as not runnable."""
    active.state, active.code, active.reason = "Failed", code, reason
    view.set_state(active.name, "Failed", emit=False)
    for other in states:
        if other is not active and other.state not in ("Done", "Failed", "Skipped"):
            other.state, other.code = "Skipped", None
            view.set_state(other.name, "Failed", emit=False)
    view.show_progress()


def run_account_queue(cfg, names, view, turn_runner=run_one_account_turn,
                      clock=time.time, waiter=None, gap_picker=None):
    """Run one account problem at a time using an eligibility-aware rotation."""
    gap_picker = gap_picker or (lambda: random.uniform(MIN_GAP, MAX_GAP) * 60)
    states = [
        AccountRunState(
            name=name,
            target=pick_count(cfg),
            next_eligible_at=_account_deadline(name, cfg["timing"]),
        )
        for name in names
    ]
    view.targets({state.name: state.target for state in states})
    stopped_code = None

    if (not cfg.get("interactive") and cfg["timing"] == "human"
            and not cfg.get("skip_start_jitter", False)):
        jitter = random.uniform(0, START_JITTER_HOURS * 3600)
        deadline = clock() + jitter
        first_index = _next_eligible_in_rotation(states, 0, deadline)
        first = (states[first_index] if first_index is not None
                 else _earliest_in_rotation(states, 0))
        view.waiting(first.name)
        view.next(
            first.name,
            action=f"Begin with {first.name} after startup delay ({_format_countdown(jitter)})",
        )
        announce(dim(
            f"Startup delay: {_format_countdown(jitter)}; then {first.name}"
        ))
        try:
            if waiter is None:
                _wait_until(deadline, next_account=first.name)
            else:
                waiter(deadline)
        except KeyboardInterrupt:
            stopped_code = 130
            _mark_shared_stop(states, states[0], 130, "Stopped by user", view)

    cursor = 0
    while stopped_code is None:
        unfinished = [s for s in states if s.state not in ("Done", "Failed", "Skipped")]
        if not unfinished:
            break

        now = clock()
        eligible_index = None
        for offset in range(len(states)):
            index = (cursor + offset) % len(states)
            state = states[index]
            if (state.state not in ("Done", "Failed", "Skipped")
                    and state.next_eligible_at <= now):
                eligible_index = index
                break

        if eligible_index is None:
            earliest = _earliest_in_rotation(states, cursor)
            remaining = max(0, earliest.next_eligible_at - now)
            view.waiting(earliest.name)
            view.next(earliest.name, remaining)
            try:
                skipped = (
                    _wait_until(earliest.next_eligible_at, next_account=earliest.name)
                    if waiter is None else waiter(earliest.next_eligible_at)
                )
            except KeyboardInterrupt:
                stopped_code = 130
                _mark_shared_stop(
                    states, earliest, 130, "Stopped by user", view
                )
                break
            if skipped:
                earliest.next_eligible_at = 0
                _save_account_deadline(earliest.name, 0)
            continue

        state = states[eligible_index]
        cursor = (eligible_index + 1) % len(states)
        state.state = "Running"
        _save_account_deadline(state.name, 0)
        view.account(state.name, eligible_index + 1, len(states))
        view.target(state.target)
        view.accepted(state.accepted)
        view.selecting(cfg["difficulties"])
        view.stage("Opening browser profile")
        _show_next_after_turn(view, states, cursor, now, state)

        try:
            outcome = turn_runner(
                state.name, {**cfg, "accepted_before": state.accepted}, state.target
            )
        except KeyboardInterrupt:
            outcome = TurnOutcome(
                code=130, attempted=False, status="Interrupted",
                reason="Stopped by user",
            )
        except (AlreadyRunning, ValueError, OSError, RuntimeError) as error:
            outcome = TurnOutcome(code=1, attempted=False, status="Failed", reason=str(error))
            say(red(f"✗ {error}"))

        state.accepted += outcome.accepted
        state.attempts += int(outcome.attempted)
        view.accepted(state.accepted)
        if outcome.code in (75, 130) or outcome.code < 0:
            stopped_code = outcome.code
            _mark_shared_stop(
                states, state, outcome.code, outcome.reason, view
            )
            break
        if state.accepted >= state.target:
            state.state, state.code = "Done", 0
            view.set_state(state.name, "Done")
            continue

        retryable = (
            outcome.attempted
            and outcome.status == "Partial"
            and state.attempts < state.target + 10
        )
        if outcome.code == 0 or retryable:
            state.state = "Waiting"
            if cfg["timing"] == "human" and outcome.paced:
                state.next_eligible_at = clock() + gap_picker()
                _save_account_deadline(state.name, state.next_eligible_at)
            else:
                state.next_eligible_at = 0
            view.set_state(state.name, "Waiting")
        else:
            state.state, state.code, state.reason = "Failed", 1, outcome.reason
            view.set_state(state.name, "Failed")

    view.complete(stopped=stopped_code is not None)
    results = []
    for state in states:
        if state.state == "Done":
            label, code = green("completed"), 0
        elif state.state == "Skipped":
            label, code = yellow("skipped — shared stop"), None
        else:
            detail = f": {reporting.clean_reason(state.reason)}" if state.reason else ""
            label, code = red(f"failed{detail}"), state.code or 1
        results.append((state.name, code, label))
    report_batch(results)
    if stopped_code == 130:
        return 130
    return 0 if all(state.state == "Done" for state in states) else 1


def run_accounts(cfg):
    """Run the chosen accounts in turn, holding one account's lock at a time."""
    global VIEW
    cfg = {
        "difficulties": ("easy",),
        "count": None,
        "timing": "instant",
        "interactive": False,
        **cfg,
    }
    names = cfg.get("accounts") or [ACCOUNT_NAME]
    view = dashboard.build(names)
    previous, VIEW = VIEW, view
    try:
        with view:
            return _run_accounts(cfg, names, view)
    finally:
        VIEW = previous


def _run_accounts(cfg, names, view):
    if len(names) == 1:
        return run_one_account(names[0], cfg)
    announce(bold(f"Running {len(names)} accounts in a one-problem rotation: {', '.join(names)}"))
    announce(dim("Only one browser profile runs at a time; eligible accounts alternate."))
    return run_account_queue(cfg, names, view)


def run_one_account(name, cfg):
    configure_account(name)
    accounts.expected_username(name)
    LOCK_FILE.parent.mkdir(parents=True, exist_ok=True)
    with exclusive_run(LOCK_FILE):
        return execute_session(cfg)


def run_command(args):
    if args.setup:
        register_account_if_needed(ACCOUNT_NAME)
        setup_login()
        return
    if args.setup_telegram:
        setup_telegram()
        return
    if args.export_report:
        reporting.import_historical_solved(HISTORY_DB, load_solved())
        return 0 if refresh_excel_report(announce=True) else 1
    return execute_session(session_config(args))


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
