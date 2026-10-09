"""Shared desktop identity; bundled artwork is separate from writable user data."""
import sys
from pathlib import Path

from PySide6.QtGui import QIcon

VERSION = '0.5.0'


def icon_path():
    if getattr(sys, 'frozen', False):
        folder = Path(sys._MEIPASS) / '_app_icon'
        return next(folder.glob('*.ico'), folder / 'xxt_helper.ico')
    return Path(__file__).resolve().parent / 'xxt_helper.ico'


def app_icon():
    return QIcon(str(icon_path()))


def set_windows_identity(packager=False):
    if sys.platform == 'win32':
        import ctypes
        # A stable, distinct ID prevents grouping with Python or the packager.
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            'CxKitty.Packager' if packager else 'CxKitty.Desktop')


def configure_application(app, packager=False):
    app.setApplicationName('CxKitty Packager' if packager else 'CxKitty')
    app.setApplicationVersion(VERSION)
    app.setWindowIcon(app_icon())
