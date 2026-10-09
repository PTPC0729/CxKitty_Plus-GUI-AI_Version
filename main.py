"""CxKitty desktop entry point; use --cli for terminals and Docker."""
import argparse
import os
import sys


def main():
    if any(option in sys.argv[1:] for option in ('--cli', '--help', '-h')):
        from runtime_paths import enable_console
        enable_console()
    parser = argparse.ArgumentParser(description='CxKitty 学习任务助手')
    parser.add_argument('--cli', action='store_true', help='使用终端界面')
    parser.add_argument('--data-dir', help='数据目录，默认是源码目录或 EXE 所在目录')
    parser.add_argument('--self-check', metavar='REPORT', help='离线自检并写入 JSON 报告，不登录或联网')
    args = parser.parse_args()
    if args.data_dir:
        os.environ['CXKITTY_DATA_DIR'] = args.data_dir
    if args.self_check:
        from package_check import run_check
        return run_check(args.self_check)
    try:
        if args.cli:
            from cli import run_cli
            return run_cli()
        from gui import run_gui
        return run_gui()
    except Exception as error:
        if args.cli:
            print(f'启动失败：{error}', file=sys.stderr)
        else:
            try:
                from PySide6.QtWidgets import QApplication, QMessageBox
                app = QApplication.instance() or QApplication(sys.argv)
                QMessageBox.critical(None, 'CxKitty 无法启动',
                                     f'启动失败：{error}\n\n请检查 config.yml 格式及依赖安装情况。原有文件未被覆盖。')
            except ImportError:
                print('缺少桌面界面依赖，请安装 requirements.txt，或使用 python main.py --cli。', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
