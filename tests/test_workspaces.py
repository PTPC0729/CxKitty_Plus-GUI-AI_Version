import copy
import json
import os
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

import ai_credentials
import video_progress
from ai_searcher import AISearcher
from app_identity import app_icon, icon_path
from build_backend import BuildOptions, pyinstaller_command
from gui import MainWindow
from packager import PackagerWindow, run_self_check
from runtime import TaskContext
from settings import DEFAULTS, save_json
from workspace_selfcheck import offline_api, run_workspace_check, wait_for
from workspace_tabs import CourseRuns, LoginSnapshot, WorkspaceWindow

APP = QApplication.instance() or QApplication([])


class WorkspaceTests(unittest.TestCase):
    def test_concurrent_workspaces_use_real_workers_and_engine(self):
        with tempfile.TemporaryDirectory() as folder:
            report = run_workspace_check(Path(folder), APP)
            self.assertTrue(report['safe_tab_and_window_close'])

    def test_snapshot_survives_source_changes_and_preserves_cookie_scope(self):
        api = offline_api(1)
        api.session.cookies.set('offline', 'scoped', domain='passport2.chaoxing.com', path='/login')
        snapshot = LoginSnapshot.capture(api)
        api.acc.name = 'changed'
        api.session.cookies.clear()
        api.session.close()
        inherited = snapshot.create_api()
        other = snapshot.create_api()
        try:
            self.assertEqual(len(inherited.session.cookies), 2)
            self.assertEqual(inherited.session.cookies.get('offline', domain='passport2.chaoxing.com', path='/login'), 'scoped')
            self.assertNotEqual(inherited.acc.name, api.acc.name)
            inherited.acc.name = 'independent'
            inherited.session.cookies.clear()
            self.assertEqual(len(other.session.cookies), 2)
            self.assertNotEqual(inherited.acc.name, other.acc.name)
            self.assertIsNot(inherited.session.adapters['https://'], other.session.adapters['https://'])
        finally:
            inherited.session.close()
            other.session.close()

    def test_saved_settings_only_update_current_and_future_pages(self):
        with patch('gui.sessions_load', return_value=[]), patch('gui.save_config') as save, \
                patch('gui.save_api_key') as key_save, patch.object(MainWindow, 'refresh_courses'):
            window = WorkspaceWindow(DEFAULTS)
            try:
                first = window.pages[0]
                second = window.add_page()
                first.speed.setValue(1.75)
                first.ai_url.setText('https://offline.test/v1')
                first.ai_key.setText('fake-key')
                first.save_settings()
                third = window.add_page()
                self.assertEqual(second.speed.value(), 1)
                self.assertEqual(third.speed.value(), 1.75)
                self.assertEqual(third.ai_key.text(), 'fake-key')
                self.assertEqual(second.ai_key.text(), '')
                third.ai_key.setText('another-fake-key')
                self.assertEqual(first.ai_key.text(), 'fake-key')
                save.assert_called_once()
                key_save.assert_called_once()
            finally:
                window.close()
                wait_for(APP, lambda: not window.pages, 'close failed')

    def test_latest_manual_account_not_current_tab_and_close_source(self):
        with patch('gui.sessions_load', return_value=[]), patch.object(MainWindow, 'refresh_courses'):
            window = WorkspaceWindow(DEFAULTS)
            try:
                first = window.pages[0]
                first.logged_in(offline_api(1))
                second = window.add_page()
                second.logged_in(offline_api(2))
                window.workspaces.setCurrentWidget(first)
                third = window.add_page()
                self.assertEqual(third.api.acc.puid, 2)
                self.assertEqual(first.api.acc.puid, 1)
                second.close()
                wait_for(APP, lambda: second not in window.pages, 'close source failed')
                fourth = window.add_page()
                self.assertEqual(fourth.api.acc.puid, 2)
                third.api.session.cookies.clear()
                self.assertEqual(len(fourth.api.session.cookies), 1)
            finally:
                window.close()
                wait_for(APP, lambda: not window.pages, 'close failed')

    def test_last_tab_close_and_new_tab_while_shutting_down(self):
        with patch('gui.sessions_load', return_value=[]):
            window = WorkspaceWindow(DEFAULTS)
            page = window.pages[0]
            page.launch('bank', lambda context: context.wait(60), lambda value: None)
            window.close()
            self.assertIsNone(window.add_page())
            wait_for(APP, lambda: not window.pages, 'close failed')
            self.assertIsNone(window.latest_login)

    def test_course_claim_is_account_and_class_scoped(self):
        registry = CourseRuns()
        first, second = object(), object()
        course = SimpleNamespace(acc=SimpleNamespace(puid=1), courseid=2, clazzid=3)
        self.assertTrue(registry.acquire(course, first))
        self.assertFalse(registry.acquire(course, second))
        other = copy.deepcopy(course)
        other.acc.puid = 2
        self.assertTrue(registry.acquire(other, second))
        other.acc.puid, other.clazzid = 1, 4
        self.assertTrue(registry.acquire(other, second))
        registry.release(first)
        self.assertTrue(registry.acquire(course, second))


class ParallelStorageTests(unittest.TestCase):
    def run_parallel(self, operation, count=12):
        barrier = threading.Barrier(count)
        def run(index):
            barrier.wait(timeout=5)
            operation(index)
        with ThreadPoolExecutor(max_workers=count) as pool:
            list(pool.map(run, range(count)))

    @staticmethod
    def delayed(function):
        def read(*args, **kwargs):
            value = function(*args, **kwargs)
            time.sleep(0.003)
            return value
        return read

    def test_progress_updates_do_not_lose_other_courses(self):
        with tempfile.TemporaryDirectory() as folder, patch('video_progress.ROOT', Path(folder)), \
                patch('video_progress.read_progress', self.delayed(video_progress.read_progress)):
            def save(index):
                task = SimpleNamespace(acc=SimpleNamespace(puid=1), courseid=index, clazzid=1,
                    knowledgeid=1, jobid=1, objectid=1, duration=100)
                video_progress.save_position(task, index, TaskContext())
            self.run_parallel(save)
            data = video_progress.read_progress()
            self.assertEqual(len(data), 12)
            self.assertEqual({v['position'] for v in data.values()}, set(range(12)))

    def test_credentials_updates_preserve_all_endpoints(self):
        with tempfile.TemporaryDirectory() as folder, patch('ai_credentials.ROOT', Path(folder)), \
                patch('ai_credentials.read_credentials', self.delayed(ai_credentials.read_credentials)):
            self.run_parallel(lambda i: ai_credentials.save_api_key(f'https://offline{i}.test/v1', f'fake{i}'))
            self.assertEqual(len(ai_credentials.read_credentials()['services']), 12)

    def test_answer_cache_updates_preserve_other_tabs_and_legacy_entries(self):
        with tempfile.TemporaryDirectory() as folder:
            cache = Path(folder) / 'cache.json'
            save_json(cache, {'version': 1, 'entries': {'legacy': 'D'}})
            config = {**DEFAULTS['searcher']['aiSearcher'], 'url': 'https://offline.test/v1',
                      'model': 'offline', 'api_key': '', 'cache_path': str(cache)}
            searchers = [AISearcher(config) for _ in range(12)]
            try:
                with patch.object(AISearcher, '_load_cache', self.delayed(AISearcher._load_cache)):
                    self.run_parallel(lambda i: searchers[i]._store_cache(str(i), 'B', TaskContext()))
                entries = json.loads(cache.read_text())['entries']
                self.assertEqual(len(entries), 13)
                self.assertEqual(entries['legacy'], 'D')
            finally:
                for searcher in searchers:
                    searcher.close()


class IconTests(unittest.TestCase):
    def test_default_and_custom_icon_are_also_bundled_for_runtime(self):
        project = Path(__file__).resolve().parents[1]
        with tempfile.TemporaryDirectory() as folder:
            default = BuildOptions(project, Path(folder)).validate()
            self.assertEqual(Path(default.icon), project / 'xxt_helper.ico')
            command = pyinstaller_command(default, folder)
            self.assertEqual(command[command.index('--icon') + 1], default.icon)
            self.assertEqual(command[command.index('--add-data') + 1], default.icon + ';_app_icon')
            custom = Path(folder) / 'my icon.ico'
            custom.write_bytes((project / 'xxt_helper.ico').read_bytes())
            options = BuildOptions(project, Path(folder), icon=str(custom)).validate()
            command = pyinstaller_command(options, folder)
            self.assertIn(str(custom) + ';_app_icon', command)

    def test_source_icon_and_project_picker_defaults(self):
        self.assertTrue(icon_path().is_file())
        self.assertFalse(app_icon().pixmap(32, 32).isNull())
        window = PackagerWindow()
        try:
            self.assertEqual(Path(window.icon.text()), Path(window.project.text()) / 'xxt_helper.ico')
            window.project.setText('D:/another-source')
            self.assertEqual(Path(window.icon.text()), Path('D:/another-source/xxt_helper.ico'))
            window.icon.setText('D:/custom.ico')
            window.project.setText('D:/third-source')
            self.assertEqual(window.icon.text(), 'D:/custom.ico')
            self.assertFalse(window.windowIcon().isNull())
        finally:
            window.close()

    def test_packager_offline_check(self):
        with tempfile.TemporaryDirectory() as folder:
            report = Path(folder) / 'check.json'
            self.assertEqual(run_self_check(PackagerWindow(), APP, report), 0)
            self.assertTrue(json.loads(report.read_text(encoding='utf-8'))['private_data_excluded'])
