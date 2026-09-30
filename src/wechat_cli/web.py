import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import service
from .errors import AutomationError
from .registry import capabilities
from .task_queue import TaskRunner, TaskStore


PAGE = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>wechat-cli Demo</title>
<style>body{margin:0;font:14px Arial;color:#18212b;background:#f4f6f8}header{padding:16px 24px;background:#fff;border-bottom:1px solid #dfe4ea}main{display:grid;grid-template-columns:330px 1fr;gap:16px;padding:16px}section{background:#fff;border:1px solid #dfe4ea;padding:16px}h1,h2{margin:0 0 12px}h1{font-size:20px}h2{font-size:16px}.methods{height:calc(100vh - 150px);overflow:auto}button{padding:8px 10px;border:1px solid #b8c2cc;background:#fff;cursor:pointer}button:hover{background:#eef5ff}.selected{background:#dff0e5}label{display:block;margin:10px 0 4px}input,textarea,select{box-sizing:border-box;width:100%;padding:8px;border:1px solid #b8c2cc}textarea{height:100px;font-family:monospace}.row{display:flex;gap:8px;align-items:center}.row>*{flex:1}.status{font-weight:bold}.queued{color:#936c00}.running{color:#0563c1}.succeeded{color:#16803c}.failed{color:#b42318}pre{white-space:pre-wrap;max-height:270px;overflow:auto;background:#101820;color:#dce6ef;padding:12px}.tasks{max-height:300px;overflow:auto}.task{padding:9px;border-bottom:1px solid #edf0f2}.small{font-size:12px;color:#5d6b78}</style>
<header><h1>wechat-cli Demo</h1><div class="small">本地控制台。任务串行执行；相同请求在 15 秒内不会重复点击。同一聊天工作面最多保留 3 分钟，便于连续任务复用。</div></header>
<main><section><h2>功能</h2><div id="methods" class="methods"></div></section><section><h2 id="title">选择功能</h2><div id="form"></div><div class="row" style="margin-top:12px"><button id="enqueue">加入任务队列</button><button id="refresh">刷新</button></div><p id="note" class="small"></p><h2>任务状态 / 队列</h2><div id="tasks" class="tasks"></div><h2>任务结果</h2><pre id="result">尚未选择任务</pre></section></main>
<script>
let methods=[], selected=null, selectedTask=null;
const api=(path,opts={})=>fetch(path,opts).then(r=>r.json());
function input(name,schema){const type=schema.type;let html='<label>'+name+'</label>';
 if(type==='boolean')html+='<select name="'+name+'"><option value="true">true</option><option value="false">false</option></select>';
 else if(type==='array')html+='<textarea name="'+name+'" placeholder="JSON array, e.g. [\\"False\\"]"></textarea>';
 else html+='<input name="'+name+'" '+(schema.enum?'list="list-'+name+'"':'')+'>'; if(schema.enum)html+='<datalist id="list-'+name+'">'+schema.enum.map(x=>'<option value="'+x+'">').join('')+'</datalist>';return html}
function selectMethod(method){selected=method;document.getElementById('title').textContent=method.method;const required=new Set(method.params_schema.required);let html='';for(const [name,schema] of Object.entries(method.params_schema.properties))html+=input(name,schema)+(required.has(name)?'<span class="small">必填</span>':'');if(method.idempotency_required)html+='<label>idempotency_key</label><input name="idempotency_key" placeholder="留空时由任务 ID 生成">';if(method.destructive)html+='<p class="small">删除类操作会先产生确认任务；随后将 confirm_token 粘贴到下方。</p><label>confirm_token</label><input name="confirm_token">';document.getElementById('form').innerHTML=html;document.querySelectorAll('[data-method]').forEach(x=>x.classList.toggle('selected',x.dataset.method===method.method));}
function renderMethods(){const holder=document.getElementById('methods');holder.innerHTML=methods.map(m=>'<button data-method="'+m.method+'" '+(m.status!=='implemented'?'disabled':'')+'>'+m.method+'<br><span class="small">'+m.status+'</span></button>').join('<br>');holder.querySelectorAll('[data-method]').forEach(x=>x.onclick=()=>selectMethod(methods.find(m=>m.method===x.dataset.method)));}
function params(){const value={};for(const [name,schema] of Object.entries(selected.params_schema.properties)){let raw=document.querySelector('[name="'+name+'"]').value;if(raw==='')continue;if(schema.type==='boolean')value[name]=raw==='true';else if(schema.type==='integer')value[name]=Number(raw);else if(schema.type==='array'){try{value[name]=JSON.parse(raw)}catch(e){throw Error(name+' 必须是 JSON 数组')}}else value[name]=raw;}return value}
async function loadTasks(){const data=await api('/api/tasks');const box=document.getElementById('tasks');const held=data.active_context?(' · 保留 '+data.active_context.context+' 至 '+new Date(data.active_context.reset_due_at*1000).toLocaleTimeString()):'';box.innerHTML=data.tasks.map(t=>{const phases=Object.entries(t.phases||{}).map(([name,value])=>name+':'+value.status).join(' · ');return '<div class="task"><button data-task="'+t.id+'">'+t.method+'</button> <span class="status '+t.status+'">'+t.status+'</span><div class="small">'+t.id+' · '+new Date(t.created_at*1000).toLocaleString()+(t.context?' · '+t.context:'')+(t.duplicate_of?' · duplicate of '+t.duplicate_of:'')+'</div><div class="small">'+phases+'</div></div>'}).join('')+(held?'<div class="task small">'+held.slice(3)+'</div>':'');box.querySelectorAll('[data-task]').forEach(x=>x.onclick=()=>showTask(x.dataset.task));}
async function showTask(id){selectedTask=id;const task=await api('/api/tasks/'+id);document.getElementById('result').textContent=JSON.stringify(task,null,2);}
document.getElementById('enqueue').onclick=async()=>{try{if(!selected)throw Error('先选择功能');const key=document.querySelector('[name="idempotency_key"]')?.value;const token=document.querySelector('[name="confirm_token"]')?.value;const body={method:selected.method,params:params()};if(key)body.idempotency_key=key;if(token)body.confirm_token=token;const task=await api('/api/tasks',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify(body)});document.getElementById('note').textContent=task.duplicate_of?'重复请求已关联到 '+task.duplicate_of:'任务已加入队列';showTask(task.id);loadTasks()}catch(e){document.getElementById('note').textContent=e.message}};
document.getElementById('refresh').onclick=()=>{loadTasks();if(selectedTask)showTask(selectedTask)};
api('/api/capabilities').then(x=>{methods=x.methods;renderMethods();selectMethod(methods.find(m=>m.method==='session.status'))});setInterval(()=>{loadTasks();if(selectedTask)showTask(selectedTask)},1500);loadTasks();
</script>'''


def serve(config, host, port):
    if host not in ("127.0.0.1", "localhost", "::1"):
        raise AutomationError("UNSAFE_BIND", "Demo may bind only to localhost")
    config.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    config.state_dir.chmod(0o700)
    metadata = capabilities()["methods"]
    available_methods = {item["method"] for item in metadata if item["status"] == "implemented"}
    idempotent_methods = {item["method"] for item in metadata if item.get("idempotency_required")}
    store = TaskStore(config.state_dir / "demo.sqlite3", idempotent_methods)
    runner = TaskRunner(store, lambda request: service.call(config, request))
    work = queue.Queue()
    for task in store.list():
        if task["status"] == "queued": work.put(task["id"])
    def worker():
        while True:
            try:
                task_id = work.get(timeout=runner.idle_timeout())
            except queue.Empty:
                runner.reset_due()
                continue
            runner.reset_due()
            runner.run_task(task_id)
    threading.Thread(target=worker, daemon=True).start()
    class Handler(BaseHTTPRequestHandler):
        def json(self, value, status=200):
            raw=json.dumps(value,ensure_ascii=False).encode();self.send_response(status);self.send_header("Content-Type","application/json; charset=utf-8");self.send_header("Content-Length",str(len(raw)));self.end_headers();self.wfile.write(raw)
        def do_GET(self):
            if self.path=="/": self.send_response(200);self.send_header("Content-Type","text/html; charset=utf-8");self.end_headers();self.wfile.write(PAGE.encode())
            elif self.path=="/api/capabilities": self.json(capabilities())
            elif self.path=="/api/tasks": self.json({"tasks":store.list(),"active_context":store.active_context()})
            elif self.path.startswith("/api/tasks/"):
                try:self.json(store.get(self.path.rsplit('/',1)[1]))
                except KeyError:self.json({"error":"not found"},404)
            else:self.json({"error":"not found"},404)
        def do_POST(self):
            if self.path!="/api/tasks": return self.json({"error":"not found"},404)
            try:
                length=int(self.headers.get("Content-Length","0"))
                if length < 1 or length > 1024 * 1024:
                    raise ValueError("request body must be 1 byte to 1 MiB")
                request=json.loads(self.rfile.read(length))
                if (not isinstance(request,dict) or request.get("method") not in available_methods
                        or not isinstance(request.get("params",{}),dict)):
                    raise ValueError("invalid method or params")
                task, fresh=store.enqueue(request)
                if fresh: work.put(task["id"])
                self.json(task,201)
            except (ValueError,json.JSONDecodeError) as error: self.json({"error":str(error) or "invalid request"},400)
        def log_message(self,*args): pass
    server=ThreadingHTTPServer((host,port),Handler)
    print(json.dumps({"ok":True,"result":{"url":f"http://{host}:{port}","status":"ready"}},ensure_ascii=False),flush=True)
    try:server.serve_forever()
    finally:
        server.server_close()
        store.close()
