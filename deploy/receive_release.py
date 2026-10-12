"""Root-owned SSH forced command. Install via bootstrap_ci.py, never from a release."""
import fcntl
import json
import os
import pwd
import re
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
from pathlib import Path, PurePosixPath

APP = Path('/opt/domonetka')
RUNTIME = {'server.py', 'binom.py', 'binom_settings.py', 'binom_sync.py', 'sso_bridge.py'}
MAX_ARCHIVE = 32 * 1024 * 1024
MAX_EXPANDED = 64 * 1024 * 1024


def run(*args):
    subprocess.run(args, check=True, timeout=90)


def unpack(archive, stage, sha):
    seen = set()
    total = 0
    allowed = {'.html', '.css', '.js', '.svg', '.ico', '.png', '.jpg', '.jpeg', '.webp', '.txt'}
    with tarfile.open(archive, 'r:gz') as package:
        for entry in package:
            path = PurePosixPath(entry.name)
            valid = entry.name in RUNTIME or (len(path.parts) > 1 and path.parts[0] == 'public'
                                               and path.suffix.lower() in allowed)
            if not entry.isfile() or path.is_absolute() or '..' in path.parts or not valid:
                raise ValueError('Invalid release entry: ' + entry.name)
            if entry.name in seen or len(seen) > 2000:
                raise ValueError('Duplicate or oversized release')
            total += entry.size
            if total > MAX_EXPANDED:
                raise ValueError('Expanded release exceeds limit')
            seen.add(entry.name)
            target = stage.joinpath(*path.parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with package.extractfile(entry) as source, target.open('wb') as output:
                shutil.copyfileobj(source, output)
    required = RUNTIME | {'public/index.html', 'public/admin.html', 'public/version.txt',
                          'public/favicon.ico', 'public/favicon.svg'}
    if not required <= seen or (stage / 'public/version.txt').read_text().strip() != sha:
        raise ValueError('Incomplete release or revision mismatch')
    return seen


def healthy(stage):
    for attempt in range(20):
        try:
            for name in ('health', 'version.txt', 'favicon.ico', 'favicon.svg', 'admin'):
                with urllib.request.urlopen('http://127.0.0.1:8097/' + name, timeout=3) as response:
                    body = response.read()
                    if name in ('version.txt', 'favicon.ico', 'favicon.svg'):
                        if body != (stage / 'public' / name).read_bytes():
                            raise ValueError('Unexpected content: ' + name)
                    elif name == 'health' and json.loads(body).get('ok') is not True:
                        raise ValueError('Unhealthy service')
                    elif name == 'admin' and b'/favicon.svg' not in body:
                        raise ValueError('Missing favicon link')
            return
        except Exception:
            if attempt == 19:
                raise
            time.sleep(1)


def deploy(stage, entries, sha):
    owner = pwd.getpwnam('domonetka')
    backup = Path('/root/domonetka-ci-backups') / (time.strftime('%Y%m%dT%H%M%SZ', time.gmtime()) + '-' + sha[:12])
    backup.mkdir(parents=True, mode=0o700)
    backup.parent.chmod(0o700)
    originals = {}
    for name in entries:
        target = APP / name
        if target.is_symlink() or not target.resolve().is_relative_to(APP.resolve()):
            raise ValueError('Unsafe deployment target: ' + name)
        originals[name] = target.exists()
        if target.exists():
            saved = backup / name
            saved.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, saved)
    # Validate code with the production interpreter; SSO/environment remain installed in place.
    runtime = APP / '.venv-sso-0.1.2/bin/python'
    if not runtime.exists():
        raise RuntimeError('Production SSO 0.1.2 runtime missing; review before deployment')
    run(str(runtime), '-m', 'py_compile', *(str(stage / name) for name in sorted(RUNTIME)))
    run(str(runtime), '-c', 'from opsvault_sso.client import OIDCClient; from cryptography.fernet import Fernet')
    run('systemctl', 'stop', 'domonetka')
    database = APP / 'data/domonetka.sqlite'
    has_database_backup = False
    changed = False
    try:
        if database.exists():
            with sqlite3.connect(database) as source, sqlite3.connect(backup / 'database.sqlite') as dest:
                source.backup(dest)
            has_database_backup = True
        for settings in (APP / 'data').glob('binom*.json'):
            saved = backup / 'data' / settings.name
            saved.parent.mkdir(exist_ok=True)
            shutil.copy2(settings, saved)
        for name in sorted(entries):
            target = APP / name
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary = target.with_name(target.name + '.ci-new')
            shutil.copyfile(stage / name, temporary)
            os.chown(temporary, owner.pw_uid, owner.pw_gid)
            temporary.chmod(0o640)
            changed = True
            os.replace(temporary, target)
        run('systemctl', 'start', 'domonetka')
        healthy(stage)
    except BaseException:
        run('systemctl', 'stop', 'domonetka')
        if changed:
            for name, existed in originals.items():
                target = APP / name
                if existed:
                    shutil.copy2(backup / name, target)
                    os.chown(target, owner.pw_uid, owner.pw_gid)
                else:
                    target.unlink(missing_ok=True)
            if has_database_backup:
                for suffix in ('-wal', '-shm'):
                    Path(str(database) + suffix).unlink(missing_ok=True)
                shutil.copy2(backup / 'database.sqlite', database)
                os.chown(database, owner.pw_uid, owner.pw_gid)
                database.chmod(0o640)
            if (backup / 'data').exists():
                for settings in (backup / 'data').glob('binom*.json'):
                    target = APP / 'data' / settings.name
                    shutil.copy2(settings, target)
                    os.chown(target, owner.pw_uid, owner.pw_gid)
        run('systemctl', 'start', 'domonetka')
        print('Deployment failed; previous release restored', file=sys.stderr)
        raise
    print('DEPLOYED', sha, 'BACKUP', backup, flush=True)


def main():
    command = os.environ.get('SSH_ORIGINAL_COMMAND', '')
    if command == 'check':
        run('systemctl', 'is-active', '--quiet', 'domonetka')
        print('DOMONETKA_CI_READY')
        return
    match = re.fullmatch(r'deploy ([0-9a-f]{40})', command)
    if not match:
        raise SystemExit('Only check or deploy <commit SHA> is permitted')
    with open('/run/lock/domonetka-ci.lock', 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with tempfile.TemporaryDirectory(prefix='domonetka-ci-', dir='/opt') as folder:
            stage = Path(folder)
            archive = stage / 'release.tar.gz'
            with archive.open('wb') as output:
                remaining = MAX_ARCHIVE + 1
                while remaining:
                    chunk = sys.stdin.buffer.read(min(1024 * 1024, remaining))
                    if not chunk:
                        break
                    output.write(chunk)
                    remaining -= len(chunk)
                if not remaining:
                    raise ValueError('Archive exceeds limit')
            entries = unpack(archive, stage, match[1])
            deploy(stage, entries, match[1])


if __name__ == '__main__':
    main()
