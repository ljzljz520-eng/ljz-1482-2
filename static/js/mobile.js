/* 移动端现场录入：图片/语音、断网队列、分块续传、跨设备补传、转写晚到 */
const MobileCapture = (() => {
  const DB_NAME = 'farm_offline', STORE = 'blobs';
  let S = null, inited = false, chosenFile = null, audioBlob = null, loc = null, mediaRecorder = null, chunks = [];

  function idb() {
    return new Promise((res, rej) => {
      const r = indexedDB.open(DB_NAME, 1);
      r.onupgradeneeded = e => e.target.result.createObjectStore(STORE);
      r.onsuccess = () => res(r.result); r.onerror = () => rej(r.error);
    });
  }
  async function putBlob(key, blob) {
    const d = await idb();
    return new Promise((res, rej) => {
      const tx = d.transaction(STORE, 'readwrite'); tx.objectStore(STORE).put(blob, key);
      tx.oncomplete = res; tx.onerror = () => rej(tx.error);
    });
  }
  async function getBlob(key) {
    const d = await idb();
    return new Promise((res, rej) => { const g = d.transaction(STORE).objectStore(STORE).get(key);
      g.onsuccess = () => res(g.result); g.onerror = () => rej(g.error); });
  }
  async function delBlob(key) {
    const d = await idb(); return new Promise(res => {
      const tx = d.transaction(STORE, 'readwrite'); tx.objectStore(STORE).delete(key); tx.oncomplete = res; });
  }
  const queue = () => JSON.parse(localStorage.getItem('offline_queue') || '[]');
  const saveQueue = q => localStorage.setItem('offline_queue', JSON.stringify(q));
  const uid = () => 'cli-' + Date.now() + '-' + Math.random().toString(36).slice(2, 7);

  async function sha256(buf) {
    const h = await crypto.subtle.digest('SHA-256', buf);
    return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, '0')).join('');
  }

  async function init(state) {
    S = state;
    if (!inited) {
      inited = true; bind();
    }
    if (!loc) loc = { lng: 120.0004, lat: 30.0003 };
    $('#capFlight').innerHTML = (S?.flights || []).map(f => `<option value="${f.id}">${f.code} ${f.flight_date}</option>`).join('');
    if (!$('#capCaptured').value) {
      const d = new Date(); d.setMinutes(d.getMinutes() - d.getTimezoneOffset());
      $('#capCaptured').value = d.toISOString().slice(0, 16);
    }
    renderQueue(); initMiniMap();
  }

  function bind() {
    $('#capImage').onchange = e => { chosenFile = e.target.files[0]; toast('已选图片：' + chosenFile.name); };
    $('#capVideo').onchange = e => { chosenFile = e.target.files[0]; toast('已选视频：' + chosenFile.name); };
    $('#capGps').onclick = () => {
      if (!navigator.geolocation) return toast('浏览器不支持定位，使用演示坐标');
      navigator.geolocation.getCurrentPosition(p => {
        loc = { lng: p.coords.longitude, lat: p.coords.latitude };
        $('#capLoc').innerHTML = `定位：${loc.lng.toFixed(6)}, ${loc.lat.toFixed(6)}（精度 ±${p.coords.accuracy.toFixed(0)}m）`;
        if (mini) mini.setView([loc.lat, loc.lng], 17), miniMarker?.setLatLng([loc.lat, loc.lng]);
      }, () => toast('定位失败，使用演示坐标'), { enableHighAccuracy: true });
    };
    // 按住说话（Pointer 事件）
    const btn = $('#capAudioBtn');
    const start = async e => {
      e.preventDefault();
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        mediaRecorder = new MediaRecorder(stream); chunks = [];
        mediaRecorder.ondataavailable = x => chunks.push(x.data);
        mediaRecorder.onstop = () => {
          audioBlob = new Blob(chunks, { type: 'audio/webm' });
          stream.getTracks().forEach(t => t.stop());
          $('#audioPreview').src = URL.createObjectURL(audioBlob);
          $('#audioPreview').classList.remove('hidden');
          btn.textContent = '🎙️ 已录音（重按重录）';
        };
        mediaRecorder.start(); btn.classList.add('rec'); btn.textContent = '● 松开发送';
      } catch (err) { toast('无法录音：' + err.message, 1); }
    };
    const stop = e => { e.preventDefault(); if (mediaRecorder && mediaRecorder.state !== 'inactive') mediaRecorder.stop(); btn.classList.remove('rec'); };
    btn.addEventListener('pointerdown', start); btn.addEventListener('pointerup', stop);
    btn.addEventListener('pointerleave', e => { if (mediaRecorder?.state === 'recording') stop(e); });

    $('#capSubmit').onclick = submit;
    $('#capSync').onclick = syncAll;
  }

  async function submit() {
    let blob = audioBlob || chosenFile;
    const kind = audioBlob ? 'audio' : (chosenFile?.type.startsWith('video') ? 'video' : 'image');
    if (!blob) return toast('请先拍照/选视频或录音');
    const item = {
      key: uid(), kind, filename: chosenFile?.name || ('voice_' + Date.now() + '.webm'),
      title: $('#capTitle').value || '现场发现', ftype: $('#capType').value,
      flight_id: +$('#capFlight').value || S.flights[0]?.id,
      captured_at: $('#capCaptured').value.replace('T', ' ').length === 16 ? $('#capCaptured').value.replace('T', ' ') + ':00' : $('#capCaptured').value,
      locate_basis: $('#capBasis').value, locate_error_m: +$('#capError').value || 15,
      lng: loc.lng, lat: loc.lat,
      workflow: 'placeholder_first', // 显式标记“先建带占位附件的记录”
      status: 'queued'
    };
    await putBlob(item.key, blob);
    const q = queue(); q.push(item); saveQueue(q);
    chosenFile = null; audioBlob = null; $('#capTitle').value = '';
    $('#audioPreview').classList.add('hidden');
    renderQueue();
    if ($('#capOffline').checked) { toast('已存入断网队列（恢复网络后点“同步”）'); return; }
    syncItem(item);
  }

  function renderQueue() {
    const el = $('#offlineList'); if (!el) return;
    const q = queue();
    el.innerHTML = q.map((i, n) => `<div class="off-item"><span>
      ${i.kind === 'audio' ? '🎙️' : i.kind === 'video' ? '🎬' : '📷'} <b>${esc(i.title)}</b><br/>
      <span class="hint">${i.filename} · 拍摄 ${i.captured_at.replace('T', ' ')}<br/>
      工作流：先建占位附件 · ${
        { queued: '⏳ 待上传', uploading: '上传中 ' + (i.pct || 0) + '%', done: '✅ 已同步', error: '❌ ' + (i.error || '') }[i.status]}</span></span>
      <button class="secondary" onclick="MobileCapture.retry(${n})">重试</button>
      <button class="danger" onclick="MobileCapture.remove(${n})">删除</button></div>`).join('') || '<p class="hint">本地队列为空</p>';
  }

  function updateItem(key, patch) {
    const q = queue(); const i = q.find(x => x.key === key);
    if (i) Object.assign(i, patch); saveQueue(q); renderQueue();
  }

  async function syncAll() {
    for (const i of queue()) if (['queued', 'error'].includes(i.status)) await syncItem(i);
  }
  function retry(n) { const i = queue()[n]; if (i) syncItem(i); }
  function remove(n) { const q = queue(); const [it] = q.splice(n, 1); saveQueue(q); delBlob(it.key); renderQueue(); }

  /** 分块上传：先 init 拿已收块（断网/跨设备续传），逐块 PUT，服务端幂等去重；秒传由 init 返回 instant。 */
  async function uploadBlob(item, blob, pendingId) {
    const CHUNK = 256 * 1024;
    const buf = await blob.arrayBuffer();
    const total = buf.byteLength, sha = await sha256(buf);
    item.status = 'uploading'; updateItem(item.key, { status: 'uploading', pct: 0 });
    const init = await api('POST', '/api/uploads', {
      client_uid: item.key, kind: item.kind, filename: item.filename,
      mime: blob.type || 'application/octet-stream', total_size: total, chunk_size: CHUNK,
      sha256: sha, captured_at: item.captured_at, flight_id: item.flight_id, pending_id: pendingId
    });
    let received = new Set(init.received || []);
    if (init.instant) { updateItem(item.key, { status: 'done', media_id: init.media_id, pct: 100 }); return init.media_id; }
    const n = Math.ceil(total / CHUNK);
    for (let i = 0; i < n; i++) {
      if (received.has(i)) continue;
      const part = buf.slice(i * CHUNK, Math.min(total, (i + 1) * CHUNK));
      const r = await fetch(`/api/uploads/${init.session_id}/chunks/${i}`, {
        method: 'POST', headers: { 'Content-Type': 'application/octet-stream',
          'X-Chunk-Sha256': await sha256(part), 'X-User-Id': userId() }, body: part
      }).then(x => x.json());
      received = new Set(r.received);
      updateItem(item.key, { pct: Math.round(received.size / n * 100) });
    }
    updateItem(item.key, { status: 'done', media_id: init.media_id, pct: 100 });
    return init.media_id;
  }

  async function syncItem(item) {
    try {
      const blob = await getBlob(item.key);
      if (!blob) throw new Error('本地 Blob 已丢失');
      // 1) 先建“占位附件 + 发现（带待补证据状态）”，再传素材
      const fd = await api('POST', '/api/findings', {
        title: item.title, ftype: item.ftype, severity: '中', status: 'pending_evidence',
        location_lng: item.lng, location_lat: item.lat,
        locate_basis: item.locate_basis + '（移动端占位，素材后补）', locate_error_m: item.locate_error_m,
        flight_id: item.flight_id, first_seen_at: item.captured_at
      });
      const pend = await api('POST', '/api/pending', {
        client_uid: item.key, owner_device: 'mobile', kind: item.kind, filename: item.filename,
        note: '移动端先发现后补素材', finding_id: fd.id
      });
      const mediaId = await uploadBlob(item, blob, pend.pending_id);
      // 2) 素材传完：建立证据（镜头/位姿未知时仅手机定位，明确大误差），挂到该发现
      const ev = await api('POST', '/api/evidence', {
        media_id: mediaId, frame_cx: .5, frame_cy: .5, fov_h: 70, fov_v: 50,
        drone_lng: item.lng, drone_lat: item.lat, drone_alt: 2, ground_elevation: 0,
        yaw: 0, pitch: -90, dem_source: 'srtm', drone_hrms: item.locate_error_m
      });
      await api('POST', `/api/findings/${fd.id}/inspections`, {
        flight_id: item.flight_id, inspected_at: item.captured_at, evidence_ids: [ev.evidence_id],
        evidence_note: '移动端补传（占位→已上传）'
      });
      toast('已同步：发现 #' + fd.id + '，语音转写将晚到并在时间线提示');
      await delBlob(item.key);
      const q = queue().filter(x => x.key !== item.key); saveQueue(q); renderQueue();
    } catch (e) {
      updateItem(item.key, { status: 'error', error: e.message });
      toast('同步失败（已保留在本地队列）：' + e.message, 1);
    }
  }

  /** 桌面“在本机补传”占位：造一个小文件走同一续传通道（演示跨设备补充） */
  async function uploadPending(pendingId) {
    const blob = new Blob([new Uint8Array(4096).fill(7)], { type: 'image/png' });
    const item = { key: 'pendfill-' + pendingId, kind: 'image', filename: 'filled.png',
      captured_at: new Date().toISOString().slice(0, 19), flight_id: S.flights[0]?.id,
      status: 'queued', pct: 0 };
    const mediaId = await uploadBlob(item, blob, pendingId);
    toast('已在本机补传占位附件 → media#' + mediaId);
    setTimeout(() => location.reload(), 800);
  }

  let mini = null, miniMarker = null;
  function initMiniMap() {
    if (mini || !$('#capMap')) return;
    mini = L.map('capMap').setView([loc.lat, loc.lng], 16);
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', { maxZoom: 20 }).addTo(mini);
    miniMarker = L.circleMarker([loc.lat, loc.lng], { radius: 8, color: '#fff', weight: 2, fillColor: '#ef6c00', fillOpacity: .95 }).addTo(mini);
    mini.on('click', e => {
      loc = { lng: e.latlng.lng, lat: e.latlng.lat }; miniMarker.setLatLng(e.latlng);
      $('#capLoc').innerHTML = `点选坐标：${loc.lng.toFixed(6)}, ${loc.lat.toFixed(6)}（请同时填写误差依据）`;
    });
  }

  return { init, retry, remove, uploadPending, syncAll };
})();
