// 自研轻量 SVG 地图 (无外部底图/CDN 依赖)
// 坐标系: WGS84 经纬度 -> 相对原点的米制平面; 支持拖动平移、滚轮缩放、要素点击。
class FarmMap {
  constructor(container) {
    this.el = container;
    this.k = 1; this.tx = 40; this.ty = 40;   // 屏幕 = 米*k + (tx,ty)
    this.origin = [0, 0];
    this.plots = [];          // {id,name,geometry,version,old?}
    this.findings = [];
    this.annotations = [];
    this.activeId = null;
    this.onSelect = null;
    this._build();
    this._bind();
  }

  _build() {
    this.el.innerHTML = `
      <div class="map-hud">
        <div class="chip" id="map-scale"></div>
        <div class="row">
          <button class="ghost" id="map-zoom-in" type="button">＋</button>
          <button class="ghost" id="map-zoom-out" type="button">－</button>
          <button class="ghost" id="map-fit" type="button">全图</button>
        </div>
      </div>
      <svg class="map-svg" id="farm-svg" preserveAspectRatio="xMidYMid meet">
        <defs>
          <pattern id="grid" width="50" height="50" patternUnits="userSpaceOnUse">
            <path d="M50 0 L0 0 0 50" fill="none" stroke="#1a241d" stroke-width="1"/>
          </pattern>
        </defs>
        <rect id="map-bg" width="100%" height="100%" fill="url(#grid)"/>
        <g id="map-layer"></g>
      </svg>
      <div class="map-legend">
        <div><span class="legend-dot" style="background:#5ec26e"></span>地块边界(当前版本)</div>
        <div><span class="legend-dot" style="background:#9bb3a4"></span>旧版本边界</div>
        <div><span class="legend-dot" style="background:#e2b34c"></span>定位误差圈</div>
        <div><span class="legend-dot" style="background:#6bb3e0"></span>待审核</div>
        <div><span class="legend-dot" style="background:#5ec26e"></span>已审核</div>
        <div><span class="legend-dot" style="background:#e2b34c"></span>待补证据</div>
      </div>`;
    this.svg = this.el.querySelector("#farm-svg");
    this.layer = this.el.querySelector("#map-layer");
  }

  _xy(lon, lat) {
    const [mx, my] = MapProj.meters(lon, lat);
    return [(mx - this.origin[0]) * this.k + this.tx,
            -(my - this.origin[1]) * this.k + this.ty];
  }

  setData({ plots = [], findings = [], annotations = [] }) {
    // 以第一个地块中心为原点
    if (plots.length && plots[0].geometry) {
      const ring = plots[0].geometry.coordinates[0];
      const lons = ring.map(p => p[0]), lats = ring.map(p => p[1]);
      const cLon = (Math.min(...lons) + Math.max(...lons)) / 2;
      const cLat = (Math.min(...lats) + Math.max(...lats)) / 2;
      this.origin = MapProj.meters(cLon, cLat);
    }
    this.plots = plots;
    this.findings = findings;
    this.annotations = annotations;
    this.render();
    this.fit();
  }

  render() {
    const NS = "http://www.w3.org/2000/svg";
    const g = this.layer;
    g.innerHTML = "";
    const mk = (tag, attrs) => {
      const e = document.createElementNS(NS, tag);
      for (const k in attrs) e.setAttribute(k, attrs[k]);
      return e;
    };

    // 地块: 当前版本 + 可选旧版本叠加
    for (const p of this.plots) {
      const polys = p._showOldVersions ? [
        ...p._oldGeoms.map((geom, i) => ({ geom, cls: "plot-poly old", ver: p.versions[i]?.version })),
        { geom: p.geometry, cls: "plot-poly", ver: p.latest_version }
      ] : [{ geom: p.geometry, cls: "plot-poly", ver: p.latest_version }];
      for (const poly of polys) {
        const pts = poly.geom.coordinates[0].map(c => this._xy(c[0], c[1]).join(",")).join(" ");
        const pel = mk("polygon", { points: pts, class: poly.cls });
        pel.addEventListener("click", () => this.onSelect && this.onSelect({ type: "plot", id: p.id }));
        g.appendChild(pel);
      }
    }

    // 发现点 + 误差圈
    for (const f of this.findings) {
      if (f.lon == null) continue;
      const [x, y] = this._xy(f.lon, f.lat);
      if (f.geo_error_m) {
        g.appendChild(mk("circle", { cx: x, cy: y, r: Math.max(3, f.geo_error_m * this.k),
          class: "error-circle" }));
      }
      const color = { reviewed: "#5ec26e", pending_review: "#6bb3e0",
                      needs_evidence: "#e2b34c", disputed: "#e06a5a" }[f.status] || "#9bb3a4";
      const c = mk("circle", { cx: x, cy: y, r: this.activeId === f.id ? 8 : 6,
        fill: color, stroke: "#0c130e", "stroke-width": 1.5, class: "finding-marker" });
      c.addEventListener("click", (ev) => { ev.stopPropagation();
        this.onSelect && this.onSelect({ type: "finding", id: f.id }); });
      g.appendChild(c);
      const t = mk("text", { x: x + 9, y: y + 4, fill: "#cfe0d5", "font-size": 11 });
      t.textContent = f.title;
      g.appendChild(t);
    }

    // 多人标注 (小菱形)
    for (const a of this.annotations) {
      const [x, y] = this._xy(a.lon, a.lat);
      const d = mk("path", { d: `M${x},${y - 6} L${x + 6},${y} L${x},${y + 6} L${x - 6},${y} Z`,
        fill: "#b39ddb", stroke: "#0c130e", "stroke-width": 1, class: "finding-marker" });
      d.addEventListener("click", (ev) => { ev.stopPropagation();
        this.onSelect && this.onSelect({ type: "annotation", id: a.id }); });
      g.appendChild(d);
    }
    this._updateScale();
  }

  _updateScale() {
    const el = document.getElementById("map-scale");
    if (el) el.textContent = "比例尺: " + Math.round(100 / this.k) + " m ≈ 100px";
  }

  fit() {
    const w = this.el.clientWidth, h = this.el.clientHeight;
    const all = [...this.findings.filter(f => f.lon != null)
      .flatMap(f => [this._xy(f.lon, f.lat)])];
    for (const p of this.plots)
      for (const c of p.geometry.coordinates[0]) all.push(this._xy(c[0], c[1]));
    if (!all.length) { this.k = 2; return; }
    const xs = all.map(p => p[0]), ys = all.map(p => p[1]);
    const bw = Math.max(...xs) - Math.min(...xs) || 200;
    const bh = Math.max(...ys) - Math.min(...ys) || 200;
    this.k = Math.min((w - 120) / bw, (h - 120) / bh);
    this.k = Math.max(0.4, Math.min(this.k, 40));
    this.tx = (w - bw * this.k) / 2 - Math.min(...xs) + this.tx - this.tx;
    // 简化: 直接以原点居中
    this.tx = w / 2; this.ty = h / 2;
    this.render();
  }

  _bind() {
    let dragging = false, sx = 0, sy = 0, ox = 0, oy = 0;
    this.el.addEventListener("mousedown", e => {
      dragging = true; sx = e.clientX; sy = e.clientY;
      ox = this.tx; oy = this.ty;
      this.svg.classList.add("dragging");
    });
    window.addEventListener("mousemove", e => {
      if (!dragging) return;
      this.tx = ox + e.clientX - sx; this.ty = oy + e.clientY - sy;
      this.render();
    });
    window.addEventListener("mouseup", () => { dragging = false;
      this.svg.classList.remove("dragging"); });
    this.el.addEventListener("wheel", e => {
      e.preventDefault();
      const f = e.deltaY < 0 ? 1.18 : 1 / 1.18;
      const r = this.svg.getBoundingClientRect();
      const mx = e.clientX - r.left, my = e.clientY - r.top;
      this.tx = mx - (mx - this.tx) * f;
      this.ty = my - (my - this.ty) * f;
      this.k = Math.max(0.1, Math.min(200, this.k * f));
      this.render();
    }, { passive: false });
    this.el.querySelector("#map-zoom-in").onclick = () => { this.k *= 1.25; this.render(); };
    this.el.querySelector("#map-zoom-out").onclick = () => { this.k /= 1.25; this.render(); };
    this.el.querySelector("#map-fit").onclick = () => this.fit();
  }

  showOldVersions(plotId, on) {
    const p = this.plots.find(x => x.id === plotId);
    if (!p) return;
    p._showOldVersions = on;
    if (on && !p._oldGeoms) {
      // 旧版本几何需要详情接口
      API.get("/api/plots/" + plotId).then(d => {
        p._oldGeoms = d.versions.filter(v => v.version < d.current_version)
          .map(v => v.geometry);
        p.versions = d.versions;
        this.render();
      });
    } else this.render();
  }
}
