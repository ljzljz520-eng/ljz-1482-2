/* 航拍农田报告工作台 — 桌面主逻辑 */
const $ = s => document.querySelector(s);
const $$ = s => [...document.querySelectorAll(s)];
let S = null, map, layers = {}, selectedFid = null;

async function api(method, path, body) {
  const opt = { method, headers: { 'Content-Type': 'application/json', 'X-User-Id': userId() } };
  if (body !== undefined) opt.body = JSON.stringify(body);
  const r = await fetch(path, opt);
  if (!r.ok) { const t = await r.text(); throw new Error((JSON.parse(t).error) || t); }
  return r.json();
}
const userId = () => $('#userSelect')?.value || 1;
function toast(msg, bad) {
  const t = $('#toast'); t.textContent = msg; t.style.color = bad ? '#ff8a80' : '#fff';
  t.classList.add('show'); clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove('show'), 2600);
}
const esc = s => (s == null ? '' : String(s).replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c])));
const tc = ms => ms == null ? '' : new Date(ms).toISOString().substr(11, 12);

/* ---------------- map ---------------- */
function initMap() {
  map = L.map('map', { zoomControl: true }).setView([30.0, 120.0], 15);
  L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png',
    { maxZoom: 20, attribution: '© OpenStreetMap', crossOrigin: true }).addTo(map);
  layers.plots = L.layerGroup().addTo(map);
  layers.old = L.layerGroup();
  layers.foot = L.layerGroup();
  layers.err = L.layerGroup().addTo(map);
  layers.flights = L.layerGroup().addTo(map);
  layers.findings = L.layerGroup().addTo(map);
  layers.drones = L.layerGroup().addTo(map);
  $('#lyPlots').onchange = e => e.target.checked ? map.addLayer(layers.plots) : map.removeLayer(layers.plots);
  $('#lyFootprints').onchange = e => e.target.checked ? map.addLayer(layers.foot) : map.removeLayer(layers.foot);
  $('#lyErrors').onchange = e => e.target.checked ? map.addLayer(layers.err) : map.removeLayer(layers.err);
  $('#lyFlights').onchange = e => e.target.checked ? map.addLayer(layers.flights) : map.removeLayer(layers.flights);
  $('#lyOldBoundary').onchange = e => e.target.checked ? map.addLayer(layers.old) : map.removeLayer(layers.old);
  drawLegend();
}
function drawLegend() {
  $('#mapLegend').innerHTML =
    '<span><span class="dot" style="background:#2e7d32"></span>当前地块边界</span><br>' +
    '<span><span class="dot" style="background:#9e9e9e"></span>旧版本边界(虚线)</span><br>' +
    '<span><span class="dot" style="background:#ef6c00"></span>发现(镜头投影目标点)</span><br>' +
    '<span><span class="dot" style="background:#1565c0"></span>无人机位置(≠目标点)</span><br>' +
    '<span><span class="dot" style="background:rgba(239,108,0,.15);border:1px dashed #ef6c00"></span>定位误差圆</span>';
}
function gj(g) { return typeof g === 'string' ? JSON.parse(g) : g; }

function drawMap() {
  layers.plots.clearLayers(); layers.old.clearLayers(); layers.foot.clearLayers();
  layers.err.clearLayers(); layers.flights.clearLayers(); layers.findings.clearLayers(); layers.drones.clearLayers();
  const plotColor = { 1: '#2e7d32', 2: '#00897b', 3: '#6d4c41' };
  S.plots.forEach(p => {
    const cur = p.versions.find(v => v.version === p.active_version) || p.versions[0];
    L.geoJSON(gj(cur.boundary_geojson), {
      style: { color: plotColor[p.id] || '#2e7d32', weight: 2.5, fillOpacity: 0.05 }
    }).bindTooltip(`${p.code} ${p.name}（v${cur.version}，RMS ${cur.rms_error_m}m）`).addTo(layers.plots);
    p.versions.filter(v => v.id !== cur.id).forEach(v =>
      L.geoJSON(gj(v.boundary_geojson), {
        style: { color: '#9e9e9e', weight: 1.5, dashArray: '5 4', fill: false }
      }).bindTooltip(`${p.code} 旧边界 v${v.version}`).addTo(layers.old));
  });
  // flights footprint
  S.flights.forEach(f => {
    if (f.footprint_geojson) L.geoJSON(gj(f.footprint_geojson), { style: { color: '#6a1b9a', weight: 1, dashArray: '2 3', fill: false } }).addTo(layers.flights);
  });
  // evidence: drone point (blue), projection center + error circle + footprint
  S.evidence.forEach(ev => {
    L.circleMarker([ev.drone_lat, ev.drone_lng], { radius: 3, color: '#1565c0', fillColor: '#1565c0', fillOpacity: .9 })
      .bindTooltip('无人机位置 #' + ev.id).addTo(layers.drones);
    if (ev.footprint_geojson)
      L.geoJSON(gj(ev.footprint_geojson), { style: { color: '#00838f', weight: 1, fillOpacity: .04, dashArray: '3 3' } })
        .bindPopup(evPopup(ev)).addTo(layers.foot);
    if (ev.center_lat)
      L.circle([ev.center_lat, ev.center_lng], { radius: ev.radius_m, color: 'rgba(239,108,0,.5)', dashArray: '4 4', weight: 1, fill: false }).addTo(layers.err);
  });
  // findings
  S.findings.forEach(f => {
    if (!f.location_lat) return;
    const isMerged = f.status === 'merged';
    const m = L.circleMarker([f.location_lat, f.location_lng], {
      radius: 9, color: '#fff', weight: 2,
      fillColor: f.status === 'reviewed' ? '#2e7d32' : (f.status === 'pending_evidence' ? '#ef6c00' : '#ef6c00'),
      fillOpacity: isMerged ? .3 : .95
    }).bindTooltip(`${f.code} ${f.title}（±${f.locate_error_m}m）`);
    m.on('click', () => selectFinding(f.id, true));
    m.addTo(layers.findings);
  });
  fitAll();
}
function evPopup(ev) {
  const m = S.media.find(x => x.id === ev.media_id);
  return `<b>证据 #${ev.id}</b><br/>镜头帧坐标 (${ev.frame_cx?.toFixed(2)},${ev.frame_cy?.toFixed(2)})<br/>
  投影法：${esc(ev.geo_method)}<br/>水平误差 ≈ ${ev.horizontal_error_m} m<br/>
  无人机(${ev.drone_lng.toFixed(5)},${ev.drone_lat.toFixed(5)}) → 目标(${ev.center_lng?.toFixed(6)},${ev.center_lat?.toFixed(6)})<br/>
  ${ev.frame_time_ms != null ? '帧时间码 ' + tc(ev.frame_time_ms) + '<br/>' : ''}媒体：${esc(m?.filename || '')}`;
}
function fitAll() {
  const pts = [];
  S.findings.forEach(f => f.location_lat && pts.push([f.location_lat, f.location_lng]));
  S.evidence.forEach(e => e.center_lat && pts.push([e.center_lat, e.center_lng]));
  S.plots.forEach(p => {
    const v = p.versions.find(x => x.version === p.active_version);
    if (!v) return;
    try { gj(v.boundary_geojson).coordinates[0].forEach(q => pts.push([q[1], q[0]])); } catch (e) {}
  });
  if (pts.length) map.fitBounds(L.latLngBounds(pts).pad(0.05));
}
function flyToFinding(id) {
  const f = S.findings.find(x => x.id === id);
  if (f?.location_lat) map.flyTo([f.location_lat, f.location_lng], 17, { duration: .6 });
}

/* ---------------- timeline ---------------- */
function drawTimeline(events) {
  const lanes = $$('.lane:checked').map(x => x.value);
  const el = $('#timeline'); el.innerHTML = '';
  events.filter(e => lanes.includes(e.lane)).forEach(e => {
    const d = document.createElement('div');
    d.className = 'ev lane-' + e.lane + (e.ref_id && e.lane === 'finding' && e.ref_id === selectedFid ? ' active' : '');
    d.innerHTML = `<span class="sw"></span><div><div>${esc(e.title)}</div>
      <div class="t">${(e.at || '').replace('T', ' ')} ${e.late ? '<span class="late">·转写晚到</span>' : ''}
      ${e.lane === 'upload' ? '·上传' : ''}</div></div>`;
    d.onclick = () => onTimelineClick(e);
    el.appendChild(d);
  });
}
function onTimelineClick(e) {
  if (e.lane === 'finding') selectFinding(e.ref_id, true);
  if (e.lane === 'capture' || e.lane === 'upload' || e.lane === 'transcript') {
    const ev = S.evidence.find(x => x.media_id === e.ref_id);
    if (ev?.center_lat) map.flyTo([ev.center_lat, ev.center_lng], 17);
    const m = S.media.find(x => x.id === e.ref_id);
    if (m) $('#detailCard').innerHTML = mediaDetail(m, e.lane === 'transcript');
  }
  if (e.lane === 'flight' && e.flight_id) {
    const f = S.findings.find(x => x.first_seen_flight_id === e.flight_id);
    if (f) selectFinding(f.id, true);
  }
}

/* ---------------- finding detail ---------------- */
function mediaDetail(m, highlightTranscript) {
  const isAudio = m.kind === 'audio';
  const isVideo = m.kind === 'video';
  const trLate = m.transcript_status === 'done' && m.uploaded_at && m.captured_at;
  return `<h3>媒体 #${m.id} ${esc(m.filename)}</h3>
  <div class="kv">
    <b>类型</b><span>${m.kind}</span>
    <b>拍摄时间</b><span>${(m.captured_at||'').replace('T',' ')}</span>
    <b>上传完成</b><span>${(m.uploaded_at||'未完成').replace('T',' ')}</span>
    <b>状态</b><span>${m.status === 'uploaded' ? '✅ 已上传' : '⚠️ ' + m.status} (sha ${(m.sha256||'').slice(0,10)})</span>
    ${isVideo ? '<b>时长</b><span>'+(m.duration_ms/1000)+' s（片段时间码见证据）</span>' : ''}
  </div>
  ${isAudio ? `<div class="suggestion ${trLate?'audio-late':''}">
      ${trLate ? '🎙️ <b>转写晚到</b>（拍摄/上传之后异步返回）<br/>' : '转写状态：' + (m.transcript_status || 'queued') + '<br/>'}
      ${esc(m.transcript || '（转写尚未返回）')}
      <audio controls src="/api/media/${m.id}/raw" style="width:100%;margin-top:6px"></audio></div>` : ''}
  ${isVideo ? `<video controls src="/api/media/${m.id}/raw" style="width:100%;border-radius:6px"></video>` : ''}
  ${(!isAudio && !isVideo) ? `<img src="/api/media/${m.id}/raw" style="max-width:100%;border-radius:6px" onerror="this.style.opacity=.3"/>` : ''}
  ${highlightTranscript ? '<p class="hint">说明：语音转写是异步晚到的数据，时间线单独成轨，不回写拍摄时间。</p>' : ''}`;
}
function selectFinding(id, pan) {
  selectedFid = id;
  $$('.finding-list .fi').forEach(el => el.classList.toggle('sel', +el.dataset.id === id));
  if (pan) flyToFinding(id);
  api('GET', `/api/findings/${id}`).then(renderFinding).catch(e => toast(e.message, 1));
  api('GET', '/api/timeline').then(d => drawTimeline(d.events));
}
function renderFinding(f) {
  const status = f.status;
  const plot = f.plot ? `${f.plot.code} ${f.plot.name}（边界v${f.plot.version}）` : '<span style="color:#c62828">不在任何地块内</span>';
  let html = `<h3>${f.code} ${esc(f.title)} <span class="pill ${status}">${
    { draft: '草稿', reviewed: '已审核', pending_evidence: '待补证据', merged: '已合并' }[status] || status}</span></h3>
  <div class="kv">
    <b>类型/严重度</b><span>${esc(f.ftype||'')} / ${esc(f.severity||'')}</span>
    <b>归属地块</b><span>${plot}</span>
    <b>定位依据</b><span>📐 ${esc(f.locate_basis)}</span>
    <b>定位误差</b><span>±${f.locate_error_m} m（必填，非“点哪算哪”）</span>
    <b>坐标</b><span>${f.location_lng?.toFixed(6)}, ${f.location_lat?.toFixed(6)}</span>
    <b>首次发现</b><span>${(f.first_seen_at||'').replace('T',' ')}</span>
  </div>
  <details open><summary><b>多次巡检证据（${f.inspections.length} 个轮次，不按图像相似合并）</b></summary>`;
  f.inspections.forEach((ins, i) => {
    html += `<div class="insp"><b>轮次 ${i+1}：飞行 ${ins.flight?.code}</b> · ${(ins.inspected_at||'').replace('T',' ')}
      ${ins.note ? '<br/><span class="hint">'+esc(ins.note)+'</span>' : ''}<br/>`;
    ins.evidence.forEach(ev => {
      const m = ev.media;
      const frame = ev.frame_cx != null ? `帧(${ev.frame_cx.toFixed(2)},${ev.frame_cy.toFixed(2)})` : '';
      html += `<span class="evchip ${m.kind==='audio'&&m.transcript_status==='done'?'audio-late':''}">
        ${m.kind === 'image' ? `<img class="ev-thumb" src="/api/media/${m.id}/raw" onclick="$('#detailCard').innerHTML='';document.dispatchEvent(new CustomEvent('show-media',{detail:${m.id}}))"/>` : (m.kind==='audio'?'🎙️':'🎬')}
        #${ev.id} ${frame} ±${ev.horizontal_error_m?.toFixed(1)}m${ev.clip_id?' <b>TC '+esc(ev.clip.start_tc)+'→'+esc(ev.clip.end_tc)+'（源时间码）</b>':''}
        ${m.status!=='uploaded'?' ⚠️缺失':''}
        <button class="secondary" style="padding:1px 6px" onclick="event.stopPropagation();previewEvidence(${ev.id},${m.id})">看</button>
      </span>`;
    });
    html += `</div>`;
  });
  html += `</details>
  <div id="addInspBox"></div>
  <details><summary><b>归属历史（边界修订）</b></summary>${
    f.plot_history.map(h => `<div class="hint">· ${h.changed_at.replace('T',' ')} ${esc(h.reason)} → ${h.plot_id ? '地块 '+h.plot_id : '无地块'}</div>`).join('')}</details>
  <h4 style="margin:8px 0 4px">用户填写的建议（系统不自动给农事处置）</h4>
  ${f.suggestions.map(s => `<div class="suggestion">👤 ${esc(s.author)}：${esc(s.content)}</div>`).join('') || '<div class="hint">暂无</div>'}
  <textarea id="sugInput" placeholder="填写人工建议（如：人工查苗后再决定补播）"></textarea>
  <div class="cap-row">
    <button class="secondary" onclick="addSuggestion(${f.id})">保存建议</button>
    ${status !== 'reviewed' ? `<button onclick="reviewFinding(${f.id})">标记为已审核</button>` : ''}
    <button class="secondary" onclick="openInspForm(${f.id})">＋追加巡检轮次/证据</button>
  </div>`;
  $('#detailCard').innerHTML = html;
}
document.addEventListener('show-media', e => {
  const m = S.media.find(x => x.id === e.detail);
  if (m) $('#detailCard').innerHTML = mediaDetail(m, false);
});
function previewEvidence(evId, mid) {
  const ev = S.evidence.find(x => x.id === evId), m = S.media.find(x => x.id === mid);
  if (ev?.center_lat) map.flyTo([ev.center_lat, ev.center_lng], 18);
  $('#detailCard').innerHTML = mediaDetail(m, false) +
    `<div class="kv"><b>投影法</b><span>${esc(ev.geo_method)}</span><b>误差</b><span>±${ev.horizontal_error_m}m</b></span></div>`;
}
window.previewEvidence = previewEvidence;
window.addSuggestion = async fid => {
  const c = $('#sugInput').value.trim(); if (!c) return toast('建议不能为空');
  await api('POST', `/api/findings/${fid}/suggestions`, { content: c });
  toast('建议已保存（仅人工内容）'); reload();
};
window.reviewFinding = async fid => { await api('POST', `/api/findings/${fid}/review`, {}); toast('已审核，可进入预览包'); reload(); };
window.openInspForm = fid => {
  const flights = S.flights.map(f => `<option value="${f.id}">${f.code} ${f.flight_date}</option>`).join('');
  const evs = S.evidence.map(e => `<option value="${e.id}">证据#${e.id} (media ${e.media_id})</option>`).join('');
  $('#addInspBox').innerHTML = `<div class="insp"><b>追加巡检轮次</b>
    <select id="inspFlight">${flights}</select><input type="datetime-local" id="inspAt"/>
    <select id="inspEv" multiple size="4">${evs}</select>
    <button onclick="submitInsp(${fid})">提交新轮次</button></div>`;
};
window.submitInsp = async fid => {
  const ev = [...$('#inspEv').selectedOptions].map(o => +o.value);
  const at = $('#inspAt').value.replace('T', ' ').substring(0, 16) + ':00';
  await api('POST', `/api/findings/${fid}/inspections`, { flight_id: +$('#inspFlight').value, inspected_at: at, evidence_ids: ev });
  toast('已作为新一轮巡检证据追加（未与旧轮次合并）'); reload();
};

/* ---------------- finding list ---------------- */
function drawFindings() {
  const el = $('#findingList'); el.innerHTML = '';
  S.findings.forEach(f => {
    const d = document.createElement('div');
    d.className = 'fi' + (f.id === selectedFid ? ' sel' : '') + (f.status === 'merged' ? ' merged' : '');
    d.dataset.id = f.id;
    const n = f.inspections.length;
    d.innerHTML = `<div><span class="pill ${f.status}">${
      { draft: '草稿', reviewed: '已审核', pending_evidence: '待补', merged: '已合并' }[f.status] || f.status}</span>
      <b>${f.code}</b> ${esc(f.title)}</div>
      <div class="meta">${esc(f.ftype||'')} · ±${f.locate_error_m}m · ${n} 轮巡检 · 证据 ${
        f.inspections.reduce((a, i) => a + i.evidence.length, 0)}</div>`;
    d.onclick = () => selectFinding(f.id, true);
    el.appendChild(d);
  });
}

/* ---------------- conflicts view ---------------- */
function drawConflicts() {
  const open = S.conflicts.filter(c => c.status === 'open');
  const badge = $('#confBadge'); badge.textContent = open.length; badge.classList.toggle('hidden', !open.length);
  const el = $('#conflictList');
  if (!el) return;
  const all = [...S.conflicts].reverse();
  el.innerHTML = all.length ? '' : '<p class="hint">暂无冲突记录。</p>';
  all.forEach(c => {
    const a = S.findings.find(x => x.id === c.finding_a) || c.finding_a_obj;
    const b = S.findings.find(x => x.id === c.finding_b) || c.finding_b_obj;
    const card = document.createElement('div');
    card.className = 'row conflict-card';
    card.innerHTML = `<b>#${c.id} 冲突</b> · 状态：${
      { open: '待处理', merged: '已合并', kept_separate: '保留为不同事件' }[c.status]}
      <p>${esc(c.reason)} · 相距 ${c.distance_m}m ${c.image_similarity != null ? '· 图像相似度提示 '+c.image_similarity : ''}
      <br/><span class="hint">⚠️ 相似度只提示，不代表同一次事件；请人工判断。</span></p>
      <table><tr><th></th><th>A</th><th>B</th></tr>
      <tr><td>编号</td><td>${a.code}</td><td>${b.code}</td></tr>
      <tr><td>标题</td><td>${esc(a.title)}</td><td>${esc(b.title)}</td></tr>
      <tr><td>定位依据</td><td>${esc(a.locate_basis)}</td><td>${esc(b.locate_basis)}</td></tr>
      <tr><td>误差</td><td>±${a.locate_error_m}m</td><td>±${b.locate_error_m}m</td></tr>
      <tr><td>巡检轮次</td><td>${(a.inspections||[]).length}</td><td>${(b.inspections||[]).length}</td></tr></table>
      ${c.status === 'open' ? `<div class="cap-row">
        <button onclick="resolveC(${c.id},${a.id},'merge')">合并：保留 A(${a.code})</button>
        <button class="secondary" onclick="resolveC(${c.id},${b.id},'merge')">合并：保留 B(${b.code})</button>
        <button class="danger" onclick="resolveC(${c.id},0,'keep_separate')">保留为两个事件</button></div>
        <input id="cnote${c.id}" placeholder="处理说明（必填建议）"/>` :
      `<div class="hint">处理人 #${c.resolved_by||''}：${esc(c.resolution_note||'')}${c.survivor_id?' · 保留方 F#'+c.survivor_id:''}</div>`}`;
    el.appendChild(card);
  });
}
window.resolveC = async (cid, survivor, action) => {
  const note = $('#cnote' + cid)?.value || '';
  if (action === 'merge' && !confirm('合并后被合并方的各轮巡检证据会迁移到保留方，确认？')) return;
  await api('POST', `/api/conflicts/${cid}/resolve`, { action, survivor_id: survivor, resolution_note: note });
  toast(action === 'merge' ? '已合并（证据已迁移）' : '已保留为不同事件'); reload();
};

/* ---------------- pending view ---------------- */
function drawPending() {
  const open = S.pending.filter(p => p.status === 'placeholder');
  $('#pendBadge').classList.toggle('hidden', !open.length); $('#pendBadge').textContent = open.length;
  $('#pendFinding').innerHTML = S.findings.filter(f => f.status !== 'merged')
    .map(f => `<option value="${f.id}">${f.code} ${f.title}</option>`).join('');
  if (!$('#revPlot')) return;
  $('#revPlot').innerHTML = S.plots.map(p => `<option value="${p.id}">${p.code} ${p.name}（当前v${p.active_version}）</option>`).join('');
  const el = $('#pendingList');
  el.innerHTML = S.pending.map(p => `<div class="off-item"><span>
    <b>${p.client_uid}</b> [${p.kind}] ${esc(p.filename||'')}<br/>
    <span class="hint">设备 ${p.owner_device||''} · 状态：${
      { placeholder: '⏳ 占位待补', uploaded: '✅ 已补传', superseded: '↪️ 被新附件替代' }[p.status]}</span>
    ${p.note ? '<br/>'+esc(p.note) : ''}</span>
    ${p.status === 'placeholder' ? `<button class="secondary" onclick="fillPendUpload(${p.id})">在本机补传</button>` : `<span class="hint">media#${p.media_id}</span>`}
    </div>`).join('') || '<p class="hint">无占位附件</p>';
}
window.fillPendUpload = (pid) => { MobileCapture.uploadPending(pid); };
window.pendCreate = null;
$('#pendCreateBtn')?.addEventListener('click', async () => {
  const uid = $('#pendUid').value.trim() || 'pend-' + Date.now();
  await api('POST', '/api/pending', { client_uid: uid, owner_device: 'desk-web', kind: $('#pendKind').value,
    filename: $('#pendFile').value, finding_id: +$('#pendFinding').value, note: '先建发现后补素材' });
  toast('已创建占位附件（待补证据状态）'); reload();
});
$('#revDemoBtn')?.addEventListener('click', async () => {
  const pid = +$('#revPlot').value, p = S.plots.find(x => x.id === pid);
  const cur = p.versions.find(v => v.version === p.active_version);
  const g = JSON.parse(cur.boundary_geojson);
  g.coordinates[0] = g.coordinates[0].map(([x, y]) => [x, y + 0.0011]); // 北移约120m
  const r = await api('POST', `/api/plots/${pid}/revisions`, { boundary_geojson: g, area_mu: cur.area_mu,
    source: '田埂实测', rms_error_m: 0.2, reason: '演示：整体北移120m重判归属' });
  toast(r.rejudged.length ? `重判完成：${r.rejudged.length} 项发现跨地块变化` : '已生成新版本；无发现跨地块变化（版本引用已刷新）');
  reload();
});

/* ---------------- reports view ---------------- */
function drawReports() {
  const el = $('#reportList'); if (!el) return;
  el.innerHTML = S.reports.map(r => `<div class="row">
    <b>${r.code} v${r.version}</b> ${esc(r.title)} ·
    <span class="pill ${r.status==='dispatched'?'reviewed':'draft'}">${r.status}</span>
    <span class="hint">生成于 ${(r.generated_at||'').replace('T',' ')}</span>
    <div style="margin-top:6px"><button class="secondary" onclick="previewReport(${r.id})">查看预览包</button>
    <button onclick="dispatchReport(${r.id})">派出复核</button>
    <button class="secondary" onclick="doExport(${r.id})">导出 ZIP（校验素材缺失）</button></div>
    <div id="rp${r.id}"></div></div>`).join('') || '<p class="hint">暂无报告</p>';
  $('#reviewList').innerHTML = S.reviews.map(t => `<div class="row"><b>${t.code}</b> 发现 F#${t.finding_id}
    · 指派人#${t.assigned_to} · <span class="pill ${t.status==='pending'?'pending_evidence':'reviewed'}">${t.status}</span>
    <div class="hint">永久引用：${t.report_code_snap} v${t.report_version_snap}（报告再版/边界修订都不改写此引用）· 截止 ${t.due_at||''}</div>
    ${esc(t.note||'')}
    ${t.status==='pending'?`<button class="secondary" onclick="completeReview(${t.id})">完成复核</button>`:''}</div>`).join('') || '<p class="hint">无</p>';
  $('#exportList').innerHTML = S.exports.map(x => `<div class="row">导出#${x.id} 报告#${x.report_id} ·
    <span class="pill ${x.status==='done'?'reviewed':'pending_evidence'}">${x.status==='done'?'完整':(x.status==='missing'?'含缺失':x.status)}</span>
    · ${x.file_count} 个媒体文件
    ${x.missing_json ? `<div class="miss">⚠️ 素材缺失 ${JSON.parse(x.missing_json).length} 项：${
      JSON.parse(x.missing_json).map(m => 'media#'+m.media_id+'('+m.reason+')').join('；')}</div>` : ''}
    <div class="hint">${x.package_path||''}</div></div>`).join('') || '<p class="hint">无</p>';
}
window.previewReport = async rid => {
  const r = await api('GET', `/api/reports/${rid}`);
  const box = $('#rp' + rid);
  box.innerHTML = `<h4>预览包内容（仅 reviewed + 用户建议）</h4><p class="hint">${esc(r.package.disclaimer)}</p>` +
    `<table><tr><th>编号</th><th>发现</th><th>归属</th><th>定位依据/误差</th><th>用户建议</th></tr>` +
    r.package.items.map(i => `<tr><td>${i.code}</td><td>${esc(i.title)}</td><td>地块#${i.plot_id||'—'}</td>
      <td>${esc(i.locate_basis)}<br/>±${i.locate_error_m}m</td>
      <td>${i.suggestions_user.map(esc).join('<br/>')||'<span class=hint>（无）</span>'}</td></tr>`).join('') + '</table>';
};
window.dispatchReport = async rid => {
  const ids = S.findings.filter(f => f.status === 'reviewed').map(f => f.id);
  if (!ids.length) return toast('没有已审核发现可派出');
  await api('POST', `/api/reports/${rid}/dispatch`, { finding_ids: ids, assigned_to: 2, due_at: '2026-10-10', note: '请现场复核' });
  toast('复核已派出（任务保留本报告快照引用）'); reload();
};
window.doExport = async rid => {
  const r = await api('POST', `/api/reports/${rid}/export`, {});
  toast(r.missing.length ? `导出完成但缺 ${r.missing.length} 个素材（已列入清单）` : '导出完整 ZIP'); reload();
};
window.completeReview = async tid => { await api('PATCH', `/api/reviews/${tid}`, { status: 'done' }); toast('复核完成'); reload(); };
$('#genReportBtn')?.addEventListener('click', async () => {
  const r = await api('POST', '/api/reports/generate', { title: $('#repTitle').value });
  toast(`预览包已生成，含 ${r.package.items.length} 条已审核发现（无自动处置）`); reload();
});

/* ---------------- shell ---------------- */
async function reload() {
  S = await api('GET', '/api/state');
  S.timeline = await (await fetch('/api/timeline')).json();
  drawMap(); drawFindings(); drawTimeline(S.timeline.events);
  drawConflicts(); drawPending(); drawReports();
  if (selectedFid) selectFinding(selectedFid, false);
}
function bindTabs() {
  $$('.tab').forEach(t => t.onclick = () => {
    $$('.tab').forEach(x => x.classList.remove('active')); t.classList.add('active');
    $$('.view').forEach(v => v.classList.add('hidden'));
    $('#view-' + t.dataset.view).classList.remove('hidden');
    if (t.dataset.view === 'workbench') setTimeout(() => map.invalidateSize(), 60);
    if (t.dataset.view === 'capture') setTimeout(() => MobileCapture.init(S), 60);
  });
}
async function initUsers() {
  const users = await (await fetch('/api/users')).json();
  $('#userSelect').innerHTML = users.map(u => `<option value="${u.id}">${u.name}（${u.role}）</option>`).join('');
}
initUsers(); initMap(); bindTabs(); reload().catch(e => toast(e.message, 1));
setInterval(async () => {
  // 语音转写晚到 / 他人标注 / 上传完成 自动刷新
  if (!document.hidden && $('#view-workbench') && !$('#view-workbench').classList.contains('hidden')) {
    try { const prev = JSON.stringify(S?.media?.map(m => [m.transcript_status, m.status]));
      S = await api('GET', '/api/state'); S.timeline = await (await fetch('/api/timeline')).json();
      const cur = JSON.stringify(S.media.map(m => [m.transcript_status, m.status]));
      drawMap(); drawFindings(); drawTimeline(S.timeline.events); drawConflicts(); drawPending(); drawReports();
    } catch (e) {}
  }
}, 8000);
