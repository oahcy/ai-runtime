"""Bounded in-memory projection of Jupyter outputs for a session viewer."""
import copy
import json

MAX_BYTES = 3 * 1024 * 1024
MAX_ITEMS = 200


class OutputBuffer:
    def __init__(self):
        self.items = []
        self.waiting = set()
        self.counter = 0
        self.dropped = 0

    def clear(self, area):
        self.items = [item for item in self.items if item['area'] != area]
        self.waiting.discard(area)

    def apply(self, eid, kind, content):
        area = eid or 'background'
        if kind == 'clear_output':
            if content.get('wait'):
                self.waiting.add(area)
                if len(self.waiting) > MAX_ITEMS:
                    self.waiting.clear()
                    self.waiting.add(area)
            else:
                self.clear(area)
            return
        if area in self.waiting:
            self.clear(area)
        payload = copy.deepcopy(content)
        display_id = payload.get('transient', {}).get('display_id')
        if kind == 'update_display_data':
            if display_id:
                for item in self.items:
                    if item.get('display_id') == display_id:
                        item['payload'] = payload
            self.trim()
            return
        if kind == 'stream' and self.items and self.items[-1]['kind'] == kind and self.items[-1]['area'] == area and self.items[-1]['payload'].get('name') == payload.get('name'):
            self.items[-1]['payload']['text'] += payload.get('text', '')
        else:
            self.counter += 1
            self.items.append(dict(id=self.counter, area=area, kind=kind, display_id=display_id, payload=payload))
        self.trim()

    def trim(self):
        # Never let a single oversized MIME bundle permanently crowd out the viewer.
        for item in self.items:
            if len(json.dumps(item).encode()) > MAX_BYTES // 2:
                item['kind'] = 'stream'
                item['payload'] = {'name': 'stderr', 'text': '[Output too large for live viewer; inspect execution artifacts.]'}
                self.dropped += 1
        while len(self.items) > MAX_ITEMS or len(json.dumps(self.items).encode()) > MAX_BYTES:
            self.items.pop(0)
            self.dropped += 1
        # clear_output(wait=True) on empty executions must also remain bounded.
        if len(self.waiting) > MAX_ITEMS:
            self.waiting.clear()

    def state(self):
        return {'items': copy.deepcopy(self.items), 'dropped': self.dropped}


OUTPUT_HTML = r'''<!doctype html><meta charset="utf-8"><title>Runtime · 内核输出</title>
<style>
body{margin:0;background:#0d1423;color:#e0e8f5;font:15px system-ui}header{position:sticky;top:0;background:#121e32;padding:18px 24px;border-bottom:1px solid #30425b}h1{font-size:20px;margin:0 0 6px}#status,#limit{font-size:13px;color:#9eb2ce}main{max-width:1100px;margin:auto;padding:20px}.output{border:1px solid #30425b;border-radius:10px;background:#131f32;margin:12px 0;padding:14px}.tag{font:12px monospace;color:#91a9c8;margin-bottom:10px}pre{white-space:pre-wrap;overflow-wrap:anywhere;margin:0;font:14px/1.5 monospace}img{max-width:100%;height:auto}iframe{background:white;width:100%;height:340px;border:0;border-radius:6px;resize:vertical}.error{color:#ffa8a8}#empty{color:#9eb2ce;padding:30px}
</style>
<header><h1>内核实时输出</h1><span id="status">连接中…</span> <span id="limit"></span></header>
<main><p id="empty">等待 display() 或文本输出…</p><div id="outputs"></div></main>
<script>
const container=document.getElementById('outputs'), nodes=new Map();
RuntimePreview.onStatus(s=>document.getElementById('status').textContent=s);
function render(item){
 const box=document.createElement('section');box.className='output';
 const tag=document.createElement('div');tag.className='tag';tag.textContent=item.area.slice(0,8)+' · '+item.kind;box.append(tag);
 const p=item.payload||{}, data=p.data||{};
 function pre(text,error=false){const e=document.createElement('pre');e.textContent=text;e.className=error?'error':'';box.append(e);}
 if(item.kind==='stream')pre(p.text||'',p.name==='stderr');
 else if(item.kind==='error')pre((p.traceback||[p.ename+': '+p.evalue]).join('\n').replace(/\x1b\[[0-9;]*m/g,''),true);
 else if(typeof data['image/png']==='string'||typeof data['image/jpeg']==='string'){
   const mime=typeof data['image/png']==='string'?'image/png':'image/jpeg';
   const img=document.createElement('img');img.alt='内核图片输出';img.src='data:'+mime+';base64,'+data[mime];box.append(img);
 }else if(typeof data['text/html']==='string'){
   const frame=document.createElement('iframe');frame.setAttribute('sandbox','');frame.referrerPolicy='no-referrer';frame.title='隔离的 HTML 输出';
   frame.srcdoc='<meta http-equiv="Content-Security-Policy" content="default-src &#39;none&#39;; style-src &#39;unsafe-inline&#39;; img-src data:;"><meta charset="utf-8">'+data['text/html'];box.append(frame);
 }else if(data['application/json']!==undefined)pre(JSON.stringify(data['application/json'],null,2));
 else pre(data['text/plain']||'[此 MIME 类型暂不支持预览]');
 return box;
}
RuntimePreview.subscribe(state=>{
 const nearBottom=innerHeight+scrollY>=document.documentElement.scrollHeight-100;
 const items=state.items||[], keep=new Set(items.map(x=>x.id));
 for(const [id,row] of nodes)if(!keep.has(id)){row.node.remove();nodes.delete(id);}
 for(const item of items){const signature=JSON.stringify(item),old=nodes.get(item.id);
   if(old&&old.signature===signature)continue;
   const node=render(item);if(old)old.node.replaceWith(node);else container.append(node);
   nodes.set(item.id,{node,signature});
 }
 document.getElementById('empty').hidden=items.length>0;
 document.getElementById('limit').textContent=state.dropped?' · 已省略 '+state.dropped+' 条超限输出':'';
 if(nearBottom)scrollTo(0,document.documentElement.scrollHeight);
});
</script>'''
