"""Configure exact existing account targets and enable granted JIT, without links."""
import json
import os
import sqlite3
import sys
from pathlib import Path
from uuid import UUID


def main():
    meta=json.load(sys.stdin)
    assert meta['issuer']=='https://portalwiki.world/sso' and meta['username']=='karlmichael'
    assert str(UUID(meta['subject']))==meta['subject']
    envfile=Path('/etc/domonetka-sso.env')
    env=dict(line.split('=',1) for line in envfile.read_text().splitlines() if '=' in line and not line.startswith('#'))
    assert env['SSO_ENABLED']=='1' and env['SSO_ISSUER']==meta['issuer']
    targets=json.loads(env.get('SSO_LINK_TARGETS','{}'))
    with sqlite3.connect('file:/opt/domonetka/data/domonetka.sqlite?mode=ro',uri=True) as db:
        admin=db.execute("SELECT id,active,local_login_enabled FROM users WHERE username='admin'").fetchone()
        dima=db.execute("SELECT id,active,local_login_enabled FROM users WHERE username='dima'").fetchone()
        assert admin and admin[1:]==(1,1) and dima
        if not targets:
            assert env['SSO_PILOT_LINK_USER_ID']==dima[0] and env['SSO_LINK_TARGET_NAME']=='DemiGO'
            dima_subject=env['SSO_LINK_TARGET_SUBJECT']
            assert str(UUID(dima_subject))==dima_subject
            targets[dima[0]]={'subject':dima_subject,'username':'DemiGO'}
        proposed={'subject':meta['subject'],'username':meta['username']}
        assert admin[0] not in targets or targets[admin[0]]==proposed
        targets[admin[0]]=proposed
        assert len({row['subject'] for row in targets.values()})==len(targets)
        for uid,row in targets.items():
            assert db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone()
            links=db.execute('SELECT issuer,subject,user_id FROM ov_sso_identities WHERE user_id=? OR (issuer=? AND subject=?)',
                             (uid,meta['issuer'],row['subject'])).fetchall()
            assert all(link==(meta['issuer'],row['subject'],uid) for link in links)
    env['SSO_LINK_TARGETS']=json.dumps(targets,separators=(',',':'))
    env['SSO_JIT_ENABLED']='1'
    for name in ('SSO_PILOT_LINK_USER_ID','SSO_LINK_TARGET_SUBJECT','SSO_LINK_TARGET_NAME'):env.pop(name,None)
    temporary=envfile.with_suffix('.env.new')
    fd=os.open(temporary,os.O_CREAT|os.O_TRUNC|os.O_WRONLY,0o600)
    with os.fdopen(fd,'w') as stream:stream.write(''.join(key+'='+value+'\n' for key,value in env.items()))
    os.chmod(temporary,0o600);os.replace(temporary,envfile)
    print('Exact admin/Dima targets configured; granted JIT enabled; account data unchanged')


if __name__=='__main__':
    try:main()
    except Exception:raise SystemExit('Admin/JIT configuration refused; private details suppressed') from None
