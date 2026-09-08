import argparse
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import accounts
import leetcode_bot as bot
from windows import run_daily
from runtime import exclusive_run


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.base = patch.object(accounts, 'BASE', self.root)
        self.base.start()
        self.addCleanup(self.base.stop)
        self.storage = patch.object(accounts, 'storage_root', return_value=self.root)
        self.storage.start()
        self.addCleanup(self.storage.stop)

    def test_separate_paths_and_default_history_unchanged(self):
        self.assertEqual(accounts.paths('default')['data'], self.root)
        for name in ('account2', 'account3'):
            accounts.register(name, name + 'user')
        a, b = accounts.paths('account2'), accounts.paths('account3')
        for key in a:
            self.assertNotEqual(a[key], b[key])
        (a['data'] / 'solved.json').write_text('["1"]')
        (a['data'] / 'bot_state.json').write_text('{"blocked":true}')
        self.assertFalse((b['data'] / 'solved.json').exists())
        self.assertFalse((b['data'] / 'bot_state.json').exists())

    def test_unknown_invalid_and_rebinding_rejected(self):
        for name in ('../escape', 'CON', 'con', 'com1', 'a/b'):
            with self.assertRaises(ValueError):
                accounts.paths(name)
        with self.assertRaisesRegex(ValueError, '--setup'):
            accounts.expected_username('unknown')
        accounts.register('account2', 'alice')
        with self.assertRaises(ValueError):
            accounts.register('account2', 'bob')

    def test_default_binds_once_and_mismatch_stops(self):
        accounts.check_identity('default', 'alice')
        accounts.check_identity('default', 'Alice')
        with self.assertRaisesRegex(RuntimeError, 'expects alice'):
            accounts.check_identity('default', 'bob')
        self.assertEqual(accounts.expected_username('default'), 'alice')

    def test_login_rejects_wrong_username_before_csrf(self):
        accounts.register('account2', 'alice')
        with patch.object(bot, 'ACCOUNT_NAME', 'account2'), patch.object(bot, 'gql', return_value={
            'data': {'userStatus': {'isSignedIn': True, 'username': 'bob'}}
        }), patch.object(bot, 'get_csrf') as csrf:
            with self.assertRaisesRegex(RuntimeError, 'expects alice'):
                bot.verify_login(Mock())
            csrf.assert_not_called()

    def setup_run(self, name, answer=None):
        with patch.object(bot, 'ACCOUNT_NAME', name), patch.object(bot, 'setup_login') as login, \
                patch('builtins.input', return_value=answer) as prompt:
            bot.run_command(argparse.Namespace(setup=True))
            return login, prompt

    def test_setup_registers_expected_username_once(self):
        login, prompt = self.setup_run('account2', 'alice')
        prompt.assert_called_once()
        login.assert_called_once_with()
        self.assertEqual(accounts.expected_username('account2'), 'alice')
        login, prompt = self.setup_run('account2')
        prompt.assert_not_called()
        login.assert_called_once_with()

    def test_setup_rejects_email_and_leaves_account_unregistered(self):
        with self.assertRaises(ValueError):
            self.setup_run('account2', 'alice@example.com')
        self.assertFalse(accounts.paths('account2')['identity'].exists())

    def test_default_setup_does_not_prompt(self):
        login, prompt = self.setup_run('default')
        prompt.assert_not_called()
        login.assert_called_once_with()

    def test_main_reports_identity_failures_without_traceback(self):
        accounts.register('account2', 'alice')
        with patch.object(bot.sys, 'argv', ['bot', '--account', 'account2', '--setup']), \
                patch.object(bot, 'configure_account'), \
                patch.object(bot, 'setup_login', side_effect=RuntimeError('expects alice')), \
                patch.object(bot, 'exclusive_run'), patch.object(bot, 'say') as say:
            self.assertEqual(bot.main(), 1)
        self.assertIn('expects alice', ' '.join(str(c.args[0]) for c in say.call_args_list))

    def test_main_reports_missing_terminal_for_setup(self):
        with patch.object(bot.sys, 'argv', ['bot', '--account', 'account2', '--setup']), \
                patch.object(bot, 'configure_account'), \
                patch.object(bot, 'setup_login', side_effect=EOFError), \
                patch.object(bot, 'exclusive_run'), patch.object(bot, 'say') as say:
            self.assertEqual(bot.main(), 1)
        self.assertIn('terminal', ' '.join(str(c.args[0]) for c in say.call_args_list))

    def test_account_locks_are_independent(self):
        accounts.register('account2', 'alice')
        accounts.register('account3', 'bob')
        with exclusive_run(accounts.paths('account2')['lock']):
            with exclusive_run(accounts.paths('account3')['lock']):
                pass

    def batch(self, codes):
        with patch.object(run_daily, 'ROOT', self.root), patch.object(run_daily, 'run_account', side_effect=codes) as run:
            result = run_daily.run_batch(['default', 'account2', 'account3'], ['--count', '1'])
        return result, run

    def test_sequential_order_and_single_jitter(self):
        code, run = self.batch([0, 0, 0])
        self.assertEqual(code, 0)
        self.assertEqual([c.args[0] for c in run.call_args_list], ['default', 'account2', 'account3'])
        self.assertEqual([c.kwargs['skip_jitter'] for c in run.call_args_list], [False, True, True])

    def test_account_failure_continues_shared_failure_stops(self):
        code, run = self.batch([1, 0, 0])
        self.assertEqual((code, run.call_count), (1, 3))
        for reason in (75, 130, -9):
            code, run = self.batch([reason])
            self.assertNotEqual(code, 0)
            self.assertEqual(run.call_count, 1)
        log = next((self.root / 'logs').glob('batch-*.log')).read_text()
        self.assertIn('account3: skipped', log)

    def test_batch_validates_every_account_before_any_launch(self):
        with patch.object(run_daily.sys, 'argv', ['runner', '--accounts', 'default', 'missing']), patch.object(run_daily, 'run_batch') as batch:
            self.assertEqual(run_daily.main(), 1)
            batch.assert_not_called()

    def test_overlapping_batch_rejected(self):
        with exclusive_run(self.root / '.batch.lock'), patch.object(run_daily.sys, 'argv', ['runner']), patch.object(run_daily, 'run_batch') as batch:
            self.assertEqual(run_daily.main(), 1)
            batch.assert_not_called()

    def test_report_heading_identifies_account(self):
        import reporting
        from openpyxl import load_workbook
        target = self.root / 'report.xlsx'
        reporting.export_excel(self.root / 'attempts.db', target, account_name='account2')
        book = load_workbook(target)
        try:
            self.assertIn('account2', book['Summary']['A1'].value)
        finally:
            book.close()
