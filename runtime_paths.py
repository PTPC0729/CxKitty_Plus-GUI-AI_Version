"""Persistent data paths shared by source and frozen entry points."""
import os
import sys
from pathlib import Path


def data_root():
    override = os.environ.get('CXKITTY_DATA_DIR')
    if override:
        return Path(override).expanduser().resolve()
    if getattr(sys, 'frozen', False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def enable_console():
    """Restore interactive / redirected streams in a Windows windowed EXE."""
    if os.name != 'nt' or not getattr(sys, 'frozen', False):
        return
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.GetStdHandle.argtypes = [wintypes.DWORD]
    kernel.GetStdHandle.restype = wintypes.HANDLE
    kernel.GetFileType.argtypes = [wintypes.HANDLE]
    kernel.GetFileType.restype = wintypes.DWORD
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    kernel.DuplicateHandle.argtypes = [wintypes.HANDLE, wintypes.HANDLE,
        wintypes.HANDLE, ctypes.POINTER(wintypes.HANDLE), wintypes.DWORD,
        wintypes.BOOL, wintypes.DWORD]
    inherited = {name: kernel.GetStdHandle(code) for name, code in
                 (('stdin', -10), ('stdout', -11), ('stderr', -12))}
    # AttachConsole may reset standard handles; preserve inherited pipes first.
    redirected = {}
    for name, handle in inherited.items():
        if handle and kernel.GetFileType(handle) in (1, 3):  # disk or pipe
            duplicate = wintypes.HANDLE()
            process = kernel.GetCurrentProcess()
            if kernel.DuplicateHandle(process, handle, process, ctypes.byref(duplicate), 0, False, 2):
                redirected[name] = duplicate.value
    if not kernel.GetConsoleWindow():
        if not kernel.AttachConsole(wintypes.DWORD(-1)):
            kernel.AllocConsole()
    kernel.SetConsoleCP(65001)
    kernel.SetConsoleOutputCP(65001)
    for name in ('stdin', 'stdout', 'stderr'):
        reading = name == 'stdin'
        if name in redirected:
            fd = msvcrt.open_osfhandle(redirected[name], os.O_RDONLY if reading else os.O_WRONLY)
            stream = os.fdopen(fd, 'r' if reading else 'w', encoding='utf-8', errors='replace', buffering=1)
        else:
            stream = open('CONIN$' if reading else 'CONOUT$', 'r' if reading else 'w',
                          encoding='utf-8', errors='replace', buffering=1)
        setattr(sys, name, stream)
        setattr(sys, '__' + name + '__', stream)
