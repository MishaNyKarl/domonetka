import json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import server
from binom_settings import save_settings
from binom_sync import SyncService,make_snapshots,read_report,report,tree,status_policy,classify
from binom import BinomError

class FakeClient:
    fail=False
    def get(self,path,params=None):
        if self.fail:raise BinomError('Binom: HTTP 503')
        if path.endswith('/columns/report'):return {'default':[dict(key='clicks',name='Clicks',decimals=0,format='def')],'custom':[]}
        if '/campaign/' in path and 'report' not in path:return {'trafficSourceId':7}
        if '/traffic_source/' in path:return {'tokens':[{'id':'external_id','parameter':'clickid'}]+[{'id':i,'parameter':k} for k,i in mapping.items()]}
        groups=params['groupings[]'];daily=groups[0]=='days'
        attributed=any(g.startswith('token_') for g in groups)
        names=([day] if daily else [])+(['domonetka','main',oid,vid,'new','Unknown'] if attributed else ([] if daily else ['123'])+['new','Unknown'])
        data=[]
        for i,name in enumerate(names):
            data.append(dict(level=str(i+1),name=name,clicks='2' if attributed else '3',leads='1',cost='1.25',revenue='2.5',cr='50',unique_clicks='1',custom_metric='8.75'))
        offset=params['offset'];limit=params['limit']
        return {'report':data[offset:offset+limit],'totals':{'total_count':len(data),'clicks':'2' if attributed else '3','leads':'1','cost':'1.25'}}

mapping={'dm_source':11,'dm_flow':12,'dm_offer':13,'dm_variant':14,'dm_click':15}
day='2026-10-08';oid='';vid=''

class ImportTest(unittest.TestCase):
    def setUp(self):
        global oid,vid
        self.previous=(server.DATA,server.DB);self.temp=tempfile.TemporaryDirectory();server.DATA=Path(self.temp.name);(server.DATA/'uploads').mkdir();server.DB=server.DATA/'test.sqlite';server.init()
        save_settings(server.DATA,dict(origin='https://tracker.example.com',api_key='test-key',tokens=mapping),'admin')
        with server.connect() as db:
            db.execute("INSERT INTO trackers(id,user_id,name,created,updated) VALUES('test-tracker','admin','Test',0,0)")
            oid=server.save_offer(db,dict(name='Tracked',url='https://tracker.example.com/click?key=abc',stream_id='main',binom_campaign_id='123',titles=['A'],descriptions=['D'],images=['/assets/home.svg']))
            vid=db.execute('select id from variants where offer_id=?',(oid,)).fetchone()[0]
            self.offer=dict(db.execute('select o.*,s.owner_id from offers o join streams s on s.id=o.stream_id where o.id=?',(oid,)).fetchone())
        self.service=SyncService(server.connect,lambda:server.DATA,auto=False);self.client=FakeClient()
    def tearDown(self):server.DATA,server.DB=self.previous;self.temp.cleanup()
    def import_once(self,start='',end=''):
        from binom_settings import read_settings
        self.service.import_offer(self.client,read_settings(server.DATA,'admin'),self.offer,dict(period_from=start,period_to=end))
    def test_replace_scope_statuses_exact_period_and_failure(self):
        self.import_once();self.import_once()
        with server.connect() as db:
            rows=read_report(db,server.DATA,dict(id='admin',role='admin'))['rows'];self.assertEqual(len(rows),3)
            daily=read_report(db,server.DATA,dict(id='admin',role='admin'))['daily'];self.assertEqual(len(daily),3);self.assertTrue(all(r['day']==day for r in daily))
            campaign=next(r for r in rows if r['scope']=='campaign');self.assertEqual(campaign['metrics']['clicks'],3)
            variant=next(r for r in rows if r['scope']=='variant');self.assertEqual(variant['metrics']['clicks'],2);self.assertEqual(variant['statuses'],[dict(one='new',two='Unknown',leads=1)])
            self.assertIsNone(variant['approved']);self.assertEqual(variant['metrics']['custom_metric'],8.75)
            count=db.execute('select count(*) from binom_snapshots').fetchone()[0]
        self.client.fail=True
        with self.assertRaises(BinomError):self.import_once()
        with server.connect() as db:self.assertEqual(db.execute('select count(*) from binom_snapshots').fetchone()[0],count)
        self.client.fail=False;self.import_once(day,day)
        with server.connect() as db:
            ranged=read_report(db,server.DATA,dict(id='admin',role='admin'),day,day)
            self.assertEqual(len(ranged['rows']),3);self.assertEqual(ranged['rows'][0]['day'],'period')
            self.assertEqual(ranged['rows'][0]['metrics']['cost'],1.25)
            # A new campaign cannot display a prior campaign's cached data.
            db.execute('update offers set binom_campaign_id=? where id=?',('124',oid))
            self.assertEqual(read_report(db,server.DATA,dict(id='admin',role='admin'))['rows'],[])
    def test_disabled_tracker_cannot_commit_inflight_import(self):
        self.import_once()
        with server.connect() as db:
            old=db.execute('SELECT metrics FROM binom_snapshots ORDER BY day,scope,variant_id').fetchall()
            db.execute('UPDATE trackers SET active=0')
        with self.assertRaises(BinomError):self.import_once()
        with server.connect() as db:
            self.assertEqual([r[0] for r in old],[r[0] for r in db.execute('SELECT metrics FROM binom_snapshots ORDER BY day,scope,variant_id')])
        with self.assertRaises(ValueError):self.service.submit('admin','admin')
    def test_background_guard_blocks_requests_and_snapshot_commit(self):
        def denied(uid): raise ValueError('Central import denied')
        self.service.background_check=denied
        with patch.object(self.client,'get',wraps=self.client.get) as remote:
            with self.assertRaises(ValueError):self.import_once()
            remote.assert_not_called()
        # Find the final guard call (after all remote reads) on a successful import.
        checks=[]
        self.service.background_check=lambda uid:checks.append(uid)
        self.import_once()
        with server.connect() as db:
            before=[tuple(r) for r in db.execute('SELECT * FROM binom_snapshots ORDER BY day,scope,variant_id')]
        final=len(checks);counter=[0]
        def deny_commit(uid):
            counter[0]+=1
            if counter[0]==final:raise ValueError('Revoked before commit')
        self.service.background_check=deny_commit
        # Exact-period request makes changed snapshot data easy to detect.
        with self.assertRaises(ValueError):self.import_once()
        with server.connect() as db:
            self.assertEqual(before,[tuple(r) for r in db.execute('SELECT * FROM binom_snapshots ORDER BY day,scope,variant_id')])

    def test_job_deduplication(self):
        a=self.service.submit('admin','admin');b=self.service.submit('admin','admin')
        self.assertEqual(a['job_id'],b['job_id']);self.assertNotEqual(a['job_id'],self.service.submit('admin','admin',day,day)['job_id'])
    def test_worker_completes_and_logs(self):
        pending=self.service.submit('admin','admin')
        with server.connect() as db:job=dict(db.execute('select * from binom_jobs where id=?',(pending['job_id'],)).fetchone())
        with patch('binom_sync.Client',return_value=self.client):self.service.run(job)
        with server.connect() as db:
            self.assertEqual(db.execute('select status from binom_jobs where id=?',(pending['job_id'],)).fetchone()[0],'success')
            self.assertEqual(db.execute("select status from tracker_logs where operation='sync' order by id desc limit 1").fetchone()[0],'success')
    def test_policy_is_explicit_and_disjoint(self):
        p=status_policy(dict(axis='one',approved=['payout'],pending=['new'],rejected=['reject']))
        r=classify([dict(one='new',two='Unknown',leads=2),dict(one='payout',two='Unknown',leads=1)],p,4)
        self.assertEqual((r['approved'],r['pending'],r['unknown'],r['approval_rate']),(1,2,1,25))
        with self.assertRaises(ValueError):status_policy(dict(approved=['new'],pending=['new']))
        self.assertIsNone(classify([],{},0)['approved'])
    def test_tree_missing_parent_is_rejected(self):
        with self.assertRaises(BinomError):list(tree([dict(level='3',name='bad')]))

if __name__=='__main__':unittest.main()
