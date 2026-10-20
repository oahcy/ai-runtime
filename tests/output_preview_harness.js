const assert=require('node:assert/strict'),vm=require('node:vm'),fs=require('node:fs');
const script=fs.readFileSync(0,'utf8');
class Element{
 constructor(tag){this.tag=tag;this.children=[];this.attrs={};}
 append(node){this.children.push(node);node.parent=this;}
 remove(){this.parent.children=this.parent.children.filter(n=>n!==this);}
 replaceWith(node){const i=this.parent.children.indexOf(this);this.parent.children[i]=node;node.parent=this.parent;}
 setAttribute(k,v){this.attrs[k]=v;}
}
const ids=Object.fromEntries(['outputs','status','empty','limit'].map(id=>[id,new Element(id)]));
let update;
vm.runInNewContext(script,{document:{getElementById:id=>ids[id],createElement:tag=>new Element(tag),documentElement:{scrollHeight:100}},RuntimePreview:{onStatus:f=>f('connected'),subscribe:f=>update=f},innerHeight:500,scrollY:0,scrollTo:()=>{}});
function item(id,data){return {id,area:'cell',kind:'display_data',payload:{data}};}
update({items:[item(1,{'image/png':'abc'})]});
assert.equal(ids.outputs.children[0].children[1].src,'data:image/png;base64,abc');
update({items:[item(1,{'text/html':'<script>parent.stolen=true</script><b>Hello</b>'})]});
assert.equal(ids.outputs.children.length,1);
const iframe=ids.outputs.children[0].children[1];
assert.equal(iframe.tag,'iframe');assert.equal(iframe.attrs.sandbox,'');assert(iframe.srcdoc.includes('Content-Security-Policy'));
const stable=ids.outputs.children[0];
update({items:[item(1,{'text/html':'<script>parent.stolen=true</script><b>Hello</b>'})]});
assert.equal(ids.outputs.children[0],stable);
update({items:[{id:2,area:'cell',kind:'stream',payload:{text:'<script>bad</script>',name:'stdout'}}]});
assert.equal(ids.outputs.children.length,1);assert.equal(ids.outputs.children[0].children[1].textContent,'<script>bad</script>');
update({items:[]});assert.equal(ids.outputs.children.length,0);assert.equal(ids.empty.hidden,false);
console.log('output renderer: images, updates, isolated HTML, text escaping, clear passed');
