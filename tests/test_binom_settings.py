import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from binom_settings import read_settings, save_settings, public_settings, check_connection, migrate_legacy


class SettingsTest(unittest.TestCase):
    def test_legacy_settings_assigned_only_to_admin(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            save_settings(root,dict(origin='https://legacy.test',api_key='legacy-key',tokens={'dm_variant':17}))
            migrate_legacy(root,'admin')
            self.assertEqual(read_settings(root,'admin')['api_key'],'legacy-key')
            self.assertEqual(read_settings(root,'other')['api_key'],'')
            save_settings(root,dict(origin='',clear_api_key=True,tokens={}), 'admin')
            migrate_legacy(root,'admin')
            self.assertEqual(read_settings(root,'admin')['api_key'],'')
    def test_secret_kept_private_preserved_and_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            result=save_settings(root,dict(origin='https://tracker.test',api_key='private-test-key',tokens={'dm_variant':17}))
            self.assertTrue(result['has_api_key'])
            self.assertNotIn('private-test-key',str(result))
            self.assertNotIn('api_key',public_settings(read_settings(root)))
            save_settings(root,dict(origin='https://tracker.test',api_key='',tokens={'dm_variant':19}))
            self.assertEqual(read_settings(root)['api_key'],'private-test-key')
            with self.assertRaises(ValueError):save_settings(root,dict(origin='https://other.test',tokens={}))
            self.assertEqual(read_settings(root)['origin'],'https://tracker.test')
            with patch('binom_settings.Client') as client:
                client.return_value.get.return_value=[]
                self.assertTrue(check_connection(root)['ok'])
                client.assert_called_with('https://tracker.test','private-test-key')
                client.return_value.get.assert_called_once_with('/public/api/v1/traffic_source/list/filtered',{'limit':1,'offset':0})
            save_settings(root,dict(origin='https://tracker.test',clear_api_key=True,tokens={}))
            self.assertEqual(read_settings(root)['api_key'],'')
            self.assertFalse(list(root.glob('*.tmp')))
