"""Verify release identity and favicon links over public HTTPS."""
import sys
import urllib.request

BASE = 'https://dop.afriro.online'


def main():
    sha = sys.argv[1]
    for path in ('/health', '/version.txt', '/favicon.svg', '/favicon.ico', '/', '/admin'):
        request = urllib.request.Request(BASE + path + '?release=' + sha,
                                         headers={'Cache-Control': 'no-cache',
                                                  'User-Agent': 'Domonetka-CI/1.0 (+https://github.com/MishaNyKarl/domonetka)'})
        with urllib.request.urlopen(request, timeout=20) as response:
            assert response.status == 200, path
            body = response.read()
            if path == '/version.txt':
                assert body.decode().strip() == sha, 'Server release mismatch'
            elif path in ('/', '/admin'):
                assert b'/favicon.svg' in body and b'/favicon.ico' in body, path
            elif path == '/favicon.ico':
                assert body[:4] == b'\x00\x00\x01\x00', path
        print('Verified:', path)


if __name__ == '__main__':
    main()
