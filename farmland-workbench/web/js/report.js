let reports = [], findings = [], current = null;

async function boot() {
  await loadReports();
}
async function loadReports() {
  const d = await API.get("/api/reports");
  reports = d.reports;
  document.getElementById("report-list").innerHTML = reports.map(r => `
    <div class="seg row clickable" onclick="openReport('${r.id}')">
      <b>${esc(r.title)}</b>
      ${badge(r.status === "published" ? "reviewed" : "pending_review")}
      <span class="muted">${esc(r.period_start || "")} ~ ${esc(r.period_end || "")}</span>
    </div>`).join("") || "<div class=muted>暂无报告</div>";
}

async function openReport(rid) {
  current = rid;
  const [fl, fd] = await Promise.all([
    API.get("/api/findings"), fetch(`/api/reports/${rid}/findings`).catch(() => null)
  ]);
  findings = fl.findings;
  // 后端没有单独的报告详情, 用 workbench 不可得; 改为报告关联信息在渲染时从表内推断:
  const included = await fetchIncluded(rid);
  const panel = $("report-panel");
  panel.hidden = false;
  const r = reports.find(x => x.id === rid);
  $("r-title").textContent = r.title;
  renderFindingTable(included);
  $("sg-finding").innerHTML = findings.map(f =>
    `<option value="${f.id}">${esc(f.title)} (${f.status})</option>`).join("");
  $("rt-finding").innerHTML = $("sg-finding").innerHTML;
  loadSuggestions(rid);
  loadTasks();
}

async function fetchIncluded(rid) {
  // 通过预览/导出 manifest 不含未发布信息; 这里加用报告聚合端点
  try {
    const d = await API.get(`/api/reports/${rid}/detail`);
    return new Set(d.finding_ids);
  } catch { return new Set(); }
}

async function renderFindingTable(included) {
  const tb = document.querySelector("#rf-table tbody");
  tb.innerHTML = findings.map(f => `
    <tr>
      <td>${esc(f.title)}</td>
      <td>${badge(f.status)}
        ${f.status !== "reviewed" ? "<div class='muted'>预览包将排除</div>" : ""}</td>
      <td>${badge(f.attribution_status)}<div class="muted">
        ${esc(METHOD_LABEL[f.geo_method] || "—")} ±${f.geo_error_m ?? "—"}m · v${f.plot_version ?? "—"}</div></td>
      <td><input type="checkbox" class="rf-check" value="${f.id}" ${included.has(f.id) ? "checked" : ""}>
        <button class="ghost" onclick="saveIncluded()">保存</button></td>
      <td class="muted" id="sg-count-${f.id}"></td>
    </tr>`).join("");
}

async function saveIncluded() {
  const ids = [...document.querySelectorAll(".rf-check:checked")].map(x => x.value);
  await API.post(`/api/reports/${current}/findings`, { finding_ids: ids },
    { "X-Actor": "web主管" });
  toast("已保存报告发现清单 (整体覆盖式提交)", "ok");
}

$("btn-publish").onclick = async () => {
  await API.post(`/api/reports/${current}/publish`, {}, { "X-Actor": "web主管" });
  toast("报告已发布", "ok"); loadReports();
};

$("btn-preview").onclick = async () => {
  try {
    const d = await API.post(`/api/reports/${current}/preview`, {},
      { "X-Actor": "web主管" });
    $("action-result").innerHTML =
      `✅ 预览包已生成: 纳入 ${d.included} 项已审核发现, 排除 ${d.excluded} 项未审核。
       <a href="${d.download}">下载预览 zip</a>
       <div class="muted">包内仅含用户填写建议, 不构成农事处置决定。</div>`;
  } catch (e) { $("action-result").textContent = "失败: " + e.message; }
};

$("btn-export").onclick = async () => {
  const job = await API.post(`/api/reports/${current}/export`, {},
    { "X-Actor": "web主管" });
  if (job.ok) {
    $("action-result").innerHTML =
      `✅ 导出成功: <a href="/api/exports/${job.export_job_id}?download=1">下载完整 zip</a>`;
  } else {
    $("action-result").innerHTML =
      `❌ 导出中止 — ${job.missing.length} 项素材缺失:
       <ul class="tight">${job.missing.map(m =>
         `<li>${esc(m.filename || m.asset_id)} (「${esc(m.finding)}」) ${esc(m.reason)}</li>`).join("")}
       </ul><span class=muted>补齐素材 (可在移动端凭占位附件续传) 后重新导出。</span>`;
  }
};

$("btn-suggestion").onclick = async () => {
  await API.post(`/api/reports/${current}/suggestions`, {
    finding_id: $("sg-finding").value,
    suggestion: $("sg-text").value,
    decision: $("sg-decision").value || null
  }, { "X-Actor": "农艺师" });
  $("sg-text").value = ""; $("sg-decision").value = "";
  toast("已保存用户建议", "ok"); loadSuggestions(current);
};

async function loadSuggestions(rid) {
  // 建议在预览 manifest 中可见; 增加便捷端点
  let d;
  try { d = await API.get(`/api/reports/${rid}/detail`); }
  catch { d = { suggestions: [] }; }
  const list = d.suggestions || [];
  $("sg-list").innerHTML = list.map(s => `
    <div class="seg"><b>${esc(findings.find(f => f.id === s.finding_id)?.title || s.finding_id)}</b>
      <div>${esc(s.suggestion)}</div>
      <div class="muted">决定: ${esc(s.decision || "未决定")} · ${esc(s.created_by || "")}</div></div>`
  ).join("") || "<div class=muted>暂无用户建议</div>";
}

$("btn-rt").onclick = async () => {
  await API.post(`/api/reports/${current}/review-tasks`, {
    finding_id: $("rt-finding").value,
    assignee: $("rt-assignee").value, note: $("rt-note").value
  }, { "X-Actor": "web主管" });
  toast("复核已派出 (original_report_id 将永久保留)", "ok");
  $("rt-note").value = ""; loadTasks();
};

async function loadTasks() {
  const d = await API.get("/api/review-tasks");
  $("rt-list").innerHTML = d.tasks.map(t => `
    <div class="seg row">
      <span><b>${esc(t.finding_title)}</b> → ${esc(t.assignee || "未指派")}</span>
      ${badge(t.status === "done" ? "reviewed" : "pending_review")}
      <span class="muted">原报告: ${esc(t.original_report_id || "无")}</span>
      <span class="muted">${esc(t.note || "")}</span>
      ${t.status === "assigned" ? `<button class="ghost" onclick="finishTask('${t.id}')">完成</button>` : ""}
    </div>`).join("") || "<div class=muted>暂无复核事项</div>";
}
async function finishTask(id) {
  await API.post(`/api/review-tasks/${id}/complete`, {}, { "X-Actor": "web主管" });
  loadTasks();
}

boot().catch(e => toast(e.message, "bad"));
