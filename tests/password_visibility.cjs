const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../src/v360_web/web/app.js'),'utf8');
test('Chinese input uses a real text field, preserves content and resets to masked',()=>{
 let click;const field={type:'password',value:'这是测试中文密码',selectionStart:3,selectionEnd:3,focus(){},setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};
 const button={dataset:{passwordToggle:'new-password'},setAttribute(k,v){this[k]=v;},addEventListener(k,f){click=f;}};
 const ctx={$:id=>id==='management-dialog'?{addEventListener(){}}:field,document:{querySelectorAll:()=>[button]},window:{addEventListener(){}}};vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function resetPasswordVisibility()')),ctx);
 click();assert.equal(field.type,'text');assert.equal(field.value,'这是测试中文密码');assert.equal(button['aria-pressed'],'true');assert.equal(field.selectionStart,3);
 ctx.resetPasswordVisibility();assert.equal(field.type,'password');assert.equal(button['aria-pressed'],'false');assert.equal(field.value,'这是测试中文密码');
});
