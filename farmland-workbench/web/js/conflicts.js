async function load() {
  const [conflicts, findings, assets, reports] = await Promise.all([
    API.get("/api/conflicts"), API.get("/api/findings"),
    API.get("/api/assets"), API.get("/api/reports")
  ]);
  renderConflicts(conflicts.conflicts);
  renderMerge(findings.findings);
  renderPending(findings.findings, assets.assets);
  renderTx(assets.assets);
  window._reports = reports.reports;
}

function renderConflicts(list) {
  const tb = document.querySelector("#conflict-table tbody");
  tb.innerHTML = list.map(c => {
    const a = c.ref_a_detail, b = c.ref_b_detail;
    return `<tr>
      <td>${badge(c.status)}</td>
      <td>${esc(a?.author)}: ${esc(a?.label || "")}
        <div class="muted">${esc(METHOD_LABEL[a?.geo_method] || a?.geo_method)} ±${a?.geo_error_m}m</div></td>
      <td>${esc(b?.author)}: ${esc(b?.label || "")}
        <div class="muted">${esc(METHOD_LABEL[b?.geo_method] || b?.geo_method)} ±${b?.geo_error_m}m</div></td>
      <td>${c.distance_m} m</td>
      <td>${c.status === "open" ? `
        <button onclick="resolve('${c.id}','keep_both')">保留双标</button>
        <button class="ghost" onclick="resolve('${c.id}','merged')">人工合并</button>
        <button class="ghost" onclick="resolve('${c.id}','dismissed')">忽略</button>`
        : esc(c.resolution_note || "")}</td>
    </tr>`;
  }).join("");
}

async function resolve(id, resolution) {
  const note = prompt("处理说明", resolution === "keep_both" ? "保留各自标注, 地面复核" : "");
  if (note === null) return;
  await API.post(`/api/conflicts/${id}/resolve`,
    { resolution, note }, { "X-Actor": "web主管" });
  toast("冲突已处理", "ok"); load();
}

let findingMap = {};
function renderMerge(findings) {
  findingMap = Object.fromEntries(findings.map(f => [f.id, f]));
  const opts = findings.map(f => `<option value="${f.id}">${esc(f.title)} (${f.status})</option>`).join("");
  $("m-src").innerHTML = opts; $("m-tgt").innerHTML = opts;
  renderMergeTable();
}

async function renderMergeTable() {
  const d = await API.get("/api/merge-proposals");
  const list = d.proposals || [];
  document.querySelector("#merge-table tbody").innerHTML = list.map(m => `
    <tr><td>${esc(findingMap[m.source_finding_id]?.title || m.source_finding_id)}
      → ${esc(findingMap[m.target_finding_id]?.title || m.target_finding_id)}</td>
    <td>${esc(m.reason || "")}</td><td>${esc(m.created_by)}</td>
    <td><span class="badge ${m.status === "accepted" ? "reviewed" :
      m.status === "rejected" ? "disputed" : "pending_review"}">${esc(m.status)}</span></td>
    <td>${m.status === "proposed" ? `
      <button onclick="decide('${m.id}',true)">接受(证据迁移)</button>
      <button class="ghost" onclick="decide('${m.id}',false)">拒绝(保留两个)</button>` : ""}</td>
    </tr>`).join("") || `<tr><td colspan=5 class=muted>暂无合并建议</td></tr>`;
}

async function decide(id, accept) {
  await API.post(`/api/merge-proposals/${id}/decide`,
    { accept }, { "X-Actor": "web主管" });
  toast(accept ? "已合并(源发现保留审计痕迹)" : "已拒绝, 两个发现保留", "ok");
  load();
}

$("btn-propose").onclick = async () => {
  const src = $("m-src").value, tgt = $("m-tgt").value;
  if (src === tgt) return toast("源与目标必须不同", "bad");
  await API.post("/api/merge-proposals", {
    source_finding_id: src, target_finding_id: tgt,
    reason: $("m-reason").value || "人工判断"
  }, { "X-Actor": "web主管" });
  toast("已记录人工合并建议", "ok"); $("m-reason").value = ""; load();
};

function renderPending(findings, assets) {
  const amap = Object.fromEntries(assets.map(a => [a.id, a]));
  const rows = [];
  findings.forEach(f => {
    if (f.status === "needs_evidence") {
      rows.push(`<tr><td>${esc(f.title)}</td><td>${badge(f.status)}</td>
        <td>待补完整素材</td><td>—</td>
        <td class=muted>上传完成后自动进入待审核</td></tr>`);
    }
  });
  assets.forEach(a => {
    if (a.status !== "complete")
      rows.push(`<tr><td class=muted>(素材)</td><td>${badge(a.status)}</td>
        <td>${esc(a.kind)} ${esc(a.filename || a.id)}</td>
        <td>${esc(fmtTime(a.capture_time))} / ${esc(fmtTime(a.upload_time))}</td>
        <td>${a.open_session ? `<span class=muted>有未完成会话, 可续传</span>` :
          `<button class="ghost" onclick="markMissing('${a.id}')">标记缺失</button>`}</td></tr>`);
  });
  document.querySelector("#pending-table tbody").innerHTML =
    rows.join("") || `<tr><td colspan=5 class=muted>全部就绪</td></tr>`;
}

async function markMissing(aid) {
  await API.post(`/api/assets/${aid}/missing`, {});
  toast("已标记素材缺失, 导出将被拦截", ""); load();
}

async function renderTx(assets) {
  const el = $("tx-list");
  const audios = [];
  for (const a of assets.filter(x => x.kind === "audio")) {
    const d = await API.get("/api/assets/" + a.id);
    const t = d.transcriptions?.[0];
    audios.push(`<div class="seg">
      <b>${esc(a.filename || a.id)}</b> ${badge(a.status)}
      <div class="muted">拍摄 ${esc(fmtTime(a.capture_time))} · 上传 ${esc(fmtTime(a.upload_time))}</div>
      <div>转写: ${t ? badge(t.status === "ready" ? "reviewed" :
        t.status === "failed" ? "disputed" : "needs_evidence") + " " + esc(t.text || "等待中…") : "未下单"}</div>
      <div class="row" style="margin-top:6px">
        ${!t || t.status === "pending" ? `
          <button onclick="arriveTx('${a.id}','东北角垄沟有积水，约两米长，建议清沟')">
            模拟转写晚到到达</button>` : ""}
        <a class="badge" href="/media/${a.sha256}">播放</a>
      </div></div>`);
  }
  el.innerHTML = audios.join("") || "<div class=muted>暂无语音</div>";
}

async function arriveTx(aid, text) {
  await API.post(`/api/assets/${aid}/transcription`, { text, provider: "mock-asr" });
  toast("晚到的转写已挂接, 相关发现状态已刷新", "ok"); load();
}

$("btn-check-exports").onclick = async () => {
  const reports = window._reports || [];
  const r = reports[0];
  if (!r) return toast("没有报告", "bad");
  const job = await API.post(`/api/reports/${r.id}/export`, {});
  const el = $("export-result");
  if (job.ok) {
    el.innerHTML = `<div class="seg">✅ 导出成功 <a href="/api/exports/${job.export_job_id}?download=1">下载 zip</a></div>`;
  } else {
    el.innerHTML = `<div class="seg">❌ 导出中止, 缺失 ${job.missing.length} 项:
      <ul class="tight">${job.missing.map(m => `<li>${esc(m.filename || m.asset_id)}
        (发现「${esc(m.finding)}」) — ${esc(m.reason)}</li>`).join("")}</ul></div>`;
  }
};

load().catch(e => toast(e.message, "bad"));
