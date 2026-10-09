"""Rebuild the optional, standalone GUI packager itself."""
import argparse
import json
import shutil
import sys
import tempfile
from pathlib import Path

from build_backend import Pipeline


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='将打包工具自身生成 CxKitty-Packager.exe')
    parser.add_argument('--output', type=Path, default=project)
    args = parser.parse_args()
    if sys.platform != 'win32':
        parser.error('请在 Windows 中构建')
    args.output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cxkitty-packager-build-') as temporary:
        work = Path(temporary)
        command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean', '--windowed',
            '--onefile', '--noupx', '--name', 'CxKitty-Packager', '--distpath', str(work / 'dist'),
            '--workpath', str(work / 'work'), '--specpath', str(work / 'spec'),
            '--icon', str(project / 'xxt_helper.ico'),
            '--add-data', str(project / 'xxt_helper.ico') + ';_app_icon']
        # These imports exist only in backend functions executed by external Python.
        for module in ('PyInstaller', 'settings', 'cxapi', 'gui', 'cli', 'package_check',
                       'ai_searcher', 'ai_credentials', 'ai_selfcheck', 'workflow_selfcheck', 'video_progress', 'reliability_selfcheck',
                       'workspace_tabs', 'workspace_selfcheck',
                       'requests', 'Crypto', 'lxml', 'jsonpath', 'qrcode', 'rich', 'wcwidth',
                       'numpy', 'cv2', 'matplotlib', 'PyQt5', 'PyQt6', 'tkinter'):
            command += ['--exclude-module', module]
        command.append(str(project / 'packager.py'))
        pipeline = Pipeline(lambda kind, **fields: print(fields.get('text', ''), flush=True))
        code = pipeline.run_process(command, project)
        if code:
            return code
        destination = args.output.resolve() / 'CxKitty-Packager.exe'
        shutil.copy2(work / 'dist' / destination.name, destination)
        report_path = args.output.resolve() / 'packager-verification' / 'self-check.json'
        code = pipeline.run_process([str(destination), '--self-check', str(report_path)], work, timeout=120)
        report = json.loads(report_path.read_text(encoding='utf-8')) if report_path.is_file() else {}
        if code or not report.get('ok') or not report.get('frozen'):
            raise RuntimeError('打包工具 EXE 自检未通过，请检查 packager-verification/self-check.json')
        print(str(destination))
    return 0


if __name__ == '__main__':
    sys.exit(main())
