"""Graphical build tool; build jobs run in the selected Python interpreter."""
import codecs
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QMainWindow, QMessageBox, QPlainTextEdit,
    QProgressBar, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from build_backend import BuildOptions, EVENT_PREFIX, clean_environment
from app_identity import app_icon, configure_application, set_windows_identity

STYLE = '''
QWidget { font-family: "Microsoft YaHei UI", "Microsoft YaHei"; font-size: 10pt; color: #213449; }
QMainWindow, QWidget#page { background: #f3f6fa; }
QLabel#title { font-size: 23pt; font-weight: 700; color: #113947; }
QLabel#hint { color: #627489; }
QGroupBox { background: white; border: 1px solid #dce4ed; border-radius: 10px; margin-top: 14px; padding: 16px; }
QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; }
QLineEdit, QComboBox { background: white; border: 1px solid #cbd7e3; border-radius: 6px; padding: 7px; }
QPushButton { background: white; border: 1px solid #cbd7e3; border-radius: 6px; padding: 8px 14px; }
QPushButton:hover { background: #eaf4f2; border-color: #147d72; }
QPushButton#primary { background: #147d72; color: white; font-weight: 600; }
QPushButton:disabled { background: #edf1f5; color: #91a0b1; border-color: #e0e6ed; }
QCheckBox { padding: 3px; spacing: 7px; }
QPlainTextEdit { background: #152435; color: #d6e7ed; border: none; border-radius: 8px; padding: 8px; font-family: Consolas, "Microsoft YaHei UI"; font-size: 9pt; }
QProgressBar { border: none; background: #dfe9ee; max-height: 8px; border-radius: 4px; }
QProgressBar::chunk { background: #1b9787; border-radius: 4px; }
'''


def label(text, hint=False):
    widget = QLabel(text)
    widget.setTextFormat(Qt.TextFormat.PlainText)
    widget.setWordWrap(True)
    if hint:
        widget.setObjectName('hint')
    return widget


def default_project():
    folder = Path(sys.executable if getattr(sys, 'frozen', False) else __file__).resolve().parent
    for candidate in (folder, *list(folder.parents)[:3], Path.cwd()):
        if (candidate / 'build_backend.py').is_file() and (candidate / 'main.py').is_file():
            return candidate
    return folder


def default_python(project):
    for directory in ('.venv-build', '.venv'):
        local = project / directory / 'Scripts' / 'python.exe'
        if local.is_file():
            return str(local)
    if not getattr(sys, 'frozen', False):
        python = Path(sys.executable)
        if python.name.lower() == 'pythonw.exe':
            python = python.with_name('python.exe')
        return str(python)
    return shutil.which('python') or ''


class PackagerWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.process = None
        self.cancel_directory = None
        self.cancel_path = None
        self.closing = False
        self.release = None
        self.executable = None
        self.action = ''
        self.cancel_requested = False
        self.buffer = ''
        self.decoder = codecs.getincrementaldecoder('utf-8')(errors='replace')
        self.setWindowTitle('CxKitty · EXE 打包工具')
        self.setWindowIcon(app_icon())
        self.resize(1000, 900)
        self.setMinimumSize(800, 620)
        self.setStyleSheet(STYLE)
        page = QWidget(objectName='page')
        layout = QVBoxLayout(page)
        layout.setContentsMargins(26, 20, 26, 20)
        layout.setSpacing(12)
        title = label('CxKitty EXE 打包工具')
        title.setObjectName('title')
        layout.addWidget(title)
        layout.addWidget(label('选择项目 → 检查环境 → 开始打包 → 自动验证成品', True))

        self.options_box = QGroupBox('构建选项')
        form = QFormLayout(self.options_box)
        form.setSpacing(10)
        project = default_project()
        self.project = QLineEdit(str(project))
        self.python = QLineEdit(default_python(project))
        self.output = QLineEdit(str(project / 'dist'))
        self.name = QLineEdit('CxKitty')
        self.mode = QComboBox()
        self.mode.addItem('单文件 EXE · 便于携带，每次启动需解压', 'onefile')
        self.mode.addItem('目录模式 · 启动较快，需要携带 _internal', 'onedir')
        self.default_icon = str(project / 'xxt_helper.ico')
        self.icon = QLineEdit(self.default_icon)
        self.icon.setPlaceholderText('留空使用所选项目中的 xxt_helper.ico')
        self.icon.setToolTip('同时用于 EXE 文件、主程序窗口及任务栏；二维码不受影响。')
        self.project.textChanged.connect(self.project_changed)
        form.addRow('项目目录', self.browse_row(self.project, 'directory'))
        form.addRow('Python 解释器', self.browse_row(self.python, 'python'))
        form.addRow('输出位置', self.browse_row(self.output, 'directory'))
        form.addRow('程序名称', self.name)
        form.addRow('打包模式', self.mode)
        form.addRow('程序图标', self.browse_row(self.icon, 'icon'))
        self.config_box = QCheckBox('携带当前配置（保留设置与扩展字段）')
        self.config_box.setChecked(False)
        self.banks_box = QCheckBox('复制本地题库及 AI 答案缓存，并调整发布版路径')
        self.banks_box.setChecked(False)
        self.sessions_box = QCheckBox('携带账号会话及视频续接记录（含 Cookie 及可能保存的密码，仅供本人使用）')
        self.ai_keys_box = QCheckBox('携带 AI 密钥（独立保存为 ai_credentials.json，仅供本人迁移）')
        form.addRow('携带数据', self.config_box)
        form.addRow('', self.banks_box)
        form.addRow('', self.sessions_box)
        form.addRow('', self.ai_keys_box)
        form.addRow('', label('原项目保持不变。每次创建新的发布目录；不会复制历史日志。\n'
                             '不携带配置时使用默认设置。EXE 的配置、会话和日志写在 EXE 旁边。', True))
        self.config_box.toggled.connect(self.toggle_data)
        self.toggle_data(False)
        options_scroll = QScrollArea()
        options_scroll.setWidgetResizable(True)
        options_scroll.setFrameShape(QScrollArea.Shape.NoFrame)
        options_scroll.setMinimumHeight(210)
        options_container = QWidget(objectName='page')
        options_layout = QVBoxLayout(options_container)
        options_layout.setContentsMargins(0, 0, 0, 0)
        options_layout.addWidget(self.options_box)
        options_scroll.setWidget(options_container)
        layout.addWidget(options_scroll, 3)

        actions = QHBoxLayout()
        self.check_button = self.button('检查环境', lambda: self.start('check'))
        self.install_button = self.button('安装 / 补齐依赖', lambda: self.start('install'))
        self.install_button.setToolTip('在上方选中的 Python 环境安装 requirements-build.txt；需要联网。建议选用虚拟环境。')
        self.build_button = self.button('开始打包', lambda: self.start('build'))
        self.build_button.setObjectName('primary')
        self.stop_button = self.button('取消', self.cancel)
        self.stop_button.setEnabled(False)
        for button in (self.check_button, self.install_button, self.build_button, self.stop_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        self.status = label('就绪 · 构建电脑需要 Python；生成的主程序 EXE 无需 Python。', True)
        layout.addWidget(self.status)
        self.progress = QProgressBar()
        self.progress.setTextVisible(False)
        self.progress.setValue(0)
        layout.addWidget(self.progress)
        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(6000)
        self.log.setMinimumHeight(170)
        layout.addWidget(self.log, 1)
        bottom = QHBoxLayout()
        self.open_button = self.button('打开输出目录', self.open_output)
        self.run_button = self.button('运行生成的 EXE', self.run_executable)
        self.run_button.setEnabled(False)
        bottom.addWidget(self.open_button)
        bottom.addWidget(self.run_button)
        bottom.addStretch()
        bottom.addWidget(self.button('导出当前日志', self.save_log))
        bottom.addWidget(self.button('使用帮助', self.show_help))
        layout.addLayout(bottom)
        self.setCentralWidget(page)

    def project_changed(self, value):
        previous = self.default_icon
        self.default_icon = str(Path(value).expanduser() / 'xxt_helper.ico')
        if not self.icon.text().strip() or self.icon.text() == previous:
            self.icon.setText(self.default_icon)

    @staticmethod
    def button(text, callback):
        button = QPushButton(text)
        button.clicked.connect(callback)
        return button

    def browse_row(self, field, kind):
        row = QWidget()
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(field)
        layout.addWidget(self.button('浏览…', lambda: self.browse(field, kind)))
        return row

    def browse(self, field, kind):
        if kind == 'directory':
            value = QFileDialog.getExistingDirectory(self, '选择目录', field.text())
        else:
            value, _ = QFileDialog.getOpenFileName(self, '选择 Python 解释器' if kind == 'python' else '选择图标',
                field.text(), 'Python (python.exe);;可执行文件 (*.exe)' if kind == 'python' else 'Windows 图标 (*.ico)')
        if value:
            field.setText(value)

    def toggle_data(self, checked):
        for box in (self.banks_box, self.sessions_box, self.ai_keys_box):
            box.setEnabled(checked)
            if not checked:
                box.setChecked(False)

    def start(self, action):
        if self.process:
            return
        try:
            project_text = self.project.text().strip()
            if not project_text:
                raise ValueError('请选择项目目录')
            project = Path(project_text).expanduser().resolve()
            backend = project / 'build_backend.py'
            if not backend.is_file():
                raise ValueError('项目目录中没有 build_backend.py，请选择完整的 CxKitty 源码目录')
            python_text = self.python.text().strip()
            python = Path(python_text).expanduser()
            if not python_text or not python.is_file() or python.name.lower() == 'pythonw.exe':
                raise ValueError('请选择有效的 python.exe（不要选择 pythonw.exe）')
            arguments = ['-u', str(backend), action, '--project', str(project)]
            if action == 'build':
                if not self.output.text().strip():
                    raise ValueError('请选择输出位置')
                options = BuildOptions(project, Path(self.output.text().strip()), self.name.text(),
                    self.mode.currentData(), self.icon.text().strip(), self.config_box.isChecked(),
                    self.banks_box.isChecked(), self.sessions_box.isChecked(), self.ai_keys_box.isChecked()).validate()
                arguments += ['--output', str(options.output), '--name', options.name, '--mode', options.mode]
                if options.icon:
                    arguments += ['--icon', options.icon]
                if not options.copy_config:
                    arguments.append('--no-config')
                if not options.copy_banks:
                    arguments.append('--no-banks')
                if options.copy_sessions:
                    arguments.append('--sessions')
                if options.copy_ai_keys:
                    arguments.append('--ai-keys')
            self.cancel_directory = tempfile.TemporaryDirectory(prefix='cxkitty-build-control-')
            self.cancel_path = Path(self.cancel_directory.name) / 'cancel'
            arguments += ['--cancel-file', str(self.cancel_path)]
            self.action = action
            self.cancel_requested = False
            self.buffer = ''
            self.decoder.reset()
            self.log.clear()
            if action == 'build':
                self.executable = self.release = None
            self.process = QProcess(self)
            self.process.setWorkingDirectory(str(project))
            environment = QProcessEnvironment()
            for key, value in clean_environment(str(python)).items():
                environment.insert(key, value)
            self.process.setProcessEnvironment(environment)
            self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
            self.process.readyReadStandardOutput.connect(self.read_output)
            self.process.finished.connect(self.finished)
            self.process.errorOccurred.connect(self.process_error)
            self.set_busy(True)
            self.status.setText('正在启动构建任务…')
            self.process.start(str(python.resolve()), arguments)
        except Exception as error:
            QMessageBox.warning(self, '无法开始', str(error))

    def set_busy(self, busy):
        self.options_box.setEnabled(not busy)
        for button in (self.check_button, self.install_button, self.build_button):
            button.setEnabled(not busy)
        self.stop_button.setEnabled(busy)
        self.run_button.setEnabled(not busy and self.executable is not None)
        self.progress.setRange(0, 0 if busy else 100)
        if busy:
            self.progress.setValue(0)

    def read_output(self):
        if not self.process:
            return
        self.buffer += self.decoder.decode(bytes(self.process.readAllStandardOutput()))
        while '\n' in self.buffer:
            line, self.buffer = self.buffer.split('\n', 1)
            self.consume_line(line.rstrip('\r'))

    def consume_line(self, line):
        if not line.startswith(EVENT_PREFIX):
            self.log.appendPlainText(line)
            return
        try:
            event = json.loads(line[len(EVENT_PREFIX):])
        except ValueError:
            self.log.appendPlainText(line)
            return
        kind = event.get('kind')
        if kind in ('log', 'error', 'cancelled'):
            self.log.appendPlainText(event.get('text', ''))
        if kind in ('stage', 'error', 'cancelled'):
            self.status.setText(event['text'])
        if kind == 'output':
            self.release = Path(event['path'])
            self.log.appendPlainText('本次输出：' + event['path'])
        if kind == 'success':
            self.executable = Path(event['executable'])
            self.release = Path(event['path'])
            self.log.appendPlainText('已通过实际 EXE 自检：' + event['executable'])

    def process_error(self, error):
        if self.process and error == QProcess.ProcessError.FailedToStart:
            self.log.appendPlainText(self.process.errorString())
            self.finished(-1, QProcess.ExitStatus.CrashExit)

    def finished(self, code, exit_status):
        if not self.process:
            return
        self.read_output()
        self.buffer += self.decoder.decode(b'', final=True)
        if self.buffer:
            self.consume_line(self.buffer)
            self.buffer = ''
        self.process.deleteLater()
        self.process = None
        self.cancel_directory.cleanup()
        self.cancel_directory = None
        self.cancel_path = None
        success = code == 0 and exit_status == QProcess.ExitStatus.NormalExit
        if self.action == 'build':
            success = success and self.executable is not None
        self.set_busy(False)
        self.progress.setValue(100 if success else 0)
        if success:
            self.status.setText('打包成功 · 实际 EXE 自检通过，可打开输出目录或运行程序。' if self.action == 'build'
                                else '环境已就绪，可以开始打包。')
        elif self.cancel_requested or code == 130:
            self.status.setText('已取消 · 原项目和以前的构建未受影响。')
        else:
            self.status.setText('未完成 · 请查看日志中的具体错误；不将未通过验证的 EXE 视为成品。')
            self.executable = None
            self.run_button.setEnabled(False)
        if self.closing:
            self.close()

    def cancel(self):
        if self.process and self.cancel_path:
            self.cancel_requested = True
            self.cancel_path.touch()
            self.stop_button.setEnabled(False)
            self.status.setText('正在取消并清理本次构建，请稍候…')

    def closeEvent(self, event):
        if self.process:
            self.closing = True
            self.cancel()
            event.ignore()
        else:
            event.accept()

    def open_output(self):
        folder = self.release or Path(self.output.text()).expanduser()
        if folder.is_dir():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder.resolve())))
        else:
            QMessageBox.information(self, '输出目录', '尚未创建输出目录；开始打包后将自动创建。')

    def run_executable(self):
        if self.executable and self.executable.is_file():
            process = QProcess(self)
            process.setProgram(str(self.executable))
            process.setWorkingDirectory(str(self.executable.parent))
            environment = QProcessEnvironment()
            for key, value in clean_environment().items():
                environment.insert(key, value)
            process.setProcessEnvironment(environment)
            started, _ = process.startDetached()
            process.deleteLater()
            if not started:
                QMessageBox.warning(self, '启动失败', '无法启动 EXE，请从输出目录打开并查看系统提示。')

    def save_log(self):
        path, _ = QFileDialog.getSaveFileName(self, '导出当前日志', 'packaging-log.txt', '文本 (*.txt)')
        if path:
            try:
                Path(path).write_text(self.log.toPlainText(), encoding='utf-8')
            except OSError as error:
                QMessageBox.warning(self, '无法保存', str(error))

    def show_help(self):
        QMessageBox.information(self, '打包工具使用说明',
            '1. 选择包含 main.py 和 build_backend.py 的完整项目目录。\n'
            '2. 选择 Python 3.10～3.14 的 python.exe，推荐使用项目虚拟环境。\n'
            '3. 点击检查环境；缺少依赖时点击安装 / 补齐依赖（需要网络）。\n'
            '4. 选择输出目录、名称、单文件或目录模式，以及需要携带的数据。\n'
            '   默认使用项目的 xxt_helper.ico，统一 EXE、窗口和任务栏图标。\n'
            '5. 点击开始打包，等待五个阶段完成；日志实时显示进度。\n'
            '6. 显示打包成功后，可以打开输出目录或运行生成的 EXE。\n\n'
            '单文件 EXE 包含 Python 和运行依赖；数据仍需存放在 EXE 外部以便更新。\n'
            '把整个发布目录复制到同架构的 Windows 电脑即可携带设置和题库。\n'
            '每次创建新目录，不覆盖旧版本。完整构建日志保存在 build-log.txt。\n'
            'AI 功能自动包含在程序中；密钥默认不携带。勾选携带后也仅写入独立 JSON 文件。\n'
            '自动验证是离线检查；实际登录与课程服务仍依赖网络和平台。')


def main():
    # Do not pass the packager bundle's DLL search path to external Python.
    if getattr(sys, 'frozen', False) and os.name == 'nt':
        import ctypes
        ctypes.windll.kernel32.SetDllDirectoryW(None)
    set_windows_identity(packager=True)
    app = QApplication.instance() or QApplication(sys.argv)
    configure_application(app, packager=True)
    window = PackagerWindow()
    if '--self-check' in sys.argv:
        return run_self_check(window, app, Path(sys.argv[sys.argv.index('--self-check') + 1]))
    window.show()
    return app.exec()


def run_self_check(window, app, report_path):
    """Inspect the real frozen packager without invoking pip or a project build."""
    import hashlib
    import traceback
    from app_identity import icon_path
    report_path = report_path.resolve()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report = {'ok': False, 'frozen': bool(getattr(sys, 'frozen', False))}
    try:
        assert not window.windowIcon().pixmap(32, 32).isNull()
        assert Path(window.icon.text()) == Path(window.project.text()) / 'xxt_helper.ico'
        assert not window.sessions_box.isChecked() and not window.ai_keys_box.isChecked()
        assert window.mode.currentData() == 'onefile'
        window.show()
        app.processEvents()
        assert window.grab().save(str(report_path.parent / 'packager.png'))
        report.update(ok=True, icon_sha256=hashlib.sha256(icon_path().read_bytes()).hexdigest(),
                      default_icon=window.icon.text(), private_data_excluded=True)
    except Exception:
        report['error'] = traceback.format_exc()
    finally:
        window.close()
        app.processEvents()
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    return 0 if report['ok'] else 1


if __name__ == '__main__':
    sys.exit(main())
