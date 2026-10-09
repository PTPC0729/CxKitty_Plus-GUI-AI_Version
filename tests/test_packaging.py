import copy
import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtWidgets import QApplication

from build_backend import BuildCancelled, BuildOptions, Pipeline, REQUIRED_FILES
from packager import PackagerWindow
from runtime_paths import data_root
from settings import DEFAULTS, load_config, save_config

APP = QApplication.instance() or QApplication([])


class PackagingTests(unittest.TestCase):
    def test_frozen_data_root_is_executable_directory_not_extraction(self):
        with tempfile.TemporaryDirectory() as folder:
            executable = Path(folder) / 'portable' / 'CxKitty.exe'
            with patch.dict(os.environ, {}, clear=True), patch.object(sys, 'frozen', True, create=True), \
                    patch.object(sys, 'executable', str(executable)), patch.object(sys, '_MEIPASS', 'unrelated', create=True):
                self.assertEqual(data_root(), executable.parent)
                os.environ['CXKITTY_DATA_DIR'] = str(Path(folder) / '数据')
                self.assertEqual(data_root(), Path(folder) / '数据')

    def test_output_name_rejects_reserved_names_and_path_traversal(self):
        project = Path(__file__).resolve().parents[1]
        for name in ('../other', 'CON', 'nul.txt', 'COM1', 'LPT1', 'x.exe', 'x.', ' x', 'x/y', 'x:y'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                BuildOptions(project, project / 'dist', name).validate()

    def test_packaged_config_and_banks_preserve_source_and_wal(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, release = root / '源码', root / '发布'
            project.mkdir()
            release.mkdir()
            external = root / '外部题库.db'
            with closing(sqlite3.connect(external)) as db:
                db.execute('PRAGMA journal_mode=WAL')
                db.execute('CREATE TABLE question (question TEXT, answer TEXT)')
                db.execute('INSERT INTO question VALUES (?, ?)', ('测试', '答案'))
                db.commit()
                config = copy.deepcopy(DEFAULTS)
                config['custom'] = {'extra': 42}
                config['sessionPath'] = '旧会话'
                config['searcher']['sqliteSearcher']['file_path'] = str(external)
                (project / 'questions.json').write_text('{"测试":"答案"}', encoding='utf-8')
                (project / '旧会话').mkdir()
                session = b'{"phone":"13000000000","puid":1,"name":"test","ck":"a=b==;","extra":42}'
                (project / '旧会话' / 'test.json').write_bytes(session)
                progress = b'{"version":1,"entries":{"example":{"duration":100,"position":37}}}'
                (project / 'video_progress.json').write_bytes(progress)
                save_config(config, project / 'config.yml')
                before = (project / 'config.yml').read_bytes()
                options = BuildOptions(project, root, copy_sessions=True)
                Pipeline().copy_data(options, release)
                copied = load_config(release / 'config.yml')
                self.assertEqual(copied['custom'], {'extra': 42})
                self.assertEqual(copied['sessionPath'], 'session/')
                self.assertEqual((release / 'session' / 'test.json').read_bytes(), session)
                self.assertEqual((release / 'video_progress.json').read_bytes(), progress)
                self.assertEqual((project / 'config.yml').read_bytes(), before)
                self.assertEqual((project / '旧会话' / 'test.json').read_bytes(), session)
                for kind in ('jsonFileSearcher', 'sqliteSearcher'):
                    self.assertFalse(Path(copied['searcher'][kind]['file_path']).is_absolute())
                with closing(sqlite3.connect(release / copied['searcher']['sqliteSearcher']['file_path'])) as copied_db:
                    self.assertEqual(copied_db.execute('SELECT answer FROM question').fetchone()[0], '答案')

    def test_default_build_does_not_copy_sessions_and_default_config_is_optional(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, release = root / 'source', root / 'release'
            project.mkdir()
            release.mkdir()
            (project / 'session').mkdir()
            (project / 'session' / 'private.json').write_text('private', encoding='utf-8')
            (project / 'video_progress.json').write_text('private checkpoints', encoding='utf-8')
            (project / 'config.yml').write_text('this: [is: invalid', encoding='utf-8')
            Pipeline().copy_data(BuildOptions(project, root, copy_config=False, copy_banks=False), release)
            self.assertFalse((release / 'session').exists())
            self.assertFalse((release / 'video_progress.json').exists())
            self.assertEqual(load_config(release / 'config.yml'), DEFAULTS)

    def test_cancellation_stops_active_subprocess(self):
        with tempfile.TemporaryDirectory() as folder:
            cancel = Path(folder) / 'cancel'
            timer = threading.Timer(0.5, cancel.touch)
            timer.start()
            started = time.monotonic()
            try:
                with self.assertRaises(BuildCancelled):
                    Pipeline(cancel_file=cancel).run_process([sys.executable, '-c', 'import time; time.sleep(30)'], folder)
            finally:
                timer.join()
            self.assertLess(time.monotonic() - started, 10)

    def test_ai_settings_and_cache_are_portable_while_keys_require_opt_in(self):
        from ai_credentials import credential_id
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, release, personal = root / 'source', root / 'release', root / 'personal'
            for path in (project, release, personal):
                path.mkdir()
            config = copy.deepcopy(DEFAULTS)
            config['searcher']['use'] = 'aiSearcher'
            config['searcher']['aiSearcher'].update(url='https://example.test/v1', model='my-model')
            save_config(config, project / 'config.yml')
            cache = {'version': 1, 'entries': {'test-question-hash': 'B'}}
            (project / 'ai_cache.json').write_text(json.dumps(cache), encoding='utf-8')
            credentials = {'version': 1, 'services': {credential_id('https://example.test/v1'): 'sensitive-key'}}
            (project / 'ai_credentials.json').write_text(json.dumps(credentials), encoding='utf-8')
            Pipeline().copy_data(BuildOptions(project, root), release)
            stored = load_config(release / 'config.yml')
            self.assertEqual(stored['searcher']['aiSearcher']['model'], 'my-model')
            self.assertFalse((release / 'ai_credentials.json').exists())
            self.assertNotIn('sensitive-key', (release / 'config.yml').read_text())
            self.assertEqual(json.loads((release / stored['searcher']['aiSearcher']['cache_path']).read_text()), cache)
            Pipeline().copy_data(BuildOptions(project, root, copy_ai_keys=True), personal)
            self.assertEqual(json.loads((personal / 'ai_credentials.json').read_text()), credentials)

    def test_inline_ai_key_is_removed_from_release_yaml(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            source, release = root / 'source', root / 'release'
            source.mkdir()
            release.mkdir()
            (source / 'config.yml').write_text('searcher:\n  aiSearcher:\n    url: https://example.test/v1\n    model: model\n    api_key: private-inline-key\n', encoding='utf-8')
            before = (source / 'config.yml').read_bytes()
            Pipeline().copy_data(BuildOptions(source, root), release)
            self.assertNotIn('private-inline-key', (release / 'config.yml').read_text())
            self.assertEqual((source / 'config.yml').read_bytes(), before)


class PackagerWindowTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.window = PackagerWindow()
        self.window.project.setText(str(self.root))
        self.window.python.setText(sys.executable)

    def tearDown(self):
        if self.window.process:
            self.window.cancel()
            self.wait_idle()
        self.window.close()
        self.directory.cleanup()

    def wait_idle(self):
        deadline = time.monotonic() + 10
        while self.window.process and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.01)
        self.assertIsNone(self.window.process)

    def test_async_process_output_and_controls(self):
        (self.root / 'build_backend.py').write_text(
            'import json, time\nprint("@@CXKITTY_BUILD@@" + json.dumps({"kind":"log","text":"中文日志"}, ensure_ascii=False), flush=True)\ntime.sleep(0.1)\n', encoding='utf-8')
        self.window.start('check')
        self.assertFalse(self.window.build_button.isEnabled())
        self.wait_idle()
        self.assertIn('中文日志', self.window.log.toPlainText())
        self.assertTrue(self.window.build_button.isEnabled())
        self.assertEqual(self.window.progress.value(), 100)
        self.assertFalse(self.window.run_button.isEnabled())

    def test_failed_job_does_not_enable_run_button(self):
        (self.root / 'build_backend.py').write_text('import sys\nprint("failure")\nsys.exit(1)\n', encoding='utf-8')
        self.window.start('check')
        self.wait_idle()
        self.assertIn('未完成', self.window.status.text())
        self.assertFalse(self.window.run_button.isEnabled())

    def test_closing_waits_for_cancel_and_cleans_up(self):
        (self.root / 'build_backend.py').write_text(
            'import pathlib, sys, time\np = pathlib.Path(sys.argv[sys.argv.index("--cancel-file") + 1])\n'
            'while not p.exists(): time.sleep(0.02)\nsys.exit(130)\n', encoding='utf-8')
        self.window.start('check')
        self.window.close()
        self.assertTrue(self.window.closing)
        self.wait_idle()
        self.assertIsNone(self.window.cancel_path)
        self.assertIn('已取消', self.window.status.text())

    def test_no_config_disables_incompatible_copy_options(self):
        self.assertFalse(self.window.config_box.isChecked())
        self.assertFalse(self.window.sessions_box.isEnabled())
        self.window.config_box.setChecked(True)
        self.window.sessions_box.setChecked(True)
        self.window.ai_keys_box.setChecked(True)
        self.window.config_box.setChecked(False)
        self.assertFalse(self.window.sessions_box.isChecked())
        self.assertFalse(self.window.banks_box.isChecked())
        self.assertFalse(self.window.sessions_box.isEnabled())
        self.assertFalse(self.window.ai_keys_box.isChecked())
        self.assertFalse(self.window.ai_keys_box.isEnabled())


if __name__ == '__main__':
    unittest.main()
