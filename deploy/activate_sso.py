"""Root-only preflight and environment generation; never prints private values."""
import os
from pathlib import Path
import re
import sqlite3

import requests
from cryptography.fernet import Fernet
from opsvault_sso.client import ClientConfig, OIDCClient


def main():
    secret_file=Path('/root/domonetka-sso-delivery/client-secret')
    secret=secret_file.read_text(encoding='ascii')
    if not re.fullmatch(r'[A-Za-z0-9_-]{64}',secret):
        raise SystemExit('Invalid delivery format')
    target=Path('/etc/domonetka-sso.env')
    previous={}
    if target.exists():
        previous=dict(line.split('=',1) for line in target.read_text().splitlines() if '=' in line and not line.startswith('#'))
    config=ClientConfig(issuer='https://portalwiki.world/sso',client_id='domonetka',client_secret=secret,
                        redirect_uri='https://dop.afriro.online/auth/opsvault/callback',
                        session_endpoint='https://portalwiki.world/sso/session/',
                        access_endpoint='https://portalwiki.world/sso/access/')
    metadata=OIDCClient(config).metadata()
    if metadata['issuer']!=config.issuer:raise SystemExit('Issuer mismatch')
    response=requests.post(config.access_endpoint,json={'lease_token':'invalid-preflight-lease'},
                           auth=(config.client_id,config.client_secret),timeout=10,allow_redirects=False)
    if response.status_code!=403:raise SystemExit('Client preflight failed (HTTP '+str(response.status_code)+')')
    with sqlite3.connect('file:/opt/domonetka/data/domonetka.sqlite?mode=ro',uri=True) as db:
        pilot=db.execute("SELECT id FROM users WHERE username='konst' AND active=1").fetchone()
        if not pilot:raise SystemExit('Pilot local account not found')
    values=dict(SSO_PILOT_LINK_USER_ID=pilot[0],SSO_ENABLED='1',SSO_JIT_ENABLED=previous.get('SSO_JIT_ENABLED','0'),SSO_ISSUER=config.issuer,SSO_CLIENT_ID=config.client_id,
                SSO_CLIENT_SECRET=secret,SSO_SESSION_ENDPOINT=config.session_endpoint,
                SSO_ACCESS_ENDPOINT=config.access_endpoint,
                SSO_ENCRYPTION_KEY=previous.get('SSO_ENCRYPTION_KEY') or Fernet.generate_key().decode())
    Fernet(values['SSO_ENCRYPTION_KEY'].encode())
    temporary=target.with_suffix('.env.new')
    fd=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
    with os.fdopen(fd,'w',encoding='ascii') as stream:
        stream.write(''.join(key+'='+value+'\n' for key,value in values.items()))
    os.chmod(temporary,0o600)
    os.replace(temporary,target)
    secret_file.unlink()
    print('Discovery and confidential client confirmed; environment stored root-only')


if __name__=='__main__':
    try:main()
    except SystemExit:raise
    except Exception:raise SystemExit('SSO preflight failed; private details suppressed') from None
