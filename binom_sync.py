"""Read-only background imports. Binom computes metrics; SQLite stores replaceable snapshots."""
import hashlib, json, math, queue, threading, time, uuid
from datetime import datetime, timedelta, timezone
from binom import Client, BinomError, token_mapping
from binom_settings import read_settings

DDL = '''
CREATE TABLE IF NOT EXISTS binom_bindings(offer_id TEXT PRIMARY KEY,user_id TEXT NOT NULL,signature TEXT NOT NULL,campaign_id TEXT NOT NULL,stream_id TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'never',error TEXT NOT NULL DEFAULT '',last_attempt INTEGER,last_success INTEGER,last_full INTEGER,attribution_ready INTEGER NOT NULL DEFAULT 0,warning TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS binom_snapshots(offer_id TEXT NOT NULL,signature TEXT NOT NULL,day TEXT NOT NULL,scope TEXT NOT NULL,variant_id TEXT NOT NULL,metrics TEXT NOT NULL,statuses TEXT NOT NULL,updated INTEGER NOT NULL,PRIMARY KEY(offer_id,signature,day,scope,variant_id));
CREATE TABLE IF NOT EXISTS binom_columns(user_id TEXT PRIMARY KEY,columns_json TEXT NOT NULL,updated INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS binom_jobs(id TEXT PRIMARY KEY,user_id TEXT NOT NULL,actor_id TEXT NOT NULL,period_from TEXT NOT NULL DEFAULT '',period_to TEXT NOT NULL DEFAULT '',status TEXT NOT NULL,created INTEGER NOT NULL,started INTEGER,finished INTEGER,error TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS binom_jobs_queue ON binom_jobs(status,created);
CREATE TABLE IF NOT EXISTS binom_sync_state(user_id TEXT PRIMARY KEY,next_due INTEGER NOT NULL DEFAULT 0,last_started INTEGER,last_finished INTEGER,last_status TEXT NOT NULL DEFAULT 'never',error TEXT NOT NULL DEFAULT '',job_id TEXT);
CREATE TABLE IF NOT EXISTS report_preferences(user_id TEXT PRIMARY KEY,preferences TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS affiliate_networks(id TEXT PRIMARY KEY,user_id TEXT NOT NULL REFERENCES users(id),name TEXT NOT NULL,status_policy TEXT NOT NULL,active INTEGER NOT NULL DEFAULT 1);
'''

def init_schema(db): db.executescript(DDL)

def binding(offer, config):
    tokens=json.loads(offer['binom_tokens']) if offer['binom_override_tokens'] else config['tokens']
    signature=hashlib.sha256(json.dumps([offer['owner_id'],offer['stream_id'],offer['binom_campaign_id'],config['origin'],hashlib.sha256(config['api_key'].encode()).hexdigest(),tokens],sort_keys=True).encode()).hexdigest()
    return signature,tokens

def metrics(row):
    result={}
    for key,value in row.items():
        if key in ('level','name','entity_id','mark','marked_at','total_count','md5_name'): continue
        try:
            number=float(value)
            if math.isfinite(number):result[key]=number
        except (ValueError,TypeError):pass
    return result

def tree(rows):
    """Preserve ancestry across flat pagination; never sum parent and child metrics."""
    path=[]
    for row in rows:
        level=int(row['level'])
        if level<1 or level>len(path)+1:raise BinomError('Binom: неполная структура отчёта')
        path=path[:level-1]+[str(row.get('name',''))]
        yield level,tuple(path),row

def report(client,campaign,groups,start='',end=''):
    rows=[];offset=0;total=None
    for _ in range(2000):
        params={'ids[]':[campaign],'groupings[]':groups,'datePreset':'custom_time' if start else 'all_time','timezone':'UTC','sortColumn':'name','sortType':'asc','limit':250,'offset':offset}
        if start:params.update(dateFrom=start+' 00:00:00',dateTo=end+' 23:59:59')
        page=client.get('/public/api/v1/report/campaign',params)
        if not isinstance(page,dict) or not isinstance(page.get('report'),list):raise BinomError('Binom: неверный формат отчёта')
        batch=page['report'];count=int(page.get('totals',{}).get('total_count',len(batch)))
        if total is not None and total!=count:raise BinomError('Binom: отчёт изменился во время загрузки, повторите синхронизацию')
        total=count;rows.extend(batch);offset+=len(batch)
        if offset>=total:return rows,page.get('totals',{})
        if not batch:raise BinomError('Binom: неполная страница отчёта')
    raise BinomError('Binom: отчёт слишком большой для одного импорта')

def make_snapshots(campaign_rows,attr_rows,offer,day_key=None,attribution_ready=False,campaign_totals=None):
    snapshots={}
    def put(day,scope,variant,row):
        snapshots[(day,scope,variant)]={'metrics':metrics(row),'statuses':[]}
    for level,path,row in tree(campaign_rows):
        day=day_key or path[0]
        if level==1:put(day,'campaign','',row)
        if level==3 and float(row.get('leads',0)):
            snapshots[(day,'campaign','')]['statuses'].append(dict(one=path[1],two=path[2],leads=float(row['leads'])))
    # days/source/flow/banner/variant/status1/status2, or without days for exact period.
    shift=0 if day_key else 1
    for level,path,row in tree(attr_rows):
        if level<3+shift:continue
        offset=shift
        if path[offset:offset+3]!=('domonetka',offer['stream_id'],offer['id']):continue
        day=day_key or path[0]
        if level==3+shift:put(day,'attributed','',row)
        if level==4+shift:put(day,'variant',path[3+shift],row)
        if level==6+shift and float(row.get('leads',0)):
            status=dict(one=path[4+shift],two=path[5+shift],leads=float(row['leads']))
            snapshots[(day,'attributed','')]['statuses'].append(status)
            snapshots[(day,'variant',path[3+shift])]['statuses'].append(status)
    if day_key and (day_key,'campaign','') not in snapshots:
        put(day_key,'campaign','',campaign_totals or {})
    # Verified attribution with no matching traffic is a genuine zero, not a missing import.
    if attribution_ready:
        for day,scope,variant in list(snapshots):
            if scope=='campaign' and (day,'attributed','') not in snapshots:
                zero={k:0 for k in snapshots[(day,scope,variant)]['metrics']}
                put(day,'attributed','',zero)
    return snapshots

def status_policy(value):
    if not isinstance(value,dict):raise ValueError('Некорректная схема статусов')
    axis=value.get('axis','one')
    if axis not in ('one','two'):raise ValueError('Уровень статуса: one или two')
    result={'axis':axis};seen=set()
    for category in ('approved','pending','rejected'):
        items=value.get(category,[])
        if not isinstance(items,list) or len(items)>100 or any(not isinstance(s,str) or not s.strip() or len(s)>100 for s in items):raise ValueError('Некорректный список статусов')
        items=list(dict.fromkeys(s.strip() for s in items))
        if seen.intersection(items):raise ValueError('Статус может относиться только к одной категории')
        seen.update(items);result[category]=items
    return result

def classify(statuses,policy,leads):
    result={k:None for k in ('approved','pending','rejected','unknown','approval_rate')}
    if not any(policy.get(k) for k in ('approved','pending','rejected')):return result
    result.update(approved=0,pending=0,rejected=0,unknown=0)
    for status in statuses:
        category=next((k for k in ('approved','pending','rejected') if status[policy.get('axis','one')] in policy.get(k,[])),'unknown')
        result[category]+=status['leads']
    result['unknown']+=max(0,(leads or 0)-sum(result[k] for k in ('approved','pending','rejected','unknown')))
    result['approval_rate']=100*result['approved']/leads if leads else None
    return result

class SyncService:
    def __init__(self,connect,data,auto=True,background_check=None):
        self.connect=connect;self.data=data;self.auto=auto;self.background_check=background_check;self.wake=threading.Event();self.stop=threading.Event()
    def start(self):
        with self.connect() as db:
            db.execute("UPDATE binom_jobs SET status='error',finished=?,error='Импорт прерван перезапуском сервиса' WHERE status='running'",(int(time.time()),))
            db.execute("UPDATE binom_bindings SET status='error',error='Импорт прерван перезапуском сервиса' WHERE status='running'")
        threading.Thread(target=self.loop,daemon=True,name='binom-import').start()
    def submit(self,user_id,actor_id,start='',end=''):
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM trackers t JOIN users u ON u.id=t.user_id WHERE t.user_id=? AND t.active=1 AND u.active=1',(user_id,)).fetchone():raise ValueError('Трекер отключён или удалён')
            old=db.execute("SELECT id,status FROM binom_jobs WHERE user_id=? AND period_from=? AND period_to=? AND status IN ('queued','running') ORDER BY created LIMIT 1",(user_id,start,end)).fetchone()
            if old:return dict(job_id=old['id'],status=old['status'])
            if db.execute("SELECT count(*) FROM binom_jobs WHERE user_id=? AND status='queued'",(user_id,)).fetchone()[0]>=20:raise ValueError('Слишком много отчётов в очереди')
            jid=uuid.uuid4().hex
            db.execute('INSERT INTO binom_jobs(id,user_id,actor_id,period_from,period_to,status,created) VALUES(?,?,?,?,?,?,?)',(jid,user_id,actor_id,start,end,'queued',int(time.time())))
        self.wake.set();return dict(job_id=jid,status='queued')
    def log(self,uid,actor,status,message,duration=0):
        with self.connect() as db:db.execute('INSERT INTO tracker_logs(user_id,actor_id,created,operation,status,message,duration_ms) VALUES(?,?,?,?,?,?,?)',(uid,actor,int(time.time()),'sync',status,message,duration))
    def loop(self):
        while not self.stop.is_set():
            try:
                if self.auto:
                    with self.connect() as db:
                        users=db.execute("SELECT t.user_id FROM trackers t JOIN users u ON u.id=t.user_id LEFT JOIN binom_sync_state st ON st.user_id=t.user_id WHERE u.active=1 AND t.active=1 AND coalesce(st.next_due,0)<=?",(int(time.time()),)).fetchall()
                    for user in users:
                        cfg=read_settings(self.data(),user['user_id'])
                        if cfg['origin'] and cfg['api_key']:self.submit(user['user_id'],user['user_id'])
                with self.connect() as db:job=db.execute("SELECT * FROM binom_jobs WHERE status='queued' ORDER BY created,rowid LIMIT 1").fetchone()
                if job:self.run(dict(job));continue
            except Exception:
                # No credentials/remote responses in stdout. A transient DB failure must not kill scheduling.
                print('Binom scheduler: temporary internal error',flush=True)
            self.wake.wait(10);self.wake.clear()
    def run(self,job):
        uid=job['user_id'];actor=job['actor_id'];started=time.monotonic();now=int(time.time())
        with self.connect() as db:
            if not db.execute("UPDATE binom_jobs SET status='running',started=? WHERE id=? AND status='queued'",(now,job['id'])).rowcount:return
            db.execute("INSERT INTO binom_sync_state(user_id,last_started,last_status,job_id) VALUES(?,?,'running',?) ON CONFLICT(user_id) DO UPDATE SET last_started=excluded.last_started,last_status=excluded.last_status,job_id=excluded.job_id",(uid,now,job['id']))
        self.log(uid,actor,'running','Начата синхронизация '+(job['period_from']+' — '+job['period_to'] if job['period_from'] else 'всей статистики'))
        status='success';message='';success=0;errors=0
        try:
            cfg=read_settings(self.data(),uid);client=Client(cfg['origin'],cfg['api_key'])
            with self.connect() as db:
                if not db.execute('SELECT 1 FROM users WHERE id=? AND active=1',(uid,)).fetchone():raise ValueError('Пользователь отключён')
                offers=[dict(r) for r in db.execute("SELECT o.*,s.owner_id FROM offers o JOIN streams s ON s.id=o.stream_id WHERE s.owner_id=? AND o.binom_campaign_id!=''",(uid,))]
            if self.background_check:self.background_check(uid)
            catalogue=client.get('/public/api/v1/columns/report')
            columns=[]
            for group in ('default','custom'):
                for c in catalogue.get(group,[]):
                    columns.append(dict(user_id=uid,key=c.get('key') or c.get('uuid'),name=c.get('name',''),decimals=c.get('decimals',2),format=c.get('format','def'),custom=group=='custom'))
            if self.background_check:self.background_check(uid)
            with self.connect() as db:db.execute('INSERT OR REPLACE INTO binom_columns VALUES(?,?,?)',(uid,json.dumps(columns,ensure_ascii=False),int(time.time())))
            for offer in offers:
                try:self.import_offer(client,cfg,offer,job);success+=1
                except (ValueError,BinomError) as exc:
                    with self.connect() as db:
                        if db.execute("SELECT 1 FROM binom_jobs WHERE id=? AND status='cancelled'",(job['id'],)).fetchone():return
                    errors+=1;message=str(exc);self.log(uid,actor,'error',f"Кампания {offer['binom_campaign_id']}: {message}")
                    with self.connect() as db:db.execute("UPDATE binom_bindings SET status='error',error=? WHERE offer_id=?",(message,offer['id']))
            if errors:status='error';message=f'Успешно кампаний: {success}; ошибок: {errors}. '+message
            elif not offers:message='Нет баннеров с ID кампании Binom'
            else:message=f'Обновлено кампаний: {success}'
        except (ValueError,BinomError) as exc:status='error';message=str(exc)
        except Exception:status='error';message='Внутренняя ошибка импорта; сохранён предыдущий успешный снимок'
        finished=int(time.time())
        with self.connect() as db:
            interval=db.execute('SELECT sync_interval_minutes FROM trackers WHERE user_id=?',(uid,)).fetchone()
            if db.execute("SELECT 1 FROM binom_jobs WHERE id=? AND status='cancelled'",(job['id'],)).fetchone():return
            db.execute('UPDATE binom_jobs SET status=?,finished=?,error=? WHERE id=?',(status,finished,message if status=='error' else '',job['id']))
            db.execute('UPDATE binom_sync_state SET next_due=?,last_finished=?,last_status=?,error=? WHERE user_id=?',(finished+(interval[0] if interval else 15)*60,finished,status,message if status=='error' else '',uid))
        self.log(uid,actor,status,message,int((time.monotonic()-started)*1000))
    def import_offer(self,client,cfg,offer,job):
        signature,tokens=binding(offer,cfg);now=int(time.time());uid=offer['owner_id'];cid=offer['binom_campaign_id']
        with self.connect() as db:
            if job.get('id') and not db.execute("SELECT 1 FROM binom_jobs WHERE id=? AND status='running'",(job['id'],)).fetchone():raise BinomError('Задание импорта отменено')
            old=db.execute('SELECT * FROM binom_bindings WHERE offer_id=?',(offer['id'],)).fetchone()
            same=old and old['signature']==signature
            db.execute("INSERT INTO binom_bindings(offer_id,user_id,signature,campaign_id,stream_id,status,last_attempt) VALUES(?,?,?,?,?,'running',?) ON CONFLICT(offer_id) DO UPDATE SET user_id=excluded.user_id,signature=excluded.signature,campaign_id=excluded.campaign_id,stream_id=excluded.stream_id,status='running',error='',last_attempt=excluded.last_attempt,last_success=CASE WHEN signature=excluded.signature THEN last_success ELSE NULL END,last_full=CASE WHEN signature=excluded.signature THEN last_full ELSE NULL END",(offer['id'],uid,signature,cid,offer['stream_id'],now))
        if self.background_check:self.background_check(uid)
        if self.background_check:
            original_client=client
            check=self.background_check
            class CheckedClient:
                def get(self,path,*args,**kwargs):
                    check(uid)
                    return original_client.get(path,*args,**kwargs)
            client=CheckedClient()
        campaign=client.get('/public/api/v1/campaign/'+cid)
        ready=False;warning='Настройте все пять токенов для статистики витрины и вариаций'
        if len(tokens)==5:
            token_mapping(tokens,complete=True)
            source=client.get('/public/api/v1/traffic_source/'+str(campaign['trafficSourceId']))
            configured={int(t['id']):t.get('parameter') for t in source.get('tokens',[]) if str(t.get('id','')).isdigit()}
            ready=all(configured.get(slot)==key for key,slot in tokens.items())
            if not ready:warning='Номера токенов не совпадают с Parameter в источнике кампании'
            else:warning=''
        groups=[f'token_{tokens[k]}' for k in ('dm_source','dm_flow','dm_offer','dm_variant')] if ready else []
        full=not same or not old['last_full'] or now-old['last_full']>=7*86400
        if job['period_from']:
            start,end=job['period_from'],job['period_to'];key='range:'+start+':'+end
            cr,ct=report(client,cid,['campaign','conversionStatusOne','conversionStatusTwo'],start,end)
            ar,_=report(client,cid,[*groups,'conversionStatusOne','conversionStatusTwo'],start,end) if ready else ([],{})
            snapshots=make_snapshots(cr,ar,offer,key,ready,ct)
            crd,_=report(client,cid,['days','conversionStatusOne','conversionStatusTwo'],start,end)
            ard,_=report(client,cid,['days',*groups,'conversionStatusOne','conversionStatusTwo'],start,end) if ready else ([],{})
            snapshots.update(make_snapshots(crd,ard,offer,attribution_ready=ready))
        else:
            cr,ct=report(client,cid,['campaign','conversionStatusOne','conversionStatusTwo'])
            ar,_=report(client,cid,[*groups,'conversionStatusOne','conversionStatusTwo']) if ready else ([],{})
            snapshots=make_snapshots(cr,ar,offer,'all',ready,ct)
            today=datetime.now(timezone.utc).date();start='' if full else (today-timedelta(days=30)).isoformat();end='' if full else today.isoformat()
            cr,_=report(client,cid,['days','conversionStatusOne','conversionStatusTwo'],start,end)
            ar,_=report(client,cid,['days',*groups,'conversionStatusOne','conversionStatusTwo'],start,end) if ready else ([],{})
            snapshots.update(make_snapshots(cr,ar,offer,attribution_ready=ready))
        if self.background_check:self.background_check(uid)
        # Check current ownership and credentials again before committing data from slow remote requests.
        with self.connect() as db:
            current=db.execute('SELECT o.*,s.owner_id FROM offers o JOIN streams s ON s.id=o.stream_id WHERE o.id=?',(offer['id'],)).fetchone()
            if not db.execute('SELECT 1 FROM trackers t JOIN users u ON u.id=t.user_id WHERE t.user_id=? AND t.active=1 AND u.active=1',(uid,)).fetchone():raise BinomError('Трекер отключён или удалён; импорт остановлен')
            if job.get('id') and not db.execute("SELECT 1 FROM binom_jobs WHERE id=? AND status='running'",(job['id'],)).fetchone():raise BinomError('Задание импорта отменено')
            if not current or binding(dict(current),read_settings(self.data(),uid))[0]!=signature:raise BinomError('Настройки изменились во время импорта; выполните повторную синхронизацию')
            if job['period_from']:db.execute('DELETE FROM binom_snapshots WHERE offer_id=? AND signature=? AND (day=? OR day BETWEEN ? AND ?)',(offer['id'],signature,key,start,end))
            elif full:db.execute("DELETE FROM binom_snapshots WHERE offer_id=? AND signature=? AND day NOT LIKE 'range:%'",(offer['id'],signature))
            else:db.execute("DELETE FROM binom_snapshots WHERE offer_id=? AND signature=? AND (day='all' OR day BETWEEN ? AND ?)",(offer['id'],signature,start,end))
            for (day,scope,variant),payload in snapshots.items():
                db.execute('INSERT OR REPLACE INTO binom_snapshots VALUES(?,?,?,?,?,?,?,?)',(offer['id'],signature,day,scope,variant,json.dumps(payload['metrics']),json.dumps(payload['statuses']),now))
            db.execute("UPDATE binom_bindings SET status='success',error='',last_success=?,last_full=CASE WHEN ? THEN ? ELSE last_full END,attribution_ready=?,warning=? WHERE offer_id=?",(now,int(full and not job['period_from']),now,int(ready),warning,offer['id']))
            # Retain at most 20 exact selected-period caches per banner.
            stale=db.execute("SELECT DISTINCT day FROM binom_snapshots WHERE offer_id=? AND signature=? AND day LIKE 'range:%' ORDER BY updated DESC LIMIT -1 OFFSET 20",(offer['id'],signature)).fetchall()
            for row in stale:db.execute('DELETE FROM binom_snapshots WHERE offer_id=? AND signature=? AND day=?',(offer['id'],signature,row[0]))

def read_report(db,data,actor,start='',end='',stream_id=''):
    offers=[dict(r) for r in db.execute("SELECT o.*,s.owner_id FROM offers o JOIN streams s ON s.id=o.stream_id WHERE (?=1 OR s.owner_id=?) AND (?='' OR s.id=?) AND o.binom_campaign_id!=''",(int(actor['role']=='admin'),actor['id'],stream_id,stream_id))]
    rows=[];daily=[];bindings=[];uids=set();key='range:'+start+':'+end if start else 'all'
    for offer in offers:
        uid=offer['owner_id'];uids.add(uid);cfg=read_settings(data,uid);signature,_=binding(offer,cfg)
        stored=db.execute('SELECT * FROM binom_bindings WHERE offer_id=? AND signature=?',(offer['id'],signature)).fetchone()
        b=dict(stored) if stored else dict(offer_id=offer['id'],user_id=uid,campaign_id=offer['binom_campaign_id'],stream_id=offer['stream_id'],status='never',error='',last_attempt=None,last_success=None,attribution_ready=False,warning='')
        b.pop('signature',None);bindings.append(b)
        for snap in db.execute("SELECT * FROM binom_snapshots WHERE offer_id=? AND signature=? AND (day=? OR (day!='all' AND day NOT LIKE 'range:%' AND (?='' OR day>=?) AND (?='' OR day<=?)))",(offer['id'],signature,key,start,start,end,end)):
            row=dict(offer_id=offer['id'],stream_id=offer['stream_id'],campaign_id=offer['binom_campaign_id'],user_id=uid,scope=snap['scope'],variant_id=snap['variant_id'],day='period' if snap['day'].startswith('range:') else snap['day'],metrics=json.loads(snap['metrics']),statuses=json.loads(snap['statuses']),updated=snap['updated'],precision_warnings=[])
            affiliate=db.execute('SELECT name,status_policy FROM affiliate_networks WHERE id=? AND user_id=?',(offer.get('affiliate_id',''),uid)).fetchone()
            row['affiliate_id']=offer.get('affiliate_id','');row['affiliate_name']=affiliate['name'] if affiliate else ''
            row.update(classify(row['statuses'],json.loads(affiliate['status_policy']) if affiliate else {},row['metrics'].get('leads')))
            (rows if snap['day']==key else daily).append(row)
    columns=[];sync=[]
    for uid in uids:
        c=db.execute('SELECT columns_json FROM binom_columns WHERE user_id=?',(uid,)).fetchone()
        if c:columns.extend(json.loads(c[0]))
        st=db.execute('SELECT * FROM binom_sync_state WHERE user_id=?',(uid,)).fetchone()
        if st:sync.append(dict(st))
    return dict(rows=rows,daily=daily,columns=columns,bindings=bindings,sync=sync,period=dict(from_=start,to=end,timezone='UTC'))
