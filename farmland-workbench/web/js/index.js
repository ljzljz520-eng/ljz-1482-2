let WB = null, map = null, ANNOTATIONS = [];

async function boot() {
  WB = await API.get("/api/workbench");
  map = new FarmMap(document.getElementById("map"));
  const ann = await API.get("/api/annotations");
  ANNOTATIONS = ann.annotations;
  map.setData({ plots: WB.plots, findings: WB.findings,
                annotations: ANNOTATIONS });
  map.onSelect = onMapSelect;
  renderTimeline();
  renderFlights();
}

function renderTimeline() {
  const el = document.getElementById("timeline");
  const items = [...WB.timeline].sort((a, b) => {
    const ta = a.ts || Date.parse(a.date + "T00:00:00");
    const tb = b.ts || Date.parse(b.date + "T00:00:00");
    return ta - tb;
  });
  el.innerHTML = items.map(it => `
    <div class="ev ${it.track}" data-type="${it.type}" data-id="${esc(it.id)}">
      <div>${esc(it.label)}</div>
      <div class="track-label">${esc(it.track)} · ${it.ts ? esc(fmtTime(it.ts)) : esc(it.date)}
        ${it.status ? " · " + esc(it.status) : ""}</div>
    </div>`).join("");
  el.querySelectorAll(".ev").forEach(node => {
    node.onclick = () => {
      el.querySelectorAll(".ev.active").forEach(n => n.classList.remove("active"));
      node.classList.add("active");
      const type = node.dataset.type, id = node.dataset.id;
      if (type.startsWith("media")) showAsset(id);
    };
  });
}

function renderFlights() {
  document.getElementById("flight-list").innerHTML = WB.flights.map(f => `
    <div class="seg">
      <b>${esc(f.flight_date)}</b> ${esc(f.aircraft || "")}
      <div class="muted">飞行员 ${esc(f.pilot || "—")} · ${esc(f.note || "")}</div>
    </div>`).join("");
}

function onMapSelect(sel) {
  if (sel.type === "finding") showFinding(sel.id);
  else if (sel.type === "plot") showPlot(sel.id);
}

async function showAsset(aid) {
  const a = WB.assets.find(x => x.id === aid) || (await API.get("/api/assets/" + aid));
  const p = document.getElementById("detail-panel");
  p.innerHTML = `<h2>🎬 素材 ${esc(a.filename || a.id)}</h2>
    <div class="grid2 kv">
      <div>类型 <b>${esc(a.kind)}</b></div>
      <div>状态 ${badge(a.status)}</div>
      <div>飞行日期 <b>${esc(flightDate(a.flight_id))}</b></div>
      <div>拍摄时刻 <b>${esc(fmtTime(a.capture_time))}</b></div>
      <div>上传时刻 <b>${esc(fmtTime(a.upload_time))}</b></div>
      <div>大小 <b>${a.size_bytes ?? "—"}</b></div>
    </div>
    <p class="muted">三类时间严格分离: 飞行日期来自作业排班; 拍摄时刻来自设备时钟;
      上传时刻为服务端收到完整对象的时间。</p>
    ${a.sha256 ? mediaBlock(a) : "<p class='muted'>占位/未完成, 暂无对象。</p>"}
    <div id="asset-extra"></div>`;
  const full = await API.get("/api/assets/" + aid);
  renderSegments(full);
  renderTranscription(full);
}

function mediaBlock(a) {
  const url = "/media/" + a.sha256;
  if (a.kind === "image") return `<img class="media" src="${url}">`;
  if (a.kind === "video") return `<video class="media" controls preload="metadata" src="${url}"></video>`;
  if (a.kind === "audio") return `<audio controls src="${url}"></audio>`;
  return `<a href="${url}">${url}</a>`;
}

function renderSegments(a) {
  const el = document.getElementById("asset-extra");
  if (!el) return;
  let html = "";
  if (a.segments && a.segments.length) {
    html += "<h3>✂️ 裁切片段(时间码已重映射)</h3>" + a.segments.map(s => `
      <div class="seg">
        <b>${esc(s.name || "片段")}</b>
        <div class="kv">源时间码 ${s.source_start_ms}–${s.source_end_ms} ms ·
          新时间码 = 源 − ${s.timecode_offset_ms} ms</div>
        <div class="muted">源素材不修改, 证据引用可回溯原始时间码。</div>
      </div>`).join("");
  }
  if (a.transcriptions && a.transcriptions.length) {
    html += "<h3>🎙 语音转写</h3>" + a.transcriptions.map(t => `
      <div class="seg">
        状态: ${badge(t.status === "ready" ? "reviewed" : t.status === "failed" ? "disputed" : "needs_evidence")}
        <div>${esc(t.text || "（转写尚未到达 — 晚到也会自动挂接）")}</div>
        <div class="kv">下单 ${esc(fmtTime(t.ordered_at))} · 到达 ${esc(fmtTime(t.ready_at))}</div>
      </div>`).join("");
  }
  el.innerHTML = html;
}

function renderTranscription() {}

function flightDate(fid) {
  const f = WB.flights.find(x => x.id === fid);
  return f ? f.flight_date : "—";
}

async function showFinding(fid) {
  const f = await API.get("/api/findings/" + fid);
  const eventMap = Object.fromEntries(WB.events.map(e => [e.id, e]));
  const grouped = {};
  f.evidences.forEach(e => {
    const k = e.event_id || "no_event";
    (grouped[k] = grouped[k] || []).push(e);
  });
  const p = document.getElementById("detail-panel");
  p.innerHTML = `
    <h2>${esc(f.title)} ${badge(f.status)}</h2>
    <div class="row" style="margin:6px 0">
      ${badge(f.attribution_status)}
      <span class="badge">${esc(METHOD_LABEL[f.geo_method] || f.geo_method || "无地理坐标")}</span>
      <span class="badge">误差 ±${f.geo_error_m ?? "—"} m</span>
      <span class="badge">地块版本 v${f.plot_version ?? "—"}</span>
    </div>
    <div class="kv">
      <div>经纬度 <b>${f.lon ? f.lon.toFixed(7) + ", " + f.lat.toFixed(7) : "— (仅画面像素)"}</b></div>
      <div>定位依据 <b>${esc(f.geo_note || "—")}</b></div>
    </div>
    <p class="muted">⚠️ evidences 中的 pixel_x/pixel_y 是<b>镜头画面像素</b>,
      只用于画面刺点, 不能直接当作地理坐标。</p>

    <h3>🔁 多次巡检证据 (${f.evidences.length})</h3>
    ${Object.entries(grouped).map(([eid, evs]) => {
      const ev = eventMap[eid];
      return `<div class="seg">
        <b>巡检事件</b> ${ev ? esc(ev.event_date + " " + (ev.kind || "") + " " + (ev.note || "")) : "未关联事件"}
        <ul class="tight">
        ${evs.map(e => `
          <li>${e.asset ? esc(e.asset.kind + " " + (e.asset.filename || "")) : "无素材"}
            ${e.asset ? badge(e.asset.status) : ""}
            ${e.pixel_x != null ? `<span class="muted"> · 画面像素(${e.pixel_x},${e.pixel_y})</span>` : ""}
            <div class="muted">拍摄 ${esc(fmtTime(e.asset?.capture_time))} ·
              上传 ${esc(fmtTime(e.asset?.upload_time))} · ${esc(e.note || "")}</div>
            ${e.asset?.sha256 ? mediaBlock(e.asset) : ""}
          </li>`).join("")}
        </ul></div>`;
    }).join("")}

    <h3>归属变更留痕</h3>
    ${f.attribution_history.length ? `<table><tr><th>原归属</th><th>新归属</th><th>版本</th><th>原因</th></tr>
      ${f.attribution_history.map(h => `<tr>
        <td>${esc(h.old_plot_id || "无")} ${badge(h.old_status)}</td>
        <td>${esc(h.new_plot_id || "无")} ${badge(h.new_status)}</td>
        <td>v${esc(h.old_version)}→v${esc(h.new_version)}</td>
        <td>${esc(h.reason || "")}</td></tr>`).join("")}</table>`
      : "<div class='muted'>尚未因边界修订发生重判。</div>"}

    <h3>操作</h3>
    <div class="row">
      <button onclick="reviewFinding('${f.id}')" ${f.status === "reviewed" ? "disabled" : ""}>标记已审核</button>
      <button class="ghost" onclick="manualPlot('${f.id}')">人工锁定归属</button>
    </div>
    <p class="muted" id="review-msg"></p>`;
}

async function reviewFinding(fid) {
  try {
    await API.post(`/api/findings/${fid}/status`,
      { status: "reviewed", note: "网页审核" }, actorHeader("web主管"));
    toast("已标记审核");
    WB = await API.get("/api/workbench");
    map.setData({ plots: WB.plots, findings: WB.findings, annotations: ANNOTATIONS });
    showFinding(fid);
  } catch (e) {
    toast("无法审核: " + (e.data?.error || e.message), "bad");
    document.getElementById("review-msg").textContent =
      "门禁: " + (e.data?.detail || e.message);
  }
}

async function manualPlot(fid) {
  const pid = prompt("输入要锁定的地块 ID (留空取消)", WB.plots[0]?.id || "");
  if (!pid) return;
  await API.post(`/api/findings/${fid}/plot`, { plot_id: pid }, actorHeader("web主管"));
  toast("已人工锁定归属", "ok");
  showFinding(fid);
}

async function showPlot(pid) {
  const p = await API.get("/api/plots/" + pid);
  const el = document.getElementById("detail-panel");
  el.innerHTML = `<h2>🗺 ${esc(p.name)}</h2>
    <div class="kv">作物 <b>${esc(p.crop || "—")}</b> · 当前版本 <b>v${p.current_version}</b></div>
    <div class="row" style="margin:8px 0">
      <label class="muted"><input type="checkbox" id="oldver"> 叠加显示历史边界版本</label>
    </div>
    <h3>版本历史 (边界只新增版本, 旧版本不可变)</h3>
    ${p.versions.map(v => `<div class="seg">v${v.version} · ${esc(v.source)}
      <div class="muted">${esc(fmtTime(v.created_at))} · ${esc(v.change_note || "")}</div></div>`).join("")}
    <h3>当前归属发现 (${p.findings.length})</h3>
    ${p.findings.map(f => `<div class="row seg clickable" onclick="showFinding('${f.id}')">
      <span>${esc(f.title)}</span>${badge(f.status)}${badge(f.attribution_status)}</div>`).join("")}
    <h3>修订边界</h3>
    <p class="muted">修订将触发该地块相关发现的归属重新判断并留痕;
      已派出的复核事项保留原报告引用。</p>
    <button class="warn" onclick="reviseDemo('${pid}')">模拟: 边界向东扩约 60m</button>`;
  document.getElementById("oldver").onchange = (e) =>
    map.showOldVersions(pid, e.target.checked);
}

async function reviseDemo(pid) {
  const plot = WB.plots.find(x => x.id === pid);
  const ring = plot.geometry.coordinates[0];
  // 仅将东边界外移约60m (西侧不动): 60 / (111320*cos39.9) ≈ 0.0007
  const maxLon = Math.max(...ring.map(c => c[0]));
  const expanded = ring.map(([x, y]) => [Math.abs(x - maxLon) < 1e-9 ? x + 0.0007 : x, y]);
  const note = prompt("修订说明", "东边界外扩约60m — 重新核界");
  if (note === null) return;
  try {
    await API.post(`/api/plots/${pid}/revisions`,
      { geometry: { type: "Polygon", coordinates: [expanded] }, change_note: note },
      actorHeader("web主管"));
    toast("边界已修订, 归属重判完成", "ok");
    WB = await API.get("/api/workbench");
    map.setData({ plots: WB.plots, findings: WB.findings, annotations: ANNOTATIONS });
    showPlot(pid);
  } catch (e) { toast(e.message, "bad"); }
}

boot().catch(e => toast("初始化失败: " + e.message, "bad"));
