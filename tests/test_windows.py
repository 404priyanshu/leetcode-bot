"""Platform seams plus real subprocess logging without using a live account."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import leetcode_bot as bot
from windows import run_daily


class WindowsTests(unittest.TestCase):
    def test_offscreen_and_setup_use_same_headed_profile(self):
        with tempfile.TemporaryDirectory() as directory:
            profile = Path(directory) / 'profile'
            driver = Mock()
            with patch.object(bot, 'PROFILE_DIR', profile), patch.object(bot, 'os', SimpleNamespace(name='nt')):
                bot._launch(driver, offscreen=True)
                bot._launch(driver)
            calls = driver.chromium.launch_persistent_context.call_args_list
            self.assertEqual([call.args[0] for call in calls], [str(profile)] * 2)
            self.assertIn('--window-position=-32000,-32000', calls[0].kwargs['args'])
            self.assertIn('--window-position=80,80', calls[1].kwargs['args'])
            for call in calls:
                self.assertFalse(call.kwargs['headless'])
                self.assertTrue(call.kwargs['no_viewport'])
                self.assertNotIn('user_agent', call.kwargs)

    def test_windows_keys_and_interrupt(self):
        keyboard = Mock()
        with patch.object(bot, 'os', SimpleNamespace(name='nt')), patch.object(bot, 'msvcrt', keyboard, create=True):
            keyboard.getwch.side_effect = ['\xe0', 'H', '\r', '\x03']
            self.assertEqual(bot._read_key(), 'up')
            self.assertEqual(bot._read_key(), 'enter')
            with self.assertRaises(KeyboardInterrupt):
                bot._read_key()

    def test_runner_logs_unicode_stderr_and_preserves_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'leetcode_bot.py').write_text(
                'import sys\nprint("\\u2713 fixture")\nprint("failure", file=sys.stderr)\nsys.exit(7)\n', encoding='utf-8')
            with patch.object(run_daily, 'ROOT', root), patch.object(run_daily.sys, 'argv', ['runner', '--instant']):
                self.assertEqual(run_daily.main(), 7)
            contents = next((root / 'logs').glob('batch-*.log')).read_text(encoding='utf-8')
            self.assertIn('✓ fixture', contents)
            self.assertIn('failure', contents)
            self.assertIn('END exit_code=7', contents)
            self.assertTrue(all(line.startswith('[') for line in contents.splitlines()))


class ManualLoginTests(unittest.TestCase):
    def test_windows_setup_does_not_start_automation(self):
        with patch.object(bot, 'os', SimpleNamespace(name='nt')), \
                patch('windows.login.manual_login') as login, \
                patch.object(bot, 'sync_playwright') as automation:
            bot.setup_login()
            login.assert_called_once_with(bot.PROFILE_DIR, bot.say)
            automation.assert_not_called()

    def test_manual_login_uses_profile_and_waits_for_browser(self):
        from windows import login
        with tempfile.TemporaryDirectory(prefix='login test ') as directory:
            profile = Path(directory) / 'profile'
            with patch.object(login, 'find_chrome', return_value=Path(directory) / 'chrome.exe'), \
                    patch.object(login.subprocess, 'run', return_value=SimpleNamespace(returncode=0)) as run:
                announce = Mock()
                login.manual_login(profile, announce)
            command = run.call_args.args[0]
            self.assertIn(f'--user-data-dir={profile}', command)
            self.assertFalse(any('remote-debugging' in arg for arg in command))
            self.assertIn('Login has not been verified', announce.call_args.args[0])

    def test_missing_chrome_provides_actionable_error(self):
        from windows import login
        with patch.dict(login.os.environ, {}, clear=True), patch.object(login.shutil, 'which', return_value=None):
            with self.assertRaisesRegex(RuntimeError, 'Install Chrome'):
                login.find_chrome()
