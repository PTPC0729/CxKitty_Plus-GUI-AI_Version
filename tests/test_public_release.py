import hashlib
import json
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from app_identity import VERSION
from build_public_release import export_release, source_files, write_zip


class PublicReleaseTests(unittest.TestCase):
    def test_source_manifest_rejects_runtime_data_and_path_escape(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for forbidden in ('config.yml', 'ai_credentials.json', 'session/user.json',
                              '../outside.py', 'C:/private.py', '.env', 'logs/run.json', 'app.exe'):
                (root / 'PUBLIC_SOURCE_FILES.txt').write_text(
                    'PUBLIC_SOURCE_FILES.txt\n' + forbidden + '\n', encoding='utf-8')
                with self.subTest(path=forbidden), self.assertRaises(ValueError):
                    source_files(root)

    def test_archives_have_no_local_timestamps_or_absolute_paths(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'source.zip'
            write_zip(path, [('CxKitty/main.py', b'print("example")')])
            with zipfile.ZipFile(path) as archive:
                info = archive.infolist()[0]
                self.assertEqual(info.filename, 'CxKitty/main.py')
                self.assertEqual(info.date_time, (1980, 1, 1, 0, 0, 0))
                self.assertEqual(info.extra, b'')

    @unittest.skipUnless(sys.platform == 'win32', 'Windows release export')
    def test_export_never_copies_private_build_sidecars(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            project, build = root / 'source', root / 'build'
            project.mkdir()
            (project / 'docs').mkdir()
            (project / 'LICENSES').mkdir()
            (build / 'verification').mkdir(parents=True)
            for name in ('LICENSE', 'NOTICE.md', 'THIRD_PARTY_NOTICES.md', 'xxt_helper.ico',
                         'docs/RELEASE_README.txt', 'docs/RELEASE_NOTES.md', 'LICENSES/example.txt'):
                (project / name).write_bytes(b'public example')
            names = sorted(p.relative_to(project).as_posix() for p in project.rglob('*') if p.is_file())
            (project / 'PUBLIC_SOURCE_FILES.txt').write_text('\n'.join(names + ['PUBLIC_SOURCE_FILES.txt']), encoding='utf-8')
            executable = build / 'CxKitty.exe'
            executable.write_bytes(b'offline-executable-fixture')
            for name in ('config.yml', 'ai_credentials.json', 'build-info.json', 'build-log.txt', 'video_progress.json'):
                (build / name).write_text('private-fixture-must-not-ship', encoding='utf-8')
            (project / 'config.yml').write_text('private-fixture-must-not-ship', encoding='utf-8')
            (build / 'verification' / 'self-check.json').write_text(json.dumps({
                'ok': True, 'frozen': True, 'checks': ['public_empty_initial_state'],
                'workspaces': {'icon_sha256': hashlib.sha256(b'public example').hexdigest()},
            }), encoding='utf-8')
            with patch('build_public_release.Pipeline.build', return_value=executable) as compile_exe:
                release = export_release(project, root / 'release')
            options = compile_exe.call_args.args[0]
            self.assertFalse(any((options.copy_config, options.copy_banks, options.copy_sessions, options.copy_ai_keys)))
            for path in release.iterdir():
                self.assertNotIn(b'private-fixture-must-not-ship', path.read_bytes())
                if path.suffix == '.zip':
                    with zipfile.ZipFile(path) as archive:
                        for name in archive.namelist():
                            self.assertNotIn(b'private-fixture-must-not-ship', archive.read(name))
            with zipfile.ZipFile(release / f'CxKitty-{VERSION}-windows-x64.zip') as archive:
                self.assertNotIn('config.yml', archive.namelist())
                self.assertIn('LICENSE', archive.namelist())
            with self.assertRaises(ValueError):
                export_release(project, root / 'release')
