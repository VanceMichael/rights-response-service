"""核验合成资料和协作方回放，不验证尚未提供的业务服务。"""
import json
import subprocess
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]

class MaterialChecks(unittest.TestCase):
    def test_references(self):
        catalog=json.loads((ROOT/'data/catalog.json').read_text())
        records=catalog['records'];ids=[r['id'] for r in records]
        self.assertEqual(len(ids),len(set(ids)))
        self.assertTrue(catalog['synthetic'])
        for row in records:
            self.assertTrue(row['kind'])
            for ref in row['refs']:self.assertIn(ref,ids)
        for row in json.loads((ROOT/'partner/commands.json').read_text())['commands']:
            for ref in row['refs']:self.assertIn(ref,ids)
            self.assertTrue(row['observations'])

    def test_partner_http_and_restart(self):
        command=json.loads((ROOT/'partner/commands.json').read_text())['commands'][0]
        with tempfile.TemporaryDirectory() as temp:
            state=str(Path(temp)/'partner.sqlite3')
            def start():
                process=subprocess.Popen([sys.executable,str(ROOT/'partner/server.py'),'--port','0','--state',state],stdout=subprocess.PIPE,text=True)
                port=json.loads(process.stdout.readline())['port']
                return process,'http://127.0.0.1:'+str(port)
            def request(base,path,value=None):
                raw=json.dumps(value).encode() if value else None
                try:
                    response=urllib.request.urlopen(urllib.request.Request(base+path,data=raw,headers={'Content-Type':'application/json'}),timeout=5)
                except urllib.error.HTTPError as e:response=e
                with response:return response.status,json.load(response)
            p,base=start()
            try:
                self.assertEqual(request(base,'/health')[0],200)
                self.assertEqual(request(base,'/commands/'+command['id'])[0],404)
                value={'command_id':command['id'],'operation':command['operation'],'request':command['request']}
                self.assertEqual(request(base,'/commands',value)[0],command['response_status'])
                self.assertEqual(request(base,'/commands',value)[0],command['response_status'])
                first=request(base,'/commands/'+command['id'])[1]
                self.assertEqual(first,{'command_id':command['id'],**command['observations'][0]})
                bad={**value,'request':{}}
                self.assertEqual(request(base,'/commands',bad)[0],400)
            finally:p.terminate();p.wait(timeout=5);p.stdout.close()
            p,base=start()
            try:
                last=None
                for _ in command['observations']:last=request(base,'/commands/'+command['id'])[1]
                self.assertEqual(last,{'command_id':command['id'],**command['observations'][-1]})
            finally:p.terminate();p.wait(timeout=5);p.stdout.close()

if __name__=='__main__':unittest.main()
