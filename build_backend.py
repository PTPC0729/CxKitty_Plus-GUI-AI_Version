"""CxKitty's reproducible Windows build pipeline (also used by packager.py)."""
import argparse
import copy
import importlib.metadata
import json
import os
import platform
import queue
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from contextlib import closing
from datetime import datetime
from pathlib import Path

EVENT_PREFIX = '@@CXKITTY_BUILD@@'
REQUIRED_FILES = ('main.py', 'gui.py', 'cli.py', 'settings.py', 'runtime_paths.py',
                  'package_check.py', 'qr_image.py', 'requirements-build.txt', 'cxapi/api.py',
                  'ai_searcher.py', 'ai_credentials.py', 'ai_selfcheck.py', 'video_progress.py', 'workflow_selfcheck.py',
                  'reliability_selfcheck.py', 'workspace_tabs.py', 'workspace_selfcheck.py',
                  'app_identity.py', 'xxt_helper.ico')
EXCLUDES = ('tkinter', 'matplotlib', 'numpy', 'cv2', 'pandas', 'scipy', 'IPython',
            'PyQt5', 'PyQt6', 'PySide2', 'pytest', 'packager', 'build_backend')


class BuildCancelled(Exception):
    pass


@dataclass
class BuildOptions:
    project: Path
    output: Path
    name: str = 'CxKitty'
    mode: str = 'onefile'
    icon: str = ''
    copy_config: bool = True
    copy_banks: bool = True
    copy_sessions: bool = False
    copy_ai_keys: bool = False

    def validate(self):
        self.project = Path(self.project).expanduser().resolve()
        self.output = Path(self.output).expanduser().resolve()
        missing = [name for name in REQUIRED_FILES if not (self.project / name).is_file()]
        if missing:
            raise ValueError('项目目录不完整，缺少：' + '、'.join(missing))
        if (not self.name or len(self.name) > 60 or self.name != self.name.strip()
                or self.name.endswith(('.', ' ')) or re.search(r'[<>:"/\\|?*\x00-\x1f]', self.name)
                or self.name in ('.', '..') or self.name.lower().endswith('.exe')
                or re.match(r'^(CON|PRN|AUX|NUL|COM[0-9¹²³]|LPT[0-9¹²³])(?:\.|$)', self.name, re.I)):
            raise ValueError('程序名称须为有效文件名（最多 60 字），不要填写 .exe 后缀或 Windows 保留名称')
        if self.mode not in ('onefile', 'onedir'):
            raise ValueError('不支持的打包模式')
        if self.output.exists() and not self.output.is_dir():
            raise ValueError('输出位置必须是目录')
        icon = Path(self.icon).expanduser() if self.icon else self.project / 'xxt_helper.ico'
        if not icon.is_absolute():
            icon = self.project / icon
        icon = icon.resolve()
        if icon.suffix.lower() != '.ico' or not icon.is_file():
            raise ValueError('图标必须是已有的 .ico 文件；默认使用项目目录中的 xxt_helper.ico')
        self.icon = str(icon)
        if not self.copy_config and (self.copy_banks or self.copy_sessions or self.copy_ai_keys):
            raise ValueError('携带题库、会话或 AI 密钥时需要同时携带当前配置')
        return self


def check_environment(project):
    if sys.platform != 'win32':
        raise ValueError('Windows EXE 需要在 Windows 中构建，不支持跨系统打包')
    if not (3, 10) <= sys.version_info[:2] < (3, 15):
        raise ValueError('请选择 Python 3.10～3.14')
    from packaging.requirements import Requirement
    from packaging.utils import canonicalize_name
    requirements = []
    for filename in ('requirements.txt', 'requirements-build.txt'):
        for line in (Path(project) / filename).read_text(encoding='utf-8-sig').splitlines():
            line = line.strip()
            if line and not line.startswith(('#', '-r')):
                requirements.append(Requirement(line))
    versions, problems = {}, []
    for requirement in requirements:
        if requirement.marker and not requirement.marker.evaluate():
            continue
        try:
            version = importlib.metadata.version(requirement.name)
            versions[canonicalize_name(requirement.name)] = version
            if version not in requirement.specifier:
                problems.append(f'{requirement.name} {version} 不符合 {requirement.specifier}')
        except importlib.metadata.PackageNotFoundError:
            problems.append(f'缺少 {requirement.name}')
    if problems:
        raise ValueError('；'.join(problems) + '。请点击“安装 / 补齐依赖”。')
    # Import binary modules as well: metadata alone cannot detect broken DLLs.
    import PyInstaller
    import PySide6.QtWidgets
    import Crypto.Cipher.AES
    import lxml.html
    import yaml
    import requests
    import jsonpath
    import qrcode
    import rich
    import wcwidth
    return {'python': sys.version.split()[0], 'executable': sys.executable,
            'architecture': platform.machine(), 'bits': 64 if sys.maxsize > 2**32 else 32,
            'dependencies': versions}


def clean_environment(python=None):
    environment = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH',
                'QT_QPA_PLATFORM', 'CXKITTY_DATA_DIR', 'CXKITTY_AI_API_KEY', '_MEIPASS2'):
        environment.pop(key, None)
    # A program launched from a frozen packager must start an independent bundle.
    environment['PYINSTALLER_RESET_ENVIRONMENT'] = '1'
    environment['PYTHONIOENCODING'] = 'utf-8'
    environment['PYTHONUNBUFFERED'] = '1'
    if os.name == 'nt':
        # Tools such as Poppler ship an incompatible ICU under the same DLL name
        # used by Windows/Qt. Do not let PyInstaller resolve against unrelated PATHs.
        windows = Path(environment.get('SystemRoot', r'C:\Windows'))
        paths = [str(Path(python or sys.executable).resolve().parent)]
        if not getattr(sys, 'frozen', False):
            paths.append(sys.base_prefix)
        paths.extend((str(windows / 'System32'), str(windows)))
        environment['PATH'] = os.pathsep.join(dict.fromkeys(paths))
    return environment


def pyinstaller_command(options, workspace):
    workspace = Path(workspace)
    command = [sys.executable, '-m', 'PyInstaller', '--noconfirm', '--clean',
               '--windowed', '--noupx', '--' + options.mode, '--name', options.name,
               '--distpath', str(workspace / 'dist'), '--workpath', str(workspace / 'work'),
               '--specpath', str(workspace / 'spec'), '--paths', str(options.project),
               '--collect-submodules', 'cxapi', '--hidden-import', 'lxml.html',
               '--hidden-import', 'jsonpath', '--collect-data', 'certifi']
    command.extend(['--hidden-import', 'ai_searcher', '--hidden-import', 'ai_credentials',
                    '--hidden-import', 'ai_selfcheck'])
    for module in EXCLUDES:
        command.extend(['--exclude-module', module])
    icon = options.icon or str(options.project / 'xxt_helper.ico')
    command.extend(['--icon', icon, '--add-data', icon + ';_app_icon'])
    command.append(str(options.project / 'main.py'))
    return command


class Pipeline:
    def __init__(self, emit=None, cancel_file=None):
        self.emit = emit or (lambda kind, **fields: None)
        self.cancel_file = Path(cancel_file) if cancel_file else None
        self.log_file = None

    def checkpoint(self):
        if self.cancel_file and self.cancel_file.exists():
            raise BuildCancelled('已取消')

    def log(self, line):
        if self.log_file:
            self.log_file.write(line + '\n')
            self.log_file.flush()
        self.emit('log', text=line)

    def stage(self, text):
        self.checkpoint()
        self.log(text)
        self.emit('stage', text=text)

    def run_process(self, command, cwd, timeout=None):
        self.checkpoint()
        flags = subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
        process = subprocess.Popen(command, cwd=str(cwd), env=clean_environment(),
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
            text=True, encoding='utf-8', errors='replace', creationflags=flags)
        lines = queue.Queue()
        def read_output():
            try:
                for line in process.stdout:
                    lines.put(line.rstrip())
            finally:
                process.stdout.close()
                lines.put(None)
        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        started = time.monotonic()
        stream_closed = False
        try:
            while process.poll() is None or not stream_closed:
                self.checkpoint()
                if timeout and time.monotonic() - started > timeout:
                    raise TimeoutError('EXE 自检超时，请检查构建日志或安全软件提示')
                try:
                    line = lines.get(timeout=0.15)
                except queue.Empty:
                    continue
                if line is None:
                    stream_closed = True
                else:
                    self.log(line)
            return process.wait()
        finally:
            if process.poll() is None:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'],
                        capture_output=True, creationflags=flags, timeout=15)
                else:
                    process.kill()
                process.wait(timeout=20)
            reader.join(timeout=2)

    def copy_data(self, options, release):
        from settings import DEFAULTS, load_config, save_config
        config = load_config(options.project / 'config.yml') if options.copy_config else copy.deepcopy(DEFAULTS)
        old_session = Path(config['sessionPath']).expanduser()
        if not old_session.is_absolute():
            old_session = options.project / old_session
        config['sessionPath'] = 'session/'
        if options.copy_banks:
            for kind, folder in (('jsonFileSearcher', 'json'), ('sqliteSearcher', 'sqlite')):
                self.checkpoint()
                value = config['searcher'][kind]['file_path']
                if not value:
                    self.log(f'{folder} 题库路径为空，保留原配置。')
                    continue
                original = Path(value).expanduser()
                if not original.is_absolute():
                    original = options.project / original
                if not original.is_file():
                    self.log(f'{folder} 题库不存在，保留原配置；该题库在 EXE 中也需要配置后才能使用。')
                    continue
                destination = release / 'data' / folder / original.name
                destination.parent.mkdir(parents=True, exist_ok=True)
                if kind == 'sqliteSearcher':
                    # Snapshot committed WAL content, never open the source for writing.
                    with closing(sqlite3.connect(original.resolve().as_uri() + '?mode=ro', uri=True)) as source:
                        with closing(sqlite3.connect(destination)) as target:
                            source.backup(target, pages=256,
                                          progress=lambda *args: self.checkpoint(), sleep=0.1)
                else:
                    self.copy_file(original, destination)
                config['searcher'][kind]['file_path'] = destination.relative_to(release).as_posix()
                self.log(f'已复制 {folder} 题库，并将发布版配置调整为相对路径。')
        if options.copy_sessions:
            count = 0
            if old_session.is_dir():
                for original in old_session.glob('*.json'):
                    self.checkpoint()
                    self.copy_file(original, release / 'session' / original.name)
                    count += 1
            self.log(f'已复制 {count} 个会话文件（保持原始 JSON 内容）。')
            progress = options.project / 'video_progress.json'
            if progress.is_file():
                self.copy_file(progress, release / 'video_progress.json')
                self.log('已复制独立的视频续接记录。')
        ai = config['searcher']['aiSearcher']
        if options.copy_banks and ai['cache']:
            original = Path(ai['cache_path']).expanduser()
            if not original.is_absolute():
                original = options.project / original
            if original.is_file():
                destination = release / 'data' / 'ai' / 'ai_cache.json'
                self.copy_file(original, destination)
                ai['cache_path'] = destination.relative_to(release).as_posix()
                self.log('已复制 AI 答案缓存，并调整为发布目录中的相对路径。')
        if options.copy_ai_keys:
            from ai_credentials import CREDENTIAL_FILE, credential_id, read_credentials
            from settings import save_json
            credentials = read_credentials(options.project / CREDENTIAL_FILE)
            inline_key = ai.get('api_key', '')
            if inline_key:
                credentials['services'][credential_id(ai['url'])] = inline_key
            if credentials['services']:
                save_json(release / CREDENTIAL_FILE, credentials)
                self.log('已将 AI 密钥复制到独立的 ai_credentials.json，没有嵌入 EXE。')
        else:
            self.log('本次不携带 AI 密钥；发布后可在设置中填写，或设置 CXKITTY_AI_API_KEY。')
        ai.pop('api_key', None)
        save_config(config, release / 'config.yml')

    def copy_file(self, source, destination):
        destination.parent.mkdir(parents=True, exist_ok=True)
        with open(source, 'rb') as src, open(destination, 'xb') as dst:
            while True:
                self.checkpoint()
                chunk = src.read(1024 * 1024)
                if not chunk:
                    break
                dst.write(chunk)

    def build(self, options):
        options.validate()
        self.stage('1 / 5  检查 Python、项目与依赖')
        environment = check_environment(options.project)
        # Fail fast on invalid YAML instead of waiting until after compilation.
        if options.copy_config:
            from settings import load_config
            load_config(options.project / 'config.yml')
        self.checkpoint()
        release = options.output / (options.name + '-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid.uuid4().hex[:6])
        release.mkdir(parents=True, exist_ok=False)
        self.emit('output', path=str(release))
        with (release / 'build-log.txt').open('w', encoding='utf-8') as self.log_file:
            try:
                with tempfile.TemporaryDirectory(prefix='cxkitty-build-') as work:
                    work = Path(work)
                    self.stage('2 / 5  收集依赖并生成 EXE（此阶段可能持续数分钟）')
                    if self.run_process(pyinstaller_command(options, work), options.project):
                        raise RuntimeError('PyInstaller 构建失败，详细原因见上方日志')
                    self.stage('3 / 5  整理可移植程序和所选数据')
                    executable = release / (options.name + '.exe')
                    if options.mode == 'onefile':
                        shutil.move(str(work / 'dist' / executable.name), str(executable))
                    else:
                        for item in (work / 'dist' / options.name).iterdir():
                            shutil.move(str(item), str(release / item.name))
                    self.copy_data(options, release)
                    if (options.project / 'LICENSE').is_file():
                        self.copy_file(options.project / 'LICENSE', release / 'LICENSE')
                    self.stage('4 / 5  启动实际 EXE，验证界面、AI 答题、数据和二维码')
                    report_path = release / 'verification' / 'self-check.json'
                    # Launch with an unrelated cwd and without source paths in the environment.
                    code = self.run_process([str(executable), '--self-check', str(report_path)], work, timeout=180)
                    if not report_path.is_file():
                        raise RuntimeError(f'EXE 未生成自检报告（退出码 {code}），不能确认打包成功')
                    report = json.loads(report_path.read_text(encoding='utf-8'))
                    if code or not report.get('ok') or not report.get('frozen'):
                        self.log(report.get('error', 'EXE 自检失败'))
                        raise RuntimeError('实际 EXE 自检未通过，请检查 verification/self-check.json')
                    if 'ai_http_answer_cache_and_submission' not in report.get('checks', []):
                        raise RuntimeError('EXE 缺少 AI 自动答题自检结果，不能确认新功能已正确打包')
                    if 'workflow_auto_load_start_and_resume' not in report.get('checks', []):
                        raise RuntimeError('EXE 缺少自动加载课程、起始章节及视频续接自检结果')
                    if 'video_resume_units_and_verified_authentication' not in report.get('checks', []):
                        raise RuntimeError('EXE 缺少视频进度单位、失真记录处理及登录误判修复的自检结果')
                    if 'platform_video_report_url_and_course_context' not in report.get('checks', []):
                        raise RuntimeError('EXE 缺少视频上报地址与课程参数协议自检结果')
                    if 'independent_workspaces_login_and_shutdown' not in report.get('checks', []):
                        raise RuntimeError('EXE 缺少多任务页、独立登录与安全关闭自检结果')
                    if 'bundled_application_icon' not in report.get('checks', []):
                        raise RuntimeError('EXE 缺少内置窗口图标自检结果')
                    self.stage('5 / 5  写入使用说明与构建清单')
                    manifest = {'ok': True, 'mode': options.mode, 'executable': executable.name,
                        'environment': environment, 'copy_config': options.copy_config,
                        'copy_banks': options.copy_banks, 'copy_sessions': options.copy_sessions,
                        'copy_ai_keys': options.copy_ai_keys, 'icon': Path(options.icon).name,
                        'verification': report['checks'], 'built_at': datetime.now().isoformat()}
                    (release / 'build-info.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
                    (release / '使用说明.txt').write_text(
                        f'双击 {executable.name} 打开 CxKitty，无需安装 Python。\n'
                        '配置、会话、题库、日志保存在 EXE 同目录。请放在有写入权限的目录。\n'
                        '顶部“＋ 新建页”或 Ctrl+T 新建独立任务页，沿用最近一次成功手动登录的账号。\n'
                        '每页单独选课并开始处理；暂停、停止、切换账号不影响其他页。\n'
                        '同一账号的同一课程不能重复启动；自动选课跳过其他页正在运行的课程。\n'
                        '关闭标签会先停止该页；关闭主窗口会停止所有页，等待后台结束后退出。\n'
                        '保存设置影响本页及以后新建页；其他已打开页保持自己的设置。\n'
                        '登录后自动加载课程并选择首门未完成课程；切换课程后自动显示章节。\n'
                        '可选择起始章节后开始处理，已完成章节自动跳过；停止后可点击继续未完成 / 重试。\n'
                        '需要跨课程执行时，勾选本课程全部完成后继续后续未完成课程。\n'
                        '视频续接记录独立保存在 video_progress.json，更新时请保留。\n'
                        '续播以平台最新进度为准，自动忽略旧版失真记录；未确认完成的结尾位置从头重播。\n'
                        'AI 自动答题：设置 → 题库类型选择 AI 自动答题 → 填写 URL、密钥、模型 → 保存设置。\n'
                        'AI 密钥独立存放于 ai_credentials.json，默认不随打包复制；答案缓存同样在 EXE 外部。\n'
                        'AI 答案取得有效选项后自动提交，不需要人工确认；请求失败会有限重试。\n'
                        '请复制整个发布目录以携带现有数据；目录模式还必须携带 _internal。\n'
                        '单文件 EXE 可单独运行，每次启动会解压依赖；旁边没有配置时使用默认值。\n'
                        f'终端使用：{executable.name} --cli\n'
                        '自定义数据目录：--data-dir "D:\\CxKittyData"\n'
                        '更新程序时，仅替换 EXE（目录模式还要替换 _internal），保留自己的配置和数据。\n'
                        '自检只访问本机模拟 AI 接口，没有调用付费模型、登录账号或提交真实课程任务。\n'
                        '账号会话未加密；勾选携带账号时请勿把发布目录发给其他人。\n', encoding='utf-8')
                    self.checkpoint()
                    self.emit('success', executable=str(executable), path=str(release))
                    return executable
            except Exception as error:
                self.log('构建未完成：' + str(error))
                (release / 'BUILD-INCOMPLETE.txt').write_text(str(error), encoding='utf-8')
                raise
            finally:
                self.log_file = None


def main():
    parser = argparse.ArgumentParser(description='CxKitty GUI 打包后端')
    parser.add_argument('action', choices=('check', 'install', 'build'))
    parser.add_argument('--project', default=str(Path(__file__).resolve().parent))
    parser.add_argument('--output', default='dist')
    parser.add_argument('--name', default='CxKitty')
    parser.add_argument('--mode', choices=('onefile', 'onedir'), default='onefile')
    parser.add_argument('--icon', default='')
    parser.add_argument('--no-config', action='store_true')
    parser.add_argument('--no-banks', action='store_true')
    parser.add_argument('--sessions', action='store_true')
    parser.add_argument('--ai-keys', action='store_true', help='将 AI 密钥复制为独立文件（默认不携带）')
    parser.add_argument('--cancel-file')
    args = parser.parse_args()
    def emit(kind, **fields):
        print(EVENT_PREFIX + json.dumps({'kind': kind, **fields}, ensure_ascii=False), flush=True)
    pipeline = Pipeline(emit, args.cancel_file)
    try:
        if args.action == 'check':
            result = check_environment(args.project)
            emit('log', text=json.dumps(result, ensure_ascii=False, indent=2))
            emit('stage', text='环境检查通过，可以开始打包')
        elif args.action == 'install':
            requirements = Path(args.project).resolve() / 'requirements-build.txt'
            if not requirements.is_file():
                raise ValueError('项目缺少 requirements-build.txt')
            pipeline.stage('正在安装或补齐所选 Python 环境的依赖')
            code = pipeline.run_process([sys.executable, '-m', 'pip', 'install', '-r', str(requirements)], args.project)
            if code:
                raise RuntimeError(f'安装失败，pip 退出码：{code}')
            check_environment(args.project)
            emit('stage', text='依赖已就绪，可以开始打包')
        else:
            options = BuildOptions(Path(args.project), Path(args.output), args.name, args.mode,
                                   args.icon, not args.no_config, not args.no_banks, args.sessions, args.ai_keys)
            pipeline.build(options)
        return 0
    except BuildCancelled:
        emit('cancelled', text='已取消，原项目和以前的发布目录未受影响')
        return 130
    except Exception as error:
        emit('error', text=f'{type(error).__name__}: {error}')
        return 1


if __name__ == '__main__':
    sys.exit(main())
