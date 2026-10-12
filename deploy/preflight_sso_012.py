"""Root-only non-mutating probes. Never outputs environment or response bodies."""
from pathlib import Path
import requests
from opsvault_sso.client import ClientConfig, OIDCClient


def main():
    env=dict(line.split('=',1) for line in Path('/etc/domonetka-sso.env').read_text().splitlines()
             if '=' in line and not line.startswith('#'))
    config=ClientConfig(issuer=env['SSO_ISSUER'],client_id=env['SSO_CLIENT_ID'],client_secret=env['SSO_CLIENT_SECRET'],
                        redirect_uri='https://dop.afriro.online/auth/opsvault/callback',
                        session_endpoint=env['SSO_SESSION_ENDPOINT'],access_endpoint=env['SSO_ACCESS_ENDPOINT'])
    assert OIDCClient(config).metadata()['issuer']==config.issuer
    for path in ('/credentials/','/credentials/read/','/local-access/'):
        payload={'lease_token':'invalid-preflight-lease','subject':'00000000-0000-4000-8000-000000000000',
                 'local_user_id':'0'*32,'username':'preflight-invalid','password':'preflight-not-stored'}
        reply=requests.post(config.issuer+path,json=payload,auth=(config.client_id,config.client_secret),
                            timeout=10,allow_redirects=False)
        if reply.status_code!=403:raise SystemExit('Endpoint not ready: '+path+' HTTP '+str(reply.status_code))
        print(path,'authenticated invalid request denied')
    print('SSO 0.1.2 provider preflight OK; no credentials saved')


if __name__=='__main__':
    try:main()
    except SystemExit:raise
    except Exception:raise SystemExit('Provider preflight failed; private details suppressed') from None
