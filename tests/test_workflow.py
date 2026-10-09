import copy
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from PySide6.QtWidgets import QApplication
from ai_searcher import AISearcher, AIError
from cxapi.chapters import ClassChapters
from cxapi.jobs.exam import ChapterExam
from cxapi.jobs.video import ChapterVideo
from cxapi.network import TimeoutSession
from cxapi.schema import AccountInfo, QuestionEnum, QuestionModel
from engine import chapter_snapshot, first_pending_course, run_course
from gui import MainWindow
from runtime import Cancelled, TaskContext
from settings import DEFAULTS
from test_ai import Response

APP = QApplication.instance() or QApplication([])
ACCOUNT = AccountInfo(1, '测试', '未知', '', '', '')


def course(completed=(), size=3):
    chapters = ClassChapters(Mock(), ACCOUNT, 1, 2, 3,
        [{'id': i, 'name': f'chapter {i}', 'label': f'1.{i}', 'layer': 2, 'jobcount': 1} for i in range(1, size + 1)])
    for c in chapters.chapters:
        c.point_total = 1
        c.point_finish = int(c.chapter_id in completed)
        c.point_unfinished = 1 - c.point_finish
    chapters.fetch_point_status = Mock()
    return chapters


class WorkflowTests(unittest.TestCase):
    def test_zero_total_uses_unfinished_and_job_count_with_directory_summary(self):
        c = ClassChapters(Mock(), ACCOUNT, 1, 2, 3, [
            {'id': 1, 'label': '1', 'layer': 1, 'jobcount': 0},
            {'id': 2, 'label': '1.1', 'layer': 2, 'jobcount': 2},
            {'id': 3, 'label': '1.2', 'layer': 2, 'jobcount': 2},
            {'id': 4, 'label': '2', 'layer': 1, 'jobcount': 0}])
        c.session.post.return_value.json.return_value = {
            '1': {'totalcount': 0, 'finishcount': 0, 'unfinishcount': 0},
            '2': {'totalcount': 0, 'finishcount': 0, 'unfinishcount': 2},
            '3': {'totalcount': 2, 'finishcount': 2, 'unfinishcount': 0},
            '4': {'totalcount': 0, 'finishcount': 0, 'unfinishcount': 0}}
        c.fetch_point_status()
        rows = chapter_snapshot(c)
        self.assertEqual((rows[0]['finished'], rows[0]['total'], rows[0]['state']), (2, 4, '目录'))
        self.assertEqual((rows[1]['finished'], rows[1]['total'], rows[1]['state']), (0, 2, '待处理'))
        self.assertEqual(rows[3]['state'], '无任务点')
        self.assertTrue(c.is_finished(2))

    def test_one_invalid_status_does_not_destroy_other_chapter_counts(self):
        c = ClassChapters(Mock(), ACCOUNT, 1, 2, 3, [{'id': i, 'label': str(i), 'jobcount': 2} for i in (1, 2)])
        c.session.post.return_value.json.return_value = {'1': {'totalcount': None, 'finishcount': 0},
            '2': {'totalcount': 0, 'finishcount': 0, 'unfinishcount': 2}}
        c.fetch_point_status()
        self.assertEqual([v.point_total for v in c.chapters], [-1, 2])

    def test_auto_select_skips_finished_and_closed_courses(self):
        data = SimpleNamespace(classes=[SimpleNamespace(name=str(i), state=(i == 1)) for i in range(4)],
            fetch_chapters_by_index=Mock(side_effect=[course((1, 2, 3)), course((1,)), course()]))
        result = first_pending_course(data, TaskContext())
        self.assertEqual(result[0], 2)
        self.assertEqual([call.args[0] for call in data.fetch_chapters_by_index.call_args_list], [0, 2])

    def test_manual_start_and_retry_include_only_requested_range(self):
        c = course((3,), 4)
        c.fetch_points_by_index = Mock(return_value=[])
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        with patch('engine.save_json'):
            result = run_course(c, config, TaskContext(), start_chapter=2)
        self.assertEqual([call.args[0] for call in c.fetch_points_by_index.call_args_list], [1, 3])
        self.assertEqual(result['pending_chapters'], [1, 2, 4])
        self.assertEqual(result['retry_chapters'], [2, 4])
        self.assertFalse(result['verified_complete'])

    def test_refresh_occurs_after_each_task_before_next_task(self):
        c = course(size=1)
        c.chapters[0].jobs = c.chapters[0].point_total = 2
        c.chapters[0].point_unfinished = 2
        def finish(context, config):
            c.chapters[0].point_finish += 1
            c.chapters[0].point_unfinished -= 1
        task = SimpleNamespace(kind='document', title='doc', pre_fetch=lambda: True, fetch=lambda: True, run=finish)
        c.fetch_points_by_index = Mock(return_value=[task, task])
        events = []
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        config['document']['wait'] = 0
        with patch('engine.save_json'):
            result = run_course(c, config, TaskContext(events.append))
        self.assertTrue(any(e['kind'] == 'chapters' and e['items'][0]['finished'] == 1 for e in events))
        self.assertTrue(result['verified_complete'])

    def test_stop_retries_interrupted_and_unvisited_chapters_in_range(self):
        c = course(size=4)
        context = TaskContext()
        def stop(context, config):
            context.control.cancel()
            context.checkpoint()
        task = SimpleNamespace(kind='document', title='doc', pre_fetch=lambda: True, fetch=lambda: True, run=stop)
        c.fetch_points_by_index = Mock(return_value=[task])
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        with patch('engine.save_json'):
            result = run_course(c, config, context, start_chapter=2)
        self.assertTrue(result['cancelled'])
        self.assertEqual(result['retry_chapters'], [2, 3, 4])
        self.assertEqual(c.fetch_points_by_index.call_count, 1)

    def test_completed_manual_range_does_not_claim_whole_course_complete(self):
        c = course((2, 3))
        c.fetch_points_by_index = Mock(return_value=[])
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        with patch('engine.save_json'):
            result = run_course(c, config, TaskContext(), start_chapter=2)
        self.assertTrue(result['scope_complete'])
        self.assertFalse(result['verified_complete'])
        self.assertIn('范围外', result['status'])
        c.fetch_points_by_index.assert_not_called()

    def test_missing_task_content_is_reported_as_failure(self):
        c = course(size=1)
        c.fetch_points_by_index = Mock(return_value=[])
        config = copy.deepcopy(DEFAULTS)
        config['exam']['enable'] = False
        with patch('engine.save_json'):
            result = run_course(c, config, TaskContext())
        self.assertEqual(result['counts']['failed'], 1)
        self.assertIn('未返回可执行内容', result['records'][0]['detail'])

    def test_cancelled_request_returns_promptly_and_discards_late_response(self):
        context, entered, release = TaskContext(), threading.Event(), threading.Event()
        response, outcomes = Mock(), []
        def blocked():
            entered.set()
            release.wait(2)
            return response
        def invoke():
            try:
                context.call(blocked)
            except Cancelled:
                outcomes.append('cancelled')
        thread = threading.Thread(target=invoke)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            context.control.cancel()
            thread.join(0.8)
            self.assertFalse(thread.is_alive())
            self.assertEqual(outcomes, ['cancelled'])
        finally:
            release.set()
            thread.join(1)
        deadline = time.monotonic() + 1
        while not response.close.called and time.monotonic() < deadline:
            time.sleep(0.01)
        response.close.assert_called_once()

    def test_platform_request_is_isolated_and_merges_cookies_only_on_success(self):
        session, context = TimeoutSession(), TaskContext()
        session.cookies.set('before', 'a')
        def request(inner, method, url, **kwargs):
            self.assertIsNot(inner, session)
            self.assertEqual(inner.cookies.get('before'), 'a')
            inner.cookies.set('after', 'b')
            return SimpleNamespace(status_code=200, url=url)
        with patch('requests.Session.request', request), context.bound():
            session.get('https://example.test')
        self.assertEqual(session.cookies.get('after'), 'b')
        session.close()

    def test_video_restart_uses_fresh_platform_position(self):
        with tempfile.TemporaryDirectory() as directory, patch('video_progress.ROOT', Path(directory)):
            def make():
                t = ChapterVideo(Mock(), ACCOUNT, 1, 2, 3, 0, 'object', 4, 0)
                t.jobid, t.duration = 'job', 100
                return t
            task = make()
            task.platform_offset_ms = 37000
            task._report = Mock(return_value={'isPassed': False})
            ctx = TaskContext()
            ctx.wait = Mock(side_effect=Cancelled())
            with self.assertRaises(Cancelled):
                task.run(ctx, {'speed': 1, 'report_rate': 58})
            next_task = make()
            next_task.platform_offset_ms = 37000
            next_task._report = Mock(return_value={'isPassed': True})
            next_task.run(TaskContext(), {'speed': 1, 'report_rate': 58})
            next_task._report.assert_called_once_with(37)

    def test_cancelled_platform_request_does_not_merge_late_cookies(self):
        session, context = TimeoutSession(), TaskContext()
        entered, release, done = threading.Event(), threading.Event(), threading.Event()
        response, outcomes = Mock(status_code=200, url='https://example.test'), []
        def request(inner, method, url, **kwargs):
            entered.set()
            release.wait(2)
            inner.cookies.set('late', 'discard')
            done.set()
            return response
        def invoke():
            try:
                with context.bound():
                    session.get('https://example.test')
            except Cancelled:
                outcomes.append('cancelled')
        with patch('requests.Session.request', request):
            thread = threading.Thread(target=invoke)
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                context.control.cancel()
                thread.join(0.8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcomes, ['cancelled'])
                release.set()
                self.assertTrue(done.wait(1))
                self.assertIsNone(session.cookies.get('late'))
            finally:
                release.set()
                thread.join(2)
                session.close()

    def test_content_error_does_not_block_next_ai_question(self):
        config = copy.deepcopy(DEFAULTS['searcher']['aiSearcher'])
        config.update(url='https://example.test/v1', model='offline', api_key='', cache=False)
        client = AISearcher(config)
        try:
            client.session.post = Mock(side_effect=[Response(status=400), Response('A')])
            q = QuestionModel(1, 'q', QuestionEnum(0), {'A': 'a'}, '')
            with self.assertRaises(AIError):
                client.answer(q, TaskContext())
            self.assertTrue(client.answer(q, TaskContext()))
            self.assertEqual(client.session.post.call_count, 2)
        finally:
            client.close()

    def test_exam_keeps_italic_question_and_handles_unrelated_script_variables(self):
        html = (Path(__file__).parent / 'fixtures/mobile_exam_encoded_count.html').read_text(encoding='utf-8')
        html = html.replace('单选测试', 'Let <i>x</i> = 2. Find <em>x + 1</em>.')
        html = html.replace('var a = "101,104,103,102";', 'const a = "101,104,103,102"\nvar seconds = "30"; /* fake setter */')
        task = ChapterExam(Mock(), ACCOUNT, 0, 1, 'w', 'j', 2, 3, 4, 0)
        task.need_jobid, task.ktoken, task.enc = True, 't', 'e'
        task.session.get.return_value.text = html
        self.assertTrue(task.fetch())
        self.assertEqual(task.questions[0].value, 'Let x = 2. Find x + 1.')


class AutomaticWindowTests(unittest.TestCase):
    def setUp(self):
        with patch('gui.sessions_load', return_value=[]):
            self.window = MainWindow(copy.deepcopy(DEFAULTS))
        self.classes = SimpleNamespace(classes=[SimpleNamespace(name=str(i), teacher_name='t', state=0) for i in range(3)],
            fetch_chapters_by_index=Mock(side_effect=lambda i: course((1, 2, 3) if i == 0 else (1,))))

    def tearDown(self):
        self.window.next_operation = None
        if self.window.worker:
            self.window.worker.control.cancel()
            self.wait_idle()
        self.window.close()
        APP.processEvents()

    def wait_idle(self):
        deadline = time.monotonic() + 4
        while self.window.worker is not None and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window.worker)

    def test_login_automatically_populates_and_selects_first_unfinished_course(self):
        api = Mock(acc=ACCOUNT)
        api.fetch_classes.return_value = self.classes
        with patch('gui.sessions_load', return_value=[]):
            self.window.logged_in(api)
            self.wait_idle()
        self.assertEqual(self.window.course_combo.count(), 3)
        self.assertEqual(self.window.course_combo.currentIndex(), 1)
        self.assertIsNotNone(self.window.chapters)
        self.assertEqual(self.window.start_chapter.count(), 4)
        self.assertIsNone(self.window.last_result)  # Loading never submits tasks.

    def test_changing_course_automatically_loads_chapters(self):
        self.window.courses_loaded(self.classes)
        self.wait_idle()
        self.window.course_combo.setCurrentIndex(2)
        self.wait_idle()
        self.assertEqual(self.classes.fetch_chapters_by_index.call_args.args, (2,))
        self.assertIsNotNone(self.window.chapters)

    def test_fast_course_switch_cannot_apply_stale_result(self):
        self.window.courses_loaded(self.classes)
        self.wait_idle()
        entered = threading.Event()
        original = self.classes.fetch_chapters_by_index.side_effect
        def slow(index):
            if index == 0:
                entered.set()
                time.sleep(0.15)
            return original(index)
        self.classes.fetch_chapters_by_index.side_effect = slow
        self.window.course_combo.setCurrentIndex(0)
        self.assertTrue(entered.wait(1))
        self.window.course_combo.setCurrentIndex(2)
        self.wait_idle()
        self.assertEqual(self.window.course_combo.currentIndex(), 2)
        self.assertEqual(self.window.chapters.chapters[1].point_finish, 0)

    def test_selected_start_is_passed_to_runner_and_tree_survives_refresh(self):
        self.window.chapters_loaded(course())
        item = self.window.chapter_items[2]
        self.window.tree.setCurrentItem(item)
        self.window.use_selected_start()
        self.window.render_chapters(chapter_snapshot(self.window.chapters))
        self.assertIs(self.window.tree.currentItem(), item)
        with patch('gui.run_course', return_value={}) as runner:
            self.window.start_course()
            self.wait_idle()
        self.assertEqual(runner.call_args.kwargs['start_chapter'], 2)

    def test_continuation_runs_next_pending_course_after_skipping_completed(self):
        self.window.courses_loaded(self.classes)
        self.wait_idle()
        self.window.course_combo.setCurrentIndex(0)
        self.wait_idle()
        self.window.continue_courses.setChecked(True)
        self.classes.fetch_chapters_by_index.side_effect = lambda i: course((1, 2, 3) if i == 1 else (1,))
        completed = {'status': 'complete', 'counts': {'success': 0, 'failed': 0, 'skipped': 3},
                     'verified_complete': True, 'blocked': False, 'retry_chapters': []}
        with patch('gui.run_course', return_value={}) as runner:
            self.window.course_finished(completed)
            self.wait_idle()
        self.assertEqual(self.window.course_combo.currentIndex(), 2)
        runner.assert_called_once()
        self.assertIsNone(runner.call_args.kwargs['start_chapter'])

    def test_stopped_queued_result_cannot_start_next_operation(self):
        success = Mock()
        self.window.launch('course_scan', lambda context: 'already returned', success)
        self.assertTrue(self.window.worker.wait(1000))
        # The result is queued for the GUI but has not yet been delivered.
        self.window.stop()
        self.wait_idle()
        success.assert_not_called()

    def test_empty_course_refresh_clears_previous_chapters(self):
        self.window.courses_loaded(self.classes)
        self.wait_idle()
        self.window.courses_loaded(SimpleNamespace(classes=[]))
        self.assertEqual(self.window.tree.topLevelItemCount(), 0)
        self.assertIsNone(self.window.chapters)
        self.assertFalse(self.window.run_button.isEnabled())
