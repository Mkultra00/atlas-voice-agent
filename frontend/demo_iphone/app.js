/* Atlas demo iPhone UI — DEMO MODE scripted Paris flow + optional LIVE wire (?room=&token=&url=) + duty-desk feed (?desk=) */
const $ = id => document.getElementById(id);
const chat = $('chat');
const p = new URLSearchParams(location.search);
const AUDIO_ONLY = p.get('audio') === '1';
if (AUDIO_ONLY) { document.body.classList.add('audio-only'); }
const ROOM = p.get('room') || '', TOKEN = p.get('token') || '', WSS = p.get('url') || '';
const DESK = p.get('desk') || '';

let room = null, connected = false, live = false, dialed = false;
let awaiting = null;                 // 'yes' | 'hear' | null
const PLAYED = new Set();

/* ---------- chat helpers ---------- */
function agent(text){ add('agent','ATLAS',text); }
function me(text){ add('me','ALEX',text); }
function add(cls,who,text){ const d=document.createElement('div'); d.className='bub '+cls; const b=document.createElement('b'); b.textContent=who; d.appendChild(b); d.appendChild(document.createTextNode(text)); chat.appendChild(d); chat.scrollTop=chat.scrollHeight; }
function evt(text,ok){ const d=document.createElement('div'); d.className='evt'+(ok?' ok':''); d.textContent=text; chat.appendChild(d); chat.scrollTop=chat.scrollHeight; }
function setPill(s){ const pill=$('connPill'); pill.className='pill '+s; pill.textContent=s.toUpperCase(); }
function setCallBtn(state){
  const b=$('callBtn');
  if(state==='connecting'){ b.disabled=true; b.innerHTML='CONNECTING…'; }
  else if(state==='end'){ b.disabled=false; b.classList.add('end'); b.innerHTML='DROP CALL'; }
  else { b.disabled=false; b.classList.remove('end'); b.innerHTML='CALL ATLAS<br><small>24/7 SAFETY LINE</small>'; }
}

/* ---------- story beats (each fires at most once) ---------- */
function beat(name){
  if (PLAYED.has(name)) return;
  PLAYED.add(name);
  switch(name){
    case 'start':
      agent("This is Atlas — 24/7 traveler safety. I know why you're calling, Alex Carter — your hotel is on Boulevard de Magenta, near Gare du Nord. Are you safe right now? Just tell me yes or no.");
      awaiting='yes'; break;
    case 'yes':
      me('1');
      agent('Good. Crowds are moving from Gare du Nord — staying sheltered in your room, door locked, away from windows, is my first recommendation. If you still want to move, I have a checked route ready. Say the word.');
      awaiting=null; break;
    case 'move':
      me('2 — I want to move');
      agent('Route checked and clear: Commissariat Police 10e arr is 150 m from your hotel — walking there avoids Gare du Nord entirely. One rule: stay on sidewalks, away from the crowd. Now start walking and talk to me — I hear you.');
      awaiting='hear'; break;
    case 'hear':
      me('1');
      agent('Heard you. Route locked in. One honest limit: the French emergency line is 1-1-2, they answer in English. I can\u2019t dial it for you — stay with me.');
      awaiting=null; break;
  }
}

/* ---------- escalation ---------- */
async function escalate(reason){
  if (PLAYED.has('esc')) return;
  PLAYED.add('esc');
  evt('► REQUESTING LIVE OPERATOR');
  agent('Understood — I\u2019m trying to reach a person on our duty desk. Stay on the line.');
  if (!DESK){
    // DEMO MODE without a desk: honest pending state
    setTimeout(()=>evt('DUTY DESK PENDING — WEBHOOK NOT WIRED IN DEMO MODE (pass ?desk= to light the board)',false),1200);
    setTimeout(()=>agent('The duty desk isn\u2019t picking up yet. Your case is queued — I\u2019m still here and I won\u2019t hang up.'),2600);
    return;
  }
  const body={
    event:'escalation',
    call_id:'h_'+Math.abs([...(ROOM||'alex-carter-paris')].reduce((a,c)=>(a*31+c.charCodeAt(0))|0,7)).toString(16),
    message:'Caller requesting live operator — '+reason,
    ts:new Date().toISOString(),
    location:{label:'Boulevard de Magenta, Paris 10e (hotel, caller-confirmed)',lat:48.8738,lon:2.3575},
    photo_b64:photoB64||undefined
  };
  try{
    const r=await fetch(DESK.replace(/\/$/,'')+'/alert',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)});
    setTimeout(()=>{ if(r.ok){ evt('DUTY DESK ACKNOWLEDGED ✓ OPERATOR SEES YOUR CASE',true); agent('The duty desk has your case — location, message, and any photo you attached. They\u2019re joining this call.'); } else { evt('DUTY DESK PENDING — RETRYING',false); agent('The desk hasn\u2019t picked up yet. I\u2019m staying on the line with you.'); } },1200);
  }catch{ setTimeout(()=>evt('DUTY DESK UNREACHABLE — CASE QUEUED',false),1200); }
}

/* ---------- keypad ---------- */
document.querySelectorAll('.keypad button').forEach(b=>b.addEventListener('click',()=>{
  if(!connected){ return; }
  const k=b.dataset.key;
  try{ telephony.dtmf(k); }catch{}   /* in-call DTMF click like a real phone */
  if(live&&room){ try{ room.localParticipant.publishDtmf(0,k); }catch{} }
  if(k==='0'){ escalate('button 0 pressed'); return; }
  if(k==='1'&&awaiting==='yes'){ beat('yes'); return; }
  if(k==='1'&&awaiting==='hear'){ beat('hear'); return; }
  if(k==='2'&&awaiting==='yes'){ beat('move'); return; }
  if(k==='2'){ me('2'); agent('Noted. Tell me what changed and I\u2019ll re-check.'); return; }
}));

/* ---------- call button: plain connection, no phone simulation ---------- */
$('callBtn').addEventListener('click',async()=>{
  if(connected){ try{ if(room) await room.disconnect(); }catch{} telephony.endAll(); dialed=false; connected=false; setCallBtn('idle'); setPill('demo'); return; }
  setCallBtn('connecting');
  dialed=true; awaiting=null;
  chat.innerHTML='';
  setPill('dial'); evt('DIALING ATLAS — PARIS LINE…',true);
  try{ await telephony.dialSequence(); }catch(e){}
  if(ROOM&&TOKEN&&WSS){
    connected=true; live=true; setPill('conn');
    try{
      room=new LivekitClient.Room({audioCaptureDefaults:{noiseSuppression:true}});
      room.on('disconnected',()=>{ setPill('drop'); evt('CALL DROPPED — ATLAS FIRED INSTANT DUTY-DESK ALERT',false); setCallBtn('idle'); connected=false; live=false; dialed=false; telephony.endAll(); });
      room.on('trackSubscribed',(tr,p)=>{ if(tr.kind==='audio'){ try{evt('AGENT AUDIO ARRIVED ('+((p&&p.identity)||'unknown')+')',true);}catch(x){} const a=document.createElement('audio'); a.autoplay=true; a.srcObject=tr; a.setAttribute('playsinline',''); document.body.appendChild(a); try{ a.volume=1.0; const p=a.play&&a.play(); if(p&&p.catch)p.catch(()=>{}); }catch(e){} } });
      await room.connect(WSS,TOKEN);
      setPill('live'); setCallBtn('end');
      telephony.timer(t=>{ $('connPill').textContent=t; });
      setTimeout(async()=>{ try{ await room.localParticipant.setMicrophoneEnabled(true); evt('MIC LIVE — SPEAK TO ATLAS',true); }catch(e){ evt('MIC BLOCKED — ALLOW MICROPHONE IN TAB',false); } },400);
    }catch(e){ live=false; setPill('demo'); setCallBtn('end'); evt('LIVE WIRE FAILED — FELL BACK TO DEMO MODE'); startScript(); }
  } else { connected=true; live=false; setPill('demo'); setCallBtn('end'); startScript(); }
});

/* ---------- scripted demo flow (timed, one-shot each) ---------- */
function startScript(){
  connected=true; setCallBtn('end');
  beat('start');
  const t=(ms,fn)=>setTimeout(()=>fn(),ms);
  t(2400,()=>beat('yes'));
  t(5200,b=>0||beat('move'));      // caller presses 2 on stage
  t(8000,()=>beat('hear'));
  t(10600,()=>escalate('caller pressed 0 (scripted demo)'));
}

/* ---------- ticker bridge: duty-desk feed → phone view ---------- */
let lastKey='';
async function pollDesk(){
  if(!DESK) return;
  try{
    const r=await fetch(DESK+'/alerts.json'); if(!r.ok) return;
    const alerts=await r.json(); const last=alerts[0]; if(!last) return;
    const key=(last.ts||'')+(last.message||'');
    if(key!==lastKey){ lastKey=key; evt('DESK FEED ▸ '+String(last.event||'alert').toUpperCase()+': '+(last.message||'')+' — TICKER UPDATING…',true); }
  }catch{}
}
if(DESK) setInterval(pollDesk,4000);

/* ---------- clock + attachments ---------- */
setInterval(()=>{const d=new Date();$('clock').textContent=d.getHours()+':'+String(d.getMinutes()).padStart(2,'0');},10000);
if(!AUDIO_ONLY){
$('photoBtn').onclick=()=>$('photoInput').click();
$('fileBtn').onclick=()=>$('fileInput').click();
$('photoInput').onchange=e=>preview(e,true);
$('fileInput').onchange=e=>preview(e,false);
}
let photoB64=null,fileName=null;
function preview(e,isPhoto){
  const f=e.target.files[0]; if(!f) return;
  fileName=f.name;
  if(isPhoto){ const r=new FileReader(); r.onload=()=>{ photoB64=r.result; const img=document.createElement('img'); img.src=photoB64; $('attachPrev').appendChild(img); $('attachNote').textContent='photo attached → included with escalation'; }; r.readAsDataURL(f); }
  else{ $('attachNote').textContent='file attached: '+fileName; const s=document.createElement('span'); s.textContent='📄 '+fileName; $('attachPrev').appendChild(s); }
}

/* ---------- typed turns: type instead of speak (works in every mode) ---------- */
const TXT=$('txtBar'), TXTS=$('txtSend');
function sendTyped(){
  const m=(TXT.value||'').trim(); if(!m) return;
  TXT.value='';
  me(m);
  if (room && connected) {
    try { room.localParticipant.publishData(new TextEncoder().encode(m), {reliable:true, topic:'atlas-text'}); }
    catch(e){ evt('TYPE SEND FAILED', false); }
  } else { evt('TAP CALL FIRST — TYPE FLOWS AFTER CONNECT', false); }
}
TXTS.onclick=sendTyped;
TXT.addEventListener('keydown',e=>{ if(e.key==='Enter'){ e.preventDefault(); sendTyped(); } });
