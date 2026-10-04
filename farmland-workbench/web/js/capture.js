// 移动端录入逻辑: 本地离线队列 + 分块上传(带 resume token, 刷新/换机可续)
// 两种模式: A 先传完再建证据; B 先占位附件+发现, 后补传。
const LS = {
  get(k, d) { try { return JSON.parse(localStorage.getItem(k)) ?? d; } catch { return d; } },
  set(k, v) { localStorage.setItem(k, JSON.stringify(v)); }
};

let deviceId = LS.get("wb_device_id", null);
let currentMedia = null;     // {blob,name,kind,duration,width,height}
let recorder = null, recChunks = [], recording = false;

const $ = id => document.getElementById(id);

function online() { return navigator.onLine && !$("offline").checked; }
function updateNet() {
  const el = $("net-state");
  el.textContent = online() ? "在线 (将走分块上传)" : "离线/模拟断网 (仅入本地队列)";
  el.className = "badge " + (online() ? "reviewed" : "needs_evidence");
}
addEventListener("online", updateNet);
addEventListener("offline", updateNet);
$("offline").onchange = updateNet;

// 默认时间
(function initTimes() {
  const d = new Date();
  const p = n => String(n).padStart(2, "0");
  const day = `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  $("flight-date").value = day; $("event-date").value = day;
  $("capture-time").value = `${day}T${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
})();

$("geo-method").onchange = () => { $("cam-box").hidden = $("geo-method").value !== "frame_projection"; };

$("btn-register").onclick = async () => {
  const d = await API.post("/api/devices",
    { name: $("dev-name").value, platform: "mobile-web" });
  deviceId = d.device_id; LS.set("wb_device_id", deviceId);
  $("dev-id").textContent = "设备ID: " + deviceId;
  toast("设备已注册", "ok");
};
if (deviceId) $("dev-id").textContent = "设备ID: " + deviceId + " (本地已存)";

// ---- 采集图片
$("btn-photo").onclick = () => $("file-input").click();
$("file-input").onchange = () => {
  const f = $("file-input").files[0];
  if (!f) return;
  currentMedia = { blob: f, name: f.name, kind: "image",
    size: f.size, type: f.type };
  renderPreview();
};
$("btn-video").onclick = () => $("video-input").click();
$("video-input").onchange = () => {
  const f = $("video-input").files[0];
  if (!f) return;
  const url = URL.createObjectURL(f);
  const v = document.createElement("video");
  v.preload = "metadata";
  v.onloadedmetadata = () => {
    currentMedia = { blob: f, name: f.name, kind: "video", size: f.size,
      type: f.type, duration: Math.round(v.duration * 1000),
      width: v.videoWidth, height: v.videoHeight };
    URL.revokeObjectURL(url); renderPreview();
  };
  v.src = url;
};

// ---- 录音 (MediaRecorder, 不可用时退化为占位)
$("btn-mic").onclick = async () => {
  if (!navigator.mediaDevices?.MediaRecorder && !window.MediaRecorder) {
    currentMedia = { blob: new Blob(["simulated-voice"]), name: "voice.m4a",
      kind: "audio", size: 15, simulated: true };
    renderPreview(); toast("环境不支持麦克风, 使用模拟语音", "");
    return;
  }
  if (!recording) {
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      recorder = new MediaRecorder(stream);
      recChunks = [];
      recorder.ondataavailable = e => e.data.size && recChunks.push(e.data);
      recorder.onstop = () => {
        const blob = new Blob(recChunks, { type: recorder.mimeType || "audio/webm" });
        currentMedia = { blob, name: "voice-" + Date.now() + ".webm",
          kind: "audio", size: blob.size };
        stream.getTracks().forEach(t => t.stop());
        renderPreview();
      };
      recorder.start(); recording = true;
      $("rec-state").textContent = "🔴 录音中… 再点结束";
    } catch (e) {
      currentMedia = { blob: new Blob(["simulated-voice"]), name: "voice.m4a",
        kind: "audio", size: 15, simulated: true };
      renderPreview(); toast("无麦克风权限, 使用模拟语音", "");
    }
  } else {
    recorder.stop(); recording = false;
    $("rec-state").textContent = "";
  }
};

function renderPreview() {
  const m = currentMedia;
  const el = $("media-preview");
  if (!m) { el.className = "muted"; el.textContent = "尚未采集素材。"; return; }
  el.className = "";
  el.innerHTML = `<div class="seg">已选 <b>${esc(m.kind)}</b> ${esc(m.name)} · ${m.size}B
    ${m.simulated ? "(模拟)" : ""}
    ${m.kind === "image" && !m.simulated ? `<br><img class="media" src="${URL.createObjectURL(m.blob)}">` : ""}
    ${m.kind === "audio" ? `<audio controls src="${URL.createObjectURL(m.blob)}"></audio>` : ""}
    ${m.kind === "video" ? `<video class="media" controls src="${URL.createObjectURL(m.blob)}"></video>` : ""}
  </div>`;
}

// ---- 提交
$("btn-submit").onclick = () => submit(false);
$("btn-queue-placeholder").onclick = () => submit(true);

async function submit(forceOffline) {
  const mode = document.querySelector('input[name=mode]:checked').value;
  const offlineMode = forceOffline || !online();
  const captureMs = new Date($("capture-time").value).getTime();
  if (!captureMs) return toast("拍摄时间必填", "bad");
  if (!deviceId) return toast("请先注册设备", "bad");

  const findingPayload = buildFindingPayload();
  const job = {
    id: "q_" + Date.now(),
    mode, deviceId,
    flightDate: $("flight-date").value,
    eventDate: $("event-date").value,
    captureMs,
    media: currentMedia ? { name: currentMedia.name, kind: currentMedia.kind,
      type: currentMedia.type, size: currentMedia.size,
      duration: currentMedia.duration, width: currentMedia.width,
      height: currentMedia.height } : null,
    finding: findingPayload,
    progress: 0, phase: offlineMode ? "waiting_offline" : "ready",
    assetId: null, resumeToken: null, shareCode: null,
    mediaBase64: currentMedia ? await blobToBase64(currentMedia.blob) : null
  };

  if (offlineMode) {
    enqueue(job);
    if (mode === "placeholder") await saveServerDraft(job);
    toast("已离线保存到本地队列" + (mode === "placeholder" ? "并尝试登记占位草稿" : ""), "");
    return;
  }
  enqueue(job);
  processQueue();
}

function buildFindingPayload() {
  const p = {
    title: $("f-title").value || "未命名发现",
    category: $("f-cat").value, severity: $("f-sev").value,
    geo_note: $("geo-note").value
  };
  const method = $("geo-method").value;
  if (!method) {
    // 允许无地理坐标: 仅画面像素证据 (服务端会校验)
    return p;
  }
  p.geo_method = method;
  p.geo_error_m = parseFloat($("geo-error").value);
  if (method === "frame_projection") {
    p.camera = {
      drone_lon: parseFloat($("c-dlon").value), drone_lat: parseFloat($("c-dlat").value),
      drone_alt_m: parseFloat($("c-alt").value), yaw_deg: parseFloat($("c-yaw").value),
      pitch_deg: parseFloat($("c-pitch").value), focal_px: parseFloat($("c-focal").value),
      frame_w: parseInt($("c-fw").value), frame_h: parseInt($("c-fh").value),
      px: parseInt($("c-px").value), py: parseInt($("c-py").value)
    };
  } else {
    p.lon = parseFloat($("lon").value);
    p.lat = parseFloat($("lat").value);
  }
  return p;
}

// ---- 本地队列
function getQueue() { return LS.get("wb_queue", []); }
function enqueue(job) { const q = getQueue(); q.push(job); LS.set("wb_queue", q); renderQueue(); }
function updateJob(id, patch) {
  const q = getQueue();
  const j = q.find(x => x.id === id);
  Object.assign(j, patch);
  LS.set("wb_queue", q); renderQueue();
}
function removeJob(id) { LS.set("wb_queue", getQueue().filter(x => x.id !== id)); renderQueue(); }

function renderQueue() {
  const q = getQueue();
  const el = $("queue");
  if (!q.length) { el.className = "muted"; el.textContent = "队列为空。"; return; }
  el.className = "";
  el.innerHTML = q.map(j => `
    <div class="queue-item">
      <progress value="${j.progress}" max="100" style="max-width:120px"></progress>
      <span>${esc(j.finding.title)}</span>
      <span class="muted">${esc(j.mode)}</span>
      <span class="badge ${j.phase === "done" ? "reviewed" :
        j.phase === "error" ? "disputed" : "needs_evidence"}">${phaseLabel(j.phase)}</span>
      <span class="muted">${j.resumeToken ? "token…" + j.resumeToken.slice(0, 6) : ""}</span>
      <span class="muted">${j.shareCode ? "补充码 " + j.shareCode : ""}</span>
    </div>`).join("");
}
function phaseLabel(p) {
  return { ready: "待传", uploading: "上传中", waiting_offline: "离线等待",
    completing: "合并校验", done: "完成", error: "错误" }[p] || p;
}

$("btn-flush").onclick = () => { $("offline").checked = false; updateNet(); processQueue(); };

async function saveServerDraft(job) {
  try {
    const d = await API.post("/api/drafts", {
      device_id: deviceId,
      resume_tokens: job.resumeToken ? [job.resumeToken] : [],
      payload: { title: job.finding.title, flight_date: job.flightDate,
                 capture_ms: job.captureMs, media: job.media }
    });
    updateJob(job.id, { shareCode: d.share_code });
    return d.share_code;
  } catch (e) { /* 离线时忽略, 本地仍保留 */ }
}

// ---- 处理队列 (模式 A / B)
async function processQueue() {
  if (!online()) return toast("当前离线, 已保留在队列, 联网后可续传", "");
  for (const job of getQueue()) {
    if (job.phase === "done") continue;
    try {
      // 1) 确保 flight
      let flightId = job.flightId;
      if (!flightId) {
        const flights = (await API.get("/api/flights")).flights;
        const fl = flights.find(f => f.flight_date === job.flightDate);
        flightId = fl ? fl.id : (await API.post("/api/flights",
          { flight_date: job.flightDate, note: "移动端自动创建" },
          { "X-Device-Id": deviceId })).flight_id;
        updateJob(job.id, { flightId });
      }
      // 2) event (同一发现多次巡检 -> 多次录入对应多个事件)
      let eventId = job.eventId;
      if (!eventId) {
        eventId = (await API.post("/api/events",
          { flight_id: flightId, event_date: job.eventDate, kind: "aerial",
            inspector: $("actor").value },
          { "X-Device-Id": deviceId })).event_id;
        updateJob(job.id, { eventId });
      }

      const mediaBuf = job.mediaBase64
        ? Uint8Array.from(atob(job.mediaBase64), c => c.charCodeAt(0)) : null;
      const wholeSha = mediaBuf ? await sha256hex(mediaBuf) : null;

      if (job.mode === "after") {
        if (!job.assetId) {
          const a = await API.post("/api/assets", {
            kind: job.media.kind, filename: job.media.name,
            flight_id: flightId, device_id: deviceId,
            capture_time: job.captureMs,
            duration_ms: job.media.duration, width: job.media.width, height: job.media.height,
            total_chunks: 1, total_size: mediaBuf.length, whole_sha256: wholeSha
          });
          updateJob(job.id, { assetId: a.asset_id, resumeToken: a.resume_token });
          await uploadOneChunk(a.resume_token, mediaBuf, job.id);
          await API.post(`/api/uploads/${a.resume_token}/complete`, {});
        }
        const f = await API.post("/api/findings",
          { ...job.finding, evidences: [{
            event_id: eventId, asset_id: job.assetId,
            frame_width: job.media.width, frame_height: job.media.height,
            note: "移动端录入" }] },
          { "X-Device-Id": deviceId, "X-Actor": $("actor").value });
        updateJob(job.id, { findingId: f.finding_id, phase: "done", progress: 100 });
      } else {
        // 模式 B: 占位 asset + finding, 然后为占位 asset 开会话续传
        if (!job.assetId) {
          const a = await API.post("/api/assets", {
            kind: job.media?.kind || "image", filename: job.media?.name || "pending",
            flight_id: flightId, device_id: deviceId,
            capture_time: job.captureMs, placeholder: true
          });
          updateJob(job.id, { assetId: a.asset_id });
        }
        if (!job.findingId) {
          const f = await API.post("/api/findings",
            { ...job.finding, evidences: [{
              event_id: eventId, asset_id: job.assetId,
              frame_width: job.media?.width, frame_height: job.media?.height,
              note: "占位附件, 弱网先登记" }] },
            { "X-Device-Id": deviceId, "X-Actor": $("actor").value });
          updateJob(job.id, { findingId: f.finding_id });
          const code = await saveServerDraft(job);
          if (code) updateJob(job.id, { shareCode: code });
        }
        if (mediaBuf) {
          let token = job.resumeToken;
          if (!token) {
            const us = await API.post(`/api/assets/${job.assetId}/upload-session`,
              { total_chunks: 1, total_size: mediaBuf.length, whole_sha256: wholeSha },
              { "X-Device-Id": deviceId });
            token = us.resume_token;
            updateJob(job.id, { resumeToken: token });
          }
          await uploadOneChunk(token, mediaBuf, job.id);
          await API.post(`/api/uploads/${token}/complete`, {});
        }
        updateJob(job.id, { phase: "done", progress: 100 });
      }
      toast(`「${job.finding.title}」录入完成`, "ok");
      setTimeout(() => removeJob(job.id), 4000);
    } catch (e) {
      updateJob(job.id, { phase: "error" });
      toast("上传中断, 已保留 token, 可续传: " + (e.data?.error || e.message), "bad");
    }
  }
}

async function uploadOneChunk(token, buf, jobId) {
  updateJob(jobId, { phase: "uploading", progress: 30 });
  const r = await fetch(`/api/uploads/${token}/chunks/0`, {
    method: "PUT",
    headers: { "Content-Type": "application/octet-stream", "X-Device-Id": deviceId },
    body: buf
  });
  if (!r.ok && r.status !== 409) throw new Error("chunk upload " + r.status);
  updateJob(jobId, { progress: 80, phase: "completing" });
}

// ---- 跨设备补充
$("btn-fetch-draft").onclick = async () => {
  const code = $("share-in").value.trim();
  if (!code) return;
  try {
    const d = await API.get("/api/drafts/" + code);
    $("draft-info").innerHTML = `<div class="seg">
      来自设备 ${esc(d.device_id || "?")}<br>
      <b>${esc(d.payload.title)}</b>
      <div class="muted">拍摄 ${esc(fmtTime(d.payload.capture_ms))} · ${esc(d.payload.media?.name || "")}</div>
      <div class="muted">在本机登录同一现场账号后, 可凭该草稿对应 asset 的上传会话 token 续传。</div>
    </div>`;
    toast("草稿已拉取, 可在本机补充上传", "ok");
  } catch (e) { toast("补充码无效或已同步", "bad"); }
};

// ---- 工具
function blobToBase64(blob) {
  return new Promise(res => {
    const fr = new FileReader();
    fr.onload = () => res(fr.result.split(",")[1]);
    fr.readAsDataURL(blob);
  });
}
async function sha256hex(buf) {
  const h = await crypto.subtle.digest("SHA-256", buf);
  return [...new Uint8Array(h)].map(b => b.toString(16).padStart(2, "0")).join("");
}

updateNet();
renderQueue();
// 回到在线时自动尝试续传
addEventListener("online", () => processQueue());
