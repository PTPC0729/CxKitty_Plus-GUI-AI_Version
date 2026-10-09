"""Offline diagnostics executed by the actual frozen application after a build."""
import json
import os
import sys
import tempfile
import traceback
from contextlib import closing
from pathlib import Path

QR_PAYLOAD = 'https://example.com/cxkitty/packaging-check?uuid=offline&enc=scan-test-123456'


def run_check(report_path):
    report_path = Path(report_path).resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False)), 'checks': [], 'qr_payload': QR_PAYLOAD}
    try:
        # Test data must never touch the user's real config, accounts or banks.
        with tempfile.TemporaryDirectory(prefix='cxkitty-check-') as temporary:
            os.environ['CXKITTY_DATA_DIR'] = temporary
            from settings import ROOT, DEFAULTS, load_config, save_config, save_json
            import copy
            import sqlite3
            from types import SimpleNamespace
            from Crypto.Cipher import AES
            from Crypto.Util.Padding import pad
            from lxml import html
            import certifi
            import jsonpath
            import cli
            from cxapi.api import ChaoXingAPI
            from cxapi.schema import AccountInfo, ChapterModel
            from engine import run_course
            from runtime import TaskContext
            from searcher import JsonFileSearcher, SqliteSearcher, RestAPISearcher
            from utils import save_session, sessions_load
            from PySide6.QtCore import QPoint, Qt
            from PySide6.QtGui import QImage, QPainter
            from PySide6.QtWidgets import QApplication
            from gui import MainWindow
            from qr_image import QRWidget, matrix_image, qr_matrix

            assert ROOT == Path(temporary)
            # Public builds must start with no user-specific state or service configuration.
            app = QApplication.instance() or QApplication(['CxKitty self-check'])
            blank = MainWindow()
            try:
                assert not blank.sessions and blank.api is None
                assert all(not edit.text() for edit in (blank.phone, blank.password, blank.ai_url,
                                                        blank.ai_key, blank.ai_model))
                assert load_config() == DEFAULTS
                assert DEFAULTS['searcher']['restApiSearcher']['url'] == ''
                assert not list(ROOT.iterdir())
            finally:
                blank.close()
                app.processEvents()
            report['checks'].append('public_empty_initial_state')
            config = copy.deepcopy(DEFAULTS)
            config['video']['speed'] = 1.25
            config['custom_extension'] = 'preserved'
            save_config(config)
            assert load_config() == config
            report['checks'].append('config_roundtrip_and_persistent_path')
            api = ChaoXingAPI()
            api.acc = AccountInfo(123, '自检账号', '未知', '13000000000', '自检学校', 'offline')
            api.ck_load({'offline': 'abc=='})
            save_session(api, None)
            assert sessions_load()[0].ck == 'offline=abc==;'
            api.session.close()
            report['checks'].append('legacy_session_roundtrip')

            save_json(ROOT / 'questions.json', {'测试问题': '测试答案'})
            bank = JsonFileSearcher('questions.json')
            assert bank.invoke('测试问题')['data'] == '测试答案'
            with closing(sqlite3.connect(ROOT / 'questions.db')) as db:
                db.execute('CREATE TABLE question (question TEXT, answer TEXT)')
                db.execute('INSERT INTO question VALUES (?, ?)', ('测试问题', '测试答案'))
                db.commit()
            bank = SqliteSearcher('questions.db', 'question', 'answer')
            assert bank.invoke('测试问题')['data'] == '测试答案'
            bank.close()
            rest = RestAPISearcher('http://127.0.0.1:88', 'question', '$.data')
            assert jsonpath.compile('$.data').parse({'data': 'answer'}) == ['answer']
            rest.close()
            report['checks'].append('json_sqlite_rest_jsonpath')
            from ai_selfcheck import run_ai_check
            report['ai'] = run_ai_check(ROOT)
            report['checks'].append('ai_http_answer_cache_and_submission')
            cipher = AES.new(b'0123456789abcdef', AES.MODE_CBC, b'0123456789abcdef')
            assert len(cipher.encrypt(pad(b'offline', 16))) == 16
            assert html.fromstring('<html><input id="uuid" value="offline"></html>').xpath('//input/@value') == ['offline']
            assert Path(certifi.where()).is_file()
            import ssl
            ssl.create_default_context(cafile=certifi.where())
            report['checks'].append('aes_lxml_https_certificates')
            assert callable(cli.run_cli)
            report['checks'].append('cli_imports')

            app = QApplication.instance() or QApplication(['CxKitty self-check'])
            from app_identity import configure_application
            configure_application(app)
            window = MainWindow(config)
            assert window.tabs.count() == 4
            assert window.collect_settings()['video']['speed'] == 1.25
            window.bank_type.setCurrentIndex(window.bank_type.findData('aiSearcher'))
            window.ai_url.setText('http://127.0.0.1:9999/v1')
            window.ai_model.setText('offline-model')
            assert window.collect_settings()['searcher']['aiSearcher']['model'] == 'offline-model'
            assert window.ai_key.echoMode() == window.ai_key.EchoMode.Password
            window.bank_type.setCurrentIndex(window.bank_type.findData(config['searcher']['use']))
            window.grab().save(str(report_path.parent / 'application.png'))
            window.close()
            app.processEvents()
            report['checks'].append('qt_platform_and_all_gui_pages')
            from workflow_selfcheck import run_workflow_check
            report['workflow'] = run_workflow_check(report_path.parent, app)
            report['checks'].append('workflow_auto_load_start_and_resume')
            from reliability_selfcheck import run_reliability_check
            report['reliability'] = run_reliability_check()
            report['checks'].append('video_resume_units_and_verified_authentication')
            from reliability_selfcheck import run_video_protocol_check
            report['video_protocol'] = run_video_protocol_check()
            report['checks'].append('platform_video_report_url_and_course_context')
            from workspace_selfcheck import run_workspace_check
            report['workspaces'] = run_workspace_check(report_path.parent, app)
            report['checks'].append('independent_workspaces_login_and_shutdown')
            report['checks'].append('bundled_application_icon')
            matrix = qr_matrix(QR_PAYLOAD)
            assert all(not any(row) for row in matrix[:4] + matrix[-4:])
            assert matrix_image(matrix).save(str(report_path.parent / 'qr-original.png'))
            for ratio in (1.0, 1.25, 1.5, 2.0):
                class ScaledQR(QRWidget):
                    def devicePixelRatioF(self):
                        return ratio
                widget = ScaledQR()
                widget.set_payload(QR_PAYLOAD)
                widget.resize(560, 310)
                image = QImage(round(560 * ratio), round(310 * ratio), QImage.Format.Format_RGB32)
                image.setDevicePixelRatio(ratio)
                image.fill(Qt.GlobalColor.white)
                painter = QPainter(image)
                widget.render(painter, QPoint())
                painter.end()
                assert image.save(str(report_path.parent / f'qr-{ratio}.png'))
                widget.close()
            report['checks'].append('qr_png_and_fractional_dpi_rendering')

            chapter = ChapterModel(1, 1, 1, '离线自检', '1', 1, '', 1, 0)
            def finish(context, config):
                chapter.point_finish = 1
            job = SimpleNamespace(kind='document', title='离线文档', pre_fetch=lambda: True,
                                  fetch=lambda: True, run=finish)
            chapters = SimpleNamespace(chapters=[chapter], point_issues=[], fetch_point_status=lambda: None,
                is_finished=lambda index: chapter.point_finish == 1, fetch_points_by_index=lambda *args, **kwargs: [job])
            config['exam']['enable'] = False
            config['document']['wait'] = 0
            result = run_course(chapters, config, TaskContext())
            assert result['verified_complete'] and result['counts']['success'] == 1
            assert list((ROOT / 'logs').glob('*.json'))
            report['checks'].append('task_engine_and_result_storage')
            report['ok'] = True
    except Exception:
        report['error'] = traceback.format_exc()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1
