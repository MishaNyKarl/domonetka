"""Build an explicit runtime-only archive; never includes local data or secrets."""
import subprocess
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME = ('server.py', 'binom.py', 'binom_settings.py', 'binom_sync.py', 'sso_bridge.py')


def main():
    sha = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
    assets = sorted(p for p in (ROOT / 'public').rglob('*') if p.is_file())
    allowed = {'.html', '.css', '.js', '.svg', '.ico', '.png', '.jpg', '.jpeg', '.webp'}
    for path in assets:
        if path.is_symlink() or path.suffix.lower() not in allowed:
            raise ValueError('Unexpected public asset: ' + str(path))
    with tarfile.open(ROOT / 'release.tar.gz', 'w:gz') as archive:
        for name in RUNTIME:
            archive.add(ROOT / name, arcname=name, recursive=False)
        for path in assets:
            archive.add(path, arcname=path.relative_to(ROOT).as_posix(), recursive=False)
        import io
        version = (sha + '\n').encode('ascii')
        info = tarfile.TarInfo('public/version.txt')
        info.size = len(version)
        info.mode = 0o640
        archive.addfile(info, io.BytesIO(version))
    print('Packaged', sha, 'with', len(assets), 'public assets')


if __name__ == '__main__':
    main()
