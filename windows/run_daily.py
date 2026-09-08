"""Sequential Hermes account runner with timestamped logs and no write retries."""
import argparse
from datetime import datetime
import os
from pathlib import Path
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import accounts
from runtime import AlreadyRunning, exclusive_run
from leetcode_bot import parse_difficulty, positive_count


def record(output, message):
    output.write(f'[{datetime.now().astimezone().isoformat(timespec="seconds")}] {message}\n')


def run_account(name, arguments, skip_jitter):
    log_dir = ROOT / 'logs' if name == 'default' else accounts.paths(name)['data'] / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    path = log_dir / f'run-{datetime.now():%Y-%m-%d}.log'
    with path.open('a', encoding='utf-8', buffering=1) as output:
        record(output, f'START account={name}')
        command = [sys.executable, '-u', str(ROOT / 'leetcode_bot.py'), '--no-menu',
                   '--account', name, '--batch-child', *arguments]
        if skip_jitter:
            command.append('--skip-start-jitter')
        process = None
        try:
            process = subprocess.Popen(
                command, cwd=ROOT, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                env={**os.environ, 'PYTHONIOENCODING': 'utf-8'},
                creationflags=subprocess.CREATE_NEW_PROCESS_GROUP if os.name == 'nt' else 0,
            )
            for line in process.stdout:
                record(output, line.rstrip('\n'))
            code = process.wait()
        except KeyboardInterrupt:
            if process is not None and process.poll() is None:
                process.send_signal(signal.CTRL_BREAK_EVENT if os.name == 'nt' else signal.SIGINT)
                try:
                    process.wait(timeout=30)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            record(output, 'Interrupted; inspect the profile before another run.')
            code = 130
        except Exception as error:
            record(output, f'LAUNCH ERROR: {error}')
            if process is not None and process.poll() is None:
                process.kill()
                process.wait()
            code = 1
        finally:
            if process is not None and process.stdout is not None:
                process.stdout.close()
        record(output, f'END exit_code={code} account={name}')
        return code


def run_batch(names, arguments, jitter=True):
    log_dir = ROOT / 'logs'
    log_dir.mkdir(parents=True, exist_ok=True)
    with (log_dir / f'batch-{datetime.now():%Y-%m-%d}.log').open('a', encoding='utf-8', buffering=1) as output:
        outcomes = []
        stop = False
        for index, name in enumerate(names):
            if stop:
                outcome = f'{name}: skipped'
            else:
                code = run_account(name, arguments, skip_jitter=index > 0 or not jitter)
                outcome = f'{name}: ' + ('completed' if code == 0 else f'failed (exit {code})')
                stop = code in (75, 130) or code < 0
            outcomes.append(outcome)
            record(output, outcome)
        summary = '; '.join(outcomes)
        record(output, f'SUMMARY {summary}')
        print(summary, flush=True)
        if any('exit 130' in item for item in outcomes):
            return 130
        return 0 if all(item.endswith(': completed') for item in outcomes) else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--accounts', nargs='+', default=None)
    selection.add_argument('--account', default=None)
    parser.add_argument('--count', type=positive_count, default=1)
    parser.add_argument('--difficulty', type=parse_difficulty, default=('easy',))
    parser.add_argument('--instant', action='store_true')
    parser.add_argument('--visible-browser', action='store_true')
    parser.add_argument('--no-jitter', action='store_true',
                        help='start at the scheduled time instead of up to '
                             '3 hours later; makes the daily run easier to spot')
    args = parser.parse_args()
    names = args.accounts or [args.account or 'default']
    try:
        if len(set(names)) != len(names):
            raise ValueError('Each account may appear only once in a batch.')
        for name in names:
            accounts.expected_username(name)
        arguments = ['--count', str(args.count), '--difficulty', ','.join(args.difficulty)]
        if args.instant:
            arguments.append('--instant')
        if args.visible_browser:
            arguments.append('--visible-browser')
        lock = accounts.storage_root() / '.batch.lock'
        lock.parent.mkdir(parents=True, exist_ok=True)
        with exclusive_run(lock):
            return run_batch(names, arguments, jitter=not args.no_jitter)
    except (ValueError, OSError, AlreadyRunning) as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 130


if __name__ == '__main__':
    sys.exit(main())
