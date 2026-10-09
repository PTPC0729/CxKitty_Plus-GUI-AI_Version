"""Export public release assets from code, never from a user's runtime directory."""
import argparse
import hashlib
import json
import shutil
import sys
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from app_identity import VERSION
from build_backend import BuildOptions, Pipeline


def source_files(project):
    manifest = project / 'PUBLIC_SOURCE_FILES.txt'
    names = [line.strip() for line in manifest.read_text(encoding='utf-8').splitlines()
             if line.strip() and not line.startswith('#')]
    if len(names) != len(set(names)) or 'PUBLIC_SOURCE_FILES.txt' not in names:
        raise ValueError('公开源码清单无效：不能重复，并且必须包含清单自身')
    files = []
    forbidden = {'config.yml', 'config.yaml', 'ai_credentials.json', 'ai_cache.json', 'video_progress.json'}
    private_dirs = {'session', 'sessions', 'data', 'logs', 'artifacts', 'dist', 'build', 'release',
                    '.git', '.idea', '.venv', '.venv-build', '__pycache__', 'verification', 'packager-verification'}
    for name in names:
        relative = PurePosixPath(name)
        path = project.joinpath(*relative.parts)
        if (relative.is_absolute() or '..' in relative.parts or '\\' in name or ':' in name
                or relative.name in forbidden or set(relative.parts) & private_dirs
                or relative.name.startswith('.env')
                or path.suffix.lower() in {'.exe', '.zip', '.pyc', '.json', '.jsonl', '.db', '.sqlite', '.sqlite3', '.log'}
                or not path.resolve().is_relative_to(project.resolve()) or not path.is_file()):
            raise ValueError('公开源码清单包含无效或私有文件：' + name)
        files.append((name, path))
    return files


def write_zip(destination, entries):
    with zipfile.ZipFile(destination, 'x', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in entries:
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)


def export_release(project, output):
    project, output = Path(project).resolve(), Path(output).resolve()
    if sys.platform != 'win32' or sys.maxsize <= 2**32:
        raise ValueError('此公开版脚本需要在 Windows x64 Python 环境中运行')
    files = source_files(project)
    destination = output / ('v' + VERSION)
    if destination.exists():
        raise ValueError('版本输出目录已经存在，请使用新的 --output 位置，避免覆盖旧版本')
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='cxkitty-public-build-') as temporary:
        pipeline = Pipeline(lambda kind, **fields: print(fields.get('text', ''), flush=True)
                            if kind == 'log' else None)
        executable = pipeline.build(BuildOptions(project, Path(temporary), name='CxKitty', mode='onefile',
            icon=str(project / 'xxt_helper.ico'), copy_config=False, copy_banks=False,
            copy_sessions=False, copy_ai_keys=False))
        verification = json.loads((executable.parent / 'verification' / 'self-check.json').read_text(encoding='utf-8'))
        assert verification['ok'] and verification['frozen']
        assert 'public_empty_initial_state' in verification['checks']
        assert verification['workspaces']['icon_sha256'] == hashlib.sha256((project / 'xxt_helper.ico').read_bytes()).hexdigest()
        destination.mkdir(exist_ok=False)
        binary = destination / f'CxKitty-{VERSION}-windows-x64.exe'
        shutil.copyfile(executable, binary)
        portable = destination / f'CxKitty-{VERSION}-windows-x64.zip'
        # Explicitly select portable contents: no config, logs, mock accounts or build metadata.
        portable_files = [('CxKitty.exe', binary.read_bytes()),
                          ('README.txt', (project / 'docs' / 'RELEASE_README.txt').read_bytes()),
                          ('LICENSE', (project / 'LICENSE').read_bytes()),
                          ('NOTICE.md', (project / 'NOTICE.md').read_bytes()),
                          ('THIRD_PARTY_NOTICES.md', (project / 'THIRD_PARTY_NOTICES.md').read_bytes())]
        portable_files.extend((name, path.read_bytes()) for name, path in files if name.startswith('LICENSES/'))
        write_zip(portable, portable_files)
        source = destination / f'CxKitty-{VERSION}-source.zip'
        write_zip(source, [(f'CxKitty-{VERSION}/{name}', path.read_bytes()) for name, path in files])
        (destination / 'SHA256SUMS.txt').write_text(''.join(
            hashlib.sha256(path.read_bytes()).hexdigest() + '  ' + path.name + '\n'
            for path in (binary, portable, source)), encoding='utf-8')
        shutil.copyfile(project / 'docs' / 'RELEASE_NOTES.md', destination / 'RELEASE_NOTES.md')
        # Public verification contains only generic outcomes, never environment paths or account data.
        (destination / 'VERIFICATION.json').write_text(json.dumps({
            'version': VERSION, 'platform': 'Windows x64', 'ok': True,
            'checks': verification['checks'], 'icon_sha256': verification['workspaces']['icon_sha256'],
            'source_files': len(files), 'configuration_included': False,
            'accounts_included': False, 'credentials_included': False,
            'cache_progress_and_logs_included': False,
        }, ensure_ascii=False, indent=2), encoding='utf-8')
    return destination


def main():
    project = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description='生成不携带用户数据的 GitHub Release 成品')
    parser.add_argument('--output', type=Path, default=project / 'release', help='成品父目录，创建新的版本子目录')
    args = parser.parse_args()
    destination = export_release(project, args.output)
    print('公开版构建、自检和导出成功：' + str(destination), flush=True)


if __name__ == '__main__':
    main()
