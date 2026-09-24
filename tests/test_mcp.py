#!/usr/bin/env python3
"""Offline MCP framing/tool-policy regression tests, no model or account needed."""
import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
import sys
import unittest
from unittest.mock import Mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from mcp_server import MCPServer, RelayClient, RelayError, serve

ID='11111111-1111-4111-8111-111111111111'
class MCPTests(unittest.TestCase):
    def setUp(self):
        self.client=Mock()
        self.client.request.return_value={'job':{'id':ID,'state':'queued'}}
        self.server=MCPServer(self.client)
        self.call('initialize',{'protocolVersion':'2025-11-25'})
    def call(self,method,params=None):
        return self.server.handle({'jsonrpc':'2.0','id':1,'method':method,'params':params or {}})
    def test_protocol_negotiation(self):
        self.assertEqual(self.call('initialize',{'protocolVersion':'2024-11-05'})['result']['protocolVersion'],'2024-11-05')
        self.assertEqual(self.call('initialize',{'protocolVersion':'future'})['result']['protocolVersion'],'2025-11-25')
    def test_initialize_required(self):
        self.assertEqual(MCPServer(self.client).handle({'jsonrpc':'2.0','id':2,'method':'tools/list'})['error']['code'],-32002)
    def test_notifications_have_no_response(self):
        self.assertIsNone(self.server.handle({'jsonrpc':'2.0','method':'notifications/initialized'}))
    def test_only_three_scoped_tools(self):
        names={t['name'] for t in self.call('tools/list')['result']['tools']}
        self.assertEqual(names,{'armory_capture','armory_status','armory_cancel'})
    def test_capture_is_queued_not_browser_approval(self):
        result=self.call('tools/call',{'name':'armory_capture','arguments':{'url':'https://example.com/article','purpose':'Read this article'}})
        self.assertFalse(result['result']['isError'])
        self.client.request.assert_called_once_with('POST','/v1/jobs',{'url':'https://example.com/article','purpose':'Read this article'})
    def test_arbitrary_remote_actions_rejected(self):
        r=self.call('tools/call',{'name':'armory_capture','arguments':{'url':'https://example.com','purpose':'test','javascript':'anything'}})
        self.assertTrue(r['result']['isError']);self.client.request.assert_not_called()
    def test_private_browser_events_are_not_tools(self):
        for name in ['approve','resume','complete','execute_js','read_cookies']:
            self.assertTrue(self.call('tools/call',{'name':name,'arguments':{}})['result']['isError'])
        self.client.request.assert_not_called()
    def test_status_wraps_web_content_as_untrusted(self):
        r=self.call('tools/call',{'name':'armory_status','arguments':{'job_id':ID}})
        data=json.loads(r['result']['content'][0]['text'])
        self.assertEqual(data['content_trust'],'untrusted_web_content')
        self.client.request.assert_called_once_with('GET','/v1/jobs/'+ID)
    def test_job_id_path_injection_rejected(self):
        r=self.call('tools/call',{'name':'armory_status','arguments':{'job_id':'../../browser/jobs'}})
        self.assertTrue(r['result']['isError']);self.client.request.assert_not_called()
    def test_cancel_endpoint(self):
        self.call('tools/call',{'name':'armory_cancel','arguments':{'job_id':ID}})
        self.client.request.assert_called_once_with('POST','/v1/jobs/'+ID+'/cancel',{})
    def test_invalid_limit_and_empty_purpose(self):
        for fields in [{'max_chars':True},{'max_chars':100001},{'purpose':''}, {'human_timeout_seconds':0}, {'human_timeout_seconds':True}]:
            a={'url':'https://example.com','purpose':'Read'};a.update(fields)
            self.assertTrue(self.call('tools/call',{'name':'armory_capture','arguments':a})['result']['isError'])
    def test_relay_error_is_tool_error(self):
        self.client.request.side_effect=RelayError('Relay unavailable')
        r=self.call('tools/call',{'name':'armory_status','arguments':{'job_id':ID}})
        self.assertTrue(r['result']['isError'])
    def test_stdio_framing_parse_error_and_notification(self):
        incoming='not json\n'+json.dumps({'jsonrpc':'2.0','method':'notifications/initialized'})+'\n'+json.dumps({'jsonrpc':'2.0','id':'x','method':'tools/list'})+'\n'
        output=io.StringIO();serve(self.server,io.StringIO(incoming),output)
        frames=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(len(frames),2);self.assertEqual(frames[0]['error']['code'],-32700);self.assertEqual(frames[1]['id'],'x')
    def test_deep_json_frame_returns_error_and_continues(self):
        nested='['*(sys.getrecursionlimit()+100)+'0'+']'*(sys.getrecursionlimit()+100)
        incoming=nested+'\n'+json.dumps({'jsonrpc':'2.0','id':'after-deep-frame','method':'ping'})+'\n'
        output=io.StringIO()
        serve(self.server,io.StringIO(incoming),output)
        frames=[json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(2,len(frames))
        # JSON nesting limits differ across CPython versions: valid deep JSON
        # may parse to a non-object (Invalid Request), or exceed the parser
        # limit (Parse error). Both must reject it and keep the next frame.
        self.assertIn(frames[0]['error']['code'], (-32700, -32600))
        self.assertEqual({'jsonrpc':'2.0','id':'after-deep-frame','result':{}},frames[1])
        self.client.request.assert_not_called()
    def test_surrogate_id_cannot_break_utf8_output_or_next_frame(self):
        incoming='\n'.join(json.dumps({'jsonrpc':'2.0','id':value,'method':'ping'})
                           for value in ['\ud800','after-surrogate','中文🙂'])+'\n'
        buffer=io.BytesIO()
        output=io.TextIOWrapper(buffer,encoding='utf-8',errors='strict')
        try:
            serve(self.server,io.StringIO(incoming),output)
            output.flush()
            frames=[json.loads(line) for line in buffer.getvalue().decode('utf-8').splitlines()]
            self.assertEqual(3,len(frames))
            self.assertEqual(-32600,frames[0]['error']['code'])
            self.assertIsNone(frames[0]['id'])
            self.assertEqual('after-surrogate',frames[1]['id'])
            self.assertEqual('中文🙂',frames[2]['id'])
        finally:
            output.close()
    def test_truncated_chunk_is_private_tool_error_and_server_recovers(self):
        request_count=[]
        class BrokenThenHealthy(BaseHTTPRequestHandler):
            def do_GET(self):
                request_count.append(True)
                if len(request_count)==1:
                    self.send_response(200)
                    self.send_header('Transfer-Encoding','chunked')
                    self.end_headers()
                    # Deliberately incomplete chunk; no real credential or account data.
                    self.wfile.write(b'80\r\nSYNTHETIC_PRIVATE_RESPONSE_MARKER\r\n')
                else:
                    body=json.dumps({'job':{'id':ID,'state':'queued'}}).encode()
                    self.send_response(200)
                    self.send_header('Content-Length',str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
            def log_message(self,*args):
                pass
        relay=ThreadingHTTPServer(('127.0.0.1',0),BrokenThenHealthy)
        thread=threading.Thread(target=relay.serve_forever,kwargs={'poll_interval':0.01},daemon=True)
        thread.start()
        try:
            client=RelayClient({'relay_url':f'http://127.0.0.1:{relay.server_port}',
                                'agent_token':'synthetic-test-token-not-secret-0000'})
            server=MCPServer(client)
            frames=[{'jsonrpc':'2.0','id':1,'method':'initialize','params':{}},
                    {'jsonrpc':'2.0','id':2,'method':'tools/call','params':{'name':'armory_status','arguments':{'job_id':ID}}},
                    {'jsonrpc':'2.0','id':3,'method':'ping'},
                    {'jsonrpc':'2.0','id':4,'method':'tools/call','params':{'name':'armory_status','arguments':{'job_id':ID}}}]
            output=io.StringIO()
            serve(server,io.StringIO('\n'.join(json.dumps(frame) for frame in frames)+'\n'),output)
            replies=[json.loads(line) for line in output.getvalue().splitlines()]
            self.assertEqual(4,len(replies))
            self.assertTrue(replies[1]['result']['isError'])
            self.assertEqual('Relay unavailable; check that it is running and reachable',replies[1]['result']['content'][0]['text'])
            self.assertEqual({},replies[2]['result'])
            self.assertFalse(replies[3]['result']['isError'])
            self.assertEqual(2,len(request_count))
            self.assertNotIn('SYNTHETIC_PRIVATE_RESPONSE_MARKER',output.getvalue())
            self.assertNotIn(client.token,output.getvalue())
        finally:
            relay.shutdown()
            relay.server_close()
            thread.join()
    def test_unknown_method_and_invalid_request(self):
        self.assertEqual(self.call('unknown')['error']['code'],-32601)
        self.assertEqual(self.server.handle([])['error']['code'],-32600)
    def test_relay_origin_no_plaintext_public_or_credentials(self):
        for url in ['http://relay.example','https://a:b@relay.example','https://relay.example/path','https://relay.example#fragment']:
            with self.assertRaises(ValueError):RelayClient({'relay_url':url,'agent_token':'a'*32})
        RelayClient({'relay_url':'http://127.0.0.1:8765','agent_token':'a'*32})
        RelayClient({'relay_url':'https://relay.example','agent_token':'a'*32})

if __name__=='__main__':unittest.main(verbosity=2)
