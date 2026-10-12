import os, json, sqlite3, uuid, time, random, secrets, hashlib, hmac, itertools, base64, re
from pathlib import Path
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from binom import campaign_id, validate_tracking_url, attributed_url, date_range, token_mapping, TOKEN_FIELDS
from binom import BinomError
from binom_settings import read_settings, public_settings, save_settings, check_connection, migrate_legacy
from binom_sync import init_schema as init_binom_schema, SyncService, read_report, status_policy

ROOT = Path(__file__).resolve().parent
DATA = ROOT / 'data'
DATA.mkdir(exist_ok=True)
(DATA / 'uploads').mkdir(exist_ok=True)
DB = DATA / 'domonetka.sqlite'
PASSWORD = os.environ.get('ADMIN_PASSWORD', 'admin-local-2026')
SESSIONS = {}
SESSION_TTL = 30*86400
LOGIN_ATTEMPTS = {}
PUBLIC_ORIGIN = os.environ.get('PUBLIC_ORIGIN','')
SECURE = '; Secure' if PUBLIC_ORIGIN.startswith('https://') else ''
SYNC = None
SSO = None
from sso_bridge import AuthError, OpsVaultBridge

def password_hash(password):
    salt=secrets.token_hex(16)
    return salt+'$'+hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),310000).hex()

def password_matches(password,stored):
    if not stored or '$' not in stored: return False
    salt,digest=stored.split('$')
    return hmac.compare_digest(digest,hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),310000).hex())

def public_users(db):
    return [{**dict(r),'sso_linked':linked_user(db,r['id'])} for r in db.execute('SELECT id,username,name,role,active,local_login_enabled FROM users ORDER BY rowid')]

def linked_user(db,uid):
    return bool(db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='ov_sso_identities'").fetchone()
                and db.execute('SELECT 1 FROM ov_sso_identities WHERE user_id=?',(uid,)).fetchone())

def save_user(db,data,actor):
    uid=data.get('id')
    old=db.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone() if uid else None
    if uid and not old: raise ValueError('Пользователь не найден')
    username=str(data.get('username','')).strip().lower()
    name=str(data.get('name','')).strip()
    role=data.get('role','user');active=int(bool(data.get('active',True)))
    if not re.fullmatch(r'[a-z0-9][a-z0-9._-]{2,63}',username): raise ValueError('Логин: 3–64 латинских символа, цифры, точка, дефис или подчёркивание')
    if not name or len(name)>100: raise ValueError('Укажите имя до 100 символов')
    if role not in ('admin','user'): raise ValueError('Неизвестная роль')
    if db.execute('SELECT 1 FROM users WHERE username=? AND id!=?',(username,uid or '')).fetchone(): raise ValueError('Этот логин уже занят')
    if old and old['role']=='admin' and old['active'] and (role!='admin' or not active):
        if db.execute("SELECT count(*) FROM users WHERE role='admin' AND active=1 AND id!=?",(uid,)).fetchone()[0]==0: raise ValueError('Нельзя отключить или понизить последнего активного администратора')
    password=str(data.get('password',''))
    if (password or not old) and not 10<=len(password)<=256: raise ValueError('Пароль должен содержать от 10 до 256 символов')
    if old and (not old['local_login_enabled'] or linked_user(db,uid)) and (password or role!=old['role'] or username!=old['username']): raise ValueError('Логин, пароль и роль связанного аккаунта нельзя менять здесь')
    digest=password_hash(password) if password else old['password_hash']
    uid=uid or uuid.uuid4().hex
    db.execute('INSERT INTO users(id,username,name,role,active,password_hash) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET username=excluded.username,name=excluded.name,role=excluded.role,active=excluded.active,password_hash=excluded.password_hash',(uid,username,name,role,active,digest))
    if old and (password or not active or role!=old['role']):
        db.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
        for token,session in list(SESSIONS.items()):
            if session['user_id']==uid: SESSIONS.pop(token,None)
    return uid

class Connection(sqlite3.Connection):
    def __exit__(self,*args):
        try: return super().__exit__(*args)
        finally: self.close()

def connect():
    db = sqlite3.connect(DB, timeout=20, factory=Connection)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    return db

def display_image(url):
    # Keep the source URL and variant identity; a derivative only changes the displayed asset.
    if re.fullmatch(r'/uploads/[a-f0-9]{32}\.(png|jpg|webp)',url):
        optimized=Path(url).stem+'-optimized.webp'
        if (DATA/'uploads'/optimized).is_file(): return '/uploads/'+optimized
    return url

def init():
    with connect() as db:
        db.execute('PRAGMA journal_mode=WAL')
        init_binom_schema(db)
        fresh = not db.execute("SELECT 1 FROM sqlite_master WHERE name='offers'").fetchone()
        db.executescript('''
        CREATE TABLE IF NOT EXISTS users(id TEXT PRIMARY KEY,username TEXT UNIQUE NOT NULL,name TEXT NOT NULL,role TEXT NOT NULL,active INTEGER NOT NULL,password_hash TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,expires INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS trackers(id TEXT PRIMARY KEY,user_id TEXT UNIQUE NOT NULL REFERENCES users(id),name TEXT NOT NULL,kind TEXT NOT NULL DEFAULT 'binom',created INTEGER NOT NULL,updated INTEGER NOT NULL);
        CREATE TABLE IF NOT EXISTS tracker_logs(id INTEGER PRIMARY KEY AUTOINCREMENT,user_id TEXT NOT NULL REFERENCES users(id),actor_id TEXT NOT NULL REFERENCES users(id),created INTEGER NOT NULL,operation TEXT NOT NULL,status TEXT NOT NULL,message TEXT NOT NULL,duration_ms INTEGER NOT NULL DEFAULT 0);
        CREATE INDEX IF NOT EXISTS tracker_logs_owner ON tracker_logs(user_id,id DESC);
        CREATE TABLE IF NOT EXISTS streams(id TEXT PRIMARY KEY,name TEXT NOT NULL,slug TEXT UNIQUE NOT NULL,country TEXT NOT NULL DEFAULT '',active INTEGER NOT NULL DEFAULT 1);
        CREATE TABLE IF NOT EXISTS offers(id TEXT PRIMARY KEY,name TEXT,url TEXT,active INTEGER, titles TEXT,descriptions TEXT,images TEXT);
        CREATE TABLE IF NOT EXISTS variants(id TEXT PRIMARY KEY,offer_id TEXT REFERENCES offers(id) ON DELETE CASCADE,title TEXT,description TEXT,image TEXT,active INTEGER DEFAULT 1);
        CREATE TABLE IF NOT EXISTS deliveries(id TEXT PRIMARY KEY,visitor TEXT,variant_id TEXT REFERENCES variants(id) ON DELETE CASCADE,created INTEGER);
        CREATE TABLE IF NOT EXISTS events(visitor TEXT,variant_id TEXT REFERENCES variants(id) ON DELETE CASCADE,day TEXT,kind TEXT, PRIMARY KEY(visitor,variant_id,day,kind));
        CREATE TABLE IF NOT EXISTS visits(visitor TEXT,day TEXT, PRIMARY KEY(visitor,day));
        CREATE TABLE IF NOT EXISTS stream_visits(visitor TEXT,day TEXT,stream_id TEXT REFERENCES streams(id), PRIMARY KEY(visitor,day,stream_id));
        ''')
        if not db.execute('SELECT 1 FROM users').fetchone():
            db.execute('INSERT INTO users(id,username,name,role,active,password_hash) VALUES(?,?,?,?,?,?)',('admin','admin','Администратор','admin',1,password_hash(PASSWORD)))
        if 'local_login_enabled' not in [r['name'] for r in db.execute('PRAGMA table_info(users)')]:
            db.execute('ALTER TABLE users ADD COLUMN local_login_enabled INTEGER NOT NULL DEFAULT 1')
        if 'csrf' not in [r['name'] for r in db.execute('PRAGMA table_info(sessions)')]:
            db.execute("ALTER TABLE sessions ADD COLUMN csrf TEXT NOT NULL DEFAULT ''")
        admin_id=db.execute("SELECT id FROM users WHERE role='admin' ORDER BY rowid LIMIT 1").fetchone()[0]
        if 'sync_interval_minutes' not in [r['name'] for r in db.execute('PRAGMA table_info(trackers)')]:
            db.execute('ALTER TABLE trackers ADD COLUMN sync_interval_minutes INTEGER NOT NULL DEFAULT 15')
        if 'active' not in [r['name'] for r in db.execute('PRAGMA table_info(trackers)')]:
            db.execute('ALTER TABLE trackers ADD COLUMN active INTEGER NOT NULL DEFAULT 1')
        migrate_legacy(DATA,admin_id)
        for user in db.execute('SELECT id FROM users').fetchall():
            if read_settings(DATA,user['id'])['origin']:
                db.execute("INSERT OR IGNORE INTO trackers(id,user_id,name,kind,created,updated) VALUES(?,?,?,'binom',?,?)",(uuid.uuid4().hex,user['id'],'Мой Binom',int(time.time()),int(time.time())))
        if 'owner_id' not in [r['name'] for r in db.execute('PRAGMA table_info(streams)')]:
            db.execute('ALTER TABLE streams ADD COLUMN owner_id TEXT REFERENCES users(id)')
            db.execute('UPDATE streams SET owner_id=? WHERE owner_id IS NULL',(admin_id,))
        db.execute("INSERT OR IGNORE INTO streams(id,name,slug,country,active,owner_id) VALUES('main','Основной поток','main','',1,?)",(admin_id,))
        if 'stream_id' not in [r['name'] for r in db.execute('PRAGMA table_info(offers)')]:
            db.execute("ALTER TABLE offers ADD COLUMN stream_id TEXT NOT NULL DEFAULT 'main'")
            db.execute("INSERT OR IGNORE INTO stream_visits SELECT visitor,day,'main' FROM visits")
        db.execute('CREATE INDEX IF NOT EXISTS offers_stream ON offers(stream_id)')
        if 'binom_campaign_id' not in [r['name'] for r in db.execute('PRAGMA table_info(offers)')]:
            db.execute("ALTER TABLE offers ADD COLUMN binom_campaign_id TEXT NOT NULL DEFAULT ''")
        if 'affiliate_id' not in [r['name'] for r in db.execute('PRAGMA table_info(offers)')]:
            db.execute("ALTER TABLE offers ADD COLUMN affiliate_id TEXT NOT NULL DEFAULT ''")
        if 'binom_tokens' not in [r['name'] for r in db.execute('PRAGMA table_info(offers)')]:
            db.execute("ALTER TABLE offers ADD COLUMN binom_tokens TEXT NOT NULL DEFAULT '{}'")
        if 'binom_override_tokens' not in [r['name'] for r in db.execute('PRAGMA table_info(offers)')]:
            db.execute('ALTER TABLE offers ADD COLUMN binom_override_tokens INTEGER NOT NULL DEFAULT 0')
            db.execute("UPDATE offers SET binom_override_tokens=1 WHERE binom_tokens!='{}'")
        db.executescript('''
        CREATE TABLE IF NOT EXISTS outbound_clicks(
          id TEXT PRIMARY KEY, visitor TEXT NOT NULL, stream_id TEXT NOT NULL,
          offer_id TEXT NOT NULL, variant_id TEXT NOT NULL, campaign_id TEXT NOT NULL,
          created INTEGER NOT NULL, day TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS outbound_day ON outbound_clicks(day,stream_id,offer_id);
        ''')
        if fresh:
            for name,title,desc,img in [('Путешествия','Небольшой отпуск. Большие впечатления.','Откройте новые места и найдите путешествие для себя.','travel'),('Для дома','Больше уюта каждый день','Полезные находки для дома и вашего комфорта.','home'),('Обучение','Время попробовать что-то новое','Новые навыки, интересные курсы и вдохновение.','learn'),('Технологии','Ваш следующий любимый гаджет','Умные решения для работы и повседневных дел.','tech'),('Стиль','Детали, которые меняют всё','Откройте подборку вещей для своего настроения.','style'),('Отдых','Маленькая пауза для себя','Идеи для спокойных вечеров и приятных выходных.','relax')]:
                save_offer(db,dict(name=name,url='/demo-offer.html',active=True,titles=[title],descriptions=[desc],images=['/assets/'+img+'.svg']))

def valid_url(url, local=False):
    if any(ord(c)<32 or ord(c)==127 for c in url): return False
    p=urlparse(url)
    return (p.scheme in ('http','https') and bool(p.netloc)) or (local and url.startswith('/') and not url.startswith('//'))

def save_offer(db, data, oid=None):
    existing=db.execute('SELECT stream_id,binom_campaign_id,binom_tokens,binom_override_tokens,affiliate_id FROM offers WHERE id=?',(oid,)).fetchone() if oid else None
    stream_id=data.get('stream_id') or (existing['stream_id'] if existing else 'main')
    if not db.execute('SELECT 1 FROM streams WHERE id=?',(stream_id,)).fetchone(): raise ValueError('Поток не найден')
    if existing and existing['stream_id']!=stream_id: raise ValueError('Для другого потока используйте копирование оффера')
    name=str(data.get('name','')).strip()
    url=str(data.get('url','')).strip()
    arrays=[]
    for key in ('titles','descriptions','images'):
        raw=data.get(key,[])
        if not isinstance(raw,list): raise ValueError('Материалы должны быть списком')
        values=list(dict.fromkeys(str(x).strip() for x in raw if str(x).strip()))
        if not values or len(values)>20: raise ValueError('Добавьте от 1 до 20 материалов в каждом поле')
        if any(len(v)>2000 for v in values): raise ValueError('Слишком длинный материал')
        arrays.append(values)
    if not name or len(name)>200 or not valid_url(url,True): raise ValueError('Укажите название и корректную ссылку http(s)')
    campaign=campaign_id(data.get('binom_campaign_id',existing['binom_campaign_id'] if existing else ''))
    if campaign: validate_tracking_url(url)
    affiliate=str(data.get('affiliate_id',existing['affiliate_id'] if existing else '') or '')
    if affiliate:
        owner=db.execute('SELECT owner_id FROM streams WHERE id=?',(stream_id,)).fetchone()[0]
        if not db.execute('SELECT 1 FROM affiliate_networks WHERE id=? AND user_id=?',(affiliate,owner)).fetchone():raise ValueError('Выберите партнёрку владельца этого потока')
    tokens=token_mapping(data.get('binom_tokens',json.loads(existing['binom_tokens']) if existing else {}))
    override=int(bool(data.get('binom_override_tokens',existing['binom_override_tokens'] if existing else bool(tokens))))
    if tokens and not campaign: raise ValueError('Сначала укажите ID кампании Binom для настройки токенов')
    if any(not valid_url(x,True) for x in arrays[2]): raise ValueError('Некорректная ссылка на картинку')
    if len(arrays[0])*len(arrays[1])*len(arrays[2])>500: raise ValueError('Максимум 500 сочетаний на оффер. Уменьшите число материалов.')
    oid=oid or uuid.uuid4().hex
    db.execute('INSERT INTO offers(id,name,url,active,titles,descriptions,images,stream_id) VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,url=excluded.url,active=excluded.active,titles=excluded.titles,descriptions=excluded.descriptions,images=excluded.images',(oid,name,url,int(bool(data.get('active',True))),*(json.dumps(a,ensure_ascii=False) for a in arrays),stream_id))
    db.execute('UPDATE offers SET binom_campaign_id=?,binom_tokens=?,binom_override_tokens=? WHERE id=?',(campaign,json.dumps(tokens),override,oid))
    db.execute('UPDATE offers SET affiliate_id=? WHERE id=?',(affiliate,oid))
    db.execute('UPDATE variants SET active=0 WHERE offer_id=?',(oid,))
    for title,desc,img in itertools.product(*arrays):
        vid=hashlib.sha256(json.dumps([oid,title,desc,img],ensure_ascii=False).encode()).hexdigest()[:32]
        db.execute('INSERT INTO variants VALUES(?,?,?,?,?,1) ON CONFLICT(id) DO UPDATE SET active=1',(vid,oid,title,desc,img))
    return oid

def stats(db, stream_id=None, start=None, end=None):
    return [dict(r) for r in db.execute('''SELECT v.*,o.name,o.url,o.stream_id,o.active AS offer_active,
      count(CASE WHEN e.kind='impression' THEN 1 END) AS impressions,
      count(CASE WHEN e.kind='click' THEN 1 END) AS clicks
      FROM variants v JOIN offers o ON o.id=v.offer_id LEFT JOIN events e ON e.variant_id=v.id
      AND (? IS NULL OR e.day>=?) AND (? IS NULL OR e.day<=?)
      WHERE (? IS NULL OR o.stream_id=?) GROUP BY v.id''',(start,start,end,end,stream_id,stream_id))]

def save_stream(db,data,actor):
    sid=data.get('id')
    old=db.execute('SELECT * FROM streams WHERE id=?',(sid,)).fetchone() if sid else None
    if sid and not old: raise ValueError('Поток не найден')
    name=str(data.get('name','')).strip()
    slug=str(data.get('slug','')).strip().lower()
    if not name or len(name)>100: raise ValueError('Название потока: от 1 до 100 символов')
    if not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*',slug) or len(slug)>60: raise ValueError('Адрес: латинские буквы, цифры и дефисы, до 60 символов')
    if old and slug!=old['slug']: raise ValueError('Адрес существующего потока менять нельзя: он уже может быть подключён на лендингах')
    if db.execute('SELECT 1 FROM streams WHERE slug=? AND id!=?',(slug,sid or '')).fetchone(): raise ValueError('Такой адрес уже занят')
    sid=sid or uuid.uuid4().hex
    owner_id=(data.get('owner_id') or (old['owner_id'] if old else actor['id'])) if actor['role']=='admin' else actor['id']
    if not db.execute('SELECT 1 FROM users WHERE id=?',(owner_id,)).fetchone(): raise ValueError('Владелец не найден')
    db.execute('INSERT INTO streams(id,name,slug,country,active,owner_id) VALUES(?,?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,active=excluded.active,owner_id=excluded.owner_id',(sid,name,slug,'',int(bool(data.get('active',True))),owner_id))
    if old and old['owner_id']!=owner_id:db.execute("UPDATE offers SET affiliate_id='' WHERE stream_id=?",(sid,))
    return sid

class Handler(BaseHTTPRequestHandler):
    def cookie(self,name):
        return next((c.strip().split('=',1)[1] for c in self.headers.get('Cookie','').split(';') if c.strip().startswith(name+'=')),None)
    def admin(self):
        token=self.cookie('admin')
        if not token or len(token)>128: return None
        digest=hashlib.sha256(token.encode()).hexdigest()
        now=int(time.time())
        with connect() as db:
            row=db.execute('SELECT u.id,u.username,u.name,u.role,u.active,u.local_login_enabled,s.expires,s.csrf FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>? AND u.active=1',(digest,now)).fetchone()
            if not row:return None
            actor={k:row[k] for k in ('id','username','name','role','active','csrf','local_login_enabled')}
            if not actor['csrf']:
                actor['csrf']=secrets.token_urlsafe(32)
                db.execute('UPDATE sessions SET csrf=? WHERE token_hash=?',(actor['csrf'],digest))
        if SSO:
            actor=SSO.authorize(token,actor,fresh=self.command=='POST')
        elif not actor['local_login_enabled'] or self.is_linked(actor['id']):
            raise AuthError(503,'Вход OpsVault временно отключён; локальный вход этого аккаунта запрещён.')
        if not actor.get('sso') and row['expires']<now+SESSION_TTL-86400:
            with connect() as db:db.execute('UPDATE sessions SET expires=? WHERE token_hash=?',(now+SESSION_TTL,digest))
            self._session_cookie=f'admin={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}{SECURE}'
        actor['sso_enabled']=bool(SSO)
        actor['sso_can_link']=bool(SSO and not actor.get('sso') and SSO.can_link(actor['id']))
        actor['sso_link_target']=SSO.link_name(actor['id']) if actor['sso_can_link'] else ''
        return actor
    def is_linked(self,uid):
        with connect() as db:return linked_user(db,uid)
    def can_stream(self,db,sid,actor):
        row=db.execute('SELECT owner_id FROM streams WHERE id=?',(sid,)).fetchone()
        return row and (actor['role']=='admin' or row['owner_id']==actor['id'])
    def can_offer(self,db,oid,actor):
        row=db.execute('SELECT stream_id FROM offers WHERE id=?',(oid,)).fetchone()
        return row and self.can_stream(db,row['stream_id'],actor)
    def send(self,status,body,ctype='application/json; charset=utf-8',headers=None):
        if isinstance(body,(dict,list)): body=json.dumps(body,ensure_ascii=False).encode()
        if isinstance(body,str): body=body.encode()
        self.send_response(status)
        self.send_header('Content-Type',ctype)
        self.send_header('Cache-Control',(headers or {}).get('Cache-Control','no-store'))
        self.send_header('X-Content-Type-Options','nosniff')
        self.send_header('Referrer-Policy',(headers or {}).get('Referrer-Policy','strict-origin-when-cross-origin'))
        if getattr(self,'_session_cookie',None) and 'Set-Cookie' not in (headers or {}): self.send_header('Set-Cookie',self._session_cookie)
        for k,v in (headers or {}).items():
            if k not in ('Cache-Control','Referrer-Policy'): self.send_header(k,v)
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, format, *args):
        if urlparse(self.path).path.startswith('/auth/'):
            return
        super().log_message(format,*args)
    def do_GET(self):
        try:return self.get_request()
        except AuthError as exc:return self.send(exc.status,{'error':str(exc)})
    def get_request(self):
        path=urlparse(self.path).path
        if path=='/api/auth/config': return self.send(200,{'enabled':bool(SSO),'auto_account_creation':bool(SSO and SSO.jit_enabled)})
        if path.startswith('/auth/') or path.startswith('/api/auth/'):
            if not SSO:return self.send(404,{'error':'Вход OpsVault не включён'})
            return SSO.handle(self,path)
        if path=='/api/feed':
            visitor=self.cookie('visitor')
            if not visitor or len(visitor)!=32: visitor=uuid.uuid4().hex
            with connect() as db:
                slug=parse_qs(urlparse(self.path).query).get('stream',['main'])[0]
                stream=db.execute('SELECT * FROM streams WHERE slug=?',(slug,)).fetchone()
                if not stream: return self.send(404,{'error':'Поток не найден'})
                if not stream['active']: return self.send(410,{'error':'Предложения этого потока временно недоступны'})
                day=time.strftime('%Y-%m-%d',time.gmtime())
                db.execute('INSERT OR IGNORE INTO visits VALUES(?,?)',(visitor,day))
                db.execute('INSERT OR IGNORE INTO stream_visits VALUES(?,?,?)',(visitor,day,stream['id']))
                db.execute('DELETE FROM deliveries WHERE created<?',(int(time.time())-86400,))
                rows=stats(db,stream['id']); groups={}
                for r in rows:
                    if r['active'] and r['offer_active']: groups.setdefault(r['offer_id'],[]).append(r)
                chosen=[]
                for variants in groups.values():
                    score=lambda r:(r['clicks']+1)/(r['impressions']+20)
                    r=min(variants,key=lambda r:r['impressions']) if random.random()<.2 else max(variants,key=score)
                    token=secrets.token_urlsafe(24)
                    db.execute('INSERT INTO deliveries VALUES(?,?,?,?)',(token,visitor,r['id'],int(time.time())))
                    chosen.append(dict(title=r['title'],description=r['description'],image=display_image(r['image']),token=token,score=score(r)))
                chosen.sort(key=lambda r:r['score'],reverse=True)
                for r in chosen: r.pop('score')
            return self.send(200,chosen,headers={'Set-Cookie':f'visitor={visitor}; Path=/; HttpOnly; SameSite=Lax; Max-Age=31536000{SECURE}'})
        if path=='/go':
            token=parse_qs(urlparse(self.path).query).get('t',[''])[0]
            with connect() as db:
                r=db.execute('SELECT d.*,o.url,o.id AS offer_id,o.stream_id,o.binom_campaign_id,s.slug FROM deliveries d JOIN variants v ON v.id=d.variant_id JOIN offers o ON o.id=v.offer_id JOIN streams s ON s.id=o.stream_id WHERE d.id=? AND d.visitor=? AND d.created>?',(token,self.cookie('visitor'),int(time.time())-86400)).fetchone()
                if not r: return self.send(404,(ROOT/'public'/'unavailable.html').read_bytes(),'text/html; charset=utf-8')
                day=time.strftime('%Y-%m-%d',time.gmtime())
                for kind in ('impression','click'): db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(r['visitor'],r['variant_id'],day,kind))
                click_id=uuid.uuid4().hex
                db.execute('INSERT INTO outbound_clicks VALUES(?,?,?,?,?,?,?,?)',(click_id,r['visitor'],r['stream_id'],r['offer_id'],r['variant_id'],r['binom_campaign_id'],int(time.time()),day))
                target=attributed_url(r['url'],r['stream_id'],r['offer_id'],r['variant_id'],click_id) if r['binom_campaign_id'] else r['url']+('?stream='+r['slug'] if r['url']=='/demo-offer.html' else '')
            return self.send(302,b'',headers={'Location':target})
        if path=='/api/affiliates':
            actor=self.admin()
            if not actor:return self.send(401,{'error':'Войдите в админку'})
            with connect() as db:rows=[dict(r) for r in db.execute('SELECT a.*,u.name AS owner_name FROM affiliate_networks a JOIN users u ON u.id=a.user_id WHERE (?=1 OR a.user_id=?) ORDER BY a.name',(int(actor['role']=='admin'),actor['id']))]
            for row in rows:row['status_policy']=json.loads(row['status_policy'])
            return self.send(200,rows)
        if path in ('/api/binom/report','/api/report/preferences'):
            actor=self.admin()
            if not actor:return self.send(401,{'error':'Войдите в админку'})
            if path=='/api/report/preferences':
                with connect() as db:r=db.execute('SELECT preferences FROM report_preferences WHERE user_id=?',(actor['id'],)).fetchone()
                return self.send(200,dict(preferences=json.loads(r[0]) if r else {}))
            query=parse_qs(urlparse(self.path).query)
            try:
                start,end=date_range(query.get('from',[''])[0],query.get('to',[''])[0])
                if start or end:
                    start=start or '1970-01-01';end=end or time.strftime('%Y-%m-%d',time.gmtime())
                    date_range(start,end)
                with connect() as db:result=read_report(db,DATA,actor,start or '',end or '',query.get('stream_id',[''])[0])
                # Exact period metrics are computed by Binom asynchronously, never by the browser.
                result['pending_jobs']=[]
                with connect() as db:
                    for uid in {b['user_id'] for b in result['bindings']}:
                        result['pending_jobs'].extend(dict(job_id=r['id'],status=r['status']) for r in db.execute("SELECT id,status FROM binom_jobs WHERE user_id=? AND status IN ('queued','running')",(uid,)))
                if SYNC and start:
                    for uid in {b['user_id'] for b in result['bindings']}:
                        subset=[b for b in result['bindings'] if b['user_id']==uid]
                        found={r['offer_id']:r['updated'] for r in result['rows'] if r['scope']=='campaign' and r['user_id']==uid}
                        with connect() as db:interval=db.execute('SELECT t.sync_interval_minutes,t.active FROM trackers t JOIN users u ON u.id=t.user_id WHERE t.user_id=? AND u.active=1',(uid,)).fetchone()
                        if any(b['offer_id'] not in found or time.time()-found[b['offer_id']]>(interval[0] if interval else 15)*60 for b in subset):
                            cfg=read_settings(DATA,uid)
                            with connect() as db:st=db.execute('SELECT last_status,next_due FROM binom_sync_state WHERE user_id=?',(uid,)).fetchone()
                            backoff=st and st['last_status']=='error' and st['next_due']>time.time()
                            if interval and interval['active'] and cfg['origin'] and cfg['api_key'] and not backoff:result['pending_jobs'].append(SYNC.submit(uid,actor['id'],start,end))
                result['period']['from']=result['period'].pop('from_')
                return self.send(200,result)
            except ValueError as e:return self.send(400,{'error':str(e)})
        if path in ('/api/trackers','/api/binom/logs'):
            actor=self.admin()
            if not actor: return self.send(401,{'error':'Войдите в админку'})
            with connect() as db:
                if path=='/api/trackers':
                    rows=[dict(r) for r in db.execute('SELECT t.*,u.name AS owner_name FROM trackers t JOIN users u ON u.id=t.user_id WHERE (?=1 OR t.user_id=?) ORDER BY t.created DESC',(int(actor['role']=='admin'),actor['id']))]
                    for row in rows:
                        row['settings']=public_settings(read_settings(DATA,row['user_id']),True)
                        last=db.execute("SELECT status,created,message FROM tracker_logs WHERE user_id=? AND operation='connection' ORDER BY id DESC LIMIT 1",(row['user_id'],)).fetchone()
                        row['last_check']=dict(last) if last else None
                        sync=db.execute('SELECT * FROM binom_sync_state WHERE user_id=?',(row['user_id'],)).fetchone()
                        row['sync']=dict(sync) if sync else None
                    return self.send(200,rows)
                target=parse_qs(urlparse(self.path).query).get('user_id',[actor['id']])[0]
                if target!=actor['id'] and actor['role']!='admin': return self.send(403,{'error':'Нет доступа к журналу другого пользователя'})
                rows=[dict(r) for r in db.execute('SELECT l.id,l.created,l.operation,l.status,l.message,l.duration_ms,u.name AS actor_name FROM tracker_logs l JOIN users u ON u.id=l.actor_id WHERE l.user_id=? ORDER BY l.id DESC LIMIT 100',(target,))]
                return self.send(200,rows)
        if path=='/api/admin':
            actor=self.admin()
            if not actor: return self.send(401,{'error':'Войдите в админку'})
            query=parse_qs(urlparse(self.path).query)
            try: start,end=date_range(query.get('from',[''])[0],query.get('to',[''])[0])
            except ValueError as e: return self.send(400,{'error':str(e)})
            config=read_settings(DATA,actor['id'])
            with connect() as db:
                streams=[dict(r) for r in db.execute('SELECT s.id,s.name,s.slug,s.active,s.owner_id,u.name AS owner_name,count(DISTINCT v.visitor) AS visitors FROM streams s JOIN users u ON u.id=s.owner_id LEFT JOIN stream_visits v ON v.stream_id=s.id AND (? IS NULL OR v.day>=?) AND (? IS NULL OR v.day<=?) WHERE (?=1 OR s.owner_id=?) GROUP BY s.id ORDER BY s.rowid',(start,start,end,end,int(actor['role']=='admin'),actor['id']))]
                allowed={s['id'] for s in streams}
                owners={s['id']:s['owner_id'] for s in streams}
                owner_configs={uid:read_settings(DATA,uid) for uid in set(owners.values())}
                offers=[dict(r) for r in db.execute('SELECT o.*,a.name AS affiliate_name FROM offers o LEFT JOIN affiliate_networks a ON a.id=o.affiliate_id ORDER BY o.rowid DESC') if r['stream_id'] in allowed]
                for o in offers:
                    for k in ('titles','descriptions','images'): o[k]=json.loads(o[k])
                    o['preview_image']=display_image(o['images'][0]) if o['images'] else ''
                    o['binom_tokens']=json.loads(o['binom_tokens'])
                    o['binom_effective_tokens']=o['binom_tokens'] if o['binom_override_tokens'] else owner_configs[owners[o['stream_id']]]['tokens']
                rows=[r for r in stats(db,start=start,end=end) if r['stream_id'] in allowed]
                daily=[dict(r) for r in db.execute('''SELECT o.stream_id,e.day,
                    count(CASE WHEN e.kind='impression' THEN 1 END) AS impressions,
                    count(CASE WHEN e.kind='click' THEN 1 END) AS clicks
                    FROM events e JOIN variants v ON v.id=e.variant_id JOIN offers o ON o.id=v.offer_id
                    WHERE (? IS NULL OR e.day>=?) AND (? IS NULL OR e.day<=?)
                    GROUP BY o.stream_id,e.day ORDER BY e.day DESC''',(start,start,end,end)) if r['stream_id'] in allowed]
                variant_daily=[dict(r) for r in db.execute('''SELECT o.stream_id,o.id AS offer_id,v.id AS variant_id,e.day,
                    count(CASE WHEN e.kind='impression' THEN 1 END) AS impressions,
                    count(CASE WHEN e.kind='click' THEN 1 END) AS clicks
                    FROM events e JOIN variants v ON v.id=e.variant_id JOIN offers o ON o.id=v.offer_id
                    JOIN streams s ON s.id=o.stream_id
                    WHERE (? IS NULL OR e.day>=?) AND (? IS NULL OR e.day<=?) AND (?=1 OR s.owner_id=?)
                    GROUP BY v.id,e.day ORDER BY e.day DESC''',(start,start,end,end,int(actor['role']=='admin'),actor['id']))]
                users=public_users(db) if actor['role']=='admin' else []
                for user in users: user['binom_settings']=public_settings(read_settings(DATA,user['id']),True)
            token_fields=[dict(parameter=key,label=label,name=name,placeholder='') for key,label,name in TOKEN_FIELDS]
            return self.send(200,dict(offers=offers,variants=rows,streams=streams,users=users,me=actor,daily=daily,variant_daily=variant_daily,period=dict(start=start,end=end,timezone='UTC'),binom_token_fields=token_fields,binom_settings=public_settings(config,True)))
        if path=='/health': return self.send(200,{'ok':True})
        if path.startswith('/s/'):
            path='/'
        if path.startswith('/uploads/'): file=DATA / path.lstrip('/')
        else: file=ROOT/'public'/('index.html' if path=='/' else 'admin.html' if path=='/admin' else path.lstrip('/'))
        allowed=DATA/'uploads' if path.startswith('/uploads/') else ROOT/'public'
        if not file.resolve().is_relative_to(allowed.resolve()) or not file.is_file(): return self.send(404,'Не найдено','text/plain; charset=utf-8')
        import mimetypes
        cache={'Cache-Control':'public, max-age=31536000, immutable'} if path.startswith('/uploads/') else None
        mime='image/webp' if file.suffix.lower()=='.webp' else mimetypes.guess_type(file)[0] or 'application/octet-stream'
        return self.send(200,file.read_bytes(),mime,headers=cache)
    def do_POST(self):
        origin=self.headers.get('Origin')
        if origin and origin!=(PUBLIC_ORIGIN or f'http://{self.headers.get("Host")}'): return self.send(403,{'error':'Недопустимый источник запроса'})
        try:
            size=int(self.headers.get('Content-Length',0))
            if size>8*1024*1024: return self.send(413,{'error':'Файл слишком большой'})
            data=json.loads(self.rfile.read(size) or b'{}')
            path=urlparse(self.path).path
            if path=='/api/auth/link/start':
                ip=self.headers.get('X-Real-IP',self.client_address[0]) if PUBLIC_ORIGIN else self.client_address[0]
                attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if time.time()-t<900]
                if len(attempts)>=10:return self.send(429,{'error':'Слишком много попыток. Попробуйте позже.'})
                LOGIN_ATTEMPTS[ip]=attempts+[time.time()]
            if path.startswith('/api/auth/'):
                if not SSO:return self.send(404,{'error':'Вход OpsVault не включён'})
                return SSO.handle(self,path,data)
            if path=='/api/logout':
                raw=self.cookie('admin') or ''
                with connect() as db:
                    row=db.execute('SELECT s.csrf,u.local_login_enabled FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),)).fetchone()
                if row and not row['local_login_enabled']:
                    if not SSO:return self.send(503,{'error':'SSO временно отключён'})
                    SSO.csrf(self,{'csrf':row['csrf']},data)
                if SSO:SSO.drop_local(raw)
                with connect() as db:db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(raw.encode()).hexdigest(),))
                return self.send(200,{'ok':True},headers={'Set-Cookie':f'admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax{SECURE}'})
            if path=='/api/login':
                now=time.time()
                ip=self.headers.get('X-Real-IP',self.client_address[0]) if PUBLIC_ORIGIN else self.client_address[0]
                attempts=[t for t in LOGIN_ATTEMPTS.get(ip,[]) if now-t<900]
                if len(attempts)>=10: return self.send(429,{'error':'Слишком много попыток. Попробуйте через 15 минут.'})
                LOGIN_ATTEMPTS[ip]=attempts+[now]
                username=str(data.get('username','admin')).strip().lower();password=str(data.get('password',''))
                if len(password)>256: return self.send(401,{'error':'Неверный логин или пароль'})
                with connect() as db: user=db.execute('SELECT * FROM users WHERE username=? AND active=1',(username,)).fetchone()
                # Run the same password work for unknown accounts.
                digest=user['password_hash'] if user else '00'*16+'$'+'00'*32
                if not password_matches(password,digest) or not user or not user['local_login_enabled']: return self.send(401,{'error':'Неверный логин или пароль'})
                token=secrets.token_urlsafe(32)
                linked=self.is_linked(user['id'])
                if linked:
                    if not SSO:raise AuthError(503,'OpsVault отключён; вход связанного аккаунта приостановлен.')
                    SSO.attach_local_session(token,user['id'])
                with connect() as db:
                    db.execute('DELETE FROM sessions WHERE expires<=?',(int(time.time()),))
                    db.execute('INSERT INTO sessions(token_hash,user_id,expires,csrf) VALUES(?,?,?,?)',(hashlib.sha256(token.encode()).hexdigest(),user['id'],int(time.time())+SESSION_TTL,secrets.token_urlsafe(32)))
                LOGIN_ATTEMPTS.pop(ip,None)
                return self.send(200,{'ok':True},headers={'Set-Cookie':f'admin={token}; HttpOnly; SameSite=Lax; Path=/; Max-Age={SESSION_TTL}{SECURE}'})
            if path=='/api/impression':
                with connect() as db:
                    r=db.execute('SELECT * FROM deliveries WHERE id=? AND visitor=? AND created>?',(data.get('token'),self.cookie('visitor'),int(time.time())-86400)).fetchone()
                    if not r: return self.send(400,{'error':'Показ не найден'})
                    db.execute('INSERT OR IGNORE INTO events VALUES(?,?,?,?)',(r['visitor'],r['variant_id'],time.strftime('%Y-%m-%d',time.gmtime()),'impression'))
                return self.send(200,{'ok':True})
            actor=self.admin()
            if not actor: return self.send(401,{'error':'Войдите в админку'})
            if path=='/api/affiliate':
                aid=str(data.get('id') or '')
                with connect() as db:
                    previous=db.execute('SELECT * FROM affiliate_networks WHERE id=?',(aid,)).fetchone() if aid else None
                    if aid and not previous:raise ValueError('Партнёрка не найдена')
                    target=previous['user_id'] if previous else (data.get('user_id') or actor['id'])
                    if target!=actor['id'] and actor['role']!='admin':return self.send(403,{'error':'Нет доступа к чужой партнёрке'})
                    if not db.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone():raise ValueError('Пользователь не найден')
                    name=str(data.get('name','')).strip()
                    if not name or len(name)>100:raise ValueError('Название партнёрки: 1–100 символов')
                    policy=status_policy(data.get('status_policy',{}));aid=aid or uuid.uuid4().hex
                    db.execute('INSERT INTO affiliate_networks VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET name=excluded.name,status_policy=excluded.status_policy,active=excluded.active',(aid,target,name,json.dumps(policy,ensure_ascii=False),int(bool(data.get('active',True)))))
                return self.send(200,dict(id=aid))
            if path=='/api/report/preferences':
                preferences=data.get('preferences',{})
                if not isinstance(preferences,dict):raise ValueError('Некорректные настройки таблицы')
                payload=json.dumps(preferences,ensure_ascii=False)
                if len(payload.encode())>65536:raise ValueError('Настройки таблицы: максимум 64 КБ')
                with connect() as db:db.execute('INSERT OR REPLACE INTO report_preferences VALUES(?,?)',(actor['id'],payload))
                return self.send(200,dict(preferences=preferences))
            if path=='/api/binom/sync':
                target=data.get('user_id') or actor['id']
                if target!=actor['id'] and actor['role']!='admin':return self.send(403,{'error':'Нет доступа к чужому трекеру'})
                cfg=read_settings(DATA,target)
                if not cfg['origin'] or not cfg['api_key']:raise ValueError('Сначала сохраните адрес трекера и API-ключ')
                if not SYNC:return self.send(503,{'error':'Служба импорта не запущена'})
                return self.send(202,SYNC.submit(target,actor['id']))
            if path in ('/api/tracker/state','/api/tracker/delete'):
                target=data.get('user_id') or actor['id']
                if target!=actor['id'] and actor['role']!='admin':return self.send(403,{'error':'Нет доступа к чужому трекеру'})
                with connect() as db:
                    tracker=db.execute('SELECT * FROM trackers WHERE user_id=?',(target,)).fetchone()
                    if not tracker:raise ValueError('Трекер не найден')
                    if path=='/api/tracker/delete':
                        # Keep an empty settings file so legacy migration cannot restore deleted credentials.
                        save_settings(DATA,dict(origin='',api_key='',clear_api_key=True,tokens={}),target)
                        db.execute('DELETE FROM trackers WHERE user_id=?',(target,))
                    else:
                        if type(data.get('active')) is not bool:raise ValueError('Укажите состояние трекера')
                        db.execute('UPDATE trackers SET active=?,updated=? WHERE user_id=?',(int(data['active']),int(time.time()),target))
                    if path.endswith('/delete') or not data.get('active'):
                        db.execute("UPDATE binom_jobs SET status='cancelled',finished=?,error='Трекер отключён или удалён' WHERE user_id=? AND status IN ('queued','running')",(int(time.time()),target))
                        db.execute("UPDATE binom_bindings SET status=CASE WHEN last_success IS NULL THEN 'never' ELSE 'success' END,error='' WHERE user_id=? AND status='running'",(target,))
                        db.execute("UPDATE binom_sync_state SET last_status='inactive',error='' WHERE user_id=?",(target,))
                    else:db.execute('UPDATE binom_sync_state SET next_due=0 WHERE user_id=?',(target,))
                    db.execute('INSERT INTO tracker_logs(user_id,actor_id,created,operation,status,message) VALUES(?,?,?,?,?,?)',(target,actor['id'],int(time.time()),'settings','success','Трекер удалён; история сохранена' if path.endswith('/delete') else 'Трекер включён' if data['active'] else 'Трекер отключён; обновление остановлено'))
                if SYNC:SYNC.wake.set()
                return self.send(200,dict(ok=True))
            if path in ('/api/binom/settings','/api/binom/check'):
                target=data.get('user_id') or actor['id']
                if target!=actor['id'] and actor['role']!='admin': return self.send(403,{'error':'Нет доступа к настройкам другого пользователя'})
                with connect() as db:
                    if not db.execute('SELECT 1 FROM users WHERE id=?',(target,)).fetchone(): raise ValueError('Пользователь не найден')
                if path=='/api/binom/settings':
                    with connect() as db: previous=db.execute('SELECT name,sync_interval_minutes FROM trackers WHERE user_id=?',(target,)).fetchone()
                    name=str(data.get('name',previous['name'] if previous else 'Мой Binom')).strip()
                    if not name or len(name)>100: raise ValueError('Укажите название трекера до 100 символов')
                    interval=data.get('sync_interval_minutes',previous['sync_interval_minutes'] if previous else 15)
                    if type(interval) is not int or interval not in (5,15,30,60): raise ValueError('Частота обновления: 5, 15, 30 или 60 минут')
                    result=save_settings(DATA,data,target)
                    now=int(time.time())
                    with connect() as db:
                        db.execute("INSERT INTO trackers(id,user_id,name,kind,created,updated,sync_interval_minutes) VALUES(?,?,?,'binom',?,?,?) ON CONFLICT(user_id) DO UPDATE SET name=excluded.name,updated=excluded.updated,sync_interval_minutes=excluded.sync_interval_minutes",(uuid.uuid4().hex,target,name,now,now,interval))
                        db.execute('INSERT INTO tracker_logs(user_id,actor_id,created,operation,status,message) VALUES(?,?,?,?,?,?)',(target,actor['id'],now,'settings','success',f'Настройки трекера сохранены · обновление каждые {interval} мин'))
                        db.execute('UPDATE binom_sync_state SET next_due=0 WHERE user_id=?',(target,))
                    if SYNC:SYNC.wake.set()
                    return self.send(200,result)
                started=time.monotonic()
                try:
                    result=check_connection(DATA,target)
                except (ValueError,BinomError) as exc:
                    # Only our own client messages; never store API keys or remote response bodies.
                    message=str(exc) if isinstance(exc,BinomError) else 'Проверьте адрес трекера и API-ключ'
                    with connect() as db:
                        db.execute('INSERT INTO tracker_logs(user_id,actor_id,created,operation,status,message,duration_ms) VALUES(?,?,?,?,?,?,?)',(target,actor['id'],int(time.time()),'connection','error',message,int((time.monotonic()-started)*1000)))
                    raise
                with connect() as db:
                    db.execute('INSERT INTO tracker_logs(user_id,actor_id,created,operation,status,message,duration_ms) VALUES(?,?,?,?,?,?,?)',(target,actor['id'],int(time.time()),'connection','success','API-ключ принят',int((time.monotonic()-started)*1000)))
                return self.send(200,result)
            if path=='/api/logout':
                SESSIONS.pop(self.cookie('admin'),None)
                with connect() as db: db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256((self.cookie('admin') or '').encode()).hexdigest(),))
                return self.send(200,{'ok':True},headers={'Set-Cookie':f'admin=; Path=/; Max-Age=0; HttpOnly; SameSite=Strict{SECURE}'})
            if path=='/api/upload':
                raw=base64.b64decode(data['data'],validate=True)
                if len(raw)>5*1024*1024: raise ValueError('Максимальный размер картинки — 5 МБ')
                ext='png' if raw.startswith(b'\x89PNG\r\n\x1a\n') else 'jpg' if raw.startswith(b'\xff\xd8\xff') else 'webp' if raw.startswith(b'RIFF') and raw[8:12]==b'WEBP' else None
                if not ext: raise ValueError('Загрузите PNG, JPG или WebP')
                name=uuid.uuid4().hex+'.'+ext
                (DATA/'uploads'/name).write_bytes(raw)
                return self.send(200,{'url':'/uploads/'+name})
            with connect() as db:
                if path=='/api/user':
                    if actor['role']!='admin': return self.send(403,{'error':'Только администратор может управлять пользователями'})
                    uid=save_user(db,data,actor);db.commit()
                    return self.send(200,{'ok':True,'id':uid})
                elif path=='/api/stream':
                    if data.get('id') and not self.can_stream(db,data['id'],actor): return self.send(403,{'error':'Нет доступа к потоку'})
                    sid=save_stream(db,data,actor)
                    db.commit()
                    return self.send(200,{'ok':True,'id':sid})
                elif path=='/api/copy':
                    target=data.get('target_stream_id')
                    ids=data.get('offer_ids',[])
                    if not isinstance(ids,list) or not ids or len(ids)>100: raise ValueError('Выберите от 1 до 100 офферов')
                    if not db.execute('SELECT 1 FROM streams WHERE id=?',(target,)).fetchone(): raise ValueError('Поток назначения не найден')
                    if not self.can_stream(db,target,actor): return self.send(403,{'error':'Нет доступа к потоку назначения'})
                    source=[]
                    for oid in dict.fromkeys(ids):
                        row=db.execute('SELECT * FROM offers WHERE id=?',(oid,)).fetchone()
                        if not row: raise ValueError('Один из офферов больше не существует')
                        if not self.can_offer(db,oid,actor): return self.send(403,{'error':'Нет доступа к исходному офферу'})
                        if row['stream_id']==target: raise ValueError('Выберите другой поток назначения')
                        source.append(dict(row))
                    copied=[]
                    for offer in source:
                        for key in ('titles','descriptions','images'): offer[key]=json.loads(offer[key])
                        if db.execute('SELECT owner_id FROM streams WHERE id=?',(offer['stream_id'],)).fetchone()[0]!=db.execute('SELECT owner_id FROM streams WHERE id=?',(target,)).fetchone()[0]:offer['affiliate_id']=''
                        offer['stream_id']=target
                        offer['binom_campaign_id']=''
                        offer['binom_tokens']={}
                        offer['binom_override_tokens']=0
                        copied.append(save_offer(db,offer))
                    db.commit()
                    return self.send(200,{'ok':True,'count':len(copied),'ids':copied})
                elif path=='/api/offer':
                    oid=data.get('id')
                    if oid and not db.execute('SELECT 1 FROM offers WHERE id=?',(oid,)).fetchone(): return self.send(404,{'error':'Оффер не найден'})
                    if oid and not self.can_offer(db,oid,actor): return self.send(403,{'error':'Нет доступа к офферу'})
                    existing=db.execute('SELECT stream_id FROM offers WHERE id=?',(oid,)).fetchone() if oid else None
                    sid=data.get('stream_id') or (existing['stream_id'] if existing else 'main')
                    if not self.can_stream(db,sid,actor): return self.send(403,{'error':'Нет доступа к потоку'})
                    save_offer(db,data,oid)
                elif path=='/api/delete':
                    if not self.can_offer(db,data['id'],actor): return self.send(403,{'error':'Нет доступа к офферу'})
                    db.execute('DELETE FROM offers WHERE id=?',(data['id'],))
                else: return self.send(404,{'error':'Не найдено'})
            return self.send(200,{'ok':True})
        except AuthError as exc:return self.send(exc.status,{'error':str(exc)})
        except (ValueError,KeyError,TypeError,BinomError) as e: return self.send(400,{'error':str(e)})
        except Exception:
            import traceback
            traceback.print_exc()
            return self.send(500,{'error':'Ошибка сервера'})

def check_sso_background(uid):
    with connect() as db:
        row=db.execute('SELECT local_login_enabled,active FROM users WHERE id=?',(uid,)).fetchone()
        linked=linked_user(db,uid)
    if not row or not row['active']:raise ValueError('Пользователь отключён; импорт остановлен')
    if SSO:SSO.check_background(uid)
    elif row and (linked or not row['local_login_enabled']):raise ValueError('OpsVault отключён; импорт связанного аккаунта приостановлен')

if __name__=='__main__':
    if PUBLIC_ORIGIN and PASSWORD=='admin-local-2026': raise RuntimeError('Задайте ADMIN_PASSWORD для публичного сервера')
    init()
    if os.environ.get('SSO_ENABLED','0')=='1':
        SSO=OpsVaultBridge(connect,PUBLIC_ORIGIN,SECURE,password_matches,password_hasher=password_hash)
    SYNC=SyncService(connect,lambda:DATA,background_check=check_sso_background,auto=os.environ.get('BINOM_AUTO_SYNC','1')=='1')
    SYNC.start()
    port=int(os.environ.get('PORT','8080'))
    print(f'Domonetka: http://127.0.0.1:{port} | Admin: http://127.0.0.1:{port}/admin',flush=True)
    ThreadingHTTPServer(('127.0.0.1',port),Handler).serve_forever()
