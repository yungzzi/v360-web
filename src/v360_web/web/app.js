"use strict";
const $ = id => document.getElementById(id);
const BASE_PATH = new URL(document.currentScript.src).pathname.replace(/\/assets\/app\.js$/, "");
const appUrl = path => BASE_PATH + path;
const appFetch = (path, options) => fetch(appUrl(path), options);
let token = "", socket = null, media = null, source = null, objectURL = null;
let chunks = [], mime = null, playing = false, requestGeneration = 0, serviceStopped = false;
let viewerId=null;
const ptzButtons=()=>document.querySelectorAll('[data-direction]');
const labels = {starting:"正在连接",live:"正在观看",stopped:"已停止",failed:"连接失败"};
const errors = {camera_busy:"摄像头正在处理其他任务，请稍后重试。",viewer_limit:"观看人数已达上限。",session_limit:"本次观看已达 30 分钟，请重新连接。",read_timeout:"摄像头暂时没有返回画面，请重新连接。",sdk_connect:"摄像头连接失败，请稍后重试。",live_sequence:"视频传输中断，请重新连接。"};
function status(text) {$('notice').textContent=text;}
function resetPlayer(message="已停止观看。") {
  if(viewerId&&token)sendMove('stop');viewerId=null;ptzButtons().forEach(b=>b.disabled=true);requestGeneration++; playing=false;
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
  if(!token || serviceStopped)return;
  try{const response=await appFetch('/api/status',{headers:{Authorization:'Bearer '+token}});if(response.status===401){logout();return;}if(!response.ok)return;const state=await response.json();$('viewers').textContent=state.live.viewers+' 人';renderBackup(state.backup);const b=state.battery||{};const pct=b.percent;const fill=$('battery-fill');fill.style.width=pct==null?'0%':Math.max(0,Math.min(100,pct))+'%';$('battery-percent').textContent=pct==null?'—':pct+'%';$('battery-badge').classList.toggle('low',pct!=null&&pct<=20);$('battery-badge').classList.toggle('stale',!!b.stale);$('battery-charge').hidden=b.charging!==true;const batteryLabel=pct==null?'尚未取得电量':'电量 '+pct+'%'+(b.updated_at?'，读取于 '+new Date(b.updated_at*1000).toLocaleString():'');$('battery-badge').setAttribute('aria-label',batteryLabel);$('battery-badge').title=batteryLabel;$('battery').textContent=b.percent==null?'未知':b.percent+'%';$('charging').textContent=b.charging==null?'未知':b.charging?'设备报告充电中':'未报告外接充电';$('battery-time').textContent=b.updated_at?new Date(b.updated_at*1000).toLocaleString()+(b.stale?' · 待读取更新':' · 最近读取'):'尚未读取电量';$('local-controls').hidden=!state.local_shutdown_available;}
  catch{/* Keep the active player's error reporting independent of status polling. */}
}
function logout(){clearReplay();if(token)appFetch('/api/playback/session',{method:'DELETE',headers:{Authorization:'Bearer '+token}}).catch(()=>{});resetPlayer();token='';$('login').hidden=false;$('dashboard').hidden=true;$('logout').hidden=true;$('token').value='';}
$('login-form').addEventListener('submit',async event=>{
  event.preventDefault();const candidate=$('token').value.trim();
  try{const response=await appFetch('/api/status',{headers:{Authorization:'Bearer '+candidate}});
    if(!response.ok){$('login-message').textContent='验证未通过，请检查网页访问令牌。';return;}
    serviceStopped=false;token=candidate;$('token').value='';$('login').hidden=true;$('dashboard').hidden=false;$('logout').hidden=false;poll();
  }catch{$('login-message').textContent='无法连接本地服务，请确认后端正在运行。';}
});
$('start').addEventListener('click',()=>{
  if(!window.MediaSource){status('当前浏览器不支持此播放器，请使用新版 Chrome 或 Edge。');return;}
  resetPlayer('正在连接摄像头…');playing=true;const generation=requestGeneration;
  $('start').disabled=true;$('stop').disabled=false;$('connection').textContent='连接中';$('state').textContent='正在连接';
  media=new MediaSource();objectURL=URL.createObjectURL(media);$('video').src=objectURL;media.addEventListener('sourceopen',prepare);
  const ws=new WebSocket((location.protocol==='https:'?'wss:':'ws:')+'//'+location.host+appUrl('/api/live/ws'));socket=ws;ws.binaryType='arraybuffer';
  ws.onopen=()=>{if(socket===ws)ws.send(JSON.stringify({token}));};
  ws.onmessage=event=>{
    if(socket!==ws || generation!==requestGeneration)return;
    if(typeof event.data==='string'){
      const data=JSON.parse(event.data);
      if(data.type==='state'){viewerId=data.viewer_id;}
      else if(data.type==='init'){mime=data.mime;prepare();ptzButtons().forEach(b=>b.disabled=false);}
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
$('shutdown').addEventListener('click',()=>$('shutdown-dialog').showModal());
$('cancel-shutdown').addEventListener('click',()=>$('shutdown-dialog').close());
$('confirm-shutdown').addEventListener('click',async()=>{
  $('confirm-shutdown').disabled=true;
  try{
    const response=await appFetch('/api/local/shutdown',{method:'POST',headers:{Authorization:'Bearer '+token}});
    if(!response.ok){status('未能关闭本地后端，仍可在启动终端按 Ctrl+C。');return;}
    serviceStopped=true;resetPlayer('关闭请求已发送，后端正在结束本次运行。重新启动后再刷新此页面。');
    $('start').disabled=true;$('shutdown').disabled=true;$('connection').textContent='正在关闭';$('state').textContent='正在关闭';
  }catch{status('无法确认关闭结果，请查看启动后端的终端。');}
  finally{$('shutdown-dialog').close();$('confirm-shutdown').disabled=false;}
});
$('fullscreen').addEventListener('click',()=>{$('video').requestFullscreen?.().catch(()=>status('当前浏览器无法进入全屏。'));});
window.addEventListener('pagehide',()=>resetPlayer());
setInterval(()=>{poll();const v=$('video');if(playing&&v.buffered.length)$('buffer').textContent=v.readyState>=3?'正常播放':'正在等待画面';},3000);


async function sendMove(direction){
  if(!viewerId||!token)return;
  try{const response=await appFetch('/api/live/ptz',{method:'POST',keepalive:true,headers:{Authorization:'Bearer '+token,'Content-Type':'application/json'},body:JSON.stringify({viewer_id:viewerId,direction})});if(!response.ok&&direction!=='stop')status('暂时无法调整方向，请确认正在观看，或稍后重试。');}
  catch{if(direction!=='stop')status('控制连接中断，后端将尝试停止转动。');}
}
ptzButtons().forEach(button=>button.addEventListener('click',()=>sendMove(button.dataset.direction)));
window.addEventListener('blur',()=>sendMove('stop'));
document.addEventListener('visibilitychange',()=>{if(document.hidden)sendMove('stop');});

let libraryOffset=0, libraryTotal=0, libraryBusy=false, mediaSessionUntil=0;
let backupState=null, replayKey=null, backupFinished=null;
const LIBRARY_PAGE_SIZE=10;
const stateLabels={discovered:'尚未下载',downloading:'正在下载',downloaded_unverified:'待检查',verified:'已保存',evicted:'已按保留规则清理',failed:'下载失败'};
const stageLabels={connecting:'正在连接摄像头',downloading:'正在下载录像',validating:'正在检查录像',preparing_playback:'正在准备网页回放',validating_playback:'正在检查回放文件'};
const jobErrors={storage_limit:'空间预算已满，暂时不能新增文件。',disk_reserve:'需要给系统保留更多磁盘空间，任务已停止。',interrupted:'上次服务重启中断了任务，可以重新操作。',preview_timeout:'准备回放用时过长，原始录像仍保留。',preview_truncated:'回放文件检查未通过，原片仍保留。'};
function libraryMessage(text){$('library-notice').textContent=text;}
function clearReplay(){replayKey=null;const v=$('replay-video');if(v){v.pause();v.removeAttribute('src');v.load();}mediaSessionUntil=0;}
function setTab(library){if(library)resetPlayer('已切换到历史录像。');else{clearReplay();if(!playing){status('已返回实时观看，可以开始连接摄像头。');$('player-message').textContent='点击「开始观看」连接摄像头';}}$('live-panel').hidden=library;$('library-panel').hidden=!library;$('tab-live').classList.toggle('selected',!library);$('tab-library').classList.toggle('selected',library);if(library){loadLibrary();recoverLibraryJob();}}
$('tab-live').addEventListener('click',()=>setTab(false));$('tab-library').addEventListener('click',()=>setTab(true));
const localToday=new Date();$('recording-day').value=[localToday.getFullYear(),String(localToday.getMonth()+1).padStart(2,'0'),String(localToday.getDate()).padStart(2,'0')].join('-');
async function api(path,options={}){let response;try{response=await appFetch(path,{...options,headers:{Authorization:'Bearer '+token,...options.headers}});}catch{throw new Error('无法连接后端，请确认服务仍在运行。');}if(response.status===401){logout();throw new Error('登录已失效，请重新登录。');}if(response.status===409)throw new Error('摄像头或服务正在处理其他任务，请停止直播并稍后再试。');if(!response.ok)throw new Error('操作未成功，请稍后重试。');return response.json();}
function sizeText(bytes){return bytes>=1024**3?(bytes/1024**3).toFixed(2)+' GiB':(bytes/1024**2).toFixed(1)+' MiB';}
async function playbackSession(){if(Date.now()<mediaSessionUntil)return;await api('/api/playback/session',{method:'POST'});mediaSessionUntil=Date.now()+50*60000;}
function setLibraryBusy(value){libraryBusy=value;$('query-recordings').disabled=value;document.querySelectorAll('[data-recording-action]').forEach(button=>button.disabled=value);}
async function waitJob(job){$('job-panel').hidden=false;for(;;){if(!token||serviceStopped)throw new Error('操作页面已关闭；后台任务仍按当前状态处理。');const value=await api('/api/jobs/'+job.id);const progress=value.progress||{};$('job-label').textContent=stageLabels[progress.stage]||'正在处理';if(progress.total){$('job-progress').value=Math.min(100,100*progress.bytes/progress.total);$('job-detail').textContent=sizeText(progress.bytes)+' / '+sizeText(progress.total);}else{$('job-progress').removeAttribute('value');$('job-detail').textContent='请稍候，结束后自动更新列表';}if(value.state==='complete'){$('job-panel').hidden=true;return value.result;}if(value.state==='failed'){$('job-panel').hidden=true;throw new Error(jobErrors[value.error]||'处理失败，文件未标记为可播放，可以重试。');}await new Promise(resolve=>setTimeout(resolve,750));}}
async function loadLibrary(){if(!token)return;try{const day=$('recording-day').value;const value=await api('/api/library?day='+encodeURIComponent(day)+'&offset='+libraryOffset+'&limit='+LIBRARY_PAGE_SIZE);libraryTotal=value.total;$('recording-count').textContent=libraryTotal+' 个片段';$('page-label').textContent=libraryTotal?'第 '+(Math.floor(libraryOffset/LIBRARY_PAGE_SIZE)+1)+' / '+Math.ceil(libraryTotal/LIBRARY_PAGE_SIZE)+' 页':'暂无记录';$('previous-page').disabled=libraryOffset===0;$('next-page').disabled=libraryOffset+LIBRARY_PAGE_SIZE>=libraryTotal;const catalog=value.catalog;$('catalog-message').textContent=catalog?(catalog.metadata_complete?'当天目录已查询。':'设备目录含异常条目，当前列表可能不完整。'):'尚未查询这一天的摄像头目录；下方只展示已记住的片段。';const space=value.storage;$('storage-info').textContent='录像占用 '+sizeText(space.archive_bytes)+' / '+sizeText(space.archive_limit)+'\n回放缓存 '+sizeText(space.cache_bytes)+' / '+sizeText(space.cache_limit)+'\n自动清理旧副本；锁定或正在使用的录像会保留。';const list=$('recording-list');list.replaceChildren();if(!value.items.length){const empty=document.createElement('p');empty.className='recording-empty';empty.textContent='这一天还没有本地记录，可更新摄像头目录查看。';list.append(empty);}for(const item of value.items){const row=document.createElement('div');row.className='recording-row';const details=document.createElement('div');const title=document.createElement('strong');title.textContent=item.start_local.replace('T',' ');details.append(title);const note=document.createElement('small');note.textContent=item.bytes?sizeText(item.bytes):'设备上的录像片段';details.append(note);const actions=document.createElement('div');actions.className='recording-actions';const state=document.createElement('span');state.className='recording-state'+(item.original_ready?' ready':'');state.textContent=stateLabels[item.state]||'等待处理';actions.append(state);const button=document.createElement('button');button.className=item.original_ready?'primary':'quiet';button.dataset.recordingAction='true';button.textContent=item.original_ready?'播放':'立即保存这段';button.disabled=libraryBusy;button.addEventListener('click',()=>recordingAction(item));actions.append(button);if(item.original_ready){const pin=document.createElement('button');pin.className='quiet';pin.textContent=item.pinned?'解锁保留':'锁定保留';pin.addEventListener('click',async()=>{try{await api('/api/recordings/'+item.id+'/pin',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({pinned:!item.pinned})});await loadLibrary();}catch(e){libraryMessage(e.message);}});actions.append(pin);const download=document.createElement('button');download.className='quiet';download.textContent='下载原片';download.dataset.recordingAction='true';download.disabled=libraryBusy;download.addEventListener('click',async()=>{try{await playbackSession();const link=document.createElement('a');link.href=appUrl('/api/recordings/'+item.id+'/file');link.download='';document.body.append(link);link.click();link.remove();}catch(e){libraryMessage(e.message);}});actions.append(download);}row.append(details,actions);list.append(row);}}catch(error){libraryMessage(error.message);}}
async function recordingAction(item){if(libraryBusy)return;setLibraryBusy(true);try{if(!item.original_ready){const job=await api('/api/jobs/download/'+item.id,{method:'POST'});await waitJob(job);libraryMessage('录像已保存并检查通过，现在可以播放。');await loadLibrary();return;}if(!item.playback_ready){const job=await api('/api/jobs/playback/'+item.id,{method:'POST'});await waitJob(job);}await playbackSession();replayKey=item.id;await api('/api/recordings/'+item.id+'/lease',{method:'POST'});$('replay-title').textContent=item.start_local.replace('T',' ');const video=$('replay-video');video.pause();video.src=appUrl('/api/recordings/'+item.id+'/preview');$('replay-message').textContent='已准备好回放。原片有声音时会保留声音，可使用播放器静音。';video.load();video.play().catch(()=>libraryMessage('回放已准备好，点击播放器的播放按钮开始。'));await loadLibrary();$('replay-video').scrollIntoView({behavior:'smooth',block:'nearest'});}catch(error){libraryMessage(error.message);}finally{$('job-panel').hidden=true;setLibraryBusy(false);}}
$('query-recordings').addEventListener('click',async()=>{if(libraryBusy)return;setLibraryBusy(true);try{const day=$('recording-day').value;if(!day)throw new Error('请先选择日期。');const job=await api('/api/jobs/list',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({channel:0,start:day+'T00:00:00',end:day+'T23:59:59'})});const result=await waitJob(job);libraryOffset=0;await loadLibrary();libraryMessage(result.metadata_complete?'当天摄像头目录已更新。':'目录已更新，但设备返回异常条目，暂不能确认完整。');}catch(error){libraryMessage(error.message);}finally{$('job-panel').hidden=true;setLibraryBusy(false);}});
$('refresh-library').addEventListener('click',loadLibrary);$('recording-day').addEventListener('change',()=>{libraryOffset=0;loadLibrary();});$('previous-page').addEventListener('click',()=>{libraryOffset=Math.max(0,libraryOffset-LIBRARY_PAGE_SIZE);loadLibrary();});$('next-page').addEventListener('click',()=>{libraryOffset+=LIBRARY_PAGE_SIZE;loadLibrary();});
window.addEventListener('pagehide',clearReplay);

async function recoverLibraryJob(){if(libraryBusy||!token)return;try{const jobs=await api('/api/jobs');if(libraryBusy)return;const job=jobs.find(j=>['discover','download','prepare'].includes(j.kind)&&['running','queued'].includes(j.state));if(job){setLibraryBusy(true);try{await waitJob(job);await loadLibrary();libraryMessage('后台任务已完成，列表已更新。');}finally{setLibraryBusy(false);$('job-panel').hidden=true;}}else if(jobs[0]?.error==='interrupted'){libraryMessage('上次任务因服务重启中断，可以重新操作；已保存的文件会继续检查。');}}catch(error){libraryMessage(error.message);}}

const backupLabels={waiting:'等待下次自动保存',running:'正在自动保存',paused:'自动保存已暂停',waiting_for_camera:'观看优先，稍后自动保存',daily_limit:'今天的自动下载额度已用完',failed:'本轮未完成，将稍后重试',authentication_required:'摄像头验证失败，请检查配置后重新开启',choose_profile:'请选择存储档位'};
function renderBackup(value){if(!value)return;backupState=value;const p=value.policy;$('backup-state').textContent=p.enabled?(backupLabels[value.state]||'等待自动保存'):'自动保存已暂停';if(document.activeElement!==$('backup-mode'))$('backup-mode').value=p.mode;$('backup-toggle').textContent=p.enabled?'暂停自动保存':'开启自动保存';$('backup-now').disabled=!p.enabled||value.running;$('backup-detail').textContent='总量上限 '+sizeText(p.archive_limit)+' · 每轮最多 '+p.batch_files+' 段 / '+sizeText(p.batch_bytes)+' · 每天下载额度 '+sizeText(p.daily_bytes)+(value.last_saved!=null?' · 上轮保存 '+value.last_saved+' 段':'');$('backup-next').textContent=p.enabled?'下次检查：'+new Date(value.next_run*1000).toLocaleString()+(value.error?' · 状态：'+(jobErrors[value.error]||value.error):''):'已暂停后台检查；手动保存仍受保留规则约束，已存录像可播放。';if(backupFinished!==value.last_finished){backupFinished=value.last_finished;if(!$('library-panel').hidden)loadLibrary();}}
async function configureBackup(enabled){if(!backupState)return;try{await api('/api/backup',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({enabled,mode:$('backup-mode').value,interval_minutes:60})});await poll();await loadLibrary();}catch(e){libraryMessage(e.message);}}
$('backup-toggle').addEventListener('click',()=>configureBackup(!backupState?.policy.enabled));
$('backup-mode').addEventListener('change',()=>configureBackup(backupState?.policy.enabled??true));
$('backup-now').addEventListener('click',async()=>{try{await api('/api/backup/run',{method:'POST'});await poll();}catch(e){libraryMessage(e.message);}});
setInterval(()=>{if(token&&!serviceStopped&&replayKey)api('/api/recordings/'+replayKey+'/lease',{method:'POST'}).catch(()=>{});},60000);
