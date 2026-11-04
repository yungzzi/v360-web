"use strict";
const $ = id => document.getElementById(id);
const BASE_PATH = new URL(document.currentScript.src).pathname.replace(/\/assets\/app\.js$/, "");
const appUrl = path => BASE_PATH + path;
const appFetch = (path, options) => fetch(appUrl(path), options);
let signedIn = false, socket = null, media = null, source = null, objectURL = null;
let chunks = [], mime = null, playing = false, requestGeneration = 0, serviceStopped = false;
let viewerId=null;
let lensViews=[{id:0,name:"主镜头",ptz:true}],lensListKey="";
const ptzButtons=()=>document.querySelectorAll('[data-direction]');
const labels = {starting:"正在连接",live:"正在观看",stopped:"已停止",failed:"连接失败"};
const errors = {lens_busy:"另一个窗口正在观看不同镜头，请先停止那个窗口，再切换。",lens_unavailable:"此镜头尚未配置，请刷新页面。",live_audio_format:'声音格式发生变化，请重新连接。',live_audio_sequence:'声音传输中断，请重新连接。',camera_busy:"摄像头正在处理其他任务，请稍后重试。",viewer_limit:"观看人数已达上限。",session_limit:"本次观看已达 30 分钟，请重新连接。",read_timeout:"摄像头暂时没有返回画面，请重新连接。",sdk_connect:"摄像头连接失败，请稍后重试。",live_sequence:"视频传输中断，请重新连接。"};
function status(text) {$('notice').textContent=text;}
function resetPlayer(message="已停止观看。") {
  if(viewerId&&signedIn)sendMove('stop');viewerId=null;ptzButtons().forEach(b=>b.disabled=true);requestGeneration++; playing=false;
  if(socket){const previous=socket;socket=null;previous.close();}
  $('snapshot').disabled=true;$('video').pause();$('video').muted=true;$('sound').disabled=true;$('sound').textContent='开启声音';$('sound').setAttribute('aria-pressed','false');$('audio-state').textContent=Number($('lens').value)===0?'连接后可开启声音':'此镜头仅提供画面';$('video').removeAttribute('src');$('video').load();
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
  if(!signedIn || serviceStopped)return;
  try{const response=await appFetch('/api/status',{});if(response.status===401){showLogin();return;}if(!response.ok)return;const state=await response.json();renderLenses(state.lenses);$('viewers').textContent=state.live.viewers+' 人';renderBackup(state.backup);const b=state.battery||{};const pct=b.percent;const fill=$('battery-fill');fill.style.width=pct==null?'0%':Math.max(0,Math.min(100,pct))+'%';$('battery-percent').textContent=pct==null?'—':pct+'%';$('battery-badge').classList.toggle('low',pct!=null&&pct<=20);$('battery-badge').classList.toggle('stale',!!b.stale);$('battery-charge').hidden=b.charging!==true;const batteryLabel=pct==null?'尚未取得电量':'电量 '+pct+'%'+(b.updated_at?'，读取于 '+new Date(b.updated_at*1000).toLocaleString():'');$('battery-badge').setAttribute('aria-label',batteryLabel);$('battery-badge').title=batteryLabel;$('battery').textContent=b.percent==null?'未知':b.percent+'%';$('charging').textContent=b.charging==null?'未知':b.charging?'设备报告充电中':'未报告外接充电';$('battery-time').textContent=b.updated_at?new Date(b.updated_at*1000).toLocaleString()+(b.stale?' · 待读取更新':' · 最近读取'):'尚未读取电量';$('local-controls').hidden=!state.local_shutdown_available;}
  catch{/* Keep the active player's error reporting independent of status polling. */}
}
function showLogin(){resetPasswordVisibility();if($('management-dialog').open)$('management-dialog').close();clearHistoryCache();mediaSessionUntil=0;clearReplay();resetPlayer();signedIn=false;$('login').hidden=false;$('dashboard').hidden=true;$('logout').hidden=true;$('token').value='';}
function showDashboard(){serviceStopped=false;signedIn=true;$('token').value='';$('login').hidden=true;$('dashboard').hidden=false;$('logout').hidden=false;poll();}
async function logout(){clearHistoryCache();mediaSessionUntil=0;
  resetPlayer();
  try{const response=await appFetch('/api/auth/session',{method:'DELETE'});if(!response.ok)throw new Error();showLogin();$('login-message').textContent='已退出登录，此设备的登录状态已清除。';}
  catch{status('退出未完成，请检查网络后重试。');}
}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();resetPasswordVisibility();const candidate=$('token').value;
  try{const response=await appFetch('/api/auth/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:candidate,remember:$('remember').checked})});
    if(!response.ok){$('login-message').textContent=response.status===429?'尝试次数过多，请稍等一分钟再试。':'密码不正确，请重新输入。';return;}
    showDashboard();
  }catch{$('login-message').textContent='无法连接服务，请检查网络后重试。';}
});
async function restoreLogin(){try{const response=await appFetch('/api/status');if(response.ok)showDashboard();}catch{$('login-message').textContent='暂时无法连接服务，请检查网络后重试。';}}
$('start').addEventListener('click',()=>{
  if(!window.MediaSource){status('当前浏览器不支持此播放器，请使用新版 Chrome 或 Edge。');return;}
  resetPlayer('正在连接摄像头…');playing=true;const generation=requestGeneration;
  $('start').disabled=true;$('stop').disabled=false;$('connection').textContent='连接中';$('state').textContent='正在连接';
  media=new MediaSource();objectURL=URL.createObjectURL(media);$('video').src=objectURL;media.addEventListener('sourceopen',prepare);
  const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+appUrl('/api/live/ws'));socket=ws;ws.binaryType='arraybuffer';
  ws.onopen=()=>{if(socket===ws)ws.send(JSON.stringify({lens:Number($('lens').value)}));};
  ws.onmessage=event=>{
    if(socket!==ws || generation!==requestGeneration)return;
    if(typeof event.data==='string'){
      const data=JSON.parse(event.data);
      if(data.type==='state'){viewerId=data.viewer_id;}
      else if(data.type==='init'){mime=data.mime;prepare();$('sound').disabled=!data.audio;$('audio-state').textContent=data.audio?'声音可用 · 默认静音':Number($('lens').value)===0?'本次连接暂无声音':'此镜头仅提供画面';ptzButtons().forEach(b=>b.disabled=!data.ptz);}
      else if(data.type==='error' || data.type==='ended')fail(errors[data.error] || '直播连接已结束，可重新开始观看。');
    }else{if(chunks.length>=8){fail('网络或浏览器播放过慢，请重新连接。');return;}chunks.push(event.data);pump();}
  };
  ws.onclose=()=>{if(socket===ws)fail('连接已断开，请重新开始观看。');};
  ws.onerror=()=>{if(socket===ws)fail('无法建立视频连接，请确认服务可用。');};
});
$('video').addEventListener('playing',()=>{if(playing){$('snapshot').disabled=false;$('empty').hidden=true;$('connection').textContent='观看中';$('state').textContent=labels.live;$('live-dot').classList.add('active');status('正在播放实时视频。');}});
$('video').addEventListener('loadeddata',()=>{if(playing)$('video').play().catch(()=>status('请点击播放器中的播放按钮。'));});
$('video').addEventListener('error',()=>{if(playing)fail('浏览器无法解码视频，请重新连接。');});
$('stop').addEventListener('click',()=>resetPlayer());$('logout').addEventListener('click',logout);
$('shutdown').addEventListener('click',()=>$('shutdown-dialog').showModal());
$('cancel-shutdown').addEventListener('click',()=>$('shutdown-dialog').close());
$('confirm-shutdown').addEventListener('click',async()=>{
  $('confirm-shutdown').disabled=true;
  try{
    const response=await appFetch('/api/local/shutdown',{method:'POST',headers:{}});
    if(!response.ok){status('未能关闭本地后端，仍可在启动终端按 Ctrl+C。');return;}
    serviceStopped=true;resetPlayer('关闭请求已发送，后端正在结束本次运行。重新启动后再刷新此页面。');
    $('start').disabled=true;$('shutdown').disabled=true;$('connection').textContent='正在关闭';$('state').textContent='正在关闭';
  }catch{status('无法确认关闭结果，请查看启动后端的终端。');}
  finally{$('shutdown-dialog').close();$('confirm-shutdown').disabled=false;}
});
$('fullscreen').addEventListener('click',()=>{const v=$('video');if(v.requestFullscreen)v.requestFullscreen().catch(()=>status('当前浏览器无法进入全屏。'));else if(v.webkitEnterFullscreen)v.webkitEnterFullscreen();else status('当前浏览器无法进入全屏，可尝试横屏观看。');});
window.addEventListener('pagehide',()=>resetPlayer());
setInterval(()=>{poll();const v=$('video');if(playing&&v.buffered.length)$('buffer').textContent=v.readyState>=3?'正常播放':'正在等待画面';},3000);


async function sendMove(direction){
  if(!viewerId||!signedIn)return;
  try{const response=await appFetch('/api/live/ptz',{method:'POST',keepalive:true,headers:{'Content-Type':'application/json'},body:JSON.stringify({viewer_id:viewerId,direction})});if(!response.ok&&direction!=='stop')status('暂时无法调整方向，请确认正在观看，或稍后重试。');}
  catch{if(direction!=='stop')status('控制连接中断，后端将尝试停止转动。');}
}
ptzButtons().forEach(button=>button.addEventListener('click',()=>sendMove(button.dataset.direction)));
window.addEventListener('blur',()=>sendMove('stop'));
document.addEventListener('visibilitychange',()=>{if(document.hidden)sendMove('stop');});

let libraryOffset=0, libraryTotal=0, libraryBusy=false, mediaSessionUntil=0;
let backupState=null, replayKey=null, backupFinished=null;
let prefetchInfo=null;
let replayWindowStart=0,replayWindowEnd=Infinity,replaySourceSeconds=null;
let timelineDragging=false, timelinePointer=null, timelineGesture=0, timelineCommitted=-1, pendingTimelineSeek=null;
let timelineData=null, timelineGeneration=0, timelineStart=0, timelineCenter=43200, replayItem=null, replayGeneration=0;
const LIBRARY_PAGE_SIZE=10;
const stateLabels={discovered:'尚未下载',downloading:'正在下载',downloaded_unverified:'待检查',verified:'已保存',evicted:'已按保留规则清理',failed:'下载失败'};
const stageLabels={connecting:'正在连接摄像头',downloading:'正在下载录像',validating:'正在检查录像',preparing_playback:'正在准备网页回放',validating_playback:'正在检查回放文件'};
const jobErrors={download_rejected:'设备暂未提供这段录像文件，后台会保留缺口并继续核对。',local_media_invalid:'文件检查未通过，已安排重新获取。',backup_yielded:'后台工作已让位或到达本轮时间上限，下轮继续。',storage_limit:'空间预算已满，暂时不能新增文件。',disk_reserve:'需要给系统保留更多磁盘空间，任务已停止。',interrupted:'上次服务重启中断了任务，可以重新操作。',preview_timeout:'准备回放用时过长，原始录像仍保留。',preview_truncated:'回放文件检查未通过，原片仍保留。'};
function libraryMessage(text){$('library-notice').textContent=text;}
function clearReplay(){prefetchInfo=null;$('prefetch-status').textContent='';if(signedIn)appFetch('/api/playback/prefetch',{method:'DELETE',keepalive:true}).catch(()=>{});pendingTimelineSeek=null;replayGeneration++;replayKey=null;replayItem=null;$('replay-snapshot').disabled=true;$('timeline-cursor').hidden=true;$('replay-title').textContent='选择一个片段';$('replay-message').textContent='选择日期，再拖动蓝线。';const v=$('replay-video');if(v){v.pause();v.removeAttribute('src');v.load();}}
function setTab(library){if(library)resetPlayer('已切换到历史录像。');else{clearReplay();if(!playing){status('已返回实时观看，可以开始连接摄像头。');$('player-message').textContent='点击「开始观看」连接摄像头';}}$('live-panel').hidden=library;$('library-panel').hidden=!library;$('tab-live').classList.toggle('selected',!library);$('tab-library').classList.toggle('selected',library);if(library){loadLibrary();recoverLibraryJob();}}
$('tab-live').addEventListener('click',()=>setTab(false));$('tab-library').addEventListener('click',()=>setTab(true));
const localToday=new Date();$('recording-day').value=[localToday.getFullYear(),String(localToday.getMonth()+1).padStart(2,'0'),String(localToday.getDate()).padStart(2,'0')].join('-');
// Only small authenticated metadata is cached; media and authorization remain server-owned.
const historyCache=new Map(),historyInflight=new Map();
let historyCacheEpoch=0;
const HISTORY_CACHE_KEY='v360-history-metadata-v1';
try{for(const [key,value] of JSON.parse(sessionStorage.getItem(HISTORY_CACHE_KEY)||'[]'))if(Date.now()-value.at<900000)historyCache.set(key,value);}catch{}
function clearHistoryCache(){historyCacheEpoch++;historyCache.clear();historyInflight.clear();try{sessionStorage.removeItem(HISTORY_CACHE_KEY);}catch{}}
function rememberHistory(path,value){historyCache.delete(path);historyCache.set(path,{at:Date.now(),value});while(historyCache.size>24)historyCache.delete(historyCache.keys().next().value);try{sessionStorage.setItem(HISTORY_CACHE_KEY,JSON.stringify([...historyCache]));}catch{}}
async function api(path,options={}){
 const metadata=!options.method&&(path.startsWith('/api/library?')||path.startsWith('/api/library/timeline?')||path.startsWith('/api/publication?'));
 if(metadata){
  const entry=historyCache.get(path),cached=entry&&Date.now()-entry.at<900000?entry:null;
  if(cached&&Date.now()-cached.at<15000)return cached.value;
  if(historyInflight.has(path))return historyInflight.get(path);
  const epoch=historyCacheEpoch;
  const request=apiRequest(path,options).then(value=>{if(epoch!==historyCacheEpoch||!signedIn)throw new Error("登录状态已变更，请重新加载。");rememberHistory(path,value);return value;}).finally(()=>{if(historyInflight.get(path)===request)historyInflight.delete(path);});historyInflight.set(path,request);
  if(cached){request.catch(()=>{});return cached.value;}
  return request;
 }
 return apiRequest(path,options);
}
async function apiRequest(path,options={}){let response;try{response=await appFetch(path,{...options,headers:{...options.headers}});}catch{throw new Error('无法连接后端，请确认服务仍在运行。');}if(response.status===401){showLogin();throw new Error('登录已失效，请重新登录。');}if(response.status===409)throw new Error('摄像头或服务正在处理其他任务，请停止直播并稍后再试。');if(!response.ok)throw new Error('操作未成功，请稍后重试。');return response.json();}
function sizeText(bytes){return bytes>=1024**3?(bytes/1024**3).toFixed(2)+' GiB':(bytes/1024**2).toFixed(1)+' MiB';}
async function playbackSession(){if(Date.now()<mediaSessionUntil)return;await api('/api/playback/session',{method:'POST'});mediaSessionUntil=Date.now()+50*60000;}
function setLibraryBusy(value){libraryBusy=value;$('timeline-seek').disabled=!timelineData;$('query-recordings').disabled=value;document.querySelectorAll('[data-recording-action]').forEach(button=>button.disabled=value);if(!value)queueMicrotask(flushTimelineSeek);}
async function waitJob(job){$('job-panel').hidden=false;for(;;){if(!signedIn||serviceStopped)throw new Error('操作页面已关闭；后台任务仍按当前状态处理。');const value=await api('/api/jobs/'+job.id);const progress=value.progress||{};$('job-label').textContent=stageLabels[progress.stage]||'正在处理';if(progress.total_seconds){$('job-progress').value=Math.min(99,100*progress.seconds/progress.total_seconds);$('job-detail').textContent='已处理 '+Math.floor(progress.seconds)+' / '+Math.ceil(progress.total_seconds)+' 秒录像 · 已等待 '+Math.floor(progress.elapsed||0)+' 秒';}else if(progress.total){$('job-progress').value=Math.min(100,100*progress.bytes/progress.total);$('job-detail').textContent=sizeText(progress.bytes)+' / '+sizeText(progress.total);}else{$('job-progress').removeAttribute('value');$('job-detail').textContent=progress.stage==='validating_playback'?'转换完成，正在检查文件；检查通过后自动播放':'请稍候，完成后自动继续';}if(value.state==='complete'){clearHistoryCache();$('job-panel').hidden=true;return value.result;}if(value.state==='failed'){$('job-panel').hidden=true;throw new Error(jobErrors[value.error]||'处理失败，文件未标记为可播放，可以重试。');}await new Promise(resolve=>setTimeout(resolve,750));}}
async function loadLibrary(){if(!signedIn)return;loadTimeline();loadPublication().catch(e=>libraryMessage(e.message));try{const day=$('recording-day').value,offset=libraryOffset;const value=await api('/api/library?day='+encodeURIComponent(day)+'&offset='+libraryOffset+'&limit='+LIBRARY_PAGE_SIZE);if(day!==$('recording-day').value||offset!==libraryOffset||!signedIn)return;libraryTotal=value.total;$('recording-count').textContent=libraryTotal+' 个片段';$('page-label').textContent=libraryTotal?'第 '+(Math.floor(libraryOffset/LIBRARY_PAGE_SIZE)+1)+' / '+Math.ceil(libraryTotal/LIBRARY_PAGE_SIZE)+' 页':'暂无记录';$('previous-page').disabled=libraryOffset===0;$('next-page').disabled=libraryOffset+LIBRARY_PAGE_SIZE>=libraryTotal;const catalog=value.catalog;$('catalog-message').textContent=catalog?(catalog.metadata_complete?'当天目录已查询。':'设备目录含异常条目，当前列表可能不完整。'):'尚未查询这一天的摄像头目录；当前仅展示已知片段。';const space=value.storage;$('storage-info').textContent=(space.combined_limit?'两份合计 '+sizeText(space.combined_bytes)+' / '+sizeText(space.combined_limit)+'\n':'')+'原片占用 '+sizeText(space.archive_bytes)+' / '+sizeText(space.archive_limit)+'\n回放缓存 '+sizeText(space.cache_bytes)+' / '+sizeText(space.cache_limit)+'\n自动清理旧副本；锁定或正在使用的录像会保留。';const list=$('recording-list');list.replaceChildren();if(!value.items.length){const empty=document.createElement('p');empty.className='recording-empty';empty.textContent='这一天还没有本地记录，可更新摄像头目录查看。';list.append(empty);}for(const item of value.items){const row=document.createElement('div');row.className='recording-row';const details=document.createElement('div');const title=document.createElement('strong');title.textContent=item.start_local.replace('T',' ');details.append(title);const note=document.createElement('small');note.textContent=item.bytes?sizeText(item.bytes):'设备上的录像片段';details.append(note);const actions=document.createElement('div');actions.className='recording-actions';const state=document.createElement('span');state.className='recording-state'+(item.original_ready?' ready':'');state.textContent=stateLabels[item.state]||'等待处理';actions.append(state);const button=document.createElement('button');button.className=item.original_ready?'primary':'quiet';button.dataset.recordingAction='true';button.textContent=item.original_ready?'播放':'立即保存这段';button.disabled=libraryBusy||Boolean(item.publication&&!item.publication.released);if(item.publication&&!item.publication.released)button.textContent='等待 '+item.publication.release_at.slice(11,16)+' 发布';button.addEventListener('click',()=>recordingAction(item));actions.append(button);if(item.original_ready){const pin=document.createElement('button');pin.className='quiet';pin.textContent=item.pinned?'解锁保留':'锁定保留';pin.addEventListener('click',async()=>{try{await api('/api/recordings/'+item.id+'/pin',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({pinned:!item.pinned})});await loadLibrary();}catch(e){libraryMessage(e.message);}});actions.append(pin);const download=document.createElement('button');download.className='quiet';download.textContent='下载原片';download.dataset.recordingAction='true';download.disabled=libraryBusy||Boolean(item.publication&&!item.publication.released);download.addEventListener('click',async()=>{try{await playbackSession();const link=document.createElement('a');link.href=appUrl('/api/recordings/'+item.id+'/file');link.download='';document.body.append(link);link.click();link.remove();}catch(e){libraryMessage(e.message);}});actions.append(download);}row.append(details,actions);list.append(row);}}catch(error){libraryMessage(error.message);}}
async function recordingAction(item,seek=0){if(libraryBusy)return;setLibraryBusy(true);const generation=++replayGeneration;try{if(replayKey===item.id&&$('replay-video').readyState>=1&&seek>=replayWindowStart&&seek<replayWindowEnd){const v=$('replay-video');v.currentTime=Math.min(Math.max(0,seek-replayWindowStart),Math.max(0,v.duration-0.1));libraryMessage('已定位到选中的录像时间。');v.play().catch(()=>libraryMessage('已定位，请点击播放器播放按钮继续。'));return;}if(!item.original_ready){const job=await api('/api/jobs/download/'+item.id,{method:'POST'});await waitJob(job);libraryMessage('录像已保存并检查通过，现在可以播放。');await loadLibrary();item={...item,original_ready:true};}let windowInfo={full:true,start:0};if(!item.playback_ready){const start=Math.floor(Math.max(0,seek)/30)*30;const next=prefetchInfo;if(next&&next.key===item.id&&next.start===start){libraryMessage('正在接入下一小段…');windowInfo=await next.promise;}else windowInfo=null;if(pendingTimelineSeek)return;if(!windowInfo){const job=await api('/api/jobs/playback/'+item.id+'?start='+start,{method:'POST'});windowInfo=await waitJob(job);}}if(!signedIn||generation!==replayGeneration)return;await playbackSession();if(!signedIn||generation!==replayGeneration)return;replayKey=item.id;replayItem=item;replayWindowStart=windowInfo.start||0;replayWindowEnd=windowInfo.full?Infinity:replayWindowStart+windowInfo.seconds;replaySourceSeconds=windowInfo.source_seconds;api('/api/recordings/'+item.id+'/lease',{method:'POST'}).catch(()=>{});if(!signedIn||generation!==replayGeneration)return;$('replay-title').textContent=item.start_local.replace('T',' ');const video=$('replay-video');video.pause();video.src=appUrl('/api/recordings/'+item.id+(windowInfo.full?'/preview':'/window?start='+replayWindowStart));$('replay-message').textContent=windowInfo.full?'正在播放完整回放。':'正在播放 '+clockText(clockSeconds(item.start_local.slice(11))+replayWindowStart)+' 起的短段；跨段定位请拖动时间轴。';video.onloadedmetadata=()=>{if(generation!==replayGeneration||replayKey!==item.id)return;if(Number.isFinite(video.duration))video.currentTime=Math.min(Math.max(0,seek-replayWindowStart),Math.max(0,video.duration-0.1));$('replay-snapshot').disabled=false;};video.load();libraryMessage('回放已准备好，正在加载选中的画面。');video.play().catch(()=>libraryMessage('回放已准备好，点击播放器的播放按钮开始。'));}catch(error){libraryMessage(error.message);}finally{$('job-panel').hidden=true;setLibraryBusy(false);}}
$('query-recordings').addEventListener('click',async()=>{if(libraryBusy)return;setLibraryBusy(true);try{const day=$('recording-day').value;if(!day)throw new Error('请先选择日期。');const job=await api('/api/jobs/list',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel:0,start:day+'T00:00:00',end:day+'T23:59:59'})});const result=await waitJob(job);libraryOffset=0;await loadLibrary();libraryMessage(result.metadata_complete?'当天摄像头目录已更新。':'目录已更新，但设备返回异常条目，暂不能确认完整。');}catch(error){libraryMessage(error.message);}finally{$('job-panel').hidden=true;setLibraryBusy(false);}});
$('refresh-library').addEventListener('click',()=>{clearHistoryCache();loadLibrary();});$('recording-day').addEventListener('change',()=>{libraryOffset=0;loadLibrary();});$('previous-page').addEventListener('click',()=>{libraryOffset=Math.max(0,libraryOffset-LIBRARY_PAGE_SIZE);loadLibrary();});$('next-page').addEventListener('click',()=>{libraryOffset+=LIBRARY_PAGE_SIZE;loadLibrary();});
window.addEventListener('pagehide',clearReplay);

async function recoverLibraryJob(){if(libraryBusy||!signedIn)return;try{const jobs=await api('/api/jobs');if(libraryBusy)return;const job=jobs.find(j=>['discover','download','prepare'].includes(j.kind)&&['running','queued'].includes(j.state));if(job){setLibraryBusy(true);try{await waitJob(job);await loadLibrary();libraryMessage('后台任务已完成，列表已更新。');}finally{setLibraryBusy(false);$('job-panel').hidden=true;}}else if(jobs[0]?.error==='interrupted'){libraryMessage('上次任务因服务重启中断，可以重新操作；已保存的文件会继续检查。');}}catch(error){libraryMessage(error.message);}}

const backupLabels={waiting:'等待下次自动保存',running:'正在自动保存',paused:'自动保存已暂停',waiting_for_camera:'观看优先，稍后自动保存',daily_limit:'今天的自动下载额度已用完',failed:'本轮未完成，将稍后重试',authentication_required:'摄像头验证失败，请检查配置后重新开启',choose_profile:'请选择存储档位'};
function renderBackup(value){if(!value)return;backupState=value;const a=value.archive||{};$('backup-summary').textContent='可直接回放 '+(a.ready??'—')+' 段'+(a.oldest_ready?' · 最早 '+a.oldest_ready.replace('T',' ')+' · 跨度约 '+a.ready_span_days+' 天（不代表完整备份）':'')+' · 已保存原片 '+(a.saved??'—')+' 段 / '+sizeText(a.saved_bytes||0)+' · 补存范围内待准备回放 '+(a.awaiting_playback??'—')+' 段（其中 '+(a.pending_in_scope??'—')+' 段尚未保存原片）'+' · 最近半小时待处理 '+(a.recent_pending??'—')+' 段 · 超过半小时未完成 '+(a.overdue_pending??'—')+' 段 · 失败待重试 '+(a.retry_pending??'—')+' 段（包含在待处理中）'+(a.stages?' · 等待获取 '+a.stages.awaiting_download+' 段 · 等待检查 '+a.stages.awaiting_validation+' 段 · 等待网页处理 '+a.stages.awaiting_web+' 段 · 设备暂未提供 '+a.stages.device_rejected+' 段（包含在等待获取中）':'')+(value.last_finished?' · 最近完成：'+new Date(value.last_finished*1000).toLocaleString():'')+(value.backfill_day?' · 最近补查：'+value.backfill_day+(value.backfill_complete?'':'（目录可能不完整）'):'')+(value.last_file_error?' · 有片段未成功，延后重试':'')+(value.backfill_error?' · 历史目录查询失败，下轮重试':'')+(value.catalog_error?' · 摄像头暂时无法连接，继续处理服务器已有原片':'');const p=value.policy;$('backup-state').textContent=p.enabled?((backupLabels[value.state]||'等待自动保存')+(value.prepare_state==='running'?' · 正在准备已下载录像的网页版本':'')):'自动保存已暂停';if(document.activeElement!==$('backup-mode'))$('backup-mode').value=p.mode;$('backup-toggle').textContent=p.enabled?'暂停自动保存':'开启自动保存';$('backup-now').disabled=!p.enabled||value.running;$('backup-detail').textContent=(p.mode==='server'?'原片＋网页版本合计上限 ':'原片上限 ')+sizeText(p.archive_limit)+(p.batch_files==null?' · 按时间窗口持续处理': ' · 每轮最多 '+p.batch_files+' 段 / '+sizeText(p.batch_bytes))+' · 每天下载额度 '+(p.daily_bytes==null?'不设上限':sizeText(p.daily_bytes))+(value.last_saved!=null?' · 上轮保存 '+value.last_saved+' 段':'');$('backup-next').textContent=p.enabled?'下次检查：'+new Date(value.next_run*1000).toLocaleString()+(value.error?' · 状态：'+(jobErrors[value.error]||value.error):''):'已暂停后台检查；手动保存仍受保留规则约束，已存录像可播放。';if(backupFinished!==value.last_finished){backupFinished=value.last_finished;if(!$('library-panel').hidden)loadLibrary();}}
async function configureBackup(enabled){if(!backupState)return;try{await api('/api/backup',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,mode:$('backup-mode').value,interval_minutes:10})});await poll();await loadLibrary();}catch(e){libraryMessage(e.message);}}
$('backup-toggle').addEventListener('click',()=>configureBackup(!backupState?.policy.enabled));
$('backup-mode').addEventListener('change',()=>configureBackup(backupState?.policy.enabled??true));
$('backup-now').addEventListener('click',async()=>{try{await api('/api/backup/run',{method:'POST'});await poll();}catch(e){libraryMessage(e.message);}});
setInterval(()=>{if(signedIn&&!serviceStopped&&replayKey)api('/api/recordings/'+replayKey+'/lease',{method:'POST'}).catch(()=>{});},60000);

restoreLogin();

$('sound').addEventListener('click',()=>{const v=$('video');v.muted=!v.muted;if(!v.muted&&v.volume===0)v.volume=1;v.play().catch(()=>status('请点击播放器播放按钮后重试。'));});
$('video').addEventListener('volumechange',()=>{if($('sound').disabled)return;const audible=!$('video').muted&&$('video').volume>0;$('sound').textContent=audible?'静音':'开启声音';$('sound').setAttribute('aria-pressed',String(audible));$('audio-state').textContent=audible?'声音已开启':'声音已静音';});

function renderLenses(value){
  if(!value)return;const key=JSON.stringify(value);if(key===lensListKey)return;lensListKey=key;lensViews=value;
  const select=$('lens'),current=Number(select.value);select.replaceChildren();
  for(const view of value){const option=document.createElement('option');option.value=view.id;option.textContent=view.name;select.append(option);}
  select.value=String(value.some(v=>v.id===current)?current:0);$('lens-switch').hidden=value.length<2;describeLens();
}
function describeLens(){const view=lensViews.find(v=>v.id===Number($('lens').value))||lensViews[0];$('camera-title').textContent=view.id===0?'家中摄像头':view.name;$('lens-note').textContent=view.ptz?'主镜头支持实时观看、方向控制和录像回放。':'此镜头仅支持实时观看；历史录像来自主镜头。';$('ptz-note').textContent=view.ptz?'开始观看后可用，每次点按短暂转动。':'请切换到主镜头调整方向。';}
$('lens').addEventListener('change',()=>{const resume=playing;describeLens();resetPlayer('已切换镜头。');if(resume)$('start').click();});


// Timeline uses device-local strings: never shift camera times by browser timezone.
function clockText(second){second=Math.max(0,Math.min(86399,Math.floor(second)));return [Math.floor(second/3600),Math.floor(second/60)%60,second%60].map(v=>String(v).padStart(2,'0')).join(':');}
function clockSeconds(value){const parts=value.split(':').map(Number);return (parts[0]||0)*3600+(parts[1]||0)*60+(parts[2]||0);}
async function loadTimeline(){
  const generation=++timelineGeneration,day=$('recording-day').value;
  if(!day)return;
  if(timelineData?.day!==day){timelineData=null;renderTimeline();$('recording-days').replaceChildren();$('timeline-message').textContent='正在读取当天目录…';}
  try{const value=await api('/api/library/timeline?day='+encodeURIComponent(day));if(generation!==timelineGeneration||day!==$('recording-day').value||!signedIn)return;
    timelineData={...value,items:$('history-source').value==='ready'?value.items.filter(i=>i.playback_ready):value.items};$('timeline-title').textContent='选择时间 · '+timelineData.items.length+' 段录像';renderTimeline();const days=$('recording-days');days.replaceChildren();
    for(const entry of value.days){const button=document.createElement('button');button.className='quiet'+(entry.day===day?' selected':'');button.textContent=entry.day.slice(5)+' · 已发现 '+entry.count;button.setAttribute('aria-label',entry.day+'，'+entry.count+' 段录像');button.addEventListener('click',()=>changeDay(entry.day));days.append(button);}
    $('timeline-message').textContent=timelineData.items.length?'拖动蓝线，松手后播放；空白处会跳到下一段。':'这一天暂时没有可看的录像，可以换个日期。';
  }catch(error){if(generation===timelineGeneration){timelineData=null;renderTimeline();$('timeline-message').textContent=error.message;}}
}
function renderTimeline(){
  if(timelineDragging)return;
  $('timeline-seek').disabled=!timelineData;
  const span=Number($('timeline-scale').value);timelineStart=Math.max(0,Math.min(86400-span,timelineCenter-span/2));
  const slider=$('timeline-seek');slider.min=Math.ceil(timelineStart);slider.max=Math.min(86399,Math.floor(timelineStart+span));slider.value=clockSeconds($('timeline-time').value);
  const ticks=$('timeline-ticks');ticks.replaceChildren();
  for(let n=0;n<=6;n++){const label=document.createElement('span');label.textContent=timelineStart+span*n/6>=86400?'24:00':clockText(timelineStart+span*n/6).slice(0,5);ticks.append(label);}
  const track=$('timeline-track');track.replaceChildren();
  for(const item of timelineData?.items||[]){const end=item.second+(item.duration_hint||1);if(end<timelineStart||item.second>timelineStart+span)continue;
    const marker=document.createElement('span');marker.className='timeline-marker '+(item.original_ready?'saved':'remote');marker.style.left=(100*Math.max(0,item.second-timelineStart)/span)+'%';marker.style.width=(100*(Math.min(timelineStart+span,end)-Math.max(timelineStart,item.second))/span)+'%';
    marker.title=clockText(item.second)+' · '+(item.original_ready?'已保存':'仅有目录，选中后下载');marker.setAttribute('aria-label',marker.title);track.append(marker);
  }
  updateTimelineCursor();
}
function chooseTimeline(item,offset){
  selectTimelineTime(item.second+offset);
  if(libraryBusy){pendingTimelineSeek={item,offset,day:$('recording-day').value};if(prefetchInfo&&(prefetchInfo.key!==item.id||prefetchInfo.start!==Math.floor(offset/30)*30))appFetch('/api/playback/prefetch',{method:'DELETE'}).catch(()=>{});libraryMessage('正在处理上一项，完成后将回放最后选中的时间。');return;}
  recordingAction(item,offset);
}
function flushTimelineSeek(){
  if(libraryBusy||!pendingTimelineSeek)return;
  const pending=pendingTimelineSeek;pendingTimelineSeek=null;
  if(signedIn&&!$('library-panel').hidden&&pending.day===$('recording-day').value)chooseTimeline(pending.item,pending.offset);
}
function seekTimeline(second){
  pendingTimelineSeek=null;
  const items=timelineData?.items||[];
  const item=[...items].reverse().find(i=>second>=i.second&&second<i.second+(i.duration_hint||1));
  if(item){chooseTimeline(item,second-item.second);return;}
  $('replay-video').pause();
  if(!items.length){$('timeline-message').textContent='这一天没有已知录像，请切换日期或更新目录。';return;}
  const next=items.find(i=>i.second>second);
  if(!next){$('timeline-message').textContent='当天这个时间之后没有可用录像，请选择更早时间或切换日期。';return;}
  timelineCenter=next.second;selectTimelineTime(next.second);renderTimeline();
  $('timeline-message').textContent='所选时间没有录像，已向后定位到 '+clockText(next.second)+'。';
  chooseTimeline(next,0);
}
const timelineSlider=$('timeline-seek');
function selectTimelineTime(second){const text=clockText(second);$('timeline-time').value=text;$('timeline-selected').value=text;timelineSlider.setAttribute('aria-valuetext',text);}
function commitTimelineGesture(){
  if(timelineCommitted===timelineGesture)return;
  timelineCommitted=timelineGesture;timelineDragging=false;
  selectTimelineTime(Number(timelineSlider.value));seekTimeline(Number(timelineSlider.value));
}
timelineSlider.addEventListener('pointerdown',event=>{
  timelineGesture++;timelineDragging=true;timelinePointer=event.pointerId;$('replay-video').pause();
});
timelineSlider.addEventListener('input',()=>{
  if(timelinePointer===null)timelineGesture++;
  timelineDragging=true;$('replay-video').pause();selectTimelineTime(Number(timelineSlider.value));
});
timelineSlider.addEventListener('change',()=>{if(timelinePointer===null)commitTimelineGesture();});
function finishTimelinePointer(event){
  if(event.pointerId!==timelinePointer)return;
  timelinePointer=null;
  // A release is meaningful even when the native range value did not change.
  commitTimelineGesture();
}
document.addEventListener('pointerup',finishTimelinePointer);
document.addEventListener('pointercancel',event=>{if(event.pointerId===timelinePointer){timelinePointer=null;timelineDragging=false;timelineCommitted=timelineGesture;}});
timelineSlider.addEventListener('blur',()=>{if(timelinePointer===null)timelineDragging=false;});
function stepClip(delta){const second=clockSeconds($('timeline-time').value);const items=timelineData?.items||[];const item=delta>0?items.find(i=>i.second>second):[...items].reverse().find(i=>i.second<second);if(!item){$('timeline-message').textContent=delta>0?'当天没有更晚的已知录像。':'当天没有更早的已知录像。';return;}selectTimelineTime(item.second);timelineCenter=item.second;renderTimeline();chooseTimeline(item,0);}
$('clip-before').addEventListener('click',()=>stepClip(-1));$('clip-after').addEventListener('click',()=>stepClip(1));
$('timeline-go').addEventListener('click',()=>{timelineCenter=clockSeconds($('timeline-time').value);renderTimeline();seekTimeline(timelineCenter);});
$('timeline-scale').addEventListener('change',()=>{timelineCenter=clockSeconds($('timeline-time').value);renderTimeline();});
function changeDay(day){if(!day)return;clearReplay();timelineData=null;renderTimeline();$('recording-day').value=day;libraryOffset=0;loadLibrary();}
function stepDay(delta){const day=$('recording-day').value;if(!day)return;const date=new Date(day+'T12:00:00Z');date.setUTCDate(date.getUTCDate()+delta);changeDay(date.toISOString().slice(0,10));}
$('day-before').addEventListener('click',()=>stepDay(-1));$('day-after').addEventListener('click',()=>stepDay(1));
$('recording-day').addEventListener('change',()=>{clearReplay();});
function updateTimelineCursor(){
  if(timelineDragging)return;
  let second=clockSeconds($('timeline-time').value);
  if(replayItem&&timelineData&&replayItem.start_local.startsWith(timelineData.day)&&!$('replay-video').paused)second=clockSeconds(replayItem.start_local.slice(11))+replayWindowStart+($('replay-video').currentTime||0);
  const slider=$('timeline-seek');if(second>=Number(slider.min)&&second<=Number(slider.max))slider.value=Math.floor(second);
  selectTimelineTime(second);
}
$('replay-video').addEventListener('timeupdate',updateTimelineCursor);
$('replay-video').addEventListener('error',()=>{$('replay-snapshot').disabled=true;$('replay-message').textContent='回放文件暂时不可用，请重新点击片段准备回放。';});
function saveSnapshot(video,label,notify){
  if(video.readyState<2||!video.videoWidth){notify('画面还没有准备好，请稍后再试。');return;}
  const canvas=document.createElement('canvas');canvas.width=video.videoWidth;canvas.height=video.videoHeight;
  try{canvas.getContext('2d').drawImage(video,0,0);canvas.toBlob(blob=>{if(!blob){notify('快照生成失败，请重试。');return;}const url=URL.createObjectURL(blob),link=document.createElement('a');link.href=url;link.download='V360-'+label+'-'+new Date().toISOString().replace(/[:.]/g,'-')+'.jpg';document.body.append(link);link.click();link.remove();setTimeout(()=>URL.revokeObjectURL(url),30000);notify('当前画面已生成快照，请查看浏览器下载。');},'image/jpeg',0.92);}catch{notify('无法保存当前画面，请等待画面加载后重试。');}
}
$('snapshot').addEventListener('click',()=>saveSnapshot($('video'),'live',status));
$('replay-snapshot').addEventListener('click',()=>saveSnapshot($('replay-video'),'replay',libraryMessage));

$('replay-video').addEventListener('ended',()=>{
 if(!signedIn||!replayItem||$('library-panel').hidden||timelineDragging||pendingTimelineSeek)return;
 if(Number.isFinite(replayWindowEnd)&&replayWindowEnd<(replaySourceSeconds||0)-0.25)chooseTimeline(replayItem,replayWindowEnd);else libraryMessage('本条录像已结束，可用时间轴或下一段继续选择。');
});

function prefetchNextWindow(){
 if(!signedIn||!replayItem||$('library-panel').hidden||$('replay-video').paused||!Number.isFinite(replayWindowEnd)||replayWindowEnd>=(replaySourceSeconds||0)-0.25)return;
 const key=replayItem.id,start=replayWindowEnd;
 if(prefetchInfo?.key===key&&prefetchInfo?.start===start)return;
 const info={key,start,promise:null};prefetchInfo=info;$('prefetch-status').textContent='正在提前准备下一小段，当前画面可继续观看。';
 info.promise=(async()=>{
  try{
   const job=await api('/api/jobs/prefetch/'+key+'?start='+start,{method:'POST'});
   for(;;){
    if(!signedIn||prefetchInfo!==info)return null;
    const value=await api('/api/jobs/'+job.id);
    if(value.state==='complete'){$('prefetch-status').textContent='下一小段已准备好。';return value.result;}
    if(value.state==='failed'){$('prefetch-status').textContent='下一小段尚未就绪，到达时将继续准备。';return null;}
    await new Promise(resolve=>setTimeout(resolve,1000));
   }
  }catch{if(prefetchInfo===info)$('prefetch-status').textContent='当前任务优先，稍后按需准备下一段。';return null;}
 })();
}
$('replay-video').addEventListener('playing',prefetchNextWindow);

$('history-source').addEventListener('change',()=>{clearReplay();loadTimeline();});

let batchPublicationEnabled=false;
async function loadPublication(){
 const day=$('recording-day').value;
 const value=await api('/api/publication?day='+encodeURIComponent(day));
 if(day!==$('recording-day').value||!signedIn)return;
 batchPublicationEnabled=value.enabled;
 $('publication-panel').hidden=!value.enabled;
 const upcoming=value.batches.find(batch=>!batch.released);
 $('family-history-status').textContent=upcoming?'录像自动同步 · 下一批 '+upcoming.release_at.slice(11,16)+' 更新':'录像自动保存 · 选择日期即可回看';
 if(!value.enabled)return;
 const batches=$('publication-batches');batches.replaceChildren();
 for(const batch of value.batches){
  const line=document.createElement('p');
  line.textContent=batch.batch_start.slice(11,16)+'–'+batch.batch_end.slice(11,16)+' · '+(batch.released?'已发布':'待 '+batch.release_at.replace('T',' ')+' 发布')+' · 准备好 '+batch.ready+' / 已发现 '+batch.known+' 段 · 停止自动重试 '+batch.stopped+' 段';
  batches.append(line);
 }
 const list=$('publication-stopped');list.replaceChildren();
 for(const item of value.stopped){
  const line=document.createElement('label');line.className='recording-row';
  const check=document.createElement('input');check.type='checkbox';check.value=item.id;check.dataset.retryClip='true';
  const text=document.createElement('span');text.textContent=item.start_local.replace('T',' ')+' · '+(item.stopped_reason==='attempt_limit'?'达到5次上限':'处理窗口结束')+' · '+(jobErrors[item.error]||item.error||'尚未准备好')+' · 自动尝试 '+item.attempts+' 次 / 手动 '+item.manual_attempts+' 次';
  line.append(check,text);list.append(line);
 }
}
$('retry-selected').addEventListener('click',async()=>{
 if(libraryBusy)return;
 const keys=[...document.querySelectorAll('[data-retry-clip]:checked')].map(input=>input.value);
 if(!keys.length){libraryMessage('请先选择要重试的录像。');return;}
 setLibraryBusy(true);let success=0,failed=0;
 try{for(const key of keys){try{await waitJob(await api('/api/jobs/retry-once/'+key,{method:'POST'}));success++;}catch(e){failed++;libraryMessage(e.message);}}}
 finally{setLibraryBusy(false);await loadLibrary();libraryMessage('重试完成：成功 '+success+' 段，失败 '+failed+' 段。');}
});

setInterval(()=>{if(batchPublicationEnabled&&signedIn&&!serviceStopped&&!$('library-panel').hidden&&!libraryBusy)loadLibrary();},30000);

$('open-management').addEventListener('click',()=>$('management-dialog').showModal());
$('close-management').addEventListener('click',()=>$('management-dialog').close());
$('today-recordings').addEventListener('click',()=>changeDay(new Intl.DateTimeFormat('sv-SE',{timeZone:'Asia/Shanghai'}).format(new Date())));

$('password-form').addEventListener('submit',async event=>{
 event.preventDefault();
 const next=$('new-password').value;
 if(next!==$('confirm-password').value){$('password-message').textContent='两次新密码不一致。';return;}
 const button=$('save-password');button.disabled=true;
 try{
  const response=await appFetch('/api/auth/password',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({current_password:$('current-password').value,new_password:next})});
  if(!response.ok){$('password-message').textContent=response.status===429?'尝试次数过多，请稍等一分钟。':'修改未成功，请检查当前密码和新密码长度。';return;}
  $('password-form').reset();showLogin();$('login-message').textContent='密码已修改，请用新密码登录。其他设备也需重新登录。';
 }catch{$('password-message').textContent='暂时无法连接服务，请稍后再试。';}
 finally{button.disabled=false;}
});

$('management-dialog').addEventListener('close',()=>{$('password-form').reset();$('password-message').textContent='';});


function resetPasswordVisibility(){
 document.querySelectorAll('[data-password-toggle]').forEach(button=>{
  const field=$(button.dataset.passwordToggle);field.classList.remove('phrase-visible');
  button.setAttribute('aria-label','查看输入内容');button.setAttribute('aria-pressed','false');
 });
}
document.querySelectorAll('[data-password-toggle]').forEach(button=>button.addEventListener('click',()=>{
 const field=$(button.dataset.passwordToggle),show=!field.classList.contains('phrase-visible');
 const start=field.selectionStart,end=field.selectionEnd;
 field.classList.toggle('phrase-visible',show);
 button.setAttribute('aria-label',show?'隐藏输入内容':'查看输入内容');button.setAttribute('aria-pressed',String(show));
 field.focus();if(start!==null&&end!==null)field.setSelectionRange(start,end);
}));
$('management-dialog').addEventListener('close',resetPasswordVisibility);
window.addEventListener('pagehide',resetPasswordVisibility);
if(!CSS.supports('-webkit-text-security','disc')){
 document.querySelectorAll('[data-phrase-note]').forEach(note=>{note.hidden=false;note.textContent='当前浏览器无法遮盖口令，输入内容将显示在屏幕上。';});
}
