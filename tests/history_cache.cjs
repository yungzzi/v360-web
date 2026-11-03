const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../src/v360_web/web/app.js'),'utf8');
const code=source.slice(source.indexOf('const historyCache='),source.indexOf('async function apiRequest('));
function setup(){
 const storage=new Map(),requests=[];
 const ctx={signedIn:true,sessionStorage:{getItem:k=>storage.get(k)||null,setItem:(k,v)=>storage.set(k,v),removeItem:k=>storage.delete(k)},apiRequest:(path)=>new Promise((resolve,reject)=>requests.push({path,resolve,reject}))};
 vm.createContext(ctx);vm.runInContext(code,ctx);return {ctx,requests,storage};
}
test('same in-flight date request is shared and returning to a date is cached',async()=>{
 const q=setup(),url='/api/library/timeline?day=2026-11-03';const a=q.ctx.api(url),b=q.ctx.api(url);assert.equal(q.requests.length,1);
 q.requests[0].resolve({items:[{id:'first'}]});await Promise.all([a,b]);
 assert.equal((await q.ctx.api(url)).items[0].id,'first');assert.equal(q.requests.length,1);
});
test('logout invalidation cannot be repopulated by the previous account response',async()=>{
 const q=setup(),p=q.ctx.api('/api/library/timeline?day=2026-11-03');
 q.ctx.clearHistoryCache();q.requests[0].resolve({items:[{id:'private-old-account'}]});
 await assert.rejects(p,/登录状态/);assert.equal(q.storage.size,0);
});
test('mutation methods and media requests never use metadata cache',async()=>{
 const q=setup();const a=q.ctx.api('/api/jobs/retry-once/one',{method:'POST'});q.requests[0].resolve({ok:true});await a;
 const b=q.ctx.api('/api/jobs/retry-once/one',{method:'POST'});assert.equal(q.requests.length,2);q.requests[1].resolve({ok:true});await b;
 assert.equal(q.storage.size,0);
});
