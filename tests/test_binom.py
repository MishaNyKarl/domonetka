import json
import unittest
from urllib.parse import parse_qs, urlsplit
from binom import Client, attributed_url, date_range, campaign_id, token_mapping


class BinomTest(unittest.TestCase):
    def test_user_selected_token_mapping(self):
        mapping={'dm_source':7,'dm_flow':13,'dm_offer':2,'dm_variant':30,'dm_click':19}
        self.assertEqual(token_mapping(mapping,complete=True),mapping)
        self.assertEqual(token_mapping({'dm_variant':17}),{'dm_variant':17})
        for invalid in ({'dm_source':3,'dm_variant':3},{'dm_variant':31},{'dm_variant':True},{'dm_variant':'3'},{'unknown':3},[]):
            with self.assertRaises(ValueError): token_mapping(invalid)
        with self.assertRaises(ValueError): token_mapping({'dm_variant':17},complete=True)

    def test_attribution_preserves_campaign_key_and_existing_query(self):
        url = attributed_url('https://tracker.test/click?key=abc&sub=x%2By&empty=#part', 'f', 'o', 'v', 'c')
        self.assertTrue(url.startswith('https://tracker.test/click?key=abc&sub=x%2By&empty=&'))
        self.assertEqual(urlsplit(url).fragment, 'part')
        query = parse_qs(urlsplit(url).query, keep_blank_values=True)
        self.assertEqual(query['dm_source'], ['domonetka'])
        self.assertEqual(query['dm_variant'], ['v'])
        self.assertEqual(query['dm_click'], ['c'])
        for source in ('/demo-offer.html', 'https://tracker.test/?dm_variant=old', 'https://tracker.test/?dm%5Fclick='):
            with self.assertRaises(ValueError): attributed_url(source, 'f', 'o', 'v', 'c')

    def test_dates_and_campaign_ids(self):
        self.assertEqual(date_range('2026-10-01', '2026-10-08'), ('2026-10-01', '2026-10-08'))
        self.assertEqual(date_range(), (None, None))
        for dates in [('2026-02-30',''), ('2026-10-09','2026-10-08'), ('2026-1-1','')]:
            with self.assertRaises(ValueError): date_range(*dates)
        for cid in ('-1', '1.5', '1 OR 1=1'):
            with self.assertRaises(ValueError): campaign_id(cid)

    def test_api_request_matches_installed_openapi_contract(self):
        requests = []

        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, size): return b'{"report":[]}'

        def transport(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 30)
            return Response()

        client = Client('https://tracker.test', 'test-key', transport)
        self.assertEqual(client.campaign_report_page('123', '2026-10-01', '2026-10-08', [21,22,23,24], offset=100), {'report': []})
        req = requests[0]
        query = parse_qs(urlsplit(req.full_url).query)
        self.assertEqual(query['ids[]'], ['123'])
        self.assertEqual(query['groupings[]'], ['days', 'token_21', 'token_22', 'token_23', 'token_24', 'conversionStatusOne', 'conversionStatusTwo'])
        self.assertEqual(query['offset'], ['100'])
        self.assertEqual(query['timezone'], ['UTC'])
        self.assertEqual(req.get_header('Api-key'), 'test-key')
        self.assertNotIn('test-key', req.full_url)
        mapping={'dm_source':9,'dm_flow':17,'dm_offer':4,'dm_variant':25,'dm_click':30}
        client.mapped_campaign_report_page('123','2026-10-01','2026-10-08',mapping)
        mapped=parse_qs(urlsplit(requests[-1].full_url).query)
        self.assertEqual(mapped['groupings[]'][1:6],['token_9','token_17','token_4','token_25','token_30'])
        from pathlib import Path
        spec = json.loads((Path(__file__).resolve().parents[1]/'docs/binom-v2-openapi.json').read_text(encoding='utf8'))
        parameters = spec['paths']['/public/api/v1/report/{page}']['get']['parameters']
        declared = {p['name'] if 'name' in p else spec['components']['parameters'][p['$ref'].split('/')[-1]]['name'] for p in parameters}
        self.assertTrue(set(query) <= declared)
        for slots in ([1,1], [31], ['1']):
            with self.assertRaises(ValueError): client.campaign_report_page('123', '2026-10-01', '2026-10-08', slots)
