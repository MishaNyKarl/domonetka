"""Exercise archive validation and failed-release rollback without a real server."""
import importlib.util
import io
import os
import sqlite3
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


@unittest.skipUnless(os.name == 'posix', 'Deployment receiver runs on Linux')
class DeploymentTest(unittest.TestCase):
    def setUp(self):
        spec = importlib.util.spec_from_file_location('receiver', Path(__file__).resolve().parents[1] / 'deploy/receive_release.py')
        self.receiver = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.receiver)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def archive(self, name, content=b'x', link=False):
        path = self.root / 'archive.tar.gz'
        with tarfile.open(path, 'w:gz') as archive:
            entry = tarfile.TarInfo(name)
            if link:
                entry.type = tarfile.SYMTYPE
                entry.linkname = '/etc/passwd'
                archive.addfile(entry)
            else:
                entry.size = len(content)
                archive.addfile(entry, io.BytesIO(content))
        return path

    def test_rejects_paths_links_and_non_runtime_files(self):
        for name, link in [('../server.py', False), ('/server.py', False),
                           ('data/domonetka.sqlite', False), ('public/key.pem', False),
                           ('public/logo.svg', True)]:
            with self.subTest(name=name), self.assertRaises(ValueError):
                self.receiver.unpack(self.archive(name, link=link), self.root / 'stage', 'a' * 40)

    def test_rejects_incomplete_release(self):
        with self.assertRaises(ValueError):
            self.receiver.unpack(self.archive('server.py'), self.root / 'stage', 'a' * 40)

    def test_health_failure_restores_code_and_database(self):
        app = self.root / 'app'
        stage = self.root / 'stage'
        app.mkdir(); stage.mkdir()
        (app / 'data').mkdir()
        (app / 'server.py').write_text('old release')
        (stage / 'server.py').write_text('new release')
        (stage / 'public').mkdir()
        (stage / 'public/version.txt').write_text('a' * 40)
        runtime = app / '.venv-sso-0.1.2/bin/python'
        runtime.parent.mkdir(parents=True); runtime.touch()
        database = app / 'data/domonetka.sqlite'
        with sqlite3.connect(database) as db:
            db.execute('CREATE TABLE example (value TEXT)')
            db.execute("INSERT INTO example VALUES ('before')")
        calls = []
        original_path = self.receiver.Path
        def path(value):
            return self.root / 'backups' if value == '/root/domonetka-ci-backups' else original_path(value)
        def fail_health(_):
            with sqlite3.connect(database) as db:
                db.execute("UPDATE example SET value='after'")
            raise ValueError('Simulated bad release')
        owner = SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid())
        with patch.object(self.receiver, 'APP', app), patch.object(self.receiver, 'Path', path), \
             patch.object(self.receiver.pwd, 'getpwnam', return_value=owner), \
             patch.object(self.receiver, 'run', side_effect=lambda *args: calls.append(args)), \
             patch.object(self.receiver, 'healthy', side_effect=fail_health):
            with self.assertRaisesRegex(ValueError, 'Simulated bad release'):
                self.receiver.deploy(stage, {'server.py', 'public/version.txt'}, 'a' * 40)
        self.assertEqual((app / 'server.py').read_text(), 'old release')
        self.assertFalse((app / 'public/version.txt').exists())
        with sqlite3.connect(database) as db:
            self.assertEqual(db.execute('SELECT value FROM example').fetchone()[0], 'before')
        self.assertEqual(calls[-1], ('systemctl', 'start', 'domonetka'))


if __name__ == '__main__':
    unittest.main()
