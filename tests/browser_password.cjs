const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../src/v360_web/web/app.js'),'utf8');
const handler=source.slice(source.indexOf("$('login-form').addEventListener"),source.indexOf('async function restoreLogin'));
for(const password of ['这是一段用于测试的中文密码',' 中文密码允许保留两侧空格 '])test('browser submits Unicode password in JSON '+password.length,async()=>{
 let submit,request,shown=false;const elements={'login-form':{addEventListener:(type,fn)=>submit=fn},token:{value:password},remember:{checked:true}};
 const ctx={$:id=>elements[id],showDashboard:()=>shown=true,appFetch:async(url,options)=>{new Headers(options.headers);request={url,options};return {ok:true};}};
 vm.createContext(ctx);vm.runInContext(handler,ctx);await submit({preventDefault(){}});
 assert.equal(shown,true);assert.equal(request.options.headers.Authorization,undefined);
 assert.equal(JSON.parse(request.options.body).password,password);
});
