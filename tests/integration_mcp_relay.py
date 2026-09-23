#!/usr/bin/env python3
"""Real local relay + real MCP subprocess; browser events are controlled fixtures.
No external sites, account/agent API calls, or personal Chrome profile are used.
"""
import datetime
import json
import os
from pathlib import Path
import selectors
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

ROOT=Path(__file__).resolve().parents[1]

def main():
    env=os.environ.copy();env['PYTHONDONTWRITEBYTECODE']='1'
    with tempfile.TemporaryDirectory(prefix='armory-integration-') as tmp:
        d=Path(tmp)
        with socket.socket() as s:s.bind(('127.0.0.1',0));port=s.getsockname()[1]
        base=f'http://127.0.0.1:{port}'
        r=subprocess.run([sys.executable,'-m','bridge.relay','init','--output-dir',str(d),'--relay-url',base],cwd=ROOT,env=env,capture_output=True,text=True,timeout=15)
        assert r.returncode==0, r.stderr
        agent=json.loads((d/'agent-client.json').read_text());browser=json.loads((d/'browser-client.json').read_text())
        relay=subprocess.Popen([sys.executable,'-m','bridge.relay','serve','--config',str(d/'server-config.json'),'--db',str(d/'jobs.sqlite3'),'--host','127.0.0.1','--port',str(port)],cwd=ROOT,env=env,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        mcp=None
        opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
        def http(method,path,body=None,token=None,origin=None):
            headers={'Content-Type':'application/json'}
            if token:headers['Authorization']='Bearer '+token
            if origin:headers['Origin']=origin
            req=urllib.request.Request(base+path,data=None if body is None else json.dumps(body).encode(),method=method,headers=headers)
            try:
                with opener.open(req,timeout=3) as response:return response.status,json.load(response)
            except urllib.error.HTTPError as exc:return exc.code,json.load(exc)
        try:
            for _ in range(80):
                try:
                    if http('GET','/health')[0]==200:break
                except OSError:pass
                time.sleep(.05)
            else:raise AssertionError('relay startup timeout')
            mcp=subprocess.Popen([sys.executable,str(ROOT/'bridge/mcp_server.py'),'--config',str(d/'agent-client.json')],cwd=ROOT,env=env,stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,bufsize=1)
            seq=0
            def rpc(method,params):
                nonlocal seq
                seq+=1
                mcp.stdin.write(json.dumps({'jsonrpc':'2.0','id':seq,'method':method,'params':params})+'\n');mcp.stdin.flush()
                with selectors.DefaultSelector() as selector:
                    selector.register(mcp.stdout,selectors.EVENT_READ)
                    assert selector.select(10),'MCP response timeout'
                line=mcp.stdout.readline();assert line,'MCP process closed stdout'
                reply=json.loads(line);assert reply['id']==seq,reply
                return reply
            init=rpc('initialize',{'protocolVersion':'2025-11-25','capabilities':{},'clientInfo':{'name':'offline-audit','version':'1'}})
            assert init['result']['serverInfo']['name']=='armory-browser'
            mcp.stdin.write('{"jsonrpc":"2.0","method":"notifications/initialized"}\n');mcp.stdin.flush()
            assert len(rpc('tools/list',{})['result']['tools'])==3
            def tool(name,args):
                response=rpc('tools/call',{'name':name,'arguments':args})['result']
                assert not response['isError'],response
                return json.loads(response['content'][0]['text'])['data']['job']
            job=tool('armory_capture',{'url':'https://example.com/article','purpose':'Local protocol test; no external fetch','idempotency_key':'integration-one'})
            jid=job['id'];assert job['state']=='queued'
            print('MCP capture -> queued (no browser navigation)',flush=True)
            code,_=http('POST',f'/v1/browser/jobs/{jid}/events',{'type':'approve'},agent['agent_token'])
            assert code==403,code
            code,_=http('GET','/v1/browser/jobs',token=browser['browser_token'],origin='https://hostile.example')
            assert code==403,code
            code,_=http('POST',f'/v1/browser/jobs/{jid}/events',{'type':'complete','result':{}},browser['browser_token'])
            assert code in (400,409),code
            states=['queued']
            for event,want in [('approve','running'),('human_required','awaiting_human'),('resume','running'),('preview_ready','awaiting_share')]:
                code,payload=http('POST',f'/v1/browser/jobs/{jid}/events',{'type':event},browser['browser_token'])
                assert code==200,(code,payload)
                observed=tool('armory_status',{'job_id':jid})
                assert observed['state']==want,observed
                assert observed.get('result') is None,'Content leaked before user share'
                states.append(want)
            result={'url':'https://example.com/article','title':'Local fixture','text':'Readable fixture article. '*8,'markdown':'# Local fixture\n\n'+'Readable fixture article. '*8,'links':[], 'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),'truncated':False}
            code,payload=http('POST',f'/v1/browser/jobs/{jid}/events',{'type':'complete','result':result},browser['browser_token'])
            assert code==200,(code,payload)
            finished=tool('armory_status',{'job_id':jid});assert finished['state']=='completed';assert finished['result']==result
            states.append('completed')
            print('SIMULATED_BROWSER_STATES='+' -> '.join(states),flush=True)
            print('PREVIEW_PRIVATE_UNTIL_SHARE=true',flush=True)
            print('AGENT_CANNOT_APPROVE=true; HOSTILE_ORIGIN_REJECTED=true',flush=True)
            other=tool('armory_capture',{'url':'https://example.com/second','purpose':'Cancellation fixture'})
            assert tool('armory_cancel',{'job_id':other['id']})['state']=='cancelled'
            print('MCP cancel -> cancelled',flush=True)
            timed=tool('armory_capture',{'url':'https://example.com/timed','purpose':'Timeout fallback fixture','human_timeout_seconds':5})
            tid=timed['id']
            for event in ('approve','human_required'):
                assert http('POST',f'/v1/browser/jobs/{tid}/events',{'type':event},browser['browser_token'])[0]==200
            assert http('POST',f'/v1/browser/jobs/{tid}/events',{'type':'timeout'},browser['browser_token'])[0]==409
            time.sleep(5.1)
            assert http('POST',f'/v1/browser/jobs/{tid}/events',{'type':'resume'},browser['browser_token'])[0]==409
            assert http('POST',f'/v1/browser/jobs/{tid}/events',{'type':'timeout'},browser['browser_token'])[0]==200
            timed=tool('armory_status',{'job_id':tid});assert timed['state']=='running' and timed['degraded'] is True
            assert http('POST',f'/v1/browser/jobs/{tid}/events',{'type':'preview_ready'},browser['browser_token'])[0]==200
            partial=dict(result,url='https://example.com/timed',degraded=True,quality='partial')
            assert http('POST',f'/v1/browser/jobs/{tid}/events',{'type':'complete','result':partial},browser['browser_token'])[0]==200
            timed=tool('armory_status',{'job_id':tid});assert timed['result']['quality']=='partial'
            print('TIMEOUT_FALLBACK=automatic -> awaiting_human -> deadline -> automatic -> completed(degraded=true,quality=partial)',flush=True)
            print('PASS real MCP stdio + local HTTP + SQLite; browser interaction simulated',flush=True)
        finally:
            if mcp:
                mcp.terminate()
                try:mcp.communicate(timeout=5)
                except subprocess.TimeoutExpired:mcp.kill();mcp.communicate()
            relay.terminate()
            try:relay.communicate(timeout=5)
            except subprocess.TimeoutExpired:relay.kill();relay.communicate()
    return 0
if __name__=='__main__':raise SystemExit(main())
