import unittest, tempfile, threading, urllib.request, http.cookiejar, json, base64
from pathlib import Path
import server

class AppTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp=tempfile.TemporaryDirectory();server.DATA=Path(cls.temp.name);(server.DATA/'uploads').mkdir();server.DB=server.DATA/'test.sqlite';server.init()
        cls.http=server.ThreadingHTTPServer(('127.0.0.1',0),server.Handler)
        cls.url='http://127.0.0.1:'+str(cls.http.server_port)
        threading.Thread(target=cls.http.serve_forever,daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.http.shutdown();cls.http.server_close();cls.temp.cleanup()
    def browser(self): return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    def test_tracker_lifecycle_permissions_and_restart(self):
        admin=self.browser();self.request(admin,'/api/login',{'password':server.PASSWORD})
        uid=self.request(admin,'/api/user',dict(username='lifecycle-owner',name='Lifecycle owner',role='user',active=True,password='lifecycle-test-password'))['id']
        buyer=self.browser();self.request(buyer,'/api/login',dict(username='lifecycle-owner',password='lifecycle-test-password'))
        self.request(buyer,'/api/binom/settings',dict(origin='https://tracker.example.com',api_key='lifecycle-secret',tokens={}))
        with server.connect() as db:
            db.execute("INSERT INTO binom_jobs(id,user_id,actor_id,status,created) VALUES('lifecycle-job',?,?,'queued',0)",(uid,uid))
            count=db.execute('SELECT count(*) FROM offers').fetchone()[0]
        self.request(buyer,'/api/tracker/state',dict(active=False))
        self.assertEqual(self.request(buyer,'/api/trackers')[0]['active'],0)
        with server.connect() as db:self.assertEqual(db.execute("SELECT status FROM binom_jobs WHERE id='lifecycle-job'").fetchone()[0],'cancelled')
        from binom_sync import SyncService
        with self.assertRaises(ValueError):SyncService(server.connect,lambda:server.DATA).submit(uid,uid)
        self.request(buyer,'/api/binom/settings',dict(origin='https://tracker.example.com',tokens={}))
        self.assertEqual(self.request(buyer,'/api/trackers')[0]['active'],0)
        self.request(admin,'/api/tracker/state',dict(user_id=uid,active=True))
        self.assertEqual(self.request(buyer,'/api/trackers')[0]['active'],1)
        for endpoint in ('/api/tracker/state','/api/tracker/delete'):
            with self.assertRaises(urllib.error.HTTPError) as err:self.request(buyer,endpoint,dict(user_id='admin',active=False))
            self.assertEqual(err.exception.code,403)
        self.request(buyer,'/api/tracker/delete',{})
        self.assertEqual(self.request(buyer,'/api/trackers'),[])
        from binom_settings import read_settings
        self.assertEqual(read_settings(server.DATA,uid)['api_key'],'')
        server.init()
        self.assertEqual(self.request(buyer,'/api/trackers'),[])
        with server.connect() as db:self.assertEqual(db.execute('SELECT count(*) FROM offers').fetchone()[0],count)
    def test_affiliate_lists_permissions_and_preferences(self):
        admin=self.browser();self.request(admin,'/api/login',{'password':server.PASSWORD})
        uid=self.request(admin,'/api/user',dict(username='pp-owner',name='PP owner',role='user',active=True,password='pp-test-password'))['id']
        buyer=self.browser();self.request(buyer,'/api/login',dict(username='pp-owner',password='pp-test-password'))
        body=dict(name='Network',status_policy=dict(axis='one',approved=['ok','paid'],pending=['new'],rejected=['no']))
        aid=self.request(buyer,'/api/affiliate',body)['id']
        mine=self.request(buyer,'/api/affiliates');self.assertEqual(len(mine),1);self.assertEqual(mine[0]['user_id'],uid)
        foreign=self.request(admin,'/api/affiliate',body)['id']
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request(buyer,'/api/affiliate',{**body,'id':foreign})
        self.assertEqual(exc.exception.code,403)
        self.request(admin,'/api/affiliate',{**body,'id':aid,'name':'Admin edit'})
        self.assertEqual(self.request(buyer,'/api/affiliates')[0]['name'],'Admin edit')
        sid=self.request(buyer,'/api/stream',dict(name='PP stream',slug='pp-stream'))['id']
        offer=dict(stream_id=sid,name='PP offer',url='https://example.com',titles=['A'],descriptions=['D'],images=['/assets/home.svg'],affiliate_id=foreign)
        with self.assertRaises(urllib.error.HTTPError):self.request(buyer,'/api/offer',offer)
        self.request(buyer,'/api/offer',{**offer,'affiliate_id':aid})
        self.request(buyer,'/api/report/preferences',dict(preferences={'visible':['cost','leads']},user_id='admin'))
        self.assertEqual(self.request(buyer,'/api/report/preferences')['preferences'],{'visible':['cost','leads']})
        self.assertNotEqual(self.request(admin,'/api/report/preferences')['preferences'],{'visible':['cost','leads']})
        self.assertEqual(self.request(buyer,'/api/binom/report')['rows'],[])
    def test_tracker_entity_logs_and_isolation(self):
        from unittest.mock import patch
        admin=self.browser();self.request(admin,'/api/login',{'password':server.PASSWORD})
        uid=self.request(admin,'/api/user',dict(username='tracker-owner',name='Tracker owner',role='user',active=True,password='tracker-test-password'))['id']
        buyer=self.browser();self.request(buyer,'/api/login',dict(username='tracker-owner',password='tracker-test-password'))
        body=dict(name='Personal tracker',origin='https://example.com',api_key='secret-test-key',tokens={},sync_interval_minutes=30)
        self.request(buyer,'/api/binom/settings',body)
        first=self.request(buyer,'/api/trackers');self.assertEqual(len(first),1)
        self.request(admin,'/api/binom/settings',{**body,'name':'Edited by admin','user_id':uid,'api_key':''})
        second=self.request(buyer,'/api/trackers');self.assertEqual(second[0]['id'],first[0]['id']);self.assertEqual(second[0]['name'],'Edited by admin')
        self.assertEqual(second[0]['sync_interval_minutes'],30)
        for invalid in (0,1,7,True,'15'):
            with self.assertRaises(urllib.error.HTTPError) as exc:self.request(buyer,'/api/binom/settings',{**body,'sync_interval_minutes':invalid})
            self.assertEqual(exc.exception.code,400)
        with patch('server.check_connection',return_value=dict(ok=True,message='Connected')):
            self.request(buyer,'/api/binom/check',{})
        with patch('server.check_connection',side_effect=server.BinomError('Binom: HTTP 401')):
            with self.assertRaises(urllib.error.HTTPError):self.request(buyer,'/api/binom/check',{})
        logs=self.request(buyer,'/api/binom/logs');self.assertEqual(logs[0]['status'],'error');self.assertEqual(logs[1]['status'],'success')
        self.assertEqual(len(logs),4);self.assertNotIn('secret-test-key',json.dumps(logs)+json.dumps(second))
        self.assertEqual(self.request(admin,'/api/binom/logs?user_id='+uid),logs)
        for path in ['/api/binom/logs?user_id=admin']:
            with self.assertRaises(urllib.error.HTTPError) as exc:self.request(buyer,path)
            self.assertEqual(exc.exception.code,403)
        self.assertTrue(all(t['user_id']==uid for t in second))
    def test_optimized_asset_preserves_variants(self):
        name='a'*32; original='/uploads/'+name+'.png'
        path=server.DATA/'uploads'/(name+'-optimized.webp')
        self.assertEqual(server.display_image(original),original)
        try:
            path.write_bytes(b'RIFFtestWEBP')
            self.assertEqual(server.display_image(original),'/uploads/'+path.name)
            self.assertEqual(server.display_image('https://example.com/'+name+'.png'),'https://example.com/'+name+'.png')
        finally:path.unlink()
    def request(self,b,path,data=None):
        r=b.open(urllib.request.Request(self.url+path,data=json.dumps(data).encode() if data is not None else None,headers={'Content-Type':'application/json'}));return json.loads(r.read())
    def test_binom_campaign_clicks_dates_and_copy(self):
        from urllib.parse import urlsplit, parse_qs
        b=self.browser();self.request(b,'/api/login',{'password':server.PASSWORD})
        sid=self.request(b,'/api/stream',dict(name='Binom pilot',slug='binom-pilot'))['id']
        oid_offer=dict(stream_id=sid,name='Banner',url='https://tracker.invalid/click?key=original&sub=x%2By#part',
            binom_campaign_id='123',titles=['A','B'],descriptions=['D'],images=['/assets/home.svg'])
        self.request(b,'/api/offer',oid_offer)
        offer=next(o for o in self.request(b,'/api/admin')['offers'] if o['stream_id']==sid)
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self,*args): return None
        browser=urllib.request.build_opener(NoRedirect(), *[h for h in b.handlers if isinstance(h,urllib.request.HTTPCookieProcessor)])
        token=self.request(b,'/api/feed?stream=binom-pilot')[0]['token']
        click_ids=[]
        for _ in range(2):
            with self.assertRaises(urllib.error.HTTPError) as exc: browser.open(self.url+'/go?t='+token)
            self.assertEqual(exc.exception.code,302)
            target=exc.exception.headers['Location'];q=parse_qs(urlsplit(target).query)
            self.assertEqual(q['key'],['original']);self.assertEqual(q['dm_flow'],[sid]);self.assertEqual(q['dm_offer'],[offer['id']])
            self.assertEqual(urlsplit(target).fragment,'part');click_ids.append(q['dm_click'][0])
        self.assertNotEqual(*click_ids)
        with server.connect() as db:
            clicks=list(db.execute('SELECT * FROM outbound_clicks WHERE offer_id=?',(offer['id'],)))
            self.assertEqual(len(clicks),2);self.assertTrue(all(c['campaign_id']=='123' for c in clicks))
            vid=clicks[0]['variant_id']
            db.execute('INSERT INTO events VALUES(?,?,?,?)',('dated',vid,'2026-01-01','impression'))
            db.execute('INSERT INTO events VALUES(?,?,?,?)',('dated',vid,'2026-01-01','click'))
        period=self.request(b,'/api/admin?from=2026-01-01&to=2026-01-01')
        self.assertEqual(next(r for r in period['daily'] if r['stream_id']==sid)['clicks'],1)
        self.assertEqual(sum(r['clicks'] for r in period['variants'] if r['stream_id']==sid),1)
        self.assertEqual(period['period']['timezone'],'UTC')
        with self.assertRaises(urllib.error.HTTPError) as exc:self.request(b,'/api/admin?from=bad')
        self.assertEqual(exc.exception.code,400)
        self.request(b,'/api/offer',{**offer,'name':'Edited'})
        self.assertEqual(next(o for o in self.request(b,'/api/admin')['offers'] if o['id']==offer['id'])['binom_campaign_id'],'123')
        result=self.request(b,'/api/copy',dict(offer_ids=[offer['id']],target_stream_id='main'))
        copy=next(o for o in self.request(b,'/api/admin')['offers'] if o['id']==result['ids'][0])
        self.assertEqual(copy['binom_campaign_id'],'')
        self.assertEqual(copy['binom_tokens'],{})
        self.request(b,'/api/delete',dict(id=copy['id']))
        self.request(b,'/api/delete',dict(id=offer['id']))
    def test_custom_binom_tokens_persist_and_validate(self):
        b=self.browser();self.request(b,'/api/login',{'password':server.PASSWORD})
        offer=dict(name='Custom tokens',url='https://tracker.invalid/click?key=original',binom_campaign_id='789',
            binom_tokens={'dm_source':9,'dm_flow':17,'dm_offer':4,'dm_variant':25,'dm_click':30},
            titles=['A'],descriptions=['D'],images=['/assets/home.svg'])
        self.request(b,'/api/offer',offer)
        data=self.request(b,'/api/admin');stored=next(o for o in data['offers'] if o['name']=='Custom tokens')
        self.assertEqual(stored['binom_tokens'],offer['binom_tokens'])
        self.assertEqual([f['parameter'] for f in data['binom_token_fields']],list(server.token_mapping(offer['binom_tokens'])))
        self.assertTrue(all(f['placeholder']=='' for f in data['binom_token_fields']))
        edit={**stored,'name':'Still custom'};edit.pop('binom_tokens')
        self.request(b,'/api/offer',edit)
        self.assertEqual(next(o for o in self.request(b,'/api/admin')['offers'] if o['id']==stored['id'])['binom_tokens'],offer['binom_tokens'])
        for mapping in ({'dm_variant':31},{'dm_variant':3,'dm_click':3}):
            with self.assertRaises(urllib.error.HTTPError) as exc:self.request(b,'/api/offer',{**stored,'binom_tokens':mapping})
            self.assertEqual(exc.exception.code,400)
        result=self.request(b,'/api/copy',dict(offer_ids=[stored['id']],target_stream_id=self.request(b,'/api/stream',dict(name='Token copy',slug='token-copy'))['id']))
        copy=next(o for o in self.request(b,'/api/admin')['offers'] if o['id']==result['ids'][0])
        self.assertEqual(copy['binom_tokens'],{})
        self.request(b,'/api/delete',dict(id=copy['id']));self.request(b,'/api/delete',dict(id=stored['id']))
    def test_platform_binom_defaults_and_access(self):
        from unittest.mock import patch
        admin=self.browser();self.request(admin,'/api/login',{'password':server.PASSWORD})
        mapping={'dm_source':5,'dm_flow':6,'dm_offer':7,'dm_variant':8,'dm_click':9}
        result=self.request(admin,'/api/binom/settings',dict(origin='https://tracker.invalid',api_key='secret-platform-test',tokens=mapping))
        self.assertTrue(result['has_api_key']);self.assertNotIn('secret-platform-test',json.dumps(result))
        offer=dict(name='Inherited Binom',url='https://tracker.invalid/click?key=example',binom_campaign_id='123',titles=['A'],descriptions=['D'],images=['/assets/home.svg'])
        self.request(admin,'/api/offer',offer)
        data=self.request(admin,'/api/admin');stored=next(o for o in data['offers'] if o['name']==offer['name'])
        self.assertEqual(stored['binom_effective_tokens'],mapping);self.assertFalse(stored['binom_override_tokens'])
        self.assertNotIn('secret-platform-test',json.dumps(data))
        custom={'dm_variant':27}
        self.request(admin,'/api/offer',{**stored,'binom_tokens':custom,'binom_override_tokens':True})
        data=self.request(admin,'/api/admin');stored=next(o for o in data['offers'] if o['id']==stored['id'])
        self.assertEqual(stored['binom_effective_tokens'],custom)
        self.request(admin,'/api/offer',{**stored,'binom_override_tokens':False})
        self.assertEqual(next(o for o in self.request(admin,'/api/admin')['offers'] if o['id']==stored['id'])['binom_effective_tokens'],mapping)
        self.request(admin,'/api/user',dict(username='binom-reader',name='Reader',password='reader-test-password',role='user',active=True))
        user=self.browser();self.request(user,'/api/login',dict(username='binom-reader',password='reader-test-password'))
        self.assertFalse(self.request(user,'/api/admin')['binom_settings']['has_api_key'])
        uid=self.request(user,'/api/admin')['me']['id']
        personal={'dm_variant':23}
        self.request(user,'/api/binom/settings',dict(origin='https://personal.invalid',api_key='personal-private',tokens=personal))
        self.assertEqual(self.request(user,'/api/admin')['binom_settings']['tokens'],personal)
        self.assertEqual(self.request(admin,'/api/admin')['binom_settings']['tokens'],mapping)
        for path in ('/api/binom/settings','/api/binom/check'):
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(user,path,{'user_id':'admin'})
            self.assertEqual(e.exception.code,403)
        self.request(admin,'/api/binom/settings',dict(user_id=uid,origin='https://personal.invalid',tokens={'dm_variant':24}))
        self.assertEqual(self.request(user,'/api/admin')['binom_settings']['tokens'],{'dm_variant':24})
        data=self.request(admin,'/api/admin')
        self.assertNotIn('personal-private',json.dumps(data))
        self.assertTrue(next(u for u in data['users'] if u['id']==uid)['binom_settings']['has_api_key'])
        sid=self.request(user,'/api/stream',dict(name='Personal Binom',slug='personal-binom'))['id']
        self.request(user,'/api/offer',{**offer,'name':'Personal inherited','stream_id':sid})
        data=self.request(admin,'/api/admin');owned=next(o for o in data['offers'] if o['name']=='Personal inherited')
        self.assertEqual(owned['binom_effective_tokens'],{'dm_variant':24})
        self.request(user,'/api/delete',dict(id=owned['id']))
        with patch('binom_settings.Client') as client:
            client.return_value.get.return_value=[]
            self.assertTrue(self.request(admin,'/api/binom/check',{})['ok'])
        with self.assertRaises(urllib.error.HTTPError) as e:user.open(self.url+'/binom-settings.json')
        self.assertEqual(e.exception.code,404)
        self.request(admin,'/api/delete',dict(id=stored['id']))
        self.request(admin,'/api/binom/settings',dict(origin='',clear_api_key=True,tokens={}))
    def test_sessions_persist_renew_expire_and_logout(self):
        import time,hashlib
        b=self.browser();self.request(b,'/api/login',{'password':server.PASSWORD})
        jar=next(h.cookiejar for h in b.handlers if isinstance(h,urllib.request.HTTPCookieProcessor))
        token=next(c.value for c in jar if c.name=='admin');digest=hashlib.sha256(token.encode()).hexdigest()
        self.assertGreater(next(c.expires for c in jar if c.name=='admin'),time.time()+29*86400)
        server.SESSIONS.clear();server.init()
        self.assertEqual(self.request(b,'/api/admin')['me']['id'],'admin')
        with server.connect() as db:
            self.assertIsNone(db.execute('SELECT 1 FROM sessions WHERE token_hash=?',(token,)).fetchone())
            db.execute('UPDATE sessions SET expires=? WHERE token_hash=?',(int(time.time())+86400,digest))
        response=b.open(self.url+'/api/admin');self.assertIn('Max-Age=2592000',response.headers['Set-Cookie']);response.read()
        with server.connect() as db:
            self.assertGreater(db.execute('SELECT expires FROM sessions WHERE token_hash=?',(digest,)).fetchone()[0],time.time()+29*86400)
            db.execute('UPDATE sessions SET expires=? WHERE token_hash=?',(int(time.time())-1,digest))
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/admin')
        self.assertEqual(e.exception.code,401)
        self.request(b,'/api/login',{'password':server.PASSWORD});self.request(b,'/api/logout',{})
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/admin')
        self.assertEqual(e.exception.code,401)
    def test_tracking_deduplicates_and_redirects(self):
        b=self.browser();feed=self.request(b,'/api/feed');self.assertEqual(len(feed),6);token=feed[0]['token']
        for _ in range(2): self.request(b,'/api/impression',{'token':token})
        for _ in range(2): self.assertEqual(b.open(self.url+'/go?t='+token).status,200)
        with server.connect() as db:
            vid=db.execute('SELECT variant_id FROM deliveries WHERE id=?',(token,)).fetchone()[0]
            self.assertEqual(db.execute('SELECT count(*) FROM events WHERE variant_id=?',(vid,)).fetchone()[0],2)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(self.browser(),'/api/impression',{'token':token})
        self.assertEqual(e.exception.code,400)
    def test_admin_auth_combinations_pause_and_preservation(self):
        b=self.browser()
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/admin')
        self.assertEqual(e.exception.code,401)
        self.request(b,'/api/login',{'password':server.PASSWORD})
        offer=dict(name='Test',url='https://example.com',active=True,titles=['A','B','A'],descriptions=['C','D'],images=['/assets/home.svg','/assets/travel.svg'])
        self.request(b,'/api/offer',offer);data=self.request(b,'/api/admin');o=next(x for x in data['offers'] if x['name']=='Test');oid=o['id']
        ids={v['id'] for v in data['variants'] if v['offer_id']==oid};self.assertEqual(len(ids),8)
        self.request(b,'/api/offer',{**offer,'id':oid,'titles':['A']});data=self.request(b,'/api/admin');self.assertEqual(sum(v['active'] for v in data['variants'] if v['offer_id']==oid),4)
        self.request(b,'/api/offer',{**offer,'id':oid,'active':False});self.assertEqual(len(self.request(b,'/api/feed')),6)
        data=self.request(b,'/api/admin');self.assertEqual(ids,{v['id'] for v in data['variants'] if v['offer_id']==oid})
        self.request(b,'/api/delete',{'id':oid});self.assertFalse(any(o['id']==oid for o in self.request(b,'/api/admin')['offers']))
    def test_limits_and_csrf(self):
        b=self.browser();self.request(b,'/api/login',{'password':server.PASSWORD})
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/offer',dict(name='x',url='javascript:alert(1)',titles=['a'],descriptions=['b'],images=['/a.png']))
        self.assertEqual(e.exception.code,400)
        req=urllib.request.Request(self.url+'/api/login',data=b'{}',headers={'Origin':'https://evil.example','Content-Type':'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as e:b.open(req)
        self.assertEqual(e.exception.code,403)
        with self.assertRaises(urllib.error.HTTPError):b.open(self.url+'/%2e%2e/server.py')
    def test_upload(self):
        b=self.browser();self.request(b,'/api/login',{'password':server.PASSWORD})
        raw=base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a1a8AAAAASUVORK5CYII=')
        data=self.request(b,'/api/upload',{'data':base64.b64encode(raw).decode()})
        self.assertEqual(b.open(self.url+data['url']).read(),raw)
        self.assertEqual(b.open(self.url+data['url']).headers['Cache-Control'],'public, max-age=31536000, immutable')
        self.assertEqual(b.open(self.url+'/api/admin').headers['Cache-Control'],'no-store')
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/upload',{'data':base64.b64encode(b'<svg onload="alert(1)"></svg>').decode()})
        self.assertEqual(e.exception.code,400)
    def test_stream_isolation_copy_and_pause(self):
        b=self.browser();self.request(b,'/api/login',{'password':server.PASSWORD})
        source=self.request(b,'/api/stream',dict(name='Source',slug='qa-source',country='DE',active=True))['id']
        target=self.request(b,'/api/stream',dict(name='Target',slug='qa-target',country='FR',active=True))['id']
        offer=dict(name='Country offer',url='/demo-offer.html',active=True,titles=['Only source'],descriptions=['Description'],images=['/assets/home.svg'],stream_id=source)
        self.request(b,'/api/offer',offer)
        feed=self.request(b,'/api/feed?stream=qa-source');self.assertEqual(len(feed),1)
        self.assertIn('stream=qa-source',b.open(self.url+'/go?t='+feed[0]['token']).url)
        data=self.request(b,'/api/admin');original=next(o for o in data['offers'] if o['stream_id']==source)
        result=self.request(b,'/api/copy',{'offer_ids':[original['id']], 'target_stream_id':target});self.assertEqual(result['count'],1)
        data=self.request(b,'/api/admin');copied=next(o for o in data['offers'] if o['stream_id']==target)
        self.assertNotEqual(original['id'],copied['id']);self.assertEqual(original['titles'],copied['titles'])
        self.assertEqual(sum(v['clicks'] for v in data['variants'] if v['stream_id']==source),1)
        self.assertEqual(sum(v['clicks']+v['impressions'] for v in data['variants'] if v['stream_id']==target),0)
        self.request(b,'/api/offer',{**offer,'id':original['id'],'titles':['Changed source']})
        self.assertEqual(self.request(b,'/api/feed?stream=qa-target')[0]['title'],'Only source')
        self.assertEqual(len(self.request(b,'/api/feed')),6)
        self.assertEqual(b.open(self.url+'/s/qa-target').status,200)
        data=self.request(b,'/api/admin');self.assertEqual(next(s for s in data['streams'] if s['id']==target)['visitors'],1)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/feed?stream=does-not-exist')
        self.assertEqual(e.exception.code,404)
        self.request(b,'/api/stream',dict(id=target,name='Target',slug='qa-target',country='FR',active=False))
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/feed?stream=qa-target')
        self.assertEqual(e.exception.code,410)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(b,'/api/stream',dict(id=source,name='Source',slug='changed',country='DE'))
        self.assertEqual(e.exception.code,400)
        with self.assertRaises(urllib.error.HTTPError):self.request(b,'/api/copy',{'offer_ids':[original['id'],'nonexistent'],'target_stream_id':target})
        self.assertEqual(len([o for o in self.request(b,'/api/admin')['offers'] if o['stream_id']==target]),1)
    def test_legacy_migration_preserves_data(self):
        import sqlite3
        previous=server.DB;legacy=server.DATA/'legacy.sqlite'
        try:
            with sqlite3.connect(legacy) as db:
                db.executescript('''CREATE TABLE offers(id TEXT PRIMARY KEY,name TEXT,url TEXT,active INTEGER,titles TEXT,descriptions TEXT,images TEXT);
                CREATE TABLE visits(visitor TEXT,day TEXT,PRIMARY KEY(visitor,day));
                INSERT INTO offers VALUES('old','Old offer','https://example.com',1,'["A"]','["B"]','["/assets/home.svg"]');
                INSERT INTO visits VALUES('old-visitor','2026-10-07');''')
            db.close();server.DB=legacy;server.init();server.init()
            with server.connect() as db:
                self.assertEqual(db.execute('SELECT stream_id FROM offers WHERE id="old"').fetchone()[0],'main')
                self.assertEqual(db.execute('SELECT count(*) FROM offers').fetchone()[0],1)
                self.assertEqual(db.execute('SELECT count(*) FROM stream_visits WHERE stream_id="main"').fetchone()[0],1)
        finally:server.DB=previous
    def test_user_permissions_and_session_revocation(self):
        admin=self.browser();self.request(admin,'/api/login',{'password':server.PASSWORD})
        a=self.request(admin,'/api/user',dict(username='buyer-a',name='Buyer A',role='user',active=True,password='qa-password-for-a'))['id']
        b_id=self.request(admin,'/api/user',dict(username='buyer-b',name='Buyer B',role='user',active=True,password='qa-password-for-b'))['id']
        buyer=self.browser();self.request(buyer,'/api/login',dict(username='buyer-a',password='qa-password-for-a'))
        blank=self.request(buyer,'/api/admin');self.assertEqual(blank['streams'],[]);self.assertEqual(blank['offers'],[]);self.assertEqual(blank['users'],[])
        sid=self.request(buyer,'/api/stream',dict(name='My custom name',slug='buyer-a-stream',owner_id=b_id))['id']
        sid2=self.request(buyer,'/api/stream',dict(name='Second name',slug='buyer-a-second'))['id']
        own=dict(name='Own',url='/demo-offer.html',active=True,titles=['Owned'],descriptions=['Own description'],images=['/assets/home.svg'],stream_id=sid)
        self.request(buyer,'/api/offer',own)
        mine=self.request(buyer,'/api/admin');oid=mine['offers'][0]['id'];self.assertEqual({s['owner_id'] for s in mine['streams']},{a})
        self.assertNotIn('password_hash',json.dumps(mine))
        self.assertEqual(self.request(buyer,'/api/copy',dict(offer_ids=[oid],target_stream_id=sid2))['count'],1)
        all_data=self.request(admin,'/api/admin');foreign=next(o for o in all_data['offers'] if o['stream_id']=='main')
        for path,body in [('/api/offer',{**foreign,'name':'Hacked'}),('/api/delete',{'id':foreign['id']}),('/api/stream',dict(id='main',name='Hacked',slug='main')),('/api/copy',dict(offer_ids=[foreign['id']],target_stream_id=sid)),('/api/copy',dict(offer_ids=[oid],target_stream_id='main')),('/api/user',dict(username='hacker',name='Hacker',password='qa-password-for-hacker',role='admin'))]:
            with self.assertRaises(urllib.error.HTTPError) as e:self.request(buyer,path,body)
            self.assertEqual(e.exception.code,403,path)
        other=self.browser();self.request(other,'/api/login',dict(username='buyer-b',password='qa-password-for-b'));self.assertEqual(self.request(other,'/api/admin')['offers'],[])
        self.request(admin,'/api/stream',dict(id=sid,name='Transferred',slug='buyer-a-stream',owner_id=b_id,active=True))
        self.assertFalse(any(s['id']==sid for s in self.request(buyer,'/api/admin')['streams']))
        self.assertTrue(any(o['id']==oid for o in self.request(other,'/api/admin')['offers']))
        self.request(admin,'/api/user',dict(id=a,username='buyer-a',name='Buyer A',role='user',active=True,password='qa-new-password-for-a'))
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(buyer,'/api/admin')
        self.assertEqual(e.exception.code,401)
        self.request(buyer,'/api/login',dict(username='buyer-a',password='qa-new-password-for-a'))
        self.request(admin,'/api/user',dict(id=a,username='buyer-a',name='Buyer A',role='user',active=False))
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(buyer,'/api/admin')
        self.assertEqual(e.exception.code,401)
        with self.assertRaises(urllib.error.HTTPError) as e:self.request(admin,'/api/user',dict(id='admin',username='admin',name='Admin',role='user',active=True))
        self.assertEqual(e.exception.code,400)
        with server.connect() as db:
            stored=db.execute('SELECT password_hash FROM users WHERE id=?',(a,)).fetchone()[0]
            self.assertNotIn('qa-new-password-for-a',stored)

if __name__=='__main__':unittest.main()
