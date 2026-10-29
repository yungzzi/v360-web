"use strict";
const $ = id => document.getElementById(id);
let token = "", socket = null, media = null, source = null, objectURL = null;
let chunks = [], mime = null, playing = false, requestGeneration = 0;
const labels = {starting:"正在连接",live:"正在观看",stopped:"已停止",failed:"连接失败"};
const errors = {camera_busy:"摄像头正在处理其他任务，请稍后重试。",viewer_limit:"观看人数已达上限。",session_limit:"本次观看已达 30 分钟，请重新连接。",read_timeout:"摄像头暂时没有返回画面，请重新连接。",sdk_connect:"摄像头连接失败，请稍后重试。",live_sequence:"视频传输中断，请重新连接。"};
function status(text) {$('notice').textContent=text;}
function resetPlayer(message="已停止观看。") {
  requestGeneration++; playing=false;
  if(socket){const previous=socket;socket=null;previous.close();}
  $('video').pause();$('video').removeAttribute('src');$('video').load();
  if(objectURL)URL.revokeObjectURL(objectURL);
  objectURL=null;media=source=null;chunks=[];mime=null;
  $('start').disabled=false;$('stop').disabled=true;$('empty').hidden=false;
  $('connection').textContent='待连接';$('state').textContent='未开始';$('live-dot').classList.remove('active');
  $('buffer').textContent='—';$('player-message').textContent=message;status(message);
}
function fail(message){resetPlayer(message);}
function pump(){
  if(!source || source.updating || !playing)return;
  try{
    const v=$('video');
    if(v.buffered.length){
      const end=v.buffered.end(v.buffered.length-1),start=v.buffered.start(0);
      if(v.currentTime<start || end-v.currentTime>4)v.currentTime=Math.max(start,end-1);
      if(v.currentTime>15 && start<v.currentTime-12){source.remove(start,v.currentTime-10);return;}
    }
    if(chunks.length)source.appendBuffer(chunks.shift());
  }catch{fail('播放缓冲出现问题，请重新连接。');}
}
function prepare(){
  if(!media || media.readyState!=='open' || !mime || source)return;
  if(!MediaSource.isTypeSupported(mime)){fail('当前浏览器不支持此视频格式，请使用新版 Chrome 或 Edge。');return;}
  try{source=media.addSourceBuffer(mime);source.addEventListener('updateend',pump);source.addEventListener('error',()=>fail('视频解码失败，请重新连接。'));pump();}
  catch{fail('无法创建视频播放器。');}
}
async function poll(){
  if(!token)return;
  try{const response=await fetch('/api/status',{headers:{Authorization:'Bearer '+token}});if(response.status===401){logout();return;}if(!response.ok)return;const state=await response.json();$('viewers').textContent=state.live.viewers+' 人';}
  catch{/* Keep the active player's error reporting independent of status polling. */}
}
function logout(){resetPlayer();token='';$('login').hidden=false;$('dashboard').hidden=true;$('logout').hidden=true;$('token').value='';}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const candidate=$('token').value.trim();
  try{const response=await fetch('/api/status',{headers:{Authorization:'Bearer '+candidate}});
    if(!response.ok){$('login-message').textContent='验证未通过，请检查网页访问令牌。';return;}
    token=candidate;$('token').value='';$('login').hidden=true;$('dashboard').hidden=false;$('logout').hidden=false;poll();
  }catch{$('login-message').textContent='无法连接本地服务，请确认后端正在运行。';}
});
$('start').addEventListener('click',()=>{
  if(!window.MediaSource){status('当前浏览器不支持此播放器，请使用新版 Chrome 或 Edge。');return;}
  resetPlayer('正在连接摄像头…');playing=true;const generation=requestGeneration;
  $('start').disabled=true;$('stop').disabled=false;$('connection').textContent='连接中';$('state').textContent='正在连接';
  media=new MediaSource();objectURL=URL.createObjectURL(media);$('video').src=objectURL;media.addEventListener('sourceopen',prepare);
  const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+'/api/live/ws');socket=ws;ws.binaryType='arraybuffer';
  ws.onopen=()=>{if(socket===ws)ws.send(JSON.stringify({token}));};
  ws.onmessage=event=>{
    if(socket!==ws || generation!==requestGeneration)return;
    if(typeof event.data==='string'){
      const data=JSON.parse(event.data);
      if(data.type==='init'){mime=data.mime;prepare();}
      else if(data.type==='error' || data.type==='ended')fail(errors[data.error] || '直播连接已结束，可重新开始观看。');
    }else{if(chunks.length>=8){fail('网络或浏览器播放过慢，请重新连接。');return;}chunks.push(event.data);pump();}
  };
  ws.onclose=()=>{if(socket===ws)fail('连接已断开，请重新开始观看。');};
  ws.onerror=()=>{if(socket===ws)fail('无法建立视频连接，请确认服务可用。');};
});
$('video').addEventListener('playing',()=>{if(playing){$('empty').hidden=true;$('connection').textContent='观看中';$('state').textContent=labels.live;$('live-dot').classList.add('active');status('正在播放实时视频。');}});
$('video').addEventListener('loadeddata',()=>{if(playing)$('video').play().catch(()=>status('请点击播放器中的播放按钮。'));});
$('video').addEventListener('error',()=>{if(playing)fail('浏览器无法解码视频，请重新连接。');});
$('stop').addEventListener('click',()=>resetPlayer());$('logout').addEventListener('click',logout);
$('fullscreen').addEventListener('click',()=>{$('video').requestFullscreen?.().catch(()=>status('当前浏览器无法进入全屏。'));});
window.addEventListener('pagehide',()=>resetPlayer());
setInterval(()=>{poll();const v=$('video');if(playing&&v.buffered.length)$('buffer').textContent=Math.max(0,v.buffered.end(v.buffered.length-1)-v.currentTime).toFixed(1)+' 秒';},3000);
