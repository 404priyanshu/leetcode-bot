import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from patchright.sync_api import TimeoutError

from runtime import AlreadyRunning, NetworkUnavailable, exclusive_run, retry_read


class RetryTests(unittest.TestCase):
    @patch('runtime.time.sleep')
    def test_transient_errors_retry_same_operation(self, sleep):
        operation = Mock(side_effect=[RuntimeError('net::ERR_NETWORK_CHANGED'),
                                      RuntimeError('net::ERR_CONNECTION_REFUSED'), 'ok'])
        self.assertEqual(retry_read(operation, 'source', Mock()), 'ok')
        self.assertEqual(operation.call_count, 3)
        self.assertEqual([c.args[0] for c in sleep.call_args_list], [5, 15])

    @patch('runtime.time.sleep')
    def test_persistent_outage_is_bounded(self, sleep):
        operation = Mock(side_effect=RuntimeError('net::ERR_CONNECTION_REFUSED'))
        with self.assertRaisesRegex(NetworkUnavailable, 'after 4 tries'):
            retry_read(operation, 'source', Mock())
        self.assertEqual(operation.call_count, 4)
        self.assertEqual(sleep.call_count, 3)

    @patch('runtime.time.sleep')
    def test_timeout_can_recover(self, sleep):
        self.assertEqual(retry_read(Mock(side_effect=[TimeoutError('timeout'), 42]),
                                    'page', Mock()), 42)

    @patch('runtime.time.sleep')
    def test_non_network_error_and_interrupt_are_not_retried(self, sleep):
        for error in (ValueError('bad data'), KeyboardInterrupt(),
                      RuntimeError('Target page, context or browser has been closed')):
            with self.subTest(error=error):
                operation = Mock(side_effect=error)
                with self.assertRaises(type(error)):
                    retry_read(operation, 'page', Mock())
                self.assertEqual(operation.call_count, 1)
        sleep.assert_not_called()


class LockTests(unittest.TestCase):
    def test_excludes_other_process_and_releases_after_exception(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / '.bot.lock'
            code = ('import sys; from runtime import exclusive_run, AlreadyRunning\n'
                    'try:\n with exclusive_run(sys.argv[1]): pass\n'
                    'except AlreadyRunning: sys.exit(7)\n')
            with self.assertRaisesRegex(ValueError, 'test failure'):
                with exclusive_run(lock):
                    other = subprocess.run([sys.executable, '-c', code, str(lock)],
                                           capture_output=True, text=True)
                    self.assertEqual(other.returncode, 7, other.stderr)
                    raise ValueError('test failure')
            other = subprocess.run([sys.executable, '-c', code, str(lock)],
                                   capture_output=True, text=True)
            self.assertEqual(other.returncode, 0, other.stderr)

    def test_crashed_owner_does_not_leave_stale_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            lock = Path(directory) / '.bot.lock'
            code = ('import os, sys; from runtime import exclusive_run\n'
                    'with exclusive_run(sys.argv[1]): os._exit(9)\n')
            child = subprocess.run([sys.executable, '-c', code, str(lock)])
            self.assertEqual(child.returncode, 9)
            with exclusive_run(lock):
                pass
