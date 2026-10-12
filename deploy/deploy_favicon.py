"""Deploy only favicon assets and head links; SSH password stays in memory."""
import getpass
import hashlib
import io
import posixpath
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]
PUBLIC = '/opt/domonetka/public'
BASE_URL = 'https://dop.afriro.online'
LINKS = (b'<link rel="icon" href="/favicon.ico" sizes="16x16 32x32 48x48">'
         b'<link rel="icon" href="/favicon.svg" type="image/svg+xml" sizes="any">')


def main():
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    # Require a known host instead of accepting an unknown server silently.
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('176.100.37.104', username='root',
                   password=getpass.getpass('SSH password: '),
                   look_for_keys=False, allow_agent=False, timeout=20)
    try:
        with client.open_sftp() as sftp:
            stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            backup = '/root/domonetka-backup-favicon-' + stamp
            sftp.mkdir(backup, mode=0o700)
            updates = {}
            for entry in sftp.listdir(PUBLIC):
                if not entry.endswith('.html'):
                    continue
                remote = posixpath.join(PUBLIC, entry)
                with sftp.open(remote, 'rb') as source:
                    original = source.read()
                if b'/favicon.svg' in original and b'/favicon.ico' in original:
                    continue
                if b'rel="icon"' in original or b"rel='icon'" in original:
                    raise RuntimeError('Existing favicon needs review: ' + entry)
                anchor = b'</head>' if b'</head>' in original else b'</title>'
                if anchor not in original:
                    raise RuntimeError('No head/title found: ' + entry)
                replacement = LINKS + anchor if anchor == b'</head>' else anchor + LINKS
                updates[entry] = original.replace(anchor, replacement, 1)
            for entry in ('favicon.svg', 'favicon.ico'):
                updates[entry] = (ROOT / 'public' / entry).read_bytes()
            # Back up all existing targets before replacing any of them.
            for entry in updates:
                remote = posixpath.join(PUBLIC, entry)
                try:
                    attrs = sftp.stat(remote)
                except FileNotFoundError:
                    continue
                with sftp.open(remote, 'rb') as source:
                    original = source.read()
                sftp.putfo(io.BytesIO(original), posixpath.join(backup, entry))
                sftp.chmod(posixpath.join(backup, entry), attrs.st_mode & 0o777)
            for entry, content in sorted(updates.items(), key=lambda item: item[0].endswith('.html')):
                remote = posixpath.join(PUBLIC, entry)
                attrs = sftp.stat(posixpath.join(PUBLIC, 'index.html'))
                try:
                    attrs = sftp.stat(remote)
                except FileNotFoundError:
                    pass
                staged = remote + '.favicon-' + stamp
                sftp.putfo(io.BytesIO(content), staged)
                sftp.chmod(staged, attrs.st_mode & 0o777)
                sftp.chown(staged, attrs.st_uid, attrs.st_gid)
                sftp.posix_rename(staged, remote)
                print('Updated:', entry)
            print('Backup:', backup)
    finally:
        client.close()
    for entry in ('favicon.svg', 'favicon.ico'):
        with urllib.request.urlopen(BASE_URL + '/' + entry, timeout=20) as response:
            actual = response.read()
            assert response.status == 200
        expected = (ROOT / 'public' / entry).read_bytes()
        assert hashlib.sha256(actual).digest() == hashlib.sha256(expected).digest(), entry
    for path in ('/', '/admin', '/s/main'):
        with urllib.request.urlopen(BASE_URL + path, timeout=20) as response:
            assert LINKS in response.read(), path
    print('Verified: public SVG, ICO, home page, admin and stream. No restart required.')


if __name__ == '__main__':
    main()
