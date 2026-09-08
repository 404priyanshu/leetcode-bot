"""Hermes daily-run entry point: timestamp output and preserve the bot's exit code."""
import os
from pathlib import Path
import subprocess
import sys
from datetime import datetime

ROOT = Path(__file__).resolve().parents[1]


def main():
    log_dir = ROOT / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f"run-{datetime.now():%Y-%m-%d}.log"
    with path.open('a', encoding='utf-8', buffering=1) as output:
        def record(message):
            output.write(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {message}\n')

        record('START daily run')
        try:
            with subprocess.Popen(
                [sys.executable, '-u', str(ROOT / 'leetcode_bot.py'), '--no-menu',
                 *(sys.argv[1:] or ['--count', '1'])],
                cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
            ) as process:
                for line in process.stdout:
                    record(line.rstrip('\n'))
                code = process.wait()
        except Exception as error:
            record(f'LAUNCH ERROR: {error}')
            code = 1
        record(f'END exit_code={code}')
        return code


if __name__ == '__main__':
    sys.exit(main())
