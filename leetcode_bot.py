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
import os
import random
import re
import sys
import termios
import time
import tokenize
import tty
import urllib.request
from datetime import date
from pathlib import Path
from select import select as _fd_select

from playwright.sync_api import TimeoutError as PWTimeout, sync_playwright

BASE = Path(__file__).parent
PROFILE_DIR = BASE / "leetcode_profile"  # persistent login
SOLVED_FILE = BASE / "solved.json"  # progress tracker
LOG_FILE = BASE / "activity.log"
TELEGRAM_FILE = BASE / "telegram.json"  # {"bot_token": ..., "chat_id": ...}

WALKCCC_URL = "https://walkccc.me/LeetCode/problems/{num}/"  # keyed by problem number
LEETCODE = "https://leetcode.com"

# ---- "human" knobs -------------------------------------------------------
MIN_PROBLEMS, MAX_PROBLEMS = 4, 9  # problems per day
MIN_GAP, MAX_GAP = 3, 12  # minutes between problems
START_JITTER_HOURS = 3  # up to N hours random start offset (non-interactive only)
SKIP_DAY_CHANCE = 0.05  # small chance of a light/skipped day
LOOSE_DAY_CHANCE = 0.15  # small chance of a lazy 1-2 problem day
# -------------------------------------------------------------------------

DIFFICULTIES = ("easy", "medium", "hard")
DIFF_LABEL = {"easy": "Easy", "medium": "Medium", "hard": "Hard"}

# JS fetch snippet used for all LeetCode API calls (runs in the real page,
# so cookies + fingerprint are identical to normal browsing)
FETCH_JS = """
async ([url, method, body, csrf, referrer]) => {
    const r = await fetch(url, {
        method: method,
        headers: {
            'content-type': 'application/json',
            'x-csrftoken': csrf,
        },
        body: body,
        credentials: 'include',
        referrer: referrer,
    });
    return {status: r.status, text: await r.text()};
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
    print(msg, flush=True)


def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{date.today()} {time.strftime('%H:%M:%S')}] {msg}\n")


# ---------------- arrow-key menu (stdlib only) -----------------------------


class _cbreak:
    """Temporarily switch the terminal to cbreak mode for single-char reads."""

    def __enter__(self):
        self.fd = sys.stdin.fileno()
        self.saved = termios.tcgetattr(self.fd)
        # TCSANOW, not the default TCSAFLUSH - flushing would drop keystrokes
        # that arrived while the terminal was still in canonical mode.
        tty.setcbreak(self.fd, termios.TCSANOW)

    def __exit__(self, *exc):
        termios.tcsetattr(self.fd, termios.TCSADRAIN, self.saved)


def _read_key():
    """Read one keypress (cbreak required). Arrows map to 'up'/'down'/...

    Reads raw bytes from the fd - going through sys.stdin would buffer a
    whole escape sequence, hiding the rest of it from _fd_select.
    """
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


def select(title, options, default=0):
    """Arrow-key picker. Returns the chosen index, or None if cancelled."""
    if not options or not sys.stdin.isatty():
        return default
    idx = min(max(default, 0), len(options) - 1)
    drawn = 0
    with _cbreak():
        while True:
            lines = [bold(title), dim("↑/↓ move · Enter select · Esc/q cancel"), ""]
            for i, opt in enumerate(options):
                lines.append(cyan(f"❯ {opt}") if i == idx else f"  {opt}")
            drawn = _draw(lines, drawn)
            key = _read_key()
            if key in ("up", "k"):
                idx = (idx - 1) % len(options)
            elif key in ("down", "j"):
                idx = (idx + 1) % len(options)
            elif key == "enter":
                _clear(drawn)
                return idx
            elif key in ("esc", "q"):
                _clear(drawn)
                return None


def checkbox(title, options, checked):
    """Multi-select toggled with Space. Returns list of bools, or None."""
    if not sys.stdin.isatty():
        return list(checked)
    state, idx, drawn = list(checked), 0, 0
    hint = "↑/↓ move · Space toggle · Enter save · Esc cancel"
    with _cbreak():
        while True:
            lines = [bold(title), dim(hint), ""]
            for i, opt in enumerate(options):
                mark = "[x]" if state[i] else "[ ]"
                lines.append(
                    cyan(f"❯ {mark} {opt}") if i == idx else f"  {mark} {opt}"
                )
            drawn = _draw(lines, drawn)
            key = _read_key()
            if key in ("up", "k"):
                idx = (idx - 1) % len(options)
            elif key in ("down", "j"):
                idx = (idx + 1) % len(options)
            elif key == "space":
                state[idx] = not state[idx]
                hint = "↑/↓ move · Space toggle · Enter save · Esc cancel"
            elif key == "enter":
                if any(state):
                    _clear(drawn)
                    return state
                hint = yellow("pick at least one option")
            elif key in ("esc", "q"):
                _clear(drawn)
                return None


# ---------------- LeetCode API helpers (fetch through the real page) -------


def get_csrf(page):
    """Read csrftoken from the leetcode.com page's cookies."""
    m = re.search(r"csrftoken=([A-Za-z0-9]+)", page.evaluate("() => document.cookie"))
    if not m:
        raise RuntimeError("csrftoken not found in cookies - not logged in?")
    return m.group(1)


def api_fetch(page, url, method="GET", payload=None, csrf="", referrer=LEETCODE, _retried=False):
    body = json.dumps(payload) if payload is not None else None
    res = page.evaluate(FETCH_JS, [url, method, body, csrf, referrer])
    status, text = res["status"], res["text"]
    # LeetCode's Cloudflare occasionally answers the in-page fetch with its
    # "Just a moment..." challenge page. Back off, reload the page so the
    # browser can clear the challenge, then retry the call once.
    if status == 403 and "Just a moment" in text and not _retried:
        log(f"CLOUDFLARE CHALLENGE on {url}")
        say(yellow("  ! Cloudflare challenge - backing off, then retrying ..."))
        time.sleep(random.uniform(15, 30))
        page.goto(referrer, wait_until="domcontentloaded")
        page.wait_for_timeout(10000)  # let the challenge auto-solve
        return api_fetch(page, url, method, payload, csrf, referrer, _retried=True)
    return status, text


def gql(page, query, variables=None):
    status, text = api_fetch(
        page,
        f"{LEETCODE}/graphql",
        "POST",
        {"query": query, "variables": variables or {}},
    )
    if status >= 400:
        raise RuntimeError(f"GraphQL error {status}: {text[:200]}")
    return json.loads(text)


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
    return {}


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


def submit_solution(page, csrf, slug, qid, code):
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
    return poll_check(page, json.loads(text)["submission_id"])


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
    res = page.goto(WALKCCC_URL.format(num=num), wait_until="domcontentloaded")
    if res is not None and res.status == 404:
        raise NoSolutionYet(f"no page for #{num} on walkccc.me yet")
    try:
        page.wait_for_selector("pre code", timeout=15000)
    except PWTimeout:
        raise NoSolutionYet("page has no code blocks yet (or was too slow)")
    blocks = page.locator("pre code").all_inner_texts()
    # Site now serves one plain code block per language; line-number gutters
    # appear as separate blocks containing only digits. Pick a Python block.
    candidates = [b for b in blocks if "class Solution" in b and "def " in b]
    if not candidates:
        candidates = [b for b in blocks if "def " in b and "return" in b]
    if not candidates:
        raise NoSolutionYet("no Python solution published yet")
    code = candidates[0].strip()
    cleaned = strip_comments(code)
    if cleaned != code:
        say(dim("  stripped comments/docstrings from the snippet"))
    return cleaned


# ---------------- browser + interactive menu ------------------------------


def _launch(p, headless):
    return p.chromium.launch_persistent_context(
        str(PROFILE_DIR),
        headless=headless,
        channel="chrome",
        args=[
            "--disable-blink-features=AutomationControlled",
            "--disable-features=IsolateOrigins,site-per-process",
        ],
        ignore_default_args=["--enable-automation"],
    )


def setup_login():
    with sync_playwright() as p:
        ctx = _launch(p, headless=False)
        page = ctx.new_page()
        page.goto(f"{LEETCODE}/accounts/login/")
        input("Log in inside the browser, then press Enter here... ")
        ctx.close()
        say(green("✓ login saved to leetcode_profile/"))


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
    token, chat = telegram_config()
    if not token or not chat:
        return False
    try:
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


def run_menu(cfg):
    """Arrow-key configuration menu. Returns the config, or None to quit."""
    while True:
        diffs = cfg["difficulties"]
        diff_label = (
            "All"
            if set(diffs) >= set(DIFFICULTIES)
            else "+".join(DIFF_LABEL[d] for d in DIFFICULTIES if d in diffs)
        )
        count_label = "Random (human-like)" if cfg["count"] is None else str(cfg["count"])
        timing_label = "Human-like" if cfg["timing"] == "human" else "Instant"
        options = [
            f"{'Start run':<12} {diff_label} · {count_label} · {timing_label}",
            f"{'Difficulty':<12} {diff_label}",
            f"{'Problems':<12} {count_label}",
            f"{'Timing':<12} {timing_label}",
            f"{'Log in':<12} one-time LeetCode setup",
            f"{'Telegram':<12} configure notifications",
            "Quit",
        ]
        i = select("LeetCode Bot", options)
        if i in (None, 6):
            return None
        if i == 0:
            return cfg
        if i == 1:
            res = checkbox(
                "Difficulty",
                [DIFF_LABEL[d] for d in DIFFICULTIES],
                [d in diffs for d in DIFFICULTIES],
            )
            if res:
                cfg = {
                    **cfg,
                    "difficulties": tuple(
                        d for d, on in zip(DIFFICULTIES, res) if on
                    ),
                }
        elif i == 2:
            res = select(
                "Problems per run",
                ["Random (human-like, 1-9)"] + [str(x) for x in range(1, 11)],
            )
            if res is not None:
                cfg = {**cfg, "count": None if res == 0 else res}
        elif i == 3:
            res = select("Timing", ["Human-like (3-12 min gaps)", "Instant (no waits)"])
            if res is not None:
                cfg = {**cfg, "timing": ("human", "instant")[res]}
        elif i == 4:
            setup_login()
        elif i == 5:
            setup_telegram()


# ---------------- solver session -------------------------------------------


def parse_difficulty(spec):
    spec = (spec or "easy").lower()
    if spec == "all":
        return DIFFICULTIES
    picked = tuple(d for d in spec.replace(" ", "").split(",") if d in DIFFICULTIES)
    return picked or ("easy",)


def pick_count(cfg):
    if cfg["count"]:
        return cfg["count"]
    r = random.random()
    if r < SKIP_DAY_CHANCE:
        return random.randint(1, 2)  # light day, keeps the streak alive
    if r < SKIP_DAY_CHANCE + LOOSE_DAY_CHANCE:
        return random.randint(2, 4)
    return random.randint(MIN_PROBLEMS, MAX_PROBLEMS)


def run_session(ctx, cfg):
    t0 = time.time()
    page = ctx.new_page()
    say(dim(f"opening {LEETCODE} ..."))
    page.goto(LEETCODE, wait_until="domcontentloaded")
    page.wait_for_timeout(3000)
    try:
        csrf = get_csrf(page)
    except RuntimeError as e:
        say(red(f"✗ {e}"))
        say(f"  run `{sys.argv[0]} --setup` (or pick 'Log in' in the menu) first.")
        send_telegram(
            "LeetCode bot: could not log in today (session expired?). "
            "Run --setup again to refresh the login."
        )
        return
    say(green("✓ logged in"))

    solved = load_solved()
    want = {d.upper() for d in cfg["difficulties"]}
    say(dim(f"fetching problem list ({'/'.join(cfg['difficulties'])}) ..."))
    pool = [
        q
        for q in get_problem_list(page)
        if q["difficulty"].upper() in want and q["frontendQuestionId"] not in solved
    ]
    say(f"  {len(pool)} problems to pick from · {len(solved)} already solved")
    if not pool:
        say(yellow("nothing left to solve"))
        log("Nothing left to solve.")
        send_telegram("LeetCode bot: nothing left to solve (all caught up).")
        return
    random.shuffle(pool)

    n = pick_count(cfg)
    say(bold(f"Today's target: {n} problem{'s' if n != 1 else ''}"))
    log(f"Solving {n} problems, {len(pool)} candidates.")

    # Pull from the pool until n are accepted; problems missing from
    # walkccc.me or failing the run are skipped in favor of the next one.
    done = run_failed = submit_failed = errors = no_solution = attempts = 0
    accepted = []
    try:
        while done < n and pool and attempts < n + 10:
            q = pool.pop(0)
            slug, name = q["titleSlug"], q["title"]
            attempts += 1
            say()
            say(
                bold(
                    f"[{done + 1}/{n}] #{q['frontendQuestionId']} {name}"
                    f" · {q['difficulty'].title()}"
                )
            )
            try:
                say(dim("  fetching solution from walkccc.me ..."))
                code = get_python_solution(page, q["frontendQuestionId"])
                # back to leetcode origin for API calls
                page.goto(f"{LEETCODE}/problems/{slug}/", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                qid, data_input = get_question_meta(page, slug)
                if data_input:
                    say(dim("  running example tests ..."))
                    run = run_solution(page, csrf, slug, qid, code, data_input)
                    if not run.get("correct_answer"):
                        run_failed += 1
                        log(f"RUN FAILED, skipping: {name}")
                        say(
                            red(
                                f"  ✗ example tests failed"
                                f" ({run.get('status_msg', 'unknown')}) - next problem"
                            )
                        )
                        continue
                    say(green("  ✓ example tests passed"))
                else:
                    say(yellow("  ! no example tests, submitting anyway"))
                say(dim("  submitting ..."))
                res = submit_solution(page, csrf, slug, qid, code)
                if res.get("status_code") == 10:  # 10 = Accepted
                    done += 1
                    solved.append(q["frontendQuestionId"])
                    save_solved(solved)
                    accepted.append(name)
                    log(f"ACCEPTED ({done}/{n}): {name} [{q['difficulty']}]")
                    rt = res.get("status_runtime") or ""
                    say(
                        green(f"  ✓ ACCEPTED — {name}")
                        + (dim(f" ({rt})") if rt else "")
                    )
                    if done < n and cfg["timing"] == "human":
                        gap = random.uniform(MIN_GAP, MAX_GAP)
                        say(
                            dim(
                                f"  waiting {gap:.0f} min before the next one"
                                " (Ctrl+C to skip) ..."
                            )
                        )
                        try:
                            time.sleep(gap * 60)
                        except KeyboardInterrupt:
                            say(dim("  wait skipped"))
                else:
                    submit_failed += 1
                    log(f"SUBMIT FAILED ({res.get('status_msg')}): {name}")
                    say(red(f"  ✗ submit failed ({res.get('status_msg', 'unknown')})"))
            except NoSolutionYet as e:
                no_solution += 1
                log(f"NO SOLUTION YET, skipping: {name}")
                say(yellow(f"  ! {e} - next problem"))
            except Exception as e:
                errors += 1
                log(f"ERROR on {name}: {e}")
                say(red(f"  ✗ error: {e}"))
    except KeyboardInterrupt:
        say()
        say(yellow("interrupted - stopping early"))

    mins = (time.time() - t0) / 60
    say()
    say(bold(f"Session done — {done}/{n} accepted"))
    say(
        f"  run failed {run_failed} · submit failed {submit_failed}"
        f" · no solution yet {no_solution} · errors {errors}"
    )
    for name in accepted:
        say(f"  {green('✓')} {name}")
    say(dim(f"  {mins:.1f} min · progress in solved.json · details in activity.log"))
    if done < n:
        log(f"Only solved {done}/{n}.")
    log("Session done.")
    msg = [
        f"LeetCode bot: {done}/{n} accepted",
        f"run failed {run_failed} · submit failed {submit_failed}"
        f" · no solution yet {no_solution} · errors {errors}",
    ]
    msg += [f"- {name}" for name in accepted]
    msg.append(f"{mins:.0f} min total")
    send_telegram("\n".join(msg))


def load_solved():
    if SOLVED_FILE.exists():
        return json.loads(SOLVED_FILE.read_text())
    return []


def save_solved(skus):
    SOLVED_FILE.write_text(json.dumps(sorted(set(skus)), indent=1))


def main():
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
    ap.add_argument("--instant", action="store_true", help="skip all random waits")
    ap.add_argument("--count", type=int, help="solve exactly N problems")
    ap.add_argument(
        "--difficulty",
        default=None,
        help="easy, medium, hard, all, or comma list (default: easy)",
    )
    ap.add_argument(
        "--no-menu", action="store_true", help="skip the interactive menu (cron mode)"
    )
    args = ap.parse_args()

    if args.setup:
        setup_login()
        return
    if args.setup_telegram:
        setup_telegram()
        return

    cfg = {
        "difficulties": parse_difficulty(args.difficulty),
        "count": args.count,
        "timing": "instant" if args.instant else "human",
        "interactive": False,
    }

    # Bare run in a terminal opens the menu; any run flags go straight to work.
    menu_ok = (
        not args.no_menu
        and sys.stdin.isatty()
        and sys.stdout.isatty()
        and not (args.count or args.instant or args.difficulty)
    )
    if menu_ok:
        cfg["interactive"] = True
        cfg = run_menu(cfg)
        if cfg is None:
            say(dim("bye"))
            return

    with sync_playwright() as p:
        ctx = _launch(p, headless=True)
        try:
            if not cfg["interactive"] and cfg["timing"] == "human":
                jitter = random.uniform(0, START_JITTER_HOURS * 3600)
                say(dim(f"start jitter: sleeping {jitter / 60:.0f} min ..."))
                time.sleep(jitter)
            run_session(ctx, cfg)
        finally:
            ctx.close()


if __name__ == "__main__":
    main()
