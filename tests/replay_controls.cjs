// Run: node tests/replay_controls.cjs [path/to/web/app.js]
const {test}=require('node:test');
const assert=require('node:assert/strict');
const fs=require('node:fs');
const vm=require('node:vm');
const path=require('node:path');
const source=fs.readFileSync(process.argv[2]||path.resolve(__dirname,'../src/v360_web/web/app.js'),'utf8');
function setup(){
  const elements={};
  function element(id){return elements[id] ||= {value:'43200',hidden:false,events:{},pause(){this.pauses=(this.pauses||0)+1;},setAttribute(){},addEventListener(name,fn){this.events[name]=fn;}};}
  const doc=element('document'),calls=[];
  const ctx={document:doc,$:element,seekTimeline:s=>calls.push(s),clockText:s=>String(s)};
  vm.createContext(ctx);
  const handlers=source.slice(source.indexOf('const timelineSlider='),source.indexOf('function stepClip('));
  vm.runInContext('let timelineDragging=false,timelinePointer=null,timelineGesture=0,timelineCommitted=-1;'+handlers,ctx);
  return {elements,calls,fire(name,arg={pointerId:7}){(name==='pointerup'||name==='pointercancel'?doc:element('timeline-seek')).events[name](arg);}};
}
test('releasing unchanged cursor resumes once',()=>{
 const q=setup();q.fire('pointerdown');q.fire('pointerup');q.fire('change');
 assert.deepEqual(q.calls,[43200]);assert.equal(q.elements['replay-video'].pauses,1);
});
test('change before pointerup is committed once on release',()=>{
 const q=setup();q.fire('pointerdown');q.elements['timeline-seek'].value='43211';q.fire('input');q.fire('change');assert.equal(q.calls.length,0);q.fire('pointerup');assert.deepEqual(q.calls,[43211]);
});
test('many drag moves only seek the final position',()=>{
 const q=setup();q.fire('pointerdown');for(const n of [43201,43202,43200]){q.elements['timeline-seek'].value=String(n);q.fire('input');}assert.equal(q.calls.length,0);q.fire('pointerup');assert.deepEqual(q.calls,[43200]);
});
test('cancelled pointer does not commit',()=>{
 const q=setup();q.fire('pointerdown');q.fire('pointercancel');q.fire('pointerup');assert.deepEqual(q.calls,[]);
});
test('keyboard changes still seek independently',()=>{
 const q=setup();for(const n of [43201,43202]){q.elements['timeline-seek'].value=String(n);q.fire('input');q.fire('change');}assert.deepEqual(q.calls,[43201,43202]);
});
test('busy selection keeps latest intent and discards it on a different day',()=>{
 let calls=[],day='2026-11-01';const ctx={selectedClipId:null,libraryBusy:true,pendingTimelineSeek:null,prefetchInfo:null,signedIn:true,selectTimelineTime(){},libraryMessage(){},recordingAction:(...args)=>calls.push(args),$:id=>id==='recording-day'?{value:day}:{hidden:false}};vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function chooseTimeline('),source.indexOf('function seekTimeline(')),ctx);
 ctx.chooseTimeline({id:'one',second:1},0);ctx.chooseTimeline({id:'two',second:2},3);assert.equal(calls.length,0);ctx.libraryBusy=false;ctx.flushTimelineSeek();assert.equal(calls.length,1);assert.equal(calls[0][0].id,'two');
 ctx.libraryBusy=true;ctx.chooseTimeline({id:'three',second:3},0);day='2026-11-02';ctx.libraryBusy=false;ctx.flushTimelineSeek();assert.equal(calls.length,1);
});

test('timeline gaps snap forward only; no backwards fallback',()=>{
 const chosen=[];const nodes={};const ctx={selectedClipId:null,pendingTimelineSeek:null,timelineData:{items:[{id:'a',second:100,duration_hint:10},{id:'b',second:130,duration_hint:10}]},$:id=>nodes[id] ||= {pause(){},textContent:''},clockText:String,selectTimelineTime(){},renderTimeline(){},chooseTimeline:(i,o)=>chosen.push([i.id,o])};vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function seekTimeline('),source.indexOf('const timelineSlider=')),ctx);
 ctx.seekTimeline(105);ctx.seekTimeline(114);ctx.seekTimeline(120);ctx.seekTimeline(500);
 assert.deepEqual(chosen,[['a',5],['b',0],['b',0]]);
 assert.match(nodes['timeline-message'].textContent,/之后没有/);
 ctx.timelineData={items:[]};ctx.seekTimeline(20);assert.equal(chosen.length,3);
});

function setupClipSteps(selectedClipId, second) {
 const clips = [
  {id:'earlier', second:22205},
  {id:'current', second:22505},
  {id:'later', second:22805},
 ];
 const chosen = [];
 const nodes = {
  'timeline-time':{value:String(second)},
  'timeline-message':{textContent:''},
 };
 const ctx = {
  timelineData:{items:clips}, timelineCenter:0, selectedClipId,
  $:id=>nodes[id], clockSeconds:Number, renderTimeline(){},
  selectTimelineTime(value){nodes['timeline-time'].value=String(value);},
  chooseTimeline(item, offset){ctx.selectedClipId=item.id;chosen.push([item.id,offset]);},
 };
 vm.createContext(ctx);
 vm.runInContext(source.slice(source.indexOf('function stepClip('),source.indexOf("$('clip-before').addEventListener")),ctx);
 return {ctx, chosen, nodes};
}

test('previous switches clips even after playback advances inside the current clip',()=>{
 const {ctx, chosen}=setupClipSteps('current',22509);
 ctx.stepClip(-1);
 assert.deepEqual(chosen,[['earlier',0]]);
 ctx.stepClip(1);
 assert.deepEqual(chosen,[['earlier',0],['current',0]]);
});

test('stepping uses the latest selected clip while playback still shows an older position',()=>{
 const {ctx, chosen, nodes}=setupClipSteps('later',22816);
 ctx.stepClip(-1);
 nodes['timeline-time'].value='22817';
 ctx.stepClip(-1);
 ctx.stepClip(-1);
 assert.deepEqual(chosen,[['current',0],['earlier',0]]);
 assert.match(nodes['timeline-message'].textContent,/没有更早/);
});

test('without a selected clip, adjacent buttons use the requested time',()=>{
 const {ctx, chosen}=setupClipSteps(null,22509);
 ctx.stepClip(1);
 assert.deepEqual(chosen,[['later',0]]);
});
