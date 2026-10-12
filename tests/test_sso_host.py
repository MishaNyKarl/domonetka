"""Host integration checks, isolated SQLite and a fake policy endpoint."""
import json
import secrets
import tempfile
import threading
import time
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import server

try:
    from cryptography.fernet import Fernet
    from opsvault_sso.client import AccessDecision, Identity, LoginAttempt, LoginResult
    from opsvault_sso.errors import AccessDenied, ProviderUnavailable
    AVAILABLE = True
except ImportError:
    AVAILABLE = False


@unittest.skipUnless(AVAILABLE, 'Run host SSO checks with the SSO-enabled venv')
class SSOHostTest(unittest.TestCase):
    def setUp(self):
        self.previous=(server.DATA,server.DB,server.SSO,server.PUBLIC_ORIGIN,server.SECURE)
        self.temp=tempfile.TemporaryDirectory()
        server.DATA=Path(self.temp.name);server.DB=server.DATA/'test.sqlite'
        server.PUBLIC_ORIGIN='https://app.example';server.SECURE='; Secure';server.SSO=None
        server.LOGIN_ATTEMPTS.clear();server.init()
        env=dict(SSO_ISSUER='https://provider.example/sso',SSO_CLIENT_ID='domonetka',
                 SSO_CLIENT_SECRET='test-only-secret',SSO_ENCRYPTION_KEY=Fernet.generate_key().decode(),
                 SSO_SESSION_ENDPOINT='https://provider.example/sso/session/',SSO_ACCESS_ENDPOINT='https://provider.example/sso/access/')
        self.bridge=server.OpsVaultBridge(server.connect,server.PUBLIC_ORIGIN,server.SECURE,server.password_matches,env,password_hasher=server.password_hash)
        self.role='user';self.provider_fail=False;self.deny=False;self.background_deny=False;self.checks=0
        self.saved_credentials=[];self.save_fail=False
        self.result=LoginResult(Identity(env['SSO_ISSUER'],'subject-one','konst','Konst'),
                                'user','test-lease',time.time()+3600,'sid-one','/admin',None)
        outer=self
        class Client:
            config=outer.bridge.config
            def begin(self,path,link_user_id=None):
                return 'https://provider.example/authorize',LoginAttempt(secrets.token_urlsafe(32),'nonce','verifier',time.time()+300,path,link_user_id)
            def complete(self,url,attempt):return replace(outer.result,link_user_id=attempt.link_user_id)
            def check_access(self,lease):
                outer.checks+=1
                if outer.provider_fail:raise ProviderUnavailable('unavailable')
                if outer.deny:raise AccessDenied('denied')
                return AccessDecision(outer.role,time.time()+60)
            def check_background(self,subject):
                if outer.provider_fail:raise ProviderUnavailable('unavailable')
                if outer.background_deny:raise AccessDenied('denied')
            def check_local_access(self,subject,uid):
                return self.check_access('local-policy')
            def save_credentials(self,lease,subject,uid,username,password):
                if outer.save_fail:raise ProviderUnavailable('unavailable')
                if outer.deny:raise AccessDenied('denied')
                outer.saved_credentials.append((subject,uid,username,password))
                return {'saved':True}
            def get_credentials(self,lease,subject):
                if outer.provider_fail:raise ProviderUnavailable('unavailable')
                if outer.deny:raise AccessDenied('denied')
                found=next((r for r in outer.saved_credentials if r[0]==subject),None)
                return SimpleNamespace(local_user_id=found[1],username=found[2],password=found[3]) if found else None
            def logout_all(self,lease):
                if outer.provider_fail:raise ProviderUnavailable('unavailable')
        self.bridge.client_type=lambda config:Client();server.SSO=self.bridge
        self.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        self.url='http://127.0.0.1:'+str(self.http.server_port)
        threading.Thread(target=self.http.serve_forever,daemon=True).start()

    def tearDown(self):
        self.http.shutdown();self.http.server_close()
        server.DATA,server.DB,server.SSO,server.PUBLIC_ORIGIN,server.SECURE=self.previous
        self.temp.cleanup()

    def request(self,path,data=None,cookie=''):
        headers={'Cookie':cookie,'Origin':server.PUBLIC_ORIGIN}
        if data is not None:headers['Content-Type']='application/json'
        request=urllib.request.Request(self.url+path,json.dumps(data).encode() if data is not None else None,headers)
        # Do not follow the OIDC redirect to a real external endpoint.
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args):return None
        try:response=urllib.request.build_opener(NoRedirect).open(request)
        except urllib.error.HTTPError as exc:response=exc
        raw=response.read()
        return response.code,json.loads(raw) if raw else {},response.headers

    def local(self):
        status,_,headers=self.request('/api/login',{'username':'admin','password':server.PASSWORD})
        self.assertEqual(status,200)
        cookie=headers['Set-Cookie'].split(';')[0]
        _,payload,_=self.request('/api/admin',cookie=cookie)
        return cookie,payload['me']['csrf']

    def callback(self,link_uid=None,local_cookie=''):
        attempt=LoginAttempt(secrets.token_urlsafe(32),'nonce','verifier',time.time()+300,'/admin',link_uid)
        binding=self.bridge.store.save_attempt(attempt)
        if link_uid:
            with server.connect() as db:
                db.execute('INSERT INTO ov_dm_link_attempts VALUES(?,?)',(self.bridge.digest(attempt.state),self.bridge.digest(local_cookie.split('=',1)[1])))
                db.execute('INSERT INTO ov_dm_link_secrets VALUES(?,?,?)',(self.bridge.digest(attempt.state),self.bridge.store.cipher.encrypt(server.PASSWORD.encode()),time.time()+300))
        return self.request('/auth/opsvault/callback?state='+attempt.state+'&code=test',cookie=local_cookie+'; __Host-ov_sso_login='+binding)

    def federated(self):
        status,_,headers=self.callback();self.assertEqual(status,303)
        return headers['Set-Cookie'].split(';')[0]

    def test_jit_reuses_subject_and_creates_vault_password_without_autolink(self):
        first=self.federated();second=self.federated()
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],2)
            row=db.execute("SELECT * FROM users WHERE username LIKE 'ov_%'").fetchone()
            self.assertEqual(row['local_login_enabled'],1)
            self.assertTrue(server.password_matches(self.saved_credentials[0][3],row['password_hash']))
            username=row['username']
        self.assertEqual(self.request('/api/login',{'username':username,'password':'test-only-password'})[0],401)
        self.assertEqual(self.request('/api/admin',cookie=first)[0],200)
        self.assertEqual(self.request('/api/admin',cookie=second)[0],200)

    def test_jit_recovers_same_vault_identity_after_local_rollback(self):
        original=self.bridge.password_hasher
        self.bridge.password_hasher=lambda password:(_ for _ in ()).throw(ValueError('temporary failure'))
        self.assertEqual(self.callback()[0],400)
        self.assertEqual(len(self.saved_credentials),1)
        uid=self.saved_credentials[0][1]
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)
        self.bridge.password_hasher=original
        cookie=self.federated()
        self.assertEqual(self.request('/api/admin',cookie=cookie)[1]['me']['id'],uid)
        self.assertEqual(len(self.saved_credentials),1)

    def test_pilot_gate_prevents_duplicate_account_before_linking(self):
        self.bridge.jit_enabled=False
        self.assertEqual(self.callback()[0],403)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)

    def test_role_is_central_and_post_forces_fresh_check(self):
        cookie=self.federated()
        self.assertEqual(self.request('/api/admin',cookie=cookie)[1]['me']['role'],'user')
        before=self.checks;self.role='admin'
        status,_,_=self.request('/api/user',dict(username='pilot-user',name='Pilot',role='user',active=True,password='pilot-long-password'),cookie)
        self.assertEqual(status,200);self.assertGreater(self.checks,before)
        self.role='user'
        self.assertEqual(self.request('/api/user',dict(username='blocked',name='Blocked',password='pilot-long-password'),cookie)[0],403)

    def test_revocation_and_provider_failure_cannot_fall_back_to_local_role(self):
        cookie=self.federated();self.provider_fail=True
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],503)
        self.provider_fail=False;self.deny=True
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],401)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)

    def test_expired_cache_requires_central_recheck(self):
        cookie=self.federated();self.request('/api/admin',cookie=cookie)
        self.provider_fail=True
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],200)
        with server.connect() as db:db.execute('UPDATE ov_sso_sessions SET checked_until=0')
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],503)

    def test_link_preserves_ownership_and_password_but_revokes_sessions(self):
        cookie,csrf=self.local()
        with server.connect() as db:before=[tuple(r) for r in db.execute('SELECT id,owner_id FROM streams')]
        self.assertEqual(self.request('/api/auth/link/start',{'password':server.PASSWORD,'csrf':'wrong','save_credentials':True},cookie)[0],403)
        self.assertEqual(self.request('/api/auth/link/start',{'password':'wrong-password','csrf':csrf,'save_credentials':True},cookie)[0],403)
        self.assertEqual(self.callback('admin',cookie)[0],303)
        status,body,_=self.request('/api/auth/link/status',cookie=cookie)
        self.assertEqual(status,200);self.assertTrue(body['pending'])
        self.assertEqual(self.request('/api/auth/link/confirm',{'csrf':csrf},cookie)[0],200)
        with server.connect() as db:
            self.assertEqual([tuple(r) for r in db.execute('SELECT id,owner_id FROM streams')],before)
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],0)
            user=db.execute("SELECT * FROM users WHERE id='admin'").fetchone()
            self.assertEqual(user['local_login_enabled'],1)
            self.assertTrue(server.password_matches(server.PASSWORD,user['password_hash']))
            self.assertEqual(db.execute('SELECT user_id FROM ov_sso_identities').fetchone()[0],'admin')
        self.assertEqual(self.saved_credentials[0][1:3],('admin','admin'))
        self.assertEqual(self.request('/api/login',{'password':server.PASSWORD})[0],200)

    def link_existing(self):
        cookie,csrf=self.local();self.assertEqual(self.callback('admin',cookie)[0],303)
        self.assertEqual(self.request('/api/auth/link/confirm',{'csrf':csrf},cookie)[0],200)

    def test_credentials_failure_keeps_old_account_and_session_unlinked(self):
        cookie,csrf=self.local();self.callback('admin',cookie);self.save_fail=True
        self.assertEqual(self.request('/api/auth/link/confirm',{'csrf':csrf},cookie)[0],503)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM sessions').fetchone()[0],1)
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],200)

    def test_link_rechecks_password_before_credentials_publication(self):
        cookie,csrf=self.local();self.callback('admin',cookie)
        with server.connect() as db:db.execute("UPDATE users SET password_hash=? WHERE id='admin'",(server.password_hash('changed-password'),))
        self.assertEqual(self.request('/api/auth/link/confirm',{'csrf':csrf},cookie)[0],403)
        self.assertFalse(self.saved_credentials)

    def test_old_link_page_cannot_start_without_vault_consent(self):
        cookie,csrf=self.local()
        self.assertEqual(self.request('/api/auth/link/start',{'password':server.PASSWORD,'csrf':csrf},cookie)[0],400)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ov_dm_link_secrets').fetchone()[0],0)
        self.assertEqual(self.request('/api/auth/link/start',{'password':server.PASSWORD,'csrf':csrf,'save_credentials':True},cookie)[0],200)
        with server.connect() as db:
            row=db.execute('SELECT payload,expires FROM ov_dm_link_secrets').fetchone()
            self.assertNotIn(server.PASSWORD.encode(),row['payload'])
            self.assertLessEqual(row['expires'],time.time()+300)

    def test_local_linked_login_checks_policy_and_writes_force_fresh(self):
        self.link_existing();cookie,csrf=self.local()
        me=self.request('/api/admin',cookie=cookie)[1]['me']
        self.assertEqual(me['role'],'user');self.assertTrue(me['sso_local'])
        self.provider_fail=True;self.assertEqual(self.request('/api/admin',cookie=cookie)[0],200)
        self.assertEqual(self.request('/api/user',{'name':'blocked'},cookie)[0],503)
        with server.connect() as db:db.execute('UPDATE ov_dm_local_sessions SET checked_until=0')
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],503)
        self.assertEqual(self.request('/api/login',{'password':server.PASSWORD})[0],503)
        self.provider_fail=False;self.deny=True
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],401)
        self.assertEqual(self.request('/api/login',{'password':server.PASSWORD})[0],403)

    def test_disabled_sso_blocks_local_linked_login_session_and_background(self):
        self.link_existing();cookie,csrf=self.local();server.SSO=None
        self.assertEqual(self.request('/api/login',{'password':server.PASSWORD})[0],503)
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],503)
        with self.assertRaises(ValueError):server.check_sso_background('admin')

    def test_linked_local_password_login_cannot_restore_local_role_or_edit_credentials(self):
        self.link_existing();cookie,csrf=self.local()
        self.assertEqual(self.request('/api/user',{'id':'admin','username':'admin','name':'Admin','role':'admin','active':True},cookie)[0],403)
        self.role='admin'
        status,body,_=self.request('/api/user',{'id':'admin','username':'changed','name':'Admin','role':'admin','active':True},cookie)
        self.assertEqual(status,400)
        self.assertEqual(self.request('/api/logout',{'csrf':csrf},cookie)[0],200)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ov_dm_local_sessions').fetchone()[0],0)

    def test_callback_rejects_duplicate_and_cross_browser_state(self):
        attempt=LoginAttempt('one-state','nonce','verifier',time.time()+300)
        binding=self.bridge.store.save_attempt(attempt)
        self.assertEqual(self.request('/auth/opsvault/callback?state=one-state&state=two&code=test',cookie='__Host-ov_sso_login='+binding)[0],400)
        self.assertEqual(self.request('/auth/opsvault/callback?state=one-state&code=test',cookie='__Host-ov_sso_login=wrong')[0],403)
        self.assertEqual(self.request('/auth/opsvault/callback?state=one-state&code=test',cookie='__Host-ov_sso_login='+binding)[0],303)
        self.assertEqual(self.request('/auth/opsvault/callback?state=one-state&code=test',cookie='__Host-ov_sso_login='+binding)[0],403)

    def test_global_logout_failure_still_removes_local_session(self):
        cookie=self.federated();_,payload,_=self.request('/api/admin',cookie=cookie)
        self.provider_fail=True
        self.assertEqual(self.request('/api/auth/logout-all',{'csrf':payload['me']['csrf']},cookie)[0],503)
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],401)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_sessions').fetchone()[0],0)

    def test_pilot_link_gate_rejects_wrong_local_account(self):
        cookie,csrf=self.local();self.bridge.pilot_link_user='another-account'
        self.assertEqual(self.request('/api/auth/link/start',{'password':server.PASSWORD,'csrf':csrf},cookie)[0],403)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)

    def test_configured_opsvault_target_rejects_another_account_at_callback(self):
        cookie,csrf=self.local()
        self.bridge.link_target_subject='expected-subject';self.bridge.link_target_name='ExpectedUser'
        status,body,_=self.callback('admin',cookie)
        self.assertEqual(status,403);self.assertIn('ExpectedUser',body['error'])
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM ov_dm_link_pending').fetchone()[0],0)
            self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)
        self.assertFalse(self.saved_credentials)
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],200)

    def test_changed_target_rejects_old_pending_confirmation_before_publication(self):
        cookie,csrf=self.local()
        self.bridge.link_target_subject=self.result.identity.subject
        self.assertEqual(self.callback('admin',cookie)[0],303)
        self.bridge.link_target_subject='another-subject'
        self.assertEqual(self.request('/api/auth/link/confirm',{'csrf':csrf},cookie)[0],403)
        self.assertFalse(self.saved_credentials)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)

    def test_expected_target_can_link_without_changing_local_password(self):
        self.bridge.link_target_subject=self.result.identity.subject
        self.bridge.link_target_name=self.result.identity.username
        self.link_existing()
        cookie,csrf=self.local()
        self.assertEqual(self.request('/api/admin',cookie=cookie)[0],200)

    def test_reserved_existing_account_cannot_jit_create_duplicate(self):
        self.bridge.link_targets={'admin':{'subject':self.result.identity.subject,'username':'karlmichael'}}
        self.assertTrue(self.bridge.jit_enabled)
        status,body,_=self.callback()
        self.assertEqual(status,403);self.assertIn('уже есть',body['error'])
        self.assertFalse(self.saved_credentials)
        with server.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)
            self.assertEqual(db.execute('SELECT count(*) FROM ov_sso_identities').fetchone()[0],0)
        self.link_existing()
        oidc=self.federated()
        self.assertEqual(self.request('/api/admin',cookie=oidc)[1]['me']['id'],'admin')
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)

    def test_account_specific_map_rejects_swapped_opsvault_account(self):
        cookie,csrf=self.local()
        self.bridge.link_targets={'admin':{'subject':'admin-subject','username':'karlmichael'},
                                  'dima-user-id':{'subject':self.result.identity.subject,'username':'DemiGO'}}
        me=self.request('/api/admin',cookie=cookie)[1]['me']
        self.assertTrue(me['sso_can_link']);self.assertEqual(me['sso_link_target'],'karlmichael')
        self.assertEqual(self.callback('admin',cookie)[0],403)
        self.assertFalse(self.saved_credentials)

    def test_unreserved_new_identity_can_jit_with_reserved_accounts_configured(self):
        self.bridge.link_targets={'admin':{'subject':'admin-subject','username':'karlmichael'}}
        cookie=self.federated()
        me=self.request('/api/admin',cookie=cookie)[1]['me']
        self.assertNotEqual(me['id'],'admin');self.assertEqual(me['role'],'user')
        self.assertEqual(len(self.saved_credentials),1)
        self.assertFalse(me['sso_can_link'])

    def test_jit_denied_by_provider_does_not_create_password_or_account(self):
        self.deny=True
        self.assertEqual(self.callback()[0],403)
        self.assertFalse(self.saved_credentials)
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM users').fetchone()[0],1)

    def test_background_requires_explicit_central_permission(self):
        cookie=self.federated();_,payload,_=self.request('/api/admin',cookie=cookie)
        uid=payload['me']['id'];self.bridge.check_background(uid)
        self.background_deny=True
        with self.assertRaises(ValueError):self.bridge.check_background(uid)
        self.background_deny=False;self.provider_fail=True
        with self.assertRaises(ValueError):self.bridge.check_background(uid)
        server.SSO=None
        with self.assertRaises(ValueError):server.check_sso_background(uid)
