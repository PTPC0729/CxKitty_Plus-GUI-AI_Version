import copy
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QImage, QPainter
from PySide6.QtWidgets import QApplication

from gui import MainWindow
from qr_image import QRWidget, matrix_image, qr_matrix
from settings import DEFAULTS
from cxapi.schema import AccountInfo, ChapterModel
from engine import run_course

PAYLOAD = 'https://passport2.chaoxing.com/toauthlogin?uuid=0123456789abcdef0123456789abcdef&enc=abcdef0123456789abcdef0123456789&xxtrefer=&clientid=&type=0&mobiletip='
APP = QApplication.instance() or QApplication([])


def decode(image):
    try:
        import cv2
        import numpy as np
    except ImportError:
        raise unittest.SkipTest('二维码解码检查需要 opencv-python-headless 和 numpy')
    image = image.convertToFormat(QImage.Format.Format_RGB888)
    data = np.frombuffer(image.bits(), dtype=np.uint8).reshape(image.height(), image.bytesPerLine())
    pixels = data[:, :image.width() * 3].reshape(image.height(), image.width(), 3).copy()
    return cv2.QRCodeDetector().detectAndDecode(pixels)[0]


class QRTests(unittest.TestCase):
    def test_original_image_has_quiet_zone_and_decodes(self):
        matrix = qr_matrix(PAYLOAD)
        self.assertTrue(all(not any(row) for row in matrix[:4] + matrix[-4:]))
        self.assertTrue(all(not any(row[:4] + row[-4:]) for row in matrix))
        self.assertEqual(decode(matrix_image(matrix)), PAYLOAD)

    def test_non_square_and_fractional_dpi_rendering(self):
        for ratio in (1.0, 1.25, 1.5, 2.0):
            class ScaledQR(QRWidget):
                def devicePixelRatioF(self):
                    return ratio
            widget = ScaledQR()
            widget.set_payload(PAYLOAD)
            for width, height in ((300, 300), (560, 310), (330, 520)):
                widget.resize(width, height)
                image = QImage(round(width * ratio), round(height * ratio), QImage.Format.Format_RGB32)
                image.setDevicePixelRatio(ratio)
                image.fill(Qt.GlobalColor.white)
                painter = QPainter(image)
                widget.render(painter, QPoint())
                painter.end()
                self.assertEqual(decode(image), PAYLOAD, (ratio, width, height))
            widget.close()


class WindowTests(unittest.TestCase):
    def setUp(self):
        with patch('gui.sessions_load', return_value=[]):
            self.window = MainWindow(copy.deepcopy(DEFAULTS))

    def tearDown(self):
        if self.window.worker:
            self.window.worker.control.cancel()
            self.window.worker.wait(1000)
            APP.processEvents()
        self.window.close()
        APP.processEvents()

    def wait_idle(self):
        deadline = time.monotonic() + 3
        while self.window.worker is not None and time.monotonic() < deadline:
            APP.processEvents()
            time.sleep(0.005)
        self.assertIsNone(self.window.worker)

    def test_initial_controls_and_settings_compatibility(self):
        self.assertEqual(self.window.tabs.count(), 4)
        self.assertFalse(self.window.use_account.isEnabled())
        self.assertFalse(self.window.run_button.isEnabled())
        config = self.window.collect_settings()
        self.assertEqual(config['sessionPath'], DEFAULTS['sessionPath'])
        self.assertEqual(config['searcher'], DEFAULTS['searcher'])

    def test_ai_controls_collect_complete_settings_and_keep_key_masked(self):
        self.window.bank_type.setCurrentIndex(self.window.bank_type.findData('aiSearcher'))
        self.window.ai_url.setText('https://example.test/v1')
        self.window.ai_model.setText('my-model')
        self.window.ai_key.setText('secret-key')
        self.window.ai_strategy.setCurrentIndex(self.window.ai_strategy.findData('bank_first'))
        self.window.ai_fallback.setCurrentIndex(self.window.ai_fallback.findData('sqliteSearcher'))
        config = self.window.collect_settings()['searcher']
        self.assertEqual(config['use'], 'aiSearcher')
        self.assertEqual(config['aiSearcher']['api_key'], 'secret-key')
        self.assertEqual(config['aiSearcher']['fallback'], 'sqliteSearcher')
        self.assertFalse(self.window.bank_fields[('sqliteSearcher', 'file_path')].property('holder').isHidden())
        self.assertTrue(self.window.bank_fields[('jsonFileSearcher', 'file_path')].property('holder').isHidden())
        self.assertEqual(self.window.ai_key.echoMode(), self.window.ai_key.EchoMode.Password)

    def test_ai_connection_check_runs_in_worker(self):
        self.window.bank_type.setCurrentIndex(self.window.bank_type.findData('aiSearcher'))
        bank = Mock()
        bank.check_connection.return_value = 'AI 接口检查通过'
        with patch('gui.create_searcher', return_value=bank):
            self.window.check_bank()
            self.wait_idle()
        bank.check_connection.assert_called_once()
        bank.close.assert_called_once()
        self.assertEqual(self.window.bank_hint.text(), 'AI 接口检查通过')

    def test_changing_endpoint_clears_previous_key(self):
        self.window.ai_key.setText('old-endpoint-key')
        self.window.ai_url.setText('https://another.test/v1')
        with patch('gui.load_api_key', return_value='new-endpoint-key'):
            self.window.load_ai_key()
        self.assertEqual(self.window.ai_key.text(), 'new-endpoint-key')

    def test_background_worker_result_and_controls(self):
        results = []
        self.window.launch('test', lambda context: 'completed', results.append)
        self.assertFalse(self.window.qr_start.isEnabled())
        self.wait_idle()
        self.assertEqual(results, ['completed'])
        self.assertTrue(self.window.qr_start.isEnabled())

    def test_cancelled_qr_cannot_deliver_late_login(self):
        results = []
        def operation(context):
            context.wait(5)
            return 'late login'
        self.window.launch('qr', operation, results.append)
        self.window.stop()
        self.wait_idle()
        self.assertEqual(results, [])
        self.assertIsNone(self.window.qr.matrix)

    def test_password_login_loads_account_without_saving_password_by_default(self):
        api = Mock()
        api.acc = AccountInfo(123, '测试账号', '未知', '13000000000', '测试学校', 'test')
        api.login_passwd.return_value = (True, {})
        api.fetch_classes.return_value = SimpleNamespace(classes=[])
        api.accinfo.return_value = True
        self.window.phone.setText('13000000000')
        self.window.password.setText('test-password')
        with patch('gui.ChaoXingAPI', return_value=api), patch('gui.save_session') as save, patch('gui.sessions_load', return_value=[]):
            self.window.login_password()
            self.wait_idle()
            save.assert_called_once_with(api, None, 'session/')
        self.assertIs(self.window.api, api)
        self.assertEqual(self.window.password.text(), '')
        self.assertEqual(self.window.tabs.currentIndex(), 1)
        self.assertTrue(self.window.refresh_button.isEnabled())

    def test_qr_login_success_clears_image(self):
        api = Mock()
        api.acc = AccountInfo(123, '测试账号', '未知', '13000000000', '测试学校', 'test')
        api.qr_geturl.return_value = PAYLOAD
        api.login_qr.return_value = {'status': True}
        api.fetch_classes.return_value = SimpleNamespace(classes=[])
        api.accinfo.return_value = True
        with patch('gui.ChaoXingAPI', return_value=api), patch('gui.save_session'), patch('gui.sessions_load', return_value=[]):
            self.window.login_qr()
            self.wait_idle()
        self.assertIs(self.window.api, api)
        self.assertIsNone(self.window.qr.matrix)
        self.assertFalse(self.window.qr_save.isEnabled())

    def test_expired_qr_clears_image_and_allows_refresh(self):
        api = Mock()
        api.qr_geturl.return_value = PAYLOAD
        api.login_qr.return_value = {'status': False, 'type': '2'}
        with patch('gui.ChaoXingAPI', return_value=api), patch('gui.QMessageBox.warning') as warning:
            self.window.login_qr()
            self.wait_idle()
            warning.assert_called_once()
        self.assertIsNone(self.window.qr.matrix)
        self.assertTrue(self.window.qr_start.isEnabled())
        api.session.close.assert_called_once()

    def test_close_waits_for_cancelled_worker(self):
        self.window.show()
        self.window.launch('test', lambda context: context.wait(5), lambda value: None)
        self.window.close()
        self.assertTrue(self.window.closing)
        self.wait_idle()
        self.assertFalse(self.window.isVisible())

    def test_course_runs_in_background_and_updates_result_table(self):
        chapter = ChapterModel(1, 1, 1, '测试章节', '1', 1, '', 1, 0)
        def run(context, config):
            context.event('progress', value=1, total=1, text='completed')
            chapter.point_finish = 1
        task = SimpleNamespace(kind='document', title='测试文档', pre_fetch=lambda: True,
                               fetch=lambda: True, run=run)
        self.window.chapters = SimpleNamespace(chapters=[chapter], point_issues=[],
            fetch_point_status=lambda: None, is_finished=lambda index: chapter.point_finish == 1,
            fetch_points_by_index=lambda index, enabled: [task])
        self.window.enable_boxes['exam'].setChecked(False)
        for wait in self.window.wait_boxes.values():
            wait.setValue(0)
        with tempfile.TemporaryDirectory() as directory:
            def runner(chapters, config, context, only_chapters=None, start_chapter=None):
                return run_course(chapters, config, context, only_chapters, Path(directory), start_chapter)
            with patch('gui.run_course', side_effect=runner):
                self.window.start_course()
                self.wait_idle()
            self.assertTrue(self.window.last_result['verified_complete'])
            self.assertEqual(self.window.last_result['counts']['success'], 1)
            self.assertEqual(self.window.result_table.rowCount(), 1)
            self.assertEqual(self.window.progress.value(), 1000)

    def test_cancelled_network_failure_does_not_open_error_dialog(self):
        release = threading.Event()
        def operation(context):
            release.wait(1)
            raise ValueError('request failed after cancellation')
        with patch('gui.QMessageBox.warning') as warning:
            self.window.launch('test', operation, lambda result: None)
            self.window.stop()
            release.set()
            self.wait_idle()
            warning.assert_not_called()

    def test_expiration_during_save_dialog_does_not_save_invalid_qr(self):
        self.window.qr.set_payload(PAYLOAD)
        def choose(*args):
            self.window.qr.clear()
            return 'unused.png', 'PNG'
        with patch('gui.QFileDialog.getSaveFileName', side_effect=choose), patch('gui.QMessageBox.information') as info, patch('gui.matrix_image') as render:
            self.window.save_qr()
            render.assert_not_called()
            info.assert_called_once()


if __name__ == '__main__':
    unittest.main()
