
const assert = require('node:assert/strict');
const sockets = [], timers = new Map(), listeners = {};
let tid = 0;
global.location = {hash:'#credential', host:'127.0.0.1:1234', protocol:'http:'};
global.addEventListener = (name, fn) => listeners[name] = fn;
global.setTimeout = fn => { timers.set(++tid, fn); return tid; };
global.clearTimeout = id => timers.delete(id);
global.WebSocket = class {
 constructor(url) { this.url=url; sockets.push(this); }
 send(value) { this.sent=JSON.parse(value); }
 close() { this.onclose?.({code:1000}); }
};
const errors=[];
console.error=(...args)=>errors.push(args);
/* CLIENT */
const values=[], statuses=[];
const unsubStatus=RuntimePreview.onStatus(s=>statuses.push(s));
const unsubBad=RuntimePreview.subscribe(()=>{throw Error('renderer failed')});
const unsub=RuntimePreview.subscribe((state, meta)=>values.push([state,meta]));
assert.equal(sockets.length,1);
const first=sockets[0];
first.onopen();
assert.deepEqual(first.sent,{token:'credential'});
first.onmessage({data:'invalid'});
first.onmessage({data:JSON.stringify({revision:1,state:{score:7},history:[]})});
assert.equal(values[0][0].score,7);
assert.equal(errors.length,1);
first.onmessage({data:JSON.stringify({revision:1,state:{score:8},history:[]})});
assert.equal(values.length,1);
first.onclose({code:1006});
assert.equal(timers.size,1);
const [id,retry]=[...timers][0]; timers.delete(id);retry();
const second=sockets[1];
second.onopen();
second.onmessage({data:JSON.stringify({revision:0,state:{score:9},history:[]})});
assert.equal(values[1][0].score,9);
first.onmessage({data:JSON.stringify({revision:2,state:{score:99},history:[]})});
assert.equal(values.length,2);
second.onclose({code:1008});
assert.equal(statuses.at(-1),'unauthorized');
assert.equal(timers.size,0);
location.hash='#new';listeners.hashchange();
assert.equal(sockets.length,3);
unsub();unsubBad();unsubStatus();
assert.equal(timers.size,0);
