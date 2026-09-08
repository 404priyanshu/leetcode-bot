"""Real browser tests using intercepted requests; no live accounts or submissions."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from patchright.sync_api import sync_playwright

import leetcode_bot as bot
from runtime import NetworkUnavailable


HTML = '''<!doctype html><html><body>
<pre><code>1\n2\n3</code></pre>
<pre><code>class Solution { public: int solve() { return 1; } };</code></pre>
<div style="display:none"><pre><code>class Solution:
    def solve(self):
        # Python is in an inactive language tab.
        return 1
</code></pre></div></body></html>'''
URL = 'https://walkccc.me/LeetCode/problems/1512/'


@unittest.skipUnless(os.environ.get('BROWSER_TESTS') == '1', 'set BROWSER_TESTS=1 for real Chromium tests')
class BrowserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.playwright = sync_playwright().start()
        try:
            options = {'channel': 'chrome'} if os.environ.get('BOT_TEST_CHROME') == '1' else {}
            cls.browser = cls.playwright.chromium.launch(headless=True, **options)
        except BaseException:
            cls.playwright.stop()
            raise

    @classmethod
    def tearDownClass(cls):
        cls.browser.close()
        cls.playwright.stop()

    def setUp(self):
        self.context = self.browser.new_context()
        self.addCleanup(self.context.close)
        # Block every external request unless explicitly fulfilled below.
        self.context.route('**/*', lambda route: route.abort('blockedbyclient'))
        self.page = self.context.new_page()
        self.notices = patch.object(bot, 'network_notice').start()
        self.addCleanup(patch.stopall)
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        patch.object(bot, 'STATE_FILE', Path(directory.name) / 'state.json').start()
        patch.object(bot, 'say').start()
        # Let Chromium finish its error-page transition without multi-second waits.
        patch('runtime.time.sleep', side_effect=lambda _: self.page.wait_for_timeout(100)).start()

    def test_in_page_graphql_fetch_retries_a_read(self):
        self.page.route('https://leetcode.com/', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<html></html>'))
        self.page.goto('https://leetcode.com/')
        methods = []
        def handle(route):
            methods.append(route.request.method)
            if len(methods) == 1:
                route.abort('connectionreset')
            else:
                route.fulfill(status=200, content_type='application/json', body='{"data": {}}')
        self.page.route('https://leetcode.com/graphql', handle)
        self.assertEqual(bot.gql(self.page, 'query { fixture }'), {'data': {}})
        self.assertEqual(methods, ['POST', 'POST'])

    def test_in_page_write_fetch_is_never_resent_on_disconnect(self):
        self.page.route('https://leetcode.com/', lambda route: route.fulfill(
            status=200, content_type='text/html', body='<html></html>'))
        self.page.goto('https://leetcode.com/')
        requests = []
        def handle(route):
            requests.append(route.request.method)
            route.abort('connectionreset')
        self.page.route('https://leetcode.com/fixture', handle)
        with self.assertRaisesRegex(NetworkUnavailable, 'not resent'):
            bot.api_fetch(self.page, 'https://leetcode.com/fixture', 'POST', payload={})
        self.assertEqual(requests, ['POST'])

    def test_hidden_python_tab_preserves_indentation(self):
        self.page.route(URL, lambda route: route.fulfill(status=200, content_type='text/html', body=HTML))
        code = bot.get_python_solution(self.page, '1512')
        self.assertIn('    def solve(self):\n', code)
        self.assertNotIn('# Python', code)
        compile(code, '<scraped>', 'exec')

    def test_real_connection_failure_recovers_on_same_problem(self):
        attempts = []
        def handle(route):
            attempts.append(route.request.url)
            if len(attempts) < 3:
                route.abort('connectionrefused')
            else:
                route.fulfill(status=200, content_type='text/html', body=HTML)
        self.page.route(URL, handle)
        self.assertIn('class Solution:', bot.get_python_solution(self.page, '1512'))
        self.assertEqual(attempts, [URL] * 3)

    def test_real_server_outage_is_not_a_missing_solution(self):
        self.page.route(URL, lambda route: route.fulfill(status=503, body='Unavailable'))
        with self.assertRaises(NetworkUnavailable):
            bot.get_python_solution(self.page, '1512')
        self.assertEqual(self.notices.call_count, 3)

    def test_genuine_404_and_cpp_only_page_are_missing_solutions(self):
        self.page.route(URL, lambda route: route.fulfill(status=404, body='Missing'))
        with self.assertRaises(bot.NoSolutionYet):
            bot.get_python_solution(self.page, '1512')
        self.page.unroute(URL)
        self.page.route(URL, lambda route: route.fulfill(status=200, content_type='text/html',
            body='<pre><code>class Solution { };</code></pre>'))
        with self.assertRaises(bot.NoSolutionYet):
            bot.get_python_solution(self.page, '1512')
