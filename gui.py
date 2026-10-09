"""Independent desktop workspace. Each page owns its session and background worker."""
import copy
import sys
import time
from datetime import datetime

from PySide6.QtCore import QThread, Qt, Signal, QSignalBlocker, QUrl
from PySide6.QtGui import QCloseEvent, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QHeaderView, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSpinBox, QSplitter,
    QTabWidget, QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from cxapi.api import ChaoXingAPI
from app_identity import VERSION, app_icon, configure_application, set_windows_identity
from ai_credentials import load_api_key, save_api_key
from cxapi.network import error_text
from engine import chapter_snapshot, run_course, first_pending_course, load_course_chapters
from qr_image import QRWidget, matrix_image
from runtime import Cancelled, TaskContext, TaskControl
from searcher import create_searcher
from settings import ROOT, load_config, save_config, save_json
from utils import ck2dict, mask_name, mask_phone, save_session, sessions_load


class Worker(QThread):
    event = Signal(object)
    result = Signal(object)
    failed = Signal(str)

    def __init__(self, operation, parent=None):
        super().__init__(parent)
        self.operation = operation
        self.control = TaskControl()

    def run(self):
        value = None
        try:
            context = TaskContext(self.event.emit, self.control)
            with context.bound():
                value = self.operation(context)
            # A stopped login must not be accepted just because its request finished.
            self.control.checkpoint()
            self.result.emit(value)
        except Cancelled:
            if isinstance(value, ChaoXingAPI):
                value.session.close()
            self.event.emit({'kind': 'log', 'text': '操作已停止', 'level': 'info'})
        except Exception as error:
            if self.control.cancelled:
                self.event.emit({'kind': 'log', 'text': '操作已停止', 'level': 'info'})
            else:
                self.failed.emit(error_text(error))


STYLE = '''
QMainWindow, QWidget#root { background: #f3f6fa; }
QWidget { color: #172c43; font-family: "Microsoft YaHei UI", "Microsoft YaHei", sans-serif; font-size: 10pt; }
QWidget#scrollContent, QScrollArea { background: #f3f6fa; }
QLabel#title { font-size: 25pt; font-weight: 700; color: #113947; }
QLabel#subtitle { color: #61748a; }
QLabel#badge { color: #126459; background: #e0f2ec; border-radius: 12px; padding: 7px 16px; }
QGroupBox { background: white; border: 1px solid #dce4ed; border-radius: 12px; margin-top: 16px; padding: 20px 16px 14px; font-weight: 600; }
QGroupBox::title { subcontrol-origin: margin; left: 16px; padding: 0 5px; }
QPushButton { background: white; border: 1px solid #cbd7e3; border-radius: 7px; padding: 8px 15px; min-height: 20px; }
QPushButton:hover { background: #edf4f7; border-color: #6d929e; }
QPushButton:disabled { color: #91a0b1; background: #edf1f5; border-color: #e0e6ed; }
QPushButton[primary="true"] { background: #147d72; color: white; border: 1px solid #147d72; font-weight: 600; }
QPushButton[primary="true"]:hover { background: #116b62; }
QPushButton[primary="true"]:disabled { background: #9ac7c1; border-color: #9ac7c1; }
QLineEdit, QComboBox, QSpinBox, QDoubleSpinBox { background: white; border: 1px solid #cbd7e3; border-radius: 6px; padding: 7px; min-height: 20px; }
QLineEdit:focus, QComboBox:focus { border-color: #147d72; }
QTabWidget::pane { border: none; }
QTabBar::tab { background: transparent; padding: 12px 24px; color: #61748a; border-bottom: 3px solid transparent; }
QTabBar::tab:selected { color: #147d72; border-bottom-color: #147d72; font-weight: 600; }
QTreeWidget, QTableWidget, QPlainTextEdit { background: white; border: 1px solid #dce4ed; border-radius: 7px; alternate-background-color: #f7fafc; }
QHeaderView::section { background: #edf3f7; border: none; padding: 10px; font-weight: 600; }
QProgressBar { border: none; border-radius: 5px; background: #dfe9ee; min-height: 10px; max-height: 10px; }
QProgressBar::chunk { border-radius: 5px; background: #1b9787; }
QCheckBox { spacing: 8px; padding: 4px; }
'''


def text_label(text, name=None):
    label = QLabel(text)
    label.setTextFormat(Qt.TextFormat.PlainText)
    label.setWordWrap(True)
    if name:
        label.setObjectName(name)
    return label


def button(text, callback, primary=False):
    widget = QPushButton(text)
    widget.setProperty('primary', primary)
    widget.clicked.connect(callback)
    return widget


def scroll_page(page):
    page.setObjectName('scrollContent')
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setWidget(page)
    return scroll


class MainWindow(QMainWindow):
    manual_login = Signal(object)
    settings_saved = Signal(object)
    state_changed = Signal()
    closed = Signal()

    def __init__(self, config=None, parent=None, run_registry=None):
        super().__init__(parent)
        if parent is not None:
            self.setWindowFlags(Qt.WindowType.Widget)
        self.run_registry = run_registry
        self.closed_emitted = False
        self.last_error = ''
        self.config = copy.deepcopy(config if config is not None else load_config())
        self.api = self.classes = self.chapters = self.worker = None
        self.active_operation = ''
        self.closing = False
        self.last_result = None
        self.next_operation = None
        self.selection_generation = 0
        self.active_chapter_id = None
        self.course_labels = []
        self.chapter_items = {}
        self.busy_widgets = []
        self.setWindowTitle(f'CxKitty {VERSION} · 学习任务助手')
        self.setWindowIcon(app_icon())
        self.resize(1120, 820)
        self.setMinimumSize(900, 640)
        root = QWidget(objectName='root')
        layout = QVBoxLayout(root)
        layout.setContentsMargins(30, 22, 30, 20)
        layout.setSpacing(16)
        top = QHBoxLayout()
        title_box = QVBoxLayout()
        title = text_label('CxKitty', 'title')
        subtitle = text_label('学习任务助手  /  课程进度，一目了然', 'subtitle')
        title.setWordWrap(False)
        subtitle.setWordWrap(False)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        top.addLayout(title_box, 1)
        self.badge = text_label('尚未登录', 'badge')
        self.badge.setWordWrap(False)
        self.badge.setMaximumHeight(36)
        top.addWidget(self.badge, 0, Qt.AlignmentFlag.AlignVCenter)
        layout.addLayout(top)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs)
        self.build_account()
        self.build_course()
        self.build_settings()
        self.build_results()
        self.footer = text_label('就绪 · 请选择已有账号，或扫码登录', 'subtitle')
        layout.addWidget(self.footer)
        self.setCentralWidget(root)
        self.setStyleSheet(STYLE)
        self.load_sessions()
        self.update_controls()

    def build_account(self):
        page = QWidget()
        row = QHBoxLayout(page)
        row.setSpacing(20)
        left = QVBoxLayout()
        saved = QGroupBox('已有账号')
        saved_layout = QVBoxLayout(saved)
        self.accounts = QComboBox()
        self.use_account = button('使用所选账号', self.restore_account, True)
        saved_layout.addWidget(text_label('选择已保存的账号，继续处理课程任务', 'subtitle'))
        saved_layout.addWidget(self.accounts)
        saved_layout.addWidget(self.use_account)
        left.addWidget(saved)
        password = QGroupBox('手机号登录')
        form = QFormLayout(password)
        self.phone = QLineEdit()
        self.phone.setPlaceholderText('请输入手机号')
        self.password = QLineEdit()
        self.password.setPlaceholderText('请输入学习通密码')
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        self.remember = QCheckBox('保存密码，用于失效后自动重登')
        self.remember.setToolTip('勾选后密码会以原项目格式保存在本地 JSON 中；不勾选仍会保存登录会话。')
        form.addRow('手机号', self.phone)
        form.addRow('密码', self.password)
        form.addRow(self.remember)
        self.password_login = button('登录', self.login_password, True)
        form.addRow(self.password_login)
        self.password.returnPressed.connect(self.login_password)
        left.addWidget(password)
        self.account_info = text_label('登录后可选择课程，并查看各章节完成情况。', 'subtitle')
        left.addWidget(self.account_info)
        left.addStretch()
        row.addLayout(left, 1)
        qr_group = QGroupBox('扫码登录')
        qr_layout = QVBoxLayout(qr_group)
        qr_layout.addWidget(text_label('打开学习通，使用“扫一扫”扫描下方二维码。', 'subtitle'))
        self.qr = QRWidget()
        qr_layout.addWidget(self.qr, 1)
        self.qr_status = text_label('二维码生成后有效时间有限，请及时扫码。', 'subtitle')
        qr_layout.addWidget(self.qr_status)
        qr_buttons = QHBoxLayout()
        self.qr_start = button('生成二维码', self.login_qr, True)
        self.qr_cancel = button('取消扫码', self.stop)
        self.qr_save = button('保存二维码图片', self.save_qr)
        qr_buttons.addWidget(self.qr_start)
        qr_buttons.addWidget(self.qr_cancel)
        qr_buttons.addWidget(self.qr_save)
        qr_layout.addLayout(qr_buttons)
        row.addWidget(qr_group, 1)
        self.busy_widgets += [self.accounts, self.use_account, self.phone, self.password,
                              self.remember, self.password_login, self.qr_start]
        self.tabs.addTab(scroll_page(page), '账号登录')

    def build_course(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        selector = QHBoxLayout()
        self.course_combo = QComboBox()
        self.course_combo.setMinimumWidth(300)
        self.course_combo.setPlaceholderText('登录后自动加载课程')
        self.refresh_button = button('刷新课程', self.refresh_courses)
        self.load_button = button('刷新章节', self.load_chapters)
        self.platform_button = button('在平台打开', self.open_course)
        selector.addWidget(self.course_combo, 1)
        selector.addWidget(self.refresh_button)
        selector.addWidget(self.load_button)
        selector.addWidget(self.platform_button)
        layout.addLayout(selector)
        self.course_combo.currentIndexChanged.connect(self.selection_changed)
        self.course_hint = text_label('登录后自动加载课程；选择课程后自动显示章节。', 'subtitle')
        layout.addWidget(self.course_hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(['章节', '任务点', '状态'])
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.setColumnWidth(1, 120)
        self.tree.setColumnWidth(2, 150)
        layout.addWidget(self.tree, 1)
        range_row = QHBoxLayout()
        range_row.addWidget(text_label('起始章节'))
        self.start_chapter = QComboBox()
        self.start_chapter.addItem('自动：从首个未完成章节开始', None)
        range_row.addWidget(self.start_chapter, 1)
        self.use_start_button = button('从选中章节开始', self.use_selected_start)
        range_row.addWidget(self.use_start_button)
        layout.addLayout(range_row)
        self.tree.itemDoubleClicked.connect(lambda item, column: self.use_selected_start())
        self.continue_courses = QCheckBox('本课程全部完成后，继续后续未完成课程')
        self.continue_courses.setChecked(self.config.get('workflow', {}).get('continue_courses', False))
        layout.addWidget(self.continue_courses)
        active = QGroupBox('当前任务')
        active_layout = QVBoxLayout(active)
        self.task_label = text_label('等待开始')
        self.progress_label = text_label('任务进度将在这里显示', 'subtitle')
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        active_layout.addWidget(self.task_label)
        active_layout.addWidget(self.progress)
        active_layout.addWidget(self.progress_label)
        actions = QHBoxLayout()
        self.run_button = button('开始处理', self.start_course, True)
        self.pause_button = button('暂停', self.pause)
        self.stop_button = button('停止', self.stop)
        self.retry_button = button('继续未完成 / 重试', lambda: self.start_course(retry=True))
        for widget in (self.run_button, self.pause_button, self.stop_button, self.retry_button):
            actions.addWidget(widget)
        actions.addStretch()
        active_layout.addLayout(actions)
        layout.addWidget(active)
        self.busy_widgets += [self.refresh_button, self.load_button, self.start_chapter,
                              self.use_start_button, self.continue_courses]
        self.tabs.addTab(page, '课程任务')

    def build_settings(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        kinds = QGroupBox('任务设置')
        form = QFormLayout(kinds)
        self.enable_boxes, self.wait_boxes = {}, {}
        for key, title in (('video', '视频'), ('document', '文档'), ('exam', '章节测验')):
            row = QHBoxLayout()
            check = QCheckBox('启用' + title)
            check.setChecked(self.config[key]['enable'])
            wait = QSpinBox()
            wait.setRange(0, 3600)
            wait.setValue(self.config[key]['wait'])
            wait.setSuffix(' 秒')
            row.addWidget(check)
            row.addStretch()
            row.addWidget(text_label('完成后等待'))
            row.addWidget(wait)
            form.addRow(row)
            self.enable_boxes[key], self.wait_boxes[key] = check, wait
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.1, 16)
        self.speed.setSingleStep(0.25)
        self.speed.setValue(self.config['video']['speed'])
        self.rate = QDoubleSpinBox()
        self.rate.setRange(1, 300)
        self.rate.setSuffix(' 秒')
        self.rate.setValue(self.config['video']['report_rate'])
        form.addRow('视频速度', self.speed)
        form.addRow('进度上报间隔', self.rate)
        layout.addWidget(kinds)
        bank = QGroupBox('题库设置')
        bank_form = QFormLayout(bank)
        self.bank_type = QComboBox()
        for label, key in (('本地 JSON', 'jsonFileSearcher'), ('本地 SQLite', 'sqliteSearcher'),
                           ('REST API', 'restApiSearcher'), ('AI 自动答题', 'aiSearcher')):
            self.bank_type.addItem(label, key)
        self.bank_type.setCurrentIndex(self.bank_type.findData(self.config['searcher']['use']))
        bank_form.addRow('题库类型', self.bank_type)
        self.bank_fields = {}
        for key, label, fields in (
            ('jsonFileSearcher', 'JSON', [('file_path', '文件')]),
            ('sqliteSearcher', 'SQLite', [('file_path', '文件'), ('table', '表名'), ('req_field', '题目字段'), ('rsp_field', '答案字段')]),
            ('restApiSearcher', 'API', [('url', '地址'), ('method', '请求方式'), ('req_field', '请求字段'), ('rsp_field', '答案 JSONPath')]),
        ):
            for field, title in fields:
                edit = QLineEdit(str(self.config['searcher'][key][field]))
                self.bank_fields[(key, field)] = edit
                row = QHBoxLayout()
                row.addWidget(edit)
                if field == 'file_path':
                    row.addWidget(button('选择', lambda checked=False, target=edit: self.choose_file(target)))
                label_widget = text_label(label + ' ' + title)
                holder = QWidget()
                holder.setMinimumHeight(42)
                holder.setLayout(row)
                row.setContentsMargins(0, 0, 0, 0)
                bank_form.addRow(label_widget, holder)
                edit.setProperty('holder', holder)
                edit.setProperty('formLabel', label_widget)
        layout.addWidget(bank)
        self.ai_box = QGroupBox('AI 自动答题 · Chat Completions 兼容接口')
        ai_form = QFormLayout(self.ai_box)
        ai = self.config['searcher']['aiSearcher']
        self.ai_url = QLineEdit(ai['url'])
        self.ai_url.setPlaceholderText('https://你的服务地址/v1，或完整的 /chat/completions 地址')
        self.ai_model = QLineEdit(ai['model'])
        self.ai_model.setPlaceholderText('填写服务商提供的模型 ID')
        self.ai_key = QLineEdit()
        self.ai_key.setEchoMode(QLineEdit.EchoMode.Password)
        self.ai_key.setPlaceholderText('API 密钥；无需认证的本地模型可留空')
        self.ai_key.setToolTip('保存设置时独立写入 ai_credentials.json（本地明文文件），不写入 config.yml 或日志。打包默认不携带。')
        self.ai_key_notice = text_label('密钥按接口地址独立保存；修改地址后会载入该地址的已存密钥。', 'subtitle')
        self.ai_key_url = ai['url'].strip()
        try:
            self.ai_key.setText(ai['api_key'] if 'api_key' in ai else load_api_key(ai['url']))
        except ValueError as error:
            self.ai_key_notice.setText(str(error))
        self.ai_url.editingFinished.connect(self.load_ai_key)
        self.ai_strategy = QComboBox()
        self.ai_strategy.addItem('全部使用 AI · 自动获取并提交', 'ai_only')
        self.ai_strategy.addItem('原题库优先 · 未匹配时使用 AI', 'bank_first')
        self.ai_strategy.setCurrentIndex(self.ai_strategy.findData(ai['strategy']))
        self.ai_fallback = QComboBox()
        for title, key in (('本地 JSON', 'jsonFileSearcher'), ('本地 SQLite', 'sqliteSearcher'), ('REST API', 'restApiSearcher')):
            self.ai_fallback.addItem(title, key)
        self.ai_fallback.setCurrentIndex(self.ai_fallback.findData(ai['fallback']))
        self.ai_timeout, self.ai_retries, self.ai_max_tokens = QSpinBox(), QSpinBox(), QSpinBox()
        for widget, name, low, high in ((self.ai_timeout, 'timeout', 5, 600),
                                       (self.ai_retries, 'retries', 0, 5),
                                       (self.ai_max_tokens, 'max_tokens', 128, 65536)):
            widget.setRange(low, high)
            widget.setValue(ai[name])
        self.ai_timeout.setSuffix(' 秒')
        self.ai_retries.setSuffix(' 次')
        self.ai_token_parameter = QComboBox()
        self.ai_token_parameter.addItem('max_tokens（多数兼容服务）', 'max_tokens')
        self.ai_token_parameter.addItem('max_completion_tokens（部分推理模型）', 'max_completion_tokens')
        self.ai_token_parameter.setCurrentIndex(self.ai_token_parameter.findData(ai['token_parameter']))
        self.ai_cache = QCheckBox('缓存已获得的答案，重复题目直接自动作答')
        self.ai_cache.setChecked(ai['cache'])
        self.ai_cache_path = QLineEdit(ai['cache_path'])
        for title, widget in (('接口 URL', self.ai_url), ('API 密钥', self.ai_key), ('模型名称', self.ai_model),
                ('答题策略', self.ai_strategy), ('优先题库', self.ai_fallback), ('请求超时', self.ai_timeout),
                ('失败后重试', self.ai_retries), ('输出 token 上限', self.ai_max_tokens),
                ('上限参数名称', self.ai_token_parameter), ('答案缓存', self.ai_cache), ('缓存文件', self.ai_cache_path)):
            ai_form.addRow(title, widget)
        ai_form.addRow(self.ai_key_notice)
        ai_form.addRow(text_label('发送题干、题型与选项，不发送学习通账号或 Cookie。获取有效选项后直接自动提交。\n'
                                 '支持当前的文字单选、多选和判断题；不进行正确率或置信度审核。\n'
                                 '“检查题库 / AI”会发送一道测试题，可能产生少量接口费用。', 'subtitle'))
        layout.addWidget(self.ai_box)
        self.bank_type.currentIndexChanged.connect(self.update_bank_fields)
        self.ai_strategy.currentIndexChanged.connect(self.update_bank_fields)
        self.ai_fallback.currentIndexChanged.connect(self.update_bank_fields)
        self.update_bank_fields()
        self.bank_hint = text_label('题库不可用时会记录测验失败，视频和文档仍可继续。', 'subtitle')
        layout.addWidget(self.bank_hint)
        actions = QHBoxLayout()
        self.save_settings_button = button('保存设置', self.save_settings, True)
        self.check_bank_button = button('检查题库 / AI', self.check_bank)
        actions.addWidget(self.save_settings_button)
        actions.addWidget(self.check_bank_button)
        actions.addStretch()
        layout.addLayout(actions)
        layout.addStretch()
        self.busy_widgets += [page]
        self.tabs.addTab(scroll_page(page), '设置')

    def build_results(self):
        page = QWidget()
        layout = QVBoxLayout(page)
        bar = QHBoxLayout()
        self.summary_label = text_label('尚无运行记录')
        bar.addWidget(self.summary_label, 1)
        bar.addWidget(button('导出结果', self.export_results))
        bar.addWidget(button('复制日志', lambda: QApplication.clipboard().setText(self.log.toPlainText())))
        layout.addLayout(bar)
        splitter = QSplitter(Qt.Orientation.Vertical)
        self.result_table = QTableWidget(0, 4)
        self.result_table.setHorizontalHeaderLabels(['章节', '任务', '结果', '说明'])
        self.result_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.result_table.setAlternatingRowColors(True)
        self.result_table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.Stretch)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setPlaceholderText('运行日志会保留在这里，完成后的明细自动写入 logs 目录。')
        splitter.addWidget(self.result_table)
        splitter.addWidget(self.log)
        splitter.setSizes([350, 180])
        layout.addWidget(splitter)
        self.tabs.addTab(page, '结果与日志')

    def append_log(self, text):
        self.log.appendPlainText(datetime.now().strftime('%H:%M:%S') + '  ' + text)

    def load_sessions(self):
        self.sessions = sessions_load(self.config['sessionPath'], self.append_log)
        self.accounts.clear()
        for session in self.sessions:
            name = mask_name(session.name) if self.config['maskAcc'] else session.name
            phone = mask_phone(session.phone) if self.config['maskAcc'] else session.phone
            self.accounts.addItem(f'{name} · {phone}')
        if not self.sessions:
            self.accounts.setPlaceholderText('暂无存档，请扫码或使用密码登录')

    def update_controls(self):
        busy = self.worker is not None
        for widget in self.busy_widgets:
            widget.setEnabled(not busy)
        self.use_account.setEnabled(not busy and bool(self.sessions))
        self.refresh_button.setEnabled(not busy and self.api is not None)
        self.load_button.setEnabled(not busy and self.classes is not None and self.course_combo.currentIndex() >= 0)
        self.course_combo.setEnabled(self.classes is not None and (not busy or self.active_operation in ('chapters', 'course_scan')))
        self.platform_button.setEnabled(self.classes is not None and self.course_combo.currentIndex() >= 0)
        self.start_chapter.setEnabled(not busy and self.chapters is not None)
        self.use_start_button.setEnabled(not busy and self.chapters is not None)
        self.run_button.setEnabled(not busy and self.chapters is not None)
        self.retry_button.setEnabled(not busy and self.chapters is not None and bool(self.last_result and self.last_result['retry_chapters']))
        self.pause_button.setEnabled(busy and self.active_operation == 'course')
        self.stop_button.setEnabled(busy)
        self.qr_cancel.setEnabled(busy and self.active_operation == 'qr')
        self.qr_save.setEnabled(self.qr.matrix is not None)
        self.state_changed.emit()

    def launch(self, name, operation, success):
        if self.worker is not None or self.closing:
            return
        self.last_error = ''
        self.active_operation = name
        self.worker = Worker(operation, self)
        self.worker.on_success = success
        self.worker.event.connect(self.handle_event)
        self.worker.result.connect(self.operation_succeeded)
        self.worker.failed.connect(self.operation_failed)
        self.worker.finished.connect(self.operation_finished)
        self.footer.setText('正在处理，请稍候…')
        self.update_controls()
        self.worker.start()

    def operation_succeeded(self, value):
        worker = self.sender()
        if worker is not self.worker or worker.control.cancelled or self.closing:
            if isinstance(value, ChaoXingAPI):
                value.session.close()
            return
        worker.on_success(value)

    def defer(self, callback):
        if self.worker is None:
            callback()
        else:
            self.next_operation = callback

    def operation_failed(self, message):
        self.last_error = message
        self.footer.setText(message)
        self.append_log(message)
        if self.active_operation == 'qr':
            self.qr.clear(message)
            self.qr_status.setText(message)
        self.state_changed.emit()
        if self.parent() is None and not self.closing:
            QMessageBox.warning(self, '操作未完成', message)

    def operation_finished(self):
        worker, self.worker = self.worker, None
        if self.run_registry is not None:
            self.run_registry.release(self)
        if self.active_operation == 'qr' and self.qr.matrix is not None:
            self.qr.clear('扫码已结束，需要登录时请重新生成')
        self.active_operation = ''
        self.pause_button.setText('暂停')
        self.update_controls()
        if worker:
            worker.deleteLater()
        if self.closing:
            self.close()
        elif self.next_operation:
            callback, self.next_operation = self.next_operation, None
            callback()

    def login_password(self):
        if self.worker is not None:
            return
        phone, password, remember = self.phone.text().strip(), self.password.text(), self.remember.isChecked()
        if not phone or not password:
            QMessageBox.information(self, '请填写登录信息', '请输入手机号和密码。')
            return
        def operation(context):
            api = ChaoXingAPI()
            try:
                status, _ = api.login_passwd(phone, password)
                if not status:
                    raise ValueError('密码登录失败，请检查账号密码；需要额外验证时可使用扫码登录')
                return self.finish_login(api, password if remember else None, context)
            except Exception:
                api.session.close()
                raise
        self.launch('login', operation, self.logged_in)

    def finish_login(self, api, password, context):
        context.checkpoint()
        if not api.accinfo():
            raise ValueError('登录尚未生效，请重新登录')
        context.checkpoint()
        try:
            save_session(api, password, self.config['sessionPath'])
        except (OSError, ValueError):
            context.log('登录成功，但会话无法保存；本次仍可继续使用', 'warning')
        return api

    def restore_account(self):
        index = self.accounts.currentIndex()
        if not 0 <= index < len(self.sessions):
            return
        session = self.sessions[index]
        def operation(context):
            api = ChaoXingAPI()
            try:
                api.ck_load(ck2dict(session.ck))
                if not api.accinfo():
                    if not session.passwd:
                        raise ValueError('会话已失效，请扫码或使用密码重新登录')
                    context.log('会话已失效，正在使用已保存密码重新登录')
                    api.session.cookies.clear()
                    status, _ = api.login_passwd(session.phone, session.passwd)
                    if not status:
                        raise ValueError('重新登录失败，请扫码登录')
                return self.finish_login(api, session.passwd, context)
            except Exception:
                api.session.close()
                raise
        self.launch('login', operation, self.logged_in)

    def login_qr(self):
        self.qr.clear('正在获取二维码…')
        self.qr_status.setText('请稍候…')
        def operation(context):
            api = ChaoXingAPI()
            try:
                api.qr_get()
                context.checkpoint()
                context.event('qr', payload=api.qr_geturl())
                deadline = time.monotonic() + 180
                scanned = False
                while time.monotonic() < deadline:
                    context.checkpoint()
                    result = api.login_qr()
                    if result.get('status') in (True, 1, 'true'):
                        return self.finish_login(api, None, context)
                    state = str(result.get('type', ''))
                    if state in ('1', '2'):
                        raise ValueError('二维码已失效或验证失败，请重新生成')
                    if state == '4' and not scanned:
                        scanned = True
                        context.event('qr_status', text='已扫描，请在手机上确认登录')
                    context.wait(1.5)
                raise ValueError('二维码等待超时，请重新生成')
            except Exception:
                api.session.close()
                raise
        self.launch('qr', operation, self.logged_in)

    def logged_in(self, api, manual=True):
        if self.api:
            self.api.session.close()
        self.api, self.classes, self.chapters = api, None, None
        self.selection_generation += 1
        self.password.clear()
        self.course_combo.clear()
        self.tree.clear()
        name = mask_name(api.acc.name) if self.config['maskAcc'] else api.acc.name
        self.badge.setText('已登录 · ' + name)
        self.account_info.setText(f'{name} · {api.acc.school}')
        self.footer.setText('登录成功，正在自动加载课程…')
        self.qr.clear('登录成功')
        self.qr_status.setText('登录成功，会话已加载')
        self.load_sessions()
        self.tabs.setCurrentIndex(1)
        if manual:
            self.manual_login.emit(api)
        self.state_changed.emit()
        self.defer(self.refresh_courses)

    def refresh_courses(self):
        api = self.api
        if api is None:
            return
        self.launch('courses', lambda context: api.fetch_classes(), self.courses_loaded)

    def courses_loaded(self, classes):
        self.classes, self.chapters = classes, None
        self.last_result = None
        self.active_chapter_id = None
        self.tree.clear()
        self.chapter_items = {}
        self.start_chapter.clear()
        self.start_chapter.addItem('自动：从首个未完成章节开始', None)
        self.course_hint.setText('正在核验课程…' if classes.classes else '当前账号没有可用课程')
        self.course_labels = [f'{c.name} · {c.teacher_name}' + ('（已结课）' if c.state else '') for c in classes.classes]
        with QSignalBlocker(self.course_combo):
            self.course_combo.clear()
            self.course_combo.addItems(self.course_labels)
            self.course_combo.setCurrentIndex(-1)
        self.footer.setText(f'已找到 {len(classes.classes)} 门课程' if classes.classes else '当前账号没有可用课程')
        if classes.classes:
            self.defer(self.select_pending_course)
        self.update_controls()

    def select_pending_course(self, start=0, autorun=False):
        classes, generation = self.classes, self.selection_generation
        if classes is None:
            return
        excluded = set()
        if self.run_registry is not None and self.api is not None:
            excluded = {i for i, course in enumerate(classes.classes)
                        if self.run_registry.is_busy(self.api.acc, course, self)}
        def selected(result):
            if generation != self.selection_generation or classes is not self.classes:
                return
            index, chapters, all_done = result
            if index is None:
                available = [i for i in range(start, len(classes.classes)) if i not in excluded]
                if not autorun and available:
                    self.course_combo.setCurrentIndex(available[0])
                self.footer.setText('没有后续可自动处理的课程；已跳过其他页正在处理的课程' if excluded else '没有后续可自动处理的课程')
                return
            if autorun and all_done:
                self.footer.setText('后续可访问课程的任务已全部完成')
                return
            with QSignalBlocker(self.course_combo):
                self.course_combo.setCurrentIndex(index)
            if chapters is None:
                self.defer(self.load_chapters)
                return
            self.chapters_loaded(chapters)
            if all_done:
                self.footer.setText('可访问课程的任务已全部完成')
            elif autorun:
                self.defer(self.start_course)
        self.launch('course_scan', lambda context: first_pending_course(classes, context, start, excluded_indices=excluded), selected)

    def selection_changed(self):
        self.selection_generation += 1
        self.chapters = None
        self.last_result = None
        self.tree.clear()
        self.chapter_items = {}
        self.start_chapter.clear()
        self.start_chapter.addItem('自动：从首个未完成章节开始', None)
        self.course_hint.setText('正在自动加载所选课程的章节…')
        if self.worker is not None and self.active_operation in ('chapters', 'course_scan'):
            self.worker.control.cancel()
        if self.classes is not None and self.course_combo.currentIndex() >= 0:
            self.defer(self.load_chapters)
        self.update_controls()

    def load_chapters(self):
        classes, index = self.classes, self.course_combo.currentIndex()
        generation = self.selection_generation
        if classes is None or index < 0:
            return
        def loaded(chapters):
            if generation == self.selection_generation and classes is self.classes:
                self.chapters_loaded(chapters)
        self.launch('chapters', lambda context: load_course_chapters(classes, index, context), loaded)

    def chapters_loaded(self, chapters):
        self.chapters = chapters
        self.active_chapter_id = None
        self.last_result = None
        self.tree.clear()
        self.chapter_items = {}
        snapshot = chapter_snapshot(chapters)
        self.render_chapters(snapshot)
        self.start_chapter.clear()
        self.start_chapter.addItem('自动：从首个未完成章节开始', None)
        for c in snapshot:
            self.start_chapter.addItem(c['label'] + '  ' + c['name'], c['id'])
        self.course_hint.setText(f"{sum(not c['is_directory'] for c in snapshot)} 个小节 · {sum(c['pending'] for c in snapshot)} 个待完成 · 已完成任务自动跳过")
        self.footer.setText('章节已加载，可以开始处理')
        self.update_controls()

    def render_chapters(self, items):
        if set(self.chapter_items) != {c['id'] for c in items}:
            self.tree.clear()
            self.chapter_items = {}
        parents = []
        for chapter in items:
            total, finished = chapter['total'], chapter['finished']
            state = chapter.get('state', '已完成' if total > 0 and finished >= total else '待核验' if total < 0 else '无任务点' if total == 0 else '待处理')
            item = self.chapter_items.get(chapter['id'])
            if item is None:
                item = QTreeWidgetItem()
                item.setData(0, Qt.ItemDataRole.UserRole, chapter['id'])
                while parents and parents[-1][0] >= chapter.get('layer', 1):
                    parents.pop()
                parent = self.chapter_items.get(chapter.get('parent_id')) or (parents[-1][1] if parents else None)
                if parent:
                    parent.addChild(item)
                else:
                    self.tree.addTopLevelItem(item)
                item.setExpanded(True)
                self.chapter_items[chapter['id']] = item
            if chapter.get('is_directory'):
                parents.append((chapter.get('layer', 1), item))
            item.setText(0, chapter['label'] + '  ' + chapter['name'])
            item.setText(1, f'{finished}/{total}' if total > 0 else '—' if total == 0 else '未知')
            item.setText(2, state)
            details = '、'.join(chapter.get('unsupported', []))
            item.setToolTip(2, ('需在平台查看的组件：' + details) if details else state)

    def use_selected_start(self):
        if self.worker is not None:
            return
        item = self.tree.currentItem()
        if item:
            self.start_chapter.setCurrentIndex(self.start_chapter.findData(item.data(0, Qt.ItemDataRole.UserRole)))
            self.footer.setText('已设置起始章节；点击“开始处理”后向后顺序执行')

    def open_course(self):
        if self.classes is not None and self.course_combo.currentIndex() >= 0:
            from urllib.parse import urlencode
            course = self.classes.classes[self.course_combo.currentIndex()]
            QDesktopServices.openUrl(QUrl('https://mooc1.chaoxing.com/visit/interaction?' + urlencode(
                {'courseId': course.courseid, 'clazzid': course.clazzid, 'cpi': course.cpi})))

    def start_course(self, checked=False, retry=False):
        if self.chapters is None or self.worker is not None or self.closing:
            return
        selected = set(self.last_result['retry_chapters']) if retry and self.last_result else None
        start = None if retry else self.start_chapter.currentData()
        config = self.collect_settings()
        if not any(config[kind]['enable'] for kind in ('video', 'exam', 'document')):
            QMessageBox.information(self, '没有启用任务', '请在设置中至少启用一种任务类型。')
            return
        if self.run_registry is not None and not self.run_registry.acquire(self.chapters, self):
            self.last_error = '同一账号的这门课程正在其他任务页处理，请选择其他课程或先停止该页'
            self.footer.setText(self.last_error)
            self.append_log(self.last_error)
            self.state_changed.emit()
            return
        self.result_table.setRowCount(0)
        self.last_result = None
        self.summary_label.setText('正在处理…')
        chapters = self.chapters
        self.launch('course', lambda context: run_course(chapters, config, context, only_chapters=selected, start_chapter=start), lambda result: None)

    def course_finished(self, result):
        self.last_result = result
        counts = result['counts']
        summary = f"{result['status']} · 成功 {counts['success']} / 失败 {counts['failed']} / 跳过 {counts['skipped']}"
        self.summary_label.setText(summary)
        self.footer.setText(summary)
        self.task_label.setText(result['status'])
        self.progress.setRange(0, 1000)
        self.progress.setValue(1000 if result['verified_complete'] else 0)
        self.progress_label.setText(result['status'])
        if result['blocked']:
            self.badge.setText('已确认登录失效，请重新登录')
            self.chapters = None
        if result.get('report_path'):
            self.append_log('运行明细已保存到 ' + result['report_path'])
        if (result['verified_complete'] and self.continue_courses.isChecked() and self.classes is not None
                and not self.closing and not (self.worker and self.worker.control.cancelled)):
            next_index = self.course_combo.currentIndex() + 1
            self.defer(lambda: self.select_pending_course(next_index, autorun=True))

    def pause(self):
        if self.worker:
            paused = not self.worker.control.paused
            self.worker.control.set_paused(paused)
            self.pause_button.setText('继续' if paused else '暂停')
            self.footer.setText('已暂停后续处理；已发出的网络请求可能仍在完成' if paused else '继续处理')
            item = self.chapter_items.get(self.active_chapter_id)
            if item:
                item.setText(2, '已暂停' if paused else '处理中')
            self.state_changed.emit()

    def stop(self):
        self.next_operation = None
        if self.worker:
            self.worker.control.cancel()
            self.footer.setText('正在停止；已发出的请求将在下次运行时重新核验')
            self.stop_button.setEnabled(False)
            self.pause_button.setEnabled(False)
            if self.active_operation == 'qr':
                self.qr.clear('已取消扫码')
                self.qr_save.setEnabled(False)
        self.state_changed.emit()

    def handle_event(self, event):
        kind = event['kind']
        if kind in ('course_scan', 'course_status') and self.worker and self.worker.control.cancelled:
            return
        if kind == 'log':
            self.append_log(event['text'])
        elif kind == 'qr':
            if self.worker and not self.worker.control.cancelled:
                self.qr.set_payload(event['payload'])
                self.qr_status.setText('等待扫码；请在手机上确认登录')
                self.qr_save.setEnabled(True)
        elif kind == 'qr_status':
            self.qr_status.setText(event['text'])
        elif kind == 'progress':
            total = event['total']
            self.progress.setRange(0, 1000 if total else 0)
            if total:
                self.progress.setValue(round(1000 * event['value'] / total))
            self.progress_label.setText(event['text'])
        elif kind == 'task':
            self.task_label.setText(event['text'])
        elif kind == 'chapter':
            self.active_chapter_id = event['id']
            item = self.chapter_items.get(event['id'])
            if item:
                self.tree.setCurrentItem(item)
                self.tree.scrollToItem(item)
            self.footer.setText('正在处理 ' + event['text'])
        elif kind == 'chapters':
            self.render_chapters(event['items'])
            self.course_hint.setText(f"{sum(not c.get('is_directory', False) for c in event['items'])} 个小节 · "
                                    f"{sum(c.get('pending', False) for c in event['items'])} 个待完成 · 已完成任务自动跳过")
        elif kind == 'result':
            row = self.result_table.rowCount()
            self.result_table.insertRow(row)
            labels = {'success': '成功', 'failed': '失败', 'skipped': '跳过', 'manual': '平台处理'}
            for column, value in enumerate((event['chapter'], event['title'], labels[event['status']], event['detail'])):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                self.result_table.setItem(row, column, item)
        elif kind == 'summary':
            # Summaries must survive cancellation; Worker discards other stopped results.
            self.course_finished(event)
        elif kind == 'course_scan':
            self.footer.setText(event['text'])
        elif kind == 'course_status':
            index = event['index']
            if index < len(self.course_labels):
                self.course_combo.setItemText(index, self.course_labels[index] + (' · 已完成' if event['complete'] else ' · 有待完成任务'))

    def choose_file(self, edit):
        path, _ = QFileDialog.getOpenFileName(self, '选择题库', str(ROOT), '题库 (*.json *.db *.sqlite *.sqlite3);;所有文件 (*)')
        if path:
            edit.setText(path)

    def update_bank_fields(self):
        selected = self.bank_type.currentData()
        ai_enabled = selected == 'aiSearcher'
        self.ai_box.setVisible(ai_enabled)
        self.ai_fallback.setEnabled(self.ai_strategy.currentData() == 'bank_first')
        if ai_enabled and self.ai_strategy.currentData() == 'bank_first':
            selected = self.ai_fallback.currentData()
        for (key, field), edit in self.bank_fields.items():
            edit.property('holder').setVisible(key == selected)
            edit.property('formLabel').setVisible(key == selected)

    def load_ai_key(self):
        url = self.ai_url.text().strip()
        if url == self.ai_key_url:
            return
        self.ai_key_url = url
        self.ai_key.clear()
        try:
            self.ai_key.setText(load_api_key(url))
            self.ai_key_notice.setText('已切换接口地址；请确认此地址对应的密钥。')
        except ValueError as error:
            self.ai_key_notice.setText(str(error))

    def collect_settings(self):
        config = copy.deepcopy(self.config)
        config.setdefault('workflow', {})['continue_courses'] = self.continue_courses.isChecked()
        for key in self.enable_boxes:
            config[key]['enable'] = self.enable_boxes[key].isChecked()
            config[key]['wait'] = self.wait_boxes[key].value()
        config['video'].update(speed=self.speed.value(), report_rate=self.rate.value())
        config['searcher']['use'] = self.bank_type.currentData()
        for (key, field), edit in self.bank_fields.items():
            config['searcher'][key][field] = edit.text().strip()
        ai = config['searcher']['aiSearcher']
        ai.update(url=self.ai_url.text().strip(), model=self.ai_model.text().strip(),
                  strategy=self.ai_strategy.currentData(), fallback=self.ai_fallback.currentData(),
                  timeout=self.ai_timeout.value(), retries=self.ai_retries.value(),
                  max_tokens=self.ai_max_tokens.value(), token_parameter=self.ai_token_parameter.currentData(),
                  cache=self.ai_cache.isChecked(), cache_path=self.ai_cache_path.text().strip())
        if config['searcher']['use'] == 'aiSearcher' or self.ai_key.text():
            ai['api_key'] = self.ai_key.text().strip()
        return config

    def save_settings(self):
        try:
            config = self.collect_settings()
            from settings import validate_config
            validate_config(config)
            if config['searcher']['aiSearcher']['url'].strip() or self.ai_key.text().strip():
                save_api_key(self.ai_url.text().strip(), self.ai_key.text().strip())
            save_config(config)
            self.config = config
            self.settings_saved.emit(copy.deepcopy(config))
            self.footer.setText('设置已保存：本页后续任务和新建页使用此设置，其他已打开页保持原设置')
        except Exception as error:
            QMessageBox.warning(self, '无法保存设置', error_text(error))

    def check_bank(self):
        config = self.collect_settings()['searcher']
        def operation(context):
            searcher = create_searcher(config)
            try:
                if config['use'] == 'aiSearcher':
                    return searcher.check_connection(context)
                if config['use'] == 'restApiSearcher':
                    result = searcher.invoke('CxKitty 连接检查')
                    if result.get('code') == -1 and 'error' in result:
                        raise ValueError(result['error'])
                    return '题库接口可访问，实际题目命中情况取决于题库内容和响应字段配置'
                return '本地题库格式与字段检查通过'
            finally:
                searcher.close()
        self.launch('bank', operation, lambda message: self.bank_hint.setText(message))

    def save_qr(self):
        matrix = self.qr.matrix
        if matrix is None:
            return
        path, _ = QFileDialog.getSaveFileName(self, '保存二维码原图', 'login-qr.png', 'PNG 图片 (*.png)')
        if path and self.qr.matrix is not matrix:
            QMessageBox.information(self, '二维码已失效', '当前二维码已失效或登录已经结束，请重新生成。')
            return
        if path and not matrix_image(matrix, 8).save(path, 'PNG'):
            QMessageBox.warning(self, '保存失败', '二维码图片无法写入所选位置。')

    def export_results(self):
        if not self.last_result:
            QMessageBox.information(self, '暂无结果', '任务结束后可以导出结果。')
            return
        path, _ = QFileDialog.getSaveFileName(self, '导出结果', 'cxkitty-results.json', 'JSON (*.json)')
        if path:
            try:
                save_json(path, self.last_result)
            except OSError:
                QMessageBox.warning(self, '导出失败', '无法写入所选位置。')

    def closeEvent(self, event: QCloseEvent):
        self.closing = True
        self.next_operation = None
        if self.worker is not None:
            self.stop()
            self.footer.setText('正在结束后台请求，完成后自动关闭窗口…')
            event.ignore()
            return
        if self.api:
            self.api.session.close()
        if self.run_registry is not None:
            self.run_registry.release(self)
        event.accept()
        if not self.closed_emitted:
            self.closed_emitted = True
            self.closed.emit()


def run_gui():
    from workspace_tabs import WorkspaceWindow
    set_windows_identity()
    application = QApplication.instance() or QApplication(sys.argv)
    configure_application(application)
    window = WorkspaceWindow()
    if application.primaryScreen():
        screen = application.primaryScreen().availableGeometry()
        window.resize(min(1160, screen.width() - 40), min(880, screen.height() - 60))
    window.show()
    return application.exec()
