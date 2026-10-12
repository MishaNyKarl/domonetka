"""OpsVault client adapter. Host data stays keyed by the original users.id."""
import hashlib
import hmac
import json
import os
import secrets
import time
import uuid
from dataclasses import asdict
from urllib.parse import parse_qs, urlparse


class AuthError(Exception):
    def __init__(self, status, message):
        self.status = status
        super().__init__(message)


class OpsVaultBridge:
    def __init__(self, connect, origin, secure, password_matches, environ=None, password_hasher=None):
        from opsvault_sso.client import ClientConfig, OIDCClient
        from opsvault_sso.sqlite import SQLiteStore
        from opsvault_sso.errors import AccessDenied, ProviderUnavailable, SSOError
        env = environ if environ is not None else os.environ
        self.jit_enabled=env.get('SSO_JIT_ENABLED','1')=='1'
        self.pilot_link_user=env.get('SSO_PILOT_LINK_USER_ID','')
        self.link_target_subject=env.get('SSO_LINK_TARGET_SUBJECT','')
        self.link_target_name=env.get('SSO_LINK_TARGET_NAME','')
        self.link_targets=json.loads(env.get('SSO_LINK_TARGETS','{}'))
        if not isinstance(self.link_targets,dict):raise ValueError('Invalid linking configuration')
        subjects=set()
        for uid,target in self.link_targets.items():
            if not isinstance(uid,str) or not isinstance(target,dict) or not isinstance(target.get('username'),str) or not target['username']:
                raise ValueError('Invalid linking configuration')
            subject=target.get('subject')
            if not isinstance(subject,str) or str(uuid.UUID(subject))!=subject or subject in subjects:
                raise ValueError('Invalid linking configuration')
            subjects.add(subject)
        self.connect, self.origin, self.secure = connect, origin, secure
        self.password_matches = password_matches
        self.password_hasher=password_hasher
        self.denied, self.unavailable, self.sso_error = AccessDenied, ProviderUnavailable, SSOError
        self.config = ClientConfig(
            issuer=env['SSO_ISSUER'], client_id=env['SSO_CLIENT_ID'],
            client_secret=env['SSO_CLIENT_SECRET'], redirect_uri=origin+'/auth/opsvault/callback',
            session_endpoint=env['SSO_SESSION_ENDPOINT'], access_endpoint=env['SSO_ACCESS_ENDPOINT'])
        self.client_type = OIDCClient
        self.store = SQLiteStore(connect, env['SSO_ENCRYPTION_KEY'].encode())
        self.store.initialize()
        with connect() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS ov_dm_link_attempts(state_hash TEXT PRIMARY KEY, session_hash TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS ov_dm_link_pending(session_hash TEXT PRIMARY KEY, payload BLOB NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS ov_dm_link_secrets(state_hash TEXT PRIMARY KEY, payload BLOB NOT NULL, expires REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS ov_dm_local_sessions(session_hash TEXT PRIMARY KEY, user_id TEXT NOT NULL, issuer TEXT NOT NULL, subject TEXT NOT NULL, role TEXT NOT NULL, checked_until REAL NOT NULL);
            ''')

    def client(self):
        return self.client_type(self.config)

    @staticmethod
    def digest(raw):
        return hashlib.sha256(raw.encode()).hexdigest()

    def linked(self, uid):
        with self.connect() as db:
            return db.execute('SELECT issuer,subject FROM ov_sso_identities WHERE user_id=?', (uid,)).fetchone()

    def authorize(self, raw, actor, fresh=False):
        if not self.linked(actor['id']):
            if not actor['local_login_enabled']:raise AuthError(401,'У аккаунта нет активной связи OpsVault.')
            return actor
        try:
            with self.connect() as db:
                local=db.execute('SELECT * FROM ov_dm_local_sessions WHERE session_hash=?',(self.digest(raw),)).fetchone()
            if local:
                identity=self.linked(actor['id'])
                if local['user_id']!=actor['id'] or local['issuer']!=identity['issuer'] or local['subject']!=identity['subject']:
                    raise self.denied('Identity mismatch')
                role=local['role']
                if fresh or local['checked_until']<=time.time():
                    decision=self.local_decision(actor['id'])
                    role=decision.role
                    with self.connect() as db:
                        db.execute('UPDATE ov_dm_local_sessions SET role=?,checked_until=? WHERE session_hash=?',
                                   (role,decision.valid_until,self.digest(raw)))
                return {**actor,'role':role,'sso':True,'sso_local':True}
            access = self.store.authorize(raw, self.client(), fresh=fresh)
            if access.user_id != actor['id']:
                raise self.denied('Identity mismatch')
        except self.unavailable:
            raise AuthError(503, 'OpsVault временно недоступен. Проверка доступа не завершена.') from None
        except self.sso_error:
            self.drop_local(raw)
            raise AuthError(401, 'Доступ OpsVault завершён. Войдите заново.') from None
        return {**actor, 'role': access.role, 'sso': True}

    def local_decision(self, uid):
        identity=self.linked(uid)
        if not identity or identity['issuer']!=self.config.issuer:raise self.denied('Identity mismatch')
        return self.client().check_local_access(identity['subject'],uid)

    def attach_local_session(self, raw, uid):
        try:decision=self.local_decision(uid)
        except self.unavailable:raise AuthError(503,'OpsVault недоступен. Вход временно приостановлен.') from None
        except self.sso_error:raise AuthError(403,'OpsVault запретил доступ к сервису.') from None
        identity=self.linked(uid)
        with self.connect() as db:
            db.execute('INSERT INTO ov_dm_local_sessions VALUES(?,?,?,?,?,?)',
                       (self.digest(raw),uid,identity['issuer'],identity['subject'],decision.role,decision.valid_until))

    def check_background(self, uid):
        identity = self.linked(uid)
        if not identity:
            return
        try:
            if identity['issuer'] != self.config.issuer:
                raise self.denied('Issuer mismatch')
            self.client().check_background(identity['subject'])
        except self.unavailable:
            raise ValueError('OpsVault недоступен; импорт приостановлен') from None
        except self.sso_error:
            raise ValueError('OpsVault запретил фоновый импорт') from None

    def cookie(self, value='', name='admin', ttl=0):
        return f'{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={ttl}{self.secure}'

    def drop_local(self, raw):
        if not raw:
            return
        self.store.logout_local(raw)
        with self.connect() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?', (self.digest(raw),))
            db.execute('DELETE FROM ov_dm_link_pending WHERE session_hash=?', (self.digest(raw),))
            db.execute('DELETE FROM ov_dm_local_sessions WHERE session_hash=?',(self.digest(raw),))

    def issue(self, handler, uid, result):
        with self.connect() as db:
            user = db.execute('SELECT active FROM users WHERE id=?', (uid,)).fetchone()
            if not user or not user['active']:
                raise AuthError(403, 'Учётная запись отключена.')
        self.drop_local(handler.cookie('admin'))
        raw = secrets.token_urlsafe(32)
        with self.connect() as db:
            db.execute('INSERT INTO sessions(token_hash,user_id,expires,csrf) VALUES(?,?,?,?)',
                       (self.digest(raw), uid, int(result.expires_at), secrets.token_urlsafe(32)))
        try:
            self.store.attach_session(raw, uid, result)
        except Exception:
            self.drop_local(raw)
            raise
        return handler.send(303, b'', headers={'Location':'/admin', 'Set-Cookie':self.cookie(raw, ttl=max(0,int(result.expires_at-time.time()))), 'Referrer-Policy':'no-referrer'})

    def create_user(self, db, identity, result=None):
        if not self.jit_enabled:
            raise AuthError(403,'Для пилота сначала войдите в существующий аккаунт домонетки и нажмите «Связать OpsVault».')
        for uid,target in self.link_targets.items():
            if target['subject']==identity.subject and identity.issuer==self.config.issuer and db.execute('SELECT 1 FROM users WHERE id=?',(uid,)).fetchone():
                raise AuthError(403,'У вас уже есть аккаунт домонетки. Войдите в него по локальному паролю и нажмите «Связать OpsVault».')
        if self.pilot_link_user and self.link_target_subject==identity.subject and identity.issuer==self.config.issuer and db.execute('SELECT 1 FROM users WHERE id=?',(self.pilot_link_user,)).fetchone():
            raise AuthError(403,'У вас уже есть аккаунт домонетки. Сначала свяжите его с OpsVault.')
        if result is None or self.password_hasher is None:raise AuthError(503,'Создание аккаунта пока недоступно.')
        client=self.client()
        stored=client.get_credentials(result.lease_token,identity.subject)
        if stored is None:
            uid=uuid.uuid4().hex;username='ov_'+uid;password=secrets.token_urlsafe(32)
            try:
                receipt=client.save_credentials(result.lease_token,identity.subject,uid,username,password)
                if receipt.get('saved') is not True:raise self.denied('Credential publication not confirmed')
            except self.sso_error:
                stored=client.get_credentials(result.lease_token,identity.subject)
                if stored is None:raise
        if stored is not None:
            uid,username,password=stored.local_user_id,stored.username,stored.password
        if not isinstance(uid,str) or len(uid)!=32 or uuid.UUID(uid).hex!=uid or not isinstance(username,str) or not isinstance(password,str) or not 10<=len(password)<=256:
            raise AuthError(403,'Неверная привязка доступа. Обратитесь к администратору.')
        import re
        if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,63}',username) or db.execute('SELECT 1 FROM users WHERE id=? OR username=?',(uid,username)).fetchone():
            raise AuthError(409,'Существующий аккаунт требует явного связывания.')
        name = (identity.name or identity.username or 'Пользователь OpsVault')[:100]
        db.execute('INSERT INTO users(id,username,name,role,active,password_hash,local_login_enabled) VALUES(?,?,?,?,?,?,1)',
                   (uid,username,name,'user',1,self.password_hasher(password)))
        return uid

    def can_link(self, uid):
        if self.link_targets:return uid in self.link_targets
        return not self.pilot_link_user or uid==self.pilot_link_user

    def link_name(self, uid):
        if self.link_targets:return self.link_targets.get(uid,{}).get('username','')
        return self.link_target_name if self.can_link(uid) else ''

    def require_link_target(self, result):
        if self.link_targets:
            target=self.link_targets.get(result.link_user_id)
            if not target:raise AuthError(403,'Связывание этого аккаунта пока не настроено.')
            if result.identity.issuer!=self.config.issuer or result.identity.subject!=target['subject']:
                raise AuthError(403,'Для связывания войдите в OpsVault как '+target['username']+'.')
            return
        if self.link_target_subject and (result.identity.issuer!=self.config.issuer or result.identity.subject!=self.link_target_subject):
            name=self.link_target_name or 'указанный администратором аккаунт'
            raise AuthError(403,'Для связывания войдите в OpsVault как '+name+'.')

    def require_local(self, handler):
        actor = handler.admin()
        if not actor or actor.get('sso') or self.linked(actor['id']):
            raise AuthError(403, 'Для связывания войдите в существующую локальную учётную запись.')
        if not self.can_link(actor['id']):raise AuthError(403,'Связывание пока доступно только пилотному аккаунту.')
        return actor

    def csrf(self, handler, actor, data):
        if handler.headers.get('Origin') != self.origin or not isinstance(data.get('csrf'),str) or not hmac.compare_digest(data['csrf'],actor['csrf']):
            raise AuthError(403, 'Обновите страницу и повторите действие.')

    def get(self, handler, path):
        if path == '/auth/opsvault/login':
            url, attempt = self.client().begin('/admin')
            binding = self.store.save_attempt(attempt)
            return handler.send(302,b'',headers={'Location':url,'Set-Cookie':self.cookie(binding,'__Host-ov_sso_login',300),'Referrer-Policy':'no-referrer'})
        if path == '/auth/opsvault/callback':
            parsed = urlparse(handler.path)
            query = parse_qs(parsed.query,keep_blank_values=True)
            if query.get('state') is None or any(len(v)!=1 for v in query.values()):
                raise AuthError(400, 'Вход не завершён. Начните заново.')
            attempt = self.store.consume_attempt(query['state'][0],handler.cookie('__Host-ov_sso_login') or '')
            result = self.client().complete(self.config.redirect_uri+'?'+parsed.query,attempt)
            if result.link_user_id is not None:
                actor = self.require_local(handler)
                self.require_link_target(result)
                raw = handler.cookie('admin') or ''
                with self.connect() as db:
                    link = db.execute('SELECT session_hash FROM ov_dm_link_attempts WHERE state_hash=?',(self.digest(attempt.state),)).fetchone()
                    db.execute('DELETE FROM ov_dm_link_attempts WHERE state_hash=?',(self.digest(attempt.state),))
                    if not link or link['session_hash'] != self.digest(raw) or actor['id'] != result.link_user_id:
                        raise AuthError(403,'Сессия связывания изменилась. Начните заново.')
                    db.execute('DELETE FROM ov_dm_link_pending WHERE expires<=?',(time.time(),))
                    secret=db.execute('SELECT payload FROM ov_dm_link_secrets WHERE state_hash=? AND expires>?',
                                      (self.digest(attempt.state),time.time())).fetchone()
                    db.execute('DELETE FROM ov_dm_link_secrets WHERE state_hash=?',(self.digest(attempt.state),))
                    if not secret:raise AuthError(400,'Подтверждение пароля истекло. Начните заново.')
                    password=self.store.cipher.decrypt(secret['payload']).decode()
                    db.execute('INSERT OR REPLACE INTO ov_dm_link_pending VALUES(?,?,?)',
                               (self.digest(raw),self.store.cipher.encrypt(json.dumps({'result':asdict(result),'password':password}).encode()),min(time.time()+300,attempt.expires_at,result.expires_at)))
                return handler.send(303,b'',headers={'Location':'/admin?sso_link=1','Set-Cookie':self.cookie(name='__Host-ov_sso_login'),'Referrer-Policy':'no-referrer'})
            return self.issue(handler,self.store.provision(result,lambda db,identity:self.create_user(db,identity,result)),result)
        if path == '/api/auth/link/status':
            actor = self.require_local(handler)
            with self.connect() as db:
                row = db.execute('SELECT payload FROM ov_dm_link_pending WHERE session_hash=? AND expires>?',
                                 (self.digest(handler.cookie('admin') or ''),time.time())).fetchone()
            if not row:
                return handler.send(200,{'pending':False})
            payload=json.loads(self.store.cipher.decrypt(row['payload']))['result']
            return handler.send(200,{'pending':True,'local_name':actor['name']+' · '+actor['username'],'opsvault_name':(payload['identity']['name'] or payload['identity']['username'])+' · '+payload['identity']['username'],'role':payload['role'],'issuer':payload['identity']['issuer']})
        return handler.send(404,{'error':'Не найдено'})

    def post(self, handler, path, data):
        if path == '/api/auth/link/start':
            actor = self.require_local(handler)
            self.csrf(handler,actor,data)
            if data.get('save_credentials') is not True:raise AuthError(400,'Подтвердите сохранение логина и пароля в OpsVault.')
            with self.connect() as db:
                user = db.execute('SELECT password_hash FROM users WHERE id=?',(actor['id'],)).fetchone()
            password = str(data.get('password',''))
            if not 10<=len(password)<=256 or not self.password_matches(password,user['password_hash']):
                raise AuthError(403,'Неверный текущий пароль.')
            url,attempt = self.client().begin('/admin',link_user_id=actor['id'])
            binding=self.store.save_attempt(attempt)
            with self.connect() as db:
                db.execute('INSERT INTO ov_dm_link_attempts VALUES(?,?)',(self.digest(attempt.state),self.digest(handler.cookie('admin'))))
                db.execute('DELETE FROM ov_dm_link_secrets WHERE expires<=?',(time.time(),))
                db.execute('INSERT INTO ov_dm_link_secrets VALUES(?,?,?)',
                           (self.digest(attempt.state),self.store.cipher.encrypt(password.encode()),min(time.time()+300,attempt.expires_at)))
            return handler.send(200,{'redirect':url},headers={'Set-Cookie':self.cookie(binding,'__Host-ov_sso_login',300)})
        if path in ('/api/auth/link/confirm','/api/auth/link/cancel'):
            actor = self.require_local(handler)
            self.csrf(handler,actor,data)
            raw=handler.cookie('admin') or ''
            if path.endswith('/cancel'):
                with self.connect() as db:db.execute('DELETE FROM ov_dm_link_pending WHERE session_hash=?',(self.digest(raw),))
                return handler.send(200,{'ok':True})
            from opsvault_sso.client import Identity, LoginResult
            with self.connect() as db:
                row=db.execute('SELECT payload FROM ov_dm_link_pending WHERE session_hash=? AND expires>?',(self.digest(raw),time.time())).fetchone()
            if not row:raise AuthError(400,'Подтверждение истекло. Начните связывание заново.')
            pending=json.loads(self.store.cipher.decrypt(row['payload']))
            payload=pending['result'];password=pending['password']
            payload['identity']=Identity(**payload['identity'])
            result=LoginResult(**payload)
            self.require_link_target(result)
            self.client().check_access(result.lease_token)
            def on_link(db,uid):
                if uid != actor['id']:raise AuthError(403,'Неверная учётная запись.')
                local=db.execute('SELECT active,local_login_enabled,username,password_hash FROM users WHERE id=?',(uid,)).fetchone()
                session=db.execute('SELECT 1 FROM sessions WHERE token_hash=? AND user_id=? AND expires>?',(self.digest(raw),uid,time.time())).fetchone()
                if not local or not local['active'] or not local['local_login_enabled'] or not session:raise AuthError(403,'Локальная сессия завершена.')
                if not self.password_matches(password,local['password_hash']):raise AuthError(403,'Пароль изменился. Начните заново.')
                receipt=self.client().save_credentials(result.lease_token,result.identity.subject,uid,local['username'],password)
                if receipt.get('saved') is not True:raise self.denied('Credential publication not confirmed')
                db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
                db.execute('DELETE FROM ov_sso_sessions WHERE user_id=?',(uid,))
                db.execute('DELETE FROM ov_dm_local_sessions WHERE user_id=?',(uid,))
                db.execute('DELETE FROM ov_dm_link_pending WHERE session_hash=?',(self.digest(raw),))
            self.store.link(result,actor['id'],on_link=on_link)
            return handler.send(200,{'ok':True,'redirect':'/auth/opsvault/login'},headers={'Set-Cookie':self.cookie()})
        if path == '/api/auth/logout-all':
            raw=handler.cookie('admin') or ''
            # Logout must still work when the central authorization service is down.
            with self.connect() as db:
                row=db.execute('SELECT csrf FROM sessions WHERE token_hash=?',(self.digest(raw),)).fetchone()
            if not row:raise AuthError(401,'Сессия завершена.')
            self.csrf(handler,{'csrf':row['csrf']},data)
            status=200;message='Вы вышли из OpsVault и сервисов.'
            try:self.store.logout_all(raw,self.client())
            except self.sso_error:
                status=503;message='Местная сессия закрыта. Общий выход не подтверждён; выйдите также из OpsVault.'
            finally:self.drop_local(raw)
            return handler.send(status,{'ok':status==200,'error':message} if status!=200 else {'ok':True},headers={'Set-Cookie':self.cookie()})
        return handler.send(404,{'error':'Не найдено'})

    def handle(self, handler, path, data=None):
        try:
            return self.get(handler,path) if data is None else self.post(handler,path,data)
        except AuthError as exc:
            return handler.send(exc.status,{'error':str(exc)},headers={'Referrer-Policy':'no-referrer'})
        except self.unavailable:
            return handler.send(503,{'error':'OpsVault временно недоступен. Повторите позже.'},headers={'Referrer-Policy':'no-referrer'})
        except self.sso_error:
            return handler.send(403,{'error':'Вход или связывание не завершены. Начните заново.'},headers={'Referrer-Policy':'no-referrer'})
        except (ValueError,KeyError,TypeError):
            return handler.send(400,{'error':'Некорректное состояние входа. Начните заново.'},headers={'Referrer-Policy':'no-referrer'})
