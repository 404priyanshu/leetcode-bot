import argparse
import json
import sqlite3
from contextlib import closing
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import Mock, call, patch

import leetcode_bot as bot
import reporting
from runtime import NetworkUnavailable, exclusive_run


def problem(number='1'):
    return dict(frontendQuestionId=number, title=f'Problem {number}',
                titleSlug=f'problem-{number}', difficulty='Easy', isPaidOnly=False)


class IsolatedBotTest(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        directory = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for name, filename in (
            ('HISTORY_DB', 'history.db'), ('SOLVED_FILE', 'solved.json'),
            ('STATE_FILE', 'state.json'), ('LOG_FILE', 'activity.log'),
            ('LOCK_FILE', '.bot.lock'), ('REPORT_FILE', 'report.xlsx'),
            ('TELEGRAM_FILE', 'telegram.json'),
        ):
            self.stack.enter_context(patch.object(bot, name, directory / filename))
        self.stack.enter_context(patch.object(bot, 'say'))
        self.telegram = self.stack.enter_context(patch.object(bot, 'send_telegram'))
        self.report = self.stack.enter_context(patch.object(bot, 'refresh_excel_report'))
        self.stack.enter_context(patch('runtime.time.sleep'))
        self.cfg = dict(difficulties=('easy',), count=3, timing='instant', interactive=False)

    def rows(self, table):
        with closing(sqlite3.connect(bot.HISTORY_DB)) as conn:
            conn.row_factory = sqlite3.Row
            return [dict(row) for row in conn.execute(f'SELECT * FROM {table}')]

    def prepare_session(self, pool=None):
        self.stack.enter_context(patch.object(bot, 'sync_playwright'))
        self.context = Mock()
        self.launch = self.stack.enter_context(patch.object(bot, '_launch', return_value=self.context))
        self.navigate = self.stack.enter_context(patch.object(bot, 'navigate'))
        self.login = self.stack.enter_context(patch.object(bot, 'verify_login', return_value='csrf'))
        self.stack.enter_context(patch.object(bot, 'get_csrf', return_value='csrf'))
        self.pool = self.stack.enter_context(patch.object(bot, 'get_problem_list',
            return_value=pool if pool is not None else [problem(str(n)) for n in range(20)]))
        self.source = self.stack.enter_context(patch.object(bot, 'get_python_solution',
            return_value='class Solution:\n    def solve(self): return 1\n'))
        self.stack.enter_context(patch.object(bot, 'get_question_meta', return_value=('1', '1')))
        self.run = self.stack.enter_context(patch.object(bot, 'run_solution',
            return_value={'correct_answer': True}))
        self.submit = self.stack.enter_context(patch.object(bot, 'submit_solution',
            return_value={'status_code': 10, 'status_msg': 'Accepted', 'submission_id': 9}))

    def execute(self):
        with exclusive_run(bot.LOCK_FILE):
            return bot.execute_session(self.cfg)


class SessionTests(IsolatedBotTest):
    def test_happy_path_stops_at_acceptance_target(self):
        self.prepare_session()
        self.assertEqual(self.execute(), 0)
        self.assertEqual(self.submit.call_count, 3)
        self.assertEqual(len(self.rows('attempts')), 3)
        self.assertEqual(self.rows('sessions')[0]['status'], 'Completed')
        self.assertEqual(len(bot.load_solved()), 3)
        self.context.close.assert_called_once()
        self.report.assert_called_once()
        self.telegram.assert_called_once()

    def test_outage_after_two_acceptances_stops_without_consuming_pool(self):
        self.prepare_session()
        self.source.side_effect = ['def solve(): return 1', 'def solve(): return 1',
                                   NetworkUnavailable('net::ERR_NETWORK_CHANGED after 4 tries')]
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.submit.call_count, 2)
        self.assertEqual(self.source.call_count, 3)
        attempts = self.rows('attempts')
        self.assertEqual([row['outcome'] for row in attempts],
                         ['Accepted', 'Accepted', 'Network error'])
        self.assertEqual(attempts[-1]['stage'], 'Solution fetch')
        self.assertEqual(self.rows('sessions')[0]['status'], 'Network error')
        self.assertEqual(len(bot.load_solved()), 2)

    def test_startup_failures_and_empty_pool_are_recorded(self):
        self.prepare_session([])
        self.launch.side_effect = RuntimeError('Chrome failed to launch')
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.rows('sessions')[-1]['status'], 'Failed')
        self.launch.side_effect = None
        self.login.side_effect = RuntimeError('session expired')
        self.assertEqual(self.execute(), 1)
        self.assertIn('session expired', self.rows('sessions')[-1]['stop_reason'])
        self.login.side_effect = None
        self.pool.side_effect = RuntimeError('GraphQL invalid response')
        self.assertEqual(self.execute(), 1)
        self.assertIn('GraphQL', self.rows('sessions')[-1]['stop_reason'])
        self.pool.side_effect = None
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.rows('sessions')[-1]['status'], 'Partial')
        self.assertEqual(self.rows('attempts'), [])
        self.assertTrue(all(row['ended_at'] for row in self.rows('sessions')))

    def test_browser_navigation_failure_is_recorded_before_problem_selection(self):
        self.prepare_session()
        self.navigate.side_effect = NetworkUnavailable('offline')
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.rows('sessions')[0]['status'], 'Network error')
        self.assertEqual(self.rows('attempts'), [])

    def test_existing_cooldown_is_recorded_without_launching_browser(self):
        self.prepare_session()
        bot.update_runtime_state(api_circuit_open_until=bot.time.time() + 60)
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.rows('sessions')[0]['status'], 'Rate limited')
        self.launch.assert_not_called()

    def test_cloudflare_block_stops_without_restarting_session(self):
        self.prepare_session()
        self.navigate.side_effect = bot.CloudflareBlocked('Run --setup')
        self.assertEqual(self.execute(), 1)
        self.submit.assert_not_called()
        self.launch.assert_called_once()
        self.assertEqual(self.rows('sessions')[0]['status'], 'Rate limited')

    def test_interrupt_during_jitter_has_finished_session(self):
        self.prepare_session()
        self.cfg['timing'] = 'human'
        with patch.object(bot, '_wait_until', side_effect=KeyboardInterrupt):
            self.assertEqual(self.execute(), 130)
        self.assertEqual(self.rows('sessions')[0]['status'], 'Interrupted')
        self.launch.assert_not_called()

    def test_interrupt_and_browser_failure_finish_current_attempt(self):
        self.prepare_session()
        self.source.side_effect = KeyboardInterrupt
        self.assertEqual(self.execute(), 130)
        self.assertEqual(self.rows('attempts')[-1]['outcome'], 'Interrupted')
        self.source.side_effect = RuntimeError('Target page, context or browser has been closed')
        self.assertEqual(self.execute(), 1)
        self.assertEqual(self.rows('attempts')[-1]['outcome'], 'Browser error')
        self.assertEqual(self.rows('sessions')[-1]['status'], 'Browser error')

    def test_missing_solutions_are_skipped_without_pacing(self):
        self.prepare_session()
        self.source.side_effect = [bot.NoSolutionYet('C++ only')] + ['def solve(): return 1'] * 3
        with patch.object(bot, 'wait_before_next_question') as wait:
            self.assertEqual(self.execute(), 0)
        self.assertEqual(wait.call_count, 2)
        self.assertEqual(self.rows('attempts')[0]['outcome'], 'No solution')
        self.assertEqual(self.submit.call_count, 3)

    def test_failed_examples_are_never_submitted_and_attempt_budget_is_bounded(self):
        self.prepare_session()
        self.run.return_value = {'correct_answer': False, 'status_msg': 'Wrong Answer'}
        self.assertEqual(self.execute(), 1)
        self.submit.assert_not_called()
        self.assertEqual(len(self.rows('attempts')), 13)
        self.assertEqual(self.rows('sessions')[0]['status'], 'Partial')

    def test_accepted_sqlite_id_prevents_repeat_if_json_write_was_missed(self):
        old = reporting.start_session(bot.HISTORY_DB, ('easy',), 'instant', 1)
        attempt, started = reporting.start_attempt(bot.HISTORY_DB, old, problem('1'))
        reporting.finish_attempt(bot.HISTORY_DB, attempt, started, 'Submission', 'Accepted')
        reporting.finish_session(bot.HISTORY_DB, old, 'Completed')
        self.prepare_session([problem('1')])
        self.assertEqual(self.execute(), 1)
        self.source.assert_not_called()

    def test_json_write_failure_does_not_relabel_accepted_submission(self):
        self.prepare_session()
        with patch.object(bot, 'save_solved', side_effect=OSError('disk full')):
            self.assertEqual(self.execute(), 1)
        self.assertEqual([row['outcome'] for row in self.rows('attempts')], ['Accepted'])
        self.assertEqual(self.rows('sessions')[0]['status'], 'Failed')

    def test_batch_child_reports_shared_failure_without_submission(self):
        self.prepare_session()
        self.cfg['batch_child'] = True
        self.navigate.side_effect = NetworkUnavailable('offline')
        self.assertEqual(self.execute(), 75)
        self.submit.assert_not_called()

    def test_batch_child_reports_cooldown_without_launch(self):
        self.prepare_session()
        self.cfg['batch_child'] = True
        bot.update_runtime_state(api_circuit_open_until=bot.time.time() + 60)
        self.assertEqual(self.execute(), 75)
        self.launch.assert_not_called()

    def test_batch_child_stops_after_cleanup_failure(self):
        self.prepare_session()
        self.cfg['batch_child'] = True
        self.context.close.side_effect = RuntimeError('Chrome still running')
        self.assertEqual(self.execute(), 75)

    def test_lock_rejection_does_not_start_or_recover_session(self):
        old = reporting.start_session(bot.HISTORY_DB, ('easy',), 'instant', 1)
        with exclusive_run(bot.LOCK_FILE), patch.object(sys, 'argv', ['bot', '--instant']), patch.object(bot, 'configure_account'), patch.object(bot.accounts, 'expected_username', return_value=None):
            self.assertEqual(bot.main(), 1)
        self.assertEqual(len(self.rows('sessions')), 1)
        self.assertEqual(self.rows('sessions')[0]['session_id'], old)
        self.assertEqual(self.rows('sessions')[0]['status'], 'Running')


class ApiTests(IsolatedBotTest):
    def test_navigation_recovers_same_url_from_reported_errors(self):
        page = Mock()
        page.goto.side_effect = [RuntimeError('net::ERR_NETWORK_CHANGED'),
                                RuntimeError('net::ERR_CONNECTION_REFUSED'), Mock(status=200)]
        self.assertEqual(bot.navigate(page, 'https://walkccc.me/test').status, 200)
        self.assertEqual({call_.args[0] for call_ in page.goto.call_args_list},
                         {'https://walkccc.me/test'})

    def test_cloudflare_page_challenge_clears_and_retries_once(self):
        page = Mock()
        page.title.side_effect = ['Just a moment...', 'Two Sum - LeetCode']
        page.goto.side_effect = [Mock(status=403), Mock(status=200)]
        url = bot.LEETCODE + '/problems/two-sum/'
        self.assertEqual(bot.navigate(page, url).status, 200)
        self.assertEqual([call_.args[0] for call_ in page.goto.call_args_list], [url, url])
        self.assertNotIn('api_circuit_open_until', bot.load_runtime_state())

    def test_plain_403_page_load_retries_once_before_breaker(self):
        page = Mock()
        page.title.return_value = 'Access denied | LeetCode'
        page.goto.return_value = Mock(status=403)
        with self.assertRaises(bot.CircuitBreakerOpen) as caught:
            bot.navigate(page, bot.LEETCODE)
        self.assertNotIsInstance(caught.exception, bot.CloudflareBlocked)
        self.assertEqual(page.goto.call_count, 2)
        self.assertGreater(bot.load_runtime_state()['api_circuit_open_until'],
                           bot.time.time())

    def test_persistent_cloudflare_block_raises_and_opens_circuit(self):
        page = Mock()
        page.title.return_value = 'Just a moment...'
        page.goto.return_value = Mock(status=403)
        with self.assertRaises(bot.CloudflareBlocked):
            bot.navigate(page, bot.LEETCODE)
        self.assertEqual(page.goto.call_count, 2)
        self.assertGreater(bot.load_runtime_state()['api_circuit_open_until'],
                           bot.time.time())

    def test_api_cloudflare_challenge_that_never_clears_blocks(self):
        page = Mock()
        page.evaluate.return_value = {'status': 403, 'text': 'Just a moment...'}
        with patch.object(bot, 'navigate') as nav:
            with self.assertRaises(bot.CloudflareBlocked):
                bot.api_fetch(page, bot.LEETCODE + '/graphql', 'POST',
                              retry_safe=True)
        self.assertGreater(bot.load_runtime_state()['api_circuit_open_until'],
                           bot.time.time())

    def test_readonly_graphql_retries_but_submission_never_replays(self):
        page = Mock()
        page.evaluate.side_effect = [RuntimeError('TypeError: Failed to fetch'),
                                     {'status': 200, 'text': '{"data": {}}'}]
        self.assertEqual(bot.gql(page, 'query { foo }'), {'data': {}})
        self.assertEqual(page.evaluate.call_count, 2)
        page.reset_mock()
        page.evaluate.side_effect = RuntimeError('TypeError: Failed to fetch')
        with self.assertRaisesRegex(NetworkUnavailable, 'not resent'):
            bot.submit_solution(page, 'csrf', 'one', '1', 'code')
        self.assertEqual(page.evaluate.call_count, 1)

    def test_submission_server_error_does_not_replay(self):
        page = Mock()
        page.evaluate.return_value = {'status': 503, 'text': 'unavailable'}
        with self.assertRaises(NetworkUnavailable):
            bot.submit_solution(page, 'csrf', 'one', '1', 'code')
        self.assertEqual(page.evaluate.call_count, 1)

    def test_poll_recovers_without_resubmitting_and_records_id_first(self):
        page = Mock()
        page.evaluate.side_effect = [
            {'status': 200, 'text': '{"submission_id": 123}'},
            RuntimeError('TypeError: Failed to fetch'),
            {'status': 200, 'text': '{"state": "SUCCESS", "status_code": 10}'},
        ]
        received = Mock()
        result = bot.submit_solution(page, 'csrf', 'one', '1', 'code', received)
        self.assertEqual(result['submission_id'], 123)
        received.assert_called_once_with(123)
        methods = [call.args[1][1] for call in page.evaluate.call_args_list]
        self.assertEqual(methods, ['POST', 'GET', 'GET'])

    def test_poll_timeout_is_explicit_instead_of_unknown_wrong_answer(self):
        with patch.object(bot, 'api_fetch', return_value=(200, '{"state": "PENDING"}')):
            with self.assertRaisesRegex(NetworkUnavailable, 'result 123'):
                bot.poll_check(Mock(), 123)

    def test_rate_limit_persists_and_short_circuits_next_call(self):
        page = Mock()
        page.evaluate.return_value = {'status': 429, 'text': 'rate limited'}
        with self.assertRaises(bot.CircuitBreakerOpen):
            bot.api_fetch(page, bot.LEETCODE + '/graphql', 'POST', retry_safe=True)
        with self.assertRaises(bot.CircuitBreakerOpen):
            bot.api_fetch(page, bot.LEETCODE + '/graphql', 'POST', retry_safe=True)
        self.assertEqual(page.evaluate.call_count, 1)

    def test_graphql_errors_and_expired_login_are_explicit(self):
        with patch.object(bot, 'api_fetch', return_value=(200, '{"errors":[{"message":"bad"}]}')):
            with self.assertRaisesRegex(RuntimeError, 'GraphQL errors'):
                bot.gql(Mock(), 'query {}')
        with patch.object(bot, 'gql', return_value={'data': {'userStatus': {'isSignedIn': False}}}):
            with self.assertRaisesRegex(RuntimeError, '--setup'):
                bot.verify_login(Mock())


class StateTests(IsolatedBotTest):
    def test_interrupted_wait_is_preserved_then_cleared_on_resume(self):
        self.cfg['timing'] = 'human'
        with patch.object(bot, '_wait_until', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                bot.wait_before_next_question(self.cfg)
        deadline = bot.load_runtime_state()['next_question_at']
        with patch.object(bot, '_wait_until', return_value=True) as wait:
            bot.wait_for_saved_question_slot(self.cfg)
        wait.assert_called_once_with(deadline)
        self.assertNotIn('next_question_at', bot.load_runtime_state())

    def test_instant_clears_saved_gap_but_not_api_cooldown(self):
        bot.update_runtime_state(next_question_at=123, api_circuit_open_until=456)
        bot.wait_for_saved_question_slot(self.cfg)
        self.assertEqual(bot.load_runtime_state(), {'api_circuit_open_until': 456})

    def test_solved_ids_are_normalized_and_write_is_atomic(self):
        bot.save_solved([1, '1', '2'])
        self.assertEqual(bot.load_solved(), ['1', '2'])
        with patch.object(Path, 'replace', side_effect=OSError('failed rename')):
            with self.assertRaises(OSError):
                bot.save_solved(['3'])
        self.assertEqual(bot.load_solved(), ['1', '2'])


class InputAndExtractionTests(unittest.TestCase):
    def test_difficulties_validate_and_deduplicate(self):
        self.assertEqual(bot.parse_difficulty(' Easy, medium, easy '), ('easy', 'medium'))
        self.assertEqual(bot.parse_difficulty(' ALL '), bot.DIFFICULTIES)
        for value in ('', 'eazy', 'easy,nope', 'easy,', 'all,easy'):
            with self.subTest(value=value), self.assertRaises(argparse.ArgumentTypeError):
                bot.parse_difficulty(value)

    def test_invalid_cli_arguments_exit_before_any_work(self):
        for flag, value in (('--count', '0'), ('--count', '-3'), ('--count', '1.5'),
                            ('--difficulty', 'easy,typo')):
            result = subprocess.run([sys.executable, '-B', str(Path(bot.__file__)), flag, value],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn('error:', result.stderr)

    def test_extraction_ignores_gutters_and_other_languages(self):
        code = 'class Solution:\n    def solve(self):\n        return 1'
        self.assertEqual(bot.extract_python_solution(['1\n2\n3', 'class Solution { int x; };', code]), code)

    def test_design_class_without_return_is_valid_python(self):
        code = 'class MyQueue:\n    def __init__(self):\n        self.items = []'
        self.assertEqual(bot.extract_python_solution([code]), code)

    def test_absent_or_invalid_python_is_not_submitted(self):
        for blocks in (['class Solution { };'], ['def broken( : return 1'], ['1\n2']):
            with self.assertRaises(bot.NoSolutionYet):
                bot.extract_python_solution(blocks)

    def test_comment_stripping_keeps_string_literals_and_compilable_body(self):
        code = 'class Solution:\n    """doc"""\n    def solve(self):\n        return "#literal" #comment\n'
        cleaned = bot.strip_comments(code)
        self.assertIn('"#literal"', cleaned)
        self.assertNotIn('#comment', cleaned)
        compile(cleaned, '<solution>', 'exec')
        only_docstring = 'def solve():\n    """only body"""\n'
        self.assertEqual(bot.strip_comments(only_docstring), only_docstring)
