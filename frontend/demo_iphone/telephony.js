/* Atlas simulated telephony — dial tone, DTMF keypad, ringback, narrowband telco audio.
   No real phone number: pure UX theater layered over the LiveKit audio bridge.
   Self-contained: injects its own styles + overlay DOM. */
const telephony = (() => {
  let ctx = null, master = null;
  let dialOsc = null, ringTimer = null, timerStop = null, skipping = false;
  const overlayState = { el: null, num: '', status: null, shown: false };

  function ac() {
    if (!ctx) {
      ctx = new (window.AudioContext || window.webkitAudioContext)();
      master = ctx.createGain(); master.gain.value = 0.5; master.connect(ctx.destination);
    }
    if (ctx.state === 'suspended') ctx.resume();
    return ctx;
  }
  function tone(freqs, dur, vol = 0.25, gap = 0.03) {
    const c = ac(), t = c.currentTime;
    const g = c.createGain(); g.gain.value = 0; g.connect(master);
    const osc = freqs.map(f => { const o = c.createOscillator(); o.frequency.value = f; o.connect(g); o.start(); return o; });
    g.gain.setValueAtTime(0, t);
    g.gain.linearRampToValueAtTime(vol, t + 0.02);
    g.gain.setValueAtTime(vol, t + dur - gap);
    g.gain.linearRampToValueAtTime(0, t + dur);
    osc.forEach(o => o.stop(t + dur + 0.05));
  }
  /* ITU DTMF pairs */
  const DTMF = { '1':[697,1209], '2':[697,1336], '3':[697,1477], '4':[770,1209], '5':[770,1336],
    '6':[770,1477], '7':[852,1209], '8':[852,1336], '9':[852,1477], '*':[941,1209],
    '0':[941,1336], '#':[941,1477] };
  function dtmf(k) { const p = DTMF[k]; if (p) tone(p, 0.12, 0.28); }
  function dialTone(on) {
    const c = ac();
    if (on && !dialOsc) {
      const g = c.createGain(); g.gain.value = 0.09; g.connect(master);
      const os = [350, 440].map(f => { const o = c.createOscillator(); o.frequency.value = f; o.connect(g); o.start(); return o; });
      dialOsc = { g, os };
    }
    if (!on && dialOsc) { try { dialOsc.os.forEach(o => o.stop()); dialOsc.g.disconnect(); } catch (e) {} dialOsc = null; }
  }
  function ring(on) {
    if (on && !ringTimer) { const cycle = () => tone([440, 480], 1.8, 0.22); cycle(); ringTimer = setInterval(cycle, 6000); }
    if (!on && ringTimer) { clearInterval(ringTimer); ringTimer = null; }
  }
  function pickup() { tone([425], 0.09, 0.2); }
  function blip() { tone([480, 620], 0.35, 0.2); }

  /* ---- overlay UI (injected) ---- */
  const CSS = `
  .dialOverlay{position:absolute;inset:0;z-index:60;display:flex;flex-direction:column;align-items:center;
    background:radial-gradient(ellipse at top,#1b2130 0%,#0d1119 70%);padding:16px 0 14px;}
  .dialOverlay .dNum{font-size:24px;letter-spacing:2px;font-weight:700;color:#eaf2ff;margin:12px 0 2px;min-height:30px;}
  .dialOverlay .dStatus{font-size:10px;color:#8fb3ff;letter-spacing:3px;min-height:16px;text-transform:uppercase;}
  .dialOverlay .dPad{display:grid;grid-template-columns:repeat(3,58px);gap:8px;margin:12px 0 10px;}
  .dialOverlay .dPad button{height:44px;border-radius:22px;border:1px solid #33415c;color:#eaf2ff;
    background:linear-gradient(180deg,#1b2334,#141b28);font-size:18px;font-weight:600;cursor:pointer;}
  .dialOverlay .dSkip{font-size:10px;letter-spacing:2px;color:#5c739a;margin-top:auto;}
  .dialOverlay .dEnd{margin-top:8px;height:44px;width:44px;border-radius:22px;border:none;background:#a4283d;color:#fff;font-size:19px;cursor:pointer;}
  `;
  function overlay() {
    const scr = document.querySelector('.screen');
    let el = document.querySelector('.dialOverlay');
    if (el) return el;
    const st = document.createElement('style'); st.textContent = CSS; document.head.appendChild(st);
    el = document.createElement('div'); el.className = 'dialOverlay';
    el.innerHTML = `
      <div class="dNum"></div>
      <div class="dStatus">ENTER NUMBER</div>
      <div class="dPad"></div>
      <div class="dSkip">TAP ANYWHERE TO SKIP RING</div>
      <button class="dEnd" title="end">✕</button>`;
    const pad = el.querySelector('.dPad');
    ['1','2','3','4','5','6','7','8','9','*','0','#'].forEach(k => {
      const b = document.createElement('button'); b.textContent = k;
      b.addEventListener('click', ev => { ev.stopPropagation(); dtmf(k); state.num = state.num.slice(0, 11); paintNum(state.num + k); });
      pad.appendChild(b);
    });
    el.querySelector('.dEnd').addEventListener('click', ev => { ev.stopPropagation(); hide(); endAll(); });
    el.addEventListener('click', () => { skipping = true; });
    scr.appendChild(el); overlayState.el = el;
    return el;
  }
  const state = { num: '' };
  function paintNum(num) {
    if (!overlayState.el) overlay();
    const groups = [num.slice(0,1), num.slice(1,4), num.slice(4,8), num.slice(8,11)];
    overlayState.el.querySelector('.dNum').textContent = groups.filter(Boolean).join(' ');
  }
  function setStatus(txt) { if (overlayState.el) overlayState.el.querySelector('.dStatus').textContent = txt; }
  function hide() { if (overlayState.el) { overlayState.el.remove(); overlayState.el = null; } }

  /* ---- theater: dial tone → auto-dial 1-855-ATLAS-24 → DTMF → ring → pickup ---- */
  const ATLAS_NUM = '18552852724';   /* 1-855-ATLAS-24 → ATLAS=2852 */
  function wait(ms) { return new Promise(r => setTimeout(r, ms)); }

  async function dialSequence() {
    skipping = false; state.num = '';
    overlay(); paintNum(''); setStatus('ENTER NUMBER');
    dialTone(true);
    await wait(700); if (skipping) return afterDial();
    const digits = ATLAS_NUM.split('');
    for (const d of digits) {
      dtmf(d); state.num += d; paintNum(state.num);
      await wait(165); if (skipping) { dialTone(false); ring(false); return afterDial(); }
    }
    dialTone(false);
    setStatus('DIALING…'); await wait(600); if (skipping) return afterDial();
    setStatus('RINGING… ATLAS DISPATCH');
    ring(true);
    /* ~1 ring then pickup, ~3.7 s total; tap to skip */
    for (let i = 0; i < 37 && !skipping; i++) await wait(100);
    if (skipping) { clearInterval(ringTimer); ringTimer = null; }
    return afterDial();
  }
  function afterDial() {
    ring(false); pickup(); setStatus('CONNECTED ✓');
    return wait(450).then(hide);
  }

  /* ---- in-call telco sound ---- */
  /* Narrowband 300–3400 Hz on agent inbound: real "telephone voice" */
  function telco(audioEl) {
    const c = ac();
    const wire = () => {
      try {
        const src = c.createMediaElementSource(audioEl);
        const hp = c.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 300; hp.Q.value = 0.7;
        const lp = c.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 3400; lp.Q.value = 0.7;
        const g = c.createGain(); g.gain.value = 2.4;
        src.connect(hp); hp.connect(lp); lp.connect(g); g.connect(c.destination);
      } catch (e) { /* fall through: raw audio still plays */ }
    };
    if (c.state === 'running') { wire(); return; }
    /* Context suspended (Chrome autoplay policy): do NOT tap the element yet —
       a suspended graph mutes the element permanently. Wait for resume; only
       route through the telco graph once the context is actually running. */
    Promise.resolve(c.resume()).then(() => { if (c.state === 'running') wire(); }).catch(() => {});
  }
  /* Acquire the mic at click-gesture time: Chrome needs a fresh user gesture
     to show the permission prompt, and denial is silent in async callbacks. */
  async function grabMic() {
    ac();  /* ensure/resume the shared context inside the gesture */
    return navigator.mediaDevices.getUserMedia({
      audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true }
    });
  }
  /* Caller mic → narrowband → custom LiveKit audio track. Returns true on success. */
  async function telcoMic(room, raw) {
    try {
      const c = ac();
      if (!raw) raw = await grabMic();
      const src = c.createMediaStreamSource(raw);
      const hp = c.createBiquadFilter(); hp.type = 'highpass'; hp.frequency.value = 300;
      const lp = c.createBiquadFilter(); lp.type = 'lowpass'; lp.frequency.value = 3400;
      const g = c.createGain(); g.gain.value = 1.5;
      const dest = c.createMediaStreamDestination();
      src.connect(hp); hp.connect(lp); lp.connect(g); g.connect(dest);
      const track = new LivekitClient.LocalAudioTrack(dest.stream.getAudioTracks()[0]);
      await room.localParticipant.publishTrack(track);
      return true;
    } catch (e) { return false; }
  }
  /* live call timer — returns stop fn */
  function timer(render) {
    stopTimer(); let s = 0;
    const show = () => { const m = String(Math.floor(s / 60)).padStart(2, '0'), ss = String(s % 60).padStart(2, '0'); render('LIVE CALL ' + m + ':' + ss); };
    show(); timerStop = setInterval(show, 1000);
    return stopTimer;
  }
  function stopTimer() { if (timerStop) { clearInterval(timerStop); timerStop = null; } }

  function endAll() { dialTone(false); ring(false); blip(); stopTimer(); }

  return { dialSequence, dtmf, telco, telcoMic, grabMic, timer, endAll, blip };
})();
