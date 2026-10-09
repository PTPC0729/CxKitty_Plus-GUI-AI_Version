"""Exercise the actual tab shell and concurrent workers with an offline platform."""
import copy
import hashlib
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app_identity import app_icon, icon_path
from cxapi.api import ChaoXingAPI
from cxapi.chapters import ClassChapters
from cxapi.schema import AccountInfo
from settings import DEFAULTS
from workspace_tabs import WorkspaceWindow


def wait_for(app, predicate, message, timeout=8):
    deadline = time.monotonic() + timeout
    while not predicate() and time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.005)
    assert predicate(), message


def offline_api(number):
    api = ChaoXingAPI()
    api.acc = AccountInfo(number, f'示例账号 {number}', '', '', '离线演示', '')
    api.session.cookies.set('offline', str(number), domain='.chaoxing.com', path='/')
    return api


def run_workspace_check(directory, app):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    config = copy.deepcopy(DEFAULTS)
    config['sessionPath'] = str(directory / 'offline-sessions')
    config['exam']['enable'] = config['video']['enable'] = False
    config['document']['wait'] = 0
    started, release, complete, contexts = {}, {}, set(), {}

    class Courses:
        def __init__(self, api):
            self.api = api
            self.classes = [SimpleNamespace(courseid=i, clazzid=10, state=0,
                name=name, teacher_name='离线示例') for i, name in enumerate(
                    ('中国文化概论', '计算机基础', '大学英语'), 1)]

        def fetch_chapters_by_index(self, index):
            course = self.classes[index]
            key = self.api.acc.puid, course.courseid
            chapters = ClassChapters(self.api.session, self.api.acc, course.courseid, 10, 20,
                [{'id': 1, 'name': '多任务独立运行演示', 'label': '1', 'layer': 1, 'jobcount': 1}])
            chapter = chapters.chapters[0]
            def refresh():
                chapter.point_total = 1
                chapter.point_finish = int(key in complete)
                chapter.point_unfinished = 1 - chapter.point_finish
            def run(context, task_config):
                contexts[key] = context
                started.setdefault(key, threading.Event()).set()
                gate = release.setdefault(key, threading.Event())
                while not gate.is_set():
                    context.wait(0.02)
                context.checkpoint()
                complete.add(key)
            chapters.fetch_point_status = refresh
            chapters.fetch_points_by_index = lambda *args, **kwargs: [SimpleNamespace(kind='document',
                title='离线任务', pre_fetch=lambda: True, fetch=lambda: True, run=run)]
            return chapters

    with patch.object(ChaoXingAPI, 'fetch_classes', lambda api: Courses(api)), \
            patch('cxapi.network.TimeoutSession.request', side_effect=AssertionError('Unexpected live request')), \
            patch('engine.ROOT', directory):
        window = WorkspaceWindow(config)
        idle = lambda page: wait_for(app, lambda: page.worker is None, 'Workspace did not become idle')
        try:
            page1 = window.pages[0]
            page1.logged_in(offline_api(1))
            idle(page1)
            page1.speed.setValue(1.75)
            page1.start_course()
            wait_for(app, lambda: (1, 1) in contexts, 'First task did not start')
            page2 = window.add_page()
            idle(page2)
            assert page2.api.acc.puid == 1 and page2.course_combo.currentIndex() == 1
            assert page2.speed.value() == 1 and page1.speed.value() == 1.75
            assert page1.api is not page2.api and page1.api.session is not page2.api.session
            page1.api.session.cookies.set('offline', 'changed', domain='.chaoxing.com', path='/')
            assert page2.api.session.cookies.get('offline', domain='.chaoxing.com', path='/') == '1'
            page2.start_course()
            wait_for(app, lambda: (1, 2) in contexts, 'Second task did not start concurrently')
            assert contexts[(1, 1)].control is not contexts[(1, 2)].control
            page1.pause()
            assert page1.worker.control.paused and not page2.worker.control.paused
            window.show()
            app.processEvents()
            assert window.grab().save(str(directory / 'workspaces.png'))
            window.resize(940, 710)
            app.processEvents()
            assert window.grab().save(str(directory / 'workspaces-minimum.png'))

            page3 = window.add_page()
            idle(page3)
            assert page3.course_combo.currentIndex() == 2
            page3.course_combo.setCurrentIndex(0)
            idle(page3)
            page3.start_course()
            assert page3.worker is None and '其他任务页' in page3.last_error
            assert len(window.run_registry.owners) == 2
            # Selecting another account is a manual success, as are password/QR login.
            page3.launch('login', lambda context: offline_api(2), page3.logged_in)
            idle(page3)
            page4 = window.add_page()
            idle(page4)
            assert page4.api.acc.puid == 2 and page2.api.acc.puid == 1
            assert page4.api.session is not page3.api.session
            snapshot = window.latest_login
            def fail(context):
                raise ValueError('离线模拟登录失败')
            page3.launch('login', fail, page3.logged_in)
            idle(page3)
            assert window.latest_login is snapshot and page3.api.acc.puid == 2
            # Dragging tabs must not change which workspace a close button targets.
            window.workspaces.tabBar().moveTab(window.workspaces.indexOf(page1), 2)
            page2_worker = page2.worker
            window.close_page(window.workspaces.indexOf(page1))
            wait_for(app, lambda: page1 not in window.pages, 'Running tab did not close')
            assert page2.worker is page2_worker and not page2.worker.control.cancelled
            assert len(window.run_registry.owners) == 1
            release[(1, 2)].set()
            idle(page2)
            assert page2.last_result['verified_complete'] and not window.run_registry.owners
            # Root close cancels all workers, including paused workers and continuations.
            for page in (page3, page4):
                page.launch('bank', lambda context: context.wait(60), lambda value: None)
            page3.pause()
            page3.next_operation = lambda: (_ for _ in ()).throw(AssertionError('Late continuation'))
            window.close()
            wait_for(app, lambda: not window.pages, 'Application did not stop all pages')
            app.processEvents()
            assert not window.isVisible() and not window.run_registry.owners
        finally:
            window.close()
            wait_for(app, lambda: not window.pages, 'Self-check cleanup could not stop workers')
            window.deleteLater()
            app.processEvents()
    icon = app_icon()
    assert icon_path().is_file() and not icon.isNull()
    for size in (16, 32, 48, 256):
        assert not icon.pixmap(size, size).isNull()
    return {'independent_sessions_and_settings': True, 'manual_login_inheritance': True,
            'concurrent_courses_and_duplicate_guard': True, 'safe_tab_and_window_close': True,
            'icon_sha256': hashlib.sha256(icon_path().read_bytes()).hexdigest()}
