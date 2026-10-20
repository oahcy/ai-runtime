'use strict';
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const path = require('node:path');
const source = fs.readFileSync(path.join(__dirname, '../plugins/game-agent/agent.js'), 'utf8');
const handlers = {}, sent = [];
const original = function () {};
const consoleObject = {log: original, info: original, warn: original, error: original};
const task = {
 onOpen: f => handlers.open = f, onMessage: f => handlers.message = f,
 onClose: f => handlers.close = f, onError: f => handlers.error = f,
 send: ({data}) => sent.push(JSON.parse(data)), close: () => {}
};
const context = vm.createContext({module: {exports: {}}, console: consoleObject,
 wx: {connectSocket: () => task}, eval: undefined, Function: undefined}, {codeGeneration: {strings:false, wasm:false}});
const receive = row => handlers.message({data: JSON.stringify(row)});
const flush = () => new Promise(resolve => setImmediate(resolve));
let seq = 0;
async function command(c, id=String(++seq)) {
 receive({type:'command', id, command:c}); await flush();
 return sent.filter(r => r.id === id).at(-1);
}
(async () => {
 vm.runInContext(source, context);
 let getterCount=0;
 const node={x:1, move(n){this.x+=n; return this;}, async answer(){return 42;},
 get dangerous(){getterCount++; throw Error('getter error');}};
 node.self=node;
 const config={url:'ws://127.0.0.1:1/ws',token:'test',roots:{node}};
 const api=context.module.exports(config);
 assert.throws(()=>context.module.exports(config), /already loaded/);
 handlers.open(); assert.equal(sent[0].token,'test');
 receive({type:'welcome',target_id:'test-target'});
 const roots=(await command({op:'roots'})).value;
 const object=roots[0].object.$ref;
 assert.equal((await command({op:'get',object,property:'self'})).value.$ref,object);
 assert((await command({op:'keys',object})).value.keys.includes('dangerous'));
 assert.equal(getterCount,0);
 assert.match((await command({op:'get',object,property:'dangerous'})).error,/getter error/);
 await command({op:'set',object,property:'x',value:{value:10}});
 await command({op:'call',object,method:'move',args:[{value:2}]},'mutation');
 await command({op:'call',object,method:'move',args:[{value:100}]},'mutation');
 assert.equal(node.x,12);
 assert.equal((await command({op:'call',object,method:'answer',args:[]})).value.value,42);
 await command({op:'set',object,property:'other',value:{$ref:object}});
 assert.equal(node.other,node);
 assert.match((await command({op:'get',object,property:'constructor'})).error,/Invalid property/);
 assert.match((await command({op:'set',object,property:'x',value:{$ref:'missing'}})).error,/Unknown/);
 await command({op:'release',object});
 assert.match((await command({op:'get',object,property:'x'})).error,/Unknown/);
 assert.notEqual((await command({op:'roots'})).value[0].object.$ref,object);
 consoleObject.log('test log'); assert(sent.some(r=>r.type==='log'&&r.message.includes('test log')));
 handlers.close(); assert.equal(consoleObject.log,original); assert.equal(context.__AI_RUNTIME_AGENT__,undefined);
 process.stdout.write('Agent object protocol passes with string code generation disabled\n');
})().catch(error=>{console.error(error);process.exitCode=1;});
