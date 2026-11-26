"""合成协作方协议回放，不处理业务申请或作出项目决定。"""
import argparse
import hashlib
import json
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CATALOG=json.loads((Path(__file__).with_name('commands.json')).read_text())
COMMANDS={r['id']:r for r in CATALOG['commands']}

def serve(host,port,state):
    with sqlite3.connect(state) as db:
        db.execute('CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, cursor INTEGER NOT NULL)')
    class Handler(BaseHTTPRequestHandler):
        def reply(self,status,value):
            raw=json.dumps(value,ensure_ascii=False).encode()
            self.send_response(status);self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(raw)));self.end_headers();self.wfile.write(raw)
        def log_message(self,*args): pass
        def do_POST(self):
            if self.path!='/commands':return self.reply(404,{'error':'接口不存在'})
            try:
                value=json.loads(self.rfile.read(int(self.headers.get('Content-Length','0'))))
                item=COMMANDS[value['command_id']]
                if value['operation']!=item['operation'] or value['request']!=item['request']:raise ValueError()
                fingerprint=hashlib.sha256(json.dumps(value,sort_keys=True).encode()).hexdigest()
            except (ValueError,KeyError,TypeError):return self.reply(400,{'error':'流水或协议内容不匹配'})
            with sqlite3.connect(state,timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                previous=db.execute('SELECT fingerprint FROM commands WHERE id=?',(item['id'],)).fetchone()
                if previous and previous[0]!=fingerprint:return self.reply(409,{'error':'同流水内容冲突'})
                db.execute('INSERT OR IGNORE INTO commands VALUES(?,?,0)',(item['id'],fingerprint))
            self.reply(item['response_status'],{'command_id':item['id'],'query':'/commands/'+item['id'], 'message':'协作方结果以查询为准'})
        def do_GET(self):
            if self.path=='/health':return self.reply(200,{'status':'ok','mode':'合成协议回放'})
            command=self.path.removeprefix('/commands/')
            if not self.path.startswith('/commands/') or command not in COMMANDS:return self.reply(404,{'error':'接口不存在'})
            with sqlite3.connect(state,timeout=10) as db:
                db.execute('BEGIN IMMEDIATE')
                row=db.execute('SELECT cursor FROM commands WHERE id=?',(command,)).fetchone()
                if not row:return self.reply(404,{'error':'流水尚未受理'})
                frames=COMMANDS[command]['observations'];index=min(row[0],len(frames)-1)
                db.execute('UPDATE commands SET cursor=? WHERE id=?',(index+1,command))
            self.reply(200,{'command_id':command,**frames[index]})
    server=ThreadingHTTPServer((host,port),Handler)
    print(json.dumps({'host':host,'port':server.server_address[1]},ensure_ascii=False),flush=True)
    server.serve_forever()

if __name__=='__main__':
    p=argparse.ArgumentParser(description='合成协作方协议回放')
    p.add_argument('--host',default='127.0.0.1');p.add_argument('--port',type=int,default=8765)
    p.add_argument('--state',default='partner-state.sqlite3');a=p.parse_args();serve(a.host,a.port,a.state)
