import argparse
from contextlib import nullcontext
import io
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
        for attribute in ('ACCOUNT_NAME', 'PROFILE_DIR', 'SOLVED_FILE', 'LOG_FILE',
                          'STATE_FILE', 'HISTORY_DB', 'REPORT_FILE', 'LOCK_FILE'):
            keep = patch.object(bot, attribute, getattr(bot, attribute))
            keep.start()
            self.addCleanup(keep.stop)

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

    def test_list_accounts_orders_default_first_and_ignores_strays(self):
        for name in ('account3', 'account2'):
            accounts.register(name, name + 'user')
        (self.root / 'accounts' / 'halfway').mkdir(parents=True)
        (self.root / 'accounts' / 'Bad Name').mkdir(parents=True)
        self.assertEqual(accounts.list_accounts(), ['default', 'account2', 'account3'])

    BASE_CFG = {'difficulties': ('easy',), 'count': 1, 'timing': 'instant',
                'interactive': True, 'accounts': ['default']}

    def row_index(self, key):
        """Resolve a menu row by key so tests survive menu reordering."""
        rows = bot.menu_rows(self.BASE_CFG, ['default'])
        return next(i for i, row in enumerate(rows)
                    if not isinstance(row, bot.Heading) and row[0] == key)

    def menu(self, keys, checks=(), cfg=None):
        picks = [self.row_index(key) for key in keys]

        def choose(title, options, default=0, hints=None, header=None):
            return picks.pop(0)

        with patch.object(bot, 'select', side_effect=choose), \
                patch.object(bot, 'checkbox', side_effect=list(checks)), \
                patch.object(bot, 'setup_login'):
            return bot.run_menu({**self.BASE_CFG, **(cfg or {})})

    def test_menu_selects_several_accounts_to_run(self):
        for name in ('account2', 'account3'):
            accounts.register(name, name + 'user')
        cfg = self.menu(['accounts', 'start'], checks=[[True, True, False]])
        self.assertEqual(cfg['accounts'], ['default', 'account2'])

    def test_menu_adds_account_and_includes_it_in_the_run(self):
        with patch.object(bot, 'ask', side_effect=['jaagrett', 'Jaagrett']), \
                patch.object(bot, 'confirm', return_value=True):
            cfg = self.menu(['add', 'start'])
        self.assertEqual(cfg['accounts'], ['default', 'jaagrett'])
        self.assertEqual(accounts.expected_username('jaagrett'), 'Jaagrett')

    def test_menu_add_account_can_be_cancelled_at_every_step(self):
        for answers, confirmed in ((['jaagrett', None], True), ([None], True),
                                   (['jaagrett', 'Jaagrett'], False)):
            with patch.object(bot, 'ask', side_effect=answers), \
                    patch.object(bot, 'confirm', return_value=confirmed), \
                    patch.object(bot, 'account_setup') as setup:
                cfg = self.menu(['add', 'start'])
            self.assertEqual(cfg['accounts'], ['default'])
            setup.assert_not_called()
        self.assertEqual(accounts.list_accounts(), ['default'])

    def test_new_account_answers_are_validated_before_the_browser_opens(self):
        accounts.register('account2', 'alice')
        for bad in ('account2', '../escape', 'con', 'com1', 'a b', ''):
            with self.assertRaises(ValueError):
                bot._new_name(bad)
        self.assertEqual(bot._new_name('  Jaagrett '), 'jaagrett')
        for bad in ('', '   ', 'me@example.com'):
            with self.assertRaises(ValueError):
                bot._new_username(bad)
        self.assertEqual(bot._new_username(' Jaagrett '), 'Jaagrett')

    def test_menu_login_targets_the_selected_account(self):
        accounts.register('account2', 'alice')
        with patch.object(bot, 'account_setup') as setup:
            self.menu(['login', 'quit'], cfg={'accounts': ['account2']})
        setup.assert_called_once_with('account2')

    def test_menu_header_shows_setup_state_and_selection(self):
        accounts.register('account2', 'alice')
        (accounts.paths('account2')['data'] / 'solved.json').write_text('["1", "2"]')
        lines = bot.account_header(['account2'])
        self.assertTrue(any('not set up yet' in line for line in lines))
        marked = next(line for line in lines if 'account2' in line)
        self.assertIn('alice', marked)
        self.assertIn('2 solved', marked)
        self.assertIn('never run', marked)

    def test_signed_in_account_without_a_recorded_username_is_not_called_unset(self):
        paths = accounts.paths('default')
        paths['profile'].mkdir(parents=True)
        (paths['profile'] / 'Cookies').write_text('x')
        (paths['data'] / 'solved.json').write_text('["1", "2", "3"]')
        name, detail = bot.account_summary('default')
        self.assertNotIn('not set up', detail)
        self.assertIn('3 solved', detail)
        self.assertIn('username saved on next run', detail)

    def test_account_with_nothing_saved_is_called_unset(self):
        self.assertIn('not set up yet', bot.account_summary('default')[1])

    QUEUE_CFG = {
        'difficulties': ('easy',), 'count': None, 'timing': 'instant',
        'interactive': True, 'skip_start_jitter': True,
    }

    def run_queue(self, targets, outcomes, deadlines=None, gap=60, timing='instant'):
        names = ['default', 'account2']
        seen, active, max_active = [], 0, 0
        now = [0.0]

        def turn(name, cfg, target):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            seen.append((name, target))
            outcome = outcomes.pop(0)
            active -= 1
            return outcome

        waits = []

        def wait_until(deadline):
            waits.append(deadline)
            now[0] = deadline
            return False

        view = Mock()
        with patch.object(bot, 'pick_count', side_effect=targets) as pick, \
                patch.object(bot, '_account_deadline', side_effect=deadlines or [0, 0]), \
                patch.object(bot, '_save_account_deadline'):
            code = bot.run_account_queue(
                {**self.QUEUE_CFG, 'timing': timing}, names, view, turn_runner=turn,
                clock=lambda: now[0], waiter=wait_until, gap_picker=lambda: gap,
            )
        return code, seen, waits, view, pick.call_count, max_active

    def test_round_robin_order_with_unequal_random_targets_sampled_once(self):
        accepted = lambda: bot.TurnOutcome(0, accepted=1, status='Completed')
        code, seen, _, _, samples, _ = self.run_queue(
            [2, 1], [accepted(), accepted(), accepted()]
        )
        self.assertEqual(code, 0)
        self.assertEqual(seen, [('default', 2), ('account2', 1), ('default', 2)])
        self.assertEqual(samples, 2)

    def test_explicit_count_applies_to_each_account_without_concurrency(self):
        outcomes = [bot.TurnOutcome(0, accepted=1) for _ in range(4)]
        with patch.object(bot, 'pick_count', wraps=bot.pick_count) as pick, \
                patch.object(bot, '_account_deadline', return_value=0), \
                patch.object(bot, '_save_account_deadline'):
            seen, active, peak = [], 0, 0

            def turn(name, cfg, target):
                nonlocal active, peak
                active += 1
                peak = max(peak, active)
                seen.append((name, target))
                active -= 1
                return outcomes.pop(0)

            code = bot.run_account_queue(
                {**self.QUEUE_CFG, 'count': 2}, ['default', 'account2'], Mock(),
                turn_runner=turn, gap_picker=lambda: 0,
            )
        self.assertEqual((code, peak), (0, 1))
        self.assertEqual(seen, [('default', 2), ('account2', 2)] * 2)
        self.assertEqual(pick.call_count, 2)

    def test_each_turn_uses_the_retained_target_without_redrawing(self):
        result = bot.SessionResult(target=1, done=1, attempts=1, paced=True)
        with patch.object(bot, 'configure_account'), \
                patch.object(bot.accounts, 'expected_username'), \
                patch.object(bot, 'exclusive_run', return_value=nullcontext()), \
                patch.object(bot, 'execute_session', return_value=(0, result)) as execute, \
                patch.object(bot, 'pick_count') as pick:
            outcome = bot.run_one_account_turn(
                'default', {**self.QUEUE_CFG, 'accepted_before': 2}, 5
            )
        pick.assert_not_called()
        turn_cfg = execute.call_args.args[0]
        self.assertEqual(
            (turn_cfg['session_target'], turn_cfg['report_target'],
             turn_cfg['accepted_before']),
            (1, 5, 2),
        )
        self.assertEqual((outcome.accepted, outcome.attempted), (1, True))

    def test_scheduler_switches_to_eligible_account_before_waiting(self):
        paced = lambda: bot.TurnOutcome(0, accepted=1, paced=True)
        code, seen, waits, _, _, _ = self.run_queue(
            [2, 1], [paced(), paced(), paced()], gap=60, timing='human'
        )
        self.assertEqual(code, 0)
        self.assertEqual([name for name, _ in seen[:2]], ['default', 'account2'])
        self.assertEqual(waits, [60])

    def test_scheduler_waits_for_earliest_account_when_all_are_cooling_down(self):
        done = bot.TurnOutcome(0, accepted=1)
        code, seen, waits, view, _, _ = self.run_queue(
            [1, 1], [done, done], deadlines=[30, 20], timing='human'
        )
        self.assertEqual(code, 0)
        self.assertEqual(waits, [20, 30])
        self.assertEqual([name for name, _ in seen], ['account2', 'default'])
        self.assertTrue(any(call.args[:2] == ('account2', 20) for call in view.next.call_args_list))

    def test_scheduler_chooses_an_eligible_account_instead_of_waiting(self):
        done = bot.TurnOutcome(0, accepted=1)
        code, seen, waits, _, _, _ = self.run_queue(
            [1, 1], [done, done], deadlines=[60, 0], timing='human'
        )
        self.assertEqual(code, 0)
        self.assertEqual([name for name, _ in seen], ['account2', 'default'])
        self.assertEqual(waits, [60])

    def test_account_failure_continues_but_shared_failure_stops_queue(self):
        failed = bot.TurnOutcome(1, attempted=False, status='Failed', reason='login expired')
        done = bot.TurnOutcome(0, accepted=1)
        code, seen, _, _, _, _ = self.run_queue([1, 1], [failed, done])
        self.assertEqual((code, [name for name, _ in seen]), (1, ['default', 'account2']))

        shared = bot.TurnOutcome(75, attempted=False, status='Network error')
        code, seen, _, view, _, _ = self.run_queue([1, 1], [shared])
        self.assertEqual((code, [name for name, _ in seen]), (1, ['default']))
        self.assertTrue(any(
            call.args[:2] == ('account2', 'Failed')
            for call in view.set_state.call_args_list
        ))

        interrupted = bot.TurnOutcome(130, attempted=False, status='Interrupted')
        code, seen, _, _, _, _ = self.run_queue([1, 1], [interrupted])
        self.assertEqual((code, [name for name, _ in seen]), (130, ['default']))

    def test_run_accounts_single_keeps_plain_exit_code(self):
        seen = []

        def session(cfg):
            seen.append((bot.ACCOUNT_NAME, cfg.get('batch_child', False)))
            return 130

        with patch.object(bot, 'execute_session', side_effect=session), patch.object(bot, 'say'):
            self.assertEqual(bot.run_accounts({'accounts': ['default']}), 130)
        self.assertEqual(seen, [('default', False)])

    def options(self, **overrides):
        chosen = dict(setup=False, setup_telegram=False, export_report=False,
                      no_menu=False, count=None, instant=False, difficulty=None)
        chosen.update(overrides)
        return argparse.Namespace(**chosen)

    def test_wants_menu_only_for_a_bare_interactive_run(self):
        terminal = Mock(isatty=Mock(return_value=True))
        with patch.object(bot.sys, 'stdin', terminal), patch.object(bot.sys, 'stdout', terminal):
            self.assertTrue(bot.wants_menu(self.options()))
            for overrides in (dict(setup=True), dict(setup_telegram=True), dict(export_report=True),
                              dict(no_menu=True), dict(count=2), dict(instant=True),
                              dict(difficulty=('easy',))):
                self.assertFalse(bot.wants_menu(self.options(**overrides)))

    def batch(self, code=0, **options):
        process = Mock(stdout=io.StringIO('phase\n'))
        process.wait.return_value = code
        with patch.object(run_daily, 'ROOT', self.root), \
                patch.object(run_daily.subprocess, 'Popen', return_value=process) as launch:
            result = run_daily.run_batch(
                ['default', 'account2', 'account3'], ['--count', '1'], **options
            )
        return result, launch

    def test_no_jitter_reaches_the_single_scheduler_process(self):
        code, launch = self.batch(jitter=False)
        self.assertEqual(code, 0)
        self.assertIn('--skip-start-jitter', launch.call_args.args[0])

    def test_no_jitter_flag_reaches_the_batch(self):
        for argv, expected in ((['runner'], True), (['runner', '--no-jitter'], False)):
            with patch.object(run_daily.sys, 'argv', argv), \
                    patch.object(run_daily, 'run_batch', return_value=0) as batch, \
                    patch.object(run_daily.accounts, 'storage_root', return_value=self.root):
                self.assertEqual(run_daily.main(), 0)
            self.assertEqual(batch.call_args.kwargs['jitter'], expected)

    def test_batch_only_forwards_an_explicit_count(self):
        cases = (
            (['runner'], ['--difficulty', 'easy']),
            (['runner', '--count', '3'], ['--count', '3', '--difficulty', 'easy']),
        )
        for argv, expected in cases:
            with self.subTest(argv=argv), patch.object(run_daily.sys, 'argv', argv), \
                    patch.object(run_daily, 'run_batch', return_value=0) as batch, \
                    patch.object(run_daily.accounts, 'storage_root', return_value=self.root):
                self.assertEqual(run_daily.main(), 0)
            self.assertEqual(batch.call_args.args[1], expected)

    def test_hermes_launches_one_process_with_accounts_in_order(self):
        code, launch = self.batch()
        self.assertEqual(code, 0)
        command = launch.call_args.args[0]
        start = command.index('--accounts') + 1
        self.assertEqual(command[start:start + 3], ['default', 'account2', 'account3'])
        self.assertIn('--batch-child', command)
        self.assertNotIn('--skip-start-jitter', command)

    def test_hermes_preserves_scheduler_exit_code(self):
        for expected in (1, 75, 130):
            code, launch = self.batch(expected)
            self.assertEqual(code, expected)
            self.assertEqual(launch.call_count, 1)

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
