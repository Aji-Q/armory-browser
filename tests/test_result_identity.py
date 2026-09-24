"""Result resource identity, real Store validation, no network or browser."""
import importlib.util,os,sys,unittest
from pathlib import Path
ROOT=Path(os.environ.get('ARMORY_ROOT',Path(__file__).resolve().parents[1]))
spec=importlib.util.spec_from_file_location('identity_relay',ROOT/'bridge/relay.py');relay=importlib.util.module_from_spec(spec);spec.loader.exec_module(relay)
class Identity(unittest.TestCase):
 def setUp(self):
  self.store=relay.Store(':memory:');self.addCleanup(self.store.close)
 def attempt(self,target,actual):
  job,_=self.store.create({'url':target,'purpose':'Identity regression'})
  self.store.event(job['id'],{'type':'approve'});self.store.event(job['id'],{'type':'preview_ready'})
  result={'url':actual,'title':'Requested article','text':'Article facts. '*60,'markdown':'# Article\n\n'+'Article facts. '*60,'links':[],'captured_at':'2026-09-24T00:00:00Z','truncated':False,'degraded':False,'quality':'full'}
  return self.store.event(job['id'],{'type':'complete','result':result})
 def test_exact_resource_accepted(self):self.assertEqual(self.attempt('https://example.com/a?p=1','https://example.com/a?p=1')['state'],'completed')
 def test_fragment_and_trailing_slash_accepted(self):self.assertEqual(self.attempt('https://example.com/a/','https://example.com/a#section')['state'],'completed')
 def test_empty_root_and_slash_accepted(self):self.assertEqual(self.attempt('https://example.com','https://example.com/')['state'],'completed')
 def test_wrong_same_origin_page_rejected(self):
  with self.assertRaises(relay.RelayError):self.attempt('https://example.com/article/123','https://example.com/account/home')
 def test_different_query_rejected(self):
  with self.assertRaises(relay.RelayError):self.attempt('https://example.com/article?id=123','https://example.com/article?id=999')
 def test_reordered_query_not_assumed_equivalent(self):
  with self.assertRaises(relay.RelayError):self.attempt('https://example.com/a?a=1&b=2','https://example.com/a?b=2&a=1')
 def test_wrong_origin_rejected(self):
  with self.assertRaises(relay.RelayError):self.attempt('https://example.com/a','https://other.example/a')
 def test_actual_url_preserved(self):self.assertEqual(self.attempt('https://example.com/a/','https://example.com/a#section')['result']['url'],'https://example.com/a#section')
 def test_browser_dot_segments_accepted(self):
  for raw in ['/a/../article','/%2E/article','/a/%2e%2E/article','/a/.%2e/article']:
   self.assertEqual(self.attempt('https://example.com'+raw,'https://example.com/article')['state'],'completed')
 def test_browser_query_apostrophe_accepted(self):
  self.assertEqual(self.attempt("https://example.com/a?q=O'Reilly",'https://example.com/a?q=O%27Reilly')['state'],'completed')
 def test_browser_ipv6_compression_accepted(self):
  self.assertEqual(self.attempt('https://[2001:4860:4860:0000:0000:0000:0000:8888]/a','https://[2001:4860:4860::8888]/a')['state'],'completed')
 def test_browser_unicode_encoding_accepted(self):
  self.assertEqual(self.attempt('https://example.com/文章?q=汉字','https://example.com/%E6%96%87%E7%AB%A0?q=%E6%B1%89%E5%AD%97')['state'],'completed')
 def test_encoded_slash_is_not_path_separator(self):
  with self.assertRaises(relay.RelayError):self.attempt('https://example.com/a%2Fb','https://example.com/a/b')
if __name__=='__main__':unittest.main(verbosity=2)
