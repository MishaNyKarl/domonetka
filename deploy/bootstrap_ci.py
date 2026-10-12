"""One-time server setup: password is entered interactively and never saved."""
import getpass
import subprocess
from pathlib import Path

import paramiko

ROOT = Path(__file__).resolve().parents[1]


def main():
    client = paramiko.SSHClient()
    client.load_system_host_keys()
    client.set_missing_host_key_policy(paramiko.RejectPolicy())
    client.connect('176.100.37.104', username='root', password=getpass.getpass('SSH password: '),
                   look_for_keys=False, allow_agent=False, timeout=20)
    public_key = (ROOT / '.ci-private/deploy.pub').read_text().strip()
    if not public_key.startswith('ssh-ed25519 '):
        raise ValueError('Expected generated deployment public key')
    try:
        # Check the target before installing keys or deployment files.
        _, stdout, stderr = client.exec_command(
            'test -d /opt/domonetka/public && '
            'test -x /opt/domonetka/.venv-sso-0.1.2/bin/python && '
            'systemctl is-active --quiet domonetka', timeout=30)
        error = stderr.read().decode()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError('Target is not the running Domonetka server. ' + error)
        with client.open_sftp() as sftp:
            # Root-owned forced command cannot be replaced by application releases.
            staged = '/root/domonetka-ci-receiver.new'
            sftp.put(str(ROOT / 'deploy/receive_release.py'), staged)
        _, stdout, stderr = client.exec_command(
            'set -eu; install -d -m 755 /usr/local/lib/domonetka; '
            'install -o root -g root -m 755 /root/domonetka-ci-receiver.new '
            '/usr/local/lib/domonetka/receive_release.py; '
            'install -d -m 700 /root/.ssh; touch /root/.ssh/authorized_keys; '
            'chmod 600 /root/.ssh/authorized_keys; '
            'systemctl is-active --quiet domonetka', timeout=30)
        error = stderr.read().decode()
        if stdout.channel.recv_exit_status() != 0:
            raise RuntimeError(error)
        with client.open_sftp() as sftp:
            with sftp.open('/root/.ssh/authorized_keys', 'rb') as source:
                original = source.read()
            if public_key.split()[1].encode() not in original:
                line = ('restrict,command="/usr/bin/python3 /usr/local/lib/domonetka/receive_release.py" '
                        + public_key + '\n').encode()
                with sftp.open('/root/.ssh/authorized_keys', 'ab') as target:
                    if original and not original.endswith(b'\n'):
                        target.write(b'\n')
                    target.write(line)
        print('Installed restricted deployment key and receiver')
    finally:
        client.close()
    result = subprocess.run(['ssh', '-i', str(ROOT / '.ci-private/deploy'),
                             '-o', 'IdentitiesOnly=yes', '-o', 'BatchMode=yes',
                             '-o', 'StrictHostKeyChecking=yes', 'root@176.100.37.104', 'check'],
                            check=True, capture_output=True, text=True, timeout=30)
    if result.stdout.strip() != 'DOMONETKA_CI_READY':
        raise RuntimeError('Deployment key verification failed')
    subprocess.run(['gh', 'variable', 'set', 'DEPLOY_ENABLED', '--body', 'true',
                    '--repo', 'MishaNyKarl/domonetka'], check=True)
    subprocess.run(['gh', 'workflow', 'run', 'ci-cd.yml', '--ref', 'main',
                    '--repo', 'MishaNyKarl/domonetka'], check=True)
    print('CI/CD enabled. First deployment dispatched (including favicon).')


if __name__ == '__main__':
    main()
