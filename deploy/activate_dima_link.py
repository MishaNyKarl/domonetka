"""Install the approved immutable linking target. No private output."""
from pathlib import Path
import os
import json
import sqlite3
import sys
from uuid import UUID


def main():
    target=json.load(sys.stdin)
    assert target['issuer']=='https://portalwiki.world/sso'
    assert target['username']=='DemiGO'
    assert str(UUID(target['subject']))==target['subject']
    with sqlite3.connect('file:/opt/domonetka/data/domonetka.sqlite?mode=ro',uri=True) as db:
        matches=db.execute("SELECT id,active,local_login_enabled FROM users WHERE username='dima'").fetchall()
        assert len(matches)==1 and matches[0][1:]==(1,1)
        uid=matches[0][0]
        assert not db.execute('SELECT 1 FROM ov_sso_identities WHERE user_id=? OR (issuer=? AND subject=?)',
                              (uid,target['issuer'],target['subject'])).fetchone()
    envfile=Path('/etc/domonetka-sso.env')
    env=dict(line.split('=',1) for line in envfile.read_text().splitlines()
             if '=' in line and not line.startswith('#'))
    assert env['SSO_ISSUER']==target['issuer'] and env['SSO_ENABLED']=='1'
    env.update(SSO_PILOT_LINK_USER_ID=uid,SSO_LINK_TARGET_SUBJECT=target['subject'],
               SSO_LINK_TARGET_NAME=target['username'])
    temporary=envfile.with_suffix('.env.new')
    fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(''.join(key+'='+value+'\n' for key,value in env.items()))
    os.chmod(temporary,0o600)
    os.replace(temporary,envfile)
    print('Target dima -> DemiGO configured; existing links, passwords and JIT setting preserved')


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Target configuration refused; private details suppressed') from None
