"""Browser-style workspaces with independent login, controls and lifetimes."""
import copy
from dataclasses import dataclass

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QTabBar, QTabWidget, QToolButton

from app_identity import VERSION, app_icon
from cxapi.api import ChaoXingAPI
from gui import MainWindow, STYLE, button
from settings import load_config
from utils import mask_name


@dataclass
class LoginSnapshot:
    account: object
    cookies: object
    headers: dict

    @classmethod
    def capture(cls, api):
        return cls(copy.deepcopy(api.acc), api.session.cookies.copy(), dict(api.session.headers))

    def create_api(self):
        api = ChaoXingAPI()
        api.acc = copy.deepcopy(self.account)
        api.session.cookies = self.cookies.copy()
        api.session.headers.update(self.headers)
        return api


class CourseRuns:
    """Owned only by the GUI thread. Release after the worker fully finishes."""
    def __init__(self):
        self.owners = {}

    @staticmethod
    def key(account, course):
        return str(account.puid), str(course.courseid), str(course.clazzid)

    def is_busy(self, account, course, owner):
        active = self.owners.get(self.key(account, course))
        return active is not None and active is not owner

    def acquire(self, chapters, owner):
        if self.is_busy(chapters.acc, chapters, owner):
            return False
        self.owners[self.key(chapters.acc, chapters)] = owner
        return True

    def release(self, owner):
        for key in [key for key, value in self.owners.items() if value is owner]:
            del self.owners[key]


class WorkspaceWindow(QMainWindow):
    def __init__(self, config=None):
        super().__init__()
        self.default_config = copy.deepcopy(config if config is not None else load_config())
        self.latest_login = None
        self.run_registry = CourseRuns()
        self.pages = []
        self.serial = 0
        self.closing = False
        self.setWindowTitle(f'CxKitty {VERSION} · 多任务学习助手')
        self.setWindowIcon(app_icon())
        self.setStyleSheet(STYLE + '''
QTabWidget#workspaces > QTabBar::tab { background: #e4ecf3; padding: 10px 16px; border: none; border-top: 3px solid transparent; }
QTabWidget#workspaces > QTabBar::tab:selected { background: #f3f6fa; border-top-color: #147d72; }
''')
        self.resize(1160, 880)
        self.setMinimumSize(940, 710)
        self.workspaces = QTabWidget(objectName='workspaces')
        self.workspaces.setMovable(True)
        self.workspaces.setTabsClosable(True)
        self.workspaces.setUsesScrollButtons(True)
        self.workspaces.tabBar().setExpanding(False)
        self.workspaces.tabBar().setElideMode(Qt.TextElideMode.ElideRight)
        self.workspaces.tabCloseRequested.connect(self.close_page)
        self.add_button = button('＋ 新建页', self.add_page)
        self.add_button.setToolTip('Ctrl+T：新建独立任务页，沿用最近一次成功手动登录的账号')
        self.workspaces.setCornerWidget(self.add_button, Qt.Corner.TopRightCorner)
        self.setCentralWidget(self.workspaces)
        self.shortcuts = []
        for key, callback in [('Ctrl+T', self.add_page), ('Ctrl+W', self.close_current_page),
                              ('Ctrl+Tab', lambda: self.switch_page(1)),
                              ('Ctrl+Shift+Tab', lambda: self.switch_page(-1))]:
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.activated.connect(callback)
            self.shortcuts.append(shortcut)
        self.add_page()

    def add_page(self, checked=False):
        if self.closing:
            return None
        self.serial += 1
        page = MainWindow(self.default_config, parent=self.workspaces, run_registry=self.run_registry)
        page.page_number = self.serial
        self.pages.append(page)
        page.manual_login.connect(self.remember_login)
        page.settings_saved.connect(self.remember_settings)
        page.state_changed.connect(lambda: self.update_page(page))
        page.closed.connect(lambda: QTimer.singleShot(0, lambda: self.remove_page(page)))
        index = self.workspaces.addTab(page, f'任务 {self.serial}')
        close_button = QToolButton(self.workspaces.tabBar())
        close_button.setText('×')
        close_button.setAccessibleName('关闭任务页')
        close_button.setToolTip('关闭此页并停止该页任务（Ctrl+W）')
        close_button.setFixedSize(24, 24)
        close_button.setStyleSheet('QToolButton { color: #61748a; border: none; border-radius: 4px; font-size: 16pt; }'
                                  'QToolButton:hover { color: #ab3030; background: #f7dede; }')
        close_button.clicked.connect(lambda: self.close_page(self.workspaces.indexOf(page)))
        self.workspaces.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, close_button)
        self.workspaces.setCurrentIndex(index)
        if self.latest_login is not None:
            page.logged_in(self.latest_login.create_api(), manual=False)
            page.append_log('已沿用最近一次手动登录的账号；本页会话、任务和设置独立')
        self.update_page(page)
        return page

    def remember_login(self, api):
        # Only successful explicit login/account selection updates this snapshot.
        self.latest_login = LoginSnapshot.capture(api)

    def remember_settings(self, config):
        self.default_config = copy.deepcopy(config)

    def update_page(self, page):
        index = self.workspaces.indexOf(page)
        if index < 0:
            return
        state = '未登录' if page.api is None else '就绪'
        if page.closing:
            state = '正在关闭'
        elif page.worker is not None:
            if page.worker.control.cancelled:
                state = '正在停止'
            elif page.worker.control.paused:
                state = '已暂停'
            else:
                state = {'course': '运行中', 'login': '登录中', 'qr': '扫码中', 'bank': '检查中'}.get(page.active_operation, '加载中')
        elif page.last_error:
            state = '需处理'
        elif page.last_result:
            result = page.last_result
            state = ('登录失效' if result['blocked'] else '已停止' if result['cancelled'] else
                     '已完成' if result['verified_complete'] else '有失败' if result['counts']['failed'] else '待核验')
        account = ''
        if page.api is not None:
            account = mask_name(page.api.acc.name) if page.config['maskAcc'] else page.api.acc.name
        course = ''
        if page.classes is not None and page.course_combo.currentIndex() >= 0:
            course = page.classes.classes[page.course_combo.currentIndex()].name
        title = course or account or f'任务 {page.page_number}'
        if len(title) > 16:
            title = title[:15] + '…'
        self.workspaces.setTabText(index, f'{title} · {state}'.replace('&', '&&'))
        self.workspaces.setTabToolTip(index, f'任务 {page.page_number} · {account}\n{course}\n{state}\n{page.footer.text()}')

    def switch_page(self, step):
        if self.workspaces.count():
            self.workspaces.setCurrentIndex((self.workspaces.currentIndex() + step) % self.workspaces.count())

    def close_current_page(self):
        self.close_page(self.workspaces.currentIndex())

    def close_page(self, index):
        page = self.workspaces.widget(index)
        if page is not None:
            page.close()

    def remove_page(self, page):
        if page not in self.pages:
            return
        self.workspaces.removeTab(self.workspaces.indexOf(page))
        self.pages.remove(page)
        page.deleteLater()
        if not self.pages:
            self.close()

    def closeEvent(self, event):
        self.closing = True
        self.add_button.setEnabled(False)
        if self.pages:
            for page in list(self.pages):
                page.close()
            event.ignore()
            return
        self.latest_login = None
        event.accept()
