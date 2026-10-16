/* RobertAris Racing Lab. Local browser client; no external services or libraries. */
'use strict';
const $ = id => document.getElementById(id);
const all = selector => [...document.querySelectorAll(selector)];
const finite = value => value !== null && value !== undefined && value !== '' && Number.isFinite(Number(value));
const num = (value, fallback = null) => finite(value) ? Number(value) : fallback;
const fmt = (value, digits = 2) => finite(value) ? Number(value).toFixed(digits) : '—';
const escapeHTML = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const colors = {teal:'#5cd9c3', amber:'#eabd72', violet:'#b79bff', white:'#f0eee5', muted:'#7f9fa0'};
const TERMINAL = new Set(['complete','completed','finished','failed','collision','no_progress','error']);
let latest = {}, mode = 'simulation', dirty = false, initialized = false, requestActive = false, commandCount = 0;
let overrideConfig = {};
let noticeTimer, mapImage, mapImageSource, mapReady = false, fitted = false, lastHistoryKey = '', localHistory = [], localTrail = [];
let showMap = true, configEvents = [], previousConfig = null, previousTime = null;
const camera = {x:0,y:0,scale:40,follow:false};
const overlays = {reference:true,driven:true,path:true,tree:false,grid:false,scan:false,body:true};
const mapCanvas = $('map-canvas');
let mapWidth = 800, mapHeight = 580, drag = null;

function showError(message) { $('error').textContent = message || ''; $('error').hidden = !message; }
function notice(message) { clearTimeout(noticeTimer); $('notice').textContent = message; $('notice').hidden = !message; noticeTimer = setTimeout(() => { $('notice').hidden = true; }, 6000); }
async function request(path, data) {
  const response = await fetch(path, data === undefined ? {cache:'no-store'} : {method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const result = await response.json();
  if (!response.ok || result.ok === false) throw new Error(result.error || `Request failed (${response.status})`);
  return result;
}
async function command(action, data = {}, success) {
  commandCount++;
  try { showError(''); const result = await request('/api/command', {action,...data}); if (success) notice(success); await poll(); return result; }
  catch (error) { showError(error.message); return null; }
  finally { commandCount--; }
}
function setConnected(connected) { $('connection-status').textContent = connected ? 'Local server connected' : 'Server disconnected'; $('connection-dot').className = `dot${connected ? '' : ' error'}`; }
async function poll() {
  if (requestActive) return;
  requestActive = true;
  try { const state = await request(initialized ? '/api/state' : '/api/bootstrap'); latest = state; setConnected(true); update(state); initialized = true; }
  catch (error) { setConnected(false); if (!initialized) showError(`Cannot reach the dashboard server. ${error.message}`); }
  finally { requestActive = false; }
}
async function pollLoop() { await poll(); setTimeout(pollLoop, 100); }

function paintRange(input) { const amount = (Number(input.value)-Number(input.min))/(Number(input.max)-Number(input.min))*100; input.style.setProperty('--range-progress', `${amount}%`); }
function markDirty() { dirty = true; $('pending-indicator').hidden = false; $('settings-state').textContent = 'PENDING'; $('apply-hint').textContent = 'Settings changed. Apply to update the active planner.'; }
function getConfig() {
  const config = {};
  all('[data-parameter]').forEach(el => config[el.dataset.parameter] = Number($(`${el.dataset.parameter}-number`).value));
  all('[data-advanced]').forEach(el => config[el.dataset.advanced] = Number(el.value)/(el.dataset.advanced === 'planning_budget' ? 1000 : 1));
  return config;
}
function fillConfig(config = {}) {
  all('[data-parameter]').forEach(el => { if (finite(config[el.dataset.parameter])) { el.value = config[el.dataset.parameter]; $(`${el.dataset.parameter}-number`).value = config[el.dataset.parameter]; paintRange(el); } });
  all('[data-advanced]').forEach(el => { if (finite(config[el.dataset.advanced])) el.value = Number((config[el.dataset.advanced]*(el.dataset.advanced === 'planning_budget' ? 1000 : 1)).toFixed(5)); });
}
function readSetup() { return {mode,repo:$('repo-path').value.trim(),helper:$('helper-path').value.trim(),waypoints:$('waypoints-path').value.trim(),map_yaml:$('map-path').value.trim(),case:$('scenario').value,seed:Number($('seed').value),rate:Number($('sim-rate').value),config:{...overrideConfig}}; }
function desiredConfig(state = latest) { return {...state.defaults,...state.setup?.config}; }
function fillSetup(setup) {
  overrideConfig = {...(setup.config || {})};
  const paths = {'repo-path':'repo','helper-path':'helper','waypoints-path':'waypoints','map-path':'map_yaml'};
  Object.entries(paths).forEach(([id,key]) => { if (setup[key] !== undefined) $(id).value = setup[key] || ''; });
  $('live-map-path').value = setup.map_yaml || '';
  if (setup.case) $('scenario').value = setup.case;
  if (finite(setup.seed)) $('seed').value = setup.seed;
  if (finite(setup.rate)) $('sim-rate').value = setup.rate;
  fillConfig(desiredConfig());
}
function displayMode(next) {
  if(next!==mode){configEvents=[];previousConfig=null;previousTime=null;}
  mode = next;
  all('[data-mode]').forEach(button => { const active = button.dataset.mode === next; button.classList.toggle('active',active); button.setAttribute('aria-pressed',String(active)); });
  $('simulation-setup').hidden = next !== 'simulation'; $('simulation-controls').hidden = next !== 'simulation';
  $('live-setup').hidden = next !== 'live'; $('live-info').hidden = next !== 'live';
  $('replay-setup').hidden = next !== 'replay'; $('replay-controls').hidden = next !== 'replay';
  $('map-mode-label').textContent = ({simulation:'SIMULATION',live:'LIVE CAR',replay:'RECORDED RUN'})[next] || next.toUpperCase();
  $('page-title').textContent = ({simulation:'Find your line.',live:'Understand the drive.',replay:'Revisit the moment.'})[next];
  $('page-description').textContent = ({simulation:'See the plan. Tune the response. Compare the run.',live:'Follow the real car, its commands and the planner behind them.',replay:'Pause the run. Inspect the decisions. Keep the evidence.'})[next];
  $('chart-window').textContent = next === 'replay' ? 'Recorded telemetry · 30 s window' : 'Recent 30 seconds';
}
function sessionStatus(session) {
  if (!session) return 'Ready to connect';
  if (session.status) return String(session.status).replaceAll('_',' ');
  if (session.completed || session.finished) return 'Completed';
  if (session.running) return 'Running';
  if (session.paused) return 'Paused';
  return mode === 'live' ? 'Receiving telemetry' : mode === 'replay' ? 'Replay paused' : 'Ready';
}
function isRunning(session) { return !!session && (session.running === true || String(session.status).toLowerCase() === 'running'); }
function hasData(session) { return session && (session.pose || session.route?.length || session.path?.length); }
function point(p) { return Array.isArray(p) ? [Number(p[0]),Number(p[1])] : p && finite(p.x) && finite(p.y) ? [Number(p.x),Number(p.y)] : null; }
function points(array) { if (!Array.isArray(array)) return []; return array.map(point).filter(p => p && p.every(Number.isFinite)); }
function sampleHistory(session) {
  if (!session) return [];
  if (Array.isArray(session.history)) return session.history;
  const key = `${mode}:${session.time_s}:${session.metadata?.sequence ?? ''}`;
  if (key !== lastHistoryKey && finite(session.time_s)) {
    if (localHistory.length && Number(session.time_s) < Number(localHistory.at(-1).time_s)) { localHistory=[]; localTrail=[]; }
    localHistory.push({...session,history:undefined,trail:undefined,route:undefined,grid:undefined});
    if (session.pose) localTrail.push(point(session.pose));
    if (localHistory.length > 3000) localHistory.shift(); if (localTrail.length > 3000) localTrail.shift(); lastHistoryKey = key;
  }
  return localHistory;
}
function summaryFor(session, history) {
  const explicit = session?.summary || {};
  const mean = key => { const vals = history.map(row => num(row[key])).filter(v => v !== null); return vals.length ? vals.reduce((a,b)=>a+Math.abs(b),0)/vals.length : null; };
  const plans=history.map(row=>num(row.planning_ms)).filter(v=>v!==null).sort((a,b)=>a-b);
  return {...explicit,status:explicit.status || explicit.completion || sessionStatus(session),time_s:explicit.time_s ?? session?.time_s,mean_cross_track_error:explicit.mean_error_m ?? explicit.mean_cross_track_error ?? explicit.mean_error ?? mean('cross_track_error'),planning_p95_ms:explicit.planning_p95_ms ?? (plans.length ? plans[Math.min(plans.length-1,Math.floor(plans.length*.95))] : null)};
}
function renderDiagnostics(diagnostics) {
  const d = diagnostics && typeof diagnostics==='object' ? diagnostics : null;
  if(!d){$('diagnostics-count').textContent='Unavailable';$('diagnostics-panel').classList.remove('has-issues');$('diagnostics-content').innerHTML='<p class="field-help">This session exposes no additional diagnostics.</p>';return;}
  const errors=Object.entries(d.errors || {}),missing=Array.isArray(d.missing_topics)?d.missing_topics:[],stale=Array.isArray(d.stale_topics)?d.stale_topics:[];
  const issues=[errors.length?`${errors.length} error${errors.length===1?'':'s'}`:'',stale.length?`${stale.length} stale`:'',d.localization_jump?'Pose jump':''].filter(Boolean);
  $('diagnostics-count').textContent=issues.join(' · ') || (missing.length?`${missing.length} missing`:'Available');
  $('diagnostics-panel').classList.toggle('has-issues',!!issues.length || !!missing.length);
  const clearance=d.observed_clearance_m ?? d.observed_body_clearance_m;
  const jump=d.localization_jump===true ? `Detected${finite(d.localization_jump_m)?` · ${fmt(d.localization_jump_m,2)} m`:''}` : d.localization_jump===false?'No recent jump':'Unavailable';
  const details=[['Observed body clearance',finite(clearance)?`${fmt(clearance,3)} m`:'Unavailable'],['Planning time · p95',finite(d.planning_p95_ms)?`${fmt(d.planning_p95_ms,1)} ms`:'Unavailable'],['Localization jump',jump],['Transport age',finite(d.transport_age_s)?`${fmt(d.transport_age_s,2)} s`:'Unavailable']];
  let html='<dl class="diagnostic-metrics">'+details.map(([label,value])=>`<dt>${escapeHTML(label)}</dt><dd${label==='Localization jump'&&d.localization_jump?' class="diagnostic-alert"':''}>${escapeHTML(value)}</dd>`).join('')+'</dl>';
  if(errors.length)html+='<h4 class="diagnostic-heading">Channel errors</h4><ul class="diagnostic-list diagnostic-errors">'+errors.map(([channel,error])=>`<li><strong>${escapeHTML(channel)}:</strong> ${escapeHTML(typeof error==='object'?JSON.stringify(error):error)}</li>`).join('')+'</ul>';
  const list=(title,values)=>values.length?`<h4 class="diagnostic-heading">${escapeHTML(title)}</h4><ul class="diagnostic-list">${values.map(value=>`<li>${escapeHTML(typeof value==='object'?JSON.stringify(value):value)}</li>`).join('')}</ul>`:'';
  html+=list('Missing topics',missing)+list('Stale topics',stale);
  if(d.clearance_note)html+=`<p class="diagnostic-note">${escapeHTML(d.clearance_note)}</p>`;
  if(d.route_deviation_note)html+=`<p class="diagnostic-note">${escapeHTML(d.route_deviation_note)}</p>`;
  ['tracking_target','rejected_samples','planner_mode'].forEach(key=>{if(typeof d[key]==='string')html+=`<p class="diagnostic-note">${escapeHTML(key.replaceAll('_',' '))}: ${escapeHTML(d[key])}</p>`;});
  html+=list('Limitations',Array.isArray(d.limitations)?d.limitations:[]);
  $('diagnostics-content').innerHTML=html;
}
function update(state) {
  const setup = state.setup || {}, session = state.session;
  if (!initialized) {
    fillSetup(setup); displayMode(setup.mode || 'simulation');
    if(setup.repo && setup.mode==='simulation')$('connection-settings').open=false;
    Object.entries(state.tunable_ranges || {}).forEach(([key,range])=>{
      const factor=key==='planning_budget'?1000:1;
      const targets=[$(key),$(`${key}-number`),document.querySelector(`[data-advanced="${key}"]`)];
      if(Array.isArray(range))targets.filter(Boolean).forEach(el=>{el.min=range[0]*factor;el.max=range[1]*factor;if(el.type==='range')paintRange(el);});
    });
  }
  else if (setup.mode && setup.mode !== mode && !commandCount) displayMode(setup.mode);
  if (!dirty && document.activeElement?.tagName !== 'INPUT') fillConfig(desiredConfig(state));
  const status = sessionStatus(session);
  $('run-status').textContent = status.charAt(0).toUpperCase()+status.slice(1);
  $('run-dot').className = `dot${isRunning(session) || session?.status==='connected' ? '' : ['error','disconnected','invalid_pose','stale_pose'].includes(session?.status) ? ' error' : session ? ' warning' : ' muted'}`;
  $('elapsed').innerHTML = `${fmt(session?.time_s,1)}<small>s</small>`;
  $('progress').innerHTML = `${fmt(session?.progress_m,1)}${finite(session?.target_m) ? ` <small>/ ${fmt(session.target_m,1)}</small>` : ''}<small>m</small>`;
  $('planning-time').innerHTML = `${fmt(session?.planning_ms,1)}<small>ms</small>`;
  $('sensor-age').textContent = `${fmt(session?.pose_age_s ?? session?.metadata?.pose_age_s ?? session?.diagnostics?.pose_age_s ?? session?.diagnostics?.sensor_age_s,2)} / ${fmt(session?.scan_age_s ?? session?.metadata?.scan_age_s ?? session?.diagnostics?.scan_age_s ?? session?.diagnostics?.sensor_age_s,2)} s`;
  const diagnostics = session?.diagnostics;
  const firstChannelError=diagnostics?.errors && Object.entries(diagnostics.errors)[0];
  const channelReason=firstChannelError ? `${firstChannelError[0]}: ${typeof firstChannelError[1]==='string'?firstChannelError[1]:JSON.stringify(firstChannelError[1])}` : null;
  const decision = typeof diagnostics === 'string' ? diagnostics : diagnostics?.stop_reason || channelReason || (diagnostics?.localization_jump ? `Localization jump detected${finite(diagnostics.localization_jump_m)?` · ${fmt(diagnostics.localization_jump_m,2)} m`:''}`:null) || diagnostics?.reason || diagnostics?.decision || diagnostics?.planner_debug?.reason || session?.reason;
  $('decision').textContent = decision || (session ? 'Decision diagnostics unavailable on this branch' : 'No telemetry yet');
  renderDiagnostics(diagnostics);
  $('map-diagnostics').textContent = session ? (diagnostics?.note || (session.tree?.length ? 'Accepted RRT branches available · rejected samples require branch instrumentation.' : 'Extra sampling and rejection diagnostics depend on the active planner branch.')) : 'Telemetry will show what this planner branch exposes.';
  $('map-empty').hidden = !!hasData(session);
  $('command-source').textContent = session?.command_arc?.length ? '· derived from command' : '· unavailable';
  const metadata = session?.metadata || {};
  const repo = metadata.repo || setup.repo;
  $('checkout-name').textContent = metadata.branch || (repo ? repo.split('/').filter(Boolean).at(-1) : 'No checkout connected');
  $('checkout-name').title = repo || '';
  $('revision').textContent = [metadata.commit?.slice(0,10),metadata.dirty ? 'uncommitted changes' : '',mode === 'replay' ? metadata.recording_name : ''].filter(Boolean).join(' · ') || (repo ? 'Active planner checkout' : '—');
  const activeMismatch = session?.config && Object.entries(getConfig()).some(([key,value])=>Math.abs(value-Number(session.config[key]))>1e-7);
  const pending = dirty || activeMismatch;
  $('pending-indicator').hidden = !pending;
  $('settings-state').textContent = mode==='live' ? 'MONITORING' : mode==='replay' ? 'RECORDING' : pending ? 'PENDING' : session?.config ? 'APPLIED' : setup.repo ? 'CHECKOUT SET' : 'NOT CONNECTED';
  $('settings-state').classList.toggle('pending-mark',!!pending && mode==='simulation');
  $('run-button').innerHTML = isRunning(session) ? 'Ⅱ <span>Pause simulation</span>' : session && !TERMINAL.has(String(session.status).toLowerCase()) ? '▶ <span>Run simulation</span>' : '▶ <span>Start simulation</span>';
  $('step-button').disabled = isRunning(session) || session?.status==='loading';
  $('run-button').disabled = session?.status==='loading'; $('apply-button').disabled = !session?.config || TERMINAL.has(session?.status) || session?.status==='loading';
  $('active-config').innerHTML = session?.config ? `Active: <strong>${fmt(session.config.max_speed,2)} m/s</strong> · <strong>${fmt(session.config.tracking_lookahead,2)} m</strong> target${finite(diagnostics?.config_revision) ? ` · revision ${diagnostics.config_revision}` : ''}` : 'Active planner settings will appear after starting a simulation.';
  const history = sampleHistory(session);
  if(finite(session?.time_s) && previousTime!==null && Number(session.time_s)<previousTime){configEvents=[];previousConfig=null;}
  if(Array.isArray(session?.events)){
    configEvents=session.events.filter(event=>['config_applied','configuration_applied'].includes(event.event || event.type)).filter(event=>finite(event.time_s)).map(event=>({time_s:Number(event.time_s),label:Object.entries(event.config || {}).filter(([key])=>Object.hasOwn(getConfig(),key)).map(([key,value])=>`${key}: ${value}`).join(', ')}));
  }else if(session?.config && mode==='simulation'){
    const config=Object.fromEntries(Object.keys(getConfig()).map(key=>[key,session.config[key]]));
    if(previousConfig && JSON.stringify(config)!==JSON.stringify(previousConfig)){
      const changes=Object.entries(config).filter(([key,value])=>value!==previousConfig[key]).map(([key,value])=>`${key}: ${value}`);
      configEvents.push({time_s:Number(session.time_s),label:changes.join(', ')});if(configEvents.length>100)configEvents.shift();
    }previousConfig=config;
  }
  previousTime=num(session?.time_s);
  $('chart-window').textContent = `${mode==='replay'?'Recorded telemetry · ':''}Recent 30 seconds${configEvents.length ? ' · amber marks: settings applied' : ''}`;
  $('speed-value').innerHTML = `${fmt(session?.speed_command,2)} <small>m/s</small>`;
  $('steering-value').innerHTML = `${fmt(finite(session?.steering) ? session.steering*180/Math.PI : null,1)} <small>°</small>`;
  $('deviation-value').innerHTML = `${fmt(finite(session?.cross_track_error) ? Math.abs(session.cross_track_error) : null,2)} <small>m</small>`;
  $('cycle-value').innerHTML = `${fmt(session?.planning_ms,1)} <small>ms</small>`;
  drawChart('speed-chart',history,[{key:'speed_command',color:colors.teal},{key:'actual_speed',color:colors.white}],{min:0});
  drawChart('steering-chart',history,[{key:'steering',color:colors.amber,scale:180/Math.PI}],{symmetric:true});
  drawChart('deviation-chart',history,[{key:'cross_track_error',color:colors.violet,absolute:true}],{min:0});
  drawChart('cycle-chart',history,[{key:'planning_ms',color:colors.teal},{key:'cycle_ms',color:colors.muted}],{min:0});
  const summary = summaryFor(session,history), baseline = state.baseline?.summary;
  renderSummary('metric',session ? summary : null); renderSummary('baseline',baseline);
  $('baseline-save').disabled = !hasData(session); $('baseline-clear').disabled = !state.baseline;
  $('baseline-description').textContent = state.baseline ? 'Baseline saved. Its ghost trace stays on the map.' : 'Save a run to keep its driven trace on the map.';
  const frames = num(state.recording?.frames,0);
  $('recording-description').textContent = `${frames.toLocaleString()} telemetry frame${frames===1?'':'s'} captured${state.recording?.dropped_frames ? ` · ${state.recording.dropped_frames} older frames dropped` : ''}`;
  $('download-recording').setAttribute('aria-disabled',String(!frames));
  $('download-recording').style.opacity = frames ? '1' : '.4';
  updatePresets(state.presets || []);
  const replay = state.replay || {};
  $('replay-scrub').max = Math.max(0,(replay.total || 0)-1);
  if (document.activeElement !== $('replay-scrub')) $('replay-scrub').value = replay.index || 0;
  $('replay-count').textContent = `${replay.total || 0} frames`;
  $('replay-position').textContent = replay.total ? `Frame ${(replay.index || 0)+1} / ${replay.total}` : 'Frame —';
  $('replay-duration').textContent = finite(session?.time_s) ? `${fmt(session.time_s,2)} s` : '—';
  $('replay-play').textContent = replay.playing ? 'Ⅱ Pause recording' : '▶ Play recording';
  $('replay-play').disabled = !replay.total; $('replay-step').disabled = !replay.total;
  $('live-message').textContent = session?.status==='disconnected' ? 'ROS bridge disconnected. Showing last received data.' : session ? `${String(session.status || 'receiving').replaceAll('_',' ')} · ${metadata.source || 'ROS telemetry'}` : 'Waiting for the ROS bridge.';
  $('frame-label').textContent = metadata.map_frame ? `${metadata.map_frame.toUpperCase()} FRAME` : 'WORLD FRAME';
  if (state.error || state.map_error) showError(state.error || state.map_error);
  if (state.map?.image && state.map.image !== mapImageSource) {
    mapImageSource = state.map.image; mapReady = false; mapImage = new Image(); mapImage.onload = () => { mapReady=true; if(!fitted) fitView(); drawMap(); }; mapImage.src = state.map.image;
  }
  $('background-button').disabled = !state.map?.image;
  if (!fitted && hasData(session)) fitView();
  if (camera.follow && session?.pose) { camera.x=Number(session.pose[0]); camera.y=Number(session.pose[1]); }
  drawMap();
}
function renderSummary(prefix,summary) {
  $(`${prefix}-completion`).textContent = summary ? String(summary.status || summary.completion || 'Recorded') : '—';
  $(`${prefix}-time`).textContent = summary && finite(summary.time_s ?? summary.elapsed_s) ? `${fmt(summary.time_s ?? summary.elapsed_s,1)} s` : '—';
  $(`${prefix}-error`).textContent = summary && finite(summary.mean_error_m ?? summary.mean_cross_track_error ?? summary.mean_error) ? `${fmt(summary.mean_error_m ?? summary.mean_cross_track_error ?? summary.mean_error,3)} m` : '—';
  $(`${prefix}-planning`).textContent = summary && finite(summary.planning_p95_ms) ? `${fmt(summary.planning_p95_ms,1)} ms` : '—';
}
let presetSignature = '';
function updatePresets(presets) {
  const signature = JSON.stringify(presets.map(p => typeof p === 'string' ? p : p.name)); if (signature === presetSignature) return; presetSignature = signature;
  const selected = $('preset-select').value;
  $('preset-select').innerHTML = '<option value="">Saved presets</option>'+presets.map(p => { const name=typeof p==='string'?p:p.name; return `<option value="${escapeHTML(name)}">${escapeHTML(name)}</option>`; }).join('');
  $('preset-select').value = selected;
}

function setupCanvas(canvas) {
  const box = canvas.getBoundingClientRect(), dpr = window.devicePixelRatio || 1;
  const width = Math.max(1,box.width), height = Math.max(1,box.height);
  if (canvas.width !== Math.round(width*dpr) || canvas.height !== Math.round(height*dpr)) { canvas.width=Math.round(width*dpr); canvas.height=Math.round(height*dpr); }
  const ctx=canvas.getContext('2d'); ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,width,height); return {ctx,width,height};
}
function fitView() {
  const s = latest.session || {}, map = latest.map;
  let pts = [...points(s.route),...points(s.path),...points(s.trail || localTrail)];
  if (map && finite(map.resolution)) {
    const [ox,oy,yaw=0]=map.origin || [0,0,0], w=map.width*map.resolution,h=map.height*map.resolution;
    if (!pts.length) [[0,0],[w,0],[0,h],[w,h]].forEach(([x,y])=>pts.push([ox+Math.cos(yaw)*x-Math.sin(yaw)*y,oy+Math.sin(yaw)*x+Math.cos(yaw)*y]));
  }
  if (!pts.length && s.pose) pts = [[s.pose[0]-3,s.pose[1]-3],[s.pose[0]+3,s.pose[1]+3]];
  if (!pts.length) return;
  const xs=pts.map(p=>p[0]),ys=pts.map(p=>p[1]),xmin=Math.min(...xs),xmax=Math.max(...xs),ymin=Math.min(...ys),ymax=Math.max(...ys);
  camera.x=(xmin+xmax)/2; camera.y=(ymin+ymax)/2;
  camera.scale=Math.max(3,Math.min(500,(mapWidth-100)/Math.max(xmax-xmin,3),(mapHeight-110)/Math.max(ymax-ymin,3)));
  fitted=true; camera.follow=false; $('follow-button').setAttribute('aria-pressed','false');
}
function line(ctx,pts,color,width=2,dash=[]) {
  const p=points(pts); if(p.length<2) return;
  ctx.beginPath(); ctx.moveTo(p[0][0],p[0][1]); for(let i=1;i<p.length;i++) ctx.lineTo(p[i][0],p[i][1]);
  ctx.strokeStyle=color;ctx.lineWidth=width/camera.scale;ctx.setLineDash(dash.map(v=>v/camera.scale));ctx.lineJoin='round';ctx.lineCap='round';ctx.stroke();ctx.setLineDash([]);
}
function poseArray(s) { if(Array.isArray(s?.pose))return s.pose; if(s?.pose)return [s.pose.x,s.pose.y,s.pose.yaw]; return null; }
function deriveArc(s) {
  const p=poseArray(s); if(!p || !s.config?.wheelbase || mode==='live' || !finite(s.steering)||!finite(s.speed_command))return [];
  const wheelbase=s.config?.wheelbase || .33,curvature=Math.tan(s.steering)/wheelbase;
  const distance=Math.max(.3,Math.min(2.5,Math.abs(s.speed_command)*.75)),out=[];
  for(let i=0;i<=24;i++){const d=distance*i/24,lx=Math.abs(curvature)<1e-5?d:Math.sin(curvature*d)/curvature,ly=Math.abs(curvature)<1e-5?0:(1-Math.cos(curvature*d))/curvature;out.push([p[0]+Math.cos(p[2])*lx-Math.sin(p[2])*ly,p[1]+Math.sin(p[2])*lx+Math.cos(p[2])*ly]);}
  return out;
}
let gridCanvas, gridDataKey;
function drawOccupancy(ctx,grid) {
  if(!grid?.data || !grid.width || !grid.height || !finite(grid.resolution))return;
  const key=grid.data;
  if(key!==gridDataKey){
    gridDataKey=key;gridCanvas=document.createElement('canvas');gridCanvas.width=grid.width;gridCanvas.height=grid.height;
    const gc=gridCanvas.getContext('2d'),pixels=gc.createImageData(grid.width,grid.height),flat=Array.isArray(grid.data[0])?grid.data.flat():grid.data;
    for(let y=0;y<grid.height;y++)for(let x=0;x<grid.width;x++){
      const v=Number(flat[y*grid.width+x]),i=((grid.height-1-y)*grid.width+x)*4;if(v>0){pixels.data[i]=230;pixels.data[i+1]=139;pixels.data[i+2]=109;pixels.data[i+3]=v===1?80:Math.min(160,40+v);}else if(v<0){pixels.data[i]=108;pixels.data[i+1]=130;pixels.data[i+2]=115;pixels.data[i+3]=35;}
    }gc.putImageData(pixels,0,0);
  }
  ctx.save();const [x,y,yaw=0]=grid.origin || [0,0,0];ctx.translate(x,y);ctx.rotate(yaw);ctx.scale(grid.resolution,-grid.resolution);ctx.imageSmoothingEnabled=false;ctx.drawImage(gridCanvas,0,-grid.height);ctx.restore();
}
function drawMap() {
  const {ctx,width,height}=setupCanvas(mapCanvas);mapWidth=width;mapHeight=height;
  const s=latest.session || {},p=poseArray(s),scale=camera.scale;
  ctx.fillStyle='#0f1713';ctx.fillRect(0,0,width,height);
  const step=scale<12?5:scale<28?2:scale<150?1:.25;
  const left=camera.x-width/2/scale,right=camera.x+width/2/scale,bottom=camera.y-height/2/scale,top=camera.y+height/2/scale;
  ctx.save();ctx.translate(width/2,height/2);ctx.scale(scale,-scale);ctx.translate(-camera.x,-camera.y);
  ctx.fillStyle='#263b2e';
  for(let x=Math.ceil(left/step)*step;x<right;x+=step)for(let y=Math.ceil(bottom/step)*step;y<top;y+=step){ctx.beginPath();ctx.arc(x,y,.65/scale,0,Math.PI*2);ctx.fill();}
  const map=latest.map;
  if(showMap && mapReady && map?.image === mapImageSource){ctx.save();const [x,y,yaw=0]=map.origin || [0,0,0];ctx.translate(x,y);ctx.rotate(yaw);ctx.scale(map.resolution,-map.resolution);ctx.globalAlpha=.95;ctx.imageSmoothingEnabled=false;ctx.drawImage(mapImage,0,-map.height,map.width,map.height);ctx.restore();}
  if(overlays.grid)drawOccupancy(ctx,s.grid);
  if(overlays.reference)line(ctx,s.route,colors.violet,1.6,[6,5]);
  if(latest.baseline?.trace)line(ctx,latest.baseline.trace,'#eabd7250',2,[3,5]);
  if(overlays.tree && Array.isArray(s.tree)){
    ctx.globalAlpha=.55;
    if(s.tree.length && Array.isArray(s.tree[0]) && s.tree[0].length===2 && finite(s.tree[0][0])) {
      for(let i=0;i+1<s.tree.length;i+=2)line(ctx,[s.tree[i],s.tree[i+1]],'#56877b',.8);
    } else s.tree.forEach(edge=>{if(Array.isArray(edge)&&Array.isArray(edge[0]))line(ctx,edge,'#56877b',.8);else if(Array.isArray(edge)&&edge.length>=4)line(ctx,[[edge[0],edge[1]],[edge[2],edge[3]]],'#56877b',.8);else if(edge?.start&&edge?.end)line(ctx,[edge.start,edge.end],'#56877b',.8);});ctx.globalAlpha=1;
  }
  if(overlays.scan){ctx.fillStyle='#e3a56a';points(s.scan_hits).forEach(([x,y])=>{ctx.beginPath();ctx.arc(x,y,1.4/scale,0,Math.PI*2);ctx.fill();});}
  if(overlays.driven)line(ctx,s.trail || localTrail,'#eeeae0aa',2.1);
  if(overlays.path){line(ctx,s.path,colors.teal,2.6);const goal=point(s.goal);if(goal){ctx.strokeStyle=colors.teal;ctx.lineWidth=1.3/scale;ctx.beginPath();ctx.arc(goal[0],goal[1],5/scale,0,Math.PI*2);ctx.stroke();}}
  if(overlays.body){
    if(Array.isArray(s.footprints))s.footprints.forEach(poly=>{line(ctx,poly,colors.amber+'50',1);});
    line(ctx,s.command_arc?.length?s.command_arc:deriveArc(s),colors.amber,2,[5,3]);
  }
  if(p && finite(p[0])&&finite(p[1])){
    ctx.save();ctx.translate(p[0],p[1]);ctx.rotate(p[2]||0);
    if(overlays.body && s.config?.vehicle_front){const c=s.config,front=c.vehicle_front,rear=c.vehicle_rear,half=c.vehicle_half_width;ctx.fillStyle='#182a23cc';ctx.strokeStyle=colors.amber;ctx.lineWidth=1.2/scale;ctx.beginPath();ctx.rect(-rear,-half,front+rear,half*2);ctx.fill();ctx.stroke();}
    const size=Math.max(.11,7/scale);ctx.fillStyle=colors.teal;ctx.strokeStyle='#0d2018';ctx.lineWidth=1.5/scale;ctx.beginPath();ctx.moveTo(size,0);ctx.lineTo(-size*.7,-size*.6);ctx.lineTo(-size*.35,0);ctx.lineTo(-size*.7,size*.6);ctx.closePath();ctx.fill();ctx.stroke();ctx.restore();
  }
  ctx.restore();
  const distance=scale<20?5:scale<50?2:scale<150?1:.5;
  $('map-scale').firstElementChild.style.width=`${distance*scale}px`;$('map-scale').lastElementChild.textContent=`${distance} m`;
}

function drawChart(id,history,series,options={}) {
  const {ctx,width,height}=setupCanvas($(id));
  const margin={left:29,right:5,top:8,bottom:20},w=width-margin.left-margin.right,h=height-margin.top-margin.bottom;
  const rows=history.filter(row=>finite(row.time_s));
  const end=rows.length ? Number(rows.at(-1).time_s) : 30,start=Math.max(0,end-30),span=Math.max(1,end-start);
  const visible=rows.filter(row=>Number(row.time_s)>=start);
  const values=visible.flatMap(row=>series.map(s=>finite(row[s.key])?(s.absolute?Math.abs(row[s.key]):Number(row[s.key]))*(s.scale||1):null)).filter(v=>v!==null);
  let min=options.min ?? Math.min(0,...values),max=Math.max(...values,options.symmetric?1:0.1);
  if(options.symmetric){max=Math.max(Math.abs(min),Math.abs(max),1);min=-max;}
  if(!values.length){min=0;max=1;}else{const pad=(max-min)*.15 || .1;max+=pad;if(options.min===undefined)min-=pad;}
  const x=t=>margin.left+(t-start)/span*w,y=v=>margin.top+h-(v-min)/(max-min)*h;
  ctx.font='8px ui-monospace,monospace';ctx.textBaseline='middle';
  for(let i=0;i<3;i++){const yy=margin.top+i*h/2,val=max-(max-min)*i/2;ctx.strokeStyle='#2a372e';ctx.lineWidth=.7;ctx.beginPath();ctx.moveTo(margin.left,yy);ctx.lineTo(width-margin.right,yy);ctx.stroke();ctx.fillStyle='#718377';ctx.textAlign='right';ctx.fillText(Math.abs(val)>=100?val.toFixed(0):Math.abs(val)>=10?val.toFixed(1):val.toFixed(2),margin.left-6,yy);}
  ctx.textBaseline='alphabetic';ctx.textAlign='left';ctx.fillStyle='#647a6b';ctx.fillText(`${start.toFixed(0)}s`,margin.left,height-3);ctx.textAlign='right';ctx.fillText(`${end.toFixed(0)}s`,width-margin.right,height-3);
  if(!values.length){ctx.textAlign='center';ctx.fillStyle='#728579';ctx.font='9px -apple-system,sans-serif';ctx.fillText('No samples yet',margin.left+w/2,margin.top+h/2+3);return;}
  ctx.save();ctx.beginPath();ctx.rect(margin.left,margin.top,w,h);ctx.clip();
  configEvents.filter(event=>event.time_s>=start && event.time_s<=end).forEach(event=>{const xx=x(event.time_s);ctx.strokeStyle='#eabd7270';ctx.lineWidth=1;ctx.setLineDash([2,3]);ctx.beginPath();ctx.moveTo(xx,margin.top);ctx.lineTo(xx,margin.top+h);ctx.stroke();ctx.setLineDash([]);});
  $(id).title=configEvents.map(event=>`Applied at ${event.time_s.toFixed(2)} s: ${event.label}`).join('\n');
  series.forEach(s=>{ctx.beginPath();let pen=false;visible.forEach(row=>{if(!finite(row[s.key])){pen=false;return;}const xx=x(Number(row.time_s)),yy=y((s.absolute?Math.abs(row[s.key]):Number(row[s.key]))*(s.scale||1));if(pen)ctx.lineTo(xx,yy);else ctx.moveTo(xx,yy);pen=true;});ctx.strokeStyle=s.color;ctx.lineWidth=1.6;ctx.lineJoin='round';ctx.stroke();});
  ctx.restore();
}

all('[data-parameter]').forEach(el=>{paintRange(el);el.addEventListener('input',()=>{$(`${el.dataset.parameter}-number`).value=el.value;overrideConfig[el.dataset.parameter]=Number(el.value);paintRange(el);markDirty();});});
all('[data-number]').forEach(el=>el.addEventListener('input',()=>{const range=$(el.dataset.number);range.value=el.value;overrideConfig[el.dataset.number]=Number(el.value);paintRange(range);markDirty();}));
all('[data-advanced]').forEach(el=>el.addEventListener('input',()=>{overrideConfig[el.dataset.advanced]=Number(el.value)/(el.dataset.advanced==='planning_budget'?1000:1);markDirty();}));
all('[data-mode]').forEach(button=>button.addEventListener('click',async()=>{if(button.dataset.mode===mode)return; const next=button.dataset.mode; const result=await command('configure',{setup:{mode:next}});if(result){displayMode(next);fitted=false;localHistory=[];localTrail=[];$('connection-settings').open=true;}}));
$('connect-button').addEventListener('click',async()=>{const result=await command('configure',{setup:readSetup()},'Checkout connected. Start a simulation when ready.');if(result){dirty=false;$('pending-indicator').hidden=true;$('connection-settings').open=false;fitted=false;}});
$('empty-connect').addEventListener('click',()=>{$('connection-settings').open=true;(mode==='simulation'?$('repo-path'):mode==='replay'?$('recording-file'):$('live-connect-button')).focus();});
$('live-connect-button').addEventListener('click',()=>command('configure',{setup:{mode:'live',map_yaml:$('live-map-path').value.trim()}},'Listening for live telemetry. Start the ROS bridge in your ROS environment.'));
async function applyConfig(restart=false){
  const config=getConfig();
  if(Object.values(config).some(value=>typeof value==='number'&&!Number.isFinite(value))){showError('Enter a valid number for each planner parameter.');return;}
  const result=restart ? await command('configure',{setup:{...readSetup(),config}}) : await command('apply',{config});
  if(result){overrideConfig={...config};dirty=false;$('pending-indicator').hidden=true;$('apply-hint').textContent='Settings requested. Active values update after the worker applies them.';if(restart){if(await command('reset')){fitted=false;localHistory=[];localTrail=[];await command('start');}}else notice('Planner settings requested. Check the active values for confirmation.');}
}
$('apply-button').addEventListener('click',()=>applyConfig());$('restart-button').addEventListener('click',()=>applyConfig(true));
$('run-button').addEventListener('click',async()=>{
  if(isRunning(latest.session)){await command('pause');return;}
  if(!latest.session || TERMINAL.has(String(latest.session.status).toLowerCase())){
    if(!(await command('configure',{setup:readSetup()})))return;
    dirty=false;$('pending-indicator').hidden=true;$('connection-settings').open=false;fitted=false;localHistory=[];localTrail=[];
    await command('start');
  }else await command('resume');
});
$('step-button').addEventListener('click',async()=>{if(!latest.session){if(!(await command('configure',{setup:readSetup()})))return;if(!(await command('reset')))return;}await command('step');});
$('reset-button').addEventListener('click',async()=>{if(!(await command('configure',{setup:readSetup()})))return;if(await command('reset')){dirty=false;localHistory=[];localTrail=[];fitted=false;}});
$('sim-rate').addEventListener('change',()=>command('rate',{rate:Number($('sim-rate').value)}));
$('save-preset').addEventListener('click',()=>{$('preset-name-row').hidden=false;$('preset-name').focus();});
$('cancel-save-preset').addEventListener('click',()=>{$('preset-name-row').hidden=true;});
$('confirm-save-preset').addEventListener('click',async()=>{const name=$('preset-name').value.trim();if(!name){$('preset-name').focus();return;}if(await command('preset_save',{name,config:getConfig()},`Preset “${name}” saved.`)){$('preset-name-row').hidden=true;$('preset-name').value='';}});
$('preset-name').addEventListener('keydown',event=>{if(event.key==='Enter')$('confirm-save-preset').click();});
$('load-preset').addEventListener('click',async()=>{const name=$('preset-select').value;if(!name){notice('Choose a saved preset first.');return;}const result=await command('preset_load',{name},`Preset “${name}” loaded. Apply it to update the planner.`);if(result){overrideConfig={...(result.config || latest.setup?.config || {})};fillConfig(result.config || desiredConfig());markDirty();}});
$('export-params').addEventListener('click',async()=>{const config=getConfig();if(await command('configure',{setup:{config}})){overrideConfig={...config};window.location.assign('/api/preset.yaml');notice('Exported pending settings for ROS startup. The running car is unchanged.');}});
$('baseline-save').addEventListener('click',()=>command('baseline',{},'Baseline saved. Restart the same scenario to compare.'));
$('baseline-clear').addEventListener('click',()=>command('clear_baseline'));
$('download-recording').addEventListener('click',event=>{if(!latest.recording?.frames){event.preventDefault();notice('Start a run to capture telemetry before downloading.');}});
$('recording-file').addEventListener('change',async event=>{const file=event.target.files?.[0];if(!file)return;try{showError('');const content=await file.text();await request('/api/import',{name:file.name,content});displayMode('replay');fitted=false;localHistory=[];localTrail=[];$('recording-file-name').textContent=file.name;$('connection-settings').open=false;await poll();notice(`Loaded ${file.name}.`);}catch(error){showError(error.message);}event.target.value='';});
$('replay-play').addEventListener('click',()=>command(latest.replay?.playing?'replay_pause':'replay_play'));
$('replay-step').addEventListener('click',async()=>{await command('replay_pause');await command('replay_seek',{index:Math.min((latest.replay?.index||0)+1,(latest.replay?.total||1)-1)});});
let seekTimer;
$('replay-scrub').addEventListener('input',()=>{clearTimeout(seekTimer);seekTimer=setTimeout(()=>command('replay_seek',{index:Number($('replay-scrub').value)}),60);});
$('follow-button').addEventListener('click',()=>{camera.follow=!camera.follow;$('follow-button').setAttribute('aria-pressed',String(camera.follow));drawMap();});
$('background-button').addEventListener('click',()=>{showMap=!showMap;$('background-button').setAttribute('aria-pressed',String(showMap));drawMap();});
$('fit-button').addEventListener('click',()=>{fitView();drawMap();});
all('[data-overlay]').forEach(el=>el.addEventListener('change',()=>{overlays[el.dataset.overlay]=el.checked;drawMap();}));
mapCanvas.addEventListener('pointerdown',event=>{drag={x:event.clientX,y:event.clientY,cx:camera.x,cy:camera.y};mapCanvas.setPointerCapture(event.pointerId);camera.follow=false;$('follow-button').setAttribute('aria-pressed','false');});
mapCanvas.addEventListener('pointermove',event=>{if(!drag)return;camera.x=drag.cx-(event.clientX-drag.x)/camera.scale;camera.y=drag.cy+(event.clientY-drag.y)/camera.scale;drawMap();});
mapCanvas.addEventListener('pointerup',()=>{drag=null;});mapCanvas.addEventListener('pointercancel',()=>{drag=null;});
mapCanvas.addEventListener('wheel',event=>{event.preventDefault();const rect=mapCanvas.getBoundingClientRect(),sx=event.clientX-rect.left-mapWidth/2,sy=event.clientY-rect.top-mapHeight/2;const wx=camera.x+sx/camera.scale,wy=camera.y-sy/camera.scale;camera.scale=Math.max(2,Math.min(800,camera.scale*Math.exp(-event.deltaY*.001)));camera.x=wx-sx/camera.scale;camera.y=wy+sy/camera.scale;camera.follow=false;$('follow-button').setAttribute('aria-pressed','false');drawMap();},{passive:false});
new ResizeObserver(()=>{drawMap();if(initialized)update(latest);}).observe($('map-stage'));
window.addEventListener('resize',()=>{if(initialized)update(latest);});
pollLoop();
