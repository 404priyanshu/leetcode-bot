#!/usr/bin/env python3
"""Daily LeetCode solver: pulls Python solutions from walkccc.me, runs them
against LeetCode's test cases, and submits if they pass."""

import argparse
import json
import random
import re
import sys
import time
from datetime import date
from pathlib import Path

from playwright.sync_api import sync_playwright

BASE = Path(__file__).parent
PROFILE_DIR = BASE / "leetcode_profile"  # persistent login
SOLVED_FILE = BASE / "solved.json"  # progress tracker
LOG_FILE = BASE / "activity.log"

WALKCCC_URL = "https://walkccc.me/LeetCode/problems/{num}/"  # keyed by problem number
LEETCODE = "https://leetcode.com"

# ---- "human" knobs -------------------------------------------------------
MIN_PROBLEMS, MAX_PROBLEMS = 4, 9  # problems per day
MIN_GAP, MAX_GAP = 3, 12  # minutes between problems
START_JITTER_HOURS = 3  # up to N hours random start offset
SKIP_DAY_CHANCE = 0.05  # small chance of a light/skipped day
LOOSE_DAY_CHANCE = 0.15  # small chance of a lazy 1-2 problem day
# -------------------------------------------------------------------------

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


def log(msg):
    with open(LOG_FILE, "a") as f:
        f.write(f"[{date.today()} {time.strftime('%H:%M:%S')}] {msg}\n")


def load_solved():
    if SOLVED_FILE.exists():
        return json.loads(SOLVED_FILE.read_text())
    return []


def save_solved(skus):
    SOLVED_FILE.write_text(json.dumps(sorted(set(skus)), indent=1))


# ---------------- LeetCode API helpers (fetch through the real page) -------


def get_csrf(page):
    """Read csrftoken from the leetcode.com page's cookies."""
    m = re.search(r"csrftoken=([A-Za-z0-9]+)", page.evaluate("() => document.cookie"))
    if not m:
        raise RuntimeError("csrftoken not found in cookies - not logged in?")
    return m.group(1)


def api_fetch(page, url, method="GET", payload=None, csrf="", referrer=LEETCODE):
    body = json.dumps(payload) if payload is not None else None
    res = page.evaluate(FETCH_JS, [url, method, body, csrf, referrer])
    return res["status"], res["text"]


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


def get_python_solution(page, num):
    """Scrape the Python solution from walkccc.me (keyed by problem number)."""
    page.goto(WALKCCC_URL.format(num=num), wait_until="domcontentloaded")
    page.wait_for_selector("pre code", timeout=10000)
    blocks = page.locator("pre code").all_inner_texts()
    # Site now serves one plain code block per language; line-number gutters
    # appear as separate blocks containing only digits. Pick a Python block.
    candidates = [b for b in blocks if "class Solution" in b and "def " in b]
    if not candidates:
        candidates = [b for b in blocks if "def " in b and "return" in b]
    if not candidates:
        raise RuntimeError("no Python solution found on walkccc.me")
    return candidates[0].strip()


# ---------------- main loop ------------------------------------------------


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument(
        "--setup",
        action="store_true",
        help="open a browser window to log into LeetCode",
    )
    ap.add_argument(
        "--instant", action="store_true", help="skip all random waits (for testing)"
    )
    ap.add_argument("--count", type=int, help="force N problems (testing)")
    args = ap.parse_args()

    with sync_playwright() as p:
        ctx = p.chromium.launch_persistent_context(
            str(PROFILE_DIR),
            headless=not args.setup,
            channel="chrome",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--disable-features=IsolateOrigins,site-per-process",
            ],
            ignore_default_args=["--enable-automation"],
        )

        if args.setup:
            page = ctx.new_page()
            page.goto(f"{LEETCODE}/accounts/login/")
            input("Log in inside the browser, then press Enter here... ")
            ctx.close()
            print("Login saved.")
            return

        # Random start offset so the daily run doesn't start like clockwork
        if not args.instant and not args.count:
            time.sleep(random.uniform(0, START_JITTER_HOURS * 3600))

        page = ctx.new_page()

        # Land on leetcode.com first - all API fetches run from this page
        page.goto(LEETCODE, wait_until="domcontentloaded")
        page.wait_for_timeout(3000)
        csrf = get_csrf(page)

        solved = load_solved()
        problems = [
            q for q in get_problem_list(page) if q["frontendQuestionId"] not in solved
        ]
        if not problems:
            log("Nothing left to solve.")
            ctx.close()
            return
        random.shuffle(problems)

        # Decide today's workload with occasional "off" days
        r = random.random()
        if r < SKIP_DAY_CHANCE and not args.count:
            n = random.randint(1, 2)  # light day, keeps the streak alive
        elif r < SKIP_DAY_CHANCE + LOOSE_DAY_CHANCE:
            n = random.randint(2, 4)
        else:
            n = random.randint(MIN_PROBLEMS, MAX_PROBLEMS)
        if args.count:
            n = args.count
        log(f"Solving {n} problems, {len(problems)} candidates.")

        # Pull from the pool until n are accepted; problems missing from
        # walkccc.me or failing the run are skipped in favor of the next one.
        done, attempts = 0, 0
        while done < n and problems and attempts < n + 10:
            q = problems.pop(0)
            slug, name = q["titleSlug"], q["title"]
            attempts += 1
            try:
                code = get_python_solution(page, q["frontendQuestionId"])
                # back to leetcode origin for API calls
                page.goto(f"{LEETCODE}/problems/{slug}/", wait_until="domcontentloaded")
                page.wait_for_timeout(2000)
                qid, data_input = get_question_meta(page, slug)
                if data_input:
                    run = run_solution(page, csrf, slug, qid, code, data_input)
                    if not run.get("correct_answer"):
                        log(f"RUN FAILED, skipping: {name}")
                        continue
                res = submit_solution(page, csrf, slug, qid, code)
                if res.get("status_code") == 10:  # 10 = Accepted
                    solved.append(q["frontendQuestionId"])
                    save_solved(solved)
                    done += 1
                    log(f"ACCEPTED ({done}/{n}): {name} [{q['difficulty']}]")
                    if done < n and not args.instant:
                        time.sleep(random.uniform(MIN_GAP, MAX_GAP) * 60)
                else:
                    log(f"SUBMIT FAILED ({res.get('status_msg')}): {name}")
            except Exception as e:
                log(f"ERROR on {name}: {e}")

        if done < n:
            log(f"Only solved {done}/{n} (too many unusable problems).")
        log("Session done.")
        ctx.close()


if __name__ == "__main__":
    main()
