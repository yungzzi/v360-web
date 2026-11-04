const {test}=require('node:test');const assert=require('node:assert/strict');const fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const source=fs.readFileSync(path.resolve(__dirname,'../src/v360_web/web/app.js'),'utf8');
test('Chinese entry is text from first focus, reveal changes only masking and preserves composition content',()=>{
 let click;const classes=new Set();const field={type:'text',value:'这是测试中文密码',selectionStart:3,selectionEnd:3,classList:{remove:k=>classes.delete(k),contains:k=>classes.has(k),toggle:(k,on)=>on?classes.add(k):classes.delete(k)},focus(){},setSelectionRange(a,b){this.selectionStart=a;this.selectionEnd=b;}};
 const button={dataset:{passwordToggle:'new-password'},setAttribute(k,v){this[k]=v;},addEventListener(k,f){click=f;}};
 const ctx={CSS:{supports:()=>true},$:id=>id==='management-dialog'?{addEventListener(){}}:field,document:{querySelectorAll:()=>[button]},window:{addEventListener(){}}};vm.createContext(ctx);vm.runInContext(source.slice(source.indexOf('function resetPasswordVisibility()')),ctx);
 assert.equal(field.type,'text');assert.equal(classes.has('phrase-visible'),false);
 click();assert.equal(field.type,'text');assert.equal(field.value,'这是测试中文密码');assert.equal(button['aria-pressed'],'true');assert.equal(field.selectionStart,3);
 ctx.resetPasswordVisibility();assert.equal(field.type,'text');assert.equal(classes.has('phrase-visible'),false);assert.equal(button['aria-pressed'],'false');
});
test('login and change fields are never initially native password fields',()=>{
 const html=fs.readFileSync(path.resolve(__dirname,'../src/v360_web/web/index.html'),'utf8');
 for(const id of ['token','current-password','new-password','confirm-password']){const tag=html.match(new RegExp('<input id="'+id+'"[^>]*>'))[0];assert.match(tag,/type="text"/);assert.match(tag,/autocomplete="off"/);assert.match(tag,/phrase-field/);}
});
