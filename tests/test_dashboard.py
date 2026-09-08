import unittest
from unittest.mock import Mock, patch

import dashboard


class EstimateTests(unittest.TestCase):
    def test_human_pacing_gives_a_range_and_instant_does_not(self):
        low, high = dashboard.estimate(accounts=2, per_account=6, timing='human')
        self.assertLess(low, high)
        work = 12 * dashboard.SECONDS_PER_PROBLEM
        self.assertEqual(low, work + 10 * dashboard.MIN_GAP * 60)
        self.assertEqual(high, work + 10 * dashboard.MAX_GAP * 60)
        self.assertEqual(dashboard.estimate(2, 6, 'instant'), (work, work))

    def test_a_single_problem_has_no_gap_to_wait_through(self):
        self.assertEqual(dashboard.estimate(1, 1, 'human'),
                         (dashboard.SECONDS_PER_PROBLEM,) * 2)

    def test_time_reads_in_hours_once_it_passes_an_hour(self):
        self.assertEqual(dashboard.human_time(59), '1 min')
        self.assertEqual(dashboard.human_time(35 * 60), '35 min')
        self.assertEqual(dashboard.human_time(60 * 60), '1 h')
        self.assertEqual(dashboard.human_time(128 * 60), '2 h 8 min')

    def test_bar_fills_in_proportion_and_never_overflows(self):
        self.assertEqual(dashboard.bar(0, 6, width=6), '░' * 6)
        self.assertEqual(dashboard.bar(3, 6, width=6), '███░░░')
        self.assertEqual(dashboard.bar(6, 6, width=6), '█' * 6)
        self.assertEqual(dashboard.bar(9, 6, width=6), '█' * 6)
        self.assertEqual(dashboard.bar(1, 0, width=6), '░' * 6)

    def test_plan_names_every_account_and_the_total(self):
        rows, notes = dashboard.plan_lines(['a', 'b'], 6, ('easy',), 'human')
        self.assertEqual([name for name, _ in rows], ['a', 'b'])
        self.assertIn('12 problems', notes[0])
        self.assertIn('human-like', notes[0])
        self.assertIn('–', notes[1])

    def test_plan_is_honest_when_the_count_is_random(self):
        rows, notes = dashboard.plan_lines(['a'], None, ('easy', 'hard'), 'instant')
        self.assertEqual(rows, [('a', 'random, 1-9 problems')])
        self.assertIn('about 5 per account', notes[0])
        self.assertIn('easy+hard', notes[0])
        self.assertIn('no waiting', notes[0])


class ViewSelectionTests(unittest.TestCase):
    def test_a_plain_pipe_gets_no_live_panel(self):
        with patch.object(dashboard.sys, 'stdout', Mock(isatty=Mock(return_value=False))):
            self.assertFalse(dashboard.supported())
            self.assertIsInstance(dashboard.build(['default']), dashboard.NullView)

    def test_missing_rich_falls_back_instead_of_failing(self):
        with patch.object(dashboard, 'Console', None):
            self.assertFalse(dashboard.supported())

    def test_null_view_accepts_every_update_and_prints_logs(self):
        view = dashboard.NullView()
        with view:
            view.account('a', 1, 2)
            view.target(3)
            view.problem(1, 3, '1', 'Two Sum', 'Easy')
            view.stage('submitting ...')
            view.accepted(1)
            view.countdown(90, 120)
            view.clear_countdown()
        self.assertFalse(view.active)

    def test_run_view_tracks_progress_without_a_live_region(self):
        view = dashboard.RunView(['default', 'jaagrett'])
        view.account('jaagrett', 2, 2)
        view.target(6)
        view.accepted(2)
        view.problem(2, 6, '338', 'Counting Bits', 'Easy')
        view.countdown(462, 700, '· press s to skip')
        panel = view._render()
        self.assertEqual((view.done, view.want, view.position), (2, 6, 2))
        self.assertIn('7:42', view.wait)
        view.stage('submitting ...')
        self.assertEqual(view.wait, '')
        self.assertIsNotNone(panel)
