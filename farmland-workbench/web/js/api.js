// 共享工具: API 调用、时间格式化、状态徽标、Toast、常量
const API = {
  async req(method, path, body, headers = {}) {
    const opt = { method, headers: { ...headers } };
    if (body !== undefined) {
      opt.headers["Content-Type"] = "application/json";
      opt.body = JSON.stringify(body);
    }
    const res = await fetch(path, opt);
    const ct = res.headers.get("content-type") || "";
    const data = ct.includes("application/json") ? await res.json() : await res.arrayBuffer();
    if (!res.ok) throw Object.assign(new Error(data.error || ("HTTP " + res.status)),
      { status: res.status, data });
    return data;
  },
  get(p) { return this.req("GET", p); },
  post(p, b, h) { return this.req("POST", p, b, h); },
  putRaw(p, buf, headers) {
    return this.req("PUT", p, undefined, {})
      .catch(() => {})
      .then(() => fetch(p, { method: "PUT",
        headers: { "Content-Type": "application/octet-stream", ...(headers || {}) },
        body: buf })).then(async r => ({ status: r.status, body: await r.json().catch(() => ({})) }));
  }
};

function toast(msg, kind = "") {
  const t = document.createElement("div");
  t.className = "toast " + kind;
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(() => t.remove(), 3200);
}

function esc(s) {
  return (s ?? "").toString().replace(/[&<>"]/g, c =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
}

const STATUS_LABEL = {
  needs_evidence: "待补证据", pending_review: "待审核", reviewed: "已审核", disputed: "存疑/已合并",
  auto_inside: "自动-界内", auto_buffer: "边界存疑", manual: "人工锁定", none: "界外",
  open: "待处理", resolved_keep_both: "已保留双标", resolved_merged: "已合并",
  resolved_dismissed: "已忽略"
};
const METHOD_LABEL = {
  exif_gps: "设备GNSS(EXIF)", manual_pin: "人工刺点",
  ortho_match: "影像配准", frame_projection: "镜头投影"
};

function badge(status) {
  if (!status) return "";
  return `<span class="badge ${esc(status)}">${esc(STATUS_LABEL[status] || status)}</span>`;
}

function fmtTime(ms) {
  if (!ms) return "—";
  const d = new Date(ms);
  const p = n => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())} ` +
         `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`;
}
function fmtDateOnly(dateStr) { return dateStr || "—"; }

function actorHeader(actor) { return actor ? { "X-Actor": encodeURIComponent(actor) } : {}; }

// 经纬度 -> 米制平面 (等距圆柱, 农田尺度), 地图组件内部使用相对原点
const MapProj = {
  R: 6378137,
  meters(lon, lat) {
    const mlat = this.R * Math.PI / 180;
    const mlon = mlat * Math.cos(lat * Math.PI / 180);
    return [lon * mlon, lat * mlat];
  }
};
